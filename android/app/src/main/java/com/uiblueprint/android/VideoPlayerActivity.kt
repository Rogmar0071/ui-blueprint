package com.uiblueprint.android

import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.MediaController
import android.widget.ProgressBar
import android.widget.Toast
import android.widget.VideoView
import androidx.appcompat.app.AppCompatActivity

class VideoPlayerActivity : AppCompatActivity() {

    private lateinit var videoView: VideoView
    private lateinit var progressBar: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_video_player)

        val url = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_URL)
        val title = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_TITLE) ?: "Video"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        videoView = findViewById(R.id.videoView)
        progressBar = findViewById(R.id.progressBar)

        if (url == null) {
            Toast.makeText(this, getString(R.string.artifact_url_unavailable), Toast.LENGTH_LONG).show()
            return
        }

        val mediaController = MediaController(this)
        mediaController.setAnchorView(videoView)
        videoView.setMediaController(mediaController)
        videoView.setVideoURI(Uri.parse(url))

        videoView.setOnPreparedListener { mp ->
            progressBar.visibility = View.GONE
            mp.isLooping = false
            videoView.start()
        }

        videoView.setOnErrorListener { _, _, _ ->
            progressBar.visibility = View.GONE
            Toast.makeText(this, getString(R.string.video_playback_error), Toast.LENGTH_LONG).show()
            true
        }

        progressBar.visibility = View.VISIBLE
        videoView.requestFocus()
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun onStop() {
        super.onStop()
        if (::videoView.isInitialized) {
            videoView.stopPlayback()
        }
    }
}
