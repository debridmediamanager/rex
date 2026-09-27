# Cycle 78 report

Cycle 78 is **complete and accepted after integrator review**. The cycle-77 implementation head was
rechecked through all twelve axes. The A/B/C/N/P matrices found no implementation
finding, the corrected live account catalog passed navigation, API conformance and
real byte decoding, and both missing client legs are now complete: the official
Plex Web bundle rendered in the Windows CDP browser against both the cycle server
and the read-only kids PMS, and the disposable native Android emulator rendered
the fixture TV path through episode detail plus an actual live-catalog movie
detail with loaded actions. No repository server or test code changed.

The implementation head was `afba7e4d2cde5777fce534b77e39bca78bb4611d`; the
assigned worktree began clean. This report is the durable token-free record. Raw
outputs and screenshots remain under `/private/tmp/plexsim-cycle78`; the complete
SHA-256 manifest is `/private/tmp/plexsim-cycle78/verification-hashes78.txt`.

## Safety and reference state

- The guarded `python3 handover/tools/refcheck7.py ref_cycle5_before.json`
  snapshot exited 0 before and after the cycle. It covers **kids only**:
  `ref_before78.txt` and `ref_after78.txt` have the same SHA-256
  (`3a32fa89ff5ee0ca975c9dad9a9f09724e2e360928151bcfae04350199e64e5b`) and
  `ref_diff78.txt` is empty (`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
  No zen snapshot is claimed. The two pre-existing section-preference length
  changes remain in both snapshots; no cycle-owned mutation was observed.
  The integrator repeated this snapshot after the final Windows/Android legs.
  `ref_final78.txt` has the same SHA-256 above; `ref_final_diff78.txt` is empty.
- Kids and zen received only the guarded GET/HEAD probes. All mutable work used
  plexscratch, plexscratch42, or cycle-owned local servers. No plex.tv write,
  account mutation, paid operation, production configuration edit, or
  destructive reference operation occurred.
- The HTTP UI fixture server ran explicitly **without `--state`**, preserving
  restart as its undo. Persistence and live-catalog acceptance servers used
  private disposable `--state` files. The main account catalog was copied to
  `/private/tmp/plexsim-cycle78/live78/catalog.json` with mode 600; its links and
  the private RD credential were never written to the repository or this report.
- Cleanup removed only cycle-owned launchd labels, servers, Android emulator and
  proxy. The unrelated port-8899 listener (PID 7331) and both physical adb
  devices remain. Windows Chrome was launched for this run with the existing CDP
  profile, then its verified root process was stopped; no other Windows Chrome
  profile process remains. See `cleanup78_final.log`.

## Verification ledger

The commands below are the exact driver legs. Hash prefixes are included for
quick lookup; the full values and the added live/browser/emulator hashes are in
`verification-hashes78.txt`.

| Evidence | Exact command | Exit/result | Raw artifact and hash summary |
|---|---|---|---|
| A: localisation and request corpus | `cd /private/tmp/plexsim-cycle78/A && zsh run76.sh` | 0; `ALLDONE` | `A/out/run76.log` `4a93722854df`; `A/i18n/r76-all44.out` `ad33120793fd`; `A/i18n/mosweep78a.out` `56765d7c7012` |
| B: read/write/profile matrices | `cd /private/tmp/plexsim-cycle78/B && sh phase2_76.sh && sh phase3_76.sh` | 0; `PHASE2_76_DONE`, `PHASE3_76_DONE` | `B/phase2_76.log` `4bbd4e77eb9f`; `B/phase3_76.log` `f641079adee5`; `B/denom76.txt` `31770e776baf` |
| C: request/vocabulary/restart legs | `cd /private/tmp/plexsim-cycle78/C && sh phase2_76.sh && sh phase3_76.sh` | 0; `PHASE2_DONE`, `PHASE3_DONE` | `C/rleg/L0_retry.log` `4d56c6e64986`; `C/ax1/rg56_zen_76.txt` `69528a611407` |
| N: Android inventory and restart legs | `cd /private/tmp/plexsim-cycle78/N && zsh legs76.sh` with its `sweep71.py` and `writes71.py` legs | 0; `LEGS_DONE` | `N/legs78_retry.log` `75d3634e7895`; `N/writes78_s42_final.txt` `58036ef14923` |
| P: delayed writes and hard-kill matrix | `cd /private/tmp/plexsim-cycle78/P && sh km_all.sh && python3 regress_p76.py` | 0; `ALLDONE`; regressions recorded | `P/km_run.txt` `4d23ec5bc15d`; `P/rg_regress_p.txt` `917c9f02a799` |
| Offline suites | `python3 tests/test_decision.py`; `test_mediainfo.py`; `test_gdm.py`; `test_accountstate.py`; `test_catalog_features.py`; `test_plex_guids.py`; `test_persist.py`; then heartbeat, part-bytes, prefix, regional-prefs, sessions and TLS correction set | 0: 2340/2340, 36/36, 103/103, 15/15, catalog checks, 12/12, 16/16; correction set 13/13, 12/12, 58/58, 3/3 documents/1,122 cells, session checks, 10/10 with one TLS 1.3 skip, 12/12 | `B/offline_test_*_78.txt` and correction artifacts; prefixes `a734d699505e`, `5fe03c15d481`, `af333ad3cd1f`, `38c30ac42f1a`, `2addf423f9a0`, `a93d3181ca4f`, `1b5ea8d5c776` |
| Fixture HTTP navigation | `RD_TOKEN=dummy python3 server.py --port 32710 --catalog tests/catalog_ui.json --assert-claimed --gdm off --state /private/tmp/plexsim-cycle78/accept/state32710.pickle`; then `python3 tests/test_navigation.py --base http://127.0.0.1:32710 --catalog tests/catalog_ui.json` | 0; 29/29 | `accept/navigation_live78.txt` `744953d22ee0` |
| Fixture UI surface | `RD_TOKEN=dummy python3 server.py --port 32701 --catalog tests/catalog_ui.json --assert-claimed --gdm off`; then `python3 tests/test_ui_surface.py --base http://127.0.0.1:32701` | 0; 1,960/1,960 with 12 explicit fixture skips; this server deliberately had no `--state` | `B/test_ui_surface_78.txt` `059813df8f0c` |
| Live account catalog navigation | `python3 tests/test_navigation.py --base http://127.0.0.1:32720 --catalog /private/tmp/plexsim-cycle78/live78/catalog.json` | 0; 25/25 | `live78/navigation_real78b.txt` `c113cc53a752` |
| Live account catalog API | `uv run --quiet --no-project --with plexapi python3 tests/conformance.py --base http://127.0.0.1:32720 --token plexsim-test-token` | 0; 38/38 | `live78/conformance_real78b.txt` `29196ca758e4` |
| Live account catalog byte/decode | `python3 tests/test_all.py --catalog /private/tmp/plexsim-cycle78/live78/catalog.json --local http://127.0.0.1:32720 --public http://127.0.0.1:32720` | 0; 5/5 served and decoded | `live78/decode_real78b.txt` `1f716cfdd01b`; `live78/status_real78b.txt` `88c3a655534a` |
| Standalone Playwright fallback | `mcp__playwright__browser_navigate`/click/screenshot calls against `http://127.0.0.1:32720/web/index.html`; the in-app browser runtime was unavailable, so the permitted standalone Playwright MCP fallback was used | rendered home, movie, TV, show, season and episode routes; French settings saved and re-snapshotted; 41 local requests in the source run | `live78/web_walk_summary78.txt` `01e49fffc567`; screenshots and snapshots under `live78/web/`, including French `cycle78-live-settings-fr.png` |
| Windows CDP rendered comparison | `set -a; source /private/tmp/plexsim-cycle78/tokens.env; set +a; uv run --quiet --no-project --with websockets python3 /private/tmp/plexsim-cycle78/windows78/win_compare.py` through `ssh ben@windows` with the existing Windows Chrome CDP profile | 0; all screen checks true for both local and kids reference legs; each full leg had 18 status failures, exactly 9 harness-origin `GET /` 404s and 9 harness-origin `GET /media/providers` 404s; console counts were sim 108 and kids 169 | `windows78/windows78_walk_summary.txt` `cd3d2e4c7525`; `windows78/win_compare78.out` `8c63c7f264ad`; `windows78/windows78_error_inventory.json` `fe03951e42c8`; `windows78/windows78_error_disposition78.txt` `e2ab2714ba6a`; ten screenshots under `windows78/` |
| Windows error inventory follow-up | `set -a; source /private/tmp/plexsim-cycle78/tokens.env; set +a; uv run --quiet --no-project --with websockets python3 /private/tmp/plexsim-cycle78/windows78/error_inventory78.py` through the same `ssh ben@windows` CDP forward | 0; per-host/path/status and failed/blocked reasons recorded; sim console grouping 108 | `windows78/windows78_error_inventory.json` `fe03951e42c8`; `windows78/windows78_error_disposition78.txt` `e2ab2714ba6a` |
| Native Android rendered walk and live detail | `env DROID_WORK=/private/tmp/plexsim-cycle78/android python3 -u /private/tmp/plexsim-cycle78/wt78/harness/android/signin.py emulator-5580`; then `python3 -u /private/tmp/plexsim-cycle78/android/tv_direct78.py`; then the owned live-catalog server/proxy and the bounded manual ADB sequence recorded in `android/native78live_realrd_summary78.txt` with the private existing RD credential read by the launcher | fixture signin 0 (`sign in True; state home`); TV walk 0; the first scripted live navigation was stopped at its library sheet and is excluded; corrected manual live leg completed with each ADB step exit 0; exact `GET /library/metadata/1001?...includeExternalMetadata=1&checkFiles=1` returned 200; live Big Buck Bunny detail rendered Watch, Add to Watchlist, Mark as Watched, More, description and media rows | `android/native_walk_summary78.txt` `8a8932a05f37`; `android/native78live_realrd_summary78.txt` `c906f9fcb299`; live screenshot `android/N/out/native78live_realrd/04-movie.png` `9d26c4e43543`; UI dump `android/N/out/native78live_realrd/04-movie.txt` `48754c5aa03f`; fixture TV screenshots/UI dumps remain under `android/N/out/native78tvdirect/` |
| Cleanup | owned launchd labels removed, owned listeners rechecked, Windows Chrome root stopped by PID/command-line guard | no cycle-owned labels/listeners; ports 32720/32712/8898 clear; port 8899 and physical adb devices preserved; Windows dedicated profile processes remaining=0 | `cleanup78_final.log` `930dbd4fbec2`; updated manifest entry |

The first unqualified `python3 tests/conformance.py` invocation exited 1 before
running because `plexapi` was absent; the corrected `uv --with plexapi` command
above passed 38/38. A stale `python3 tests/test_guids.py` command exited 2 because
that path does not exist; the required `tests/test_plex_guids.py` suite passed
12/12. The earlier fixture conformance (`29/38`) and fixture decode (`0/11`)
results are known dead account-scoped links in `tests/catalog_ui.json`: the
browse/identity/queue/decision/watch-state checks passed, while the fixture media
URLs returned HTML/502. They are not live-catalog evidence and do not identify a
server behavior mismatch. The corrected live catalog is the 38/38 and 5/5 row
above.

The Windows error counts are classified in `windows78/windows78_error_disposition78.txt`.
The 18 status failures on each full rendered leg are exactly the two harness-origin
rows listed in the ledger (nine `GET /` 404s and nine `GET /media/providers` 404s).
The remaining non-2xx events in the follow-up JSON are failed or blocked auxiliary
requests: stale local endpoint attempts, plex.tv bootstrap/telemetry requests,
reference write/media gates, Sentry/cast telemetry and the intentionally refused
pubsub websocket. No target library route failed, and every screen assertion stayed
true. The sim console grouping is 108 (13 network TypeErrors, 7 pubsub 403s, 70
failed resource loads and 18 404 resource loads); the authoritative full kids walk
count is 169, with the route-equivalent follow-up varying by optional gate timing.

The fixture Android episode screenshot `android/N/out/native78tvdirect/06-episode.png`
captured four action placeholders and a bottom spinner, but its same-capture UI dump
contains the loaded title, Watch control, description and Video/Audio/Subtitles rows.
That dump does not prove the four action buttons finished loading; this fixture
frame is not counted as loaded-action evidence.
The decisive live-catalog leg, run with the existing private RD credential rather
than the earlier dummy-token instrument, returned 200 for the app's exact metadata
request and rendered all movie action controls in the hashed screenshot and UI dump
above. The dummy-token 500 (RD unrestrict 401) is retained only as an instrument
bound and is not counted as a client finding.

## Axis ledger

Each axis has a denominator and a disposition. The client legs are now present,
so “clean” means no new client-visible or malformed-only difference remained in
this cycle's bounded measurements.

| Axis | Concrete denominator and evidence | Disposition |
|---|---|---|
| 1. Request inventory | C's 442-request restart legs plus 38 snapshot keys; N's 160 Android templates over five hosts/800 read cells; A/B endpoint matrices | **Clean** |
| 2. Python API reads | C's 1,018 probes; B read chains; N's 800 read cells; 231 expected refusals unchanged | **Clean** |
| 3. Client writes | C write regressions; B disposable chains; N scoped 62-line transcript; P's 67 delayed-write cases and queue controls | **Clean** |
| 4. Owner screens | B owner/readback chains, 1,960 HTTP UI assertions, Windows rendered home/detail routes and native home/library routes | **Clean** |
| 5. Plex Web rendering | Windows CDP official bundle rendered home/movie/show/season/episode against both the cycle server and kids PMS; all route assertions true; the 18 status rows and 108/169 console dispositions are classified in the error artifact; local standalone fallback also rendered the walk and French settings | **Clean** |
| 6. Python API writes | B's s42 81 POST/81 PUT and sim 82 POST/64 PUT ledgers; disposable 1.42.2 chains and restart readbacks | **Clean** |
| 7. Vocabulary | C frozen/own/restart comparators; regression sets 17/17, 15/15, 14/14, 2/2 and 4/4; known malformed-only Device fixture row unchanged | **Clean** |
| 8. Playback by identity | B decision/session/crossing matrices and N play, decision, timeline and sidecar requests; live catalog direct-play/decode checks | **Clean** |
| 9. Localisation | A's 44-language exact corpus, 153 `Accept-Language` forms, stream/item checks and four `.mo` sweeps; rendered French Plex Web settings evidence | **Clean** |
| 10. Discovery | C discovery requests plus N's 800 read cells covering server/provider/device inventory; both rendered client legs found their libraries | **Clean** |
| 11. Native Android client | signin reached `home`; fixture emulator rendered both library cards, recommended/browse screens, The Simpsons show, Season 19 and episode detail; the actual live catalog rendered Big Buck Bunny detail with Watch/add/rate/watched/more controls; UI/screenshot hashes retained | **Clean** |
| 12. Restart durability | C L0/L1/L2 legs, B readbacks, N TERM/KILL legs and P delayed-write/hard-kill matrix | **Clean** |

## Findings and follow-up

No implementation finding was produced. The only nonzero results were corrected
instrument invocations and the fixture catalog's dead account-scoped media links,
with precise dispositions above. The Android and browser issues were instrument
configuration issues: the Android proxy needed the 10.0.2.2-to-local alias and
304-body/favorite normalization; the browser runtime required the Windows CDP
fallback after the in-app browser runtime was unavailable. The fixture Android
episode screenshot's skeleton action row was paired with its loaded UI dump and
then with the actual live-catalog movie screenshot, whose actions are fully
rendered after the exact metadata request returned 200. Both client legs were
therefore exercised successfully; no server architecture, authentication,
production or security change is proposed.

All twelve cycle-78 axes are clean. Cycle 77's historical closure remains in
`HANDOVER.md` and `docs/findings.md`; this cycle adds the fresh client and
live-catalog evidence without rewriting that historical record.
