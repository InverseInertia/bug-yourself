"""Milestone D: what a sound is, and finding sounds by describing them.

Two models, both loaded from Hugging Face on first use (about 1 GB, cached in
~/.cache/huggingface) and run on the GPU when there's room, else the CPU:

  tagging   MIT/ast-finetuned-audioset-10-10-0.4593 (Audio Spectrogram Transformer):
            the 527 AudioSet classes, e.g. "Glass", "Snare drum", "Bird"
  search    laion/clap-htsat-unfused (CLAP): audio and text in one embedding
            space, so "metallic ring" finds metallic rings

Needs torch and transformers (server/requirements-ai.txt; install.sh --with-ai).
Without them the analyzer still detects and cuts; tagging and embeddings are
added later with `analyzer.py tag`.

BUG_AI_FAKE=1 swaps in a tiny deterministic stand-in with the same interface,
used for testing the pipeline where the real models can't be downloaded.
"""
import os

import numpy as np
from scipy import signal

TAG_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
CLAP_MODEL = "laion/clap-htsat-unfused"
VERSION = 1
SR = 48000            # what the recordings and CLAP use; AST wants 16 kHz
TAG_SR = 16000
MAX_CLIP_S = 10.0     # both models look at up to ~10 s
MIN_CLIP_S = 1.0      # shorter moments are padded with silence to this
WINDOW_S = 10.0       # whole recordings are embedded in windows this long, for open search
BATCH = 8

# AudioSet labels that describe the setting rather than the sound. A more
# specific label wins when it's reasonably likely.
GENERIC = {"Music", "Speech", "Silence", "Inside, small room", "Inside, large room or hall",
           "Inside, public space", "Outside, urban or manmade", "Outside, rural or natural",
           "Sound effect", "Noise", "Environmental noise", "Static", "Mains hum", "Sound"}


def pick_tag(tags):
    """The label to name a clip by: the best specific label, else the best one."""
    specific = [(l, p) for l, p in tags if l not in GENERIC and p >= 0.1]
    return (specific or tags or [("untagged", 0.0)])[0][0]


def fit(x, sr=SR):
    """Trim to MAX_CLIP_S and pad to MIN_CLIP_S."""
    x = np.asarray(x, dtype=np.float32)[: int(MAX_CLIP_S * sr)]
    if len(x) < MIN_CLIP_S * sr:
        x = np.pad(x, (0, int(MIN_CLIP_S * sr) - len(x)))
    return x


class Models:
    """The real models. Construct with text_only=True to load just CLAP's text side
    (the review app does this for searches)."""

    def __init__(self, device="auto", text_only=False):
        import torch
        from transformers import ClapModel, ClapProcessor
        self.torch = torch
        self.device = self._pick_device(device)
        self.clap = ClapModel.from_pretrained(CLAP_MODEL).to(self.device).eval()
        self.clap_proc = ClapProcessor.from_pretrained(CLAP_MODEL)
        self.tagger = None
        if not text_only:
            from transformers import ASTFeatureExtractor, ASTForAudioClassification
            self.tag_fe = ASTFeatureExtractor.from_pretrained(TAG_MODEL)
            self.tagger = ASTForAudioClassification.from_pretrained(TAG_MODEL).to(self.device).eval()
            self.labels = self.tagger.config.id2label
        self.name = f"{TAG_MODEL}+{CLAP_MODEL}"

    def _pick_device(self, want):
        torch = self.torch
        if want != "auto":
            return want
        if not torch.cuda.is_available():
            return "cpu"
        free, _ = torch.cuda.mem_get_info()
        # Leave the GPU alone if something else (a game) is using most of it.
        return "cuda" if free > 2.5 * 2**30 else "cpu"

    @staticmethod
    def _features(out):
        # Some transformers versions return a tensor, others an output object.
        if hasattr(out, "pooler_output") and out.pooler_output is not None:
            out = out.pooler_output
        elif not hasattr(out, "norm") and hasattr(out, "__getitem__"):
            out = out[0]
        return out

    def _normed(self, t):
        t = self._features(t).float()
        return (t / t.norm(dim=-1, keepdim=True).clamp_min(1e-9)).cpu().numpy()

    def tag(self, clips):
        out = []
        with self.torch.no_grad():
            for i in range(0, len(clips), BATCH):
                batch = [signal.resample_poly(fit(c), 1, SR // TAG_SR).astype(np.float32) for c in clips[i:i + BATCH]]
                inputs = self.tag_fe(batch, sampling_rate=TAG_SR, return_tensors="pt").to(self.device)
                probs = self.torch.sigmoid(self.tagger(**inputs).logits).cpu().numpy()
                for p in probs:
                    top = np.argsort(-p)[:5]
                    out.append([(self.labels[int(k)], round(float(p[k]), 4)) for k in top])
        return out

    def _clap_audio_inputs(self, batch):
        try:
            return self.clap_proc(audio=batch, sampling_rate=SR, return_tensors="pt")
        except TypeError:  # older transformers call it `audios`
            return self.clap_proc(audios=batch, sampling_rate=SR, return_tensors="pt")

    def embed_audio(self, clips):
        vecs = []
        with self.torch.no_grad():
            for i in range(0, len(clips), BATCH):
                inputs = self._clap_audio_inputs([fit(c) for c in clips[i:i + BATCH]]).to(self.device)
                vecs.append(self._normed(self.clap.get_audio_features(**inputs)))
        return np.concatenate(vecs) if vecs else np.zeros((0, 512), np.float32)

    def embed_text(self, texts):
        with self.torch.no_grad():
            inputs = self.clap_proc(text=list(texts), return_tensors="pt", padding=True).to(self.device)
            return self._normed(self.clap.get_text_features(**inputs))


class FakeModels:
    """Deterministic stand-in with the same interface, for tests (BUG_AI_FAKE=1).
    Tags and embeddings come from simple spectral measurements, and text maps a
    few words onto the same measurements, so search behaves plausibly."""

    BANDS = np.geomspace(40, 20000, 17)
    WORDS = {"low": 0, "bass": 0, "hum": 1, "boom": 0, "kick": 1, "drum": 2, "thud": 1, "knock": 3,
             "snare": 8, "click": 12, "hiss": 13, "rain": 11, "noise": 10, "metal": 12, "ring": 9,
             "tone": 4, "note": 4, "whistle": 9, "bright": 14}

    def __init__(self, device="cpu", text_only=False):
        self.device, self.name = "cpu", "fake"
        self.labels = None

    def _spectrum(self, x):
        x = fit(x)
        f, p = signal.welch(x, SR, nperseg=4096)
        e = np.array([p[(f >= lo) & (f < hi)].sum() for lo, hi in zip(self.BANDS[:-1], self.BANDS[1:])])
        return np.log10(e + 1e-12)

    def tag(self, clips):
        out = []
        for c in clips:
            x = fit(c)
            crest = float(np.max(np.abs(x)) / (np.sqrt(np.mean(x ** 2)) + 1e-9))
            spec = self._spectrum(c)
            flat = float(np.std(spec))
            low = float(np.argmax(spec)) < 5
            if crest > 8:
                tags = [("Drum", 0.8), ("Bass drum" if low else "Snare drum", 0.6)]
            elif flat > 1.5:
                tags = [("Sine wave", 0.7), ("Hum", 0.5)]
            else:
                tags = [("White noise", 0.6), ("Rain", 0.4)]
            out.append(tags + [("Music", 0.3)])
        return out

    def embed_audio(self, clips):
        v = np.array([self._spectrum(c) for c in clips], dtype=np.float32)
        v = v - v.mean(axis=1, keepdims=True)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)

    def embed_text(self, texts):
        out = []
        for t in texts:
            v = np.full(16, -1.0, np.float32)
            for w in t.lower().split():
                if w.rstrip("s") in self.WORDS:
                    v[self.WORDS[w.rstrip("s")]] = 3.0
            v -= v.mean()
            out.append(v / max(np.linalg.norm(v), 1e-9))
        return np.array(out, dtype=np.float32)


def load(device="auto", text_only=False):
    """(models, None), or (None, why not)."""
    if os.environ.get("BUG_AI_FAKE"):
        return FakeModels(device, text_only), None
    try:
        return Models(device, text_only), None
    except ImportError as e:
        return None, f"{e.name or e} not installed (server/install.sh --with-ai)"
    except Exception as e:  # download failures, CUDA trouble: carry on without tags
        return None, f"{type(e).__name__}: {e}"


# --- storage and search (numpy + sqlite only) ----------------------------------------

def embed_name(models):
    return "fake" if isinstance(models, FakeModels) else CLAP_MODEL


def store_vector(db, vec, model, run, chunk, a, b, sr, at, ref_event=None):
    db.execute("INSERT INTO embeddings(ref_event, run, chunk, start_sample, end_sample, sr, at_time, dur, vector, model) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (ref_event, run, chunk, int(a), int(b), int(sr), float(at), (b - a) / sr,
                np.asarray(vec, dtype=np.float16).tobytes(), model))


class VectorIndex:
    """All stored vectors of one model as a matrix, reloaded when rows change."""

    def __init__(self):
        self.key, self.ids, self.mat = None, None, None

    def get(self, db, model):
        key = db.execute("SELECT count(*), max(id) FROM embeddings WHERE model = ?", (model,)).fetchone()
        if key != self.key:
            rows = db.execute("SELECT id, vector FROM embeddings WHERE model = ?", (model,)).fetchall()
            self.ids = np.array([r[0] for r in rows], dtype=np.int64)
            self.mat = (np.stack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(np.float32)
                        if rows else np.zeros((0, 1), np.float32))
            self.key = key
        return self.ids, self.mat


def search(db, models, text, index=None, limit=50):
    """Detected moments and stretches of recording that sound like `text`, best first."""
    index = index or VectorIndex()
    ids, mat = index.get(db, embed_name(models))
    if not len(ids):
        return []
    q = models.embed_text([text])[0]
    if mat.shape[1] != len(q):
        return []
    sims = mat @ q
    order = np.argsort(-sims)[: limit * 2]
    hits = {int(ids[i]): float(sims[i]) for i in order}
    rows = db.execute(
        f"SELECT m.id, m.ref_event, m.run, m.at_time, m.dur, e.kind, e.label, e.tag, e.score, c.id, c.state "
        f"FROM embeddings m LEFT JOIN events e ON e.id = m.ref_event LEFT JOIN clips c ON c.event_id = e.id "
        f"WHERE m.id IN ({','.join('?' * len(hits))})", list(hits)).fetchall()
    out = []
    for mid, ev, run, at, dur, kind, label, tag, score, clip_id, cstate in rows:
        item = {"match": round(hits[mid], 4), "at": at, "dur": dur, "run": run}
        if ev is not None and kind is not None:
            item.update({"type": "event", "id": ev, "kind": kind, "label": label, "tag": tag, "score": score,
                         "clip_id": clip_id if cstate in ("new", "library") else None})
        else:
            item.update({"type": "window"})
        out.append(item)
    out.sort(key=lambda r: -r["match"])
    return out[:limit]
