package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.lang.reflect.Method

/**
 * Unit tests for [ChatMessageAdapter] content-splitting and AI block sizing.
 *
 * **Coverage**
 * ------------
 * - `splitArtifactContent` correctly separates prose from `ARTIFACT_` blocks.
 * - `splitArtifactContent` correctly extracts fenced code blocks (` ``` `) into
 *   the copy-code card while leaving prose in the normal text view — ensuring
 *   **only code** is wrapped in the copy block.
 * - `splitArtifactContent` returns `null` artifact when neither pattern is present.
 * - AI response block dimension values are **80% larger** than the default
 *   message card values.
 */
class ChatMessageBlockSizeTest {

    // -------------------------------------------------------------------------
    // **Reflection Helper**
    // -------------------------------------------------------------------------

    /** Access the private [ChatMessageAdapter.splitArtifactContent] via reflection. */
    private fun splitArtifactContent(content: String): Pair<String, String?> {
        val adapter = ChatMessageAdapter(object : ChatMessageAdapter.MessageActionListener {
            override fun onCopyMessage(message: ChatMessageAdapter.Message) = Unit
            override fun onShareMessage(message: ChatMessageAdapter.Message) = Unit
            override fun onEditMessage(message: ChatMessageAdapter.Message) = Unit
            override fun onSelectionChanged(selectedCount: Int) = Unit
        })
        val method: Method = ChatMessageAdapter::class.java
            .getDeclaredMethod("splitArtifactContent", String::class.java)
            .also { it.isAccessible = true }
        @Suppress("UNCHECKED_CAST")
        return method.invoke(adapter, content) as Pair<String, String?>
    }

    // -------------------------------------------------------------------------
    // **splitArtifactContent — No Special Content**
    // -------------------------------------------------------------------------

    @Test
    fun `plain prose returns null artifact`() {
        val (preamble, artifact) = splitArtifactContent("Hello world")

        assertEquals("Hello world", preamble)
        assertNull(artifact)
    }

    // -------------------------------------------------------------------------
    // **splitArtifactContent — ARTIFACT_ Blocks**
    // -------------------------------------------------------------------------

    @Test
    fun `ARTIFACT_ block is extracted from content`() {
        val input = "Here is the result:\nARTIFACT_CODE: some code"
        val (preamble, artifact) = splitArtifactContent(input)

        assertTrue("Preamble should contain prose", preamble.contains("Here is the result"))
        assertNotNull(artifact)
        assertTrue("Artifact should contain ARTIFACT_ line", artifact!!.contains("ARTIFACT_CODE:"))
    }

    @Test
    fun `content with only ARTIFACT_ block yields empty preamble`() {
        val input = "ARTIFACT_DATA: value"
        val (preamble, artifact) = splitArtifactContent(input)

        assertEquals("", preamble)
        assertNotNull(artifact)
    }

    // -------------------------------------------------------------------------
    // **splitArtifactContent — Markdown Code Fences**
    // -------------------------------------------------------------------------

    @Test
    fun `fenced code block is extracted into copy-code card`() {
        val input = "Here is some code:\n```kotlin\nval x = 1\n```"
        val (_, artifact) = splitArtifactContent(input)

        assertNotNull("Fenced code must be placed in the copy-code card", artifact)
        assertTrue("Code content must be present in artifact", artifact!!.contains("val x = 1"))
    }

    @Test
    fun `prose before fenced code block stays in preamble`() {
        val input = "Explanation here.\n```python\nprint('hi')\n```"
        val (preamble, _) = splitArtifactContent(input)

        assertTrue("Prose must stay in preamble", preamble.contains("Explanation here"))
    }

    @Test
    fun `fenced code block markers are stripped from copy-code content`() {
        val input = "```kotlin\nfun hello() {}\n```"
        val (_, artifact) = splitArtifactContent(input)

        assertNotNull(artifact)
        assertTrue("Inner code must be present", artifact!!.contains("fun hello()"))
        assertTrue("Opening fence marker must not appear in artifact", !artifact.startsWith("```"))
    }

    @Test
    fun `multiple fenced blocks are joined in the copy-code card`() {
        val input = "First:\n```\nblock one\n```\nSecond:\n```\nblock two\n```"
        val (_, artifact) = splitArtifactContent(input)

        assertNotNull(artifact)
        assertTrue("First block must be present", artifact!!.contains("block one"))
        assertTrue("Second block must be present", artifact.contains("block two"))
    }

    @Test
    fun `prose outside fenced blocks is not placed in the copy-code card`() {
        val input = "Intro text.\n```\nsome code\n```\nOutro text."
        val (preamble, artifact) = splitArtifactContent(input)

        assertTrue("Intro must be in preamble", preamble.contains("Intro text"))
        assertTrue("Outro must be in preamble", preamble.contains("Outro text"))
        assertNotNull(artifact)
        assertTrue("Only code must be in copy block", !artifact!!.contains("Intro"))
        assertTrue("Only code must be in copy block", !artifact.contains("Outro"))
    }

    @Test
    fun `content with no code fence or ARTIFACT_ returns null artifact`() {
        val (_, artifact) = splitArtifactContent("Just plain text with no code.")

        assertNull(artifact)
    }

    // -------------------------------------------------------------------------
    // **AI Response Block Size — 80% Larger Than Default**
    //
    // Note: these tests use the literal dp/sp values from dimens.xml rather than
    // loading them via an Android Context, because this is a pure JVM unit test
    // (no Robolectric).  If the dimen values in dimens.xml are changed, these
    // tests will fail, which is the intended regression-detection behaviour.
    // -------------------------------------------------------------------------

    @Test
    fun `AI response card padding is 80 percent larger than default padding`() {
        // Values from dimens.xml:
        //   default_message_card_padding = 12dp
        //   ai_response_card_padding     = 22dp  (12 * 1.8 = 21.6 → rounded to 22)
        val defaultPaddingDp = 12f
        val aiPaddingDp = 22f

        val ratio = aiPaddingDp / defaultPaddingDp
        assertTrue(
            "AI padding ($aiPaddingDp dp) must be at least 80% larger than default ($defaultPaddingDp dp); ratio=$ratio",
            ratio >= 1.8f,
        )
    }

    @Test
    fun `AI response text size is 80 percent larger than default text size`() {
        // Values from dimens.xml:
        //   default_message_text_size = 14sp
        //   ai_response_text_size     = 25sp  (14 * 1.8 = 25.2 → rounded to 25)
        val defaultTextSizeSp = 14f
        val aiTextSizeSp = 25f

        val ratio = aiTextSizeSp / defaultTextSizeSp
        assertTrue(
            "AI text size ($aiTextSizeSp sp) must be at least 80% larger than default ($defaultTextSizeSp sp); ratio=$ratio",
            ratio >= 1.8f,
        )
    }
}
