# Cycle 79 report

Cycle 79 repeated all twelve axes on `d3125c1` (implementation identical to cycle 77's
`afba7e4`), driver-run from cycle 78's tooling copied to `/private/tmp/plexsim-cycle79`. It found
**three client-visible rules**, built in three commits on `cycle79`, and repaired five comparisons
that had been silently dead since cycle 78. Raw outputs stay under `/private/tmp/plexsim-cycle79`;
nothing here carries a token.

## Instruments

| | Cycle 79 | Cycle 78 |
|---|---|---|
| kids PMS | 1.43.4.10903 (newest on plex.tv) | same |
| zen PMS | 1.42.2.10156 | same |
| Plex Web served / hosted | 4.147.1 / app.plex.tv 4.160.0 | same |
| Plex for Android | 2026.9.0 (emulator `c70phone`) | same |
| python-plexapi | **4.18.2** (mise Python 3.10.20) | 4.17.2 (venv Python 3.9.6) |
| zen library | shows with a season 0 in its hubs (Bluey in Top Rated) | none sampled |

## Safety

- `refcheck7.py` (kids only) before the cycle: identical to cycle 78's `ref_before78.txt`.
- kids and zen received GET only. Writes went to plexscratch (1.43.4), plexscratch42 (1.42.2) and
  cycle-owned plexsim instances. P restarted plexscratch as its axis requires. The flag fixture
  created play queues on both copies (disposable; their DELETE answers 404).
- One Android tap opened plex.tv's Discover page for The Simpsons (read-only; it shows the lab
  account's watch history). No write was made; the walk was repeated from home.
- The fixture UI suite ran on servers without `--state`; every other server used a private state file.

## Ledger

| Leg | Command (from `/private/tmp/plexsim-cycle79`) | Result |
|---|---|---|
| C: axes 1-4, 7, 10, 12 | `sh phase2_76.sh && sh phase3_76.sh` in `C/`; then `legs79.sh` (L0c/L1c, each started on its own state) | ax1/ax2/ax10 identical to 78 up to set order and zen content; L0c, L1c, L2 restart == control except `/playlists?type=42` `addedAt` |
| B: axes 6, 8, 12 | `remote1_76.sh`, `localall76.sh`, `phase2_76.sh`, `phase3_76.sh` in `B/` | denominator identical to 78; regressions identical; `diff60s` shows the queue-flag 400 (finding 3); advance flow's s42 history row was N's concurrent scrobble |
| A: axes 5, 9, 12 | `zsh A/run76.sh`; `zsh A/mo79.sh` after restoring `A/mo` from zen | 44-language corpus 8,202 exact cells, identical to 78; Android corpus, titles, aliases, zen item settings and `.mo` sweeps identical to 77/78; s42 item settings identical to 77 once repaired |
| N: axes 11, 12 | `zsh N/run77.sh`; `fix76.py`, `sidecar76.py sim` rerun once `out/` existed | regressions 70b/71n/73n/76n/76n_ka as in 78; sweep comparisons differ only in zen content and hub `seasonCount` (finding 1) |
| P: axis 12 | `sh P/run77.sh` | hard-kill matrix, rc2/r1 and regressions (5/5, 7/7, 3/4, 5/5) as in 78 |
| Offline suites | `tests/test_{decision,mediainfo,gdm,accountstate,catalog_features,plex_guids,persist,heartbeat,part_bytes,prefix_registry,regional_prefs,sessions,tls}.py` | all pass on `head79` and on the merged head |
| Fixture HTTP | `test_navigation.py` on `--state`, `test_ui_surface.py` without | 29/29; 1,960/1,960 (12 skips) on both heads |
| Live account catalog | private copy, `start_live.py` on 32720/32721 | navigation 25/25, conformance 38/38, decode 5/5 (the first run's five RD 429s rerun clean) |
| Windows Plex Web | `win_compare.py`, `error_inventory79.py` over `ssh -L 19222 -R 32720 ben@windows` | every screen true on sim and kids; 18 status failures per leg (same nine `/` and nine `/media/providers`); console 105/171, same classes as 78 |
| Android emulator | `signin.py`, `tv_direct78.py` | home, TV, browse, show, season, episode rendered, with the episode's actions loaded |
| Specials, rendered | `android/catalog_android_specials.json` served from `head79`, then from `wt79` | Android season card `Season 0` -> `Specials`; Plex Web `Season 0`/`Staffel 0` -> `Specials`, French `Spécials` |

## Findings

1. **A season 0 is titled `Specials`** and translated like `Season N` (zen, 45 languages; 13 keep
   English). `8e337a1`.
2. **A show with a season 0 carries `seasonCount`** (seasons without specials) after `childCount`;
   no other show does (zen, 509 shows). `16a8c09`.
3. **`shuffle` and `repeat` on `POST /playQueues` are 0/1 integer flags**; anything else is a 400
   (1.42.2 and 1.43.4, 17 values). python-plexapi 4.17.2's `shuffle=True` is refused. `7e77c8c`.

Each is proven by `tests/test_catalog_features.py` failing on its parent and passing on it, driven by
`tests/fixtures/specials_zen.json` and `tests/fixtures/playqueue_flags.json`.

## Repaired comparisons

`A/mo` (symlink into a wiped scratchpad; `.mo` sweeps crashed in 78 too), `C/ax1/old72/hubkeys.txt`
(never copied; L0/L1 snapshot halves dead in 78), B's `$P` cycle-59 s42 captures (wiped; compared
plexsim-to-plexsim instead), A's s42 item-settings sweep (plexsim's collection 50001 was a cycle-76
leftover; created first, identical to cycle 77), and the copy's dropped files (`N/{nreq,restart76,killleg76*}.py`,
`P/poster.png`, root `start.py`, empty directories).
