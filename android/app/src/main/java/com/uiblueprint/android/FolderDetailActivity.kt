package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
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
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.bottomsheet.BottomSheetDialog
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

    /** True while AudioCaptureService is actively recording. */
    private var isAudioRecording = false

    /** Last full folder JSON response; used by openLastAnalysisArtifact(). */
    private var lastFolderJson: JSONObject? = null

    /** clip_object_key from the last successful loadFolder() response. */
    private var folderClipObjectKey: String? = null

    // Adapters for expandable sections
    private val jobAdapter = JobItemAdapter()
    private val artifactAdapter = ArtifactItemAdapter { artifact ->
        ArtifactViewerRouter.open(this, JSONObject().apply {
            put("id", artifact.id)
            put("type", artifact.type)
            put("object_key", artifact.objectKey)
            artifact.url?.let { put("url", it) }
        }, folderId)
    }
    private val supportingDataAdapter = ArtifactItemAdapter { artifact ->
        ArtifactViewerRouter.open(this, JSONObject().apply {
            put("id", artifact.id)
            put("type", artifact.type)
            put("object_key", artifact.objectKey)
            artifact.url?.let { put("url", it) }
        }, folderId)
    }

    // Chat adapter for per-folder chat (edit hidden; copy/share work same as MainActivity)
    private val chatMessages = mutableListOf<ChatMessageAdapter.Message>()
    private val chatAdapter = ChatMessageAdapter(object : ChatMessageAdapter.MessageActionListener {
        override fun onCopyMessage(message: ChatMessageAdapter.Message) {
            val clipboard = ContextCompat.getSystemService(
                this@FolderDetailActivity, android.content.ClipboardManager::class.java,
            )
            clipboard?.setPrimaryClip(
                android.content.ClipData.newPlainText("chat_message", message.content),
            )
            Toast.makeText(this@FolderDetailActivity, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
        }
        override fun onShareMessage(message: ChatMessageAdapter.Message) {
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_TEXT, message.content)
            }
            startActivity(Intent.createChooser(intent, getString(R.string.share_via)))
        }
        override fun onEditMessage(message: ChatMessageAdapter.Message) {
            // Edit not supported in folder chat
        }
        override fun onSelectionChanged(selectedCount: Int) {}
    })

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
        checkRecordAudioThenCapture()
    }

    // RECORD_AUDIO permission launcher for screen recording (with-audio flow).
    private val recordAudioForScreenLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        // Proceed with screen capture regardless of outcome; CaptureService
        // falls back to video-only if RECORD_AUDIO is not granted.
        requestScreenCapture()
    }

    // RECORD_AUDIO permission launcher for standalone audio recording.
    private val recordAudioLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startAudioCapture()
        } else {
            resetAudioRecordButton()
            Toast.makeText(this, "Microphone permission is required for audio recording.", Toast.LENGTH_SHORT).show()
        }
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

    // Generic file/document/audio attach launcher.
    private val folderAttachPickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        if (uri != null) {
            uploadClipFromUri(uri)
        } else {
            setActionStatus(null)
        }
    }

    // Repo ZIP picker launcher.
    private val repoZipPickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        if (uri != null) {
            uploadRepoZip(uri)
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

    // Receives AUDIO_CAPTURE_DONE broadcast from AudioCaptureService.
    private val audioCaptureReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            isAudioRecording = false
            resetAudioRecordButton()
            val audioPath = intent.getStringExtra(AudioCaptureService.EXTRA_AUDIO_PATH)
            val error = intent.getStringExtra(AudioCaptureService.EXTRA_ERROR)
            val durationMs = intent.getIntExtra(AudioCaptureService.EXTRA_RECORDING_DURATION_MS, 0)
            if (error != null || audioPath == null) {
                Toast.makeText(
                    this@FolderDetailActivity,
                    error ?: getString(R.string.status_audio_upload_failed),
                    Toast.LENGTH_LONG,
                ).show()
            } else {
                uploadAudioFromFile(audioPath, durationMs)
            }
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

        val initialTitle = intent.getStringExtra(EXTRA_FOLDER_TITLE)
        if (!initialTitle.isNullOrBlank()) {
            binding.tvFolderTitle.text = initialTitle
            supportActionBar?.title = initialTitle
        }

        binding.btnRecord.setOnClickListener { onRecordClicked() }
        binding.btnPickGallery.setOnClickListener { onPickGalleryClicked() }
        binding.btnAnalyze.setOnClickListener { showAnalyzeBottomSheet() }
        binding.btnRecordAudio.setOnClickListener { onRecordAudioClicked() }
        binding.btnSend.setOnClickListener { onSendClicked() }
        binding.btnAttach.setOnClickListener { showAttachBottomSheet() }
        if (initialTitle.isNullOrBlank()) {
            binding.tvFolderTitle.text = getString(R.string.folder_detail_title)
        }
        binding.tvFolderStatus.text = getString(R.string.folder_loading)
        binding.tvFolderId.text = getString(R.string.label_folder_id, folderId)

        // Set up expandable sections
        binding.rvJobs.layoutManager = LinearLayoutManager(this)
        binding.rvJobs.adapter = jobAdapter
        binding.rvArtifacts.layoutManager = LinearLayoutManager(this)
        binding.rvArtifacts.adapter = artifactAdapter
        binding.rvSupportingData.layoutManager = LinearLayoutManager(this)
        binding.rvSupportingData.adapter = supportingDataAdapter

        // Set up folder chat RecyclerView
        binding.rvFolderChatMessages.layoutManager = LinearLayoutManager(this).apply {
            stackFromEnd = true
        }
        binding.rvFolderChatMessages.adapter = chatAdapter

        toggleSection(binding.headerJobs, binding.rvJobs, binding.ivJobsChevron)
        toggleSection(binding.headerArtifacts, binding.rvArtifacts, binding.ivArtifactsChevron)
        toggleSection(binding.headerSupportingData, binding.rvSupportingData, binding.ivSupportingDataChevron)

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
        ContextCompat.registerReceiver(
            this,
            audioCaptureReceiver,
            IntentFilter(AudioCaptureService.ACTION_AUDIO_CAPTURE_DONE),
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
        unregisterReceiver(audioCaptureReceiver)
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
            checkRecordAudioThenCapture()
        }
    }

    private fun checkRecordAudioThenCapture() {
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            requestScreenCapture()
        } else {
            recordAudioForScreenLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
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

    // -------------------------------------------------------------------------
    // Audio recording flow
    // -------------------------------------------------------------------------

    private fun onRecordAudioClicked() {
        if (isAudioRecording) {
            // Already recording — stop it.
            AudioCaptureService.stop(this)
            isAudioRecording = false
            resetAudioRecordButton()
            return
        }

        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startAudioCapture()
        } else {
            recordAudioLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun startAudioCapture() {
        isAudioRecording = true
        setActionStatus(getString(R.string.status_recording_audio))
        binding.btnRecordAudio.isEnabled = false

        val intent = Intent(this, AudioCaptureService::class.java)
        try {
            startForegroundService(intent)
        } catch (_: Exception) {
            isAudioRecording = false
            resetAudioRecordButton()
            Toast.makeText(this, "Failed to start audio recording.", Toast.LENGTH_SHORT).show()
        }
    }

    private fun resetAudioRecordButton() {
        binding.btnRecordAudio.isEnabled = true
        if (!isAudioRecording) {
            setActionStatus(null)
        }
    }

    /**
     * Upload a recorded audio [File] path to this project's folder on the backend.
     */
    private fun uploadAudioFromFile(audioPath: String, durationMs: Int) {
        val file = File(audioPath)
        if (!file.exists()) {
            Toast.makeText(this, getString(R.string.status_audio_upload_failed), Toast.LENGTH_SHORT).show()
            return
        }

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        clipUploadExecutor.execute {
            try {
                val audioBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "audio", file.name,
                        file.asRequestBody("audio/mp4".toMediaType()),
                    )
                    .build()

                val request = Request.Builder()
                    .url("$baseUrl/v1/folders/$folderId/audio")
                    .post(audioBody)
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                BackendClient.executeWithRetry(request).use { resp ->
                    if (!resp.isSuccessful) throw IOException("Audio upload failed: ${resp.code}")
                }

                runOnUiThread {
                    Toast.makeText(this, getString(R.string.status_audio_upload_succeeded), Toast.LENGTH_SHORT).show()
                    loadFolder()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(
                        this,
                        getString(R.string.status_audio_upload_failed),
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
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
    private fun enqueueAnalyzeJob(options: JSONObject = JSONObject()) {
        binding.btnAnalyze.isEnabled = false

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val bodyJson = JSONObject().put("type", "analyze")
        if (options.length() > 0) bodyJson.put("options", options)
        val body = bodyJson.toString().toRequestBody("application/json".toMediaType())
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
        lastFolderJson = json
        val rawTitle = json.optString("title", "").trim()
        val title = if (rawTitle == "null") "" else rawTitle
        val displayTitle = if (title.isNotEmpty()) title else getString(R.string.label_untitled_project)
        binding.tvFolderTitle.text = displayTitle
        supportActionBar?.title = displayTitle
        binding.tvFolderStatus.text = getString(R.string.label_folder_status, json.optString("status", "?"))
        binding.tvFolderId.text = getString(R.string.label_folder_id, folderId)

        // Track clip_object_key from server so Analyze can be re-run across sessions.
        val serverClipKey = json.optString("clip_object_key", "")
        if (serverClipKey.isNotBlank()) {
            folderClipObjectKey = serverClipKey
        }

        // Jobs
        val jobs = json.optJSONArray("jobs")
        val jobList = mutableListOf<JobItem>()
        if (jobs != null) {
            for (i in 0 until jobs.length()) {
                val job = jobs.getJSONObject(i)
                jobList.add(
                    JobItem(
                        id = job.optString("id", i.toString()),
                        type = job.optString("type", "?"),
                        status = job.optString("status", "?"),
                        progress = job.optInt("progress", 0),
                        createdAt = job.optString("created_at", ""),
                    )
                )
            }
        }
        jobAdapter.submitList(jobList)
        binding.tvJobsCount.text = "${jobList.size}"

        // Artifacts – split into main and supporting
        val artifacts = json.optJSONArray("artifacts")
        val mainArtifacts = mutableListOf<ArtifactItem>()
        val supportingArtifacts = mutableListOf<ArtifactItem>()
        if (artifacts != null) {
            for (i in 0 until artifacts.length()) {
                val a = artifacts.getJSONObject(i)
                val item = ArtifactItem(
                    id = a.optString("id", i.toString()),
                    type = a.optString("type", "?"),
                    objectKey = a.optString("object_key", ""),
                    url = a.optString("url", "").takeIf { it.isNotBlank() },
                )
                val t = item.type
                if (t.contains("segment") || t.contains("manifest") ||
                    t.contains("baseline") || t.contains("supporting")
                ) {
                    supportingArtifacts.add(item)
                } else {
                    mainArtifacts.add(item)
                }
            }
        }
        artifactAdapter.submitList(mainArtifacts)
        supportingDataAdapter.submitList(supportingArtifacts)
        binding.tvArtifactsCount.text = "${mainArtifacts.size}"
        binding.tvSupportingDataCount.text = "${supportingArtifacts.size}"

        // Manage Analyze button state and polling based on active analyze jobs.
        val hasActiveJob = hasActiveAnalyzeJob(jobs)
        if (hasActiveJob && jobs != null) {
            val activeAnalyzeStatus = (0 until jobs.length())
                .map { jobs.getJSONObject(it) }
                .firstOrNull {
                    it.optString("type") in ACTIVE_JOB_TYPES &&
                        it.optString("status") in ACTIVE_JOB_STATUSES
                }
                ?.optString("status") ?: "queued"
            binding.btnAnalyze.isEnabled = false
            binding.btnAnalyze.text = if (activeAnalyzeStatus == "running") {
                getString(R.string.btn_analyze_running)
            } else {
                getString(R.string.btn_analyze_queued)
            }
            startPolling()
        } else {
            stopPolling()
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
        val list = mutableListOf<ChatMessageAdapter.Message>()
        for (i in 0 until messages.length()) {
            val msg = messages.getJSONObject(i)
            val role = msg.optString("role", "user")
            val content = msg.optString("content", "")
            val id = msg.optString("id", i.toString())
            list.add(ChatMessageAdapter.Message(id = id, role = role, content = content))
        }
        chatMessages.clear()
        chatMessages.addAll(list)
        chatAdapter.submitList(chatMessages.toList())
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

    // Counter for locally generated message IDs to ensure uniqueness
    private var localMessageCounter = 0

    private fun appendChatLine(line: String) {
        val id = "local_${++localMessageCounter}"
        val role: String
        val content: String
        when {
            line.startsWith("You: ") -> {
                role = "user"
                content = line.removePrefix("You: ")
            }
            line.startsWith("AI: ") -> {
                role = "assistant"
                content = line.removePrefix("AI: ")
            }
            else -> {
                role = "assistant"
                content = line
            }
        }
        chatMessages.add(ChatMessageAdapter.Message(id = id, role = role, content = content))
        chatAdapter.submitList(chatMessages.toList())
        scrollChatToBottom()
    }

    private fun scrollChatToBottom() {
        binding.rvFolderChatMessages.post {
            val count = chatAdapter.itemCount
            if (count > 0) {
                binding.rvFolderChatMessages.scrollToPosition(count - 1)
            }
        }
    }

    // -------------------------------------------------------------------------
    // Expandable section toggle
    // -------------------------------------------------------------------------

    private fun toggleSection(header: View, rv: RecyclerView, chevron: ImageView) {
        header.setOnClickListener {
            if (rv.visibility == View.VISIBLE) {
                rv.visibility = View.GONE
                chevron.rotation = -90f
            } else {
                rv.visibility = View.VISIBLE
                chevron.rotation = 0f
            }
        }
    }

    // -------------------------------------------------------------------------
    // Analyze bottom sheet
    // -------------------------------------------------------------------------

    private fun showAnalyzeBottomSheet() {
        val sheet = BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.bottom_sheet_analyze, null)
        sheet.setContentView(view)

        val hasClip = !folderClipObjectKey.isNullOrBlank()

        // Clip info label
        val tvCurrentClip = view.findViewById<TextView>(R.id.tvCurrentClip)
        val tvNoClipHint = view.findViewById<TextView>(R.id.tvNoClipHint)
        if (hasClip) {
            val clipName = folderClipObjectKey!!.substringAfterLast('/')
            tvCurrentClip.text = getString(R.string.label_current_clip, clipName)
            tvCurrentClip.visibility = View.VISIBLE
            tvNoClipHint.visibility = View.GONE
        } else {
            tvCurrentClip.visibility = View.GONE
            tvNoClipHint.visibility = View.VISIBLE
        }

        // Pick / Change clip buttons
        val btnPickClip = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnPickClip)
        val btnChangeClip = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnChangeClip)
        val btnAnalyzeStandard = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnAnalyzeStandard)
        val btnAnalyzeRerun = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnAnalyzeRerun)

        if (hasClip) {
            btnChangeClip.visibility = View.VISIBLE
            btnPickClip.visibility = View.GONE
            btnAnalyzeStandard.visibility = View.VISIBLE
            btnAnalyzeRerun.visibility = View.VISIBLE
        } else {
            btnPickClip.visibility = View.VISIBLE
            btnChangeClip.visibility = View.GONE
            btnAnalyzeStandard.visibility = View.GONE
            btnAnalyzeRerun.visibility = View.GONE
        }

        btnPickClip.setOnClickListener {
            sheet.dismiss()
            galleryPickLauncher.launch("video/*")
        }
        btnChangeClip.setOnClickListener {
            sheet.dismiss()
            galleryPickLauncher.launch("video/*")
        }

        // Additional analysis master toggle
        val switchAdditionalAnalysis = view.findViewById<androidx.appcompat.widget.SwitchCompat>(R.id.switchAdditionalAnalysis)
        val layoutAdditionalOptions = view.findViewById<LinearLayout>(R.id.layoutAdditionalOptions)
        switchAdditionalAnalysis.setOnCheckedChangeListener { _, isChecked ->
            layoutAdditionalOptions.visibility = if (isChecked) View.VISIBLE else View.GONE
        }

        val checkboxKeyframes = view.findViewById<CheckBox>(R.id.checkboxKeyframes)
        val checkboxOcr = view.findViewById<CheckBox>(R.id.checkboxOcr)
        val checkboxTranscript = view.findViewById<CheckBox>(R.id.checkboxTranscript)
        val checkboxEvents = view.findViewById<CheckBox>(R.id.checkboxEvents)
        val checkboxSegmentSummaries = view.findViewById<CheckBox>(R.id.checkboxSegmentSummaries)

        btnAnalyzeStandard.setOnClickListener {
            sheet.dismiss()
            val opts = JSONObject().apply {
                val aa = JSONObject().apply {
                    val enabled = switchAdditionalAnalysis.isChecked
                    put("enabled", enabled)
                    put("keyframes", enabled && checkboxKeyframes.isChecked)
                    put("ocr", enabled && checkboxOcr.isChecked)
                    put("transcript", enabled && checkboxTranscript.isChecked)
                    put("events", enabled && checkboxEvents.isChecked)
                    put("segment_summaries", enabled && checkboxSegmentSummaries.isChecked)
                }
                put("additional_analysis", aa)
            }
            enqueueAnalyzeJob(opts)
        }
        btnAnalyzeRerun.setOnClickListener {
            sheet.dismiss()
            enqueueAnalyzeJobForced(JSONObject())
        }
        view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnViewLastAnalysis)
            .setOnClickListener {
                sheet.dismiss()
                openLastAnalysisArtifact()
            }
        sheet.show()
    }

    private fun enqueueAnalyzeJobForced(options: JSONObject = JSONObject()) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        binding.btnAnalyze.isEnabled = false
        val bodyJson = JSONObject().put("type", "analyze")
        if (options.length() > 0) bodyJson.put("options", options)
        val body = bodyJson.toString().toRequestBody("application/json".toMediaType())
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

    private fun openLastAnalysisArtifact() {
        val folderJson = lastFolderJson ?: run {
            Toast.makeText(this, getString(R.string.toast_no_analysis_yet), Toast.LENGTH_SHORT).show()
            return
        }
        val artifacts = folderJson.optJSONArray("artifacts")
        if (artifacts == null || artifacts.length() == 0) {
            Toast.makeText(this, getString(R.string.toast_no_analysis_yet), Toast.LENGTH_SHORT).show()
            return
        }
        val analysisTypes = setOf("analysis_md", "analysis_json")
        val analysisArtifact = (0 until artifacts.length())
            .map { artifacts.getJSONObject(it) }
            .lastOrNull { it.optString("type") in analysisTypes }
        if (analysisArtifact == null) {
            Toast.makeText(this, getString(R.string.toast_no_analysis_yet), Toast.LENGTH_SHORT).show()
            return
        }
        ArtifactViewerRouter.open(this, analysisArtifact, folderId)
    }

    // -------------------------------------------------------------------------
    // Attach bottom sheet
    // -------------------------------------------------------------------------

    private fun showAttachBottomSheet() {
        val sheet = BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.bottom_sheet_attach, null)
        sheet.setContentView(view)

        view.findViewById<ImageButton>(R.id.btnAttachGallery).setOnClickListener {
            sheet.dismiss()
            galleryPickLauncher.launch("image/*")
        }
        view.findViewById<ImageButton>(R.id.btnAttachVideo).setOnClickListener {
            sheet.dismiss()
            galleryPickLauncher.launch("video/*")
        }
        view.findViewById<ImageButton>(R.id.btnAttachRepoZip).setOnClickListener {
            sheet.dismiss()
            repoZipPickerLauncher.launch("application/zip")
        }
        view.findViewById<ImageButton>(R.id.btnAttachCamera).setOnClickListener {
            sheet.dismiss()
            Toast.makeText(this, "Camera coming soon", Toast.LENGTH_SHORT).show()
        }
        view.findViewById<ImageButton>(R.id.btnAttachDocument).setOnClickListener {
            sheet.dismiss()
            folderAttachPickerLauncher.launch("*/*")
        }
        view.findViewById<ImageButton>(R.id.btnAttachAudio).setOnClickListener {
            sheet.dismiss()
            folderAttachPickerLauncher.launch("audio/*")
        }
        // Show video row in FolderDetailActivity
        view.findViewById<View>(R.id.rowAttach2)?.visibility = View.VISIBLE
        sheet.show()
    }

    private fun uploadRepoZip(uri: Uri) {
        setActionStatus(getString(R.string.status_uploading_repo))
        clipUploadExecutor.execute {
            try {
                val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
                val apiKey = BuildConfig.BACKEND_API_KEY

                val fileName = contentResolver.query(uri, null, null, null, null)?.use { c ->
                    val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (c.moveToFirst() && idx >= 0) c.getString(idx) else "repo.zip"
                } ?: "repo.zip"

                val inputStream = contentResolver.openInputStream(uri)
                    ?: throw IOException("Cannot open URI: $uri")

                val requestBody = object : RequestBody() {
                    override fun contentType() = "application/zip".toMediaType()
                    override fun writeTo(sink: BufferedSink) {
                        sink.writeAll(inputStream.source())
                    }
                }

                val multipart = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("repo", fileName, requestBody)
                    .build()

                val request = Request.Builder()
                    .url("$baseUrl/v1/folders/$folderId/repo")
                    .post(multipart)
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            setActionStatus(getString(R.string.status_repo_upload_succeeded))
                            Toast.makeText(
                                this,
                                getString(R.string.toast_repo_analysis_queued),
                                Toast.LENGTH_SHORT,
                            ).show()
                            loadFolder()
                        } else {
                            setActionStatus(getString(R.string.status_repo_upload_failed))
                            Toast.makeText(
                                this,
                                "${getString(R.string.status_repo_upload_failed)} HTTP ${resp.code}",
                                Toast.LENGTH_LONG,
                            ).show()
                        }
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    setActionStatus(getString(R.string.status_repo_upload_failed))
                    Toast.makeText(
                        this,
                        "${getString(R.string.status_repo_upload_failed)}: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    companion object {
        const val EXTRA_FOLDER_ID = "folder_id"
        const val EXTRA_FOLDER_TITLE = "folder_title"
        private const val RECORDING_TIMEOUT_MS = 30_000L
        private const val POLL_INTERVAL_MS = 2_000L
        private val ACTIVE_JOB_STATUSES = setOf("queued", "running")
        private val ACTIVE_JOB_TYPES = setOf("analyze", "analyze_optional")
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
    }
}
