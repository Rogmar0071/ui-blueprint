package com.uiblueprint.android

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import java.io.File

/**
 * Saves a recorded mp4 clip into the device Gallery via [MediaStore].
 *
 * After a successful save the clip appears in Gallery under [GALLERY_FOLDER].
 *
 * - Android Q+ (API 29+): uses scoped storage — [MediaStore.Video.Media.RELATIVE_PATH]
 *   and [MediaStore.Video.Media.IS_PENDING] to atomically publish the file.
 * - Android 8–9 (API 26–28): inserts a MediaStore entry and writes bytes through
 *   the [Uri] returned by [android.content.ContentResolver.insert].
 */
object MediaStoreVideoSaver {

    private const val TAG = "MediaStoreVideoSaver"

    /** Gallery sub-folder where clips are stored (visible in Gallery / Files apps). */
    const val GALLERY_FOLDER = "Movies/UIBlueprint"

    /** MIME type for all recorded clips. */
    const val MIME_TYPE = "video/mp4"

    /** User-facing error shown when the MediaStore save fails for any reason. */
    const val ERROR_SAVE_FAILED = "Could not save to Gallery. Please try again."

    // -------------------------------------------------------------------------
    // Result type
    // -------------------------------------------------------------------------

    /**
     * Outcome of [saveClipToGallery].
     */
    sealed class SaveResult {
        /** Clip was inserted successfully. */
        data class Success(
            /** String form of the MediaStore [Uri] for future open/share. */
            val uriString: String,
            /** Display name used in MediaStore (e.g. `clip_20260408_123000.mp4`). */
            val displayName: String,
        ) : SaveResult()

        /** Clip could not be saved. [userMessage] is safe to show in a Toast. */
        data class Failure(val userMessage: String) : SaveResult()
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    /**
     * Copies [clipFile] into MediaStore so it appears in the device Gallery.
     *
     * This runs synchronously on the caller's thread; call from a background thread
     * or coroutine if needed (MainActivity already calls it from the UI thread only
     * after the 10-second clip is fully written).
     *
     * @return [SaveResult.Success] with the new MediaStore [Uri], or
     *         [SaveResult.Failure] with a safe user-facing message.
     */
    fun saveClipToGallery(context: Context, clipFile: File): SaveResult {
        if (!clipFile.exists() || clipFile.length() == 0L) {
            Log.w(TAG, "Clip file missing or empty: ${clipFile.absolutePath}")
            return SaveResult.Failure(ERROR_SAVE_FAILED)
        }
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            saveWithScopedStorage(context, clipFile)
        } else {
            saveLegacy(context, clipFile)
        }
    }

    // -------------------------------------------------------------------------
    // Internal helpers – pure logic extracted for unit testing
    // -------------------------------------------------------------------------

    /**
     * Builds a simple description of the [ContentValues] that will be inserted for a clip.
     * Exposed `internal` so unit tests can verify values without touching the Android framework.
     */
    internal fun buildInsertSpec(displayName: String, sdkInt: Int): ClipInsertSpec = ClipInsertSpec(
        displayName = displayName,
        mimeType = MIME_TYPE,
        relativePathOrNull = if (sdkInt >= Build.VERSION_CODES.Q) GALLERY_FOLDER else null,
        usePendingFlag = sdkInt >= Build.VERSION_CODES.Q,
    )

    // -------------------------------------------------------------------------
    // Private implementation
    // -------------------------------------------------------------------------

    private fun saveWithScopedStorage(context: Context, clipFile: File): SaveResult {
        val spec = buildInsertSpec(clipFile.name, Build.VERSION.SDK_INT)
        val values = spec.toContentValues()

        val uri = context.contentResolver.insert(
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values,
        ) ?: run {
            Log.e(TAG, "ContentResolver.insert returned null (scoped storage)")
            return SaveResult.Failure(ERROR_SAVE_FAILED)
        }

        return try {
            context.contentResolver.openOutputStream(uri)?.use { output ->
                clipFile.inputStream().use { input -> input.copyTo(output) }
            }
            // Publish the item by clearing IS_PENDING.
            val publishValues = ContentValues().apply {
                put(MediaStore.Video.Media.IS_PENDING, 0)
            }
            context.contentResolver.update(uri, publishValues, null, null)
            SaveResult.Success(uriString = uri.toString(), displayName = clipFile.name)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to copy clip into MediaStore", e)
            runCatching { context.contentResolver.delete(uri, null, null) }
            SaveResult.Failure(ERROR_SAVE_FAILED)
        }
    }

    private fun saveLegacy(context: Context, clipFile: File): SaveResult {
        // Pre-Q: no RELATIVE_PATH / IS_PENDING support. Insert an entry and write via stream.
        val spec = buildInsertSpec(clipFile.name, Build.VERSION.SDK_INT)
        val values = spec.toContentValues()

        return try {
            val uri = context.contentResolver.insert(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values,
            ) ?: run {
                Log.e(TAG, "ContentResolver.insert returned null (legacy)")
                return SaveResult.Failure(ERROR_SAVE_FAILED)
            }
            context.contentResolver.openOutputStream(uri)?.use { output ->
                clipFile.inputStream().use { input -> input.copyTo(output) }
            }
            SaveResult.Success(uriString = uri.toString(), displayName = clipFile.name)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to save clip into MediaStore (legacy)", e)
            SaveResult.Failure(ERROR_SAVE_FAILED)
        }
    }
}

/**
 * Pure data holder describing what should be inserted into MediaStore for a clip.
 * Kept separate from [ContentValues] so it can be tested in plain JVM unit tests.
 */
internal data class ClipInsertSpec(
    val displayName: String,
    val mimeType: String,
    /** Non-null on API 29+; null on API 26–28. */
    val relativePathOrNull: String?,
    /** True on API 29+ (IS_PENDING = 1 while writing, 0 after). */
    val usePendingFlag: Boolean,
) {
    fun toContentValues(): ContentValues = ContentValues().apply {
        put(MediaStore.Video.Media.DISPLAY_NAME, displayName)
        put(MediaStore.Video.Media.MIME_TYPE, mimeType)
        relativePathOrNull?.let { put(MediaStore.Video.Media.RELATIVE_PATH, it) }
        if (usePendingFlag) put(MediaStore.Video.Media.IS_PENDING, 1)
    }
}
