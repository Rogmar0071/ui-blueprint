package com.uiblueprint.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.util.TypedValue
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView

/**
 * RecyclerView adapter for global chat messages.
 *
 * **Features**
 * ------------
 * - Always-visible action row: Copy, Share (all messages), Edit (user messages only).
 * - Artifact rendering: if a message contains lines beginning with `ARTIFACT_`,
 *   those are displayed in a monospace card with its own Copy button.
 * - **Code-fence rendering**: markdown fenced code blocks (` ``` `) in AI responses
 *   are extracted and rendered in the same monospace copy-code card.  Only code
 *   appears in the copy block; prose text stays in the normal message view.
 * - Multi-select mode: long-press a message to enter selection mode.
 *   Selected messages are highlighted. The host activity is notified via
 *   [SelectionListener] to show/hide a contextual action toolbar.
 * - [superseded] messages (original user messages after editing) are shown
 *   with reduced opacity and a "superseded" label so the user can see history
 *   but understand the message has been replaced.
 */
class ChatMessageAdapter(
    private val listener: MessageActionListener,
) : RecyclerView.Adapter<ChatMessageAdapter.ViewHolder>() {

    // -------------------------------------------------------------------------
    // **Public Data Class**
    // -------------------------------------------------------------------------

    data class Message(
        val id: String,
        val role: String,          // "user" | "assistant" | "system"
        val content: String,
        val superseded: Boolean = false,
    )

    // -------------------------------------------------------------------------
    // **Listener Interfaces**
    // -------------------------------------------------------------------------

    interface MessageActionListener {
        fun onCopyMessage(message: Message)
        fun onShareMessage(message: Message)
        fun onEditMessage(message: Message)
        fun onSelectionChanged(selectedCount: Int)
    }

    // -------------------------------------------------------------------------
    // **State**
    // -------------------------------------------------------------------------

    private val items = mutableListOf<Message>()
    private val selectedIds = mutableSetOf<String>()
    var isMultiSelectMode: Boolean = false
        private set

    // -------------------------------------------------------------------------
    // **ViewHolder**
    // -------------------------------------------------------------------------

    class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val cardMessage: MaterialCardView = view.findViewById(R.id.cardMessage)
        val layoutMessageContent: LinearLayout = view.findViewById(R.id.layoutMessageContent)
        val tvRole: TextView = view.findViewById(R.id.tvRole)
        val tvContent: TextView = view.findViewById(R.id.tvContent)
        val cardArtifact: MaterialCardView = view.findViewById(R.id.cardArtifact)
        val tvArtifact: TextView = view.findViewById(R.id.tvArtifact)
        val btnCopyArtifact: MaterialButton = view.findViewById(R.id.btnCopyArtifact)
        val btnCopy: MaterialButton = view.findViewById(R.id.btnCopy)
        val btnShare: MaterialButton = view.findViewById(R.id.btnShare)
        val btnEdit: MaterialButton = view.findViewById(R.id.btnEdit)
    }

    // -------------------------------------------------------------------------
    // **Adapter Overrides**
    // -------------------------------------------------------------------------

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_chat_message, parent, false)
        return ViewHolder(view)
    }

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val msg = items[position]
        val context = holder.itemView.context

        // Role label
        holder.tvRole.text = if (msg.role == "user") "You" else "AI"

        val paddingPx = context.resources.getDimensionPixelSize(R.dimen.default_message_card_padding)
        holder.layoutMessageContent.setPadding(paddingPx, paddingPx, paddingPx, paddingPx)
        holder.tvContent.setTextSize(
            TypedValue.COMPLEX_UNIT_PX,
            context.resources.getDimension(R.dimen.default_message_text_size),
        )

        // Superseded styling
        holder.itemView.alpha = if (msg.superseded) 0.45f else 1.0f

        // --- Artifact detection ---
        val (normalText, artifactText) = splitArtifactContent(msg.content)

        if (artifactText != null) {
            // Show normal text (preamble before artifact blocks) if present.
            if (normalText.isNotBlank()) {
                holder.tvContent.visibility = View.VISIBLE
                holder.tvContent.text = normalText.trim()
            } else {
                holder.tvContent.visibility = View.GONE
            }
            holder.cardArtifact.visibility = View.VISIBLE
            holder.tvArtifact.text = artifactText
            holder.btnCopyArtifact.setOnClickListener {
                copyToClipboard(context, artifactText)
            }
        } else {
            holder.tvContent.visibility = View.VISIBLE
            holder.tvContent.text = msg.content
            holder.cardArtifact.visibility = View.GONE
        }

        // --- Edit button (user messages only, not superseded) ---
        if (msg.role == "user" && !msg.superseded) {
            holder.btnEdit.visibility = View.VISIBLE
            holder.btnEdit.setOnClickListener { listener.onEditMessage(msg) }
        } else {
            holder.btnEdit.visibility = View.GONE
        }

        // --- Copy / Share actions ---
        holder.btnCopy.setOnClickListener { listener.onCopyMessage(msg) }
        holder.btnShare.setOnClickListener { listener.onShareMessage(msg) }

        // --- Multi-select: highlight selected items ---
        val isSelected = selectedIds.contains(msg.id)
        holder.cardMessage.isChecked = isSelected
        holder.itemView.isActivated = isSelected

        // Long-press enters / adds to multi-select mode.
        holder.cardMessage.setOnLongClickListener {
            if (!isMultiSelectMode) {
                isMultiSelectMode = true
            }
            toggleSelection(msg.id)
            true
        }

        // Tap while in multi-select mode toggles selection.
        holder.cardMessage.setOnClickListener {
            if (isMultiSelectMode) {
                toggleSelection(msg.id)
            }
        }
    }

    // -------------------------------------------------------------------------
    // **Data Helpers**
    // -------------------------------------------------------------------------

    fun submitList(newItems: List<Message>) {
        items.clear()
        items.addAll(newItems)
        selectedIds.clear()
        isMultiSelectMode = false
        notifyDataSetChanged()
    }

    // -------------------------------------------------------------------------
    // **Multi-Select Helpers**
    // -------------------------------------------------------------------------

    private fun toggleSelection(id: String) {
        if (selectedIds.contains(id)) {
            selectedIds.remove(id)
        } else {
            selectedIds.add(id)
        }
        if (selectedIds.isEmpty()) {
            isMultiSelectMode = false
        }
        notifyDataSetChanged()
        listener.onSelectionChanged(selectedIds.size)
    }

    fun selectAll() {
        selectedIds.clear()
        selectedIds.addAll(items.map { it.id })
        isMultiSelectMode = true
        notifyDataSetChanged()
        listener.onSelectionChanged(selectedIds.size)
    }

    fun clearSelection() {
        selectedIds.clear()
        isMultiSelectMode = false
        notifyDataSetChanged()
        listener.onSelectionChanged(0)
    }

    fun getSelectedMessages(): List<Message> =
        items.filter { selectedIds.contains(it.id) }

    // -------------------------------------------------------------------------
    // **Artifact / Code Parsing**
    // -------------------------------------------------------------------------

    /**
     * Splits [content] into a `(preamble, codeBlock?)` pair.
     *
     * **Priority 1 — ARTIFACT_ blocks:** if any line starts with `ARTIFACT_`
     * followed by a colon, all lines from that point onward are treated as the
     * artifact block and placed in the copy-code card.  Text before the first
     * `ARTIFACT_` line is returned as preamble.
     *
     * **Priority 2 — Markdown code fences:** if the content contains one or more
     * fenced code blocks (` ``` `), only the **code inside the fences** is placed
     * in the copy-code card.  The surrounding prose is returned as preamble.
     * When multiple fenced blocks are present they are joined with a blank line.
     *
     * Returns `(content, null)` when neither pattern is found.
     */
    private fun splitArtifactContent(content: String): Pair<String, String?> {
        // --- Priority 1: ARTIFACT_ blocks (existing behaviour) ---
        val lines = content.lines()
        val firstArtifactIdx = lines.indexOfFirst {
            val trimmed = it.trimStart()
            trimmed.startsWith("ARTIFACT_") && trimmed.contains(":")
        }
        if (firstArtifactIdx >= 0) {
            val preamble = lines.take(firstArtifactIdx).joinToString("\n")
            val artifactPart = lines.drop(firstArtifactIdx).joinToString("\n")
            return preamble to artifactPart
        }

        // --- Priority 2: Markdown fenced code blocks ---
        val fenceMatches = CODE_FENCE_REGEX.findAll(content).toList()
        if (fenceMatches.isEmpty()) return content to null

        // Extract inner code from each fence, stripping the opening/closing markers.
        val codeBlocks = fenceMatches.joinToString("\n\n") { match ->
            CODE_FENCE_INNER_REGEX.find(match.value)?.groupValues?.get(1)?.trimEnd() ?: match.value
        }

        // Remove fenced blocks from the prose, collapsing any resulting blank lines.
        val proseOnly = CODE_FENCE_REGEX.replace(content, "")
            .replace(Regex("\n{3,}"), "\n\n")
            .trim()

        return proseOnly to codeBlocks
    }

    // -------------------------------------------------------------------------
    // **Clipboard Helper**
    // -------------------------------------------------------------------------

    private fun copyToClipboard(context: Context, text: String) {
        val clipboard = ContextCompat.getSystemService(context, ClipboardManager::class.java)
        clipboard?.setPrimaryClip(ClipData.newPlainText("chat_artifact", text))
    }

    // -------------------------------------------------------------------------
    // **Companion**
    // -------------------------------------------------------------------------

    companion object {
        /**
         * Matches a complete fenced code block including its opening/closing ` ``` `
         * markers and optional language tag (e.g. ` ```kotlin `).
         * The newline after the opening marker is optional to handle single-line fences.
         */
        private val CODE_FENCE_REGEX = Regex("""```[\w]*\n?[\s\S]*?```""")

        /**
         * Captures the **inner code content** of a single fenced block,
         * stripping the opening marker (with optional language tag) and
         * the closing ` ``` `.
         * The newline after the opening marker is optional to handle single-line fences.
         */
        private val CODE_FENCE_INNER_REGEX = Regex("""```[\w]*\n?([\s\S]*?)```""")
    }
}
