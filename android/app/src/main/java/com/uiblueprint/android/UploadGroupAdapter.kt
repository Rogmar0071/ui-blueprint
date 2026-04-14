package com.uiblueprint.android

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton

data class UploadGroupItem(
    val id: String,
    val title: String,
    val subtitle: String,
    val uploadArtifact: ArtifactItem?,
    val relatedArtifacts: List<ArtifactItem>,
    val canAnalyze: Boolean = false,
    val analyzeEnabled: Boolean = true,
)

class UploadGroupAdapter(
    private val onOpenUpload: (UploadGroupItem) -> Unit,
    private val onRenameUpload: (UploadGroupItem) -> Unit,
    private val onAnalyzeUpload: (UploadGroupItem) -> Unit,
    private val onDeleteUpload: (UploadGroupItem) -> Unit,
    private val onOpenArtifact: (ArtifactItem) -> Unit,
    private val onDeleteArtifact: (ArtifactItem) -> Unit,
) : ListAdapter<UploadGroupItem, UploadGroupAdapter.ViewHolder>(DIFF) {
    private val expandedIds = mutableSetOf<String>()

    inner class ViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        val tvTitle: TextView = itemView.findViewById(R.id.tvUploadTitle)
        val tvSubtitle: TextView = itemView.findViewById(R.id.tvUploadSubtitle)
        val btnView: MaterialButton = itemView.findViewById(R.id.btnUploadView)
        val btnRename: MaterialButton = itemView.findViewById(R.id.btnUploadRename)
        val btnAnalyze: MaterialButton = itemView.findViewById(R.id.btnUploadAnalyze)
        val btnDelete: MaterialButton = itemView.findViewById(R.id.btnUploadDelete)
        val btnToggle: MaterialButton = itemView.findViewById(R.id.btnUploadToggle)
        val ivChevron: ImageView = itemView.findViewById(R.id.ivUploadChevron)
        val rvArtifacts: RecyclerView = itemView.findViewById(R.id.rvUploadArtifacts)
        val tvEmpty: TextView = itemView.findViewById(R.id.tvUploadEmpty)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_upload_group, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val item = getItem(position)
        val isExpanded = expandedIds.contains(item.id)
        holder.tvTitle.text = item.title
        holder.tvSubtitle.text = item.subtitle

        val childAdapter = ArtifactItemAdapter(onOpenArtifact, onDeleteArtifact)
        holder.rvArtifacts.layoutManager = LinearLayoutManager(holder.itemView.context)
        holder.rvArtifacts.adapter = childAdapter
        childAdapter.submitList(item.relatedArtifacts)

        val hasUpload = item.uploadArtifact != null
        holder.btnView.visibility = if (hasUpload) View.VISIBLE else View.GONE
        holder.btnRename.visibility = if (hasUpload) View.VISIBLE else View.GONE
        holder.btnAnalyze.visibility = if (hasUpload && item.canAnalyze) View.VISIBLE else View.GONE
        holder.btnAnalyze.isEnabled = item.analyzeEnabled
        holder.btnAnalyze.alpha = if (item.analyzeEnabled) 1f else 0.45f
        holder.btnDelete.visibility = if (hasUpload) View.VISIBLE else View.GONE
        holder.btnView.setOnClickListener { onOpenUpload(item) }
        holder.btnRename.setOnClickListener { onRenameUpload(item) }
        holder.btnAnalyze.setOnClickListener { onAnalyzeUpload(item) }
        holder.btnDelete.setOnClickListener { onDeleteUpload(item) }

        holder.rvArtifacts.visibility = if (isExpanded && item.relatedArtifacts.isNotEmpty()) View.VISIBLE else View.GONE
        holder.tvEmpty.visibility = if (isExpanded && item.relatedArtifacts.isEmpty()) View.VISIBLE else View.GONE
        holder.btnToggle.text = if (isExpanded) {
            holder.itemView.context.getString(R.string.btn_hide_details)
        } else {
            holder.itemView.context.getString(R.string.btn_show_details)
        }
        holder.ivChevron.rotation = if (isExpanded) 0f else -90f

        val toggle = {
            if (expandedIds.contains(item.id)) expandedIds.remove(item.id) else expandedIds.add(item.id)
            notifyItemChanged(holder.bindingAdapterPosition)
        }
        holder.btnToggle.setOnClickListener { toggle() }
        holder.itemView.setOnClickListener { toggle() }
    }

    companion object {
        private val DIFF = object : DiffUtil.ItemCallback<UploadGroupItem>() {
            override fun areItemsTheSame(a: UploadGroupItem, b: UploadGroupItem) = a.id == b.id
            override fun areContentsTheSame(a: UploadGroupItem, b: UploadGroupItem) = a == b
        }
    }
}
