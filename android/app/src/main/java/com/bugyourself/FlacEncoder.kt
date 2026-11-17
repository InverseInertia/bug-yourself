package com.bugyourself

import java.io.File
import java.io.OutputStream
import java.io.RandomAccessFile
import java.security.MessageDigest

/**
 * Minimal lossless FLAC encoder for 16-bit PCM: fixed predictors (order 0-4),
 * partitioned Rice residuals, constant/verbatim fallback. Pure JVM code.
 *
 * Feed little-endian interleaved PCM bytes to [write], then call [finish] and
 * patch the header with [streamInfoBytes] at [STREAMINFO_OFFSET].
 */
class FlacEncoder(
    private val out: OutputStream,
    private val sampleRate: Int,
    private val channels: Int,
) {
    private val block = Array(channels) { IntArray(BLOCK_SIZE) }
    private var filled = 0          // samples per channel in the current block
    private var carry = ByteArray(0) // bytes of an incomplete frame from the last write
    private var frameNumber = 0L
    private val md5 = MessageDigest.getInstance("MD5")
    private val bw = BitWriter()

    var totalSamples = 0L
        private set

    init {
        out.write(fileHeader(sampleRate, channels, 0, ByteArray(16)))
    }

    fun write(buf: ByteArray, off: Int = 0, len: Int = buf.size) {
        val bytesPerFrame = channels * 2
        val data = if (carry.isEmpty()) buf.copyOfRange(off, off + len) else carry + buf.copyOfRange(off, off + len)
        val whole = data.size - data.size % bytesPerFrame
        carry = data.copyOfRange(whole, data.size)
        md5.update(data, 0, whole) // FLAC's MD5 is over interleaved LE samples: exactly these bytes
        var i = 0
        while (i < whole) {
            for (c in 0 until channels) {
                block[c][filled] = ((data[i + 1].toInt() shl 8) or (data[i].toInt() and 0xFF)).toShort().toInt()
                i += 2
            }
            if (++filled == BLOCK_SIZE) flushBlock()
        }
    }

    /** Encodes the final partial block. Returns the MD5 for the STREAMINFO patch. */
    fun finish(): ByteArray {
        if (filled > 0) flushBlock()
        out.flush()
        return md5.digest()
    }

    private fun flushBlock() {
        val n = filled
        bw.reset()
        // Frame header
        bw.write(0xFFF8, 16) // sync + fixed-blocksize strategy
        val bsCode = if (n == BLOCK_SIZE) 12 else 7
        bw.write(bsCode, 4)
        bw.write(sampleRateCode(sampleRate), 4)
        bw.write(channels - 1, 4) // independent channels
        bw.write(4, 3)            // 16 bits per sample
        bw.write(0, 1)
        writeUtf8(frameNumber++)
        if (bsCode == 7) bw.write(n - 1, 16)
        bw.write(crc8(bw.bytes, 0, bw.size), 8)
        // Subframes
        for (c in 0 until channels) writeSubframe(block[c], n)
        bw.alignToByte()
        bw.write(crc16(bw.bytes, 0, bw.size), 16)
        out.write(bw.bytes, 0, bw.size)
        totalSamples += n
        filled = 0
    }

    private val residual = IntArray(BLOCK_SIZE)
    private val zig = LongArray(BLOCK_SIZE)

    private fun writeSubframe(x: IntArray, n: Int) {
        if ((1 until n).all { x[it] == x[0] }) {
            bw.write(0, 8) // CONSTANT
            bw.write(x[0], 16)
            return
        }
        // Pick the fixed predictor order with the smallest total |residual|.
        var bestOrder = 0
        var bestSum = Long.MAX_VALUE
        for (order in 0..minOf(4, n - 1)) {
            var s = 0L
            for (i in order until n) s += kotlin.math.abs(predictResidual(x, i, order).toLong())
            if (s < bestSum) { bestSum = s; bestOrder = order }
        }
        val order = bestOrder
        for (i in order until n) {
            val r = predictResidual(x, i, order)
            residual[i] = r
            zig[i] = ((r shl 1) xor (r shr 31)).toLong() and 0xFFFFFFFFL
        }
        val plan = planRice(n, order)
        val fixedBits = 8 + order * 16 + 6 + plan.bits
        if (fixedBits >= 8 + n * 16) {
            bw.write(1 shl 1, 8) // VERBATIM
            for (i in 0 until n) bw.write(x[i], 16)
            return
        }
        bw.write((8 + order) shl 1, 8) // FIXED, order
        for (i in 0 until order) bw.write(x[i], 16)
        bw.write(0, 2) // Rice, 4-bit parameters
        bw.write(plan.partitionOrder, 4)
        val parts = 1 shl plan.partitionOrder
        val per = n shr plan.partitionOrder
        for (p in 0 until parts) {
            val k = plan.params[p]
            bw.write(k, 4)
            val start = if (p == 0) order else p * per
            for (i in start until (p + 1) * per) {
                val u = zig[i]
                bw.unary((u ushr k).toInt())
                if (k > 0) bw.write((u and ((1L shl k) - 1)).toInt(), k)
            }
        }
    }

    private class RicePlan(val partitionOrder: Int, val params: IntArray, val bits: Long)

    private fun planRice(n: Int, order: Int): RicePlan {
        var best: RicePlan? = null
        var p = 0
        while (p <= 8 && n % (1 shl p) == 0 && (n shr p) > order) {
            val parts = 1 shl p
            val per = n shr p
            val params = IntArray(parts)
            var bits = 0L
            for (part in 0 until parts) {
                val start = if (part == 0) order else part * per
                val end = (part + 1) * per
                var sum = 0L
                for (i in start until end) sum += zig[i]
                val m = (end - start).toLong()
                var bestK = 0
                var bestCost = Long.MAX_VALUE
                for (k in 0..14) {
                    val cost = m * (k + 1) + (sum ushr k)
                    if (cost < bestCost) { bestCost = cost; bestK = k }
                }
                params[part] = bestK
                bits += 4 + bestCost
            }
            if (best == null || bits < best.bits) best = RicePlan(p, params, bits)
            p++
        }
        return best!!
    }

    private fun writeUtf8(v: Long) {
        if (v < 0x80) { bw.write(v.toInt(), 8); return }
        val bytes = when {
            v < 0x800 -> 2; v < 0x10000 -> 3; v < 0x200000 -> 4
            v < 0x4000000 -> 5; v < 0x80000000L -> 6; else -> 7
        }
        val lead = if (bytes == 7) 0xFE else (0xFF00 shr bytes) and 0xFF
        bw.write(lead or (v ushr (6 * (bytes - 1))).toInt(), 8)
        for (i in bytes - 2 downTo 0) bw.write(0x80 or ((v ushr (6 * i)).toInt() and 0x3F), 8)
    }

    companion object {
        const val BLOCK_SIZE = 4096
        const val STREAMINFO_OFFSET = 8L
        const val HEADER_BYTES = 42

        private fun predictResidual(x: IntArray, i: Int, order: Int): Int = when (order) {
            0 -> x[i]
            1 -> x[i] - x[i - 1]
            2 -> x[i] - 2 * x[i - 1] + x[i - 2]
            3 -> x[i] - 3 * x[i - 1] + 3 * x[i - 2] - x[i - 3]
            else -> x[i] - 4 * x[i - 1] + 6 * x[i - 2] - 4 * x[i - 3] + x[i - 4]
        }

        private fun sampleRateCode(rate: Int) = when (rate) {
            32000 -> 8; 44100 -> 9; 48000 -> 10; 96000 -> 11; else -> 0 // 0 = see STREAMINFO
        }

        fun fileHeader(sampleRate: Int, channels: Int, totalSamples: Long, md5: ByteArray): ByteArray =
            "fLaC".toByteArray() + byteArrayOf(0x80.toByte(), 0, 0, 34) +
                streamInfoBytes(sampleRate, channels, totalSamples, md5)

        fun streamInfoBytes(sampleRate: Int, channels: Int, totalSamples: Long, md5: ByteArray): ByteArray {
            val w = BitWriter()
            w.write(BLOCK_SIZE, 16); w.write(BLOCK_SIZE, 16) // min/max block size
            w.write(0, 24); w.write(0, 24)                    // min/max frame size: unknown
            w.write(sampleRate, 20)
            w.write(channels - 1, 3)
            w.write(15, 5)                                    // 16 bits per sample
            w.write((totalSamples ushr 32).toInt(), 4)
            w.write(totalSamples.toInt(), 32)
            return w.bytes.copyOf(w.size) + md5
        }

        /**
         * Repairs a file cut off mid-write: keeps every frame whose CRC-16 checks out,
         * truncates the rest, and marks length/MD5 as unknown. Returns false if nothing usable.
         */
        fun recover(f: File, sampleRate: Int, channels: Int): Boolean {
            val b = f.readBytes()
            if (b.size <= HEADER_BYTES || String(b, 0, 4) != "fLaC") return false
            var good = HEADER_BYTES
            var p = HEADER_BYTES
            while (p + 2 <= b.size && b[p] == 0xFF.toByte() && b[p + 1] == 0xF8.toByte()) {
                // A frame ends where the running CRC-16 (including the stored CRC) hits zero,
                // right before the next sync code or EOF.
                var crc = 0
                var q = p
                var end = -1
                while (q < b.size) {
                    crc = ((crc shl 8) xor CRC16[((crc ushr 8) xor (b[q].toInt() and 0xFF)) and 0xFF]) and 0xFFFF
                    q++
                    if (crc == 0 && q - p > 6 &&
                        (q == b.size || (q + 1 < b.size && b[q] == 0xFF.toByte() && b[q + 1] == 0xF8.toByte()))
                    ) { end = q; break }
                }
                if (end < 0) break
                good = end
                p = end
            }
            if (good == HEADER_BYTES) return false
            RandomAccessFile(f, "rw").use { raf ->
                raf.setLength(good.toLong())
                raf.seek(STREAMINFO_OFFSET)
                raf.write(streamInfoBytes(sampleRate, channels, 0, ByteArray(16)))
            }
            return true
        }

        private val CRC8 = IntArray(256) { i ->
            var c = i
            repeat(8) { c = if (c and 0x80 != 0) (c shl 1) xor 0x07 else c shl 1 }
            c and 0xFF
        }
        private val CRC16 = IntArray(256) { i ->
            var c = i shl 8
            repeat(8) { c = if (c and 0x8000 != 0) (c shl 1) xor 0x8005 else c shl 1 }
            c and 0xFFFF
        }

        private fun crc8(b: ByteArray, off: Int, len: Int): Int {
            var c = 0
            for (i in off until off + len) c = CRC8[c xor (b[i].toInt() and 0xFF)]
            return c
        }

        private fun crc16(b: ByteArray, off: Int, len: Int): Int {
            var c = 0
            for (i in off until off + len) c = ((c shl 8) xor CRC16[((c ushr 8) xor (b[i].toInt() and 0xFF)) and 0xFF]) and 0xFFFF
            return c
        }
    }

    private class BitWriter {
        var bytes = ByteArray(1 shl 16)
        var size = 0
        private var acc = 0L
        private var nbits = 0

        fun reset() { size = 0; acc = 0; nbits = 0 }

        /** Writes the low [bits] bits of [value], MSB first. bits <= 32. */
        fun write(value: Int, bits: Int) {
            if (bits == 0) return
            acc = (acc shl bits) or (value.toLong() and ((1L shl bits) - 1))
            nbits += bits
            while (nbits >= 8) {
                nbits -= 8
                put((acc ushr nbits).toInt())
            }
            acc = acc and ((1L shl nbits) - 1)
        }

        fun unary(q: Int) {
            var z = q
            while (z >= 31) { write(0, 31); z -= 31 }
            write(1, z + 1)
        }

        fun alignToByte() { if (nbits > 0) write(0, 8 - nbits) }

        private fun put(v: Int) {
            if (size == bytes.size) bytes = bytes.copyOf(bytes.size * 2)
            bytes[size++] = v.toByte()
        }
    }
}
