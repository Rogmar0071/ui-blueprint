package com.uiblueprint.android

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.uiblueprint.android.databinding.ActivityChatBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
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

    override fun onResume() {
        super.onResume()
        loadMessages()
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    private fun loadMessages() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                binding.tvChatLog.text = "Unauthorized: check BACKEND_API_KEY"
                            !resp.isSuccessful ->
                                appendLine("Error: HTTP ${resp.code}")
                            else -> {
                                val messages = runCatching {
                                    JSONObject(body).getJSONArray("messages")
                                }.getOrNull()
                                renderMessages(messages)
                            }
                        }
                    }
                }
            } catch (_: IOException) {
                // Best-effort: keep whatever is currently shown.
            }
        }
    }

    private fun onSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false

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
                                val responseJson = runCatching {
                                    JSONObject(body)
                                }.getOrNull()
                                val userMessage = runCatching {
                                    responseJson
                                        ?.getJSONObject("user_message")
                                        ?.getString("content")
                                }.getOrNull()
                                val reply = runCatching {
                                    responseJson
                                        ?.getJSONObject("assistant_message")
                                        ?.getString("content")
                                }.getOrElse { "Error: unexpected response format" }
                                if (!userMessage.isNullOrBlank()) {
                                    appendLine("You: $userMessage")
                                }
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

    private fun renderMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            binding.tvChatLog.text = ""
            return
        }

        val lines = buildString {
            for (i in 0 until messages.length()) {
                val msg = messages.getJSONObject(i)
                val prefix = if (msg.optString("role") == "user") "You" else "AI"
                append(prefix)
                append(": ")
                append(msg.optString("content"))
                if (i < messages.length() - 1) append('\n')
            }
        }
        binding.tvChatLog.text = lines
    }
}
