#!/usr/bin/env python3
"""Receive WAV segments from the phone, and join them back into one file.

    python3 receiver.py serve [--dir recordings] [--bind 0.0.0.0] [--port 8765] [--token SECRET]
    python3 receiver.py join recordings/<session> out.wav

Standard library only. Segments land in <dir>/<session>/<session>_<seq>.wav.
"""
import argparse
import hashlib
import hmac
import os
import re
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME_RE = re.compile(r"^(?P<session>[0-9A-Za-z-]+)_(?P<seq>\d+)\.wav$")
CHUNK = 1 << 16


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


class Handler(BaseHTTPRequestHandler):
    root: Path = Path("recordings")
    token: str = ""

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
        if not m:
            return self.reply(400, "bad name")
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.reply(411, "length required")
        expected = (self.headers.get("X-Sha256") or "").lower()

        dest_dir = self.root / m["session"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        part = dest_dir / (name + ".part")

        h = hashlib.sha256()
        remaining = length
        with open(part, "wb") as f:
            while remaining:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                h.update(block)
                f.write(block)
                remaining -= len(block)
            f.flush()
            os.fsync(f.fileno())
        digest = h.hexdigest()

        if remaining or (expected and digest != expected):
            part.unlink(missing_ok=True)
            return self.reply(400, "incomplete or corrupt upload")

        # A retry after a lost response: same bytes are already here.
        if dest.exists() and file_sha256(dest) == digest:
            part.unlink()
            return self.reply(200, "already have it")

        os.replace(part, dest)
        self.log_message("saved %s (%d bytes)", dest, length)
        return self.reply(201, "saved")


def serve(args):
    Handler.root = Path(args.dir).resolve()
    Handler.token = args.token or os.environ.get("BUG_TOKEN", "")
    Handler.root.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Saving to {Handler.root}, listening on {args.bind}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def join(args):
    src = Path(args.session_dir)
    segs = []
    for p in src.glob("*.wav"):
        m = NAME_RE.match(p.name)
        if m:
            segs.append((int(m["seq"]), p))
    segs.sort()
    if not segs:
        sys.exit(f"no segments in {src}")

    missing = sorted(set(range(segs[0][0], segs[-1][0] + 1)) - {s for s, _ in segs})
    if missing:
        print(f"WARNING: missing segments {missing}; output will have gaps there", file=sys.stderr)
    if segs[0][0] != 0:
        print(f"WARNING: first segment is {segs[0][0]}, not 0", file=sys.stderr)

    with wave.open(str(segs[0][1]), "rb") as first:
        params = first.getparams()
    with wave.open(args.out, "wb") as out:
        out.setparams(params)
        for _, p in segs:
            with wave.open(str(p), "rb") as w:
                if w.getparams()[:3] != params[:3]:
                    sys.exit(f"{p.name} has a different format")
                out.writeframes(w.readframes(w.getnframes()))
    print(f"joined {len(segs)} segments into {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="receive uploads from the phone")
    s.add_argument("--dir", default="recordings")
    s.add_argument("--bind", default="0.0.0.0", help="tip: your Tailscale IP, so only the tailnet can reach it")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", help="shared secret (or set BUG_TOKEN)")
    s.set_defaults(fn=serve)
    j = sub.add_parser("join", help="concatenate a session's segments into one WAV")
    j.add_argument("session_dir")
    j.add_argument("out")
    j.set_defaults(fn=join)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
