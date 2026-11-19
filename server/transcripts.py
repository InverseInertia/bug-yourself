"""Transcript storage and lookup. Standard library only.

Nothing produces transcripts yet: transcription (milestone E) will call
store_chunk() once per analyzed chunk. Everything that reads them (the
analyzer's find-text command, the review app's timeline panel, clip details
and text search) already works against this table.

Times are kept two ways, like detected events: as sample positions within a
run (exact, for cutting audio) and as wall-clock epoch seconds (for the
timeline and searches by time).
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_segments (
    id            INTEGER PRIMARY KEY,
    run           TEXT NOT NULL,       -- the run (continuous recording) it's in
    chunk         TEXT NOT NULL,       -- the chunk it was transcribed with; replaced as a unit
    start_sample  INTEGER NOT NULL,    -- position in the run
    end_sample    INTEGER NOT NULL,
    sr            INTEGER NOT NULL,
    at_time       REAL NOT NULL,       -- wall-clock start (epoch seconds)
    end_time      REAL NOT NULL,
    text          TEXT NOT NULL,
    speaker       TEXT,                -- for speaker labels, if they're ever added
    confidence    REAL,                -- e.g. Whisper's average log-probability
    words         TEXT,                -- JSON [[start, end, word, probability], ...], epoch seconds
    model         TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_by_time ON transcript_segments(at_time);
CREATE INDEX IF NOT EXISTS transcript_by_chunk ON transcript_segments(chunk);
"""

# Full-text index kept in step with the table by triggers. Optional: if this
# SQLite lacks FTS5, search falls back to a plain substring match.
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
    USING fts5(text, content='transcript_segments', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS transcript_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS transcript_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS transcript_au AFTER UPDATE ON transcript_segments BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    try:
        db.executescript(FTS)
    except sqlite3.OperationalError:
        pass  # no FTS5 in this SQLite build


def has_fts(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='transcript_fts'").fetchone())


def store_chunk(db, chunk, run, chunk_start_sample, sr, run_epoch, segments, model=None):
    """Replace one chunk's transcript.

    segments: dicts with "start" and "end" in seconds from the chunk's first
    sample, "text", and optionally "speaker", "confidence" and "words"
    ([start, end, word, probability] with times in seconds from the chunk start).
    """
    db.execute("DELETE FROM transcript_segments WHERE chunk = ?", (chunk,))
    now = time.time()
    for seg in segments:
        a = chunk_start_sample + int(round(seg["start"] * sr))
        b = chunk_start_sample + int(round(seg["end"] * sr))
        base = run_epoch + chunk_start_sample / sr
        words = seg.get("words")
        db.execute(
            "INSERT INTO transcript_segments(run, chunk, start_sample, end_sample, sr, at_time, end_time, text, "
            "speaker, confidence, words, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run, chunk, a, b, sr, run_epoch + a / sr, run_epoch + b / sr, seg["text"].strip(),
             seg.get("speaker"), seg.get("confidence"),
             json.dumps([[base + w[0], base + w[1], w[2], w[3] if len(w) > 3 else None] for w in words])
             if words else None, model, now))


def _row(r):
    return {"id": r[0], "at": r[1], "end": r[2], "text": r[3], "speaker": r[4], "run": r[5]}


COLUMNS = "id, at_time, end_time, text, speaker, run"


def between(db, t0, t1, limit=2000):
    """Segments overlapping wall-clock [t0, t1), in time order."""
    rows = db.execute(f"SELECT {COLUMNS} FROM transcript_segments WHERE at_time < ? AND end_time > ? "
                      f"ORDER BY at_time LIMIT ?", (t1, t0, limit)).fetchall()
    return [_row(r) for r in rows]


def for_span(db, run, a, b):
    """Segments overlapping samples [a, b) of a run, e.g. what's said during a clip."""
    rows = db.execute(f"SELECT {COLUMNS} FROM transcript_segments WHERE run = ? AND start_sample < ? "
                      f"AND end_sample > ? ORDER BY start_sample", (run, b, a)).fetchall()
    return [_row(r) for r in rows]


def search(db, query, limit=100):
    """Best matches first when full-text search is available, else newest first."""
    query = query.strip()
    if not query:
        return []
    if has_fts(db):
        # Quote each word so punctuation in what people type can't break the FTS syntax.
        match = " ".join('"' + w.replace('"', '""') + '"' for w in query.split())
        rows = db.execute(
            f"SELECT s.id, s.at_time, s.end_time, s.text, s.speaker, s.run FROM transcript_fts f "
            f"JOIN transcript_segments s ON s.id = f.rowid WHERE transcript_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit)).fetchall()
    else:
        rows = db.execute(f"SELECT {COLUMNS} FROM transcript_segments WHERE text LIKE ? "
                          f"ORDER BY at_time DESC LIMIT ?", (f"%{query}%", limit)).fetchall()
    return [_row(r) for r in rows]


def count(db):
    return db.execute("SELECT count(*) FROM transcript_segments").fetchone()[0]
