package dev.phone.voice

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Capture and playback, nothing else.
 *
 * Two decisions worth explaining:
 *
 * 1. **`VOICE_COMMUNICATION` as the capture source.** It is the source that turns
 *    on the platform's echo canceller, noise suppressor and AGC. Without it the
 *    agent hears itself through the phone's speaker and the conversation becomes
 *    a feedback loop - the single most common way a DIY voice agent fails in the
 *    real world, and the reason Asterisk's AudioSocket (a raw media tap) cannot
 *    do this job on its own.
 *
 * 2. **Playback is scheduled, not queued.** Frames arrive over a mobile network
 *    with jitter; writing them straight into the track makes the audio stutter.
 *    The track is opened in streaming mode with a modest buffer and fed through a
 *    small ring of buffers, which costs ~100 ms of latency and buys audible
 *    continuity. Human turn-taking tolerates a second of silence, not a
 *    stuttering voice.
 *
 * The class owns no network code: frames go in and out through the callbacks, so
 * the same engine would work over the AudioSocket transport if someone wired the
 * app to a socket on the LAN.
 */
class AudioEngine(
    private val onFrame: (ByteArray) -> Unit,
    private val onLevel: (Float) -> Unit,
) {
    companion object {
        private const val TAG = "phone-voice/audio"

        /** Playback ring: 8 x 20 ms. Enough to absorb jitter, short enough to interrupt. */
        private const val PLAY_BUFFERS = 8
    }

    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var captureThread: Thread? = null
    private var playbackThread: Thread? = null
    private val running = AtomicBoolean(false)
    private val playbackQueue = ArrayBlockingQueue<ByteArray>(PLAY_BUFFERS * 4)

    val isRunning: Boolean get() = running.get()

    @SuppressLint("MissingPermission") // the activity checks RECORD_AUDIO before calling start()
    fun start(sampleRate: Int = Protocol.REQUEST_RATE): Boolean {
        if (running.get()) return true

        val frameBytes = Protocol.frameSamples * 2
        val minCapture = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minCapture <= 0) {
            Log.e(TAG, "device cannot capture $sampleRate Hz mono PCM (minBuffer=$minCapture)")
            return false
        }

        val recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minCapture, frameBytes * 4),
        )
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord refused to initialise at $sampleRate Hz")
            recorder.release()
            return false
        }

        val minPlay = AudioTrack.getMinBufferSize(
            Protocol.SESSION_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val player = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(Protocol.SESSION_RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setBufferSizeInBytes(maxOf(minPlay, 320 * PLAY_BUFFERS * 2))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        player.play()

        record = recorder
        track = player
        running.set(true)

        captureThread = Thread({ captureLoop(frameBytes) }, "voice-capture").also { it.start() }
        playbackThread = Thread({ playbackLoop() }, "voice-playback").also { it.start() }
        return true
    }

    private fun captureLoop(frameBytes: Int) {
        val recorder = record ?: return
        val buffer = ByteArray(frameBytes)
        recorder.startRecording()
        try {
            while (running.get()) {
                val read = recorder.read(buffer, 0, frameBytes)
                if (read <= 0) continue
                val frame = if (read == frameBytes) buffer.copyOf() else buffer.copyOf(read)
                if (read > 0) onLevel(rms(frame))
                onFrame(frame)
            }
        } catch (error: Exception) {
            Log.e(TAG, "capture stopped: ${error.message}")
        } finally {
            try {
                recorder.stop()
            } catch (_: IllegalStateException) {
            }
        }
    }

    private fun playbackLoop() {
        val player = track ?: return
        try {
            while (running.get()) {
                val chunk = playbackQueue.poll() ?: run {
                    Thread.sleep(5)
                    continue
                }
                player.write(chunk, 0, chunk.size)
            }
        } catch (error: Exception) {
            Log.e(TAG, "playback stopped: ${error.message}")
        }
    }

    /** Queue audio from the agent. Drops the oldest frame if the queue is full. */
    fun play(pcm: ByteArray) {
        if (!running.get()) return
        if (!playbackQueue.offer(pcm)) {
            playbackQueue.poll()
            playbackQueue.offer(pcm)
        }
    }

    /** Throw away anything queued: used when the caller barges in or hangs up. */
    fun flush() {
        playbackQueue.clear()
    }

    fun stop() {
        running.set(false)
        try {
            record?.stop()
        } catch (_: IllegalStateException) {
        }
        record?.release()
        record = null
        captureThread?.join(500)
        captureThread = null
        try {
            track?.stop()
        } catch (_: IllegalStateException) {
        }
        track?.release()
        track = null
        playbackThread?.join(500)
        playbackThread = null
        playbackQueue.clear()
    }

    /** Handset routing: earpiece by default, speakerphone when asked. */
    fun routeToSpeaker(speaker: Boolean, audioManager: AudioManager) {
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = speaker
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
    }

    private fun rms(frame: ByteArray): Float {
        if (frame.size < 2) return 0f
        var sum = 0.0
        var index = 0
        while (index + 1 < frame.size) {
            val sample = ((frame[index + 1].toInt() shl 8) or (frame[index].toInt() and 0xFF)).toShort()
            sum += (sample.toDouble() * sample.toDouble())
            index += 2
        }
        return Math.sqrt(sum / (frame.size / 2)).toFloat()
    }
}
