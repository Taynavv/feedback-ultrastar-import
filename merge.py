"""UltraStar → existing feedpak vocal merge.

Grafts the vocal chart from an UltraStar song onto an existing library feedpak of
the *same recording*: verifies recording identity and estimates the constant time
offset by cross-correlating onset envelopes of the two audio files, then writes
lyrics.json / vocal_pitch.json / notation_vocals.json (times shifted onto the pak's
audio timeline) plus a notation-only Vocals arrangement into the pak.

Refuses low-confidence matches — a live chart must not merge onto a studio pak.

Usage:
    python merge.py <ultrastar_song_dir> <pak.feedpak>            # dry run: report only
    python merge.py <ultrastar_song_dir> <pak.feedpak> --apply    # write (backs up first)
    python merge.py --scan <ultrastar_songs_root> <dlc_dir>       # list merge candidates

Requires numpy and ffmpeg.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert import (  # noqa: E402
    ConvertError,
    build_lyrics,
    build_notation,
    build_vocal_pitch,
    find_audio,
    find_tool,
    language_tag,
    pick_chart,
)
from ultrastar_parse import merge_melisma, parse_file  # noqa: E402

SR = 22050
HOP = 512
WIN = 1024
MAX_LAG_S = 90.0
# Verdict gates, calibrated on real pairs (2026-07-04):
#   same recording   (Believer CDLC vs UltraStar):  peak_z 24.9, secondary 2.20
#   unrelated songs  (5 controls vs Believer pak):  peak_z 6.4–10.7, secondary ≤ 1.25
# Accept needs BOTH a strong peak and a *dominant* one — unrelated audio produces
# many similar-height peaks, so its secondary ratio hugs 1.0.
Z_ACCEPT = 15.0
Z_REVIEW = 12.0
SECONDARY_ACCEPT = 1.4


class MergeError(RuntimeError):
    pass


BACKUP_SUFFIX = ".pre-merge.bak"


def backup_path(pak: Path) -> Path:
    return pak.with_suffix(pak.suffix + BACKUP_SUFFIX)


def restore(pak: Path, log=print) -> Path:
    """Undo a merge: put the pre-merge backup back in place (consuming it)."""
    bak = backup_path(pak)
    if not bak.is_file():
        raise MergeError(f"no backup for {pak.name} ({bak.name} not found)")
    bak.replace(pak)
    log(f"  restored {pak.name} from its pre-merge backup")
    return pak


@dataclass
class Segment:
    """One span of the UltraStar timeline sharing a constant offset to the pak."""
    us_start: float
    us_end: float
    offset: float


@dataclass
class MergeReport:
    offset: float           # first segment's offset (seconds ADDED to US times)
    peak_z: float           # correlation peak strength (z-score vs lag window)
    secondary_ratio: float  # main peak vs best peak elsewhere (>1 = distinct)
    onset_score: float      # mean envelope energy at syllable onsets vs baseline
    verdict: str            # "accept" | "review" | "refuse"
    stem_used: str          # pak stem id correlated against
    duration_delta: float   # |pak duration - ultrastar audio duration|
    segments: list = None   # piecewise offset map (different edits of a recording)
    ambiguous: int = 0      # syllables falling between segments (unmappable)

    def summary(self) -> str:
        segs = ""
        if self.segments and len(self.segments) > 1:
            parts = ", ".join(f"[{s.us_start:.0f}..{s.us_end:.0f}s -> {s.offset:+.3f}s]"
                              for s in self.segments)
            segs = f"  segments={parts}"
            if self.ambiguous:
                segs += f"  AMBIGUOUS_SYLLABLES={self.ambiguous}"
        return (f"verdict={self.verdict}  offset={self.offset:+.3f}s  "
                f"peak_z={self.peak_z:.1f}  secondary={self.secondary_ratio:.2f}  "
                f"onset_score={self.onset_score:.2f}  stem={self.stem_used}  "
                f"dur_delta={self.duration_delta:.1f}s{segs}")


# ---------------------------------------------------------------------------
# Audio → onset envelope → offset
# ---------------------------------------------------------------------------

def decode_pcm(path: Path, sr: int = SR) -> np.ndarray:
    """Decode any audio file to float32 mono PCM via ffmpeg."""
    ffmpeg = find_tool("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le",
         "-ac", "1", "-ar", str(sr), "pipe:1"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise MergeError(f"ffmpeg decode failed for {path.name}: "
                         f"{proc.stderr.decode(errors='replace')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def onset_envelope(samples: np.ndarray, hop: int = HOP, win: int = WIN) -> np.ndarray:
    """Half-wave-rectified log-energy flux — a cheap, robust onset envelope."""
    if len(samples) < win:
        return np.zeros(1, dtype=np.float32)
    n_frames = 1 + (len(samples) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = samples[idx]
    energy = np.log1p(np.sqrt(np.mean(frames * frames, axis=1)) * 1000.0)
    flux = np.diff(energy, prepend=energy[0])
    flux[flux < 0] = 0.0
    std = flux.std()
    return (flux - flux.mean()) / std if std > 0 else flux


def estimate_offset(env_pak: np.ndarray, env_us: np.ndarray,
                    hop_s: float = HOP / SR,
                    max_lag_s: float = MAX_LAG_S) -> tuple[float, float, float]:
    """Cross-correlate envelopes; return (offset_seconds, peak_z, secondary_ratio).

    offset is what to ADD to UltraStar-timeline times to land on the pak timeline.
    """
    n = len(env_pak) + len(env_us) - 1
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(env_pak, nfft)
                        * np.conj(np.fft.rfft(env_us, nfft)), nfft)
    # lag k = shift of env_us to the right (later) to align with env_pak.
    # Restrict lags to the linear-correlation support — beyond it the circular
    # FFT indices alias back into the window and fabricate phantom peaks.
    max_lag = int(max_lag_s / hop_s)
    lags = np.arange(-min(max_lag, len(env_us) - 1),
                     min(max_lag, len(env_pak) - 1) + 1)
    vals = corr[lags % nfft]

    peak_i = int(np.argmax(vals))
    peak = float(vals[peak_i])
    offset = float(lags[peak_i]) * hop_s

    # Peak strength vs the rest of the lag window, excluding ±1 s around the peak.
    guard = int(1.0 / hop_s)
    mask = np.ones(len(vals), dtype=bool)
    mask[max(0, peak_i - guard):peak_i + guard + 1] = False
    rest = vals[mask]
    std = float(rest.std())
    peak_z = (peak - float(rest.mean())) / std if std > 0 else 0.0
    secondary = float(rest.max()) if len(rest) else 0.0
    secondary_ratio = peak / secondary if secondary > 0 else float("inf")
    return offset, peak_z, secondary_ratio


WIN_S = 15.0      # windowed-alignment window
STEP_S = 5.0      # window stride
SEARCH_S = 12.0   # local lag search range around the global offset
Z_WINDOW = 6.0    # per-window confidence gate
OFF_TOL = 0.12    # offsets within this are "the same segment"


def windowed_offsets(env_pak: np.ndarray, env_us: np.ndarray, global_offset: float,
                     hop_s: float = HOP / SR) -> list[tuple[float, float, float]]:
    """Local (us_time, offset, z) measured over sliding windows of the US envelope.

    Different *edits* of one recording (radio cut vs video cut) align perfectly in
    spans separated by insert/cut points; a single global offset cannot represent
    that. This is the raw data the segment map is built from.
    """
    win = int(WIN_S / hop_s)
    step = int(STEP_S / hop_s)
    search = int(SEARCH_S / hop_s)
    g_off = int(round(global_offset / hop_s))
    rows: list[tuple[float, float, float]] = []
    for start in range(0, max(1, len(env_us) - win), step):
        w = env_us[start:start + win]
        if len(w) < win or w.std() < 1e-6:
            continue
        w = (w - w.mean()) / w.std()
        lo = max(0, start + g_off - search)
        hi = min(len(env_pak), start + g_off + win + search)
        seg = env_pak[lo:hi]
        if len(seg) <= win:
            continue
        corr = np.correlate(seg, w, mode="valid")
        peak_i = int(np.argmax(corr))
        guard = int(1.0 / hop_s)
        rest = np.delete(corr, np.arange(max(0, peak_i - guard),
                                         min(len(corr), peak_i + guard + 1)))
        z = ((float(corr[peak_i]) - float(rest.mean())) / float(rest.std())
             if len(rest) > 2 and rest.std() > 0 else 0.0)
        rows.append((start * hop_s, (lo + peak_i - start) * hop_s, z))
    return rows


def build_segments(rows: list[tuple[float, float, float]],
                   fallback_offset: float) -> list[Segment]:
    """Cluster confident windows into constant-offset segments."""
    confident = [(t, o) for t, o, z in rows if z >= Z_WINDOW]
    if not confident:
        return [Segment(float("-inf"), float("inf"), fallback_offset)]
    segments: list[Segment] = []
    cur_t = [confident[0][0]]
    cur_o = [confident[0][1]]
    for t, o in confident[1:]:
        if abs(o - float(np.median(cur_o))) <= OFF_TOL:
            cur_t.append(t)
            cur_o.append(o)
        else:
            segments.append(Segment(cur_t[0], cur_t[-1] + WIN_S, float(np.median(cur_o))))
            cur_t, cur_o = [t], [o]
    segments.append(Segment(cur_t[0], cur_t[-1] + WIN_S, float(np.median(cur_o))))
    # Merge neighbours that re-converged to the same offset.
    merged: list[Segment] = [segments[0]]
    for s in segments[1:]:
        if abs(s.offset - merged[-1].offset) <= OFF_TOL:
            merged[-1] = Segment(merged[-1].us_start, s.us_end, merged[-1].offset)
        else:
            merged.append(s)
    merged[0] = Segment(float("-inf"), merged[0].us_end, merged[0].offset)
    merged[-1] = Segment(merged[-1].us_start, float("inf"), merged[-1].offset)
    return merged


def offset_at(segments: list[Segment], t: float) -> tuple[float, bool]:
    """(offset, is_ambiguous) for a US-timeline instant.

    A time between two segments' covered spans sits inside an edit whose exact
    cut point is unknown — mapped to the nearest covered edge, flagged ambiguous.
    """
    for s in segments:
        if s.us_start <= t <= s.us_end:
            return s.offset, False
    for a, b in zip(segments, segments[1:]):
        if a.us_end < t < b.us_start:
            mid = (a.us_end + b.us_start) / 2.0
            return (a.offset if t <= mid else b.offset), True
    return segments[-1].offset, True  # unreachable in practice


def onset_alignment_score(env: np.ndarray, onset_times: list[float],
                          hop_s: float = HOP / SR) -> float:
    """How energetic the pak audio is at the (shifted) syllable onsets, vs its
    own baseline. ~1.0 = onsets are no better than random; >1.5 = clearly synced."""
    frames = [int(t / hop_s) for t in onset_times]
    frames = [f for f in frames if 0 <= f < len(env)]
    if not frames:
        return 0.0
    # env is z-scored; compare mean-at-onsets against a shuffled baseline spread.
    at_onsets = float(np.mean(np.maximum(env[frames], 0)))
    baseline = float(np.mean(np.maximum(env, 0)))
    return at_onsets / baseline if baseline > 0 else 0.0


# ---------------------------------------------------------------------------
# Pak access
# ---------------------------------------------------------------------------

def read_manifest(pak: Path) -> dict:
    with zipfile.ZipFile(pak) as z:
        return yaml.safe_load(z.read("manifest.yaml"))


def extract_stem(pak: Path, tmp: Path) -> tuple[Path, str]:
    """Extract the best stem for correlation: vocals if present, else the
    default/full mix. Returns (extracted file, stem id)."""
    manifest = read_manifest(pak)
    stems = manifest.get("stems") or []
    pick = None
    for s in stems:
        if s.get("id") == "vocals":
            pick = s
            break
    if pick is None:
        for s in stems:
            if s.get("default") in (True, "true", "on", "yes"):
                pick = s
                break
    if pick is None and stems:
        pick = stems[0]
    if pick is None:
        raise MergeError("pak has no stems to correlate against")
    rel = pick["file"]
    out = tmp / Path(rel).name
    with zipfile.ZipFile(pak) as z:
        out.write_bytes(z.read(rel))
    return out, pick.get("id", "?")


# ---------------------------------------------------------------------------
# Matching (—scan mode)
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.casefold())
    return re.sub(r"\s+", " ", s).strip()


def scan_candidates(songs_root: Path, dlc_dir: Path) -> list[dict]:
    """Pair UltraStar folders with library paks by normalized artist+title."""
    paks = []
    for pak in sorted(dlc_dir.rglob("*.feedpak")) + sorted(dlc_dir.rglob("*.sloppak")):
        try:
            m = read_manifest(pak)
            paks.append((normalize(str(m.get("artist", ""))),
                         normalize(str(m.get("title", ""))),
                         float(m.get("duration", 0) or 0), pak))
        except Exception:
            continue
    out = []
    for folder in sorted(p for p in songs_root.iterdir() if p.is_dir()):
        try:
            chart = pick_chart(folder)
            song = parse_file(chart)
        except Exception:
            continue
        na, nt = normalize(song.artist), normalize(song.title)
        if not nt:
            continue
        for pa, pt, pdur, pak in paks:
            if nt == pt and (na == pa or not na or not pa):
                # Skip paks that already carry a vocals arrangement.
                has_vocals = any(a.get("id") == "vocals"
                                 for a in read_manifest(pak).get("arrangements", []))
                out.append({"folder": folder.name, "pak": str(pak),
                            "pak_duration": pdur, "already_merged": has_vocals})
    return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def analyze(song_dir: Path, pak: Path, log=print) -> tuple[MergeReport, object, list]:
    """Correlate the two recordings; return (report, parsed song, shifted lines)."""
    chart = pick_chart(song_dir)
    song = parse_file(chart)
    for w in song.warnings:
        log(f"  chart warning: {w}")

    audio = find_audio(song_dir, song.headers)
    if audio is None:
        raise MergeError(f"no audio in {song_dir.name} to correlate")

    with tempfile.TemporaryDirectory(prefix="usmerge_") as td:
        stem_file, stem_id = extract_stem(pak, Path(td))
        pcm_pak = decode_pcm(stem_file)
        pcm_us = decode_pcm(audio)

    env_pak = onset_envelope(pcm_pak)
    env_us = onset_envelope(pcm_us)
    offset, peak_z, secondary = estimate_offset(env_pak, env_us)

    # Different edits of the same recording align in constant-offset spans —
    # map each syllable through the segment its onset falls in.
    segments = build_segments(windowed_offsets(env_pak, env_us, offset), offset)
    ambiguous = 0
    lines = merge_melisma(song.lines)
    for line in lines:
        for n in line.notes:
            off, amb = offset_at(segments, n.t)
            if amb and n.text.strip():
                ambiguous += 1
            n.t += off
    if len(segments) > 1:
        log(f"  edit detected: {len(segments)} constant-offset segments")
    onsets = [n.t for line in lines for n in line.notes if n.text.strip()]
    onset_score = onset_alignment_score(env_pak, onsets)

    dur_pak = len(pcm_pak) / SR
    dur_us = len(pcm_us) / SR

    if peak_z >= Z_ACCEPT and secondary >= SECONDARY_ACCEPT:
        verdict = "accept"
    elif peak_z >= Z_REVIEW:
        verdict = "review"
    else:
        verdict = "refuse"
    # A great correlation with syllables landing in silence is still suspect,
    # and so are syllables charted inside an edit whose cut point is unknown.
    if verdict == "accept" and (onset_score < 0.8 or ambiguous):
        verdict = "review"

    report = MergeReport(
        offset=segments[0].offset, peak_z=peak_z, secondary_ratio=secondary,
        onset_score=onset_score, verdict=verdict, stem_used=stem_id,
        duration_delta=abs(dur_pak - dur_us),
        segments=segments, ambiguous=ambiguous,
    )
    return report, song, lines


def apply_merge(song_dir: Path, pak: Path, report: MergeReport, song, lines,
                log=print) -> Path:
    """Write the vocal files + arrangement into the pak (with a .bak backup)."""
    first_t = min((n.t for line in lines for n in line.notes), default=0.0)
    if first_t < -0.05:
        raise MergeError(f"offset pushes first syllable to {first_t:.2f}s (<0) — refusing")
    for line in lines:                     # sub-50ms underflow: clamp to song start
        for n in line.notes:
            if n.t < 0:
                n.t = 0.0

    lyrics = build_lyrics(lines)
    if not lyrics:
        raise MergeError("chart produced no lyrics entries")
    vocal_pitch = build_vocal_pitch(lines)
    notation = build_notation(song, lines, t0_offset=report.offset)

    manifest = read_manifest(pak)
    manifest["lyrics"] = "lyrics.json"
    manifest["lyrics_source"] = "authored"
    manifest["vocal_pitch"] = "vocal_pitch.json"
    lang = language_tag(song.headers)
    if lang and "language" not in manifest:
        manifest["language"] = lang
    manifest["lyric_tracks"] = [t for t in manifest.get("lyric_tracks") or []
                                if t.get("id") != (lang or "original")]
    manifest["lyric_tracks"].append({
        "id": lang or "original", "file": "lyrics.json",
        "language": lang or "und", "kind": "original",
    })
    arrangements = [a for a in manifest.get("arrangements", [])
                    if a.get("id") != "vocals"]
    arrangements.append({"id": "vocals", "name": "Vocals", "type": "vocals",
                         "notation": "notation_vocals.json"})
    manifest["arrangements"] = arrangements

    backup = backup_path(pak)
    if not backup.exists():
        shutil.copyfile(pak, backup)
        log(f"  backup: {backup.name}")

    replaced = {"manifest.yaml", "lyrics.json", "vocal_pitch.json",
                "notation_vocals.json"}
    tmp_zip = pak.with_suffix(pak.suffix + ".tmp")
    with zipfile.ZipFile(pak) as zin, \
            zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename not in replaced:
                zout.writestr(item, zin.read(item.filename))
        dump = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
        zout.writestr("lyrics.json", dump(lyrics))
        zout.writestr("vocal_pitch.json", dump(vocal_pitch))
        zout.writestr("notation_vocals.json", dump(notation))
        zout.writestr("manifest.yaml",
                      yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True,
                                     default_flow_style=None))
    tmp_zip.replace(pak)
    return pak


def merge(song_dir: Path, pak: Path, apply: bool = False, force: bool = False,
          log=print) -> MergeReport:
    report, song, lines = analyze(song_dir, pak, log=log)
    log(f"  {report.summary()}")
    if not apply:
        log("  dry run — pass --apply to write")
        return report
    if report.verdict == "refuse" and not force:
        raise MergeError(f"refusing to merge ({report.summary()})")
    if report.verdict == "review" and not force:
        raise MergeError(
            f"match is uncertain — rerun with --force to merge anyway ({report.summary()})")
    apply_merge(song_dir, pak, report, song, lines, log=log)
    log(f"  merged vocals into {pak.name}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path,
                    help="UltraStar song folder (songs root with --scan; pak with --restore)")
    ap.add_argument("target", type=Path, nargs="?",
                    help="target .feedpak (or DLC dir with --scan)")
    ap.add_argument("--apply", action="store_true", help="write the merge")
    ap.add_argument("--force", action="store_true",
                    help="apply even on a review/refuse verdict")
    ap.add_argument("--scan", action="store_true",
                    help="list merge candidates: source=songs root, target=DLC dir")
    ap.add_argument("--restore", action="store_true",
                    help="undo a merge: restore <pak> from its .pre-merge.bak")
    args = ap.parse_args(argv)
    try:
        if args.restore:
            restore(args.source)
            return 0
        if args.target is None:
            ap.error("target is required unless --restore is given")
        if args.scan:
            for c in scan_candidates(args.source, args.target):
                flag = " [already merged]" if c["already_merged"] else ""
                print(f"{c['folder']}  ->  {c['pak']}{flag}")
            return 0
        merge(args.source, args.target, apply=args.apply, force=args.force)
        return 0
    except (MergeError, ConvertError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
