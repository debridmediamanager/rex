# Cycle 80 report

Cycle 80 repeated all twelve axes on `0cb760b` (cycle 79's three fixes) and found **no client-visible
and no new malformed-only difference**. Driver-run; raw outputs stay under `/private/tmp/plexsim-cycle80`.
Nothing here carries a token.

## Instruments

kids 1.43.4.10903, zen 1.42.2.10156, plexscratch (1.43.4) and plexscratch42 (1.42.2); Plex Web 4.147.1
served (app.plex.tv 4.160.0); Plex for Android 2026.9.0 in emulator `c70phone`; python-plexapi 4.18.2
(mise Python 3.10.20). The tree was built by `handover/tools/cycle_mk_next.sh 79 80 0cb760b` and
launched by `handover/tools/cycle_start.sh 80`.

## Safety

`refcheck7.py` (kids) before and after: identical, and identical to cycle 79's. kids and zen received
GET only; writes went to plexscratch, plexscratch42 and cycle-owned plexsim instances. The fixture UI
suite ran without `--state`.

## Ledger

| Leg | Result against cycle 79 |
|---|---|
| C (`phase2_76.sh`, `phase3_76.sh`) | comparators, regressions and discovery identical; L0/L1/L2 restart == control but the background list's `addedAt` |
| B (`remote1`, `localall`, `phase2`, `phase3`) | denominator identical but 14 fewer s42 DELETEs (cycle 79's contamination); regressions identical; 59b chain 0/20 differ from cycle 79's plexsim; advance flow's s42 history row gone |
| A (`run76.sh`) | 44-language corpus, Android corpus, `.mo` sweeps (168/21/5/0), titles, aliases, zen and s42 item settings all identical |
| N (`run77.sh`, s42 pass gated on B) | regressions identical; sweeps differ only in zen/s42 content and the playlist-id race; `hubsec-ss` season-0 difference gone |
| P (`run77.sh`) | hard-kill matrix, rc2/r1 and regressions identical |
| Offline suites | 13/13 |
| Fixture HTTP | navigation 29/29; UI surface 1,959/1,960 on its second run (a statistics sample landed mid-check; first run stopped on a raw-socket reset that does not reproduce idle) |
| Live account catalog | navigation 25/25, conformance 38/38, decode 5/5, no Real-Debrid 429 |
| Windows Plex Web | every screen true on sim and kids; 18 status failures each; same failure classes |
| Android emulator | home to episode rendered (the season page re-entered once: plex.tv's `luma` request hung on the first visit) |
| Specials copy | Android `Specials`; Plex Web `Specials` / French `Spécials` |

## Findings

None. The stopping rule is met on all twelve axes.
