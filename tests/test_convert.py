"""Unit tests for convert.py's pure builders (no audio needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convert import (  # noqa: E402
    build_lyrics,
    build_notation,
    build_vocal_pitch,
    display_bpm,
    fallback_title_artist,
    language_tag,
    pick_chart,
    quantize_dur,
)
from ultrastar_parse import merge_melisma, parse_text  # noqa: E402

CHART = """\
#TITLE:Test
#ARTIST:Artist
#LANGUAGE:English
#BPM:400
#GAP:2000
: 0 4 0 Hel
: 4 4 2 lo
: 12 4 4  world
- 20
: 24 4 0 next
: 28 2 4 ~
F 32 4 0  line
E
"""


def _lines():
    return merge_melisma(parse_text(CHART).lines)


def test_build_lyrics_suffixes():
    entries = build_lyrics(_lines())
    words = [e["w"] for e in entries]
    # 'Hel' joins 'lo' (no boundary) → 'Hel-'; 'lo' precedes ' world' (boundary);
    # 'world' ends line 1 → '+'; melisma merged 'next' precedes ' line' (boundary);
    # freestyle keeps its lyrics entry; 'line' ends line 2 → '+'.
    assert words == ["Hel-", "lo", "world+", "next", "line+"]


def test_build_lyrics_strips_tilde_prefix():
    # '~ned'-style continuation-with-text: tilde is a vowel-hold marker, not text.
    text = "#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\n: 0 2 0 rai\n: 3 2 4 ~ned\n: 6 2 0  down\nE\n"
    entries = build_lyrics(merge_melisma(parse_text(text).lines))
    assert [e["w"] for e in entries] == ["rai-", "ned", "down+"]


def test_build_lyrics_timing():
    entries = build_lyrics(_lines())
    assert entries[0]["t"] == pytest.approx(2.0)
    assert entries[0]["d"] == pytest.approx(4 * 0.0375, abs=1e-3)
    # Melisma-merged syllable spans beats 24..30 (6 beats).
    assert entries[3]["d"] == pytest.approx(6 * 0.0375, abs=1e-3)


def test_build_vocal_pitch_skips_unpitched():
    vp = build_vocal_pitch(_lines())
    assert vp["version"] == 1
    # 4 pitched syllables (freestyle ' line' excluded).
    assert len(vp["notes"]) == 4
    assert all(0 <= n["midi"] <= 127 for n in vp["notes"])
    # vocal_pitch may be shorter than lyrics — spec-allowed.
    assert len(vp["notes"]) < len(build_lyrics(_lines()))


def test_vocal_pitch_mirrors_lyrics_timing():
    lyr = build_lyrics(_lines())
    vp = build_vocal_pitch(_lines())
    assert [n["t"] for n in vp["notes"]] == [e["t"] for e in lyr[:4]]


def test_display_bpm_folds_into_range():
    assert display_bpm(395.98) == pytest.approx(98.995)
    assert display_bpm(108.0) == pytest.approx(108.0)
    assert display_bpm(255.68) == pytest.approx(127.84)
    assert display_bpm(801.0) == pytest.approx(100.125)
    assert display_bpm(60.0) == pytest.approx(60.0)


def test_quantize_dur_exact():
    whole = 2.0  # whole note = 2 s
    assert quantize_dur(0.5, whole) == (4, None)      # quarter
    assert quantize_dur(0.25, whole) == (8, None)     # eighth
    assert quantize_dur(0.75, whole) == (4, 1)        # dotted quarter
    assert quantize_dur(2.0, whole) == (1, None)      # whole


def test_quantize_dur_degenerate():
    assert quantize_dur(0.0, 2.0) == (32, None)
    assert quantize_dur(0.5, 0.0) == (32, None)


def test_build_notation_shape():
    song = parse_text(CHART)
    notation = build_notation(song, _lines())
    assert notation["version"] == 1
    assert notation["instrument"] == "vocals"
    assert notation["staves"] == [{"id": "voice", "clef": "G2", "label": "Vocals"}]
    ms = notation["measures"]
    assert ms and ms[0]["idx"] == 1
    assert ms[0]["ts"] == [4, 4] and ms[0]["ks"] == 0 and ms[0]["tempo"] > 0
    assert ms[0]["t"] == pytest.approx(2.0)  # grid starts at GAP
    # Monotonic idx, valid durs, all beats carry exactly one midi note.
    seen_beats = 0
    for i, m in enumerate(ms):
        assert m["idx"] == i + 1
        for voice in m.get("staves", {}).get("voice", {}).get("voices", []):
            for b in voice["beats"]:
                assert b["dur"] in (1, 2, 4, 8, 16, 32)
                assert len(b["notes"]) == 1
                assert 0 <= b["notes"][0]["midi"] <= 127
                seen_beats += 1
    assert seen_beats == 4  # pitched syllables only


def test_build_notation_empty_pitch():
    song = parse_text("#TITLE:t\n#ARTIST:a\n#BPM:400\n#GAP:0\nF 0 2 0 rap\nE\n")
    notation = build_notation(song, merge_melisma(song.lines))
    assert notation["measures"] == []


def test_language_tag():
    assert language_tag({"LANGUAGE": "English"}) == "en"
    assert language_tag({"LANGUAGE": "Spanish"}) == "es"
    assert language_tag({"LANGUAGE": "English, Spanish"}) == "en"
    assert language_tag({"LANGUAGE": "Klingon"}) is None
    assert language_tag({}) is None


def test_fallback_title_artist():
    song = parse_text("#BPM:400\n#GAP:0\n: 0 2 0 x\nE\n")
    chart = Path("songs") / "Some Artist - Some Title" / "Some Artist - Some Title.txt"
    title, artist = fallback_title_artist(chart, song)
    assert title == "Some Title"
    assert artist == "Some Artist"


def test_pick_chart_prefers_solo(tmp_path):
    (tmp_path / "Song [MULTI].txt").write_text("x")
    (tmp_path / "Song.txt").write_text("x")
    assert pick_chart(tmp_path).name == "Song.txt"


def test_pick_chart_single(tmp_path):
    (tmp_path / "Only.txt").write_text("x")
    assert pick_chart(tmp_path).name == "Only.txt"
