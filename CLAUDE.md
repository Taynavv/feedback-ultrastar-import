# feedback-ultrastar-import — development guide

Import UltraStar is a [FeedBack](https://github.com/got-feedback/feedBack) plugin
(id `ultrastar_import`) that turns UltraStar karaoke songs into playable feedpak
packages — hand-authored per-syllable lyrics, pitch, and timing become a Vocals
arrangement plus karaoke side-files — and can **merge** those vocals into an
existing library pak of the same recording. This file is the map for contributors
and coding agents.

## Architecture

| File | Role |
|---|---|
| [ultrastar_parse.py](ultrastar_parse.py) | Pure-Python UltraStar `.txt` parser: headers, notes, line breaks, duets (parsed fully; conversion emits every player as a `vocal_tracks` voice, P1 mirrored to the singular keys — see [docs/vocal-tracks.md](docs/vocal-tracks.md)), `#RELATIVE`, encodings, melisma, octave folding |
| [convert.py](convert.py) | Song folder → `.feedpak`: manifest, `lyrics.json`, `vocal_pitch.json`, `notation_vocals.json`, ffmpeg-transcoded stem, cover. Also a CLI |
| [merge.py](merge.py) | Graft vocals into an existing pak of the same recording: onset-envelope cross-correlation, piecewise offset map, accept/review/refuse gating, backup/restore. Also a CLI (needs numpy) |
| [routes.py](routes.py) | Plugin backend (`setup(app, context)`): scan a songs folder, batch import over a websocket, merge-with-fallback, backup management endpoints |
| [screen.html](screen.html) / [screen.js](screen.js) | Import screen: folder scan, song table with merge→/merged/imported states, progress log, merge-backup list |
| [tests/](tests) | pytest, fully content-free: synthetic charts, ffmpeg-generated audio, fake paks. Optional env vars light up extra coverage (below) |

Data shapes follow the
[feedpak spec](https://github.com/got-feedback/feedpak-spec) v1.14 (§5 manifest,
§7.1 lyrics, §7.2 vocal_pitch, §7.6 notation). Extend the format only by the
spec's **golden rule** (§9.1) — a new `snake_case` manifest key pointing at a new
versioned side-file, gated on the key's presence — **never** by overloading or
repurposing an existing field (§9.4 calls that "the most dangerous" change). The
manifest schema is `additionalProperties: true`, so a golden-rule extension key
still passes the spec repo's `tools/validate.py`, which every produced or modified
pak must. An extension key we author is a plugin-space proving ground for a future
upstream proposal: document it, mirror an existing spec pattern where one fits
(e.g. `lyric_tracks`), and keep the equivalent singular/standard keys populated so
older Readers still work.

## Load-bearing subtleties — do not "clean up" casually

- **UltraStar timing**: `seconds = GAP/1000 + beat * 60 / (BPM * 4)` — beats are
  quarter-beats and the `#BPM` header is ~4x the musical tempo. `display_bpm()`
  halves it into a 60–180 display range for notation only; all real timing is
  computed in seconds directly.
- **Octave folding is one global shift per player.** Charters are octave-sloppy
  (UltraStar scores octave-independently), so `fold_octaves` moves the whole song's
  median into C3–C5. Per-note folding would destroy melodic intervals.
- **Note text is not `.rstrip()`ed.** Word boundaries live in syllable whitespace —
  *leading* space on the next token or *trailing* space on the current one — and both
  conventions appear in real charts. `build_lyrics` turns them into the spec's `-`
  (join) / `+` (line end) suffixes.
- **Two tilde conventions**: a syllable of exactly `~` is a melisma continuation and
  merges into its parent (`merge_melisma`); `~text` is a continuation *with* text —
  the note stays (it is a real pitch event) but the tilde is stripped from display.
- **Merge alignment is a piecewise-constant offset map**, not a single offset.
  Different *edits* of one recording (video cut vs album cut) align perfectly in
  spans separated by insert/cut points; `windowed_offsets` → `build_segments` finds
  them. Syllables inside an uncovered edit window are ambiguous and demote the
  verdict — a single-offset merge shipped once and drifted 10 s after an edit point.
- **The accept gate needs BOTH correlation strength and peak dominance.** Real
  calibration: same recording → peak_z ≈ 25, secondary ratio ≈ 2.2; unrelated songs
  → peak_z 6–11, secondary ≤ 1.25 (many similar-height peaks). Gating on peak_z
  alone misclassifies; thresholds live at the top of merge.py with the data.
- **Per-song websocket failures use the `message` key, not `error`.** The client
  loop aborts the whole batch on `error`; one bad song must not stop an import run.
- **`meta_db.put` keys are POSIX-slash relpaths.** The core scanner keys entries
  with forward slashes; a backslash key shadow-duplicates the entry.
- **Backups are first-merge-wins.** `<pak>.feedpak.pre-merge.bak` is the pak before
  *any* vocal merge; re-merges never overwrite it. Restore consumes it. They never
  expire by policy — disposal is an explicit user action in the UI.
- **`.sloppak` in the scanners is host-parity, not a stale brand.** FeedBack accepts
  `.feedpak` *and* legacy `.sloppak` (core `lib/sloppak.py` `SONG_EXTS`); `merge.py`
  and the backup scan match both so merge finds legacy library paks. Don't reduce them
  to `.feedpak`-only — that silently skips legacy paks. Only the output *subdir*
  (`_OUTPUT_SUBDIR = "ultrastar_import"`) is ours to name.
- **`lyrics_source: authored` is correct per spec** even though current FeedBack
  logs a warning and falls back to its legacy vocabulary — stay spec-conformant.
- **ffmpeg/ffprobe resolution order**: `FFMPEG_DIR` env → `PATH` → the FeedBack
  desktop app's own `resources/bin` (relative to the running Python). The packaged
  app works with zero configuration.

## Rules

- **License**: MIT. Keep every contribution MIT-compatible.
- **No song content, ever**: no UltraStar files, no audio, no generated paks in the
  repo, tests, or CI. Tests synthesize everything (charts inline, audio via ffmpeg
  `lavfi`, paks from code).
- **Spec conformance is the gate**: validate against a
  [feedpak-spec](https://github.com/got-feedback/feedpak-spec) checkout (CI pins the
  spec tag matching `FEEDPAK_VERSION` in convert.py).
- Match the release tag to `plugin.json`'s `version` — the release workflow fails
  the build if they disagree. `feedback_target` records the FeedBack version the
  plugin was last verified against.

## Development

```
python -m venv .venv
.venv/Scripts/pip install pytest pyyaml jsonschema fastapi numpy
.venv/Scripts/python -m pytest tests/ -q
```

Optional env vars for extra local coverage:

- `FFMPEG_DIR` — directory holding ffmpeg/ffprobe if they are not on PATH
  (audio-dependent tests skip without ffmpeg).
- `FEEDPAK_SPEC_DIR` — a feedpak-spec checkout; enables the reference-validator
  end-to-end test.
- `ULTRASTAR_SONGS` — a real UltraStar `songs` folder; enables the full-corpus
  parser smoke test (content is read in place, never copied).

CLI entry points: `convert.py <song_dir> <out.feedpak>`,
`merge.py <song_dir> <pak> [--apply|--force]`, `merge.py <pak> --restore`,
`merge.py --scan <songs_root> <dlc_dir>`.
