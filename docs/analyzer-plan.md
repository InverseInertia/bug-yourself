# Analyzer plan

Status: milestones A–D built (`server/analyzer.py`, `detect.py`, `sampling.py`, `web.py`, `ai.py`); E (transcription) planned, with storage and display already in place. Target machine: Bazzite (NVIDIA image), RTX 3080, SSD + HDD.

The analyzer runs once a night. It finds points of interest for sampling in recordings that haven't been analyzed yet, writes them to a searchable index, cuts clips into a samples library, and moves the audio into the HDD archive.

## Storage layout

| Folder | Drive | Owner | Size policy |
|---|---|---|---|
| `recordings/` (working) | SSD | receiver writes; receiver moves out at 00:05 | ~1 day; 10 GB hard cap, receiver refuses uploads when full (phone holds them) |
| `unanalyzed/<YYYYMMDD>/` | HDD | receiver moves chunks in at 00:05; analyzer archives them out | grows only while the analyzer is down (~2.4 GB/day) |
| `archive/<YYYYMMDD>/` | HDD | analyzer | 500 GB; oldest chunks deleted first |
| `samples-new/` | SSD | analyzer writes tonight's clips | about one night of clips; moved to the HDD at the start of the next run |
| `samples/` | HDD | analyzer | 100 GB; oldest clips deleted first |
| `analyzer.db` (SQLite) | HDD, next to archive | analyzer | small (MBs per day) |
| model cache, venv | SSD | analyzer | ~5 GB once |

Unanalyzed audio can be in two places: chunks that arrived since 00:05 are still on the SSD, and everything older is in `unanalyzed/` on the HDD. The analyzer checks both.

Every night at 00:05, and once at startup to catch up after downtime, the receiver moves every finished chunk from the SSD to `unanalyzed/`. Each copy is verified by SHA-256 before the SSD copy is deleted. The SSD therefore holds about a day of audio no matter whether the analyzer works; a broken analyzer just lets `unanalyzed/` grow on the HDD. The receiver only moves into `unanalyzed/` if that folder already exists. If the HDD isn't mounted, nothing moves; the SSD then fills, and at 10 GB (about 4 days) the receiver refuses uploads with 507, the phone keeps buffering, and nothing is lost.

The HDD root holds a marker file (`.bug-yourself-archive`). The analyzer refuses to run if it's missing. That stops it from "archiving" onto the SSD when the HDD isn't mounted.

## Nightly run

A systemd **user** timer starts it at 00:00 with `Persistent=true`, so a run missed because the PC was off happens at the next boot. A lock file prevents two runs from overlapping. It can also be run by hand, which is useful while tuning.

Each run handles every finished chunk in either location: `unanalyzed/` on the HDD and the SSD working folder (`*.ogg`, never `*.part.*`), skipping chunks the DB already marks done. A chunk is identified by its name, wherever it currently sits. It doesn't treat chunks of a session that's still recording any differently. Tonight takes whatever exists, and tomorrow picks up the rest, using the archived chunk from tonight as context (see "Chunk boundaries").

The GPU is shared with gaming. Midnight usually avoids conflict, but the run checks `nvidia-smi` first. If another process is holding more than a few GB of VRAM, it waits and re-checks rather than competing. CPU-only stages can proceed meanwhile.

## Pipeline

Each chunk is decoded once (Ogg → 48 kHz mono float) and then passes through these stages:

- **Detect (CPU, numpy/scipy; built in milestone B as `server/detect.py`).** Harmonic/percussive separation (median filtering on 96 log-spaced bands), spectral-flux onsets, spectral flatness and YIN pitch tracking produce candidates of four kinds. librosa was avoided: it depends on numba, which lags new Python releases, and Bazzite's host Python is recent.
  - **tonal:** a stable pitch with high confidence for at least 0.5 s, low flatness
  - **percussive:** a strong onset, fast decay, quiet before and after (clean one-shots)
  - **loop:** a stretch where beat tracking is consistent for several bars
  - **texture:** sustained, broadband, but with stable spectral character (drones, rain, crowd)

  Each candidate gets a 0–1 score built from the features stored with it. All candidates go into the DB; only the best per hour and kind become clips (`[detect]` in analyzer.toml: `quota_per_hour`, `min_score`, defaulting to 6 percussive, 4 tonal, 2 loop and 2 texture per hour). Ranking rather than fixed thresholds, because phone-mic audio is mostly background noise.

  What milestone B detection doesn't do yet:
  - **Speech isn't excluded.** Voices will show up as tonal and percussive candidates until voice detection arrives with transcription (milestone E). The isolation and pitch-stability terms keep most speech below the clip cutoffs.
  - **Steady sounds lasting a whole chunk aren't tonal candidates.** Tonal detection measures against the chunk's own floor, so a fridge hum lasting the whole chunk isn't found. Steady broadband sound is caught as texture, because texture is measured against an absolute quiet reference (−60 dBFS).
  - **The last 10 s of a recording's final chunk are never owned**, because the next chunk normally owns them. Those 10 s are lost to detection if the recording ends there.
- **Voice detection (CPU, Silero VAD).** Speech regions feed transcription and are excluded from the tonal/texture candidates.
- **Tag (GPU, PANNs or BEATs).** Candidate windows get AudioSet labels with confidences. The top label becomes part of the clip name.
- **Embed (GPU, CLAP).** Every candidate gets a 512-dim embedding, plus one embedding per 10 s window of the whole day for open-ended search. Stored as blobs and searched by brute force in numpy; 100k vectors take milliseconds.
- **Transcribe (GPU, faster-whisper large-v3-turbo, 8-bit).** Runs on the speech regions, with word timestamps. Segments whose confidence suggests invented text (high no-speech probability, very low average log-probability) are dropped.
- **Clip.** Each kept candidate is cut sample-exactly (start already includes a few ms of pre-roll), with a 3 ms fade-in and 15 ms fade-out, and written as 24-bit FLAC to `samples-new/<kind>/<label>_<yyyymmdd>_<event id>.flac` on the SSD. FLAC avoids a second round of lossy encoding. A clip whose decoded source overshoots full scale is turned down just enough to fit instead of clipping. Until tagging (milestone D) the label is the note for tonal clips (`E4`), the tempo for loops (`120bpm`), and `hit` or `texture` otherwise.

**Chunk boundaries.** Each chunk is analyzed with the last 15 s of the previous chunk prepended, read from wherever that chunk now is (SSD, `unanalyzed/` or archive). An event belongs to the chunk it starts in, except that events starting in a chunk's last 10 s belong to the next chunk, which sees them whole thanks to its context. Every event is therefore found once and never truncated. Chunks are decoded in full rather than seeked into, because libsndfile's Ogg seeking lands ~192 samples late within a file's last half second. Speech that runs across a boundary gets transcribed whole.

## Processing order

There's no speed requirement, so the analyzer is one sequential process. It works through chunks oldest first, one at a time: each one goes through every stage, then gets archived. Each chunk needs its predecessor's tail as context, and taking them in order keeps that simple. Models load once at the start of a run and stay loaded for the whole night, so per-chunk overhead is just the inference itself.

Rough cost for a full day of recording on the 3080: detection about 15–30 minutes on one CPU core, GPU stages about 10–20 minutes depending on how much talking there was. That's well inside one night. A backlog after missed nights just takes proportionally longer, and the run lock stops the next night's timer from starting a second run on top.

## Migration and archive

For each analyzed chunk, from whichever folder it's in, the analyzer:
- copies it to `archive/<YYYYMMDD>/<name>.part.ogg` and fsyncs it (from `unanalyzed/` this is a same-drive rename, so it's instant)
- verifies the copy's size and SHA-256 against the source
- renames it into place, then deletes the source copy

If the destination already exists with the same hash, the source copy is just deleted, which covers a crash mid-migration.

The analyzer (00:00) and the receiver's move (00:05) can overlap on a long night. Both take an exclusive lock on `recordings/.move.lock` (already implemented in the receiver) while moving files out of the SSD folder, so they never move the same file at once. The analyzer holds it only while moving SSD chunks, not while analyzing. A chunk can also move from the SSD to `unanalyzed/` while the analyzer is reading it. An open file stays readable on Linux, and at archive time the analyzer looks the chunk up again by name in both folders.

If a chunk fails analysis three nights running, it's archived anyway and marked `analysis_failed`, so one bad file never sits in `unanalyzed/` forever. `analyzer.py reanalyze` can retry it from the archive later.

After migration, the archive is pruned to 500 GB (the receiver's `prune()` logic, reused). Pruning touches only archive chunks, never clips or the DB. DB rows for pruned audio stay, marked `audio_pruned`, so transcripts and tags remain searchable even after the source audio is gone.

## Samples library

Clips follow the same SSD-then-HDD pattern as the recordings:
- **New clips** from tonight's run land in `samples-new/` on the SSD, so the most recent finds open quickly the next day.
- **Moving to the HDD** happens at the start of the next run: everything in `samples-new/` moves to `samples/<kind>/`, with the same verify-then-delete as the chunk moves. A clip still being reviewed is just read from its new location; the DB tracks each clip's current path.
- **The 100 GB cap** is enforced after that move, deleting the oldest clips first until the folder fits.

"Oldest" means when the clip was cut, not when the audio was recorded. Otherwise a clip you deliberately re-cut today from months-old audio (`cut`, `reanalyze`) would be the first thing deleted. In nightly operation the two orders are nearly identical anyway. The cut time comes from the DB, not file timestamps, which copying can change.

When a clip is deleted, its DB row stays with `clip_path` cleared and `clip_pruned` set. The tags, embedding and transcript remain searchable. If the source audio is still in the archive, `cut <event-id>` makes the clip again.

At roughly 10–20 GB of clips a year, the 100 GB cap takes years to reach. The archive's 500 GB (about 7 months of recordings) runs out much sooner, so in practice clips outlive their source audio. That's why clips are always cut at full length with padding, never just referenced.

The library is a holding area. Clips worth keeping are moved out by hand, to a folder outside `samples/` that the analyzer never touches, so there's no keep flag or protection inside the library. Anything left in `samples/`, including subfolders you create there, counts toward the 100 GB and can be deleted.

Clips that vanish because you moved or deleted them are normal, not errors. At the start of each run the analyzer checks the clip paths it knows about, and marks missing ones `clip_removed` (distinct from `clip_pruned`, which it set itself). Their index entries stay searchable like pruned ones. Cleanup only counts and deletes files that are actually there.

`receiver.py join` accepts any mix of folders and chunk files, so a recording spread over the SSD, `unanalyzed/` and the archive can still be joined.

## Data model (SQLite)

- `chunks(name, session, seq, start_sample, frames, state, location, ...)`
- `events(id, session, chunk, start_sample, end_sample, kind, score, label, features, status, ...)`
- `embeddings(event_id or window_id, vector BLOB)`
- `transcript(id, session, start_sample, end_sample, text, avg_logprob)` plus an FTS5 virtual table over `text`, with word timings in a side table

**Chunk names and runs.** Chunks are named after the UTC time of their first sample (`20261117T201500Z.ogg`) and stored in UTC day folders. Nothing in a name says which recording a chunk belongs to. Instead, the analyzer links a chunk to the one before it (by time) when that one's start plus its length lands within a second of this chunk's start. Names are truncated to the second, so continuous chunks always land within that. Linked chunks form a *run*, which is what the `session` column holds; it's named after the run's first chunk. Positions are sample offsets within the run, so clips and transcripts can always be traced back to the exact spot, and any window can be re-cut with different padding.

Older builds named chunks `<session>_<seq>`. Each run renames any such files on disk, and their DB rows, to their start time: chunk 0 starts at the session time, and each later chunk where the previous one ended. A table in the working folder (`.legacy-names.json`) records each conversion, so an old-style chunk that arrives later from the phone's outbox continues exactly where its predecessor left off. The receiver uses the same table when it renames old-style uploads. Clips and transcripts can therefore always be traced back to the exact spot, and any window can be re-cut with different padding.

## Interface

A CLI first:
- `analyzer.py run` (what the timer calls; `--dry-run`)
- `search "metallic ring"` (CLAP text search over events and day windows)
- `find-text "phrase"` (transcript search)
- `list --kind percussive --since 7d`
- `cut <event-id> --pad 200ms`
- `reanalyze [--prefix 20261117]`

Results print with clip paths, so they open straight in a sampler or DAW.

## Review web app

This is the main way to look at results and the tool for tuning the detector. `analyzer.py web` serves it from the PC, like the receiver: bound to the Tailscale IP, protected by the same token, so it works from the phone too. Its pages:

- **Proposed:** new clips in `samples-new/` (SSD) and the library in `samples/` (HDD). Filters for kind, tag, date and score, sorted by score or time.
- **Kept:** the folder(s) you move keepers into, set by a `--kept-dir` option. It browses whatever audio is there, including long clips you've cut yourself. A kept file still carrying its original name is linked back to its index entry, so its source, time, tags and transcript show alongside it. Files from anywhere else just play.
- **Search:** CLAP text search ("metallic ring") and transcript search, with results shown in the same click-through player.

**Player.** Opening any list gives a click-through view: one clip at a time with a waveform and spectrogram drawn in the browser, plus next/previous. Keyboard shortcuts let you get through a night's proposals in minutes: space plays, arrow keys step, `w` widens. Widen replays the clip with a few seconds of surrounding audio from the source recording (SSD, `unanalyzed/` or archive, wherever it is), so you can hear whether a better cut sits just outside the proposal. If the source has been pruned, widen is greyed out.

**Rating for tuning.** Each proposed clip can be marked good or bad with one key. Ratings go into the DB, never move or delete files, and are separate from keeping: you still move keepers out by hand. A **Tuning** page uses the ratings:
- shows, for each kind (tonal, percussive, loop, texture), how often top-scored proposals were rated good
- has a slider for each kind's score cutoff and per-hour quota, which re-ranks the existing proposals live (scores are already in the DB, so this is just a query). You can hear what a stricter or looser setting would have proposed before saving it.
- writes saved settings to the analyzer config; the next nightly run uses them.

Feature weights inside each score (how much onset strength vs. decay vs. isolation counts) are tuned the same way. Once a few hundred ratings exist, a small fit (logistic regression over the stored features) can suggest weights, which you then audition on the Tuning page before accepting.

**As built (milestone C).** `analyzer.py web` (service `bug-web`, port 8676), with the token entered once and kept in a cookie. Ratings live in a `ratings` table. The Search page waits for milestones D (CLAP) and E (transcripts), and so do their CLI commands. Suggested feature weights are left until there are a few hundred real ratings to fit them to. The Tuning page already shows, per kind, how ratings fall across the score range, which is what a weight fit would use.

**Build.** A single-page app with plain HTML and JavaScript, served by a small Python web server inside the analyzer, reading the same SQLite DB. Audio and waveforms are decoded in the browser from the clip files; there's no server-side rendering and no extra service to run.

## Environment (Bazzite)

One distrobox (Fedora or Ubuntu) on the NVIDIA image. It holds a Python venv with CUDA PyTorch, faster-whisper, librosa, silero-vad, a CLAP package and a PANNs/BEATs package. Model weights download once into the SSD cache. The receiver and analyzer run as systemd user services from the same box. The analyzer unit declares `RequiresMountsFor=` the HDD path, so it never starts without the drive.

## Milestones

Each milestone is useful on its own:

| Milestone | Delivers | Needs GPU |
|---|---|---|
| A (built) | nightly timer, migration to HDD, archive and samples pruning, chunk state DB | no |
| B (built) | tonal / percussive / loop / texture detection, clip cutting, `list`, `cut`, `reanalyze` | no |
| C (built) | review web app: proposed/kept pages, click-through player, widen, ratings, Tuning page | no |
| D (built) | AudioSet tags in clip names, CLAP search (CLI and web) | yes |
| E | VAD + Whisper transcripts, transcript search (CLI and web) | yes |

Milestone A comes first because, without it, `unanalyzed/` on the HDD grows by about 2.4 GB a day with nothing archiving or pruning it. The web app comes right after detection, because detection can only be tuned by listening to what it proposes.

## Tags and sound search (milestone D, as built)

`server/ai.py` loads two Hugging Face models on first use: `MIT/ast-finetuned-audioset-10-10-0.4593` (AudioSet tags) and `laion/clap-htsat-unfused` (CLAP). They run on the GPU when it has 2.5 GB free, else on the CPU (`[ai] device`). The tagging step runs per chunk right after detection, so the night's clips are named by their tag:
- The best specific AudioSet label is used; generic ones like "Music", "Speech" and "Inside, small room" are passed over.
- Tonal clips keep their note and loops their tempo, e.g. `glass-E4`, `drum-120bpm`.
- Clips already cut keep their file names.

Every detected moment gets a CLAP vector, and so does every audible 10 s window of the recordings. Windows below −55 dBFS, an empty room, are skipped; this keeps the database from growing about 1 GB a month with nothing in it. Window vectors are deleted along with their audio when the archive is pruned.

Search embeds the description with CLAP's text side and ranks every stored vector by cosine similarity, brute force in numpy. It's available as `analyzer.py search`, as `/api/search/sound`, and on the review app's Search tab.

Tagging never fails a chunk: if the models can't load, chunks are archived untagged, and `analyzer.py tag` adds tags and vectors later (`--all` re-tags everything). `reanalyze` tags as it goes.

Tested with a stand-in (`BUG_AI_FAKE=1`) through the whole pipeline: tags, labels, vectors, search, reanalysis and backfill, the CLI and the web UI. The real models couldn't be downloaded in the development environment, so their first real run is on the PC. The model code handles both the newer and older transformers call styles for CLAP.

## Transcripts: what's already in place

Milestone E only has to produce text. Storage, search and display already exist (`server/transcripts.py`):
- **Storage:** a `transcript_segments` table holds the run, the chunk, sample positions, wall-clock start and end, text, an optional speaker, a confidence value, per-word timings as JSON, and the model name. An FTS5 full-text index is kept in step by triggers; if SQLite lacks FTS5, search falls back to a substring match.
- **Writing:** `transcripts.store_chunk(db, chunk, run, chunk_start_sample, sr, run_epoch, segments, model)` is the one call the transcription stage makes per chunk. Segment times are in seconds from the chunk's first sample, and calling it again for the same chunk replaces that chunk's lines. The stage should use the same context and ownership rule as detection (15 s of the previous chunk, lines starting in a chunk's last 10 s belong to the next), so a sentence across a boundary is stored once.
- **Reading:** `analyzer.py find-text`, the review app's timeline panel (it follows playback, and tapping a line jumps there), the "said" lines under a clip in the player, and `GET /api/search/text`. That search API is ready for a Search page.



- **Speaker labels** (pyannote): not wanted for now. Transcripts are stored with sample positions, so labels can be added later without re-transcribing.
