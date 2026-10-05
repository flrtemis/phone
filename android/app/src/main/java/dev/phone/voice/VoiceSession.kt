package dev.phone.voice

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * One connection to the agent: the WebSocket, the audio, and the conversation.
 *
 * Lifetime is deliberately simple: connect -> start audio -> talk -> close. There
 * is no reconnect logic, because a reconnecting *microphone* that you did not ask
 * for is a bug, not a feature.
 */
class VoiceSession(
    private val settings: ServerSettings,
    private val onState: (State) -> Unit,
    private val onTurn: (Turn) -> Unit,
    private val onLevel: (Float) -> Unit,
    private val onEnded: (String) -> Unit,
) {
    enum class State { IDLE, CONNECTING, LIVE, ENDED, FAILED }

    companion object {
        private const val TAG = "phone-voice/session"

        /** Read timeout is generous: the agent may think for a few seconds. */
        private val client = OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS) // WebSockets are long-lived
            .pingInterval(20, TimeUnit.SECONDS)    // keeps NAT mappings alive in a tunnel
            .build()
    }

    private var socket: WebSocket? = null
    private var audio: AudioEngine? = null
    @Volatile private var muted = false
    val turns = mutableListOf<Turn>()
    var isLive: Boolean = false
        private set

    fun connect() {
        if (isLive) return
        onState(State.CONNECTING)
        val engine = AudioEngine(
            onFrame = { frame ->
                // Only send while live: audio captured during the handshake belongs
                // to a call the server has not agreed to yet.
                // Muting is client-side and total: the frames are simply not
                // sent, so the server has nothing to transcribe. (The microphone
                // stays open, which is why the notification stays up.)
                if (isLive && !muted) socket?.send(ByteString.of(*frame))
            },
            onLevel = onLevel,
        )
        audio = engine
        if (!engine.start()) {
            onState(State.FAILED)
            onEnded("this device cannot open a microphone at ${Protocol.REQUEST_RATE} Hz")
            return
        }
        val request = Request.Builder().url(settings.socketUrl()).build()
        socket = client.newWebSocket(request, Listener())
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            webSocket.send(Protocol.hello(Protocol.REQUEST_RATE))
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val event = try {
                JSONObject(text)
            } catch (error: Exception) {
                Log.d(TAG, "ignoring unparseable message")
                return
            }
            when (event.optString("type")) {
                "ready" -> {
                    isLive = true
                    onState(State.LIVE)
                }
                "turn" -> {
                    val turn = Turn(
                        fromCaller = event.optString("role") == "user",
                        text = event.optString("text"),
                    )
                    turns.add(turn)
                    onTurn(turn)
                }
                "bye" -> {
                    isLive = false
                    onState(State.ENDED)
                    onEnded(event.optString("reason", "hangup"))
                    close()
                }
                else -> Log.d(TAG, "ignoring event ${event.optString("type")}")
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val engine = audio ?: return
            // Barge-in: if we start talking, drop what the agent was still saying.
            // The server does the same thing on its side; doing it here as well
            // means the speaker goes quiet immediately rather than one round trip
            // later, which is the difference between "interrupted" and "talked over".
            engine.play(bytes.toByteArray())
        }

        override fun onFailure(webSocket: WebSocket, error: Throwable, response: Response?) {
            isLive = false
            val detail = response?.message?.let { " (${response.code} $it)" } ?: ""
            Log.w(TAG, "websocket failed: ${error.message}$detail")
            onState(State.FAILED)
            onEnded(error.message ?: "connection failed")
            close()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            isLive = false
            onState(State.ENDED)
            onEnded(if (code == 1000) "ended" else "closed ($code $reason)")
            close()
        }
    }

    /** Stop sending audio without tearing the call down. */
    fun setMuted(value: Boolean) {
        muted = value
    }

    fun sendDigit(digit: Char) {
        if (isLive) socket?.send(Protocol.dtmf(digit))
    }

    /** Ask the server to stop; the server closes the socket once the call ends. */
    fun hangup() {
        if (isLive) socket?.send(Protocol.hangup())
        isLive = false
    }

    fun close() {
        isLive = false
        socket?.close(1000, "client closing")
        socket = null
        audio?.stop()
        audio = null
    }
}
