package com.uiblueprint.android

import android.content.Intent
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.ActionBarDrawerToggle
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.LinearLayoutManager
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
 *  - Main area: global chat panel.
 *
 * All recording, gallery-pick, and analyze actions have been moved to
 * FolderDetailActivity so that each clip is automatically associated with a project.
 */
class MainActivity : AppCompatActivity(), FolderAdapter.FolderActionListener {

    private lateinit var binding: ActivityMainBinding
    private lateinit var drawerToggle: ActionBarDrawerToggle
    private val folderAdapter = FolderAdapter(this)

    private val chatExecutor = Executors.newSingleThreadExecutor { Thread(it, "GlobalChat-worker") }
    private val projectExecutor = Executors.newSingleThreadExecutor { Thread(it, "NewProject-worker") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupDrawer()
        setupFolderList()

        binding.btnNewProject.setOnClickListener { onNewProjectClicked() }
        binding.btnSend.setOnClickListener { onChatSendClicked() }
        binding.tvBackendUrl.text = getString(R.string.label_backend_url, BuildConfig.BACKEND_BASE_URL)

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

    data class FolderItem(val id: String, val status: String, val label: String)

    // -------------------------------------------------------------------------
    // FolderActionListener – Rename / Delete
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

    companion object {
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
    }
}

