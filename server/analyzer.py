#!/usr/bin/env python3
"""Nightly housekeeping and (later) analysis of recorded chunks.

    python3 analyzer.py run    [--config FILE] [--dry-run]
    python3 analyzer.py status [--config FILE]

Milestone A: finds unanalyzed chunks on the SSD working folder and in the HDD
'unanalyzed' folder, runs the analysis stages on them (none yet), archives them,
keeps the archive under its size cap, moves staged clips into the samples library
and keeps that under its own cap. See docs/analyzer-plan.md.

Standard library only (Python 3.11+). The config file is TOML, see
analyzer.example.toml. Refuses to run unless <hdd_root>/.bug-yourself-archive exists.
"""
import argparse
import contextlib
import fcntl
import os
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from receiver import (  # noqa: E402  (shared helpers)
    NAME_RE, Conflict, chunk_files, human, move_lock, move_verified, parse_size, remove_empty_dirs,
)

MARKER = ".bug-yourself-archive"
MAX_ATTEMPTS = 3
DEFAULT_CONFIG = Path.home() / ".config" / "bug-yourself" / "analyzer.toml"
AUDIO_EXT = {".flac", ".wav", ".ogg", ".mp3", ".aif", ".aiff"}


def log(msg):
    print(msg, flush=True)


# --- config -------------------------------------------------------------------

@dataclass
class Config:
    working_dir: Path     # SSD: where the receiver writes
    hdd_root: Path        # HDD: holds the marker, unanalyzed/, archive/, samples/, analyzer.db
    samples_new_dir: Path  # SSD: tonight's clips
    archive_max: int
    samples_max: int

    @property
    def unanalyzed_dir(self): return self.hdd_root / "unanalyzed"
    @property
    def archive_dir(self): return self.hdd_root / "archive"
    @property
    def samples_dir(self): return self.hdd_root / "samples"
    @property
    def db_path(self): return self.hdd_root / "analyzer.db"


def load_config(path):
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"config not found: {path} (copy server/analyzer.example.toml there)")
    try:
        return Config(
            working_dir=Path(raw["working_dir"]).expanduser(),
            hdd_root=Path(raw["hdd_root"]).expanduser(),
            samples_new_dir=Path(raw["samples_new_dir"]).expanduser(),
            archive_max=parse_size(raw.get("archive_max", "500GB")),
            samples_max=parse_size(raw.get("samples_max", "100GB")),
        )
    except KeyError as e:
        sys.exit(f"config is missing {e}")


# --- database -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    name         TEXT PRIMARY KEY,      -- <session>_<seq>.ogg
    session      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    state        TEXT NOT NULL,         -- pending | analyzed | analysis_failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    location     TEXT NOT NULL,         -- working | unanalyzed | archive | pruned
    first_seen   REAL NOT NULL,
    archived_at  REAL,
    pruned_at    REAL
);
CREATE TABLE IF NOT EXISTS clips (
    id       INTEGER PRIMARY KEY,
    path     TEXT UNIQUE NOT NULL,      -- current location
    cut_at   REAL NOT NULL,             -- "oldest" for pruning means oldest cut
    state    TEXT NOT NULL,             -- new | library | clip_removed | clip_pruned
    event_id INTEGER                    -- filled in once detection exists (milestone B)
);
"""


def open_db(path, dry=False):
    if dry:
        # Work on an in-memory copy so a dry run leaves no trace, not even a new DB file.
        db = sqlite3.connect(":memory:")
        if Path(path).exists():
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            src.backup(db)
            src.close()
    else:
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# --- analysis stages ----------------------------------------------------------
# Each stage takes (db, chunk_path) and raises on failure. Milestone A has none;
# detection, tagging and transcription plug in here later.
STAGES = []


def analyze(db, name, path):
    try:
        for stage in STAGES:
            stage(db, path)
    except Exception as e:  # one bad chunk must not stop the night's run
        attempts = db.execute("SELECT attempts FROM chunks WHERE name=?", (name,)).fetchone()[0] + 1
        state = "analysis_failed" if attempts >= MAX_ATTEMPTS else "pending"
        db.execute("UPDATE chunks SET attempts=?, state=?, last_error=? WHERE name=?",
                   (attempts, state, f"{type(e).__name__}: {e}", name))
        log(f"analysis of {name} failed ({attempts}/{MAX_ATTEMPTS}): {e}")
        return
    db.execute("UPDATE chunks SET state='analyzed', last_error=NULL WHERE name=?", (name,))


# --- the nightly run ----------------------------------------------------------

def dir_files(root):
    return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def total_size(files):
    return sum(f.stat().st_size for f in files)


class Run:
    def __init__(self, cfg, db, dry):
        self.cfg, self.db, self.dry = cfg, db, dry
        # Dry run: files that a real run would have added, so the pruning forecast is right.
        self.would_archive = []  # (name, size)
        self.would_add_samples = []  # (sort key, size)

    def say(self, msg):
        log(("[dry run] " if self.dry else "") + msg)

    # chunks ------------------------------------------------------------------

    def discover(self):
        """Register every finished chunk in either unanalyzed location."""
        found = {}
        for where, root in (("working", self.cfg.working_dir), ("unanalyzed", self.cfg.unanalyzed_dir)):
            for p in chunk_files(root) if root.is_dir() else []:
                found.setdefault(p.name, (where, p))  # mid-move duplicates: either copy is fine
        now = time.time()
        for name, (where, _) in found.items():
            m = NAME_RE.match(name)
            self.db.execute(
                "INSERT INTO chunks(name, session, seq, state, location, first_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET location=excluded.location",
                (name, m["session"], int(m["seq"]), "pending", where, now))
        return found

    def locate(self, name):
        """Chunks can move from the SSD to unanalyzed/ (receiver, 00:05) mid-run: look again."""
        session = NAME_RE.match(name)["session"]
        for root in (self.cfg.unanalyzed_dir, self.cfg.working_dir):
            p = root / session / name
            if p.exists():
                return p
        return None

    def process_chunks(self):
        found = self.discover()
        rows = self.db.execute(
            "SELECT name, state FROM chunks WHERE location IN ('working','unanalyzed') ORDER BY name").fetchall()
        archived = analyzed = 0
        for name, state in rows:
            if name not in found:
                continue  # registered earlier but gone; nothing to do
            if state == "pending":
                path = self.locate(name)
                if path and not self.dry:
                    analyze(self.db, name, path)
                    self.db.commit()
                analyzed += 1
            state = self.db.execute("SELECT state FROM chunks WHERE name=?", (name,)).fetchone()[0]
            if state != "pending" or self.dry:
                if self.archive(name):
                    archived += 1
        self.say(f"chunks: {analyzed} processed, {archived} archived")
        if not self.dry:
            remove_empty_dirs(self.cfg.unanalyzed_dir)

    def archive(self, name):
        session = NAME_RE.match(name)["session"]
        dst = self.cfg.archive_dir / session / name
        if self.dry:
            src = self.locate(name)
            if src:
                self.would_archive.append((name, src.stat().st_size))
            return src is not None
        # The SSD copy can only be moved while holding the receiver's lock.
        with move_lock(self.cfg.working_dir) if self.cfg.working_dir.is_dir() else contextlib.nullcontext():
            src = self.locate(name)
            if src is None:
                return False
            try:
                move_verified(src, dst)
            except (OSError, Conflict) as e:
                log(f"archiving {name} failed, will retry next run: {e}")
                return False
            if src.parent.parent == self.cfg.working_dir:
                remove_empty_dirs(self.cfg.working_dir, keep_newest=True)
        self.db.execute("UPDATE chunks SET location='archive', archived_at=? WHERE name=?", (time.time(), name))
        self.db.commit()
        return True

    def prune_archive(self):
        entries = [(p.name, p.stat().st_size, p) for p in dir_files(self.cfg.archive_dir) if NAME_RE.match(p.name)]
        entries += [(n, size, None) for n, size in self.would_archive]
        entries.sort(key=lambda e: e[0])
        total = total_size(dir_files(self.cfg.archive_dir)) + sum(size for _, size in self.would_archive)
        removed = 0
        for name, size, p in entries:
            if total <= self.cfg.archive_max:
                break
            if not self.dry:
                p.unlink()
                self.db.execute("UPDATE chunks SET location='pruned', pruned_at=? WHERE name=?", (time.time(), p.name))
            total -= size
            removed += 1
        if not self.dry:
            self.db.commit()
            remove_empty_dirs(self.cfg.archive_dir)
        self.say(f"archive: {human(total)} of {human(self.cfg.archive_max)}, pruned {removed} oldest chunk(s)")

    def clean_partials(self):
        """Half-written copies left by a crash mid-move. The original still exists."""
        cutoff = time.time() - 3600
        stale = [p for d in (self.cfg.unanalyzed_dir, self.cfg.archive_dir, self.cfg.samples_dir)
                 for p in dir_files(d) if ".part." in p.name and p.stat().st_mtime < cutoff]
        if not self.dry:
            for p in stale:
                p.unlink()
        if stale:
            self.say(f"removed {len(stale)} leftover partial file(s)")

    # samples -----------------------------------------------------------------

    def reconcile_clips(self):
        """Clips you moved out or deleted by hand are expected, not errors."""
        gone = [(cid,) for cid, path in self.db.execute(
            "SELECT id, path FROM clips WHERE state IN ('new','library')") if not Path(path).exists()]
        if gone and not self.dry:
            self.db.executemany("UPDATE clips SET state='clip_removed' WHERE id=?", gone)
            self.db.commit()
        if gone:
            self.say(f"samples: {len(gone)} clip(s) removed by you since last run")

    def move_new_samples(self):
        src_root, dst_root = self.cfg.samples_new_dir, self.cfg.samples_dir
        moved = 0
        for src in sorted(dir_files(src_root)):
            if ".part." in src.name:
                continue
            dst = dst_root / src.relative_to(src_root)
            if self.dry:
                self.would_add_samples.append((time.time(), src.stat().st_size))
            else:
                try:
                    move_verified(src, dst)
                except (OSError, Conflict) as e:
                    log(f"moving clip {src.name} failed, will retry next run: {e}")
                    continue
                cur = self.db.execute("UPDATE clips SET path=?, state='library' WHERE path=?", (str(dst), str(src)))
                if cur.rowcount == 0:  # a file the analyzer didn't make: track it anyway
                    self.db.execute("INSERT OR IGNORE INTO clips(path, cut_at, state) VALUES (?,?,?)",
                                    (str(dst), dst.stat().st_mtime, "library"))
            moved += 1
        if not self.dry:
            self.db.commit()
            if src_root.is_dir():
                for d in sorted((d for d in src_root.rglob("*") if d.is_dir()), reverse=True):
                    with contextlib.suppress(OSError):
                        d.rmdir()
        self.say(f"samples: moved {moved} new clip(s) to the library")

    def prune_samples(self):
        cut = dict(self.db.execute("SELECT path, cut_at FROM clips WHERE state='library'"))
        # Oldest cut first; files the DB doesn't know (your own subfolders) go by modification time.
        entries = [(cut.get(str(p), p.stat().st_mtime), p.stat().st_size, p) for p in dir_files(self.cfg.samples_dir)]
        entries += [(key, size, None) for key, size in self.would_add_samples]
        entries.sort(key=lambda e: e[0])
        total = sum(size for _, size, _ in entries)
        removed = 0
        for _, size, p in entries:
            if total <= self.cfg.samples_max:
                break
            if not self.dry:
                p.unlink()
                self.db.execute("UPDATE clips SET state='clip_pruned' WHERE path=?", (str(p),))
            total -= size
            removed += 1
        if not self.dry:
            self.db.commit()
        self.say(f"samples: {human(total)} of {human(self.cfg.samples_max)}, pruned {removed} oldest clip(s)")


@contextlib.contextmanager
def run_lock(cfg):
    f = open(cfg.hdd_root / ".analyzer.lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another analyzer run is in progress")
    try:
        yield
    finally:
        f.close()


def check_hdd(cfg):
    if not (cfg.hdd_root / MARKER).exists():
        sys.exit(f"{cfg.hdd_root / MARKER} not found: HDD not mounted, or create the marker "
                 f"once with: touch {cfg.hdd_root / MARKER}")


def cmd_run(cfg, args):
    check_hdd(cfg)
    if not args.dry_run:
        for d in (cfg.unanalyzed_dir, cfg.archive_dir, cfg.samples_dir):
            d.mkdir(exist_ok=True)
        cfg.samples_new_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with run_lock(cfg):
        db = open_db(cfg.db_path, dry=args.dry_run)
        run = Run(cfg, db, args.dry_run)
        # Last night's clips go to the HDD first, so tonight's land in an empty staging folder.
        run.clean_partials()
        run.reconcile_clips()
        run.move_new_samples()
        run.process_chunks()
        run.prune_archive()
        run.prune_samples()
        if args.dry_run:
            db.rollback()  # discovery inserts rows; a dry run keeps none of them
        else:
            db.commit()
        db.close()
    log(f"run finished in {time.time() - started:.0f}s")


def cmd_status(cfg, args):
    check_hdd(cfg)
    db = open_db(cfg.db_path)
    log("chunks by location/state:")
    for loc, state, n in db.execute("SELECT location, state, count(*) FROM chunks GROUP BY 1, 2 ORDER BY 1, 2"):
        log(f"  {loc:<11} {state:<16} {n}")
    for name, err in db.execute(
            "SELECT name, last_error FROM chunks WHERE last_error IS NOT NULL ORDER BY name DESC LIMIT 5"):
        log(f"  recent error: {name}: {err}")
    for label, d, cap in (("working (SSD)", cfg.working_dir, None),
                          ("unanalyzed", cfg.unanalyzed_dir, None),
                          ("archive", cfg.archive_dir, cfg.archive_max),
                          ("samples-new (SSD)", cfg.samples_new_dir, None),
                          ("samples", cfg.samples_dir, cfg.samples_max)):
        size = total_size(dir_files(d))
        log(f"{label:<18} {human(size):>9}" + (f" of {human(cap)}" if cap else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("BUG_ANALYZER_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the nightly run (what the timer calls)")
    r.add_argument("--dry-run", action="store_true", help="report what would move or be deleted, change nothing")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="show chunk states and folder sizes").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(load_config(args.config), args)


if __name__ == "__main__":
    main()
