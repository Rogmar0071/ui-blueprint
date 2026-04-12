package com.uiblueprint.android

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException

object ArtifactViewerRouter {

    const val EXTRA_ARTIFACT_URL = "artifact_url"
    const val EXTRA_ARTIFACT_TITLE = "artifact_title"

    fun open(context: Context, artifact: JSONObject, folderId: String) {
        val type = artifact.optString("type", "")
        val artifactId = artifact.optString("id", "")

        val directUrl = artifact.optString("url", "").takeIf { it.isNotBlank() }
        if (directUrl != null) {
            launchViewer(context, type, directUrl)
            return
        }

        Toast.makeText(context, context.getString(R.string.artifact_viewer_loading), Toast.LENGTH_SHORT).show()

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY
        val request = Request.Builder()
            .url("$baseUrl/v1/folders/$folderId/artifacts/$artifactId/url")
            .get()
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .build()

        Thread {
            try {
                val response = BackendClient.httpClient.newCall(request).execute()
                response.use { resp ->
                    val body = resp.body?.string() ?: ""
                    val url = if (resp.isSuccessful) {
                        runCatching { JSONObject(body).getString("url") }.getOrNull()
                    } else null

                    Handler(Looper.getMainLooper()).post {
                        if (url != null) {
                            launchViewer(context, type, url)
                        } else {
                            Toast.makeText(
                                context,
                                context.getString(R.string.artifact_url_unavailable),
                                Toast.LENGTH_SHORT,
                            ).show()
                        }
                    }
                }
            } catch (e: IOException) {
                Handler(Looper.getMainLooper()).post {
                    Toast.makeText(
                        context,
                        context.getString(R.string.artifact_url_unavailable),
                        Toast.LENGTH_SHORT,
                    ).show()
                }
            }
        }.start()
    }

    private fun launchViewer(context: Context, type: String, url: String) {
        if (type == "clip") {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(Uri.parse(url), "video/mp4")
                if (context !is Activity) {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
            }
            try {
                context.startActivity(intent)
            } catch (e: ActivityNotFoundException) {
                Toast.makeText(
                    context,
                    context.getString(R.string.artifact_url_unavailable),
                    Toast.LENGTH_SHORT,
                ).show()
            }
            return
        }
        val activityClass = when {
            type.endsWith("_video") -> VideoPlayerActivity::class.java
            type.endsWith("_md") || type == "analysis_md" || type == "blueprint_md" -> TextViewerActivity::class.java
            type.endsWith("_json") -> TextViewerActivity::class.java
            type.contains("audio") -> AudioPlayerActivity::class.java
            type.contains("image") -> ImageViewerActivity::class.java
            else -> TextViewerActivity::class.java
        }
        val intent = Intent(context, activityClass).apply {
            putExtra(EXTRA_ARTIFACT_URL, url)
            putExtra(EXTRA_ARTIFACT_TITLE, type)
            if (context !is Activity) {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        }
        context.startActivity(intent)
    }
}
