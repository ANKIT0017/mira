// ChatScreen.kt (Jetpack Compose)
package com.aion.ui

import androidx.compose.material.*
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.layout.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun ChatScreen() {
    var text by remember { mutableStateOf("") }
    val ctx = LocalContext.current
    Column(modifier = Modifier.fillMaxSize().padding(12.dp)) {
        OutlinedTextField(value = text, onValueChange={text = it}, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        Button(onClick = { /* send to agent via MCP */ }) { Text("Send") }
    }
}
