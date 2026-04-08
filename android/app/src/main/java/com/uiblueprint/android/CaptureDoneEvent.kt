package com.uiblueprint.android

import org.json.JSONObject
import java.io.File

data class CaptureDoneEvent(
    val clipPath: String? = null,
    val error: String? = null,
    val recordingDurationMs: Int? = null,
    val schemaVersion: String = SCHEMA_VERSION,
) {
    fun normalized(): CaptureDoneEvent {
        val normalizedClipPath = clipPath?.takeIf { it.isNotBlank() }
        val normalizedError = error?.takeIf { it.isNotBlank() }
        if (normalizedClipPath != null || normalizedError != null) {
            return copy(clipPath = normalizedClipPath, error = normalizedError)
        }
        return copy(clipPath = null, error = ERROR_NO_OUTPUT)
    }

    fun clipLabel(): String? = normalized().clipPath?.let { File(it).name }

    fun toJson(): String = JSONObject().apply {
        put(KEY_SCHEMA_VERSION, schemaVersion)
        clipPath?.let { put(KEY_CLIP_PATH, it) }
        error?.let { put(KEY_ERROR, it) }
        recordingDurationMs?.let { put(KEY_RECORDING_DURATION_MS, it) }
    }.toString()

    companion object {
        const val SCHEMA_VERSION = "v1.0.0"
        const val ERROR_NO_OUTPUT = "Capture completed with no output"
        const val ERROR_TIMEOUT = "Capture timed out. Please try again."

        private const val KEY_SCHEMA_VERSION = "schema_version"
        private const val KEY_CLIP_PATH = "clip_path"
        private const val KEY_ERROR = "error"
        private const val KEY_RECORDING_DURATION_MS = "recording_duration_ms"

        fun fromJson(json: String): CaptureDoneEvent? {
            return runCatching {
                val obj = JSONObject(json)
                CaptureDoneEvent(
                    clipPath = obj.optString(KEY_CLIP_PATH).takeIf { it.isNotBlank() },
                    error = obj.optString(KEY_ERROR).takeIf { it.isNotBlank() },
                    recordingDurationMs = obj.takeIf { it.has(KEY_RECORDING_DURATION_MS) }
                        ?.optInt(KEY_RECORDING_DURATION_MS),
                    schemaVersion = obj.optString(KEY_SCHEMA_VERSION).ifBlank { SCHEMA_VERSION },
                ).normalized()
            }.getOrNull()
        }
    }
}
