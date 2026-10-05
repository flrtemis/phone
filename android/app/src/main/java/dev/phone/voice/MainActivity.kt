package dev.phone.voice

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The whole UI: one screen, six buttons, and a transcript.
 *
 * State lives here rather than in a ViewModel because a call is a screen-level
 * thing - if the activity goes away, so does the microphone, and that is the
 * behaviour people expect from a phone call they can see.
 */
class MainActivity : ComponentActivity() {

    private var session: VoiceSession? = null

    private val requestMic = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCall() else appendSystem("Microphone permission is required to talk to the agent.")
    }

    private val requestNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    // --- UI state -----------------------------------------------------
    private val turns = mutableStateListOf<Turn>()
    private val status = mutableStateOf("idle")
    private val level = mutableStateOf(0f)
    private val live = mutableStateOf(false)
    private val muted = mutableStateOf(false)
    private val speaker = mutableStateOf(false)
    private val dialling = mutableStateOf(false)
    private val busy = mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF7FD1A8))) {
                Surface(color = Color(0xFF0F1218), modifier = Modifier.fillMaxSize()) {
                    Screen()
                }
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        handleIntent(intent)
        // Ask the server whether it can dial: the "Ring my phone" button is only
        // offered when the server says an owner number is configured, so the UI
        // never promises something the server will refuse.
        if (load().isConfigured) {
            CoroutineScope(Dispatchers.IO).launch {
                try {
                    val config = AgentApi(load()).config()
                    withContext(Dispatchers.Main) {
                        dialling.value = config.dialling
                        appendSystem("Connected to ${config.title}")
                    }
                } catch (error: Exception) {
                    withContext(Dispatchers.Main) { appendSystem("Server not reachable yet: ${error.message}") }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    /** `phonevoice://connect?url=...&token=...` - what a QR code or a script sends. */
    private fun handleIntent(intent: Intent?) {
        val data: Uri = intent?.data ?: return
        val url = data.getQueryParameter("url") ?: return
        val token = data.getQueryParameter("token") ?: ""
        save(url, token)
        appendSystem("Server set from a link.")
    }

    override fun onDestroy() {
        session?.close()
        session = null
        CallService.stop(this)
        super.onDestroy()
    }

    // --- settings -----------------------------------------------------
    private fun prefs() = getSharedPreferences("phone-voice", Context.MODE_PRIVATE)

    private fun load(): ServerSettings = ServerSettings(
        url = prefs().getString("url", "") ?: "",
        token = prefs().getString("token", "") ?: "",
    )

    private fun save(url: String, token: String) {
        prefs().edit().putString("url", url.trim()).putString("token", token.trim()).apply()
    }

    // --- call control -------------------------------------------------
    private fun startCall() {
        val settings = load()
        if (!settings.isConfigured) {
            appendSystem("Set the server URL and token first.")
            return
        }
        CallService.start(this)
        session = VoiceSession(
            settings = settings,
            onState = { state ->
                status.value = when (state) {
                    VoiceSession.State.IDLE -> "idle"
                    VoiceSession.State.CONNECTING -> "connecting"
                    VoiceSession.State.LIVE -> "live"
                    VoiceSession.State.ENDED -> "ended"
                    VoiceSession.State.FAILED -> "failed"
                }
                live.value = state == VoiceSession.State.LIVE
                if (state == VoiceSession.State.LIVE) appendSystem("Connected. Say something.")
            },
            onTurn = { turns.add(it) },
            onLevel = { level.value = it },
            onEnded = { reason ->
                live.value = false
                appendSystem("Call ended: $reason")
                CallService.stop(this)
                session = null
            },
        ).also { it.connect() }
    }

    private fun endCall() {
        session?.hangup()
        session?.close()
        session = null
        live.value = false
        status.value = "ended"
        level.value = 0f
        CallService.stop(this)
    }

    private fun appendSystem(text: String) {
        turns.add(Turn(fromCaller = false, text = text))
    }

    // --- the screen ---------------------------------------------------
    @Composable
    private fun Screen() {
        val scope = rememberCoroutineScope()
        var url by remember { mutableStateOf(load().url) }
        var token by remember { mutableStateOf(load().token) }
        var note by remember { mutableStateOf("") }
        var noteState by remember { mutableStateOf("") }
        val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager

        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(20.dp),
        ) {
            Text("phone voice", color = Color(0xFF7FD1A8), fontSize = 18.sp)
            Text(
                "your agent, on your server — audio never goes anywhere else",
                color = Color(0xFF7A869A),
                fontSize = 12.sp,
                modifier = Modifier.padding(bottom = 16.dp),
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(10.dp)
                        .background(if (live.value) Color(0xFF7FD1A8) else Color(0xFF7A869A)),
                )
                Spacer(Modifier.width(8.dp))
                Text(status.value, fontSize = 14.sp)
            }
            Spacer(Modifier.height(10.dp))
            LinearProgressIndicator(
                progress = { (level.value / 6000f).coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().height(6.dp),
                color = Color(0xFF7FD1A8),
                trackColor = Color(0xFF1E2633),
            )

            Spacer(Modifier.height(18.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                if (!live.value) {
                    Button(
                        onClick = {
                            if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO)
                                == PackageManager.PERMISSION_GRANTED
                            ) {
                                startCall()
                            } else {
                                requestMic.launch(Manifest.permission.RECORD_AUDIO)
                            }
                        },
                    ) { Text(getString(R.string.call)) }
                } else {
                    Button(onClick = { endCall() }) { Text(getString(R.string.hangup)) }
                    OutlinedButton(
                        onClick = {
                            muted.value = !muted.value
                            session?.setMuted(muted.value)
                        },
                    ) {
                        Text(if (muted.value) getString(R.string.unmute) else getString(R.string.mute))
                    }
                    OutlinedButton(
                        onClick = {
                            speaker.value = !speaker.value
                            audioManager.let { manager ->
                                @Suppress("DEPRECATION")
                                manager.isSpeakerphoneOn = speaker.value
                            }
                        },
                    ) { Text(if (speaker.value) "Earpiece" else "Speaker") }
                }
            }

            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(
                    enabled = !busy.value && dialling.value,
                    onClick = {
                        busy.value = true
                        scope.launch {
                            try {
                                appendSystem(AgentApi(load()).ringMyPhone())
                            } catch (error: Exception) {
                                appendSystem("Could not place the call: ${error.message}")
                            } finally {
                                busy.value = false
                            }
                        }
                    },
                ) { Text(getString(R.string.ring_phone)) }
                OutlinedButton(
                    enabled = !busy.value,
                    onClick = {
                        busy.value = true
                        scope.launch {
                            try {
                                AgentApi(load()).transcript(30).forEach { turns.add(it) }
                            } catch (error: Exception) {
                                appendSystem("No transcript: ${error.message}")
                            } finally {
                                busy.value = false
                            }
                        }
                    },
                ) { Text("Last transcript") }
            }

            Spacer(Modifier.height(20.dp))
            Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF161B24))) {
                Column(Modifier.padding(14.dp).heightIn(min = 160.dp, max = 320.dp).verticalScroll(rememberScrollState())) {
                    if (turns.isEmpty()) {
                        Text("Not connected.", color = Color(0xFF7A869A), fontSize = 13.sp)
                    }
                    turns.forEach { turn ->
                        val prefix = if (turn.fromCaller) "you: " else "agent: "
                        Text(
                            prefix + turn.text,
                            fontSize = 14.sp,
                            color = if (turn.fromCaller) Color(0xFFE6B45C) else Color(0xFFD8DEE9),
                            modifier = Modifier.padding(bottom = 6.dp),
                        )
                    }
                }
            }

            Spacer(Modifier.height(16.dp))
            Text(getString(R.string.settings), color = Color(0xFF7A869A), fontSize = 12.sp)
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("server") },
                placeholder = { Text(getString(R.string.server_hint)) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("token") },
                placeholder = { Text(getString(R.string.token_hint)) },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Button(onClick = { save(url, token); appendSystem("Saved.") }) { Text("Save") }

            Spacer(Modifier.height(20.dp))
            OutlinedTextField(
                value = note,
                onValueChange = { note = it },
                label = { Text(getString(R.string.note_hint)) },
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(
                    enabled = note.isNotBlank() && !busy.value,
                    onClick = {
                        busy.value = true
                        scope.launch {
                            try {
                                AgentApi(load()).saveNote(note)
                                noteState = "saved — read at the start of the next call"
                                note = ""
                            } catch (error: Exception) {
                                noteState = "not saved: ${error.message}"
                            } finally {
                                busy.value = false
                            }
                        }
                    },
                ) { Text(getString(R.string.save_note)) }
                Text(noteState, color = Color(0xFF7A869A), fontSize = 12.sp)
            }

            Spacer(Modifier.height(20.dp))
            Text(
                "This app has no analytics and no other network traffic. It records only " +
                    "while a call is active, and the notification stays up for exactly as long.",
                color = Color(0xFF7A869A),
                fontSize = 11.sp,
            )
        }
    }

}
