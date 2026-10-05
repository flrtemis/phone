package dev.phone.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log

/**
 * Keeps a call alive with the screen off.
 *
 * Android kills background audio after a few minutes unless something declares
 * why it is running. A foreground service with a visible notification is that
 * declaration, and it doubles as the honest answer to "is my microphone live?" -
 * if the notification is there, it is.
 *
 * The service holds no audio itself: it exists to keep the process alive while
 * [VoiceSession] does its work. That keeps the interesting logic in one place and
 * lets the UI be a plain Compose screen.
 */
class CallService : Service() {

    companion object {
        private const val TAG = "phone-voice/service"
        private const val CHANNEL = "voice-call"
        private const val NOTIFICATION_ID = 1

        const val ACTION_START = "dev.phone.voice.START"
        const val ACTION_STOP = "dev.phone.voice.STOP"

        fun start(context: Context) {
            val intent = Intent(context, CallService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(Intent(context, CallService::class.java).setAction(ACTION_STOP))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                // Stop the service *and* the notification together; leaving a
                // "microphone live" notification behind would be a lie.
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
            else -> {
                createChannel()
                val notification = buildNotification()
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
                } else {
                    startForeground(NOTIFICATION_ID, notification)
                }
            }
        }
        return START_NOT_STICKY // never restart a call the user did not start
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL) != null) return
        val channel = NotificationChannel(
            CHANNEL,
            getString(R.string.notification_channel),
            NotificationManager.IMPORTANCE_LOW, // no sound: it would be picked up by the microphone
        ).apply {
            description = getString(R.string.notification_text)
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, CallService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentIntent(open)
            .addAction(
                Notification.Action.Builder(null, getString(R.string.hangup), stop).build(),
            )
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        Log.d(TAG, "service stopped")
        super.onDestroy()
    }
}
