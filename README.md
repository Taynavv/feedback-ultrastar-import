# Import UltraStar

A [FeedBack](https://github.com/got-feedback/feedBack) plugin that imports
[UltraStar](https://usdx.eu/) karaoke songs into feedpak packages — hand-authored
per-syllable lyrics, pitch, and timing become a playable **Vocals arrangement** —
and merges those vocals into songs already in your library.

Pair it with [Karaoke Highway](https://github.com/Taynavv/feedback-vocals-viz) to
sing them.

## Features

- **Bulk import** — point it at your UltraStar `songs` folder; it lists every chart
  with artist/title/language, skips what you've already imported, and converts the
  rest with per-song progress.
- **Faithful conversion** — syllable-level lyrics with word-join/line-end markers,
  per-syllable MIDI pitch, a standard-notation vocals melody, transcoded audio, and
  cover art, all in spec-conformant feedpak v1.14 packages (golden notes flatten to
  normal; freestyle/rap keep lyrics without pitch; duets import player 1).
- **Merge into existing songs** — if the same recording is already in your library
  (say, a guitar chart), the importer grafts the vocal chart *into that pak* instead
  of creating a duplicate. Identity is verified acoustically: onset-envelope
  cross-correlation must find one dominant alignment, and different *edits* of a
  recording (video cut vs album cut) are handled with a piecewise offset map.
  Uncertain or non-matching audio is refused and imported as its own song instead —
  a live version will never be grafted onto a studio pak.
- **Undoable merges** — the first merge into a pak leaves a `.pre-merge.bak` beside
  it. The plugin screen lists all backups with per-pak Restore (undo the merge) and
  Delete, so cleanup is always your call.
- **Robust parsing** — comma decimals, BOM/`#ENCODING`/CP1252 fallback, `#RELATIVE`
  charts, melisma continuations, `P1/P2/P3` duet markers (only P1's part is
  imported; P2-only lines are dropped), octave-sloppy pitch data (folded into a
  plausible vocal register), audio referenced via video files.

## Requirements

- FeedBack **0.3.0-alpha.1** or later (the desktop app ships everything else the
  plugin needs, including ffmpeg).
- Your own UltraStar song library — a folder of song folders, each with a `.txt`
  chart and its audio. **This plugin ships no song content**, and your content is
  read in place, never uploaded anywhere.
- For CLI use outside the app: Python 3.10+, `pyyaml` (+ `numpy` for merge), and
  ffmpeg/ffprobe on `PATH` or via the `FFMPEG_DIR` environment variable.

## Install

**From a release:** download the plugin zip from the releases page and extract the
`feedback-ultrastar-import` folder into your FeedBack user-plugins directory.
Restart FeedBack.

**From source:** copy (or junction/symlink) this repository into the user-plugins
directory and restart FeedBack.

"Import UltraStar" appears in the navigation. Point it at your songs folder, Scan,
review the list (songs with a library match show **merge →**), and Import. The
"merge into existing songs" toggle controls whether matches merge or import
standalone.

## CLI (no app needed)

```
python convert.py "<UltraStar song folder>" "out.feedpak"     # one song → one pak
python merge.py "<song folder>" "<library pak>"               # dry run: match verdict
python merge.py "<song folder>" "<library pak>" --apply       # merge (keeps a backup)
python merge.py "<library pak>" --restore                     # undo a merge
python merge.py --scan "<songs root>" "<DLC dir>"             # list merge candidates
```

## Development

```
python -m venv .venv
.venv/Scripts/pip install pytest pyyaml jsonschema fastapi numpy
.venv/Scripts/python -m pytest tests/ -q
```

The test suite is fully content-free: charts are synthesized inline, audio is
generated with ffmpeg's `lavfi` sources, and paks are built from code. Optional
environment variables enable extra coverage — `FEEDPAK_SPEC_DIR` (a
[feedpak-spec](https://github.com/got-feedback/feedpak-spec) checkout, for the
reference-validator gate; CI pins `v1.14.0`) and `ULTRASTAR_SONGS` (a real songs
folder, for the full-corpus parser smoke test).

See [CLAUDE.md](CLAUDE.md) for an architecture map and contributor/agent notes.

## AI disclosure, warranty, and contributions

**This plugin was built with heavy use of AI coding tools.** The large majority of
the code was written by an AI assistant working under human direction, with human
review and hands-on testing against a real FeedBack install — but you should read
it with the same skepticism you'd apply to any code of unknown provenance.

**There is no warranty.** This is open-source software provided **as-is**, without
warranty of any kind, express or implied — see the [LICENSE](LICENSE). The merge
feature rewrites pak files in your library (it takes a backup first, and refuses
uncertain matches, but treat your library like the data it is and keep your own
backups of anything you can't recreate).

**Contributions are welcome.** If you find a bug or want a feature, open an issue —
or better, submit a pull request. Small, focused PRs with a description of what was
tested are the easiest to review. By contributing you agree your changes are
licensed under the same MIT terms.

## License

**MIT** — see [LICENSE](LICENSE). Song content is yours; this repository contains
and distributes none.
