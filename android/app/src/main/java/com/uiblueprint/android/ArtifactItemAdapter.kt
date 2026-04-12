package com.uiblueprint.android

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton

data class ArtifactItem(
    val id: String,
    val type: String,
    val objectKey: String,
    val url: String?,
)

class ArtifactItemAdapter(
    private val onArtifactClick: (ArtifactItem) -> Unit,
) : ListAdapter<ArtifactItem, ArtifactItemAdapter.ViewHolder>(DIFF) {

    inner class ViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        val ivIcon: ImageView = itemView.findViewById(R.id.ivArtifactIcon)
        val tvType: TextView = itemView.findViewById(R.id.tvArtifactType)
        val btnView: MaterialButton = itemView.findViewById(R.id.btnArtifactView)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_artifact, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val item = getItem(position)
        holder.tvType.text = item.type

        val iconRes = when {
            item.type == "clip" || item.type.endsWith("_video") ->
                android.R.drawable.ic_media_play
            item.type.contains("audio") ->
                android.R.drawable.ic_btn_speak_now
            item.type.endsWith("_json") || item.type.endsWith("_md") ||
                item.type == "analysis_md" || item.type == "blueprint_md" ->
                android.R.drawable.ic_menu_edit
            else ->
                android.R.drawable.ic_menu_agenda
        }
        holder.ivIcon.setImageResource(iconRes)

        holder.btnView.setOnClickListener { onArtifactClick(item) }
        holder.itemView.setOnClickListener { onArtifactClick(item) }
    }

    companion object {
        private val DIFF = object : DiffUtil.ItemCallback<ArtifactItem>() {
            override fun areItemsTheSame(a: ArtifactItem, b: ArtifactItem) = a.id == b.id
            override fun areContentsTheSame(a: ArtifactItem, b: ArtifactItem) = a == b
        }
    }
}
