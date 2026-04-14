package com.uiblueprint.android

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

data class FolderUploadArchiveResult(
    val archiveFile: File,
    val folderName: String,
    val totalFiles: Int,
    val structure: List<String>,
)

object FolderUploadArchive {
    @Throws(IOException::class)
    fun createZipFromTree(context: Context, treeUri: Uri): FolderUploadArchiveResult {
        val contentResolver = context.contentResolver
        val rootDocumentId = DocumentsContract.getTreeDocumentId(treeUri)
        val folderName = queryDisplayName(
            contentResolver,
            DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocumentId),
        ) ?: "folder-upload"
        val archiveFile = File.createTempFile("folder-upload-", ".zip", context.cacheDir)
        val structure = mutableListOf<String>()
        var totalFiles = 0

        ZipOutputStream(FileOutputStream(archiveFile)).use { zipOutput ->
            writeDocumentTree(
                contentResolver = contentResolver,
                treeUri = treeUri,
                parentDocumentId = rootDocumentId,
                relativePrefix = "",
                zipOutput = zipOutput,
                onFileAdded = { relativePath ->
                    totalFiles += 1
                    structure += relativePath
                },
            )
        }

        if (totalFiles == 0) {
            archiveFile.delete()
            throw IOException("Selected folder is empty")
        }

        return FolderUploadArchiveResult(
            archiveFile = archiveFile,
            folderName = folderName,
            totalFiles = totalFiles,
            structure = structure.sorted(),
        )
    }

    @Throws(IOException::class)
    private fun writeDocumentTree(
        contentResolver: ContentResolver,
        treeUri: Uri,
        parentDocumentId: String,
        relativePrefix: String,
        zipOutput: ZipOutputStream,
        onFileAdded: (String) -> Unit,
    ) {
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocumentId)
        val projection = arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
        )
        contentResolver.query(childrenUri, projection, null, null, null)?.use { cursor ->
            val documentIdIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            val displayNameIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            val mimeTypeIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)

            while (cursor.moveToNext()) {
                val documentId = cursor.getString(documentIdIndex)
                val displayName = cursor.getString(displayNameIndex) ?: continue
                val mimeType = cursor.getString(mimeTypeIndex) ?: continue
                val relativePath = buildRelativePath(relativePrefix, displayName)
                if (mimeType == DocumentsContract.Document.MIME_TYPE_DIR) {
                    writeDocumentTree(
                        contentResolver = contentResolver,
                        treeUri = treeUri,
                        parentDocumentId = documentId,
                        relativePrefix = relativePath,
                        zipOutput = zipOutput,
                        onFileAdded = onFileAdded,
                    )
                    continue
                }

                val documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, documentId)
                contentResolver.openInputStream(documentUri)?.use { inputStream ->
                    zipOutput.putNextEntry(ZipEntry(relativePath))
                    inputStream.copyTo(zipOutput)
                    zipOutput.closeEntry()
                    onFileAdded(relativePath)
                } ?: throw IOException("Cannot open file in selected folder: $relativePath")
            }
        } ?: throw IOException("Cannot list selected folder")
    }

    private fun queryDisplayName(contentResolver: ContentResolver, documentUri: Uri): String? {
        val projection = arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
        return contentResolver.query(documentUri, projection, null, null, null)?.use { cursor ->
            if (!cursor.moveToFirst()) return@use null
            cursor.getString(cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME))
        }
    }

    private fun buildRelativePath(prefix: String, displayName: String): String {
        val normalizedName = displayName
            .replace('\\', '/')
            .trim('/')
            .split('/')
            .filter { it.isNotBlank() && it != "." && it != ".." }
            .joinToString("/")
        return listOf(prefix.trim('/'), normalizedName)
            .filter { it.isNotBlank() }
            .joinToString("/")
    }
}
