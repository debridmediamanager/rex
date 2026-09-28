# Cycle 81 report

Cycle 81 repeated the twelve-axis stopping cycle from `ed66770`, the clean cycle-80
head. The driver tree and raw evidence are under `/private/tmp/plexsim-cycle81`;
the worktree is `/Users/bensarmiento/debridmediamanager/plexsim-cycle81`. No server
implementation file changed. The cycle started with four dead comparisons from
cycle 80; those inputs and instruments were repaired before their results were
counted.

## Instruments and safety

The live references were kids PMS 1.43.4.10903 and zen PMS 1.42.2.10156. The
cycle used Plex Web 4.147.1, Plex for Android 2026.9.0 in a disposable local
arm64 emulator, and python-plexapi 4.18.2. Kids and zen were read-only. Writes
went only to plexscratch, plexscratch42, and cycle-owned plexsim instances; P
restarted only plexscratch and B used only plexscratch42. The fixture UI run
used a server without `--state`.

`refcheck7.py` returned exit 0 before and after the cycle. The token-free
snapshots are `/private/tmp/plexsim-cycle81/ref_before81.txt` and
`/private/tmp/plexsim-cycle81/ref_after81.txt`; `diff -u` is empty. No
plex.tv write, claim or account mutation was issued by the harness. The
rendered Windows and Android clients still attempted external plex.tv/luma/
pubsub reads; browser gates blocked the external write attempts, and the
resulting read/error inventory is retained in the Windows and Android
artifacts. No physical Android device was touched, and all cycle listeners and
the disposable emulator were stopped after the run.

## Dead-probe audit

| Dead comparison | Failure before a request | Repair and evidence |
|---|---|---|
| C `f587` r1 body set | Reference JSON named bodies absent from the cycle copy, so the r1 comparator stopped before comparing a response. | `C/f587/run74.py` copies each existing body from the measured `C/ax1` capture and raises if the measured source is absent. All 776 referenced body files hash identically to their measured sources; `f587/r1.jsonl` has the original 1,068-line denominator and `ana1.txt` contains comparisons. |
| C `f587` r2 body set | The second reference JSON named the same missing body set, so the r2 comparator also stopped before comparing a response. | The same measured body repair covers r2; `f587/r2.jsonl` has the original 130-line denominator and `ana2.txt`/`ana_r2.txt` contain comparisons, with no traceback or missing-body error. |
| P R0 | `ids_ref_r0.json` was never created, so the baseline readset attempted to load an absent write manifest. | `P/wr.py` measures `/identity` from isolated plexscratch for R0 and writes the manifest; `P/ids_ref_r0.json` exists and contains only measured identity data. `step1.sh` refreshes the disposable scratch token before this read. Non-baseline readsets now fail explicitly when a required write-created ID is absent. |
| B subtitle upload | `fx/c61b.srt` did not exist; the upload callback returned before sending the request. | The cycle copier and repair helper point the probe at measured `fx/c59b.srt` and remove the synthetic fallback. `B/run_sim.log` records successful `movie.uploadSubtitles(srt)` and the subsequent uploaded-stream removal. |

The copy guard is a safety instrument, not a fourth probe: it requires an exact
`/private/tmp/plexsim-cycle<digits>` root, resolves every edited target and
refuses a target symlink that escapes that tree. Valid, idempotent, missing-
anchor, refused-root and symlink-escape checks are recorded in
`accept/helper_checks.txt`.

## Axis ledger

| Axis | Fresh evidence | Result |
|---|---|---|
| 1 request inventory / JSON / XML | C `ax1`, repaired `f587`; A localization and N request sweeps | clean; malformed refusals and capability-only XML remain bounded |
| 2 python-plexapi reads | C `ax2`, B read chains | clean; measured content and timing differences only |
| 3 client writes, redraws and frames | C phase 3, B/N regressions, P write/readback flows | clean |
| 4 owner screens | LAN-bound UI fixture, B/N owner legs | clean; 1,960/1,960, 12 fixture-capability skips |
| 5 rendered Plex Web | Windows CDP sim and kids walks; French fallback walk | clean on movie/show/season/episode; home shelf is account-pinned state |
| 6 python-plexapi writes/playback | B local and remote chains on both disposable scratch servers | clean; subtitle upload now has a real request |
| 7 server vocabulary | C regressions and B vocabulary matrices | clean; known malformed-only rows unchanged |
| 8 playback per profile/identity | B/N identity, queue, decision and byte legs; live catalog | clean; public tunnel media legs were unavailable |
| 9 localization | A's fresh simulator requests over 8,064 web cells in 42 locales and 1,032 Android cells in 43 locales, `Accept-Language` 153/153, `.mo` 168/21/5/0, titles, aliases and item settings | clean against the preserved measured zen paging corpus from cycle 80; known malformed-only translation cells unchanged |
| 10 discovery/claim/connection | C discovery, N provider/device and GDM legs | clean |
| 11 Plex for Android | disposable emulator walk through movie, show, season and episode detail | clean; 149 target requests in the capture, loaded actions and artwork |
| 12 restart/durability | C L0/L1/L2, N TERM/KILL, P hard-kill matrix | clean; reference restart bounds unchanged |

The raw ledgers are `C/phase2_cycle81.log` (which contains both `PHASE2_DONE`
and `PHASE3_DONE` markers), `C/rleg/L2.log`,
`P/km.log`, `B/chain_sim.log`, `B/chain_s42.log`, `B/REMOTE*_76_DONE`,
`N/run77.log`, `N/legs77.log`, the A `i18n/r81*.out` files, and the Windows and
Android artifacts under the cycle directory.

The first A wrapper launch was discarded: its `ALLDONE` marker followed
`ModuleNotFoundError: websockets` in every localization subleg and sent no
comparison requests. A dedicated venv with websocket-client, websockets and
plexapi 4.18.2 was installed, and every A subleg listed above was rerun to
completion before the axis was classified.

The A comparator deliberately retained `i18n/zcache69`, the previously measured
zen paging corpus carried forward from cycle 80's reference capture, while
issuing fresh requests to the cycle simulator. It is reference measurement
data, not fabricated output. The live locale checks in `al71.json` exercised
153 `Accept-Language` forms and established the locale-selection partitions;
the fresh simulator cells establish that those partitions and localized
payloads remain equal to the preserved corpus. They do not claim a new full
zen response corpus. The live reference was independently rechecked on the
other axes and the resulting language partitions are byte-equivalent to cycle
80.

## Verification

| Command or leg | Exit | Salient result |
|---|---:|---|
| `cycle_mk_next.sh 80 81 ed66770` | 0 | copied 926 files and repaired four dead comparisons in three probe families |
| `cycle_start.sh 81` pre-reference check | 0 | all twelve cycle services started; protected snapshot matched cycle 80 |
| C phase 2, phase 3 and L0/L1/L2 | 0 | 410/410 comparator requests covered; restart controls matched except recorded timing/state bounds |
| P step 1, hard-kill matrix and regressions | 0 | R0/R1/R2/R3/R4 and kill matrix completed; 66-path redraw sets had no new difference |
| focused P readset guard | 0 | `accept/p_readset_guard81.txt`: R0 53 paths and R1 66 paths contain no `/None`; a missing non-baseline ID raises explicitly |
| B local, remote and s42 chains | 0 | `CHAIN76_SIM_DONE`, `LOCAL76_DONE`, `REMOTE1_76_DONE`, `REMOTE2A/B_76_DONE`, `REMOTE3_76_DONE` |
| A completed web/Android localization, `.mo`, title/alias and item-settings comparisons | 0 | 8,064 web cells and 1,032 Android cells match cycle 80; `Accept-Language` 153/153; item settings, aliases and all four `.mo` candidate counts match |
| exact offline suites with plexapi 4.18.2 | 0 | decision 2,340/2,340; mediainfo 36/36; GDM 103/103; persist 16/16; account state 15/15 |
| required offline commands | 0 | `python3 tests/test_heartbeat.py` 13/13; `python3 tests/test_tls.py` 10/10 + 1 skip; `python3 tests/test_part_bytes.py` 12/12; `python3 tests/test_prefix_registry.py` 58/58; `python3 tests/test_regional_prefs.py` 3/3; `python3 tests/test_sessions.py` all checks passed. Logs: `/private/tmp/plexsim-cycle81/accept/missing_test_*.txt` |
| catalog feature suite under Python 3.12.10 and supported system Python 3.9 | 0 | pre-fix `/private/tmp/plexsim-cycle81/B/test_catalog_features_head81.txt` failed only on the runtime-generated 7,933-vs-7,531 Unicode candidate-domain count; post-fix `accept/catalog_features_py312_fixed.txt` and `accept/catalog_features_py39_fixed.txt` both pass the exact 112-codepoint U+0300..U+036F behavior check |
| fixture `test_ui_surface.py` on LAN-bound server | 0 | 1,960/1,960, 12 skips |
| fixture `test_navigation.py` on LAN-bound server | 0 | `/private/tmp/plexsim-cycle81/accept/navigation_fixture81.txt`: 29/29 |
| live account `test_navigation.py` | 0 | 25/25 |
| live account `conformance.py` | 0 | 38/38 |
| live account `test_all.py` | 1 | local prefix/magic/seek/decode passed for all 5 playable rows; public tunnel prefix/seek returned HTTP 530, a tunnel bound rather than a server finding |
| Windows rendered/error inventory | 0 | movie/show/season/episode true on sim and kids; 14 harness/reference failures per leg are classified in `windows81_error_inventory.json` |
| Android emulator walk | 0 | library picker through episode detail rendered; screenshots and `android81.jsonl` capture saved |
| protected pre/post snapshot and cleanup | 0 | snapshots identical; no cycle ports/processes remained, and the physical adb device remained untouched |

The changed tooling passes `zsh -n handover/tools/cycle_mk_next.sh`,
`python3 -m py_compile handover/tools/repair_cycle_dead_probes.py`, the helper
valid/idempotent/missing-anchor/refused-root/symlink-escape checks in
`accept/helper_checks.txt`, and `git diff --check`.

## Findings

None. The dead probes were instrument defects, not server behavior: each was
repaired from measured input and then completed with a real request/comparison.
All twelve axes have fresh bounded evidence and no client-visible or new
malformed-only difference. The only acceptance limits are the recorded public
Cloudflare tunnel media failure, the fixture's unavailable-account links, and
the account-pinned empty Home shelf in the Windows walk. The catalog test now
separates the interpreter-dependent candidate-domain denominator from the
measured title-folding behavior and passes on both supported interpreters.
