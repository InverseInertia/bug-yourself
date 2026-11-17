package com.bugyourself

import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile

/**
 * Splits one continuous PCM stream into WAV files of exactly [segmentFrames] frames.
 *
 * Splits happen at frame boundaries of the *stream*, never at read boundaries, so
 * concatenating the data chunks of segments 0..N reproduces the original stream
 * sample-for-sample. Pure JVM code: no Android imports.
 *
 * Files are written into [workDir] and moved into [outbox] once complete.
 */
class SegmentWriter(
    private val workDir: File,
    private val outbox: File,
    private val session: String,
    private val sampleRate: Int,
    private val channels: Int,
    segmentFrames: Long,
    private val onSegmentDone: (File) -> Unit = {},
) {
    private val bytesPerFrame = channels * BYTES_PER_SAMPLE
    private val segmentBytes = segmentFrames * bytesPerFrame

    private var seq = 0
    private var current: File? = null
    private var out: BufferedOutputStream? = null
    private var written = 0L

    init {
        workDir.mkdirs()
        outbox.mkdirs()
    }

    fun write(buf: ByteArray, len: Int = buf.size) {
        var off = 0
        while (off < len) {
            if (out == null) open()
            val n = minOf(segmentBytes - written, (len - off).toLong()).toInt()
            out!!.write(buf, off, n)
            written += n
            off += n
            if (written == segmentBytes) closeSegment()
        }
    }

    /** Flush and finish the partially filled final segment. */
    fun finish() {
        if (out != null) closeSegment()
    }

    private fun open() {
        val f = File(workDir, "%s_%05d.wav".format(session, seq++))
        val s = BufferedOutputStream(FileOutputStream(f), 1 shl 16)
        s.write(wavHeader(sampleRate, channels, 0))
        current = f
        out = s
        written = 0
    }

    private fun closeSegment() {
        val f = current!!
        out!!.close()
        out = null
        current = null
        // Drop a trailing partial frame (only possible on the last segment).
        val dataBytes = written - written % bytesPerFrame
        RandomAccessFile(f, "rw").use { raf ->
            raf.setLength(HEADER_BYTES + dataBytes)
            patchSizes(raf, dataBytes)
        }
        val dest = File(outbox, f.name)
        if (!f.renameTo(dest)) error("could not move ${f.name} to outbox")
        onSegmentDone(dest)
    }

    companion object {
        const val HEADER_BYTES = 44
        const val BYTES_PER_SAMPLE = 2

        fun wavHeader(sampleRate: Int, channels: Int, dataBytes: Long): ByteArray {
            val b = java.nio.ByteBuffer.allocate(HEADER_BYTES).order(java.nio.ByteOrder.LITTLE_ENDIAN)
            b.put("RIFF".toByteArray()).putInt((36 + dataBytes).toInt()).put("WAVE".toByteArray())
            b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(channels.toShort())
            b.putInt(sampleRate).putInt(sampleRate * channels * BYTES_PER_SAMPLE)
            b.putShort((channels * BYTES_PER_SAMPLE).toShort()).putShort((BYTES_PER_SAMPLE * 8).toShort())
            b.put("data".toByteArray()).putInt(dataBytes.toInt())
            return b.array()
        }

        private fun patchSizes(raf: RandomAccessFile, dataBytes: Long) {
            fun putLe(pos: Long, v: Long) {
                raf.seek(pos)
                raf.write(byteArrayOf(v.toByte(), (v shr 8).toByte(), (v shr 16).toByte(), (v shr 24).toByte()))
            }
            putLe(4, 36 + dataBytes)
            putLe(40, dataBytes)
        }

        /**
         * A crash or force-stop leaves an in-progress segment with zeroed sizes in its
         * header. Fix the header from the file length and hand it to the outbox.
         */
        fun recoverOrphans(workDir: File, outbox: File, channels: Int) {
            outbox.mkdirs()
            val bytesPerFrame = channels * BYTES_PER_SAMPLE
            workDir.listFiles { f -> f.name.endsWith(".wav") }?.forEach { f ->
                val raw = f.length() - HEADER_BYTES
                if (raw <= 0) { f.delete(); return@forEach }
                val dataBytes = raw - raw % bytesPerFrame
                RandomAccessFile(f, "rw").use { raf ->
                    raf.setLength(HEADER_BYTES + dataBytes)
                    patchSizes(raf, dataBytes)
                }
                f.renameTo(File(outbox, f.name))
            }
        }
    }
}
