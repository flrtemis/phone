package dev.phone.voice

import org.json.JSONObject

/**
 * The wire protocol, in one file.
 *
 * The server side lives in `voice/websocket.py` and `voice/web.py`; this is the
 * client half, deliberately kept small enough to audit in one sitting:
 *
 *   -> {"type":"hello","sample_rate":16000}
 *   <- {"type":"ready","session_rate":8000,"client_rate":16000}
 *   -> binary frames: signed 16-bit little-endian mono PCM
 *   <- binary frames: the same, in the agent's voice
 *   -> {"type":"dtmf","digit":"5"} | {"type":"hangup"}
 *   <- {"type":"turn","role":"user"|"assistant","text":"..."}
 *   <- {"type":"bye","reason":"caller-hung-up"}
 *
 * The sample rate is whatever the device will actually give us. This app asks for
 * 16 kHz (every Android device supports it, unlike 8 kHz on some), and the server
 * resamples to 8 kHz for the voice-activity detector, whisper and the model.
 */
object Protocol {
    const val SESSION_RATE = 8000
    const val REQUEST_RATE = 16000
    const val FRAME_MS = 20

    /** Samples per frame at the requested capture rate: 320 at 16 kHz. */
    val frameSamples: Int get() = REQUEST_RATE / 1000 * FRAME_MS

    fun hello(sampleRate: Int): String =
        JSONObject()
            .put("type", "hello")
            .put("sample_rate", sampleRate)
            .toString()

    fun hangup(): String = JSONObject().put("type", "hangup").toString()

    fun dtmf(digit: Char): String =
        JSONObject()
            .put("type", "dtmf")
            .put("digit", digit.toString())
            .toString()
}

/** One line of the conversation, as the UI shows it. */
data class Turn(val fromCaller: Boolean, val text: String)

/** What the server says about itself on connect. */
data class ServerConfig(
    val title: String,
    val greeting: String,
    val dialling: Boolean,
    val maxCalls: Int,
)

/** Where the app connects. Stored in private preferences; never exported. */
data class ServerSettings(val url: String, val token: String) {
    val isConfigured: Boolean get() = url.isNotBlank() && token.isNotBlank()

    /** The wss:// URL for the audio socket, with the token in the query string. */
    fun socketUrl(): String {
        val base = url.trim().trimEnd('/')
            .replaceFirst("^https://", "wss://")
            .replaceFirst("^http://", "ws://")
        return "$base/ws?token=${java.net.URLEncoder.encode(token, "UTF-8")}"
    }

    fun httpBase(): String = url.trim().trimEnd('/')
}
