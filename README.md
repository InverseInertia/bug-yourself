# bug-yourself

A tiny Android app that records the mic continuously and ships the audio to a PC over Tailscale.

The phone records into fixed-length WAV segments (48 kHz, 16-bit, mono) and uploads each one when it's finished. If the PC is unreachable, segments pile up on the phone and upload later. Recording never stops.

**Seamless by design.** One `AudioRecord` stream runs for the whole session and is never restarted. It gets split into segments at exact sample counts, so joining a session's segments gives back the original stream with no gaps and no clicks. Reading the mic and writing to disk happen on separate threads, so slow storage can't cause dropouts. The app uses the `UNPROCESSED` mic source (no AGC or noise suppression) when the phone supports it.

## PC side

You need Python 3 and nothing else.

```bash
python3 server/receiver.py serve --bind <your-pc-tailscale-ip> --token <pick-a-secret>
```

Files land in `recordings/<session>/<session>_<seq>.wav` (session = UTC start time). To stitch a session into one file:

```bash
python3 server/receiver.py join recordings/20261117-193000 session.wav
```

Uploads carry a SHA-256 and are written to `.part` first, then renamed into place, so a half-received file is never mistaken for a finished one. Retries are idempotent.

## Phone side

Get the APK from the **Build APK** GitHub Actions run (artifact `bug-yourself-apk`) and sideload it. In the app:

- **Server URL**: `http://<pc-tailscale-name-or-ip>:8765`
- **Token**: same secret as the server
- **Segment minutes**: default 5 (≈29 MB per segment)

Tap **Disable battery optimization** once, then **Start**. A notification stays up while it records. **Stop** finishes the current segment, and the app exits after everything has uploaded.

Pending audio lives in `Android/data/com.bugyourself/files/outbox/` and survives app restarts. A segment cut short by a crash gets repaired and queued the next time you tap Start.

## Notes

- The APK is signed with `android/app/debug.keystore`, which is committed on purpose so every CI build installs over the last one. Uninstalling the app would delete any audio that hasn't uploaded yet.
- Android cuts off mic input during phone calls and when another app takes the mic.
- Storage use is ~330 MB per hour on the phone until uploaded.
