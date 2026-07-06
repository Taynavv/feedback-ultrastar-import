"""UltraStar .txt chart parser.

Pure-Python, no dependencies. Parses an UltraStar karaoke chart into headers plus a
normalized note list: absolute seconds, octave-folded MIDI pitch, and line (phrase)
structure, per player.

Format notes (the gotchas below all occur in real-world chart libraries):
- Note lines: ``<type> <startBeat> <lengthBeats> <pitch> <text>`` where type is
  ``:`` normal, ``*`` golden, ``F`` freestyle (unpitched), ``R``/``G`` rap (unpitched).
- ``- <beat>`` is a line break; in ``#RELATIVE:YES`` charts it may carry a second
  number and both advance the running beat offset.
- Timing: ``seconds = GAP/1000 + beat * 60 / (BPM * 4)`` — UltraStar beats are
  quarter-beats and #BPM is ~4x the musical BPM.
- Decimal commas (``#BPM:395,98``) and mixed CP1252/UTF-8 encodings are common.
- A syllable text of ``~`` continues the previous syllable at a new pitch (melisma).
- ``P1``/``P2`` markers introduce duet parts (``P3`` means both singers). The
  parser keeps every player's part; conversion emits each as a ``vocal_tracks``
  voice (P3 lines count toward both players; P1 is the primary voice, mirrored to
  the singular lyrics/vocal_pitch keys).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

# Note types that carry a meaningful pitch.
PITCHED_TYPES = {":", "*"}
# All recognized note types: normal, golden, freestyle, rap, golden rap.
NOTE_TYPES = {":", "*", "F", "R", "G"}

_NOTE_RE = re.compile(r"^([:*FRG])\s+(-?\d+)\s+(\d+)\s+(-?\d+)(?:[ \t](.*))?$")
_LINEBREAK_RE = re.compile(r"^-\s*(-?\d+)(?:\s+(-?\d+))?\s*$")
_PLAYER_RE = re.compile(r"^P\s*(\d+)\s*$")
_HEADER_RE = re.compile(r"^#([^:]+):(.*)$")


class UltraStarError(ValueError):
    """Raised when a chart cannot be parsed at all (missing/broken essentials)."""


@dataclass
class Note:
    """One sung syllable (or continuation) with raw and normalized timing."""

    type: str                 # ':' normal, '*' golden, 'F' freestyle, 'R'/'G' rap
    start_beat: int
    length_beats: int
    pitch: int                # raw pitch column (meaningless for unpitched types)
    text: str                 # raw syllable text, leading space significant
    player: int = 1
    is_continuation: bool = False  # text was '~' — melisma continuation
    # Filled in by normalization:
    t: float = 0.0            # absolute start, seconds
    d: float = 0.0            # duration, seconds
    midi: int | None = None   # folded MIDI pitch; None for unpitched types

    @property
    def pitched(self) -> bool:
        return self.type in PITCHED_TYPES


@dataclass
class Line:
    """One phrase — the notes between two line-break markers."""

    notes: list[Note] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.notes[0].t if self.notes else 0.0

    @property
    def end(self) -> float:
        return self.notes[-1].t + self.notes[-1].d if self.notes else 0.0


@dataclass
class Song:
    """A parsed chart: headers plus per-player phrase lists."""

    headers: dict[str, str]
    players: dict[int, list[Line]]
    bpm: float                # header value (quarter-beat BPM, ~4x musical)
    gap_ms: float
    relative: bool
    encoding_used: str
    octave_shift: dict[int, int]   # per player, semitones applied by fold
    warnings: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[Line]:
        """Player 1's phrases (the only part for non-duet charts)."""
        return self.players.get(1, [])

    @property
    def is_duet(self) -> bool:
        return any(p != 1 and lines for p, lines in self.players.items())

    @property
    def title(self) -> str:
        return self.headers.get("TITLE", "")

    @property
    def artist(self) -> str:
        return self.headers.get("ARTIST", "")

    def all_notes(self, player: int = 1) -> list[Note]:
        return [n for line in self.players.get(player, []) for n in line.notes]


def _parse_number(value: str) -> float:
    """Parse a numeric header value accepting both ',' and '.' decimals."""
    return float(value.strip().replace(",", "."))


_ENCODING_ALIASES = {
    "UTF8": "utf-8",
    "UTF-8": "utf-8",
    "CP1252": "cp1252",
    "CP1250": "cp1250",
    "ANSI": "cp1252",
    "LOCALE": "cp1252",
    "AUTO": "",  # fall through to detection
}


def decode_chart_bytes(data: bytes) -> tuple[str, str]:
    """Decode chart bytes honoring BOM and #ENCODING, else UTF-8 → CP1252 fallback.

    Returns (text, encoding_name_used).
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"

    # The #ENCODING header is ASCII, so it is findable in any single-byte decode.
    probe = data[:4096].decode("latin-1")
    m = re.search(r"#ENCODING:\s*(\S+)", probe, re.IGNORECASE)
    if m:
        declared = _ENCODING_ALIASES.get(m.group(1).upper(), m.group(1).lower())
        if declared:
            try:
                return data.decode(declared), declared
            except (UnicodeDecodeError, LookupError):
                pass  # lying or unknown header — fall through to detection

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def beat_to_seconds(beat: float, bpm: float, gap_ms: float) -> float:
    """UltraStar beat → absolute seconds: GAP/1000 + beat * 60 / (BPM * 4)."""
    return gap_ms / 1000.0 + beat * 60.0 / (bpm * 4.0)


def fold_octaves(notes: list[Note], lo: int = 48, hi: int = 72) -> int:
    """Choose one whole-song octave shift so the median pitched MIDI lands in [lo, hi].

    UltraStar scores octave-independently, so charters are octave-sloppy; nominal
    ``midi = 60 + pitch`` can land anywhere. A single global shift preserves every
    interval while putting the melody in a plausible vocal register (C3–C5). Returns
    the shift in semitones (multiple of 12).
    """
    pitched = [60 + n.pitch for n in notes if n.pitched]
    if not pitched:
        return 0
    med = median(pitched)
    shift = 12 * round((60 - med) / 12.0)
    # round() targets the register center; nudge if the median still fell outside.
    while med + shift < lo:
        shift += 12
    while med + shift > hi:
        shift -= 12
    return shift


def merge_melisma(lines: list[Line]) -> list[Line]:
    """Merge ``~`` continuation notes into their parent syllable.

    The parent keeps its pitch and text; its duration extends to the end of the last
    continuation. Continuations with no preceding syllable in the same line are
    dropped. Returns new Line objects; input Notes are not mutated except the parent
    copies (callers keep the original for later contour work).
    """
    merged_lines: list[Line] = []
    for line in lines:
        out: list[Note] = []
        for n in line.notes:
            if n.is_continuation and out:
                parent = out[-1]
                parent.d = (n.t + n.d) - parent.t
                parent.length_beats = (n.start_beat + n.length_beats) - parent.start_beat
            elif not n.is_continuation:
                out.append(
                    Note(
                        type=n.type, start_beat=n.start_beat,
                        length_beats=n.length_beats, pitch=n.pitch, text=n.text,
                        player=n.player, t=n.t, d=n.d, midi=n.midi,
                    )
                )
            # else: leading continuation with no parent — drop
        merged_lines.append(Line(notes=out))
    return merged_lines


def parse_text(text: str, encoding_used: str = "utf-8") -> Song:
    """Parse chart text into a Song. Raises UltraStarError on missing essentials."""
    headers: dict[str, str] = {}
    warnings: list[str] = []
    # Raw parse pass: collect (player, Note) and line-break beats per player.
    raw_notes: dict[int, list[Note]] = {}
    breaks: dict[int, list[int]] = {}
    current_players = [1]
    rel_offset = {1: 0, 2: 0, 3: 0}
    relative = False
    header_done = False

    lines_in = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Headers first (they may technically appear anywhere before notes; we accept
    # them until the first note line).
    body_start = 0
    for i, raw in enumerate(lines_in):
        stripped = raw.strip("﻿").rstrip()
        if not stripped:
            continue
        hm = _HEADER_RE.match(stripped)
        if hm and not header_done:
            headers[hm.group(1).strip().upper()] = hm.group(2).strip()
            body_start = i + 1
            continue
        header_done = True

    if "BPM" not in headers:
        raise UltraStarError("missing #BPM header")
    try:
        bpm = _parse_number(headers["BPM"])
    except ValueError as exc:
        raise UltraStarError(f"unparseable #BPM: {headers['BPM']!r}") from exc
    if bpm <= 0:
        raise UltraStarError(f"non-positive #BPM: {bpm}")
    gap_ms = 0.0
    if headers.get("GAP"):
        try:
            gap_ms = _parse_number(headers["GAP"])
        except ValueError:
            warnings.append(f"unparseable #GAP {headers['GAP']!r}; assuming 0")
    relative = headers.get("RELATIVE", "").strip().upper() == "YES"

    def _add_break(player: int, beat: int) -> None:
        breaks.setdefault(player, []).append(beat)

    for lineno, raw in enumerate(lines_in[body_start:], start=body_start + 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "E":
            break

        pm = _PLAYER_RE.match(stripped)
        if pm:
            p = int(pm.group(1))
            if p == 3:
                current_players = [1, 2]
            elif p in (1, 2):
                current_players = [p]
            else:
                warnings.append(f"line {lineno}: unknown player marker {stripped!r}")
                current_players = [1]
            continue

        bm = _LINEBREAK_RE.match(stripped)
        if bm:
            b1 = int(bm.group(1))
            b2 = int(bm.group(2)) if bm.group(2) is not None else b1
            for p in current_players:
                _add_break(p, rel_offset[p] + b1)
                if relative:
                    rel_offset[p] += b2
            continue

        # Match note lines against the un-right-stripped text: trailing spaces in
        # syllable text are a legitimate word-boundary convention.
        nm = _NOTE_RE.match(raw.lstrip())
        if nm:
            ntype, sb, lb, pitch = nm.group(1), int(nm.group(2)), int(nm.group(3)), int(nm.group(4))
            ntext = nm.group(5) if nm.group(5) is not None else ""
            for p in current_players:
                note = Note(
                    type=ntype,
                    start_beat=rel_offset[p] + sb,
                    length_beats=lb,
                    pitch=pitch,
                    text=ntext,
                    player=p,
                    is_continuation=ntext.strip() in ("~", ""),
                )
                # An empty-text note is only a continuation if it is pitched;
                # keep genuinely empty unpitched notes out entirely.
                if note.text.strip() == "" and not note.pitched:
                    continue
                raw_notes.setdefault(p, []).append(note)
            continue

        warnings.append(f"line {lineno}: unrecognized line {stripped!r}")

    if not raw_notes:
        raise UltraStarError("chart contains no note lines")

    # Normalize per player: absolute seconds, octave fold, split into phrases.
    players: dict[int, list[Line]] = {}
    octave_shift: dict[int, int] = {}
    for p, notes in raw_notes.items():
        notes.sort(key=lambda n: n.start_beat)
        shift = fold_octaves(notes)
        octave_shift[p] = shift
        for n in notes:
            n.t = beat_to_seconds(n.start_beat, bpm, gap_ms)
            n.d = n.length_beats * 60.0 / (bpm * 4.0)
            if n.pitched:
                n.midi = max(0, min(127, 60 + n.pitch + shift))

        phrase_breaks = sorted(breaks.get(p, []))
        lines_out: list[Line] = []
        cur = Line()
        bi = 0
        for n in notes:
            while bi < len(phrase_breaks) and n.start_beat >= phrase_breaks[bi]:
                if cur.notes:
                    lines_out.append(cur)
                    cur = Line()
                bi += 1
            cur.notes.append(n)
        if cur.notes:
            lines_out.append(cur)
        players[p] = lines_out

    return Song(
        headers=headers,
        players=players,
        bpm=bpm,
        gap_ms=gap_ms,
        relative=relative,
        encoding_used=encoding_used,
        octave_shift=octave_shift,
        warnings=warnings,
    )


def parse_file(path: str | Path) -> Song:
    """Read and parse an UltraStar .txt chart file."""
    data = Path(path).read_bytes()
    text, enc = decode_chart_bytes(data)
    return parse_text(text, encoding_used=enc)
