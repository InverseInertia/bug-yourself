package com.bugyourself

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** One screen: server URL, token, segment length, Start/Stop, status. No layouts, no AndroidX. */
class MainActivity : Activity() {
    private lateinit var url: EditText
    private lateinit var token: EditText
    private lateinit var seg: EditText
    private lateinit var toggle: Button
    private lateinit var statusView: TextView
    private val ui = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("cfg", MODE_PRIVATE)

        fun field(hint: String, value: String, type: Int = InputType.TYPE_CLASS_TEXT) = EditText(this).apply {
            this.hint = hint
            setText(value)
            inputType = type
        }
        url = field("Server URL, e.g. http://my-pc:8675", prefs.getString("url", "")!!, InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_CLASS_TEXT)
        token = field("Token (optional)", prefs.getString("token", "")!!)
        seg = field("Segment minutes", prefs.getString("segMinutes", "5")!!, InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL)
        toggle = Button(this).apply { setOnClickListener { onToggle() } }
        val battery = Button(this).apply {
            text = "Disable battery optimization"
            setOnClickListener { requestBatteryExemption() }
        }
        statusView = TextView(this).apply { setPadding(0, 32, 0, 0) }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
            listOf(url, token, seg, toggle, battery, statusView).forEach(::addView)
        })
    }

    override fun onResume() {
        super.onResume()
        ui.post(refresh)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(refresh)
    }

    private val refresh = object : Runnable {
        override fun run() {
            val rec = RecorderService.recording
            toggle.text = if (rec) "Stop" else "Start"
            listOf(url, token, seg).forEach { it.isEnabled = !rec }
            statusView.text = buildString {
                appendLine(RecorderService.status)
                if (RecorderService.source.isNotEmpty()) appendLine("Mic source: ${RecorderService.source}")
                appendLine("Waiting to upload: ${RecorderService.pending(this@MainActivity)}")
                append(RecorderService.lastUpload)
            }
            ui.postDelayed(this, 1000)
        }
    }

    private fun onToggle() {
        if (RecorderService.recording) {
            startService(Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_STOP))
            return
        }
        getSharedPreferences("cfg", MODE_PRIVATE).edit()
            .putString("url", url.text.toString().trim())
            .putString("token", token.text.toString().trim())
            .putString("segMinutes", seg.text.toString().trim())
            .apply()

        val needed = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isNotEmpty()) {
            requestPermissions(needed.toTypedArray(), 1)
            return
        }
        startForegroundService(Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_START))
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) onToggle()
    }

    private fun requestBatteryExemption() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        @Suppress("BatteryLife")
        startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
    }
}
