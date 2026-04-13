package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [AudioProcessingTimeoutHelper].
 *
 * **Coverage**
 * ------------
 * - [AudioProcessingTimeoutHelper.withTimeout] returns [AudioProcessingTimeoutHelper.TimeoutResult.Success]
 *   when the block finishes within the allowed time.
 * - [AudioProcessingTimeoutHelper.withTimeout] returns [AudioProcessingTimeoutHelper.TimeoutResult.TimedOut]
 *   when the block exceeds the allowed time.
 * - [AudioProcessingTimeoutHelper.withTimeout] returns [AudioProcessingTimeoutHelper.TimeoutResult.Error]
 *   when the block throws an exception.
 * - [AudioProcessingTimeoutHelper.QUEUE_POLL_YIELD_DELAY_MS] is a positive value so the
 *   event-loop yield is a real pause, not a no-op.
 * - [AudioProcessingTimeoutHelper.DEFAULT_CODEC_TIMEOUT_MS] is bounded to prevent
 *   indefinitely hanging codec calls.
 */
class AudioProcessingTimeoutHelperTest {

    // -------------------------------------------------------------------------
    // **Constants**
    // -------------------------------------------------------------------------

    @Test
    fun `yield delay is positive to ensure a real event-loop pause`() {
        assertTrue(
            "QUEUE_POLL_YIELD_DELAY_MS must be > 0",
            AudioProcessingTimeoutHelper.QUEUE_POLL_YIELD_DELAY_MS > 0L,
        )
    }

    @Test
    fun `default codec timeout is bounded between 1s and 30s`() {
        val timeout = AudioProcessingTimeoutHelper.DEFAULT_CODEC_TIMEOUT_MS
        assertTrue("Timeout must be at least 1 000ms", timeout >= 1_000L)
        assertTrue("Timeout must be at most 30 000ms", timeout <= 30_000L)
    }

    // -------------------------------------------------------------------------
    // **withTimeout — Success**
    // -------------------------------------------------------------------------

    @Test
    fun `withTimeout returns Success when block finishes in time`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(500L) { 42 }

        assertTrue(result is AudioProcessingTimeoutHelper.TimeoutResult.Success)
        assertEquals(42, (result as AudioProcessingTimeoutHelper.TimeoutResult.Success).value)
    }

    @Test
    fun `withTimeout Success value is preserved correctly`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(500L) { "hello" }

        assertEquals(
            "hello",
            (result as AudioProcessingTimeoutHelper.TimeoutResult.Success).value,
        )
    }

    // -------------------------------------------------------------------------
    // **withTimeout — Timeout**
    // -------------------------------------------------------------------------

    @Test
    fun `withTimeout returns TimedOut when block exceeds deadline`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(100L) {
            Thread.sleep(5_000L) // sleeps far longer than the 100ms budget
        }

        assertTrue(
            "Expected TimedOut but got $result",
            result is AudioProcessingTimeoutHelper.TimeoutResult.TimedOut,
        )
    }

    @Test
    fun `withTimeout TimedOut carries the timeout value`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(50L) {
            Thread.sleep(5_000L)
        }

        assertEquals(
            50L,
            (result as AudioProcessingTimeoutHelper.TimeoutResult.TimedOut).timeoutMs,
        )
    }

    // -------------------------------------------------------------------------
    // **withTimeout — Error**
    // -------------------------------------------------------------------------

    @Test
    fun `withTimeout returns Error when block throws`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(500L) {
            throw IllegalStateException("codec boom")
        }

        assertTrue(
            "Expected Error but got $result",
            result is AudioProcessingTimeoutHelper.TimeoutResult.Error,
        )
    }

    @Test
    fun `withTimeout Error preserves original exception message`() {
        val result = AudioProcessingTimeoutHelper.withTimeout(500L) {
            throw RuntimeException("prepare failed")
        }

        val error = result as AudioProcessingTimeoutHelper.TimeoutResult.Error
        assertEquals("prepare failed", error.exception.message)
    }
}
