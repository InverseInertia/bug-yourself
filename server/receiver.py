#!/usr/bin/env python3
"""Receive FLAC (or WAV) segments from the phone, store them as Ogg Vorbis, and join them back up.

    python3 receiver.py serve [--dir recordings] [--bind 0.0.0.0] [--port 8675]
                              [--token SECRET] [--quality 10] [--max-size 10GB]
    python3 receiver.py join recordings/<session> out.wav

Needs `oggenc`/`oggdec` (vorbis-tools) or `ffmpeg` built with libvorbis.
Chunks land in <dir>/<session>/<session>_<seq>.ogg. When the folder grows past
--max-size, the oldest chunks are deleted first.
"""
import argparse
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME_RE = re.compile(r"^(?P<session>[0-9A-Za-z-]+)_(?P<seq>\d+)\.(?P<ext>wav|flac|ogg)$")
CHUNK = 1 << 16
UNITS = {"": 1, "B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20,
         "G": 1 << 30, "GB": 1 << 30, "T": 1 << 40, "TB": 1 << 40}


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
                if NAME_RE.match(p.name):
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
                p.parent.rmdir()  # only succeeds once the session folder is empty
            except OSError:
                pass


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    root: Path = Path("recordings")
    token: str = ""
    quality: float = 10
    max_bytes: int = 0

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
        m = NAME_RE.match(name)
        if not m or m["ext"] not in ("wav", "flac"):
            return self.reply(400, "bad name")
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.reply(411, "length required")
        expected = (self.headers.get("X-Sha256") or "").lower()

        dest_dir = self.root / m["session"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = name.rsplit(".", 1)[0]
        dest = dest_dir / (stem + ".ogg")
        in_part = dest_dir / f"{stem}.part.{m['ext']}"  # keep the real extension for the encoder
        ogg_part = dest_dir / (stem + ".part.ogg")

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
        if dest.exists():
            in_part.unlink()
            return self.reply(200, "already have it")

        try:
            encode(in_part, ogg_part, self.quality)
            with open(ogg_part, "rb+") as f:
                os.fsync(f.fileno())
            os.replace(ogg_part, dest)
        except Exception as e:
            ogg_part.unlink(missing_ok=True)
            self.log_message("encode failed for %s: %s", name, e)
            return self.reply(500, "encode failed")
        finally:
            in_part.unlink(missing_ok=True)

        self.log_message("saved %s (%d -> %d bytes)", dest.name, length, dest.stat().st_size)
        prune(self.root, self.max_bytes)
        return self.reply(201, "saved")


def serve(args):
    codec = check_codec()
    Handler.root = Path(args.dir).resolve()
    Handler.token = args.token or os.environ.get("BUG_TOKEN", "")
    Handler.quality = args.quality
    Handler.max_bytes = args.max_size
    Handler.root.mkdir(parents=True, exist_ok=True)
    for stale in Handler.root.rglob("*.part.*"):
        stale.unlink()
    prune(Handler.root, args.max_size)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    limit = human(args.max_size) if args.max_size else "unlimited"
    print(f"Saving to {Handler.root} as Ogg Vorbis q{args.quality:g} via {codec}, limit {limit}, "
          f"listening on {args.bind}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --- join -------------------------------------------------------------------

def join(args):
    check_codec()
    src = Path(args.session_dir)
    segs = sorted((int(m["seq"]), p) for p in src.iterdir() if (m := NAME_RE.match(p.name)))
    if not segs:
        sys.exit(f"no segments in {src}")

    missing = sorted(set(range(segs[0][0], segs[-1][0] + 1)) - {s for s, _ in segs})
    if missing:
        print(f"WARNING: missing segments {missing}; output will have gaps there", file=sys.stderr)
    if segs[0][0] != 0:
        print(f"WARNING: first segment is {segs[0][0]}, not 0 (older chunks pruned?)", file=sys.stderr)

    out_path = Path(args.out)
    want_ogg = out_path.suffix.lower() == ".ogg"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        joined_wav = tmp / "joined.wav" if want_ogg else out_path
        params = None
        with wave.open(str(joined_wav), "wb") as out:
            for seq, p in segs:
                pcm = p
                if p.suffix != ".wav":
                    pcm = tmp / f"{seq}.wav"
                    decode(p, pcm)
                with wave.open(str(pcm), "rb") as w:
                    if params is None:
                        params = w.getparams()
                        out.setparams(params)
                    elif w.getparams()[:3] != params[:3]:
                        sys.exit(f"{p.name} has a different format")
                    out.writeframes(w.readframes(w.getnframes()))
                if pcm is not p:
                    pcm.unlink()
        if want_ogg:
            encode(joined_wav, out_path, args.quality)
    print(f"joined {len(segs)} segments into {out_path}")


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
                   help="delete oldest chunks past this folder size (default 10GB, 0 = unlimited)")
    s.set_defaults(fn=serve)
    j = sub.add_parser("join", help="decode a session's chunks and join them into one file (.wav or .ogg)")
    j.add_argument("session_dir")
    j.add_argument("out")
    j.add_argument("--quality", type=quality, default=quality(os.environ.get("BUG_QUALITY", 10)),
                   help="Vorbis quality if out is .ogg (default 10, or set BUG_QUALITY)")
    j.set_defaults(fn=join)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
