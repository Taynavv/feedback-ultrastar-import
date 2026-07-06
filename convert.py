"""UltraStar song → feedpak converter.

Usage:
    python convert.py <song_dir_or_chart.txt> <out.feedpak> [--keep-dir]

Produces a spec-conformant feedpak (v1.14.0) with:
- manifest.yaml       — metadata, language, lyric_tracks, vocal_tracks (duets),
                        notation-only "Vocals" arrangement, single full stem, cover
- lyrics.json         — flat syllable array with '-' join / '+' line-end suffixes
- vocal_pitch.json    — per-note MIDI (pitched notes only; melisma preserved)
- notation_vocals.json— one-staff monophonic melody (4/4 grid on the chart's BPM)
- stems/full.ogg      — ffmpeg-transcoded source audio
- cover.jpg/.png      — copied cover art when the source has one

Duets import every player as a vocal_tracks[] voice (player 1 is primary and is
mirrored to the singular lyrics/vocal_pitch keys for native karaoke). Melisma is
preserved as its own pitch event. Golden notes flatten to normal; freestyle/rap
keep their lyrics but emit no vocal_pitch note. See docs/vocal-tracks.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ultrastar_parse import Line, Note, Song, merge_melisma, parse_file  # noqa: E402

FEEDPAK_VERSION = "1.14.0"

# UltraStar #LANGUAGE values (full English names) → BCP 47 tags.
LANGUAGE_TAGS = {
    "english": "en", "spanish": "es", "german": "de", "french": "fr",
    "italian": "it", "portuguese": "pt", "japanese": "ja", "korean": "ko",
    "chinese": "zh", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "dutch": "nl", "polish": "pl", "russian": "ru",
    "turkish": "tr", "czech": "cs", "hungarian": "hu", "greek": "el",
    "romanian": "ro", "catalan": "ca", "basque": "eu", "galician": "gl",
    "croatian": "hr", "slovak": "sk", "slovenian": "sl", "ukrainian": "uk",
    "icelandic": "is", "estonian": "et", "latvian": "lv", "lithuanian": "lt",
    "hebrew": "he", "arabic": "ar", "hindi": "hi", "thai": "th",
    "vietnamese": "vi", "indonesian": "id",
}


class ConvertError(RuntimeError):
    pass


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: $FFMPEG_DIR, then PATH, then the FeedBack desktop
    app's bundled copies (resources/bin, relative to the running interpreter)."""
    exe = f"{name}.exe" if os.name == "nt" else name
    env_dir = os.environ.get("FFMPEG_DIR", "").strip()
    if env_dir and (Path(env_dir) / exe).is_file():
        return str(Path(env_dir) / exe)
    found = shutil.which(name)
    if found:
        return found
    app_bin = Path(sys.executable).resolve().parent.parent / "bin" / exe
    if app_bin.is_file():
        return str(app_bin)
    raise ConvertError(f"{name} not found — put it on PATH or set FFMPEG_DIR")


def pick_chart(source: Path) -> Path:
    """Resolve a song dir (or direct .txt path) to one chart file.

    When a folder holds several charts (the corpus' duet '[MULTI]' variants),
    prefer the plain solo chart.
    """
    if source.is_file():
        return source
    charts = sorted(source.glob("*.txt"))
    if not charts:
        raise ConvertError(f"no .txt chart in {source}")
    if len(charts) > 1:
        solo = [c for c in charts if "[MULTI]" not in c.name.upper()]
        return solo[0] if solo else charts[0]
    return charts[0]


# Any container ffmpeg can pull a stem from. Dedicated audio formats rank before
# video: some charts ship audio only inside the #VIDEO clip, and transcode drops
# the video track with -vn.
AUDIO_EXTS = (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".flac",
              ".mp4", ".webm", ".mkv", ".avi", ".mov")


def find_audio(song_dir: Path, headers: dict[str, str]) -> Path | None:
    """Resolve a song folder's audio source.

    Honors #AUDIO (modern UltraStar), then #MP3 (legacy), then #VIDEO; otherwise
    falls back to the first file (case-insensitively) with a known audio/video
    extension, preferring dedicated audio over video. Returns None when nothing
    usable is present.
    """
    for key in ("AUDIO", "MP3", "VIDEO"):
        name = headers.get(key, "").strip()
        if name:
            cand = song_dir / name
            if cand.is_file():
                return cand
    by_ext: dict[str, list[Path]] = {}
    for p in sorted(song_dir.iterdir()):
        if p.is_file():
            by_ext.setdefault(p.suffix.lower(), []).append(p)
    for ext in AUDIO_EXTS:
        if by_ext.get(ext):
            return by_ext[ext][0]
    return None


def language_tag(headers: dict[str, str]) -> str | None:
    raw = headers.get("LANGUAGE", "").strip()
    if not raw:
        return None
    # Multi-language values like "English, Spanish" — take the first.
    first = raw.replace(";", ",").split(",")[0].strip().lower()
    return LANGUAGE_TAGS.get(first)


def fallback_title_artist(chart: Path, song: Song) -> tuple[str, str]:
    """Fill missing #TITLE/#ARTIST from the conventional 'Artist - Title' name."""
    title, artist = song.title, song.artist
    if not title or not artist:
        stem = chart.parent.name if chart.parent.name else chart.stem
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            artist = artist or a.strip()
            title = title or t.strip()
    return title or chart.stem, artist or "Unknown Artist"


# ---------------------------------------------------------------------------
# Side-file builders (pure — unit-testable without audio)
# ---------------------------------------------------------------------------

def build_lyrics(lines: list[Line]) -> list[dict]:
    """lyrics.json entries with the §7.1 suffix conventions.

    Leading space is stripped from each syllable; a syllable followed (in the same
    line) by a token with no word boundary gets a trailing '-' (joins as one word);
    the last syllable of each line gets '+'.
    """
    out: list[dict] = []
    for line in lines:
        for i, n in enumerate(line.notes):
            word = n.text
            # A leading '~' marks a same-vowel continuation that still carries
            # text ("rai" + "~ned") — the tilde itself is not display text.
            text = word.strip().lstrip("~")
            if not text:
                continue
            last = i == len(line.notes) - 1
            if last:
                suffix = "+"
            else:
                nxt = line.notes[i + 1].text
                boundary = (
                    nxt.startswith(" ") or nxt.startswith("\t")
                    or word.endswith(" ") or word.endswith("\t")
                )
                suffix = "" if boundary else "-"
            out.append({"t": round(n.t, 3), "d": round(n.d, 3), "w": text + suffix})
    return out


def build_vocal_pitch(lines: list[Line]) -> dict:
    notes = [
        {"t": round(n.t, 3), "d": round(n.d, 3), "midi": n.midi}
        for line in lines
        for n in line.notes
        if n.pitched and n.midi is not None and n.text.strip()
    ]
    return {"version": 1, "notes": notes}


def build_voice_data(song: Song, lang: str | None) -> list[dict]:
    """Per-voice bundle for every player present, primary voice first.

    Each item carries both the on-disk data (``lyrics``, ``pitch``) and the
    manifest fields (``id``, ``name``, ``role``, ``primary``, ``*_file``,
    ``lyric_tracks``) for one singer. Player 1 is the primary voice and reuses the
    singular file names (``lyrics.json`` / ``vocal_pitch.json``) the top-level
    manifest keys also point at; other players get ``lyrics_p<n>.json`` /
    ``vocal_pitch_p<n>.json``.

    Lyrics come from the melisma-*merged* phrases (display); pitch comes from the
    *raw* phrases, so a ``~`` melisma keeps its own pitch event — native karaoke
    pairs pitch to syllables by onset ``t`` and simply ignores the extra notes
    (see docs/vocal-tracks.md §4.2). Voices with no lyric content are dropped.
    """
    tag = lang or "und"
    present = [p for p in sorted(song.players) if song.players[p]]
    if not present:
        return []
    primary_p = 1 if 1 in present else present[0]
    ordered = [primary_p] + [p for p in present if p != primary_p]

    out: list[dict] = []
    for p in ordered:
        raw = song.players[p]
        lyrics = build_lyrics(merge_melisma(raw))
        if not lyrics:
            continue
        is_primary = p == primary_p
        suffix = "" if is_primary else f"_p{p}"
        vid = f"p{p}"
        lyrics_file = f"lyrics{suffix}.json"
        pitch_file = f"vocal_pitch{suffix}.json"
        name = (song.headers.get(f"DUETSINGERP{p}", "").strip()
                or song.headers.get(f"P{p}", "").strip() or None)
        out.append({
            "id": vid,
            "player": p,
            "name": name,
            "role": "lead" if is_primary else "duet",
            "primary": is_primary,
            "lyrics_file": lyrics_file,
            "pitch_file": pitch_file,
            "lyrics": lyrics,
            "pitch": build_vocal_pitch(raw),
            "lyric_tracks": [{
                "id": f"{vid}-{lang or 'original'}",
                "file": lyrics_file,
                "language": tag,
                "kind": "original",
            }],
        })
    return out


def _vocal_track_entry(v: dict) -> dict:
    """Manifest ``vocal_tracks[]`` entry from a :func:`build_voice_data` item."""
    entry: dict = {"id": v["id"]}
    if v["name"]:
        entry["name"] = v["name"]
    entry["role"] = v["role"]
    if v["primary"]:
        entry["primary"] = True
    entry["stem"] = "full"
    entry["lyrics"] = v["lyrics_file"]
    entry["lyrics_source"] = "authored"
    entry["lyric_tracks"] = v["lyric_tracks"]
    entry["vocal_pitch"] = v["pitch_file"]
    return entry


def display_bpm(header_bpm: float) -> float:
    """Fold the quarter-beat header BPM into a displayable musical tempo (<= 180).

    UltraStar headers are ~4x the musical BPM (395,98 → ~99) but low-valued headers
    are often already musical (108 stays 108). Halving into the sung-tempo range is
    a display heuristic only — all note timing is computed in exact seconds.
    """
    bpm = header_bpm
    while bpm > 180.0:
        bpm /= 2.0
    return bpm


def quantize_dur(seconds: float, whole_note_s: float) -> tuple[int, int | None]:
    """Map a duration to the closest notation (dur denominator, optional dot).

    dur ∈ {1,2,4,8,16,32} per the notation schema; a single dot multiplies the
    nominal length by 1.5. Returns (dur, dot) with dot None when plain.
    """
    if whole_note_s <= 0 or seconds <= 0:
        return 32, None
    best: tuple[float, int, int | None] = (float("inf"), 32, None)
    for den in (1, 2, 4, 8, 16, 32):
        for dot, mult in ((None, 1.0), (1, 1.5)):
            nominal = whole_note_s * mult / den
            err = abs(seconds - nominal)
            if err < best[0]:
                best = (err, den, dot)
    return best[1], best[2]


def build_notation(song: Song, lines: list[Line], t0_offset: float = 0.0) -> dict:
    """notation_vocals.json: one G2 staff, one monophonic voice, 4/4 grid.

    The measure grid runs at the chart's musical tempo (header BPM halved into a
    60–180 display range) starting at the GAP; notes land in the measure containing
    their onset. Durations are quantized to the nearest plain/dotted value.
    `t0_offset` shifts the grid origin — used by merge mode, where all note times
    have been moved onto the target pak's audio timeline.
    """
    bpm = display_bpm(song.bpm)
    quarter_s = 60.0 / bpm
    measure_s = 4.0 * quarter_s
    whole_s = 4.0 * quarter_s
    t0 = song.gap_ms / 1000.0 + t0_offset

    notes = [n for line in lines for n in line.notes
             if n.pitched and n.midi is not None and n.text.strip()]
    measures: list[dict] = []
    if notes:
        last_end = max(n.t + n.d for n in notes)
        # First measure starts at the GAP; earlier notes (negative GAP drift) clamp in.
        count = max(1, int((last_end - t0) / measure_s) + 1)
        beats_by_measure: dict[int, list[dict]] = {}
        for n in notes:
            mi = int((n.t - t0) // measure_s)
            mi = min(max(mi, 0), count - 1)
            dur, dot = quantize_dur(n.d, whole_s)
            beat: dict = {"t": round(n.t, 3), "dur": dur, "notes": [{"midi": n.midi}]}
            if dot:
                beat["dot"] = dot
            beats_by_measure.setdefault(mi, []).append(beat)
        for mi in range(count):
            m: dict = {"idx": mi + 1, "t": round(t0 + mi * measure_s, 3)}
            if mi == 0:
                m["ts"] = [4, 4]
                m["ks"] = 0
                m["tempo"] = round(bpm, 2)
            mb = beats_by_measure.get(mi)
            if mb:
                m["staves"] = {"voice": {"voices": [{"v": 1, "beats": mb}]}}
            measures.append(m)

    return {
        "version": 1,
        "instrument": "vocals",
        "staves": [{"id": "voice", "clef": "G2", "label": "Vocals"}],
        "measures": measures,
    }


# ---------------------------------------------------------------------------
# Audio / packaging
# ---------------------------------------------------------------------------

def transcode_audio(src: Path, dest: Path) -> None:
    ffmpeg = find_tool("ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(src), "-vn",
         "-c:a", "libvorbis", "-q:a", "5", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dest.is_file():
        raise ConvertError(f"ffmpeg transcode failed: {proc.stderr.strip()[:400]}")


def probe_duration(path: Path) -> float:
    ffprobe = find_tool("ffprobe")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise ConvertError(f"ffprobe could not read duration of {path.name}") from exc


def convert(source: Path, out_path: Path, keep_dir: bool = False,
            log=print) -> Path:
    """Convert one UltraStar song to a zipped .feedpak at out_path."""
    chart = pick_chart(source)
    song_dir = chart.parent
    song = parse_file(chart)
    for w in song.warnings:
        log(f"  chart warning: {w}")

    title, artist = fallback_title_artist(chart, song)
    lang = language_tag(song.headers)
    voices = build_voice_data(song, lang)
    primary = next((v for v in voices if v["primary"]), None)
    if primary is None or not primary["lyrics"]:
        raise ConvertError("chart produced no lyrics entries")
    if len(voices) > 1:
        log(f"  duet chart: importing {len(voices)} voices "
            f"({', '.join(v['id'] for v in voices)})")

    audio = find_audio(song_dir, song.headers)
    if audio is None:
        raise ConvertError(f"no audio file for {song_dir.name}")

    notation = build_notation(song, merge_melisma(song.players[primary["player"]]))

    work = Path(tempfile.mkdtemp(prefix="usimport_")) / "pack"
    work.mkdir(parents=True)
    try:
        transcode_audio(audio, work / "stems" / "full.ogg")
        duration = probe_duration(work / "stems" / "full.ogg")

        for v in voices:
            (work / v["lyrics_file"]).write_text(
                json.dumps(v["lyrics"], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            (work / v["pitch_file"]).write_text(
                json.dumps(v["pitch"], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        (work / "notation_vocals.json").write_text(
            json.dumps(notation, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

        cover_rel = None
        cover_name = song.headers.get("COVER", "").strip()
        cover_src = song_dir / cover_name if cover_name else None
        if not cover_src or not cover_src.is_file():
            jpgs = sorted(song_dir.glob("*.jpg")) + sorted(song_dir.glob("*.png"))
            cover_src = jpgs[0] if jpgs else None
        if cover_src and cover_src.is_file():
            cover_rel = "cover" + cover_src.suffix.lower()
            shutil.copyfile(cover_src, work / cover_rel)

        manifest: dict = {
            "feedpak_version": FEEDPAK_VERSION,
            "title": title,
            "artist": artist,
        }
        album = song.headers.get("ALBUM", "").strip()
        if album:
            manifest["album"] = album
        year = song.headers.get("YEAR", "").strip()
        if year.isdigit():
            manifest["year"] = int(year)
        genre = song.headers.get("GENRE", "").strip()
        if genre:
            manifest["genres"] = [genre]
        if lang:
            manifest["language"] = lang
        # #CREATOR (modern) / #AUTHOR (legacy) name the charter, not the artist.
        charter = (song.headers.get("CREATOR", "").strip()
                   or song.headers.get("AUTHOR", "").strip())
        if charter:
            manifest["authors"] = [{"name": charter, "role": "charter"}]
        manifest["duration"] = round(duration, 3)
        manifest["arrangements"] = [{
            "id": "vocals",
            "name": "Vocals",
            "type": "vocals",
            "notation": "notation_vocals.json",
        }]
        manifest["stems"] = [{"id": "full", "file": "stems/full.ogg", "default": True}]
        manifest["lyrics"] = primary["lyrics_file"]
        manifest["lyrics_source"] = "authored"
        manifest["lyric_tracks"] = primary["lyric_tracks"]
        manifest["vocal_pitch"] = primary["pitch_file"]
        if len(voices) > 1:
            manifest["vocal_tracks"] = [_vocal_track_entry(v) for v in voices]
        if cover_rel:
            manifest["cover"] = cover_rel

        (work / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True,
                           default_flow_style=None),
            encoding="utf-8")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if keep_dir:
            if out_path.exists():
                shutil.rmtree(out_path)
            shutil.copytree(work, out_path)
        else:
            tmp_zip = out_path.with_suffix(out_path.suffix + ".tmp")
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(work.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(work).as_posix())
            tmp_zip.replace(out_path)
        return out_path
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="UltraStar song folder or chart .txt")
    ap.add_argument("out", type=Path, help="output .feedpak path")
    ap.add_argument("--keep-dir", action="store_true",
                    help="write the directory form instead of a zip")
    args = ap.parse_args(argv)
    try:
        out = convert(args.source, args.out, keep_dir=args.keep_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
