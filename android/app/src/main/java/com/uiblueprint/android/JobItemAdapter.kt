package com.uiblueprint.android

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView

data class JobItem(
    val id: String,
    val type: String,
    val status: String,
    val progress: Int,
    val createdAt: String,
)

class JobItemAdapter : ListAdapter<JobItem, JobItemAdapter.ViewHolder>(DIFF) {

    inner class ViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        val tvType: TextView = itemView.findViewById(R.id.tvJobType)
        val tvStatus: TextView = itemView.findViewById(R.id.tvJobStatus)
        val tvProgress: TextView = itemView.findViewById(R.id.tvJobProgress)
        val tvCreatedAt: TextView = itemView.findViewById(R.id.tvJobCreatedAt)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_job, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val item = getItem(position)
        holder.tvType.text = item.type
        holder.tvStatus.text = item.status
        val badgeColor = when (item.status) {
            "queued" -> ContextCompat.getColor(holder.itemView.context, R.color.status_queued)
            "running" -> ContextCompat.getColor(holder.itemView.context, R.color.status_running)
            "succeeded" -> ContextCompat.getColor(holder.itemView.context, R.color.status_succeeded)
            "failed" -> ContextCompat.getColor(holder.itemView.context, R.color.status_failed)
            else -> ContextCompat.getColor(holder.itemView.context, R.color.status_unknown)
        }
        holder.tvStatus.setBackgroundColor(badgeColor)

        if (item.status == "running" && item.progress > 0) {
            holder.tvProgress.text = "${item.progress}%"
            holder.tvProgress.visibility = View.VISIBLE
        } else {
            holder.tvProgress.visibility = View.GONE
        }

        holder.tvCreatedAt.text = item.createdAt
    }

    companion object {
        private val DIFF = object : DiffUtil.ItemCallback<JobItem>() {
            override fun areItemsTheSame(a: JobItem, b: JobItem) = a.id == b.id
            override fun areContentsTheSame(a: JobItem, b: JobItem) = a == b
        }
    }
}
