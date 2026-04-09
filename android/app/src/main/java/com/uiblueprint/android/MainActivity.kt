package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.uiblueprint.android.databinding.ActivityMainBinding
import org.json.JSONObject
import java.io.File
import java.util.UUID

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
 * Backend upload is disabled by default; see [MediaStoreVideoSaver] for local storage.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var captureResultStore: CaptureResultStore
    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val uploadPollHandler = Handler(Looper.getMainLooper())
    private val recordingCompletionHelper = RecordingCompletionHelper(RECORDING_TIMEOUT_MS)
    private val sessions = mutableListOf<SessionItem>()
    private var lastClipPath: String? = null
    private var lastRecordingDurationMs: Int? = null
    // Cancellation flag: set false in onPause so in-flight poll threads skip their post-back.
    @Volatile private var uploadPollActive = false
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
        renderSessionList()
        showIdleUi()
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
    // Chat button
    // -------------------------------------------------------------------------

    private fun onChatClicked() {
        startActivity(Intent(this, ChatActivity::class.java))
    }

    // -------------------------------------------------------------------------
    // Session list rendering
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
