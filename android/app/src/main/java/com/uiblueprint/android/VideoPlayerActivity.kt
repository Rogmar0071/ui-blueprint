package com.uiblueprint.android

import android.media.MediaPlayer
import android.os.Bundle
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.widget.ProgressBar
import androidx.appcompat.app.AppCompatActivity

class VideoPlayerActivity : AppCompatActivity(), SurfaceHolder.Callback {

    private var mediaPlayer: MediaPlayer? = null
    private lateinit var surfaceView: SurfaceView
    private lateinit var progressBar: ProgressBar
    private var artifactUrl: String? = null
    private var surfaceReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_video_player)

        artifactUrl = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_URL)
        val title = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_TITLE) ?: "Video"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        surfaceView = findViewById(R.id.surfaceView)
        progressBar = findViewById(R.id.progressBar)

        surfaceView.holder.addCallback(this)
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun surfaceCreated(holder: SurfaceHolder) {
        surfaceReady = true
        preparePlayer(holder.surface)
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {}

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        surfaceReady = false
    }

    private fun preparePlayer(surface: Surface) {
        val url = artifactUrl ?: return
        progressBar.visibility = View.VISIBLE
        val player = MediaPlayer()
        mediaPlayer = player
        player.setDataSource(url)
        player.setSurface(surface)
        player.setOnPreparedListener {
            progressBar.visibility = View.GONE
            it.start()
        }
        player.setOnErrorListener { _, _, _ ->
            progressBar.visibility = View.GONE
            true
        }
        player.prepareAsync()
    }

    override fun onStop() {
        super.onStop()
        mediaPlayer?.release()
        mediaPlayer = null
    }
}
