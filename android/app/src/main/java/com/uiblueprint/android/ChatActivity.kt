package com.uiblueprint.android

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.uiblueprint.android.databinding.ActivityChatBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors

/**
 * Global chat screen.
 *
 * Features
 * --------
 * - Messages displayed in a RecyclerView using [ChatMessageAdapter].
 * - Always-visible Copy / Share action row under each message.
 * - Edit button on user messages: opens a dialog, sends an edit request to the
 *   backend (POST /api/chat/{id}/edit), and refreshes the conversation.
 * - Long-press enters multi-select mode; selection count stays in the top bar and
 *   action icons move into the input row.
 * - Agent Mode toggle: persisted in SharedPreferences.
 *   When enabled, sends ``X-Agent-Mode: 1`` header + ``agent_mode: true`` body
 *   so the backend formats the response with ARTIFACT_* sections.
 * - ARTIFACT_* blocks are rendered as a monospace card with their own Copy button.
 *
 * Authorization: Bearer <BACKEND_API_KEY> is added when the key is non-empty.
 */
class ChatActivity : AppCompatActivity(), ChatMessageAdapter.MessageActionListener {

    private lateinit var binding: ActivityChatBinding
    private lateinit var prefs: SharedPreferences
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "ChatActivity-worker") }
    private lateinit var adapter: ChatMessageAdapter

    private val speechInputLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val matches = result.data
                ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            if (!matches.isNullOrEmpty()) {
                val current = binding.etMessage.text.toString()
                binding.etMessage.setText(
                    if (current.isBlank()) matches[0] else "$current ${matches[0]}"
                )
                binding.etMessage.setSelection(binding.etMessage.text.length)
            }
        }
    }

    private val micPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startSpeechRecognition()
        } else {
            Toast.makeText(
                this,
                getString(R.string.toast_mic_permission_denied),
                Toast.LENGTH_SHORT,
            ).show()
        }
    }

    companion object {
        private const val PREFS_NAME = "chat_prefs"
        private const val PREF_AGENT_MODE = "agent_mode"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        adapter = ChatMessageAdapter(this)
        binding.rvMessages.layoutManager = LinearLayoutManager(this).apply {
            stackFromEnd = true
        }
        binding.rvMessages.adapter = adapter

        // Restore agent mode preference.
        binding.switchAgentMode.isChecked = prefs.getBoolean(PREF_AGENT_MODE, false)
        binding.switchAgentMode.setOnCheckedChangeListener { _, isChecked ->
            prefs.edit().putBoolean(PREF_AGENT_MODE, isChecked).apply()
        }

        binding.btnSend.setOnClickListener { onSendClicked() }

        setupMicButton()

        // Multi-select action buttons
        binding.btnSelectAll.setOnClickListener {
            adapter.selectAll()
        }
        binding.btnCopySelected.setOnClickListener {
            val text = adapter.getSelectedMessages().joinToString("\n\n") {
                "${if (it.role == "user") "You" else "AI"}: ${it.content}"
            }
            copyToClipboard(text)
            adapter.clearSelection()
            updateMultiSelectToolbar()
            Toast.makeText(this, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
        }
        binding.btnShareSelected.setOnClickListener {
            val text = adapter.getSelectedMessages().joinToString("\n\n") {
                "${if (it.role == "user") "You" else "AI"}: ${it.content}"
            }
            shareText(text)
            adapter.clearSelection()
            updateMultiSelectToolbar()
        }
        binding.btnCancelSelect.setOnClickListener {
            adapter.clearSelection()
            updateMultiSelectToolbar()
        }
    }

    override fun onResume() {
        super.onResume()
        loadMessages()
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    // -------------------------------------------------------------------------
    // ChatMessageAdapter.MessageActionListener
    // -------------------------------------------------------------------------

    override fun onCopyMessage(message: ChatMessageAdapter.Message) {
        copyToClipboard(message.content)
        Toast.makeText(this, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
    }

    override fun onShareMessage(message: ChatMessageAdapter.Message) {
        shareText(message.content)
    }

    override fun onEditMessage(message: ChatMessageAdapter.Message) {
        showEditDialog(message)
    }

    override fun onSelectionChanged(selectedCount: Int) {
        updateMultiSelectToolbar()
    }

    // -------------------------------------------------------------------------
    // Load messages
    // -------------------------------------------------------------------------

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
                                showError("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                showError("Error: HTTP ${resp.code}")
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

    // -------------------------------------------------------------------------
    // Send message
    // -------------------------------------------------------------------------

    private fun onSendClicked() {
        val message = binding.etMessage.text.toString().trim()
        if (message.isBlank()) return

        binding.etMessage.setText("")
        binding.btnSend.isEnabled = false

        val agentMode = binding.switchAgentMode.isChecked

        val bodyJson = JSONObject().apply {
            put("message", message)
            put("agent_mode", agentMode)
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
            // Send X-Agent-Mode header as well so backends that read the header work.
            .addHeader("X-Agent-Mode", if (agentMode) "1" else "0")
            .build()

        executor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        showError(getString(R.string.status_chat_retrying, attempt, total))
                    }
                }
                response.use { resp ->
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                showError("Unauthorized: check BACKEND_API_KEY")
                            !resp.isSuccessful ->
                                showError("Error: HTTP ${resp.code}")
                            else -> loadMessages()
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    showError("Error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Edit message
    // -------------------------------------------------------------------------

    private fun showEditDialog(message: ChatMessageAdapter.Message) {
        val editText = EditText(this).apply {
            setText(message.content)
            setSelection(message.content.length)
        }

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_edit_message_title))
            .setView(editText)
            .setPositiveButton(getString(R.string.dialog_btn_save)) { _, _ ->
                val newContent = editText.text.toString().trim()
                if (newContent.isNotBlank()) {
                    submitEdit(message.id, newContent)
                }
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    private fun submitEdit(messageId: String, newContent: String) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val bodyJson = JSONObject().apply {
            put("content", newContent)
        }.toString()

        val request = Request.Builder()
            .url("$baseUrl/api/chat/$messageId/edit")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        binding.btnSend.isEnabled = false

        executor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadMessages()
                        } else {
                            showError("Edit failed: HTTP ${resp.code}")
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    showError("Edit error: ${e.message ?: "Network error"}")
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    // -------------------------------------------------------------------------
    // Render
    // -------------------------------------------------------------------------

    private fun renderMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            adapter.submitList(emptyList())
            return
        }

        val list = mutableListOf<ChatMessageAdapter.Message>()
        for (i in 0 until messages.length()) {
            val msg = messages.getJSONObject(i)
            list.add(
                ChatMessageAdapter.Message(
                    id = msg.optString("id"),
                    role = msg.optString("role"),
                    content = msg.optString("content"),
                    superseded = msg.optBoolean("superseded", false),
                )
            )
        }
        // API returns newest-first; reverse so the RecyclerView shows oldest-first
        // with stackFromEnd=true (most recent at bottom).
        list.reverse()
        adapter.submitList(list)
        binding.rvMessages.scrollToPosition(adapter.itemCount - 1)
    }

    // -------------------------------------------------------------------------
    // Multi-select toolbar
    // -------------------------------------------------------------------------

    private fun updateMultiSelectToolbar() {
        val inMultiSelect = adapter.isMultiSelectMode
        binding.toolbarMultiSelect.visibility = if (inMultiSelect) View.VISIBLE else View.GONE
        binding.layoutMultiSelectActions.visibility = if (inMultiSelect) View.VISIBLE else View.GONE
        binding.btnAttach.visibility = if (inMultiSelect) View.GONE else View.VISIBLE
        binding.btnMic.visibility = if (inMultiSelect) View.GONE else View.VISIBLE
        binding.btnSend.visibility = if (inMultiSelect) View.GONE else View.VISIBLE
        if (inMultiSelect) {
            val count = adapter.getSelectedMessages().size
            binding.tvSelectionCount.text = resources.getQuantityString(
                R.plurals.multi_select_count, count, count
            )
        }
    }

    // -------------------------------------------------------------------------
    // Clipboard / Share helpers
    // -------------------------------------------------------------------------

    private fun copyToClipboard(text: String) {
        val clipboard = ContextCompat.getSystemService(this, ClipboardManager::class.java)
        clipboard?.setPrimaryClip(ClipData.newPlainText("chat_message", text))
    }

    private fun shareText(text: String) {
        startActivity(
            Intent.createChooser(
                Intent(Intent.ACTION_SEND).apply {
                    type = "text/plain"
                    putExtra(Intent.EXTRA_TEXT, text)
                },
                getString(R.string.share_via)
            )
        )
    }

    private fun showError(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    // -------------------------------------------------------------------------
    // Voice / microphone input
    // -------------------------------------------------------------------------

    private fun setupMicButton() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            binding.btnMic.isEnabled = false
            return
        }
        binding.btnMic.setOnClickListener { onMicClicked() }
    }

    private fun onMicClicked() {
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startSpeechRecognition()
        } else {
            micPermissionLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun startSpeechRecognition() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.btn_mic))
        }
        speechInputLauncher.launch(intent)
    }
}
