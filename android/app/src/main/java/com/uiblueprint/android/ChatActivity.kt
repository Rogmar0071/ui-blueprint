package com.uiblueprint.android

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.uiblueprint.android.databinding.ActivityChatBinding
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Simple chat screen that sends messages to the backend /api/chat endpoint
 * and displays the AI replies in a scrollable log.
 *
 * Authorization: Bearer <BACKEND_API_KEY> is added when the key is non-empty.
 * The key is never logged.
 */
class ChatActivity : AppCompatActivity() {

    companion object {
        private val client = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    private lateinit var binding: ActivityChatBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnSend.setOnClickListener { onSendClicked() }
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

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread {
                    appendLine("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }

            override fun onResponse(call: Call, response: Response) {
                val body = response.body?.string() ?: ""
                runOnUiThread {
                    when {
                        response.code == 401 || response.code == 403 ->
                            appendLine("Unauthorized: check BACKEND_API_KEY")
                        !response.isSuccessful ->
                            appendLine("Error: HTTP ${response.code}")
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
        })
    }

    private fun appendLine(line: String) {
        val current = binding.tvChatLog.text
        binding.tvChatLog.text = if (current.isNullOrEmpty()) line else "$current\n$line"
    }
}
