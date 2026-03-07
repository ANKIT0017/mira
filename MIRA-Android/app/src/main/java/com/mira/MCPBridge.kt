// MCPBridge.kt
package com.aion

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.WebSocket
import okhttp3.WebSocketListener

object MCPBridge {
    // Use OkHttp WebSocket to connect to ws://localhost:8765 (or use adb reverse / local socket)
    val client = OkHttpClient()
    fun connect(url: String, listener: WebSocketListener): WebSocket {
        val req = Request.Builder().url(url).build()
        return client.newWebSocket(req, listener)
    }
}
