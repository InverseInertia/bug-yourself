# Analyzer plan

Status: plan, not built yet. Target machine: Bazzite (NVIDIA image), RTX 3080, SSD + HDD.

The analyzer runs once a night. It finds points of interest for sampling in the day's recordings, writes them to a searchable index, cuts clips into a samples library, and moves the day's audio from the SSD working folder to the HDD archive.

## Storage layout

| Folder | Drive | Owner | Size policy |
|---|---|---|---|
| `recordings/` (working) | SSD | receiver writes, analyzer empties | 10 GB; receiver refuses uploads when full (phone holds them) |
| `archive/<session>/` | HDD | analyzer | 500 GB; oldest chunks deleted first |
| `samples/` | HDD | analyzer | never pruned automatically |
| `analyzer.db` (SQLite) | HDD, next to archive | analyzer | small (MBs per day) |
| model cache, venv | SSD | analyzer | ~5 GB once |

The SSD folder normally holds less than a day (~2.4 GB at ~100 MB/hour). The 10 GB cap only matters if the analyzer stops running. Then the receiver refuses uploads with 507 and the phone keeps buffering, so a broken analyzer costs nothing until the phone fills up. The receiver no longer deletes anything by default; deletion only happens in the archive.

The HDD root holds a marker file (`.bug-yourself-archive`). The analyzer refuses to run if it's missing. That stops it from "archiving" onto the SSD when the HDD isn't mounted.

## Nightly run

A systemd **user** timer starts it at 00:00 with `Persistent=true`, so a run missed because the PC was off happens at the next boot. A lock file prevents two runs from overlapping. It can also be run by hand, which is useful while tuning.

Each run handles every finished chunk in the working folder (`*.ogg`, never `*.part.*`). It doesn't treat chunks of a session that's still recording any differently. Tonight takes whatever exists, and tomorrow picks up the rest, using the archived chunk from tonight as context (see "Chunk boundaries").

The GPU is shared with gaming. Midnight usually avoids conflict, but the run checks `nvidia-smi` first. If another process is holding more than a few GB of VRAM, it waits and re-checks rather than competing. CPU-only stages can proceed meanwhile.

## Pipeline

Each chunk is decoded once (Ogg → 48 kHz mono float) and then passes through these stages:

- **Detect (CPU, librosa).** Harmonic/percussive separation, onset strength, pitch tracking (pYIN) and spectral flatness produce candidate windows of four kinds:
  - **tonal:** a stable pitch with high confidence for at least 0.5 s, low flatness
  - **percussive:** a strong onset, fast decay, quiet before and after (clean one-shots)
  - **loop:** a stretch where beat tracking is consistent for several bars
  - **texture:** sustained, broadband, but with stable spectral character (drones, rain, crowd)

  Each candidate gets a 0–1 score. Rather than fixed thresholds, the top N per hour by score are kept, because phone-mic audio is mostly background noise.
- **Voice detection (CPU, Silero VAD).** Speech regions feed transcription and are excluded from the tonal/texture candidates.
- **Tag (GPU, PANNs or BEATs).** Candidate windows get AudioSet labels with confidences. The top label becomes part of the clip name.
- **Embed (GPU, CLAP).** Every candidate gets a 512-dim embedding, plus one embedding per 10 s window of the whole day for open-ended search. Stored as blobs and searched by brute force in numpy; 100k vectors take milliseconds.
- **Transcribe (GPU, faster-whisper large-v3-turbo, 8-bit).** Runs on the speech regions, with word timestamps. Segments whose confidence suggests invented text (high no-speech probability, very low average log-probability) are dropped.
- **Clip.** Each kept candidate is cut with 50 ms of padding and short fades, and written to `samples/<kind>/<label>_<yyyymmdd>_<id>.flac`. FLAC avoids a second round of lossy encoding. The label comes from the tagger, e.g. `glass_20261117_a3f9.flac`; `untagged_…` is used when the tagger isn't confident.

**Chunk boundaries.** Each chunk is analyzed with the last 10 s of the previous chunk prepended, read from the working folder or the archive. Events that start inside that context are owned by the previous chunk, so nothing is counted twice. Speech that runs across a boundary gets transcribed whole.

## Parallelism

Sessions are independent, so they're the unit of parallelism. Chunks within one session run in order, because each one needs its predecessor's tail as context.

```
session workers (CPU processes, one session each)          GPU worker (one process)
  decode → detect → VAD → candidate windows  ──queue──▶  tag + CLAP + Whisper, batched
                                              ◀──queue──  results
                      │
                      ▼
              DB writer (single thread, SQLite WAL) → clip cutter → migration
```

- **Session workers:** a process pool of `cores − 2` workers. Each takes one session and walks its chunks in order. Decoding and the librosa features are the CPU-heavy part and parallelize cleanly across sessions.
- **One GPU worker:** it loads the models once, using about 5 GB of VRAM for turbo int8, the tagger and CLAP together. It pulls work from a shared queue and batches across sessions. Extra GPU processes would just load duplicate model copies; batching gives the speedup.
- **One DB writer:** SQLite allows one writer. All results go through a single thread, and readers (search) can work during a run thanks to WAL mode.
- **Backpressure:** the queue between CPU and GPU is bounded, so fast CPU workers can't pile up memory while Whisper works through a talkative day.
- **Migration** runs per session as soon as all of that session's chunks are committed. It doesn't wait for the whole run, so the SSD frees up progressively.

Rough cost for a full day of recording on the 3080: CPU detection about 5–10 minutes spread over the pool, GPU about 10–20 minutes depending on how much talking there was. The phone typically produces one session per Start press, so most nights have only a few sessions. Parallelism matters most when catching up after missed nights.

## Migration and archive

For each analyzed chunk, the analyzer:
- copies it to `archive/<session>/<name>.part.ogg` on the HDD and fsyncs it
- verifies the copy's size and SHA-256 against the source
- renames it into place, then deletes the SSD copy

If the destination already exists with the same hash, the SSD copy is just deleted, which covers a crash mid-migration. Chunk state lives in the DB (`analyzed`, `migrated`), so a crash at any point resumes cleanly.

If a chunk fails analysis three nights running, it's migrated anyway and marked `analysis_failed`. This keeps one bad file from clogging the SSD. `analyzer.py reanalyze` can retry it from the archive later.

After migration, the archive is pruned to 500 GB (the receiver's `prune()` logic, reused). Pruning touches only archive chunks; `samples/` and the DB are never deleted. DB rows for pruned audio stay, marked `audio_pruned`, so transcripts and tags remain searchable even after the source audio is gone, and the clips in `samples/` survive.

`receiver.py join` already accepts several folders, so a session split between the working folder and the archive can still be joined.

## Data model (SQLite)

- `chunks(session, seq, path, sha256, state, analyzed_at, migrated_at)`
- `events(id, session, start_sample, end_sample, kind, score, label, label_conf, tags_json, clip_path)`
- `embeddings(event_id or window_id, vector BLOB)`
- `transcript(id, session, start_sample, end_sample, text, avg_logprob)` plus an FTS5 virtual table over `text`, with word timings in a side table

Positions are stored as sample offsets within the session (`seq × segment length + offset`). Clips and transcripts can therefore always be traced back to the exact spot, and any window can be re-cut with different padding.

## Interface

A CLI first:
- `analyzer.py run` (what the timer calls; `--dry-run`, `--session X`)
- `search "metallic ring"` (CLAP text search over events and day windows)
- `find-text "phrase"` (transcript search)
- `list --kind percussive --since 7d`
- `cut <event-id> --pad 200ms`
- `reanalyze`

Results print with clip paths, so they open straight in a sampler or DAW. A small local web page (waveforms, play buttons, search box) can come later, built on the same DB.

## Environment (Bazzite)

One distrobox (Fedora or Ubuntu) on the NVIDIA image. It holds a Python venv with CUDA PyTorch, faster-whisper, librosa, silero-vad, a CLAP package and a PANNs/BEATs package. Model weights download once into the SSD cache. The receiver and analyzer run as systemd user services from the same box. The analyzer unit declares `RequiresMountsFor=` the HDD path, so it never starts without the drive.

## Milestones

Each milestone is useful on its own:

| Milestone | Delivers | Needs GPU |
|---|---|---|
| A | nightly timer, migration to HDD, archive pruning, chunk state DB | no |
| B | tonal / percussive / loop / texture detection, clip cutting, `list` | no |
| C | AudioSet tags in clip names, CLAP `search` | yes |
| D | VAD + Whisper transcripts, `find-text` | yes |
| E | web browser for results | no |

Milestone A is needed before anything else, because without it the SSD working folder fills in about four days and uploads stop.

## Open questions

- **Samples cap:** should `samples/` have a size cap or a "keep top N per week" rule, or grow forever? At a few MB per clip and maybe a hundred clips a day, it grows by roughly 10–20 GB a year.
- **Speaker labels** (pyannote): add later if transcripts are used a lot. They need a Hugging Face token and about 1 GB more VRAM.
- **Tuning:** scoring needs listening sessions on real recordings. Plan a feedback command (`keep`/`reject` on clips) and use those labels to tune thresholds.
