// AgentService.kt
package com.aion

import android.app.Service
import android.content.Intent
import android.os.IBinder

class AgentService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null
    // Implement background agent runtime (connect to WebSocket or local model)
}
