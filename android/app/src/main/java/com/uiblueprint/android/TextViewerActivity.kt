package com.uiblueprint.android

import android.content.Intent
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException

class TextViewerActivity : AppCompatActivity() {

    private lateinit var tvContent: TextView
    private lateinit var progressBar: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_text_viewer)

        val url = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_URL)
        val title = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_TITLE) ?: "Artifact"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        tvContent = findViewById(R.id.tvContent)
        progressBar = findViewById(R.id.progressBar)

        if (url != null) {
            progressBar.visibility = View.VISIBLE
            fetchContent(url)
        } else {
            tvContent.text = getString(R.string.artifact_url_unavailable)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.menu_text_viewer, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_share_text -> {
                val text = tvContent.text.toString()
                if (text.isNotBlank()) {
                    startActivity(
                        Intent.createChooser(
                            Intent(Intent.ACTION_SEND).apply {
                                type = "text/plain"
                                putExtra(Intent.EXTRA_TEXT, text)
                            },
                            getString(R.string.action_share_text),
                        )
                    )
                }
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    private fun fetchContent(url: String) {
        val request = Request.Builder().url(url).get().build()
        Thread {
            try {
                val response = BackendClient.httpClient.newCall(request).execute()
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    val display = if (resp.isSuccessful) {
                        prettifyIfJson(body)
                    } else {
                        "HTTP ${resp.code}"
                    }
                    runOnUiThread {
                        progressBar.visibility = View.GONE
                        tvContent.text = display
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    progressBar.visibility = View.GONE
                    Toast.makeText(this, e.message ?: "Network error", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun prettifyIfJson(content: String): String {
        return try {
            JSONObject(content).toString(2)
        } catch (_: Exception) {
            content
        }
    }
}
