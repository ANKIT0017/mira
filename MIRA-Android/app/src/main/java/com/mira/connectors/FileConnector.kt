// FileConnector.kt
package com.aion.connectors

import android.content.Context
import java.io.File

object FileConnector {
    fun listFiles(ctx: Context, path: String): List<String> {
        val dir = File(path)
        if (!dir.exists() || !dir.isDirectory) return emptyList()
        return dir.list()?.toList() ?: emptyList()
    }
}
