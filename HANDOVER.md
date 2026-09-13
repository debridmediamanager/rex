# plexsim handover

This file hands over the "implement everything the Plex UI asks of the server" programme:
what it is, how it is run, every cycle it has been through, and — most importantly — **which
recorded beliefs have been overturned, and which are still at risk**. It is tracked in the
repository and carries **no tokens**. Tokens live in the private
`../docs/operations/credentials.md` and in zen's own `Preferences.xml`.

Read `CLAUDE.md` and `docs/findings.md` as well. Those two record *what was measured*. This
file records *how the work is run and what not to trust*.

---

## 0. How to use this file

If you are picking this up cold, read in this order:

1. **§1**, the mission and the one rule — two minutes, and everything else follows from it.
2. **§3, the revalidation register** — before you trust anything in `docs/findings.md`. It is
   the list of things this project has already got wrong, the six ways it got them wrong, and
   the items still standing on thin evidence.
3. **§4**, the instruments, and **§5**, the cycle procedure.
4. Then §6–§9 as reference while a cycle runs.

**The standing instruction for whoever takes over:** a rule in `docs/findings.md` is a
measurement *with a date and an instrument attached*, not a fact. Eleven cycles have overturned
at least twenty of them. Before you build on a rule that matters, check it against §3's six
failure classes and re-measure it if it fits one. The cheapest re-measurement is always the
one you do before writing code, not the one a later cycle does for you.

---

## 1. The mission and the one rule

plexsim is a Plex Media Server that owns no files: it answers the PMS API well enough that the
**official** Plex clients treat it as a real server, and streams a catalog backed by
Real-Debrid links. The programme is to cover everything the Plex UI asks of a server.

The user's standing asks, in their words, across the sessions:

- "cover all functionality in the ui related to the server, including functionalities in the
  context menu of each item"
- "add everything to the harness"
- "do it in a cycle until the agent returns that there are no more findings"
- use a **Fable** subagent and an **Opus** subagent for the hunts (explicitly authorised)
- **plex.tv is out of scope**: Optimize and Sync, the plug-in app store, the account side

**The one rule: every emulated response is a measurement, not a guess.** A response shape is
taken from what a reference PMS answers *for the client's exact request*, never written by
hand. The loop stops only when a cycle's hunters both return "no findings".

A note on that stopping condition, recorded in the eleventh cycle: it is probably not reachable
as literally stated. The PMS surface is large enough that a determined hunter always finds
another attribute, and some of what is left changes nothing for any client — cycle 11's gzip
finding moves the wire by seven bytes. The useful reading is **stop when a cycle's findings
stop being things a real client would notice**, and to make that judgeable, each finding from
cycle 12 onwards should be tagged with its client impact (what breaks, and for whom).

---

## 2. Where things stand

| | |
|---|---|
| `main` | `74c77e7` (cycle 11 merged), equal to `origin/main`, clean tree |
| Cycles complete | **11**. Cycle 12 is **running**. |
| Worktrees and stashes | cycle 12's worktree only; every earlier one removed |
| Work half-done | cycle 12's two hunts are out |

Coverage, measured from the repository rather than asserted:

| Cycle | Checks in the suites | `server.py` lines | Findings acted on |
|---|---|---|---|
| pre-loop | 24 | 1,798 | — |
| 1–4 | 442 | 6,953 | the build-out |
| 5 | 501 | 7,551 | ~20 |
| 6 | 533 | 7,848 | ~25 |
| 7 | 553 | 8,192 | ~25 |
| 8 | 579 | 8,661 | ~26 |
| 9 | 642 | 9,502 | ~63 |
| 10 | 658 | 9,910 | 33 |
| 11 | 674 | 10,176 | 14 |

Read that curve honestly. Cycles 1–4 closed the functional gap; everything since is fidelity
work at a steady ~33 checks a cycle, with findings per hunt now trending down (≈11 in cycle 10,
7 in cycle 11). **The ceiling has been the instrument, not the effort**: every time the
instrument improved, a batch of findings appeared that no amount of re-hunting the old one
would have produced — real media files gave cycle 10 sixteen media rules, thirty-minute media
gave it the completion-crossing rule, and a 1.42.2 server gave cycle 11 its version oracle.
Cycle 12 is the first cycle pointed at a real 3,887-row library. Expect it to find *more* than
cycle 11, not fewer.

Suite results at `74c77e7`, all run 2026-09-13:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **861/861**, 4 skipped | the skips need a TMDB key or real RD links |
| `tests/test_mediainfo.py` | **27/27** | offline |
| `tests/test_navigation.py` on the full catalog | **46/46** | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the 9 failures are byte reads of placeholder RD links (502); expected |
| `tests/test_all.py --skip-decode` | playQueue and decision PASS | byte reads 502 for the same reason |

---

## 3. The revalidation register

**This is the most important section in the file.** Twenty-odd recorded rules have been
overturned across eleven cycles. They did not fail randomly: they failed in six ways, and the
same six keep recurring. Learn the classes, then check the items.

### 3.1 The six ways a measurement in this project goes wrong

| # | Class | What it looks like | Canonical case |
|---|---|---|---|
| **A** | **Measured without the parameter the client actually sends** | The recording is real, the request is not the client's | The Seasons block: a real PMS's `/children` payload was substituted wholesale and the bug survived, because the recording was taken without the paging params the client sends — so it was missing the same `totalSize`. This is the reason the project's one rule exists. |
| **B** | **Measured on data too thin to show the rule** | Everything agrees, because the sample cannot disagree | `/library/metadata/{id}/related` was recorded as "always empty" from four lab items that have no related titles. |
| **C** | **Rule recorded correctly, implemented as a proxy** | findings.md is right and the server is wrong | The play-queue add: cycle 4 measured "after the block of already-added items" and implemented `id > selected`, which every original member also satisfies. Wrong for **seven cycles** while the docs said fixed. |
| **D** | **A threshold or constant inferred from the sample** | A number nobody ever measured | The gzip floor: "860 is the smallest compressed answer measured off it" — a real PMS compresses an 83-byte body into 90 bytes and sends it. |
| **E** | **An instrument limit mistaken for a server rule** | The copy cannot do it, so the rule is written as if the server cannot | "POST subtitles is a 500 on the copy with nothing in its log" — the client's own request is a clean 200. |
| **F** | **A hunter's report taken at face value** | Plausible, specific, and not reproducible | Roughly one finding per cycle is refuted on re-measurement. `/system/agents` "has the wrong shape" was the modern rows matching exactly. |

**How to use this table.** When you write a rule, name the class it could belong to and say why
it does not. When you read a rule, ask which class it could be. The re-measurement that costs
ten minutes now has cost whole cycles when skipped.

### 3.2 Beliefs this project recorded and later overturned

Every one of these was in `docs/findings.md` as a measurement. Sorted by the class above.

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| A show's `/children` needs no `totalSize` | The Seasons block draws from `totalSize`, not the rows | A | cycle 1 |
| "The Console's live log stream is not fed by that socket" (75 s, zero frames) | `filters=` is a whitelist and `log` is opt-in; with the filter the client passes, the reference pushed 386 frames in 14 s | A | cycle 6 |
| `resourceSession` is the client identifier | It is the `X-Plex-Session-Identifier`, then the client id, then `""` — the earlier note was measured without a session identifier | A | cycle 6 |
| `POST /library/metadata/{id}/subtitles` is a 500 on the copy | The client's exact request (title with extension, format, bytes) is a 200 that creates a stream | A, E | cycle 11 |
| "An item-detail container is BARE — no identifier, mediaTagPrefix or mediaTagVersion" | Both servers send all three; `mediaTagPrefix` is what turns a tag into an image URL | B | cycle 8 |
| `/library/metadata/{id}/related` is always empty | It carries the same hub as the `/hubs/` form | B | cycle 7 |
| A season row carries no `year` or `titleSort` | Only a season *without* a year does; the seasons sampled had none yet | B | cycle 7 |
| A Device row's `name` and `platform` are empty | They are `X-Plex-Device-Name` and `X-Plex-Platform`; the earlier probe sent no X-Plex headers | B | cycle 5 |
| Hub rows are light on every shelf | Related and postplay shelves carry the FULL row | B | cycle 8 |
| A video's `displayTitle` never carries the codec | Thirteen products get it — Plex Web among them, which is what the suite's own client claims to be | B | cycle 10 |
| `viewGroup`/`viewMode` on `/library/people/{id}/media` for every tag type | An ACTOR's list only (tagType 6); the earlier reading came from an actor whose rows were shows | B | cycle 11 |
| A plain queue add goes after the already-added block *(rule right, code wrong)* | Implemented as `id > selected`, which every original member satisfies → appended to the end | C | cycle 11 |
| `nocache` on a listing is paged-versus-unpaged | It tracks the library TYPE, exactly as the secondary containers do | C | cycle 8 |
| Scrobbling one episode twice takes the show to `viewCount` 2 | The parent's own count moves when the PARENT is scrobbled; implemented the wrong way round | C | cycle 9 |
| Unmatch gives a filename title and a `none://` guid | That is a MOVIE's rule; an episode or season gets `local://` and a generated `Episode N` | C | cycle 9 |
| The timeline's `viewOffset` is always -1; `Bandwidths` follows the state | `viewOffset` is the item's stored offset; `Bandwidths` is armed by a DECISION and then present even on `stopped` | C | cycle 6 |
| gzip has an 860-byte floor and only applies when it shrinks | Neither is real: the gate is the client's offer and nothing else | D | cycle 11 |
| A single history read is the row `history/all` lists | It is a nine-attribute projection with `viewType: 1` and no key/ratingKey/thumb | D | cycle 11 |
| The `Session` block appears only after a decision | Not reproduced; the unclaimed copy carries no `Session` block at all | E, F | cycle 10 |
| `/status/sessions/history/all` answering `size 0` is a kids oddity | Both kids AND zen do it. Still unexplained | E | cycle 11 |
| A real PMS announces itself with `PUT /devices/{numeric id}` | It announces with `PUT /devices/{machineIdentifier}`. **The numeric form resolves to a pre-existing row and silently overwrites it** | F | cycle 10 |
| `/system/agents` has the wrong shape | The six modern `tv.plex.*` rows match exactly; the `primary`/`MediaType` shape is the legacy plug-ins | F | cycle 7 |
| `title!=` is the negation of the word match; a double space never matches; a yearless season lacks year/titleSort | All three refuted on re-measurement | F | cycles 7–8 |
| A detail row's tags carry `count` (items in the library sharing the tag) | They carry none, on either server. `count` appears only on the tag `Directory` rows of `/hubs/search` and `/library/search`, where plexsim already emits it with the right value. Recorded in cycle 11 as a "boundary note" from a hunter's report and written into this file as cycle 12's lead territory **without being verified first** — the cheapest possible instance of class F | F | cycle 12 |
| Seven more write rules reported in cycle 9 | Refuted: progress `state=playing` opening a session; a bogus `playQueueItemID` selecting by ratingKey; a library type change being a 400; a collection create with no uri being a 400; smart collections tagging members; a whole-library playlist add adding nothing; `continuous=1` stopping at one episode | F | cycle 9 |

### 3.3 Standing on thin evidence right now — revalidate before building on these

| Item | Why it is thin | How to settle it |
|---|---|---|
| The `<CODEC> External` subtitle codec label | **One witness.** Measured on plexscratch only; no sidecar subtitle exists on zen to confirm it | Upload one to the copy on a fresh reset and read it back; look for a sidecar on any real library |
| `/status/sessions/history/all` answering `size 0` | Reproduced on both servers and unexplained; plexsim's listing was built against fun's 3,879 rows, which are no longer readable | Find the parameter or account state that makes a real PMS list history, or record it as a genuine reference behaviour |
| Every rule measured before cycle 10's `long` mode | plexscratch's clips were **ten seconds**, so any timeline rule measured on them crossed 90% instantly | Anything about sessions, completion or resume measured in cycles 9 or earlier deserves a re-run on `long` |
| Every row-shape rule measured before cycle 12 | Eleven cycles used a five-movie, one-show library. Tag counts, collections with members, multi-edition items, deep listings and real search were all unmeasurable | zen's 3,887-row library on 1.42.2 is now the instrument for this |
| Anything whose only second witness was fun | fun is out of scope (production, and stopped mid-deploy in cycle 11). Its `newest` bound, its Recently Viewed order and its postplay shelves are unverifiable now | Re-measure on zen, or mark the rule single-witness |
| Rules measured on kids alone | kids is **1.43.4** and plexsim reports **1.42.2** | zen answers 1.42.2: one request settles whether a field is version-only |

### 3.4 The revalidation protocol for each new cycle

1. **Before launching**, pick two or three entries from §3.3 and re-measure them. Ten minutes
   each. Record the result in the cycle's findings section whether it holds or not.
2. **When a hunter reports**, re-measure every finding yourself against the same instrument
   before touching code. Historically about one finding per cycle does not reproduce.
3. **When a re-measurement overturns a recorded rule**, update `docs/findings.md` *and* the
   check in the suite, never delete either, and add a row to §3.2 with its class.
4. **When you implement a rule as anything other than a literal transcription of it** — a
   proxy, a threshold, a shortcut — say so in the comment and add it to §3.3. Class C and D
   failures are the ones that survive longest, because the docs agree with themselves.

---

## 4. Instruments

Four servers and a copy. Know which one you are allowed to write to before you send anything.

### 4.1 Reference PMS — `kids` (the authority for SHAPE)

| | |
|---|---|
| URL | `http://kids:32400` |
| Token | "server token" in `docs/operations/credentials.md` (the plexlab entry) |
| machineIdentifier | `78a364ad887787f74e834a5c48c987d3e6fb165a` |
| Version | **Plex 1.43.4** |
| Where it runs | docker container `plexlab-plex` from `/home/ben/plexlab`; config in `/home/ben/plexlab/config` |
| Harness source | `harness/` in this repo (CLAUDE.md has setup and traps) |
| Lab ports | 9081 mitmweb, 7902 desktop, **9224 CDP (plexlab only)**. Anything else on kids uses 9223. |
| Rule | **GET and HEAD only for hunts** since cycle 7 |

Library contents, which matter when you read tests and findings:

- **Section 1 "Movies"**: 5 movies — ratingKeys `1` Big Buck Bunny, `18` Cosmos Laundromat,
  `17` Elephants Dream, `3` Sintel, `4` Tears of Steel. Cosmos was `30` until cycle 4 deleted
  the library and it had to be restored from backup, which re-keyed it.
- **Section 2 "TV Shows"**: Firefly `6`. Season 1 is `7`, episodes `8`–`10`. Season 2 is `11`,
  episodes `12`–`14`; those three are **unmatched** (`local://`) and titled "Episode 1–3".
- Season `7` has `year` 2002 and `titleSort` "Season 1", both gained from a metadata refresh on
  2026-09-12 that repaired the showOrdering incident in §9.
- **Download queue `1`** exists and cannot be deleted (DELETE answers 404). A cycle-6 agent
  created it; it is status `done` with 0 items. Leave it.
- **State baseline**: `handover/tools/ref_cycle5_before.json`, taken before cycle 5. It still
  matches exactly. Compare with `handover/tools/refcheck7.py`.

**Its limitation, and it is the big one:** the library is ten synthetic ten-second clips named
as real titles. It can show you a *shape* but never a rule that needs real content — no tag is
shared by enough items, no item has two editions, no listing is deep, no search matches many
rows. Eleven cycles of row-shape rules were measured here. See §3.3.

### 4.2 Second witness — `zen` (the authority for VERSION, and the only real library)

| | |
|---|---|
| URL | `http://zen:32400` (`100.64.196.40`) |
| Token | its own `PlexOnlineToken`, from `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml` via `sudo -n grep` over ssh |
| Version | **1.42.2.10156 — the exact build plexsim reports** |
| Sections | 1 "Zen Movies" (3,887 rows), 2 "Zen Shows", 4 "Bench TV" |
| Rule | **GET and HEAD only, ever.** It is production; its zurg feeds Plex and Jellyfin. |

Added as an instrument in cycle 11 and it changed the economics of the whole programme:

- **It settles version questions in one request.** kids answers 1.43.4 and plexsim claims
  1.42.2, so "is this field 1.43-only?" used to need fun. Now a field kids sends and zen does
  not is a version difference *by construction*, and a rule both answer is not.
- **It is the first real library the project has had as a routine instrument**: real agent
  metadata, tags shared by hundreds of items, real collections, multi-edition items, 3,887-row
  listings, searches that match many rows and several types at once.

Always send paging headers. An unpaged listing of 3,887 rows is a large, slow response.

### 4.3 Disposable write reference — `plexscratch` (WRITE FREELY)

Set up 2026-09-13 after cycle 8, with the user's approval. A sanitised copy of the lab's config,
for measuring WRITE behaviour the shared reference must never receive.

| | |
|---|---|
| URL | `http://100.85.222.1:32410` (kids' Tailscale address only) |
| Token | `ssh ben@kids /home/ben/plexscratch/scratch.sh token` — the copy's own `.LocalAdminToken`. **It changes on every start**, so fetch it after each `up`/`reset`. |
| machineIdentifier | `12081b20eeb2f1ca3be0473ea444236d942d156a`; unclaimed, same 1.43.4 image by digest |
| Library | identical to the lab at copy time (5 movies, Firefly 6, same ratingKeys) |
| Control | `/home/ben/plexscratch/scratch.sh up \| down \| reset \| status \| token \| long \| short`; source in `harness/scratch/` |
| Media | `short` (the lab's ten-second clips) or `long` (`media-long/`: movies 30 min, episodes 22 min, same paths and streams). The mode sticks in `.mode` across `up`/`reset`. **Currently `long`.** |
| Undo | `./scratch.sh reset` restores from `pristine/`; the lab is never read again |

Why it is built the way it is (all measured while setting it up):

- **It cannot reach plex.tv.** PMS sits on an `internal: true` docker network with no default
  route and no DNS. `make` strips `PlexOnlineToken`, account, email and certificate from
  `Preferences.xml`, skips Logs, Cache, `.LocalAdminToken` and `Setup Plex.html`, and mints new
  machine identifiers. A copy announcing the lab's identity would rewrite the lab's plex.tv
  registration.
- **Two socat hops.** An unclaimed PMS lets only LOOPBACK in, and even then 401s a `Host` that
  is not 127.0.0.1/localhost. A hop inside PMS's namespace (port 32499; **not** 32401, which
  PMS binds itself and exits over) makes every request loopback, and clients send the local
  admin token. A session's client address therefore always reads 127.0.0.1 there.
- **It is unclaimed, and that is an instrument limit**: `/status/sessions/terminate` 401s in
  every form, `/statistics/*` 404s, and no `Session` block is ever carried. Those three are
  recorded as limits, not as rules.

### 4.4 Production PMS — `fun` (OUT OF SCOPE)

`http://fun:32400`, Plex 1.42.2, 15/20 movie sections and 19 shows. It was the second witness
for cycles 5–10. **It is out of scope as of cycle 11**, at the user's instruction ("make sure
you are testing this in zen or kids not on fun"). Its Plex was also stopped mid-deploy by
another session during cycle 11. Anything whose only second witness was fun is now
single-witness — see §3.3.

### 4.5 plexsim under test

```bash
# UI-surface fixture (the suite WRITES; restart to undo)
RD_TOKEN=dummy exec python3 server.py --port 32541 --catalog tests/catalog_ui.json --assert-claimed
# full catalog (gitignored, RD links are account-scoped)
RD_TOKEN=dummy exec python3 server.py --port 32543 --catalog catalog.json --assert-claimed
```

From an agent tool, start servers with `subprocess.Popen(..., start_new_session=True)` or as a
background task (Claude Code: `run_in_background: true`). `nohup … &` dies with the tool call.
`RD_TOKEN` must be set or startup fails.

**Ports.** Pick freely EXCEPT `32400`, `32502`, `32511`, `32512`, `32595`, `32596` and `8787`,
which belong to other sessions — a server started on one of those silently fails to bind and
every request then goes to somebody else's plexsim, which reads as a wildly failing suite.
`32599` is `test_ui_surface.py`'s own gated server; leave it free.

`tests/catalog_ui.json` holds movies `1007` Tears of Steel, `1008` and `1009` (fixture copies);
show `20001` The Simpsons; seasons `30001` (S19) and `30002` (S20); episodes `1001` (S19E09),
`1002` and `1003`; sections `1` movie and `2` show. Multiplicity is a prerequisite, not a
detail: on a one-movie section the Shuffle, Play Next, Merge and second-page checks have nothing
to compare and quietly become no-ops.

---

## 5. The cycle procedure

One cycle, as run for cycles 8–12:

1. **Snapshot the reference.** `refcheck7.py` against the baseline (read-only); keep the output
   to diff at the end.
2. **Revalidate two or three §3.3 entries.** Ten minutes each. Record the result either way.
3. **Prepare the instruments.** Write a fresh mode-600 `tokens.env` in the scratch directory
   (`PLEXLAB_TOKEN`, `ZEN_TOKEN`, `MACHINE_ID`), reset plexscratch, and export the current
   `main` into a frozen tree the hunters talk to: `git archive main | tar -x -C <scratch>/headN`,
   served on a port of its own. **The frozen tree matters**: it stops a hunt from measuring your
   half-finished edits.
4. **Launch the hunters** with the Agent tool, one territory each, one on fable and one on opus.
   The prompt is assembled from `handover/tools/hunt_prompts.md`, which holds the shared preamble
   and every cycle's territories verbatim. For a new cycle update: the cycle number and the
   commit; **Already fixed** (the previous cycle's items from §7 — a hunt against an un-updated
   list spends itself re-reporting what just landed); **Deliberately NOT implemented** and
   **Refuted** (§8); the SAFETY section; the ports, the scratch paths, and the report format
   ending in *If you find nothing, say exactly "no findings"*. Write the whole prompt to a FILE
   and give the agent one line telling it to read that file and follow it.
5. **Re-measure every finding yourself** before touching code, against the same instrument the
   hunter used. Hunters are often partly wrong, and about one finding per cycle is refuted
   outright.
6. **Implement in an isolated worktree:** `git worktree add -b cycleN <scratch>/wtN origin/main`.
7. **One commit per finding**, each with its check, proven **against its own parent commit**:
   `handover/tools/sdrive.py` exports HEAD, serves it, applies the patch, serves that, runs the
   patch's own check block against both, and commits only on fail-then-pass. `mdrive.py` does
   the same for an offline check. Re-exporting the parent per patch is the point: proving
   against an older tree proves less than it looks.
8. **Run every suite** on the fixture and on the full catalog, from a freshly restarted server.
9. **Document.** A `#### The Nth pass …` section in `docs/findings.md` inserted just before
   `### Emulation calibrated against the account's own claimed servers`, ending with a "Measured
   and not acted on" list that includes what was REFUTED; trap bullets in `CLAUDE.md` just
   before `## Conventions`; and this file — §2, §3.2, §3.3, §7 and §8.
10. **Commit and push.** Scan the whole branch diff for secrets (both tokens, `X-Plex-Token=…`,
    real-debrid `/d/` links) **in the same `&&` chain as the commit**, so the scan gates it.
    Commit as `Ben Adrian Sarmiento <me@bensarmiento.com>`, semantic single-line messages, **no
    attribution lines**. `git merge --ff-only cycleN` on `main`, then `git push origin main`.
    Remove the worktree, `git branch -d cycleN`, `git worktree prune`, confirm
    `HEAD == origin/main`.
11. **Snapshot the reference again** and diff it against step 1.

Push the work branch to `origin` as you go: it is the only backup of a cycle's commits while the
scratch directory holds the worktree.

Large cycles are split into batches, one per hunt, each merged to `main` on its own once its
suites pass (cycle 10 ran three).

---

## 6. Testing

Run against a live server. Neither needs network beyond RD and TMDB.

```bash
python3 tests/test_navigation.py --base http://127.0.0.1:32543 --catalog catalog.json
python3 tests/test_ui_surface.py --base http://127.0.0.1:32541          # restart the server after
uv run --quiet --no-project --with plexapi python3 tests/conformance.py --base http://127.0.0.1:32543
python3 tests/test_all.py --catalog catalog.json --local http://127.0.0.1:32543 --public http://127.0.0.1:32543 --skip-decode
python3 tests/test_mediainfo.py        # no server
```

| Suite | What it is |
|---|---|
| `test_navigation.py` | **The regression suite.** Every fixed rendering bug gets a check here, and it must fail against the unfixed server — prove it |
| `test_ui_surface.py` | The context menu and every server page, end to end. **It writes**; restarting the server is the undo |
| `test_all.py` | Play queue → decision → byte reads → real decode. It decodes frames rather than trusting a 200 |
| `conformance.py` | Drives plexsim with python-plexapi, as a third-party client |
| `test_mediainfo.py` | `mediainfo.py` against 46 recorded probes and a real PMS's analysis of the same files. Offline |

Suite conventions, each learnt the hard way:

- **Where new checks go.** The newest block of `test_ui_surface.py` sits right before
  `status, _, _ = request(base, "DELETE", "/library/metadata/%s?proxy=1" % movie)`. Everything
  after it deletes a row and a section.
- **Restart the fixture server BETWEEN runs, by waiting for the PORT to be free** — a kill plus
  an `/identity` poll can be answered by the process you just killed, and the suite then runs
  against a server it has already written to. Confirm the new pid holds the port.
- **A check must survive the END of the suite, not just its own proof.** By the insertion point
  the match check has merged a fixture movie away (two rows, not three), titles have been edited
  into sort ties, and tag lists have been replaced. Count rows live, compare a leaf against the
  grid's own answer rather than a hand-computed order, and scan the section for a person rather
  than reading one row.
- **A check block run by `runblock.py` sees the suite MODULE only** — a block that calls
  `detail_of()`, which is defined inside `main()`, raises before it can fail.
- **A key-level comparison is not enough.** The `viewMode` bug was invisible to one: both sides
  carried the field and only the values were swapped.
- **When a re-measurement shows a check encoded a wrong belief, UPDATE the check** with a comment
  saying what was re-measured. Never delete it.
- **A suite run that raises stops without its summary line.** Always look for `N/M checks passed`
  before trusting a run.

---

## 7. Every cycle, from the first

The hunter runs, and what each one changed. This doubles as the **"Already fixed — do not
re-report"** list the hunt prompts are built from: a hunt launched against a list that stops a
cycle short spends itself re-reporting what just landed. `handover/tools/hunt_prompts.md` holds
the same lists in prompt form, verbatim, newest last.

### Before the loop — getting one stream to play

`docs/findings.md` opens with this: the seven things that had to be fixed before the official
web client would play a debrid stream at all, then TMDB identification and artwork, then a real
show → season → episode hierarchy. **The Seasons block is the founding lesson of the project**
— show pages drew no Seasons block, and the bug survived substituting a real server's recorded
`/children` payload wholesale, because the recording was taken without the paging params the
client actually sends, so it was missing the same `totalSize`. Replaying the client's exact
request is the difference between finding it in an hour and not finding it at all. Everything
in §3 grows out of that one bug.

### Cycles 1–4 — the build-out (`2596437`, 36 commits)

| Cycle | Hunter A | Hunter B |
|---|---|---|
| 1 (two rounds) | playback session surface (fable) | filter and smart-query surface (opus) |
| 2 | playback and queue surface (opus) | search, hubs and system surface (fable) |
| 3 | client-driven surface (opus) | library writes and multi-user (fable) |
| 4 | broad sweep (opus) | sequence and state sweep (fable) |

This is where 24 checks became 442 and `server.py` went from 1,798 lines to 6,953. What landed:

- **The filter query language**, and the discovery that defines it: *the operator is part of the
  parameter NAME*. The client sends `year%3E%3E=2000`, so the server reads a parameter called
  `year>>`. plexsim had looked for names it knew and filtered nothing, while advertising 40
  filterable fields. Then `/query/parse`, `/filters` and the axes, and smart lists as stored
  QUERIES rather than snapshots.
- **The UI is a write surface, and the server decides what it offers**: `/media/providers`
  decides whether Edit, Delete, Download, Optimize and Mark as Played exist at all. The Edit
  dialog saved to a path that answered the listing (200, nothing written); every edit parameter
  was then measured one at a time, and the tag table behind them.
- **The play queue** (plexsim kept a key list), `/:/progress` vs `/:/timeline`, the timeline's
  minute, history, `group=`, the include/exclude options, the metadata tree, the butler.
- **`GET /` is not `/identity`**, and eleven refusals a real PMS makes. **The catch-all now
  404s**, which is what finally surfaced `GET /library` — python-plexapi asks for it first and
  it had never been routed. An unrouted path answering an empty 200 reads to a client as "this
  endpoint exists and is empty".
- **The token gate** (plexsim had none at all, so any peer that could reach the port read the
  whole library), `Cache-Control`, gzip, CORS.
- **The notification LIFECYCLE**, which is what makes a client refetch, and the four types
  plexsim never pushed.
- **XML**, from the one angle nothing had used — and the stream title split turned out to be
  backwards, which is what the track picker prints.
- Default listing order, `includeAdvanced`, the substring search box, Change Poster, cascade
  deletes, unknown-section 404s, and the removal of every 405.
- **Merge is not reversible, and that was learnt the hard way** — see §9.

### Cycle 5 (`daf88c8`) — settings, agents and the wire format

Hunters: wire-format sweep (opus), settings and item sweep (fable).

The Add Library form (`/library/sections/prefs?type&agent`, from `section_prefs_new.json`);
setting-write validation and typing on `/:/prefs`, library prefs and item prefs; `/system/agents`
with its per-type lists, scanners per TYPE, `/:/plugins/*` answering 404; `/devices` and device
attribution; statistics rollups; users/top, `/nearest`, `/channels/all`, `/log`,
`/services/browse`, `/library/tags`. The wire format: Accept negotiation is exact, JSON carries
no `null` and no `[]`, XML number formats. Row rules (`titleSort` article-stripped and sent only
when it differs; `originalTitle` is never a filename); the grid and hub row projection
(`_grid_row`); `mixedParents`; global `/hubs`; library shelves; `/common`; `/sorts`.

**Corrected here:** a Device row's `name`/`platform` (the earlier probe sent no X-Plex headers).

### Cycle 6 (`3e52226`) — the player, and what every write pushes

Hunters: playback and streams sweep (opus), notifications and identifiers sweep (fable).

Decision validation (path, mediaIndex/partIndex, and the client profile read out of
`Resources/Profiles` — `Chrome` passes, `chrome` and `Darwin` do not) and the 2000/4005 refusal;
the decision row projection and `resourceSession`; transcode start/ping/stop refusals;
`/library/streams`; subtitle search language validation; `/downloadQueue`; timeline type 15 for a
playlist and 18 for a collection; the collection create/delete frame sequences; the 60 000 ms
minute on every state; query edges (a pair with no `=` is a 400, an empty path segment a 404, a
leading `//` read off `requestline`, ids ≥ 2^31 a 400).

**Corrected here:** the timeline's `viewOffset` and `Bandwidths` (four corrections at once);
`resourceSession` (the earlier note was measured without a session identifier); and the Console
log stream — `filters=` is a whitelist and `log` is opt-in, so the "zero log frames in 75
seconds" measurement had been taken without the parameter the client passes.

### Cycle 7 (`11a5100`, `c62c51b`) — corpus replay, and python-plexapi

Hunters: the whole probe table and every corpus diffed mechanically (opus), python-plexapi as a
third-party client (fable).

No `theme`/`parentTheme`/`grandparentTheme` anywhere; `excludeFields` on every row AND the
container; hub row projection by type; `includeLibraryPlaylists`; an episode's `Image` order.
Then the second batch: **type-prefixed sort keys** (`movie.titleSort`, `season.index`) and
**scoped filters** — a show library advertises its fields PREFIXED (`show.genre`,
`episode.title`) and the client sends the advertised key back verbatim, so plexsim's map, keyed
by bare names, meant no filter on a show grid ever constrained anything. `title=` is a
word-prefix matcher; `titleSort`/`summary`/`tagline` are case-insensitive equality;
`/library/all` is a query.

**Corrected here:** `/library/metadata/{id}/related` is not always empty (the recording covered
four lab items with no relations); a season *with* its own year carries it, overturning cycle
5's "a season row carries no year or titleSort". **Refuted:** `/system/agents` "wrong shape",
`title!=` as a negated word match, and "a double space never matches".

### Cycle 8 (`5b7d4ff`, `c8c072a`) — the bundle's own URLs, and the XML path

Hunters: every URL template in the web bundle across three servers in JSON and XML (opus), then
the XML path and four client families (fable, re-run on opus after a rate limit).

Section subsets (`onDeck`, `recentlyAdded`, `newest`, `recentlyViewed`, `recentlyViewedShows`);
`/autocomplete`; `/filters` keyed by `type=`; By Folder `?parent=`; `/grandchildren`; paged
`/children` keeping the All-episodes row at `Start=0`; `guid=` as the client's "do you have
this?" prefix lookup; `/devices.xml` as the plug-in dispatcher. Second batch: `/status/sessions`
no longer writing playback flags into the catalog; the tree's blank season titles and root
parentTitle; iOS typing All episodes as a season; **XML child element order**
(`xmlout._CHILD_ORDER`) and `Meta` first.

**Corrected here:** `nocache` tracks the library TYPE, not paged-versus-unpaged; Related and
postplay shelves carry the FULL row, overturning cycle 7's "light on every shelf"; the
item-detail container is not bare; the All-episodes row is dropped only by a ONE-ROW page.

### Cycle 9 (`ae19756`, `4f2bb31`) — the OpenAPI GETs, and the first write cycle

Hunters: the community OpenAPI description's every GET (fable, re-run on opus), and write
semantics on the brand-new plexscratch (fable).

Batch 1: `/library/sections/all`; paging on every container; season and episode `allLeaves`;
`/hubs/metadata/{id}`; section `allLeaves`/`albums`/`cluster`/`moment`/`computePath`/`nearest`;
`/library/people/{id}` and `/media`; `/library/matches`; `/hubs/items`; `/sorts` keyed by type;
`randomArtwork`.

Batch 2 — 46 new checks, the first systematic write measurement: the section save path MERGES
tags where the item path REPLACES them; an unlocked Sort Title is re-derived on items,
collections and playlists alike; dates are parsed and set the year; the section path stores
`userRating`; a parent's title and studio are copied onto its children; frame titles
(`Show (year)`, `Show S01 E07`); generated `Season N`/`Episode N` titles; the identifier refusal
order; an episode scrobble counting on its season and show; one On Deck row per show; playlist
and collection create validation; `collectionSort`; play-queue selection, per-client retention
and moves; library edit applying language and prefs and running the measured scan; merge, split
and unmatch.

**Corrected here:** the scrobble direction (the parent's count moves when the PARENT is
scrobbled — implemented the wrong way round); unmatch giving `local://` and a generated title
for an episode or season, where the filename-title rule is a movie's. **Seven findings refuted**
on re-measurement — see §3.2.

### Cycle 10 (`a7b3b77`, `173305c`, `dd52196`) — media analysis, more writes, the session surface

Three batches: `mediainfo.py` against fun's own analysis of 35 real files (opus); the writes
cycle 9 did not reach (fable); the playback surface on plexscratch's new `long` media (opus).

Batch 1 built `tests/fixtures/media/` and `tests/test_mediainfo.py` from 46 recorded probes:
codec names (DTS is `dca`, `dca-ma` on the Media for DTS-HD MA; a Blu-ray subtitle `pgs`);
languages named in themselves with 639-2/T and 639-1 codes (`languages.json`); ten-bit depth
from the pixel format; `codedHeight` macroblock-aligned for H.264; every video stream (a Dolby
Vision enhancement layer is one, a Matroska cover image is not) and the eight `DOVI*`
attributes; the resolution label bucketed by width AND height; the aspect ratio snapped to one
of six values; the Media's audio fields from the WIDEST track; a stored catalog corrected at
load with the titles composed LAST.

Batch 2: a library's extras leaf and hub; a settings save pushing a `preference` frame per id;
butler runs answering EMPTY; a smart list's Save replacing its stored query, `limit` included;
artwork uploads storing the image and re-versioning the row; Fix Match needing a `name` and
merging onto a guid another row holds; a scan naming its library in a `Context`.

Batch 3, on thirty-minute media: **a play completes only when a beat CROSSES 90% inside its own
session**; a beat's own `duration` is the only one that counts; a beat at or under a minute
DELETES the stored resume point; `playQueueItemID`/`playQueueID` on every `playing` frame.

**Corrected here:** a video's `displayTitle` DOES carry the codec for thirteen products, Plex
Web among them — and the suite's own client says `X-Plex-Product: Plex Web`; two older checks
that completed a play with a single beat at the end. **Refuted:** the `Session` block appearing
only after a decision. **And the dangerous one:** a real PMS announces with
`PUT /devices/{machineIdentifier}`, not the numeric form, which resolves to a pre-existing row
and silently overwrites it.

### Cycle 11 (`74c77e7`, 19 commits) — the query grammar, and the XML path of the writes

Hunters: the XML path of every write and session beat plus the writes cycle 10 could not reach
(fable, on plexscratch); the XML path of everything cycles 9–10 added plus the transport around
it (opus, read-only). **fun was out of scope**, and zen joined as the version oracle.

The listings ran none of the query grammar they advertise: `?limit=N` (`limit=0` answers ONE
row, a negative one every row, `abc` a 500) while `/newest`, `/recentlyAdded` and `/children`
ignore it; a `type=` that does not parse is a 500 on a section listing and a **400** on
`/library/all`; **`id` is a filter field on every listing**, comparing like the integer column it
is, and `/query/parse` echoes it with an empty `fieldTitle`; a section's `allLeaves` takes the
whole grammar. Then `excludeElements` on the three listings cycles 9–10 added; `includeStorage`;
`/library/people/{id}/media` typed by the TAG; a statistics container paging its SERIES and
picking its Device/Account rows by content; `timespan` validated; gzip on whatever the client
offers; a plain queue add landing in Up Next; the Upload Subtitle modal's whole grammar; stream
PUT/DELETE; the light row on a playlist's items and a collection's children; a single history
read as a projection.

**Corrected here:** the queue-add proxy from cycle 4 (seven cycles wrong); cycle 9's
people/media `viewGroup`; cycle 10's subtitle-upload "instrument limit"; the inferred gzip floor;
the single-history-read belief; and cycle 9's "history/all `size 0` is a kids oddity" (both
servers do it).

### Cycle 12 — in progress

Hunters: every row shape a REAL library shows and a five-clip library cannot (opus, read-only on
kids AND zen); the writes cycles 9–11 could not reach plus the frames cycle 11's new writes push
(fable, on plexscratch).

---

## 8. Measured and deliberately NOT implemented

Keep these in the hunt prompt's "do not re-report" list. Each one's reason is in
`docs/findings.md`.

**Inherited from cycles 1–6:** ICU collation; image filters; `random:` sort regeneration;
updatedAt/contentChangedAt semantics; session ordering and the Session block; duplicate
collection titles; scan `refreshing` observability; `transcoderVideo*` fields; XML-only
`/system` responses; text/plain error bodies (a deliberate deviation); Part `exists`/`accessible`
on metadata reads; `/livetv/epg` countries and languages; legacy agents and the `/:/plugins`
listing; typo-tolerant search; season and episode external Guids (no data); Common's
episode/season quirks; the random actor/director shelf; markers and chapters (no data);
`/services/browse` mount points; mp4 `optimizedForStreaming`; the eventsource transport; download-
queue item adds; a valid transcode start (plexsim has no encoder); anything on plex.tv; pure
catalog-content differences.

**Cycle 7:** the `count=1` hub quirk on kids; orphaned collection tags carrying a `guid`;
`/system/agents`' legacy `primary`/`MediaType` shape; `/matches?manual=1` candidates;
`thumbBlurHash`/`artBlurHash`; `slug=` 500 on listings; `/squareArts` (1.43.4 only).

**Cycle 8:** fun's `newest` bound and Recently Viewed order; ICU numeric collation in
autocomplete; All episodes at `Start>0` (kids drops it, fun keeps it; plexsim follows kids);
`guid==`/`guid!=`; `/system/services/url/lookup`'s plug-in traceback; mixed folder levels; fun's
postplay `movie.extras` and `tv.ondeck` shelves; minted studio tag ids.

**Cycle 9:** `/services/ultrablur/image` and `/colors?url=`; `/file?url=`; episode `allLeaves`
container disagreements; kids' history filtering by account; genre/actor `/hubs/items`
identifiers; `/library/matches` provider options; paging on write-shaped reads; plexscratch's
session-bound and duration-less timeline limits; optimize, `showOrdering` and library-create
frames captured only in part; reference tag-column oddities; copy artefacts (item DELETE 400,
`editionTitle` 403).

**Cycle 10:** what the probe cannot give (`refFrames`; a Matroska video stream's bitrate; DTS-HD,
TrueHD, FLAC, Opus and subtitle bitrates; `headerCompression`; Part `packetLength`;
`hasThumbnail`, `indexes`, `failureBIF*`; `eia_608`); which audio track is `selected`;
counterexamples (E-AC-3 with Atmos in mp4, DTS Express with no layout, TrueHD in mp4 with no bit
depth); 1.43.4-only labelling; what the copy cannot do (download queue, external artwork URL,
provider match search); a library create's per-item `created` frames and the
`backgroundProcessingQueue` frame; the copy's timezone artefacts; an unclaimed copy's 401 on
terminate and 404 on `/statistics/*`; `Player.profile` only ever `Web`;
`/statistics/resources` per-second host samples.

**Cycle 11:** `/status/sessions/history/all` answering `size 0` on both servers;
`includeBandwidths=1`; `/library/people/{id}` answering any TAG id;
`playQueueLastAddedItemID` re-anchoring on a move; the subtitle SEARCH path's 503 on the isolated
copy; the `<CODEC> External` label's single witness.

---

## 9. Safety rules and incidents

The rules:

- **zen and fun are production: GET/HEAD only, always.** fun is out of scope entirely.
- **kids is read-only for hunting agents since cycle 7.** Every write needs its undo listed
  first, and a snapshot before and after. Never delete a section or an item; never merge, split,
  match or unmatch; never run maintenance endpoints; never change network or auth prefs; **never
  write server, library or item settings**.
- **Several PMS GETs WRITE.** Guard every request to a reference with `probe.py`'s `WRITE_GET`
  regex.
- **Never `PUT /library/metadata/{id}/merge` on the lab.** It is not reversible; the `split` that
  undoes it hands the second item back on a NEW ratingKey carrying the first one's metadata.
- **Never write to plex.tv's device registry, and never repeat claim exchanges.** Both have
  signed real servers out. `PUT /devices/{numeric id}` resolves to a pre-existing row and
  silently overwrites it.
- **Never commit a token, a Real-Debrid `/d/` link or a tunnel credential.** The workspace root,
  `docs/providers/` and `docs/operations/` are not git repos and hold live secrets.
  `catalog.json` and `run.sh` are gitignored for this reason. **Put the secret scan and the
  commit in one `&&` chain**, or the scan's exit status stops nothing.
- **No attribution lines in commit messages.**
- **CDP on kids: 9223 for general browser work, never 9224** (plexlab's, which records bodies).

Incidents, so the rules make sense:

| When | What happened | Repair |
|---|---|---|
| Cycle 4 | A hunter sent `DELETE /library/sections/1` | Movies restored from Plex's backup; Cosmos Laundromat re-keyed 30 → 18 |
| Cycle 5 | Writing `showOrdering=bogus` on show 6 silently re-matched episodes 8–10 to local guids | `PUT /library/metadata/6/refresh`, which also gave season 7 its year and titleSort |
| Cycle 6 | A stray `POST /downloadQueue` created queue 1 | Cannot be deleted (404); left in place |
| Earlier | Repeated `/api/claim/exchange` calls with fabricated server identities triggered an account-wide token reset that signed fun AND zen out of plex.tv | Both re-linked with `POST /myplex/claim?token=<claim>`; the account token was rotated 2026-09-11 |
| 2026-09-12 | A broken-DB copy parked at `kids:/home/ben/plexlab/db-broken-20260912/` | Deleted at the user's request; the live config verified intact |
| Cycle 9 | Three patches committed with every backtick-quoted word stripped from their comments, and a fourth that never ran | An UNQUOTED heredoc runs backticked words as commands — see §10 |
| Cycle 11 | fun's Plex found stopped and SIGKILLed mid-`deploy-fun.sh` by another session | Left alone; it came back when that session finished. Not ours to restart |

Local processes **not** belonging to this work, left alone on purpose: `32400` is another
session's plexsim; `8787` another session's `http.server`; `32502` a plexsim on
`catalog.example.json`; `32511` an old agent's parent process; `32595`/`32596` plexsims of
unverified origin.

---

## 10. Tooling and environment gotchas

Every one of these cost real time at least once.

**The shell**

- **`grep`/`rg` go through `rtk` in this shell and can mangle or empty the output.** Search with
  `python3`. `ps` output through a pipe can come back empty too — check with `lsof` instead.
- **Never pass code or Markdown through an UNQUOTED heredoc** (`<<EOF`). The shell runs every
  backtick-quoted word inside it as a command and substitutes the output: cycle 9 committed
  three patches whose comments, test labels and findings had lost every `word` in backticks, and
  a fourth never ran at all. Write patch scripts to files with the Write tool, or use `<<'EOF'`.
- **Address every git command at its worktree with `git -C <path>`; never rely on an earlier
  `cd`.** A heredoc ends the `&&` chain it sits in, so when a step before it fails the commands
  after it still run — in whatever directory the shell was left in. That once committed two
  changes on `main` in the shared checkout instead of the worktree branch.
- **A `cd` inside a Bash call moves the session's working directory** for the calls after it.
- zsh expands a leading `=`, so `echo ======` fails. macOS `ps -o etimes` is invalid; use `etime`.
- **`cmd | tail` hides a failure from `&&`.** Chain drivers under `set -o pipefail`.
- **`rtk` can swallow a redirected `ssh` stdout.** `ssh host cmd > file` wrote an empty file
  while `ssh host cmd | wc -c` counted 42 bytes; write through a pipe (`| cat > file`). And
  `wc -c < file` read 0 for a 42-byte file: check sizes with `stat -f %z`.

**Servers and suites**

- **Restarting a test server is not a kill plus a health check.** The poll can be answered by the
  process you are killing, or by something else already on the port: wait for the PORT to be
  free, start the new one, and confirm the new pid holds it. A whole suite run once went to a
  server that had already been written to, and read as twelve regressions.
- **A test server started with `( ... &)` from a foreground call can die later.** Use
  `subprocess.Popen(..., start_new_session=True)` or a background task.
- Background servers started by an agent tool show "failed, exit 144" when you kill them later.
  That is the kill, not a crash.
- **`test_ui_surface.py` starts its own gated server on 32599.** A proof server there makes its
  six gate checks measure the ungated one and report 401s that never happened.
- Run long suites with `run_in_background: true`; a foreground run was cut off once.

**Editing and committing**

- **A replacement that leaves the old block behind DUPLICATES it.** A suite edit that replaced a
  region but not its `if` header ran the whole tail twice — 882 checks instead of 845. Assert the
  occurrence count AFTER the edit, not just before.
- **An assertion in an edit script is the cheap half.** Assert what the file should look like
  afterwards (`count == 1`, the old text gone), or a "successful" edit can leave an `if False:`
  branch and dead code in a commit.
- **A `git checkout -- <file>` throws away every uncommitted edit in it**, including ones made
  earlier in the same session for a different reason.
- **The secret scan must GATE the commit, not merely precede it** (put the scan and the commit
  in one `&&` chain), and it must not match its own description. A naive `X-Plex-Token=\S+`
  matches `X-Plex-Token=REDACTED` inside `guarded.py`, and it also matches this very file's
  prose about the pattern — it cried wolf on both. Anchor the value instead:
  `X-Plex-Token=[A-Za-z0-9_-]{15,}`, which a real token matches and an ellipsis does not.
- **A proof's parent tree needs every sidecar.** `mediainfo.py` loads `languages.json` beside
  itself; a parent tree holding only `mediainfo.py` dies at import, and that reads as a failing
  check.
- **CLAUDE.md bullets wrap mid-phrase and use em dashes.** A patch that finds a bullet by its
  opening words has to match the line break; read the lines with `repr` first.

**The references and the copy**

- **plexscratch's token changes on every start, `reset` included**; re-read it after each.
- **plexscratch's SHORT clips are ten seconds long**: send timelines with an explicit `duration`,
  or every beat is ignored — a ten-second clip is crossed past 90% at once. `./scratch.sh long`
  is the copy on half-hour media, and `reset` stays in the current mode.
- **A client's new play queue retires its older ones.** A check that builds a second queue cannot
  write to the first.
- Item settings on the reference live in `/library/metadata/{id}?includePreferences=1`;
  `/library/metadata/{id}/prefs` is a 404 there.
- The base64 in `/services/browse/<b64>` is sent **unquoted**; `%3D` is not decoded.
- Only the paging headers and `X-Plex-Platform` are normalised from headers into `query`.
- **A hunter on `model: "fable"` can die on a session rate limit** ("resets 1am"); re-run the
  same prompt on opus rather than stall the cycle. Two of the last four cycles lost a hunter this
  way — relaunch against the CURRENT head with the Already-fixed list extended, or the second run
  re-reports the first batch's work.

**plexsim itself**

- **`catalog.json` links are account-scoped.** A different key answers `unavailable_file`, which
  surfaces as a 502 on every part read and looks like plexsim is broken.
- Cloudflare 403s a `Python-urllib/*` User-Agent on the media path; probes set
  `User-Agent: curl/8`.
- `http.server` collapses a leading `//` in the path before the handler sees it. Read
  `self.requestline`.

---

## 11. Artifacts

`handover/tools/` is tracked and token-free. Scripts read tokens from `PLEXLAB_TOKEN` and
`ZEN_TOKEN` (and `FUN_TOKEN` historically); `cmp.py`, `xdiff.py` and `probe.py` take the plexsim
base from `PLEXSIM_BASE`.

| File | What it does |
|---|---|
| `hunt_prompts.md` | The shared preamble and every cycle's territories, verbatim, newest last. **This is the template**: copy the preamble, refresh the lists named in §5 step 4, write the whole prompt to a file for the agent to read |
| `refcheck7.py` + `ref_cycle5_before.json` | Read-only comparison of kids' library state against the baseline |
| `sdrive.py` | **The proof driver.** Per patch: export and serve the worktree's HEAD, apply, serve the fix, run the patch's suite block against both, commit only on fail-then-pass |
| `mdrive.py` | The same for an OFFLINE check (`tests/test_mediainfo.py`); copies every tracked sidecar into the parent tree |
| `runblock.py` | Runs one patch's check block against one or more servers, in the suite's own namespace. Both drivers call it |
| `guarded.py` | The write hunt's HTTP client: writes only to plexscratch and `PLEXSIM_BASE`, GET/HEAD only to the references, a `WRITE_GET` refusal list, plus the frame listener and the JSON/XML diffs |
| `probe.py`, `diff.py` | The python-plexapi sweep: ~460 read calls behind a session that refuses writes and off-host URLs |
| `cmp.py`, `m.py`, `xdiff.py` | Fetch one path from several servers and diff the JSON; the cycle-8 triage fetcher; the XML structure diff |
| `ui_ro.py`, `replay_ro.py`, `replay_remap.py` | `probes/ui_surface.py` and `harness/scripts/replay.py` restricted to GET/HEAD, and a ratingKey remap for replaying a recorded corpus onto plexsim's ids |
| `territory_a.py`, `territory_b.py` | The cycle-6 notification-frame and malformed-identifier sweeps |

Per-cycle scratch directories are ephemeral. Each holds the hunt prompts, the re-measurement
clients, the patch module, the frozen trees and the suite logs. The transcripts are the
authoritative record of a hunt's reasoning:

- cycles 5–8: `~/.claude/projects/-Users-bensarmiento-debridmediamanager-plexsim/2bf061c1-….jsonl`
- cycle 9: `…/5e5b69a0-….jsonl`
- cycle 10: `…/ebe127ef-….jsonl` plus its `subagents/agent-*.jsonl`
- cycles 11–12: `…/778e8a59-81ef-4a89-a46b-16ef32b5343d.jsonl`

The cycle-11 and cycle-12 scratch directories share one session:
`/private/tmp/claude-501/-Users-bensarmiento-debridmediamanager-plexsim/778e8a59-81ef-4a89-a46b-16ef32b5343d/scratchpad/`,
in `c11/` and `c12/`. Each holds `prompt_A.md`/`prompt_B.md` (the hunts' full briefs, also
appended to `hunt_prompts.md`), `rb.py` (the read-only re-measurement client), `ra.py` (the write
one), `pb.py` (every patch with its `apply`, message and check block), `restart.py`, the frozen
head, the worktree, and the suite logs.
