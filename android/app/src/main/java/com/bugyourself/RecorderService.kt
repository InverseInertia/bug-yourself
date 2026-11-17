package com.bugyourself

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.Process
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.LinkedBlockingQueue

/**
 * Foreground service with three threads:
 *  reader   - pulls PCM from AudioRecord as fast as possible, never touches disk
 *  writer   - drains the queue into SegmentWriter (disk I/O can't stall the mic)
 *  uploader - pushes finished segments from the outbox to the PC, retrying forever
 *
 * One AudioRecord runs for the whole session and is never restarted, so segments
 * are contiguous. The service stays up after Stop until the outbox is empty.
 */
class RecorderService : Service() {

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val SAMPLE_RATE = 48_000
        const val CHANNELS = 1
        private const val TAG = "bug-yourself"
        private const val NOTIF_ID = 1
        private const val CHANNEL_ID = "rec"

        @Volatile var recording = false
        @Volatile var status = "Idle"
        @Volatile var lastUpload = ""
        @Volatile var source = ""

        fun workDir(ctx: Context) = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "recording")
        fun outbox(ctx: Context) = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "outbox")
        fun isAudio(f: File) = f.name.endsWith(".flac") || f.name.endsWith(".wav")
        fun pending(ctx: Context) = outbox(ctx).listFiles()?.count(::isAudio) ?: 0
    }

    private val queue = LinkedBlockingQueue<ByteArray>()
    private val endMarker = ByteArray(0)
    private val uploadSignal = Object()
    private val lifecycle = Object()
    private var reader: Thread? = null
    private var writer: Thread? = null
    private var uploader: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start()
            ACTION_STOP -> stopRecording()
        }
        return START_NOT_STICKY
    }

    private fun start() {
        synchronized(lifecycle) {
            if (recording) return
            goForeground()
            if (wakeLock?.isHeld != true) {
                wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bugyourself:rec").apply { acquire() }
            }

            SegmentWriter.recoverOrphans(workDir(this), outbox(this), SAMPLE_RATE, CHANNELS)

            val prefs = getSharedPreferences("cfg", MODE_PRIVATE)
            val segMinutes = prefs.getString("segMinutes", "5")!!.toDoubleOrNull()?.coerceAtLeast(0.1) ?: 5.0
            val session = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date())
            val segWriter = SegmentWriter(
                workDir(this), outbox(this), session, SAMPLE_RATE, CHANNELS,
                (segMinutes * 60 * SAMPLE_RATE).toLong(),
            ) { synchronized(uploadSignal) { uploadSignal.notifyAll() } }

            val record = try {
                openAudioRecord()
            } catch (e: Exception) {
                status = "Mic error: ${e.message}"
                if (uploader == null) shutdown()
                return
            }
            recording = true
            status = "Recording $session"

            reader = Thread({
                Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
                val chunk = SAMPLE_RATE / 10 * CHANNELS * 2 // 100 ms
                record.startRecording()
                while (recording) {
                    val buf = ByteArray(chunk)
                    val n = record.read(buf, 0, chunk)
                    if (n > 0) queue.put(if (n == chunk) buf else buf.copyOf(n))
                    else if (n < 0) Log.e(TAG, "AudioRecord.read error $n")
                }
                record.stop()
                record.release()
                queue.put(endMarker)
            }, "reader").apply { start() }

            writer = Thread({
                try {
                    while (true) {
                        val b = queue.take()
                        if (b === endMarker) break
                        segWriter.write(b)
                    }
                    segWriter.finish()
                } catch (e: Exception) {
                    Log.e(TAG, "writer failed", e)
                    status = "Write error: ${e.message}"
                }
            }, "writer").apply { start() }

            if (uploader == null) uploader = Thread(::uploadLoop, "uploader").apply { start() }
        }
    }

    private fun openAudioRecord(): AudioRecord {
        // UNPROCESSED skips AGC / noise suppression / echo cancel where the device
        // supports it; VOICE_RECOGNITION is the closest fallback.
        val am = getSystemService(AUDIO_SERVICE) as AudioManager
        val unprocessed = am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) == "true"
        val src = if (unprocessed) MediaRecorder.AudioSource.UNPROCESSED else MediaRecorder.AudioSource.VOICE_RECOGNITION
        source = if (unprocessed) "UNPROCESSED" else "VOICE_RECOGNITION"
        val chMask = if (CHANNELS == 2) AudioFormat.CHANNEL_IN_STEREO else AudioFormat.CHANNEL_IN_MONO
        val min = AudioRecord.getMinBufferSize(SAMPLE_RATE, chMask, AudioFormat.ENCODING_PCM_16BIT)
        // ~2 s of hardware-side buffering as extra slack against scheduling hiccups.
        val bufBytes = maxOf(min * 4, SAMPLE_RATE * CHANNELS * 2 * 2)
        @Suppress("MissingPermission")
        val r = AudioRecord(src, SAMPLE_RATE, chMask, AudioFormat.ENCODING_PCM_16BIT, bufBytes)
        check(r.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord failed to initialize" }
        return r
    }

    private fun stopRecording() {
        if (!recording) {
            synchronized(lifecycle) { if (uploader == null) shutdown() }
            return
        }
        recording = false
        Thread {
            reader?.join()
            writer?.join()
            status = "Stopped"
            synchronized(uploadSignal) { uploadSignal.notifyAll() }
        }.start()
    }

    private fun uploadLoop() {
        var backoff = 5_000L
        while (true) {
            val prefs = getSharedPreferences("cfg", MODE_PRIVATE)
            val url = prefs.getString("url", "")!!
            val token = prefs.getString("token", "")!!
            val files = outbox(this).listFiles(::isAudio)?.sortedBy { it.name }.orEmpty()

            if (files.isEmpty()) {
                synchronized(lifecycle) {
                    if (!recording && writer?.isAlive != true) {
                        uploader = null
                        shutdown()
                        return
                    }
                }
                synchronized(uploadSignal) { uploadSignal.wait(30_000) }
                continue
            }

            var failed = false
            for (f in files) {
                val err = when {
                    url.isBlank() -> "No server URL set"
                    else -> try {
                        when (val code = Uploader.upload(f, url, token)) {
                            in 200..299 -> null
                            507 -> "PC working folder full, holding files on phone"
                            401 -> "Wrong token"
                            else -> "Server rejected ${f.name} ($code)"
                        }
                    } catch (e: Exception) {
                        "Upload failed: ${e.message}"
                    }
                }
                if (err == null) {
                    f.delete()
                    lastUpload = "Uploaded ${f.name}"
                    backoff = 5_000L
                } else {
                    lastUpload = err
                    failed = true
                    break
                }
            }
            if (failed) {
                synchronized(uploadSignal) { uploadSignal.wait(backoff) }
                backoff = minOf(backoff * 2, 60_000L)
            }
        }
    }

    private fun shutdown() {
        if (status == "Stopped") status = "Idle"
        wakeLock?.let { if (it.isHeld) it.release() }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun goForeground() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(CHANNEL_ID, "Recording", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Recording")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    override fun onDestroy() {
        recording = false
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }
}
