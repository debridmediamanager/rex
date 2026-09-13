# plexsim handover — 2026-09-13 (refreshed after cycle 9)

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
| `main` | the cycle-9 docs commit on top of `4f2bb31`, equal to `origin/main`, clean tree |
| Worktrees and stashes from this work | none (the `cycle9` worktree and branch were removed) |
| Cycles completed | 9. Cycle 9 had findings, so **cycle 10 is due**. |
| Work half-done | **none**. Every finding reported so far is implemented, recorded as deliberately not implemented, or refuted by re-measurement. |

Suite results at `4220879` (the code as of `4f2bb31`, plus test fixes), all run on 2026-09-13:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **830/830**, 4 skipped | The skips need a TMDB key or real RD links |
| `tests/test_navigation.py` on the full catalog | **46/46** | |
| `tests/conformance.py` (python-plexapi) | **29/38** | The 9 failures are byte reads of placeholder RD links (502). They are expected. |
| `tests/test_all.py --skip-decode` | playQueue and decision PASS | Byte reads fail with 502 for the same reason |

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

From an agent tool, start servers as **background tasks** (Claude Code:
`run_in_background: true`). `nohup … &` dies with the tool call. `RD_TOKEN` must
be set or startup fails.

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
  Never delete it.

---

## 5. The cycle procedure

One cycle, as run for cycles 5–7:

1. **Snapshot the reference.** Run `refcheck7.py` against the baseline, which
   is read-only, and note that it matches.
2. **Start a plexsim** for the hunters on a free port.
   - 32541, 32542 and 32543 were used last.
   - Do not use 32511 or 32512, which an old agent's parent process keeps
     restarting.
3. **Launch two hunters in parallel** with the Agent tool, `model: "fable"` and
   `model: "opus"`. Each gets a distinct territory. The prompt template is in
   `handover/tools/hunt_prompts.md`: the verbatim cycle-7 prompts, tokens
   replaced. For the next cycle, update:
   - the cycle number and "Verify against the CURRENT plexsim (commit …)";
   - **Already fixed**: add the cycle-7 items in §7;
   - **Deliberately NOT implemented**: add the cycle-7 items in §9;
   - the SAFETY section: keep it read-only on kids, and keep the snapshot
     before and after;
   - the territory (suggestions in §10);
   - the report format ending in *If you find nothing, say exactly "no findings"*.
4. **Triage and re-measure every finding yourself** before touching code: GET
   on kids, GET on fun. Hunters are often partly wrong. In cycle 7 batch 2 alone:
   - `/system/agents` "wrong shape": false for the modern agents. The shape
     described belongs to the legacy plug-ins.
   - "`title!=` is the negation of the word match": false. `!=` is still a
     substring.
   - "a double space never matches": false. Empty words are allowed.
5. **Implement in an isolated worktree:**
   `git worktree add -b cycleN <scratch>/wtN origin/main`.
6. **Add checks** in a new block, and update any old checks the measurement
   overturns.
7. **Prove it.**
   - Extract the unfixed code: `git archive <previous commit> | tar -x -C <scratch>/baseN`.
   - Run it on another port.
   - Run the **new** suite against both servers. Every new check must fail on the
     old server, and the whole suite must pass on the new one.
8. **Run the other three suites** on the full catalog.
9. **Document.**
   - Add a `#### The Nth pass …` section to `docs/findings.md`, inserted just
     before `### Emulation calibrated against the account's own claimed servers`.
     Include a "Measured and not acted on" list.
   - Add trap bullets to `CLAUDE.md`, just before `## Conventions`.
   - If a sidecar fixture was added, list it in CLAUDE.md's Layout paragraph.
10. **Commit and push.**
    - Scan the staged diff for secrets: both tokens, `X-Plex-Token=…`,
      real-debrid `/d/` links.
    - Commit with
      `git -c user.name="Ben Adrian Sarmiento" -c user.email="me@bensarmiento.com" commit`,
      a semantic single-line message and **no attribution lines**.
    - `git merge --ff-only cycleN` on `main`, then `git push origin main`.
    - Remove the worktree, `git branch -d cycleN`, `git worktree prune`, and
      confirm `HEAD == origin/main`.
11. **Snapshot the reference again** and compare.

Large cycles were split into batches: cycle 7 batch 1 came from the probe/replay
hunter and batch 2 from the python-plexapi hunter.

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

Commits of the loop era, newest first:

```
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
Cycles 5–7 start at "#### The fifth pass" (about line 2067).

---

## 7. What cycles 5–7 changed

Add these to the hunt prompt's "Already fixed" list. Cycles 1–6 are already
there, verbatim, in `hunt_prompts.md`.

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

---

## 10. Next: cycle 10

Cycle 9 had findings, so the loop continues. Suggested territories:

1. **The writes cycle 9 did not reach, on plexscratch.** Smart playlist and collection
   filter edits, butler task runs, server-settings writes (not network or auth), stream
   and subtitle selection, artwork uploads by url, Fix Match offline, extras, and the
   download queue. Item deletes need the copy's media mount made writable first.
2. **Real-length media for the copy.** Rebuild plexscratch's media with long synthetic
   clips so session-bound timelines, progress with a session, and "no duration" become
   measurable; then re-measure the timeline and session surface with the client's own
   session headers.
3. **The write responses on the XML path** (no Accept header), which cycle 9 compared only
   as JSON, and the frames cycle 9 captured only in part (§9), with longer settle times.

Before launching: refresh `hunt_prompts.md`, write a fresh `tokens.env`, pick ports,
snapshot kids, and reset plexscratch. One hunter writes to plexscratch per cycle.

---

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
- **plexscratch's clips are ten seconds long**: send timelines with an explicit `duration`
  and no `X-Plex-Session-Identifier`, or every beat is ignored.
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

---

## 12. Artifacts

`handover/tools/` is untracked and token-free. Scripts read tokens from
`PLEXLAB_TOKEN` and `FUN_TOKEN` (cycle 8 wrote both into a mode-600 `tokens.env` in the
session scratch directory and had hunters `source` it; extract them again from
`docs/operations/credentials.md` line 57 for a new session). `cmp.py`, `xdiff.py` and
`probe.py` take the plexsim base from `PLEXSIM_BASE`:

| File | What it does |
|---|---|
| `hunt_prompts.md` | The two cycle-7 hunter prompts, verbatim. This is the template. |
| `refcheck7.py` + `ref_cycle5_before.json` | Read-only comparison of kids' library state against the baseline: `python3 refcheck7.py ref_cycle5_before.json`. Item prefs must be read via `?includePreferences=1` (see §11). |
| `probe.py`, `diff.py` | The python-plexapi sweep. `probe.py` drives one PMS through ~460 plexapi read calls behind a session that refuses writes and off-host URLs, and writes `out_<name>.json`. `diff.py` compares `out_sim/ref/fun.json` in its working directory. |
| `ui_ro.py` | `probes/ui_surface.py` with every non-GET/HEAD refused locally, so it is safe against kids |
| `replay_ro.py`, `replay_remap.py` | `harness/scripts/replay.py` restricted to GET/HEAD, and a ratingKey remap for replaying the recorded corpus onto plexsim's ids |
| `cmp.py` | Fetch one path from kids, plexsim and optionally fun, and print the shapes side by side |
| `m.py` | Cycle 8's triage fetcher: one path (or a per-server path each) to kids, fun and plexsim, summarised; refuses write-shaped GETs on kids and fun |
| `xdiff.py` | XML structure diff (attrs and child order per path) |
| `territory_a.py`, `territory_b.py` | The cycle-6 notification-frame and malformed-identifier sweeps, aimed at plexsim |

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

Cycle 10 ran in session `ebe127ef-5796-499a-9bac-d61169c66fc8`; its scratch directory's `c10/`
holds hunter B's media corpus inputs (`media/`), hunter A's write logs and guarded client
(`writes/`, `w10.py`), `mdrive.py` (apply one media patch, prove its offline check fails on the
parent commit and passes on the fix, commit), `drive.py` + `runblock.py` (the same for a UI-surface
block against an unfixed server), the patch modules `p_media.py`, `p_media2.py` and `p_srv.py`,
`candidates.py` (every claimed media rule as a check), `remeasure_a.py` (hunter A's write findings
sent to plexscratch and plexsim), and the hunter C prompt `prompt_C.md`.

The full session transcript, including every hunter's report, is at:

- `~/.claude/projects/-Users-bensarmiento-debridmediamanager-plexsim/2bf061c1-6eb4-40e8-82db-c501ea893af5.jsonl`
- `…/2bf061c1-6eb4-40e8-82db-c501ea893af5/subagents/agent-*.jsonl`
