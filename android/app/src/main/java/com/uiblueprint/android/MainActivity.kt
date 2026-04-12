package com.uiblueprint.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.ActionBarDrawerToggle
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.bottomsheet.BottomSheetDialog
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
 *  - A hamburger-icon AppBar that opens the Projects drawer.
 *  - A left-side drawer (~half screen, min 320dp) listing all projects fetched
 *    from GET /v1/folders via a scrollable RecyclerView, plus a "New Project" button.
 *  - Main area: global chat panel with RecyclerView, always-visible Copy/Share/Edit
 *    action rows, multi-select mode, and Agent Mode toggle.
 *
 * All recording, gallery-pick, and analyze actions have been moved to
 * FolderDetailActivity so that each clip is automatically associated with a project.
 */
class MainActivity : AppCompatActivity(),
    FolderAdapter.FolderActionListener,
    ChatMessageAdapter.MessageActionListener {

    private lateinit var binding: ActivityMainBinding
    private lateinit var drawerToggle: ActionBarDrawerToggle
    private lateinit var prefs: SharedPreferences
    private val folderAdapter = FolderAdapter(this)
    private lateinit var chatAdapter: ChatMessageAdapter

    private val chatExecutor = Executors.newSingleThreadExecutor { Thread(it, "GlobalChat-worker") }
    private val projectExecutor = Executors.newSingleThreadExecutor { Thread(it, "NewProject-worker") }

    private val attachPickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { _ ->
        // Global chat attachment not yet supported — picker opened as a stub
    }

    companion object {
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
        private const val PREFS_NAME = "chat_prefs"
        private const val PREF_AGENT_MODE = "agent_mode"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        setupDrawer()
        setupFolderList()
        setupChatList()

        // Wire hamburger and close-drawer buttons
        binding.btnDrawerToggle.setOnClickListener {
            if (binding.drawerLayout.isDrawerOpen(GravityCompat.START)) {
                binding.drawerLayout.closeDrawer(GravityCompat.START)
            } else {
                binding.drawerLayout.openDrawer(GravityCompat.START)
            }
        }
        binding.btnCloseDrawer.setOnClickListener {
            binding.drawerLayout.closeDrawer(GravityCompat.START)
        }

        binding.btnNewProject.setOnClickListener { onNewProjectClicked() }
        binding.btnSend.setOnClickListener { onChatSendClicked() }
        binding.btnAttach.setOnClickListener { showAttachBottomSheet() }
        binding.tvBackendUrl.text = getString(R.string.label_backend_url, BuildConfig.BACKEND_BASE_URL)

        // Restore agent mode preference.
        binding.switchAgentMode.isChecked = prefs.getBoolean(PREF_AGENT_MODE, false)
        binding.switchAgentMode.setOnCheckedChangeListener { _, isChecked ->
            prefs.edit().putBoolean(PREF_AGENT_MODE, isChecked).apply()
        }

        // Multi-select toolbar buttons.
        binding.btnSelectAll.setOnClickListener { chatAdapter.selectAll() }
        binding.btnCopySelected.setOnClickListener { onCopySelectedClicked() }
        binding.btnShareSelected.setOnClickListener { onShareSelectedClicked() }
        binding.btnCancelSelect.setOnClickListener {
            chatAdapter.clearSelection()
            updateMultiSelectToolbar()
        }

        loadFolders()
        loadGlobalChat()
    }

    override fun onResume() {
        super.onResume()
        loadFolders()
    }

    override fun onPostCreate(savedInstanceState: Bundle?) {
        super.onPostCreate(savedInstanceState)
        drawerToggle.syncState()
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (drawerToggle.onOptionsItemSelected(item)) return true
        return super.onOptionsItemSelected(item)
    }

    override fun onDestroy() {
        super.onDestroy()
        chatExecutor.shutdownNow()
        projectExecutor.shutdownNow()
    }

    // -------------------------------------------------------------------------
    // Drawer setup
    // -------------------------------------------------------------------------

    private fun setupDrawer() {
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.setHomeButtonEnabled(true)

        drawerToggle = ActionBarDrawerToggle(
            this,
            binding.drawerLayout,
            R.string.drawer_open,
            R.string.drawer_close,
        )
        binding.drawerLayout.addDrawerListener(drawerToggle)

        // Adjust drawer width: at least drawer_min_width, or half the screen on wider devices.
        val screenWidth = resources.displayMetrics.widthPixels
        val minWidthPx = resources.getDimensionPixelSize(R.dimen.drawer_min_width)
        val targetWidthPx = maxOf(minWidthPx, screenWidth / 2)
        val drawerParams = binding.drawerPanel.layoutParams as DrawerLayout.LayoutParams
        drawerParams.width = targetWidthPx
        binding.drawerPanel.layoutParams = drawerParams
    }

    // -------------------------------------------------------------------------
    // Folder/Project list (RecyclerView in drawer)
    // -------------------------------------------------------------------------

    private fun setupFolderList() {
        binding.rvFolders.layoutManager = LinearLayoutManager(this)
        binding.rvFolders.adapter = folderAdapter
    }

    private fun loadFolders() {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        val request = Request.Builder()
            .url("$baseUrl/v1/folders")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        projectExecutor.execute {
            try {
                val response = BackendClient.executeWithRetry(request)
                response.use { resp ->
                    val bodyStr = resp.body?.string() ?: ""
                    if (resp.isSuccessful) {
                        val foldersArray = runCatching {
                            JSONObject(bodyStr).getJSONArray("folders")
                        }.getOrNull() ?: JSONArray()
                        val items = parseFolderItems(foldersArray)
                        runOnUiThread {
                            folderAdapter.submitList(items)
                            binding.tvProjectsEmpty.visibility =
                                if (items.isEmpty()) View.VISIBLE else View.GONE
                        }
                    }
                }
            } catch (_: IOException) {
                // Best-effort: keep whatever is currently shown.
            }
        }
    }

    private fun parseFolderItems(array: JSONArray): List<FolderItem> {
        val result = mutableListOf<FolderItem>()
        for (i in 0 until array.length()) {
            val obj = array.getJSONObject(i)
            val id = obj.optString("id")
            if (id.isBlank()) continue
            val title = obj.optString("title", "")
            val status = obj.optString("status", "")
            val label = if (title.isNotBlank()) title else "Project ${id.take(8)}"
            result.add(FolderItem(id, status, label))
        }
        return result
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
                val folderJson = response.use { resp ->
                    if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                    JSONObject(resp.body?.string() ?: "{}")
                }
                val folderId = folderJson.getString("id")
                val title = folderJson.optString("title", "")
                val label = if (title.isNotBlank()) title else "Project ${folderId.take(8)}"
                val newItem = FolderItem(folderId, "new", label)

                runOnUiThread {
                    binding.tvStatus.text = getString(R.string.status_idle)
                    binding.btnNewProject.isEnabled = true
                    // Insert at top of list immediately; onResume will reload from server.
                    folderAdapter.prependIfAbsent(newItem)
                    binding.tvProjectsEmpty.visibility = View.GONE
                    binding.drawerLayout.closeDrawers()
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
    // Global chat (embedded panel with RecyclerView)
    // -------------------------------------------------------------------------

    private fun setupChatList() {
        chatAdapter = ChatMessageAdapter(this)
        binding.rvChatMessages.layoutManager = LinearLayoutManager(this).apply {
            stackFromEnd = true
        }
        binding.rvChatMessages.adapter = chatAdapter
    }

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
            // Send X-Agent-Mode header alongside body param for full compatibility.
            .addHeader("X-Agent-Mode", if (agentMode) "1" else "0")
            .build()

        chatExecutor.execute {
            try {
                val response = BackendClient.executeWithRetry(request) { attempt, total ->
                    runOnUiThread {
                        Toast.makeText(
                            this,
                            getString(R.string.status_chat_retrying, attempt, total),
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    runOnUiThread {
                        when {
                            resp.code == 401 || resp.code == 403 ->
                                Toast.makeText(this, "Unauthorized: check BACKEND_API_KEY", Toast.LENGTH_SHORT).show()
                            !resp.isSuccessful ->
                                Toast.makeText(this, "Error: HTTP ${resp.code}", Toast.LENGTH_SHORT).show()
                            else -> loadGlobalChat()
                        }
                        binding.btnSend.isEnabled = true
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    Toast.makeText(this, "Error: ${e.message ?: "Network error"}", Toast.LENGTH_SHORT).show()
                    binding.btnSend.isEnabled = true
                }
            }
        }
    }

    private fun renderChatMessages(messages: JSONArray?) {
        if (messages == null || messages.length() == 0) {
            chatAdapter.submitList(emptyList())
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
        // API returns newest-first; reverse for stackFromEnd display.
        list.reverse()
        chatAdapter.submitList(list)
        binding.rvChatMessages.scrollToPosition(chatAdapter.itemCount - 1)
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
    // Multi-select toolbar
    // -------------------------------------------------------------------------

    private fun updateMultiSelectToolbar() {
        val inMultiSelect = chatAdapter.isMultiSelectMode
        binding.toolbarMultiSelect.visibility = if (inMultiSelect) View.VISIBLE else View.GONE
        if (inMultiSelect) {
            val count = chatAdapter.getSelectedMessages().size
            binding.tvSelectionCount.text = resources.getQuantityString(
                R.plurals.multi_select_count, count, count
            )
        }
    }

    private fun onCopySelectedClicked() {
        val text = chatAdapter.getSelectedMessages().joinToString("\n\n") {
            "${if (it.role == "user") "You" else "AI"}: ${it.content}"
        }
        copyToClipboard(text)
        chatAdapter.clearSelection()
        updateMultiSelectToolbar()
        Toast.makeText(this, getString(R.string.toast_copied), Toast.LENGTH_SHORT).show()
    }

    private fun onShareSelectedClicked() {
        val text = chatAdapter.getSelectedMessages().joinToString("\n\n") {
            "${if (it.role == "user") "You" else "AI"}: ${it.content}"
        }
        shareText(text)
        chatAdapter.clearSelection()
        updateMultiSelectToolbar()
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

        val bodyJson = JSONObject().apply { put("content", newContent) }.toString()
        val request = Request.Builder()
            .url("$baseUrl/api/chat/$messageId/edit")
            .post(bodyJson.toRequestBody("application/json".toMediaType()))
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        chatExecutor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadGlobalChat()
                        } else {
                            Toast.makeText(this, "Edit failed: HTTP ${resp.code}", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    Toast.makeText(this, "Edit error: ${e.message ?: "Network error"}", Toast.LENGTH_SHORT).show()
                }
            }
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

    // -------------------------------------------------------------------------
    // Folder rename / delete dialogs
    // -------------------------------------------------------------------------

    override fun onRenameFolder(folderId: String, currentTitle: String) {
        val editText = EditText(this).apply {
            hint = getString(R.string.dialog_rename_hint)
            setText(currentTitle)
            selectAll()
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_rename_title))
            .setView(editText)
            .setPositiveButton(getString(R.string.dialog_btn_rename)) { _, _ ->
                val newTitle = editText.text.toString().trim()
                if (newTitle.isBlank()) {
                    Toast.makeText(this, getString(R.string.error_title_empty), Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                callRenameFolder(folderId, newTitle)
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    override fun onDeleteFolder(folderId: String) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.dialog_delete_title))
            .setMessage(getString(R.string.dialog_delete_message))
            .setPositiveButton(getString(R.string.dialog_btn_delete)) { _, _ ->
                callDeleteFolder(folderId)
            }
            .setNegativeButton(getString(R.string.dialog_btn_cancel), null)
            .show()
    }

    private fun callRenameFolder(folderId: String, newTitle: String) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val body = JSONObject().put("title", newTitle).toString()
            .toRequestBody("application/json".toMediaType())
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId")
            .patch(body)
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        projectExecutor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadFolders()
                        } else {
                            Toast.makeText(this, getString(R.string.error_rename_failed), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (_: IOException) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_rename_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun callDeleteFolder(folderId: String) {
        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId")
            .delete()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        projectExecutor.execute {
            try {
                BackendClient.executeWithRetry(request).use { resp ->
                    runOnUiThread {
                        if (resp.isSuccessful) {
                            loadFolders()
                        } else {
                            Toast.makeText(this, getString(R.string.error_delete_failed), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (_: IOException) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_delete_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    data class FolderItem(val id: String, val status: String, val label: String)

    // -------------------------------------------------------------------------
    // Attach bottom sheet
    // -------------------------------------------------------------------------

    private fun showAttachBottomSheet() {
        val sheet = BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.bottom_sheet_attach, null)
        sheet.setContentView(view)

        view.findViewById<android.widget.ImageButton>(R.id.btnAttachGallery).setOnClickListener {
            sheet.dismiss()
            attachPickerLauncher.launch("image/*")
        }
        view.findViewById<android.widget.ImageButton>(R.id.btnAttachCamera).setOnClickListener {
            sheet.dismiss()
            Toast.makeText(this, "Camera coming soon", Toast.LENGTH_SHORT).show()
        }
        view.findViewById<android.widget.ImageButton>(R.id.btnAttachDocument).setOnClickListener {
            sheet.dismiss()
            attachPickerLauncher.launch("*/*")
        }
        view.findViewById<android.widget.ImageButton>(R.id.btnAttachAudio).setOnClickListener {
            sheet.dismiss()
            attachPickerLauncher.launch("audio/*")
        }
        // Hide video row in MainActivity
        view.findViewById<android.view.View>(R.id.rowAttach2)?.visibility = android.view.View.GONE
        sheet.show()
    }
}
