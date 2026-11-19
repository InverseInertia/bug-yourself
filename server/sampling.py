"""Milestone B glue: run detection on chunks, pick the best candidates, cut clips.

Needs numpy, scipy and soundfile (server/requirements.txt). analyzer.py imports
this lazily, so the housekeeping part of the nightly run works without them.
"""
import functools
import json
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

import detect
from names import chunk_epoch, continuous, ogg_info

CONTEXT_S = 15.0  # audio from the previous chunk prepended to each chunk
GUARD_S = 10.0    # events starting this close to a chunk's end belong to the next chunk
FADE_IN_S = 0.003
FADE_OUT_S = 0.015


def run_epoch(run):
    """A run (a stretch of continuous chunks) is named after its first chunk's start."""
    return chunk_epoch(run + ".ogg")


@functools.lru_cache(maxsize=3)
def _decode(path, mtime):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    x.setflags(write=False)
    return x, sr


def read_mono(path, start=0, stop=None):
    """Decode a chunk and slice it. Always decodes from the start: libsndfile's Ogg
    seeking lands ~192 samples late inside a file's last half second, which would
    misplace clips cut near chunk boundaries. A small cache keeps this cheap when
    consecutive cuts and the next chunk's context hit the same file."""
    p = Path(path)
    x, sr = _decode(str(p), p.stat().st_mtime_ns)
    return x[start:stop], sr


# --- detection stage ------------------------------------------------------------

def detect_chunk(db, locate, name, path):
    """Analyze one chunk (with context from its predecessor) and store its candidates."""
    x, sr = read_mono(path)
    frames = len(x)
    epoch = chunk_epoch(name)

    # Chunks are continuous when the previous one (by time) ends where this one starts.
    # Continuous chunks form a run: positions count from the run's first sample.
    run, seq, start, prev_path = name[:-4], 0, 0, None
    prev = db.execute("SELECT name, session, seq, start_sample, frames, sr FROM chunks "
                      "WHERE name < ? ORDER BY name DESC LIMIT 1", (name,)).fetchone()
    if prev and chunk_epoch(prev[0]) is not None:
        p_name, p_run, p_seq, p_start, p_frames, p_sr = prev
        p_path = locate(p_name)
        if p_frames is None and p_path is not None:
            p_frames, p_sr = ogg_info(p_path)
        if p_frames and p_sr == sr and continuous(chunk_epoch(p_name), p_frames / p_sr, epoch):
            prev_path = p_path  # context, even if its analysis failed
            if p_start is not None:
                run, seq, start = p_run, p_seq + 1, p_start + p_frames

    ctx = np.zeros(0, dtype=np.float32)
    if prev_path:
        ctx, _ = read_mono(prev_path, start=-int(CONTEXT_S * sr))
    buf = np.concatenate([ctx, x])
    guard = int(GUARD_S * sr)
    own_from = max(0, len(ctx) - guard) if len(ctx) else 0
    own_to = len(buf) - guard

    events = detect.analyze(buf, sr, own_from, own_to)
    r_epoch = run_epoch(run)
    now = time.time()
    db.execute("DELETE FROM embeddings WHERE ref_event IN "
               "(SELECT id FROM events WHERE chunk=? AND status='candidate')", (name,))
    db.execute("DELETE FROM events WHERE chunk=? AND status='candidate'", (name,))
    for ev in events:
        # Plain ints/floats: numpy scalars would be stored by sqlite as blobs.
        s = int(start - len(ctx) + ev.start)
        e = int(start - len(ctx) + ev.end)
        db.execute(
            "INSERT INTO events(session, chunk, start_sample, end_sample, sr, at_time, kind, score, label, "
            "features, status, detect_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run, name, s, e, int(sr), r_epoch + s / sr, ev.kind, float(ev.score), ev.label,
             json.dumps(ev.features), "candidate", detect.VERSION, now))
    # New candidates have no tags yet: mark the chunk for the tagging step.
    db.execute("UPDATE chunks SET session=?, seq=?, start_sample=?, frames=?, sr=?, detect_version=?, ai_version=NULL "
               "WHERE name=?", (run, seq, int(start), int(frames), int(sr), detect.VERSION, name))
    return len(events)


# --- picking and cutting ----------------------------------------------------------

def read_session_range(db, locate, session, a, b):
    """Audio for samples [a, b) of a run ("session" in the DB), stitched across chunks
    wherever they live now."""
    rows = db.execute(
        "SELECT name, start_sample, frames FROM chunks WHERE session=? AND start_sample IS NOT NULL "
        "AND start_sample < ? AND start_sample + frames > ? ORDER BY start_sample", (session, b, a)).fetchall()
    pieces, pos, sr = [], a, None
    for name, start, frames in rows:
        if start > pos:
            raise ValueError(f"gap in session audio before {name}")
        path = locate(name)
        if path is None:
            raise FileNotFoundError(f"{name} is no longer available")
        x, sr = read_mono(path, start=pos - start, stop=min(frames, b - start))
        pieces.append(x)
        pos += len(x)
        if pos >= b:
            break
    if pos < b:
        raise ValueError("session audio ends before the requested range")
    return np.concatenate(pieces), sr


def fade(x, sr):
    n_in, n_out = min(len(x) // 4, int(FADE_IN_S * sr)), min(len(x) // 4, int(FADE_OUT_S * sr))
    x = x.copy()
    if n_in:
        x[:n_in] *= np.linspace(0, 1, n_in, dtype=np.float32)
    if n_out:
        x[-n_out:] *= np.linspace(1, 0, n_out, dtype=np.float32)
    return x


def clip_name(event_id, kind, label, at_time, suffix=""):
    day = datetime.fromtimestamp(at_time).strftime("%Y%m%d")
    return f"{label}_{day}_{event_id:05d}{suffix}.flac"


def write_flac(x, sr, dest):
    # Decoded Vorbis can overshoot full scale slightly on loud material; FLAC is integer
    # and would clip it. Turn the clip down just enough instead.
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 1.0:
        x = x * (0.999 / peak)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + ".part.flac")
    sf.write(str(part), x, sr, format="FLAC", subtype="PCM_24")
    os.replace(part, dest)


def cut_event(db, locate, event_id, out_dir, pad_s=0.0, suffix=""):
    session, a, b, sr, kind, label, at_time = db.execute(
        "SELECT session, start_sample, end_sample, sr, kind, label, at_time FROM events WHERE id=?",
        (event_id,)).fetchone()
    pad = int(pad_s * sr)
    x, sr = read_session_range(db, locate, session, max(0, a - pad), b + pad)
    dest = Path(out_dir) / clip_name(event_id, kind, label, at_time, suffix)
    write_flac(fade(x, sr), sr, dest)
    return dest


def select_and_cut(db, locate, samples_new_dir, quota, min_score, since):
    """Fill each hour's per-kind quota with tonight's best candidates and cut them.

    Hours already holding proposals from an earlier night (a recording crossing
    midnight) only get the remaining slots. A candidate overlapping an existing
    proposal of the same kind is skipped, so re-analysis doesn't duplicate clips.
    """
    cands = db.execute(
        "SELECT id, session, start_sample, end_sample, kind, score, at_time FROM events "
        "WHERE status='candidate' AND created_at >= ? ORDER BY score DESC", (since,)).fetchall()
    cut = failed = 0
    for ev_id, session, a, b, kind, score, at_time in cands:
        if score < min_score.get(kind, 1.0):
            continue
        hour = int(at_time // 3600) * 3600
        taken = db.execute(
            "SELECT count(*) FROM events WHERE status='proposed' AND kind=? AND at_time >= ? AND at_time < ?",
            (kind, hour, hour + 3600)).fetchone()[0]
        if taken >= quota.get(kind, 0):
            continue
        overlap = db.execute(
            "SELECT 1 FROM events WHERE status='proposed' AND kind=? AND session=? "
            "AND start_sample < ? AND end_sample > ?", (kind, session, b, a)).fetchone()
        if overlap:
            continue
        try:
            dest = cut_event(db, locate, ev_id, Path(samples_new_dir) / kind)
        except (OSError, ValueError, RuntimeError) as e:
            print(f"cutting event {ev_id} failed: {e}", flush=True)
            failed += 1
            continue
        db.execute("UPDATE events SET status='proposed' WHERE id=?", (ev_id,))
        db.execute("INSERT INTO clips(path, cut_at, state, event_id) VALUES (?,?,?,?)",
                   (str(dest), time.time(), "new", ev_id))
        db.commit()
        cut += 1
    return cut, failed


# --- tags and search vectors (milestone D) -------------------------------------------

QUIET_WINDOW_DB = -55.0  # windows quieter than this (an empty room) aren't worth searching


def slug(text, default="untagged"):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    return s or default


def tagged_label(kind, base, tag):
    """Clip name stem once the tagger has spoken: the tag, plus the note or tempo."""
    if kind in ("tonal", "loop"):
        return f"{slug(tag)}-{base}"
    return slug(tag)


def ai_chunk(db, models, locate, name):
    """Tag and embed one analyzed chunk's moments, and embed its audible 10 s windows."""
    import ai
    row = db.execute("SELECT session, start_sample, frames, sr FROM chunks WHERE name=?", (name,)).fetchone()
    if not row or row[1] is None:
        return 0, 0
    run, start, frames, sr = row
    end = start + frames
    model = ai.embed_name(models)
    r_epoch = run_epoch(run)

    evs = db.execute("SELECT id, start_sample, end_sample, kind, label, base_label, status FROM events "
                     "WHERE chunk=? AND tags IS NULL", (name,)).fetchall()
    clips, keep = [], []
    for ev in evs:
        a = ev[1]
        b = min(ev[2], a + int(ai.MAX_CLIP_S * sr), end)  # this chunk's part; the next may not be analyzed yet
        if b > a:
            clips.append(read_session_range(db, locate, run, a, b)[0])
            keep.append((ev, a, b))
    if clips:
        tags, vecs = models.tag(clips), models.embed_audio(clips)
        for (ev, a, b), t, v in zip(keep, tags, vecs):
            ev_id, _, _, kind, label, base, status = ev
            base = base or label
            tag = ai.pick_tag(t)
            # Clips already cut keep their file name; candidates take the tag in theirs.
            new_label = label if status == "proposed" or kind == "manual" else tagged_label(kind, base, tag)
            db.execute("UPDATE events SET tags=?, tag=?, base_label=?, label=? WHERE id=?",
                       (json.dumps(t), tag, base, new_label, ev_id))
            db.execute("DELETE FROM embeddings WHERE ref_event=?", (ev_id,))
            ai.store_vector(db, v, model, run, name, a, b, sr, r_epoch + a / sr, ref_event=ev_id)

    db.execute("DELETE FROM embeddings WHERE chunk=? AND ref_event IS NULL", (name,))
    w = int(ai.WINDOW_S * sr)
    windows, spans = [], []
    for a in range(start, end, w):
        b = min(end, a + w)
        if b - a < 3 * sr:
            continue
        x = read_session_range(db, locate, run, a, b)[0]
        if 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-10) < QUIET_WINDOW_DB:
            continue
        windows.append(x)
        spans.append((a, b))
    if windows:
        for (a, b), v in zip(spans, models.embed_audio(windows)):
            ai.store_vector(db, v, model, run, name, a, b, sr, r_epoch + a / sr)
    db.execute("UPDATE chunks SET ai_version=? WHERE name=?", (ai.VERSION, name))
    return len(keep), len(windows)
