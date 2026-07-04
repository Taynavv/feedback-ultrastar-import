"""Unit tests for merge.py — offset recovery, refusal, matching, pak rewrite.

The end-to-end test builds a synthetic 'recording' (a distinctive click pattern),
a fake pak whose audio is the same recording behind extra leading silence, and an
UltraStar chart — then checks the estimated offset, the verdict, and the merged
pak's contents. No real song content is used anywhere.
"""
import json
import struct
import sys
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convert import ConvertError, find_tool  # noqa: E402
from merge import (  # noqa: E402
    HOP,
    SR,
    MergeError,
    Segment,
    analyze,
    apply_merge,
    build_segments,
    estimate_offset,
    merge,
    normalize,
    offset_at,
    onset_envelope,
    scan_candidates,
)

try:
    find_tool("ffmpeg")
    HAVE_FFMPEG = True
except ConvertError:
    HAVE_FFMPEG = False


# ---------------------------------------------------------------------------
# Pure-numpy units
# ---------------------------------------------------------------------------

def _spike_env(n: int, seed: int, density: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    env = np.zeros(n, dtype=np.float64)
    idx = rng.choice(n, size=max(3, int(n * density)), replace=False)
    env[idx] = rng.uniform(0.5, 2.0, size=len(idx))
    return (env - env.mean()) / env.std()


def test_estimate_offset_recovers_known_shift():
    hop_s = HOP / SR
    base = _spike_env(6000, seed=1)
    shift = 130  # frames — pak audio starts ~3 s later
    pak = np.concatenate([np.zeros(shift), base])
    offset, peak_z, ratio = estimate_offset(pak, base, hop_s=hop_s)
    assert offset == pytest.approx(shift * hop_s, abs=2 * hop_s)
    assert peak_z > 20
    assert ratio > 1.2


def test_estimate_offset_negative_shift():
    hop_s = HOP / SR
    base = _spike_env(6000, seed=2)
    pak = base[100:]  # pak audio starts ~2.3 s EARLIER than the ultrastar mp3
    offset, peak_z, _ = estimate_offset(pak, base, hop_s=hop_s)
    assert offset == pytest.approx(-100 * hop_s, abs=2 * hop_s)
    assert peak_z > 20


def test_estimate_offset_unrelated_is_weak():
    a = _spike_env(6000, seed=3)
    b = _spike_env(6000, seed=4)
    _, peak_z, _ = estimate_offset(a, b)
    assert peak_z < 8  # below Z_REVIEW — would be refused


def test_onset_envelope_click():
    samples = np.zeros(SR, dtype=np.float32)
    samples[SR // 2: SR // 2 + 400] = 0.9
    env = onset_envelope(samples)
    assert int(np.argmax(env)) == pytest.approx((SR // 2) / HOP, abs=2)


def test_normalize():
    assert normalize("Imagine Dragons") == normalize("  imagine   DRAGONS!! ")
    assert normalize("Céline Dion") == normalize("Celine Dion")
    assert normalize("AC/DC") == normalize("ac dc")


def test_build_segments_two_edits():
    # Confident windows at +2.0 s until t=100, then at -8.0 s — one weak outlier
    # in the transition that must not create a segment of its own.
    rows = ([(t, 2.0, 9.0) for t in range(0, 100, 5)]
            + [(102.5, -3.0, 2.0)]                       # weak — ignored
            + [(t, -8.0, 9.0) for t in range(130, 200, 5)])
    segs = build_segments(rows, fallback_offset=0.0)
    assert len(segs) == 2
    assert segs[0].offset == pytest.approx(2.0)
    assert segs[1].offset == pytest.approx(-8.0)
    assert segs[0].us_start == float("-inf")
    assert segs[1].us_end == float("inf")
    # Covered spans map cleanly; the gap between them is ambiguous.
    assert offset_at(segs, 50.0) == (pytest.approx(2.0), False)
    assert offset_at(segs, 150.0) == (pytest.approx(-8.0), False)
    # Inside the uncovered transition (110..130): ambiguous, mapped to nearest side.
    off, amb = offset_at(segs, 112.0)
    assert amb and off == pytest.approx(2.0)
    off, amb = offset_at(segs, 128.0)
    assert amb and off == pytest.approx(-8.0)


def test_build_segments_constant():
    rows = [(t, 3.9, 10.0) for t in range(0, 200, 5)]
    segs = build_segments(rows, fallback_offset=3.9)
    assert len(segs) == 1
    assert offset_at(segs, 12345.0) == (pytest.approx(3.9), False)


def test_build_segments_no_confident_windows():
    segs = build_segments([(0.0, 1.0, 2.0)], fallback_offset=1.5)
    assert segs == [Segment(float("-inf"), float("inf"), 1.5)]


# ---------------------------------------------------------------------------
# End-to-end synthetic merge
# ---------------------------------------------------------------------------

CHART = """\
#TITLE:Click Track
#ARTIST:Synthetic
#LANGUAGE:English
#BPM:400
#GAP:1000
: 0 4 0 one
: 8 4 2 two
- 16
: 24 4 4 three
: 32 4 5 four
E
"""

PAK_DELAY_S = 1.5  # pak audio = same recording with this much extra leading silence


def _click_signal(seconds: float, seed: int = 7,
                  clicks_at: tuple[float, ...] = ()) -> np.ndarray:
    """A distinctive aperiodic click pattern — sharply correlatable. Clicks are
    additionally placed at `clicks_at` (the chart's syllable times, so the
    onset-alignment score has something real to measure)."""
    rng = np.random.default_rng(seed)
    sig = np.zeros(int(seconds * SR), dtype=np.float32)
    for t in clicks_at:
        i = int(t * SR)
        sig[i:i + 300] = 0.9
    t = max(clicks_at) + 0.8 if clicks_at else 0.3
    while t < seconds - 0.1:
        i = int(t * SR)
        sig[i:i + 300] = rng.uniform(0.5, 0.95)
        t += rng.uniform(0.15, 0.6)
    return sig


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


@pytest.fixture(scope="module")
def synthetic_pair(tmp_path_factory):
    """(ultrastar song dir, fake pak) sharing one 'recording'."""
    root = tmp_path_factory.mktemp("merge_e2e")
    # Chart syllables (UltraStar timeline): GAP 1.0 s + beats {0,8,24,32} * 0.0375 s.
    recording = _click_signal(18.0, clicks_at=(1.0, 1.3, 1.9, 2.2))

    song_dir = root / "Synthetic - Click Track"
    song_dir.mkdir()
    (song_dir / "song.txt").write_text(
        CHART.replace("#GAP:1000", "#GAP:1000\n#MP3:song.wav"), encoding="utf-8")
    _write_wav(song_dir / "song.wav", recording)

    pak_audio = np.concatenate(
        [np.zeros(int(PAK_DELAY_S * SR), dtype=np.float32), recording])
    pak = root / "Click_Track_Synthetic.feedpak"
    manifest = {
        "title": "Click Track", "artist": "Synthetic",
        "duration": round(len(pak_audio) / SR, 3),
        "arrangements": [{"id": "lead", "name": "Lead",
                          "file": "arrangements/lead.json"}],
        "stems": [{"id": "full", "file": "stems/full.wav", "default": True}],
        "_producer": "synthetic-test",
    }
    wav_tmp = root / "full.wav"
    _write_wav(wav_tmp, pak_audio)
    with zipfile.ZipFile(pak, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        z.writestr("arrangements/lead.json", json.dumps(
            {"notes": [], "chords": [], "anchors": [], "handshapes": [],
             "templates": []}))
        z.write(wav_tmp, "stems/full.wav")
    return song_dir, pak


pytestmark_e2e = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")


@pytestmark_e2e
def test_analyze_synthetic(synthetic_pair):
    song_dir, pak = synthetic_pair
    report, song, lines = analyze(song_dir, pak, log=lambda m: None)
    assert report.offset == pytest.approx(PAK_DELAY_S, abs=0.05)
    assert report.verdict == "accept"
    assert report.peak_z > 12
    # First syllable: GAP 1.0 s + offset 1.5 s.
    assert lines[0].notes[0].t == pytest.approx(2.5, abs=0.06)


@pytestmark_e2e
def test_merge_refuses_unrelated(synthetic_pair, tmp_path):
    _, pak = synthetic_pair
    other = tmp_path / "Other - Song"
    other.mkdir()
    other_audio = _click_signal(18.0, seed=99)
    (other / "song.txt").write_text(
        CHART.replace("Click Track", "Song").replace("Synthetic", "Other")
             .replace("#GAP:1000", "#GAP:1000\n#MP3:song.wav"),
        encoding="utf-8")
    _write_wav(other / "song.wav", other_audio)
    with pytest.raises(MergeError, match="refus|uncertain"):
        merge(other, pak, apply=True, log=lambda m: None)
    # Dry run on the same pair must not raise.
    report = merge(other, pak, apply=False, log=lambda m: None)
    assert report.verdict in ("refuse", "review")


@pytestmark_e2e
def test_apply_merge_synthetic(synthetic_pair):
    song_dir, pak = synthetic_pair
    report, song, lines = analyze(song_dir, pak, log=lambda m: None)
    apply_merge(song_dir, pak, report, song, lines, log=lambda m: None)

    backup = pak.with_suffix(pak.suffix + ".pre-merge.bak")
    assert backup.exists()

    with zipfile.ZipFile(pak) as z:
        names = set(z.namelist())
        manifest = yaml.safe_load(z.read("manifest.yaml"))
        lyrics = json.loads(z.read("lyrics.json"))
        vp = json.loads(z.read("vocal_pitch.json"))
        notation = json.loads(z.read("notation_vocals.json"))
    # Original content preserved (spec round-trip rule).
    assert "arrangements/lead.json" in names
    assert "stems/full.wav" in names
    assert manifest["_producer"] == "synthetic-test"
    assert manifest["arrangements"][0]["id"] == "lead"
    # Vocal graft present and time-shifted.
    ids = [a["id"] for a in manifest["arrangements"]]
    assert ids == ["lead", "vocals"]
    assert manifest["lyrics"] == "lyrics.json"
    assert manifest["vocal_pitch"] == "vocal_pitch.json"
    assert manifest["lyric_tracks"][0]["kind"] == "original"
    assert [e["w"] for e in lyrics] == ["one-", "two+", "three-", "four+"]
    assert lyrics[0]["t"] == pytest.approx(2.5, abs=0.06)
    assert len(vp["notes"]) == 4
    assert notation["measures"][0]["t"] == pytest.approx(2.5, abs=0.06)


@pytestmark_e2e
def test_restore_roundtrip(tmp_path):
    import shutil

    from merge import backup_path, restore
    import merge as merge_mod

    # Build an isolated pak + chart pair (independent of the shared fixture).
    recording = _click_signal(18.0, clicks_at=(1.0, 1.3, 1.9, 2.2))
    song_dir = tmp_path / "Synthetic - Click Track"
    song_dir.mkdir()
    (song_dir / "song.txt").write_text(
        CHART.replace("#GAP:1000", "#GAP:1000\n#MP3:song.wav"), encoding="utf-8")
    _write_wav(song_dir / "song.wav", recording)
    pak_audio = np.concatenate(
        [np.zeros(int(PAK_DELAY_S * SR), dtype=np.float32), recording])
    pak = tmp_path / "Click_Track_Synthetic.feedpak"
    wav_tmp = tmp_path / "full.wav"
    _write_wav(wav_tmp, pak_audio)
    manifest = {"title": "Click Track", "artist": "Synthetic",
                "duration": round(len(pak_audio) / SR, 3),
                "arrangements": [{"id": "lead", "name": "Lead",
                                  "file": "arrangements/lead.json"}],
                "stems": [{"id": "full", "file": "stems/full.wav", "default": True}]}
    with zipfile.ZipFile(pak, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        z.writestr("arrangements/lead.json", json.dumps(
            {"notes": [], "chords": [], "anchors": [], "handshapes": [],
             "templates": []}))
        z.write(wav_tmp, "stems/full.wav")
    original = pak.read_bytes()

    merge_mod.merge(song_dir, pak, apply=True, log=lambda m: None)
    assert backup_path(pak).is_file()
    assert pak.read_bytes() != original

    restore(pak, log=lambda m: None)
    assert pak.read_bytes() == original          # byte-identical undo
    assert not backup_path(pak).is_file()        # backup consumed

    with pytest.raises(MergeError, match="no backup"):
        restore(pak, log=lambda m: None)

    # CLI form.
    shutil.copyfile(pak, backup_path(pak))
    assert merge_mod.main([str(pak), "--restore"]) == 0
    assert not backup_path(pak).is_file()


@pytestmark_e2e
def test_apply_merge_is_idempotent(synthetic_pair):
    """Re-merging replaces the vocals arrangement instead of duplicating it."""
    song_dir, pak = synthetic_pair
    report, song, lines = analyze(song_dir, pak, log=lambda m: None)
    apply_merge(song_dir, pak, report, song, lines, log=lambda m: None)
    with zipfile.ZipFile(pak) as z:
        manifest = yaml.safe_load(z.read("manifest.yaml"))
    assert [a["id"] for a in manifest["arrangements"]] == ["lead", "vocals"]
    assert len(manifest["lyric_tracks"]) == 1


PIECE_CHART = """\
#TITLE:Edit Test
#ARTIST:Synthetic
#LANGUAGE:English
#BPM:400
#GAP:2000
#MP3:song.wav
: 0 4 0 one
: 8 4 2 two
- 16
: 1014 4 4 three
: 1022 4 5 four
E
"""


@pytestmark_e2e
def test_piecewise_two_edit_merge(tmp_path):
    """US audio = pak recording with a 6 s insert at 30 s (a longer video cut).

    Syllables before the insert map with the first offset, syllables after it
    with the second; none are charted inside the insert, so verdict stays clean.
    """
    # Base 'recording' with clicks under the charted syllables on both sides:
    # us-time 2.0/2.3 (base 2.0/2.3) and us-time ~40.0/40.3 (base 34.0/34.3).
    base = _click_signal(60.0, seed=11, clicks_at=(2.0, 2.3, 34.0, 34.3))
    insert_at, insert_len, pak_lead = 30.0, 6.0, 1.0

    us_audio = np.concatenate([
        base[:int(insert_at * SR)],
        np.zeros(int(insert_len * SR), dtype=np.float32),
        base[int(insert_at * SR):],
    ])
    pak_audio = np.concatenate(
        [np.zeros(int(pak_lead * SR), dtype=np.float32), base])

    song_dir = tmp_path / "Synthetic - Edit Test"
    song_dir.mkdir()
    (song_dir / "song.txt").write_text(PIECE_CHART, encoding="utf-8")
    _write_wav(song_dir / "song.wav", us_audio)

    pak = tmp_path / "Edit_Test_Synthetic.feedpak"
    manifest = {
        "title": "Edit Test", "artist": "Synthetic",
        "duration": round(len(pak_audio) / SR, 3),
        "arrangements": [{"id": "lead", "name": "Lead",
                          "file": "arrangements/lead.json"}],
        "stems": [{"id": "full", "file": "stems/full.wav", "default": True}],
    }
    wav_tmp = tmp_path / "full.wav"
    _write_wav(wav_tmp, pak_audio)
    with zipfile.ZipFile(pak, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        z.writestr("arrangements/lead.json", json.dumps(
            {"notes": [], "chords": [], "anchors": [], "handshapes": [],
             "templates": []}))
        z.write(wav_tmp, "stems/full.wav")

    report, song, lines = analyze(song_dir, pak, log=lambda m: None)
    assert report.segments and len(report.segments) == 2
    assert report.segments[0].offset == pytest.approx(pak_lead, abs=0.06)
    assert report.segments[1].offset == pytest.approx(pak_lead - insert_len, abs=0.06)
    assert report.ambiguous == 0

    apply_merge(song_dir, pak, report, song, lines, log=lambda m: None)
    with zipfile.ZipFile(pak) as z:
        lyrics = json.loads(z.read("lyrics.json"))
    # Chart beats: 0 → us 2.0 s, 1014 → us ~40.03 s.
    assert lyrics[0]["t"] == pytest.approx(2.0 + pak_lead, abs=0.08)
    assert lyrics[2]["t"] == pytest.approx(40.025 + pak_lead - insert_len, abs=0.08)


@pytestmark_e2e
def test_scan_candidates(synthetic_pair, tmp_path):
    song_dir, pak = synthetic_pair
    songs_root = song_dir.parent
    dlc = pak.parent
    cands = scan_candidates(songs_root, dlc)
    ours = [c for c in cands if "Click_Track" in c["pak"]]
    assert ours and ours[0]["folder"] == song_dir.name
