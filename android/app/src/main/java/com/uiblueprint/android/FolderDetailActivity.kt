package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.OpenableColumns
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.uiblueprint.android.databinding.ActivityFolderDetailBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Per-folder/project detail screen.
 *
 * Shows:
 *  - Folder title, status, and UUID
 *  - Action buttons: Record clip, Pick from gallery, Analyze
 *    (clips are automatically associated with this project/folder)
 *  - Jobs list (type + status + progress)
 *  - Artifacts list (type)
 *  - Per-folder chat (GET/POST /v1/folders/{id}/messages)
 *
 * Requires [EXTRA_FOLDER_ID] to be set in the launching Intent.
 *
 * Authorization: Bearer <BACKEND_API_KEY> is added when non-empty.
 * The API key is never logged.
 */
class FolderDetailActivity : AppCompatActivity() {

    private lateinit var binding: ActivityFolderDetailBinding
    private lateinit var folderId: String
    private lateinit var captureResultStore: CaptureResultStore

    private val executor = Executors.newSingleThreadExecutor { Thread(it, "FolderDetail-worker") }
    private val clipUploadExecutor = Executors.newSingleThreadExecutor { Thread(it, "ClipUpload-worker") }

    /** Guards against overlapping concurrent folder-load network requests. */
    private val isFolderLoading = AtomicBoolean(false)

    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val pollHandler = Handler(Looper.getMainLooper())
    private val pollRunnable = object : Runnable {
        override fun run() {
            loadFolder()
            pollHandler.postDelayed(this, POLL_INTERVAL_MS)
        }
    }
    private val recordingCompletionHelper = RecordingCompletionHelper(RECORDING_TIMEOUT_MS)

    private var lastClipPath: String? = null
    private var lastRecordingDurationMs: Int? = null
    private var lastGalleryUri: Uri? = null

    /** clip_object_key from the last successful loadFolder() response. */
    private var folderClipObjectKey: String? = null

    // MediaProjection permission launcher.
    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startCapture(result.resultCode, result.data!!)
        } else {
            handlePermissionDenied()
        }
    }

    // Notification permission launcher (Android 13+).
    private val notificationLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) {
        requestScreenCapture()
    }

    // Gallery video picker launcher.
    private val galleryPickLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        if (uri != null) {
            uploadClipFromUri(uri)
        } else {
            setActionStatus(null)
        }
    }

    // Receives CAPTURE_DONE broadcast from CaptureService.
    private val captureReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            processCaptureDone(
                CaptureDoneEvent(
                    clipPath = intent.getStringExtra(CaptureService.EXTRA_CLIP_PATH),
                    error = intent.getStringExtra(CaptureService.EXTRA_ERROR),
                    recordingDurationMs = intent.takeIf {
                        it.hasExtra(CaptureService.EXTRA_RECORDING_DURATION_MS)
                    }?.getIntExtra(CaptureService.EXTRA_RECORDING_DURATION_MS, 0),
                    schemaVersion = intent.getStringExtra(CaptureService.EXTRA_SCHEMA_VERSION)
                        ?: CaptureDoneEvent.SCHEMA_VERSION,
                ),
            )
        }
    }

    private val recordingWatchdogRunnable = Runnable {
        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: return@Runnable
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, SystemClock.elapsedRealtime())) {
            handleRecordingTimeout()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        captureResultStore = SharedPreferencesCaptureResultStore(applicationContext)
        binding = ActivityFolderDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        folderId = intent.getStringExtra(EXTRA_FOLDER_ID)
            ?: run {
                finish()
                return
            }

        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = getString(R.string.folder_detail_title)

        binding.btnRecord.setOnClickListener { onRecordClicked() }
        binding.btnPickGallery.setOnClickListener { onPickGalleryClicked() }
        binding.btnAnalyze.setOnClickListener { onAnalyzeClicked() }
        binding.btnSend.setOnClickListener { onSendClicked() }
        binding.tvFolderTitle.text = getString(R.string.folder_detail_title)
        binding.tvFolderStatus.text = getString(R.string.folder_loading)
        binding.tvFolderId.text = getString(R.string.label_folder_id, folderId)

        loadFolder()
        loadMessages()
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.registerReceiver(
            this,
            captureReceiver,
            IntentFilter(CaptureService.ACTION_CAPTURE_DONE),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        recoverPendingCaptureState()
        // Refresh folder state on resume; renderFolder() will restart polling
        // if there is still an active analyze job.
        loadFolder()
    }

    override fun onPause() {
        super.onPause()
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        stopPolling()
        unregisterReceiver(captureReceiver)
    }

    override fun onDestroy() {
        super.onDestroy()
        stopPolling()
        executor.shutdownNow()
        clipUploadExecutor.shutdownNow()
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.menu_folder_detail, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_rename_project -> {
                showRenameDialog()
                true
            }
            R.id.action_delete_project -> {
                showDeleteDialog()
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    // -------------------------------------------------------------------------
    // Rename / Delete from overflow menu
    // -------------------------------------------------------------------------

    private fun showRenameDialog() {
        val currentTitle = binding.tvFolderTitle.text?.toString() ?: ""
        val editText = EditText(this).apply {
            hint = getString(R.string.dialog_rename_hint)
            setText(currentTitle)
            selectAll()
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_rename_title))
            .setView(editText)
            .setPositiveButton(getString(R.string.dialog_btn_rename)) { _, _ ->
                val newTitle = editText.text.toString().trim()
                if (newTitle.isBlank()) {
                    Toast.makeText(this, getString(R.string.error_title_empty), Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                callRenameFolder(newTitle)
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    private fun showDeleteDialog() {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_delete_title))
            .setMessage(getString(R.string.dialog_delete_message))
            .setPositiveButton(getString(R.string.dialog_btn_delete)) { _, _ ->
                callDeleteFolder()
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    private fun callRenameFolder(newTitle: String) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val body = JSONObject().put("title", newTitle).toString()
            .toRequestBody("application/json".toMediaType())
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId")
            .patch(body)
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadFolder()
                        } else {
                            Toast.makeText(this, getString(R.string.error_rename_failed), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (_: IOException) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_rename_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun callDeleteFolder() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId")
            .delete()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            finish()
                        } else {
                            Toast.makeText(this, getString(R.string.error_delete_failed), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (_: IOException) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_delete_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Recording flow
    // -------------------------------------------------------------------------

    private fun onRecordClicked() {
        setActionStatus(getString(R.string.status_requesting_permission))
        binding.btnRecord.isEnabled = false

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            requestScreenCapture()
        }
    }

    private fun requestScreenCapture() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        projectionLauncher.launch(mpm.createScreenCaptureIntent())
    }

    private fun startCapture(resultCode: Int, data: Intent) {
        captureResultStore.clearLastResult()
        captureResultStore.markRecordingStarted(SystemClock.elapsedRealtime())
        setActionStatus(getString(R.string.status_recording))
        scheduleRecordingWatchdog(RECORDING_TIMEOUT_MS)
        val intent = Intent(this, CaptureService::class.java).apply {
            putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(CaptureService.EXTRA_RESULT_DATA, data)
        }
        try {
            startForegroundService(intent)
        } catch (_: Exception) {
            watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
            clearRecoveryState()
            resetActionButtons()
            Toast.makeText(this, ERROR_START_FAILED, Toast.LENGTH_LONG).show()
        }
    }

    private fun processCaptureDone(event: CaptureDoneEvent) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        try {
            val normalizedEvent = recordingCompletionHelper.normalize(event)
            val error = normalizedEvent.error
            if (error != null) {
                resetActionButtons()
                Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                return
            }

            val clipPath = normalizedEvent.clipPath
            if (clipPath == null) {
                resetActionButtons()
                Toast.makeText(this, CaptureDoneEvent.ERROR_NO_OUTPUT, Toast.LENGTH_LONG).show()
                return
            }
            onCaptureDone(File(clipPath), normalizedEvent.recordingDurationMs)
        } finally {
            clearRecoveryState()
        }
    }

    private fun onCaptureDone(clip: File, recordingDurationMs: Int?) {
        lastClipPath = clip.absolutePath
        lastRecordingDurationMs = recordingDurationMs
        // Do NOT enable Analyze here — uploadClipFromFile() will auto-queue
        // an analyze job on the backend, so polling will track progress.

        // Save clip to gallery
        when (val result = MediaStoreVideoSaver.saveClipToGallery(applicationContext, clip)) {
            is MediaStoreVideoSaver.SaveResult.Success ->
                Toast.makeText(this, getString(R.string.status_saved_to_gallery), Toast.LENGTH_SHORT).show()
            is MediaStoreVideoSaver.SaveResult.Failure ->
                Toast.makeText(this, result.userMessage, Toast.LENGTH_LONG).show()
        }

        // Upload clip to this project/folder
        uploadClipFromFile(clip, recordingDurationMs)
    }

    private fun handleRecordingTimeout() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        resetActionButtons()
        Toast.makeText(this, CaptureDoneEvent.ERROR_TIMEOUT, Toast.LENGTH_LONG).show()
    }

    private fun handlePermissionDenied() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        resetActionButtons()
        Toast.makeText(this, ERROR_PERMISSION_DENIED, Toast.LENGTH_SHORT).show()
    }

    private fun recoverPendingCaptureState() {
        captureResultStore.getLastResult()?.let {
            processCaptureDone(it)
            return
        }

        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: run {
            return
        }
        val nowMs = SystemClock.elapsedRealtime()
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, nowMs)) {
            handleRecordingTimeout()
            return
        }

        setActionStatus(getString(R.string.status_recording))
        binding.btnRecord.isEnabled = false
        scheduleRecordingWatchdog(recordingCompletionHelper.remainingTimeoutMs(startedAtMs, nowMs))
    }

    private fun clearRecoveryState() {
        captureResultStore.clearLastResult()
        captureResultStore.clearRecordingStarted()
    }

    private fun scheduleRecordingWatchdog(delayMs: Long) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        watchdogHandler.postDelayed(recordingWatchdogRunnable, delayMs)
    }

    // -------------------------------------------------------------------------
    // Gallery pick flow
    // -------------------------------------------------------------------------

    private fun onPickGalleryClicked() {
        setActionStatus(getString(R.string.status_picking_gallery))
        galleryPickLauncher.launch("video/*")
    }

    /**
     * Upload a video from a content URI to this project's folder on the backend.
     * No new folder is created — the clip is associated with [folderId].
     */
    private fun uploadClipFromUri(uri: Uri) {
        setActionStatus(getString(R.string.status_gallery_uploading_clip))

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        clipUploadExecutor.execute {
            try {
                val fileName = uri.lastPathSegment ?: "clip.mp4"
                val clipBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("clip", fileName, uriRequestBody(uri, "video/mp4"))
                    .build()

                val request = Request.Builder()
                    .url("$baseUrl/v1/folders/$folderId/clip")
                    .post(clipBody)
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                BackendClient.executeWithRetry(request).use { resp ->
                    if (!resp.isSuccessful) throw IOException("Upload failed: ${resp.code}")
                }

                runOnUiThread {
                    lastGalleryUri = uri
                    // Do NOT enable Analyze — upload already enqueued an analyze
                    // job on the backend. Polling will reflect progress and re-enable
                    // the button only after the job finishes.
                    setActionStatus(null)
                    binding.tvFolderStatus.text = getString(R.string.label_folder_status, "queued")
                    Toast.makeText(this, getString(R.string.status_upload_succeeded), Toast.LENGTH_SHORT).show()
                    loadFolder()
                    startPolling()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    setActionStatus(null)
                    Toast.makeText(
                        this,
                        "Upload failed: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    /**
     * Upload a recorded clip [File] to this project's folder on the backend.
     */
    private fun uploadClipFromFile(clip: File, recordingDurationMs: Int?) {
        setActionStatus(getString(R.string.status_gallery_uploading_clip))

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        clipUploadExecutor.execute {
            try {
                val metaJson = JSONObject().apply {
                    put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
                    recordingDurationMs?.let { put("recordingDurationMs", it) }
                }.toString()

                val clipBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "clip", clip.name,
                        clip.asRequestBody("video/mp4".toMediaType()),
                    )
                    .addFormDataPart("meta", metaJson)
                    .build()

                val request = Request.Builder()
                    .url("$baseUrl/v1/folders/$folderId/clip")
                    .post(clipBody)
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                BackendClient.executeWithRetry(request).use { resp ->
                    if (!resp.isSuccessful) throw IOException("Upload failed: ${resp.code}")
                }

                runOnUiThread {
                    setActionStatus(null)
                    resetActionButtons()
                    binding.tvFolderStatus.text = getString(R.string.label_folder_status, "queued")
                    Toast.makeText(this, getString(R.string.status_upload_succeeded), Toast.LENGTH_SHORT).show()
                    loadFolder()
                    startPolling()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    setActionStatus(null)
                    resetActionButtons()
                    Toast.makeText(
                        this,
                        "Clip upload failed: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    private fun uriRequestBody(uri: Uri, mimeType: String): RequestBody {
        val contentLen = contentResolver.query(
            uri,
            arrayOf(OpenableColumns.SIZE),
            null,
            null,
            null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) {
                cursor.getLong(cursor.getColumnIndexOrThrow(OpenableColumns.SIZE))
            } else {
                -1L
            }
        } ?: -1L

        return object : RequestBody() {
            override fun contentType() = mimeType.toMediaType()
            override fun contentLength() = contentLen
            override fun writeTo(sink: BufferedSink) {
                contentResolver.openInputStream(uri)?.use { stream ->
                    sink.writeAll(stream.source())
                } ?: throw IOException("Could not open input stream for $uri")
            }
        }
    }

    // -------------------------------------------------------------------------
    // Analyze button
    // -------------------------------------------------------------------------

    private fun onAnalyzeClicked() {
        // Safety guard: button should already be disabled while a job is active,
        // but check here to prevent accidental duplicate jobs.
        if (!binding.btnAnalyze.isEnabled) {
            Toast.makeText(this, getString(R.string.toast_analyze_already_running), Toast.LENGTH_SHORT).show()
            return
        }

        // If no clip exists on the backend yet, ask the user to upload first.
        val hasClip = lastClipPath != null || lastGalleryUri != null || !folderClipObjectKey.isNullOrBlank()
        if (!hasClip) {
            Toast.makeText(this, getString(R.string.toast_upload_clip_first), Toast.LENGTH_SHORT).show()
            return
        }

        // Re-queue an analyze job without re-uploading the clip.
        enqueueAnalyzeJob()
    }

    /**
     * POST /v1/folders/{id}/jobs with type=analyze to (re-)run analysis.
     * Does NOT upload the clip — upload only happens via Record or Pick from Gallery.
     */
    private fun enqueueAnalyzeJob() {
        binding.btnAnalyze.isEnabled = false

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val body = JSONObject().put("type", "analyze").toString()
            .toRequestBody("application/json".toMediaType())
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId/jobs")
            .post(body)
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadFolder()
                            startPolling()
                        } else {
                            binding.btnAnalyze.isEnabled = true
                            Toast.makeText(
                                this,
                                "Failed to start analyze: HTTP ${resp.code}",
                                Toast.LENGTH_LONG,
                            ).show()
                        }
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    binding.btnAnalyze.isEnabled = true
                    Toast.makeText(this, "Failed to start analyze: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // UI helpers
    // -------------------------------------------------------------------------

    private fun setActionStatus(message: String?) {
        if (message == null) {
            binding.tvActionStatus.visibility = View.GONE
        } else {
            binding.tvActionStatus.text = message
            binding.tvActionStatus.visibility = View.VISIBLE
        }
    }

    private fun resetActionButtons() {
        binding.btnRecord.isEnabled = true
        setActionStatus(null)
    }

    private fun startPolling() {
        pollHandler.removeCallbacks(pollRunnable)
        pollHandler.postDelayed(pollRunnable, POLL_INTERVAL_MS)
    }

    private fun stopPolling() {
        pollHandler.removeCallbacks(pollRunnable)
    }

    private fun hasActiveAnalyzeJob(jobs: JSONArray?): Boolean {
        if (jobs == null) return false
        for (i in 0 until jobs.length()) {
            val job = jobs.getJSONObject(i)
            if (job.optString("type") in ACTIVE_JOB_TYPES &&
                job.optString("status") in ACTIVE_JOB_STATUSES
            ) {
                return true
            }
        }
        return false
    }

    // -------------------------------------------------------------------------
    // Load folder detail
    // -------------------------------------------------------------------------

    private fun loadFolder() {
        // Skip if a load is already in progress to avoid overlapping requests.
        if (!isFolderLoading.compareAndSet(false, true)) return

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                val response = BackendClient.executeWithRetry(request)
                response.use { resp ->
                    val bodyStr = resp.body?.string() ?: ""
                    runOnUiThread {
                        isFolderLoading.set(false)
                        if (resp.isSuccessful) {
                            val json = runCatching { JSONObject(bodyStr) }.getOrNull()
                            if (json != null) renderFolder(json)
                            else binding.tvFolderStatus.text = getString(R.string.folder_load_error)
                        } else {
                            binding.tvFolderStatus.text = getString(
                                R.string.folder_load_error,
                            )
                        }
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    isFolderLoading.set(false)
                    binding.tvFolderStatus.text = getString(R.string.folder_load_error)
                }
            }
        }
    }

    private fun renderFolder(json: JSONObject) {
        val title = json.optString("title", "").trim()
        binding.tvFolderTitle.text = if (title.isNotEmpty()) title else getString(R.string.label_untitled_project)
        binding.tvFolderStatus.text = getString(R.string.label_folder_status, json.optString("status", "?"))
        binding.tvFolderId.text = getString(R.string.label_folder_id, folderId)

        // Track clip_object_key from server so Analyze can be re-run across sessions.
        val serverClipKey = json.optString("clip_object_key", "")
        if (serverClipKey.isNotBlank()) {
            folderClipObjectKey = serverClipKey
        }

        // Jobs (newest-first – backend already returns desc)
        val jobs = json.optJSONArray("jobs")
        binding.tvJobs.text = if (jobs == null || jobs.length() == 0) {
            getString(R.string.folder_no_jobs)
        } else {
            buildString {
                for (i in 0 until jobs.length()) {
                    val job = jobs.getJSONObject(i)
                    appendLine(
                        "${job.optString("type")}  –  ${job.optString("status")} " +
                            "(${job.optInt("progress")}%)",
                    )
                }
            }.trim()
        }

        // Artifacts
        val artifacts = json.optJSONArray("artifacts")
        binding.tvArtifacts.text = if (artifacts == null || artifacts.length() == 0) {
            getString(R.string.folder_no_artifacts)
        } else {
            buildString {
                for (i in 0 until artifacts.length()) {
                    val a = artifacts.getJSONObject(i)
                    appendLine("• ${a.optString("type")}")
                }
            }.trim()
        }

        // Manage Analyze button state and polling based on active analyze jobs.
        val hasActiveJob = hasActiveAnalyzeJob(jobs)
        if (hasActiveJob) {
            // hasActiveJob being true guarantees jobs is non-null and contains an active
            // analyze job. Use the status of the first matching active job to set the
            // button label. The backend returns jobs in insertion order (oldest first),
            // so the active job found first is the earliest still-running one.
            val activeAnalyzeStatus = (0 until jobs.length())
                .map { jobs.getJSONObject(it) }
                .firstOrNull {
                    it.optString("type") in ACTIVE_JOB_TYPES &&
                        it.optString("status") in ACTIVE_JOB_STATUSES
                }
                ?.optString("status") ?: "queued" // unreachable: guaranteed by hasActiveJob
            binding.btnAnalyze.isEnabled = false
            binding.btnAnalyze.text = if (activeAnalyzeStatus == "running") {
                getString(R.string.btn_analyze_running)
            } else {
                getString(R.string.btn_analyze_queued)
            }
            startPolling()
        } else {
            stopPolling()
            // Re-enable Analyze when a clip exists (from this session or a previous upload).
            val hasClip = lastClipPath != null || lastGalleryUri != null || !folderClipObjectKey.isNullOrBlank()
            binding.btnAnalyze.isEnabled = hasClip
            binding.btnAnalyze.text = getString(R.string.btn_analyze)
        }
    }

    // -------------------------------------------------------------------------
    // Load and render chat messages
    // -------------------------------------------------------------------------

    private fun loadMessages() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId/messages")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                val response = BackendClient.executeWithRetry(request)
                response.use { resp ->
                    val bodyStr = resp.body?.string() ?: ""
                    if (resp.isSuccessful) {
                        val messages = runCatching { JSONObject(bodyStr) }.getOrNull()
                            ?.optJSONArray("messages")
                        runOnUiThread { renderMessages(messages) }
                    }
                }
            } catch (_: IOException) {
                // Best-effort; chat log stays empty on error
            }
        }
    }

    private fun renderMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) return
        val sb = StringBuilder()
        for (i in 0 until messages.length()) {
            val msg = messages.getJSONObject(i)
            val role = msg.optString("role", "?")
            val content = msg.optString("content", "")
            val prefix = if (role == "user") "You" else "AI"
            if (sb.isNotEmpty()) sb.append("\n")
            sb.append("$prefix: $content")
        }
        binding.tvChatLog.text = sb.toString()
        scrollChatToBottom()
    }

    // -------------------------------------------------------------------------
    // Send chat message
    // -------------------------------------------------------------------------

    private fun onSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false
        appendChatLine("You: $message")

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val bodyJson = JSONObject().put("message", message).toString()
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId/messages")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        appendChatLine(getString(R.string.folder_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    val bodyStr = resp.body?.string() ?: ""
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            val reply = runCatching {
                                JSONObject(bodyStr)
                                    .getJSONObject("assistant_message")
                                    .getString("content")
                            }.getOrElse { "Error: unexpected response" }
                            appendChatLine("AI: $reply")
                        } else {
                            appendChatLine("Error: HTTP ${resp.code}")
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    appendChatLine("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    private fun appendChatLine(line: String) {
        val current = binding.tvChatLog.text
        binding.tvChatLog.text = if (current.isNullOrEmpty()) line else "$current\n$line"
        scrollChatToBottom()
    }

    private fun scrollChatToBottom() {
        binding.scrollChat.post {
            binding.scrollChat.fullScroll(View.FOCUS_DOWN)
        }
    }

    companion object {
        const val EXTRA_FOLDER_ID = "folder_id"
        private const val RECORDING_TIMEOUT_MS = 30_000L
        private const val POLL_INTERVAL_MS = 2_000L
        private val ACTIVE_JOB_STATUSES = setOf("queued", "running")
        private val ACTIVE_JOB_TYPES = setOf("analyze", "analyze_optional")
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
    }
}
