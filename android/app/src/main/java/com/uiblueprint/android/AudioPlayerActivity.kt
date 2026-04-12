package com.uiblueprint.android

import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ImageButton
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.IOException
import java.util.concurrent.TimeUnit

class AudioPlayerActivity : AppCompatActivity() {

    private var mediaPlayer: MediaPlayer? = null
    private lateinit var btnPlayPause: ImageButton
    private lateinit var seekBar: SeekBar
    private lateinit var tvPosition: TextView
    private val updateHandler = Handler(Looper.getMainLooper())
    private val updateRunnable = object : Runnable {
        override fun run() {
            val player = mediaPlayer ?: return
            if (player.isPlaying) {
                seekBar.progress = player.currentPosition
                tvPosition.text = formatTime(player.currentPosition, player.duration)
                updateHandler.postDelayed(this, 500)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_audio_player)

        val url = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_URL)
        val title = intent.getStringExtra(ArtifactViewerRouter.EXTRA_ARTIFACT_TITLE) ?: "Audio"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        val tvTitle = findViewById<TextView>(R.id.tvAudioTitle)
        btnPlayPause = findViewById(R.id.btnPlayPause)
        seekBar = findViewById(R.id.seekBar)
        tvPosition = findViewById(R.id.tvPosition)

        tvTitle.text = title
        btnPlayPause.isEnabled = false

        if (url != null) {
            preparePlayer(url)
        } else {
            Toast.makeText(this, getString(R.string.artifact_url_unavailable), Toast.LENGTH_SHORT).show()
        }

        btnPlayPause.setOnClickListener { togglePlayPause() }

        seekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                if (fromUser) mediaPlayer?.seekTo(progress)
            }
            override fun onStartTrackingTouch(sb: SeekBar) {}
            override fun onStopTrackingTouch(sb: SeekBar) {}
        })
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    private fun preparePlayer(url: String) {
        val player = MediaPlayer()
        mediaPlayer = player
        player.setAudioAttributes(
            AudioAttributes.Builder()
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .build()
        )
        try {
            player.setDataSource(url)
            player.setOnPreparedListener { mp ->
                seekBar.max = mp.duration
                btnPlayPause.isEnabled = true
                tvPosition.text = formatTime(0, mp.duration)
            }
            player.setOnCompletionListener {
                btnPlayPause.setImageResource(android.R.drawable.ic_media_play)
                updateHandler.removeCallbacks(updateRunnable)
            }
            player.prepareAsync()
        } catch (e: IOException) {
            Toast.makeText(this, e.message ?: "Playback error", Toast.LENGTH_SHORT).show()
        }
    }

    private fun togglePlayPause() {
        val player = mediaPlayer ?: return
        if (player.isPlaying) {
            player.pause()
            btnPlayPause.setImageResource(android.R.drawable.ic_media_play)
            updateHandler.removeCallbacks(updateRunnable)
        } else {
            player.start()
            btnPlayPause.setImageResource(android.R.drawable.ic_media_pause)
            updateHandler.post(updateRunnable)
        }
    }

    private fun formatTime(posMs: Int, durMs: Int): String {
        val pos = TimeUnit.MILLISECONDS.toSeconds(posMs.toLong())
        val dur = TimeUnit.MILLISECONDS.toSeconds(durMs.toLong())
        return "${pos / 60}:${String.format("%02d", pos % 60)} / ${dur / 60}:${String.format("%02d", dur % 60)}"
    }

    override fun onStop() {
        super.onStop()
        updateHandler.removeCallbacks(updateRunnable)
        mediaPlayer?.release()
        mediaPlayer = null
    }
}
