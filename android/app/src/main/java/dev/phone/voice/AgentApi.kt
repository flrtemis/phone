package dev.phone.voice

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * The small JSON API on the same server: ring my phone, leave a note, read the
 * last transcript. Deliberately thin - every call is one request, and every error
 * is the server's own sentence rather than a generic "something went wrong".
 */
class AgentApi(private val settings: ServerSettings) {

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()

        private val client = OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS) // /api/ask waits for the model
            .build()
    }

    class ApiError(message: String) : IOException(message)

    private fun request(path: String, body: JSONObject? = null): Request {
        val builder = Request.Builder()
            .url(settings.httpBase() + path)
            .header("Authorization", "Bearer ${settings.token}")
        if (body == null) {
            builder.get()
        } else {
            builder.post(body.toString().toRequestBody(JSON))
        }
        return builder.build()
    }

    private suspend fun call(path: String, body: JSONObject? = null): JSONObject =
        withContext(Dispatchers.IO) {
            try {
                client.newCall(request(path, body)).execute().use { response ->
                    val text = response.body?.string().orEmpty()
                    val payload = try {
                        JSONObject(text)
                    } catch (_: Exception) {
                        JSONObject()
                    }
                    if (!response.isSuccessful) {
                        throw ApiError(payload.optString("error", "HTTP ${response.code}"))
                    }
                    payload
                }
            } catch (error: ApiError) {
                throw error
            } catch (error: IOException) {
                throw ApiError(error.message ?: "cannot reach the server")
            }
        }

    suspend fun config(): ServerConfig {
        val payload = call("/api/config")
        return ServerConfig(
            title = payload.optString("title", "phone voice"),
            greeting = payload.optString("greeting", ""),
            dialling = payload.optBoolean("dialling", false),
            maxCalls = payload.optInt("max_calls", 1),
        )
    }

    /** Ask the agent to ring the owner's number. The server refuses any other. */
    suspend fun ringMyPhone(): String {
        val payload = call("/api/call-me", JSONObject().put("to", "owner"))
        return payload.optString("detail", "calling")
    }

    suspend fun saveNote(text: String): String {
        val payload = call("/api/note", JSONObject().put("text", text))
        return payload.optString("status", "stored")
    }

    suspend fun ask(prompt: String): String {
        val payload = call("/api/ask", JSONObject().put("prompt", prompt))
        return payload.optString("reply", "")
    }

    suspend fun transcript(lines: Int = 20): List<Turn> {
        val payload = call("/api/transcript?lines=$lines")
        val entries = payload.optJSONArray("entries") ?: return emptyList()
        val turns = mutableListOf<Turn>()
        for (index in 0 until entries.length()) {
            val entry = entries.optJSONObject(index) ?: continue
            turns.add(
                Turn(
                    fromCaller = entry.optString("role") == "user",
                    text = entry.optString("text"),
                ),
            )
        }
        return turns
    }
}
