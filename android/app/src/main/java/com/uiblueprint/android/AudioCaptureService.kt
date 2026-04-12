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

/**
 * Foreground service that records audio from the device microphone (no
 * MediaProjection required).
 *
 * Start the service with an Intent containing:
 *   - [EXTRA_MAX_DURATION_MS]  — maximum recording duration in ms (default 300 000)
 *
 * When recording completes (or an error occurs) the service sends a broadcast
 * [ACTION_AUDIO_CAPTURE_DONE] with extras:
 *   - [EXTRA_AUDIO_PATH]           — absolute path to the .m4a file, or null on error
 *   - [EXTRA_ERROR]                — error string, or null on success
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

        outputFile = File(
            outputDir,
            "audio_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.m4a",
        )

        try {
            mediaRecorder = MediaRecorder(this).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(128_000)
                setAudioSamplingRate(44_100)
                setOutputFile(outputFile!!.absolutePath)
                prepare()
            }
            mediaRecorder!!.start()
            recordingStartedAtMs = SystemClock.elapsedRealtime()
            handler.postDelayed(stopRunnable, maxDurationMs)
        } catch (e: SecurityException) {
            Log.e(TAG, "RECORD_AUDIO permission not granted", e)
            mediaRecorder?.release()
            mediaRecorder = null
            broadcastDone(
                audioPath = null,
                error = "RECORD_AUDIO permission is required for audio recording.",
                durationMs = 0,
            )
            stopSelf()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start audio recording", e)
            mediaRecorder?.release()
            mediaRecorder = null
            broadcastDone(
                audioPath = null,
                error = "Failed to start audio recording: ${e.message}",
                durationMs = 0,
            )
            stopSelf()
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
            } catch (_: Exception) {}
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
        mediaRecorder?.release()
        super.onDestroy()
    }

    // -------------------------------------------------------------------------
    // Notification
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
         * Send a stop intent to terminate an active [AudioCaptureService] session.
         */
        fun stop(context: Context) {
            context.stopService(Intent(context, AudioCaptureService::class.java))
        }
    }
}
