package com.bugyourself

import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * Splits one continuous PCM stream into FLAC files of exactly [segmentFrames] frames.
 *
 * Splits happen at frame boundaries of the *stream*, never at read boundaries, so
 * decoding segments 0..N and concatenating them reproduces the original stream
 * sample-for-sample. Pure JVM code: no Android imports.
 *
 * Each file is named after the UTC time of its first sample (20261117T201500Z.flac),
 * computed from [startMillis] plus the samples before it rather than read from the
 * clock, so a clock adjustment mid-recording can't skew the names.
 *
 * Files are written into [workDir] and moved into [outbox] once complete.
 */
class SegmentWriter(
    private val workDir: File,
    private val outbox: File,
    private val startMillis: Long,
    private val sampleRate: Int,
    private val channels: Int,
    segmentFrames: Long,
    private val onSegmentDone: (File) -> Unit = {},
) {
    private val segmentFrames = segmentFrames
    private val segmentBytes = segmentFrames * channels * 2

    private var seq = 0
    private var current: File? = null
    private var stream: BufferedOutputStream? = null
    private var encoder: FlacEncoder? = null
    private var written = 0L

    init {
        workDir.mkdirs()
        outbox.mkdirs()
    }

    fun write(buf: ByteArray, len: Int = buf.size) {
        var off = 0
        while (off < len) {
            if (encoder == null) open()
            val n = minOf(segmentBytes - written, (len - off).toLong()).toInt()
            encoder!!.write(buf, off, n)
            written += n
            off += n
            if (written == segmentBytes) closeSegment()
        }
    }

    /** Finish the partially filled final segment. */
    fun finish() {
        if (encoder != null) closeSegment()
    }

    private fun open() {
        val f = File(workDir, chunkName(startMillis + seq++ * segmentFrames * 1000 / sampleRate))
        val s = BufferedOutputStream(FileOutputStream(f), 1 shl 16)
        current = f
        stream = s
        encoder = FlacEncoder(s, sampleRate, channels)
        written = 0
    }

    private fun closeSegment() {
        val f = current!!
        val enc = encoder!!
        val md5 = enc.finish()
        stream!!.close()
        encoder = null
        stream = null
        current = null
        RandomAccessFile(f, "rw").use { raf ->
            raf.seek(FlacEncoder.STREAMINFO_OFFSET)
            raf.write(FlacEncoder.streamInfoBytes(sampleRate, channels, enc.totalSamples, md5))
        }
        val dest = File(outbox, f.name)
        if (!f.renameTo(dest)) error("could not move ${f.name} to outbox")
        onSegmentDone(dest)
    }

    companion object {
        private val STAMP = DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'").withZone(ZoneOffset.UTC)

        /** UTC start time to the second: chunks are at least 6 s long, so names never collide. */
        fun chunkName(millis: Long): String = STAMP.format(Instant.ofEpochMilli(millis)) + ".flac"

        /**
         * A crash or force-stop leaves an in-progress segment whose last frame may be
         * cut off. Keep the intact frames and hand the file to the outbox.
         */
        fun recoverOrphans(workDir: File, outbox: File, sampleRate: Int, channels: Int) {
            outbox.mkdirs()
            workDir.listFiles()?.forEach { f ->
                if (f.name.endsWith(".flac") && FlacEncoder.recover(f, sampleRate, channels)) {
                    f.renameTo(File(outbox, f.name))
                } else {
                    f.delete()
                }
            }
        }
    }
}
