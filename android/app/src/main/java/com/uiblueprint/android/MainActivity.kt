package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Build
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.uiblueprint.android.databinding.ActivityMainBinding
import java.io.File
import java.util.UUID

/**
 * Main screen.
 *
 * Shows a "Record 10 s" button.  When tapped:
 * 1. Requests MediaProjection permission.
 * 2. Starts CaptureService (foreground, mediaProjection type).
 * 3. CaptureService records 10 s and broadcasts CAPTURE_DONE.
 * 4. MainActivity inserts the clip into the device Gallery via MediaStore.
 * 5. A simple session list (in-memory) shows [saved] or [failed] status.
 *
 * Backend upload is disabled by default; see [MediaStoreVideoSaver] for local storage.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var captureResultStore: CaptureResultStore
    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val recordingCompletionHelper = RecordingCompletionHelper(RECORDING_TIMEOUT_MS)
    private val sessions = mutableListOf<SessionItem>()
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
        renderSessionList()
        showIdleUi()
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
    }

    override fun onPause() {
        super.onPause()
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
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

    private fun onCaptureDone(clip: File) {
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
            onCaptureDone(File(clipPath))
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
        private const val RECORDING_TIMEOUT_MS = 15_000L
        private const val ERROR_PREFIX = "Capture failed"
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
    }
}
