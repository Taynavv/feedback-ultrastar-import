"""End-to-end test with zero song content: synthesize a song, convert, validate.

Audio and cover are generated with ffmpeg's lavfi test sources, so this runs
anywhere ffmpeg is installed (locally and in CI) without touching real songs.
The spec-validator step runs when a feedpak-spec checkout is available — set
FEEDPAK_SPEC_DIR, or rely on the local sibling checkout.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convert import ConvertError, convert, find_tool  # noqa: E402

try:
    FFMPEG = find_tool("ffmpeg")
except ConvertError:
    FFMPEG = None

# A feedpak-spec checkout enables the reference-validator test; absent → skip.
_spec_env = os.environ.get("FEEDPAK_SPEC_DIR", "")
SPEC_DIR = Path(_spec_env) if _spec_env else None

CHART = """\
#TITLE:Synthetic Song
#ARTIST:CI Fixture
#LANGUAGE:English
#GENRE:Test
#YEAR:2026
#MP3:song.mp3
#COVER:cover.jpg
#BPM:400
#GAP:2000
: 0 4 0 Syn
: 4 4 2 the
: 8 6 4 tic
- 16
* 20 4 7 gold
: 26 2 5 en
: 29 3 9 ~
F 34 4 0  free
- 40
: 44 4 -2 last
: 48 8 0  line
E
"""

DUET_CHART = """\
#TITLE:Duet Fixture
#ARTIST:Two Singers
#LANGUAGE:English
#DUETSINGERP1:Alice
#DUETSINGERP2:Bob
#ALBUM:Together
#CREATOR:Charter Jane
#MP3:song.mp3
#BPM:400
#GAP:0
P1
: 0 4 0 Al
: 4 4 2 ice
- 10
P2
: 12 4 4 Bob
: 16 4 5  here
E
"""

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


@pytest.fixture(scope="module")
def song_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("synthetic_song")
    (d / "song.txt").write_text(CHART, encoding="utf-8")
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=20", "-q:a", "4",
         str(d / "song.mp3")],
        check=True, capture_output=True)
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=navy:s=600x600:d=1", "-frames:v", "1",
         str(d / "cover.jpg")],
        check=True, capture_output=True)
    return d


@pytest.fixture(scope="module")
def pak(song_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "synthetic.feedpak"
    convert(song_dir, out, log=lambda m: None)
    return out


def test_pak_structure(pak):
    with zipfile.ZipFile(pak) as z:
        names = set(z.namelist())
    assert {"manifest.yaml", "lyrics.json", "vocal_pitch.json",
            "notation_vocals.json", "stems/full.ogg", "cover.jpg"} <= names


def test_manifest_contents(pak):
    with zipfile.ZipFile(pak) as z:
        manifest = yaml.safe_load(z.read("manifest.yaml"))
        lyrics = json.loads(z.read("lyrics.json"))
        vp = json.loads(z.read("vocal_pitch.json"))
    assert manifest["title"] == "Synthetic Song"
    assert manifest["artist"] == "CI Fixture"
    assert manifest["language"] == "en"
    assert manifest["year"] == 2026
    assert 19.0 < manifest["duration"] < 21.5
    arr = manifest["arrangements"][0]
    assert arr["id"] == "vocals" and arr["notation"] == "notation_vocals.json"
    assert "file" not in arr
    assert manifest["lyrics_source"] == "authored"
    # 8 lyric syllables (melisma merged into 'en', freestyle keeps its entry);
    # 8 pitched vocal_pitch notes: the '~' melisma keeps its own pitch event
    # (per-note stream, docs/vocal-tracks.md §4.2); freestyle emits none.
    assert len(lyrics) == 8
    assert len(vp["notes"]) == 8
    assert [e["w"] for e in lyrics][:3] == ["Syn-", "the-", "tic+"]


@pytest.fixture(scope="module")
def duet_pak(tmp_path_factory):
    d = tmp_path_factory.mktemp("duet_song")
    (d / "song.txt").write_text(DUET_CHART, encoding="utf-8")
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=10", "-q:a", "4",
         str(d / "song.mp3")],
        check=True, capture_output=True)
    out = tmp_path_factory.mktemp("duet_out") / "duet.feedpak"
    convert(d, out, log=lambda m: None)
    return out


def test_duet_vocal_tracks(duet_pak):
    with zipfile.ZipFile(duet_pak) as z:
        names = set(z.namelist())
        manifest = yaml.safe_load(z.read("manifest.yaml"))
    # Per-voice side-files present; the primary voice reuses the singular names.
    assert {"lyrics.json", "vocal_pitch.json",
            "lyrics_p2.json", "vocal_pitch_p2.json"} <= names
    # Metadata that used to be dropped is now mapped.
    assert manifest["album"] == "Together"
    assert manifest["authors"] == [{"name": "Charter Jane", "role": "charter"}]
    # vocal_tracks: two voices, P1 primary + named; singular keys mirror P1.
    vt = manifest["vocal_tracks"]
    assert [v["id"] for v in vt] == ["p1", "p2"]
    assert vt[0].get("primary") is True and "primary" not in vt[1]
    assert vt[0]["name"] == "Alice" and vt[1]["name"] == "Bob"
    assert vt[0]["vocal_pitch"] == "vocal_pitch.json"
    assert vt[1]["vocal_pitch"] == "vocal_pitch_p2.json"
    assert manifest["lyrics"] == "lyrics.json"
    assert manifest["vocal_pitch"] == "vocal_pitch.json"


@pytest.mark.skipif(
    not (SPEC_DIR and (SPEC_DIR / "tools" / "validate.py").is_file()),
    reason="feedpak-spec checkout not available (set FEEDPAK_SPEC_DIR)")
def test_duet_spec_validator(duet_pak):
    proc = subprocess.run(
        [sys.executable, str(SPEC_DIR / "tools" / "validate.py"), str(duet_pak)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"validator failed:\n{proc.stdout}\n{proc.stderr}"
    assert "PASS" in proc.stdout


@pytest.mark.skipif(
    not (SPEC_DIR and (SPEC_DIR / "tools" / "validate.py").is_file()),
    reason="feedpak-spec checkout not available (set FEEDPAK_SPEC_DIR)")
def test_spec_validator(pak):
    proc = subprocess.run(
        [sys.executable, str(SPEC_DIR / "tools" / "validate.py"), str(pak)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"validator failed:\n{proc.stdout}\n{proc.stderr}"
    assert "PASS" in proc.stdout
