# bug-yourself

A tiny Android app that records the mic continuously and ships the audio to a PC over Tailscale.

The phone records into fixed-length lossless FLAC segments (48 kHz, 16-bit, mono) and uploads each one when it's finished. The PC encodes each segment to Ogg Vorbis at quality 10 (well above transparency, leaving headroom for sample editing) and keeps the folder under a size cap. If the PC is unreachable, segments pile up on the phone and upload later. Recording never stops.

**Seamless by design.** One `AudioRecord` stream runs for the whole recording and is never restarted. It gets split into segments at exact sample counts, so joining a recording's segments gives back the original stream with no gaps and no clicks. Each segment is named after the UTC time of its first sample, e.g. `20261117T201500Z.flac`. The time is computed from the recording's start plus the samples before it, so a clock adjustment mid-recording can't skew the names. Reading the mic and encoding/writing happen on separate threads, so slow storage can't cause dropouts. The FLAC encoder is a small built-in one (`FlacEncoder.kt`), so its output is the same on every phone. The app uses the `UNPROCESSED` mic source (no AGC or noise suppression) when the phone supports it.

## PC side

You need Python 3, plus either `vorbis-tools` (`oggenc`/`oggdec`) or `ffmpeg` built with libvorbis on your PATH.

```bash
python3 server/receiver.py serve --bind <your-pc-tailscale-ip> --token <pick-a-secret>
```

The default port is 8675. Chunks land in day folders, `recordings/20261117/20261117T201500Z.ogg`, named after their UTC start time. UTC avoids the repeated hour when daylight saving ends. Uploads using the old `<session>_<seq>` names (from older app builds, or still in the phone's outbox) are renamed to their start time on arrival.

- `--quality` sets the Vorbis quality, -1 to 10 (default 10, about 250 kbps on dense material). You can also set it with the `BUG_QUALITY` environment variable. It applies to chunks that arrive after you change it.
- `--max-size` caps the working folder (default `10GB`, `0` for no limit). When it's full, the receiver refuses uploads with 507 and the phone keeps its files and retries, so nothing is lost. `--when-full prune` deletes the oldest chunks instead. The working folder is meant to live on the SSD; the analyzer moves each night's audio to the HDD archive (see [docs/analyzer-plan.md](docs/analyzer-plan.md)).

- `--unanalyzed-dir` moves every finished chunk off the working folder to a second folder, e.g. `unanalyzed/` on the HDD. It runs daily at `--offload-at` (default `00:05`, local time) and once at startup, and verifies each copy before deleting the original. This keeps the SSD to about a day of audio even if the analyzer isn't running. The folder must already exist; if it's missing (drive not mounted), nothing moves and the problem is logged.

To stitch chunks into one file, give `join` folders or chunk files (from the working folder, `unanalyzed/` and the archive alike). It puts them in time order, and `--from`/`--to` pick out a stretch by chunk name:

```bash
python3 server/receiver.py join archive/20261117 --from 20261117T193000Z --to 20261117T203000Z out.wav
```

`join` decodes every chunk and joins them at the sample level. Wherever one chunk doesn't continue the previous one (you stopped and started recording), it warns that the output jumps there. The output has the exact original sample count, and quality at the joins matches the rest of the audio. Name the output `.ogg` instead of `.wav` to re-encode it, at the cost of one more round of lossy encoding.

Uploads (FLAC, or WAV from older app builds) carry a SHA-256 and are verified before encoding. The `.ogg` is written to a `.part` file first and renamed into place, and the phone only deletes its copy after that. Retries are idempotent.

## Phone side

Get the APK from the **Build APK** GitHub Actions run (artifact `bug-yourself-apk`) and sideload it. In the app:

- **Server URL**: `http://<pc-tailscale-name-or-ip>:8675`
- **Token**: same secret as the server
- **Segment minutes**: default 5 (≈29 MB per segment)

Tap **Disable battery optimization** once, then **Start**. A notification stays up while it records. **Stop** finishes the current segment, and the app exits after everything has uploaded.

Pending audio lives in `Android/data/com.bugyourself/files/outbox/` and survives app restarts. A segment cut short by a crash keeps every intact FLAC frame and is queued the next time you tap Start.

## Notes

- The APK is signed with `android/app/debug.keystore`, which is committed on purpose so every CI build installs over the last one. Uninstalling the app would delete any audio that hasn't uploaded yet.
- Android cuts off mic input during phone calls and when another app takes the mic.
- The phone uploads lossless FLAC (typically 50-70% of WAV's ~330 MB per hour, depending on how noisy the room is) and the PC does the Vorbis encoding, because Android has no built-in Vorbis encoder. On the PC, q10 works out to roughly 100 MB per hour, so the 10 GB default holds about 4 days of continuous audio.

## Nightly analyzer

`server/analyzer.py` runs every night at midnight. It archives the day's chunks from the SSD working folder and the HDD `unanalyzed/` folder into `archive/`, keeps the archive under 500 GB and the samples library under 100 GB (deleting the oldest first), and moves the previous night's clips from the SSD into the library. Detection, tagging and transcription plug into the same run later; see [docs/analyzer-plan.md](docs/analyzer-plan.md).

Each run also looks for sample-worthy moments: isolated hits, held notes, steady rhythms and textures. It cuts the best few per hour into 24-bit FLAC clips in `samples-new/<kind>/`, named by note, tempo or kind (e.g. `E4_20261117_00033.flac`). Every detected moment is kept in the database with its score, so you can change how many become clips without re-running detection.

```bash
python3 server/analyzer.py list
```

`list` shows proposed clips; `--all` includes everything detected, `--kind tonal` filters, and `--best` sorts by score. `cut <id> --pad 1` re-cuts a moment with a second of extra audio either side, and `reanalyze` runs detection over chunks that are already archived (for example everything recorded before detection existed). Clip counts and score cutoffs are set under `[detect]` in `analyzer.toml`.

### Review app

The review app runs at `http://<tailscale-ip>:8676` and works on the phone too; sign in with the same token as uploads. Its tabs:
- **Archive** and **Staged** step through the clips with a waveform and spectrogram. Archive holds clips in the HDD library; Staged holds the newest ones, still on the SSD until the next nightly run. **Widen** replays a clip with 3 s of the surrounding recording, so you can hear whether a better cut sits just outside it.
- **Kept** browses the folders you move keepers into (`kept_dirs` in `analyzer.toml`). A kept clip that still has its original name is linked back to where it came from.
- **Timeline** plays the recordings as one continuous stream in your local time. Tap or drag the day bar, or the zoomed bar showing the hour around the playhead, to start anywhere. Playback runs seamlessly across chunk boundaries and jumps over stretches with no recording. Buttons step ±10 s or ±1 min, or jump to the previous or next recording or clip (`P`/`N`). Detected moments are marked in each kind's colour: clips solid on both bars, other detected moments faint on the zoomed bar. Tap a marker to jump to it, and a caption names the moment under the playhead. Below the bars, the transcript of the surrounding hour follows playback, and tapping a line jumps there. Transcripts arrive with transcription (milestone E); the panel says so until then.
- **Tuning** shows, for each kind, a score histogram coloured by your ratings. A slider for the score cutoff and one for clips per hour re-rank the stored scores instantly. "Listen to this setting" plays exactly what that setting would cut, including moments that aren't clips yet. Saving writes `[detect]` in `analyzer.toml` for the next nightly run.

**Run analyzer** in the header starts the nightly run right away: it analyzes what's waiting on the SSD and in `unanalyzed/`, cuts clips, and moves staged clips to the archive. Tap its status line for the full output. It can't overlap the midnight run; if that one is going, it says so and stops.

Keys: `space` play, `←`/`→` step (on Timeline: ±10 s, with shift ±1 min), `W` widen, `G`/`B` rate good/bad, `0` clear the rating. Ratings never move or delete files. On a phone, the player stays at the top and only the list below it scrolls.

Housekeeping needs only Python 3.11+. Detection needs numpy, scipy and soundfile; the installer puts them in a private virtualenv. Without them, chunks wait in `unanalyzed/` instead of being archived undetected. Configuration lives in `~/.config/bug-yourself/analyzer.toml` (template: `server/analyzer.example.toml`). The HDD folder must contain an empty `.bug-yourself-archive` marker file, or the analyzer refuses to run, so an unmounted drive is never treated as empty.

```bash
python3 server/analyzer.py run --dry-run
```

`--dry-run` prints what would be archived, moved and deleted without changing anything. `status` shows chunk states, recent errors and folder sizes.

A chunk whose analysis fails is retried on the next two nights, then archived anyway and marked failed.

### Running both as services

`server/install.sh` sets up the receiver (always on) and the analyzer (midnight timer) as systemd user services. The first time, it asks a few questions:
- the Tailscale IP (detected automatically)
- the SSD and HDD folders
- the size caps
- the upload token (it can generate one)

Your answers go to `~/.config/bug-yourself/`, and the installer creates the HDD marker once you confirm the folder really is on the HDD. At the end it prints the URL and token to enter on the phone.

```bash
server/install.sh
```

To update after new code lands in the repo, run it with `--update`. That pulls, regenerates the service files, and restarts the receiver; your settings aren't touched.

```bash
server/install.sh --update
```

`--reconfigure` asks the questions again, with your current values as defaults. `--uninstall` removes the services and leaves settings and recordings in place. You can also edit `~/.config/bug-yourself/receiver.env` or `analyzer.toml` by hand and re-run the installer.

The installer also enables lingering, so the services run after boot without anyone logging in. If the host has no Vorbis encoder (`ffmpeg` with libvorbis, or `oggenc`), it offers to run the receiver inside a distrobox instead. Logs: `journalctl --user -u bug-receiver -u bug-analyzer`.
