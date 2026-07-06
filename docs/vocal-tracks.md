# `vocal_tracks` — multi-voice, multi-language vocals for feedpak

- **Status:** plugin-space extension (proving ground for a future upstream feedpak proposal)
- **Owner:** feedback-ultrastar-import + feedback-vocals-viz
- **Targets:** feedpak spec v1.14 (extends via the §9.1 golden rule; changes nothing normative)
- **Draft date:** 2026-07-05

## 1. Summary

feedpak v1 represents a song's sung vocal as a **single** melody: one `lyrics`
pointer and one `vocal_pitch` pointer at the manifest top level (§5.1). That is
enough for a solo, but a **duet** — two singers with independent words, pitches,
and timing — has nowhere to live, and neither does a song carrying more than one
*sung language*.

This note proposes a new top-level manifest list, **`vocal_tracks[]`**, that makes
the `(lyrics, pitch)` pair **repeatable per voice**, and lets each voice carry its
own language representations. It is designed as **two orthogonal axes**:

- **singer axis** — `vocal_tracks[]`, N independent pitched vocal lines;
- **language axis** — `lyric_tracks[]` (the existing v1.11 mechanism), M text
  representations, now **voice-scoped**.

It follows the spec's golden rule (§9.1: new key → new/side files, gated on
presence), reuses the existing `lyrics.json` and `vocal_pitch.json` file shapes
unchanged, and keeps the singular `lyrics`/`vocal_pitch` keys populated so older
Readers (including FeedBack's native karaoke) keep working untouched. Every pack it
produces still passes the spec's `tools/validate.py` (`manifest.schema.json` is
`additionalProperties: true`).

## 2. Motivation

**The gap.** The singular pointers are structurally singular: `vocal_pitch` is one
path, `lyrics` is one path. `lyric_tracks[]` (v1.11) looked like the escape hatch
but its semantics are "the *same* sung line, re-scripted or translated" (`kind` ∈
`original` / `transliteration` / `translation`; an unknown kind degrades to
`translation`). It is the **language** axis, not a second singer — and §5.5 is
explicit that it deliberately **omits per-track pitch**:

> *"Per-track vocal pitch is out of scope for this version (the melody is largely
> language-independent); a future version MAY add it."*

That reasoning holds for languages (a translation shares the melody) and **breaks
for singers** (a duet's two parts are different melodies). Multi-singer is
precisely the case §5.5 left open.

**The data exists.** UltraStar duet charts carry exactly this — `P1`/`P2` parts,
per-syllable pitch and timing, `#DUETSINGERP1/P2` names — and
[`ultrastar_parse.py`](../ultrastar_parse.py) already parses every player in full.
Only conversion currently drops all but P1. So the importer is positioned to be
both the reference **writer** and (with [feedback-vocals-viz](../../feedback-vocals-viz))
the reference **reader** for this extension, validated against a real corpus.

**Why not overload an existing field.** §9.4 calls repurposing a field's meaning
"the most dangerous" change. A new key gated on presence is the safe, spec-blessed
path, and — because the manifest schema admits extension keys — it validates today.

## 3. How other karaoke formats handle these axes

The design mirrors what the established formats converge on. No mainstream format
does *both* axes structurally with pitch, which is where this lands feedpak ahead.

| Format | Multi-singer | Multi-language | Pitch |
|---|---|---|---|
| **UltraStar** (our source) | `P1`/`P2`/`P3` parts on a shared clock | thin (`#LANGUAGE` metadata only) | per-syllable, yes |
| **Rock Band / Harmonix** | `PART VOCALS` + `HARM1/2/3` tracks | no | per-note, yes |
| **`.ass` (anime karaoke)** | Actor / style colour | stacked lines (kanji + romaji + translation) | no (`\k` timing only) |
| **LRC / enhanced LRC** | no | bilingual A-B lines | no |

The invariants:

- **Multi-singer** everywhere is *N independent `(pitch, lyric, timing)` streams
  sharing one song clock* → this is `vocal_tracks[]`.
- **Multi-language** everywhere is *M text representations sharing one melody* →
  this is `lyric_tracks[]`.

The two are genuinely orthogonal, so they **compose**: N voices × M languages. And
because a duet's two singers sing *different words*, the language variants must be
**voice-scoped** — each voice owns its `lyric_tracks`. Today's song-level
`lyric_tracks` is then just the one-voice special case.

## 4. Specification

### 4.1. `vocal_tracks[]` (new top-level manifest key)

A list of voices. Each entry is, in effect, a self-contained `(lyrics,
lyric_tracks, pitch)` bundle scoped to one singer — the same fields the manifest
carries at top level today, promoted into a per-voice record.

```yaml
vocal_tracks:
  - id: p1                         # REQUIRED — stable, lowercase, filesystem-safe
    name: "Freddie"                # OPTIONAL — display; from #DUETSINGERP1
    role: lead                     # OPTIONAL — lead | duet | harmony | backing (open set)
    primary: true                  # OPTIONAL — the voice mirrored to the singular keys; default = first entry
    stem: full                     # OPTIONAL — stems[] id this voice was sung on
    lyrics: lyrics.json            # REQUIRED — this voice's primary lyrics (lyrics.json shape, §7.1)
    lyrics_source: authored        # OPTIONAL — authored | transcribed | user
    lyric_tracks:                  # OPTIONAL — this voice's language representations (voice-scoped §5.5)
      - id: p1-en
        file: lyrics.json
        language: en
        kind: original
    vocal_pitch: vocal_pitch.json             # REQUIRED — per-note stream (§7.2 shape); primary reuses the singular file
    # vocal_pitch_contour: …                  # OPTIONAL — continuous Hz contour (§7.3 shape); omit when absent
```

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | string | **yes** | Stable, filesystem-safe, lowercase. |
| `name` | string | no | Display label; from `#DUETSINGERP1/P2` when present. |
| `role` | string | no | `lead` / `duet` / `harmony` / `backing`. Open set; a Reader treats an unknown value generically. Advisory/display only — a scorer MUST NOT use it to decide correctness. |
| `primary` | bool | no | Marks the one voice the singular top-level keys mirror (§4.4). Default: the first entry. At most one entry SHOULD set it. |
| `stem` | string | no | `stems[]` id this voice was sung on (mirrors `lyric_tracks.stem`). Pairs a voice with a separated per-singer stem when one exists. |
| `lyrics` | path | **yes** | This voice's primary lyrics file (`lyrics.json` flat-array shape, §7.1). |
| `lyrics_source` | string | no | `authored` / `transcribed` / `user`. Default `authored`. |
| `lyric_tracks` | list | no | This voice's language representations — same entry shape as the top-level `lyric_tracks[]` (§5.5), scoped to the voice. |
| `vocal_pitch` | path | **yes** | This voice's per-note pitch stream (`vocal_pitch.json` shape, §7.2). See §4.2. |
| `vocal_pitch_contour` | path | no | Optional continuous Hz contour for the voice (`vocal_pitch_contour.json` shape, §7.3). Not populated from UltraStar; reserved for transcribed sources. |

`vocal_tracks[]` is a manifest list, so — like `arrangements`, `stems`, and
`lyric_tracks` — it carries no `version` field of its own; the files it points at
carry theirs.

### 4.2. Per-voice pitch is **per-note**, not per-syllable

Each voice's `vocal_pitch` file uses the **existing** `vocal_pitch.json` object
shape (`{ "version": 1, "notes": [ {"t","d","midi"}, … ] }`) — **no new schema**.
The importer's population rule for that file changes in exactly one way:

- **Do not flatten melisma.** Every pitched note is emitted, including a bare `~`
  continuation that moves to a new pitch. One lyric syllable MAY therefore span
  **several** pitch notes — the ribbon shows the pitch *moving* under a held vowel.

This is why the choice is "per-note": UltraStar's source is discrete pitched notes,
not a continuous curve, so a note stream (not a resampled Hz contour) is the honest
richer representation. The spec permits it — §7.2 says the pitch list "MAY be
shorter" than the lyrics (unpitched syllables omitted) but does not require 1:1, and
a melisma legitimately produces *more* pitch notes than syllables. A true
continuous contour remains available per voice via the optional
`vocal_pitch_contour` pointer for a future source that actually has one.

> **Verified against native (2026-07-05).** FeedBack's native karaoke renderer
> (`feedBack-plugin-lyrics-karaoke`) and vocals-viz both pair pitch to lyrics **by
> exact onset `t`, iterating over syllables** — each syllable looks up the pitch note
> sharing its `t` (`routes.py`: build `pitch_by_t[repr(float(n["t"]))]`, then
> `pitch_by_t.get(repr(float(tok["t"])))`). Extra melisma notes have no matching
> syllable `t`, so they are never looked up — never misaligned (a dict lookup, not a
> positional `zip`), never fatal. A melisma-rich stream and a flattened one produce an
> **identical** native overlay. So the singular `vocal_pitch.json` **is** the per-note
> rich stream — **one file, no flattened duplicate**. Native picks the onset note per
> syllable and ignores the movement; vocals-viz reads the whole stream. The primary
> voice's `vocal_track` reuses `vocal_pitch.json`, exactly as a `lyric_tracks` entry
> may reuse `lyrics.json`.

### 4.3. Per-voice lyrics and the language axis

Each voice's `lyrics` is an ordinary `lyrics.json` flat array (§7.1). A voice that
is sung in more than one language, or wants a transliteration/translation for
read-along, carries its own `lyric_tracks[]` with exactly the v1.11 entry shape and
semantics (§5.5) — `original` / `transliteration` / `translation`, optional `stem`,
etc. — scoped to that voice.

UltraStar almost always supplies a single language, so in practice each voice's
`lyric_tracks` has one `original` entry today. The slot exists so the shape is
future-proof the moment multi-language source data appears; we design for both axes
and populate what the source gives us.

### 4.4. Backward compatibility (mirrors §5.5)

- When `vocal_tracks` is **absent**, a Reader behaves exactly as v1 — it reads the
  singular `lyrics`/`vocal_pitch` and shows one voice.
- When `vocal_tracks` is **present** it is **authoritative** for multi-voice
  Readers. A Writer **SHOULD** keep the singular top-level `lyrics`,
  `lyrics_source`, `lyric_tracks`, and `vocal_pitch` populated for the **primary**
  voice, so a Reader predating this extension still plays that voice. (The singular
  `vocal_pitch` is the same per-note stream; native reads it onset-wise — see §4.2.)
- A Reader that understands `vocal_tracks` **MAY** ignore the singular keys.
- An older Reader ignores the unknown `vocal_tracks` key entirely (§1.2) and
  preserves it on re-emit.

### 4.5. File-naming conventions

Filenames are convention only (the manifest is the index, §2.2), but for legibility:

```
lyrics.json                 # primary voice lyrics (also the singular `lyrics`)
lyrics_p2.json              # second voice lyrics
lyrics_p1_es.json           # a voice's translation track (voice id + language)
vocal_pitch.json            # primary voice, per-note stream (singular; native reads onset-wise)
vocal_pitch_p2.json         # second voice, per-note stream
```

### 4.6. Relationship to the Vocals arrangement / notation

The notation-only "Vocals" arrangement and `notation_vocals.json` remain a *derived
staff render*, not the authoritative pitch source (that is `vocal_tracks`). For a
duet, either emit one notation file with **one staff per voice** (notation natively
supports multiple `staves`, §7.6) or one arrangement per voice. This is secondary;
the ribbon reader consumes `vocal_tracks`, not notation.

## 5. Worked example — a bilingual-capable duet

`manifest.yaml` (excerpt):

```yaml
feedpak_version: "1.14.0"
title: "Barcelona"
artist: "Freddie Mercury & Montserrat Caballé"
duration: 236.0
arrangements:
  - id: vocals
    name: Vocals
    type: vocals
    notation: notation_vocals.json
stems:
  - id: full
    file: stems/full.ogg
    default: true

# Back-compat: singular keys mirror the primary voice (P1).
lyrics: lyrics.json
lyrics_source: authored
lyric_tracks:
  - id: p1-en
    file: lyrics.json
    language: en
    kind: original
vocal_pitch: vocal_pitch.json

# Authoritative multi-voice data (extension key).
vocal_tracks:
  - id: p1
    name: "Freddie"
    role: lead
    primary: true
    stem: full
    lyrics: lyrics.json
    lyrics_source: authored
    lyric_tracks:
      - id: p1-en
        file: lyrics.json
        language: en
        kind: original
    vocal_pitch: vocal_pitch.json
  - id: p2
    name: "Montserrat"
    role: duet
    stem: full
    lyrics: lyrics_p2.json
    lyrics_source: authored
    lyric_tracks:
      - id: p2-en
        file: lyrics_p2.json
        language: en
        kind: original
    vocal_pitch: vocal_pitch_p2.json
```

`vocal_pitch_p1.json` — note the melisma on "free" spanning two pitch notes under
one syllable (per-note, §4.2):

```json
{
  "version": 1,
  "notes": [
    {"t": 12.34, "d": 0.30, "midi": 67},
    {"t": 12.64, "d": 0.20, "midi": 69},
    {"t": 12.84, "d": 0.40, "midi": 71}
  ]
}
```

`lyrics.json` — one syllable ("free-") where the pitch above moves:

```json
[
  {"t": 12.34, "d": 0.70, "w": "free-"},
  {"t": 13.04, "d": 0.30, "w": "dom+"}
]
```

## 6. Population from UltraStar

Concrete mapping in [`convert.py`](../convert.py):

- **Voices.** Emit one `vocal_tracks[]` entry per parsed player. `P1` → `primary`
  voice (`role: lead`); `P2` → `role: duet`. `P3` (both sing) is already copied
  into both players by the parser, so unison sections appear in each voice.
- **Names.** `#DUETSINGERP1` / `#DUETSINGERP2` (or `#P1` / `#P2`) → `name`.
- **Pitch.** Per voice, emit the **melisma-preserving** note stream (§4.2). The
  singular `vocal_pitch.json` **is** P1's stream; native reads it onset-wise.
- **Lyrics.** Per voice, one `original` `lyric_tracks` entry in the chart's
  language. Multi-value `#LANGUAGE`: the primary language maps to `language` as
  today; a second *sung* language needs per-language lyric data UltraStar rarely
  carries, so it waits for such a source (the voice-scoped `lyric_tracks` slot is
  ready for it).
- **Bundled precision fixes** (see §7): map `#CREATOR`/`#AUTHOR` → `authors[]`
  (`role: charter`) and `#ALBUM` → `album`; both are dropped today.

## 7. Precision notes carried by this work

This extension closes or logs several fidelity gaps found in the current converter
(full audit in the chat that produced this doc):

- **Duet P2/P3-exclusive parts** — the core fix; no longer dropped.
- **Melisma pitch movement** — preserved by the per-note pitch stream (§4.2);
  flattened away today.
- **Charter / album metadata** — `#CREATOR`/`#AUTHOR` → `authors[]`, `#ALBUM` →
  `album`; mapped as part of this pass.
- **Golden notes (`*`, `G`)** — still flattened; there is no feedpak golden-note
  field. Out of scope here; a candidate for a separate `notes[].golden`-style
  proposal.
- **Mid-song `#BPM` changes** — **known limitation, unrelated to this work.** The
  parser has no `B <beat> <bpm>` rule, so a tempo-changing chart mis-times
  everything after the change. Logged here so it is not forgotten; feedpak tempo
  maps (§7.4) could represent it in a later pass.

## 8. Validation

- `manifest.schema.json` is `additionalProperties: true`, so `vocal_tracks` and the
  per-entry fields pass `tools/validate.py` unchanged.
- Every per-voice `vocal_pitch` file validates against the existing
  `vocal-pitch.schema.json`; every per-voice `lyrics` file against
  `lyrics.schema.json`. No schema is modified.
- The importer's spec-validation test should assert a duet fixture round-trips and
  validates with `vocal_tracks` present.

## 9. Open questions / future work

- **Scoring semantics** are a *Reader* concern, not a format one. The format
  supplies the parts (name, role, stem, pitch); how a Reader scores them — one mic
  on the active part, two mics, or user-selected voice — is up to the Reader.
  vocals-viz will render N lanes and score the selected/active voice.
- **Primary-voice selection** uses an explicit `primary` flag (default: first
  entry). The primary voice reuses the singular `lyrics`/`vocal_pitch` files, so
  file-matching would also work; the flag is the robust, explicit signal.
- **Golden notes** and a **continuous contour** from transcribed sources are
  deferred (fields reserved: `vocal_pitch_contour`).
- **Upstream path.** When the plugin pair proves this out over the corpus, promote
  this note to a feedpak RFC: `vocal_tracks[]` as a v1.x additive minor, mirroring
  the `lyric_tracks` precedent, with the per-note-vs-per-syllable pitch guidance
  from §4.2 folded into §7.2.
