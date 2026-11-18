"""Review web app: browse proposed and kept clips, rate them, tune detection.

Started by `analyzer.py web`. Standard library HTTP server; serving audio cut
straight from the recordings (widen, previews of uncut candidates) needs the
same numpy/soundfile modules as detection.

Endpoints (JSON unless noted):
  GET  /                         the app (web/index.html)
  GET  /api/clips?view=proposed|kept&kind=&sort=time|score&offset=&limit=
  GET  /api/event/<id>           details, features, rating
  GET  /api/event/<id>/audio?pad=S   WAV cut from the source recording (X-Clip-Start/End: seconds)
  GET  /api/clip/<id>/file       the clip file itself
  GET  /api/kept/file?p=<token>  a file from a kept folder
  POST /api/event/<id>/rate      {"rating": 1 | -1 | 0}
  GET  /api/tuning               settings, per-kind stats, score histogram with ratings
  GET  /api/preview?kind=&min_score=&quota=&days=   what a setting would propose
  POST /api/tuning               {"quota": {...}, "min_score": {...}} -> analyzer.toml
"""
import base64
import hashlib
import hmac
import io
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KINDS = ["percussive", "tonal", "loop", "texture"]
AUDIO_EXT = {".flac", ".wav", ".ogg", ".mp3", ".aif", ".aiff", ".m4a"}
CLIP_NAME_RE = re.compile(r"^.+_\d{8}_(?P<event>\d+)(?:_pad[\d.]+)?\.flac$")
INDEX = Path(__file__).resolve().parent / "web" / "index.html"
COOKIE = "bug_session"
MAX_PAD_S = 15.0


class App:
    """Everything the handler needs; one per server."""

    def __init__(self, cfg, token, analyzer):
        self.cfg, self.token, self.analyzer = cfg, token, analyzer
        self.cookie = hashlib.sha256(("bug-yourself:" + token).encode()).hexdigest() if token else ""
        self.sampling, self.sampling_error = analyzer.load_sampling()

    def db(self):
        db = sqlite3.connect(self.cfg.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        db.row_factory = sqlite3.Row
        return db

    def locate(self, name):
        return self.analyzer.locate_chunk(self.cfg, name, archive=True)


def kept_token(path):
    return base64.urlsafe_b64encode(str(path).encode()).decode().rstrip("=")


def kept_path(token):
    return Path(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())


def event_row(r):
    d = dict(r)
    d["duration"] = (d.pop("end_sample") - d.pop("start_sample")) / d.pop("sr")
    if "features" in d:
        d["features"] = json.loads(d["features"])
    return d


# --- queries ------------------------------------------------------------------

def list_proposed(app, kind, sort, offset, limit):
    where, params = ["e.status='proposed'", "c.state IN ('new','library')"], []
    if kind:
        where.append("e.kind=?")
        params.append(kind)
    order = "e.score DESC, e.at_time DESC" if sort == "score" else "e.at_time DESC"
    with app.db() as db:
        rows = db.execute(
            f"SELECT e.id, e.at_time, e.kind, e.label, e.score, e.start_sample, e.end_sample, e.sr, "
            f"c.id AS clip_id, c.state AS clip_state, r.rating FROM events e "
            f"JOIN clips c ON c.event_id = e.id LEFT JOIN ratings r ON r.event_id = e.id "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        total = db.execute(
            f"SELECT count(*) FROM events e JOIN clips c ON c.event_id = e.id WHERE {' AND '.join(where)}",
            params).fetchone()[0]
    return {"items": [event_row(r) for r in rows], "total": total}


def list_kept(app, offset, limit):
    files = []
    for d in app.cfg.kept_dirs:
        if d.is_dir():
            files += [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    page = files[offset:offset + limit]
    ids = {p: int(m["event"]) for p in page if (m := CLIP_NAME_RE.match(p.name))}
    events = {}
    if ids:
        with app.db() as db:
            q = ",".join("?" * len(ids))
            for r in db.execute(
                    f"SELECT e.id, e.at_time, e.kind, e.label, e.score, e.start_sample, e.end_sample, e.sr, "
                    f"r.rating FROM events e LEFT JOIN ratings r ON r.event_id = e.id WHERE e.id IN ({q})",
                    list(ids.values())):
                events[r["id"]] = event_row(r)
    items = []
    for p in page:
        ev = events.get(ids.get(p))
        items.append({"file": kept_token(p), "name": p.name, "folder": str(p.parent),
                      "modified": p.stat().st_mtime, "size": p.stat().st_size, "event": ev})
    return {"items": items, "total": len(files), "dirs": [str(d) for d in app.cfg.kept_dirs]}


def event_details(app, ev_id):
    with app.db() as db:
        r = db.execute(
            "SELECT e.id, e.session AS run, e.chunk, e.at_time, e.kind, e.label, e.score, e.features, e.status, "
            "e.start_sample, e.end_sample, e.sr, r.rating, c.id AS clip_id, c.state AS clip_state, c.path AS clip_path "
            "FROM events e LEFT JOIN ratings r ON r.event_id = e.id "
            "LEFT JOIN clips c ON c.event_id = e.id WHERE e.id = ?", (ev_id,)).fetchone()
    return event_row(r) if r else None


def tuning_stats(app):
    out = {"quota": app.cfg.quota, "min_score": app.cfg.min_score, "kinds": {}}
    with app.db() as db:
        for kind in KINDS:
            rows = db.execute(
                "SELECT e.score, e.status, r.rating FROM events e LEFT JOIN ratings r ON r.event_id = e.id "
                "WHERE e.kind = ?", (kind,)).fetchall()
            bins = [{"lo": i / 10, "hi": (i + 1) / 10, "total": 0, "good": 0, "bad": 0} for i in range(10)]
            for score, _, rating in rows:
                b = bins[min(9, int(score * 10))]
                b["total"] += 1
                b["good"] += rating == 1
                b["bad"] += rating == -1
            out["kinds"][kind] = {
                "detected": len(rows),
                "proposed": sum(1 for _, s, _ in rows if s == "proposed"),
                "good": sum(1 for *_, r in rows if r == 1),
                "bad": sum(1 for *_, r in rows if r == -1),
                "bins": bins,
            }
    return out


def preview(app, kind, min_score, quota, days):
    """Which moments a setting would turn into clips, using the stored scores.
    Same rule as the nightly run: best first, at most `quota` per hour."""
    since = time.time() - days * 86400
    with app.db() as db:
        rows = db.execute(
            "SELECT e.id, e.at_time, e.kind, e.label, e.score, e.status, e.start_sample, e.end_sample, e.sr, "
            "c.id AS clip_id, c.state AS clip_state, r.rating FROM events e "
            "LEFT JOIN clips c ON c.event_id = e.id LEFT JOIN ratings r ON r.event_id = e.id "
            "WHERE e.kind = ? AND e.at_time >= ? ORDER BY e.score DESC", (kind, since)).fetchall()
    per_hour, picked = {}, []
    for r in rows:
        if r["score"] < min_score:
            break
        hour = int(r["at_time"] // 3600)
        if per_hour.get(hour, 0) >= quota:
            continue
        per_hour[hour] = per_hour.get(hour, 0) + 1
        picked.append(r)
    current = sum(1 for r in rows if r["status"] == "proposed")
    picked_ids = {r["id"] for r in picked}
    items = sorted((event_row(r) for r in picked), key=lambda e: -e["at_time"])
    for e in items:
        e["in_current"] = e["id"] in {r["id"] for r in rows if r["status"] == "proposed"}
    return {
        "items": items[:300], "would_propose": len(picked), "currently_proposed": current,
        "both": sum(1 for r in rows if r["status"] == "proposed" and r["id"] in picked_ids),
        "rated_good": sum(1 for r in picked if r["rating"] == 1),
        "rated_bad": sum(1 for r in picked if r["rating"] == -1),
        "days": days,
    }


def event_audio(app, ev_id, pad_s):
    """WAV of an event straight from the recording, with pad_s extra on each side."""
    if app.sampling is None:
        raise RuntimeError(f"audio cutting unavailable: {app.sampling_error}")
    import numpy as np
    import soundfile as sf
    with app.db() as db:
        r = db.execute("SELECT session, start_sample, end_sample, sr FROM events WHERE id = ?", (ev_id,)).fetchone()
        if r is None:
            raise KeyError(ev_id)
        run, a, b, sr = r
        pad = int(min(MAX_PAD_S, max(0.0, pad_s)) * sr)
        # Clamp to the run: only pad as far as the recording actually goes.
        first, last = db.execute("SELECT min(start_sample), max(start_sample + frames) FROM chunks "
                                 "WHERE session = ? AND start_sample IS NOT NULL", (run,)).fetchone()
        lo, hi = max(first or 0, a - pad), min(last or b, b + pad)
        x, sr = app.sampling.read_session_range(db, app.locate, run, lo, hi)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 1.0:
        x = x * (0.999 / peak)
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue(), (a - lo) / sr, (b - lo) / sr


def set_rating(app, ev_id, rating):
    with app.db() as db:
        if not db.execute("SELECT 1 FROM events WHERE id = ?", (ev_id,)).fetchone():
            raise KeyError(ev_id)
        if rating == 0:
            db.execute("DELETE FROM ratings WHERE event_id = ?", (ev_id,))
        else:
            db.execute("INSERT INTO ratings(event_id, rating, rated_at) VALUES (?,?,?) "
                       "ON CONFLICT(event_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at",
                       (ev_id, rating, time.time()))


def save_tuning(app, body):
    try:
        quota = {k: int(body["quota"][k]) for k in KINDS}
        min_score = {k: float(body["min_score"][k]) for k in KINDS}
    except (KeyError, TypeError) as e:
        raise ValueError(f"quota and min_score need a value for each of {', '.join(KINDS)}") from e
    if not all(0 <= v <= 1000 for v in quota.values()) or not all(0 <= v <= 1 for v in min_score.values()):
        raise ValueError("quota must be 0-1000 and min_score 0-1")
    app.analyzer.save_detect_settings(app.cfg.path, quota, min_score)
    app.cfg.quota, app.cfg.min_score = quota, min_score


# --- HTTP ---------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bug Yourself</title><style>body{font:16px system-ui;background:#16181d;color:#e6e6e6;display:grid;
place-items:center;height:100vh;margin:0}form{display:grid;gap:12px;width:min(320px,90vw)}input,button{font:inherit;
padding:10px;border-radius:8px;border:1px solid #3a3f4b;background:#20232b;color:inherit}button{background:#4f7cff;
border:0;color:#fff}</style><form method=post action=/login><h2>Bug Yourself</h2>
<input type=password name=token placeholder="Token (same as the phone)" autofocus>%s<button>Open</button></form>"""


class Handler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the journal quiet: errors only
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if not self.app.token:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, self.app.cookie):
                return True
        return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self.authed():
            if url.path.startswith("/api/"):
                return self.send(401, {"error": "not signed in"})
            return self.send(200, LOGIN_PAGE % "", "text/html; charset=utf-8")
        try:
            return self.route_get(url.path, q)
        except KeyError:
            return self.send(404, {"error": "not found"})
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            return self.send(409, {"error": str(e)})

    def route_get(self, path, q):
        app = self.app
        if path in ("/", "/index.html"):
            return self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/clips":
            offset, limit = int(q.get("offset", 0)), min(500, int(q.get("limit", 100)))
            if q.get("view") == "kept":
                return self.send(200, list_kept(app, offset, limit))
            return self.send(200, list_proposed(app, q.get("kind") or None, q.get("sort", "time"), offset, limit))
        if (m := re.fullmatch(r"/api/event/(\d+)", path)):
            ev = event_details(app, int(m[1]))
            return self.send(200, ev) if ev else self.send(404, {"error": "no such event"})
        if (m := re.fullmatch(r"/api/event/(\d+)/audio", path)):
            data, s, e = event_audio(app, int(m[1]), float(q.get("pad", 0)))
            return self.send(200, data, "audio/wav", {"X-Clip-Start": f"{s:.4f}", "X-Clip-End": f"{e:.4f}"})
        if (m := re.fullmatch(r"/api/clip/(\d+)/file", path)):
            with app.db() as db:
                r = db.execute("SELECT path FROM clips WHERE id = ? AND state IN ('new','library')",
                               (int(m[1]),)).fetchone()
            if not r or not Path(r[0]).is_file():
                return self.send(404, {"error": "clip file is gone"})
            p = Path(r[0])
            return self.send(200, p.read_bytes(), "audio/flac",
                             {"Content-Disposition": f'inline; filename="{p.name}"'})
        if path == "/api/kept/file":
            p = kept_path(q["p"]).resolve()
            # Only files inside a configured kept folder, never anything else on disk.
            if not any(p.is_relative_to(d.resolve()) for d in app.cfg.kept_dirs) or not p.is_file():
                return self.send(404, {"error": "not a kept file"})
            ctype = {".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
                     ".mp3": "audio/mpeg"}.get(p.suffix.lower(), "application/octet-stream")
            return self.send(200, p.read_bytes(), ctype, {"Content-Disposition": f'inline; filename="{p.name}"'})
        if path == "/api/tuning":
            return self.send(200, tuning_stats(app))
        if path == "/api/preview":
            kind = q.get("kind")
            if kind not in KINDS:
                raise ValueError("unknown kind")
            return self.send(200, preview(app, kind, float(q.get("min_score", 0.6)),
                                          int(q.get("quota", 4)), float(q.get("days", 14))))
        raise KeyError(path)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/login":
            form = {k: v[0] for k, v in parse_qs(self.body().decode()).items()}
            if self.app.token and hmac.compare_digest(form.get("token", ""), self.app.token):
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"{COOKIE}={self.app.cookie}; HttpOnly; SameSite=Strict; "
                                               f"Path=/; Max-Age=31536000")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self.send(401, LOGIN_PAGE % "<p>Wrong token.</p>", "text/html; charset=utf-8")
        if not self.authed():
            return self.send(401, {"error": "not signed in"})
        try:
            body = json.loads(self.body() or b"{}")
            if (m := re.fullmatch(r"/api/event/(\d+)/rate", url.path)):
                rating = int(body.get("rating", 0))
                if rating not in (-1, 0, 1):
                    raise ValueError("rating must be 1, -1 or 0")
                set_rating(self.app, int(m[1]), rating)
                return self.send(200, {"ok": True})
            if url.path == "/api/tuning":
                save_tuning(self.app, body)
                return self.send(200, tuning_stats(self.app))
            raise KeyError(url.path)
        except KeyError:
            return self.send(404, {"error": "not found"})
        except (ValueError, TypeError) as e:
            return self.send(400, {"error": str(e)})


def serve(cfg, analyzer, bind, port, token):
    Handler.app = App(cfg, token, analyzer)
    analyzer.open_db(cfg.db_path).close()  # create/upgrade tables before the first request
    srv = ThreadingHTTPServer((bind, port), Handler)
    note = "" if token else " (no token set: anyone who can reach it can use it)"
    print(f"review app on http://{bind}:{port}{note}", flush=True)
    if Handler.app.sampling is None:
        print(f"WARNING: widen and candidate previews unavailable: {Handler.app.sampling_error}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
