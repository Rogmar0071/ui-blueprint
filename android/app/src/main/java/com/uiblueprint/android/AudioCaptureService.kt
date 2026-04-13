package com.uiblueprint.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Foreground service that records audio from the device microphone (no
 * MediaProjection required).
 *
 * **Usage**
 * ---------
 * Start the service with an Intent containing:
 *   - [EXTRA_MAX_DURATION_MS]  — maximum recording duration in ms (default 300 000)
 *
 * **Broadcasts**
 * --------------
 * When recording completes (or an error occurs) the service sends a broadcast
 * [ACTION_AUDIO_CAPTURE_DONE] with extras:
 *   - [EXTRA_AUDIO_PATH]            — absolute path to the .m4a file, or null on error
 *   - [EXTRA_ERROR]                 — error string, or null on success
 *   - [EXTRA_RECORDING_DURATION_MS] — actual elapsed duration in ms
 *
 * Stop early by calling [stop].
 */
class AudioCaptureService : Service() {

    private var mediaRecorder: MediaRecorder? = null
    private var outputFile: File? = null
    private val handler = Handler(Looper.getMainLooper())
    private val stopRunnable = Runnable { finishRecording() }
    private var recordingStartedAtMs: Long? = null
    private var isStopped = false

    /**
     * Single-threaded executor that runs blocking `MediaRecorder` setup off the
     * main/handler thread so that the **event loop is never stalled** by
     * synchronous I/O or codec initialisation.  (D1, D3)
     */
    private val setupExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "AudioCaptureService-setup").also { it.isDaemon = true }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) {
            broadcastDone(audioPath = null, error = "Service started without intent", durationMs = 0)
            stopSelf()
            return START_NOT_STICKY
        }

        val maxDurationMs = intent.getLongExtra(EXTRA_MAX_DURATION_MS, DEFAULT_MAX_DURATION_MS)
        startRecording(maxDurationMs)
        return START_NOT_STICKY
    }

    private fun startRecording(maxDurationMs: Long) {
        val outputDir = getExternalFilesDir(null)
        if (outputDir == null) {
            broadcastDone(audioPath = null, error = "Output directory unavailable", durationMs = 0)
            stopSelf()
            return
        }

        // D1: Create the output file path on the calling thread (fast, no blocking I/O).
        outputFile = File(
            outputDir,
            "audio_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.m4a",
        )

        // Initialise the MediaRecorder instance on the main thread (lightweight).
        try {
            mediaRecorder = MediaRecorder(this).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(128_000)
                setAudioSamplingRate(44_100)
                setOutputFile(outputFile!!.absolutePath)
                // prepare() and start() are blocking — deferred to setupExecutor below.
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to configure MediaRecorder", e)
            mediaRecorder?.release()
            mediaRecorder = null
            broadcastDone(
                audioPath = null,
                error = "Failed to configure audio recorder: ${e.message}",
                durationMs = 0,
            )
            stopSelf()
            return
        }

        // D1/D3: Off-load blocking prepare()/start() to a background thread so the
        // main/handler event loop is never stalled by synchronous codec initialisation.
        setupExecutor.execute {
            // D3: Short yield before blocking setup to let any pending handler messages
            // be processed first (prevents CPU busy-wait on the main thread).
            try {
                AudioProcessingTimeoutHelper.yieldToEventLoop()
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                return@execute
            }

            val mr = mediaRecorder
            if (mr == null) {
                Log.w(TAG, "MediaRecorder released before background setup started; aborting")
                return@execute
            }

            // D2: Wrap prepare() in a timeout to avoid indefinite hangs.
            val prepareResult = AudioProcessingTimeoutHelper.withTimeout(CODEC_TIMEOUT_MS) {
                mr.prepare()
            }
            when (prepareResult) {
                is AudioProcessingTimeoutHelper.TimeoutResult.TimedOut -> {
                    Log.e(TAG, "MediaRecorder.prepare() timed out after ${CODEC_TIMEOUT_MS}ms")
                    handler.post {
                        mediaRecorder?.release()
                        mediaRecorder = null
                        broadcastDone(audioPath = null, error = "Audio setup timed out.", durationMs = 0)
                        stopSelf()
                    }
                    return@execute
                }
                is AudioProcessingTimeoutHelper.TimeoutResult.Error -> {
                    val e = prepareResult.exception
                    val isPermissionError = e is SecurityException
                    Log.e(TAG, if (isPermissionError) "RECORD_AUDIO permission not granted" else "MediaRecorder.prepare() failed", e)
                    handler.post {
                        mediaRecorder?.release()
                        mediaRecorder = null
                        broadcastDone(
                            audioPath = null,
                            error = if (isPermissionError)
                                "RECORD_AUDIO permission is required for audio recording."
                            else
                                "Failed to start audio recording: ${e.message}",
                            durationMs = 0,
                        )
                        stopSelf()
                    }
                    return@execute
                }
                is AudioProcessingTimeoutHelper.TimeoutResult.Success -> Unit // continue
            }

            // D2: Wrap start() in a timeout as well.
            val startResult = AudioProcessingTimeoutHelper.withTimeout(CODEC_TIMEOUT_MS) {
                mr.start()
            }
            when (startResult) {
                is AudioProcessingTimeoutHelper.TimeoutResult.TimedOut -> {
                    Log.e(TAG, "MediaRecorder.start() timed out after ${CODEC_TIMEOUT_MS}ms")
                    handler.post {
                        mediaRecorder?.release()
                        mediaRecorder = null
                        broadcastDone(audioPath = null, error = "Audio start timed out.", durationMs = 0)
                        stopSelf()
                    }
                    return@execute
                }
                is AudioProcessingTimeoutHelper.TimeoutResult.Error -> {
                    val e = startResult.exception
                    Log.e(TAG, "MediaRecorder.start() failed", e)
                    handler.post {
                        mediaRecorder?.release()
                        mediaRecorder = null
                        broadcastDone(
                            audioPath = null,
                            error = "Failed to start audio recording: ${e.message}",
                            durationMs = 0,
                        )
                        stopSelf()
                    }
                    return@execute
                }
                is AudioProcessingTimeoutHelper.TimeoutResult.Success -> Unit // recording started
            }

            // Recording is now active — post the start timestamp and stop-timer to the main thread.
            handler.post {
                recordingStartedAtMs = SystemClock.elapsedRealtime()
                handler.postDelayed(stopRunnable, maxDurationMs)
            }
        }
    }

    private fun finishRecording() {
        if (isStopped) return
        isStopped = true
        handler.removeCallbacks(stopRunnable)

        val durationMs = recordingStartedAtMs
            ?.let { (SystemClock.elapsedRealtime() - it).toInt().coerceAtLeast(0) }
            ?: 0

        var stopFailed = false
        try {
            mediaRecorder?.stop()
        } catch (e: Exception) {
            Log.e(TAG, "MediaRecorder.stop() failed", e)
            stopFailed = true
        } finally {
            mediaRecorder?.release()
            mediaRecorder = null
        }

        if (stopFailed) {
            try {
                outputFile?.delete()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to delete corrupt audio file", e)
            }
            broadcastDone(audioPath = null, error = "Recording finalization failed.", durationMs = durationMs)
            stopSelf()
            return
        }

        val file = outputFile
        if (file != null && file.exists() && file.length() > 0) {
            broadcastDone(audioPath = file.absolutePath, error = null, durationMs = durationMs)
        } else {
            broadcastDone(audioPath = null, error = "Audio file is empty or missing.", durationMs = durationMs)
        }
        stopSelf()
    }

    private fun broadcastDone(audioPath: String?, error: String?, durationMs: Int) {
        sendBroadcast(Intent(ACTION_AUDIO_CAPTURE_DONE).apply {
            audioPath?.let { putExtra(EXTRA_AUDIO_PATH, it) }
            error?.let { putExtra(EXTRA_ERROR, it) }
            putExtra(EXTRA_RECORDING_DURATION_MS, durationMs)
            setPackage(packageName)
        })
    }

    override fun onDestroy() {
        handler.removeCallbacks(stopRunnable)
        // Allow the background setup thread to finish naturally before interrupting,
        // so that any in-progress MediaRecorder operation can release cleanly.
        setupExecutor.shutdown()
        try {
            if (!setupExecutor.awaitTermination(CODEC_TIMEOUT_MS, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                setupExecutor.shutdownNow()
            }
        } catch (_: InterruptedException) {
            setupExecutor.shutdownNow()
            Thread.currentThread().interrupt()
        }
        mediaRecorder?.release()
        super.onDestroy()
    }

    // -------------------------------------------------------------------------
    // **Notification**
    // -------------------------------------------------------------------------

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                getString(R.string.audio_capture_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
    }

    private fun buildNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.status_recording_audio))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()

    companion object {
        private const val TAG = "AudioCaptureService"

        const val ACTION_AUDIO_CAPTURE_DONE = "com.uiblueprint.android.AUDIO_CAPTURE_DONE"
        const val EXTRA_AUDIO_PATH = "audio_path"
        const val EXTRA_ERROR = "error"
        const val EXTRA_RECORDING_DURATION_MS = "recording_duration_ms"
        const val EXTRA_MAX_DURATION_MS = "max_duration_ms"

        private const val CHANNEL_ID = "audio_capture_channel"
        private const val NOTIF_ID = 1002
        private const val DEFAULT_MAX_DURATION_MS = 300_000L

        /**
         * **Maximum time** allowed for a single codec/prepare/start call before it is
         * considered hung and aborted.  (D2)
         */
        private const val CODEC_TIMEOUT_MS = 5_000L

        /**
         * **Stop** — send a stop intent to terminate an active [AudioCaptureService] session.
         */
        fun stop(context: Context) {
            context.stopService(Intent(context, AudioCaptureService::class.java))
        }
    }
}
