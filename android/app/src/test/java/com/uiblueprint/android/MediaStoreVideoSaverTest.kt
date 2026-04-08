package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [MediaStoreVideoSaver] and [ClipInsertSpec].
 *
 * These tests cover the pure logic that builds the MediaStore insert specification,
 * which can be exercised without an Android runtime or Robolectric.
 */
class MediaStoreVideoSaverTest {

    // -------------------------------------------------------------------------
    // Constants
    // -------------------------------------------------------------------------

    @Test
    fun `gallery folder is under Movies subdirectory`() {
        assertTrue(
            "GALLERY_FOLDER must start with 'Movies/'",
            MediaStoreVideoSaver.GALLERY_FOLDER.startsWith("Movies/"),
        )
    }

    @Test
    fun `mime type is video mp4`() {
        assertEquals("video/mp4", MediaStoreVideoSaver.MIME_TYPE)
    }

    @Test
    fun `error save failed message is safe and matches contract`() {
        assertEquals("Could not save to Gallery. Please try again.", MediaStoreVideoSaver.ERROR_SAVE_FAILED)
        assertFalse(
            "Error message must not contain 'Exception'",
            MediaStoreVideoSaver.ERROR_SAVE_FAILED.contains("Exception"),
        )
        assertFalse(
            "Error message must not contain 'java.'",
            MediaStoreVideoSaver.ERROR_SAVE_FAILED.contains("java."),
        )
    }

    // -------------------------------------------------------------------------
    // ClipInsertSpec — pure logic for API version branching
    // -------------------------------------------------------------------------

    @Test
    fun `buildInsertSpec on API 29 uses relative path and pending flag`() {
        val spec = MediaStoreVideoSaver.buildInsertSpec("clip_20260408_123000.mp4", sdkInt = 29)

        assertEquals("clip_20260408_123000.mp4", spec.displayName)
        assertEquals("video/mp4", spec.mimeType)
        assertEquals(MediaStoreVideoSaver.GALLERY_FOLDER, spec.relativePathOrNull)
        assertTrue("API 29+ must use pending flag", spec.usePendingFlag)
    }

    @Test
    fun `buildInsertSpec on API 34 uses relative path and pending flag`() {
        val spec = MediaStoreVideoSaver.buildInsertSpec("clip_20260408_123000.mp4", sdkInt = 34)

        assertNotNull(spec.relativePathOrNull)
        assertTrue(spec.usePendingFlag)
    }

    @Test
    fun `buildInsertSpec on API 28 has no relative path and no pending flag`() {
        val spec = MediaStoreVideoSaver.buildInsertSpec("clip_20260408_120000.mp4", sdkInt = 28)

        assertEquals("clip_20260408_120000.mp4", spec.displayName)
        assertEquals("video/mp4", spec.mimeType)
        assertNull("Pre-Q must not set RELATIVE_PATH", spec.relativePathOrNull)
        assertFalse("Pre-Q must not use pending flag", spec.usePendingFlag)
    }

    @Test
    fun `buildInsertSpec on API 26 (minSdk) has no relative path and no pending flag`() {
        val spec = MediaStoreVideoSaver.buildInsertSpec("clip_20260101_000000.mp4", sdkInt = 26)

        assertNull(spec.relativePathOrNull)
        assertFalse(spec.usePendingFlag)
    }

    @Test
    fun `buildInsertSpec display name matches provided file name`() {
        val name = "clip_20260408_093015.mp4"
        val spec = MediaStoreVideoSaver.buildInsertSpec(name, sdkInt = 33)
        assertEquals(name, spec.displayName)
    }

    // -------------------------------------------------------------------------
    // SaveResult — sealed class sanity checks
    // -------------------------------------------------------------------------

    @Test
    fun `SaveResult Success holds uri string and display name`() {
        val result = MediaStoreVideoSaver.SaveResult.Success(
            uriString = "content://media/external/video/media/42",
            displayName = "clip_20260408_123000.mp4",
        )
        assertEquals("content://media/external/video/media/42", result.uriString)
        assertEquals("clip_20260408_123000.mp4", result.displayName)
    }

    @Test
    fun `SaveResult Failure holds user-safe message`() {
        val result = MediaStoreVideoSaver.SaveResult.Failure(MediaStoreVideoSaver.ERROR_SAVE_FAILED)
        assertEquals(MediaStoreVideoSaver.ERROR_SAVE_FAILED, result.userMessage)
        assertFalse(result.userMessage.contains("Exception"))
    }
}
