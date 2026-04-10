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
import android.view.View
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.uiblueprint.android.databinding.ActivityMainBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.UUID
import java.util.concurrent.Executors

/**
 * Main screen.
 *
 * Shows a "Record 20 s" button.  When tapped:
 * 1. Requests MediaProjection permission.
 * 2. Starts CaptureService (foreground, mediaProjection type).
 * 3. CaptureService records 20 s and broadcasts CAPTURE_DONE.
 * 4. MainActivity inserts the clip into the device Gallery via MediaStore.
 * 5. A simple session list (in-memory) shows [saved] or [failed] status.
 *
 * A "Pick from Gallery" button allows picking an existing video and uploading
 * it to the backend as a new folder. Tapping any folder item in the list
 * opens [FolderDetailActivity].
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var captureResultStore: CaptureResultStore
    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val uploadPollHandler = Handler(Looper.getMainLooper())
    private val recordingCompletionHelper = RecordingCompletionHelper(RECORDING_TIMEOUT_MS)
    private val sessions = mutableListOf<SessionItem>()
    // Folder items created via gallery pick or from backend
    private val folderItems = mutableListOf<FolderItem>()
    private var lastClipPath: String? = null
    private var lastRecordingDurationMs: Int? = null
    // Cancellation flag: set false in onPause so in-flight poll threads skip their post-back.
    @Volatile private var uploadPollActive = false
    private val galleryExecutor = Executors.newSingleThreadExecutor { Thread(it, "GalleryUpload") }
    private val chatExecutor = Executors.newSingleThreadExecutor { Thread(it, "GlobalChat-worker") }
    private val recordingWatchdogRunnable = Runnable {
        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: return@Runnable
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, SystemClock.elapsedRealtime())) {
            handleRecordingTimeout()
        }
    }

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
        // Permission result handled silently; foreground service notification will still show
        // on older Android versions even without the permission.
        requestScreenCapture()
    }

    // Gallery video picker launcher.
    private val galleryPickLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        if (uri != null) {
            onGalleryVideoPicked(uri)
        } else {
            binding.tvStatus.text = getString(R.string.status_idle)
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        captureResultStore = SharedPreferencesCaptureResultStore(applicationContext)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnRecord.setOnClickListener { onRecordClicked() }
        binding.btnAnalyze.setOnClickListener { onAnalyzeClicked() }
        binding.btnChat.setOnClickListener { onChatClicked() }
        binding.btnPickGallery.setOnClickListener { onPickGalleryClicked() }
        binding.btnSend.setOnClickListener { onChatSendClicked() }
        binding.tvBackendUrl.text = getString(R.string.label_backend_url, BuildConfig.BACKEND_BASE_URL)
        renderSessionList()
        showIdleUi()
        loadGlobalChat()
    }

    override fun onResume() {
        super.onResume()
        uploadPollActive = true
        ContextCompat.registerReceiver(
            this,
            captureReceiver,
            IntentFilter(CaptureService.ACTION_CAPTURE_DONE),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        recoverPendingCaptureState()
    }

    override fun onPause() {
        super.onPause()
        uploadPollActive = false
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        uploadPollHandler.removeCallbacksAndMessages(null)
        unregisterReceiver(captureReceiver)
    }

    override fun onDestroy() {
        super.onDestroy()
        chatExecutor.shutdownNow()
        galleryExecutor.shutdownNow()
    }

    // -------------------------------------------------------------------------
    // Recording flow
    // -------------------------------------------------------------------------

    private fun onRecordClicked() {
        showRequestingPermissionUi()

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
        showRecordingUi()
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
            showIdleUi()
            showCaptureError(ERROR_START_FAILED)
        }
    }

    private fun onCaptureDone(clip: File, recordingDurationMs: Int?) {
        lastClipPath = clip.absolutePath
        lastRecordingDurationMs = recordingDurationMs
        binding.btnAnalyze.isEnabled = true
        val sessionId = UUID.randomUUID().toString()
        when (val result = MediaStoreVideoSaver.saveClipToGallery(applicationContext, clip)) {
            is MediaStoreVideoSaver.SaveResult.Success -> {
                sessions.add(0, SessionItem(sessionId, STATUS_SAVED, result.displayName, result.uriString))
                renderSessionList()
                Toast.makeText(this, "Saved to Gallery", Toast.LENGTH_SHORT).show()
            }
            is MediaStoreVideoSaver.SaveResult.Failure -> {
                sessions.add(0, SessionItem(sessionId, STATUS_FAILED, clip.name))
                renderSessionList()
                showCaptureError(result.userMessage)
            }
        }
    }

    private fun processCaptureDone(event: CaptureDoneEvent) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        showIdleUi()
        try {
            val normalizedEvent = recordingCompletionHelper.normalize(event)
            val error = normalizedEvent.error
            if (error != null) {
                showCaptureError(error)
                return
            }

            val clipPath = normalizedEvent.clipPath
            if (clipPath == null) {
                showCaptureError(CaptureDoneEvent.ERROR_NO_OUTPUT)
                return
            }
            onCaptureDone(File(clipPath), normalizedEvent.recordingDurationMs)
        } finally {
            clearRecoveryState()
        }
    }

    private fun handleRecordingTimeout() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        showIdleUi()
        showCaptureError(CaptureDoneEvent.ERROR_TIMEOUT)
    }

    private fun handlePermissionDenied() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        showIdleUi()
        Toast.makeText(this, ERROR_PERMISSION_DENIED, Toast.LENGTH_SHORT).show()
    }

    private fun recoverPendingCaptureState() {
        // Fallback: if a completed capture result is stored (broadcast was missed),
        // process it now — this also sets lastClipPath via onCaptureDone.
        captureResultStore.getLastResult()?.let {
            processCaptureDone(it)
            return
        }

        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: run {
            showIdleUi()
            return
        }
        val nowMs = SystemClock.elapsedRealtime()
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, nowMs)) {
            handleRecordingTimeout()
            return
        }

        showRecordingUi()
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

    private fun showIdleUi() {
        binding.btnRecord.isEnabled = true
        binding.tvStatus.text = getString(R.string.status_idle)
    }

    private fun showRequestingPermissionUi() {
        binding.btnRecord.isEnabled = false
        binding.tvStatus.text = getString(R.string.status_requesting_permission)
    }

    private fun showRecordingUi() {
        binding.btnRecord.isEnabled = false
        binding.tvStatus.text = getString(R.string.status_recording)
    }

    private fun showCaptureError(message: String) {
        val displayMessage = if (message.startsWith(ERROR_PREFIX, ignoreCase = true)) {
            message
        } else {
            "$ERROR_PREFIX: $message"
        }
        Toast.makeText(this, displayMessage, Toast.LENGTH_LONG).show()
    }

    // -------------------------------------------------------------------------
    // Analyze button
    // -------------------------------------------------------------------------

    private fun onAnalyzeClicked() {
        val clipPath = lastClipPath
        if (clipPath == null) {
            binding.tvStatus.text = getString(R.string.status_no_clip)
            return
        }

        val metaJson = JSONObject().apply {
            put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
            lastRecordingDurationMs?.let { put("recordingDurationMs", it) }
        }.toString()

        val tag = UploadWorker.enqueue(applicationContext, clipPath, metaJson)
        binding.tvStatus.text = getString(R.string.status_upload_enqueued)

        val uploadItem = SessionItem(tag, "enqueued", File(clipPath).name)
        sessions.add(0, uploadItem)
        renderSessionList()

        pollUploadState(tag, 0)
    }

    private fun pollUploadState(tag: String, elapsedMs: Int) {
        if (elapsedMs >= UPLOAD_POLL_MAX_MS) {
            updateUploadSessionStatus(tag, "timeout")
            binding.tvStatus.text = getString(R.string.status_upload_failed)
            return
        }
        uploadPollHandler.postDelayed({
            Thread {
                val state = UploadWorker.getState(applicationContext, tag)
                if (uploadPollActive) {
                    uploadPollHandler.post {
                        updateUploadSessionStatus(tag, state)
                        when (state) {
                            "succeeded" -> binding.tvStatus.text = getString(R.string.status_upload_succeeded)
                            "failed", "cancelled" -> binding.tvStatus.text = getString(R.string.status_upload_failed)
                            else -> pollUploadState(tag, elapsedMs + UPLOAD_POLL_INTERVAL_MS)
                        }
                    }
                }
            }.start()
        }, UPLOAD_POLL_INTERVAL_MS.toLong())
    }

    private fun updateUploadSessionStatus(tag: String, status: String) {
        val idx = sessions.indexOfFirst { it.id == tag }
        if (idx >= 0) {
            sessions[idx] = sessions[idx].copy(status = status)
            renderSessionList()
        }
    }

    // -------------------------------------------------------------------------
    // Gallery pick flow
    // -------------------------------------------------------------------------

    private fun onPickGalleryClicked() {
        binding.tvStatus.text = getString(R.string.status_picking_gallery)
        galleryPickLauncher.launch("video/*")
    }

    /**
     * Called when the user picks a video from the gallery.
     *
     * 1. Creates a new folder via POST /v1/folders
     * 2. Streams the video directly from the content URI via a multipart upload
     *    (no readBytes() — avoids loading the entire file into memory)
     * 3. Uploads via POST /v1/folders/{id}/clip
     * 4. Adds the folder to the in-memory list so it appears in the UI
     */
    private fun onGalleryVideoPicked(uri: Uri) {
        binding.tvStatus.text = getString(R.string.status_gallery_creating_folder)

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        galleryExecutor.execute {
            try {
                // Step 1 — create folder.
                val createRequest = Request.Builder()
                    .url("$baseUrl/v1/folders")
                    .post("{}".toRequestBody("application/json".toMediaType()))
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                val createResp = BackendClient.executeWithRetry(createRequest)
                val folderId = createResp.use { resp ->
                    if (!resp.isSuccessful) throw IOException("Create folder failed: ${resp.code}")
                    JSONObject(resp.body?.string() ?: "{}").getString("id")
                }

                runOnUiThread {
                    binding.tvStatus.text = getString(R.string.status_gallery_uploading_clip)
                    addFolderItem(FolderItem(folderId, "uploading", uri.lastPathSegment ?: "gallery"))
                }

                // Step 2 — build a streaming multipart body (no readBytes()).
                val fileName = uri.lastPathSegment ?: "clip.mp4"
                val clipBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("clip", fileName, uriRequestBody(uri, "video/mp4"))
                    .build()

                // Step 3 — upload clip.
                val uploadRequest = Request.Builder()
                    .url("$baseUrl/v1/folders/$folderId/clip")
                    .post(clipBody)
                    .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                    .build()

                BackendClient.executeWithRetry(uploadRequest).use { resp ->
                    if (!resp.isSuccessful) throw IOException("Clip upload failed: ${resp.code}")
                }

                runOnUiThread {
                    updateFolderItemStatus(folderId, "queued")
                    binding.tvStatus.text = getString(R.string.status_upload_succeeded)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    binding.tvStatus.text = getString(R.string.status_upload_failed)
                    Toast.makeText(
                        this,
                        "Gallery upload failed: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    /**
     * Build an OkHttp [RequestBody] that streams bytes from [uri] via the
     * [ContentResolver] without loading the entire file into memory.
     *
     * @param uri      Content URI of the video to upload.
     * @param mimeType MIME type sent as the Content-Type for this part (e.g.
     *                 "video/mp4"). Should match the actual content; a mismatch
     *                 is passed through as-is without validation.
     *
     * The content length is queried from [OpenableColumns.SIZE] so OkHttp can
     * set an accurate Content-Length header; -1 is returned when the size is
     * unavailable (chunked transfer encoding will be used instead).
     */
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
    // Chat button
    // -------------------------------------------------------------------------

    private fun onChatClicked() {
        startActivity(Intent(this, ChatActivity::class.java))
    }

    // -------------------------------------------------------------------------
    // Global chat (embedded panel)
    // -------------------------------------------------------------------------

    private fun loadGlobalChat() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        chatExecutor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            val messages = runCatching {
                                JSONObject(body).getJSONArray("messages")
                            }.getOrNull()
                            renderChatMessages(messages)
                        }
                    }
                }
            } catch (_: IOException) {
                // Best-effort: keep whatever is currently shown.
            }
        }
    }

    private fun onChatSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false

        val bodyJson = JSONObject().apply {
            put("message", message)
            put(
                "context",
                JSONObject().apply {
                    put("session_id", JSONObject.NULL)
                    put("domain_profile_id", JSONObject.NULL)
                },
            )
        }.toString()

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        chatExecutor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        appendChatLine(getString(R.string.status_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                appendChatLine("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                appendChatLine("Error: HTTP ${resp.code}")
                            else -> {
                                val responseJson = runCatching { JSONObject(body) }.getOrNull()
                                val userMessage = runCatching {
                                    responseJson?.getJSONObject("user_message")?.getString("content")
                                }.getOrNull()
                                val reply = runCatching {
                                    responseJson?.getJSONObject("assistant_message")?.getString("content")
                                }.getOrElse { "Error: unexpected response format" }
                                if (!userMessage.isNullOrBlank()) appendChatLine("You: $userMessage")
                                appendChatLine("AI: $reply")
                            }
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

    private fun renderChatMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            scrollChatToBottom()
            return
        }
        val lines = buildString {
            for (i in 0 until messages.length()) {
                val msg = messages.getJSONObject(i)
                val prefix = if (msg.optString("role") == "user") "You" else "AI"
                if (i > 0) append('\n')
                append(prefix)
                append(": ")
                append(msg.optString("content"))
            }
        }
        binding.tvChatLog.text = lines
        scrollChatToBottom()
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

    // -------------------------------------------------------------------------
    // Folder items list
    // -------------------------------------------------------------------------

    private fun addFolderItem(item: FolderItem) {
        folderItems.add(0, item)
        renderFolderList()
    }

    private fun updateFolderItemStatus(folderId: String, status: String) {
        val idx = folderItems.indexOfFirst { it.id == folderId }
        if (idx >= 0) {
            folderItems[idx] = folderItems[idx].copy(status = status)
            renderFolderList()
        }
    }

    private fun renderFolderList() {
        val container = binding.llFolderList
        container.removeAllViews()
        for (item in folderItems) {
            val tv = TextView(this).apply {
                text = "📁 ${item.label}  [${item.status}]"
                textSize = 12f
                typeface = android.graphics.Typeface.MONOSPACE
                setPadding(0, 4, 0, 4)
                isClickable = true
                isFocusable = true
                setOnClickListener {
                    val intent = Intent(this@MainActivity, FolderDetailActivity::class.java)
                    intent.putExtra(FolderDetailActivity.EXTRA_FOLDER_ID, item.id)
                    startActivity(intent)
                }
            }
            container.addView(tv)
        }
    }

    // -------------------------------------------------------------------------
    // Session list rendering (legacy upload sessions)
    // -------------------------------------------------------------------------

    private fun renderSessionList() {
        if (sessions.isEmpty()) {
            binding.tvSessions.visibility = View.GONE
            return
        }
        binding.tvSessions.visibility = View.VISIBLE
        binding.tvSessions.text = sessions.joinToString("\n") { "• ${it.label}  [${it.status}]" }
    }

    data class SessionItem(val id: String, val status: String, val label: String, val uri: String? = null)
    data class FolderItem(val id: String, val status: String, val label: String)

    companion object {
        private const val RECORDING_TIMEOUT_MS = 30_000L
        private const val ERROR_PREFIX = "Capture failed"
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
        private const val UPLOAD_POLL_INTERVAL_MS = 1_000
        private const val UPLOAD_POLL_MAX_MS = 60_000
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
    }
}
