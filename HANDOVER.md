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
measurement *with a date and an instrument attached*, not a fact. Eighteen cycles have overturned
80 of them. Before you build on a rule that matters, check it against §3's six
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
| `main` | cycle 25 merged, equal to `origin/main`, clean tree |
| Cycles complete | **25**. Cycle 25 ran two hunts on two dimensions nobody had swept, and drew the last of cycle 23's deferred backlog down. |
| Worktrees and stashes | none |
| Work half-done | **none** |

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
| 12 | 690 | 10,322 | 16 (of 18 reported) |
| 13 | 899 | 10,509 | 22 (of 28 reported) |
| 14 | 926 | 10964 | 29 (of ~40 reported) |
| 15 | 979 | 11678 | 33 (of 24 reported plus 9 the driver found) |
| 16 | 1,027 | 12,536 | **49** (of 39 reported plus 6 the driver found, plus 4 corrections) |
| 17 | 1,057 | 12,928 | **29** (of 23 reported plus 6 the driver found; 1 refuted, 1 corrected) |
| 18 | 1,186 | 13,357 | **26** (of 23 reported plus 6 the driver found; 1 refuted, 1 corrected) |
| 19 | 1,229 | 13,720 | **31** (of 28 reported plus 4 the driver found; 1 refuted, 2 corrected) |
| 20 | 1,262 | 13,905 | **33** (of 24 reported plus 9 the driver found; 1 refuted, 5 recorded rules overturned) |
| 21 | 1,285 | 14,210 | **24** (of 24 reported across two hunts; 5 edit-field names held out on version, 3 recorded rules overturned) |
| 22 | 1,299 | 14,365 | **16** (of 25 reported plus 3 the driver found; 1 driver finding refuted by its own re-measurement, 9 held out -- see §8) |
| 23 | 1,307 | 14,497 | **9** (of 24 reported plus 2 the driver took off cycle 22's held-out list; 1 cycle-22 entry REFUTED, 2 hunter claims corrected on re-measurement, 15 held out -- see §8) |
| 24 | 1,307 (+2 in `test_mediainfo`, 27 -> 29) | 14,497 | **6** (no hunts -- the fully measured half of cycles 21-23's held-out backlog, re-derived from the corpora; 1 reported difference re-measured and refuted) |
| 25 | 1375 (+2 in `test_mediainfo`, 29 -> 31) | 14,852 | **21** (of 13 reported across two hunts plus 6 the driver found and 1 regression it caught in its own patch; 1 hunter negative refuted on re-measurement and re-confirmed, 3 recorded rules overturned, 2 cycle-23 butler claims refuted) |

Read that curve honestly. Cycles 1–4 closed the functional gap; everything since is fidelity
work. **The ceiling has been the instrument, not the effort**: every time the
instrument improved, a batch of findings appeared that no amount of re-hunting the old one
would have produced — real media files gave cycle 10 sixteen media rules, thirty-minute media
gave it the completion-crossing rule, and a 1.42.2 server gave cycle 11 its version oracle.
Cycle 12 was the first cycle pointed at a real 3,887-row library, and it found **18 where cycle
11 found 14** — the instrument catching up, exactly as predicted. Seven of its findings are
things no five-clip library can show at all (tag-list caps, search caps, tag types), and one of
them was a bug cycle 11 introduced by guessing a frame set instead of measuring it.

Cycle 13 found **28 and acted on 22** — the largest single cycle since the build-out, and the
same lesson twice over. The read hunt took the rest of zen (show libraries, deep paging, the
axes, search at scale, the Home screen) and **not one of its fourteen findings was refuted**,
which has not happened before; the write hunt asked a question no cycle had asked — *what does a
write to a PARENT row do to its children?* — and found that plexsim answered none of it. Two of
the cycle's findings came out of the revalidation step itself, before either hunter reported.
The instrument is still the ceiling: three of the fourteen read findings needed a show with more
than two seasons, and the UI fixture had to be extended three times to express them.

**Where the curve stands after cycle 25, and what it means for the stopping rule.** The
loop stops when a cycle reports no findings, so the trend in the last column is the thing to
watch — and cycle 25 breaks the fall: **24, 16, 9, 6, 21**. That is not the surface refilling.
It is what cycle 24's own note predicted, in the third bullet below and in §10's "sweep a NEW
DIMENSION rather than the same one again": the two dimensions cycle 25 swept — what a real PMS
answers differently to a different CLIENT, and which OTHER endpoints a write changes — had
never been swept at all, and between them they produced thirteen findings in one cycle. A low
number in this column measures the dimensions being swept, not the surface being clean.

Three things were behind the fall to 6, and only one of them was the surface running out.

* **The mechanical dimensions are finished.** Path (14), method/parameter (15), header (16),
  row (17), container (18), child element (19), engine holdout (20-22), attribute order
  (22), child-element order (23). Cycle 23's sweep of the last one witnessed 203 ordered
  pairs across 2,994 paths with **zero contradictions** — that dimension is settled and
  should not be re-swept. Each closed dimension removes a class of finding permanently.
* **The backlog absorbed the difference.** Cycles 21-23 reported 24, 25 and 24 findings and
  ACTED on 24, 16 and 9 — the gap went to §3.3, not away. Cycle 24 spent itself drawing that
  backlog down and reported 6 because it hunted nothing. **A low number in this column is
  not evidence the surface is clean** unless §3.3 is also short, and it is not: 50 open
  entries stand there now.
* **What is left is harder, not smaller.** The largest outstanding item is the butler, which
  needs a queue notion plexsim does not have; the next is a pair of frame cascades that need
  an uncontended socket on the one writable reference. Neither is a measurement problem.

So the honest reading is that the loop has not converged. Cycle 24 read the fall as "moved
from finding differences to owing implementations", and cycle 25 tested that reading directly:
it drew the last fully measured item off the backlog (the unmatch cascades, `pixelAspectRatio`,
the search-row lead, the `/match` verb table) AND ran two hunts, and the hunts alone found
thirteen. **The backlog was real and so was the surface.** What is left in §3.3 after this
cycle is 45 entries, most of them single-witness or instrument-limited rather than
unimplemented.

Suite results at the head of cycle 25, run 2026-09-15 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | two new checks (`pixel-aspect`, `pixel-aspect-threshold`), each proven to fail on its own parent commit |
| `tests/fixtures/media-resolutions.json` | **194 shapes, from 177** | zen's own 17 that fun's libraries did not hold. The corpus itself failed against the unfixed code on exactly one row, which is the point of adding to it |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1375 PASS / 0 FAIL / 9 skipped** | |
| `tests/test_navigation.py` | **28/28** on the UI fixture, **47/47** on the full catalog | run each against its OWN server: pointed at the one the UI suite is writing to, it reports twelve failures that are not there |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline (the catalog's RD links are account-scoped and dead) |

---

## 3. The revalidation register

**This is the most important section in the file.** 80 recorded rules have been overturned
across eighteen cycles. They did not fail randomly: they failed in six ways, and the
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
| A listing row carries its whole tag list | It CAPS them: 2 `Genre`, 2 `Writer`, 2 `Country`, 3 `Role`. Never visible on a library where nothing is long enough to truncate | B | cycle 12 |
| `/hubs/search` and `/library/search` default to 30 rows | 3 per hub and 10 in total. Same cause: a five-clip library never overflows either | B | cycle 12 |
| An actor tag row is `tagType: 4` ("4 is a person") | An actor is 6 and a director 4. plexsim's own `/library/people/{id}` already said 6, so the server contradicted itself | B | cycle 12 |
| A subtitle upload pushes an edit frame pair | It pushes NOTHING. **Cycle 11 invented this** while implementing the upload, without measuring the socket — this project's own class-D/F mistake, one cycle old when cycle 12 caught it | D | cycle 12 |
| A library edit needs a `name`, and a form with no `scanner` is 400 | It needs an `agent`; `agent` alone is a 200. plexsim refused the client's own library-upgrade form every time | F | cycle 12 |
| `notify_refreshed` emits its `updated 49` after `loading` | It comes SECOND, before `processing` — **which the function's own docstring already said**. The canonical class-C shape: the docs agreed with themselves and the code did something else | C | cycle 12 |
| A detail row's tags carry `count` (items in the library sharing the tag) | They carry none, on either server. `count` appears only on the tag `Directory` rows of `/hubs/search` and `/library/search`, where plexsim already emits it with the right value. Recorded in cycle 11 as a "boundary note" from a hunter's report and written into this file as cycle 12's lead territory **without being verified first** — the cheapest possible instance of class F | F | cycle 12 |
| The light-row `Director` cap is "only known to be above 2" | It is **3**, like `Role`; `Producer` is not on a light row at all. The richest Director row in cycle 12's 240-movie sample had two | B | cycle 13 |
| All episodes at `Start>0`: "kids drops it, fun keeps it, follow kids" | The row follows the PAGE's row count, not the offset. kids has TWO seasons, so every page past the first there is a one-row page — the servers never disagreed, and fun was right | B | cycle 13 |
| A hub with no `count=` carries up to 12 rows | It carries **6** and says `more`. No reference ever answered 12: with five movies and one show, no shelf on kids can reach even six | B, D | cycle 13 |
| An empty tag value matches the rows with no value | Only for `contentRating` and the languages. A tag-TABLE field matches NOTHING (37 of 600 zen movies have no Genre and `genre=` still answers none) and `studio`/`editionTitle` match EVERY row. The old check's own comment — "`genre=` returned none because every row has a genre" — was an inference from five movies | B | cycle 13 |
| A row with no value sorts above every value | It sorts BELOW — and twelve of the fifteen advertised sorts default to `desc`, so the grid opened on the valueless rows | C | cycle 13 |
| Unmatch edits the row it is called on | Every identity write CASCADES: a season takes its episodes, a show takes its seasons and episodes, and each descendant's parent links follow | B | cycle 13 |
| Seven more write rules reported in cycle 9 | Refuted: progress `state=playing` opening a session; a bogus `playQueueItemID` selecting by ratingKey; a library type change being a 400; a collection create with no uri being a 400; smart collections tagging members; a whole-library playlist add adding nothing; `continuous=1` stopping at one episode | F | cycle 9 |

| `titleSort` is the article-stripped title | It is the title TRIMMED and combining-mark FOLDED first; the recorded rule's only witness was fun, out of scope since cycle 11 | B | cycle 14 |
| The word matcher compares titles as they are written | `/hubs/search`, `/library/search` and a one-word `title=` all FOLD combining marks. plexsim split on `[^0-9a-z]`, so an accented letter was a word SEPARATOR and "Amélie" was indexed as "am" + "lie" | B | cycle 14 |
| A movie library advertises 28 filters and an episode list 17 | Those are 1.43.4's. 1.42.2 — the build plexsim reports — advertises 22 and 9, and `section_filters.json`'s own provenance note already said the per-version lists are shorter | B, C | cycle 14 |
| Which row survives a `group=` is an unknowable SQL artifact, so keep the sort's first | It is the group's minimum `addedAt`, chosen BEFORE the sort — the surviving set is the same under every sort | C, D | cycle 14 |
| A `group=` key's type prefix is decoration to strip | It is a SCOPE, like a filter's and a sort key's | C | cycle 14 |
| The `actor` axis lists every Role tag in the library | It is the union of each item's FIRST THREE roles, and actor-only | B | cycle 14 |
| A string field keeps exactly the six operators the FieldType advertises | That restriction is the ADVERTISED FieldType's. `titleSort`, `summary`, `tagline` and `originalTitle` are in no Meta and take `>>`/`<<` | B | cycle 14 |
| `/library/sections/{id}/recentlyReleased` is a listing | It is a 404 on both references. plexsim invented it: the name is in no findings document and no suite, only in `server.py` | F | cycle 14 |

| A show's prefs write pushes the refresh cascade | It pushes it for `showOrdering` and for NOTHING else; `flattenSeasons`, `episodeSort`, `useOriginalTitle` and `subtitleMode` push zero frames and change the ROWS instead | B | cycle 14 |
| On Deck takes the LOWEST-numbered in-progress episode | The MOST RECENTLY VIEWED one. Cycle 9 measured it on ten-second clips, where nothing stays in progress | B | cycle 14 |
| A hub container says `allowSync: false` | True on every hub path but `/hubs/metadata/{id}/postplay`; `/hubs/search` and `/hubs/items` carry none | B | cycle 14 |
| `/hubs/metadata/{id}/postplay` carries neither the librarySection triple nor `allowSync` | It carries `allowSync`, as false. Only the triple half was right | B | cycle 14 |
| A merge absorbs the other row whatever the type | A merge whose TARGET has no `Media` absorbs nothing -- and plexsim's cascading delete made a season merge destroy the season's episodes | C | cycle 14 |

| `titleSort` trims both ends (`.strip()`) | It trims the LEFT only. zen holds no row with trailing or doubled whitespace, so cycle 14 recorded the symmetric reading with the trailing half explicitly unwitnessed; written on the copy, a trailing space survives | B, D | cycle 15 |
| `/status/sessions/history/all` answering `size 0` is unexplained | It lists nothing without a `type=`, and the `type` is the row's `viewType` -- kids' MOVIES and zen's EPISODES both answer to `type=1`. Carried in 3.3 for four cycles | A | cycle 15 |
| `title=` folds only while the query holds no space | The space was never the rule -- the COLON was. A row matches if the raw word-prefix matcher matches OR a folded pass in which every term but the last must equal a WHOLE whitespace word. 52 queries, 27 fitted and 25 held out | B, D | cycle 15 |
| The library-wide `flattenSeasons`/`episodeSort` behave like the item path | They do -- and the item value is a TRI-STATE where `-1` INHERITS. plexsim read `-1` as "off", so the library-level write moved the setting list and half a row | C | cycle 15 |
| An unwritten show's `episodeSort` is `0` | It is `-1`, equal to the setting's own default. `item_prefs.json` was captured from a show that had been written, and `-1` is the value that inherits | B | cycle 15 |
| `/library/search` finds a collection as a tag row | Only `/hubs/search` does, and the library's own `?type=18` search box. Recorded in cycle 14 from the hubs measurement | F | cycle 15 |
| A member's `Collection` tag carries `guid` AND `count` | The guid only -- which cycle 12 had already shown for a detail row's tags in general | F | cycle 15 |
| `PATCH` and `OPTIONS` are blanket 404s | They are ROUTED like any other verb: `/identity`, `/hubs` and `/playlists` answer 200 to both. Reported from the paths the hunt tried | B | cycle 15 |

| `titleSort` is the article-stripped title on a listing, and a listing's default order | A show library's `type=3`/`type=4` grid defaults to BROADCAST order -- the show's sort title, then `parentIndex`, then `index`. findings.md had already asserted it for "`/allLeaves` AND the section listing"; only the allLeaves half was implemented | C | cycle 16 |
| A `firstCharacter` bucket is counted and listed on the same basis | The AXIS counts a season or an episode by its SHOW's sort title and the LISTING filters on the row's own, so a season list's `A` bucket says 64 and answers 0 rows. 28 of 28 buckets on zen, both types | B | cycle 16 |
| `/library/sections/{id}/{leaf}/{anything}` is the leaf | It is that leaf's BUCKET or a 404 -- never the leaf again. plexsim answered 200 with the axis to every one of ~20 paths | F | cycle 16 |
| An artwork path is a 200 wherever the row has artwork; an EPISODE alone owns no `/art` | It is a 200 only on the row that OWNS the image, and a borrower carries the owner's path verbatim. Cycle 15's episode rule is the special case | B | cycle 16 |
| A real PMS answers the same text to every client | It LOCALISES by `X-Plex-Language`, in 28 languages, across the menus, the hub titles, a generated `Season N`, the track labels and 200+ pref strings. plexsim read the header nowhere | B | cycle 16 |
| The token gate is checked before anything else | A real PMS ROUTES first: an unknown path is a 404 without a token, eleven single-segment paths are 401 and every other one is a 403 | B | cycle 16 |
| The Accept `q` grammar drops a zero or unparseable q | It RANKS it -- `application/json;q=0` alone is answered in JSON. Six of cycle 5's readings were wrong | D | cycle 16 |
| `includeBefore`/`includeAfter` need a `window=` | They window the queue with none, the radius unlimited; every value but 0/1 is a 400. Cycle 9 measured them only ever WITH `window=1` | A | cycle 16 |
| Only the SELECTED play-queue item's delete answers the pre-mutation container | EVERY item delete does; the selected one merely also drops the rows | B | cycle 16 |
| A playlist's items are a fixed list, and `/playlists` is unfiltered | Both are real queries -- a typed one with its own `Meta`, and a listing filtered by `title`, `smart`, `sectionID` and `type` | B | cycle 16 |

| A fixture captured off kids is version-neutral unless it is a Filter list | The `Meta.Type[].Field` list and `/system/agents` are per VERSION too -- 1.42.2 advertises 34 movie Fields and 20 episode ones and four modern agents, where 1.43.4 has 40, 26 and six. Cycle 14 split one fixture of three | B | cycle 17 |
| The six media-codec axes are fields a 1.42.2 listing filters on | They are UNKNOWN fields there: `videoCodec=h264` answers all 3,887 of zen's movies and `/query/parse` reports an `Error` | B | cycle 17 |
| `translations.json`'s coverage is as wide as it needs to be | 119 (attribute, string) pairs the reference translates were missing -- the whole library and item PREFERENCES family, the seventeen search hubs, the butler and a generated `Episode N`. §3.3 had flagged it since cycle 16 | B | cycle 17 |
| The localiser's row guard is the whole rule | A row's `Preferences` block and its `Stream` language fields ARE translated inside it; only the row's own titles and tags are not | B | cycle 17 |
| `titleSort` trims whitespace and then the article | A leading run of PUNCTUATION comes off too, before the article -- eight ASCII characters plus every codepoint from U+0080..U+00BF and U+2000..U+2FFF. zen's `'Til Death Do Us Art` sorts as `Til Death...` and sits in the `T` bucket | B | cycle 17 |
| `quoteattr` is close enough for XML attribute values | A real PMS escapes into pure ASCII: numeric `&#34;`/`&#39;`, HEX `&#xA;`/`&#xD;`, a decimal reference for every codepoint from U+0080 up, and a literal `?` for every control XML forbids | D | cycle 17 |
| An artwork candidate's `key` and `thumb` are its uri | A LOCAL candidate's are `/library/metadata/{id}/file?url=<encoded>`, which really serves the image; the bare uri is the `ratingKey` alone. plexsim's `img src="upload://..."` could never load | B | cycle 17 |
| An edit applies what it can and refuses the rest | A refused save writes NOTHING, and the item path's 400 means "this request edits nothing" -- with a real edit beside it every unrecognised name is IGNORED. The recorded rule was measured with the unknown name ALONE | A, C | cycle 17 |
| A bad int on a library preference is ignored | It is stored as **0**. The old reading came from watching the write answer 200 without reading the value back | D | cycle 17 |
| An activity's subtitle is `title (year)` | It is the row's FRAME title -- a season's `<show> S01`, an episode's `<show> S01 E01`. `notify_timeline` computed it correctly and the helper beside it did not | C | cycle 17 |
| A read-only route 404s every verb but GET and HEAD, everywhere | That is the MULTI-segment rule: a KNOWN single-segment path (`/butler`, `/devices`) falls through to the legacy plug-in dispatcher for the verbs it does not implement | B | cycle 17 |
| An episode's borrowed artwork is stamped with its own `addedAt` *(hunt A's reading)* | Only on the show's `/allLeaves`; the section grid disagrees 641 times on zen. A property of the LIST, not of the row | B | cycle 17 |

| `titleSort` is DERIVED for every row | A title the SERVER generated is stored NOWHERE -- an empty sort title (740 of zen's 741 seasons answer `titleSort=`), and absent from the search index and the `title` filter as well. plexsim derived one for every row, so its seasons bucketed under `S` and answered a search for "season" | B | cycle 18 |
| A section's search box never returns seasons *(hunt A's reading)* | It returns the ones whose title somebody TYPED. Every season on both references is titled `Season N`, which the server generated, so the sample could not separate "seasons" from "generated titles" | B | cycle 18 |
| The fold is Unicode combining marks | Eleven Latin-1 characters fold that NFD cannot reach (`Æ→E`, `Ø→0`, `ð→O`, `ß→s`, …), swept character by character over U+0080..U+02FF. zen's `Død Kalm` sorts as `D0d Kalm` | B | cycle 18 |
| `title=` reads the row's title | It reads the ORIGINAL title too, and only the bare `=` does: 1,484 of zen's 3,887 rows against 1,481 | B | cycle 18 |
| An unlisted string field TAKES `>>`/`<<` | It takes them and COMPARES on them -- plexsim allowed the operator and fell through to containment, so `titleSort<<=0` matched every title holding a `0`. `<=`/`>=` order there too, where an advertised field keeps begins-with/ends-with | C | cycle 18 |
| `All Movies` and `All Shows` are constants | A MOVIE library COMPOSES it from its own title ("All Zen Movies"); only a SHOW library answers the constant. plexsim had each half wrong in the opposite place, and its own two lines disagreed | B, C | cycle 18 |
| A section's `/search` container is typed by the library | It is typed by the ASKED `type=`: `type=3` is `viewGroup=season`/`viewMode` 65593 even on a movie library, `type=4` carries `mixedParents` | B | cycle 18 |
| A plug-in framework container (`noHistory`) ignores paging | `/system` PAGES -- `totalSize` first, no `offset`, response headers -- and only `/system/scanners/{N}` does not. Cycle 9 inferred the rule from that one sample | B, D | cycle 18 |
| `/identity` and `/hubs` answer any verb | They answer the plug-in DISPATCHER to every verb but GET and HEAD, like every known single-segment path. The recorded rule was plexsim's own GET handler answering | B | cycle 18 |
| A real PMS never answers 405 | The `/system` subtree does, to PATCH, on every path under it -- it is a different server with its own envelope (a bare OPTIONS is an empty 200, everything else an empty-bodied 404) | B | cycle 18 |
| `/media`, `/services`, `/status` and `/statistics` are empty | All four are the dispatcher's POPULATED listings, byte-identical on both references, and `/security` is a real container with NO `content` key | B | cycle 18 |
| `limit=` means one thing | Three endpoints, three meanings: `/library/recentlyAdded` caps and rewrites `totalSize` (negative lifts the cap), the history page caps and `-1` answers NONE, a section listing answers ONE row to 0 | B | cycle 18 |
| `parentIndex` is a 500 on a listing *(rule right, code wrong)* | Implemented on the section listing alone; `/library/all` and `/playlists` are listings too | C | cycle 18 |
| `section_prefs_new.json` is version-neutral | The FOURTH fixture found to be per version: 16 of 35 (type, agent) cells differ, and on 1.42.2 the agent's settings are appended whatever the type | B | cycle 18 |
| An item's language enum is the fixture's | It is led by the ITEM's own stream languages, audio first then subtitles, before the generic list | B | cycle 18 |

| A missing numeric column compares as 0 | A row with NO value matches NOTHING, `!=` and `!==` included -- invisible on a library where every row has a value | B | cycle 19 |
| A `boolean` is `state == target` with target 1 only for `1` | Only `field=1` and `field!=0` answer the true set; every other operator and value answers the FALSE set, `field!=2` included | B | cycle 19 |
| A `tag` field takes `=` and `!=` and answers nothing else | That is the tag-TABLE fields; a tag-typed COLUMN (`contentRating`) takes all eight, with the four ordering keys a byte comparison | B | cycle 19 |
| `resolution` under an unadvertised operator matches nothing | Every operator, `!=` included, answers the `=` set -- and `=` matches ANY of the row's Media, not the first | B | cycle 19 |
| `?includeDetails=1` always carries the advanced sort list | It honours `includeAdvanced=1` like every other form, carries no `folder` Type row and marks none active | A | cycle 19 |
| A `Meta/Type`'s `active` is a property of the library | It follows the REQUEST's `type=`; the captured fixture's stored flags are one request's answer | A | cycle 19 |
| `excludeFields=` trims the row and the container | It strips the named field from every CHILD ELEMENT too | B | cycle 19 |
| `excludeElements=` removes whatever it names | `Image` and `UltraBlurColors` are no-ops on both references, like an unknown name | B | cycle 19 |
| One child-element order serves every row | A LIGHT row puts `Guid` before its tags where a detail row puts it after | B | cycle 19 |
| JSON key order is not a measurement | A real PMS's JSON key order IS its XML order, on both references and all four row types | B | cycle 19 |
| A library edit applies the whole form and SCANS | It scans only when the form carries `language` or `scanner`; cycle 9's form carried a language | A | cycle 19 |
| `CleanOldBundles` steps its subtitle through FOUR phases | Seven: the analysing phase is followed by two `Deleting N metadata bundles` steps at 25 and 50 | B | cycle 19 |
| `?path=` on a section refresh is decoration | It SCOPES the scan; a path the library does not hold scans nothing at all | B | cycle 19 |
| `/:/prefs` accepts any known setting | Four answer 403 even writing back their own value, and two integer settings take a word | B | cycle 19 |
| `server_prefs.json` is version-independent | The fifth per-version fixture: `AllowLegacyAgents` and `CLDriver` are 1.43.4-only | B | cycle 19 |

| **A season with a `year` carries its title as `titleSort`** (cycle 7) | **B** | Fitted to kids' season 7 -- the only season with a year the lab has -- whose `Season 1` came from the AGENT and is therefore stored. zen has 741 real seasons: 740 carry no `titleSort` at all and the one that does carries `Egghead`, not its displayed `Season 22`. The sort title follows the STORED title, which is cycle 18's rule |
| **A NULL date matches "is before"** (pre-cycle-11, fun) | **A** | Measured on fun, which is out of scope. On zen and kids a null date matches NOTHING, on every operator -- of 3,887 movies the 1,518 with a value answer `<=2100-01-01` and the other 2,369 answer neither half. It put every season of a show library into every `<=` set |
| **`id==`/`id!==` never match a number** (cycle 11) | **B** | Measured against a value that was not a number, where they really do match nothing, and recorded as if it were the whole rule. Against a NUMBER both compare like `=`/`!=`, on both references over two libraries and two row types |
| **The four unlisted string fields compare case-insensitively** (cycle 18) | **B** | Measured on `titleSort` and read onto the other three. `summary`, `tagline` and `originalTitle` compare BYTES: `summary<=b` is 3,884 of zen and `<=B` 2,911 |
| **An artwork URL's version is a hash of its source URL** (cycle 8) | **D** | A defensible constant nobody measured. It is the row's `updatedAt`, on 726 of 726 owned paths on zen and 12 of 12 on kids, and the copy re-versions the poster when `updatedAt` is written |
| **Every edit moves `updatedAt`** (plexsim's own, never measured) | **D** | An edit moves nothing -- five fields written to one movie over half a minute left the column where it was, while a poster write in the same session moved it |
| **An episode's inherited `contentRating` is copied when the show is written** (cycle 13/16) | **C** | The rule was recorded correctly and implemented as a write-time copy. It is a READ-TIME fallback: clearing the episode's own makes it read the show's at once |
| **`unmatched` is a boolean** (cycle 19's rule, applied by family) | **C** | It ignores the operator entirely: only the literal `1` is true, under all eight. `unwatched` really is a boolean and was the control |
| **A parent row's `duplicate` is one scalar** (cycle 19) | **C** | The any-leaf rule was recorded and implemented as a collapse, so the two halves were complements by construction. On zen 24 shows and 35 seasons are in BOTH |
| **A media column on a parent always reads the leaves** (cycle 19) | **C** | True of `duration`, `resolution` and `mediaSize`; NOT of `audioLanguage`/`subtitleLanguage`, which answer nothing at all on a show or season row |
| **A `/:/` state write's `key=` is a path whose last segment is the ratingKey** (plexsim's own, never measured) | **D** | It is a BARE ratingKey. `/library/metadata/17` is a 404 on scrobble and unscrobble and a 500 on rate and progress; plexsim split on `/` and accepted the shape a real PMS refuses | cycle 21 |
| **All four `/:/` writes answer 400 with no key and 404 for a key that is not there** (cycle 16) | **B** | Measured on one key shape. `rate` and `progress` int-parse the key and answer 500 for a word (and `rate` for an empty value); `/:/progress` answers 200 to ANY integer key, present or not | cycle 21 |
| **A refresh re-versions the artwork and moves `updatedAt`** (plexsim's own, never measured) | **D** | It moves neither -- and the reference really ran the refresh, because it pushed its seven frames each time. Every Refresh Metadata told every client an unchanged row's poster had changed | cycle 21 |
| **A playlist's born `viewCount`/`lastViewedAt` pair is the LISTING row's own** (cycle 16) | **B** | Measured on `/playlists` alone. The whole per-user-state block is on `/library/metadata/{id}` too, and what omits it is `/playlists/{id}` and the create response | cycle 21 |
| **A TEXT library preference carrying `%` is stored EMPTY** (cycle 17) | **B** | Only a MALFORMED escape is. The value is percent-decoded a SECOND time and then escaped for storage, so `%2541` stores `A` and `%2525` stores `%25` | cycle 21 |
| **A section `/analyze` frames the library's movies** (cycle 15) | **C** | It frames the LEAVES. The movie library's frames-per-row was the leaf rule seen where a row is its own leaf, so a show library pushed nothing at all | cycle 21 |
| **An empty-bodied `/system` 404 is an empty `text/html`** (cycle 18) | **B** | Measured on the body length alone. It carries NO `Content-Type` at all -- and the GET was never covered, so it kept plexsim's own `text/plain` refusal | cycle 21 |
| **`/library/collections/{id}` is a list writer** (plexsim's own, never measured) | **D** | It is the ITEM edit path, with the whole grammar behind it; plexsim ran a second, weaker code path there that answered 200 to every form while the metadata path was already right | cycle 21 |
| **"every other element `excludeElements=` names really goes"** (cycle 19) | **B** | A two-name sample generalised the wrong way. It is an ALLOW-LIST of ten; `Extras`, `Related`, `Preferences`, `Field`, `Chapter` and `Review` are no-ops, and `Part` is a real name on it | cycle 22 |
| **`excludeElements=` applies to the top-level row** (plexsim's own, never measured) | **C** | It reaches the rows INSIDE the inline `Related` and `Extras` blocks too, which is the one request whose purpose is to make the response smaller | cycle 22 |
| **`checkFiles` is the one option with that value grammar** (cycle 19) | **B** | Five more carry it exactly: `skipRefresh`, `asyncCheckFiles`, `asyncAugmentMetadata`, `asyncRefreshLocalMediaAgent`, `asyncRefreshAnalysis` | cycle 22 |
| **An `include*` option is on for the string `1`** (plexsim's own, never measured) | **D** | Four of them parse the value as an INTEGER and take `01`; `includePreferences` alone compares the string | cycle 22 |
| **A text `sort=` folds case** (plexsim's own, never measured) | **C** | It compares RAW CODEPOINTS -- lowercase sorts ABOVE uppercase. Cycle 20 measured the same split on the FILTER side and the sort engine never followed | cycle 22 |
| **A filter key with a stray comparison character is a 400** (cycle 11) | **C** | Cycle 11 measured a pair with NO `=` at all, which is still a 400. A well-formed pair whose NAME keeps one is an unknown FIELD -- a 200 listing every row | cycle 22 |
| **A `type=` parameter is never an item edit** (plexsim's own, never measured) | **D** | On the ITEM path a bare `type=<anything>` is an accepted no-op; only the SECTION path reads it as the scope | cycle 22 |
| **A timeline frame's type comes from the row's own `type`** (plexsim's own) | **C** | A STORED collection or playlist row carries no `type` -- the projection mints it -- so every frame plexsim pushed for one said `type 1`, and the client builds the URL it updates from that number | cycle 22 |
| **A real PMS's XML is one line** (plexsim's own, never measured) | **D** | One element per LINE, and the document ends with a newline: 236 of them in one zen movie | cycle 22 |
| **`_ALWAYS_PAIRED` is `Feature` alone** (cycle 21) | **B** | Six more names are paired when empty -- `MediaContainer`, `Stream`, `Hub`, `Extras`, `Media`, `Activity` | cycle 22 |
| **`DELETE /butler/{Task}` is a 200 that CANCELS a queued task** (cycle 22) | **F** | Re-measured with a control proving the task would otherwise run: it is the **404** that cancels and the 200 that does not. `DELETE` within the queue window is a 200 and the task still runs; after it, a 404 and the task does not | cycle 23 |
| **`Hub` is paired when empty** (cycle 22) | **D** | A name rule read off a census of ~45 paths that never reached `/hubs/sections/{id}/manage`, whose childless `Hub` SELF-CLOSES on both references. The rule is the one cycle 22 left open for `Directory`: paired when the element carries a child collection | cycle 23 |
| **`includeDetails=1` carries no `advanced` on a Filter** (cycle 16) | **B** | Measured without `includeAdvanced=1`, where the Duplicates row does not exist at all. With the pair, the references carry NO such row and plexsim carries one | cycle 23 |
| **A `/search` row is the detail row** (plexsim's own, never measured) | **D** | It is the GRID row with a six-attribute lead, byte-identical otherwise | cycle 23 |
| **The `Meta`'s active Sort is the library's own default** (plexsim's own) | **D** | It follows the asked `type=` AND the `sort=` parameter, and a cell whose type was not asked marks nothing at all | cycle 23 |
| **A `Common` row's relational attributes come last** (plexsim's own) | **D** | The grandparent pair LEADS the element and the parent pair precedes `ratingKey` | cycle 23 |
| **plexsim's two writers agree everywhere** (cycle 19, after cycle 22's light-row fix) | **C** | `_send_container` POPS the `_xml=` override before either writer runs, so `/library/sections/{id}/common` was still ordering its JSON by build order | cycle 23 |
| **The extended title's language part follows the first-word rule** (cycles 17-23) | **B** | It has its OWN rule: a SINGLE token, with any trailing parenthesised group stripped from the name first. The first-word rule missed 179 of zen's rows and cycle 22's whole-name rule broke the recorded corpus | cycle 24 |
| **A sidecar subtitle's codec suffix is the constant `External`** (cycle 17) | **B** | It is the stream's `providerTitle`, and `External` is the FALLBACK. Measured on one server, where no sidecar carried a provider | cycle 24 |
| **`DELETE /butler/{Task}` is a 200 that cancels** (cycle 22) -- see also the row above | **F** | Already corrected in cycle 23; repeated here because cycle 24 left the butler unimplemented and the corrected grammar is what cycle 25 must build to | cycle 23 |

**Cycle 25 overturned eight more.**

| Belief | Held since | Overturned by | Class |
|---|---|---|---|
| "a SEASON's unmatch runs the seven-frame refresh sequence" | 9 | It runs the SEASON cascade; a SHOW's pushes 46 frames where plexsim pushed none; and the opener differs by row type (a movie and a show take the `matching` PAIR, a season one frame) | C |
| "a season cascade's activity has no `updated` frame" -- the docstring -- against `finish_activity`, which inserts one | 15 | 18 frames on the reference, 19 here. The rule was recorded right and implemented as the nearest helper to hand | C |
| "the `anamorphic` threshold is bracketed to (1.028, 1.067]" | 23 | 358 of zen's streams over 16 distinct ratios: the ratio ROUNDED TO ONE DECIMAL is not 1.0. A pixel-difference model is refuted outright | D |
| "`/match` answers EVERY verb with its 400" -- recorded -- against a code list of two | 17 | Re-measured over all seven verbs on kids | C |
| "`system_agents.json` is the modern agents captured from a real PMS" | 5 | Captured with NO `X-Plex-Language`, so its `Language` list is the one a real PMS answers only to a client that asks for no language. **Class A, the failure this project's one rule exists for** | **A** |
| "the localiser reaches inside a row for exactly two blocks" | 20 | A `/tree` node is a row too -- keyed by `id`, not `ratingKey` -- and its `Setting` rows are NOT localised, so plexsim translated what a real PMS leaves alone | C |
| "a SCROBBLE writes NO history row" | 9 | It writes one per LEAF marked, every time, driven one scrobble at a time with the count read between each | F |
| "`POST /butler/{Task}` QUEUES rather than runs, and `/activities` retains the finished wrapper" | 23 | Neither reproduces on a freshly reset copy: the first frame is at +0.0 and `/activities` is empty the moment the run returns | F |

### 3.3 Standing on thin evidence right now — revalidate before building on these

| Item | Why it is thin | How to settle it |
|---|---|---|
| **Every write rule from cycles 9–12** | plexscratch is their ONLY possible witness: kids is read-only and zen cannot answer a write at all. That includes all of cycle 12's frame sequences, the upload grammar and the queue rules | Nothing settles it short of a second writable copy. Treat "measured on the copy" as one witness and say so in every write rule you record |
| ~~The three cycle-13 cascades cycle 14 re-measured and did NOT implement~~ | **BUILT in cycle 15**, each re-measured on the copy with a control write first. The refresh cascade is 7/18/**49** frames (cycle 14 counted 40); the analyze triple is per LEAF and never on a season or show row; the library create is 75 frames with its `queueRegenerated` at **+63.1 s**. What is left of the create is its per-item half, which is over real files a plexsim library does not have | — |
| An episode's INHERITED content rating versus its own | Both are the same string on the row, so plexsim cannot tell "no rating, showing the show's" from "its own, equal to the show's". The hunt saw a show edit move one and not the other | Only a second column would settle it; record the derived half and leave the edit-follow half alone |
| `/status/sessions/history/all` answering `size 0` | Reproduced on both servers and still unexplained; plexsim's listing was built against fun's 3,879 rows, which are no longer readable | Find the parameter or account state that makes a real PMS list history |
| Every rule measured before cycle 10's `long` mode | plexscratch's clips were **ten seconds**, so any timeline rule measured on them crossed 90% instantly | Re-run anything about sessions, completion or resume from cycles 9 or earlier on `long` |
| Every row-shape rule measured before cycle 12 | Eleven cycles used a five-movie, one-show library. Cycle 12 found seven rules invisible there and there are certainly more | Keep pointing hunts at zen's 3,887 rows |
| Anything whose only second witness was fun | fun is out of scope. Its `newest` bound, its Recently Viewed order and its postplay shelves are unverifiable now | Re-measure on zen, or mark the rule single-witness |
| Rules measured on kids alone | kids is **1.43.4** and plexsim reports **1.42.2** | zen answers 1.42.2: one request settles it |
| ~~The `addedAt` TIE inside one `group=`~~ | **SETTLED in cycle 15.** Movies 1 and 17 on the copy share `addedAt` 1789187071; grouped together and renamed so that "lowest ratingKey" and "lowest titleSort" predict different survivors, the survivor is the lowest ratingKey under every sort. plexsim was already right | — |
| ~~The TRAILING half of `titleSort`'s whitespace trim~~ | **SETTLED in cycle 15.** It is an `lstrip`: a trailing space survives, and a title written with one carries no `titleSort` at all | — |
| ~~The one-word condition on the `title=` fold~~ | **SETTLED in cycle 15, and overturned.** The space was never the rule; the colon was. The model is fitted on 27 queries and held out on 25 more it reproduced exactly | — |
| **A non-alphanumeric `firstCharacter` bucket** | The digits half is solid (29 + 13 + 13 + 185 rows across four library/type pairs). The "any other non-letter gets its own bucket" half rests on **one row** on zen. The percent-encoding is corroborated on kids' other axes | A library with several punctuation- or CJK-led titles |
| **The non-English article strip** | `La` (4 rows) and `Das` (1) are stripped while `Les`, `Die` and `O` are not, so the article list is per-item LANGUAGE and not mechanical. Deliberately NOT implemented — plexsim's catalog carries no per-item language | Nothing short of a library with real per-item languages |
| **`banners` and `thumbs` answering EMPTY** | Both are 200 on both references; `banners` is empty on both, but `thumbs` is populated from the media BUNDLE, which plexsim has none of. The empty answer is the honest shape, not a measured value | Nothing to settle: plexsim has no bundle |
| **A merge on a SHOW** | The season half is measured (a 200 no-op on two seasons); a show has no `Media` either and takes the same branch, but the copy holds ONE show | Put a second show on the copy and merge them |
| ~~The `episodeSort`/`flattenSeasons` row effects at the SECTION level~~ | **SETTLED in cycle 15.** The library-wide write produces exactly the item path's row effects, and the item value is a tri-state where `-1` inherits — measured in all four combinations | — |
| **Whether a cascade CREATES a borrowed artwork field** | The artwork cascade moves `parentThumb`/`grandparent*` on children that already carry them; no measurement says what happens to a child that carries none | Strip one on the copy, then pick a poster on its parent |
| **`DELETE /library/metadata/{id}` on a season and a show** | The copy answers **400** to every media-item DELETE — episode, season, show and movie alike — while a collection DELETE is a clean 200, with `allowMediaDeletion` true and the media mounted read-only. So whether a real PMS cascades a season delete onto its episodes is unproven, and plexsim's own cascading delete is unverified. The merge half of the same question was settled in cycle 14 | A claimed copy with writable media |
| **The `queueRegenerated` frame's DELAY** | Measured at +63.1 s after a library create and +7.1 s after an analyze, and by cycle 14 at +2.4 s after a season split and +1.2 s after a collection add. plexsim pushes its frames synchronously and emits it with the write | Nothing a synchronous server can reproduce; recorded rather than faked |
| **The non-alphanumeric `firstCharacter` bucket** (again) | Re-read AGAIN in cycle 17, this time against zen's section 2 -- 507 shows, 741 seasons, 9,287 episodes, the instrument that did not exist when this was written. It contributes no non-digit non-letter-led title at all, so the "its own bucket, listed after Z" half still rests on the single `『` row in section 1 | A library with several punctuation- or CJK-led titles |
| ~~plexsim's `Server:` banner~~ | **SETTLED in cycle 16.** Neither reference sends one, on fifteen paths and every status; it is gone, and every suite still passes | — |
| ~~The translation table's coverage~~ | **WIDENED in cycle 17** to 451 entries over ~100 paths, and `capture_i18n.py` is now tracked in `handover/tools/`. It is still only as wide as that list: the prefs family, the search hubs, the butler and a generated `Episode N` are in, and anything plexsim emits from a path still off the list stays English | Add paths and re-capture; the diff is mechanical |
| ~~The XML half of the localisation~~ | **SETTLED in cycle 17.** Diffed language by language on four paths in de and fr: plexsim moves exactly the attribute KINDS zen does (`Filter.title`, `Sort.title`, `Field.title`, `Type.title`, `Directory.title`, `Hub.title`), with no kind on either side the other lacks | -- |
| ~~A ROW's XML attribute ORDER~~ | **SETTLED in cycle 17.** 25 element buckets, each a topological sort of every PAIR both references emit and each verified as a superset of every observed sequence; 634 inverted pairs fixed and 0 remain | -- |
| **The 1024 / 4096 / ~41 KB header limits** | Measured as BRACKETS -- 1024 passes and 1100 fails -- not probed to the byte. The refusal is real; the exact constants are inferred, which is class D | Bisect, if it ever matters |

| **The `titleSort` punctuation set outside the two swept ranges** | Every character of U+0080..U+00BF and U+2000..U+2FFF strips and every block boundary was probed, but U+0300..U+1FFF and U+3000+ were sampled rather than swept. The eight ASCII members were each written individually | Sweep the gaps, if a title ever lands in one |
| **Whether an EPISODE refresh runs the SHOW's activity** | The hunt reported it and one of the driver's two captures reproduced it; the other showed the episode's own seven-frame sequence. Two runs of the same request on the same copy disagreed, so only the SUBTITLE half is implemented | Run it repeatedly on a freshly reset copy and see which is the steady state |
| **The butler's per-task COUNTS** | The message TEMPLATES are measured verbatim on the copy; the numbers in them are that server's content, and plexsim fills in its own. A template whose count plexsim cannot derive honestly (albums, artists, cache files) is hard-coded at 0 | Nothing: plexsim has no music library and no cache |

| **Which titles count as GENERATED** | The rule is measured from three directions and is not in doubt; what plexsim cannot see is the DISTINCTION. kids' season 7 is titled `Season 1` by TMDB and stores it, and 176 of zen's episodes are titled `Episode N` by their agent and store it too, so plexsim decides on the title FORM -- a proxy, and a class-C shape by construction. Every season plexsim holds is one it synthesised itself, so the proxy is exact for its own rows | Nothing short of a column the row does not carry; re-check it if plexsim ever takes season titles from an agent |
| **The eleven-character fold TABLE** | Swept exhaustively over U+0080..U+02FF on the copy (1.43.4) and witnessed twice on zen (1.42.2), so the rule is not single-version -- but the eleven TARGETS come from the copy alone, and nothing explains why `ø` becomes a digit | Write the eleven on a second writable copy, if one ever exists |
| **The `/hubs` refusal's scope** | Both references hold a show library, so "the server-wide `/hubs` always refuses" and "it refuses because a show library exists" are the same measurement. plexsim refuses on the same terms | A reference with no show library |
| **The library delete's frame IDS** | 14 frames for a nine-row library, keyed on the server's own internal metadata ids; nothing in the response explains the id space. plexsim frames the rows it removes | Nothing the API exposes |

| **The four write-protected settings** | `PlexOnlineUrl`, `TranscoderH264Options`, `TranscoderH264OptionsOverride` and `allowMediaDeletionLanOnly` answer 403 on plexscratch even when the value written is the one already stored, and nothing in the row marks them. The copy is UNCLAIMED, so `PlexOnlineUrl` could be claim-gated; the other three are not obviously so | A claimed writable copy |
| **`server_prefs.json`'s version split** | Implemented as a FILTER on two ids rather than as two captured fixtures, because plexsim only ever reports 1.42.2. It is the fifth fixture found to be per version; the other four are split properly | Capture zen's own list if plexsim ever reports 1.43.4 |
| **`KNOWN_LANGUAGES`** | The Add Library form's accepted set is read off the captured `languageOverride` enum, not probed exhaustively: 13 values were sent and every one agreed with that list. It is the reference's own data rather than a constant nobody measured, but the two sets have not been proved identical | Probe the whole enum, if it ever matters |
| **The `allowSync`-on-the-ROW signal** | `/library/recentlyAdded` and `/library/onDeck` lead with `allowSync` and the librarySection triple and no other listing does. `allowSync` on the row is used as the tell because the XML writer cannot see the path; it holds on every row measured, but it is a proxy for "this endpoint", which is class C | A row carrying `allowSync` on some other path |
| **The `_light` projection marker** | A LIGHT row's child order is chosen by a private key the builder sets, because nothing in the row itself distinguishes the two projections. `_drop_private` strips it from both formats and the suite checks that it reaches no client, but it is a proxy for the same reason | Nothing the response exposes |
| **`sort=duration`, `duration=` and a show's media columns** | The rule is measured -- both read the MEDIA -- and only the show/season half is built: plexsim's own rows always carry a Media duration equal to the row's, so the movie half cannot differ for its catalog. The `audioLanguage` operator table is measured and unexplained | A catalog row whose Media duration differs from its own |

| **The `Specials` and `[Unknown Season]` season labels** | Both are zen's alone -- kids holds neither, its two seasons being `Season N`. 4 and 5 rows, each corroborated by three independent probes (`titleSort=`, `title=`, `/search`) plus the `#` bucket, and the MECHANISM behind them is cycle 18's two-witness rule | A second library with a season 0 or an index-less season |
| **`createdAt` read as `addedAt`** | The column is real on both references (every row answers `createdAt>=2000-01-01`) and tracks `addedAt` almost exactly on zen -- 2,437 rows above one epoch against `addedAt`'s 2,436 -- but the one row that differs proves they are not the same column. plexsim holds no other creation time | Nothing the API exposes |
| **The episode `contentRating` re-inherit** | The rule is a READ-TIME fallback and it is implemented on the WRITE side, because `_public_row` cannot reach the catalog. Every step of the measured sequence is reproduced; a rating cleared by some path that does not go through `library.edit` would not be | Move the fallback into the row projection if one is ever given the catalog |
| **The `mediaCount` column on a parent** | Read as one value per leaf, which reproduces zen's `=1`/`>>=1` split exactly (487/44 shows, 702/74 seasons) -- but that is also the Duplicates split, so the two rules are not independently witnessed on the same data | A library where a show's leaves differ in media count without duplicating |
| **`slug` minted by the agent** | Measured on 2,406 of zen's rows (every `local://` movie and show carries none) and deliberately NOT implemented: plexsim mints a `plex://` guid for every row, so the two rules coincide for its catalog. The children's `parentSlug`/`grandparentSlug` are derived from the parent TITLE rather than from the parent's slug -- a proxy that agrees today | A catalog row that fails identification |
| **The five codec edit-field names held out** | `atmos`, `audioCodec`, `audioLayout`, `subtitleCodec` and `videoCodec` take an item lock on plexscratch, which is **1.43.4**. Cycle 20 established that family is 1.43.4-only on the filter and parse surface, and zen cannot be written, so 1.42.2 -- the build plexsim reports -- is unmeasurable. Held out; `hdr10plus` is a 200 that stores nothing on a single witness | A writable 1.42.2 copy. Nothing else settles it |
| **A refresh not moving `updatedAt`** | plexscratch is isolated from plex.tv, so its agent returns nothing new. What is measured is that running the refresh SEQUENCE does not by itself stamp the row; an agent returning changed metadata might | A claimed writable copy, which needs plex.tv |
| **The collection and playlist action leaves' FRAMES** | The statuses and container shapes are measured and implemented; `/refresh` and `/unmatch` on those two rows push real sequences (3 and 7 frames on a collection, 4 and 2 on a playlist) carrying `state 2` / `metadataState: matching`, a state plexsim has never emitted. Implemented as the statuses alone | Implement the sequences, having re-measured them per row type |
| **A collection's `/allLeaves` and `/grandchildren` ROWS** | Both containers are measured empty on plexscratch (its collection held one movie and the leaf view returned none), so the container is measured and the row projection is inferred from the light-grid rule | A collection whose members make those lists non-empty |
| **The `/system/scanners/{N}` 500 BODY** | All three references answer a 641-byte `application/xml` plug-in-framework traceback; plexsim answers 23 bytes of `text/plain`. The status and type are measured, the body is PMS's own Python traceback | Nothing worth settling: the body names PMS's own file paths |
| **Which butler DELETE actually cancels** | Cycle 23 recorded a 200 inside a queue window; cycle 25 could not reproduce a queue window at all -- a task's frames arrive at +0.0 -- so there is nothing to cancel and `DELETE`, `GET`, `PUT` and `PATCH` are all 404s when idle, which is what plexsim answers. The Dashboard's Cancel button may simply have nothing to do on a copy this small | A server with a task slow enough to have a window |
| **`AC-3` carrying `AC3` in a title** | 132 zen streams. `VC-1` does NOT carry `VC1` and `E-AC3`/`E-AC-3` do not carry `EAC3`, so no punctuation-folding rule fits, and a general one breaks 58 other streams. Cycle 24 scored it and left it | A rule that separates the three, or a decision to special-case `AC-3` |
| **Streams whose stored title is the EMPTY string** | ~37 on zen; they compose `" (1080p HEVC Main 10)"` with a leading space and carry no `title` attribute in XML at all, so a published row cannot be told apart from one with no title | Nothing from a published row; it needs the server's own stored value |
| **The rest of cycle 23's butler findings, WHAT IS LEFT OF THEM** | Cycle 25 re-measured the family on an uncontended socket and **two of the six did not reproduce**: `POST /butler/{Task}` pushes its first frame at +0.0 on a freshly reset copy, not after a 7-9 s queue delay, and `/activities` is EMPTY the moment the run returns. `general.db.backup`'s doubling reproduced and is BUILT, together with its `started` at -1 with the subtitle already set. Still unmeasured this cycle: `CleanOldBundles`' interleaved messages, `RefreshLibraries` reporting before it scans, and `POST /butler` running only what is DUE | Measure the three, on a freshly reset copy with nothing else writing to it |
| ~~**The cycle-23 `mediainfo` model**~~ **SETTLED in cycle 24** | Hunt B fitted a model on 105,558 zen streams and held it out on the 46 recorded probes: misses fall from 613/105,558 and 3/642 to 174/105,558 and 0/642. It adds the language parenthesis-strip rule (which settles cycle 22's five-witness contradiction), `eia_608` -> `Closed Captions` in BOTH titles, `Signs`/`Songs` standing in for Forced, `CC`/`HI` for SDH, `1.0` for Mono, `HDR`/`Dolby` for the dynamic range, the raw ffprobe codec name for audio and the punctuation-stripped label for video, and `providerTitle or "External"` for a sidecar's suffix. The model file is in the cycle's artefacts | Implement it; it is the largest measured block now outstanding |
| ~~**`pixelAspectRatio` and `anamorphic`**~~ | **BUILT in cycle 25** on 1,688 of zen's video Streams (358 carrying the pair, 1,330 neither, none only one). Only the CONSTANT is still inferred: see the new entry below | -- |
| ~~**The `videoResolution` cutoff below 720**~~ | **BUILT in cycle 25** as the table's own smallest-in-bucket construction -- `(856, "480")`, the way the other five entries were built -- with 720 named as the largest below it. The boundary is still un-bisected and the comment says so | A file 721-855 wide |
| ~~**`dub` on a video Stream**~~ **IN as of cycle 24**; `visualImpaired` (1 witness, in neither table) is not | zen carries `dub` on 88 video Streams (never `comment` or `hearingImpaired`), so the video disposition set is default/forced/original/dub; `visualImpaired` is real and in neither table | Implement both from ffprobe's `dub` / `visual_impaired` |
| ~~**`languages.json`: 13 codes, not 3**~~ **the 13 are IN as of cycle 24**; the regional-tag family (`en-US`, `zh-Hant`, ...) is not | `cha guj hye kat mar mdr mon mya nno pan sin yor zul`, each 1-24 streams on zen. Separately 21 streams carry a REGIONAL `languageTag` (`en-US`, `es-ES`, `pt-BR`, `es-419`, `zh-Hans`, `zh-Hant`) the one-name-one-tag table cannot express, and `zh-Hant` changes the NAME too | Add the 13; the regional family needs an ffprobe of such a file |
| ~~**`_channel_label`'s trailing space**~~ **fixed in cycle 24**; the reference's full parenthetical form still has one witness | `8 channels (FL+FR+...)` -> plexsim writes `8 channels )` with a double space. A defect under plexsim's own model; the reference's full form has one witness | Fix the strip; the reference form needs a second witness |
| ~~**Nine more verb-table rows**~~ | **CLOSED in cycle 25**: the last of them, `/library/metadata/{id}/match`'s HEAD/PATCH/OPTIONS, is implemented. Re-measured over all seven verbs on kids, which answers 400 to each | -- |
| ~~**The SEASON and SHOW unmatch cascades**~~ | **SETTLED in cycle 25**, on an uncontended socket, twice over -- once with the show unmatched first on a freshly reset copy and once second, so the ordering is not an artefact of the state the first left behind. The media pass is state-dependent and deliberately not emulated | -- |
| ~~**`/hubs/search`'s row lead**~~ | **SETTLED in cycle 25**: `query=Sintel` hits on kids and both references agree exactly. `/library/search` leads with `librarySectionTitle` alone | -- |
| **The mediainfo LANGUAGE-part rule** | Hunt B fitted "the whole name, not the first word" to 13 of zen's subtitle rows. Implemented, it BREAKS the recorded corpus: `Русский (Full) - Кубик в Кубе` with language `Русский` drops it on a real PMS, which only the first-word rule gets right. Five witnesses across both corpora: `Русский`, `English (Weeb)`, `dansk` dropped; `norsk bokmål`, `Tiếng Việt` kept. The narrowest model that fits all five -- "a MULTI-token name whose title is exactly that name is kept, everything else takes the first word" -- is fitted to two positive witnesses and is class D | A library with more multi-token language names. NOT implemented |
| **`eia_608` labelled `Closed Captions`** | Real on zen (3 streams), and it appears in the PLAIN display title as well as the extended one -- a different composition path from the codec label, which is why it was not implemented alongside the language rule | Implement it on the display-title path, having measured which of the two carries it |
| **The `videoResolution` 480 bucket** | zen's 856×358 Media is `480` where plexsim's model says `sd`; every existing cutoff is its height bucket × 16/9 and the 480 entry (853.3) is missing. zen's 720×400 and 688×384 are `sd`, so the cutoff is in (720, 856] -- **not bisected**, so asserting a constant would be class D | Bisect it on zen's own rows; 3 of 13,643 Media witness it |
| **The dynamic-range part carried by `HDR`/`HDR10` in a title** | Two witnesses on zen, and they disagree about whether the match is a prefix or an any-word test | A third witness |
| **`languages.json` missing `zul`, `cha`, `kat`** | Real on zen (4 of 6,581 language-carrying streams); plexsim's catalog holds no such track, so it changes nothing today | Add the three, having checked the 639-2/T and 639-1 pair for each |
| **The collection and playlist `/refresh` and `/unmatch` FRAMES** | Measured in full this cycle -- a collection's refresh is 3 frames on an agent guid and 8 on a `local://` one, its unmatch 7, a playlist's 4 and 2 -- and they carry `state 2` with `metadataState: "matching"`, plus a `state 3 matching` on a MOVIE and SHOW unmatch, which plexsim emits nowhere. plexsim also pushes ZERO frames for a movie or show unmatch and no cascade for a season's. The STATUSES are implemented (cycle 21); the sequences are not | Implement the four sequences and the two missing `metadataState` values; the measurements are in cycle 22's findings |
| **`DELETE /butler/{Task}` while the task is QUEUED** | A 200 that cancels it; a 404 when idle, and GET/PUT/PATCH stay 404. Measured 3/3. plexsim answers 404 always, so the Dashboard's Cancel button reports failure | Implement it; it needs a queued-task notion plexsim does not have |
| ~~**`POST /butler/BackupDatabase` running its activity TWICE**~~ | **BUILT in cycle 25**, four runs agreeing across cycles 22, 23 and 25. The `started` at -1 with the subtitle already set is built with it; `general.db.optimize` keeps its own 0-with-an-empty-subtitle opener, which is the pair plexsim had collapsed into one shape | -- |

**Cycle 25 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| **The `anamorphic` CONSTANT** | Both SIDES are measured on 358 real streams over 16 distinct ratios -- the largest false is 0.0406 (`333:320`, from the recorded corpus) and the smallest true 0.0667 (`16:15`) -- and 0.05 is the only round number between them. "The ratio rounded to one decimal is not 1.0" fits every witness and nothing measured sits in the gap | A file whose SAR falls between 1.0406 and 1.0667 |
| **The show-unmatch activity's PROGRESS numbers** | The PAIRS are measured -- (S01, 50), (S01, 95), (S02, 95), (S02, 100), the subtitle moving first at the old progress and the progress stepping after -- and `100 - 5 * seasons left` is fitted to the copy's two-season show, the only one it holds | A copy with a three-season show |
| **The `Samsung` prefs row's PURPOSE** | The row is measured verbatim on both references and both library types, and it is `hidden="1"` with an empty label and summary: no settings pane draws it, so what a Samsung client does with it cannot be shown from the server side | A Samsung client, or its bundle |
| **The plug-in framework's non-localisation** | `/system` is the only one of the six plug-in containers that carries a translatable string at all -- the other five title their rows with their own lowercase key names -- so "the framework does not localise" has ONE witness and `noHistory` is a proxy for it | A plug-in container with a sentence in it |
| **The agent `Language` ORDER** | Stored per language rather than modelled, because a real PMS collates by the localised title with ICU and this project does not implement collation. Correct by construction for the 30 captured languages and undefined for any other | Nothing short of ICU |
| **A `Common`'s string `userRating`** | Single witness by CONSTRUCTION: the field appears only once every row in the section shares a value, which neither read-only reference can be made to do -- zen's section 1 holds 3,887 movies. So plexscratch (1.43.4) is the only instrument | A second writable copy |
| **A SCROBBLE writing a history row** | Re-measured 6/6 on the copy this cycle and it overturns cycle 9, so the copy has answered both ways across sixteen cycles. What did NOT change is the timeline half, which writes no row there -- the recorded session-bound limit -- so the pair cannot be checked against each other on one instrument | A claimed writable copy, where a timeline opens a real session |

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

**Ports (cycle 25).** Re-checked after both hunters had stopped and every server this cycle
started was killed. `32502`, `32596` and `8787` are held -- the same three cycles 17-19 ended
with. Everything cycle 25 used is FREE again: `32541`, `32543`, `32546`, `32587`/`32588` (the
hunters'), `32592`/`32593` (the proof driver's). **`32594`/`32595` were used ONCE** by a single
`sdrive` run, and that is worth recording rather than hiding: `sdrive` SIGTERMs whatever listens
on its ports before it starts, `32595` was NOT in the port scan taken at the head of this cycle,
and so a pre-existing listener there would have been killed without a record. It cannot now be
proved either way; every later run went back to `32592`/`32593`. **Neither hunter left a server
running**, as in cycles 18 and 19. `32599` is `test_ui_surface.py`'s own gated server and
`32598` the `--web-root` instance its asset check starts; leave both free.

**Ports (cycle 19).** Re-checked after both hunters had stopped and every server this
cycle started was killed: `32502`, `32596` and `8787` are held -- the same three cycles
17 and 18 ended with. Everything cycle 19 used is FREE again: `32541`, `32543`, `32546`
(the driver's probe server), `32587`/`32588` (the hunters'), and `32592`/`32593` (the
proof driver's). `32599` is `test_ui_surface.py`'s own gated server and `32598` the
`--web-root` instance its asset check starts; leave both free. **Neither hunter left a
server running**, as in cycle 18.

**Ports (cycle 18).** Re-checked after both hunters had stopped and every server this
cycle started was killed: only `32502`, `32596` and `8787` are held -- the same three
cycle 17 ended with, and the same three that were held when cycle 18 began. Everything
cycle 18 used is FREE again: `32541`, `32543`, `32546`, `32587`, `32588` (the hunters'),
`32591` (the driver's probe server), `32592`-`32594` (the proof driver's) and the
parent-comparison server. Neither hunter left a server running this time -- the first
cycle since 15 where that is true -- and the driver killed its own. `32599` is
`test_ui_surface.py`'s own gated server and `32598` the `--web-root` instance its asset
check starts; leave both free.

**Ports.** Re-checked at the end of cycle 17, after both hunters had stopped and every
leftover server was killed: only `32502` (another session's plexsim on
`catalog.example.json`), `32596` (a plexsim of unverified origin) and `8787` (another
session's `http.server`) are held — the same three cycle 16 ended with, and the same three
that were held when cycle 17 began. A server started on a held port silently fails to bind
and every request then goes to somebody else's plexsim, which reads as a wildly failing
suite. Everything cycle 17 used is FREE again: `32511`, `32512`, `32541`, `32543`,
`32587`, `32588` (the two hunters') and `32592`–`32594` (the proof driver's). `32599` is
`test_ui_surface.py`'s own gated server and `32598` is the `--web-root` instance its asset
check starts; leave both free. Check with `lsof -ti tcp:<port> -sTCP:LISTEN` rather than
trusting this list — and check it AFTER the agents have finished, not only at cleanup
(§5 step 12). **Both of this cycle's hunters left their plexsim running**, as cycle 15's
did, and hunt A then restarted its own on `32587` a SECOND time -- after the cycle was
merged and pushed -- detached through a double fork so it reparented to init, which is
cycle 15's incident exactly. Kill the listeners, then list them again, and do it once more
after any late task notification: a finished agent can still act.

`tests/catalog_ui.json` holds movies `1007` Tears of Steel, `1008` and `1009` (fixture copies);
show `20001` The Simpsons; **four** seasons `30001` (S19), `30002` (S20), `30003` (S21) and
`30004` (S22); **eight** episodes `1001` (S19E09), `1002`–`1003` (S20), `1004` (S21) and
`1005`, `1006`, `1010`, `1011` (S22); sections `1` movie and `2` show. Cycle 13 grew it three
times and cycle 14 relied on the extra seasons and episodes, so **read the fixture rather than
this paragraph** before writing a check that counts rows. Multiplicity is a prerequisite, not a
detail: on a one-movie section the Shuffle, Play Next, Merge and second-page checks have nothing
to compare and quietly become no-ops.

---

## 5. The cycle procedure

One cycle, as run for cycles 8–15:

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
12. **Re-check the ports AFTER the hunters have stopped, not only at cleanup.** A finished
    agent can still act: cycle 15's write hunt fired two more task notifications after the
    cycle was merged and pushed, and restarted its plexsim on 32582 both times — the second
    time detached, reparented to init, so it would have outlived the session. Its housekeeping
    was well meant and its measurements were unaffected, but a stray server on a port the
    handover lists as FREE is exactly what §10 says reads as a wildly failing suite. Kill the
    listeners, then list them again.

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

- **Where new checks go.** Right before the cycle-18 library-DELETE block, whose comment
  begins `# Cycle 18, measured on plexscratch with a control write first (a title edit`.
  NOT before the Delete Version check -- that anchor is a thousand lines further down, past
  the row delete AND the section delete, so a block placed there reads a fixture with no
  show library. Cycles 19, 20 and 21 each appended there and each had to relocate.
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
- **Put the destructive-path guard in the HARNESS, not in the brief.** Cycle 22's hunt B
  sent `DELETE /library/sections/{id}` during a verb sweep and deleted the movie library on
  plexscratch and on its own plexsim; cycle 23's brief forbade that path by name and cycle
  23's hunt B did it again, having dropped the guard while rewriting its sweep script. Both
  recovered and both re-measured, but a sentence in a prompt has now failed twice.
- **One writable reference and two agents is a real constraint.** Cycle 23's driver was
  measuring frame sequences on plexscratch while that cycle's hunt B ran the butler on it --
  30 stray `butler` activity frames through the listener. The season and show unmatch
  cascades were deferred rather than taken from a contended socket.
- **Kill the previous cycle's hunters BEFORE the next cycle starts its servers**, and have
  a hunter verify a port's OWNERSHIP rather than that it answers. Cycle 21's hunt B was
  still alive when cycle 22 launched, re-used port 32588 -- by then cycle 22's hunt-B
  baseline -- and wrote a collection, a playlist, five `Field` locks and an artwork
  re-version onto it. It reported the write, the baseline was restarted from its own export
  and the hunter was told what to re-take, but nothing in the tooling would have caught it.
- **A check must not depend on what an EARLIER check left behind.** Cycle 21 found two that
  did: a versioned artwork GET that was a 200 or a 404 depending on which row the loop
  reached first (an earlier check picks an `upload://` candidate, and once it does the real
  remote candidate is gone from `/posters`, so nothing later can put it back -- the suite
  now copies the row's original poster url before any artwork is written), and an
  attribute-order check on an episode an earlier unmatch had stripped the `year` from.
  Prepare the state the check needs, or derive the expectation from what is actually there.

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

### Cycle 12 — the first real library, and the frames the writes really push

Hunters: every row shape a REAL library shows and a five-clip library cannot (opus, read-only on
kids AND zen); the writes cycles 9–11 could not reach plus the frames cycle 11's new writes push
(fable, on plexscratch). **18 findings, 16 implemented** — more than cycle 11, because the
instrument finally matched the question.

What a real library showed: a light row **caps** its tag lists (2 Genre, 2 Writer, 2 Country,
3 Role) where the detail row carries all 155 of a movie's roles; `/hubs/search` caps at 3 rows
per hub with no `limit` and `/library/search` at 10; an actor tag row is `tagType: 6`, not 4; an
episode's `parentGuid` is the SEASON's guid; the section grid says `allowSync: true`;
`group=type|guid|id` are 200; `/common` compares an episode's title and not a season's, and
answers a real sort title; the `actor` axis alone carries no `content`.

What the writes push: a subtitle upload pushes **nothing** (cycle 11 had invented an edit pair);
a refresh's `updated 49` comes before the processing timeline, with the year in its subtitle; a
scan ends inside a nested `provider.subscriptions.process` pair and `?force=1` runs the whole
scan first; Optimize Database runs a `general.db.optimize` activity; a library edit needs an
`agent`, not a `name`, and a create answers a `Location` header; the upload format check is
case-sensitive and takes `psb`; a stream path tells a malformed id from a missing one; a queue
add that adds nothing changes nothing, and another machine's uri adds nothing at all.

**Corrected here:** cycle 11's tag-`count` note (refuted outright — it was never verified before
being written into this file); cycle 11's subtitle-upload frames; cycle 4's library-edit form;
`notify_refreshed`'s frame order, which its own docstring had right all along; and the suite's
tag-axis check, which compared listing rows with listing rows and agreed only because plexsim
sent the whole list.

### Cycle 13 — the rest of the real library, and the cascade family

Hunters: everything about a real library that is not a movie detail row (opus, read-only on kids
AND zen); what a write to a PARENT row does to its children (fable, on plexscratch). **28
findings, 22 implemented**, and for the first time none of the read hunt's were refuted.

The cycle opened with three revalidations from §3.3 and two of them were findings: the light-row
`Director` cap is **3** and `Producer` is not on a light row at all, and unmatching a SEASON
unmatches every episode under it. The third closed the `<CODEC> External` label's single-witness
note — eleven real sidecar subtitles on zen carry the same form.

What a real library showed: a valueless row sorts BELOW every value (and twelve of fifteen sorts
default to `desc`); the search hubs' base order has `movie` fifth and `collection` fourteenth, and
`/hubs/search` returns the ITEMS behind a matched tag while `/library/search` does not; the "All
episodes" row follows the PAGE's row count, not the offset; `duplicate=1` is "more than one Media"
and, on a show or season, "a leaf has more than one"; a bare `<field>=` means three different
things by field, and `contentRating=None` is the empty synonym and the key of the one axis that
has such a bucket; a tag or field axis refuses an unparseable `type=` where a media axis ignores
it entirely; an episode with an `audienceRating` carries `audienceRatingImage` and a `Rating` row;
a show's `/allLeaves` stamps the show's `studio`; a season row of `/library/recentlyAdded` alone
carries `parentSummary`; three leaf-count fields filter a show listing. On the version plexsim
reports: a shelf caps at SIX with `more`, three shelves 1.43.4 does not build, and
`pinnedContentDirectoryID` reshapes the promoted shelf.

What the cascade family showed: an episode unmatch drops the people lists, `audienceRating` and
`year` and empties `summary` by either path; a show unmatch takes its seasons AND episodes, with
every descendant's parent links and borrowed artwork following; an unmatch leaves no activity
running; a season's `index` lands on its episodes' `parentIndex`; an episode carries its show's
`contentRating` and a season carries none.

**Corrected here:** cycle 8's "All episodes at `Start>0`" (a two-season library, not a version
difference — fun was right); the recorded hub cap of 12, which no reference ever answered; the
empty-tag-value rule and the suite check that encoded it; cycle 12's `Director` cap note; and the
sort-null ordering, which had the flag the wrong way round since the sort was written.

---

### Cycle 14 — how a real PMS folds a title, and the carried cascades

Hunters: the rest of a real library — grouping, the people surface, real watch state and
the listing query at scale (opus, read-only on kids AND zen); collections from nothing, an
in-progress leaf, and a re-measurement of cycle 13's six carried cascades (fable, on
plexscratch). **29 findings implemented**, the largest cycle since the build-out, and the
driver's own sweeps produced eight of them.

The cycle opened with two revalidations and both paid. **`titleSort` is not the
article-stripped title**: it is the title TRIMMED and combining-mark FOLDED first, and the
old rule's only witness was fun, out of scope since cycle 11. Re-measured over all 5,294
rows of zen, the model reproduces 5,289; the five misses are Spanish `La` and German `Das`,
which are per-item language and deliberately not implemented. Following the fold into the
matchers gave two more: the word matcher folds (and it is a FOLD, not the fuzzy score —
`Amelie` and `Amélie` both answer 0.93095 where real typos answer 0.33095) while the
substring box and autocomplete do not, and `title=` folds only while the query holds no
space.

**`/filters` and the `Meta` descriptor are per VERSION**: 1.43.4 advertises 28 movie
filters and 17 at `type=4`; 1.42.2 — the build plexsim reports — advertises 22 and 9, and
`section_filters.json`'s own provenance note had said so since it was captured.

What the read hunt found: a `group=`'s surviving row is the group's lowest `addedAt`,
picked BEFORE the sort; a prefixed `group=` is scoped; the `actor` axis is each item's
first THREE roles; nine columns filter and thirteen keys sort that no `Meta` advertises,
while `parentIndex` is a 500 and `leafCount` must be refused; `/status/sessions/history/all`
validates its ids; `group=` reaches the section `allLeaves`; and `#` is the DIGITS bucket of
`firstCharacter`, with every bucket key percent-encoded.

Then the driver swept the surface mechanically — every axis, section leaf, top-level path
and item leaf, from both references and from plexsim, reporting only where the references
AGREE and plexsim differs. 59 top-level paths and 48 item leaves agreed; five did not.
`/userRating` is a real axis with a fixed six-bucket scale; nine axes of other library types
answer an empty container rather than a 404, and the two families disagree about `type=`;
`/banners` and `/thumbs` are real candidate lists; and **`/recentlyReleased`,
`/composite` and `/background` are endpoints plexsim invented** — 404 on both references,
named in no findings document and no suite.

The write side re-measured cycle 13's six cascades and found **the programme's one
data-loss bug**: a SEASON merge is a 200 that changes nothing on a real PMS, while
plexsim's cascading delete destroyed the absorbed season AND its episodes. Beside it: On
Deck is the most recently viewed in-progress episode (cycle 9 measured the opposite on
ten-second clips); an artwork pick re-versions three fields, locks one and cascades; a hub
container says `allowSync: true` everywhere but postplay; a season listing's leaf-count
filters read the SHOW's counts; and collections were measured for the first time, since
neither reference holds one.

**Corrected here:** the `titleSort` derivation; the advertised filter lists on both
fixtures; the `group=` survivor, which the code's own docstring had called unknowable; the
`actor` axis; cycle 9's On Deck rule; cycle 9's prefs cascade, which belongs to
`showOrdering` alone; the `allowSync` default and `item_hubs`'s docstring; and cycle 4's
merge, which was losing data.

**And a correction the suite made to the driver.** The write hunt reported that a show's
prefs write pushes NOTHING; that was implemented, and the suite's own cycle-13 check —
which asserts the cascade for `showOrdering` — failed. Re-measuring one setting at a time
showed the report was right about the four settings it tried and wrong as a
generalisation: `showOrdering` really does push 45 frames. The fix had swung from "cascade
on everything" to "cascade on nothing", and a standing check caught it. That is the
clearest argument in the programme's history for never deleting a check.

---

### Cycle 16 — the HTTP envelope, the writes below the collection, and the first real show library

Hunters: the write surface one level below the collection (opus, on plexscratch --
playlists, the play queue, the section save on a SHOW library, item prefs); the HTTP
envelope, swept by request header, by response header and by XML shape (fable, read-only
on kids and zen). The driver took the `Server:` banner §3.3 had carried and then swept
**zen's section 2**, which nobody had pointed anything at: 507 shows, 741 seasons, 9,287
episodes — the first real SHOW library the programme has had. **49 findings implemented**,
the largest cycle in its history, and 1,030 checks in the suites.

The new instrument paid immediately. Three of the driver's six findings are things no
five-clip library can show: the `firstCharacter` axis counts a leaf by its SHOW's sort
title (28 of 28 buckets at `type=3` and again at `type=4`; bucketing by the rows' own
titles reproduces 0 of 28) while the LISTING behind each bucket filters on the row's own,
so a season list's `A` bucket says 64 and answers no rows — the reference disagreeing with
itself, on both servers. A sweep of the same family found that **every** section leaf's
extra path segment is a bucket or a 404 and never the leaf again, which plexsim answered
200 to about twenty times. And a show library's typed grid defaults to BROADCAST order,
which findings.md had asserted for the section listing since cycle 8 and which only
`/allLeaves` ever implemented — the canonical class C.

The envelope sweep produced the cycle's largest single piece of work: **a real PMS
localises its own text**, in 28 languages, keyed by `X-Plex-Language` over
`Accept-Language`. Rather than write translations, the driver captured them — each
endpoint fetched in English and in each language, the two documents diffed string by
string at the same structural path — which is also what showed the rule is per ATTRIBUTE
and not per string: "By Genre" is translated as a section pivot's `title` and left alone
as an axis's `title2`. Beside it: a real PMS ROUTES before it authenticates (an unknown
path is a 404 with no token; eleven single-segment paths are 401 and every other one is a
403, measured path by path on both servers), `Range` applies to every response body, a
malformed header is a 400, the Accept `q` grammar ranks a zero rather than dropping it,
and the `Server:` banner is gone.

The write hunt found the playlist surface was barely a surface: its items are a typed
QUERY with their own `Meta`, `/playlists` filters and orders smart-first,
`PUT /playlists/{id}` takes the whole item edit grammar, `playlistItemID` is server-wide,
a smart list's items carry none, and the artwork paths are on the other endpoint from the
one plexsim served. On the queue: shuffle was a deterministic reverse, every item delete
answers the pre-mutation container, and `includeBefore`/`includeAfter` window it with no
`window=` at all — cycle 9 measured those two only ever WITH one, which is class A in the
original.

**Corrected here:** cycle 8's section-listing order (implemented for one half only); cycle
5's Accept grammar in six places; cycle 9's queue window and its "only the selected
delete" rule; cycle 15's episode-only `/art` 404, which is the special case of the
ownership rule; and the belief that the token gate runs before routing.

**And a defect this cycle introduced and caught itself.** The first implementation of
"`smart=1` over a key list is a 400" refused the create whenever the uri resolved to any
rows — and a listing uri resolves to rows too, so every smart playlist create became a
400. The next patch's own check block failed on the parent three commits later and
surfaced it. The test is the uri's SHAPE, not whether it resolves: a rule implemented as a
proxy for itself is exactly what §3.1 class C describes.

**Two housekeeping deviations, recorded rather than hidden.** `docs/findings.md` landed
inside the `test: the movie prefs check follows the unmatch rule` commit and `CLAUDE.md`
plus `hunt_prompts.md` inside `fix: a selected-item delete answers the selection as it
stood` — a `git add -A` swept them in while a code fix was being committed. The content is
right and complete; the history is not as atomic as section 5 asks. Rewriting it would
have meant rebasing fifty commits whose whole value is that each was proved against its
own parent, so it was left alone. **Stage by path when a doc edit is open.**

**One incident, and it cost nothing.** The scratch disk filled mid-cycle and the working
directory was cleared to recover it, taking the worktree with it. Every commit survived,
because a worktree is only a checkout — the objects and the branch ref live in the main
repository. The branch was pushed to `origin` immediately afterwards, which §5 says to do
as you go and which this cycle had not been doing. **Do that from the first commit.**

### Cycle 17 — the row itself, the write surface below the playlist, and three fixtures captured off the wrong version

Hunters: the ROW, swept type by type and container by container (opus, read-only on kids
and zen), and the write surface one level below the playlist -- the collection ITEM path,
the section prefs family, the butler's individual tasks and the item writes nobody had
reached (opus, on plexscratch). **29 findings implemented** over 32 commits, and 1,057
checks in the suites.

The driver's own round was the surprise. Cycle 16's localisation finding left an obvious
next question -- how wide is the captured table? -- and answering it (87 paths, 119
missing pairs) turned up the family the cycle is really about: **three fixtures were
captured off kids and plexsim reports 1.42.2**. Cycle 14 split `Meta.Type[].Filter` per
version and left the `Field` list beside it at 1.43.4's, so plexsim offered the smart
filter builder six media-codec rules a 1.42.2 server does not have -- and on 1.42.2 those
six are UNKNOWN fields, which answer EVERY row rather than none. `system_agents.json` had
the same shape: two `tv.plex.agents.nfo.*` rows that exist only on 1.43.4. **When a fixture
disagrees with zen, suspect the version before the shape.**

The row sweep produced the cycle's most mechanical piece of work and its most surprising
one. Mechanical: a ROW's XML attribute order, 25 element buckets derived as a topological
sort of every pair both references emit, 634 inverted pairs closed and 0 left -- invisible
to every real client, and fixed because the corpus replay compares presence and values but
not order. Surprising: **`titleSort` strips a leading run of PUNCTUATION**, and the set is
not a Unicode category -- eight ASCII characters and then two whole ranges, with the CJK
and fullwidth brackets deliberately outside. One row of zen witnesses it naturally, and its
own `firstCharacter` axis corroborates: 27 buckets, no apostrophe one, and `'Til Death Do
Us Art` in `T`.

The write hunt's own lead finding is the one that matters most to a user: **a save a real
PMS refuses writes nothing**, where plexsim applied each parameter as it validated it. The
client is told the save failed, keeps its old model, and the row on disk has moved. Beside
it, the same path's 400 turned out to mean "this request edits nothing" rather than "an
unknown field" -- the recorded rule was measured with the unknown name alone, which is
class A -- so plexsim refused 34 of 71 option names a client might append to its save.

**Corrected here:** cycle 14's per-version fixture split (one of three); cycle 16's
`DELETE .../{poster,art,thumb}` (two of three names implemented); cycle 15's artwork-pick
`updatedAt` (the upload was left out); the recorded "a bad int on a library pref is
ignored"; the recorded activity subtitle form; and hunt A's own reading of the `/allLeaves`
artwork stamp, which is a property of that container and not of an episode row -- zen's
section grid disagrees with the row reading 641 times.

**Refuted:** `PUT /library/metadata/{id}/poster?url=nonsense` being a 404 on plexsim. Both
sides answer 200 and re-version, twice in a row with a distinct stamp each time.

**And a defect this cycle introduced and caught.** The first implementation of "the
collection item paths 404 the verbs they do not implement" encoded `/items` as PUT-only --
but the verbs measured on the reference were POST, DELETE, PATCH and OPTIONS, and GET was
never among them. `GET /library/collections/{id}/items` is the member LIST, and the table
took it out; the next finding's own measurement showed a three-member collection answering
an empty list. **A rule is scoped to what was TRIED, and a table written from a refusal
list has to say which verbs were on it.**

**One process note.** Two of this cycle's own checks passed in isolation and failed at the
END of the suite, which by then has re-picked every artwork version, emptied every tag list
and merged the spare movie away -- the trap §6 names, hit twice in one cycle. The
`test:` commit that fixed them also made the Shuffle check non-flaky: cycle 16 made the
shuffle real, and a real shuffle of three rows matches the plain order one time in six.

### Cycle 18 -- the container, the write surface below the collection item, and the title a server generates

Hunters: the CONTAINER, endpoint by endpoint against both references (opus, read-only on
kids and zen), and the write surface one level below the collection ITEM path -- the
playlist item verbs, the section prefs family per TYPE and the `/actions/` family (opus,
on plexscratch). **26 findings implemented** over 28 commits, and 1,186 checks in
`test_ui_surface.py`.

The cycle's own family came from the driver's round. Cycle 17 settled what `titleSort`
IS; holding that model out against zen's 14,433 real rows found the half nobody had
asked: **a title the SERVER generated is stored nowhere at all**. It has an empty sort
title (740 of zen's 741 seasons answer a bare `titleSort=`), it is in no search index
(the copy's `Season 2` answers nothing to `query=season` while the same season renamed
answers at once) and the `title` filter cannot see it either (`type=3&title=Season` is 0
of 741). That one rule REFUTED territory A's reading that a section's search box never
returns seasons -- every season on both references is titled `Season N`, so the sample
could not tell "seasons" from "generated titles" -- and it explains the other half of
cycle 16's `firstCharacter` split, whose listing side plexsim had implemented by reading
the row's title.

Beside it, two more corrections to rules this project had already written down: the fold
is **eleven characters wider** than NFD (`Æ→E`, `Ø→0`, `ð→O`, swept character by
character), and an unlisted string field does not merely TAKE `>>`/`<<`, it COMPARES on
them -- plexsim allowed the operator and fell through to containment, so `titleSort<<=0`
matched every title holding a `0`.

Territory A's own lead was the `/search` container, typed by the ASKED `type=` rather
than by the library: at `type=3` a real PMS answers `viewMode` 65593 and plexsim answered
the EPISODE layout for a season list, which is the Seasons bug's exact inversion, on the
path the library search menu takes. Territory B's was the plug-in dispatcher: four more
single-segment paths are its POPULATED listings, and a KNOWN single-segment path answers
it for every verb it does not implement -- `PUT /identity` and `PUT /hubs` included,
which overturns the recorded "they answer any verb".

**Corrected here:** cycle 9's "a `noHistory` container ignores paging" (`/system` pages,
and only `/system/scanners/{N}` does not); cycle 15's `parentIndex` refusal (implemented
on the section listing alone); cycle 5's `/identity` and `/hubs` verb rule; the
"never 405" rule (the `/system` subtree does); and the fourth fixture found to be per
VERSION, `section_prefs_new.json`.

**Refuted:** hunt A's reading that `/library/sections/{id}/search?type=3` never answers
seasons. A season whose title somebody typed answers at once.

**And a defect this cycle introduced and caught.** The first implementation of the
fifteen hub refusals called `hub_refusal(query)` from inside `section_hubs`, which never
receives the query, so every show library's Recommended tab answered a `NameError`'s 500
-- and the patch's own check PASSED, because it asserted a 500 and a crash is also a 500.
The full suite caught it four checks later on the tab's contents. **A check that asserts
an error status has to read something back afterwards.**

**One process note.** Three of this cycle's checks passed in isolation and failed at the
END of the suite, which by then has merged the spare movie away, edited every title and
given a season a sort title of its own. The `test:` commit that fixed them computes each
expectation from the rows as they stand rather than from the fixture the suite started
with -- the trap 6 names, hit three times in one cycle.

### Cycle 19 -- the child element, the filter engine held out against a real library, and the write's own payload

Hunters: the CHILD ELEMENT -- every element between the container and the row, per
endpoint and per row type, against both references (opus, read-only on kids and zen),
and the write surface's own payload -- the notification FRAME, the `Activity` element
and the server-level writers (opus, on plexscratch). The driver's round ran plexsim's
OWN engines over a real library: `ssweep.py` (new, tracked in `handover/tools/`) holds
the `sort=` engine out against zen's ordering, and cycle 18's `fsweep.py` was pointed at
a SHOW library for the first time.

The cycle's own family came from that second run. Cycle 18 found the
`title=`/`originalTitle` rule by evaluating `filters.compare` over zen's 3,887 movie
rows and diffing the SETS; the same tool over its 507 shows disagreed **74 times**, and
behind those disagreements were four rules about the filter grammar that eighteen cycles
of endpoint-by-endpoint measurement had never reached -- because each one needs a library
where most rows have NO value for the column being filtered, and neither five-clip
reference has one. A boolean answers the true set to exactly two requests (`=1` and
`!=0`); a null numeric column matches NOTHING, `!=` included; `==`/`!==` on a numeric are
an exact STRING comparison of the stored number; a `tag`-typed COLUMN takes all eight
operators where a tag-TABLE field takes two, and `resolution` takes none of them -- every
operator there answers the `=` set.

Territory A's own lead was the one with a visible cost: **a row that has children names
its child list**, and three of the four seasons cycle 13 added to `tests/catalog_ui.json`
by hand carry the bare item path instead. The client follows `key` verbatim, so a click
on Season 20, 21 or 22 opened the season's metadata row rather than its episodes and the
page drew nothing -- for six cycles, under a suite that passed. Beside it: the section
`Meta`'s `active` flag follows the REQUEST rather than the library, `?includeDetails=1`
honours `includeAdvanced=1` and carries no `folder` row, `excludeFields=` reaches every
CHILD ELEMENT, `excludeElements=Image` and `=UltraBlurColors` are no-ops on both
references, `checkFiles=` is validated, `/library/metadata/{id}/extras/all` is real, and
five more XML attribute-order buckets plus a LIGHT row's own child order.

**And a rule the project had only half-built.** Cycles 16 to 19 ordered the XML --
container attributes, row attributes, child elements -- and left the JSON in whatever
order the dict that built it held. A real PMS's JSON key order IS its XML order, on both
references and all four row types. `xmlout._split` is now the one answer both writers
ask, which is also what made the light-row order reach JSON for free.

Territory B's own family was the write's PAYLOAD, and it corrected two rules this project
had already written down. Cycle 9's "a library edit applies the whole form and scans" was
measured with a form that carried a `language` -- the half that scans; `agent` alone,
`agent` with `name` and `agent` with a `prefs[...]` push ZERO frames. And cycle 17's
four-phase `CleanOldBundles` is seven: the analysing phase it ended on is followed by two
`Deleting N metadata bundles` steps. Beside them, `?path=` really scopes a scan, three
butler tasks push the wrong activity, `provider.subscriptions.process` is the one
cancellable one, an analyze pushes no `backgroundProcessingQueue` frame, `PUT
/updater/check` pushes one of its own, `DELETE /devices/{id}` and `/accounts/{id}` are
404s, `PUT /activities/{x}` is a 400, `HEAD /:/prefs` is a 200, four settings answer 403,
two integer settings take a word, `server_prefs.json` is the FIFTH per-version fixture,
and the Add Library form validates four ways plexsim did not.

**And a trap that cost the cycle a whole round of measurement.** `filters=` on a
notification channel is a WHITELIST of event types and `all` is not one of them, so a
socket opened with `filters=all` receives NOTHING -- indistinguishable from a server that
pushes none. The control write is what caught it, exactly as HANDOVER 10 says it should.

**Refuted:** hunt A's reading that the `Agent` element's attribute order is inverted. The
only pairs both references constrain are `hasPrefs < identifier < primary`, which plexsim
already satisfies; the `id` kids puts first is 1.43.4-only and plexsim reports 1.42.2.

**Revalidated and still standing** (§3.4 step 1): the non-alphanumeric `firstCharacter`
bucket, re-read across all twenty library x type combinations both references hold and
still resting on the single `『` row of zen's section 1; `/banners` empty and `/thumbs`
from the media bundle on six rows of both servers; the `/allLeaves` borrowed-artwork
stamp, which kids reproduces exactly (`grandparentThumb` at the LEAF's `addedAt` on the
list, at the show's current version on the detail read) and zen cannot witness because
its show library carries no artwork at all; `/squareArts` as 1.43.4-only (kids 200, zen
404); and the "All episodes" row following the PAGE's row count, with `size` counting it
and `totalSize` not.

### Cycle 20 -- the season and the episode row, the edit grammar by column, and the engines again

Hunters: the show library at **type 3 and type 4** -- zen's 741 real seasons and 9,287
real episodes, the rows carrying the most derived fields on the surface and the ones
cycle 19's holdout never reached (opus, read-only on kids and zen) -- and the **edit
grammar swept by COLUMN**, every column of every row type on both write paths (opus, on
plexscratch). The driver's round ran plexsim's remaining engines over a real library:
`hsweep.py` (new) holds `hierarchy.py`'s derivations out against 10,535 rows and
`psweep.py`/`psweep3.py` (new) hold `filters.parse` out against both references'
`/query/parse`, field by field and operator by operator.

The driver's round found eight of the cycle's findings before either hunter reported, and
two of them are the kind only a holdout produces. **A season's `titleSort` follows the
title it STORES, not its `year`** -- cycle 7 fitted that rule to kids' season 7, the only
season with a year a five-clip lab has, and zen's one year-carrying season carries
`Egghead` rather than its displayed `Season 22`. **An episode carries `parentThumb` only
when the season's poster is the season's own**: 654 seasons hand it down, 27 that borrow
the show's hand down nothing, 0 exceptions over 9,293 episodes on both references, and
`art` is the opposite. Beside them the parse surface turned out to be wider than the
filter surface in three more ways (`theme`/`banner`/`art`/`thumb`/`slug` parse and 500 a
listing; `type<op>` is a real numeric column; `sourceType` parses an integer and adds the
default Order block), `guid` takes only two operator keys and reports both as `<=`, and
`group=` takes five more keys -- the same unadvertised columns that FILTER.

Territory A's own family was the one cycle 19 predicted: nearly every rule the show rows
gave has a different answer at type 3 and type 4. `Specials` and `[Unknown Season]` are
generated titles too; `viewCount` and `viewOffset` read a missing value as below every
value and equal to 0 where `skipCount` does not; the stream-language operators are
universally quantified negations (the question cycle 19 left open, now fitted and held out
over 14,400 points); those two columns do NOT read the leaves where the other media
columns do; a show library's media AXES do; `file=` is a 500 on a show listing;
`unmatched` ignores its operator; `duplicate`'s two halves OVERLAP on a parent; a NULL
date matches nothing; only `titleSort` folds case; `id==` compares a number; `mediaCount`
and `createdAt` were columns that could never select anything; a search row carries no
librarySection triple; and four small leaves refuse every unparseable `type=`.

Territory B swept the edit by column and found that **an edit does not move `updatedAt`**
-- and that the artwork version IS that column, on 738 of 738 owned paths across both
references. Beside it: eleven more columns take an item edit and a lock, the SECTION path
parses every numeric column where the item path parses three, `year` is bounded to signed
32-bit (the last hole in cycle 19's whole-form refusal rule), a negative `rating` removes
the field, `theme` and `banner` are artwork columns, an episode's inherited
`contentRating` is a read-time fallback, and a collection and a playlist take the item
path's whole validation.

**Refuted:** hunt A's reading that `addedAt!==<date>` differs. Re-measured, zen, kids and
plexsim all answer 500.

**Revalidated and still standing** (3.4 step 1): the `allowSync`-on-the-ROW signal, swept
over 28 paths on both references -- only `/library/recentlyAdded` (every `type=`) and
`/library/onDeck` carry it on a Metadata row, and `/library/sections`' Directory rows are a
different element; and the non-English article strip, which zen's `La La Land`,
`La Dolce Villa` and `Das Deutsche Volk` still witness and which stays unimplemented.

### Cycle 21 -- the detail read of a season and an episode, the write surface below the item, and the XML writers held out

Hunters: the **detail read** of a season and an episode and the **hub shelves** those two
row types appear on -- the dimension cycle 20 left when it swept the listing grammar at
type 3 and type 4 (opus, read-only on kids and zen) -- and the **write surface below the
item**: the `/library/sections/{id}/prefs` family per library type, the per-item prefs, the
`/:/` write family swept as a SET, and the writers this project has never sent (opus, on
plexscratch). The driver's round added `xsweep.py`, which compares every XML attribute and
child-element sequence both references agree on with plexsim's own, per path.

Territory A's family was the row's own shape rather than its query grammar. **The two forms
of `/system/agents` carry different children** -- the bare form a `MediaType` per agent and
no `Language`, `?mediaType=N` the other way round -- and **that endpoint writes its repeated
children as repeated JSON KEYS** rather than arrays, 150 occurrences on zen and 195 on kids,
so a JSON parser keeps exactly one per agent while the XML carries all of them. It is the
only path on the surface that does. Beside it: a `Feature` element is never self-closed
where `Directory`, `Action` and `Pivot` in the same document are; a SEASON's language
settings inherit from the SERIES (`:Series default`, not `:Account default`); an EPISODE's
`/tree` is the ancestor CHAIN of exactly three nodes; `/hubs/home/recentlyAdded` is
populated at `type=1` and `type=2` only; a `/library/onDeck` row leads with `allowSync` and
the librarySection triple as a `/library/recentlyAdded` row does; a section's
`continueWatching/items` container is three keys and nothing else; an artwork path with an
EXTRA segment after the version is a 404; and **the per-user-state block sits MID-ROW,
before the year, on every row type** -- which moved both formats, because cycle 19
established that a real PMS's JSON key order is its XML order.

Territory B took the write surface as a set and found the `/:/` family was measured on one
key shape for its whole life. **A state write's `key=` is a BARE ratingKey**: the path form
the client builds for `/:/timeline` is a 404 on scrobble and unscrobble and a 500 on rate
and progress, and plexsim split on `/` and accepted the shape a real PMS refuses. The four
then diverge -- rate and progress int-parse the key, scrobble and unscrobble look it up,
`/:/progress` accepts ANY integer key with a 200 that stores nothing, and the two that
validate a parameter of their own do it on opposite sides of the identifier check. Beside
it, `/:/progress?time=` takes a negative value and a leading `+`.

The larger half of B was the two LIST row types. **A collection and a playlist take every
`/:/` state write**, storing `userRating`, `viewOffset` and the rest exactly as an item
does, and they part only on the scrobble: a playlist counts up from the 1 it is born with,
a collection never carries `viewCount` at all and its scrobble changes nothing. The state
comes back MID-ROW in its own order, on every read but `/playlists/{id}` and the create
response -- which is the precise shape of cycle 16's "the pair is on the LISTING row only".
**They answer the ITEM action leaves** too, splitting by row type rather than by leaf, and
**`PUT /library/collections/{id}` is the ITEM edit path**, which plexsim had as a second,
weaker code path answering 200 to every form while the metadata path beside it was already
right. And `POST`, `PATCH` and a bare `OPTIONS` are 404s on all of them -- cycle 19's
read-only-verb rule, which had never reached these.

Three more came out of sweeping rather than reading: **twenty-nine more columns take an
item edit and a real lock** (eleven names are a 400 on both, so the boundary is measured);
**a SHOW library's `/analyze` frames every episode LEAF three times**, because the section
analyze reads the leaves and plexsim framed only rows typed `movie`; and **a TEXT library
preference is percent-decoded a SECOND time**, which is all cycle 17's "a value carrying
`%` is stored EMPTY" ever was. Two `updatedAt` rules moved in opposite directions: a
`DELETE .../indexes` moves the SECTION's, and a `/refresh` moves the ROW's on plexsim and
on no reference -- so plexsim re-versioned the artwork of every row anyone refreshed.
Finally, an EMPTY response under `/system` carries no `Content-Type` at all, on every verb.

**Held out on version:** five of the twenty-nine lockable names (`atmos`, `audioCodec`,
`audioLayout`, `subtitleCodec`, `videoCodec`) are the media-codec family cycle 20
established is 1.43.4-only. plexscratch is 1.43.4 and zen cannot be written, so 1.42.2 --
the build plexsim reports -- is unmeasurable for them; the other twenty-four are
implemented and the five are in §3.3.

**Refuted during re-measurement:** the `backgroundProcessingQueue queueRegenerated` frame
that appeared after the first show-library analyze. Three runs with an idle-socket control
between them pushed 18 frames and nothing else; the queue frame was left over from the
collection and playlist creates that set the probe up.

**Revalidated and still standing** (3.4 step 1): the artwork-ownership rule, which cycle
21's own extra-segment finding sits on top of; and `/hubs/home/recentlyAdded` keying on
`type`, re-measured across seven values on both references.

### Cycle 22 -- the movie detail read, the writers at scale, and the frames a write really pushes

Hunters: the **MOVIE** -- the row type nobody had swept since cycle 13, whose instrument
then was a five-clip lab -- together with the `include*` matrix and the XML and JSON
writers at scale (opus, read-only on kids and zen); and the **frames a write really
pushes**, the dimension twenty-one cycles of measuring what a write STORES had left
thinnest, plus `mediainfo.py` held out against zen's 13,643 real Media (opus, on
plexscratch). The driver re-ran every engine holdout against the cycle's own HEAD.

The driver's round found three of its own. **A text `sort=` compares RAW CODEPOINTS** --
`ssweep` reported 58 of 66 keys disagreeing, fifty of them ICU-collation ties that §8
records as deliberately unimplemented and eight of them one rule: lowercase sorts ABOVE
uppercase on `title`, `studio`, `summary`, `tagline` and `originalTitle`, which is the sort
side of the split cycle 20 measured on the filter side. **A filter key whose NAME keeps a
comparison character is an unknown FIELD**, not the 400 `split_key` raised. **And a bare
`type=` on the item edit path is an accepted no-op**, which cycle 21's hunt B reported at
the very end and cycle 21 did not implement.

**The driver's other finding was wrong, and how it was wrong is worth keeping.** `fsweep`
reported 57 disagreements on the tag-table fields and the first reading was "a tag-TABLE
field under an operator outside `=`/`!=` is a 400 on plexsim and every row on a reference".
The client puts the operator characters in the parameter NAME -- `year>>=2000` is the
parameter `year>>` -- so a probe sending `collection%3D%3D=zzzz` is sending the parameter
`collection==` and not the `==` operator at all. Re-sent in the client's own encoding,
plexsim already agreed on all eight operators for every tag-table field. What survived is
the narrower rule above. That is class F caught by the project's own rule, one step before
it became a commit.

Territory A's family was the option matrix and the writers. **`excludeElements=` honours an
ALLOW-LIST of ten names** and cycle 19's "every other element really goes" was a two-name
sample generalised the wrong way -- so plexsim removed `Extras`, `Related` and
`Preferences`, blocks a real PMS still sends; **`Part` is on that list** and takes the
Streams with it; **and the option reaches the rows INSIDE the inline Related and Extras
blocks**, which is the one request whose whole purpose is a smaller response. Beside them:
five more options carry `checkFiles`'s value grammar, four `include*` options parse their
value as an integer where `includePreferences` compares the string, and
`includeRelatedCount=0` empties the hub rather than meaning "absent".

The writers gave four more. A real PMS writes **one element per LINE** and ends the document
with a newline (236 in one zen movie, 34 on kids); **six more element names are written
PAIRED when empty**; **three attribute orders** that a five-clip library can never emit --
`Part` leads `accessible, exists`, a pivot `Directory` leads `secondary`, a `Feature` leads
`flavor` -- each of them the documented failure mode of a topological sort over pairs
nobody observed; and **a LIGHT row's JSON child order was never the light order**, because
`json_body` stripped the `_light` marker before the orderer read it.

Territory B's write-frame sweep was the largest single measurement of the cycle and most of
it is NOT implemented, deliberately. **An artwork frame carries the LIST row's own type** --
a collection's 18, a playlist's 15 -- and plexsim sent `type 1` for both, which matters
because the client builds the URL it updates from that number; that one is fixed, along
with the collection action leaves' empty body, `/resources/{anything}`, `/search/voice`,
`/search`'s writers and the `PATCH` table. What is **held out in §3.3** is the frame
SEQUENCES those writes push: a collection's refresh is 3 frames on an agent guid and 8 on a
`local://` one, its unmatch 7, a playlist's 4 and 2, and they carry `state 2` with
`metadataState: "matching"` plus a `state 3 matching` on a movie and show unmatch -- two
values plexsim emits nowhere -- while plexsim pushes ZERO frames for a movie or show
unmatch. Implementing a sequence is cheap; implementing the state machine behind two new
`metadataState` values without measuring where else they appear is not, and cycle 22 ran
out of cycle before it could.

`mediainfo` held out against a real library produced five more, four of them held out too.
**The language-part rule is the one worth reading**: hunt B fitted "the whole name, not the
first word" to 13 of zen's subtitle rows, and implemented it breaks the RECORDED corpus --
`Русский (Full) - Кубик в Кубе` drops its language on a real PMS and only the first-word
rule gets that right. Five witnesses across the two corpora contradict both pure rules, and
the narrowest model that fits them all is fitted to two positive witnesses. Not implemented;
the table is in §3.3. The `videoResolution` 480 bucket, `eia_608`'s `Closed Captions` label
and three missing `languages.json` codes are there beside it.

**Revalidated and still standing** (3.4 step 1): the non-alphanumeric `firstCharacter`
bucket (zen still witnesses `『` and nothing else beside `#`), `/banners` and `/thumbs`
answering 200 with `banners` empty on both references, and the `allowSync`-on-the-ROW
signal, re-swept over three paths on both.

**An incident worth recording.** Cycle 21's hunt-B agent was still alive when cycle 22
launched and re-used port 32588 -- which had been handed to cycle 22's hunt B -- for a
verification run, creating a collection and a playlist, locking five `Field` names on a
movie and re-versioning another's artwork on the new cycle's baseline. It reported the
write itself. The baseline was restarted from its own export before anything was measured
against it, and cycle 22's hunt B was told which measurements to re-take. **Kill a cycle's
hunters before the next cycle starts its servers**, and have a hunter verify port OWNERSHIP
and not merely that the port answers.

### Cycle 23 -- the child-element order held out, the frames cycle 22 measured, and a cycle-22 entry refuted

Hunters: the **child-element order at scale** and the containers no cycle had swept (opus,
read-only on kids and zen); and the **butler end to end** with `mediainfo` finished against
zen's real library (opus, on plexscratch). The driver's round implemented the two largest
items cycle 22 had measured and deliberately held out.

**Territory A's first part found nothing, and that is the point of running it.** 2,994
paths harvested across the three servers, child sequences in 31 element buckets per
reference, **137 ordered pairs witnessed by both references and 66 by one -- and not one of
the 203 contradicts `xmlout._CHILD_ORDER`**. No reference sequence in any bucket is
impossible under it. zen's rows exercised `Chapter`, `Marker`, `Review`,
`CommonSenseMedia`, `Producer`, `Extras`, `Related`, `Preferences`, `OnDeck`, `Children`,
`Location`, 155-role and 5-Media rows. A table built on a five-clip lab held against a real
library, and cycle 19's JSON-order rule held over 1,241 reference path/format pairs. That is
a dimension this programme can now stop sweeping.

What territory A did find was in the rows, not their children. **A `/search` row is the GRID
row with a six-attribute lead** where plexsim answered the detail row -- several times the
bytes per hit on a real library. **A `Meta/Type`'s active SORT follows the asked type AND
the `sort=` parameter**, so plexsim's sort menu said "Title" on a Seasons grid whose rows
were in broadcast order. **`?includeDetails=1&includeAdvanced=1` carries no Duplicates
Filter row at all** -- cycle 16 measured that path without the second option, where the row
does not exist to carry a flag, so the rule it recorded was the absence of the FLAG.
Then three orders (`/library/tags`'s trailing `thumb`, `/manage`'s `identifier` lead and
self-close, `Common`'s grandparent lead) and **the last path where plexsim's own two writers
contradicted each other**: `_send_container` pops the `_xml=` override before either writer
runs, so `/library/sections/{id}/common` bucketed its row as `Video[common]` and left its
JSON keys in build order.

The driver's round took the two biggest items off cycle 22's held-out list, both
re-measured before a line was written. **A MOVIE's unmatch pushes twelve frames** and
plexsim pushed none, opening on `2/matching` then `3/matching` -- a pair plexsim emitted
nowhere -- with an activity that steps through a second TITLE, `Analyzing Media`. **A
collection and a playlist answer `/refresh` and `/unmatch`** with their own sequences, both
previously 404s, and a collection's refresh is **gated on its guid**: three frames on an
agent guid and eight once an unmatch has given it a `local://` one.

**Territory B refuted a cycle-22 entry, which is the most useful thing it did.** §3.3
recorded "`DELETE /butler/{Task}` is a 200 that cancels a queued task". Re-measured with a
control proving the task would otherwise run, it is the **404 that cancels and the 200 that
does not**: a DELETE inside the queue window answers 200 and the task still runs, and after
it a 404 and the task does not. The driver reproduced the status grammar itself. Beside it,
`POST /butler/{Task}` QUEUES rather than runs, `general.db.backup` runs twice and that
doubling is peculiar to `BackupDatabase` (which answers cycle 22's open question),
`CleanOldBundles` interleaves its two messages between the phases with independent counts,
`RefreshLibraries` reports before it scans, and `/activities` retains the finished wrapper.
None of that is implemented: plexsim runs every butler task synchronously inside the POST,
and the whole family needs a queue notion it does not have. It is in §3.3 as one block.

`mediainfo` held out against 105,558 real streams produced a MODEL rather than a list of
fixes, and it settles the contradiction cycle 22 could not: the language part's rule is the
first-word test **with a trailing parenthesised group stripped from the name**, which fits
zen's 179 counter-examples and the recorded corpus at once. With that and five more measured
stand-ins the miss rate falls from 613/105,558 and 3/642 to 174/105,558 and **0/642**. It is
not implemented this cycle -- it is one coherent change to `stream_titles` and it belongs in
one commit with the suite run behind it, which cycle 23 ran out of room for. §3.3 carries it
with the model file named.

**Two hunter claims were corrected on re-measurement**, both in the driver's favour: the
Duplicates finding was reported as "keeps `advanced="1"`" and is really "carries no such row
at all", and the `Meta` active-Sort finding was reported as a presence rule and is really a
presence rule AND a `sort=`-following rule. Neither would have been caught without re-measuring.

**An incident, and it is the second in three cycles.** This cycle's hunt B sent
`DELETE /library/sections/1` during its verb sweep -- the one path its brief explicitly
forbade, because cycle 22's hunt B had done exactly the same thing -- and deleted the movie
library on plexscratch and on its own plexsim. It recovered both, re-measured everything
taken in that window, and discarded two candidate findings that were artefacts of the
damage. kids and zen were never touched. **The guard belongs in the harness, not in the
brief**: two cycles running have now proved that a sentence in a prompt does not stop it.

### Cycle 24 -- the backlog cycle: `mediainfo` finished against a real library

**No hunts, deliberately, and that is the first thing to know about this cycle.** Cycles 21,
22 and 23 each closed carrying measured-but-unimplemented findings, and by the end of cycle
23 that backlog was the largest this programme had held -- fifteen §3.3 entries, several of
them fully measured and simply waiting for room. Launching two more hunters would have added
to it rather than drawn it down. So cycle 24 ran the driver alone, against the corpora the
hunts had already captured, and **re-derived every rule from those corpora rather than from
the report that proposed it**. Cycle 25 is due and must run hunts.

The one that mattered is the extended title's **LANGUAGE part, settled at the third
attempt**. Cycle 22 fitted "the whole name, not the first word" to zen and it broke the
recorded corpus, so it was held out; cycle 23's hunt proposed a parenthesis strip; cycle 24
scored both models on both corpora and the rule is: **a single token that is one of the
title's words, with any trailing parenthesised group stripped from the name first**. That
explains `Русский (Full) - Кубик в Кубе` dropping its language and `Tiếng Việt` keeping its
own name at once, which neither earlier rule could. Beside it, five stand-ins (`Signs`/
`Songs`, `CC`/`HI`, `1.0`, `HDR`/`Dolby`, and the raw ffprobe codec name with punctuation
folded), `eia_608` labelled `Closed Captions`, and a sidecar's suffix being its
`providerTitle` rather than the constant cycle 17 measured on one server.

Scored end to end the miss rate falls from **613 of zen's 105,558 real Streams to 174**, and
from 3 of the 46 recorded probes to **0**. The 174 are recorded rather than fitted: 132 are
`AC-3` titles, where no punctuation rule fits (`VC-1` does not carry `VC1`), and the rest are
streams whose stored title is the empty string, which a published row cannot be told apart
from one with no title.

Three smaller items came off the same corpus: thirteen missing `languages.json` codes, the
`dub` disposition on a video stream, and a trailing space in the label for a channel layout
ffprobe cannot name.

**One reported difference was re-measured and refuted.** zen's stored `displayTitle` carries
the codec on 78,902 streams where plexsim's catalog form does not -- which is the recorded
CLIENT-TITLE rule, not a defect, because those rows were read as `Plex Web`. Scoring
`mediainfo` against `displayTitle` measures that rule and not the model.

**And one check had to be restated before it was trusted.** The first version of the
language-table check read `mediainfo._VIDEO_FLAGS` directly, so on the unfixed tree it
raised an `AttributeError` -- which is cycle 19's lesson exactly: a check that fails for the
wrong reason proves nothing about its other six clauses. Restated with `getattr` and a
default, all seven fail on the parent for the right reasons and pass on the fix.

### Cycle 25 -- two dimensions nobody had swept, and the last of cycle 23's backlog

Two hunts and a driver round, and it is the cycle that tests cycle 24's reading of the curve.
Cycle 24 fell to 6 findings without hunting and concluded the programme had "moved from
finding differences to owing implementations". Cycle 25 did both halves: it drew the last
fully measured items off the backlog AND ran two hunts, on **dimensions nobody had swept at
all**. The backlog gave it 6 findings and the hunts gave it 13.

**The driver opened with the two items cycle 24 named first.** The SEASON and SHOW unmatch
cascades, deferred by cycle 23 because its own hunt was running the butler on the same
writable copy, were measured on an uncontended socket twice over -- once with the show
unmatched first on a freshly reset copy and once second, so the ordering is not an artefact
of the state the first run left behind. A season's unmatch pushes ONE `matching` frame and
then the season cascade; a show's pushes the MOVIE's `matching` PAIR and 46 frames of its own,
with the movie unmatch's activity progression (`Analyzing Media` at -1 and 25, then `Updating
Metadata` at 25 and 50) before the subtitle starts following each season. plexsim answered the
season with the seven-frame EPISODE form and the show with nothing at all. The same capture
caught a class-C gap beside it: a season cascade's activity has had "no `updated` frame" in
its docstring since cycle 15 and the code called `finish_activity`, which inserts one.

A seventh came out of checking that work rather than out of a hunt: **a show UNMATCH's
activity opens with the show's BARE TITLE** where its refresh opens with the frame title. The
cascade had been written to use the frame title everywhere, and running the refresh against the
same copy on the same socket showed the two paths differ at exactly one frame.

**`pixelAspectRatio` and `anamorphic` are a PAIR, and the boundary is the ratio rounded to one
decimal place.** 1,688 of zen's video Streams: 358 carry both, 1,330 carry neither, and not one
carries only one of the two. Sixteen distinct ratios agree on the boundary and the pixel-
difference model is refuted outright -- a 3840-wide `333:320` displaces 156 pixels and is false
where a 716-wide `16:15` displaces 48 and is true. The same harvest found the resolution label
had no 480 WIDTH bucket at all (zen's three 856x358 Media are `480`) and put every shape zen's
libraries hold and fun's did not into `media-resolutions.json` -- 17 of them, and **the corpus
itself failed against the unfixed code on exactly one row**, which is what adding to a corpus
is for. Two §3.3 entries closed with them: a search hit's row lead (`query=Sintel` is the query
that finally hits on kids) and the last of the nine verb-table rows.

**Territory A swept the CLIENT-IDENTITY dimension**, and its most valuable result is a
negative that BOUNDS the dimension: of sixteen identity headers sent alone against ~80 paths on
both references, **only three move anything at all** -- `X-Plex-Platform`, `X-Plex-Product` and
`X-Plex-Language`. No identity changes a `Media`, `Part` or `Stream` row, the XML-vs-JSON
default, the gzip decision, the paging defaults, a response header or a hub. The driver
re-swept that itself and reproduced it, after one false positive of its own: `X-Plex-Features`
appeared to move `/hubs` until the requests were INTERLEAVED, at which point it moves nothing
and the difference is kids' own drifting clip durations.

What it did find: **`X-Plex-Platform: Samsung` appends a hidden `includeInGlobal` row to a
library's `/prefs`** -- that exact spelling, the existing-library leaf alone, the second
platform-keyed reshape after `Roku` and the one that makes them a family. **A modern agent's
`Language` rows are TITLED and ORDERED in the client's language**, and `system_agents.json` was
captured with no `X-Plex-Language` at all -- **class A, the failure this project's one rule
exists for**. Two things about that list are unlike every other localised string: the ORDER
moves with the titles, and the no-header default is not English but each language named in
ITSELF, which is also where an unrecognised language lands. The preselected `default="1"` row
follows the client's language too, which the driver found only because its first version of the
change dropped the flag and turned a cycle-17 check red. Beside them:
`/media/providers?includePreferences=1` carries each library's whole `Preferences` block;
`/library`'s three `Directory` titles localise while `/system`'s identical string does not; and
a `/tree` node's `Setting` rows are NOT localised, where plexsim translated them -- the inverse
of the usual defect and still a rule broken.

**Territory B swept the CROSS-ENDPOINT dimension**: not what a write ANSWERS and not what
FRAMES it pushes, both well covered, but **which other endpoints change**. That is the question
a real client depends on, because the UI reads Home's hubs and the section's shelves rather than
the row it just wrote. 72 derived endpoints snapshotted before and after each of 21 writes on
both servers, 1,512 pairs per server, and the DIFF SETS compared rather than the responses.

Its two largest findings are one mechanism. **Continue Watching is not On Deck** -- it holds
only the rows with a stored `viewOffset`, where On Deck holds one row per show including a
next-up episode with no offset at all -- and **Home DROPS the `home.ondeck` shelf when its rows
are Continue Watching's**, which plexsim could never have reproduced because its two lists were
always equal. Beside them: **a SCROBBLE writes one history row per LEAF**, overturning cycle 9;
an episode history row carries `title` and `originallyAvailableAt` in an order of its own,
witnessed on zen and the copy alike; a playlist reaches Home's Recent Playlists shelf and both
search endpoints, where plexsim hardcoded the shelf to `[]` from a measurement taken when the
reference had no playlists; the untyped `/library/all` lists collections; the Continue Watching
hide does NOT reach a section's own shelf; and a `Common`'s `userRating` is a string leading the
row.

**The butler came back smaller than it went in.** Cycle 23 recorded six behaviours needing "a
queue notion plexsim does not have". Re-measured this cycle on an uncontended socket, **two of
them did not reproduce**: `POST /butler/{Task}` pushes its first frame at +0.0 on a freshly
reset copy, not after a 7-9 second delay, and `/activities` is empty the moment the run
returns. So there is no queue window, nothing to cancel, and the DELETE grammar plexsim already
answers is right. What did reproduce is the doubled `general.db.backup` -- four runs now -- and
it is built, with the opener that goes with it: `general.db.backup` starts at -1 with its
subtitle already set where `general.db.optimize` starts at 0 with an empty one, a pair plexsim
had collapsed into one shape.

**The driver broke its own build once, and the suite caught it.** The per-language agent list
needs the resolved language inside a PlexSim handler, and the first version put it in `query`.
Six write paths read `query` as a FORM and exclude `X-Plex-*` plus the one private key they
know by name, so a second private key reads as an unknown field: five passing checks turned
into 400s. It rides in a thread-local now, which is the scope of a request on a
`ThreadingHTTPServer`, and the commit that moved it carries the checks that caught it.

**And one hunter negative was challenged and re-confirmed**, which is the shape this project
wants: the driver's own spot-check of "only three headers move anything" flagged
`X-Plex-Features` on `/hubs`, and interleaving the requests showed the difference was kids'
drifting clip durations and bitrates, not the header. The negative stands.

### Next: cycle 26

1. **Run the hunts, on a dimension nobody has swept.** That is what made cycle 25 a
   21-finding cycle after three falling ones, and §10 said so before cycle 24 tested it.
   Closed and not to be re-swept: path, method/parameter, header, row, container, child
   element, attribute order, child-element order, the filter engine, and now client
   identity and the cross-endpoint cascade. Candidates nobody has taken: the PLAYBACK
   surface end to end (`/video/:/transcode/universal/*`, the session lifecycle, the
   Dashboard's byte counters) -- knowing that "plexsim has no encoder" bounds it; and a
   fresh CLIENT-SESSION capture through the harness, replayed against plexsim, which is
   the founding instrument and has not been re-run since cycle 7.
2. **The three butler behaviours that are still unmeasured**: `CleanOldBundles`' interleaved
   messages, `RefreshLibraries` reporting before it scans, and `POST /butler` running only
   what is DUE. Measure them on a freshly reset copy with nothing else writing to it -- the
   other three from that list did not survive being re-measured that way.
3. **The collection and playlist `/refresh` and `/unmatch` FRAME sequences** (§3.3), which
   carry `state 2` / `metadataState: matching` -- a state plexsim now emits on a season and
   a show unmatch, so the state machine behind it is no longer new.
4. **`/hubs/sections/{id}`'s own in-progress shelf**, which cycle 25 left half-measured: the
   hide leaves `tv.inprogress.{id}` alone on a real PMS as well as the section's
   `continueWatching/items`, and only the second half was implemented.

### Next: cycle 24 (superseded)

1. **The butler as one block** -- the queue notion, the six measured behaviours and the
   cancel grammar, all in §3.3 with their counts.
2. **The `mediainfo` model**, which is measured, held out against both corpora and sitting
   in a file. Largest single measured-but-unimplemented item now outstanding.
3. **The season and show unmatch cascades**, deferred this cycle because the writable copy's
   socket was contended -- 28 and 46 frames against plexsim's 8 and 12.
4. **`pixelAspectRatio`/`anamorphic`, `dub` on a video Stream, the 13 `languages.json` codes
   and `_channel_label`'s trailing space** -- four small, fully measured `mediainfo` items.
5. **Stop sweeping the child-element order.** It is held out against a real library with
   zero contradictions; the next dimension worth a hunt is not that one.

### Next: cycle 23 (superseded)

1. **The frame sequences §3.3 now holds**, which is the largest measured-but-unimplemented
   block this programme has ever carried: the four list-row sequences, the two missing
   `metadataState` values, the movie and show unmatch, and the season unmatch cascade. All
   of it is measured; none of it is guessed.
2. **`mediainfo` against a real library, finished.** Bisect the 480 cutoff, settle the
   language-part model on a library with more multi-token names, and take `eia_608` and the
   three `languages.json` codes.
3. **The butler**, whose cancel path and doubled backup activity are both measured.
4. **`xmlout`'s CHILD-element order at scale** -- cycle 22 took the attribute order and the
   paired-element table; the child order of zen's 155-role, 59-Media rows is unheld-out.
5. **The three §3.3 entries a second writable copy would settle**, unchanged: the media-item
   DELETE, `terminate`, `/statistics/*`, plus "a refresh not moving `updatedAt`". Still the
   user's decision.

### Next: cycle 22 (superseded)

Territories worth taking next, in order of expected yield:

1. **The frames the two LIST row types push.** Cycle 21 implemented the statuses and the
   container shapes of a collection's and a playlist's action leaves and left their frame
   sequences in §3.3 -- `/refresh` and `/unmatch` push real sequences carrying `state 2`
   and `metadataState: matching`, a state plexsim has never emitted anywhere. That is a
   measurable, self-contained territory on plexscratch.
2. **`xmlout` and the JSON writer at scale, which `xsweep.py` only started.** The per-row
   attribute order was swept per path against both references; what is unheld-out is the
   CHILD-element order of a real library's rows (zen's 155-role movies, its multi-Media
   rows) and the container attribute order on the paths a five-clip library cannot reach.
3. **The hub shelves and the detail read of a MOVIE**, the row type cycle 20 and 21 both
   stepped around: cycle 20 took type 3 and 4's listing grammar, cycle 21 their detail
   read, and nobody has swept a movie's `include*` matrix against zen since cycle 13.
4. **`mediainfo.py` against zen's real Media rows** rather than the 46 recorded probes --
   the last engine with no holdout, named in cycle 20's own next-steps and still unrun.
5. **The three §3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`, plus cycle 21's "a refresh
   not moving `updatedAt`". All need a CLAIMED copy, which needs plex.tv. Still the user's
   decision.

### Next: cycle 21 (superseded)

Territories worth taking next, in order of expected yield:

1. **Keep running plexsim's OWN engines over a real library.** Three cycles of this have
   now produced the largest batches: `fsweep` (cycle 18-19), `ssweep` (19), and cycle 20's
   `hsweep` and `psweep`. What is still unheld-out is `xmlout`'s writers against a real
   row's element and attribute order at scale, `library.edit`'s grammar against the copy
   column by column for the row types this cycle did not reach (collection, playlist,
   season, show on the SECTION path), and `mediainfo.py` against zen's real Media rows
   rather than the 46 recorded probes.
2. **The season and episode row at type 3 and 4 is not spent.** Cycle 20 took the listing
   grammar and the subsets; what is left is the detail read (`includeChildren`,
   `includePreferences`, `includeExtras` and the rest on a season and an episode), the
   artwork paths of a season, and the hub shelves those two row types appear on.
3. **The column census of a WRITE, for the row types hunt B did not sweep.** It covered a
   movie, a show, a season, an episode, a collection and a playlist on the item path and
   four row types on the section path. The `/library/sections/{id}/prefs` family per type
   and the item-prefs writes are the obvious gap.
4. **Widen `capture_i18n.py` again** -- the table is 545 entries over ~130 paths.
5. **The three 3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`. All three need a CLAIMED
   copy, which needs plex.tv. Still the user's decision.

### Next: cycle 20 (superseded)

Territories worth taking next, in order of expected yield:

1. **Run plexsim's OWN engines over a reference's rows, everywhere else they reach.**
   This is now two cycles' best instrument and it is nowhere near spent: `fsweep.py` on
   zen's SHOW library found four filter rules in one run, and `ssweep.py` proved the sort
   engine right, which is worth as much. What has never been held out this way is
   `hierarchy.py`'s derivations (every `parentTitle`, `grandparent*`, `parentYear`,
   generated title and borrowed-artwork rule, row by row against 14,433 real rows),
   `filters.parse`'s whole tree against `/query/parse`, `group=` against a real library
   at every key, and `mediainfo.py` against zen's real Media rows rather than the 46
   recorded probes.
2. **The show library at TYPE 3 and TYPE 4.** Cycle 19's holdout ran at `type=2` only.
   zen's 741 seasons and 9,287 episodes will have their own answers for every rule the
   show rows gave, and the season row is the one with the most derived fields on it.
3. **The media column family, which cycle 19 measured and only half built.** A show's
   media filter now reads its leaves; `sort=duration` and the `duration=` filter reading
   the MEDIA rather than the row's column are recorded and not implemented, and the
   `audioLanguage` operator table is unexplained.
4. **Widen `capture_i18n.py` again** -- the table is 545 entries over ~130 paths and the
   families still off the list are the ones no probe has fetched in another language.
5. **The three §3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`. All three need a CLAIMED
   copy, which needs plex.tv. Still the user's decision.

### Next: cycle 19 (superseded)

Territories worth taking next, in order of expected yield:

1. **The dimension left after the row and the container is the CHILD ELEMENT.** `Media`,
   `Part`, `Stream`, `Guid`, `Rating`, `Image`, `UltraBlurColors`, `Field`, `Location`,
   `Collection`, `Label`, `Setting`, `Chapter`, `Marker`, `Hub`, `Directory` -- which
   elements each container and row type carries, in which order, with which attributes,
   under which options. Cycle 17 mapped a row's attribute ORDER and cycle 18 the
   container's; nobody has censused the elements BETWEEN them.
2. **zen's section 2 is still the richest unexploited instrument.** Cycle 18 used it for
   the sort-title sweep and the filter holdout and found four rules in an afternoon; its
   per-show watch state, its deep paging and its 9,287 episodes' row shapes are still
   barely touched.
3. **Run plexsim's OWN engine over a reference's rows.** `fsweep.py` (in this cycle's
   scratch) fetches every row of zen's movie library once, asks the reference which rows
   a (field, operator, value) probe matches, then evaluates `filters.compare` locally
   over the same rows and diffs the SETS. It found the `title=`/`originalTitle` rule in
   its first run, on real data, with the disagreeing rows named. The same shape works for
   `sort=`, for `group=` and for the derivation rules in `hierarchy.py`.
4. **Widen `capture_i18n.py` again**, and check the identity-keying control found this
   cycle against the paths it now covers: the table is 545 entries over ~130 paths, and
   the families still off the list are the ones no probe has fetched in another language.
5. **The three 3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`. All three need a CLAIMED
   copy, which needs plex.tv. Still the user's decision.

### Next: cycle 18 (superseded)

Territories worth taking next, in order of expected yield:

1. **The dimension swept this cycle was the ROW; the one left is the CONTAINER.** Every
   container attribute, per endpoint, against both references -- the same treatment the row
   just had. `viewGroup`, `viewMode`, `title1`/`title2`, `nocache`, `allowSync`, the
   librarySection triple and the paging trio are each recorded from individual bugs and have
   never been swept as a set.
2. **zen's section 2 is still the richest unexploited instrument.** Cycle 16 got six findings
   from one pass and cycle 17 used it only to settle a §3.3 entry. Its 9,287 episodes, its
   per-show watch state and its deep paging are barely touched.
3. **The write surface below the COLLECTION ITEM path.** This cycle found the collection item
   layer was barely a surface; what is left under it is the playlist item path's own verb
   table, `/library/sections/{id}/prefs` per TYPE, and the `/actions/` family.
4. **Widen `capture_i18n.py` again.** The mechanism is measured and the table is now 451
   entries, but it is still only as wide as that path list -- and the list is now long enough
   that adding to it is cheap.
5. **The three §3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`. All three need a CLAIMED copy,
   which needs plex.tv. Still the user's decision.

### Next: cycle 17 (superseded)

Cycle 15 is complete, and it emptied §3.3's carried list: all three cascades are built and
four of its thin-evidence entries are settled. Territories worth taking next, in order of
expected yield:

1. **Keep pointing hunts at zen's SECTION 2.** One pass at the first real show library
   the programme has had produced six findings, three of which no five-clip library can
   show at all. Its seasons, its 9,287 episodes, its deep paging, its per-show watch state
   and its `/allLeaves` at scale are all barely touched. This is the same curve cycle 12
   rode when zen's movie library arrived, and it is nowhere near exhausted.
2. **The dimensions still unswept.** By PATH (cycle 14), by METHOD and PARAMETER (15) and
   by HEADER (16) have each produced a batch. What is left is the **XML half language by
   language**, a ROW's attribute order, and the surface swept by **row TYPE** — every
   attribute of a movie, show, season, episode, collection and playlist row, in every
   container that carries one, against both references.
3. **The write surface below the playlist.** This cycle found the playlist layer was
   barely implemented; the same is likely true of the collection ITEM path, the
   `/library/sections/{id}/prefs` family and the butler's individual tasks.
4. **The three §3.3 entries a second writable copy would settle**: the media-item DELETE
   (season and show cascades), `terminate` and `/statistics/*`. All three need a CLAIMED
   copy, which needs plex.tv, which past claim exchanges have shown resets account-wide
   tokens. Still the user's decision.
5. **Widen `translations.json`.** The mechanism is measured and the table is captured, but
   its coverage is only as wide as `capture_i18n.py`'s path list. Any string plexsim emits
   from an endpoint not on that list stays English.

### Cycle 15 — the parameter that makes history list, what a fold really folds, and the verb table

Hunters: the rest of the collection surface and the first sweep of the surface by HTTP
METHOD (opus, on plexscratch); the real library swept by PARAMETER rather than by path
(opus, read-only on kids and zen). **33 findings implemented** — the largest cycle in the
programme's history, and 1,006 checks in the suites for the first time.

The revalidation round paid three times before either hunter reported. **`titleSort`
trims the LEFT only** — a trailing space survives into the sort title, which is why a
title written with one carries no `titleSort` at all. **The library-level
`flattenSeasons`/`episodeSort` produce the item path's row effects**, and the item value
is a TRI-STATE where `-1` inherits; plexsim read `-1` as "off", so the write the Edit
Library dialog actually makes moved the setting list and half a row. And the **`group=`
`addedAt` tie-break** is settled: the copy has one tied pair, the survivor is the lowest
ratingKey under every sort, and plexsim was already right.

Two of §3.3's oldest entries fell. **`/status/sessions/history/all` lists nothing without
a `type=`** — the answer to cycle 11's unexplained `size 0`, carried for four cycles —
and the `type` is the row's `viewType`, not its metadata type: kids' MOVIES and zen's
EPISODES both answer to `type=1`. And **`title=` folds across a space**: cycle 14's
"the fold stops once the query has a space" was the colon on `Bogotá:`, and the real rule
— a raw word-prefix pass OR a folded pass whose non-final terms must equal WHOLE words —
was fitted on 27 queries and reproduced 25 more it had never seen.

The read hunt swept by PARAMETER where cycle 14 swept by path: the section search box
answers the LIGHT row, `excludeElements=` belongs to the RESPONSE rather than to eight
hand-wired handlers, four more columns filter and eight more parse, `contentDirectoryID`
scopes and validates `/hubs/search` alone, `/playlists` and the Recently Added hub carry
a `Meta`, `includeChildren` is a SHOW's alone, the inline Related block caps at five, and
three refusal grammars (`limit=`/`parent=`, `/hubs/search?limit=`, a fractional value on
the seven timestamp and state columns) were measured end to end.

The write hunt finished the collection surface — `ratingCount` is the member TYPE number
plus 100 when smart, the Preferences block is two settings whose second `smart` swaps,
`/prefs` is a 404, `collectionSort` is alphabetical for any parseable non-zero integer,
the section-path rename moves the members' TAG, a member's tag carries the collection's
guid, a label reaches the `/label` axis and a collection is findable in `/hubs/search` —
and swept the **verb** surface for the first time, which needs writes and so had never
been done. Thirty-three paths, every one different: a read-only route 404s every verb but
GET and HEAD, the legacy plug-in dispatcher answers all of them, an unknown method is a
500 and never a 501, and an empty POST to any candidate list is a 406.

And **all three cascades §3.3 had carried since cycle 13 are built**: the refresh cascade
(7/18/49 frames), the analyze triple per LEAF with its chapter-thumbs activity, and the
library create's `queueRegenerated` frame.

**Corrected here:** cycle 14's `titleSort` trim and its `title=` fold rule; cycle 11's
unexplained history `size 0`; the stale `episodeSort` in `item_prefs.json`; cycle 14's
note that `/library/search` finds a collection; cycle 14's "guid AND count" on a member's
Collection tag; and two standing suite checks that encoded beliefs this cycle overturned
(the bare-OPTIONS preflight and the banners/thumbs write statuses).

**And two corrections the hunters made to each other and to themselves.** The write hunt
reported `PATCH` and `OPTIONS` as blanket 404s; nine paths show they are routed like any
other verb, and the three that answer every verb answer these too. The write hunt's own
first run of the `collectionSort` test found "plexsim ignores it entirely" — a class-B
error it caught itself, because all three fixture movies carried the same year and the
same title stem, so release, alphabetical and insertion order coincided.

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

**Cycle 12:** the tag `count` on a detail row (refuted — it exists only on search tag rows, where
plexsim already emits it); a library create's per-item frames over real files and the
`backgroundProcessingQueue` regeneration; the 201 row's `metadataAgentProviderGroupId`,
`updatedAt`, `createdAt` and `contentChangedAt: -1`; the download queue (404 everywhere on the
copy, and Plex Web never builds the path); the subtitle SEARCH and apply-a-result PUT (503, no
OpenSubtitles); `/search` ignoring `limit` entirely; `titleSort` article stripping beyond
the/a/an (agent-supplied, samples far too small).

**Cycle 13:** the score PROMOTION `/hubs/search` applies when a query has fuzzy hits (a hub whose
best row scores above ~0.5 moves to the front; four of five zen queries fit, `comedy` does not);
the codec axes' empty-value behaviour (kids nothing, zen every row — a version difference);
collections, season 0 and `inProgress`, none of which either reference holds; which `Media` a
decision or a play queue picks on a two-version row (write-shaped paths the read guard refuses);
and the six write cascades in §3.3's first row, recorded and carried to cycle 14.

**Cycle 14:** the three cascades re-measured and not built (the show/season REFRESH
sequence, `analyze`'s per-LEAF frames plus its chapter-thumbs activity, and the 74-frame
library create with `queueRegenerated` at +60.8 s); the rest of the collection surface
(`includePreferences`, `collectionSort` semantics, the `/prefs` GET 404, the section-path
tag rename, collections as search tag rows, the member tag's `guid`/`count`, the numeric
`librarySectionID`, labels on a collection, `/library/collections/{id}/posters`); the
smaller write differences (a type-mismatched playlist create being a 400 that creates it
anyway, `/playlists/upload`, a 404 on a malformed collection item delete, the global
`/label` axis); the episode SPLIT (the copy cannot re-identify offline, so it is not
comparable with cycle 4's online measurement); `ratingCount` on a collection row; and the
non-English article strip in `titleSort`, which is per-item language and not mechanical.

**Cycle 15:** the library create's per-item frames (the 75-frame scan is over REAL files and
a library plexsim creates is empty by construction); the DELAY on the `queueRegenerated`
frame (plexsim pushes synchronously); the media pass inside a show refresh (state-dependent
work on real files, not a rule); `DELETE /library/metadata/{id}` on a season and a show (the
copy 400s every media-item delete); `enableCreditsMarkerGeneration` (the copy 400s it and
does not advertise it, where kids does); `collectionSort=2` on a fixture whose custom,
insertion and alphabetical orders coincide; plexsim's `Server:` banner; and the
non-alphanumeric `firstCharacter` bucket, re-read and still single-witness.

**Cycle 16:** a ROW's XML attribute order (the container's is settled; a tag is
`id, filter, tag`, which is not alphabetical, and the rest was not mapped);
`Set-Cookie: com.plexapp.system=…` on `/system` and `/system/agents/contributors` (the
plug-in framework's); the 1024/4096/~41 KB header limits as exact constants (measured as
brackets); the XML half of the localisation language by language (it follows by
construction, but was not diffed); `/library/metadata/{playlist}`'s container attributes
(`allowSync`, `identifier`, `mediaTagPrefix`, `mediaTagVersion` — measured on the copy,
and the paths that matter now answer); and the reference's own duplicated `Content-Length`
on a 206, which is a quirk rather than a rule.

**Cycle 17:** `PUT /library/metadata/{id}/poster?url=nonsense` being a 404 (REFUTED -- both
sides answer 200 and re-version); an EPISODE refresh running the SHOW's activity (two runs of
the same request on the same copy disagreed, so only the subtitle half is built); a `state 2`
/ `metadataState "matching"` opener on a season or episode refresh (reported by the hunt, not
reproduced in either driver capture); a remote artwork candidate's `images.plex.tv` proxy
`thumb` (plex.tv is out of scope, so the upstream URL stays); the three legacy
`com.plexapp.agents.*` rows at `/system/agents` (no plug-in framework -- the fixture's own
provenance records why); the non-alphanumeric `firstCharacter` bucket, re-read against zen's
section 2 and still single-witness; and the butler's per-task COUNTS, whose templates are
measured verbatim while the numbers are that server's own content.

**Cycle 18:** `/playlists/{id}` resolving any metadata ratingKey (both references
answer 200 with that item in a bare `{size: 1}` container and 500 on its `/items`, but
the projection is not stably characterised between them -- kids' movie 1 drops `duration`
and zen's keeps it -- and no client builds the path); the `/system` 404's MISSING
`Content-Type` (the status and the empty body are reproduced, the absent header is not);
the library delete's frame IDS (the server's own internal metadata ids, 14 for a nine-row
library, with nothing in the response to explain the id space); and the eleven-character
fold table's MECHANISM (the targets are measured, and nothing explains why `ø` becomes a
digit).

**Cycle 19:** `sort=duration` and the `duration=` filter reading the MEDIA's duration
rather than the row's column (measured on zen, where 23 rows whose Media carry no
duration head the ascending list in title order; plexsim's own rows always carry a Media
duration equal to the row's, so the two cannot differ for its catalog); `lastViewedAt=`
and `lastRatedAt=` being a 500 on both references where every other date column answers a
listing (two columns, and no rule that predicts which); `audioLanguage`'s six
non-`=`/`!=` operators, which no single model fits on a field where a row holds several
values (zen: `==en` 1,512, `!==en` 528, `<=en` 1,330, `>=en` 148, `<<=en` 3,664,
`>>=en` 657); the `viewCount`/`skipCount` NULL-versus-zero distinction, which orders a
tied group on both references and which the API omits either way; the `Agent` element's
attribute order (REFUTED -- the only constrained pairs are `hasPrefs < identifier <
primary`, which plexsim satisfies, and kids' leading `id` is 1.43.4-only); and the fact
that neither reference library holds a single `Collection` or `Label` tag, so those two
child elements could not be exercised at all.
Also from hunt B, triaged as content or as the copy's own limit rather than as rules: the
scan `progress` percentages (both servers are `round(99*i/N)`); every butler task's
COUNTS; `general.db.backup`'s step count (size-dependent); `/updater/status`'s
`checkedAt`; `ButlerUpdateChannel`'s 1.43.4 enum; `/security/token?type=delegation`'s 403
on an unclaimed copy; `POST /butler` and `DELETE /butler` frame counts (the reference runs
only DUE tasks, asynchronously); the playlist-create frame's `title`, which the reference
sent two different ways in one run; and `PlexOnlineUrl`'s 403, which could be claim-gated
on an unclaimed copy even though the other three write-protected settings are not
obviously so. A running `Activity` could not be caught in `/activities` at all -- every
activity on the copy completes in under 150 ms -- so that element was censused from the
frames instead.

**Cycle 20:** the `slug` being minted by the AGENT (measured on 2,406 of zen's rows -- every `local://` movie and show carries none, and a season's `parentSlug`/`grandparentSlug` are the SHOW's slug exactly -- and not implemented because plexsim mints a `plex://` guid for every row it holds, so the two rules coincide for its catalog; the slug's VALUE is not the slugified title either, 935 of zen's 1,536 matching and the rest carrying a disambiguating year or counter); a season with no `index` being titled `[Unknown Season]` with its episodes carrying neither `parentTitle` nor `parentIndex` and its `index` column reading -1 (plexsim synthesises an index for every season it builds, so the row cannot arise -- the generated-title half IS built); `addedAt!==<date>` (REFUTED -- 500 on zen, on kids and on plexsim alike); the `Specials`/`[Unknown Season]` labels being single-reference, kids holding neither; and the exact spelling a real PMS gives a slug, which nothing in the API exposes.

**Cycle 24:** `AC-3` in a title carrying the `AC3` codec part (132 zen streams) -- no
punctuation rule separates it from `VC-1`/`VC1` and `E-AC3`/`EAC3`, and a general one breaks
58 other streams; streams whose STORED title is the empty string, which a published row
cannot be told apart from one carrying no title; `visualImpaired`, which is real on zen but
has one witness and is in neither disposition table; the REGIONAL `languageTag` family
(`en-US`, `es-ES`, `pt-BR`, `es-419`, `zh-Hans`, `zh-Hant` -- and `zh-Hant` changes the NAME
too), which the one-name-one-tag `languages.json` shape cannot express; and
`pixelAspectRatio`/`anamorphic`, whose derivation is pinned but whose `anamorphic` threshold
is only BRACKETED to (1.028, 1.067).

**Cycle 25:** the `anamorphic` CONSTANT itself (both sides measured, 0.05 the only round
number between them, nothing in the gap); the show-unmatch activity's PROGRESS numbers (the
pairs are measured, the formula is fitted to a two-season show); the MEDIA pass inside an
unmatch cascade (state-dependent work on real files -- present on a freshly reset copy and
absent on one that has run it, which is cycle 15's rule); the `Samsung` prefs row's PURPOSE
(`hidden="1"` with an empty label, so no settings pane draws it and the server side cannot
show what a Samsung client does with it); the agent `Language` ORDER as a MODEL (stored per
language instead, because a real PMS collates it with ICU); the `videoResolution` 480 boundary
as a bisected constant (856 is the smallest width zen has in the bucket, built the way the
other five entries were); and the three butler behaviours cycle 25 did not re-measure
(`CleanOldBundles`' interleaving, `RefreshLibraries`' ordering, `POST /butler` running only
what is due) -- the other three from that list were re-measured and two did not reproduce.

**Cycle 23:** the whole butler family -- `POST /butler/{Task}` queueing rather than running,
`general.db.backup`'s doubled activity, `CleanOldBundles`' interleaved messages,
`RefreshLibraries` reporting before it scans, `POST /butler` running only what is due and
stepping its wrapper `done/(done+1)`, the `queueRegenerated` frame on a second POST,
`/activities` retaining the finished wrapper, and the cancel grammar -- all measured 3/3 and
all needing a QUEUE notion plexsim does not have (§3.3, one block); the `mediainfo` model
that takes the miss rate from 613/105,558 to 174/105,558 and from 3/642 to 0/642, including
the language parenthesis-strip rule that settles cycle 22's five-witness contradiction;
`pixelAspectRatio`/`anamorphic`, `dub` on a video Stream, `visualImpaired`, the 13
`languages.json` codes and the regional `languageTag` family; `_channel_label`'s trailing
space; the season and show unmatch cascades (28 and 46 frames), deferred because the
writable copy's socket was contended; `/library/metadata/{id}/match`'s HEAD/PATCH/OPTIONS;
and `hasScalingMatrix`, which is on every one of zen's 8,650 h264 video Streams and on no
other codec but is not derivable from ffprobe -- emitting it would be a proxy.

**Cycle 22:** the FRAME sequences a collection's and a playlist's `/refresh` and `/unmatch`
push, and the two `metadataState` values they carry (`state 2 matching` on every row type's
unmatch and on a list's refresh, `state 3 matching` on a movie's and a show's) -- all
measured in full, all in §3.3, and held out because the state machine behind two new values
needs a sweep of where else they appear; the mediainfo language-part rule, whose reported
model breaks the recorded corpus (§3.3 has the five-witness table); `eia_608`'s
`Closed Captions` label, which sits on the plain display-title path; the `videoResolution`
480 cutoff, un-bisected; the `HDR`/`HDR10` dynamic-range match, two witnesses; the three
`languages.json` codes zen uses and plexsim's catalog does not; `DELETE /butler/{Task}`
cancelling a QUEUED task; and `POST /butler/BackupDatabase` running its activity twice.

**Cycle 21:** the five media-codec edit-field names (`atmos`, `audioCodec`, `audioLayout`,
`subtitleCodec`, `videoCodec`) that take an item lock on plexscratch, which is 1.43.4 --
cycle 20 established that family is 1.43.4-only elsewhere and zen cannot be written, so
1.42.2 is unmeasurable for them (§3.3); `hdr10plus`, a 200 that stores nothing on a single
witness; the FRAME sequences a collection's and a playlist's `/refresh` and `/unmatch` push
(3 and 7 frames on a collection, 4 and 2 on a playlist, carrying `state 2` /
`metadataState: matching`, a state plexsim emits nowhere) -- the statuses and container
shapes ARE implemented; the `/system/scanners/{N}` 500's 641-byte body, which is PMS's own
Python traceback naming its own file paths; and the `text/html` body a real PMS sends with
a 404 anywhere OUTSIDE `/system` (plexsim's `text/plain` refusal remains a deliberate
deviation, inherited from cycle 1).

**Cycle 11:** ~~`/status/sessions/history/all` answering `size 0`~~ (ANSWERED in cycle 15:
it needs a `type=`); `includeBandwidths=1`; `/library/people/{id}` answering any TAG id;
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

Local processes **not** belonging to this work, left alone on purpose (re-checked at the end of
cycle 15): `32400` is another session's plexsim; `8787` another session's `http.server`; `32502`
a plexsim on `catalog.example.json`; `32595`/`32596` plexsims of unverified origin. `32511` and
`32512` were here and their processes are gone.

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
- **A finished agent can still start a server.** See §5 step 12. Twice in cycle 15 the write
  hunt restarted its plexsim after the cycle had been pushed, the second time with `ppid 1`.
- **`run_in_background: true` DOES outlive the tool call.** Cycle 15's write hunt reported that
  it does not on this machine, and that is not reproduced: every background server the driver
  started that way survived its call, including the ones answering suites for minutes
  afterwards. The agent's servers exited for some other reason, not chased because nothing in
  either report depended on them. Recorded here so the claim is not picked up as a rule — it is
  the kind of thing §3.1 class F is for.

- **The scratch disk can fill, and when it does the Bash tool stops entirely** -- it
  cannot create its own output file, so every command fails before it runs and there is
  no way to clear space from inside the session. Cycle 16 hit this after ~40 commits.
  `sdrive.py` re-exports the whole tree per patch and each hunt keeps a frozen tree of
  its own, so a long cycle accumulates hundreds of megabytes under the scratchpad. Clear
  `tasks/*.output` (the subagent transcripts, the largest single items) and the frozen
  trees (`head*`, `oldtree*`, `parent_srv`) as you go.
- **A worktree is only a checkout: deleting it does NOT lose the commits.** They live in
  the main repository's object store and the branch ref with them, so
  `git worktree prune && git worktree add <path> <branch>` brings everything back.
  Cycle 16's worktree was deleted while recovering disk space and all 40 commits were
  intact. **Push the branch from the first commit anyway** -- section 5 says so, cycle 16
  had not been doing it, and that is the difference between a scare and a loss.

- **`filters=` on a notification channel is a WHITELIST of EVENT TYPES, and `all` is not
  one of them.** A socket opened with `filters=all` receives nothing at all, which is
  indistinguishable from a server that pushes nothing -- cycle 19 lost a whole round of
  frame measurements to it, on a copy that had been answering the same hunt minutes
  before. Open with NO filters, and always send the control write first.
- **The eventsource channel is the easier listener of the two.** It is plain HTTP, it
  needs no `websocket` package, and it carries the same events -- `event: <type>` plus a
  `data:` line holding one bare row. `scratchpad/c19/sse.py` is the whole client.

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

**Measuring, as of cycle 15**

- **A "zero frames" measurement needs a control.** A silent notification socket and a broken
  listener look identical. Every cycle-14 frame count was taken by running a write that MUST
  push frames (a title edit: `state 3 loading`, `state 5`) on the same socket first.
- **Probe a FRESHLY RESTARTED fixture server.** Pointed at one `test_ui_surface.py` has
  finished with — it deletes a row and a section at the end, and has rewritten every tag list
  and every piece of artwork — a sweep reported `thumb` and `poster` as 404s that both
  references answer 200.
- **Sweep a surface by comparing THREE servers, not two.** Fetch each path from kids, from zen
  and from plexsim, and report only where the two references AGREE and plexsim differs. That
  filters out every version difference automatically. One afternoon of it found five findings,
  three of them endpoints plexsim had invented.
- **A hunter's rule is scoped to what it tried.** "A show's prefs write pushes nothing" was true
  of the four settings the hunt sent and false of `showOrdering`. Re-measure the rule on a case
  the hunt did NOT use before generalising it.
- **The edit parameter for a tag is the FILTER field name, not the row's element name.**
  `actor[0].tag.tag` writes a `Role`; `role[0].tag.tag` is a 400 that writes nothing — and a
  check built on it skipped itself silently.
- **`lastViewedAt` has one-second resolution.** Two timeline beats inside the same second tie,
  and any rule about "most recently viewed" then reads its tie-break instead.
- **Fit a model, then HOLD OUT a set it has never seen.** Cycle 14 left the `title=` fold's
  mechanism "unexplained" and §3.3 flagged it as the kind of rule this project gets wrong.
  Cycle 15 enumerated candidate models, scored each against 27 measured queries, and then ran
  the winner against 25 queries on five rows it had not been fitted to — which it reproduced
  exactly. A model that scores 24/27 and a model that scores 27/27 look equally plausible in
  prose; only the holdout separates them.
- **Never probe the server a suite is currently running against.** Writing one title to the
  fixture mid-run cost a whole suite pass and read as a collapse — 889/926 with eleven extra
  skips, every one of them "need three movies". Use a second port for probes, and if a run
  looks catastrophic, check the fixture's row count before believing the failures.
- **Sweep a NEW DIMENSION rather than the same one again.** Cycle 14 swept the surface by PATH;
  cycle 15 swept it by METHOD (33 paths, every one different) and by PARAMETER
  (`excludeElements=` turned out to belong to the RESPONSE, not to eight hand-wired handlers).
  Each sweep produced a batch no amount of re-hunting the previous dimension would have. The
  dimensions still untouched are HEADERS, response headers, and the XML half.

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
| `capture_agent_languages.py` | Cycle 25: `/system/agents`' per-language `Language` list, read from the XML (the JSON writes the rows as repeated KEYS and a parser keeps one) in each of the 28 languages plus English plus the no-header default. Writes `agent_languages.json`, which `server.py` loads beside itself. Re-run it if the reference's language list ever changes |
| `secretscan.py` | Cycle 25: the gating scan §5 step 10 asks for, over every file the branch changes rather than over a `git diff` (which this shell's filter can truncate). Anchored on the VALUE, with the suite's own literal `suite-gate-token` excluded by name -- a naive pattern matches that and the handover's own prose about the pattern |
| `sdrive.py` | **The proof driver.** Per patch: export and serve the worktree's HEAD, apply, serve the fix, run the patch's suite block against both, commit only on fail-then-pass |
| `mdrive.py` | The same for an OFFLINE check (`tests/test_mediainfo.py`); copies every tracked sidecar into the parent tree |
| `runblock.py` | Runs one patch's check block against one or more servers, in the suite's own namespace. Both drivers call it |
| `guarded.py` | The write hunt's HTTP client: writes only to plexscratch and `PLEXSIM_BASE`, GET/HEAD only to the references, a `WRITE_GET` refusal list, plus the frame listener and the JSON/XML diffs |
| `probe.py`, `diff.py` | The python-plexapi sweep: ~460 read calls behind a session that refuses writes and off-host URLs |
| `fsweep.py` + `rd.py` | **The engine holdout** (cycle 18): fetch every row of a real library once, ask the reference which rows a (field, operator, value) probe matches, then run `filters.compare` locally over the same rows and diff the SETS. A difference is a rule plexsim has wrong, on real data, with the disagreeing rows named. `rd.py` is the guarded GET/HEAD client it reads kids and zen through |
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
- cycle 13: `…/75a28f3e-2e35-4b90-b25b-8aba31a8d0cd.jsonl`
- cycle 14: `…/03c44878-09cf-46fb-b529-806106f49f09.jsonl` plus its `subagents/agent-*.jsonl`
- cycle 15: `…/744f97c4-4f4c-41ea-a881-f16ca8d3e4c2.jsonl` plus its `subagents/agent-*.jsonl`
- cycle 25: `…/b59cf966-bebc-4d11-b0a6-beb9e8e6f6a5.jsonl` plus its `subagents/agent-*.jsonl`.
  Its scratch directory (`c25/`) holds both hunt prompts, `cascade.py` and `sse.py` (the
  dependency-free eventsource listener the frame work runs on), `par.py` (zen's
  pixelAspectRatio/anamorphic harvest), `identity_check.py` (the driver's own re-sweep of hunt
  A's negative), `remeasure_a.py`/`remeasure_b.py`, `butler.py`/`backup2.py`, the twenty patch
  modules and the suite logs.
- cycle 16: `…/12a3f40a-efad-46c2-85e4-3da3421fe8ea.jsonl` plus its `subagents/agent-*.jsonl`.
  **Its scratch directory was cleared mid-cycle to recover disk space**, so the patch
  modules, the assembled prompt files and the re-measurement clients are gone; the
  territories are in `hunt_prompts.md` and everything measured is in `docs/findings.md`.
  The one artifact that mattered — the branch — survived, because a worktree is only a
  checkout and the objects live in the main repository.

The scratch directory of each is
`/private/tmp/claude-501/-Users-bensarmiento-debridmediamanager-plexsim/<session>/scratchpad/`
(cycles 11 and 12 share one, in `c11/` and `c12/`; cycle 13 is in `c13/`, cycle 14 in `c14/`).
All five still existed at the end of cycle 15. Each holds `prompt_A.md`/`prompt_B.md` (the
hunts' full briefs, also appended to `hunt_prompts.md`), the re-measurement clients, the patch
modules (every patch with its `apply`, message and check block), the frozen head, the worktree,
and the suite logs. Cycle 14 adds `rd.py` (the driver's guarded GET/HEAD client for kids and
zen), `frames.py` (a dependency-free notification-socket listener) and `vsweep.py` (the
structural kids-vs-zen diff). Cycle 15 (in `c15/`) adds `compose.py` (which builds both hunt
prompts from the previous cycle's, so the lists in 5 step 4 are refreshed in one place rather
than by hand), `remeasure_a.py`/`remeasure_a2.py`/`remeasure_b.py`/`remeasure_b2.py`/
`remeasure_b3.py` (the driver's re-measurement of every hunter finding), `fold.py`–`fold5.py`
(the `title=` model, fitted and then held out), `history.py`–`history5.py`, `verbsweep.py`,
`cascade.py` and `create.py`.
