package com.uiblueprint.android

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.uiblueprint.android.databinding.ActivityChatBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors

/**
 * Simple chat screen that sends messages to the backend /api/chat endpoint
 * and displays the AI replies in a scrollable log.
 *
 * Authorization: Bearer <BACKEND_API_KEY> is added when the key is non-empty.
 * The key is never logged.
 *
 * Uses [BackendClient] for a shared OkHttpClient with sane timeouts and automatic
 * retry/backoff to handle Render free-plan cold-start latency (502/timeout).
 */
class ChatActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChatBinding
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "ChatActivity-worker") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnSend.setOnClickListener { onSendClicked() }
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    private fun onSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false
        appendLine("You: $message")

        val bodyJson = JSONObject().apply {
            put("message", message)
            put(
                "context",
                JSONObject().apply {
                    put("session_id", JSONObject.NULL)
                    put("domain_profile_id", JSONObject.NULL)
                },
            )
        }.toString()

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        appendLine(getString(R.string.status_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                appendLine("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                appendLine("Error: HTTP ${resp.code}")
                            else -> {
                                val reply = runCatching {
                                    JSONObject(body).getString("reply")
                                }.getOrElse { "Error: unexpected response format" }
                                appendLine("AI: $reply")
                            }
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    appendLine("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    private fun appendLine(line: String) {
        val current = binding.tvChatLog.text
        binding.tvChatLog.text = if (current.isNullOrEmpty()) line else "$current\n$line"
    }
}
