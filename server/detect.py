"""Find sample-worthy moments in a stretch of mono audio. numpy + scipy only.

analyze(x, sr, own_from, own_to) returns Events of four kinds:

  percussive  isolated hits: strong onset, fast decay, quiet before and after
  tonal       sustained pitched sound with a stable pitch
  loop        a stretch with a steady pulse (60-180 BPM)
  texture     steady, non-tonal, non-rhythmic sound clearly above the room floor

Positions are sample indices into x. Only events starting in [own_from, own_to)
are returned, which lets the caller analyze overlapping buffers without
counting anything twice. Every event carries the features behind its score,
so ranking can be re-tuned later without re-running detection.
"""
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage, signal

VERSION = 1

N_FFT = 2048
HOP = 512
N_BANDS = 96
# Below this, a room is "quiet" no matter what the chunk's own floor says, so a
# chunk that is all rain still has its rain measured against silence.
QUIET_REFERENCE_DB = -60.0

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


@dataclass
class Event:
    kind: str
    start: int
    end: int
    score: float
    label: str
    features: dict = field(default_factory=dict)


@dataclass
class Frames:
    """Frame-level features shared by all detectors."""
    sr: int
    rms_db: np.ndarray       # level per frame, dBFS
    onset: np.ndarray        # spectral-flux onset strength, normalized
    flatness: np.ndarray     # 0 = pure tone, 1 = white noise
    harmonic: np.ndarray     # share of energy that is harmonic (HPSS), 0..1
    floor_db: float          # this buffer's own background level
    ref_db: float            # what "quiet" means for loudness scoring

    @property
    def fps(self):
        return self.sr / HOP

    def frames(self, seconds):
        return max(1, int(round(seconds * self.fps)))


def clip01(v):
    return float(min(1.0, max(0.0, v)))


def note_name(f0):
    midi = int(round(69 + 12 * np.log2(f0 / 440.0)))
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# --- features -------------------------------------------------------------------

def band_matrix(sr, n_bins, n_bands=N_BANDS, fmin=40.0):
    """Log-spaced triangular bands, so HPSS runs on 96 bands instead of 1025 bins."""
    freqs = np.linspace(0, sr / 2, n_bins)
    edges = np.geomspace(fmin, sr / 2, n_bands + 2)
    m = np.zeros((n_bands, n_bins), dtype=np.float32)
    for b in range(n_bands):
        lo, mid, hi = edges[b], edges[b + 1], edges[b + 2]
        up = (freqs - lo) / (mid - lo)
        down = (hi - freqs) / (hi - mid)
        m[b] = np.maximum(0, np.minimum(up, down))
    m /= np.maximum(m.sum(axis=1, keepdims=True), 1e-9)
    return m


def compute_frames(x, sr):
    x = np.asarray(x, dtype=np.float32)
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad))
    n = 1 + (len(xp) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    frames = xp[idx]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_db = 20 * np.log10(rms + 1e-10)
    win = np.hanning(N_FFT).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32)  # frames x bins
    power = mag ** 2

    # Spectral flatness over 100 Hz - 10 kHz.
    bin_hz = sr / N_FFT
    lo, hi = int(100 / bin_hz), int(min(10000, sr / 2 - bin_hz) / bin_hz)
    p = power[:, lo:hi] + 1e-12
    flatness = np.exp(np.mean(np.log(p), axis=1)) / np.mean(p, axis=1)

    bands = mag @ band_matrix(sr, mag.shape[1]).T  # frames x bands
    # Harmonic/percussive separation by median filtering (Fitzgerald 2010).
    h = ndimage.median_filter(bands, size=(17, 1))  # smooth along time
    pc = ndimage.median_filter(bands, size=(1, 9))  # smooth along frequency
    mask_h = h ** 2 / (h ** 2 + pc ** 2 + 1e-12)
    eh = np.sum((mask_h * bands) ** 2, axis=1)
    ep = np.sum(((1 - mask_h) * bands) ** 2, axis=1)
    harmonic = eh / (eh + ep + 1e-12)

    # Onset strength: positive log-spectral flux, normalized so the median frame is ~1.
    logb = np.log1p(100 * bands)
    flux = np.maximum(0, np.diff(logb, axis=0, prepend=logb[:1])).sum(axis=1)
    onset = flux / (np.median(flux) + 1e-9)

    floor_db = float(np.percentile(rms_db, 10))
    return Frames(sr, rms_db, onset, flatness, harmonic, floor_db, min(floor_db, QUIET_REFERENCE_DB))


def onset_peaks(fr, min_height=4.0):
    peaks, props = signal.find_peaks(fr.onset, height=min_height, distance=fr.frames(0.05))
    return peaks, props["peak_heights"]


# --- detectors ------------------------------------------------------------------

def detect_percussive(fr, peaks, heights):
    out = []
    pre_n, look = fr.frames(0.15), fr.frames(1.5)
    for t, h in zip(peaks, heights):
        peak_db = float(fr.rms_db[t:t + 4].max())
        snr = peak_db - fr.floor_db
        if snr < 12:
            continue
        pre_db = float(np.mean(fr.rms_db[max(0, t - pre_n):max(1, t - 1)]))
        pre_quiet = clip01((fr.floor_db + 15 - pre_db) / 12)
        tail = fr.rms_db[t:t + look]
        below = np.nonzero(tail < max(peak_db - 25, fr.floor_db + 3))[0]
        if len(below) == 0:
            continue  # never decays within 1.5 s: not a one-shot
        decay_s = below[0] / fr.fps
        decay = clip01((1.5 - decay_s) / 0.9)
        # Other onsets close by (even quieter ones, like hi-hats in a groove) mean this
        # is part of a pattern, not a one-shot.
        end_f = t + below[0]
        near = fr.frames(0.4)
        rivals = sum(1 for tt, hh in zip(peaks, heights) if tt != t and t - near < tt < end_f + near and hh > 0.25 * h)
        isolated = clip01(1 - rivals / 2)
        perc = 1.0 - float(np.mean(fr.harmonic[t:t + 5]))
        score = 0.2 * clip01(snr / 35) + 0.2 * pre_quiet + 0.15 * decay + 0.3 * isolated + 0.15 * perc
        # Start a few ms before the onset frame so the attack isn't clipped.
        start = max(0, t * HOP - N_FFT // 2 - int(0.005 * fr.sr))
        end = (end_f + fr.frames(0.05)) * HOP
        out.append(Event("percussive", start, end, score, "hit", {
            "snr_db": round(snr, 1), "pre_quiet": round(pre_quiet, 2), "decay_s": round(float(decay_s), 3),
            "isolated": isolated, "rivals": rivals, "percussive": round(perc, 2), "onset": round(float(h), 1)}))
    return out


def runs(mask, min_len):
    """(start, end) frame index pairs of True runs at least min_len long."""
    m = np.concatenate([[False], mask, [False]])
    d = np.diff(m.astype(np.int8))
    starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    return [(s, e) for s, e in zip(starts, ends) if e - s >= min_len]


def yin_f0(x, sr, hop_s=0.02, fmin=50.0, fmax=1500.0, threshold=0.15):
    """Per-frame f0 (Hz, nan when unvoiced) using the YIN difference function."""
    w = int(0.05 * sr)
    tau_min, tau_max = int(sr / fmax), min(int(sr / fmin), w - 1)
    hop = int(hop_s * sr)
    f0 = []
    for i in range(0, len(x) - 2 * w, hop):
        frame = x[i:i + 2 * w].astype(np.float64)
        a = frame[:w]
        # d(tau) = sum (a_j - x_{j+tau})^2, via energies and a cross-correlation.
        corr = signal.fftconvolve(frame, a[::-1], mode="valid")[: tau_max + 1]
        cs = np.cumsum(np.concatenate([[0.0], frame ** 2]))
        energy_shift = cs[np.arange(tau_max + 1) + w] - cs[np.arange(tau_max + 1)]
        d = cs[w] + energy_shift - 2 * corr
        d[0] = 0
        cmnd = d[1:] * np.arange(1, tau_max + 1) / np.maximum(np.cumsum(d[1:]), 1e-12)
        cand = np.nonzero(cmnd[tau_min - 1:] < threshold)[0]
        if len(cand) == 0:
            f0.append(np.nan)
            continue
        k = cand[0] + tau_min - 1  # index into cmnd (cmnd[k] is tau = k + 1)
        while k + 1 < len(cmnd) and cmnd[k + 1] < cmnd[k]:  # walk down to the local minimum
            k += 1
        # Parabolic interpolation between neighbouring lags for sub-sample precision.
        shift = 0.0
        if 0 < k < len(cmnd) - 1:
            y0, y1, y2 = cmnd[k - 1], cmnd[k], cmnd[k + 1]
            denom = y0 - 2 * y1 + y2
            shift = 0.5 * (y0 - y2) / denom if denom > 0 else 0.0
        f0.append(sr / (k + 1 + shift))
    return np.array(f0)


def detect_tonal(fr, x):
    out = []
    mask = (fr.harmonic > 0.6) & (fr.flatness < 0.1) & (fr.rms_db > fr.floor_db + 10)
    mask = ndimage.binary_closing(mask, structure=np.ones(5))
    max_len = fr.frames(8.0)
    for s, e in runs(mask, fr.frames(0.5)):
        for s2 in range(s, e, max_len):
            e2 = min(e, s2 + max_len)
            if e2 - s2 < fr.frames(0.5):
                continue
            a, b = s2 * HOP, e2 * HOP
            seg = signal.resample_poly(x[a:b], 1, 3) if fr.sr >= 44100 else x[a:b]
            sr_p = fr.sr // 3 if fr.sr >= 44100 else fr.sr
            f0 = yin_f0(seg, sr_p)
            voiced = f0[~np.isnan(f0)]
            voiced_frac = len(voiced) / max(1, len(f0))
            if len(voiced) < 5:
                continue
            cents = 1200 * np.log2(voiced / np.median(voiced))
            spread = float(np.std(cents))
            stability = clip01(1 - spread / 100)
            dur = (e2 - s2) / fr.fps
            snr = float(np.mean(fr.rms_db[s2:e2])) - fr.ref_db
            score = 0.3 * voiced_frac + 0.3 * stability + 0.2 * clip01(snr / 35) + 0.2 * clip01(dur / 2)
            f = float(np.median(voiced))
            out.append(Event("tonal", a, b, score, note_name(f), {
                "f0_hz": round(f, 1), "voiced": round(voiced_frac, 2), "spread_cents": round(spread, 1),
                "duration_s": round(float(dur), 2), "snr_db": round(snr, 1)}))
    return out


def windows(n_frames, length, hop):
    return range(0, max(1, n_frames - length + 1), hop)


def detect_loops(fr, peaks):
    out = []
    win, hop = fr.frames(8.0), fr.frames(2.0)
    lag_lo, lag_hi = fr.frames(60 / 180), fr.frames(60 / 60)
    for s in windows(len(fr.onset), win, hop):
        e = s + win
        level = float(np.mean(fr.rms_db[s:e]))
        n_onsets = int(np.sum((peaks >= s) & (peaks < e)))
        if level < fr.ref_db + 8 or n_onsets < 6:
            continue
        env = fr.onset[s:e] - np.mean(fr.onset[s:e])
        ac = np.correlate(env, env, mode="full")[len(env) - 1:]
        if ac[0] <= 0:
            continue
        ac /= ac[0]
        lag = lag_lo + int(np.argmax(ac[lag_lo:lag_hi + 1]))
        strength = float(ac[lag])
        double = float(ac[2 * lag]) if 2 * lag < len(ac) else 0.0
        if strength < 0.3:
            continue
        bpm = 60 * fr.fps / lag
        score = 0.5 * clip01(strength) + 0.2 * clip01(double) + 0.3 * clip01((level - fr.ref_db) / 30)
        # Two bars of 4/4, starting on the first loud onset: kicks rather than the
        # off-beat hi-hats, which have bigger spectral-flux spikes but less level.
        inside = peaks[(peaks >= s) & (peaks < e)]
        loud = np.array([fr.rms_db[p:p + 4].max() for p in inside])
        first = inside[loud >= loud.max() - 3][0]
        start = max(0, first * HOP - N_FFT // 2)
        end = start + int(8 * 60 / bpm * fr.sr)
        out.append(Event("loop", start, end, score, f"{round(bpm)}bpm", {
            "bpm": round(bpm, 1), "periodicity": round(strength, 2), "double": round(double, 2),
            "onsets": n_onsets, "level_db": round(level, 1)}))
    return out


def detect_textures(fr, peaks):
    out = []
    win, hop = fr.frames(6.0), fr.frames(3.0)
    for s in windows(len(fr.rms_db), win, hop):
        e = s + win
        lv = fr.rms_db[s:e]
        level = float(np.mean(lv))
        snr = level - fr.ref_db
        if snr < 12:
            continue
        steady = clip01(1 - float(np.std(lv)) / 4)
        density = np.sum((peaks >= s) & (peaks < e)) / (win / fr.fps)
        calm = clip01(1 - density / 1.5)
        flat = float(np.median(fr.flatness[s:e]))
        if flat < 0.05 or steady < 0.25:
            continue  # tonal (handled elsewhere) or too erratic
        score = 0.4 * steady + 0.3 * clip01(snr / 30) + 0.3 * calm
        out.append(Event("texture", s * HOP, e * HOP, score, "texture", {
            "level_db": round(level, 1), "snr_db": round(snr, 1), "level_std": round(float(np.std(lv)), 2),
            "onsets_per_s": round(float(density), 2), "flatness": round(flat, 3)}))
    return out


def suppress_overlaps(events):
    """Within each kind, keep the best of any overlapping events."""
    kept = []
    for ev in sorted(events, key=lambda e: -e.score):
        if all(k.kind != ev.kind or ev.end <= k.start or ev.start >= k.end for k in kept):
            kept.append(ev)
    return sorted(kept, key=lambda e: e.start)


def analyze(x, sr, own_from=0, own_to=None):
    own_to = len(x) if own_to is None else own_to
    if len(x) < sr:  # under a second: nothing meaningful to find
        return []
    fr = compute_frames(x, sr)
    peaks, heights = onset_peaks(fr)
    events = (detect_percussive(fr, peaks, heights) + detect_tonal(fr, x)
              + detect_loops(fr, peaks) + detect_textures(fr, peaks))
    for ev in events:
        ev.end = min(ev.end, len(x))
        ev.score = round(ev.score, 4)
    return [e for e in suppress_overlaps(events) if own_from <= e.start < own_to]
