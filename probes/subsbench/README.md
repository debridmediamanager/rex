# subsbench: is a subtitle in sync with this release?

The measurement behind rex's subtitle search and its pick. It answers, for real releases
and every candidate subtitle a source offers for them, whether the candidate's cues sit on
the release's own speech, and if not whether a constant shift or a frame-rate ratio fixes it.

| Step | Script | Where | What |
|---|---|---|---|
| 1 | `corpus.py [seed]` | beside a rex state file (`REX_DB`) | a stratified sample of movies and episodes, no remux or 2160p |
| 2 | `candidates.py` | the same host (`REX_CLAIM`, `REX_URL`) | each file's OpenSubtitles hash read through rex, Plex's own search (`metadata.provider.plex.tv/library/streams/matches`, the request a PMS makes) for `en`, `pt-BR` and `pt`, and the Stremio OpenSubtitles v3 addon's list |
| 3 | `fetch.py DIR cands.json` | the same host | every candidate's file: Plex's from the provider (`/library/streams/{id}`), the addon's from its url; one request at a time per host, both answer 429s to parallel fetches |
| 4 | `measure.py cands.json DIR out.jsonl [k/n]` | a test host (`REX_URL`, `REX_TOKEN`, Silero VAD v5's `silero_vad.onnx` in `~/subsbench`) | the meter, below |
| 5 | `analyze.py cands.json out.jsonl` | anywhere | pick strategies scored against the meter |
| 6 | `variant.py MODE N SECS PCT out.jsonl` | the test host | the meter re-run with another detector or window plan, against step 4's verdicts |
| 7 | `export.py` | the test host | `tests/fixtures/subtitles/sync_corpus.json.gz`: cue timings (no text), each window's energy mask and the meter's verdicts |

`apply_probe.py` records a real PMS's Search Subtitles and apply on plexlab and deletes the
applied stream again (`tests/fixtures/subtitles/plex_search_apply_s143.json`).

## The meter

Six 90 s windows (10, 25, 40, 55, 70 and 85% of the runtime) of the release's first audio
track, read through rex with FFmpeg seeking by Range, run through Silero VAD v5 at 32 ms.
Each candidate's cues become a mask on the same grid and are cross-correlated with the speech
mask (+-1 encoding, as ffsubsync does) for every lag within +-30 s; each window's curve is
summed into one joint curve per frame-rate ratio (1, 25/23.976, 24/23.976, 25/24, 30/29.97
and their inverses), and the ratio whose joint peak stands highest wins, 1 by a 10% margin.
A reading needs a joint peak 3 deviations above the curve's mean and at least three windows
whose own peak sits within 0.5 s of it, those being at least half the windows that say
anything. Then `in_sync` (ratio 1, |lag| <= 0.5 s), `shift` (ratio 1, a constant lag),
`fps` (another ratio) or `broken` (the windows disagree: another cut).

Single windows were too weak to decide anything (z 3-11 on a MAD scale, measured); the joint
curve is what makes the meter work. WebRTC VAD called 66-89% of film audio speech -- music
and effects -- and flattened every peak; Silero replaced it.

Controls, 120 releases (2026-10-20):

| Control | Verdicts |
|---|---|
| Another title's subtitle against this release's audio (null) | 119 unknown, 1 shift: never in sync |
| The release's own embedded text track | 13 of 14 readings in sync |
| An in-sync candidate moved 2 s late | 79 of 80 readings `shift`, measured 1.8-2.1 s |
| An in-sync candidate played at 23.976/25 | 80 of 80 readings `fps`, ratio 25/23.976 |

## What it found

Items with at least one candidate the meter could read:

| Pick | en (75) | pt-BR (47) |
|---|---|---|
| Plex's first result (what Plex Web lists first; the client keeps the provider's order) | 38 in sync | 22 in sync |
| Plex's highest `score` | 38 | 26 |
| The Stremio OpenSubtitles addon's first | 31 | 17 |
| Release-name ranking over both sources | 46 | 23 |
| Some candidate in sync | 59 | 30 |
| Some candidate in sync, or fixed by a shift or a frame-rate ratio | 75 | 47 |

Coverage is no lever: Plex's search had any English subtitle for 112 of 120 releases and
pt-BR for 98, the addon for 90 and 55, and the addon found none Plex lacked. Measuring the
candidates and correcting them is: about half of Plex's first picks are off, and every
release with a readable candidate has one that is in sync or fixable.

What rex can compute with no model: a band-passed (300-3400 Hz) energy detector, speech above
the window's median dB, decides about half as often as Silero and agrees with it when it does
(183 of 185 in sync, 66 of 72 shifts, 62 of 62 frame-rate errors; 2 of 120 nulls misread).
Aligning against the release's own embedded text track instead of audio -- rex reads one through
its Cues for a few hundred bytes a block (`runtime/mkvsubs.rs`) -- agreed with the audio meter on
20 of 20 in sync, 4 of 4 shifts and 4 of 6 frame-rate errors, with every null unknown; 61% of the
episodes and 52% of the movies on the library sampled carry an embedded text track.
