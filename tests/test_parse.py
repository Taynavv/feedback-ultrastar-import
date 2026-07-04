"""Unit tests for ultrastar_parse — synthetic fixtures covering every corpus gotcha."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultrastar_parse import (  # noqa: E402
    Note,
    UltraStarError,
    beat_to_seconds,
    decode_chart_bytes,
    fold_octaves,
    merge_melisma,
    parse_file,
    parse_text,
)

# A real UltraStar library enables the full-corpus smoke test; content is read in
# place and never copied. Absent → those tests skip.
_corpus_env = os.environ.get("ULTRASTAR_SONGS", "")
CORPUS = Path(_corpus_env) if _corpus_env else None

MINIMAL = """\
#TITLE:Test Song
#ARTIST:Test Artist
#BPM:400
#GAP:2000
: 0 4 0 Hel
: 4 4 2 lo
- 10
: 12 4 4  world
E
"""


def test_minimal_headers():
    song = parse_text(MINIMAL)
    assert song.title == "Test Song"
    assert song.artist == "Test Artist"
    assert song.bpm == 400.0
    assert song.gap_ms == 2000.0


def test_minimal_timing():
    song = parse_text(MINIMAL)
    notes = song.all_notes()
    # seconds = GAP/1000 + beat * 60 / (BPM * 4); one beat = 60/1600 = 0.0375 s
    assert notes[0].t == pytest.approx(2.0)
    assert notes[0].d == pytest.approx(4 * 0.0375)
    assert notes[1].t == pytest.approx(2.0 + 4 * 0.0375)
    assert notes[2].t == pytest.approx(2.0 + 12 * 0.0375)


def test_minimal_line_structure():
    song = parse_text(MINIMAL)
    assert len(song.lines) == 2
    assert [n.text for n in song.lines[0].notes] == ["Hel", "lo"]
    assert [n.text for n in song.lines[1].notes] == [" world"]


def test_midi_no_fold_needed():
    song = parse_text(MINIMAL)
    notes = song.all_notes()
    # Median of 60, 62, 64 is 62 — already in register, no shift.
    assert song.octave_shift[1] == 0
    assert [n.midi for n in notes] == [60, 62, 64]


def test_comma_decimal_bpm_and_gap():
    text = MINIMAL.replace("#BPM:400", "#BPM:395,98").replace("#GAP:2000", "#GAP:2000,5")
    song = parse_text(text)
    assert song.bpm == pytest.approx(395.98)
    assert song.gap_ms == pytest.approx(2000.5)


def test_bpm_trailing_space():
    # Real corpus: '#BPM:285.80 ' (trailing space)
    song = parse_text(MINIMAL.replace("#BPM:400", "#BPM:285.80 "))
    assert song.bpm == pytest.approx(285.80)


def test_missing_gap_defaults_zero():
    text = "\n".join(l for l in MINIMAL.splitlines() if not l.startswith("#GAP")) + "\n"
    song = parse_text(text)
    assert song.gap_ms == 0.0
    assert song.all_notes()[0].t == pytest.approx(0.0)


def test_missing_bpm_raises():
    text = "\n".join(l for l in MINIMAL.splitlines() if not l.startswith("#BPM")) + "\n"
    with pytest.raises(UltraStarError):
        parse_text(text)


def test_no_notes_raises():
    with pytest.raises(UltraStarError):
        parse_text("#TITLE:x\n#ARTIST:y\n#BPM:400\nE\n")


def test_octave_fold_high():
    # Chart written an octave or two high: pitches ~24-28 → midi 84-88 → fold down.
    text = MINIMAL.replace(" 0 Hel", " 24 Hel").replace(" 2 lo", " 26 lo").replace(" 4  world", " 28  world")
    song = parse_text(text)
    assert song.octave_shift[1] == -24
    assert [n.midi for n in song.all_notes()] == [60, 62, 64]


def test_octave_fold_preserves_intervals():
    text = MINIMAL.replace(" 0 Hel", " -30 Hel").replace(" 2 lo", " -28 lo").replace(" 4  world", " -26 world")
    song = parse_text(text)
    notes = song.all_notes()
    assert notes[1].midi - notes[0].midi == 2
    assert notes[2].midi - notes[1].midi == 2
    assert 48 <= sorted(n.midi for n in notes)[1] <= 72


def test_fold_outlier_clamped():
    # Corpus has pitches up to 237; midi must stay within 0..127.
    text = MINIMAL.replace(" 4  world", " 237  world")
    song = parse_text(text)
    assert all(0 <= n.midi <= 127 for n in song.all_notes() if n.midi is not None)


def test_golden_is_pitched_freestyle_rap_not():
    text = """\
#TITLE:t
#ARTIST:a
#BPM:400
#GAP:0
: 0 2 0 la
* 2 2 2 GOLD
F 4 2 0 free
R 6 2 0 rap
G 8 2 0 grap
E
"""
    song = parse_text(text)
    notes = song.all_notes()
    assert [n.type for n in notes] == [":", "*", "F", "R", "G"]
    assert notes[1].midi is not None          # golden keeps pitch
    assert [n.midi for n in notes[2:]] == [None, None, None]  # F/R/G unpitched


def test_melisma_merge():
    text = """\
#TITLE:t
#ARTIST:a
#BPM:400
#GAP:0
: 0 2 0 glad
: 3 2 4 ~
: 6 2 0  to
E
"""
    song = parse_text(text)
    merged = merge_melisma(song.lines)
    assert [n.text for n in merged[0].notes] == ["glad", " to"]
    # Parent extends to the end of the continuation: beats 0..5 → 5 beats.
    assert merged[0].notes[0].d == pytest.approx(5 * 60.0 / (400 * 4))
    # Parent keeps its own (first) pitch.
    assert merged[0].notes[0].midi == 60
    # Originals are not mutated.
    assert song.lines[0].notes[0].d == pytest.approx(2 * 60.0 / (400 * 4))


def test_leading_melisma_dropped():
    text = """\
#TITLE:t
#ARTIST:a
#BPM:400
#GAP:0
: 0 2 0 ~
: 3 2 0 word
E
"""
    song = parse_text(text)
    merged = merge_melisma(song.lines)
    assert [n.text for n in merged[0].notes] == ["word"]


def test_duet_players():
    text = """\
#TITLE:t
#ARTIST:a
#BPM:400
#GAP:0
P1
: 0 2 0 one
- 4
P2
: 8 2 2 two
P3
: 16 2 4 both
E
"""
    song = parse_text(text)
    assert song.is_duet
    assert [n.text for n in song.all_notes(1)] == ["one", "both"]
    assert [n.text for n in song.all_notes(2)] == ["two", "both"]


def test_duet_player_marker_with_space():
    # Real corpus uses 'P 1' / 'P 2' in one chart.
    text = "#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\nP 1\n: 0 2 0 one\nP 2\n: 4 2 0 two\nE\n"
    song = parse_text(text)
    assert [n.text for n in song.all_notes(2)] == ["two"]


def test_relative_mode():
    text = """\
#TITLE:t
#ARTIST:a
#BPM:400
#GAP:0
#RELATIVE:YES
: 0 2 0 one
: 4 2 0 two
- 8 10
: 0 2 0 three
- 6 6
: 2 2 0 four
E
"""
    song = parse_text(text)
    notes = song.all_notes()
    assert [n.text for n in notes] == ["one", "two", "three", "four"]
    assert [n.start_beat for n in notes] == [0, 4, 10, 18]
    assert len(song.lines) == 3


def test_relative_single_number_break():
    text = "#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\n#RELATIVE:YES\n: 0 2 0 one\n- 8\n: 0 2 0 two\nE\n"
    song = parse_text(text)
    assert [n.start_beat for n in song.all_notes()] == [0, 8]


def test_absolute_two_number_break():
    # 425 corpus line-breaks carry a second number without #RELATIVE — it must NOT shift beats.
    text = "#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\n: 0 2 0 one\n- 4 6\n: 8 2 0 two\nE\n"
    song = parse_text(text)
    assert [n.start_beat for n in song.all_notes()] == [0, 8]
    assert len(song.lines) == 2


def test_stops_at_e_marker():
    song = parse_text(MINIMAL + ": 99 4 0 ghost\n")
    assert all(n.text != "ghost" for n in song.all_notes())


def test_negative_pitch():
    song = parse_text(MINIMAL.replace(" 0 Hel", " -3 Hel"))
    assert song.all_notes()[0].midi == 57


def test_trailing_space_syllable_preserved():
    # Word boundary marked by trailing space on the previous syllable.
    text = "#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\n: 0 2 0 one \n: 4 2 0 two\nE\n"
    song = parse_text(text)
    assert song.all_notes()[0].text == "one "


def test_decode_utf8_bom():
    data = "﻿#TITLE:Boom\n#ARTIST:a\n#BPM:400\n: 0 2 0 x\nE\n".encode("utf-8-sig")
    text, enc = decode_chart_bytes(data)
    song = parse_text(text, enc)
    assert song.title == "Boom"
    assert song.encoding_used == "utf-8-sig"


def test_decode_cp1252_fallback():
    raw = "#TITLE:Céline\n#ARTIST:a\n#BPM:400\n: 0 2 0 é\nE\n".encode("cp1252")
    text, enc = decode_chart_bytes(raw)
    assert enc == "cp1252"
    song = parse_text(text, enc)
    assert song.title == "Céline"
    assert song.all_notes()[0].text == "é"


def test_decode_declared_encoding():
    raw = "#ENCODING:CP1252\n#TITLE:Céline\n#ARTIST:a\n#BPM:400\n: 0 2 0 x\nE\n".encode("cp1252")
    text, enc = decode_chart_bytes(raw)
    assert enc == "cp1252"
    assert parse_text(text, enc).title == "Céline"


def test_decode_utf8_wins_when_valid():
    raw = "#TITLE:Müller\n#ARTIST:a\n#BPM:400\n: 0 2 0 x\nE\n".encode("utf-8")
    text, enc = decode_chart_bytes(raw)
    assert enc == "utf-8"
    assert parse_text(text, enc).title == "Müller"


def test_unrecognized_line_warns_but_parses():
    song = parse_text(MINIMAL.replace("- 10", "- 10\nTHIS IS JUNK"))
    assert song.warnings
    assert len(song.all_notes()) == 3


def test_beat_to_seconds():
    assert beat_to_seconds(0, 400, 2000) == pytest.approx(2.0)
    assert beat_to_seconds(1600, 400, 0) == pytest.approx(60.0)


def test_fold_octaves_empty():
    assert fold_octaves([]) == 0
    assert fold_octaves([Note(type="F", start_beat=0, length_beats=1, pitch=0, text="x")]) == 0


# ---------------------------------------------------------------------------
# Corpus smoke test — parses every real chart; never copies content anywhere.
# ---------------------------------------------------------------------------

corpus_charts = sorted(CORPUS.glob("*/*.txt")) if CORPUS and CORPUS.is_dir() else []


@pytest.mark.skipif(not corpus_charts, reason="UltraStar corpus not present")
def test_corpus_smoke():
    failures = []
    for chart in corpus_charts:
        try:
            song = parse_file(chart)
            assert song.all_notes(), "no notes for player 1"
            assert song.bpm > 0
            # Every pitched note must have folded into MIDI range.
            for p in song.players:
                for n in song.all_notes(p):
                    if n.pitched:
                        assert n.midi is not None and 0 <= n.midi <= 127
                    assert n.d >= 0
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{chart.parent.name}: {exc}")
    assert not failures, "\n".join(failures)


@pytest.mark.skipif(not corpus_charts, reason="UltraStar corpus not present")
def test_corpus_count():
    # A collapse in glob results would silently weaken the smoke test, so require
    # that a configured corpus actually yields a meaningful number of charts.
    assert len(corpus_charts) >= 50
