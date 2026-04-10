package com.uiblueprint.android

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.uiblueprint.android.databinding.ActivityMainBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors

/**
 * Main (Home) screen.
 *
 * Shows:
 *  - A "New Project" button that creates an empty project/folder and opens it.
 *  - A list of existing projects (tappable → opens FolderDetailActivity).
 *  - A global chat panel that takes the majority of the screen.
 *
 * All recording, gallery-pick, and analyze actions have been moved to
 * FolderDetailActivity so that each clip is automatically associated with a project.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    // Folder items created on this screen or fetched from backend
    private val folderItems = mutableListOf<FolderItem>()

    private val chatExecutor = Executors.newSingleThreadExecutor { Thread(it, "GlobalChat-worker") }
    private val projectExecutor = Executors.newSingleThreadExecutor { Thread(it, "NewProject-worker") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnNewProject.setOnClickListener { onNewProjectClicked() }
        binding.btnSend.setOnClickListener { onChatSendClicked() }
        binding.tvBackendUrl.text = getString(R.string.label_backend_url, BuildConfig.BACKEND_BASE_URL)

        loadGlobalChat()
    }

    override fun onDestroy() {
        super.onDestroy()
        chatExecutor.shutdownNow()
        projectExecutor.shutdownNow()
    }

    // -------------------------------------------------------------------------
    // New Project
    // -------------------------------------------------------------------------

    private fun onNewProjectClicked() {
        binding.tvStatus.text = getString(R.string.status_creating_project)
        binding.btnNewProject.isEnabled = false

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/v1/folders")
            .post("{}".toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        projectExecutor.execute {
            try {
                val response = BackendClient.executeWithRetry(request)
                val folderId = response.use { resp ->
                    if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                    JSONObject(resp.body?.string() ?: "{}").getString("id")
                }

                runOnUiThread {
                    binding.tvStatus.text = getString(R.string.status_idle)
                    binding.btnNewProject.isEnabled = true
                    addFolderItem(FolderItem(folderId, "new", "Project"))
                    val intent = Intent(this, FolderDetailActivity::class.java)
                    intent.putExtra(FolderDetailActivity.EXTRA_FOLDER_ID, folderId)
                    startActivity(intent)
                }
            } catch (e: IOException) {
                runOnUiThread {
                    binding.tvStatus.text = getString(R.string.status_idle)
                    binding.btnNewProject.isEnabled = true
                    Toast.makeText(
                        this,
                        "Failed to create project: ${e.message}",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Global chat (embedded panel)
    // -------------------------------------------------------------------------

    private fun loadGlobalChat() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/api/chat")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        chatExecutor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            val messages = runCatching {
                                JSONObject(body).getJSONArray("messages")
                            }.getOrNull()
                            renderChatMessages(messages)
                        }
                    }
                }
            } catch (_: IOException) {
                // Best-effort: keep whatever is currently shown.
            }
        }
    }

    private fun onChatSendClicked() {
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

        chatExecutor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        appendChatLine(getString(R.string.status_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                appendChatLine("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                appendChatLine("Error: HTTP ${resp.code}")
                            else -> {
                                val responseJson = runCatching { JSONObject(body) }.getOrNull()
                                val userMessage = runCatching {
                                    responseJson?.getJSONObject("user_message")?.getString("content")
                                }.getOrNull()
                                val reply = runCatching {
                                    responseJson?.getJSONObject("assistant_message")?.getString("content")
                                }.getOrElse { "Error: unexpected response format" }
                                if (!userMessage.isNullOrBlank()) appendChatLine("You: $userMessage")
                                appendChatLine("AI: $reply")
                            }
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    appendChatLine("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    private fun renderChatMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            scrollChatToBottom()
            return
        }
        val lines = buildString {
            for (i in 0 until messages.length()) {
                val msg = messages.getJSONObject(i)
                val prefix = if (msg.optString("role") == "user") "You" else "AI"
                if (i > 0) append('\n')
                append(prefix)
                append(": ")
                append(msg.optString("content"))
            }
        }
        binding.tvChatLog.text = lines
        scrollChatToBottom()
    }

    private fun appendChatLine(line: String) {
        val current = binding.tvChatLog.text
        binding.tvChatLog.text = if (current.isNullOrEmpty()) line else "$current\n$line"
        scrollChatToBottom()
    }

    private fun scrollChatToBottom() {
        binding.scrollChat.post {
            binding.scrollChat.fullScroll(View.FOCUS_DOWN)
        }
    }

    // -------------------------------------------------------------------------
    // Folder/Project items list
    // -------------------------------------------------------------------------

    private fun addFolderItem(item: FolderItem) {
        folderItems.add(0, item)
        renderFolderList()
    }

    private fun renderFolderList() {
        val container = binding.llFolderList
        container.removeAllViews()
        for (item in folderItems) {
            val tv = TextView(this).apply {
                text = "📁 ${item.label}  [${item.status}]"
                textSize = 12f
                typeface = android.graphics.Typeface.MONOSPACE
                setPadding(0, 4, 0, 4)
                isClickable = true
                isFocusable = true
                setOnClickListener {
                    val intent = Intent(this@MainActivity, FolderDetailActivity::class.java)
                    intent.putExtra(FolderDetailActivity.EXTRA_FOLDER_ID, item.id)
                    startActivity(intent)
                }
            }
            container.addView(tv)
        }
    }

    data class FolderItem(val id: String, val status: String, val label: String)

    companion object {
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
    }
}

