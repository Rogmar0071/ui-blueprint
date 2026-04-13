package com.uiblueprint.android

/**
 * Pure helper for timeout-bounded execution of blocking audio-processing steps.
 *
 * Extracted from [AudioCaptureService] so that the core logic can be exercised
 * by plain JVM unit tests without an Android runtime.
 *
 * **Design Notes**
 * ----------------
 * - [withTimeout] spawns a daemon thread for the blocking call and uses
 *   [Thread.join] to wait at most [timeoutMs] ms.  If the thread is still
 *   alive after the join, the result is [TimeoutResult.TimedOut].
 * - The daemon thread is interrupted after a timeout so it has the opportunity
 *   to exit; note that native blocking calls (e.g. `MediaRecorder.prepare()`)
 *   may not respond to interrupts, but the **caller** is unblocked immediately.
 * - [yieldToEventLoop] inserts a short sleep so that calling threads (including
 *   Android's main/handler thread) can service pending messages before a
 *   long-running setup begins — preventing CPU busy-wait starvation.
 */
object AudioProcessingTimeoutHelper {

    /** **Default timeout** for codec/prepare operations (millis). */
    const val DEFAULT_CODEC_TIMEOUT_MS = 5_000L

    /** **Yield delay** inserted before blocking setup to avoid event-loop starvation. */
    const val QUEUE_POLL_YIELD_DELAY_MS = 100L

    // -------------------------------------------------------------------------
    // **Timeout Wrapper**
    // -------------------------------------------------------------------------

    /**
     * Executes [block] on a separate daemon thread, waiting up to [timeoutMs]
     * milliseconds for it to complete.
     *
     * **Returns**
     * - [TimeoutResult.Success] — [block] completed within [timeoutMs].
     * - [TimeoutResult.TimedOut] — [block] did not complete in time.
     * - [TimeoutResult.Error] — [block] threw an exception.
     */
    fun <T> withTimeout(timeoutMs: Long, block: () -> T): TimeoutResult<T> {
        var value: T? = null
        var thrown: Exception? = null

        val worker = Thread {
            try {
                value = block()
            } catch (e: Exception) {
                thrown = e
            }
        }.apply { isDaemon = true }

        worker.start()
        worker.join(timeoutMs)

        return when {
            worker.isAlive -> {
                worker.interrupt()
                TimeoutResult.TimedOut(timeoutMs)
            }
            thrown != null -> TimeoutResult.Error(thrown!!)
            else -> @Suppress("UNCHECKED_CAST") TimeoutResult.Success(value as T)
        }
    }

    /**
     * Inserts a [QUEUE_POLL_YIELD_DELAY_MS] sleep so the current thread
     * **yields control** before starting a long-running blocking operation,
     * preventing CPU busy-wait on the calling thread.
     */
    @Throws(InterruptedException::class)
    fun yieldToEventLoop() {
        Thread.sleep(QUEUE_POLL_YIELD_DELAY_MS)
    }

    // -------------------------------------------------------------------------
    // **Result Type**
    // -------------------------------------------------------------------------

    sealed class TimeoutResult<out T> {
        data class Success<T>(val value: T) : TimeoutResult<T>()
        data class TimedOut(val timeoutMs: Long) : TimeoutResult<Nothing>()
        data class Error(val exception: Exception) : TimeoutResult<Nothing>()
    }
}
