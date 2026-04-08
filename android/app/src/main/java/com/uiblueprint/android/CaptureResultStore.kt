package com.uiblueprint.android

import android.content.Context
import androidx.core.content.edit

interface CaptureResultStore {
    fun saveLastResult(event: CaptureDoneEvent)
    fun getLastResult(): CaptureDoneEvent?
    fun clearLastResult()
    fun markRecordingStarted(startedAtMs: Long)
    fun getRecordingStartedAtMs(): Long?
    fun clearRecordingStarted()
}

class SharedPreferencesCaptureResultStore(context: Context) : CaptureResultStore {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun saveLastResult(event: CaptureDoneEvent) {
        prefs.edit {
            putString(KEY_LAST_RESULT, event.normalized().toJson())
        }
    }

    override fun getLastResult(): CaptureDoneEvent? =
        prefs.getString(KEY_LAST_RESULT, null)?.let(CaptureDoneEvent::fromJson)

    override fun clearLastResult() {
        prefs.edit { remove(KEY_LAST_RESULT) }
    }

    override fun markRecordingStarted(startedAtMs: Long) {
        prefs.edit { putLong(KEY_RECORDING_STARTED_AT_MS, startedAtMs) }
    }

    override fun getRecordingStartedAtMs(): Long? =
        prefs.takeIf { it.contains(KEY_RECORDING_STARTED_AT_MS) }
            ?.getLong(KEY_RECORDING_STARTED_AT_MS, 0L)

    override fun clearRecordingStarted() {
        prefs.edit { remove(KEY_RECORDING_STARTED_AT_MS) }
    }

    companion object {
        private const val PREFS_NAME = "capture_result_store"
        private const val KEY_LAST_RESULT = "last_result"
        private const val KEY_RECORDING_STARTED_AT_MS = "recording_started_at_ms"
    }
}
