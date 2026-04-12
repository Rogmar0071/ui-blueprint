package com.uiblueprint.android

import android.graphics.BitmapFactory
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.AppCompatImageView
import okhttp3.Request
import java.io.IOException

class ImageViewerActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_image_viewer)

        val url = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_URL)
        val title = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_TITLE) ?: "Image"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        val ivContent = findViewById<AppCompatImageView>(R.id.ivContent)

        if (url != null) {
            loadImage(ivContent, url)
        } else {
            Toast.makeText(this, getString(R.string.artifact_url_unavailable), Toast.LENGTH_SHORT).show()
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    private fun loadImage(imageView: AppCompatImageView, url: String) {
        val request = Request.Builder().url(url).get().build()
        Thread {
            try {
                val response = BackendClient.httpClient.newCall(request).execute()
                response.use { resp ->
                    if (resp.isSuccessful) {
                        val bytes = resp.body?.bytes()
                        if (bytes != null) {
                            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                            runOnUiThread { imageView.setImageBitmap(bitmap) }
                        }
                    } else {
                        runOnUiThread {
                            Toast.makeText(this, "HTTP ${resp.code}", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (e: IOException) {
                runOnUiThread {
                    Toast.makeText(this, e.message ?: "Network error", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }
}
