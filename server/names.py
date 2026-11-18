"""Chunk file names. Standard library only; shared by the receiver and the analyzer.

Every chunk is named after the UTC time its first sample was recorded:

    20261117T201500Z.ogg     stored under a day folder: <root>/20261117/

UTC because local time repeats an hour when daylight saving ends. Chunks are at
least 6 s long, so one name per second can't collide within a recording, and
two recordings starting in the same second is vanishingly unlikely.

Older builds named chunks <session>_<seq> (session = UTC start of the whole
recording). Those are still recognised so they can be converted on arrival
and renamed on disk (see legacy_start_ms).
"""
import calendar
import re
import time

NAME_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)\.(?P<ext>wav|flac|ogg)$")
LEGACY_RE = re.compile(r"^(?P<session>\d{8}-\d{6})_(?P<seq>\d+)\.(?P<ext>wav|flac|ogg)$")
STAMP_FMT = "%Y%m%dT%H%M%SZ"

# Two chunks are continuous when the earlier one's start plus its length lands
# within this many seconds of the later one's start. Names are truncated to the
# second, so the true gap between continuous chunks is always under 1 s.
CONTINUITY_TOLERANCE_S = 1.05


def stamp_epoch(stamp):
    return calendar.timegm(time.strptime(stamp, STAMP_FMT))


def epoch_stamp(epoch):
    return time.strftime(STAMP_FMT, time.gmtime(int(epoch)))


def chunk_epoch(name):
    """Start time (UTC epoch seconds, truncated) of a new-style chunk name, else None."""
    m = NAME_RE.match(name)
    return stamp_epoch(m["stamp"]) if m else None


def day_folder(name):
    """Folder a chunk lives in: its UTC day, e.g. 20261117."""
    return name[:8]


def is_chunk(name):
    return bool(NAME_RE.match(name))


def is_legacy(name):
    return bool(LEGACY_RE.match(name))


def continuous(prev_epoch, prev_seconds, epoch):
    return abs(prev_epoch + prev_seconds - epoch) < CONTINUITY_TOLERANCE_S


def legacy_session_epoch(session):
    return calendar.timegm(time.strptime(session, "%Y%m%d-%H%M%S"))


def ogg_info(path):
    """(frames, sample_rate) of an Ogg Vorbis file, read from its headers and last page."""
    with open(path, "rb") as f:
        head = f.read(4096)
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 65536))
        tail = f.read()
    i = head.find(b"\x01vorbis")
    if i < 0:
        raise ValueError(f"{path} is not Ogg Vorbis")
    rate = int.from_bytes(head[i + 12:i + 16], "little")
    j = tail.rfind(b"OggS")
    if j < 0:
        raise ValueError(f"{path} has no Ogg pages")
    frames = int.from_bytes(tail[j + 6:j + 14], "little")  # granule of the last page
    return frames, rate
