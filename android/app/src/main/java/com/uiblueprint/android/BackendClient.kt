package com.uiblueprint.android

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Shared OkHttp client and retry helper for backend calls.
 *
 * Timeouts are intentionally generous to survive Render free-plan cold starts
 * (the first request after idle can take 10–20 s before the backend is ready).
 *
 * Retry policy:
 *  - Total attempts = [MAX_ATTEMPTS] (1 original + [MAX_RETRIES] retries)
 *  - Retries on [IOException] (connect/read/write timeout) and HTTP 502/503/504
 *  - No retry on 4xx (client errors – retrying would not help)
 *  - Backoff delays: [BACKOFF_DELAYS_MS]
 */
object BackendClient {

    const val CONNECT_TIMEOUT_S = 15L
    const val READ_TIMEOUT_S = 60L
    const val WRITE_TIMEOUT_S = 60L

    const val MAX_RETRIES = 2
    const val MAX_ATTEMPTS = MAX_RETRIES + 1

    /** Delays (ms) between successive attempts – index 0 before attempt 2, index 1 before attempt 3. */
    val BACKOFF_DELAYS_MS = longArrayOf(1_500L, 3_000L)

    /** HTTP status codes that warrant a retry (server temporarily unavailable). */
    private val RETRYABLE_STATUS_CODES = setOf(502, 503, 504)

    val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(CONNECT_TIMEOUT_S, TimeUnit.SECONDS)
        .readTimeout(READ_TIMEOUT_S, TimeUnit.SECONDS)
        .writeTimeout(WRITE_TIMEOUT_S, TimeUnit.SECONDS)
        .build()

    /**
     * Execute [request] up to [MAX_ATTEMPTS] times.
     *
     * [onRetry] is called on the UI/worker thread **before** the sleep, with
     * the 1-based retry number and total retries so callers can update the UI.
     * It runs on the calling thread (not the main thread), so callers that
     * update Android Views must post to the main thread themselves.
     *
     * Returns the [Response] of the last attempt (caller must close it).
     * Throws [IOException] if all attempts fail with a network error.
     */
    @Throws(IOException::class)
    fun executeWithRetry(
        request: Request,
        onRetry: ((attempt: Int, total: Int) -> Unit)? = null,
    ): Response {
        var lastException: IOException? = null
        for (attempt in 1..MAX_ATTEMPTS) {
            if (attempt > 1) {
                // retryNumber is 1-based: 1 for the first retry, 2 for the second, etc.
                val retryNumber = attempt - 1
                val delay = BACKOFF_DELAYS_MS[retryNumber - 1]
                onRetry?.invoke(retryNumber, MAX_RETRIES)
                try {
                    Thread.sleep(delay)
                } catch (ie: InterruptedException) {
                    Thread.currentThread().interrupt()
                    throw IOException("Interrupted during retry backoff", ie)
                }
            }
            try {
                val response = httpClient.newCall(request).execute()
                val isBodyless = request.body == null
                if (isBodyless && response.code in RETRYABLE_STATUS_CODES && attempt < MAX_ATTEMPTS) {
                    // Close the unusable response body before retrying
                    response.close()
                    continue
                }
                return response
            } catch (e: IOException) {
                lastException = e
                if (attempt == MAX_ATTEMPTS) throw e
            }
        }
        // Should be unreachable, but satisfies the compiler.
        throw lastException ?: IOException("Unknown network error")
    }
}
