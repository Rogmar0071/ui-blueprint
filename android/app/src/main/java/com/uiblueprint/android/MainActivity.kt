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
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.uiblueprint.android.databinding.ActivityMainBinding
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Main screen.
 *
 * Shows a "Record 10 s" button.  When tapped:
 * 1. Requests MediaProjection permission.
 * 2. Starts CaptureService (foreground, mediaProjection type).
 * 3. CaptureService records 10 s and broadcasts CAPTURE_DONE.
 * 4. MainActivity picks up the broadcast and enqueues UploadWorker.
 * 5. A simple session list (in-memory) shows status of each upload.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var captureResultStore: CaptureResultStore
    private val recordingUiStateMachine = RecordingUiStateMachine()
    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val sessions = mutableListOf<SessionItem>()
    private val recordingWatchdogRunnable = Runnable {
        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: return@Runnable
        if (SystemClock.elapsedRealtime() - startedAtMs >= RECORDING_TIMEOUT_MS) {
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
            val transition = recordingUiStateMachine.onPermissionDenied(ERROR_PERMISSION_DENIED)
            renderState(transition.state)
            Toast.makeText(this, ERROR_PERMISSION_DENIED, Toast.LENGTH_SHORT).show()
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
        renderState(recordingUiStateMachine.onIdle().state)
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
        renderState(recordingUiStateMachine.onRecordRequested().state)

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
        renderState(recordingUiStateMachine.onRecordingStarted().state)
        scheduleRecordingWatchdog(RECORDING_TIMEOUT_MS)
        val intent = Intent(this, CaptureService::class.java).apply {
            putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(CaptureService.EXTRA_RESULT_DATA, data)
        }
        try {
            startForegroundService(intent)
        } catch (_: Exception) {
            watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
            captureResultStore.clearRecordingStarted()
            captureResultStore.clearLastResult()
            val transition = recordingUiStateMachine.onCaptureCompleted(
                CaptureDoneEvent(error = ERROR_START_FAILED),
            )
            renderState(transition.state)
            Toast.makeText(this, "Capture failed: $ERROR_START_FAILED", Toast.LENGTH_LONG).show()
        }
    }

    private fun onCaptureDone(clip: File) {
        val meta = buildMeta()
        val sessionId = UploadWorker.enqueue(applicationContext, clip.absolutePath, meta)
        sessions.add(0, SessionItem(sessionId, STATUS_ENQUEUED, clip.name))
        renderSessionList()
        observeWorkerStatus(sessionId)
    }

    private fun buildMeta(): String {
        return JSONObject().apply {
            put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("os_version", Build.VERSION.RELEASE)
            put("sdk_int", Build.VERSION.SDK_INT)
            put("timestamp", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).format(Date()))
        }.toString()
    }

    private fun observeWorkerStatus(sessionId: String) {
        // Each call observes a distinct LiveData keyed by the unique sessionId tag.
        // All observers are automatically removed when the Activity is destroyed.
        WorkManager.getInstance(this)
            .getWorkInfosByTagLiveData(sessionId)
            .observe(this) { workInfos ->
                val info = workInfos?.firstOrNull() ?: return@observe
                val status = when (info.state) {
                    WorkInfo.State.ENQUEUED -> STATUS_ENQUEUED
                    WorkInfo.State.RUNNING -> STATUS_UPLOADING
                    WorkInfo.State.SUCCEEDED -> STATUS_COMPLETED
                    WorkInfo.State.FAILED -> STATUS_FAILED
                    WorkInfo.State.BLOCKED -> STATUS_BLOCKED
                    WorkInfo.State.CANCELLED -> STATUS_CANCELLED
                }
                val idx = sessions.indexOfFirst { it.id == sessionId }
                if (idx >= 0) {
                    sessions[idx] = sessions[idx].copy(status = status)
                    renderSessionList()
                }
            }
    }

    private fun processCaptureDone(event: CaptureDoneEvent) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        captureResultStore.clearRecordingStarted()
        captureResultStore.clearLastResult()

        when (val effect = recordingUiStateMachine.onCaptureCompleted(event).effect) {
            is RecordingUiEffect.EnqueueUpload -> onCaptureDone(File(effect.clipPath))
            is RecordingUiEffect.ShowError -> {
                Toast.makeText(this, "Capture failed: ${effect.message}", Toast.LENGTH_LONG).show()
            }
            RecordingUiEffect.None -> Unit
        }
        renderState(recordingUiStateMachine.state)
    }

    private fun handleRecordingTimeout() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        captureResultStore.clearRecordingStarted()
        captureResultStore.clearLastResult()
        val transition = recordingUiStateMachine.onWatchdogTimeout()
        renderState(transition.state)
        val effect = transition.effect as? RecordingUiEffect.ShowError ?: return
        Toast.makeText(this, "Capture failed: ${effect.message}", Toast.LENGTH_LONG).show()
    }

    private fun recoverPendingCaptureState() {
        captureResultStore.getLastResult()?.let {
            processCaptureDone(it)
            return
        }

        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: run {
            renderState(recordingUiStateMachine.onIdle().state)
            return
        }
        val elapsedMs = SystemClock.elapsedRealtime() - startedAtMs
        if (elapsedMs >= RECORDING_TIMEOUT_MS) {
            handleRecordingTimeout()
            return
        }

        renderState(recordingUiStateMachine.onRecordingStarted().state)
        scheduleRecordingWatchdog(RECORDING_TIMEOUT_MS - elapsedMs)
    }

    private fun scheduleRecordingWatchdog(delayMs: Long) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        watchdogHandler.postDelayed(recordingWatchdogRunnable, delayMs.coerceAtLeast(0L))
    }

    private fun renderState(state: UiRecordingState) {
        binding.btnRecord.isEnabled = state.state == RecordingUiStatus.IDLE
        binding.tvStatus.text = when (state.state) {
            RecordingUiStatus.IDLE -> getString(R.string.status_idle)
            RecordingUiStatus.REQUESTING_PERMISSION -> getString(R.string.status_requesting_permission)
            RecordingUiStatus.RECORDING -> getString(R.string.status_recording)
        }
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

    data class SessionItem(val id: String, val status: String, val label: String)

    companion object {
        private const val RECORDING_TIMEOUT_MS = 15_000L
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
        const val STATUS_ENQUEUED = "enqueued"
        const val STATUS_UPLOADING = "uploading"
        const val STATUS_COMPLETED = "completed"
        const val STATUS_FAILED = "failed"
        const val STATUS_BLOCKED = "blocked"
        const val STATUS_CANCELLED = "cancelled"
    }
}
