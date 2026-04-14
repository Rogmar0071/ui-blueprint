package com.uiblueprint.android

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.IOException
import java.io.InputStream

data class RepoZipChunk(
    val index: Int,
    val totalChunks: Int,
    val bytes: ByteArray,
)

object RepoZipTransferSettings {
    private const val PREFS_NAME = "repo_transfer_prefs"
    private const val PREF_REPO_ZIP_CHUNK_SIZE_MB = "repo_zip_chunk_size_mb"
    private const val PREF_REPO_ZIP_RETRY_COUNT = "repo_zip_retry_count"
    private const val DEFAULT_CHUNK_SIZE_MB = 5
    private const val DEFAULT_RETRY_COUNT = 3
    private const val MIN_CHUNK_SIZE_MB = 1
    private const val MAX_CHUNK_SIZE_MB = 64
    private const val MIN_RETRY_COUNT = 0
    private const val MAX_RETRY_COUNT = 6

    fun getChunkSizeBytes(context: Context): Long {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val configuredMb = prefs.getInt(PREF_REPO_ZIP_CHUNK_SIZE_MB, DEFAULT_CHUNK_SIZE_MB)
        return sanitizeChunkSizeMb(configuredMb).toLong() * 1024L * 1024L
    }

    fun getRetryCount(context: Context): Int {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val configuredValue = prefs.getInt(PREF_REPO_ZIP_RETRY_COUNT, DEFAULT_RETRY_COUNT)
        return sanitizeRetryCount(configuredValue)
    }

    internal fun sanitizeChunkSizeMb(value: Int): Int {
        return value.coerceIn(MIN_CHUNK_SIZE_MB, MAX_CHUNK_SIZE_MB)
    }

    internal fun sanitizeRetryCount(value: Int): Int {
        return value.coerceIn(MIN_RETRY_COUNT, MAX_RETRY_COUNT)
    }
}

object RepoZipChunking {
    fun shouldChunk(totalBytes: Long, chunkSizeBytes: Long): Boolean {
        return totalBytes > chunkSizeBytes && chunkSizeBytes > 0
    }

    fun totalChunks(totalBytes: Long, chunkSizeBytes: Long): Int {
        require(totalBytes >= 0) { "totalBytes must be non-negative" }
        require(chunkSizeBytes > 0) { "chunkSizeBytes must be positive" }
        if (totalBytes == 0L) return 0
        return ((totalBytes + chunkSizeBytes - 1) / chunkSizeBytes).toInt()
    }

    fun splitBytes(data: ByteArray, chunkSizeBytes: Int): List<ByteArray> {
        require(chunkSizeBytes > 0) { "chunkSizeBytes must be positive" }
        if (data.isEmpty()) return emptyList()
        return buildList {
            var offset = 0
            while (offset < data.size) {
                val endExclusive = minOf(offset + chunkSizeBytes, data.size)
                add(data.copyOfRange(offset, endExclusive))
                offset = endExclusive
            }
        }
    }

    fun streamChunks(
        inputStream: InputStream,
        totalBytes: Long,
        chunkSizeBytes: Long,
    ): Sequence<RepoZipChunk> = sequence {
        val totalChunks = totalChunks(totalBytes, chunkSizeBytes)
        if (totalChunks == 0) return@sequence

        val buffer = ByteArray(chunkSizeBytes.toInt())
        var currentIndex = 0
        while (true) {
            val bytesRead = inputStream.read(buffer)
            if (bytesRead == -1) break

            // Copy each chunk into an exact-size byte array so retries can safely
            // reuse the same payload without holding the entire ZIP in memory.
            yield(
                RepoZipChunk(
                    index = currentIndex,
                    totalChunks = totalChunks,
                    bytes = buffer.copyOf(bytesRead),
                ),
            )
            currentIndex += 1
        }
    }

    fun buildContentRange(chunkIndex: Int, chunkSizeBytes: Long, chunkLength: Int, totalBytes: Long): String {
        val start = chunkIndex * chunkSizeBytes
        val end = start + chunkLength - 1
        return "bytes $start-$end/$totalBytes"
    }

    fun resolveDisplayName(contentResolver: ContentResolver, uri: Uri): String {
        return contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (cursor.moveToFirst() && index >= 0) cursor.getString(index) else "repo.zip"
        } ?: "repo.zip"
    }

    fun resolveContentLength(contentResolver: ContentResolver, uri: Uri): Long? {
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (cursor.moveToFirst() && index >= 0 && !cursor.isNull(index)) {
                val size = cursor.getLong(index)
                if (size >= 0) return size
            }
        }

        contentResolver.openAssetFileDescriptor(uri, "r")?.use { descriptor ->
            if (descriptor.length >= 0) return descriptor.length
        }
        return null
    }

    @Throws(IOException::class)
    fun requireInputStream(contentResolver: ContentResolver, uri: Uri): InputStream {
        return contentResolver.openInputStream(uri)
            ?: throw IOException("Cannot open URI: $uri")
    }
}
