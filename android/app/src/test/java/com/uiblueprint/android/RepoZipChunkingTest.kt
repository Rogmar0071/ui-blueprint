package com.uiblueprint.android

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.ByteArrayInputStream

class RepoZipChunkingTest {

    @Test
    fun `split bytes preserves order across chunks`() {
        val chunks = RepoZipChunking.splitBytes("abcdefghij".encodeToByteArray(), chunkSizeBytes = 4)

        assertEquals(3, chunks.size)
        assertArrayEquals("abcd".encodeToByteArray(), chunks[0])
        assertArrayEquals("efgh".encodeToByteArray(), chunks[1])
        assertArrayEquals("ij".encodeToByteArray(), chunks[2])
    }

    @Test
    fun `stream chunks emits sequential indexes and expected totals`() {
        val stream = ByteArrayInputStream("hello-world".encodeToByteArray())

        val chunks = RepoZipChunking.streamChunks(
            inputStream = stream,
            totalBytes = 11,
            chunkSizeBytes = 5,
        ).toList()

        assertEquals(listOf(0, 1, 2), chunks.map { it.index })
        assertEquals(listOf(3, 3, 3), chunks.map { it.totalChunks })
        assertEquals(listOf("hello", "-worl", "d"), chunks.map { String(it.bytes) })
    }

    @Test
    fun `chunk size setting is clamped to supported range`() {
        assertEquals(1, RepoZipTransferSettings.sanitizeChunkSizeMb(0))
        assertEquals(5, RepoZipTransferSettings.sanitizeChunkSizeMb(5))
        assertEquals(64, RepoZipTransferSettings.sanitizeChunkSizeMb(128))
    }
}
