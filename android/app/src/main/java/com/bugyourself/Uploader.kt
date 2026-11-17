package com.bugyourself

import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** PUTs one file to `<baseUrl>/upload/<name>` and returns true once the server has verified it. */
object Uploader {
    fun upload(file: File, baseUrl: String, token: String): Boolean {
        val sha = sha256Hex(file)
        val conn = URL(baseUrl.trimEnd('/') + "/upload/" + file.name).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "PUT"
            conn.doOutput = true
            conn.connectTimeout = 10_000
            conn.readTimeout = 60_000
            conn.setFixedLengthStreamingMode(file.length())
            conn.setRequestProperty("Content-Type", if (file.name.endsWith(".flac")) "audio/flac" else "audio/wav")
            conn.setRequestProperty("X-Sha256", sha)
            if (token.isNotBlank()) conn.setRequestProperty("Authorization", "Bearer $token")
            conn.outputStream.use { os -> file.inputStream().use { it.copyTo(os, 1 shl 16) } }
            return conn.responseCode in 200..299
        } finally {
            conn.disconnect()
        }
    }

    private fun sha256Hex(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { ins ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}
