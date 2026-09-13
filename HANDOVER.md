# plexsim handover — 2026-09-13 (refreshed after cycle 10)

This file hands over the "implement everything the Plex UI asks of the server"
programme: the measurement-cycle loop, how it is run, where it stands, and what bit
us along the way. It is tracked in the repository and carries **no tokens**.
Tokens live in the private `../docs/operations/credentials.md`.

Read `CLAUDE.md` and `docs/findings.md` as well. This file tells you how the work
is run. Those two record what was measured.

---

## 1. Where things stand

| | |
|---|---|
| `main` | `dd52196`, equal to `origin/main`, clean tree (cycle 10 = `b3dd23f..dd52196`, 49 commits) |
| Worktrees and stashes from this work | none; both cycle-10 worktrees and their branches, local and remote, are gone |
| Cycles completed | 10, in three batches, all three implemented and merged. Cycle 10 had findings, so **cycle 11 is due**. |
| Work half-done | **none**. |

Cycle 10 ran as three batches:

| Batch | Territory | Outcome |
|---|---|---|
| 1 (opus) | `mediainfo.py` against fun's own analysis of the same real files | 16 rules, all re-measured; 29 commits (`b3dd23f..a7b3b77`); corpus in `tests/fixtures/media/` and the new `tests/test_mediainfo.py` |
| 2 (fable) | the writes cycle 9 did not reach, on plexscratch | 9 findings, all re-measured on the copy; 8 implemented, 1 recorded (no client sends it); 12 commits (`a7b3b77..173305c`) |
| 3 (opus) | the playback and session surface, on plexscratch's long media | 8 findings; 7 implemented, 1 **refuted** on re-measurement; 8 commits (`173305c..dd52196`). The first attempt died on a model weekly limit and was relaunched against the merged head |

Suite results at the end of cycle 10, all run on 2026-09-13:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **846/846**, 4 skipped | The skips need a TMDB key or real RD links. Restart the server between runs -- it writes, and the match check now merges a row away |
| `tests/test_mediainfo.py` | **27/27** | Offline: 46 recorded probes against what fun and the reference wrote for the same files |
| `tests/test_navigation.py` on the full catalog | **46/46** | |
| `tests/conformance.py` (python-plexapi) | **29/38** | The 9 failures are byte reads of placeholder RD links (502). They are expected. |
| `tests/test_all.py --skip-decode` | playQueue and decision PASS, output identical to `b3dd23f` | Byte reads fail with 502 for the same reason |

Proof for cycle 10. Every fix was proven one at a time against ITS OWN parent commit:
`c10/mdrive.py` for the media rules (the parent tree exported per patch, the offline check run
against both) and `c10/sdrive.py` for the write and session rules (the parent commit exported
and served, the patch's suite block run against both, committed only on fail-then-pass). The
parent is re-exported per patch, so "fails on the parent" means the commit's own parent rather
than some older tree -- the first run of batch 1 had to be re-proven because the parent tree
was missing a sidecar and died at import. The kids snapshot was byte-identical before and after
each batch.

Cycle 10 also corrected three OLDER checks whose beliefs it overturned: two completed a play
with a single beat at the end (the crossing rule), and one asserted that a video's
`displayTitle` never carries the codec -- thirteen products get it, Plex Web among them, and
the suite's own client says `X-Plex-Product: Plex Web`.

Proof for cycle 9. Batch 1 (hunter B, `985293a`..`ae19756`) added 17 checks, all failing
against `a7c3408` (768/784). Batch 2 (hunter A, `7589e32`..`4f2bb31`) was proven one check
at a time: `c9/drive.py` applied each patch, ran its check against an unfixed server
started from `ae19756` and against the fixed one, and committed only on fail-then-pass.
The whole final suite against that unfixed tree: 781/829, the 46 new checks and the 2 re-measured collection checks failing (plus Delete Version, whose movie the old code's cross-type merge had already moved into the show). Old checks re-measured and updated in
this cycle: a collection's unlocked Sort Title, the collection reorder response, the
"renames its members' tag" label, the state-write refusals (now sent with the identifier),
the play-queue checks that build a second queue (a client's new queue retires its older
one), and the frame-batch order. The kids snapshot was identical before and after.

---

## 2. The mission and the one rule

The user's standing asks, in their words, across the sessions:

- "cover all functionality in the ui related to the server, including
  functionalities in the context menu of each item"
- "add everything to the harness"
- "do it in a cycle until the agent returns that there are no more findings"
- use a **Fable** subagent and an **Opus** subagent for the hunts (explicitly
  authorised)
- **plex.tv is out of scope**: Optimize and Sync, the plug-in app store, the
  account side

**Every emulated response is a measurement, not a guess.** A response shape is
taken from what the reference PMS answers for the client's exact request, never
written by hand. The loop stops only when a cycle's hunters both return
"no findings".

---

## 3. Instruments

### Reference PMS — `kids` (the authority)

| | |
|---|---|
| URL | `http://kids:32400` |
| Token | "server token" in `docs/operations/credentials.md` (the plexlab entry) |
| machineIdentifier | `78a364ad887787f74e834a5c48c987d3e6fb165a` |
| Version | Plex 1.43.4 |
| Where it runs | docker container `plexlab-plex` from `/home/ben/plexlab`; config in `/home/ben/plexlab/config` |
| Harness source | `harness/` in this repo (CLAUDE.md has setup and traps) |
| Lab ports | 9081 mitmweb, 7902 desktop, **9224 CDP (plexlab only)**. Anything else on kids uses 9223. |

Library contents, which matter when you read tests and findings:

- **Section 1 "Movies"**: 5 movies with ratingKeys `1` Big Buck Bunny,
  `18` Cosmos Laundromat, `17` Elephants Dream, `3` Sintel, `4` Tears of Steel.
  Cosmos was `30` until cycle 4 deleted the library and it had to be restored
  from backup, which re-keyed it.
- **Section 2 "TV Shows"**: Firefly `6`.
  - Season 1 is `7`, episodes `8`–`10`.
  - Season 2 is `11`, episodes `12`–`14`. These three are **unmatched**
    (`local://`) and titled "Episode 1–3".
- Season `7` has `year` 2002 and `titleSort` "Season 1". It gained both from a
  metadata refresh of show 6 on 2026-09-12. That refresh repaired the
  showOrdering incident in §8.
- **Download queue `1`** exists and cannot be deleted (DELETE answers 404). A
  cycle-6 agent created it; it is status `done` with 0 items. Leave it.
- **State baseline**: `handover/tools/ref_cycle5_before.json`, taken before
  cycle 5. It still matches exactly: sections, both listings with watch state and
  ratings, episodes, library and server prefs, item prefs, playlists (0),
  collections (0). Compare with `handover/tools/refcheck7.py`.

### Disposable write reference — `plexscratch` on kids (WRITE FREELY)

Set up 2026-09-13, after cycle 8, with the user's approval. It is a sanitised copy of the
lab's config, for measuring WRITE behaviour the shared reference must never receive.

| | |
|---|---|
| URL | `http://100.85.222.1:32410` (kids' Tailscale address only) |
| Token | `ssh ben@kids /home/ben/plexscratch/scratch.sh token`: the copy's own `.LocalAdminToken`. **It changes on every start**, so fetch it after each `up`/`reset`. |
| machineIdentifier | `12081b20eeb2f1ca3be0473ea444236d942d156a`; unclaimed, same 1.43.4 image by digest |
| Library | identical to the lab at copy time (5 movies, Firefly 6, same ratingKeys) |
| Control | `/home/ben/plexscratch/scratch.sh up | down | reset | status | token | long | short`; source in `harness/scratch/` |
| Media | `short` (default): the lab's ten-second clips. `long`: `media-long/` (movies 30 min, episodes 22 min, same paths and streams, built by `make-long-media.sh`) with its own `pristine-long/` baseline taken after a re-analysis. The mode sticks in `.mode` across `up`/`reset`. |
| Undo | `./scratch.sh reset` restores from `pristine/`, which was copied once; the lab is never read again |

Why it is built the way it is (all measured while setting it up):

- **It cannot reach plex.tv.** PMS sits on an `internal: true` docker network with no
  default route and no DNS (TCP to 1.1.1.1 is "Network is unreachable"). `make` strips
  `PlexOnlineToken`, account, email and certificate from `Preferences.xml`, skips Logs,
  Cache, `.LocalAdminToken` and `Setup Plex.html` (the only other places the token or
  certificate id appear), and mints new machine identifiers. A copy announcing the lab's
  identity would rewrite the lab's plex.tv registration.
- **Two socat hops.** An unclaimed PMS lets only LOOPBACK in, and even then 401s a
  `Host` that is not 127.0.0.1/localhost. So a hop inside PMS's network namespace
  (port 32499; NOT 32401, which PMS binds itself and exits over) makes every request
  loopback, and clients send the local admin token. A session's client address
  therefore always reads 127.0.0.1 on the copy.
- `make` paused the lab's PMS for the seconds the copy took (its database writes its
  WAL continuously); the kids snapshot matched before and after.

### Production PMS — `fun` (READ ONLY)

| | |
|---|---|
| URL | `http://fun:32400` |
| Token | fun's `plex_token` in `docs/operations/credentials.md` |
| Version | 1.42.2 (plexsim reports 1.42.2 too) |
| Sections | 15 and 20 movies, 19 shows (tens of thousands of episodes) |
| Rule | **GET and HEAD only, ever.** It is production. |

fun is the second witness. A rule is only implemented when both servers agree,
or when the difference is a version (1.43-only fields) or catalog content.

### plexsim under test

```bash
# UI-surface fixture (the suite WRITES; restart to undo)
RD_TOKEN=dummy exec python3 server.py --port 32541 --catalog tests/catalog_ui.json --assert-claimed
# full catalog (gitignored, RD links are account-scoped)
RD_TOKEN=dummy exec python3 server.py --port 32543 --catalog /Users/bensarmiento/debridmediamanager/plexsim/catalog.json --assert-claimed
```

From an agent tool, start servers with `subprocess.Popen(..., start_new_session=True)` or as
a background task (Claude Code: `run_in_background: true`). `nohup … &` dies with the tool
call. `RD_TOKEN` must be set or startup fails.

**Ports.** Pick freely EXCEPT: `32400`, `32502`, `32511`, `32512`, `32595`, `32596` and `8787`
belong to other sessions -- a server started on one of those silently fails to bind and every
request then goes to somebody else's plexsim, which reads as a wildly failing suite. `32599`
is `test_ui_surface.py`'s own gated server. Cycle 10 used 32583–32599 around those.

`tests/catalog_ui.json` holds:

- movies `1007` Tears of Steel, `1008` and `1009` (fixture copies);
- show `20001` The Simpsons;
- seasons `30001` (S19) and `30002` (S20);
- episodes `1001` (S19E09), `1002` and `1003` (S20E01 and S20E02);
- sections `1` movie and `2` show.

---

## 4. Running the suites

```bash
python3 tests/test_ui_surface.py --base http://127.0.0.1:32541          # restart the server after
python3 tests/test_navigation.py --base http://127.0.0.1:32543 --catalog catalog.json
uv run --quiet --no-project --with plexapi python3 tests/conformance.py --base http://127.0.0.1:32543
python3 tests/test_all.py --catalog catalog.json --local http://127.0.0.1:32543 --public http://127.0.0.1:32543 --skip-decode
python3 tests/test_mediainfo.py        # no server: mediainfo.py against real files' probes and a real PMS's analysis
```

Restart the fixture server BETWEEN runs, and restart it **by waiting for the port to be
free** -- a kill plus a `/identity` poll can be answered by the process you just killed, and
the suite then runs against a server it has already written to. Check that the new pid holds
the port:

```python
for pid in lsof_pids(port): os.kill(int(pid), signal.SIGTERM)
while lsof_pids(port): time.sleep(0.5)          # the PORT, not a health check
proc = subprocess.Popen([...], start_new_session=True)
while str(proc.pid) not in lsof_pids(port): time.sleep(0.5)
```

`test_ui_surface.py`'s own gate checks start a **token-gated server on 32599**. Leave that
port free or they measure whatever else is listening on it and report six false failures.

Suite conventions:

- **Where new checks go.** The newest block of `test_ui_surface.py` sits right
  before this line, and new checks go there:
  `status, _, _ = request(base, "DELETE", "/library/metadata/%s?proxy=1" % movie)`
  Everything after it deletes a row and a section.
- The `request()` helper sends `X-Plex-Platform: Chrome`. Decisions refuse a
  request with no real client profile (cycle 6).
- **Fixture ids.** `c5_movie`, `c5_show`, `c5_season` and `c5_episode` are picked
  from the live listings. `detail_of(key)` reads a detail row. Grid and hub rows
  are a lighter projection, so any check that needs streams or tag ids must read
  the detail row.
- **Wrong beliefs.** When a re-measurement shows an existing check encoded a
  wrong belief, **update the check**, with a comment saying what was re-measured.
  Never delete it. Cycle 10 did this four times: two checks completed a play with a
  single beat at the end (the crossing rule), and two more assumed the old display-title
  and artwork behaviour.
- **A check that MERGES or deletes a fixture row starves every check after it.** The suite
  is one long sequence against one server: by the anchor there are two movies, not three.
  The match check absorbs the SPARE row for that reason, and a check that needs unwatched
  rows has to count views rather than assume none.

---

## 5. The cycle procedure

One cycle, as run for cycles 8–10:

1. **Snapshot the reference.** Run `refcheck7.py` against the baseline, which is read-only,
   and keep the output to diff at the end.
2. **Prepare the instruments.** Write a fresh mode-600 `tokens.env` in the scratch directory,
   reset plexscratch (`./scratch.sh short` unless the territory wants the long media), and
   export the current `main` into a frozen tree the hunters talk to:
   `git archive main | tar -x -C <scratch>/headN`, served on a port of its own.
3. **Launch the hunters** with the Agent tool, one territory each. The prompt is assembled
   from `handover/tools/hunt_prompts.md`, which holds the shared preamble and the last
   cycle's territories verbatim. For a new cycle update:
   - the cycle number and "Verify against the CURRENT plexsim (commit …)";
   - **Already fixed**: the previous cycle's items from §7 -- a hunt against an un-updated
     list re-reports what just landed;
   - **Deliberately NOT implemented** and **Refuted**: the items in §9;
   - the SAFETY section (kids and fun GET-only, the snapshot before and after);
   - the ports, the scratch paths, and the report format ending in
     *If you find nothing, say exactly "no findings"*.
   A long prompt is easier to hand over as a FILE: write it to the scratch directory and give
   the agent one line telling it to read that file and follow it.
4. **Re-measure every finding yourself** before touching code, against the same instrument the
   hunter used. Hunters are often partly wrong, and one finding per cycle is typically
   refuted outright -- cycle 10's "the Session block appears only after a decision" did not
   reproduce with a decision that answers 200.
5. **Implement in an isolated worktree:** `git worktree add -b cycleN <scratch>/wtN origin/main`.
6. **One commit per finding**, each with its check, proven **against its own parent commit**:
   - `c10/sdrive.py` does this for a check that needs a server: export HEAD, serve it, apply
     the patch, serve that, run the patch's block against both, require fail-then-pass, commit.
   - `c10/mdrive.py` does the same for an offline check (`tests/test_mediainfo.py`).
   - Re-exporting the parent per patch is the point: proving against an older tree proves less
     than it looks, and a parent tree missing a sidecar dies at import and reads as a failure.
7. **Run every suite** on the fixture and on the full catalog, from a freshly restarted server.
8. **Document.**
   - Add a `#### The Nth pass …` section to `docs/findings.md`, inserted just before
     `### Emulation calibrated against the account's own claimed servers`, ending with a
     "Measured and not acted on" list that includes what was REFUTED.
   - Add trap bullets to `CLAUDE.md`, just before `## Conventions`.
   - If a sidecar fixture was added, list it in CLAUDE.md's Layout paragraph.
   - Update this file: §1, §6, §7, §9 and §12.
9. **Commit and push.**
   - Scan the whole branch diff for secrets: both tokens, `X-Plex-Token=…`, real-debrid
     `/d/` links.
   - Commit as `Ben Adrian Sarmiento <me@bensarmiento.com>`, a semantic single-line message,
     **no attribution lines**.
   - `git merge --ff-only cycleN` on `main`, then `git push origin main`.
   - Remove the worktree, `git branch -d cycleN`, `git worktree prune`, delete the remote
     backup branch, and confirm `HEAD == origin/main`.
10. **Snapshot the reference again** and diff it against step 1.

Push the work branch to `origin` as you go: it is the only backup of a cycle's commits while
the scratch directory holds the worktree.

Large cycles are split into batches, one per hunt: cycle 10 ran three (media analysis,
writes, the session surface), each merged to `main` on its own once its suites passed.

---

## 6. Cycle history

The hunter runs (from the session transcript):

| Cycle | Hunter A | Hunter B |
|---|---|---|
| 1 (two rounds) | playback session surface (fable) | filter and smart-query surface (opus) |
| 2 | playback and queue surface (opus) | search, hubs and system surface (fable) |
| 3 | client-driven surface (opus) | library writes and multi-user (fable) |
| 4 | broad sweep (opus) | sequence and state sweep (fable) |
| 5 | wire-format sweep (opus) | settings and item sweep (fable) |
| 6 | playback and streams sweep (opus) | notifications and identifiers sweep (fable) |
| 7 | probe table + corpus replay diffed mechanically (opus) | python-plexapi as a third-party client (fable) |
| 8 | every URL template in the web bundle, three servers, JSON and XML (opus) | the XML path, four client families, plexapi extended (fable hit a session rate limit; re-run on opus) |
| 9 | write semantics on plexscratch: edits, play state, playlists, collections, queues, libraries, merge (fable) | the community OpenAPI description, every GET, three servers (fable hit a rate limit; re-run on opus) |
| 10 | the writes cycle 9 did not reach, on plexscratch (fable) | `mediainfo.py` against fun's analysis of 35 real files (opus) |
| 10 batch 3 | the playback and session surface on plexscratch's long media (opus; the first attempt died on a model weekly limit and was relaunched against the merged head) | — |

Commits of the loop era, newest first:

```
dd52196 docs: hand over the finished cycle 10 and point the loop at cycle 11                             (cycle 10 batch 3: 8 commits from e8af2b5)
173305c docs: hand over cycle 10 and point the loop at its unfinished third batch                        (cycle 10 batch 2: 12 commits from c318783)
a7b3b77 docs: record what the tenth pass's media batch measured and did not act on                       (cycle 10 batch 1: 24 commits from 02af369)
4f2bb31 fix: unmatch an episode or a season to a local guid and its generated title, keeping its Guids   (cycle 9 batch 2: 49 commits from 7589e32)
ae19756 test: expect a library's albums leaf to answer, as both servers do                               (cycle 9 batch 1: 17 commits from 985293a)
c8c072a fix: keep session flags out of the catalog, carry full related rows, ...                           (cycle 8 batch 2)
5b7d4ff ...                                                                                                 (cycle 8 batch 1)
c62c51b feat: sort and filter by the row a scope names, match titles by word, and answer the reads python-plexapi makes   (cycle 7 batch 2)
11a5100 fix: send no theme that cannot play, and shape hub rows and excludeFields like a real PMS                        (cycle 7 batch 1)
3e52226 feat: refuse the decisions a real PMS refuses, and push the frames every write pushes                            (cycle 6)
daf88c8 feat: answer the settings forms and agents a real PMS answers, and speak its wire format                          (cycle 5)
010d196 test: fail the poster check on a binary body instead of crashing the run
2596437 feat: sort like a PMS, save artwork to the path the client writes, and cascade a delete                           (cycles 1–4 …)
ea98659 feat: refuse the artwork requests a real PMS refuses, and route by verb
329e7fb feat: push the whole notification lifecycle, and honour the channel filters
7a615ef feat: gate on the token a real PMS demands, and send the headers it sends
9b1cd29 feat: let the Edit dialog actually save, and validate every edit parameter
…       (git log has the full list: 42 commits since 9a68e00)
```

Cycles 1–4 are recorded in `docs/findings.md` from "The filter query language"
(about line 748) through "A negative result worth keeping" (about line 2039).
Cycles 5–7 start at "#### The fifth pass" (about line 2067); cycle 10's three batches are
"#### The tenth pass: what a real PMS writes for a real file", "#### The tenth pass, second
batch: the writes cycle 9 did not reach" and "#### The tenth pass, third batch: the playback
surface, on media long enough to drive it".

---

## 7. What cycles 5–10 changed

Add these to the hunt prompt's "Already fixed" list. Cycles 1–10 are already there,
verbatim, in `hunt_prompts.md` -- a hunt launched against a list that stops a cycle short
spends itself re-reporting what just landed.

**Cycle 5 (`daf88c8`)**

- The Add Library form: `/library/sections/prefs?type&agent`, from
  `section_prefs_new.json`.
- Setting-write validation and typing on `/:/prefs`, library prefs and item
  prefs.
- `/system/agents` with its per-type lists (`system_agents.json`), scanners per
  TYPE, `/:/plugins/*` answering 404.
- `/devices` and device attribution; statistics rollups when no timespan is
  given.
- users/top, `/nearest`, `/channels/all`, `/log` validation, `/services/browse`,
  `/library/tags`.
- The wire format:
  - Accept negotiation is exact;
  - JSON carries no null and no `[]`;
  - XML number formats.
- Media flags.
- Row rules:
  - `titleSort` is article-stripped and sent only when it differs;
  - `originalTitle` is never a filename;
  - plus rules for Field, Location and rating.
- The grid and hub row projection (`_grid_row`); tree strings; the season
  children container.
- `mixedParents`; the flags on `GET /`; global `/hubs`; library shelves; related
  hubs; `/common`; `/sorts` per type with includeAdvanced ordering; collections
  read as items.

**Cycle 6 (`3e52226`)**

- **Decisions**:
  - validation of the path, mediaIndex/partIndex, and the client profile from
    Resources/Profiles;
  - a refusal answers 2000/4005 with no Metadata;
  - the decision row projection;
  - resourceSession: the session id, then the client id, then "".
- Refusals of transcode start, ping and stop; `/transcode/sessions` writes
  answer 404.
- Streams and parts:
  - `/library/streams` unknown ids answer 404;
  - subtitle search language validation;
  - malformed stream ids on PUT parts;
  - deeper part paths;
  - subtitle flags;
  - `/downloadQueue`.
- Notifications:
  - timeline type 15 for a playlist and 18 for a collection;
  - the collection create and delete frame sequences;
  - edit frame states; movie frame titles carry the year.
- `/children` member DELETE; the collection create response.
- Timeline rules:
  - the 60 000 ms minute is strict on every state;
  - a play completes past 90%, once per session;
  - the response `size` counts only Bandwidths.
- Request edges:
  - a query pair with no `=` is a 400;
  - an empty path segment is a 404 and one trailing slash is stripped;
  - a leading `//` is read off `requestline`;
  - a non-numeric section is a 404;
  - `/children` on a leaf is a 400;
  - `/activities/{id}`;
  - a comma list with one numeric member;
  - ids ≥ 2^31 are a 400;
  - `/playQueues/{word}` is a 400;
  - a repeated `type` selects nothing;
  - an empty container size is a 500.
- The eventsource first ping comes 10 s in; `filters=` empty includes log.

**Cycle 7 batch 1 (`11a5100`)**

- No `theme`, `parentTheme` or `grandparentTheme` anywhere.
- `excludeFields` applies to every row and to the container.
- Hub row projection by type: a show on a `mixed` shelf is the full row;
  `/hubs/home/recentlyAdded` and `/hubs/search` rows are light.
- `includeLibraryPlaylists` adds `recent.library.playlists.{id}` after Recently
  Added.
- A season's and an episode's `/arts` are empty.
- An episode's `Image` order is coverPoster, then snapshot.
- `probes/ui_surface.py` sends `PUT /updater/check` only under `--mutate`.

**Cycle 7 batch 2 (`c62c51b`)**

- **Type-prefixed sort keys**: `movie.titleSort`, `show.titleSort`,
  `season.index`, `episode.id`. Ties on season and episode lists fall back to
  broadcast order.
- **Scoped filters**: the value is read from the named row (`scope_rows`), and a
  descendant scope matches when any child does. `index` is filterable.
- **`title=` is a word-prefix matcher.** `titleSort`, `summary` and `tagline` are
  case-insensitive equality; `==` and `!==` are case-insensitive.
- `/library/all` is a query.
- Four reads plexsim used to 404:
  - `/hubs/sections/{id}/manage`;
  - `/library/sections/{id}/timeline`;
  - `/library/metadata/{id}/clearLogos`;
  - `/library/metadata/{id}/themes`.
- `/services/browse`: `includeFiles` must be present; `=1` adds File rows.
- Rows:
  - recentlyAdded rows carry the librarySection ID, Title and UUID plus
    `allowSync`, and are light;
  - a season in a `type=3` listing has no leaf counts;
  - `/children` rows carry no librarySection fields;
  - a show's listing row carries `duration` and `originallyAvailableAt`;
  - a show's Location has no `id`.
- A season with its own year carries `year`, `titleSort` = its title, and no
  `parentYear`.

**Cycle 8 batch 1 (`5b7d4ff`)**, from the bundle's URL templates

- Section subsets `onDeck`, `recentlyAdded`, `newest`, `recentlyViewed`,
  `recentlyViewedShows`: title2, episode lists on a show library, allowSync, caps.
- `/library/sections/{id}/autocomplete` (phrase at a word start, `queryRange`, tag rows).
- `/filters` keyed by `type=`, Duplicates under includeAdvanced (`section_filters.json`).
- By Folder `?parent=`; `/library/sections?includePreferences=1`; `/grandchildren`.
- Paged `/children`: All-episodes row kept at Start=0 with counts and thumb, light
  paged rows, `mediaCount` under excludeElements=Media, All episodes first in XML.
- `guid=` prefix on listings and `/library/all`; `/:/prefs/get`; the comma-list
  container; manage?metadataItemId 400; `/neighborhood/devices`;
  `/channels/recentlyViewed`; subscriptions/template and defaultLibraries 400s;
  `/devices.xml` is the plug-in dispatcher.

**Cycle 8 batch 2 (`c8c072a`)**, from the XML path and non-web clients

- `/status/sessions` no longer writes playback flags into the catalog.
- Related and postplay shelves carry full rows; `/library/search` rows are light and
  title-only; server-wide onDeck/recentlyAdded page, are light, and list seasons.
- A season's episode list carries its own year as `parentYear`; show `/collections`
  carries `nocache`.
- The tree: blank season titles, root parentTitle, episode `metadataType`, show Settings.
- iOS types All episodes as a season (the platform header is now normalised into query).
- `movie.random` sorts; `/library/tags?type=318|319`; stream `/levels` 404.
- XML child element order (`xmlout._CHILD_ORDER`) and `Meta` first.

**Cycle 9 batch 1 (`985293a`..`ae19756`)**, from the community OpenAPI description's GETs

- `/library/sections/all`; every MediaContainer pages when both paging headers are sent.
- Refusals: one agent by identifier 404; stream loudness 404, a sidecar extension 501, a
  bare `/file` 400; the live TV reads a server with no tuner refuses.
- Season and episode `allLeaves`; voice search keeps the asked type; `excludeElements` on
  a listing; bandwidth statistics filters; `/status/sessions/history/{id}`.
- `/hubs/metadata/{id}`; section `allLeaves`/`albums`/`cluster`/`moment`/`computePath`/
  `nearest`; `/library/people/{id}` and `/media`; `/library/matches`; `/hubs/items`;
  `/sorts` keyed by type (`section_sorts.json`); `randomArtwork` and a library's `/arts`.

**Cycle 9 batch 2 (`7589e32`..`4f2bb31`)**, writes measured on plexscratch

- Edits: the section save path MERGES tags; an unlocked Sort Title is re-derived (items,
  collections, playlists); the save is scoped by type and library, and a type-scoped filter
  there is a 400; dates are parsed and set the year on the section path; the section path
  stores `userRating` and ratings are numbers; the section lock grammar, the lock-only
  columns and the `Field` column order; a parent's title and studio are copied to its
  children; frame titles (`Show (year)`, `Show S01 E07`), a section save's frame order, list
  rename frames; generated `Season N`/`Episode N` titles and the kept sort form; value-less
  fields accepted and discarded; POST on an item and PUT on `/library/all` 404.
- Play state: the identifier refusal order; an episode scrobble counts on its season and
  show; one on-deck row per show; `/:/progress` never completes; a negative rating keeps
  `lastRatedAt`; timeline `time`/`duration` 400s; `complete` until a `stopped` beat closes
  the play; Remove from Continue Watching is a PUT with a ratingKey.
- Playlists: create validation; unknown item 404, DELETE on the items clears, smart lists
  refuse item writes; a move after nothing goes to the front.
- Collections: create validation; a show or season collection holds that row; add
  refusals; `collectionSort` order and moves from the order shown; labels and `type=18`
  saves; a rename leaves member tags alone.
- Play queues: `key=` selection; per-client retention; `playlistID` adds; DELETE on the
  items keeps the selected one; deleting the selected item; moves to the front;
  `includeBefore`/`includeAfter`; a foreign server's uri.
- Libraries: an edit applies language and prefs and runs the measured scan; refresh all;
  a movie's settings on its row; a create applies prefs and answers bare; analyze frames;
  clean-bundles and delete activities.
- Merge, split, unmatch: merge refusals; merge and split frames, and a split onto a key
  never used; an episode or season unmatches to `local://` and its generated title.

---

**Cycle 10 batch 1**, `mediainfo.py` against fun's own analysis of 35 real files

- Codec names: DTS is `dca` (the Media's is `dca-ma` for DTS-HD MA), a Blu-ray subtitle `pgs`,
  a DVD one `vobsub`, and ffprobe's `+ Dolby Atmos` profile is not written.
- Languages: named in the language itself, ISO 639-2/T code, 639-1 tag (`languages.json`).
- Ten-bit depth from the pixel format; `colorRange`; `scanType` from the field order;
  `codedHeight` aligned to macroblocks for H.264; `streamIdentifier`; `codecID`; audio
  `bitDepth`; ffprobe's channel layout verbatim; the average frame rate; rounded bitrates;
  ffprobe's unknown profile and level markers dropped; a video stream's language and title.
- Every video stream (a Dolby Vision enhancement layer is one; a cover image is not), and the
  eight `DOVI*` attributes.
- The resolution label bucketed by width AND height (`2k`, `2.7k`), and the aspect ratio
  snapped to one of six values.
- The Media's audio fields taken from the WIDEST audio track.
- Track titles composed, and the codec added to `displayTitle` for the thirteen products that
  get it (`_with_client_titles`).
- A stored catalog's media fields corrected at load, with the titles composed last.

**Cycle 10 batch 2**, the writes cycle 9 did not reach

- A library's extras leaf and hub; an item's `/extras/{n}` is a 404.
- A settings save pushes a `preference` frame per id, and `FriendlyName` names the server.
- Track selection takes a POST.
- Butler runs push a `butler` activity and answer empty; `DELETE /butler/{Task}` is a 404.
- A smart list's Save replaces its stored query, `limit` included.
- An artwork upload stores the image and re-versions the row; a pick is always a versioned
  path, never the url; a playlist's and a collection's artwork list answer.
- Fix Match needs a `name`, merges onto a guid another row holds, and ends its activity.
- A scan names its library in a `Context` and steps a show library by season.
- A show's prefs write pushes the refresh cascade of its seasons and episodes.

**Cycle 10 batch 3**, the playback surface on real-length media

- A play completes only when a beat CROSSES 90% inside its own session; past 90% the answer is
  `complete` or `ignore`, never `progress`.
- A beat's own `duration` is the only one that counts: none, or `duration=0` (a 200), stores
  nothing and completes nothing. `/:/progress` still stores without one.
- A beat at or under a minute deletes the stored resume point; a progress beat stamps
  `lastViewedAt` on the season and the show.
- `playQueueItemID` and `playQueueID` on every `playing` frame of a queue-driven session.
- A `/status/sessions` row drops `Guid` and `Image`, and its `Player` names a `profile`.
- `/statistics/media` filters by `accountID` and `deviceID`.

## 8. Safety rules and incidents

The rules:

- **fun is production: GET/HEAD only.**
- **kids is read-only for hunting agents since cycle 7.** Every write needs its
  undo listed first, and a snapshot before and after.
  - Never delete a section or an item.
  - Never use merge, split, match or unmatch.
  - Never run maintenance endpoints (emptyTrash, clean bundles, optimize).
  - Never change network or auth prefs.
  - **Never write server, library or item settings.**
- **Never `PUT /library/metadata/{id}/merge` on the lab.** It is not reversible;
  a split re-keys.
- **Never write to plex.tv's device registry, and never repeat claim exchanges.**
  Both have signed real servers out before.
- **Never commit a token, a Real-Debrid `/d/` link or a tunnel credential.**
  - The workspace root, `docs/providers/` and `docs/operations/` are not git
    repos and hold live secrets. Never copy from them into tracked files.
  - `catalog.json` and `run.sh` are gitignored for this reason.
- **No attribution lines in commit messages.**
- **CDP on kids: 9223 for general browser work, never 9224** (plexlab's, which
  records bodies). Windows CDP comes first.

Incidents, so the rules make sense:

| When | What happened | Repair |
|---|---|---|
| Cycle 4 | A hunter sent `DELETE /library/sections/1` | Movies restored from Plex's backup; Cosmos Laundromat re-keyed 30 → 18 |
| Cycle 5 | Writing `showOrdering=bogus` on show 6 silently re-matched episodes 8–10 to local guids | `PUT /library/metadata/6/refresh`, which also gave season 7 its year and titleSort |
| Cycle 6 | A stray `POST /downloadQueue` created queue 1 | Cannot be deleted (404); left in place |
| 2026-09-12 | A broken-DB copy parked at `kids:/home/ben/plexlab/db-broken-20260912/` | Deleted at the user's request; the live config was verified intact |

Local processes **not** belonging to this work, left alone on purpose:

- `32400` is another session's plexsim (Xcode Python).
- `8787` is another session's `http.server`.
- `32502` runs plexsim on `catalog.example.json`.
- `32511` belongs to an old agent's parent process, which restarts it.
- `32595` and `32596` are plexsims of unverified origin.

---

## 9. Measured and deliberately NOT implemented

Keep these in the hunt prompt's "do not re-report" list. Each one's reason is in
`docs/findings.md`.

Inherited verbatim from the cycle 1–6 prompts:

- ICU collation; image filters; `random:` sort regeneration.
- updatedAt/contentChangedAt semantics.
- Session ordering and the Session block; duplicate collection titles.
- Scan `refreshing` observability; `transcoderVideo*` fields.
- XML-only `/system` responses; text/plain error bodies.
- Part `exists`/`accessible` on metadata reads; `/livetv/epg` countries and
  languages.
- Legacy agents and the `/:/plugins` listing; typo-tolerant search.
- Season and episode external Guids (no data); Common's episode/season quirks;
  the random actor/director shelf.
- Markers and chapters (no data); `/services/browse` mount points; mp4
  `optimizedForStreaming`.
- The eventsource transport (chunked vs Connection: close); download-queue item
  adds.
- A valid transcode start (plexsim has no encoder).
- Anything on plex.tv.
- Pure catalog-content differences.

Added in cycle 7:

- The `count=1` hub quirk on kids (it drops Recently Watched; fun does not).
- Orphaned collection tags carrying a `guid` (kids does; 3 of fun's 372).
- `/system/agents` legacy `primary`/`MediaType` shape. The modern rows already
  match exactly.
- `/library/metadata/{id}/matches?manual=1` candidates (20 from the provider;
  would need a provider search per request).
- `thumbBlurHash`/`artBlurHash` (would need image decoding).
- `slug=` 500 on listings. (`guid=` was implemented in cycle 8: the client's
  availability lookup sends it.)
- `/squareArts` (200 on 1.43.4, 404 on 1.42.2, and plexsim reports 1.42.2).

Added in cycle 8:

- fun's `newest` bound (47 movies, 60 episodes; kids lists all) and fun's Recently
  Viewed order.
- ICU numeric collation in autocomplete order.
- All episodes at `Start>0` (kids drops it, fun keeps it; plexsim follows kids).
- `guid==` / `guid!=`; untyped `/library/all` with `show.guid`.
- `/system/services/url/lookup`'s plug-in framework traceback (500).
- Mixed folder levels (directories beside files); no plexsim catalog has one.
- fun's postplay `movie.extras` and episode `tv.ondeck` shelves, related
  `movie.same.actor.N`.
- Child elements outside the measured order list; minted studio tag ids.

Added in cycle 9:

- `/services/ultrablur/image` and `/colors?url=` (no image codec on a stdlib server path);
  `/library/metadata/{id}/file?url=` (the servers disagree); an episode `allLeaves`
  container's disagreeing fields; kids' history filtering by account; genre and actor
  hub identifiers on `/hubs/items`; `/library/matches` provider options; paging on
  write-shaped reads.
- plexscratch's instrument limits: session-bound timelines and timelines with no
  `duration` (ten-second clips).
- Not reproduced on re-measurement: progress `state=playing` opening a session; a bogus
  `playQueueItemID` selecting by ratingKey; a library type change being a 400; a
  collection create with no uri being a 400; smart collections tagging members; a
  whole-library playlist add adding nothing; `continuous=1` stopping at one episode.
- Captured only in part: optimize's activity and type-12 frames, the `showOrdering`
  refresh cascade, a library create's per-item created frames, TV scan subtitles.
- Reference oddities (tag types landing in other columns, `merge?ids=<self>`, the
  `C13` capitalisation) and copy artefacts (item DELETE 400, `editionTitle` 403).

Added in cycle 10, batch 1 (media analysis):

- What the probe cannot give: `refFrames` (ffprobe 8.1 no longer prints `refs`); a Matroska
  video stream's bitrate, and DTS-HD, TrueHD, FLAC, Opus and subtitle bitrates (the server's
  own analysis); `headerCompression`; Part `packetLength`; `hasThumbnail`, `indexes` and the
  `failureBIF*` fields; `eia_608` captions.
- Which audio track is `selected` (it follows the account's language settings).
- Counterexamples left alone: E-AC-3 with Atmos in an mp4 (fun reads its 5.1 core where
  ffprobe says 7.1); DTS Express has no channel layout; TrueHD in an mp4 has no bit depth.
- Version, not rule: 1.43.4 labels an untagged track English, omits `lc` and the mono layout
  on most AAC clips, and writes its clips' Media bitrates about 8 kbps under the probe's.
- Leads with one sample each: a `.webm` reported as container `mkv`; `anamorphic: false` on a
  333:320 sample aspect ratio; an AVI audio track numbered with no id in the probe; a forced
  track with no linguistic content leaving `Forced` out of its extended title; `AVC` standing
  in for H.264 in a title.

Added in cycle 10, batch 2 (writes):

- What the copy cannot do: the download queue (no account), `POST .../posters?url=` to an
  external image (406, it is offline), a provider match search, and
  `POST /library/metadata/{id}/subtitles` (a 500 with nothing in its log).
- Captured and not reproduced: a library create's per-item `created` frames and the
  `backgroundProcessingQueue` frame a minute later; the 201 row's
  `metadataAgentProviderGroupId`, `uuid`, `createdAt` and `contentChangedAt: -1`; the
  `provider.subscriptions.process` activity pairs a scan ends with.
- Copy oddities: the artwork write's response body and the row's `thumb` differ by the
  copy's timezone offset; a smart collection's `childCount` goes stale where its `/children`
  does not; fun's largest library times out on `/extras/all`.

Added in cycle 10, batch 3 (the session surface):

- **Refuted:** the `Session` block appearing only after a DECISION. Re-measured with a
  decision that answers 200, the copy carries no `Session` block at all.
- An unclaimed copy 401s `/status/sessions/terminate` in every form and 404s `/statistics/*`,
  so neither could be measured there; the statistics rules came from kids and fun instead.
- Only Plex Web was ever seen, so `Player.profile` is `Web` for every session.
- An unmatched row's timeline answer carries no `skipCount`, `viewCount` or `viewOffset`; no
  plexsim catalog has a row without an identity.
- `/statistics/resources` per-second host samples (a stdlib server on another machine has no
  such numbers).

---

## 10. Next: cycle 11

Cycle 10 is complete: all three batches are measured, implemented and merged. Territories
worth taking next:

1. **The writes this cycle could not reach**: the download queue (it needs a claimed copy),
   a subtitle upload, and the library-create frames recorded in findings.md as captured only
   in part.
2. **The XML path of cycle 10's writes and session beats**, which were compared as JSON.
3. **A second media corpus**: music and photo libraries are wholly unmeasured, and every media
   rule in this cycle came from video.
4. **Terminate and the statistics surface on a CLAIMED copy.** An unclaimed plexscratch 401s
   `/status/sessions/terminate` and 404s `/statistics/*`, so neither could be measured there;
   both are recorded as instrument limits rather than as findings.

Before launching: refresh `hunt_prompts.md` (§5 step 3 lists what), write a fresh
`tokens.env`, pick ports clear of the reserved ones (§3), snapshot kids, serve the current
`main` from a frozen tree, and reset plexscratch -- `./scratch.sh short` first unless the
territory wants the long media, which is the mode it is left in. **One hunter writes to
plexscratch per cycle**; a second one measuring writes at the same time reads the first one's
state as the server's.

A note on hunts that die. Two of the last three cycles lost a hunter to a model rate limit.
The prompt file survives, so a relaunch is cheap -- but relaunch it against the CURRENT head,
with the Already-fixed list extended by whatever landed in the meantime, or the second run
re-reports the first batch's work.

## 11. Tooling and environment gotchas

- **`grep`/`rg` go through `rtk` in this shell and can mangle or empty the
  output.** Search with `python3`.
- zsh expands a leading `=`, so `echo ======` fails.
- macOS `ps -o etimes` is invalid; use `etime`.
- `http.server` collapses a leading `//` in the path before your handler sees
  it. Read `self.requestline`.
- Cloudflare 403s a `Python-urllib/*` User-Agent on the media path. Probes set
  `User-Agent: curl/8`.
- `catalog.json` RD links are account-scoped. Another key answers
  `unavailable_file`, which surfaces as a 502 on every part read.
- Background servers started by an agent tool show "failed, exit 144" when you
  kill them later. That is the kill, not a crash.
- The base64 in `/services/browse/<b64>` is sent **unquoted**; `%3D` is not
  decoded.
- Item settings on the reference live in `/library/metadata/{id}?includePreferences=1`
  (the `Preferences` block). `/library/metadata/{id}/prefs` is a 404 there.
- **A suite run that raises stops without its summary line.** `test_ui_surface.py`'s
  `get()` returns THREE values; a two-value unpack crashed mid-run and looked like a
  short, all-passing log. Always look for `N/M checks passed` before trusting a run.
- Run long suites with `run_in_background: true`; a foreground run was cut off once.
- Only the paging headers and `X-Plex-Platform` are normalised from headers into
  `query` (server.py, the `for name in (...)` loop in the request handler).
- A hunter on `model: "fable"` can die on a session rate limit ("resets 1am"); re-run
  the same prompt on opus rather than stall the cycle.
- **Never pass code or Markdown through an UNQUOTED heredoc** (`<<EOF`). The shell runs
  every backtick-quoted word inside it as a command and substitutes the output: cycle 9
  committed three patches whose comments, test labels and findings had lost every
  `word` in backticks, and a fourth never ran at all. Write patch scripts to files with
  the Write tool (or use `<<'EOF'`) and pass values as arguments.
- **Address every git command at its worktree with `git -C <path>`; never rely on a `cd`
  earlier in a chain.** A heredoc ends the `&&` chain it sits in, so when a step before it
  fails, the commands after the heredoc still run -- in whatever directory the shell was
  left in. That once committed two changes on `main` in the shared checkout instead of
  the worktree branch (caught before any push: `git log origin/main..main`).

- **`rtk` can swallow a redirected `ssh` stdout.** `ssh host cmd > file` wrote an empty file
  while `ssh host cmd | wc -c` counted 42 bytes; write through a pipe (`| cat > file`). And
  `wc -c < file` read 0 for a 42-byte file: check sizes with `stat -f %z`.
- **A test server started with `( ... &)` from a foreground call can die later.** Start
  long-lived servers with `subprocess.Popen(..., start_new_session=True)`.
- **`cmd | tail` hides a failure from `&&`.** Chain drivers under `set -o pipefail`.
- **A check that `%`-formats a path twice breaks on an encoded `%2F`**; build the path in
  a function with one substitution.
- **plexscratch's token changes on every start, `reset` included**; re-read it after each.
- **plexscratch's SHORT clips are ten seconds long**: send timelines with an explicit
  `duration`, or every beat is ignored. (The reason, found in cycle 10: a play completes
  only when a beat crosses 90% inside its session, and a ten-second clip is crossed at
  once. `./scratch.sh long` is the copy on half-hour media.)
- **A client's new play queue retires its older ones.** A check that builds a second queue
  cannot write to the first.
- **A proof's parent tree needs every sidecar.** `mediainfo.py` loads `languages.json` beside
  itself; a parent tree holding only `mediainfo.py` dies at import, and that reads as a failing
  check. Cycle 10's first run of 16 media commits "failed on the parent" that way and had to be
  re-proven. `c10/mdrive.py` copies the tracked JSON too and refuses a parent run that raised.
- **A `cd` inside a Bash call moves the session's working directory** for the calls after it.
  Use absolute paths, or `cd` back in the same call.
- **CLAUDE.md bullets wrap mid-phrase and use em dashes.** A patch that finds a bullet by its
  opening words has to match the line break; read the lines with `repr` first.
- **ffprobe lists a Matroska cover image as a video stream** (`disposition.attached_pic`).
- **plexscratch in `long` mode**: switch back with `./scratch.sh short` before re-measuring
  something the short clips measured, and remember `reset` stays in the current mode.
- **Restarting a test server is not a kill plus a health check.** The poll can be answered by
  the process you are killing, or by something else already on the port: wait for the PORT to
  be free, start the new one, and confirm the new pid holds it. A whole suite run once went to
  a server that had already been written to, and read as twelve regressions.
- **`test_ui_surface.py` starts its own gated server on 32599.** A proof server there makes its
  six gate checks measure the ungated one and report 401s that never happened.
- **A `git checkout -- <file>` throws away every uncommitted edit in it**, including the ones
  made earlier in the same session for a different reason. Two check updates had to be redone
  that way; commit a fix before restoring a file beside it.
- **A replacement that leaves the old block behind DUPLICATES it.** A suite edit that replaced
  a region but not its `if` header ran the whole tail twice -- 882 checks instead of 845, with
  failures caused by the first copy's writes. Assert the occurrence count after the edit, not
  just before.
- **An assertion in an edit script is the cheap half.** Assert what the file should look like
  AFTERWARDS (`count == 1`, the old text gone), or a "successful" edit can leave a `if False:`
  branch and dead code in a commit.

---

## 12. Artifacts

`handover/tools/` is untracked and token-free. Scripts read tokens from
`PLEXLAB_TOKEN` and `FUN_TOKEN` (cycle 8 wrote both into a mode-600 `tokens.env` in the
session scratch directory and had hunters `source` it; extract them again from
`docs/operations/credentials.md` line 57 for a new session). `cmp.py`, `xdiff.py` and
`probe.py` take the plexsim base from `PLEXSIM_BASE`:

| File | What it does |
|---|---|
| `hunt_prompts.md` | The shared preamble and every cycle's territories, verbatim, newest last (cycle 10's three are at the end). This is the template: copy the preamble, refresh the lists named in §5 step 3, and write the whole prompt to a file for the agent to read. |
| `refcheck7.py` + `ref_cycle5_before.json` | Read-only comparison of kids' library state against the baseline: `python3 refcheck7.py ref_cycle5_before.json`. Item prefs must be read via `?includePreferences=1` (see §11). |
| `probe.py`, `diff.py` | The python-plexapi sweep. `probe.py` drives one PMS through ~460 plexapi read calls behind a session that refuses writes and off-host URLs, and writes `out_<name>.json`. `diff.py` compares `out_sim/ref/fun.json` in its working directory. |
| `ui_ro.py` | `probes/ui_surface.py` with every non-GET/HEAD refused locally, so it is safe against kids |
| `replay_ro.py`, `replay_remap.py` | `harness/scripts/replay.py` restricted to GET/HEAD, and a ratingKey remap for replaying the recorded corpus onto plexsim's ids |
| `cmp.py` | Fetch one path from kids, plexsim and optionally fun, and print the shapes side by side |
| `m.py` | Cycle 8's triage fetcher: one path (or a per-server path each) to kids, fun and plexsim, summarised; refuses write-shaped GETs on kids and fun |
| `xdiff.py` | XML structure diff (attrs and child order per path) |
| `territory_a.py`, `territory_b.py` | The cycle-6 notification-frame and malformed-identifier sweeps, aimed at plexsim |
| `sdrive.py` | **The proof driver for a check that needs a server.** Per patch: export and serve the worktree's HEAD, apply, serve the fix, run the patch's suite block against both, commit only on fail-then-pass. `WT=<worktree> WORK=<scratch> OLD_PORT= NEW_PORT= python3 sdrive.py <patches.py> NAME …` |
| `mdrive.py` | The same for an OFFLINE check (`tests/test_mediainfo.py`): copies every tracked sidecar into the parent tree and refuses a parent run that raised |
| `runblock.py` | Runs one patch's check block against one or more servers, in the suite's own namespace. Both drivers call it |
| `guarded.py` | The write hunt's HTTP client: writes only to plexscratch and the plexsim named by `PLEXSIM_BASE`, GET/HEAD only to kids and fun, and a `WRITE_GET` refusal list for the GETs that write. Also the frame listener and the JSON/XML diffs the hunts use. Reads plexscratch's token from a `scratch.token` beside it |

Everything else from the session is in the scratch directory, which is
ephemeral:
`/private/tmp/claude-501/-Users-bensarmiento-debridmediamanager-plexsim/2bf061c1-6eb4-40e8-82db-c501ea893af5/scratchpad/`.
It holds:

- the captures `c5/`, `c6/`, `c7/`;
- the per-cycle before/after logs `c*_before.log` / `c*_after.log`;
- the plexapi corpora `out_{sim,ref,fun}.json`;
- the unfixed trees `base6/`, `base7/`, `base7b/`.

Cycle 9's scratch directory was
`/private/tmp/claude-501/-Users-bensarmiento-debridmediamanager-plexsim/5e5b69a0-29b2-4bf9-9163-a14559c47805/scratchpad/`.
Its `c9/` holds hunter A's per-operation data and guarded client (`writes/`, `w.py`), the
re-measurement scripts `remeasure*.py` (each write sent to plexscratch and to an unfixed
plexsim, both read back), the patch modules `p_w*.py`, `drive.py` (apply one named patch,
prove its check fails on the unfixed server and passes on the fixed one, commit) and
`runblock.py` (run one check block against servers), plus the unfixed tree `old9b/`.

The four scripts at the bottom of that table were written for cycle 10 and are now tracked,
because they are what enforces "every fix proven against its own parent". Everything else from
cycle 10 stayed in its scratch directory (session `ebe127ef-5796-499a-9bac-d61169c66fc8`,
`c10/`), which is ephemeral:

| Path | What it is |
|---|---|
| `media/` | Hunter B's inputs: 46 ffprobe probes (`probes_fun/`, `probes_kids/`), 381 detail rows from fun, and the survey scripts the resolution and aspect rules came from |
| `writes/`, `sessions/` | Hunter A's and hunter C's logs and guarded clients (`w10.py`: writes only to plexscratch and the local plexsim, GET-only to kids and fun, and a `WRITE_GET` refusal list) |
| `drive.py` | Cycle 9's driver, which proves against ONE fixed unfixed server rather than a per-patch parent. `sdrive.py` in `handover/tools/` supersedes it |
| `p_media.py`, `p_media2.py` | The media patches: codecs, languages, DOVI, resolution, aspect ratio, flags, titles, per-product titles |
| `p_srv.py`, `p_writes.py`, `p_art.py`, `p_match.py`, `p_cascade.py` | The write patches: served titles, stored-catalog normalisation, extras, settings frames, butler, smart saves, artwork, match, the scan Context, the refresh cascade |
| `p_session.py` | The session patches: the crossing rule, duration handling, queue frames, session rows, media statistics |
| `candidates.py` | Every rule a hunter claimed, as a check, before any of it was implemented |
| `remeasure_a.py`, `remeasure_c.py` | Each finding sent to plexscratch AND to a plexsim, both read back -- the re-measurement step, reusable next cycle |
| `prompt_C.md` | The session hunt's full prompt, as an agent-readable file |
| `parent_srv/`, `head10/`, `base10/` | The exported trees the drivers served |
| `lang_table.json`, `survey_res.json` | The language names and the 177 width/height pairs the corpus fixtures were built from |

The full session transcripts, including every hunter's report, are at:

- `~/.claude/projects/-Users-bensarmiento-debridmediamanager-plexsim/2bf061c1-6eb4-40e8-82db-c501ea893af5.jsonl` (cycles 5–8)
- `…/5e5b69a0-29b2-4bf9-9163-a14559c47805.jsonl` (cycle 9)
- `…/ebe127ef-5796-499a-9bac-d61169c66fc8.jsonl` (cycle 10), plus its
  `…/ebe127ef-5796-499a-9bac-d61169c66fc8/subagents/agent-*.jsonl`
