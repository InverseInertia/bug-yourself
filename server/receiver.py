#!/usr/bin/env python3
"""Receive FLAC (or WAV) segments from the phone, store them as Ogg Vorbis, and join them back up.

    python3 receiver.py serve [--dir recordings] [--bind 0.0.0.0] [--port 8675]
                              [--token SECRET] [--quality 10] [--max-size 10GB]
                              [--when-full reject|prune] [--unanalyzed-dir HDD/unanalyzed]
                              [--offload-at 00:05]
    python3 receiver.py join <folders or chunk files...> out.wav [--from STAMP] [--to STAMP]

Needs `oggenc`/`oggdec` (vorbis-tools) or `ffmpeg` built with libvorbis.
Chunks are named after the UTC time they start (see names.py) and land in
<dir>/<YYYYMMDD>/<YYYYMMDDTHHMMSSZ>.ogg. <dir> is a working folder: the analyzer
moves chunks out of it to the long-term archive every night. Uploads with the old
<session>_<seq> names are renamed to their start time on arrival.

When the folder reaches --max-size, the default (--when-full reject) is to refuse
new uploads with 507; the phone keeps its files and retries, so nothing is lost
while the analyzer catches up. --when-full prune deletes the oldest chunks instead.

With --unanalyzed-dir, every finished chunk is moved there once a day (default
00:05 local time, and once at startup), so the SSD working folder only ever holds
about a day even if the analyzer is down. The analyzer reads from both folders.
The target folder must already exist, so an unmounted drive is skipped rather
than silently filled on the wrong disk; uploads then pile up until the 507 limit.
"""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from names import (  # noqa: E402
    LEGACY_RE, NAME_RE, chunk_epoch, continuous, day_folder, epoch_stamp, legacy_session_epoch, ogg_info,
)

LEGACY_TABLE = ".legacy-names.json"
CHUNK = 1 << 16
UNITS = {"": 1, "B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20,
         "G": 1 << 30, "GB": 1 << 30, "T": 1 << 40, "TB": 1 << 40}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def parse_size(s):
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)\s*", s)
    if not m or m[2].upper() not in UNITS:
        raise argparse.ArgumentTypeError(f"bad size {s!r} (try 10GB, 500MB, 0 for unlimited)")
    return int(float(m[1]) * UNITS[m[2].upper()])


def quality(s):
    q = float(s)
    if not -1 <= q <= 10:
        raise argparse.ArgumentTypeError("quality must be between -1 and 10")
    return q


def hhmm(s):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise argparse.ArgumentTypeError("use HH:MM, e.g. 00:05")
    return s


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.3g} {unit}"
        n /= 1024
    return f"{n:.3g} TB"


# --- codec ------------------------------------------------------------------

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[-500:]}")


def encode(wav, ogg, quality):
    if shutil.which("oggenc"):
        run(["oggenc", "-Q", "-q", f"{quality:g}", "-o", str(ogg), str(wav)])
    else:
        run(["ffmpeg", "-v", "error", "-y", "-i", str(wav),
             "-c:a", "libvorbis", "-q:a", f"{quality:g}", "-f", "ogg", str(ogg)])


def decode(ogg, wav):
    if shutil.which("oggdec"):
        run(["oggdec", "-Q", "-o", str(wav), str(ogg)])
    else:
        run(["ffmpeg", "-v", "error", "-y", "-i", str(ogg), "-c:a", "pcm_s16le", "-f", "wav", str(wav)])


def check_codec():
    if shutil.which("oggenc") and shutil.which("oggdec"):
        return "oggenc"
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libvorbis" in enc:
            return "ffmpeg/libvorbis"
    sys.exit("Need vorbis-tools (oggenc/oggdec) or ffmpeg with libvorbis on PATH.")


# --- storage limit ----------------------------------------------------------

prune_lock = threading.Lock()


def folder_size(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def prune(root, max_bytes):
    """Delete the oldest chunks until the folder is under max_bytes. Names sort by time."""
    if max_bytes <= 0:
        return
    with prune_lock:
        chunks = []
        total = 0
        for p in root.rglob("*"):
            if p.is_file():
                size = p.stat().st_size
                total += size
                if is_any_chunk(p.name):
                    chunks.append((p.name, p, size))
        chunks.sort()
        # Never delete the newest chunk, even if it alone is over the limit.
        for _, p, size in chunks[:-1]:
            if total <= max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size
            print(f"pruned {p.name} (folder over limit)", flush=True)
            try:
                p.parent.rmdir()  # only succeeds once the day folder is empty
            except OSError:
                pass


# --- nightly offload --------------------------------------------------------

LOCK_NAME = ".move.lock"


@contextlib.contextmanager
def move_lock(root):
    """Held by whoever moves chunks out of the working folder (this offload, or the analyzer)."""
    with open(root / LOCK_NAME, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Conflict(Exception):
    """The destination already holds a different file with the same name."""


def move_verified(src, dst):
    """Move src to dst so that a crash at any point leaves at least one intact copy.

    Same filesystem: atomic rename. Across drives: copy to a .part file, fsync, compare
    SHA-256, rename into place, then delete src. If dst already exists (an earlier move
    was interrupted) and is identical, src is just deleted.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if file_sha256(dst) != file_sha256(src):
            raise Conflict(f"{dst} exists with different content")
        src.unlink()
        return
    if src.stat().st_dev == dst.parent.stat().st_dev:
        os.replace(src, dst)
        return
    digest = file_sha256(src)
    part = dst.with_name(dst.stem + ".part" + dst.suffix)
    shutil.copyfile(src, part)
    with open(part, "rb+") as f:
        os.fsync(f.fileno())
    if file_sha256(part) != digest:
        part.unlink()
        raise OSError("copy does not match source")
    os.replace(part, dst)
    src.unlink()


def remove_empty_dirs(root, keep_newest=False):
    """Remove empty day folders. keep_newest spares the folder uploads may still be landing in."""
    dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
    for d in dirs[:-1] if keep_newest else dirs:
        try:
            d.rmdir()
        except OSError:
            pass


def is_any_chunk(name):
    return bool(NAME_RE.match(name) or LEGACY_RE.match(name))


def chunk_files(root):
    """Finished chunks under root (never *.part.*), oldest first. Includes old-style
    names, which the analyzer renames on its next run."""
    return sorted((p for p in root.rglob("*.ogg") if is_any_chunk(p.name)), key=lambda p: p.name)


# --- old-style names ------------------------------------------------------------
# A <session>_<seq> chunk starts where chunk seq-1 of that session ended. The table
# remembers each converted chunk's exact start, so the next one continues exactly
# even when it arrives later (from the phone's outbox, or after the analyzer has
# renamed the ones already on disk). Callers hold move_lock(root).

def load_legacy_table(root):
    try:
        return json.loads((root / LEGACY_TABLE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_legacy_table(root, table):
    tmp = root / (LEGACY_TABLE + ".tmp")
    tmp.write_text(json.dumps(table))
    os.replace(tmp, root / LEGACY_TABLE)


def legacy_assign(table, session, seq, frames, rate):
    """New file name for chunk <session>_<seq>, which has `frames` samples at `rate`."""
    entries = table.setdefault(session, {})
    if str(seq) not in entries:
        prev = entries.get(str(seq - 1))
        if seq == 0:
            start_ms = legacy_session_epoch(session) * 1000
        elif prev:
            start_ms = prev[0] + prev[1] * 1000 / prev[2]
        else:  # exact for every chunk but a session's last, which is the only short one
            start_ms = legacy_session_epoch(session) * 1000 + seq * frames * 1000 / rate
        entries[str(seq)] = [start_ms, frames, rate]
    return epoch_stamp(entries[str(seq)][0] / 1000) + ".ogg"


def offload(root, dest):
    """Move every finished chunk to dest (the HDD 'unanalyzed' folder) so the SSD stays small."""
    if not dest.is_dir():
        print(f"offload skipped: {dest} does not exist (drive not mounted?)", flush=True)
        return
    moved = 0
    with move_lock(root):
        for src in chunk_files(root):
            try:
                move_verified(src, dest / src.parent.name / src.name)
                moved += 1
            except FileNotFoundError:
                continue  # the analyzer archived it first
            except (OSError, Conflict) as e:
                print(f"offload of {src.name} failed: {e}", flush=True)
        # The phone may still be uploading into the newest day folder. (Any other
        # collision just fails one upload, which the phone retries.)
        remove_empty_dirs(root, keep_newest=True)
    print(f"offload: moved {moved} chunk(s) to {dest}", flush=True)


def next_run(at, now):
    h, m = map(int, at.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def offload_loop(root, dest, at):
    offload(root, dest)  # catch up after downtime
    while True:
        wait = (next_run(at, datetime.now()) - datetime.now()).total_seconds()
        time.sleep(max(wait, 1))
        offload(root, dest)


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    root: Path = Path("recordings")
    token: str = ""
    quality: float = 10
    max_bytes: int = 0
    when_full: str = "reject"

    def reply(self, code, msg=""):
        body = (msg + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, "ok")
        else:
            self.reply(404, "not found")

    def do_PUT(self):
        if self.token and not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            return self.reply(401, "bad token")

        prefix = "/upload/"
        name = self.path[len(prefix):] if self.path.startswith(prefix) else ""
        m = NAME_RE.match(name) or LEGACY_RE.match(name)
        if not m or m["ext"] not in ("wav", "flac"):
            return self.reply(400, "bad name")
        legacy = "session" in m.groupdict()
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.reply(411, "length required")
        expected = (self.headers.get("X-Sha256") or "").lower()

        if self.when_full == "reject" and self.max_bytes > 0:
            if folder_size(self.root) + length > self.max_bytes:
                self.log_message("working folder full, refusing %s", name)
                return self.reply(507, "working folder full")

        stem = name.rsplit(".", 1)[0]
        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        in_part = incoming / f"{stem}.part.{m['ext']}"  # keep the real extension for the encoder
        ogg_part = incoming / (stem + ".part.ogg")
        # Old-style names only get their final name once the encoded length is known.
        dest = None if legacy else self.root / day_folder(name) / (stem + ".ogg")

        h = hashlib.sha256()
        remaining = length
        with open(in_part, "wb") as f:
            while remaining:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                h.update(block)
                f.write(block)
                remaining -= len(block)

        if remaining or (expected and h.hexdigest() != expected):
            in_part.unlink(missing_ok=True)
            return self.reply(400, "incomplete or corrupt upload")

        # A retry after a lost response: this chunk is already stored.
        if dest is not None and dest.exists():
            in_part.unlink()
            return self.reply(200, "already have it")

        try:
            encode(in_part, ogg_part, self.quality)
            with open(ogg_part, "rb+") as f:
                os.fsync(f.fileno())
            if legacy:
                frames, rate = ogg_info(ogg_part)
                with move_lock(self.root):
                    table = load_legacy_table(self.root)
                    final = legacy_assign(table, m["session"], int(m["seq"]), frames, rate)
                    save_legacy_table(self.root, table)
                dest = self.root / day_folder(final) / final
                if dest.exists():
                    ogg_part.unlink()
                    return self.reply(200, "already have it")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(ogg_part, dest)
        except Exception as e:
            ogg_part.unlink(missing_ok=True)
            self.log_message("encode failed for %s: %s", name, e)
            return self.reply(500, "encode failed")
        finally:
            in_part.unlink(missing_ok=True)

        self.log_message("saved %s%s (%d -> %d bytes)", dest.name, f" (sent as {name})" if legacy else "",
                         length, dest.stat().st_size)
        if self.when_full == "prune":
            prune(self.root, self.max_bytes)
        return self.reply(201, "saved")


def serve(args):
    codec = check_codec()
    Handler.root = Path(args.dir).resolve()
    Handler.token = args.token or os.environ.get("BUG_TOKEN", "")
    Handler.quality = args.quality
    Handler.max_bytes = args.max_size
    Handler.when_full = args.when_full
    Handler.root.mkdir(parents=True, exist_ok=True)
    for stale in Handler.root.rglob("*.part.*"):
        stale.unlink()
    if args.when_full == "prune":
        prune(Handler.root, args.max_size)
    if args.unanalyzed_dir:
        threading.Thread(target=offload_loop, daemon=True,
                         args=(Handler.root, Path(args.unanalyzed_dir).resolve(), args.offload_at)).start()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    limit = f"{human(args.max_size)} ({args.when_full} when full)" if args.max_size else "unlimited"
    print(f"Saving to {Handler.root} as Ogg Vorbis q{args.quality:g} via {codec}, limit {limit}, "
          f"listening on {args.bind}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --- join -------------------------------------------------------------------

def chunk_sort_key(p):
    if (m := NAME_RE.match(p.name)):
        return (chunk_epoch(p.name), 0)
    m = LEGACY_RE.match(p.name)
    return (legacy_session_epoch(m["session"]), int(m["seq"]))


def join(args):
    """Join chunks in time order into one file, warning wherever the audio isn't continuous."""
    check_codec()
    found = {}
    for src in map(Path, args.inputs):
        for p in [src] if src.is_file() else sorted(src.rglob("*")):
            if p.is_file() and is_any_chunk(p.name):
                found.setdefault(p.name, p)  # the same chunk may be in two folders mid-move
    segs = sorted(found.values(), key=chunk_sort_key)
    if args.start:
        segs = [p for p in segs if chunk_sort_key(p)[0] >= chunk_epoch(args.start + ".ogg")]
    if args.end:
        segs = [p for p in segs if chunk_sort_key(p)[0] <= chunk_epoch(args.end + ".ogg")]
    if not segs:
        sys.exit(f"no chunks found in {', '.join(args.inputs)}")

    out_path = Path(args.out)
    want_ogg = out_path.suffix.lower() == ".ogg"
    gaps = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        joined_wav = tmp / "joined.wav" if want_ogg else out_path
        params = None
        prev = None  # (start epoch, seconds) of the previous new-style chunk
        with wave.open(str(joined_wav), "wb") as out:
            for i, p in enumerate(segs):
                pcm = p
                if p.suffix != ".wav":
                    pcm = tmp / f"{i}.wav"
                    decode(p, pcm)
                with wave.open(str(pcm), "rb") as w:
                    if params is None:
                        params = w.getparams()
                        out.setparams(params)
                    elif w.getparams()[:3] != params[:3]:
                        sys.exit(f"{p.name} has a different format")
                    epoch = chunk_epoch(p.name)
                    if prev and epoch is not None and not continuous(prev[0], prev[1], epoch):
                        gap = epoch - prev[0] - prev[1]
                        print(f"WARNING: {gap:+.0f} s gap before {p.name}; the output jumps there",
                              file=sys.stderr)
                        gaps += 1
                    prev = (epoch, w.getnframes() / w.getframerate()) if epoch is not None else None
                    out.writeframes(w.readframes(w.getnframes()))
                if pcm is not p:
                    pcm.unlink()
        if want_ogg:
            encode(joined_wav, out_path, args.quality)
    print(f"joined {len(segs)} chunks into {out_path}" + (f" ({gaps} gap(s))" if gaps else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="receive uploads from the phone")
    s.add_argument("--dir", default="recordings")
    s.add_argument("--bind", default="0.0.0.0", help="tip: your Tailscale IP, so only the tailnet can reach it")
    s.add_argument("--port", type=int, default=8675)
    s.add_argument("--token", help="shared secret (or set BUG_TOKEN)")
    s.add_argument("--quality", type=quality, default=quality(os.environ.get("BUG_QUALITY", 10)),
                   help="Vorbis quality, -1..10 (default 10, or set BUG_QUALITY)")
    s.add_argument("--max-size", type=parse_size, default=parse_size("10GB"),
                   help="working folder size limit (default 10GB, 0 = unlimited)")
    s.add_argument("--unanalyzed-dir", help="move chunks here daily, e.g. an 'unanalyzed' folder on the HDD (must exist)")
    s.add_argument("--offload-at", type=hhmm, default="00:05", help="local time for the daily move (default 00:05)")
    s.add_argument("--when-full", choices=["reject", "prune"], default="reject",
                   help="at the limit: refuse uploads so the phone holds them (default), or delete oldest chunks")
    s.set_defaults(fn=serve)
    j = sub.add_parser("join", help="decode chunks and join them in time order into one file (.wav or .ogg)")
    j.add_argument("inputs", nargs="+", metavar="folder_or_chunk",
                   help="day folders (recordings/20261117), whole folders, or chunk files")
    j.add_argument("--from", dest="start", metavar="STAMP", help="first chunk, e.g. 20261117T201500Z")
    j.add_argument("--to", dest="end", metavar="STAMP", help="last chunk")
    j.add_argument("out")
    j.add_argument("--quality", type=quality, default=quality(os.environ.get("BUG_QUALITY", 10)),
                   help="Vorbis quality if out is .ogg (default 10, or set BUG_QUALITY)")
    j.set_defaults(fn=join)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
