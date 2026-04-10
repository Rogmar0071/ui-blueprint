package com.uiblueprint.android

import android.content.Intent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

/**
 * RecyclerView adapter for the Projects drawer folder list.
 * Displays folders fetched from GET /v1/folders; each row navigates to
 * [FolderDetailActivity] when tapped.
 */
class FolderAdapter : RecyclerView.Adapter<FolderAdapter.ViewHolder>() {

    private val items = mutableListOf<MainActivity.FolderItem>()

    class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val tvLabel: TextView = view.findViewById(R.id.tvFolderItemLabel)
        val tvStatus: TextView = view.findViewById(R.id.tvFolderItemStatus)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_folder, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val item = items[position]
        holder.tvLabel.text = "📁 ${item.label}"
        holder.tvStatus.text = item.status
        holder.itemView.setOnClickListener {
            val intent = Intent(holder.itemView.context, FolderDetailActivity::class.java)
            intent.putExtra(FolderDetailActivity.EXTRA_FOLDER_ID, item.id)
            holder.itemView.context.startActivity(intent)
        }
    }

    override fun getItemCount(): Int = items.size

    /** Replace the entire list (used when reloading from server). */
    fun submitList(newItems: List<MainActivity.FolderItem>) {
        items.clear()
        items.addAll(newItems)
        notifyDataSetChanged()
    }

    /** Prepend an item only if its id is not already in the list. */
    fun prependIfAbsent(item: MainActivity.FolderItem) {
        if (items.none { it.id == item.id }) {
            items.add(0, item)
            notifyItemInserted(0)
        }
    }
}
