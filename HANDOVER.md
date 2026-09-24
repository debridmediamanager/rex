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
   the list of things this project has already got wrong, the seven ways it got them wrong, and
   the items still standing on thin evidence.
3. **§4**, the instruments, and **§5**, the cycle procedure.
4. Then §6–§9 as reference while a cycle runs.

**The standing instruction for whoever takes over:** a rule in `docs/findings.md` is a
measurement *with a date and an instrument attached*, not a fact. Successive cycles have overturned
more than 80 of them. Before you build on a rule that matters, check it against §3's seven
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
- **plex.tv writes are out of scope**: Optimize and Sync, plug-in/provider control planes and
  account/device mutation. Authenticated READS that shape the server's own claimed state are in
  scope and were exhaustively classified in Cycles 46–48.

**The one rule: every emulated response is a measurement, not a guess.** A response shape is
taken from what a reference PMS answers *for the client's exact request*, never written by
hand. The loop stops only when every finite territory in a repeat cycle returns `no findings` and
every remaining cell is an explicit capability, state, external-service or safety bound.

**The stopping rule since cycle 56, set by the user:** the loop stops when a cycle finds
**nothing a real client would notice**. Every hunt finding is tagged CLIENT-VISIBLE -- an
official client or python-plexapi builds that request shape in ordinary use and the difference
changes what it renders, plays or does, with the call site cited -- or MALFORMED-ONLY, reachable
only with input no client sends. Client-visible findings are built; malformed-only ones are
recorded in §8. The hunt preamble carries the rule and the sentence "no client-visible
findings". This is the reading the note below arrived at, made operational after cycle 55's 37
findings turned out to be mostly refusal grammar for malformed input.

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
| `main` | cycle 64 merged, equal to `origin/main`, clean tree |
| Cycles complete | **58. The last CLEAN cycle was 51 — the stopping gate below — and cycles 52, 53 and 54 each reopened the loop by recognising a new instrument; cycle 55 repeated 54's two axes at full resolution and neither was finished (§7).** The stopping gate as it stood: **51 — met on every finite Cycle-50 axis.** Cycle 50 recovered seven surviving commits and the original transcripts, found one lost measured patch, **18 report entries not yet implemented**, three unprotected transport/GDM rules and one now-actionable Cycle-49 deferred entry, then closed every Territory-A **23/23**, Territory-B **8/8** and Territory-C **10/10** entry. It added the exact 58-path prefix registry, three complete regional preference documents, per-session playback state, the searchable-tag gate/current count, exact GDM lifecycle and the remaining catalog/transport/format rules. Cycle 51 froze that behavior/test head and ran three independent read-only stopping audits: exact requests/client, catalog/hierarchy/controls, and claim/dual-format/transport/TLS/discovery. **All three reports end literally `NO FINDINGS`**; every finite denominator is green or named as an explicit capability/safety/underivable-state bound. See §7 and §8. |
| Cycle 64 | **64 — the catalogue as an instrument: 2 CLIENT-VISIBLE findings in 2 fix commits.** C: zero findings on axes 1, 2, 3, 4, 7, 10. B: the whole product and platform vocabulary of the binary swept over 40 read paths on four servers -- one product-keyed rule plexsim lacked (`Plex Home Theater`'s five-component version, axis 8). A: the rendered client clean in English and in 18 localised captures, and a new instrument, the reference's `plex.mo` catalogues, found an unwalked dialog -- a collection's Advanced settings never localised (axis 9). Hunt A stalled once mid-run on a dead background monitor and was resumed |
| Cycle 63 | **63 — nine axes clean: 2 CLIENT-VISIBLE findings in 2 fix commits.** A and C reported no client-visible findings on axes 1, 2, 3, 4, 5, 7, 9, 10 (A one malformed-only, C none at all); B found two on axis 8 by sweeping the product strings the binary carries across the two product-keyed rules -- the codec suffix is 21 products, not 13, and `Bandwidths` is withheld from `Plex for Windows Phone` too |
| Cycle 62 | **62 — eight axes clean: 2 CLIENT-VISIBLE findings in 2 fix commits.** All ten axes with the same three territories. Clean: 1, 2, 4, 5, 6, 7, 9, 10 -- A reported exactly `no findings` over 18 localised captures and 14,790 settings cells. C found one on axis 3 (a renamed collection's members keep the OLD name on their light rows), B one on axis 8 (`Bandwidths` withheld from `Plex for Windows`) |
| Cycle 61 | **61 — the stopping repeat that was not: 7 CLIENT-VISIBLE findings in 4 fix commits.** All ten axes repeated with cycle 60's three territories. Clean: 1, 2, 4, 5, 7, 8, 10. C found two on axis 3 (a collection rename onto any name in the tag TABLE is a 409; a new tag's first ASCII letter is upper-cased); B four on axis 6 (a session listed only from its first `playing` beat and given a `Session` block only after a decision; a theme uploaded after a logo lands; a show theme reaches its seasons as `parentTheme`); A one on axis 9 (`/:/prefs` pages, so cycle 60 had re-captured 20 of zen's 173 settings) |
| Cycle 60 | **60 — localisation as an axis, and the first three-hunter repeat: 7 CLIENT-VISIBLE findings in 6 fix commits.** Three hunters on the axis ledger. A (opus) repeated the rendered client clean in English and walked it in de, da, ja and pt: five, all localisation -- stream language names by TAG (under `en` too) and the words titles are composed from, hub titles around the name they carry, the Related/post-play/manage titles the table never held, and `translations.json` still carrying 1.43.4's translation in 414 cells where 1.42.2 answers English (cycle 59 overlaid changed cells only; the capture now records unchanged ones). B (fable) repeated python-plexapi's writes (every cycle-59 fix held) and drove playback as 15 client identities (Player blocks, timelines, queues, stream selection and the profile model all identical): one client-visible, a regression cycle 59's upload store introduced (a DELETE of an uploaded theme answered its bytes). C (opus) repeated axes 1, 2, 3, 4, 7 and 10: one client-visible (joining a collection locks the member's `collection` field) and one unmeasurable bound (Manage Recommendations' writes need a claimed writable 1.42.2). Axes 1, 2, 7 and 10 are clean |
| Cycle 59 | **59 — the rendered client, and python-plexapi's writes: 16 CLIENT-VISIBLE findings in 14 fix commits, 7 recorded.** Two axes no client-impact cycle had taken. Territory A (opus) drove the REAL Plex Web app in a browser -- kids' 9223 Chromium, the bundle plexsim serves, plex.tv answered by `page.route` so the app saw exactly one server -- against plexscratch42, kids and plexsim, page by page: five, all built (the `en` enum injection, the Collections tab, the Playlists source and its Home shelf, a library's Recent Playlists shelf, and 1.42.2's pinned-list Recently Added). Territory B (fable) drove python-plexapi's whole write and playback surface -- 182 methods, 291 steps a server -- and found nine client-visible: the same `en` enums, the proxied `/player` 404, a smart collection's user filter, the library-wide scan cancel, a playlist's timeline, three upload rules, and a match's rename cascade; its Optimize cell stays the recorded bound. Re-measuring them the driver found three more: an item's own language enum was never localised (fixed from a zen capture), `translations.json` is per VERSION (405 cells re-captured off zen), and cycle 58's Continue Watching gate is the LIBRARY's play history, not the show's |
| Cycle 58 | **58 — the flows repeated, and the owner's screens: 9 CLIENT-VISIBLE findings (all built) and 5 recorded.** Territory A repeated cycles 56-57's client read and write flows (every fix held) with sequences no earlier cycle ran, and found four about playback state: a library's Continue Watching shelf gates on the SHOW's play history rather than on how the next-up arose; a new session's sub-minute beats keep the resume point; a beat at exactly the stored position writes nothing; and a collection rename onto a taken collection tag is a 409. Its fifth, the section shelf's per-URL cache, is recorded. Territory B drove the owner-facing screens -- Settings, library Add/Edit/Manage, Manage Recommendations, the Dashboard, activities and console -- and found four: the legacy agents' combination chain and its order save, Top Played rolling an episode up to its show and season (plus a flattened-season follow-up), statistics buckets aligned to local calendar boundaries, and a hidden library kept off Home and out of global search. The session that ran the hunts ended mid-cycle; the next session rebuilt the implementation pass from the surviving worktree and hunt outputs |
| Cycle 57 | **57 — lists, and what every screen shows after a user acts: 9 CLIENT-VISIBLE findings (all built) and 8 recorded.** Territory A repeated cycle 56's client inventory (every fix held) and drove python-plexapi's whole read surface (108 classes, 646 members, 1,018 probes a server); neither reference holds a list, so its four findings -- collection search ladder, a list's history, a playlist's score base, the sync flag -- were measured on the two writable copies, which agree across versions. Territory B drove every client write on plexscratch42 and plexsim with 41 redraw reads after each, and found five: the merged Continue Watching hub holding the next-up rows, the section shelf's play-only next-up, a pristine show's OnDeck (refuting cycle 41), the collection Related shelf, and the play-queue marker's block rule. Territory B's implementation was delegated to an agent under sdrive, as cycle 55's was |
| Cycle 56 | **56 — the first cycle under the client-impact stopping rule: 6 CLIENT-VISIBLE findings (all built) and 18 MALFORMED-ONLY (recorded in §8).** Territory A started from the clients -- recorded Plex Web sessions, the reference walk, python-plexapi's templates and 1,455 probes through it, the bundle's path families, every published hub key, and the client's writes driven on plexscratch42 -- and found three (a show library's provider storage totals, the playlist Meta's Type key and active flags, the legacy agents' per-MediaType languages). Territory B repeated cycles 54 and 55 (every fix held, `regress55.py` 123/123) and widened them, finding three (`decade` ignoring its operator, the per-user counters reading 0 once any state is written, `includeGuids` on a library's hub page). **The rule is working: every client-visible finding had a named call site, and the malformed-only count (18) is three times the client-visible one** |
| Cycle 55 | **55 — cycle 54's two instruments at full resolution: 37 findings in 37 fix commits.** Territory B gave every one of the binary's 366 non-maintenance SQL reads a verdict and found eight: Related is a server-side TAG SCORE (first three genres and four roles, two shared, 59 of 59 zen rows exact), On Deck's next-up anchors on the most recent view, `recentlyViewed` and a section's `/onDeck` order by the view-state write time, `popular` counts history rows, a playlist's duration sums whole seconds, the genre shelf is the head of its own key, and a season's generated title follows its index. Territory A repeated cycle 54's parameter sweep (every fix held) and widened it to eighteen values and eight operators: 26 rules, most of them the listing's structural parameters being PARSED wherever they are read, the integer and option grammars, and the filter value grammar -- built as 27 commits by an implementation agent against the hunter's 123-check regression script. The driver found that a library's TV Recently Added shelf IS the section's roll-up, and the implementation pass that `/hubs/promoted` refuses the hub keys when a show library is in scope. **Five recorded beliefs overturned**; **two §3.3 entries CLOSED**; **one reference crash** (kids' PMS, from a search `limit` of 4294967296, restarted in seconds) |
| Cycle 54 | **54 — the binary's own parameter names and its own SQL, and containers made one at a time: 22 findings in 22 commits.** Territory A derived 1,018 query-parameter names mechanically from both binaries' string tables (cycle 46 had swept only the seven prefixed families) and crossed them with 94 safe families -- 332,304 cells on zen -- finding nine: six media filter columns, eighteen more tag-table fields, the cluster columns and `trash`, the listing query reaching seven more families, the TV Recently Added shelves filtering their EPISODES before the roll-up, `/hubs/items` scoping, `unwatched=1` as a flag with two grammars, the Dashboard's view-time keys, thirteen parameter refusals and `sectionID` as a number. Territory B read 707 SQL statements out of the binary and found `viewUpdatedAt` (Plex Web's own `sort=viewUpdatedAt:desc` answered 500 here) and the episode listing's leaf-count filters reading the SHOW. The driver made seventeen containers on plexscratch42 and found the part `Content-Type` table, `.mov` as `mov`, and `packetLength: 188`, and in re-measuring the hunts found the per-user date family, three date-typed columns and the negative-rating stamp. **Four recorded beliefs overturned** (cycle 9's "a negative rating keeps `lastRatedAt`", cycle 14's "`viewedAt>=abc` is a 400", cycle 20's "a NULL date matches nothing" for the three per-user dates, cycle 46's "a show's season list ignores `skipCount`") and **two §3.3 entries CLOSED** |
| Cycle 53 | **53 — the framework's own source, and the database's own schema: 19 findings.** Cycle 52's instrument was the binary; cycle 53's is what ships beside it. The plug-in framework is uncompiled **Python** — 289 files plus `System.bundle/Contents/Code/*.py`, which IS the `/system` surface, so every `@route` is an endpoint and "which endpoints exist" became a read rather than a sweep — and `Resources/com.plexapp.plugins.library.db` is an empty database TEMPLATE whose schema names every column PMS can store. The driver found the media flag image family (`mediaTagPrefix` points every client at it and every URL under it was a 404); Territory A enumerated the framework and found thirteen (eighteen more routes, five more plug-in sub-paths, the eleven legacy agents and their attributions, four envelope rules, two writer corrections, and the state cookie that makes every framework route answer Tornado's empty 200 with the one `Server:` banner on the surface); Territory B held the schema and the binary's attribute vocabulary against the surface and found four (ten more real query columns where an unknown field answers EVERY row, `isAdult`, `includedAt`, and `includeAlternates` inlining artwork the row does not own) plus the census negative — 130 request families, three servers, zero JSON scalar-type differences. **One inherited §8 bound retired** (the legacy agents, whose sibling half cycle 52 had already retired, leaving the split as drift) and **two recorded beliefs corrected** (cycle 33's "no `Server:` banner", cycle 18's "the only 405 on the surface"). **Six existing checks re-measured** where they encoded plexsim's own four-agent catalogue |
| Cycle 52 | **52 — the loop reopened on a new instrument, and 24 findings came out of it.** Cycle 51 was clean, so cycle 52 did what cycles 44, 45 and 49 did: it recognised an instrument nobody had used. Fifty-one cycles built plexsim's route table out of what a CLIENT was seen to ask for; the **Plex Media Server binary** names the endpoints no client sends, and it is readable on both references. Its string table carries PMS's own route templates with their parameter captures, and diffing the two binaries splits the vocabulary per version for free. The driver took the route table (the dispatcher's prefix listing as a RULE where cycle 50 had 58 captured points, three static files that answer with NO token, `/:/plugins`' seventeen rows and the second framework subtree under it, the cookie, `/connections` as text/plain, the widened 401 partition); Territory A took the artwork family (`/:/resources/*` as 70 names, the read leaves' verb table, the photo transcoder fetching the path it is given, list artwork ownership, the empty playlist's candidate); Territory B took the leaf vocabulary the binary names (six refusal shapes, `/hubs/sections/{id}/popular`, the Categories `blendColor` table, the actor silhouette, the part leaf's arity, `.tag.role` on the section save). **Two recorded beliefs overturned** and one §8 bound retired. Closing the cycle found cycle 53's instrument: PMS ships the plug-in framework as 289 readable Python files, and one grep of `agentservice.py` replaced a model this cycle had fitted to twelve cells |
| Cycle 48 | **48 — the stopping repeat was clean across every Cycle-46 axis.** Cycle 46 exhausted the query-option namespace (**116 names × 76 safe families × 3 hosts = 26,448 cells**), the request-header namespace (**61 normalized names / 88 spellings; 24,156 logical cells**), the installed Plex Desktop static/native boundary, live/dead provider availability, all **1,829** frozen outbound exchanges, and both finite source/control universes. It produced **19 product findings**: 14 query propagation rules, two `/web` header rules, provider-backed file availability, exact account feature acquisition, and cloud watch/rating import. Cycle 47 repeated the external axes and found one product residual — daily account/feature renewal — plus one test-order defect in the new regressions. Cycle 48 repeated query, header, outbound, historical and advertised-control denominators after both repairs. Every Cycle-48 territory report ends exactly `no findings`; UI is **1,629/1,629**, decisions **2,340/2,340**, mediainfo **35/35**, and the explicit safety/capability bounds are retained rather than silently omitted. |
| Cycle 43 | **43 — stopping gate met on every finite axis at the time.** Cycle 43 reopened the clean Cycle-42 head and independently replayed the 332-atom historical universe and the complete 4.147.1 advertised-control graph; both reports again end exactly `no findings`. It also closed the only remaining current-client boundary by obtaining the complete public Plex Web 4.160.0 app build: 205/205 JavaScript assets, 16/16 CSS assets and 207/207 SRI checks. The 177 JavaScript assets absent from Cycle 40's app-side subset contain zero new PMS call sites, request-builder imports, controls or actions. The historical axis is 332/332 with all 24 Cycle-41 cross-fix audits passing and zero actionable/unbounded unresolved atoms. The advertised graph remains 49 source families, 1,221 advertisements, 47 predicates, 339 controls and 45 action/readback pairs, with 39 exact witnesses, six explicit bounds and zero unresolved. Root independently repeated all suites. Protected references are byte-identical, secret scans are clean and every verified Cycle-43 process is stopped. |
| Cycle 36 | **36**. Cycle 36's driver swept a dimension no cycle had taken -- **what a real PMS answers when it REFUSES** -- and got eight findings in an afternoon: a refusal's BODY is one fixed HTML document per status (plexsim had three of eight and answered `text/plain` sentences everywhere else, cycle 1's "deliberate deviation" re-justified three times without anyone reading the reference), the ENVELOPE's own refusals answered Python's DOCTYPE page, a path-segment id parses as a NUMBER, four paths bound it to int32, the request target must be printable ASCII with a readable escape, and two hub leaves never looked their id up. **Both hunts overturned a recorded belief.** The search hunt refuted cycle 35's own tier model one cycle after it landed and found the score is a TIER plus the row's `audienceRating`/10000 over 11,076 held-out pairs, a FOURTH tier at 0.53, and a base per row TYPE. The write hunt held out the list cycle 35 called "already correct" (§3.1 class F) and four of its seven items were open. |
| Cycle 35 | **35**. Cycle 35 got its hunts back after two cycles without them, and all three territories produced two apiece. The driver swept the ENCODED body, which no cycle had looked at: a real PMS ZEROES its gzip header's MTIME (so a compressed body is a pure function of the body, where plexsim's changed every second), and the `Range` grammar -- 172 cells -- had twelve of 43 header values wrong, two of them provokable by a real client. The write-frames hunt found that a library CREATE scans, skeleton and all, even over an empty folder, and that Ad Markers ships DISABLED. The search hunt found that both SCORERS had never used the matcher's own rule (class C), so a skipped middle word scored at the fuzzy floor. |
| Cycle 34 | **34**. Cycle 34 ran with NO hunts -- the same weekly rate limit that crippled cycle 33 killed both inside a minute (§10) -- so it is the driver's two territories. A fresh CLIENT-SESSION capture through plexlab, the founding instrument, gave three envelope findings no probe had sent (an EMPTY body orders its length first; the preflight echoes only the headers the client asked for; the 101 handshake's order, which cycle 33 had recorded as unmeasurable). And §3.3's oldest entry was settled by MAKING the row it asked for: two language-less audio tracks muxed onto plexscratch reproduce zen 19813 exactly, and they show that a decision reads EVERY audio stream and that cycle 32's language-less re-pick was per-user state the item row does not carry. `tests/test_decision.py` and a 2,028-cell corpus now keep the model honest. |
| Cycle 33 | **33**. Cycle 33 read the RESPONSE HEADER BLOCK -- ordered per response family, `X-Plex-Protocol` first and `Date` last, with a session cookie on the plug-in framework's `/system` paths, a connection state the preflight and the `/web` 302 never named, and a ranged container answering BOTH lengths -- refuted its one hunt finding's version framing with two controls, and came back with two large NEGATIVE results (`translations.json` reproduces the image's own gettext catalogs over 13,494 triples; cycle 32's decision model scores 312/312 on rows it never saw). Its hunts ran on a fallback model after a weekly rate limit killed both (§10). |
| Cycle 32 | **32**. Cycle 32 swept CONCURRENCY for the first time -- two requests released from one barrier -- and found plexsim building a write's answer outside the lock it wrote under; finished the title FOLD as a measured 100-character table on the sort-title, query and hub-search sides; replaced the two hard-coded direct-play names with a DECISION MODEL read out of the reference image's own profile files (exact on 1,871 of 1,872 measured cells); and found the JSON BYTE FORM in its revalidation step. It was begun by one session and finished by another (§7). |
| Cycle 31 | **31**. Cycle 31 held the WRITE surface out between 1.42.2 and 1.43.4 -- the verb table is where they differ and every grammar table is identical -- scored plexsim's own DERIVATIONS against zen's 14,433 rows (the artwork derivation has zero misses), and found that **the reference's own files and database are readable**, which settled two §3.3 entries and one rule the API cannot express. |
| Worktrees and stashes | none |
| Work half-done | **none — every Cycle-59 client-visible finding is implemented and proved fail-then-pass; the rest are in §8. Cycle 60 is next under the stopping rule (§1); the axis ledger in §7 "Cycle 64" names what it must cover** |

Coverage, measured from the repository rather than asserted:

| Cycle | Checks in the suites | `server.py` lines | Findings acted on |
|---|---|---|---|
| 64 | UI **1,932/1,932** with 12 fixture skips (serial, final tree, port 32596); every offline suite passes; LIVE navigation **25/25**, python-plexapi **29/38** (baseline) | 24,476 | **2 client-visible** in **2 fix commits**, both proved fail-then-pass; **2 recorded** |
| 63 | UI **1,930/1,930** with 12 fixture skips (serial, final tree, port 32596); every offline suite passes; LIVE navigation **25/25**, python-plexapi **29/38** (baseline) | 24,467 | **2 client-visible** in **2 fix commits**, both proved fail-then-pass; **2 recorded** (A's sorts, B's sidecar-burn decision) |
| 62 | UI **1,928/1,928** with 12 fixture skips (serial, final tree, port 32596); every offline suite passes; LIVE navigation **25/25**, python-plexapi **29/38** (baseline) | 24,467 | **2 client-visible** in **2 fix commits**, both proved fail-then-pass by `sdrive.py`; **0 recorded** |
| 61 | UI **1,926/1,926** with 12 fixture skips (serial, final tree, port 32596, after two `test:` commits re-basing six checks on this cycle's measurements); every offline suite passes; LIVE navigation **25/25**, python-plexapi **29/38** (baseline) | 24,454 | **7 client-visible** in **4 fix commits**, every one proved fail-then-pass by `sdrive.py`; **1 recorded**. **3 recorded beliefs refined** (cycle 58's 409 rule, cycle 60's "zen's token sees 20 settings", hunt B's per-row `Session` claim) |
| 60 | UI **1,921/1,921** with 12 fixture skips (serial, final tree, port 32596); every offline suite passes; LIVE navigation **25/25**, python-plexapi **29/38** (the placeholder-link baseline) | 24,434 | **7 client-visible** in **6 fix commits**, every one proved fail-then-pass by `sdrive.py`; **12 recorded** in §8. **2 recorded beliefs refined** (cycle 17's stand-in rule, cycle 59's overlay) |
| 59 | UI **1,914/1,914** with 12 fixture skips (serial run on the final tree, `RD_TOKEN=dummy`); decisions **2,340/2,340**, mediainfo **36/36**, catalog features, GDM **103/103**, heartbeat **13/13**, account state **15/15**, sessions, part bytes **12/12**, prefix **58/58**, regional **3/3** all pass | 24,348 | **16 client-visible** in **14 fix commits**, every one proved fail-then-pass against its own parent (13 by `sdrive.py`, the library gate by hand in the offline suite); **7 recorded** in §8. **3 recorded beliefs refined** (cycle 50's language-list injection, cycle 58's per-show gate, the translation table's version) |
| 58 | UI **1,894/1,894** with 12 fixture skips (the driver's serial run on the merged tree, `RD_TOKEN=dummy`); catalog, decisions, mediainfo, GDM, prefix, part bytes, TLS, heartbeat, account state, sessions and regional all pass; LIVE navigation **25/25**; python-plexapi **29/38**, the recorded placeholder-link baseline under `RD_TOKEN=dummy` (§4.5); `test_all.py` not run (its CLI takes no `--base`) | 24,117 | **9 client-visible** in **9 fix commits** (hunt B's Top Played fix plus a flattened-season follow-up) and 1 test commit; **5 recorded** in §8. **2 recorded beliefs refined** (the sub-minute resume delete, cycle 57's play-only shelf gate) |
| 57 | UI **1,881/1,881** with 12 fixture skips (the implementation agent's final run); catalog, decisions 2,340, mediainfo 36, GDM 103, prefix 58, part bytes 12, TLS 10, heartbeat 13, account state 15, sessions and regional all pass; LIVE navigation **25/25** (one check re-based: a never-watched show's OnDeck), python-plexapi **38/38**, byte/decode **5/5** | 23,886 | **9 client-visible** in **10 fix commits** (plus a `tagId` filter the collection shelf's key needs, and a flattened-season follow-up) and 2 test commits; **8 recorded** in §8. **1 recorded belief overturned** (cycle 41's pristine OnDeck) |
| 56 | UI **1,868/1,868** with 15 fixture skips (after one re-based check: cycle 20's null half now reads the rows never written); catalog **all pass**; regional **3/3**; decisions **2,340/2,340**; mediainfo **36/36**; GDM **103/103**; prefix **58/58**; part bytes **12/12**; TLS **10/10**; heartbeat **13/13**; account state **15/15**; sessions all pass; LIVE navigation **24/24**, python-plexapi **38/38**, byte/decode **5/5** | 23,664 | **6 client-visible** in **6 fix commits**, every one proved fail-then-pass; **18 malformed-only** recorded in §8. Denominators: 80 recorded client request shapes, 178 GET templates x 3 servers x 2 formats, 1,455 python-plexapi probes, 22 client writes with 336 reads each side; the repeat of cycles 54-55 (13,912 + 150,588 + 6,970 + 7,008 plexsim cells) and three widening grids (7,264 operator cells, 8,085 hub-page cells, 3,564 post-write cells a server) |
| 55 | UI **1,863/1,863** with 15 fixture skips (after one repair: the Related projection check now builds the shared tags the measured rule needs); catalog **all pass**; regional **3/3**; decisions **2,340/2,340**; mediainfo **36/36**; GDM **103/103**; prefix **58/58**; part bytes **12/12**; TLS **10/10**; heartbeat **13/13**; account state **15/15**; sessions all pass; hunt A's `regress55.py` **123/123**; LIVE catalog navigation **24/24**, python-plexapi **38/38**, byte/decode **5/5** | 23,586 | **37** in **37 fix commits** plus 2 test commits, every fix proved fail-then-pass. **5 recorded beliefs overturned**, **2 §3.3 entries CLOSED** (the per-library TV shelf's filtered form; `sectionId`'s two values), **1 reference crash** recorded in §9. Denominators: 366 SQL reads with a verdict each, 147,384 name-level value cells, 7,008 operator cells a server, the 13,616-cell repeat of cycle 54's sweep. The implementation of Territory A was DELEGATED to an agent working in the cycle worktree under sdrive, then re-verified by the driver |
| 54 | UI **1,830/1,830** with 14 fixture skips (after two repairs of this cycle's own); catalog **all pass**; regional prefs **3/3, 1,122/1,122**; decisions **2,340/2,340**; mediainfo **36/36** (+17 shallow fixtures); GDM **103/103**; prefix **58/58**; part bytes **12/12**; TLS **10/10**; heartbeat **13/13**; account state **15/15**; sessions all pass; LIVE catalog navigation **24/24**, python-plexapi **38/38**, byte/decode **5/5** | 23,094 | **22** in **22 commits**, every one proved fail-then-pass. **4 recorded beliefs overturned**, **2 §3.3 entries CLOSED** (the legacy agents' load order is the filesystem's; the section key ignores `Host`), **1 regression the cycle caught in its own patch** (the media axes lost their `type=` blindness) and **1 check of its own starved by the suite** (the season read off a flattened episode). Denominators: 1,018 parameter names x 94 families (332,304 cells on zen), 707 SQL statements, 17 containers, 9,287 episodes and 3,887 movies fitted cell by cell. **1 hunter family bounded** (the tagging join) |
| 53 | UI **1,811/1,811** with 14 fixture skips; catalog **all pass**; regional prefs **3/3, 1,122/1,122**; decisions **2,340/2,340**; mediainfo **35/35**; GDM **103/103**; prefix **58/58**; part bytes **11/11**; heartbeat **13/13**; account state **15/15**; sessions all pass | 22,733 | **19** in **21 commits**, every one proved fail-then-pass. **2 recorded beliefs corrected** (cycle 33's "no `Server:` banner anywhere on the surface" — the framework's own error page carries one; cycle 18's "the only 405 on the surface", which names two subtrees) and **1 inherited §8 bound retired** (the legacy agents, whose sibling half cycle 52 had already retired). Denominators: 289 framework source files and 71 schema tables read out of the image, 130 request families x 3 servers in the JSON scalar-type census (zero differences), 742 media-flag PNGs, 624 substitution regexes, 11 legacy agents x 2 references. **6 existing checks re-measured** where they encoded plexsim's own four-agent catalogue, and **1 check of the cycle's own starved by the suite** and repaired — it keyed on the candidate list's `size` where the measured rule is the AGENT's candidate |
| 52 | UI **1,785/1,785** with 14 fixture skips; catalog **54/54**; regional prefs **3/3, 1,122/1,122**; decisions **2,340/2,340**; mediainfo **35/35**; GDM **103/103**; prefix **58/58**; part bytes **11/11**; heartbeat **13/13**; account state **15/15**; sessions all pass | 22,208 | **24** in **26 commits**, every one proved fail-then-pass. **2 recorded beliefs overturned** (cycle 29's seven `/:/resources` names; §8's inherited `/:/plugins` bound) and cycle 18's "the only 405 on the surface" widened to two framework subtrees. Denominators: 190 single-segment dispatcher prefixes x 4 hosts, 131 names swept unauthenticated, 70 bundled resources x 3 hosts, 658 route templates x 3 hosts, 39 Categories tiles, 12 framework kwarg cells. **1 check of the cycle's own starved by the suite** and repaired; **1 model fitted and then corrected from the reference's own Python** |
| 51 | UI **1,756/1,756** with 14 fixture skips; catalog **54/54**; regional prefs **3/3, 1,122/1,122**; navigation **28/28**; decisions **2,340/2,340**; mediainfo **35/35**; GDM **103/103**; prefix **58/58**; part bytes **11/11**; heartbeat **13/13**; account state **15/15**; TLS **10/10** under LibreSSL and OpenSSL | 21,537 | **0.** Three independent reports end `NO FINDINGS`. Fresh finite repeats include 111 safe claim families / 444 GETs, 173 dual-format pairs / 2,540 nodes / 13,072 scalar occurrences, nine system-writer routes, 92 numeric-platform cells plus four repair controls, every raw transport boundary and eight GDM lifecycle hooks. Retained 1,776-request claim, 692-pair safe dual-format, 1,331-pair complete dual-format, 421-pair catalog and 519-flow official-client denominators are all explicitly reconciled |
| 50 | same final suites; GDM grew to **103/103**, catalog to **54/54**, plus regional and prefix suites | 21,537 | Recovered and closed Territory A **23/23**, Territory B **8/8**, Territory C **10/10**, including **18 entries absent from the seven-commit recovered branch**, one lost measured patch, three missing regressions and the Cycle-49 numeric-platform deferred entry. Safe claim replay **1,776 requests**; dual format **692 pairs / 9,206 nodes / 0 mismatches**; regional **1,122 cells**; prefix **58 paths / 174 reference GETs**. The expanded `/connections` matrix is an explicit terminal bound, not a guessed implementation |
| 49 | UI **1,685/1,685** with 14 fixture skips; decisions **2,340/2,340**; mediainfo **35/35**; catalog features; part bytes 11/11; heartbeat 13/13; account state 15/15; **new** `test_gdm` **101/101** | 20,657 | **26 findings in 21 commits**, every one proved fail-then-pass. **8 recorded beliefs overturned** (cycle 35's ad-marker row as class E, cycle 15's `/playlists/sorts`, cycle 23's Duplicates row, cycle 37's body rule, the `authToken` claim, the emptied collection, the `transcoderVideoRemuxOnly` version framing, and `/servers`' own address). **1 new protocol** (GDM: two UDP ports, a multicast group, an 11-byte prefix trigger, a 5 s search and the `/servers` list it populates), **1 new offline suite**, **1 broken instrument found and repaired**, **4 suite checks repaired** where they read state the suite takes away, and **6 findings recorded rather than emulated** |
| 48 | UI **1,629/1,629**; decisions **2,340/2,340**; mediainfo **35/35**; catalog features; part bytes **11/11**; heartbeat **13/13**; account state **15/15** | 20,094 | **0.** Exact stopping repeats: query options **26,448 cells**; headers **24,156 logical / 16,842 physical requests**; outbound **1,829 exchanges / 1,620 reads**; durable history **745 rows including all 24 Cycle-41 fixes**; current client **205 assets / 9,314,084 bytes**; advertisements **24,452**; controls **49 / 39 normalized families**. Every territory report ends `no findings` |
| 47 | same final suites; the pre-repair full UI exposed six false late failures, while fresh discriminators passed | 20,094 | **2**: one product finding (claimed user/features renew daily, user then features about 11 s later) and one audit-harness finding (six option regressions consumed state mutated earlier in the suite). The product repair is `3cff10e`; the final pristine-response harness repair is `768377a` |
| 46 | UI grew from 1,612 to **1,629**; decisions 2,340; mediainfo 35; catalog features; part bytes 11; heartbeat/account-state suites added | 20,086 before the Cycle-47 renewal | **19 product findings**: **14** query-option projection/source fixes, **2** request-header fixes, **1** provider availability fix and **2** outbound account-state fixes. Query denominator 26,448 cells; header denominator 8,052 candidates plus controls; outbound prefix 1,665 exchanges. One deliberate query bound (`includeBandwidths`) and three Desktop runtime cells were explicitly retained |
| 45 | UI **1,612** (+26); decisions 2,340 cells; mediainfo 35; catalog features 39; part bytes 10; heartbeat 11 | 19,780 | **31** in **27 commits**, every one proved fail-then-pass. **4 recorded beliefs overturned** (`includeAlternates`'s version framing, the minute gate on completion, the per-row `file=` refusal, the per-column axis bucket), **2 regressions the cycle's own suites caught in its own patches**, **1 GET that mints rows** re-measured on a disposable copy, **1 reference crash** found and isolated, and a name-space sweep of 8,240 cells built from the references' own vocabulary |
| 44 | UI 1586; decisions **2,340** cells (+234); mediainfo **35** (+1); catalog features **39** (+4); **new** `test_part_bytes` 9 and `test_heartbeat` 11 | 18,782 | **20** in **16 commits**, every one proved fail-then-pass. **2 recorded beliefs overturned** (the 416's connection state, the `.webm` container), **1 suite gap closed** (`test_mediainfo` never compared the container, so the corpus held the refutation from cycle 10), **2 per-version splits found**, **1 new offline instrument** (`tests/test_part_bytes.py` stands up its own CDN and Real-Debrid stub, so the byte path needs no credential), and the server-to-plex.tv axis swept for the first time |
| post-43 media refresh | conformance 38/38; full byte/decode 5/5; navigation 24/24 | 18,652 | **1 test-harness repair**: the newly reachable MP4 proved `conformance.py` hard-coded Matroska magic after selecting a non-Matroska Part; no server mismatch |
| 43 | 1582 (+2,106 decision cells; mediainfo 34; catalog features 35; focused sessions) | 18,652 | **0**. Three independent clean axes: the complete current public Plex Web 4.160.0 app build (205 JS, 16 CSS, 207/207 SRI; 177 newly obtained JS assets with zero new PMS call sites), 332/332 historical atoms with 24/24 cross-fix audits, and the 49/1,221/47/339/45/45 advertised-control graph with 39 exact witnesses and six explicit bounds; every report ends exactly `no findings` |
| 42 | 1582 (+2,106 decision cells; mediainfo 34; catalog features 35; focused sessions) | 18,652 | **0**. Clean replay: 332/332 historical atoms, 24/24 previous fixes, 49/1,221/47/339/45/45 advertised-control graph, both reports exactly `no findings`; the first stopping cycle, reopened at the user's request |
| 41 | 1582 (+2,106 cells in `test_decision`; `test_mediainfo` 34; `test_catalog_features` 35; plus the focused session suite) | 18,652 | **24** in **24 fix commits** from a finite 332-atom historical census; 49 advertisement-source families, 1,221 normalized advertisements, 47 exact predicates, 339 controls and 45 action/readback pairs independently closed with **0** advertisement findings; 51 historical atoms remain explicitly bounded by capability, state or instrument and **0** are unbounded/actionable |
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
| 26 | 1421 (`test_mediainfo` unchanged at 31) | 15,259 | **21** (of 19 reported across two hunts plus 2 the driver found while re-measuring them; **8 recorded beliefs overturned**, 2 hunter claims corrected on re-measurement, 3 suite checks found to assert a shape no real PMS answers, 1 §3.3 entry CLOSED by revalidation, 4 findings recorded rather than emulated) |
| 27 | 1451 (`test_mediainfo` unchanged at 31) | 15,701 | **22** (10 from the write hunt, 3 from the read hunt and 9 the driver found in the revalidation and the envelope sweep; **6 recorded beliefs overturned**, 4 §3.3 entries CLOSED, 3 suite checks updated to what was re-measured, 2 regressions the cycle's own suite caught in its own patches, 7 findings recorded rather than emulated) |
| 28 | 1472 (`test_mediainfo` unchanged at 31) | 16,155 | **19** (12 from the two hunts' reports as re-measured and 7 the driver found while re-measuring them; **8 recorded beliefs overturned**, 1 of them a per-VERSION split only plexscratch could show, 2 suite-check repairs -- one belief re-measured and four checks refitted to make the state they need -- 5 findings recorded rather than emulated) |
| 40 | 1588 (+2,106 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 18,452 | **2** (one from the complete inward route/evidence census and one from the recovered official-client provider settings surface, in **2 fix commits**; 867 source branches, 308 normalized families, 526 verb cells and 3,080 protocol cells enumerated; the complete local 4.147.1 bundle and surviving 4.160.0 cache subset classified; the absent 176 mapped 4.160.0 chunks remain an explicit coverage limit) |
| 39 | 1586 (+2,106 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 18,322 | **2** (both from the search residual hunt, in **2 fix commits**; the official-client exact replay reported no findings; **2 §3.3 entries CLOSED** -- hub score-fold case handling and faithful tag ranks -- with 659 Unicode cells and a 697,334-query bounded vocabulary comparison; 1 unsafe first client capture discarded and repeated behind a pre-navigation socket/media gate) |
| 38 | 1581 (+2,106 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 18,270 | **4** (one request-envelope finding from the concurrency hunt and three search findings from static recovery, in **4 fix commits**; **2 §3.3 entries CLOSED** -- spelling rebuild schedule and final search ordering -- a broad concurrency matrix otherwise matched, and the storage-failure axis was measured but deliberately not emulated because plexsim owns none of those persistent backends) |
| 37 | 1577 (+2,106 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 18,055 | **7** (4 from the raw request-envelope sweep, 2 from the catalog/reachability hunt and 1 from the spellfix hunt, in **7 fix commits**; **2 recorded beliefs overturned**, **3 §3.3 entries CLOSED** -- fuzzy membership/pruning, multipart reachability and the clip surface -- one existing UI check pair repaired because it still asserted that fuzzy search was absent, 1 fixture-only Dolby Vision limitation made reachable, 2 negative results, and 2 unresolved details retained) |
| 36 | 1565 (+2,106 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 17,700 | **19** (8 the driver found in the refusal surface, 6 from the search hunt and 5 from the write hunt, in **20 commits**; **4 recorded beliefs overturned** -- cycle 35's `word_tier`, cycle 19's scoped scan, cycle 1's text/plain refusal and cycle 22's empty collection leaves -- **2 §3.3 entries CLOSED** (`video.separateFields` and the DoVi dual-layer disagreement, both by MAKING the row), 1 suite check found to encode half a rule and repaired, 1 regression the suite caught in the cycle's own patch, 8 findings recorded rather than emulated, 3 negative results) |
| 35 | 1541 (+1,950 cells in `test_decision`; `test_mediainfo` unchanged at 31) | 17,342 | **6** (2 from the write-frames hunt, 2 from the search-and-decision hunt and 2 the driver found in the encoded-body territory, in **6 fix commits**; **1 recorded belief corrected** (a create over an empty folder), 1 §3.3 half CLOSED, 4 findings recorded rather than emulated, 5 negative results) |
| 34 | 1536 (+1,872 cells in the new `test_decision`; `test_mediainfo` unchanged at 31) | 17,265 | **5** (no hunts -- both died on the weekly rate limit; 3 from a fresh client-session capture and 2 from the made language-less row, in **4 fix commits and 2 test commits**; **1 recorded belief overturned** and the §3.3 entry behind it CLOSED, 1 sidecar fixture found to be a projection rather than a capture, 1 suite check starved by this cycle's own earlier commit and repaired, 2 findings recorded rather than emulated) |
| 33 | 1533 (`test_mediainfo` unchanged at 31) | 17,230 | **6** (5 the driver found in the header-block sweep its own territory became when the hunts were rate-limited, and 1 from the write-frames hunt -- as re-measured, which changed what it was; in **7 fix commits and 1 test commit**, one fix repairing a regression the cycle's own first patch caused and one correcting its own family table, both caught by the suite; **3 recorded beliefs overturned**, 1 hunt finding REFRAMED on its controls, 1 §3.3 entry closed and 2 narrowed, 3 findings recorded rather than emulated) |
| 32 | 1526 (`test_mediainfo` unchanged at 31) | 16,971 | **9** (3 from the concurrency hunt, 3 from the fold-and-decision hunt, and **3 the driver found** -- the sort-title and hub-search halves of the fold, and the JSON byte form -- in **9 fix commits and 1 test commit**; **9 recorded beliefs overturned**, **4 §3.3 entries CLOSED**, 1 of the cycle's own fix comments refuted by re-running its race on its own tree, 2 of its checks refitted to the state the suite leaves them, 5 findings recorded rather than emulated) |
| 31 | 1515 (`test_mediainfo` unchanged at 31) | 16,866 | **22** (9 from the write-surface hunt, 7 from the derivation hunt, 6 the driver found before either reported, of which 22 were implemented across **21 fix commits** -- three of those commits CORRECTING an earlier patch of the same cycle, each caught by the suite rather than by review; **9 recorded beliefs overturned**, **6 §3.3 entries CLOSED**, 2 hunt findings recorded rather than built (a partial fold table and an episode-still case that did not reproduce over 1,500 live rows), 1 contradiction left open, and 8 existing checks rewritten where they encoded 1.43.4's verb table) |
| 30 | 1498 (`test_mediainfo` unchanged at 31) | 16,697 | **20** (5 from the new 1.42.2 instrument, 3 from the same-request-twice hunt, 6 from the notification hunt, 1 the write hunt's aside that only the new instrument could settle, and **5 the driver found in the revalidation step and in its own patches**; **8 recorded beliefs overturned**, one of them a sample in `docs/findings.md` that was plexsim's own answer read back as the reference's -- §3.1 now has a **seventh** failure class for that -- **4 §3.3 entries CLOSED by the new instrument**, 2 suite-check repairs, 6 findings recorded rather than emulated) |
| 29 | 1490 (`test_mediainfo` unchanged at 31) | 16,501 | **20** (11 from the read hunt on response bodies, 4 from the write hunt on extras plus its clip-row build spec, and **7 the driver found in the revalidation step and in its own patches** -- the first cycle where that step out-produced a hunt; **10 recorded beliefs overturned**, 2 stale §3.3/§8 entries CLOSED as already built, 1 regression the cycle caught inside its own patch, 3 suite-check repairs (twelve checks refitted to make the state the suite takes away), 8 findings recorded rather than emulated) |

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

**Where the curve stands after cycle 26, and what it means for the stopping rule.** The
loop stops when a cycle reports no findings, so the trend in the last column is the thing to
watch — and it now reads **24, 16, 9, 6, 21, 21**. Cycle 26 is the second cycle in a row to
sweep a dimension nobody had swept, and the second in a row to come back with a double-figure
number from it. Cycle 25 broke the fall; cycle 26 confirms why. That is not the surface
refilling.
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
  (Cycle 26 closed the frame-cascade half of that: driven side by side against the copy, all
  four list sequences reproduce frame for frame. What is left of the butler is which tasks are
  DUE, which no row of `GET /butler` exposes at all.)

So the honest reading is that the loop has not converged. Cycle 24 read the fall as "moved
from finding differences to owing implementations", and cycle 25 tested that reading directly:
it drew the last fully measured item off the backlog (the unmatch cascades, `pixelAspectRatio`,
the search-row lead, the `/match` verb table) AND ran two hunts, and the hunts alone found
thirteen. **The backlog was real and so was the surface.** What is left in §3.3 after this
cycle is 45 entries, most of them single-witness or instrument-limited rather than
unimplemented.

**And cycle 26 found the largest functional gap since the build-out, in the surface everybody
assumed worked.** plexsim refused every decision that did not carry `hasMDE=1` — so every
client that is not Plex Web asked whether it could play the catalog and was told it could not,
by a server whose entire purpose is direct play. Nothing in the suites caught it because every
check was written by someone sending Plex Web's own parameters. The lesson is §10's, in a new
form: a surface that only one client's parameters have ever exercised has not been tested, and
the playback path had never been walked as a client walks it. Three more of the cycle's
findings are the same shape — a decision's validation refusing what a real PMS accepts, the
statistics endpoints answering a series for a window they hold no data for, and
`/status/sessions` answering nine paths that are 404s.

Suite results at the head of cycle 49, run 2026-09-18 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1685 PASS / 0 FAIL / 14 skipped** | +56 checks. SIX existing checks had to be repaired: four of this cycle's own read state the suite takes away three thousand lines later, one was cycle 23's Duplicates belief this cycle refuted, and one -- cycle 33's `/web` 302 -- had never tested what it claimed, because `urllib` FOLLOWS the redirect and it was reading `/web/index.html`'s headers instead |
| `tests/test_gdm.py` | **101/101** | NEW. `gdm.py` against the GDM replies of four real servers, a 68-cell trigger bisection, and an end-to-end drive of the real responder over real sockets including multicast. No network |
| `tests/test_part_bytes.py` | **11/11** | unchanged, and the strongest evidence the new request reader did not break the byte path |
| `tests/test_decision.py` | **2340/2340 cells exact** | unchanged |
| `tests/test_mediainfo.py` | **35/35** | unchanged |
| `tests/test_catalog_features.py` | all pass | unchanged |
| `tests/test_heartbeat.py` | **13/13** | unchanged |
| `tests/test_accountstate.py` | **15/15** | unchanged |
| `tests/test_sessions.py` | all pass | unchanged |
| `tests/test_navigation.py` | **24/24** on the LIVE catalog | |
| `tests/conformance.py` (python-plexapi) | **38/38** on the LIVE catalog | |
| `tests/test_all.py` with decode | **5/5 served** | every playable part passed queue, decision, both transport legs and real frame decode -- after a cycle that rewrote the request path |

The protected reference snapshot is byte-identical before and after. kids' device row 1 was
REPAIRED during this cycle (§9) and is the one deliberate change to a reference's state; zen was
not touched. Both writable copies are healthy; the hunts' writes are declared in §7. Exact-value
secret scans over the whole branch diff are clean.

Suite results at the head of cycle 44, run 2026-09-17 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1585 PASS / 0 FAIL / 14 skipped** | +3 checks this cycle; four codec-axis checks were REWRITTEN, having encoded 1.43.4's surface |
| `tests/test_part_bytes.py` | **10/10** | NEW. Stands up a local CDN and a one-route Real-Debrid stub behind `RD_API` and drives the real handler — the byte path needs no credential |
| `tests/test_heartbeat.py` | **11/11** | NEW. `announce()` against the three `PUT /devices/{machineIdentifier}` exchanges a real PMS made, recorded in `tests/fixtures/outbound-announce.json`. No network |
| `tests/test_decision.py` | **2340/2340 cells exact** | +234 cells: three zen rows whose selected embedded subtitle is not the first |
| `tests/test_mediainfo.py` | **35/35** | +1: the Media's and Part's `container`, which this suite had never compared |
| `tests/test_catalog_features.py` | **39/39** | +4: the TMDB pipeline's season year and season/episode `Guid`, and the load-time correction |
| `tests/test_sessions.py` | **all pass** | |
| `tests/test_navigation.py` | **24/24** on the LIVE catalog | |
| `tests/conformance.py` (python-plexapi) | **38/38** on the LIVE catalog | |
| `tests/test_all.py` with decode | **5/5 served** | every playable part passed queue, decision, both transport legs and real frame decode |

The protected reference snapshot is byte-identical before and after at
`64579ece7b9f77a2914bc07f5f3aef36f71bf2ddf0c57aec68b428819386e68a`. Neither writable copy was
reset; hunt B declared two synthetic media files added to `plexscratch`/`plexscratch42` and the
sections refreshed. Exact-value secret scans over the whole branch diff are clean; the two strings
the pattern scanner flags are the pre-existing `…/d/XXXX` placeholder in `debrid.py`'s docstring
and this cycle's literal `…/d/PARTBYTESTEST`, which the local stub accepts regardless.

Post-Cycle-43 live-media refresh, run 2026-09-17 against the private gitignored `catalog.json`:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_navigation.py` | **24/24** | one explicit multiplicity skip; the refreshed catalog has a measured movie/show/season/episode hierarchy |
| `tests/conformance.py` (python-plexapi) | **38/38** | prefix bytes, true size, media type and mid-file seek all pass over a fresh account-owned RD link |
| `tests/test_all.py` with decode enabled | **5/5 served** | every playable part passed queue, decision, both harness transport legs against the fresh local proxy, and real frame decode; Big Buck Bunny is MP4/H.264/AAC and passes the Plex Web direct-play gate |

The private catalog now holds seven metadata rows and five playable parts backed by freshly sourced
account-owned links. A valid TMDB key was used while building it, so the two Mentalist leaves form
a show and season rather than flattening into movies. The checked-in UI fixture intentionally keeps
redacted placeholder links; it is still for protocol/write coverage, not live bytes. The first
working MP4 exposed one harness assumption: `conformance.py` selected the browser-direct-play row
and then hard-coded Matroska magic. Its assertion is now container-specific and the target selection
explicitly prefers MP4/H.264/AAC before falling back to another H.264 row.

Suite results at the head of cycle 42, run 2026-09-17 against frozen merged head `f8cd6c2`:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_catalog_features.py` | **35/35** | includes the exhaustive title-fold and all Cycle-41 focused catalog regressions |
| `tests/test_mediainfo.py` | **34/34** | |
| `tests/test_decision.py` | **2106/2106 cells exact** | |
| `tests/test_sessions.py` | **all pass** | |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1582 PASS / 0 FAIL / 14 skipped** | independently passed in root and in both census verification paths |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | all nine failures are the fixture's unusable RD links |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the dead-link baseline; all eleven queues and decisions passed |

Territory A rebuilt 332/332 atoms, passed 24/24 cross-fix audits, 24 current safe GET cells and its
focused bucket discriminator. Territory B recollected every source and rebuilt the complete
49/1,221/47/339/45/45 graph, then traced 7,090 requests / 1,363 distinct method-path pairs. Both
reports end `no findings`; both protected snapshots are byte-identical and both exact-value secret
scans have zero hits. This is the stopping cycle.

Suite results at the head of cycle 41, run 2026-09-17 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_catalog_features.py` | **35/35** | includes the exhaustive 7,531-scalar title-fold assertion, multi-Media selection, mixed folders, duration sort, show merge and Recent TV cap/roll-up |
| `tests/test_mediainfo.py` | **34/34** | adds exact 480-width, anamorphic and visually-impaired subtitle boundaries |
| `tests/test_decision.py` | **2106/2106 cells exact** | unchanged corpus, with the multi-Media selector independently covered in catalog features |
| `tests/test_sessions.py` | **all pass** | focused fake-clock proof of the global 30-second reaper and 180-second cutoff |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1582 PASS / 0 FAIL / 14 skipped** | all Cycle-41 protocol, write and localization regressions; independently repeated twice on fresh processes |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | all nine failures are the fixture's unusable RD links |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded dead account-scoped links; all eleven queues and decisions passed |

The historical axis retains 487 kids, 488 zen, 447 frozen and 8,674 cells on each writable copy.
Its final protected-reference snapshot is byte-identical, both copies were restored and released,
and a five-value secret scan found zero occurrences. The advertisement axis independently reran the
UI postcondition suite at 1563/1563 on its frozen disposable server before the fixes landed.

Suite results at the head of cycle 40, run 2026-09-17 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_catalog_features.py` | **25/25** | unchanged |
| `tests/test_mediainfo.py` | **31/31** | unchanged |
| `tests/test_decision.py` | **2106/2106 cells exact** | unchanged |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1563 PASS / 0 FAIL / 13 skipped** | adds exact legacy URL-lookup GET/XML/HEAD/gzip coverage and the metadata-agent group lifecycle |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | all nine failures are the fixture's unusable RD links |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded dead account-scoped links; all eleven queues and decisions passed |

The first navigation and conformance commands used the wrong local invocation (`--catalog` omitted;
Xcode Python without `plexapi`); both were rerun with the fixture and the repository's mise Python.
The first UI run reached more than 1,500 checks before exposing a literal `%20` in the new test's
format string; that test-only error was repaired inside the provider finding's atomic commit, and
the fresh authoritative run is the 1563/1563 result above.

Suite results at the head of cycle 39, run 2026-09-17 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_catalog_features.py` | **25/25** | adds pass-specific Unicode score tiers, full row-source vocabulary extraction, exact static vocabulary and GUID-rank protection |
| `tests/test_mediainfo.py` | **31/31** | unchanged |
| `tests/test_decision.py` | **2106/2106 cells exact** | unchanged |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1561 PASS / 0 FAIL / 13 skipped** | unchanged count; the new search checks are in the focused suite |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline; all nine are the fixture's unusable RD link |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause; play queues and all eleven decisions passed |

Suite results at the head of cycle 38, run 2026-09-17 against the cycle tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_catalog_features.py` | **20/20** | adds hidden-precision hub ordering, flat-search bucket order and spelling rebuild trigger coverage |
| `tests/test_mediainfo.py` | **31/31** | unchanged |
| `tests/test_decision.py` | **2106/2106 cells exact** | unchanged |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1561 PASS / 0 FAIL / 13 skipped** | includes the raw-socket query-only-write/body-dependent-upload discriminator |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline; all nine are the fixture's unusable RD link |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause (account-scoped/dead RD links); play queues and all eleven decisions passed |

Suite results at the head of cycle 36, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: the interlaced row the cycle MADE is derived correctly by the rule already there |
| `tests/test_decision.py` | **2106/2106 cells exact** | zen:1120 (Dolby Vision profile 5) and zen:1098 (two video Streams, the corpus's first `dca-ma`) at 78 profiles each. Against the model as it stood the same corpus scores **2069** |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1556 PASS / 0 FAIL / 13 skipped** | ~16 minutes. THREE existing checks had to be repaired: two were starved by the unmatch three thousand lines above them and one encoded half a rule (an unmatched movie's prefs block is EMPTY, so every write to it is a 400) |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause (dead RD links) |

Suite results at the head of cycle 35, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle |
| `tests/test_decision.py` | **1950/1950 cells exact** | zen's row 3831 added: the corpus's first `list=` limitation and its first `has64bitOffsets` true |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1541 PASS / 0 FAIL / 10 skipped** | ~13 minutes |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause (dead RD links) |

Suite results at the head of cycle 34, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle |
| `tests/test_decision.py` | **1872/1872 cells exact** | NEW this cycle: `decision.py` against the recorded corpus, outcome and refusal text. The 156 cells the reference answers before a client has chosen an audio track are counted separately and never fail the suite. Against the model as it stood the same corpus scores **1826** |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1536 PASS / 0 FAIL / 10 skipped** | ~13 minutes. The run before the repair was 1535/1536: this cycle's own empty-body check left a lower-case summary on the fixture's first movie and starved cycle 20's fold check three thousand lines below |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause (dead RD links) |

Suite results at the head of cycle 33, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1533 PASS / 0 FAIL / 10 skipped** | ~14 minutes. An earlier run of this cycle's tree was 1527/1529: the header-order patch had broken every malformed-request-line refusal, which only the raw-socket checks could see |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded baseline cause (dead RD links), byte-identical to the pre-cycle tree |

Suite results at the head of cycle 32, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1526 PASS / 0 FAIL / 10 skipped** | ~14 minutes, including two 150-pair barrier race checks. The first full run was 1523/1524 with 11 skipped: two of the cycle's own checks had proved on a fresh fixture and then read the state the suite leaves (§10) |
| `tests/test_navigation.py` | **28/28** on the UI fixture | |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline (cycle 31 settled why) |
| `tests/test_all.py --skip-decode` | **0/11 served** on the UI fixture | the recorded cause -- every byte read a 502 from a dead RD link -- and byte-identical to the pre-cycle tree's output; the decision step itself PASSES (`directplay`) |

Suite results at the head of cycle 31, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1515 PASS / 0 FAIL / 10 skipped** | ~13 minutes. Eight existing checks were rewritten this cycle where they encoded 1.43.4's verb table |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server, and **never two suites at once** -- see section 10 |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline, and cycle 31 settled why: see the row in §2's table -- the nine cannot be made to pass |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline, same cause |

Suite results at the head of cycle 30, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1498 PASS / 0 FAIL / 10 skipped** | ~13 minutes, and longer than it looks: this cycle's checks include a 25-second ping capture with a pong to each ping and a 260-write starvation probe. The 10 skips are the fixture's own limits |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server, and **never two suites at once** -- see section 10 |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline. Cycle 31 settled WHY, and it is structural: all nine are downstream of ONE part read, which fails with `RD_TOKEN=dummy` (Real-Debrid answers **401**, so the harness's token is what the suite actually hits) and fails with a real token too, because the checked-in fixture's link is the literal placeholder `…/d/EXAMPLELINKID00000` and RD answers **`unavailable_file`, error_code 24**. A working `/d/` link is a credential the repo may never hold, so these nine cannot be made to pass |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline, and the same cause as the row above -- not "links that went dead" |

Suite results at the head of cycle 29, run 2026-09-16 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1490 PASS / 0 FAIL / 10 skipped** | ~15 minutes. The 10 skips are the fixture's own limits; **twelve** of this cycle's own checks had to be refitted to make the state the suite takes away from them, in three rounds -- see section 10 |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server, and **never two suites at once** -- see section 10 |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline, unchanged |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline (the catalog's RD links are account-scoped and dead) |

Suite results at the head of cycle 28, run 2026-09-15 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1472 PASS / 0 FAIL / 10 skipped** | ~14 minutes. The 10 skips are the fixture's own limits (no TMDB key, dead RD links, two movies left by the end); cycle 28's own three skips were REFITTED to make the state they need -- see section 10 |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server, and **never two suites at once** -- see section 10 |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline, unchanged: cycle 27 settled that all nine are the fixture's dead Real-Debrid links |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline (the catalog's RD links are account-scoped and dead) |

Suite results at the head of cycle 27, run 2026-09-15 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1451 PASS / 0 FAIL / 10 skipped** | ~25 minutes, most of it the butler's per-task listener waits and the cycle-26 session-TTL sleep |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server, and **never two suites at once** -- see section 10 |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline, and cycle 27 settled what the nine are: all of them the fixture's dead Real-Debrid links, none a plexapi limitation and none a server difference |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline (the catalog's RD links are account-scoped and dead) |

Suite results at the head of cycle 26, run 2026-09-15 against the merged tree:

| Suite | Result | Notes |
|---|---|---|
| `tests/test_mediainfo.py` | **31/31** | unchanged this cycle: no `mediainfo` finding |
| `tests/fixtures/media-resolutions.json` | **194 shapes** | unchanged this cycle |
| `tests/test_ui_surface.py` on `tests/catalog_ui.json` | **1421 PASS / 0 FAIL / 9 skipped** | the cycle's own checks add ~95 s of WAITING: the session cutoff can only be measured by letting a session age |
| `tests/test_navigation.py` | **28/28** on the UI fixture | run each against its OWN server: pointed at the one the UI suite is writing to, it reports twelve failures that are not there |
| `tests/conformance.py` (python-plexapi) | **29/38** | the recorded baseline |
| `tests/test_all.py --skip-decode` | **0/3 served** | the recorded baseline (the catalog's RD links are account-scoped and dead) |

---

## 3. The revalidation register

**This is the most important section in the file.** 80 recorded rules have been overturned
across eighteen cycles. They did not fail randomly: they failed in seven ways, and the
same six keep recurring. Learn the classes, then check the items.

### 3.1 The seven ways a measurement in this project goes wrong

| # | Class | What it looks like | Canonical case |
|---|---|---|---|
| **A** | **Measured without the parameter the client actually sends** | The recording is real, the request is not the client's | The Seasons block: a real PMS's `/children` payload was substituted wholesale and the bug survived, because the recording was taken without the paging params the client sends — so it was missing the same `totalSize`. This is the reason the project's one rule exists. |
| **B** | **Measured on data too thin to show the rule** | Everything agrees, because the sample cannot disagree | `/library/metadata/{id}/related` was recorded as "always empty" from four lab items that have no related titles. |
| **C** | **Rule recorded correctly, implemented as a proxy** | findings.md is right and the server is wrong | The play-queue add: cycle 4 measured "after the block of already-added items" and implemented `id > selected`, which every original member also satisfies. Wrong for **seven cycles** while the docs said fixed. |
| **D** | **A threshold or constant inferred from the sample** | A number nobody ever measured | The gzip floor: "860 is the smallest compressed answer measured off it" — a real PMS compresses an 83-byte body into 90 bytes and sends it. |
| **E** | **An instrument limit mistaken for a server rule** | The copy cannot do it, so the rule is written as if the server cannot | "POST subtitles is a 500 on the copy with nothing in its log" — the client's own request is a clean 200. |
| **F** | **A hunter's report taken at face value** | Plausible, specific, and not reproducible | Roughly one finding per cycle is refuted on re-measurement. `/system/agents` "has the wrong shape" was the modern rows matching exactly. |
| **G** | **plexsim's own answer recorded as the reference's** | A verbatim sample in the documents that nobody ever re-read | Cycle 30: the 401 printed in `docs/findings.md` since cycle 16 carried `Connection: close`, which is plexsim's answer -- both references keep the socket alive and it really is reusable. A pasted response is a measurement only if it came off the reference, and once it is in the document it reads like one for ever. Capture into a file and quote the file. |

**How to use this table.** When you write a rule, name the class it could belong to and say why
it does not. When you read a rule, ask which class it could be. The re-measurement that costs
ten minutes now has cost whole cycles when skipped.

### 3.2 Beliefs this project recorded and later overturned

Every one of these was in `docs/findings.md` as a measurement. Sorted by the class above.

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| **An item's Advanced language enums are translated for every language `translations.json` lacks** (cycle 50) | Only the three LANGUAGE-LIST enums are; the injection keyed on the default's label caught `-1:Library default|0:Show|1:Hide`, `showOrdering` and every other library-default enum, so under `en` a show's Seasons dropdown offered 46 languages | C | cycle 59 |
| **A stream's extended title decides the language part's omission on the ENGLISH name and renders the English one** (cycle 17) | It decides on the STORED name and renders the LOCALISED one: zen's track titled "French" reads `French (French SRT)` under `en` and `www.TamilRockers.com (Englisch Erzwungen SRT)` under `de`. Held out over 23,832 cells | B | cycle 60 |
| **Overlaying the other version's changed cells makes the table 1.42.2's** (cycle 59) | Where 1.42.2 answers ENGLISH the old translation stayed -- 414 cells. The capture has to record unchanged cells too | C | cycle 60 |
| **A section-path collection rename is a 409 onto a tag some row still carries** (cycle 58) | Onto ANY name in the collection tag TABLE -- a deleted collection's included | B | cycle 61 |
| **zen's token sees only 20 settings** (cycle 60) | `/:/prefs` PAGES and the capture sent a page size of 20; zen holds 173 | G | cycle 61 |
| **A section's Continue Watching gate is the SHOW's play history** (cycle 58) | It is the LIBRARY's: with a second show added to one library on plexscratch42, a session completion on one show lists the other's scrobble-only next-up. Cycle 58's copies held one show per library | B | cycle 59 |
| **`translations.json` is version-neutral** (cycles 16-25) | It is 1.43.4's: zen words 405 cells differently (Danish `Bibliotekets standard`, Serbian, Ukrainian, Hebrew and Portuguese enum rows among them). The seventh per-version fixture | A | cycle 59 |
| **A playlist timeline stores nothing** (cycle 59's hunt B) | With a session identifier the beat runs the item machine: `progress` stores the offset on the list, a crossing completes it with `viewCount` 1 -> 2, and Now Playing shows a `Playlist` row | F | cycle 59 (driver) |
| **A beat at or under 60 000 ms DELETES the stored resume point** (cycle 10) | Only inside a playback SESSION that has already reported a position past the minute. A NEW session's sub-minute beats answer `ignore` with the stored offset and keep it, on plexscratch42 and plexscratch alike; the old measurement was taken inside one session | B | cycle 58 |
| **A section's Continue Watching lists a next-up only when the episode it follows was completed in a session** (cycle 57) | The gate is the SHOW's play history: after any one session completion, every next-up is listed however it arose -- Mark Played on a later episode or the season, and after the played episode is unscrobbled. Cycle 57 drove only one scrobble-then-play sequence, which cannot tell the anchor from the show | B | cycle 58 |
| **A pristine unwatched show omits the inline `OnDeck`** (cycle 41) | It carries its FIRST episode, and so does the season that owns it -- kids' show 6 and season 7 carry episode 8, zen's show 5189 carries 5191. Cycle 41 measured the omission somewhere the block was absent for another reason | A | cycle 57 |
| **A negative hub `count` is `len(rows) + count`** (cycle 28) | Four rules per path: `/hubs` ignores `-1` and 500s `<= -2`; a section's hub page answers one row at `-1` and none below; related/postplay answer `11 - N` rows down to an empty container at `-11` and every row below. The formula did not fit its own `-1` | D | cycle 56 (malformed-only, recorded) |
| **The section search box ignores every legacy limit spelling** (cycle 41) | A plain `limit=1` caps it to one row on zen and kids; only the other spellings are ignored | A | cycle 56 (malformed-only, recorded) |
| **A good `limit` really constrains the section extras listing** (cycle 29) | zen ignores it (`limit=2` answers 20 of 3,580); kids' one clip could not show it | E | cycle 56 (malformed-only, recorded) |
| **`skipCount` is not in the null-as-zero family** (cycle 20) | A never-written row's `skipCount` matches nothing, but ANY per-user write creates the settings record with both counters 0 -- zen's `skipCount=0` is its 424 rows with state | D | cycle 56 |
| **A library's TV Recently Added shelf keeps its top-level show rows** (cycle 50) | It IS the section's mixed roll-up -- zen's `tv.recentlyadded.2` is row for row `/hubs/home/recentlyAdded?type=2&sectionID=2`, show, episode, season, episodes. kids' whole roll-up is its one show, so kids could not tell the two apart | A | cycle 55 |
| **`popular` lists the seasons with a `viewCount` above zero** (cycle 52) | It ranks seasons by their HISTORY rows (`group by parent_id order by count(*)`); a season an unscrobble stripped of its `viewCount` stays | D | cycle 55 |
| **Related "is filled by the metadata agent"** (plexsim's own docstring since cycle 4) | The server computes it from tags: first three genres and four roles, two shared, count then release date, cap 200 -- zen's 59 rows exact | C | cycle 55 |
| **`/hubs/search?sectionId=` refuses `0` and 2147483647** (cycle 54) | It refuses ANY id naming no section; the two were samples of that rule | D | cycle 55 |
| **The hub-refused keys are refused on `/hubs` and a show library's section page** (cycle 18) | And on `/hubs/promoted` whenever a show library's shelf is in scope | D | cycle 55 |
| **A negative rating other than -1 clears the stars and KEEPS `lastRatedAt`** (cycle 9) | It STAMPS `lastRatedAt` with the current time -- measured on both copies from a row with no rating at all, where the "kept" value would have been none. Cycle 9 rated the row seconds before, so the old and the new stamp were indistinguishable | A | cycle 54 |
| **`viewedAt>=abc` is a 400 on the history page** (cycle 14) | The key is `viewedAt>`, which is INCLUSIVE; a raw `>=` in a URL IS that key. The client's own encoding (`viewedAt%3E%3D=`) is the key `viewedAt>=`, which is unknown -- ignored and not validated. plexsim validated and filtered both and read `viewedAt>` as strict | B | cycle 54 |
| **A NULL date matches NOTHING, on every operator** (cycle 20) | True of every date column EXCEPT the three per-user ones: `lastViewedAt`, `lastRatedAt` and `lastSkippedAt` read an unset value as 0, the `viewCount` family -- zen's `lastRatedAt<<=2020-01-01` is its 3,465 unrated movies. Cycle 20 measured `originallyAvailableAt` and generalised | D | cycle 54 |
| **A show's season list ignores `skipCount=`** (cycle 46) | It runs the whole listing query -- `skipCount=0`, `title=zzzq` and `year=2002` constrain it, a word is a 500, `sort=` orders it -- on kids and on a 25-season zen show | A | cycle 54 |
| **A real PMS sends no `Server:` banner at all** — swept over fifteen paths and every status in cycle 16, and restated in cycle 33's header-order measurement | That is the API's rule, not the surface's. The plug-in FRAMEWORK answers through Tornado, and a framework route whose own `com.plexapp.*` state cookie the server cannot unpack answers a 200 with an empty body and **`Server: TornadoServer/1.0`** — the one `Server:` banner on the surface. It also swallows the route's own status, so `/system/zzz` becomes 200/0 instead of 404. Neither sweep could see it: both walked API paths, and this one is reached only by sending a cookie the server cannot decode | B | cycle 53 |
| **PATCH is a 405, and `/system` is the only subtree on the surface that answers one** | Two subtrees do. Everything under `/:/plugins` is a SECOND plug-in framework instance with the same envelope — a bare OPTIONS is an empty 200, every other verb an empty-bodied 404, PATCH the version's own refusal. Cycle 18 swept the API and `/system`; `/:/plugins` had never been walked, so the sample could not disagree | B | cycle 52, widened in cycle 53 |
| **`/:/resources/` is SEVEN names** | It is **70** — PMS's whole `Resources/Graphics` tree, identical on both versions (59 png, 9 jpeg, 2 octet-stream). Cycle 29 probed ten names, found seven and recorded the sample as the list. One of the 63 it missed is `actor-icon.png`, which 479 of zen's 3,416 actor rows carry as their `thumb` | B, D | cycle 52 |
| **The legacy dispatcher's prefix listing is a captured table of 58 paths** | It is a RULE: the last segment of every registered legacy prefix that begins with the requested path and is strictly longer than it. A breadth-first walk over the alphabet recovered 154 listings and the rule reproduces all 154 with zero membership mismatches. Cycle 50's 58 paths were that rule sampled at 58 points, and the other 119 prefixes answered an empty listing here | D | cycle 52 |
| **The unauthenticated partition is eleven single-segment 401 roots** | Thirteen — plus three static files that answer with NO token at all (`/crossdomain.xml`, `/clientaccesspolicy.xml`, `/favicon.ico`), three paths that answer their own refusal BEFORE the gate (`/search` 400, `/downloadQueue` 404, `/player` 400 on 1.42.2) and `/manage`, which is the `/web` redirect. Cycle 16 swept the vocabulary a client sends; the binary names the rest | B | cycle 52 |
| **`ButlerTaskGenerateAdMarkers` ships DISABLED** -- "re-measured on both writable copies", 1.42.2 and 1.43.4 alike | `enabled` is COMPUTED from CLAIM state, and both writable copies are UNCLAIMED. plexscratch is a byte copy of kids' own config with only the claim stripped and it answers `0` where kids answers `1`; zen and kids, the two claimed references, both answer `1`, and so does the `GenerateAdMarkerBehavior` preference (`scheduled` -- a setting that does not exist at all on an unclaimed server). Cycle 35 fixed a class-D error by installing a class-E one, and this is the finding the claim audit existed to produce | **E** | cycle 49 |
| **`/playlists/sorts` ends with `updatedAt` "Date Modified"** -- "the two lists really are different, and each was measured on its own path" | It ends with `mediaCount` "Item Count", the same row `/playlists?includeMeta=1`'s `Meta` carries. Four servers, both versions, both claim states, six parameter spellings. plexsim's own two surfaces therefore contradicted each other | B | cycle 49 |
| **`?includeDetails=1&includeAdvanced=1` carries NO Duplicates Filter row at all** | It carries the row, and it is the ONE Filter row on that path that keeps `advanced="1"` -- every other carries no flag, which is cycle 16's rule and still holds. Cycle 16 measured the path WITHOUT `includeAdvanced=1`, where the row does not exist to carry a flag; cycle 23 read that absence of the FLAG as the absence of the ROW | A | cycle 49 |
| **A real PMS does NOT put its account token in `/myplex/account`** | zen (1.42.2, claimed and fully mapped) answers `authToken="<its PlexOnlineToken>"`. kids does not -- and kids also answers an empty public/private address pair, so its whole plex.tv mapping block is missing. Version and mapping state are CONFOUNDED on the only two claimed servers this project has. plexsim's omission stands, but as a deliberate deviation rather than a measurement | B | cycle 49 |
| **"GET and HEAD never consume a declared request body"** | Half the rule. A real PMS answers WITHOUT waiting and then CONSUMES exactly `Content-Length` bytes, and closes when they never arrive. Cycle 37 measured it with `Content-Length: 999` and NO body bytes at all -- a sample that cannot tell "does not wait" from "does not consume". plexsim left the bytes in the stream and one request produced two responses | A | cycle 49 |
| **An emptied collection cannot be kept, because a real PMS "also leaves the stale tag on the item"** | It does both representable things: the collection ITEM survives (`/library/metadata/{id}` 200, still in `/library/all?type=18`) AND the tag comes off the item. The state plexsim called unrepresentable is exactly what the reference holds, so plexsim was deleting a row a real PMS keeps | F | cycle 49 |
| **`transcoderVideoRemuxOnly` looks like a 1.42.2 attribute** (zen carries it, kids does not) | Refuted by the copy: plexscratch42 is 1.42.2 and omits it too, so it is zen's own state and not a version split. Recorded rather than built -- and it is why the 2x2 of claim and version is the right instrument for anything that looks version-shaped | E | cycle 49 |
| **`/servers` and `/myplex/account` may report loopback and a constant port** | Never measured at all: `private_address` was declared and never assigned for the whole life of the field. zen answers its own `192.168.88.244`/32400 and kids `172.31.7.2`/32400, and `privateAddress`/`privatePort` move together or are `""`/**0** | C | cycle 49 |
| **`includeAlternates` is a 1.43.4-only option** | Refuted by the instrument, not by the servers: the hunt measured 1.42.2 on **plexscratch42**, a fresh install whose every row is unmatched and which therefore holds no agent artwork for the option to inline. zen -- a real matched 1.42.2 library -- honours it on `/hubs`, `/library/onDeck`, `/library/metadata/{id}` and both `related` paths. CLAUDE.md's "use the copy for grammar, never for a count" read from the other side: an EMPTY answer on the copy is not a negative | **E** | cycle 45 |
| **A play completes only past a minute** -- "the timeline's minute is strict and has no `stopped` exemption" | The minute gate belongs to the RESUME POINT, not to completion. A beat that CROSSES 90% completes the play however short the item is; the gate is `position >= (total * 9) // 10`, an integer comparison, and the float `0.9` plexsim used disagreed with it on short rows | **D** | cycle 45 |
| **`file=` is a 500 on a SHOW listing** -- decided per ROW | The refusal is a property of the QUERY's declared type. `type=2` refuses on a section listing and on `/library/all` alike, `type=3` refuses on `/library/all` and ANSWERS on a section listing, and an **untyped** `/library/all?file=` answers and really filters (kids 11 of 14 rows, zen 12,899). plexsim raised it from inside the row walk -- "this row is a show" -- so the untyped whole-library query was a 500 where a real PMS answers | **C** | cycle 45 |
| **An axis bucket resolves against the leaf's own column** | It resolves against the server's GLOBAL tag TABLE and the name in front of it is decoration: a genre tag id answers the identical container under `genre`, `director`, `actor`, `mood`, `style`, `similar`, `tag` and eleven more names, and a director tag id answers ITS rows under every one of them. Only `studio` and `platform` are not bucket names at all. plexsim keyed on the name, so nine names worked for their own tags and the other ten 404'd everything | **C** | cycle 45 |
| **A 416 names no connection state at all -- the only response class that does not** | Half right: a 416 of a **MediaContainer** names it (`Connection`, `Keep-Alive: timeout=20`, after the type and before `Cache-Control`) and a 416 of a **binary** body -- artwork, `/:/resources/*`, a `/web` asset, a media part, `/photo/:/transcode` -- names none. Cycle 33 and cycle 36 both probed with `Connection: close` in the REQUEST and were reading their own header back. Every 416 is also `no-cache`, the transcoder's included | **E** | cycle 44 |
| **A `.webm` file's container is `webm` -- "the extension can tell them apart, and it is what a real PMS reports"** | Refuted on the IDENTICAL file: zen answers `container: "mkv"` for the same 571,346,576-byte `Tears of Steel.webm`, and no row of its 4,508 movie Media carries `webm` at all. The project's own corpus recorded `mkv` for a `.webm` file in cycle 10 and nobody read it, because `test_mediainfo.py` compared every stream field and never the container. The override was kept to protect Plex Web's direct play; across all 78 profiles it changes no browser outcome and costs `Sony Bravia 2013` one | **C** | cycle 44 |
| **An episode and a season carry no `Guid[]` -- "matching a real PMS, which leaves those arrays empty"** | False on both references: kids' season 7 answers `tmdb://3846` + `tvdb://15791` and its episode 8 three ids; 58 of 60 zen episodes surveyed carry a block. `metadata.py` fetched the ids and discarded them on that note, and §8 carried it as "no data" -- the premise was void, not the exemption | **C** | cycle 44 |
| **`/devices/{id}` takes plex.tv's NUMERIC device id, and `resolve_device_id` is the way to look it up** | A real PMS announces `PUT https://servers.plex.tv/devices/{machineIdentifier}` -- the identifier form, three times identically in the harness's own capture. `CLAUDE.md` recorded that correction on 2026-09-12 and `heartbeat.py` never followed it, while the numeric form it kept using is the one that silently overwrites whatever device row holds that id | **C** | cycle 44 |
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

**Cycle 26 overturned six more.**

| Belief | Held since | Overturned by | Class |
|---|---|---|---|
| "a decision without `hasMDE=1` is a refusal" -- unconditional, and never measured at all | 6 | A decision with no `hasMDE` runs the CLIENT'S OWN PROFILE. `Roku` and `Xbox One` get the directPlay/general 1000 PAIR with the full Metadata row; plexsim answered the `3000/2000/4005` "neither" to every client that is not Plex Web. **The largest functional gap since the build-out** | **A** |
| "a decision refuses a missing `mediaIndex`, a bad one and a bad `partIndex`" | 6 | Only an OUT-OF-RANGE `mediaIndex` refuses, over nine parameter shapes on both references. python-plexapi's `getStreamURL` omits `mediaIndex` and was refused before any profile question | A |
| "a statistics `timespan` the server does not keep falls back to the nearest bucket" | 20 | It is an EMPTY 200 on all three endpoints and both references; the fallback invented a series for a window the server holds no data for | D |
| "`removeFromContinueWatching` does not reach a section's own Continue Watching list" | 25 | True for a SHOW library and false for a MOVIE one, and the shelf and its see-all list always agree. Cycle 25 measured one half on an episode and applied it to both types on one of the two surfaces, so the halves ended up crossed | B |
| "a MOVIE's decision row drops `summary`" | 6 | Fitted to the one copy row whose summary is the EMPTY STRING. Four movies with real summaries and two episodes refute it | B |
| "`SESSION_TTL = 70`" -- a row survives 60-80 s past its last timeline | 9 | Ten runs on `long` media: gone between +184.0 and +210.4. Cycle 9 measured it on ten-second clips, before `long` mode existed | D |
| "`xmlout._ATTR_ORDER` governs every container" -- 203 pairs, 2,994 paths, zero contradictions | 22 | `/status/sessions` writes its attributes ALPHABETICALLY. The sweep could not reach it: reading it needs a live playback session, which a read-only reference cannot be given | E |
| "a list's `/unmatch` moves its guid" -- the line has been there since cycle 23 | 23 | It writes into a projection `_collection_row` rebuilds on every read, so no client ever saw it; a playlist's branch returned before the line ran at all | C |

**Cycle 27 added these six.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| The header block is bounded at "~41 KB" | It is a COUNT, not a size: 100 header LINES pass and the 101st is a 400, whatever they weigh. 200 KB of headers in fifty lines is a 200. The old figure is the same cap measured with ~410-byte headers | D | cycle 27 |
| A session row's `Role` is `filter, actor, id, role, tag, tagKey, thumb` -- "alphabetical after `filter`" | Every tag element there is plainly alphabetical and a `Role` carries no `actor` attribute at all. Measured on a row with seven tag types where cycle 26 had one | F | cycle 27 |
| The BARE `/system/agents` carries a `MediaType` per media type and NO `Language` | That is the LEGACY plug-in agents' rule. All six modern `tv.plex.agents.*` rows carry their 45 `Language` rows in BOTH forms and a `MediaType` in neither, on 1.42.2 and 1.43.4 alike | B | cycle 27 |
| `Player.profile` is the constant `Web` | It is the profile the server RESOLVED: 23 of the 78 profile names report another profile's NAME. The copy had only ever been driven by Plex Web | B | cycle 27 |
| An artwork cascade moves a borrowed field where it already exists | It DERIVES it: it creates the field where there was none, moves a borrowing row's own `thumb` and `art`, and removes the borrower's copy when the owner's goes | B | cycle 27 |
| `/library/all/top` takes `type=1`, `2` or `4` and 400s the rest | It answers an empty 200 to any type NUMBER and refuses only a value that does not parse. The rule was written from the three types plexsim holds rows of | B | cycle 27 |

**Cycle 28 added these eight.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| `contentDirectoryID` "scopes and VALIDATES `/hubs/search` alone -- ignored on every other hub path" | It scopes `/hubs` and all three `/hubs/home/*` leaves, removes `home.playlists` from `/hubs`, takes a COMMA LIST, and is VALIDATED on `/hubs/continueWatching` and its `/items` leaf as well. Only `/hubs/sections/{id}` ignores it | B | cycle 28 |
| A COLLECTION is NOT found by `/library/search` | It is, as a `tagType: 2` row. Cycle 15 measured it on a copy that held no collection at all, so the absence was the fixture's | **E** | cycle 28 |
| On Deck and Continue Watching rows are the LIGHT projection, On Deck's named by UUID and not by key | On 1.42.2 an On Deck row is the DETAIL row with `librarySectionKey` on it. Cycle 21 measured it on fun, which had nothing in progress, so the shape was inferred from an empty list -- and 1.43.4 really does drop the key, `Guid`, `Rating` and `Producer`, which is why the two could never be reconciled | **E** | cycle 28 |
| `/hubs/home/recentlyAdded?type=2` is the list of recently added SHOWS | It is a MIXED list, rolled up: two or more recently-added siblings collapse into their parent, recursively, and a parent row is dated by its NEWEST leaf | B | cycle 28 |
| A shelf's rows are the sorted head of its key, `random="1"` or not | 29 of 32 non-empty shelves are exactly that head; the three exceptions are the random ones, which are a random SAMPLE of the whole key set in random order | D | cycle 28 |
| `/clients/profiles` is empty because plexsim hosts no client | It is the server's own CLIENT PROFILE catalogue: 78 rows bare, seven at `type=42`, a 400 for every other value. True of `/clients`, not of this | B | cycle 28 |
| A search tag hit is one row per tag NAME, counting every row that carries it | It is one row per LIBRARY, and the count is the size of the listing the row's own `key` names | B | cycle 28 |
| A `/library/search` tag row is the `/hubs/search` row | It drops `reason`, `reasonID`, `reasonTitle` and its own `score` -- the score is the `SearchResult` wrapper's alone, a number in JSON and five decimals in XML | B | cycle 28 |

**Cycle 29 added these ten.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| An artwork cascade MOVES a borrowed field where it exists (cycle 27) | It DERIVES it from the NEAREST ancestor that OWNS one, and it CREATES the field where there was none. Cycle 27 measured the move half and inferred the rest; `_follow_artwork` returns early unless the child already names the owner, so a leaf that lost its `art` never got one back | B | cycle 29 |
| "The artwork that is LEFT moves to a new version stamp" (cycle 15) | Only the artwork the row OWNS. Clearing a season's thumb left its BORROWED `art` naming the show untouched on the copy, where plexsim rewrote it as the season's own -- a row claiming an image the server has none of | B | cycle 29 |
| An artwork UPLOAD sets the row's two fields and frames it (cycles 15, 17) | It CASCADES like a pick: the row's own fields land on one stamp and every child's borrowed copy follows. plexsim never called the cascade there, so children kept the old stamp and a client that caches on it never refetched | C | cycle 29 |
| `/photo/:/transcode` "404s a `url` naming a row it does not have" (cycle 14) | That is one of seven shapes. An EMPTY `url=` is a 404, a server path that is not artwork a **500**, a scheme or a missing leading slash a 404, and a row that does not OWN the field a 404. plexsim answered a generated grey tile -- with `max-age=259200` on it -- to five of them | B | cycle 29 |
| `/:/resources/` has nine names (cycle 1) | SEVEN. `library-art.png`, `playlist.png` and `album.png` are 404s on both references -- even though a real PMS's own `/library` container advertises the first as its `art` -- and `width`/`height` are IGNORED there | B | cycle 29 |
| A 0/1 option takes `0`, `1`, `00`, `01` and nothing else (cycle 23) | `+?0*[01]`: a leading `+` and any number of zeros. Cycle 23's ten-value sweep had no sign in it. The same family includes **`download=`**, which plexsim never validated at all | B | cycle 29 |
| `+1` does not turn an `include*` block on (cycle 22) | It does. A RAW `+` in a query string decodes to a SPACE, so cycle 22 measured `" 1"` -- which really is off. **The same trap produced the line above**, one cycle apart | **A** | cycle 29 |
| `sort=type` is "a real ordering" among the text sorts (cycle 14) | It orders by the type NUMBER (movie 1, show 2, season 3, episode 4), not the type's name. Cycle 14 added the key without checking the order it produces | C | cycle 29 |
| `group=` belongs to a section listing and the section `allLeaves` (cycles 13, 20) | `/library/all` honours it too, and 500s an unknown or empty field there. plexsim's `grouped()` was right and that handler never called it | C | cycle 29 |
| A 416 is the resource with no bytes (plexsim's own) | It is an ERROR response: `text/html`, a 129-byte body, and neither `Accept-Ranges` nor `Content-Range`. plexsim sent the original media type with `Content-Length: 0` and the whole success header set | D | cycle 29 |

**Cycle 30 overturned these.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| A 401, a 403 and a 404 all carry `Connection: close` (printed verbatim in `docs/findings.md`) | Every one of them is `Connection: Keep-Alive` and the socket really is reusable on both references. The sample was plexsim's OWN answer, read back as the reference's for eleven cycles | **G** | cycle 30 |
| An On Deck row carries `librarySectionKey` on 1.42.2 | It carries neither that nor `Field`, on a movie and an episode alike. Cycle 28 read the key off zen's single on-deck row; a fresh 1.42.2 copy, plexscratch and cycle 21's fun all say absent -- three instruments against one | B | cycle 30 |
| A library DELETE pushes one `state 9 deleted` frame per row | It pushes NONE. Cycle 18's fourteen frames named a DIFFERENT section, none of their itemIDs was a row of the library, and a nine-row library produced the same fourteen: they are a one-off cleanup of orphaned `type: 12` rows. Two libraries built for the question, 3 rows and 200, push only the eight `library.delete` activity frames | B, F | cycle 30 |
| Four settings answer 403 to a PUT | Two do on 1.42.2. `TranscoderH264Options` and `TranscoderH264OptionsOverride` take a write there and really store it; cycle 19 measured the list on the only writable reference it had, which was 1.43.4 | E | cycle 30 |
| A `random="1"` shelf is re-sampled per request *(rule right, code wrong)* | The rule cycle 28 measured is the sampling; what it implemented was a fresh `random.sample` on every request, so "Top Unwatched" re-ordered on every navigation. zen's sample is STABLE -- 21 reads over seven minutes, identical -- and is held per (shelf, count) | C | cycle 30 |
| A `collection[0].tag.tag=` write creates a collection | It creates the TAG and no item: `/library/all?type=18` lists nothing new twenty minutes later and after a refresh, on both versions. Only `POST /library/collections` makes one, and the search row's `guid` is the tell | B | cycle 30 |
| The timeline state alphabet is 0, 3, 4, 5, 9 | `state 1` is in it: a row's CREATION pushes eight frames -- `state 0` with an empty title, five `state 1`s, `state 4 loading`, `state 5` | B | cycle 30 |
| "No scan on a real server does a metadata activity per row" (cycle 19) | True only of a scan that finds NOTHING NEW. A scan that creates rows pushes a `library.update.item.metadata` activity per new row | B | cycle 30 |

**Cycle 31 overturned these.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| `PATCH` is ROUTED like any other verb (cycle 15), a 405 under `/system` (18), a 404 on four paths (22) and on the collection paths (28) | All of that is **1.43.4's**. On 1.42.2 `PATCH` is not in the method table: a 500 on every path, `/identity` included, with the body `FROB` gets | E | cycle 31 |
| `DELETE /library/metadata/{id}/{poster,art,thumb}` clears the artwork | 1.43.4's. On 1.42.2 all five artwork DELETE leaves are **404s**, and the clear route there is an EMPTY `thumb.value=` | E | cycle 31 |
| `thumb` and `art` are accepted and DISCARDED as values (cycle 20) | They are artwork COLUMNS: the url is discarded and the column becomes the row's own versioned path. Cycle 20 wrote to rows that already owned a poster, where the write looks like a no-op | B | cycle 31 |
| `absoluteIndex` is read on both write paths (cycle 20) | It is read on NEITHER -- a 200 that stores nothing on both paths, both versions, four row types -- while its lock IS recorded | B | cycle 31 |
| `hdr10plus` takes an item LOCK (cycle 30) | It stores neither a value nor a lock. Cycle 21 recorded that correctly; cycle 30 checked the 200 and never read the `Field` list back | C | cycle 31 |
| `GET /library/collections/{id}/prefs` is a 404, so the leaf does not exist (cycle 15) | The GET is a 404 and the **PUT really writes**, on both versions -- the same one-verb-each shape as the collection's other leaves | A | cycle 31 |
| `smart=1` over a comma-joined key list is a 400, and every item write on a smart list is a 400 (cycle 16) | 1.43.4's. On 1.42.2 both are 200s, and the items PUT REPLACES the stored query with whatever uri it is given | E | cycle 31 |
| A comma and a dot in a `title=` query are ignored | The comma is an ALTERNATION (an empty alternative matches every row, so a trailing comma answers the whole library) and the dot is LITERAL | B | cycle 31 |
| The hub matcher needs the query as one PHRASE, the title split on `[^0-9a-z]+`, and a two-character floor | Word by word in order; the title tokenised on WHITESPACE with punctuation inside the word; and a one-character query matching a title equal to it | B | cycle 31 |
| The in-library search box reads `title` OR `originalTitle` | The title alone. The hub search is the endpoint that reads the original title -- and it reads it as a consecutive run of whole words, unfolded | B | cycle 31 |

**Cycle 32 overturned these.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| A title folds its combining marks away (cycle 14) plus eleven characters NFD cannot reach (cycle 18) | A TABLE of exactly 100 characters, identical on both versions over 2,076 codepoints. NFD over-folds `ę ą ş ć` and the capital macrons, which a real PMS keeps. Cycle 14 fitted it to zen's 21 accented rows, all of them Latin-1, and cycle 18 swept only the characters NFD leaves alone -- so neither sample could contain a character NFD folds and PMS does not | B | cycle 32 |
| The `title=` folded pass reads the TITLE (cycle 15) | It reads the stored SORT title: `Tráin Job` finds "The Train Job" and `Thé Train` finds nothing. Rows without an article match either way, which is every row the fit used | B | cycle 32 |
| The hub search "folds combining marks on BOTH sides" (cycle 14) | It folds by the title TABLE (825 probes); its two score-case passes were completed and built in cycle 39 from the full 659-cell domain | B | cycle 32, completed cycle 39 |
| `Roku` and `Xbox One` are the profiles that direct play (cycle 26) | Direct play is decided PER ITEM by the profile file: 44 of 78 profiles direct play a real row, and neither Roku nor Xbox One lists webm or eac3. Cycle 26's four items were all h264/aac, which cannot show the item dependence, and the list was implemented as if it were the rule | B, C | cycle 32 |
| Firefox and Edge "declare video targets that ought to cover the item" (cycle 31) | They declare only `dash` and `hls` targets, and the request asked for `http`; with `protocol=dash` both answer "Conversion OK" | A | cycle 32 |
| `/livetv` is a 403 on the copies, so plexsim's container is in doubt (cycle 31) | Both CLAIMED references answer the container. The 403 is an unclaimed server's, and the copies answer the container at `/livetv/` | E | cycle 32 |
| "A running Activity could not be caught at all" (cycle 19) | It can: polling the 1.42.2 copy during a forced section refresh catches `library.update.section` at 0 and `library.update.item.metadata` at 50. plexsim's own writes finish too fast to catch, which is its speed and not a rule | E | cycle 32 |
| Paging and `excludeElements=` are properties of the RESPONSE (cycles 21, 23) *(rule right, code wrong)* | Implemented for GET and HEAD only, so twelve playlist, collection and play-queue WRITE responses escaped it on both counts | C | cycle 32 |
| The play-queue create fix "also fixes" two concurrent ADDS answering one container (cycle 32's own comment) | It did not: re-run on its own tree, 9 of 180 add pairs still collided. The add, the clear, the item delete and the move each built their answer after the lock too | C, F | cycle 32 |

**Cycle 33 overturned these.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| `Cache-Control: no-cache` goes on EVERY response (cycle 16) | A PART READ carries none, on both references -- the one exception. Cycle 16 sampled eight endpoints and none of them was a part read; plexsim was already right and the RULE was wrong | B | cycle 33 |
| plexsim's response headers are the reference's set, so the response is right (cycles 16, 27) | The SET was right and the ORDER was never compared. `X-Plex-Protocol` is the reference's first header and `Date` its last; plexsim had both ends reversed, and the order is per response family | B | cycle 33 |
| "An unmatched movie's refresh on 1.42.2 pushes `state 5` before `state 3/queued` -- one server, unmatched rows only" (cycle 32) | Both versions do it, and it is the NEVER-MATCHED guid (`local://`) that decides it: an explicitly unmatched row opens with the queued frame alone. The cycle-33 hunt's reading of the same shape as a VERSION difference does not survive its controls either -- what differs is whether an isolated copy's agent can do any work | B, E | cycle 33 |
| Cycle 35's `word_tier` -- the search score's ANCHORED tier as the matcher's in-order, skip-tolerant walk plus "the first term matches the first word" | **D** | Gridded in cycle 36 on zen's "Riders of Justice" over forty queries: `Riders X`, `Riders XY ZW`, `Riders of Justices` and `Riders of Justice Riders` are all 0.73 on both references, and that model calls every one of them fuzzy. The anchored tier is ONE token comparison. It was fitted on sixteen cells, none of which carried a term the title does not have -- and it landed ONE CYCLE earlier |
| Cycle 1's `text/plain` refusal body as a "deliberate deviation" | **F/G** | Carried in §8 for thirty-five cycles and re-justified in cycles 16, 27 and 29 without anyone reading what the reference sends. It sends one fixed HTML document per status, built from HTTP's own reason phrase, on seven statuses and both references. Nothing was ever measured; the deviation was inherited |
| Cycle 19's "a CHILD of the section's Location scans nothing" | **E** | Measured on a MOVIE library, whose children are FILES. A show's own directory -- a folder the library really holds -- scans, 17 frames against the no-path scan's 16 with the same three `progress` rows, on both copies |
| Cycle 22's "`/library/collections/{id}/{refresh,unmatch,analyze}` answer an EMPTY body" | **F** | True and half the answer: they really RUN. The refresh pushes the metadata path's three frames and the unmatch gives the row `local://{rk}`, `subtype "unknown"` and no `ratingCount`. plexsim answered the empty body and did nothing, so the same action from two paths had different effects |

**Cycle 37 overturned these.**

| Belief as recorded | What it actually is | Class | Corrected in |
|---|---|---|---|
| The spellfix score is `editdist1 + 32 - floor(log2(rank))` | SQLite uses `editdist1 + 32 - bit_length(rank)`, equivalently `+31-floor(log2(rank))` for a positive rank. `riders`, rank 4 and distance 0, scores 29, not 30. The old expression was inferred from black-box candidates without reading the extension's arithmetic | C | cycle 37 |
| The database has four spellfix vocabularies: title, sort title, original title and tags | Both versions have exactly TWO live virtual tables: `spellfix_metadata_titles` and `spellfix_tag_titles`. Sort and original-title spellings survive only in historical executable strings; the live FTS table, not a separate spellfix table, indexes title, sort title and original title | F | cycle 37 |
| A mid-word or non-table Unicode search miss is a useful negative control | It enters the measured spelling fallback and may be a real hit. Two UI checks still asserted that fuzzy membership was absent after the server acquired it; both were replaced with measured score/membership controls | C | cycle 37 |
| The recovered Cycle-50 branch's seven commits represented the completed cycle | The original session had measured an eighth patch, and the hunt reports still held 18 unimplemented entries plus three unprotected rules. Recovery must join reports and transcripts to code, not count commits | F | cycle 50 |
| A stored resume offset determines timeline `playbackState` | `progress` is a live latch keyed by `X-Plex-Session-Identifier`: crossing one minute sticks for that session after rewind/stop; a new session is `ignore` despite the stored offset | C | cycle 50 |
| A searchable tag's count is the size of its top-level key list or a persisted historical counter | Top-level current membership is only the admission gate; `count` is every current tagged metadata row in that library, including episodes. The tag id persists, the count does not | B, C | cycle 50 |
| GDM `Updated-At` changes with any advertised field, including FriendlyName | Only section create/edit/delete move it. FriendlyName changes `Name` without moving the stamp; refresh, item edit, collection create and index delete are non-events | C | cycle 50 |
| The retained legacy-prefix denominator was 57 paths | The durable executed command contains 58 unique paths. All 58 shapes agree over 174 reference GETs and are now protected exactly | F | cycle 50 |
| A post-response body drain needs a small grace interval | The measured boundary is zero: drain only framing already buffered when the response completes. A body arriving later becomes the next request's bytes | C | cycle 50 |

### 3.3 Standing on thin evidence right now — revalidate before building on these

| Item | Why it is thin | How to settle it |
|---|---|---|
| **Manage Recommendations' writes** | Plex Web's promote, reorder, remove, reset and publish (`PUT/DELETE /hubs/sections/{id}/manage[/{identifier}[/move]]`, `POST ...manage?metadataItemId=`) are 404s on plexsim and the route exists in 1.42.2's binary (`HubManagementRequestHandler`), but both writable copies 404 the whole manage leaf (unclaimed) and kids and zen are read-only. CLIENT-VISIBLE: the dialog's checkboxes snap back | A claimed, writable 1.42.2 server |
| **The ORDER of an item language enum's head** | Language-independent on zen and fitting neither tag order nor English-name order over 507 shows (many heads need five or more sorted runs); plexsim orders by tag within the audio and subtitle groups (cycle 18). The NAMES are exact in 30 language forms | Read the stream table order out of a zen show whose head plexsim would order differently, or the binary's SQL |
| **Plex Web against plexsim in a real browser** | Cycle 59 is the first render diff and ran on kids' 9223 Chromium, because the Windows CDP machine was unreachable; Playwright's `connect_over_cdp` hangs there, so hunt A wrote a raw-CDP driver. The claimed-account panels (Dashboard upsell, Watch Together, Trending) and anything needing a media read were bounds | Repeat on the Windows browser with a media-reading play, and drive the Dashboard with the account's feature ids |
| **`viewUpdatedAt`'s stamping rules** | Driven write by write on plexscratch42, which is UNCLAIMED (class E, cycle 49). The column's existence and grammar rest on zen and kids; which rows each write stamps rests on the copy alone, because a read-only reference cannot be written | The same writes on a claimed disposable 1.42.2 server |
| **Cycle 58's implementation choices the hunt did not settle** | ~~Whether the shelf's play-history gate is per SHOW or per LIBRARY~~ -- per LIBRARY, measured in cycle 59 with a second show (`Twofold`, relative symlinks to Threefold's files under `/home/ben/plexscratch42/media/shows`) added to the Threefold library; whether it is per library or per ACCOUNT is still open (no library on either copy is free of session history). That the history outlives every later scrobble, unscrobble and cloud import (plexsim no longer clears it anywhere); the sub-minute rule's latch on ANY earlier beat past a minute rather than the last one; the 409 rule's `id=` list refusing the whole write when one member conflicts; the hidden library's effect on paths hunt B did not read | Drive each on plexscratch42 |
| **Cycle 57's implementation choices the hunt did not settle** | The merged hub orders by the next-up's anchor view time; the section shelf's play mark follows the most recent anchor and a cloud import clears it; a pristine OnDeck needs no view state on ANY leaf and puts Specials last; the collection shelf replaces `movie.similar`, its suffix is always 1 and a smart collection adds none; the queue block's membership on a move; `tagId`'s operators other than `=` (zen's `tagId!=` is not the complement) | Drive each on plexscratch42 |
| **A section's `/onDeck` leaf is ordered by the view-state write time** | Measured on plexscratch42 alone (unclaimed, class E); zen and kids hold no two in-progress rows to show it | Two in-progress rows on a claimed reference, one then rated |
| **The client-language rule for a separator-led `X-Plex-Language`** | One fitted rule (drop one leading `-`, `_` or space; otherwise English) reproduces all sixteen measured values; nothing on the surface names it | A value outside the sixteen that the rule mispredicts |
| ~~**The per-library TV shelf's filtered form**~~ | **CLOSED in cycle 55.** The shelf IS the section's roll-up, filtered or not. | — |
| ~~**`sectionId` on `/hubs/search` refuses `0` and 2147483647 only**~~ | **CLOSED in cycle 55**: any id naming no section refuses | — |
| **What `unwatched=1` drops from a PARENT row** | On `/library/recentlyAdded` only watched MOVIES were witnessed; a season row there carried no `viewCount` on either reference, so plexsim drops leaf rows only | A season row whose episodes are all watched, on a list that shows it |
| **`includedAt` is stamped equal to `lastViewedAt`** | Cycle 53 measured the attribute on `/hubs/continueWatching` and its `/items` leaf on both references, and in all five witnesses the two values are EQUAL. Cycle 54 drove `/:/progress` on plexscratch42 twice and both moved to the same second each time. plexsim therefore stamps one from the other. Nothing on any instrument makes a hub's inclusion stamp diverge from the view time, so "they are the same field" and "they happen to agree here" are the same measurement | A row whose Continue Watching membership was changed without a view — a client action no instrument here can drive |
| ~~**The legacy agents' LOAD ORDER**~~ | **CLOSED in cycle 54.** zen's `Resources/Plug-ins-*` sits on btrfs, whose `ls -U` order is alphabetical, and kids, plexscratch and plexscratch42 are overlayfs with one shared order -- plexscratch42 is the SAME 1.42.2 build as zen and answers kids' order. The swap follows the filesystem's readdir order, not the version; plexsim keeps the docker-install order | — |
| ~~**`/system/library/sections`' `key`**~~ | **CLOSED in cycle 54.** Seven `Host` spellings on both copies leave it unchanged: it encodes the server's OWN interface address (the container's) and its listening port, padded with `_` -- which plexsim already does | — |
| **The framework state cookie's real-world trigger** | The BEHAVIOUR is measured on four servers: a `com.plexapp.*` cookie the server cannot unpack makes the framework answer Tornado's empty 200, banner and all, swallowing the route's own status. That a browser can actually come to hold such a blob — a Plex Web session carrying a cookie from an earlier bundle — is inferred from the cookie being same-origin, not observed | Watch a real Plex Web session across a server version change, or accept the behaviour and drop the impact claim |
| ~~**GDM's `Updated-At`**~~ | **CLOSED and BUILT in cycle 50.** Controlled writes on both disposable versions prove that only section create, edit and delete move the stamp. Friendly-name changes move `Name` without moving the stamp; refresh, item edit, collection creation and index deletion do not move it. `tests/test_gdm.py` now protects the lifecycle | — |
| **`Host` in the GDM reply is claim-dependent** | Both CLAIMED references carry `<hash>.plex.direct` and neither unclaimed copy carries the line at all. An unclaimed PMS has no certificate, so the CLAIM and the CERTIFICATE are confounded on all four instruments. plexsim emits the line only when it holds a plex.direct hostname, which is right under either reading | A claimed server with its certificate removed, which nothing here can make |
| ~~**The drain grace after a response**~~ | **CLOSED and BUILT in cycle 50.** A response drains only body framing already buffered at the response boundary; there is no post-response grace. A delayed body becomes the next request's bytes. The raw-socket regression holds the boundary at zero | — |
| **`SERVERS_TTL` = 180 s** | Bracketed to (177, 180] in ONE clean run on zen, by starting a server on its wire and polling every three seconds. A second run read (154, 165] and is discarded because it reused a machineIdentifier zen already held -- so the number rests on a single uncontaminated observation | Repeat it with a fresh machineIdentifier each time |
| ~~**The legacy dispatcher's PREFIX listing**~~ | **CLOSED and BUILT in cycle 50.** The retained command contains 58 unique executed paths, not the 57 the old prose claimed. All 58 ordered response shapes agree over 174 reference GETs and are served from a finite captured registry; the regression is 58/58 | — |
| **`/connections`** | Cycle 50 expanded this to 78 observer snapshots and 26 bounded sockets across both disposable versions. Pending, Routing, Paused/Pipelined and Finished states and the ~20 s cleanup are qualitative matches, but ordinals/order are unstable, request numbers consume invisible events, Finished cleanup is scheduler-coupled and the byte-count form requires excluded media traffic. An exact row remains underivable, not merely unmeasured | A reference-exposed stable id/allocator and an isolated media-byte experiment; otherwise any implementation fabricates visible fields |
| **`Mood`'s slot in `xmlout._CHILD_ORDER`** | Absent, deliberately: a real PMS refuses to STORE a `mood` on a movie, so no reference row can ever carry one for its position to be read from. A movie library is the only type plexsim holds | A music library, which plexsim will never have |
| **`/library/shared/all?sort=` crashes a real PMS** | An EMPTY sort dropped the connection on kids and SEGV'd zen's Plex (`code=killed, signal=SEGV`), reproduced on both and isolated to that one parameter. There is no answer to copy, so plexsim reads the empty value as absent -- the convention `/collections` already has for an empty sort -- which is a SURFACE CONVENTION applied, not a measurement | Nothing on a reference can settle it. A third 1.42.2 instance that may be crashed freely could confirm the crash is universal; the correct ANSWER is unobtainable |
| **The generating rule behind `/{name}/top`'s eleven-name map** | The eleven names resolve in six pairs -- a tag name and a metadata COLUMN name sharing each number (`genre`/`title` 1, `director`/`studio` 4, `writer`/`rating` 5, `actor`/`tagline` 6, `producer`/`summary` 7) -- which says PMS looks the segment up in one integer space that both vocabularies feed. The TABLE is measured on three servers and two versions and is what plexsim implements; the rule that generates it is not identified, so a name outside the swept 91 is a guess | Read the lookup out of the binary, or sweep a wider vocabulary against a library holding tags of types 2, 3 and 9+ |
| **The `/library/shared` verb table** | GET and HEAD are measured on kids and zen, which are read-only references; the two writable copies **404 the whole subtree** (they are unclaimed and isolated from plex.tv, the same shape as `/livetv`'s 403 there). So the non-GET half is applied from the surface's read-only rule rather than measured | A claimed writable copy, which the isolation rules currently forbid |
| **plexsim's minted theme rows live BESIDE the catalog** | A real PMS stores the minted `track` row in its metadata table; plexsim keeps it in `self.theme_rows` so a `type: "track"` row with no `librarySectionID` cannot leak into the tag tables, the spelling index or `/library/all`. The observable difference is nil on every path swept -- the reference's own minted rows are invisible in all of them -- but "invisible in every listing I looked at" is not "invisible everywhere" | Sweep the minted key against every container that walks `catalog.items` on a copy, or accept the divergence and say so |
| **The 32 named account features** | Identical on kids and zen, so they are account-independent across two servers of one account -- but only ONE account has ever been read. A different Plex Pass account, or a free account, would show whether the 32 are a plan's list or this account's | A second account, which the project does not have |
| **Everything about the media BYTE path measured before 2026-09-17** | Every rule about a media body was measured on a REFERENCE and implemented on faith, because plexsim's own byte path answered 502 to everything -- `test_all.py` reported `0/11 served` for fifteen cycles. Cycle 44 ran it for the first time and 13 of 20 `Range` cells were wrong | Re-run anything about the part path through `tests/test_part_bytes.py`, which needs no credential; and re-check any rule whose only witness was a reference's own part |
| **The `Part.accessible`/`exists` deviation** | Hard-coded true since cycles 1-6 on the note that plexsim owns no file. zen rows whose file is unreachable answer `false`/`false` AND a plain **404** to the byte read, where plexsim answers true and a **502**. Now that links can be live, the Get Info dialog says the file is there and the next request fails | Decide whether plexsim should probe its own upstream, and whether an unresolvable part should answer 404 rather than 502 |
| **Season and episode `Guid` on a catalog built before cycle 44** | The builder now writes them; a catalog already on disk carries none, and the load-time pass cannot invent ids it never fetched | Rebuild the catalog, or accept the absence and say so |
| **A season's own `year` at LOAD** | The load-time correction keys on the STORED sort title, because both references' year-carrying seasons store one (kids' `Season 1`, zen's `Egghead`) and plexsim's builder wrote a year with none. That is 2 witnesses for the positive side | A third year-carrying season, from a real library |
| **`POST servers.plex.tv/servers.xml`** | Measured in the harness capture (57 of them against 3 device announces) and NOT built: it is a plex.tv write, out of scope under §8/§9. So plexsim registers but never tells plex.tv what libraries it holds | Only a decision to widen the plex.tv scope settles it |
| **The `.webm` container change's effect on a real browser** | Driven across all 78 profile FILES, the browser profiles answer the same outcome either way -- but no real Plex Web client has played the row since the change | Play the row in a real client through CDP |
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
| ~~**A non-alphanumeric `firstCharacter` bucket**~~ | **CLOSED and BUILT in cycle 41.** Two writable copies were driven through digit, ASCII punctuation, non-ASCII punctuation and letter classes. The axis's `#` row aggregates every ASCII nonletter, while an explicitly requested punctuation or digit bucket stays literal; non-ASCII punctuation keeps its own bucket | — |
| **The non-English article strip** | `La` (4 rows) and `Das` (1) are stripped while `Les`, `Die` and `O` are not, so the article list is per-item LANGUAGE and not mechanical. Deliberately NOT implemented — plexsim's catalog carries no per-item language | Nothing short of a library with real per-item languages |
| **`banners` and `thumbs` answering EMPTY** | Both are 200 on both references; `banners` is empty on both, but `thumbs` is populated from the media BUNDLE, which plexsim has none of. The empty answer is the honest shape, not a measured value | Nothing to settle: plexsim has no bundle |
| ~~**A merge on a SHOW**~~ | **CLOSED and BUILT in cycle 41.** Both copies merged two shows, deleted the absorbed show/season/episode, combined corresponding leaf Media and emitted the exact delete/survivor frame sequence. The regression uses a synthetic two-hierarchy catalog | — |
| ~~The `episodeSort`/`flattenSeasons` row effects at the SECTION level~~ | **SETTLED in cycle 15.** The library-wide write produces exactly the item path's row effects, and the item value is a tri-state where `-1` inherits — measured in all four combinations | — |
| ~~**Whether a cascade CREATES a borrowed artwork field**~~ | **SETTLED in cycle 27: it does.** Cleared season 7's poster on the copy (its episodes lost `parentThumb`), picked one back, and all three episodes carry it again at the new stamp. A borrowed field is DERIVED from its owner: created, moved, and removed when the owner's goes | -- |
| **`DELETE /library/metadata/{id}` on a season and a show** | The copy answers **400** to every media-item DELETE — episode, season, show and movie alike — while a collection DELETE is a clean 200, with `allowMediaDeletion` true and the media mounted read-only. So whether a real PMS cascades a season delete onto its episodes is unproven, and plexsim's own cascading delete is unverified. The merge half of the same question was settled in cycle 14 | A claimed copy with writable media |
| **The `queueRegenerated` frame's DELAY** | Measured at +63.1 s after a library create and +7.1 s after an analyze, and by cycle 14 at +2.4 s after a season split and +1.2 s after a collection add. plexsim pushes its frames synchronously and emits it with the write | Nothing a synchronous server can reproduce; recorded rather than faked |
| ~~**The non-alphanumeric `firstCharacter` bucket** (duplicate)~~ | **CLOSED by cycle 41's controlled two-copy matrix.** See the canonical row above | — |
| ~~plexsim's `Server:` banner~~ | **SETTLED in cycle 16 for the API, CORRECTED in cycle 53 for the framework.** Neither reference sends one on fifteen API paths and every status, so plexsim sends none there — but a framework route handed a `com.plexapp.*` state cookie it cannot unpack answers Tornado's empty 200 carrying `Server: TornadoServer/1.0`, and plexsim reproduces that on the framework path alone | — |
| ~~The translation table's coverage~~ | **CLOSED for the complete safe-read Plex Web 4.147.1 plan in cycle 41.** 374 guarded paths per server covered all 124 safe-read families and every known localization-bearing variant. Four missing 1.42.2 source pairs were captured in all 28 languages (112 values) and built. Fifteen byte/private/write-shaped cells are explicitly guarded and cannot contribute a safe JSON localization pair | — |
| ~~The XML half of the localisation~~ | **SETTLED in cycle 17.** Diffed language by language on four paths in de and fr: plexsim moves exactly the attribute KINDS zen does (`Filter.title`, `Sort.title`, `Field.title`, `Type.title`, `Directory.title`, `Hub.title`), with no kind on either side the other lacks | -- |
| ~~A ROW's XML attribute ORDER~~ | **SETTLED in cycle 17.** 25 element buckets, each a topological sort of every PAIR both references emit and each verified as a superset of every observed sequence; 634 inverted pairs fixed and 0 remain | -- |
| ~~**The 1024 / 4096 / ~41 KB header limits**~~ | **SETTLED in cycle 27, and the third REFUTED.** Bisected on kids and zen: a name of 1024 and a value of 4096 pass, 1025 and 4097 are 400s, so plexsim's two constants were right to the byte. The third is not a size -- it is a cap of 100 header LINES, whatever they weigh | -- |

| ~~**The `titleSort` punctuation set outside the two swept ranges**~~ | **CLOSED and BUILT in cycle 41.** Both versions agree over 7,531 assigned leading scalars and an 811-scalar interior discriminator: exactly U+0300..U+036F is removed in either position. The generic combining-class predicate over-removed 699 Mn/Mc scalars and missed U+034F | — |
| **Whether an EPISODE refresh runs the SHOW's activity** | The hunt reported it and one of the driver's two captures reproduced it; the other showed the episode's own seven-frame sequence. Two runs of the same request on the same copy disagreed, so only the SUBTITLE half is implemented | Run it repeatedly on a freshly reset copy and see which is the steady state |
| **The butler's per-task COUNTS** | The message TEMPLATES are measured verbatim on the copy; the numbers in them are that server's content, and plexsim fills in its own. A template whose count plexsim cannot derive honestly (albums, artists, cache files) is hard-coded at 0 | Nothing: plexsim has no music library and no cache |

| ~~**Which titles count as GENERATED**~~ | **CLOSED in cycle 31 by reading the reference's DATABASE.** `metadata_items` stores `title = ''` for every season and episode whose title the server generates, and kids' agent-titled season 7 stores `title = ''` with `title_sort = 'Season 1'` -- so the display title is derived from the index in BOTH cases and what an agent supplies is the SORT title. That is the distinction, it is in the storage and not on the row, and it exposed a real defect (see §7). plexsim's title-FORM proxy stays, and it is exact for the rows it synthesises. Original entry: The rule is measured from three directions and is not in doubt; what plexsim cannot see is the DISTINCTION. kids' season 7 is titled `Season 1` by TMDB and stores it, and 176 of zen's episodes are titled `Episode N` by their agent and store it too, so plexsim decides on the title FORM -- a proxy, and a class-C shape by construction. Every season plexsim holds is one it synthesised itself, so the proxy is exact for its own rows | Nothing short of a column the row does not carry; re-check it if plexsim ever takes season titles from an agent |
| ~~**The eleven-character fold TABLE**~~ | **CLOSED in cycle 31, 11/11 on the second writable copy.** Written one title per character on plexscratch42 (1.42.2) and read back: `Æ→E Ð→D ×→x Ø→0 Þ→P ß→s æ→e ð→O ÷→/ ø→0 þ→p`, every one reproducing the 1.43.4 table -- including the two that look like mistakes, `ø` becoming the DIGIT zero and `ð` a capital `O`. Two versions, two instruments. (The `title=` QUERY fold is a different table and is open above.) | -- |
| **The `/hubs` refusal's scope** | Both references hold a show library, so "the server-wide `/hubs` always refuses" and "it refuses because a show library exists" are the same measurement. plexsim refuses on the same terms | A reference with no show library |
| **The library delete's frame IDS** | 14 frames for a nine-row library, keyed on the server's own internal metadata ids; nothing in the response explains the id space. plexsim frames the rows it removes | Nothing the API exposes |

| **The four write-protected settings** | `PlexOnlineUrl`, `TranscoderH264Options`, `TranscoderH264OptionsOverride` and `allowMediaDeletionLanOnly` answer 403 on plexscratch even when the value written is the one already stored, and nothing in the row marks them. The copy is UNCLAIMED, so `PlexOnlineUrl` could be claim-gated; the other three are not obviously so | A claimed writable copy |
| **`server_prefs.json`'s version split** | Implemented as a FILTER on two ids rather than as two captured fixtures, because plexsim only ever reports 1.42.2. It is the fifth fixture found to be per version; the other four are split properly | Capture zen's own list if plexsim ever reports 1.43.4 |
| **`KNOWN_LANGUAGES`** | The Add Library form's accepted set is read off the captured `languageOverride` enum, not probed exhaustively: 13 values were sent and every one agreed with that list. It is the reference's own data rather than a constant nobody measured, but the two sets have not been proved identical | Probe the whole enum, if it ever matters |
| **The `allowSync`-on-the-ROW signal** | `/library/recentlyAdded` and `/library/onDeck` lead with `allowSync` and the librarySection triple and no other listing does. `allowSync` on the row is used as the tell because the XML writer cannot see the path; it holds on every row measured, but it is a proxy for "this endpoint", which is class C | A row carrying `allowSync` on some other path |
| **The `_light` projection marker** | A LIGHT row's child order is chosen by a private key the builder sets, because nothing in the row itself distinguishes the two projections. `_drop_private` strips it from both formats and the suite checks that it reaches no client, but it is a proxy for the same reason | Nothing the response exposes |
| ~~**`sort=duration`, `duration=` and a show's media columns**~~ | **CLOSED and BUILT in cycle 41.** A differentiating local catalog now carries unequal/missing row and Media durations; the filter and sort both read the primary Media value, while a missing Media sorts as absent | — |

| **The `Specials` and `[Unknown Season]` season labels** | Both are zen's alone -- kids holds neither, its two seasons being `Season N`. 4 and 5 rows, each corroborated by three independent probes (`titleSort=`, `title=`, `/search`) plus the `#` bucket, and the MECHANISM behind them is cycle 18's two-witness rule | A second library with a season 0 or an index-less season |
| **`createdAt` read as `addedAt`** | The column is real on both references (every row answers `createdAt>=2000-01-01`) and tracks `addedAt` almost exactly on zen -- 2,437 rows above one epoch against `addedAt`'s 2,436 -- but the one row that differs proves they are not the same column. plexsim holds no other creation time | Nothing the API exposes |
| ~~**The episode `contentRating` re-inherit**~~ | **CLOSED in cycle 31 by enumeration.** The entry's own condition was "a rating cleared by some path that does not go through `library.edit` would not be [re-inherited]", and there is no such path: every `contentRating` mutation in `library.py` is in `_edit` (the re-inherit), `carry` (the show cascade) or `unmatch` (the deliberate drop, which a real PMS makes too), and `edit(..., section=False|True)` is ONE function whose both paths reach `_edit`. Driven on plexsim: the item path clears to `TV-14`, the SECTION path clears to `TV-14`, a later show write moves it to `TV-Y7`, and a show unmatch drops it -- the whole measured sequence. The invariant to keep is that any NEW mutating path calls the same hook, which is cheaper than the read-time projection this entry asked for | -- |
| ~~**The `mediaCount` column on a parent**~~ | **SETTLED in cycle 27 by asking for a VALUE rather than a split.** On zen's shows `mediaCount=2` is 30, `=3` is 10 and `>>2` is 17, against 44 for `>>1`. Under a MAX-over-leaves reading those are disjoint and sum to 47, which is more shows than have any duplicated leaf at all -- so max is refuted, and the per-leaf ANY reading plexsim implements is the one that fits. The Duplicates coincidence was an artefact of only ever asking the `=1`/`>>1` question | -- |
| **`slug` minted by the agent** | Measured on 2,406 of zen's rows (every `local://` movie and show carries none) and deliberately NOT implemented: plexsim mints a `plex://` guid for every row, so the two rules coincide for its catalog. The children's `parentSlug`/`grandparentSlug` are derived from the parent TITLE rather than from the parent's slug -- a proxy that agrees today | A catalog row that fails identification |
| ~~**The five codec edit-field names held out**~~ | **CLOSED in cycle 30 by measurement, on the new 1.42.2 copy: all five are a 400 there**, so plexsim's refusal was right all along and nothing needed building. `hdr10plus` is the opposite -- a 200 on both versions -- so it became a lockable name. Original entry: `atmos`, `audioCodec`, `audioLayout`, `subtitleCodec` and `videoCodec` take an item lock on plexscratch, which is **1.43.4**. Cycle 20 established that family is 1.43.4-only on the filter and parse surface, and zen cannot be written, so 1.42.2 -- the build plexsim reports -- is unmeasurable. Held out; `hdr10plus` is a 200 that stores nothing on a single witness | A writable 1.42.2 copy. Nothing else settles it |
| **A refresh not moving `updatedAt`** | plexscratch is isolated from plex.tv, so its agent returns nothing new. What is measured is that running the refresh SEQUENCE does not by itself stamp the row; an agent returning changed metadata might | A claimed writable copy, which needs plex.tv |
| ~~**The collection and playlist action leaves' FRAMES**~~ | **CLOSED in cycle 26.** Driven side by side against the copy, plexsim reproduces all four sequences frame for frame -- 3 and 7 on a collection, 8 once its guid is `local://`, 4 and 2 on a playlist -- including the `state 2` / `metadataState: matching` opener. Cycle 23 built them; what was missing was the `ended` frame's 100 and the guid reaching a read, both fixed this cycle | -- |
| ~~**A collection's `/allLeaves` and `/grandchildren` ROWS**~~ | **CLOSED and BUILT in cycle 41.** On both copies a selected show appears in collection `/children` and carries the collection tag, while `/allLeaves` and `/grandchildren` each return an empty 200 container. Frozen's generic member projection was wrong | — |
| **The `/system/scanners/{N}` 500 BODY** | All three references answer a 641-byte `application/xml` plug-in-framework traceback; plexsim answers 23 bytes of `text/plain`. The status and type are measured, the body is PMS's own Python traceback | Nothing worth settling: the body names PMS's own file paths |
| **Which butler DELETE actually cancels** | Cycle 23 recorded a 200 inside a queue window; cycle 25 could not reproduce a queue window at all -- a task's frames arrive at +0.0 -- so there is nothing to cancel and `DELETE`, `GET`, `PUT` and `PATCH` are all 404s when idle, which is what plexsim answers. The Dashboard's Cancel button may simply have nothing to do on a copy this small | A server with a task slow enough to have a window |
| **`AC-3` carrying `AC3` in a title** | 132 zen streams. `VC-1` does NOT carry `VC1` and `E-AC3`/`E-AC-3` do not carry `EAC3`, so no punctuation-folding rule fits, and a general one breaks 58 other streams. Cycle 24 scored it and left it | A rule that separates the three, or a decision to special-case `AC-3` |
| **Streams whose stored title is the EMPTY string** | ~37 on zen; they compose `" (1080p HEVC Main 10)"` with a leading space and carry no `title` attribute in XML at all, so a published row cannot be told apart from one with no title | Nothing from a published row; it needs the server's own stored value |
| **What is left of cycle 23's butler findings after cycle 26** | Two of the three cycle 25 could not reach are now BUILT: `CleanOldBundles`' messages interleave with its own phases, and `RefreshLibraries` reports and closes the wrapper before it scans. The wrapper's own `round(100n/(n+1))` stepping is built with them. What is left is which tasks are DUE: the copy ran 22 of 23 on a fresh reset and 12 on an immediate second POST, and no row of `GET /butler` carries a last-run or next-run time | Nothing the API exposes. plexsim runs every enabled task |
| **The rest of cycle 23's butler findings, WHAT IS LEFT OF THEM (superseded by the row above)** | Cycle 25 re-measured the family on an uncontended socket and **two of the six did not reproduce**: `POST /butler/{Task}` pushes its first frame at +0.0 on a freshly reset copy, not after a 7-9 s queue delay, and `/activities` is EMPTY the moment the run returns. `general.db.backup`'s doubling reproduced and is BUILT, together with its `started` at -1 with the subtitle already set. Still unmeasured this cycle: `CleanOldBundles`' interleaved messages, `RefreshLibraries` reporting before it scans, and `POST /butler` running only what is DUE | Measure the three, on a freshly reset copy with nothing else writing to it |
| ~~**The cycle-23 `mediainfo` model**~~ **SETTLED in cycle 24** | Hunt B fitted a model on 105,558 zen streams and held it out on the 46 recorded probes: misses fall from 613/105,558 and 3/642 to 174/105,558 and 0/642. It adds the language parenthesis-strip rule (which settles cycle 22's five-witness contradiction), `eia_608` -> `Closed Captions` in BOTH titles, `Signs`/`Songs` standing in for Forced, `CC`/`HI` for SDH, `1.0` for Mono, `HDR`/`Dolby` for the dynamic range, the raw ffprobe codec name for audio and the punctuation-stripped label for video, and `providerTitle or "External"` for a sidecar's suffix. The model file is in the cycle's artefacts | Implement it; it is the largest measured block now outstanding |
| ~~**`pixelAspectRatio` and `anamorphic`**~~ | **CLOSED in cycle 41.** The broad Cycle-25 derivation remains; generated probes on both copies pin the final constant as a strict `> 5%` deviation: SAR 21:20 is false and 20:19 is true | — |
| ~~**The `videoResolution` cutoff below 720**~~ | **CLOSED in cycle 41.** Generated even-width probes on both versions put the short-wide transition between effective width 847 and 848, replacing the fitted 856 boundary | — |
| ~~**`dub` and `visualImpaired` dispositions**~~ | **CLOSED and BUILT in cycle 41.** `dub` remained correct; a retained positive subtitle witness plus false/absent controls proves `visualImpaired:true` is emitted only for the positive subtitle and never as a video flag | — |
| ~~**`languages.json`: 13 codes, not 3**~~ **the 13 are IN as of cycle 24**; the regional-tag family (`en-US`, `zh-Hant`, ...) is not | `cha guj hye kat mar mdr mon mya nno pan sin yor zul`, each 1-24 streams on zen. Separately 21 streams carry a REGIONAL `languageTag` (`en-US`, `es-ES`, `pt-BR`, `es-419`, `zh-Hans`, `zh-Hant`) the one-name-one-tag table cannot express, and `zh-Hant` changes the NAME too | Add the 13; the regional family needs an ffprobe of such a file |
| ~~**`_channel_label`'s trailing space**~~ **fixed in cycle 24**; the reference's full parenthetical form still has one witness | `8 channels (FL+FR+...)` -> plexsim writes `8 channels )` with a double space. A defect under plexsim's own model; the reference's full form has one witness | Fix the strip; the reference form needs a second witness |
| ~~**Nine more verb-table rows**~~ | **CLOSED in cycle 25**: the last of them, `/library/metadata/{id}/match`'s HEAD/PATCH/OPTIONS, is implemented. Re-measured over all seven verbs on kids, which answers 400 to each | -- |
| ~~**The SEASON and SHOW unmatch cascades**~~ | **SETTLED in cycle 25**, on an uncontended socket, twice over -- once with the show unmatched first on a freshly reset copy and once second, so the ordering is not an artefact of the state the first left behind. The media pass is state-dependent and deliberately not emulated | -- |
| ~~**`/hubs/search`'s row lead**~~ | **SETTLED in cycle 25**: `query=Sintel` hits on kids and both references agree exactly. `/library/search` leads with `librarySectionTitle` alone | -- |
| **The mediainfo LANGUAGE-part rule** | Hunt B fitted "the whole name, not the first word" to 13 of zen's subtitle rows. Implemented, it BREAKS the recorded corpus: `Русский (Full) - Кубик в Кубе` with language `Русский` drops it on a real PMS, which only the first-word rule gets right. Five witnesses across both corpora: `Русский`, `English (Weeb)`, `dansk` dropped; `norsk bokmål`, `Tiếng Việt` kept. The narrowest model that fits all five -- "a MULTI-token name whose title is exactly that name is kept, everything else takes the first word" -- is fitted to two positive witnesses and is class D | A library with more multi-token language names. NOT implemented |
| **`eia_608` labelled `Closed Captions`** | Real on zen (3 streams), and it appears in the PLAIN display title as well as the extended one -- a different composition path from the codec label, which is why it was not implemented alongside the language rule | Implement it on the display-title path, having measured which of the two carries it |
| ~~**The `videoResolution` 480 bucket**~~ | **CLOSED and BUILT in cycle 41.** Both copies agree: at height 360, effective width 847 is `sd` and 848 is `480`; scanner even-width normalization is covered | — |
| **The dynamic-range part carried by `HDR`/`HDR10` in a title** | Two witnesses on zen, and they disagree about whether the match is a prefix or an any-word test | A third witness |
| **`languages.json` missing `zul`, `cha`, `kat`** | Real on zen (4 of 6,581 language-carrying streams); plexsim's catalog holds no such track, so it changes nothing today | Add the three, having checked the 639-2/T and 639-1 pair for each |
| **The collection and playlist `/refresh` and `/unmatch` FRAMES** | Measured in full this cycle -- a collection's refresh is 3 frames on an agent guid and 8 on a `local://` one, its unmatch 7, a playlist's 4 and 2 -- and they carry `state 2` with `metadataState: "matching"`, plus a `state 3 matching` on a MOVIE and SHOW unmatch, which plexsim emits nowhere. plexsim also pushes ZERO frames for a movie or show unmatch and no cascade for a season's. The STATUSES are implemented (cycle 21); the sequences are not | Implement the four sequences and the two missing `metadataState` values; the measurements are in cycle 22's findings |
| **`DELETE /butler/{Task}` while the task is QUEUED** | A 200 that cancels it; a 404 when idle, and GET/PUT/PATCH stay 404. Measured 3/3. plexsim answers 404 always, so the Dashboard's Cancel button reports failure | Implement it; it needs a queued-task notion plexsim does not have |
| ~~**`POST /butler/BackupDatabase` running its activity TWICE**~~ | **BUILT in cycle 25**, four runs agreeing across cycles 22, 23 and 25. The `started` at -1 with the subtitle already set is built with it; `general.db.optimize` keeps its own 0-with-an-empty-subtitle opener, which is the pair plexsim had collapsed into one shape | -- |

**Cycle 26 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**The session cutoff CONSTANT and reaper tick**~~ | **CLOSED and BUILT in cycle 41.** Fifteen staggered sessions on each copy split at a 180-second eligibility boundary and disappear in global clusters 29.94/29.97 seconds apart. The process-wide 30-second reaper applies `seen < tick - 180`, giving a 180-210 second visible lifetime | — |
| **`POST /butler` running only what is DUE** | The counts are measured (22 of 23 on a fresh reset, 12 on an immediate second POST) and the MECHANISM is invisible: no row of `GET /butler` carries a last-run or a next-run time, and two runs cannot separate "its interval has not elapsed" from "it found nothing to do" | Nothing the API exposes |
| ~~**The session row's `Role` attribute order**~~ | **REFUTED and CLOSED in cycle 27.** Driven on a row carrying `Genre`, `Country`, `Collection`, `Director`, `Writer`, `Role` and `Producer`, every one of them is plainly alphabetical -- `Role` is `filter, id, role, tag, tagKey, thumb` and no `Role` carries an `actor` attribute. plexsim was already right | -- |
| **What plexsim answers to a transcode START once a decision exists** | The 400 with no decision is measured on every protocol and every leaf. What comes next is plexsim's own 404 -- it has no encoder -- and NOT the reference's, whose `start.mpd` is a 404 there while the same session's `start.m3u8` is a 200 | Nothing: plexsim cannot transcode |
| ~~**The `directPlay`/`general` 1000 profile LIST**~~ | **CLOSED in cycle 32 by a model read out of the profile files and scored over 1,872 cells** (§7, `decision.py`). The list is gone; only zen 19813's eight cells stay open, below | — |

**Cycle 34 overturned this.**

| Belief | What it really is |
|---|---|
| **"A decision re-picks the widest DEFAULT audio track when the selected one carries no language"** (cycle 32, `decision.py`) | It is not a property of the row. The same bytes answer 41 of the 78 profiles "not the first audio stream" before any client has chosen a track and answer every one of them from the visible row afterwards, with `Media`, `Part` and `Stream` byte-identical in both states; `PUT /library/parts/{id}?audioStreamID=<id>` sets it and `audioStreamID=-1` clears it. The rule was fitted to one witness read in the first state. plexsim holds a chosen track, so it models the second and records the first (§8) |

**Cycle 25 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**The `anamorphic` CONSTANT**~~ | **CLOSED and BUILT in cycle 41.** Synthetic SAR 21:20 and 20:19 rows on both copies prove the threshold is strictly greater than 0.05 | — |
| **The show-unmatch activity's PROGRESS numbers** | The PAIRS are measured -- (S01, 50), (S01, 95), (S02, 95), (S02, 100), the subtitle moving first at the old progress and the progress stepping after -- and `100 - 5 * seasons left` is fitted to the copy's two-season show, the only one it holds. **Cycle 30 built a three-season show** (`Threefold`, nine clips) on plexscratch42 and found something that fits neither model: `Analyzing Media` steps per LEAF from 25 to 50 (-1, 25, 31, 36, 39, 42, 44, 47, 50) and `Updating Metadata` sits at 50 twice, with the SHOW's subtitle throughout and no per-season pass -- enough to refute `100 - 5 * seasons_left` as the mechanism and not enough to replace it, because that show was already UNMATCHED and that server is 1.42.2 where cycle 25 measured a MATCHED show on 1.43.4 | A MATCHED three-season show, which needs a claimed writable copy -- and every writable instrument is isolated from plex.tv by design. That is the real blocker, not the season count |
| **The `Samsung` prefs row's PURPOSE** | The row is measured verbatim on both references and both library types, and it is `hidden="1"` with an empty label and summary: no settings pane draws it, so what a Samsung client does with it cannot be shown from the server side | A Samsung client, or its bundle |
| **The plug-in framework's non-localisation** | `/system` is the only one of the six plug-in containers that carries a translatable string at all -- the other five title their rows with their own lowercase key names -- so "the framework does not localise" has ONE witness and `noHistory` is a proxy for it | A plug-in container with a sentence in it |
| **The agent `Language` ORDER** | Stored per language rather than modelled, because a real PMS collates by the localised title with ICU and this project does not implement collation. Correct by construction for the 30 captured languages and undefined for any other | Nothing short of ICU |
| ~~**A `Common`'s string `userRating`**~~ | **CLOSED in cycle 31 on the second copy.** Rated all five movies of plexscratch42 (1.42.2) and read `/common` back: `userRating` is the STRING `"8"` in a `<Common>` element whose attribute order is `mixedFields, userRating, guid, type, titleSort, index, originallyAvailableAt` -- cycle 26's rule exactly, now on two versions | -- |
| **A SCROBBLE writing a history row** | Re-measured 6/6 on the copy this cycle and it overturns cycle 9, so the copy has answered both ways across sixteen cycles. What did NOT change is the timeline half, which writes no row there -- the recorded session-bound limit -- so the pair cannot be checked against each other on one instrument | A claimed writable copy, where a timeline opens a real session |

**Cycle 27 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**`PROFILE_NAMES`, the 23 aliases**~~ | **CLOSED in cycle 30.** All 78 platform names driven through a live session on plexscratch42 (1.42.2), 23 aliases included: **zero mismatches** against plexsim's table. Two versions now, and the `Player` row's fifteen attributes come back in the modelled order on both | -- |
| **The `Player` string/non-string split** | Two witnesses, six strings and eleven, and a FIXED bucket list fits the same fifteen attributes exactly as well. The type split is implemented because it also answers for an attribute neither witness carried, which is a model rather than a transcription | A `Player` carrying an attribute outside the fifteen |
| **The `width`/`height` 500s of `/photo/:/transcode`** | `width=1&height=100` is a 500 on a 1000x1500 poster where `width=2` is a 200, and `height=-1` is a 200 where `width=-1` is a 500 -- the refusal depends on the image's own aspect ratio and on a resizer plexsim does not have. The five OTHER parameters' validation is implemented | Nothing: plexsim cannot resize |
| **What a real PMS does to a borrower when the OWNER's artwork is cleared** | The three positive halves are measured on the copy; the removal half is the completion of the same rule rather than a measurement, and it is what the cycle-14 cascade check caught as a regression | Clear a show's `/art` on the copy and read its episodes back |
| **Whether a leaf with NO `art` GAINS one when its show does** | The move half is measured on the copy (an episode's `art` names the show and follows its new stamp) and the CREATE half is not: the only way to produce a leaf with no `art` at all is a show unmatch, which drops it, and the copy was not driven through unmatch-then-re-add. `parentThumb`'s create half IS measured; `art`'s is not, and cycle 20's "handed down borrowed or not" predicts it would be | Unmatch a show on the copy, give it an `art` back, and read the leaves |
| **A lower-case method holding the connection** | Both references answer nothing and hold the socket to their own 20-second idle close. plexsim closes it at once -- the same silence, sooner -- because holding a thread for twenty seconds per malformed line is a self-inflicted denial of service | Nothing worth settling |

**Cycle 28 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**The Recently Added TV ROLL-UP threshold and cap**~~ | **CLOSED in cycle 41.** Controlled one- then two-sibling batches on both copies prove the roll-up threshold; 55-family batches prove the independent 50-row cap and page-50 behavior. The cap mismatch was built | — |
| **`TV_RECENT_CAP = 50`** | A bare constant with ONE witness: zen answers `size 50, totalSize 50` at a 100-row window and pages to `offset 50`, and neither `limit=` nor `count=` moves it. Nothing on the surface explains the number, and kids' library is too small to reach it | A second big library |
| ~~**The On Deck row's projection is per VERSION, one witness each side**~~ | **CLOSED in cycle 30, and the 1.42.2 side OVERTURNED.** A fresh 1.42.2 copy carries neither `librarySectionKey` nor `Field`, on a movie and an episode alike, which is what 1.43.4 and cycle 21's fun said -- so cycle 28's patch was built from zen's single on-deck row and put the key back wrongly. `/hubs/home/onDeck` and `/hubs/home/continueWatching` DO carry the key on both versions. Original entry: zen (1.42.2) answers the detail row with `librarySectionKey`; plexscratch (1.43.4) drops that key, `Guid`, `Rating` and `Producer` and stamps `studio`. kids' On Deck is empty, so each side of the split rests on one instrument -- and plexsim follows zen because it reports 1.42.2 | A second 1.42.2 server with a row on deck, or kids driven into having one |
| ~~**1.42.2's CONTINUE WATCHING row projection**~~ | **CLOSED in cycle 30** on the new copy, with a `viewOffset` written on a movie and on an episode: the row is the detail row minus `Field`, with `librarySectionKey` present on `/hubs/home/continueWatching` and absent from `/library/onDeck` and the section leaf -- the same shape as On Deck beside it. **No resume list carries `Field`** | -- |
| **`searchTypes=otherVideos`'s metadata half, and `music`/`photos`** | No instrument holds an Other Videos, music or photo library, so only the TAG half of `otherVideos` is witnessed (it selects the movie library's tag rows and not its movies, which is what "a type-1 library too" predicts). The two other tokens are recorded from their empty answers alone | A library of each type on the copy |
| ~~**`/clients/profiles?type=42`'s seven names**~~ | **CLOSED in cycle 31 with a negative, by reading the files.** The lab PMS container is shell-accessible (§4.1), and `Resources/Profiles/` holds 79 entries -- 78 `.xml` plus a stray `sugar.cmake`, which is why the API answers 78. The seven LOCAL names carry **no distinguishing attribute**: their `<Client>` elements are as plain as any other's, so the membership is PMS's own hard-coded list and there is nothing to derive. The same read explained the row TITLE (it is the `<Client name="…">` attribute, not the file name) and all 23 aliases -- see §7 | -- |

**Cycle 29 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| **A `url=` naming a PART on `/photo/:/transcode`** | The two references DISAGREE -- kids 500, zen 404 -- so there is no rule. plexsim takes kids' 500, which is also the general "a server path that is not artwork" answer; the other five shapes in that table agree on both | A third witness at either version |
| **`TYPE_SORT_NUMBERS` beyond the four types** | Only movie 1, show 2, season 3 and episode 4 are witnessed (they are the only types either reference holds). The rest of the table is Plex's own published type numbering, which is a model rather than a measurement -- and a collection sorts among them untested | A library of another type |
| ~~**The plain `filename=`'s `_` for `+`**~~ | **CLOSED and BUILT in cycle 41.** A plus-named file on both writable copies proves the plain form maps `+` to `_` while `filename*` preserves it as `%2B` | — |
| **`/photo/:/transcode`'s `Accept-Ranges` cache tell** | Measured four fresh sizes on both references: the FIRST (generated) response omits the header and every later one sends it, which is the transcode cache answering. plexsim has no transcode cache, so there is no state to key it on | Nothing worth settling |
| ~~**The whole `clip` surface**~~ | **BUILT in cycle 37 from a measured-shape catalog.** The parent now advertises `primaryExtraKey`, inline `Extras` is uncapped and ordered, both item and section `/extras/all` projections carry the clips without inventing a librarySection triple, and a clip borrows the parent's `art` attribute while its own `/art` leaf is a 404 | — |
| **plexscratch stamps an artwork PICK at LOCAL time** | Not a rule and not a clock: all three servers' `Date` headers agree with the host to the second, and no stored `addedAt`/`updatedAt` pair on kids or zen shows the offset -- but on the copy a PICK writes `max(stored, now) + 7200` (the container's UTC offset, which therefore COMPOUNDS across writes) while an UPLOAD writes the true epoch, and the pick's own response body names the true-epoch path. Any measurement that compares a copy stamp with `now`, or two copy stamps across write kinds, is measuring this | Nothing to settle; know it before reading a stamp off the copy |

**Cycle 30 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**plexsim closing the eventsource channel at ~256 s**~~ | **CLOSED in cycle 31: it does not reproduce, and the 256 s was the PROBE's.** Re-measured with a raw socket that imposes no read timeout of its own (`settimeout(None)`, every frame logged): **open at 460.2 s** -- outliving the reference's own 430-second capture -- with 46 ping frames at gaps of 10.00 to 10.10 s and no drift, carrying real frames alongside the pings. The cycle-30 capture script is no longer on disk, so which client limit produced 256 s cannot be named, and that is the finding rather than a gap in it: **a duration measured through a client is the client's** until a probe with no limit of its own says otherwise -- §3.1 class G one step out from a pasted response | -- |
| **What makes a real PMS REDRAW a `random="1"` shelf** | The stability is measured hard -- 21 reads over seven minutes on three shelves, a different client identifier getting the same sample, and a different `count=` getting its own -- and the expiry is not measured at all. plexsim mints one seed per (shelf, count) and holds it for the life of the process, which reproduces everything seen and invents no TTL | Read one shelf for an hour; write to its key set and re-read; restart a copy's PMS and compare. Only the copy can be restarted |
| **The reference's per-socket notification BACKLOG bound** | Measured as a shape and a rough size: a dead consumer took ~415 KB of frames on the reference and then stopped queueing, with every request between 0.019 and 0.031 s throughout. Whether it drops the channel, drops frames or blocks the producer is invisible from outside. plexsim gives a sink a two-second budget and DROPS it, which is the half it can implement. **Cycle 31 quantified plexsim's side and the two are within noise**: a dead consumer absorbs **400,667 bytes** over 350 writes at ~0 ms each, then writes 351 and 352 take exactly 2.00 s and the sink is dropped at 353 -- 4 seconds of writer cost in total, not the 20 s of one write that cycle 30 started from -- while a **slow but alive** consumer (draining 4 KB every 20th write) survives 200 writes with a slowest write of 0.00 s and the channel still open, which was the risk the fix introduced. The 400 KB is the HOST's TCP send buffer (`net.inet.tcp.sendspace` 131072, `autosndbufmax` 4 MB), so it is a platform number and not a constant to write down (class D) | The reference's side stays outside-in. plexsim's side is now measured; re-run `c31/D/backlog.py` on a host with a different send buffer before quoting the 400 KB anywhere |
| **The row-CREATION frame sequence** | Measured in full on the copy (eight frames per new row -- `state 0` with an empty title, five `state 1`s, `state 4 loading`, `state 5` -- plus a `library.update.item.metadata` activity each) and unreachable: plexsim cannot create a metadata row during a scan, because its catalog is loaded at start. Recorded rather than built | Nothing until plexsim has a scanner that adds rows |
| **Every notification-channel rule this cycle** | Single-witness BY CONSTRUCTION, and it is a SAFETY rule rather than an instrument limit: kids and zen are GET and HEAD only and **a websocket or eventsource must never be opened on either**, so the copy is the only server a socket may be opened on at all | A second writable copy is the only answer, and plexscratch42 (§4.6) is now one -- re-run the ping and backlog captures there for a second version as well as a second witness |
| **plexscratch42's own SETTINGS counts** | The copy is unclaimed and unsubscribed, so `/:/prefs` answers 151 rows against zen's 173 and `/library/sections/prefs?type=1&agent=tv.plex.agents.movie` answers 14 against 19. plexsim's fixtures match ZEN's counts exactly, so they are right and the copy's numbers are its own limit -- a class-E trap sitting inside the new instrument | Nothing: it needs a claimed, subscribed server. **Never read a settings count off plexscratch42** |

**Cycle 31 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**The `title=` query FOLD table**~~ | **CLOSED in cycle 32.** Hunt B swept all 27 probe letters over U+0080..U+024F plus eleven further ranges on kids (11,662 requests, then 1,666 codepoints by alternation) and U+0080..U+024F on zen (4,270): 102 characters, identical on both versions. The driver then swept the SORT title by writing 2,076 codepoints on both copies (100 characters, the same table minus two case folds) and the hub matcher with 825 probes. All three are built | — |
| **An episode with no still carrying the SEASON's poster path** | Hunt B reported 448 such rows on zen and counted only **21 live** (427 trashed). Re-measured over 1,500 live episodes: **0** carry a thumb equal to their `parentThumb` -- 1,372 own one and 128 have none -- and the derivation the same hunt scored at 0 misses over 9,287 rows says `thumb` is own-or-absent. Recorded, not built | A live witness, fetched by `thumb == parentThumb` over the whole library rather than a window |
| ~~**`/livetv`'s 403**~~ | **CLOSED in cycle 32, class E.** Both CLAIMED references answer `/livetv` with the three-row container, `/livetv/dvrs` and `/livetv/sessions` empty and `/livetv/epg` a 404 -- plexsim's answers exactly. The 403 is the unclaimed copies', which answer the container at `/livetv/` | — |
| ~~**The decision model behind `DIRECT_PLAY_PROFILES`**~~ | **CLOSED in cycle 32.** Built as `decision.py`: exact outcome on 1,871 of 1,872 cells and exact refusal text on 1,864. Firefox and Edge are explained (only `dash`/`hls` targets) | — |
| **plexsim's `title=` comma alternation, on its own fixture** | The rule is measured on kids (5 rows) and zen (3,887) and the COMMA half cannot be checked on the UI fixture: all three of its movies share the word the check picks, so `Steel`, `Steel,` and `Steel,,` all answer 3. The dot half discriminates (3 -> 0) and is what the suite proves | A fixture whose movie titles share no word |

**Cycle 32 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**zen 19813: a decision over audio tracks that carry no language**~~ | **CLOSED in cycle 34 by making the row this entry asked for.** plexscratch's row 60 -- an mp3 2.0 and an ac3 5.1, neither tagged with a language, `+faststart` so `optimizedForStreaming` matches -- answers 68 of the 78 identically to zen 19813, the other ten being its sidecar subtitle refusing earlier. Two rules came out: a decision reads EVERY audio stream (the first in index order the profile cannot play is the codec it names) unless the profile declares `StreamUnselectedIncompatibleAudioStreams`; and the eight cells were never about the row -- they are a per-user audio selection the row does not carry, flipped by `PUT /library/parts/{id}?audioStreamID=` and cleared by `-1`, with `Stream` byte-identical either way. §8 has what is recorded and not emulated | — |
| ~~**The hub search matcher's CASE handling**~~ | **CLOSED and BUILT in cycle 39.** The complete cycle-32 2,076-codepoint domain yielded 659 compatible cells and 97 supported ASCII-letter mappings, exactly the letter entries of `_TITLE_FOLD`; a 333/326 fit/holdout split had zero misses. The partial score pass applies the table to the RAW query before ASCII-only case folding, while whole-title exactness is a separate broader pass with measured long-s and two-letter macron-`o` exceptions. The prior lowercase-first implementation promoted eleven unsupported capitals and lost `Ð` | — |
| **The fold table outside its swept ranges** | NARROWED in cycle 33: 534 characters with an ASCII COMPATIBILITY base -- Greek Extended, Roman numerals, circled, parenthesised and mathematical letters, the `\ufb01`/`\ufb02` ligatures, superscripts, Armenian and Cyrillic -- were substituted into a `title=` query on kids and NONE folds, which is what plexsim's table says. What is still unswept is the letters of scripts with no ASCII base (Hebrew, Arabic, the Indic scripts, CJK), where a fold has nothing to fold TO | The remaining ranges through `handover/tools/sortfold.py`, if a reason ever appears |
| ~~**Decision limitations no measured row trips**~~ | 24 rows exercised width, level, channels, bitrate and `optimizedForStreaming`. `video.frameRateMode` and `video.separateFields` are read as absent/0, `part.has64bitOffsets` from the Part, and a `Match` with a `list` on `video.profile` was never tripped. A multi-part Media was never driven | A row that trips each -- zen holds interlaced, VFR and 10-bit rows -- driven against the profiles that name the limitation |
| **Concurrent refreshes of one row** | The references DISAGREE: 1.43.4 coalesces the second refresh (one activity, 8 frames), 1.42.2 runs both. But every 1.42.2 row is unmatched on the only 1.42.2 copy, so its refresh is three frames long and the comparison is confounded. plexsim runs both | A matched row on a 1.42.2 server that can be written to -- which needs a claimed 1.42.2 copy |
| ~~**The JSON byte form beyond what was swept**~~ | **CLOSED in cycle 33**: the float form is the shortest round-trip decimal with at least one place (`10.0`, `24.0`, `2.35`, `0.3306` on zen's own rows), which is exactly Python's `repr` and what plexsim writes | — |

**Cycle 33 added these.**

| Item | Why it is thin | How to settle it |
|---|---|---|
| **Whether a 1.42.2 server really stops a refresh after the `matching` pair** | On plexscratch42 an online-agent row's refresh ends at `state 3 queued` and no cascade runs; on plexscratch (1.43.4) the same shape continues. Both copies are isolated from plex.tv, but one is a COPY whose rows carry local metadata bundles and the other a FRESH install with none, so the continuation tracks the bundles rather than the version. plexsim runs the cascade, which is 1.43.4's observable behaviour and possibly 1.42.2's too | A CLAIMED writable 1.42.2 server -- the same instrument four other entries want -- or a 1.42.2 copy given a metadata bundle for one row |
| **The eventsource and websocket HANDSHAKE header order** | Every other response class was read off both references; a socket may never be opened on kids or zen, so the two handshakes are the only responses on the surface whose header order has no witness. plexsim keeps the order it already sent, with `Date` last | A claimed reference that may be socketed, or a capture from the lab's own client through the harness |
| **`/system/search?query=` ordering** | The bare `/system/search` puts `Cache-Control` last and the query form puts it early (`X-Plex-Protocol, Cache-Control, Content-Length, Content-Type, Set-Cookie, Connection, Date`), measured on both references. plexsim's `/system/search` is a static empty container with no query semantics, so the variant has nothing to attach to | Only worth building if plexsim ever answers a real `/system/search` query |
| **Everything the write-frames hunt did not reach** | The library create and delete counts, a show-level refresh, the list type-15/18 pairs, the subtitle upload, the stream `PUT`, the butler, `match`, a scoped scan and `showOrdering`, each per version. The hunt ran out of budget after the refresh opening | Cycle 34's first territory, with the agent confound above designed out |

| Item | Why it is thin | How to settle it |
|---|---|---|
| ~~**`video.separateFields` read as a constant 0**~~ | **CLOSED in cycle 36 by MAKING the row.** An MBAFF interlaced encode (`-flags +ilme+ildct -x264-params interlaced=1:tff=1`) muxed into the copy's media and scanned gives `scanType: "interlaced"` and `displayTitle "1080i (H.264)"` on both versions, and `mediainfo._scan_type("tt")` already derives it. Driven against all 78 profiles on both copies: only `RokuDLNA-5.1` refuses, and on `video.scanType`, while iOS and tvOS -- the two profiles naming `video.separateFields NotMatch 1` -- direct-play it. The constant 0 is RIGHT for this shape | A row that sets it needs field-coded (PAFF) pictures, which neither libx264 nor ffmpeg's mpeg2video encoder can write |
| ~~**The dual-layer Dolby Vision disagreement**~~ | **CLOSED in cycle 36, and BOTH notes were right.** Every one of zen's 40 DV Media carries one video Stream -- including all 17 at `DOVIProfile 7` with `DOVIELPresent: true` -- except one m2ts, which carries two: a 4K base track with no DOVI and a 1080p profile-7 track with **`DOVIBLPresent: false`**. The discriminator is `DOVIBLPresent`, not `DOVIELPresent`, and `mediainfo.py` already keys on exactly that | — |
| ~~**The search fuzzy BAND's PRUNING**~~ | **BUILT in cycle 37 after reading the table and both binaries.** The prune is `top=10 AND score<=200`, with `score = editdist1 + 32 - bit_length(rank)` over the live two-vocabulary schema; the fallback gate, short-token rule and AND-of-OR FTS construction are implemented too. A fresh 93-query holdout scored **822/822** after using the FTS table's ICU primary/shifted collation | — |
| **The Dolby Vision profile-5 veto's discriminator** | Of the 38 client profiles with a matching http target, exactly ONE converts a profile-5 source, and it is the only one whose target names the source's own codec (`Plex Desktop`, `videoCodec="h264,hevc,*"`). "The target names the source codec" and "the target declares `*`" fit the same 78 cells, because those are the only two profiles on the surface that declare either. The narrower is built | A reference profile that names `hevc` without `*`, or a second positive witness of any kind |
| ~~**`partIndex` on a multi-part Media**~~ | **BUILT in cycle 37.** The catalog builder groups only the measured `Title - cdN.ext` spelling, each Part owns its own debrid link, Media duration is the sum, `partIndex=0/1` projects and evaluates that Part, out-of-range is a 400 and `-1` returns the measured all-parts conversion refusal | — |
| ~~**The spellfix vocabulary rebuild schedule**~~ | **CLOSED and BUILT in cycle 38.** The complete caller set is two historical migrations (plus a third on 1.43.4) and database optimization. Optimization is reached by Butler OptimizeDatabase, `PUT /library/optimize` and section deletion. Automatic Butler eligibility is a roughly five-minute transition into its local window with the task enabled and `floor(time/86400) % 7 == 0`, not elapsed time since the last run. Edits do not rebuild | — |
| ~~**Faithful spellfix tag ranks from a catalog**~~ | **CLOSED and BUILT in cycle 39.** The 1.42.2 table has 71,124 exact tokens. Generic row sources -- ordinary tags plus Chapter/Review/Marker/CommonSenseMedia/Network tags, Guid ids, Rating images, studio and four presence constants -- reconstruct 71,109; the complete server-static remainder is 28 measured optimize/show-ordering strings. A 697,334-query bounded comparison found 187,616 post-gate people-membership differences from the old extractor, including GUID and static-string live witnesses. plexsim now indexes those sources and the 28-string sidecar, not a library-specific 71k-token dump | — |
| ~~**Final equal-score search ordering**~~ | **CLOSED and BUILT in cycle 38.** Title candidates begin exact-first, then raw-title bytes/signed-id descending and unique. The final hub sort compares the unrounded double score only, through the reference build's unstable libc++ comparison sort; formatting to five decimals is later. The reconstruction is exact on 331 eligible hubs, 2,306 rows and 1,578 equal-wire-score pairs. Flat search's comparator reads an empty generic attribute, so it preserves movie/show/episode bucket order | Capped/tag-associated groups lack their full pre-sort vectors; that is an exact-permutation validation limit, not an unknown comparator |
| **A NUL in a filter value** | 40 cells on both references, and the refusal keys on an interaction of column KIND and OPERATOR that no model explains (`title=` and `title!=` refuse, `title==` and `title>>` do not; `genre=` refuses, `genre!=` does not). No Plex client can send one | Nothing worth settling; recorded with the cells |

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

**The lab's PMS container is SHELL-ACCESSIBLE, and that is an instrument (cycle 31).**
`ssh ben@kids docker exec plexlab-plex sh -c '…'` reads the image's own files -- no PMS request, no
write, nothing that can alter the reference -- and it answers what the API only implies.
`/usr/lib/plexmediaserver/Resources/Profiles/` settled two §3.3 entries in twenty minutes: the 78
profile rows are `<Client name="…">` attributes rather than file names, the 23 `Player.profile`
aliases are 22 `redirect="…"` attributes plus one name-versus-filename difference, and the
`type=42` seven carry no marker at all. `Resources/` also holds the decision model's inputs
(`<DirectPlayProfiles>`, `<TranscodeTargets>`, `<CodecProfiles><Limitations>`). **Read, never
write**, and remember it is the IMAGE: a `docker compose` recreate restores it, and nothing there
should ever be edited.

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

**Ports (cycle 55).** Hunters on `32587`/`32588` (frozen `158f836`); proofs `32592`/`32593`;
the implementation agent `32597`/`32598`; serial UI `32595`; live catalog `32596`. All stopped
and the processes listed. plexscratch42's Firefly carries no net state from the hunts (every
write undone), except `viewUpdatedAt` stamps, which outlive their undo by measurement.

**Ports (cycle 54).** The hunters had `32587` (A) and `32588` (B) on frozen `f551bf7`; the
proof driver used `32592`/`32593`, the hunt-A regression cross-check `32594`, the serial UI
suite `32595` and the live catalog `32596`. `32502` and `8787` are the standing exceptions.
All cycle-owned listeners are stopped at cleanup and the processes listed, not just the ports.
plexscratch42 now holds twenty `Container Probe C54 *` rows in section 1 (files under kids'
`/home/ben/plexscratch/media-long/movies/`, which plexscratch will also see on its next scan),
and movie 4 there carries a `viewOffset` of 700000 from the `includedAt` revalidation.

**Ports (cycles 50–51).** The first root full-UI pass used `32683` and passed, but was discarded
when the serial-suite rule found worker-owned duplicate runs on `32671` and `32752` during part of
its lifetime. The workers stopped only their own processes. Root then ran the authoritative
pristine serial UI pass alone on `32684`; `32598`/`32599` were clear before it started. Focused
Cycle-51 audits used ephemeral loopback ports and `32751`/`32752`. Every named listener is stopped
at final cleanup. The disposable PMS copies were restored/recreated, proved unclaimed and attached
only to `internal: true` networks, then released; no launchd job was registered.

**Ports (cycle 43).** Root served frozen `5874d01` on `32643`/`32644`, ran the serial live suites
on `32645`, and Territory C used `32647`/`32648` plus suite helpers `32649`/`32650`; Territory B's
interrupted duplicate UI run used `32654`. At startup, 32587/32588 were unexpectedly still held by
Cycle-41 frozen trees (pids 24850/24852); cwd/command-line ownership was verified before killing
only those two processes. At final cleanup every Cycle-43 port and both stale ports are free.
`32502` and `8787` remain the standing foreign listeners and were left alone. No writable reference
was used, protected snapshots are byte-identical, and no launchd job was registered.

**Ports (cycle 40).** The hunters had `32587`/`32588` on frozen `bcda2f9`; the inward trace used
`32640`, fail/pass proof used `32592`/`32593`, and the serial suites used `32589`. `32502` and
`8787` are the standing exceptions. All cycle-owned listeners are free again, both disposable
references were reset/restored, and the guarded reference snapshots are byte-identical.

**Ports (cycle 39).** The hunters had `32587`/`32588` on frozen `7d98293`; the serial suites used
`32589`. `32502` and `8787` are the standing exceptions. All cycle-owned listeners are free again,
and the guarded reference snapshots are byte-identical. The first official-client capture was
discarded after its gate admitted two websocket upgrades and a theme read; the accepted replacement
was installed before navigation and contained only the 21 declared safe GETs.

**Ports (cycle 38).** The hunters had `32587`/`32588` on frozen `9403d3d`; the proof and serial
suites used `32589`. `32502` and `8787` are the standing exceptions. All cycle-owned listeners are
free again; both disposable copies were reset/restored and the guarded reference snapshot is
byte-identical.

**Ports (cycle 37).** The hunters had `32587`/`32588` on frozen `9a2d9c2`; the proof driver used
`32592`/`32593`; temporary catalog-shape probes used `32637`/`32638`; and the serial suites used
`32594`. `32502` and `8787` are the standing exceptions. All cycle-owned listeners are free again;
the reference snapshot before and after the cycle is byte-identical.

**Ports (cycle 36).** The hunters had `32587`/`32588` on a frozen `d179203` tree; the driver used
`32500` (the suites), `32505`-`32512` (the refusal, id-grammar and copy-comparison probes, and the
by-hand gated and Dolby-Vision proofs), `32592`/`32593` (the proof driver) and `32594`/`32595` (the
two gated servers the token half was proved against). `32502` and `8787` are the standing
exceptions. All are free again, and the processes were listed as well as the ports.

**Hunt A rebuilt plexscratch42 and reset plexscratch, so BOTH tokens changed mid-cycle.**
plexscratch42's machineIdentifier is now `0d2eb677…` and it holds a third library, `Threefold`
(section 3, 15/9 leaves). `S42_TOKEN` must be re-read from `/home/ben/plexscratch42/token.txt` and
`SCRATCH_TOKEN` from `scratch.sh token` after any `reset` -- this cycle lost two probe runs to a
401 that looked like a permissions change and was a stale token.

**Ports (cycle 35).** The hunters had `32587`/`32588` on a frozen `c369500` tree; the driver used `32592`/`32593` (the proof driver), `32594` (one hand proof), `32596` (the header re-check) and `32587` (the suites, after the hunters finished). `32502` and `8787` are the standing exceptions. All are free again.

**Ports (cycle 34).** No hunters this cycle, so no hunter ports. The driver used `32587` (the fixture server for the suites and for the before/after comparisons), `32588` (the baseline tree's server), `32592`/`32593` (the proof driver, from the cycle's earlier capture-driven patches) and `32596` (the replay target). `32502` and `8787` are the standing exceptions. All are free again.

**Ports (cycle 33).** The hunters had `32587`/`32588` on a frozen `df87253` tree; the driver used
`32589` (its own header-sweep server), `32592`/`32593` (the proof driver) and `32500` (the suites).
`32502` and `8787` are the standing exceptions. Both hunters were relaunched once after a weekly
rate limit killed the first pair inside a minute (§10), and neither left a server running.

**Ports (cycle 32).** This cycle was begun by one session (`eba8c320-…`) and taken over by
another (`61d69236-…`) at the user's direction, and the ports tell that story. The first session's
hunter servers on `32587`/`32588` (serving its frozen `head32` tree) were still up when the second
started -- its hunts had finished two hours earlier -- and were killed at cleanup. The second used
`32592`/`32593` (the proof driver), `32594`-`32597` (hand proofs of the refitted checks and the
`test_all` comparison) and `32500` (the suites); all are free again. `32502` and `8787` are held as
before. On kids, `plexscratch` keeps `32410` and `plexscratch42` `32411`.

**Ports (cycle 31).** Re-checked after both hunters had stopped, with the processes listed as
well as the ports: `32502` and `8787` are held, the same two every cycle since 17 has ended with --
and `32502`'s listener belongs to the `com.plexsim.dev` launchd job of ANOTHER session, which is
this file's standing exception and not something to remove. Everything cycle 31 used is FREE again:
`32587`/`32588` (the hunters'), `32592`/`32593` (the proof driver's), `32500` (the suite runs) and
`32505`-`32515` (the driver's own probes: the SSE lifetime capture, the backlog probe, the
`contentRating` and `titleSort` drivers and the re-measurement servers). **Neither hunter started a
server**, and both said so with `ps -o lstart` evidence rather than a bare assertion, which is the
first time that has been checked from the agent's side. On kids, `plexscratch42` holds `32411` and
`plexscratch` `32410`; leave `32599` and `32598` free for the suite's own gated and `--web-root`
servers.

**Ports (cycle 30).** Re-checked after both hunters had stopped, with the processes listed as well
as the ports (`pgrep -af "server.py --port"`): `32502` and `8787` are held, the same two every cycle
since 17 has ended with. Everything cycle 30 used is FREE again: `32587`/`32588` (the hunters'),
`32592`/`32593` (the proof driver's), `32500`/`32505`/`32506`/`32507` (the suite runs) and
`32594`-`32597` (the by-hand `runblock` proofs, which need a server per parent tree -- cycle 30
needed them because `sdrive` cannot prove a check that starts its own server). **Two servers were still up at cleanup** and had to be
killed by hand, 5 hours after the hunts ended: `32587`'s was the one `setup.sh` starts FOR the
hunters (the script starts them and nothing stops them -- a gap in step 12's checklist), and
`32588`'s was **hunt B's own restart of that frozen tree**, which its report described as left
running deliberately "for the driver". Listing the processes rather than the expected ports is
what caught both, as in cycle 28.

**And a finished agent acted again, after the merge.** Hunt B's task notified a second time once
cycle 30 was pushed, with a correction: it had re-measured its finding against a freshly restarted
32588, then started ANOTHER server (pid 23917) on that port, noticed the frozen tree had been
deleted under it, and killed it. So the honest sequence is: the hunt left one server up, the
cleanup killed it, and the agent then created and removed a third one unobserved. §4.5's "a
finished agent can still act" now has a fourth witness, and the rule it implies is the one this
file already gives -- re-check the processes after any late task notification, not only at
cleanup. The correction's substance changed nothing: it re-confirmed the refusal finding 3/3
against a pristine instance, which is what was implemented.

**No launchd job was registered by this cycle, and one exists.** `com.plexsim.dev` is held by a
DIFFERENT session (`launchctl print gui/501/com.plexsim.dev` names a scratchpad under
`…/0c9d535b-…/`), and it is what holds `32502`. Leave it: it is not this programme's to remove,
and a future cycle checking "no launchd job" should expect to find exactly this one. **New this cycle, on kids:** `plexscratch42` holds `32411` (its relay)
and `32499` inside its own namespace; `plexscratch` keeps `32410`. Leave `32599` and `32598` free for
the suite's own gated and `--web-root` servers.

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

**Ports (cycle 29).** Re-checked after both hunters had stopped, with the processes listed as
well as the ports: `32502` and `8787` are held, the same two every cycle since 17 has ended with.
Everything cycle 29 used is FREE again: `32587`/`32588` (the hunters'), `32592`/`32593` (the proof
driver's), `32500`/`32505`/`32506` (the suite runs), `32503`/`32504`/`32507`-`32511` (the driver's
probes) and `32594`-`32597` (the by-hand `runblock` proofs, which need a server per parent tree).
Hunt A started one `--web-root` instance on `32591` and killed it; hunt B started none and killed
the plexsim it was given. No launchd job was registered.

**Ports (cycle 28).** Re-checked after both hunters had stopped and every server this cycle
started was killed: `32502` and `8787` are held -- the same two every cycle since 17 has ended
with. Everything cycle 28 used is FREE again: `32587`/`32588` (the hunters'), `32592`/`32593`
(the proof driver's), `32500`/`32501`/`32503`/`32505`/`32506` (the suite runs and the probes)
and `32594`-`32597` (the by-hand `runblock` proofs of the refitted checks, which need a server
per parent tree). **One hunter DID leave a server running**, on `32589` -- a port this file's
scan does not list, serving the frozen `head28` tree, found only because the cleanup listed the
`server.py --port` processes rather than the ports it expected. Step 12 exists for exactly that;
it is now worth listing the processes as well as the ports. No launchd job was registered.
`32599` is `test_ui_surface.py`'s own gated server and `32598` the `--web-root` instance its
asset check starts; leave both free. One suite run was killed mid-flight by a harness timeout
this cycle -- its server stayed up and had to be killed by hand before the next run, because a
suite pointed at a fixture another run has written to reads as a collapse (section 10).

**Ports (cycle 27).** Re-checked after both hunters had stopped and every server this cycle
started was killed: `32502` and `8787` are held -- the same two every cycle since 17 has ended
with. Everything cycle 27 used is FREE again: `32541`, `32543`, `32546`/`32547`,
`32587`/`32588` (the hunters'), and `32592`/`32593` (the proof driver's). **Neither hunter left a
server running** and no launchd job was registered. `32599` is `test_ui_surface.py`'s own gated
server and `32598` the `--web-root` instance its asset check starts; leave both free -- and see
section 10's new rule about never running two suites at once, which is what cost this cycle an
hour.

**Ports (cycle 26).** Re-checked after both hunters had stopped and every server this cycle
started was killed: `32502` and `8787` are held -- the same two every cycle since 17 has ended
with, `32596` having been released since cycle 25. Everything cycle 26 used is FREE again:
`32541`, `32543`, `32546`/`32547`, `32587`/`32588`/`32589` (the hunters'), `32592`/`32593` (the
proof driver's) and the `32594`-`32598` comparison servers the driver started and killed.
**Neither hunter left a server running**, as in cycles 18, 19 and 25, and no launchd job was
registered -- cycle 25's `com.plexsim.c25b` was the incident that put an explicit prohibition
in both prompts.

**`32598` is not a free port, and this cycle proved it the expensive way.** The note below has
said so since cycle 18 and cycle 26 started a comparison server there anyway: the UI suite ran
for fifteen minutes and then died with an unhandled `HTTPError 404` inside cycle 16's `/web`
asset check, which starts its OWN `--web-root` instance on that port and got the wrong server's
404 instead. It reads as a crash in an unrelated check, twenty minutes from the top of the
suite. **The two reserved ports are `32598` and `32599`; pick comparison ports well away from
them** -- `32592`/`32593` are the proof driver's and `32541`/`32543`/`32546` are the suite's
usual bases.

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

### 4.6 Disposable write reference pinned to 1.42.2 — `plexscratch42` (WRITE FREELY)

Four §3.3 entries across cycles 21, 25, 27 and 28 all ended with the same sentence: *a writable
1.42.2 copy would settle this.* `plexscratch` is a copy of the lab's config and is therefore
**1.43.4**; zen is 1.42.2 and is production, read-only. Cycle 30 built the missing instrument.

It is **not a copy** — it is a fresh install of `plexinc/pms-docker:1.42.2.10156-f737b826c`, the
exact build zen runs and the one plexsim reports, so there is no identity to strip and it has never
held a plex.tv token. It lives on **kids** at `/home/ben/plexscratch42` (source in
`harness/scratch42/`) and answers at `http://100.85.222.1:32411`.

```bash
cd /home/ben/plexscratch42
docker compose up -d plex            # then wait for /identity to lose startState
docker compose up -d loopback relay
cat token.txt                        # the .LocalAdminToken clients send
docker compose down                  # and `rm -rf config` to start over
```

**What it can show.** Every question about a 1.42.2 server's *grammar*: which edit-field names it
takes, what a `Player.profile` resolves to, what shape an On Deck or Continue Watching row is, what
`/filters` and the `Meta` descriptor advertise, which settings exist, what a write's frames are.
In one afternoon it closed four §3.3 entries and **corrected cycle 28's own On Deck patch**.

**What it cannot show, and these are class-E traps.**

- **Anything needing real metadata.** It is isolated from plex.tv by construction (`internal:
  true`, no DNS), so every item is UNMATCHED: `local://` guids, filename titles, no `Guid`, no
  `Rating`, no people tags, an empty `/genre` axis. Those questions are zen's.
- **A SETTINGS COUNT.** Unclaimed and unsubscribed, `/:/prefs` answers 151 rows where zen answers
  173, and `/library/sections/prefs?type=1&agent=tv.plex.agents.movie` answers 14 where zen and
  kids both answer 19 — the missing rows are the marker-generation settings and `ratingsSource`.
  plexsim's fixtures match ZEN, which is right.
- **Anything needing a CLAIMED server**: cycle 25's scrobble-versus-timeline history pair, a
  refresh that an agent can actually change, and a MATCHED three-season show all still wait on one.
- **`/statistics/*`** is a 404 there, and `/system` and `/system/scanners/{N}` are empty on a fresh
  install.

**Cycle 36 REBUILT it** (hunt A needed a `tv.plex.agents.none` library over a hardlinked copy of
Firefly to design the agent confound out). Its machineIdentifier is now `0d2eb677…`, it holds a
third library `Threefold` (section 3, 15 with 9 leaves) beside Movies (5) and TV Shows (Firefly 6),
and **its token changed** -- read it from `/home/ben/plexscratch42/token.txt`, never from a
tokens.env written before this cycle. `rm -rf config` left two root-owned empty parent directories
behind, which are harmless.

**Its traps are plexscratch's, plus one.** An unclaimed PMS admits only loopback with a local
`Host`, so a `loopback` service in PMS's own namespace relays `0.0.0.0:32499 -> 127.0.0.1:32400`
and a `relay` publishes it; start order is load-bearing (`loopback` shares `plex`'s namespace);
section creation 400s while `/identity` still reports `startState`. **The new one: its media is the
lab's, mounted READ-ONLY**, so an item delete can never remove a file — and its show library needs
an explicit `/library/sections/2/refresh` after creation, because the first scan finds nothing.
Its subnets, container names and port are all distinct from plexscratch's, so the two never touch.
`harness/scratch42/README.md` carries the same list with the isolation proof.

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
   half-finished edits. Before choosing ports, globally list every `server.py --port` process and
   resolve its cwd/command line; Cycle 43 found Cycle 41's supposedly stopped frozen listeners only
   because startup repeated the process inventory. Kill only processes whose prior-cycle ownership
   is proved.
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
12. **Re-check the ports AFTER the hunters have stopped, not only at cleanup -- and list the
    PROCESSES, not just the ports you expect.** Cycle 28's cleanup found a hunter's server still
    up on `32589`, a port no list in this file names; `pgrep -af "server.py --port"` found it and
    a port scan never would have. A finished
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
python3 tests/test_all.py --catalog catalog.json --local http://127.0.0.1:32543 --public http://127.0.0.1:32543
python3 tests/test_mediainfo.py        # no server
python3 tests/test_decision.py         # no server
python3 tests/test_gdm.py              # no server, no network
```

| Suite | What it is |
|---|---|
| `test_navigation.py` | **The regression suite.** Every fixed rendering bug gets a check here, and it must fail against the unfixed server — prove it |
| `test_ui_surface.py` | The context menu and every server page, end to end. **It writes**; restarting the server is the undo |
| `test_all.py` | Play queue → decision → byte reads → real decode. It decodes frames rather than trusting a 200 |
| `conformance.py` | Drives plexsim with python-plexapi, as a third-party client |
| `test_mediainfo.py` | `mediainfo.py` against 46 recorded probes and a real PMS's analysis of the same files. Offline |
| `test_gdm.py` | `gdm.py` against the GDM replies of four real servers and a byte-by-byte trigger bisection, plus an end-to-end drive of the real responder over real sockets. Offline |
| `test_decision.py` | `decision.py` against `tests/fixtures/decisions/` -- 2,028 measured (profile, row) cells from kids, zen and plexscratch, each row's `Media` as the reference wrote it. Offline. The 156 cells taken before a client had chosen an audio track are counted and never fail it |

Suite conventions, each learnt the hard way:

- **Where new checks go.** Right before the cycle-18 library-DELETE block, whose comment
  begins `# Cycle 18, measured on plexscratch with a control write first (a title edit`.
  NOT before the Delete Version check -- that anchor is a thousand lines further down, past
  the row delete AND the section delete, so a block placed there reads a fixture with no
  show library. Cycles 19, 20 and 21 each appended there and each had to relocate.
- **Restart the fixture server BETWEEN runs, by waiting for the PORT to be free** — a kill plus
  an `/identity` poll can be answered by the process you just killed, and the suite then runs
  against a server it has already written to. Confirm the new pid holds the port.
- **Never run the UI suite against port 32597 (or 32599).** Both are bound by the suite's
  own checks -- cycle 29's `/web/*` check starts a server of its own on 32597 -- so a suite
  pointed there measures itself and fails that check. Cycle 59 lost a run to it; 32596 is free.
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

### Cycle 27 -- the HTTP envelope below the API, and the serialiser dimension

Two hunts and a driver round. The driver spent its half **below the API entirely**, on the HTTP
envelope every request arrives in, which twenty-six cycles had taken from Python's
`BaseHTTPRequestHandler` without ever asking what a real PMS does with a malformed one. Six
findings came out of it and every one is a refusal plexsim answered:

* the **URL** is bounded at exactly **32768** bytes and 32769 is a 400 -- the URL, not the
  request LINE, since the boundary does not move with the method. plexsim took Python's 64 KiB
  request-line limit and answered `414` past it.
* the **header caps are 1024 and 4096 bytes and 100 LINES**, all three bisected. The first two
  confirm plexsim's constants to the byte; the third **refutes** §3.3's "~41 KB total", which
  was the same cap measured with 410-byte headers. plexsim refused at a hundred -- one short,
  because Python's reader counts the blank terminator -- with a `431`.
* **the request LINE has a grammar**: two spaces, a trailing space, tab separators,
  asterisk-form and LF-only endings are each a 400 where plexsim answered 200; `HTTP/2.0` and
  `HTTP/9.9` are 200s where plexsim sent Python's 505; an HTTP/0.9 line gets a full HTTP/1.1
  response where plexsim wrote a bare body; and a lower-case method gets NO response at all.
* **an unparseable `Content-Length` is ignored and a negative one is a 500** -- plexsim called
  `int()` on the raw header, so one killed the handler thread and the other blocked on
  `rfile.read(-1)`.
* **`/photo/:/transcode` validates five more parameters**, swept over 77 requests: `minSize` and
  `upscale` are booleans, `opacity` and `quality` take any integer, `blur` 500s a negative one.

Neither reference sends `414` or `431` anywhere on the surface, which is the shape of all of
this: **Python's own refusals were leaking through as plexsim's answers.**

**The write hunt swept 45,436 (path, element, attribute) triples** across 253 paths in both
formats and three servers, reporting only where kids and zen agree and plexsim differs. Ten
landed, of which the two that matter to a client are `durationTotal`/`storageTotal` being JSON
STRINGS (a typed decoder rejects the grid row) and the bare `/system/agents` carrying a modern
agent's 45 `Language` rows -- which **overturns cycle 21's model**: the `MediaType`-in-the-bare-
form rule belongs to the LEGACY plug-in agents alone, and plexsim, which holds the modern ones,
was stripping exactly the rows the Agents page reads. The rest are order and pairing:
`?includeDetails=1` paging its 29 rows as one sequence, a `Sort` leading with its active pair, a
`Hub`'s `random` before `style`, a modern `Agent` leading `identifier, name`, the SINGLE history
read's own attribute order, and three more empty elements written paired -- one of which, the
`/media/providers` `Home` `Directory`, resolves part of the question cycle 22 left explicitly
open for `Directory`.

**Two §3.3 entries closed by revalidation, and one of them refuted.** Cycle 26's session `Role`
order ("alphabetical AFTER `filter`") does not exist: driven on a row carrying seven tag types,
every one is plainly alphabetical and no `Role` carries an `actor` attribute at all. What IS
special there is `Player` -- its strings first, alphabetically, then `local, relayed, secure,
userID` -- and its `profile`, which cycle 10 recorded as the constant `Web` from a copy only
Plex Web had ever driven and which is in fact the profile the server RESOLVED, with 23 of the 78
names reporting another profile's own `name`.

**And the artwork cascade is a DERIVATION, not a move.** The other closed entry: clearing a
season's poster on the copy stripped `parentThumb` from its episodes and picking one back
CREATED it on all three. The same rule explains two more differences -- a borrowing season's own
`thumb` and every episode's own `art` follow a show pick, where plexsim moved `grandparentArt`
and left `art` at the old stamp -- and its fourth half, "the borrower's copy goes when the
owner's does", was caught as a regression by the cycle-14 cascade check inside this cycle's own
patch. That check itself encoded two beliefs this cycle re-measured, and both were narrower than
they read; it is updated rather than deleted, with the measurement in its comment.

**And the same rule from the other side, found while checking the first:** a pick re-versions
only the artwork the row OWNS. Season 7 on the copy still carried the SHOW's `art` path after
its own poster was picked, and zen corroborates it at scale -- episode 11115 carries `art`
naming its SEASON beside a `grandparentArt` naming the show at a different stamp. plexsim
re-versioned `thumb`, `art` and `theme` onto the picking row whatever it owned, so a season that
borrowed the show's backdrop came away claiming a path only the owner serves -- cycle 16's "an
artwork path is a 200 only on the row that OWNS the image" from the inside.

**And the founding instrument, re-run after nineteen cycles, paid for itself.** The read hunt
drove the official web client through plexlab across four passes -- far wider than cycle 7's
eight navigations -- and captured 603 GET flows, 385 unique requests, 122 after dedup. It
sanity-checked the tool first (121 of 122 clean replayed at the reference itself) and then found
**three** things no hand-built probe had in nineteen cycles: `/library/all/top` refusing every
type but 1, 2 and 4, where a real PMS answers an empty 200 to any type NUMBER -- and the web
client asks for movie, show and ARTIST in ONE `all([...])` under one try/catch, so that refusal
fails the whole Dashboard Top Played panel; `/media/providers/metadata` and its `/group`, the
modern agent-provider tables the Settings Agents pane reads, never routed at all; and no person
tag carrying a `tagKey`, which is what the client builds every Cast & Crew link out of. The
same hunt also answered §7's oldest open question: **all nine `conformance.py` failures are one
cause**, the fixture's dead Real-Debrid links, and none is a plexapi limitation or a server
difference.

**The suite caught two regressions in this cycle's own patches, and that is the system working.**
The first was the artwork cascade's missing removal half (above). The second was a person tag
minted by an EDIT: the `tagKey` is minted at LOAD, and by the time the suite reaches the check an
unmatch has dropped the agent's people and an edit has added one back -- a tag through the other
door, with no key. Both were found by running the whole suite rather than each patch's own block,
which is the difference between a proof and a regression test. Three of this cycle's own checks
also had to be re-stated to survive the END of the suite (section 6's rule): by the insertion
point the show has been unmatched, the fixture movie has been merged away, and several rows carry
a `thumb` naming a ratingKey that no longer exists.

**A lesson about the instrument, again.** Two suite runs overlapped for part of this cycle -- one
finished run's process was still driving the port a new run had just bound -- and the second run
came back with 31 failures and a crash that read as a catastrophic regression. Nothing was wrong
with the tree. §10 already says never to probe the server a suite is running against; the same
sentence has to cover **never run two suites at once**, because they share the fixture, the
port and the gate servers on 32598/32599.

### Cycle 26 -- the playback surface, and the session lifecycle driven

Two hunts and a driver round, on the one large functional surface nobody had ever swept. Cycle
6 measured the decision container's refusals and the `/player/*` 400s; cycles 10, 14 and 15
measured the timeline's storage grammar. Nothing had walked the playback path as a client walks
it, and nothing had swept the `/video/:/transcode/universal/*` family or the Dashboard's
statistics endpoints as a QUERY surface.

**plexsim never ran a decision at all.** Every request without `hasMDE=1` was refused, whatever
the client's profile said. Plex Web always sends it, so nothing in twenty-five cycles of suites
ever saw this: every check was written by someone sending Plex Web's own parameters. Every
client that does not -- Roku, plex-for-kodi's `ServerDecision`, python-plexapi, the older TV
apps -- asked plexsim whether it could play an item and was told the catalog was unplayable, by
a server whose entire purpose is direct play. Measured on kids and zen across four items in
both containers, a decision with no `hasMDE` runs the client's own profile and answers one of
three shapes; the Metadata row is byte-identical between the two forms, so CLAUDE.md's "a
direct play is ONLY `mdeDecisionCode: 1000`" is the MDE form's rule and stays right.

Three more findings are the same shape: the decision's validation refused a missing or
unparseable `mediaIndex` and any `partIndex` where a real PMS accepts all of them; the
statistics endpoints answered the live per-second series relabelled for any `timespan` the
server holds no data for; and `/status/sessions` and `/statistics/{which}` were matched as
PREFIXES, so nine paths that are 404s on both references answered 200.

**The write hunt drove the session lifecycle, and found that a decision arms a SESSION.**
`self.decided` was a set of ratingKeys and three behaviours read it -- the timeline's
`Bandwidths`, the session row's `selected`/`decision`/`location` flags, and the transcode
start's gate, which was not consulting it at all. Measured with two sessions on one row: only
the one that decided is armed, the same session beating another row is not, and only its own
`stopped` beat disarms it. The arming identity is `X-Plex-Session-Identifier` then the client
identifier and **not** the decision's own `session=` parameter, which names the transcode
session -- a distinction that had to be measured rather than assumed.

**Two class-C writes that never reached a read.** A list's `/unmatch` has set `guid` to
`local://{rk}` since cycle 23 and `_collection_row` re-mints the agent guid on every read, so
the write landed in the stored dict -- which is also what the frame-count gate reads, so
everything on the notification socket said the unmatch had happened while the row said it had
not. A playlist's branch returned before the line ran at all. The reference writes more than
the guid: a collection loses `ratingCount` and its `subtype` becomes `unknown`, a playlist
loses `duration`.

**And `/status/sessions` is written by a different serialiser.** Its attributes come out
ALPHABETICALLY -- the row, its `Media` and its `Part` -- where cycle 22's measured
`_ATTR_ORDER` governs everywhere else, and in JSON four values are strings that are numbers on
the detail row while `UltraBlurColors` is a list of one. Cycle 22 swept 2,994 paths for
attribute order and this is the one it could not reach: reading it needs a live playback
session, which neither read-only reference can be given. It is the documented failure mode of
a sweep, found by sweeping somewhere the sweep could not go.

**The butler drew down two of cycle 23's last three**, on a freshly reset copy with an
uncontended socket, twice over and identical frame for frame: `CleanOldBundles` interleaves its
two messages with its own activity's phases, `RefreshLibraries` reports and closes the wrapper
BEFORE its scan, and the wrapper steps `round(100n/(n+1))` once per task. The third --
`POST /butler` running only what is DUE -- is measured (22 of 23 on a fresh reset, 12 on an
immediate second POST) and **recorded rather than emulated**, because no row of `GET /butler`
exposes a last-run or next-run time. Cycle 23's "a second POST is one `queueRegenerated` frame
and nothing else" is REFUTED 2 of 2: it is a full run of its own.

**Two suite checks were found to assert a shape no real PMS answers.** Cycle 6's decision check
asserted refusals a real PMS does not make, and cycle 9's session-row check asserted
`decision: directplay` on a session that had never made one -- which is what kept plexsim
stamping it on every session row. Both are SPLIT rather than deleted, each half saying what was
measured. A third, this cycle's own start-gate check, sent its decision and its start as two
different sessions and had to be corrected once the arming was measured.

**And a revalidation CLOSED a §3.3 entry.** The collection and playlist `/refresh` and
`/unmatch` frame sequences, carried as measured-but-unimplemented since cycle 22, reproduce
exactly: cycle 23 built them and what was missing was the `ended` frame's 100 and the guid
reaching a read.

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

### Cycle 28 -- Home's own parameters, the shelf as its own key, and the search row's identity

Two hunts and a driver round: 19 findings in 18 commits, **eight recorded beliefs overturned**,
one of them a per-VERSION split that only plexscratch could show.

**Territory A swept HOME** -- every hub path, with the parameters the official client actually
sends on each one. Its result is that the hub PARAMETERS had never been run. CLAUDE.md recorded
from cycle 15 that `contentDirectoryID` "scopes and VALIDATES `/hubs/search` alone -- ignored on
every other hub path"; measured again over nine values on both references, that is true of
`/hubs/sections/{id}` and false of four other paths. It scopes `/hubs` and all three
`/hubs/home/*` leaves and removes the `home.playlists` shelf from `/hubs`; it takes a COMMA LIST,
which is the form Plex Web's own Home saga sends, joining its array of pinned ids; and
`/hubs/continueWatching` with its `/items` leaf VALIDATES it -- one comma member must be a
positive integer, and `abc`, `0`, `-1`, `1.5` and a space are 404s. Beside it,
`excludeContinueWatching` is true for the integer 1 alone and drops BOTH resume shelves on both
paths, where plexsim compared the raw string and dropped one shelf on one path.

**A shelf is its own `key`.** At `count=40`, 29 of 32 non-empty shelves on both references answer
exactly the head of the listing their own `key` names, in that listing's order, byte for byte --
and the three that do not are the ones advertising `random="1"`, which are a random SAMPLE of the
whole key set. That single measurement settled three findings at once: plexsim's random shelves
were the deterministic sorted head, so Top Unwatched would have shown the same six titles for
ever; its Recently Watched shelf published `?sort=lastViewedAt:desc&unwatched=0&viewOffset=0` and
honoured neither clause, so a row in progress sat on the shelf and vanished when the user clicked
that shelf's own See All; and `count=`'s negative edge is `len(rows) + count` clamped at zero with
`more: false`, where `count=0` answers none with `more: true`.

**Recently Added TV is a mixed, rolled-up list.** zen's `/hubs/home/recentlyAdded?type=2` is 50
rows: 12 shows, 31 seasons and 7 episodes. Two or more recently-added siblings collapse into their
parent, recursively -- one new episode in an old season stays an episode, two become the season,
six or 161 of a show's only season still become that SEASON because one season is not two, and two
new seasons become the show -- and a parent row is dated by its NEWEST leaf, the opposite
aggregation from `/library/recentlyAdded`. plexsim filtered `type == "show"`, so a new episode or
season of an existing show was invisible on Home.

**Territory B swept SEARCH**, both endpoints, row by row -- and the driver's re-measurement of its
one reported finding turned into seven. The reported finding is the grammar: `searchTypes=` is
`/library/search`'s own filter, ignored on `/hubs/search`, with six exact case-sensitive tokens, a
comma list a union and unknown tokens dropped; and `limit=` caps the WHOLE list, not each hub,
refusing an unparseable value with a 400 and a non-positive one with a 500. Re-measuring it needed
a query that hit several kinds at once, which is how the SIX other defects in the same rows
appeared: a tag hit is per LIBRARY rather than per NAME (kids' `drama` is two `Drama` rows against
one tag id); its `count` is the size of the listing its own `key` names; a person row carries the
24-hex `tagKey` cycle 27 minted for the detail row and never put here, so a search hit's Cast &
Crew link was `/library/people/undefined`; the FLAT form drops the reason triple and the row's own
score, which lives on the `SearchResult` wrapper as a number and prints five decimals in XML; a
COLLECTION is found by `/library/search` after all; and `contentDirectoryID` scopes every row
family there, plexsim having compared the tag ENTRIES on a key they never carried, so a scoped
search answered no tag rows at all.

**The cycle's sharpest lesson is about single witnesses.** Hunt A's On Deck finding -- all three
On Deck paths answer the FULL detail row, where cycle 21 recorded the light one from fun, which
had nothing in progress -- was implemented from zen alone, with a §3.3 note saying so, because
kids' On Deck is empty on every path. The cycle-21 suite check then went red, and re-measuring it
against **plexscratch** showed why: 1.43.4 answers the same row MINUS `librarySectionKey`, `Guid`,
`Rating` and `Producer` and stamps `studio` instead. The two versions disagree, plexsim reports
1.42.2, and the writable copy -- which is a version-bearing instrument nobody had pointed at this
question -- is what turned a single-witness guess into a measured per-version split. That is the
sixth per-version shape on the surface, and §10 now carries the rule.

**Two leaves were not what plexsim thought they were**, both found by sweeping the parameters
rather than the paths: `/clients/profiles` is the server's own CLIENT PROFILE catalogue -- 78 rows,
seven at `type=42`, a 400 for every other value -- and not the hosted-client list plexsim answered
empty; and the `/collections` leaf is the `/all` listing with a default `type=18`, running the
same query and making the same three 500s, where plexsim answered 200 and `size 0` to every one
of them.

### Cycle 29 -- the bytes below the container, a row type plexsim has never held, and the artwork derivation finished

Two hunts and a driver round: **20 findings in 20 commits**, ten recorded beliefs overturned, and
for the first time in the programme **the driver's own revalidation step produced more findings
than either hunt**.

**Six of them came out of two §3.3 entries whose method was written down and never run.** Cycle 27
left "clear a show's `/art` on the copy and read its episodes back" and "unmatch a show, give it an
`art` back, and read the leaves" as open questions. Running both, and then the writes they imply,
settled the whole artwork derivation: **a row's own artwork field is its NEAREST ancestor's that
OWNS one, and `parentX`/`grandparentX` always name that specific ancestor.** Clearing a show's
`art` removes it from both seasons and every episode and picking one back puts all of them back --
so a borrowed field is derived and is RE-CREATED, which plexsim could not do. Uploading an `art` to
one season of a two-season show switches that season's episodes to it while their `grandparentArt`
goes on naming the show, which is zen's episode 11115 -- the row cycle 27 recorded and could not
produce -- made on demand. A later show pick leaves the owning season alone. Clearing a row's own
`art` drops it back to the ancestor's and makes the bare `/art` a 404. An artwork UPLOAD cascades
like a pick, which plexsim's never did. And both write paths re-version the row's OTHER field only
when the row OWNS it, where plexsim rewrote a borrowed path as the row's own -- a row claiming an
image the server has none of, which the derivation then handed to every episode under it.

One of the six was plexsim's own, and it shipped inside this cycle's second patch: `catalog.items`
is keyed by INTEGER ratingKey and every row carries its key as a STRING, so the cascade's
`items.get(row["parentRatingKey"])` was always None, a season cascade could not see its show, and
it STRIPPED `art` from every episode under it. The next check in the same dimension caught it two
patches later. That is the argument for driving one finding at a time against its own parent, and
for the §10 rule this cycle adds about proofs that cannot come from `base`.

**Two stale claims fell out of the same step.** HANDOVER 3.3 and section 8 both said the
collection's and playlist's `/refresh` and `/unmatch` FRAME sequences were measured and not
implemented. Driven again on the copy with a listener open, plexsim reproduces all four frame for
frame -- 3 frames on an agent guid and 8 on a `local://` one, 7 for a collection unmatch, 4 and 2
for a playlist -- because cycle 26 built them and nobody updated the lists.

**Territory A swept the RESPONSE BODY**, the bytes themselves, which only cycle 16 had ever looked
at: eleven findings, every one on both references. The largest is `/photo/:/transcode`'s `url=`
grammar -- the dimensions are validated first and then the url, an empty one is a 404, a server
path that is not artwork a 500, a scheme or a missing leading slash a 404, and a row that does not
OWN the field a 404 -- against which plexsim answered a GENERATED GREY TILE carrying
`max-age=259200`, so a browser kept the grey square for three days. That is CLAUDE.md's own trap
read from the other side, five doors after cycle 14 closed the first. Beside it: `/:/resources/` is
seven names and not nine (and ignores `width`/`height`); a **0/1 option's value is `+?0*[01]`** on
six options including `download=`, which plexsim never validated at all; a 416 is an error response
and not the resource with no bytes; a `/web/*` directory is an empty octet-stream 200 where plexsim
served the whole client; `X-Plex-Local-Asset` marks an item's own artwork and nothing else; and the
`Content-Disposition` escaper is "unreserved minus the dot", the same escaper CLAUDE.md already
records for `/file?url=`.

**Territory B swept EXTRAS and the `clip` row -- and corrected the brief it was given.** kids holds
one extra after all, so the territory had two witnesses instead of one. Its four findings are all
listing grammar that plexsim never ran: the section `/extras/all` leaf is a real QUERY with its own
500s (including four media keys the section grid accepts), `group=` is honoured on `/library/all`,
`sort=type` orders by the type NUMBER rather than its name, and an `include*` option takes a
leading `+`. The fifth deliverable is the one that will matter later: a **build spec for the `clip`
row**, measured over all 3,904 of zen's extras and kids' one -- attribute order with `extraType`
last, no `Guid`/`Rating`/tags/`Field`, a container with `allowSync="0"` and no librarySection
triple, `art` the parent's verbatim while `/art` is a 404, `primaryGuid` on trailer rows only, six
`extraType` values, three projections, uncapped lists, and clips in NO listing or axis anywhere.
plexsim's catalog holds no clips, so that spec is the finding and the build waits on a catalog that
does.

**And two cycles' grammars were wrong for the same reason.** Cycle 22 recorded "`+1` does not turn
an `include*` block on" and cycle 23 "a 0/1 option takes four values", both from sweeps that sent a
bare `+` -- which every real PMS decodes as a SPACE. Encoded as `%2B1` it passes both gates on both
references. Two independent findings, one instrument error, found in the same hour because the
boolean sweep and the extras sweep happened to run together. §10 now says to encode every value you
sweep with.

### Cycle 30 -- the instrument the handover kept asking for, and the response that is not a pure function of the request

Two hunts and a driver round: **20 findings in 16 commits plus 3 test commits**, eight recorded
beliefs overturned, four §3.3 entries closed, and a **seventh** failure class added to §3.1.

**The instrument, not the effort -- again, and this time the handover had written the brief.** Four
§3.3 entries across cycles 21, 25, 27 and 28 all ended with the same sentence: *a writable 1.42.2
copy would settle this.* Cycle 30 built it -- **plexscratch42** (§4.6), a fresh install of the exact
build zen runs, writable and isolated from plex.tv by construction. It closed all four entries in an
afternoon, and one of its answers **corrected cycle 28's own patch**: an On Deck row carries no
`librarySectionKey` and is the detail row minus `Field`, on a movie and an episode alike, where
cycle 28 had read the opposite off zen's single on-deck row and put the key back. Three instruments
against one. Beside it: the five media-codec edit-field names are a 400 on 1.42.2, so cycle 21's
hold-out closed by MEASUREMENT with nothing to build, while `hdr10plus` is a 200 on both versions
and became a lockable name; the write-protected settings are TWO on 1.42.2 and not the four cycle 19
measured on 1.43.4; and `PROFILE_NAMES`' 23 aliases are confirmed, all 78 names driven through a
live session with zero mismatches. Two more things it settled without a code change -- the
per-version `/filters` split is real and visible twice over, and cycle 26's session serialiser holds
field for field on 1.42.2 -- and one thing it taught about itself: its settings lists are SHORT
(151 prefs against zen's 173), because it is unclaimed and unsubscribed. **Never read a settings
count off it.** That caveat is class E sitting inside the new instrument, so it is in §3.3 and in
its own README.

**Territory B swept THE SAME REQUEST TWICE** -- the assumption every measurement this project has
ever made. Cycle 29 found by accident that a response is not a pure function of its request (the
photo transcoder's first uncached answer omits `Accept-Ranges`), so this hunt swept the dimension:
~1,600 responses across four servers, each path read three times back to back and again four minutes
later. Both findings are about STATE rather than shape. A `random="1"` shelf's sample is **stable**
-- 21 reads over seven minutes answering the identical rows, the same sample for a different client
identifier, its own stable sample per `count=` -- so cycle 28 measured the sampling correctly and
implemented it as a fresh `random.sample` per request, which re-ordered Top Unwatched on every
navigation: class C, the same complaint cycle 28 fixed from the other side. And **the connection
state of every refusal was inverted**: a 401 with no token, a 401 with a wrong one, the 403 for an
unknown single segment and a 404 for a path that does not exist all keep the socket alive and it
really is reusable, where plexsim forced `Connection: close` on every one -- one TCP teardown per
refused request on a token-gated server. The one refusal that closes is the 400 a query pair with no
`=` answers, and it is the ONLY one on the surface: seventeen other reachable 400s keep it alive on
both references, and plexsim kept this one alive too.

**That finding also corrected this file's sibling.** The 401 printed verbatim in `docs/findings.md`
since cycle 16 carries `Connection: close` -- which is plexsim's own answer, pasted in and read back
as the reference's for eleven cycles. §3.1 now has a class for it, **G**, because it is a distinct
failure mode from the other six: a sample in a document reads like a capture for ever.

**Territory A swept the NOTIFICATION CHANNEL at scale and over time, and found a self-inflicted
denial of service.** plexsim answered a client's PONG with another PING -- **903,480 pings in 35
seconds**, about 26,000 frames a second for the life of the socket. The reference's ping timer is
free-running (three pings in 35 s whether the client pongs or not); plexsim's loop waited out one
interval with a `select()` that returns for ANY frame, so a pong ended the wait and it pinged again
at once. Every compliant client pongs automatically -- every browser, every Plex app,
python-plexapi's `AlertListener` -- and twelve cycles missed it because a hand-written probe never
pongs. Beside it, **one consumer that stops reading hung every write**: writes 1-103 took a
millisecond, write 104 took 5.2 s, write 105 took 20.0 s, and a second client's unrelated title edit
timed out at 30 s while `/identity` still answered instantly. The reference took the same shape at
ten times the volume with every request between 0.019 and 0.031 s, bounding the per-socket backlog
at ~415 KB. plexsim now gives a sink a two-second budget and drops the channel: it drops where the
reference buffers, which is the half it can implement, and the check proves it -- 20.00 s before,
2.08 s after, over the same 260 writes. And a **library DELETE pushes no per-row frames** at all:
cycle 18's fourteen `state 9` frames named a different section, none of their itemIDs was a row of
the library, and a nine-row library produced the same fourteen -- a one-off cleanup of orphaned
`type: 12` rows, generalised into a rule. plexsim sent one entry per top-level row, so a zen-sized
library would have handed every client a ~1 MB frame of 3,887 deletes.

**Three smaller ones, and a nineteenth the cycle found in its own patch.** The eventsource channel
has no five-minute deadline on a real PMS; plexsim's 300-second constant is gone, and a capture
afterwards showed it still closing at **256 s for an unfound reason**, which is recorded as open
rather than claimed fixed. Its 101 upgrade carried no `Date`, the one response on the surface
without one. The frames a row's CREATION pushes are now recorded -- eight per row, `state 1` among
them -- which corrects the state alphabet and narrows cycle 19's scan rule, and is unreachable in
plexsim because its catalog is loaded at start. And the collection-search `guid` this cycle added
sat under an unconditional `return`: dead code, shipped, and caught by the cycle's own final suite
run. §10 now says what that failure looks like, along with the probe rule that cost the run -- **a
socket-reuse probe must drain each response by its `Content-Length`**, because a bare `recv()`
reports a live socket as closed the moment a body arrives in its own segment.

### Cycle 31 -- the write surface per version, the derivations held out, and the reference's own files

Two hunts and a driver round: **22 findings in 21 fix commits and 3 test commits**, six §3.3 entries
CLOSED, and two instrument steps — one of them the most consequential thing in the cycle.

**The instrument again: the reference's own FILES are readable.** Two §3.3 entries had waited on the
words *"read the profile files, if a reference is ever shell-accessible"*. kids runs the lab PMS in
Docker and `ssh ben@kids docker exec plexlab-plex …` reads the image — no PMS request, no write,
nothing that can alter the reference. `Resources/Profiles/` holds **79 entries: 78 `.xml` and one
stray `sugar.cmake`**, which is why the API answers 78 rows; a row's title is its
`<Client name="…">` attribute and not the file name (77 coincide, and
`Plex Home Theater-RaspberryPi.xml` declares `name="Raspberry PI"`); and the **23 `Player.profile`
aliases cycle 27 measured behaviourally are 22 `redirect="…"` attributes plus that one
name-versus-filename difference.** Scored against plexsim's table: all 22 shared pairs agree exactly,
nothing in the files is missing from it, nothing in it is absent from the files. A table built
entirely from black-box session sweeps reproduces the source of truth with **zero errors** — which is
a result about the METHOD, not just about the table. The `type=42` seven carry no distinguishing
attribute at all, so that membership is PMS's own list and the entry closes with a negative.

The same read makes the decision split legible. A census of all 78 files — taken **twice**, because
the first pass counted the wrong element name and produced a tidy, plausible story that did not
survive re-running — says 45 declare `<DirectPlayProfiles>`, 22 are `redirect=` stubs, 51 carry
`<CodecProfiles><Limitations>` and exactly one (`Generic`) is a bare `<Client/>`. Of the thirteen
products cycle 26 sampled, the two that direct-play are exactly the two whose resolved profile
declares a direct-play block covering mp4/mkv + h264 + aac; the seven that get `1001` declare
transcode targets and no direct-play block; `Generic` declares nothing and **`Plexamp`'s only block is
a `MusicProfile`**, so neither can touch a video item. Firefox and Edge are the open half — they
declare video targets that ought to cover the item and still answer "neither".

**And the database settles what the API could only imply.** Copied read-only out of both writable
copies and out of the lab: **a generated title is stored as the EMPTY STRING.** Every season and
episode of a fresh 1.42.2 install stores `title = ''` and `title_sort = ''`, and kids' agent-titled
season 7 stores `title = ''` with `title_sort = 'Season 1'`. So the displayed `Season N` is derived
from the index in *both* cases and what an agent supplies is the **sort** title — which is the whole
mechanism behind cycle 18's `titleSort=` split, and it exposed a real defect: plexsim treated a stored
sort title equal to the generated title as no sort title at all, so `titleSort=Season 1` answered
nothing where kids answers the row, `titleSort=` wrongly kept it, and the ordering keys ignored it.
One clause, three symptoms, invisible from the API alone.

**Territory A held the WRITE surface out between the versions, and the negative result is the
headline.** About 1,500 requests, the identical sequence on both copies and on plexsim: the 78
edit-field names with their locks, 66 of them with values, 95 write-grammar cases, 24 numeric columns
on the section path, the settings writers, the list writers, the action leaves, the six state writes,
the upload ladder and 99 paths under a bare OPTIONS all came back **identical on both versions**. What
differs is the **verb table**, and plexsim carried 1.43.4's:

- **`PATCH` is not a method 1.42.2 recognises** — a 500 on all 20 paths swept and three body
  dispositions, the same 109-byte body `FROB` and `BREW` get on both versions. The decisive case is
  `/identity`, the one path that answers every other verb: six verbs are 200 there on 1.42.2 and
  PATCH alone is a 500. Four recorded rules (cycle 15's routing, cycle 18's 405 under `/system`,
  cycle 22's four 404s, cycle 28's collection 404s) were the other version's.
- **All five `DELETE /library/metadata/{id}/{poster,thumb,art,banner,theme}` leaves are 404s on
  1.42.2**, where 1.43.4 answers an empty 200 and clears the field. The controls make it the VERB:
  the same paths take `PUT ?url=<candidate>` with a 200 there, `DELETE` itself works (a playlist
  delete is a 204), and a real PMS routes before it authenticates. plexsim had implemented three of
  the five, so it answered neither version consistently.
- **1.42.2 accepts two writes 1.43.4 refuses**: `smart=1` over a comma-joined KEY LIST creates a real
  smart playlist (`leafCount 3`, `icon playlist://image.smart`), and `PUT /playlists/{smart}/items`
  takes an ITEM uri and **replaces the stored query with it** — `content` moving from
  `library://x/directory/…%2F1%2C2` to `library://x/item/…%2F4`, which is also how the middle segment
  is chosen: `item` for a single key, `directory` otherwise.
- **`PUT /library/collections/{id}/prefs` really writes** on both versions. Cycle 15 measured the GET,
  recorded its 404 and stopped, so plexsim advertised `collectionMode` and `collectionSort` in the
  collection's `includePreferences` block and 404'd the save behind them.

Three smaller ones from the same sweep. **`absoluteIndex.value=` stores nothing** on either path or
version (twelve measurements across four row types) where cycle 20 recorded it as "read on both", so
an edited row carried an attribute no real PMS puts there and `sort=absoluteIndex` ordered on it.
**`lastRatedAt.value=` on the section path is a 500** on both, the ninth member of that set, where
plexsim answered its unknown-field 400. And **`hdr10plus` stores neither a value nor a lock** — cycle
21 recorded exactly that, cycle 30 confirmed the 200 on the new copy and then put the name in
`LOCK_ONLY_FIELDS`, which records a lock: class C inside the cycle that re-measured it, because the
status was checked and the row was never read back.

**An empty artwork value is the clear route 1.42.2 has.** Measured on both copies, identically: an
EMPTY `thumb.value=` or `art.value=` is a 200 that REMOVES the column and makes the bare path a 404 —
the same end state 1.43.4's DELETE reaches. That matters beyond the grammar: with the DELETE gone on
1.42.2 it is the only way a client can clear artwork, so every cycle-29 derivation rule keeps a route
to the cleared state it is about. A NON-empty value is an artwork COLUMN write, the url discarded and
the column becoming the row's own versioned path, exactly as `theme` and `banner` already were — the
column has to be empty for that to be visible, which is why cycle 20 read both as discarded.

**Territory B scored plexsim's own DERIVATIONS against zen's 14,433 rows, and the biggest result is
that they are right.** The artwork derivation — two cycles old and fitted on a two-season copy — has
**zero misses** over 507 shows, 741 seasons and 9,287 episodes: `season.parentThumb == show.thumb`
741/741, `grandparentThumb`/`grandparentArt` 9,287/9,287, `episode.art` equal to the season's resolved
art 9,287/9,287, `parentThumb` present exactly when the season owns its poster 9,287/9,287, and the
stamp equal to `updatedAt` on every owned path with `addedAt` the fallback on the 268 rows that carry
no `updatedAt` — which is `_stamp_artwork`'s own expression. The carried fields are 100% over the same
rows, and the light projection differs from the real light row in nothing but one row's tag casing.
Four §3.3 entries close on that population.

What it found wrong is the **matchers**, and every one of them is a query a user types:

- **A comma in `title=` is an ALTERNATION and a dot is LITERAL.** `title=Steel,,` and `title=Love,`
  answer the whole library (an empty alternative matches every row), `title=Tears, of` answers the one
  row, `title=Steel.` answers none. plexsim stripped both characters out of the query, so a typed
  comma found one row where a real PMS answers 3,887 and a trailing period found one where a real PMS
  answers none.
- **The hub matcher takes a multi-word query WORD BY WORD, in order** — `Tears Steel` finds "Tears of
  Steel" at 0.73055 and reversed it falls to the 0.33 fuzzy band — where plexsim required the joined
  phrase and therefore answered **no hubs at all**: 230 of zen's rows scoring 0.5 or better across 119
  of 1,237 generated queries. That is Plex Web's global search box.
- **It tokenises the TITLE on WHITESPACE**, punctuation inside the word: `don't` finds "Don't Move",
  `Handmaid` finds "The Handmaid's Tale", and `Delete` does not find 'Control-Alt-Delete'. plexsim
  split on `[^0-9a-z]+`, so a query with an apostrophe could never match while `Air` found "L'air
  Perdu".
- **A ONE-character query matches a title equal to it** (`X` finds the movie 'X'), where plexsim had a
  two-character floor.
- **`title=` reads the ORIGINAL title as a consecutive run of whole words, unfolded**, where plexsim
  ran the title's own two-pass folded matcher over it. And **the in-library search box does not read
  `originalTitle` at all** — 102 of 1,244 queries disagreed, every one a plexsim-only hit.

**Two findings this cycle were NOT implemented, and both for stated reasons.** The `title=` query fold
is a per-character TABLE rather than NFD (kids folds `é ï ē ī ì` and does not fold `ị ĩ ı ş ę ȉ`), and
sweeping U+0080..U+024F for three letters gives a real but partial table — 11 characters folding to
`i`, four to `s`, two to `l`. A partial table would break the accented rows that work today, so the
sweep script and its output are recorded and the entry stays open with its method proven. And hunt B's
episode-still finding did not reproduce: over **1,500 live zen episodes, 0 carry a thumb equal to
their `parentThumb`** (1,372 own one, 128 have none), against the 21 live witnesses the hunt counted,
so it is recorded rather than built. `/livetv` is the third: both copies answer **403 to every verb**
today where cycle 18 recorded a three-row container from the same copy, so plexsim is left alone until
that contradiction is settled.

### Cycle 32 -- concurrency, the fold table in full, and the decision model from the profile files

Two hunts and a driver round: **9 findings in 9 fix commits and 1 test commit**, four §3.3 entries
CLOSED, nine recorded beliefs overturned.

**It ran across two sessions, and that is worth recording in its own right.** Session `eba8c320-…`
merged cycle 31, launched both cycle-32 hunts at 08:26, re-measured their findings and committed three
fixes by 09:02, then went idle. Session `61d69236-…` was started with the instruction to read this file
and continue, found the live `cycle32` branch, its worktree and its hunters' two servers still up, and
**asked the user before touching any of it** -- two sessions on one cycle would have written to the same
two copies and the same branch. On the answer "take it over", it read both hunt reports out of the
first session's `subagents/*.jsonl` and the driver's own progress out of its transcript, removed the
clean worktree and re-added it under its own scratchpad, rebased the three commits on `main` (which had
gained a docs commit meanwhile) and pushed the branch. §10 has the procedure.

**Hunt A swept CONCURRENCY**, which the brief had named for two cycles. Every pair came from two
threads released by one barrier and was repeated tens of times, with the row read back afterwards, on
both copies and plexsim. The negative result covers two writers on a row, reads during writes (6,870
validated reads on plexsim, no torn body in either format), two sessions on one item, the 28-pair play-queue mutation
matrix, duplicate creates, frames under overlapping writes and pipelining. The positive result is one
defect in two places -- **a write that builds its answer after releasing the lock it wrote under**: a
play-queue CREATE answered 404 for the queue it had just made (9 of 60 pairs; the references 0 of 110),
and two concurrent ADDS answered the same container (11 of 67; the references 0 of 110). Plus a
deterministic one: **a write's response ignored paging and `excludeElements=`**, because both
post-processors were gated on the method.

The first session fixed the create and wrote that the fix covered the adds too. **It did not**: the
second session re-ran the add race on that tree and got 9 collisions in 180 pairs. Every mutation of an
existing queue -- add, clear, item delete, move -- had the same shape and now builds its answer inside
the lock.

**Hunt B closed the fold-table and decision-model entries.** The `title=` query fold is 102 characters,
identical on both versions, and the folded pass reads the stored SORT title. **The driver finished the
fold on the write side**: 2,076 codepoints written into titles on both copies say the sort title folds
exactly 100 characters -- the query table without the two case folds `ſ` and `K` -- and a locked
accented sort title showed the stored side is only case-folded. NFD, which plexsim used for all three,
over-folds `ę ą ş ć` and the capital macrons. 825 probes on kids put the hub matcher on the same table,
with a case handling split across two passes that is recorded rather than guessed (§3.3).

**The decision model is built from the reference's own files.** Hunt B scored it over 78 profiles × 24
rows; the driver re-parsed the 78 XML files independently (identical), then fitted the ORDER of the
checks to the reference's refusal TEXTS by scoring every permutation: exact outcome on 1,871 of 1,872
cells and exact text on 1,864. The eight misses are one row, zen 19813, whose audio tracks carry no
language -- recorded, not fitted. The model replaces a two-name list: 44 profiles direct play a real row,
and the two plexsim listed were being told to direct play the fixture's own webm and eac3 files, which
neither profile names. Two cycle-26 suite checks had encoded that list and were refitted to a
measured-model pair.

**The revalidation step found the ninth.** Settling `/livetv` -- both claimed references answer the
container, so cycle 31's 403 was the unclaimed copies' (class E) -- meant reading bodies side by side,
and plexsim's were 11% longer: **a real PMS writes JSON compact, in raw UTF-8, with upper-case `\u00XX`
escapes**, and plexsim wrote Python's defaults on all three. Measured on zen, kids and both copies,
through every C0 control, and on the notification frames.

**Two of the cycle's checks failed inside the full suite after proving on a fresh fixture** -- one
assumed a movie still had one Media after the suite's merge check, the other a third movie the suite has
deleted -- and were refitted and re-proved by hand, the exclude check also against a server with the
second version merged in. The final run is 1526/1526 with the baseline ten skips.

### Cycle 36 -- the refusal surface, the grammar of an id, and a search score that was never one number

Three territories, and the driver's was a dimension no cycle had swept: **what a real PMS answers
when it refuses**. Thirty-five cycles had measured what the surface says when it works, and the
statuses and header blocks of its refusals; nobody had read the BYTES of a refusal's body, and
nobody had asked how a path SEGMENT is parsed before the row behind it is looked up.

**The driver's territory produced eight findings in an afternoon**, all of them uniform rules
measured over both references. A refusal's BODY is one fixed HTML document per status, built from
HTTP's own reason phrase and ignoring `Accept` -- plexsim had three of the eight, each built by
the cycle that happened to measure that one refusal, and answered `text/plain` with a sentence of
its own everywhere else. That sentence is cycle 1's "deliberate deviation", carried in §8 for
thirty-five cycles and re-justified three times without anyone reading what the reference sends;
it is now in the LOG, where it was always the useful half. The same document reaches the ENVELOPE's
own refusals, which came out of `BaseHTTPRequestHandler.send_error` as Python's 460-byte DOCTYPE
page. A path-segment id is parsed as a NUMBER (`01`, `001`, `+1` and `+01` name the same row, byte
for byte, on six path families), four paths bound it to a SIGNED 32-BIT integer, and the request
TARGET must be printable ASCII with a `%` that begins an escape the parser can read. Two leaves
that never looked their id up were found on the way: `/hubs/sections/{id}` built a full Recommended
page for any id at all, and `/hubs/metadata/{id}/related` 404'd where a real PMS answers the empty
hub -- and it turns out that leaf never resolves the row, where its `/library/metadata/` spelling
does.

**The search hunt found that the SCORE was never one number.** Cycle 35 had just corrected both
scorers to the matcher's tier rule and printed 0.93/0.73/0.33; every one of those is short of what
a real PMS sends. The five decimals are a TIER plus the row's own `audienceRating`/10000 -- held
out over **11,076 scored (query, row) pairs across 619 queries**, 11,075 of which decompose exactly
that way -- there is a **fourth tier at 0.53** for a query that is a token-wise prefix of the title,
the BASE is the row TYPE's (an episode 0.02 lower, a tag 0.92/0.52/0.32 with no anchored step), and
the anchored tier is ONE token comparison, which **refutes cycle 35's own `word_tier`** on four
cells it was never scored against. Its other half took the decision corpus into Dolby Vision: a
PROFILE 5 source is refused by the server itself with a code and a sentence the corpus had never
seen, sparing only the one client profile whose transcode target names the source's own codec; and
the §8 dual-layer disagreement is RESOLVED, with `DOVIBLPresent` as the discriminator and plexsim
already right.

**What was left.** The fuzzy band is now understood and still not built, which is a better place
than the cycle started: the reference's own binary names `spellfix_metadata_titles` and three
siblings as `CREATE VIRTUAL TABLE … USING spellfix1`, the candidate rule is a phonetic-hash prefix
(897 of 898 reference band rows predicted by a transcription of `phoneticHash()`), and what cannot
be read from outside is the top-N prune over a GLOBAL token frequency -- which is why `Tears zzz`
answers the row on kids' five titles and `Riders zzz` answers nothing on zen's 3,887. Emulating the
candidate half alone would answer most of a catalog to a nonsense query. What WAS built from it is
the half that is certain: a query whose words are all in the title but out of order comes back in
the 0.33 band, where plexsim answered no hubs at all.

### Cycle 35 -- the encoded body, the Range grammar, and two scores that were never the matcher's

The hunts ran again after two cycles without them, and the shape of the cycle was three territories
that each produced two: **six findings in six fix commits**, five negative results, four measurements
recorded rather than built, and one §3.3 half closed.

**The driver took the ENCODED body, an axis no cycle had looked at.** Cycle 27 established that a
real PMS gzips every compressible body a client offers to take compressed; nobody had read the bytes.
**It zeroes the gzip header's MTIME**, and with that the whole member is byte-identical to
`gzip.compress(payload, 6, mtime=0)` on both versions and every body measured -- so a compressed
response is a pure function of its body, where plexsim's answered the same request with different
bytes every second. Cycle 30's same-request-twice sweep could not see it, because it compared the
DECODED bodies; that is the lesson, and it is in §10. The same territory swept **the `Range`
grammar** -- 43 header values against both references over a container and a `/web` asset, 172
cells, the references and the two body kinds identical in every one -- and found plexsim wrong on
twelve of the 43, two of them provokable by a real client: a reversed range answered a 206 with an
EMPTY body where a real PMS answers 416, and an unparseable one answered a 200 with no
`Content-Range` where a real PMS answers the whole body as a 206.

**The write-frames hunt finally got its turn.** Cycle 33 reached one sequence and cycle 34 had no
hunts at all; the list survived both. It came back with **a library CREATE's frames**: a create
SCANS, and the scan's eleven-frame skeleton survives a location holding no files -- which is why
cycle 15, which only ever created libraries over real files, concluded an empty library had nothing
to push and left plexsim's create sending one queue frame. The client's own Add Library dialog draws
its spinner and its completion toast off those frames. And **`ButlerTaskGenerateAdMarkers` ships
disabled**, alone among the three marker tasks and the one disagreement in the whole 23-row enabled
table, which `POST /butler` turns into a task a fresh real server never runs (class D).

**The search hunt found that the SCORE had never been the matcher's.** Cycle 31 built the word-by-
word, skip-tolerant rule into `Catalog.search`'s `hit()` and cited `Tears Steel` → 0.73055 in its own
comment; both scorers, a few hundred lines away, kept comparing a title PREFIX. Measured over sixteen
(query, row) cells on kids and zen: an exact title is 0.93 -- not the 0.95 plexsim invented -- a
query whose terms match the title's words in order **starting at the title's first word** is 0.73,
and everything else is the 0.33 fuzzy band. So a skipped middle word, the commonest thing a person
leaves out when typing, scored below the threshold the client promotes a hub on, and a single word
from the middle of a title scored above it. Class C in its canonical shape. Its other finding put
zen's row 3831 into the decision corpus -- a 4.6 GB `high 10` mp4 -- which shows that a **`list=`
limitation appends ` [list]`** to its refusal, is the corpus's first `has64bitOffsets` true (the
reference's 400 there matching the model exactly, closing that half of §3.3), and takes the corpus to
1,950 cells.

**What was left.** The hub matcher's fuzzy BAND is now measured -- by word position and word length,
over 16 zen titles -- and still not built: emulating it means emulating a continuous similarity
score, and plexsim answers prefixes. `decision.py` reads `video.separateFields` as a constant 0 where
every neighbouring value comes off the stream, and zen's one interlaced Stream sits on a stale Media
that answers "File is missing" to all 78 profiles, so no check can be proven fail-then-pass; recorded
with the citation rather than fixed blind. And zen says a dual-layer Dolby Vision file is ONE video
stream where the existing note, measured on fun, says two -- one witness each side.

### Cycle 34 -- a fresh client capture, and the decision's audio streams made rather than found

No hunts: the weekly rate limit that crippled cycle 33 killed both inside a minute, on every model
tried. So this is the driver's own two territories, and both were on cycle 33's list. **Five
findings in four fix commits and two test commits**, one §3.3 entry CLOSED, one recorded belief
overturned, one sidecar fixture found to be a projection rather than a capture, and one of the
cycle's own earlier commits caught starving a check three thousand lines below it.

**A fresh CLIENT-SESSION capture** through plexlab -- the founding instrument, last fully recorded
in cycle 27. `session-web.py` drove the official client through the walk again and `replay.py`
compared 26 unique requests against plexsim with the reference re-fetched live at the same moment:
every element and attribute difference was one already recorded as a version or a library
difference, which is the negative result. What the capture carried that no probe had sent was the
client's own ENVELOPE, and three findings came out of it. **A response with an EMPTY body orders
`Content-Length` before `Content-Type`**, the way an error does, where a container body is
type-first -- read off the client's `PUT /updater/check` and then re-measured on both copies over an
edit, a `/refresh` and a 204 playlist delete, on both versions. So cycle 33's response families do
not split on the STATUS: they split on whether the body carries a container. **The preflight sends
`Access-Control-Allow-Headers` only when the request carried `Access-Control-Request-Headers`**, and
echoes exactly that; Plex Web's own preflight asks for none and the reference answers without the
field, where plexsim substituted a fallback list cycle 16 had invented beside the echo it measured.
And **the websocket handshake's header ORDER**, which cycle 33 recorded as unmeasurable because a
socket may never be opened on kids or zen: the client opens one itself, so the capture holds both
sides. The CORS pair LEADS, ahead of the handshake headers.

**zen 19813's shape, made rather than found.** §3.3's oldest open entry was the one measured row
whose audio tracks carry no language and the eight cells cycle 32's model could not fit; its own
instruction was to make one. `ffmpeg` muxed an mp3 2.0 and an ac3 5.1, both `default` and neither
tagged with a language, over Sintel's h264 video into an mp4 with `+faststart`, so that
`optimizedForStreaming=1` matches 19813 and no container limitation can fire first. plexscratch's
row 60 then reads exactly 19813's shape, and all 78 profiles answer **68 of 78 identically to zen's
row** -- the ten that differ being the ones zen's selected sidecar subtitle refuses earlier, each
exactly where the model puts that check.

With the shape reproduced the rule was measurable, and it is two rules. **A decision reads EVERY
audio stream, not only the selected one**: Roku plays the selected mp3 and still refuses the row,
naming the **ac3** it does not list, while Android names the **mp3** -- the first in INDEX order it
cannot play. **Unless the profile declares `StreamUnselectedIncompatibleAudioStreams`**, where only
the selected stream counts; that is the whole difference between Windows Phone, which direct plays
the row, and Universal TV, which refuses it. And that setting was one of **twenty of the reference's
thirty `<Setting>` rows that `client_profiles.json` had dropped**: cycle 31 read the profile files
into it and kept the one the model of the day happened to read. A sidecar fixture read out of the
image has to be the whole thing.

**The eight cells were never about the row.** The same bytes answer 41 of the 78 profiles "Selected
audio stream is not the first audio stream" although `selected` names the FIRST audio track and no
profile capability explains the split -- and one write, `PUT /library/parts/{id}?audioStreamID=<the
track already selected>`, makes all 41 answer from the visible row instead; `audioStreamID=-1` puts
every one of the 78 back, byte for byte. The item's `Media`, `Part` and `Stream` rows are identical
throughout. So cycle 32's "the server re-picks the widest default track when the selected one has no
language" was per-user state the row does not carry, fitted to one witness read in that state
(§3.2). plexsim holds a chosen track, so it models the after and records the before.

`tests/test_decision.py` is what keeps the model honest from now on: `tests/fixtures/decisions/`
holds each measured row's `Media` as the reference wrote it and one line per (profile, row) cell --
78 profiles against 24 rows on kids and zen plus 78 against the made row in both of its states.
**1,872 of 1,872 cells exact**, outcome and refusal text; the 156 pre-selection cells are counted and
never fail it. The same corpus scores 1,826 against the model as it stood, which is the fail-then-
pass proof this cycle's one model commit rests on.

**And the cycle caught one of its own.** The empty-body check writes `summary.value=c34` to the
fixture's first movie and left it there, which starved cycle 20's "only titleSort folds case" check
three thousand lines below: it counts the rows whose summary sorts under `b` against those under
`B`, and its whole population is this fixture's three movies. The commit was proven with the driver,
which runs one block; the full suite is what caught it, for the fifth cycle running.

### Cycle 33 -- the response header block, and a version claim that did not survive its control

Two hunts and a driver round: **six findings in seven fix commits and one test commit** -- one fix
repairing a regression this cycle's own first patch caused, and one correcting its own family table. The cycle's shape was set by something outside it:
both hunts died inside a minute on an account-level weekly rate limit for the two models this
programme has always used, and were relaunched on the model that was available. One came back with
"no findings" and a large negative result; the other came back with a single finding whose framing
did not survive re-measurement. **The driver's own territory produced five of the six.**

**The response HEADER BLOCK had never been compared.** Cycle 27 measured header values, cycle 32
found the JSON bytes unmeasured, and the block itself was still untouched. Read off a raw socket --
`urllib` normalises exactly what is being measured -- for twenty-six response classes on kids and
zen, keep-alive and closing, the two references identical in every case: `X-Plex-Protocol` is the
FIRST header and `Date` the LAST (plexsim had both ends the wrong way round, because Python writes
`Date` first), `Keep-Alive` always follows `Connection`, and the rest is per response FAMILY -- a 200
container is type-then-length where an error is length-then-type, a gzipped body moves `Connection`
ahead of the encoding pair, artwork leads with its type and `X-Plex-Local-Asset`, the transcoder and
a static asset send the same five names in two different orders, a part read leads `ETag,
Last-Modified`, and the preflight wraps its CORS headers around everything else. Two response classes
turned out to be missing headers entirely: **the plug-in framework's `/system` paths carry a session
cookie** (a constant, on every status, and only there -- `/system/agents`, `/system/scanners/{N}` and
`/system/proxy` send none), and **the preflight and the `/web` 302 never named their connection
state**. Three more measurements came out of the same sweep: a **416 names no connection state at all**, a
**206 of a CONTAINER answers `Content-Length` twice** -- the whole body's and the slice's, with
`Content-Range` between them, where a ranged artwork or asset answers the slice's alone -- and a
**part read carries no `Cache-Control`** -- the exception to cycle 16's "no-cache on EVERY
response", where the rule was wrong and plexsim was already right.

**The first header patch broke every malformed-request-line refusal.** A request whose LINE is
malformed is refused before `parse_request` sets `self.path`, so a family lookup that reads the
attribute raises inside `end_headers` and answers nothing at all: cycle 27's six malformed lines and
the 70 KB URL went from a 400 to NO-RESPONSE. The patch had passed its own fail-then-pass proof; the
full suite caught it, for the fourth cycle running.

**The hunt's one finding was reported as a version difference and is not one.** It measured a refresh
of an unmatched row under an online agent stopping after three frames on 1.42.2 where 1.43.4 runs the
whole cascade. Two controls dissolve that: the 1.43.4 copy shows **the same three-frame opening** on
its own never-matched row, and the 1.42.2 copy runs a full cascade for a row in a
`tv.plex.agents.none` library. The hunt had compared a `local://` row on one server with
`tv.plex.agents.none://` rows on the other -- two different states, not two versions -- and whether
the cascade runs at all is the copies' own: one carries local metadata bundles, the other is a fresh
isolated install with none (class E). What the measurement does establish, on both versions, is that
**a never-matched row opens its refresh with `state 2 matching` and a bare `state 5`** before the
queued frame, where an explicitly unmatched row opens with the queued frame alone -- which is the
chase cycle 32 deferred, now with the guid identified as the discriminator.

**The negative results are the other half.** `translations.json` was scored against the reference
image's own gettext catalogs -- 13,494 triples, zero mismatches, plus an independent 400-entry sample
by the driver -- so a table built over five cycles from black-box API diffs reproduces the compiled-in
source exactly, as cycle 31's profile table did. The same survey established that `languages.json`,
`server_prefs.json`, `system_agents.json` and `mediainfo`'s codec tables have no source file in the
image at all. 534 compatibility characters (Greek Extended, Roman numerals, circled and mathematical
letters, ligatures, superscripts, Armenian, Cyrillic) were substituted into a `title=` query and none
folds. Every status line and every header value matches except the recorded `text/plain` deviation.
And cycle 32's decision model was held out on four zen rows it had never seen: **312 of 312 cells
exact**, outcome and refusal text.

### Cycle 37 -- the raw target, the catalog shapes that never existed, and the real spelling index

Cycle 37 took every implementable entry in cycle 36's plan. It produced **seven fix commits**, each
with a failing parent and a passing fixed tree, and closed three long-running §3.3 entries by making
their rows instead of reasoning about unreachable code.

**The driver swept the raw request target and found four uniform rules.** A literal semicolon and
hash are PATH/QUERY data to PMS; Python's URL parser treated them as params/fragment syntax and
silently routed a different request. A non-terminal path segment made only of two or more dots is a
400 after exactly one `%2e` decode -- validation, not path normalisation; terminal `..` remains a
404 and a double escape is not decoded twice. GET and HEAD answer immediately without consuming a
declared body, with or without `Expect: 100-continue`, where plexsim blocked every read-only route
waiting for bytes a client had no reason to send. Finally the literal-query rule had to reach the
response-wide helpers as well as the router: language, paging, `excludeElements` and notification
filters each reparsed `self.path` through a fragment-aware helper and could contradict the endpoint
that had just built the response. Raw-socket regressions retain the discriminators.

**The catalog hunt made multipart media and clips real.** `build_catalog.py` now groups only the
measured Plex stack spelling, `Title - cdN.ext`, into one Media with ordered Parts and summed
duration; each Part carries its own private debrid link, with the old row-level shape retained as a
compatibility fallback. A decision's `partIndex=0/1` evaluates and projects that Part alone,
out-of-range is 400, and `-1` returns the measured 3000/2000/4005 all-parts conversion refusal with
no row. The focused fixture also carries a profile-5 Dolby Vision row and proves the already-built
2003 refusal is reachable end to end; there was no missing DoVi code path.

The same fixture supplies the clip schema cycle 29 had measured over 3,905 reference rows. A parent
advertises its lowest-index trailer in `primaryExtraKey`; inline `Extras` is uncapped and ordered;
item and section `/extras/all` return the measured full/light projections; a clip publishes no
invented librarySection triple; XML leaves `extraType` last; and artwork ownership is split exactly
as measured -- the row borrows its parent's `art` attribute, but its own `/art` leaf is a 404 while
`thumb` and `poster` serve. Frozen cycle-36 code answered all three extras projections empty, crashed
on the faithful section-less clip detail, and served the borrowed art as if the clip owned it.

**The search hunt read the reference's own database and binaries and recovered the whole fallback.**
There are two live spellfix tables, not four: metadata titles and tags. On 1.42.2 the metadata rank
SQL tokenises DISTINCT `title || ' ' || title_sort` strings with ICU; 1.43.4 uses `concat_ws`.
Repeated words inside a string count, duplicate complete strings do not, original title enters FTS
but not the vocabulary, and no library/type/deleted/media predicate exists. The zen reconstruction
is exact on **11,755 metadata tokens and 71,124 tag tokens**, ranks included.

The query is literal: `word MATCH <raw token> AND top=10 AND score<=200`, default scope 3 and langid
0. Its exact cost is `editdist1(transliterated_query,k1) + 32 - bit_length(rank)`; the older note was
off by one. Normal movie/show/list/tag hits suppress fallback globally, while an episode-only hit
does not. Fallback discards query tokens shorter than three UTF-8 bytes, ORs each retained token's
spellfix candidates with its original prefix, then ANDs those groups in the ICU primary/shifted FTS
table. It searches movie/show titles, sort titles and original titles, not episode titles; the
measured fuzzy tag path covers people, not genres. `top=10` and the inclusive cost boundary each
have live discriminators (`kickd`, `defeted`/`Defeted`). The fresh held-out result is **93/93
queries, 822/822 row cells**, after the two initial misses forced the implementation to use the
table's real ICU collation rather than ordinary string equality.

**The negative results and limits at the end of cycle 37 mattered.** The decision model already
carried profile-5 Dolby Vision correctly once a row existed. The spellfix vocabulary was known not
to update synchronously, its rebuild callers were not yet recovered, technical tag ranks were not
yet reconstructible, and final equal-score API ordering was unsolved. Cycles 38 and 39 subsequently
closed and built all four questions; the only retained limit is deliberately reproducing plexsim's
own stale-until-optimization state rather than another server's historical snapshot.

The final cycle tree passes `test_catalog_features` 17/17, `test_ui_surface` 1560/1560,
`test_navigation` 28/28, `test_mediainfo` 31/31 and `test_decision` 2106/2106. The 29/38
python-plexapi and 0/11 byte-read results are their recorded dead-link baselines. The reference
snapshot before and after both hunts is byte-identical.

### Cycle 38 -- slow consumers, failed storage, and the last two spelling-index questions

Cycle 38 completed all four entries in cycle 37's plan. It produced **four fix commits**, each
proved against its immediate parent, and two broad negative matrices. The agents used independent
artifacts and frozen servers; every reference write was confined to plexscratch, every temporary
list and item value was undone, both disposable copies were restored, and the kids/zen snapshots
before and after are byte-identical.

**The concurrency hunt found one route-level request-envelope rule.** Both PMS 1.42.2 and 1.43.4
answer and commit a query-only item metadata `PUT` and playlist-create `POST` immediately even when
the request declares one million body bytes, sends none and keeps the socket open. Twenty concurrent
trials per route and version all completed in 0.012--0.194 seconds. Frozen plexsim timed out after
20 seconds, emitted no response and committed none. A subtitle upload remained blocked everywhere,
so the implementation bypasses body reading only for those two measured method/path pairs. The
rest of the matrix matched: a stalled multi-megabyte response, a dead websocket reader, 1,200 writes
and 2,402 live frames did not block ordinary clients, while three-client item/rating/progress/list
races, request abort, response abort and EOF exposed no torn or lost completed write.

**The search follow-up recovered the rebuild and both final comparators statically.** The spelling
rebuild has no item trigger. It belongs to two old search migrations (and one new 1.43.4 migration)
and database optimization. The optimizer is reached by explicit library optimize, Butler
OptimizeDatabase and section-deletion completion. Automatic Butler eligibility is a roughly
five-minute transition into the configured maintenance window, enabled preference and
`floor(time/86400) % 7 == 0`; the rebuild itself requires the ICU FTS table, transactionally
rebuilds FTS and both spellfix tables, then optimization runs `ANALYZE` and `VACUUM`. plexsim now
rebuilds at all three optimization paths it owns and keeps ordinary edits stale.

For hub ordering, exact-title candidates precede unique FTS candidates sorted by raw title bytes
and signed id descending. The final sort compares only the unrounded double score through the old
libc++ unstable comparison sort, with no secondary key, and serializes five decimals afterward.
The source-level reconstruction matches 331 eligible reference hubs, 2,306 rows and 1,578 adjacent
equal-wire-score pairs with zero misses. For flat `/library/search`, PMS accidentally reads an empty
generic `score` attribute in its final comparator; the no-op retains its movie/show/episode bucket
assembly and permits four observed global score inversions. plexsim now does the same explicitly.

**The storage axis was fully exercised and has no plexsim analogue.** Isolated reference-copy
faults covered SQLite, preferences, metadata bundles, transcode/cache targets, recovery and
atomicity. The answers vary by backing subsystem: database ENOSPC can be a 500 or a silent 200 whose
write never lands, preferences can change in memory then revert, and bundle leaves fail separately.
plexsim has no SQLite database, `Preferences.xml`, bundle tree, cache or encoder. Introducing those
faults into its in-memory catalog would invent a storage backend rather than emulate PMS, so the
measurements are retained and no response path was changed.

The final cycle tree passes `test_catalog_features` 20/20, `test_ui_surface` 1561/1561,
`test_navigation` 28/28, `test_mediainfo` 31/31 and `test_decision` 2106/2106. The 29/38
python-plexapi and 0/11 byte-read results remain the recorded dead-link baselines.

### Cycle 39 -- a clean client replay and the last two search residuals

Cycle 39 completed all three entries in cycle 38's plan. It produced **two fix commits**, each
proved against its immediate parent. The official-client axis returned exactly `no findings`; the
search residual axis found two client-visible differences, so the cycle is not the stopping cycle.
Both hunters used frozen servers and private artifacts, reference access stayed GET/HEAD or
read-only static copies, and the guarded kids snapshots before and after are byte-identical.

**The current official client introduced no new navigation request.** Plex Web 4.160.0 completed
the eight-step home → sections → grids → movie → show → season → episode walk with 21 safe GETs and
20 unique exact envelopes. All statuses and formats agreed, common attribute/child order had zero
mismatches, and every remaining structural/value difference was catalog state, a measured
1.43.4-versus-1.42.2 descriptor, or a deliberately unadvertised file/encoder capability. The 20
signatures are a strict subset of cycle 27's 95. The first attempted capture was discarded -- not
counted -- because its gate admitted two notification websocket upgrades and a theme read; the
replacement blocked sockets and media before navigation. A diagnostic also printed the sourced lab
token to the private tool transcript, but no tracked or derived artifact contains it; rotate that
token after the programme finishes.

**Hub score-tier normalization is not membership normalization.** Over the complete 2,076-codepoint
domain, 659 character/target cells yielded exactly the 97 letter entries of `_TITLE_FOLD`; the
333-cell fit and 326-cell holdout had zero misses. The partial-title score pass applies that table
to the raw query before ASCII-only case folding. Lowercasing first wrongly promoted eleven capitals
and lost `Ð`. A second whole-title pass is broader: capital macrons and long-s can be exact there,
while a two-letter macron-`o` word remains anchored. The regression set preserves the exact
`.33055`/`.73055`/`.93055` witnesses rather than replacing the two passes with Unicode normalization.

**The hidden tag-rank problem collapsed to generic row extraction plus 28 static strings.** The
reference's 94,343 distinct inputs rebuild exactly to 71,124 tokens. Current row fields reconstruct
71,109 when Chapter/Review/Marker/CommonSenseMedia/Network tags, Guid ids, Rating images, studio and
Thumb/Art/Theme/ClearLogo presence are included. The full server-static remainder is three optimize
labels and 25 distinct show-ordering labels. A 697,334-query bounded comparison produced 526,777
candidate-vector and 187,616 post-gate people-membership differences with the old extractor;
`emtz`, `arcewell`, `tft` and `flr` are retained wire-level witnesses. plexsim now uses the generic
extractor and exact 28-string sidecar. It does not carry zen's library-specific token table.

The final cycle tree passes `test_catalog_features` 25/25, `test_ui_surface` 1561/1561,
`test_navigation` 28/28, `test_mediainfo` 31/31 and `test_decision` 2106/2106. The 29/38
python-plexapi and 0/11 byte-read results remain the recorded dead-link baselines.

### Cycle 40 -- complete boundary censuses found two reachable failures

Cycle 40 completed both axes in cycle 39's plan and produced **two fix commits**. The inward census
mechanically enumerated 867 source branches, 308 normalized route/shared-postprocessor families,
526 family/verb cells and a 3,080-cell protocol-axis grid (2,986 applicable). Evidence exists for
all 308 families; branch-entry tracing plus semantic assertions reaches 304, and the only two
intentionally unopened families are image bodies and notification channels, both already measured
and regression-covered. Ninety two-version residual cells found one mismatch: the bare
`GET|HEAD /system/services/url/lookup` does not return an empty negotiated container. Both PMS
versions return a 500 legacy plug-in XML `Response`; plexsim now emits the exact 464-byte 1.42.2
body, ignores Accept, gzips GET when offered, preserves the uncompressed length on HEAD and closes
the connection.

The outward census exhaustively classified the locally served Plex Web 4.147.1 tree: 207 chunks
plus its OAuth helper, 3,029 module occurrences, 670 offset-verified candidates and 204 normalized
path families. The hunt also recovered 57 version-marked 4.160.0 JavaScript bodies across two
cache builds. That recovered subset exposed eight provider-settings writes. Successful provider
table reads make **Add Agent** visible without another gate, but frozen plexsim returned 400 to the
group create. Both references accept create and rename with a full JSON group, refuse the offered
incompatible Personal Media membership with 400, return 404 for absent membership deletion, and
delete the group with an empty 200. The full create/read/rename/refusal/delete/postcondition
lifecycle is now in memory and regression-proved.

The 4.160.0 cache is not a complete client artifact: the gw manifest names 205 JavaScript assets
and only 29 survive, leaving 176 mapped chunks absent; the app build has no retained manifest
denominator. All surviving call sites are classified, and the 21 exact runtime requests from cycle
39 remain a strict subset of the old corpus, but the missing chunks forbid a complete 4.160.0
static-universe claim. Both disposable references were restored byte-for-byte and the protected
reference snapshots match. The final tree passes catalog 25/25, UI 1563/1563, navigation 28/28,
mediainfo 31/31 and decision 2106/2106; conformance 29/38 and byte-read 0/11 remain the recorded
dead-link baselines.

### Cycle 41 -- the finite historical backlog found 24 real mismatches

Cycle 41 completed both finite axes from Cycle 40's plan. Territory A mechanically extracted 513
raw historical notes, retained 496 claim sources, normalized 287 parents and split compound rows
into **332 atomic behaviors**. Its final current-tree dispositions are 180 built, 42 refuted or
superseded, 36 deliberate capability absences, 23 client-invisible fidelity choices, 40
external/instrument bounds and 11 safely unreachable state models. The last 51 are individually
named and bounded; actionable findings and unbounded unresolved cells are both **zero**.

Frozen `2c06bab` nevertheless differed on **24** of those atoms. All 24 are implemented in 24 fix
commits and proved fail-parent/pass-fixed: child/tag order, empty collection leaf views, ASCII
nonletter buckets, mixed folder row kinds, legacy search limits, untyped scoped conjunctions,
multi-Media decisions, Media-duration sort, anamorphic and 480 boundaries, show merge, `checkFiles`
Part state, plus download names, playlist identity, queue re-anchoring, global session reaping,
search-hub promotion, two playlist edge grammars, exact title-mark folding, four localization source
pairs, the 50-row Recent TV cap and visually-impaired subtitles. The exact claim-to-commit table is
in the private `A/REPORT.md` and the public measurement prose is in `docs/findings.md`.

The title-mark close is the cycle's largest finite matrix: 7,531 assigned scalars at the front and
811 between ASCII sentinels on each writable version. Both remove exactly U+0300..U+036F. The old
combining-class predicate removed 699 additional Mn/Mc characters and missed U+034F. The session
close similarly separates two facts: 180 seconds is the cutoff, and a global 30-second process tick
applies it, so rows live 180-210 seconds.

Territory B independently joined **49 source families / 1,221 normalized advertisements / 47 exact
consumer predicates / 339 controls / 45 actions / 45 readbacks** for the complete locally served
Plex Web 4.147.1 bundle. Thirty-nine actions have exact route plus assertion witnesses; the other
six are explicitly bounded media, encoder/sync or auth/external cells. It found **no client-visible
mismatch and no unresolved cell**. The 176 mapped 4.160.0 chunks missing from the retained cache
remain an external limit and are not presented as a complete-current-client static census.

Safety remained intact: the protected reference snapshot is byte-identical, both writable copies
were restored and released, and the final exact five-secret scan found zero occurrences. Retained
coverage is 487 kids, 488 zen, 447 frozen and 8,674 cells on each writable copy. The final tree
passes catalog features 35/35, mediainfo 34/34, decisions 2106/2106, sessions, navigation 28/28 and
UI 1582/1582 with 14 explicit fixture skips. Conformance 29/38 and byte reads 0/11 remain solely the
known dead account-scoped link baseline.

### Cycle 42 -- the clean replay reports no findings

Cycle 42 froze merged Cycle-41 head `f8cd6c2` and rebuilt both finite universes from current source,
current tests and fresh live evidence. It is the first cycle eligible for the stopping rule after
Cycle 41's positives, and both independent reports end exactly **`no findings`**.

Territory A rebuilt **332/332** historical atoms from the same 513 raw / 496 claim-source entries,
preserving 645 source references even where Cycle 41's closing documentation moved or rewrote the
sentence. It rejoined every atom to current code and regression evidence, then audited all 24
Cycle-41 fixes across their adjacent dependencies. The result is **24/24 pass, pending 0**. Current
activity included 24 safe target GET cells, a dedicated `#`-bucket discriminator, and the complete
suite. Final dispositions are unchanged; actionable and unbounded unresolved remain zero.

Territory B recollected every one of the 49 source families on both references and frozen plexsim,
rescanned all 207 local 4.147.1 JavaScript files and rebuilt the graph at exactly **1,221
advertisements / 47 predicates / 339 controls / 45 actions / 45 readbacks**. Stable ids are unique,
all foreign keys resolve, 39 chains have current exact route plus postcondition witnesses, and the
six media/capability/auth/external bounds are explicit. Unresolved is zero. Its disposable replay
passed 1582/1582 and traced 7,090 requests across 1,363 distinct method/path pairs.

Root independently repeated catalog features 35/35, mediainfo 34/34, decisions 2106/2106,
sessions, navigation 28/28 and UI 1582/1582. The 29/38 conformance and 0/11 byte-read results remain
the named dead-link baseline, with all queues and decisions passing. Both audit snapshots are
byte-identical to their befores; exact-value secret scans have zero hits; the scratch copies were
not needed; all disposable and frozen listeners were stopped; repository and frozen export stayed
clean. The protocol-conformance loop therefore stops at Cycle 42. The missing 176 mapped 4.160.0
chunks remain the same explicit external coverage limit, not an unreported finding.

### Cycle 43 -- the complete current-client universe also reports no findings

Cycle 43 reopened frozen Cycle-42 head `5874d01` because the stopping claim still carried one
named external boundary: 176 webpack chunks mapped by the retained Plex Web 4.160.0 gateway
runtime but absent from that historical cache. Three independent territories covered that boundary,
the historical universe and the advertised-control universe. Every report ends exactly
**`no findings`**.

Territory A obtained the complete current public Plex Web **4.160.0** app build `75ddd7b` from its
anonymous manifest-rooted static graph: **205/205 JavaScript assets, 16/16 CSS assets, 30
same-origin static dependencies, zero missing assets and 207/207 verified SRI values**. Cycle 40's
28 recovered app-build JavaScript bodies match the current public graph byte-for-byte; the other
177 JavaScript assets contribute **zero direct PMS/player path call sites, zero PMS request-builder
imports, zero XHR/AJAX/Backbone write sites and zero new controls or actions**. All 281 direct path
call sites remain in the same 28 files Cycle 40 recovered. The three current-only PMS families are
the already implemented metadata-provider id, group-id and membership surface. The reconciled
current ledger is 207 PMS families; adding its four predicates, eight controls and ten
action/readback cells to the complete 4.147.1 graph gives 51 predicates, 347 controls and 55
action/readback cells. The old gateway variant remains an accurately bounded historical artifact at
29/205 JavaScript bodies, but it is no longer a limit on the complete current official app build.

Territory B independently rebuilt **332/332** historical atoms from 513 raw records and 496
claim-source records, preserving 645 source references. All **24/24** Cycle-41 fixes pass their
cross-fix/adjacent-dependency audits; actionable and unbounded unresolved are both zero. The final
dispositions remain 180 built, 42 refuted/superseded, 36 deliberate capability absences, 23
client-invisible fidelity choices, 40 external/instrument bounds and 11 safely unreachable state
models. Its attempted full UI replay was stopped when the concurrent-suite rule fired; it claims
only fresh targeted/offline evidence and hash-verified retained UI evidence, while root and
Territory C supply the independent complete current-head UI runs.

Territory C recollected all **49** advertisement-source families on kids, zen and current plexsim,
rescanned all 207 local 4.147.1 JavaScript files and rebuilt the graph at exactly **1,221
advertisements / 47 predicates / 339 controls / 45 actions / 45 readbacks**. All stable ids are
unique and every foreign key resolves. Thirty-nine reachable chains have fresh exact route,
assertion and postcondition witnesses; the two forbidden media reads, hidden Optimize/Sync actions
and three external/auth actions remain the same six terminal bounds; unresolved is zero. Its
instrumented disposable replay passed **1582/1582** with 14 explicit fixture skips and recorded
7,149 requests over 1,365 distinct method/path pairs.

Root independently repeated catalog features 35/35, mediainfo 34/34, decisions 2106/2106,
sessions, navigation 28/28 and UI 1582/1582. Python-plexapi remains 29/38 and byte reads 0/11 only
because the fixture carries dead account-scoped Real-Debrid links; every queue and decision passes.
All three protected-reference before/after pairs are byte-identical at
`64579ece7b9f77a2914bc07f5f3aef36f71bf2ddf0c57aec68b428819386e68a`; all exact-value secret scans
are clean; neither writable PMS copy was needed. At cycle start, root found the supposedly stopped
Cycle-41 frozen listeners still on 32587/32588 (pids 24850/24852) and killed only those verified
scratch-tree processes before allocating Cycle-43 ports. Every Cycle-43 disposable/frozen listener
was then stopped and rechecked. With the current client, historical behavior and advertised-control
graph all finitely enumerated and clean, the protocol-conformance loop stops at Cycle 43.

#### Post-cycle live-media refresh

At the user's request, the dead private catalog was replaced with freshly sourced account-owned RD
links. Archive-backed candidates were rejected by the existing probe rather than admitted as video.
The final gitignored catalog contains five probed video parts in seven metadata rows, including a TMDB-identified Mentalist
show/season hierarchy. Blender's openly distributed Big Buck Bunny MP4 was added to the RD account
as the browser-native MP4/H.264/AAC control. The refreshed catalog passes navigation 24/24,
python-plexapi **38/38**, and the full queue/decision/prefix/magic/seek/decode matrix **5/5**.

That MP4 found one test-only defect: `tests/conformance.py` chose an H.264 item but asserted
Matroska magic unconditionally. The old check failed 37/38 on the measured MP4 bytes; the
container-aware check passes 38/38 and now deliberately prefers the Plex Web direct-play shape.
No emulated server behavior changed. Live links remain only in `catalog.json`; neither links nor
credentials enter the repository.

### Cycle 64 — the catalogue as an instrument

**C** reported exactly zero findings on axes 1, 2, 3, 4, 7 and 10. **B** swept 115 platform-like
and 40 product strings -- every one the 1.42.2 binary and the 78 profile files carry -- over 40
read paths on kids, zen, plexscratch42 and plexsim (3,793 + 1,336 stable cells agree), plus
profile resolution (345 cells) and timelines per platform (115); one product-keyed rule was
missing, `Plex Home Theater`'s five-component version, now built. **A** repeated the rendered walk
and 18 localised captures (identical to cycle 63) and added the reference's own gettext
catalogues: 180 paths x 8 languages searched for strings plexsim leaves English and the
catalogue translates, which found a collection's Advanced settings -- a dialog no capture walked.
Built from a live capture of plexscratch42.

**Process.** Background waiters died silently in this session (a watchdog and a hunter's own
monitor), which left hunt A idle for two hours after 13:02 while it reported "waiting". Check a
hunter's newest output file, not its last message, and nudge it with a narrowed brief.

**The axis ledger after cycle 64.** Axes 1, 2, 3, 4, 5, 6, 7 and 10 clean; 8 and 9 each found one.

### Cycle 63 — nine axes clean

**A** walked the rendered client on the Windows browser (until it went offline mid-run, then kids'
9223) and localisation in nine languages: 18 captures identical to cycle 62's, 14,790 settings
cells, 2,682 stream-name cells, 224 hub-template cells and a new 74,849-cell whole-stream-title
check (58 mismatches, all the recorded empty-stored-title streams) -- no client-visible findings,
one malformed-only (music/photo sort titles). **C** reported exactly zero findings of either kind on
axes 1, 2, 3, 4, 7 and 10. **B** repeated every earlier regression clean, then swept the product
strings the 1.42.2 binary carries across the two product-keyed rules plexsim has, and found both
tables sampled short: the codec suffix (21 products, not 13) and the `Bandwidths` suppressor
(`Plex for Windows Phone` too). Both built.

**The axis ledger after cycle 63.** Every axis but 8 is clean in cycle 63; axis 8's two tables are
now swept whole from the binary's own product strings.

| # | Axis | Status |
|---|---|---|
| 1, 2, 4, 7, 10 | request inventory, python-plexapi reads, owner's screens, vocabulary, discovery | **clean four times** (60-63) |
| 3 | Client write flows | **clean** (63) |
| 5 | The rendered client | **clean four times** |
| 6 | python-plexapi's writes | **clean twice** (62, 63) |
| 8 | Playback per client identity | found 2 (product tables); repeat |
| 9 | Localisation | **clean twice** (62, 63) |

### Cycle 62 — eight axes clean

The same three territories on all ten axes. **A** reported exactly `no findings`: the rendered
client in English (every cycle-59..61 fix holds rendered) and localisation in nine languages, with
zen read WHOLE -- 14,790 settings cells, 2,682 stream-name cells, 224 hub-template cells, 0
mismatches. **B**: every earlier fix held, 0 plexsim 500s over the 291-step matrix, and one
finding on axis 8 -- `Bandwidths` withheld from `Plex for Windows` -- from a 48-cell platform x
product grid no cycle had driven. **C**: axes 1, 2, 4, 7 and 10 byte-identical to cycle 61;
one axis-3 finding, a renamed collection's stale light rows, which had sat undispositioned in
cycle 60's diff and which cycle 61's 409 had hidden. Two fixes, both built.

**The axis ledger after cycle 62.**

| # | Axis | Status |
|---|---|---|
| 1 | Plex Web's request inventory | **clean three times** (60, 61, 62) |
| 2 | python-plexapi's read surface | **clean three times** |
| 3 | Client write flows, redraw reads, frames | found 1; repeat |
| 4 | The owner's screens | **clean three times** (one unmeasurable bound) |
| 5 | The rendered client | **clean three times** (60 in English, 61, 62) |
| 6 | python-plexapi's write and playback surface | **clean** (62) |
| 7 | Server vocabulary under client tags | **clean three times** |
| 8 | Playback per client profile and identity | found 1; repeat |
| 9 | Localisation, per version | **clean** (62) |
| 10 | Discovery, claim and connection | **clean three times** |

### Cycle 61 — the stopping repeat that was not

All ten axes, with cycle 60's three territories and tooling. Seven clean; three not:

* **C** (axes 1, 2, 3, 4, 7, 10): every axis byte-identical to cycle 60 but axis 3, where a new diff
  line -- a rename that became a 409 on plexscratch42 because cycle 60 left `C60C Coll4` in its tag
  table -- led to two rules: the TAG TABLE blocks a rename, and a NEW tag is capitalised.
* **B** (axes 6, 8): cycle 59's fixes held (34/34); four findings, two of them from a two-item
  queue-advance flow no earlier cycle drove -- the session listing and `Session` block -- and two
  artwork follow-ups of cycle 59's own upload work. Re-measuring corrected one: a decided session
  keeps `Session` when it moves to another row.
* **A** (axes 5, 9): the rendered client clean in English and eight other languages but for the
  server settings: `/:/prefs` pages, so cycle 60 had re-captured 20 of zen's 173 settings. Taken
  whole, 3,045 of 3,045 compared cells match zen.

**The axis ledger after cycle 61.**

| # | Axis | Last taken | Status |
|---|---|---|---|
| 1 | Plex Web's request inventory, JSON and XML | 61 C | **clean twice** (60, 61) |
| 2 | python-plexapi's read surface | 61 C | **clean twice** |
| 3 | Client write flows, the redraw reads, the frames | 61 C | found 2; repeat |
| 4 | The owner's screens | 61 C | **clean twice** (one unmeasurable bound, §3.3) |
| 5 | The RENDERED client | 61 A | **clean twice** |
| 6 | python-plexapi's write and playback surface | 61 B | found 4; repeat |
| 7 | Server vocabulary under client tags | 61 C | **clean twice** |
| 8 | Playback per client profile and identity | 61 B | **clean twice** |
| 9 | Localisation, per version | 61 A | found 1 (the settings); repeat |
| 10 | Discovery, claim and connection | 61 C | **clean twice** |

### Cycle 60 — localisation as an axis, and the first three-hunter repeat

Laid out on the axis ledger: A (opus) the rendered client repeated plus localisation (axes 5, 9),
B (fable) python-plexapi's writes repeated plus playback per client identity (axes 6, 8), C (opus)
axes 1, 2, 3, 4, 7 and 10 repeated with the tooling that last took each. The copies were shared by
writing only to disjoint content (A movies 1-5 and Firefly, B section 3, C the probe movies).

* **A** walked 34 captures a run in en, de, da, ja and pt on plexsim and plexscratch42 and found
  five localisation findings, all built -- by capture, never by hand: stream language names by
  TAG from zen's axes and show heads (83 tags), the title words (`stream_words.json`), hub title
  templates (`title_templates.json`), and a re-captured 1.42.2 table (`capture_i18n.py` with
  `I18N_KEEP_SAME=1`, zen every path, plexscratch42 the settings documents). Held out on zen the
  stream pass is exact on 23,820 of 23,832 cells. Its English walk was clean.
* **B**: every cycle-59 fix held; fifteen client identities agree on Player blocks, timelines, queues,
  stream selection and the profile model (15/15); one client-visible regression of cycle 59's own.
  Its section-3 scan re-homed the symlinked `Twofold` into Threefold -- rebuilt from re-muxed files
  (show 391, episodes 393-395), and Threefold gained its own Season 1 (386-388, 396).
* **C**: axes 1, 2, 7 and 10 clean; axis 3 one client-visible (the collection lock, which cycles
  57 and 58 had both seen in their diffs and taken for undo residue); axis 4 one bound.

**The axis ledger after cycle 60.**

| # | Axis | Last taken | Status |
|---|---|---|---|
| 1 | Plex Web's request inventory, JSON and XML | 60 C | **clean** |
| 2 | python-plexapi's read surface | 60 C | **clean** |
| 3 | Client write flows, the redraw reads, the frames | 60 C | found 1; repeat |
| 4 | The owner's screens | 60 C | clean but for one unmeasurable bound (§3.3) |
| 5 | The RENDERED client | 60 A | **clean** in English |
| 6 | python-plexapi's write and playback surface | 60 B | found 1 (a regression); repeat |
| 7 | Server vocabulary under client tags | 60 C | **clean** |
| 8 | Playback per client profile and identity | 60 B | **clean** on the client side |
| 9 | Localisation of everything the client draws, per version | 60 A | found 5; repeat |
| 10 | Discovery, claim and connection | 60 C | **clean** |

### Cycle 59 — the rendered client, and python-plexapi's writes

Cycles 56-58 compared the RESPONSES to the requests a client builds. Cycle 59 took two axes none
of them had.

* **Territory A (opus) drove the real Plex Web app in a browser.** The Windows CDP machine was
  unreachable, so it used kids' 9223 Chromium through a raw-CDP driver (Playwright's
  `connect_over_cdp` hangs there), loaded the bundle plexsim serves from a `localhost` origin (a
  real PMS echoes CORS only to `*.plex.tv`, `localhost` and `127.0.0.1`), and answered plex.tv
  with `page.route` so the app saw exactly one owned server. About 60 pages and dialogs a server
  on plexsim and plexscratch42, 45 on kids behind a GET-only gate (595 photo transcodes and 13
  other reads refused, zero writes reached kids). Five client-visible, all built.
* **Territory B (fable) drove python-plexapi's WRITE and PLAYBACK surface**: 182 methods over 62
  classes, 291 steps on plexscratch42 and on plexsim with the websocket and eventsource frames and
  a reload of everything each call should reach. Nine client-visible, all built; its Optimize
  cell stays the recorded bound. Every cycle-58 fix held (`regress58b.py` 22/22 once a play
  exists; cycle 58's flow scripts clean).
* **The driver's re-measurement found three more.** An item's own language enum is built at read
  time and was never localised (captured off zen per language into `item_language_names.json`);
  `translations.json` is 1.43.4's and zen words 405 cells differently (re-captured with
  `capture_i18n.py`, now version-aware, and overlaid -- never replaced, since zen's content-only
  strings must not enter the table); and cycle 58's Continue Watching gate is the LIBRARY's play
  history, measured by adding a second show (`Twofold`) to plexscratch42's Threefold library.
  Hunt B's playlist-timeline report ("nothing stored") was corrected the same way: with a session
  identifier the beat runs the item machine.

**Built** (14 fix commits): the `en` enum injection and the item language enum; the proxied
`/player` 404; a smart collection's `collectionFilterBasedOnUser`; `DELETE
/library/sections/all/refresh`; a playlist's timeline; uploads per row, the agent candidate kept,
theme and logo uploads stored and served; a match's rename cascade; the pinned list on Home's
Recently Added (1.42.2); the Collections tab; the Playlists source (and its Roku projection) and
`home.playlists` on `contentDirectoryID=playlists`; a library's Recent Playlists shelf with empty
lists in every library; the zen translations; the library-wide gate.

**Writes left on the copies.** plexscratch42: the `Twofold (2019)` show (three relative symlinks
under `/home/ben/plexscratch42/media/shows`, section 3 -- keep it, it is the two-shows-in-one-
library instrument), a `clearLogo` upload on movie 4 (1.42.2 has no artwork DELETE and
`clearLogo.value=` does not clear it), `upload://` candidates on movies 3, 4 and 5, and hunt B's
298->353 split key. plexscratch: hunt B's rows 69-72 from a library-wide scan of the cycle-54
probe files, and 72->76. `scratch.sh reset` removes plexscratch's.

**The axis ledger for the stopping rule.** The loop stops when a cycle reports no client-visible
findings on EVERY axis below. A new axis that finds something has to be repeated clean before the
loop can stop.

| # | Axis | Taken in | Status after cycle 59 |
|---|---|---|---|
| 1 | Plex Web's request inventory, JSON and XML | 56 A, 57 A, 58 A | found things in 58; repeat |
| 2 | python-plexapi's read surface | 57 A, 58 A | clean in 58's repeat |
| 3 | Client write flows, the redraw reads, the frames | 57 B, 58 A | found things in 58; repeat |
| 4 | The owner's screens (settings, library management, Dashboard) | 58 B | found things; repeat |
| 5 | The RENDERED client | 59 A | found 5; repeat |
| 6 | python-plexapi's write and playback surface | 59 B | found 9; repeat |
| 7 | Server vocabulary (binary params, SQL) under client tags | 56 B | clean on the client side; repeat |
| 8 | Playback per client PROFILE and identity (decision, queue, timeline, bytes for non-Web clients) | not under the client rule | untaken |
| 9 | Localisation of everything the client draws, per version | 59 (partial) | untaken as an axis |
| 10 | Discovery, claim and connection (GDM, `/identity`, resources) | 49, 51 | clean before the rule; repeat |

### Cycle 58 — the flows repeated, and the owner's screens

Territory A re-ran cycles 56-57's client flows against the new code, and every earlier fix held.
It then ran sequences no cycle had tried: a real play followed by later Mark Played, a new
playback session that stops under a minute, a beat at exactly the stored position, and
re-reading Continue Watching at the same URL. Four findings were built:

* **The library's Continue Watching gate is the show's play history.** Once any episode has
  been completed in a playback session, the shelf lists the On Deck episode however it became
  next. This holds after the played episode is unscrobbled, and after Mark Played on a later
  episode or on the season. Cycle 57's never-played control still holds, and now runs at the
  head of the UI suite, before the suite plays that show.
* **A new session's sub-minute beats keep the resume point.** The deletion happens only inside a
  session that has already reported more than a minute.
* **A beat at exactly the stored position writes nothing.** `lastViewedAt` does not move, and a
  row the user removed from Continue Watching stays removed.
* **A section-path collection rename onto a collection tag already in use is a 409.** The match
  ignores case. The driver measured one more cell on plexscratch42: renaming a collection to its
  own current title in any case is a 200, so the rule compares against the collection's current
  TITLE.

Territory B drove the owner-facing screens: Settings, a library's Add/Edit/Manage dialogs,
Manage Recommendations, the Dashboard, and the activity and console panels. It read on zen and
kids and wrote on the copies, and four findings were built:

* the legacy agents' `config/{mediaType}` chain, `contributors`, and the order save with its
  framework refusals;
* `/library/all/top?type=2|3` rolling an episode play up to its show and season, and a
  follow-up that finds a flattened episode's owning season;
* statistics `at` buckets aligned to local midnight, local Monday, the 1st and 1 January;
* `hidden=1`/`2` on a library: kept off Home and `/library/recentlyAdded`, and at 2 also out of
  both searches, with the Home shelf kept and emptied.

**Process.** The session that ran the hunts ended before the implementation pass. Hunt A's report
was on disk; hunt B's findings were rebuilt from its regression script and verbatim reads. Hunt B
was built by a forked agent in a second worktree and cherry-picked onto the cycle branch. Two UI
checks written earlier had to be re-based on the new rules, as test changes rather than
behaviour changes: the restore beat sent exactly the stored offset, and cycle 57's no-play control
ran after the suite had played the show. Run the UI suite with `RD_TOKEN=dummy`. With a real key,
Real-Debrid answers the fixture's placeholder link with `unavailable_file`, which plexsim turns
into a 404, so the two part-path arity checks fail.

### Cycle 57 — lists, and what every screen shows after a user acts

Neither kids nor zen holds a playlist or a collection, so every list rule this project had came
from the copies, and no cycle had compared a list through a client object. Territory A (opus)
did, through python-plexapi's whole read surface -- 108 classes, 646 members, 1,018 probes a
server -- and found four list findings on the copies, which agree across both versions.
Territory B (fable) drove every client write on plexscratch42 and plexsim and read the 41 things
the client redraws after each: five findings, one of them (a pristine show's OnDeck) refuting
cycle 41 on kids and zen as well. Territory B's findings were built by an implementation agent
under sdrive; it added a `tagId` filter the collection shelf's own key needs and a flattened-show
follow-up, and the driver re-based one navigation check. **Nine client-visible, all built;
eight recorded.**

### Cycle 56 — the first cycle under the client-impact stopping rule

After cycle 55 the user set the stopping rule: stop when a cycle finds nothing a real client
would notice (§1). Every hunt finding now carries a tag -- CLIENT-VISIBLE with its call site, or
MALFORMED-ONLY -- and only the first kind is built.

* **Territory A (opus) started from the clients** rather than from the server's vocabulary: 3,885
  recorded Plex Web exchanges reduced to 80 shapes, the reference walk replayed, python-plexapi's
  templates and 1,455 probes through it, the local bundle's 160 path families and every hub key
  the servers publish, and the client's writes (Mark Played, rate, Edit, a timeline, a
  collection tag) driven on plexscratch42 and plexsim with the reads after them diffed. Three
  client-visible (provider storage on a show library, the playlist Meta, the legacy agents'
  languages) and five malformed-only.
* **Territory B (fable) repeated cycles 54 and 55** -- every cycle-55 fix held -- and widened them
  to a show library at every type, the hub pages and the per-user columns after writes. Three
  client-visible (`decade`, the counters, `includeGuids` on a section's hubs) and thirteen
  malformed-only.

**Six built, eighteen recorded.** Four recorded beliefs were refined (§3.2), three of them on
the malformed-only side.

### Cycle 55 — cycle 54's instruments at full resolution

Cycle 54 sampled both of its new instruments: four values per parameter name, and two findings
out of 707 SQL statements with no verdict for the rest. Cycle 55 asked whether either axis was
finished. **Neither was: 37 findings in 37 fix commits**, each proved against its own parent.

* **Territory B (fable) gave every client-reachable SQL read a verdict** -- 366 of 438 re-extracted
  reads, each identical, a finding or a named bound -- and found eight, making state on
  plexscratch42 where no read-only reference held it: Related is a server-side tag score (59 of
  59 zen rows exact), On Deck's next-up anchors on the most recent view, `recentlyViewed` and a
  section's `/onDeck` order by the view-state write time, `popular` counts history rows, a
  playlist sums whole seconds, the genre shelf is its key's head, and a season's generated title
  follows its index.
* **Territory A (opus) repeated cycle 54's sweep and widened it** to eighteen values per option
  and eight operators per filter field. Every cycle-54 fix held. Twenty-six rules came out --
  structural parameters parsed wherever they are read, the `[+-]?digits` integer, the
  `+?0*[01]` option at every reader, include blocks on every hub's full rows, the filter value
  grammar, and a dozen endpoint rules.
* **The implementation of Territory A was delegated** to an opus agent working in the cycle
  worktree under `sdrive.py`, one commit per finding, with the hunter's `regress55.py` (123
  checks) as its acceptance test. The driver re-measured a cross-section of fourteen cells before
  delegating and re-ran every suite afterwards. It found one more rule on its own
  (`/hubs/promoted` refusing the hub keys when a show library is in scope), which the driver
  measured and built.
* **The driver** found, revalidating cycle 54's §3.3 entry, that a library's TV Recently Added
  shelf IS the section's roll-up -- cycle 50's "top-level shows" was kids' one show.

**One reference crash.** The implementation agent's probe `/library/search?query=the&limit=
4294967296` took kids' PMS down; it restarted in about five seconds, claimed, with its state
snapshot byte-identical. §9 records it. **Five beliefs overturned**, two §3.3 entries closed.

### Cycle 54 — the binary's parameter names, its SQL, and containers made one at a time

Cycle 53 closed by reading what ships beside the binary. Cycle 54 went back into the binary
for the two vocabularies neither earlier cycle had used -- **the query-parameter names the
server reads** and **the SQL it runs** -- and the driver added an instrument of its own:
**one small file per container**, made on kids' host with ffmpeg and scanned into
plexscratch42, because no library the project reads holds a `.ts`, `.m4v`, `.wmv`, `.mpg`,
`.flv`, `.ogv`, `.3gp` or `.divx` part.

**22 findings in 22 commits, every one proved fail-then-pass against its own parent.**

* **Territory A (opus), the binary's parameter vocabulary** -- nine findings. 1,018 names
  were derived mechanically from both string tables (531 literal `name=` tokens, 1,824
  camelCase identifiers adjacent to a route template) and 903 of them crossed with 92 safe
  families on zen in 25-name groups (332,304 cells), each moved group bisected to its name.
  The one gap behind most of what it found: a real PMS reads these names in its SHARED query
  engine, and plexsim either treated the name as unknown -- which answers every row -- or
  never ran the engine on that family. Six media columns and eighteen more tag-table fields;
  the listing query on a show's children and grandchildren, `/unwatched`,
  `/recentlyViewedShows`, the media axes, `/autocomplete` and `/common`; the TV Recently
  Added shelves filtering their episodes before the roll-up; `unwatched=1` as a flag with
  two grammars; the Dashboard's view-time keys; thirteen parameter refusals; `sectionID` as
  a number. Its tagging-join family is the one bound: every kids movie carries hidden
  `tagType=318` taggings plexsim does not hold.
* **Territory B (fable), the binary's SQL** -- two findings from 707 statements naming a PMS
  table. `viewUpdatedAt` is in the projection the server reads view state with, and Plex
  Web's own hub query sorts on it, which plexsim answered with a 500; and an EPISODE
  listing's leaf-count filters read the SHOW, fitted 16 of 16 on zen's 9,287 episodes.
* **The driver** -- the part `Content-Type` table, `.mov` as `mov` (the extension decides
  inside the QuickTime family, brand and extension crossed both ways) and `packetLength:
  188` on every MPEG-TS part. Re-measuring the hunts found five more: `viewUpdatedAt`'s
  stamping rules, the three per-user dates reading an unset value as 0, three columns cycle
  53 typed as integers that are dates, a negative rating STAMPING `lastRatedAt`, and a hub
  PAGE dropping an empty-valued pair before it filters.

**Four recorded beliefs overturned**, each in §3.2: cycle 9's negative rating, cycle 14's
`viewedAt>=abc` (the cycle-22 encoding trap again: a raw `>=` IS the `viewedAt>` key),
cycle 20's null date, cycle 46's season list. **Two §3.3 entries CLOSED**: the legacy
agents' load order is the filesystem's, and `/system/library/sections`' key ignores `Host`.
**The cycle's own suite caught two of its own mistakes**: the media axes lost their
`type=` blindness when the query reached them (caught at proof time and fixed in the same
commit), and the `viewUpdatedAt` cascade check read the season off an episode the suite had
flattened by then (repaired to read it from the show's children) -- and that repair then exposed a real
gap: a flattened show's episode stores its season only privately, so a scrobble stamped the
show and not the season. The fix reads the private key; plexscratch42's Firefly was itself
flattened when the stamping was measured (its episodes carried `skipParent`) and its season
7 WAS stamped, so the rule was already measured on the flattened case.

**How to read the curve.** Cycle 54 is the third cycle in a row reopened by an instrument
nobody had used, and the largest since cycle 45. What produced it is the same lesson cycle
45 recorded, one level down: cycle 46 built its option vocabulary from PMS literals but
only under seven PREFIXES, so a name like `mediaHeight` or `unwatchedLeaves`-on-an-episode
was outside the denominator by construction. A sweep's vocabulary bounds what it can find.

### Cycle 53 — the framework's own source, and the database's own schema

Cycle 52 read PMS's route table out of the binary. Cycle 53 read the **source and the
schema beside it**: the plug-in framework ships as uncompiled Python — 289 files, plus
`System.bundle/Contents/Code/*.py`, which *is* the `/system` surface — and
`Resources/com.plexapp.plugins.library.db` is an empty database template whose schema
names every column PMS can store. Cycle 52 had already met the first instrument without
recognising it: it fitted the framework's argument counting to twelve cells and then found
the rule in one grep of `agentservice.py`.

**19 findings, 21 commits, every one proved fail-then-pass against its own parent.**

The driver took the media flag images, and the two hunts took the framework and the
vocabulary.

* **Territory A, the framework from its own source** — thirteen findings. Every `@route`
  in `System.bundle` enumerated and then measured: eighteen more framework routes,
  five more sub-paths under every hosted plug-in, the eleven legacy agents and their
  attributions, and four envelope rules (a framework error does NOT close the connection;
  five routes put `Cache-Control` second; a framework container pages without an `offset`
  and still names its window; `/system//` is the framework's empty 404). Plus two writer
  corrections and the discovery that a state cookie the server cannot unpack makes every
  framework route answer Tornado's empty 200 — carrying the one `Server:` banner on the
  surface, which cycle 33 recorded as never sent.
* **Territory B, the attribute vocabulary and the schema** — four findings and one large
  negative. Ten more database columns are real query fields where plexsim treated each as
  unknown (and an unknown field answers EVERY row); `isAdult` is a real row attribute;
  a continue-watching row carries `includedAt`; and `includeAlternates=1` inlined artwork
  the row does not own. The negative is the census: 130 request families across three
  servers, **zero JSON scalar-type differences** where the references agree.

**One inherited bound retired and two recorded beliefs corrected.** HANDOVER §8 has
carried "legacy agents and the `/:/plugins` listing" as one bound since cycles 1–6; cycle
52 retired the listing half on the grounds that a fixed table in the image is not hosting,
and leaving the two halves split was drift rather than a decision, so the agents are now
listed too — 15 rows where plexsim answered four. Cycle 33's "no `Server:` banner at all"
is the API's rule and not the framework's, and cycle 18's "PATCH is a 405, the only one on
the surface" names two framework subtrees. **Six existing checks were re-measured and
updated** where they encoded plexsim's own four-agent catalogue rather than the surface.

### Cycle 52 — the server's own binary as the route table

Cycle 51 was clean, so cycle 52 reopened the loop the only way it has ever been reopened:
**by recognising an instrument.** Fifty-one cycles built plexsim's route table out of what a
CLIENT was seen to ask for. The **Plex Media Server binary** names the endpoints no client
sends — it is 22 MB, readable on both references, and its string table carries PMS's own
route templates with their parameter captures. Diffing the two binaries splits the
vocabulary per version for free.

**22 findings, 22 commits, every one proved fail-then-pass against its own parent.**

The driver took the route table itself and found that **the legacy dispatcher's prefix
listing is a rule, not a table**: a breadth-first walk over the alphabet recovered 154
listings and one rule reproduces all 154 with zero membership mismatches, where cycle 50's
captured 58 paths left 119 prefixes answering an empty listing. It also found **three static
files at the top of the surface** (`/crossdomain.xml`, `/clientaccesspolicy.xml`,
`/favicon.ico`) that answer **with no token** — the third, fourth and fifth exception to the
401 rule — that **`/:/plugins` is seventeen constant rows** with a whole second plug-in
framework subtree under it, that **the framework's cookie rides only its own responses**,
that **`/connections` is `text/plain`**, and that **the unauthenticated partition is wider
than cycle 16's eleven**.

Two hunts ran beside it.

* **Territory A, the artwork family** — five findings. `/:/resources/*` is **70 names**, PMS's
  whole `Resources/Graphics` tree, where cycle 29 fixed a ten-name sample at seven; the
  artwork read leaves answered **any verb**, including a bare OPTIONS returning 56 KB of
  JPEG; the **photo transcoder** fetches the path it is given, so a section composite — the
  path Plex Web builds every library card from — transcodes to its own jpeg where plexsim
  500'd it; a collection's and a playlist's artwork leaves follow the item ownership rule;
  and an empty playlist's poster candidate named a relative url.
* **Territory B, the leaf vocabulary the binary names** — nine findings, six of them
  refusal shapes measured on both versions (a stream, account or device id's extra segment;
  the four plug-in framework actions under `/system/agents`; the augmentation id's parse;
  all four transcode prefixes; the part leaf's changestamp/filename arity), plus
  `/hubs/sections/{id}/popular`, the Categories tile's `blendColor` table, the actor axis's
  silhouette, and the section save path reading `.tag.role` as a scoped filter.

**Two recorded beliefs overturned.** Cycle 29's "`/:/resources/` is SEVEN names" was a
ten-name sample; HANDOVER §8's inherited "legacy agents and the `/:/plugins` listing" was a
bound on a fixed table in the image, which is the same class as the profile files cycle 31
read out and shipped. Cycle 18's "PATCH is a 405, the only one on the surface" names two
framework subtrees rather than one, and cycle 16's eleven 401 roots are thirteen plus three
own-refusal paths and a redirect.

### Cycle 51 — clean stopping repeat over every Cycle-50 axis

Cycle 51 froze behavior/test head `4069fd4` and assigned the recovered exact-request and
official-client findings, catalog/hierarchy surface, and claim/format/transport/discovery surface
to three independent read-only audits. All three reports end literally **`NO FINDINGS`**. They
contacted no protected reference, scratch copy, plex.tv endpoint or media body; retained external
denominators are labelled as retained rather than presented as fresh observations.

The exact-request audit closes Territory A **23/23** and Territory B **8/8** against 421 retained
request pairs / 37,112 leaf cells, five official-client sessions / 519 flows / 34 method-path
shapes, the 70-exchange durable walk, and the checked-in prefix and regional fixtures. The catalog
audit closes all 12 assigned catalog changes and repeats catalog features **54/54**, regional
preferences **3/3 and 1,122/1,122**, navigation **28/28**, decisions **2,340/2,340**, mediainfo
**35/35**, GDM **103/103**, and prefix registry **58/58**, plus the retained 36 axes, 18 subsets
and complete option/control matrices.

The format/transport audit locally repeats the 116-family claim registry as 111 safe families /
444 GETs, 173 safe dual-format pairs / 2,540 nodes / 13,072 scalar occurrences, nine `/system`
writer routes, the 92-cell numeric-platform matrix plus four repair controls, every maintained raw
transport boundary, TLS **10/10** under both LibreSSL and TLS-1.3-capable OpenSSL, the eight GDM
lifecycle hooks, and all regional/prefix regressions. It separately re-audits the retained 1,776-
request claim and 692-pair safe dual-format captures. The completed 78-observation/26-socket
`/connections` matrix remains terminally underivable for exact emulation. There are no actionable
or unbounded unresolved cells, so this is the clean cycle that stops the loop.

### Cycle 50 — recovered, audited, and completed entry by entry

Cycle 50 began as a recovered branch: seven measured commits survived, but the original session
had ended after finding an eighth change, three hunt reports still held **18 unimplemented
entries**, three transport/GDM rules lacked durable discriminators, and no post-cycle full suite or
handover existed. The recovery audit replayed the surviving evidence, separated the two apparently
bounded findings on both disposable versions, and implemented every reported entry. Territory A
is **23/23**, Territory B **8/8**, and Territory C **10/10**; the remaining actionable Cycle-49
entry, numeric-platform list failure, is built too.

The largest corrections were the per-session playback-progress latch, exact regional `/:/prefs`
documents, the top-level gate/all-row-current-count searchable-tag rule, season-scoped state-aware
OnDeck, show/catalog projection ownership and dating, a distinct stored untyped runtime, the
framework `/system` XML writer, background-playlist children, raw transport boundaries, the exact
58-path legacy prefix registry, and GDM's section-only `Updated-At` lifecycle. Every finding has a
focused discriminator; stale UI assumptions about OnDeck were re-measured and corrected rather
than preserving a fixture accident.

The safe claim repeat covered 111 of 116 families over four hosts, two formats and two auth states
(**1,776 requests**). The safe dual-format repeat issued **692 pairs**, parsed 614 / 9,206 nodes,
and found zero structure, set, order or scalar-type mismatch; the retained complete corpus remains
1,331 pairs. The served-client graph is 208 JavaScript files / 9,318,734 bytes, with no reachable
new PMS family after the background-child repair. Regional preferences are three 174-row captures
and 1,122 exact localisable cells; the prefix registry is 58 paths × three references = 174 exact
GETs.

`/connections` received the requested disposable-only expansion: 78 observer snapshots and 26
bounded sockets across both PMS versions. Pending, Routing, Paused/Pipelined, Finished and the
~20-second cleanup are measured, but unstable ordinals/order, invisible request-number events,
scheduler-coupled cleanup and the excluded byte-count form prevent an exact row. It therefore
remains absent rather than fabricated. The first wrapper caused the Cycle-50 safety incident in
§9; all contaminated observations were discarded, both copies were restored and isolated, and
the replacement matrix never left container loopback.

### Cycle 49 — the LAN, the connection and the claim

Cycle 48 was clean on every axis it repeated, so cycle 49 reopened the loop the only way it
has ever been reopened: **by recognising an instrument**. Every measurement this programme
has ever made happens over TCP 32400 *after a client already knows the address*. Nobody had
asked how a client FINDS the server, what a real PMS does with the TCP stream around a
request, or what the CLAIM itself moves — and the last of those is the project's own
failure class E, unaudited for forty-eight cycles.

**26 findings, 21 commits, every one proved fail-then-pass against its own parent.**

The driver took GDM, the LAN discovery protocol, and found a whole surface plexsim answered
nothing on: two UDP ports, a multicast group, a prefix-compare trigger bisected byte by
byte, a seven-header reply whose one claim-dependent field is `Host`, a five-second search
of its own, and `/servers` as the list that search populates. The proof is the strongest
this programme has had: **a real Plex Media Server discovered plexsim on the wire and listed
it**, at the address and port plexsim advertised, within six seconds of the module starting.
The same territory then found that `/servers`' own row had been answering `127.0.0.1` and a
constant `32400` for the life of the field, and that `/` was missing the transcoder quality
ladder the client's Quality menu is built from.

Three hunts ran beside it.

* **Territory A, the claimed-vs-unclaimed census** — 116 read families over the 2×2 of claim
  and version. 15 claim-dependent families, 40 claim-dependent container attributes, and
  plexsim correct on all but one: `ButlerTaskGenerateAdMarkers`, which cycle 35 measured on
  two UNCLAIMED copies and recorded as shipping disabled where both claimed references ship
  it enabled. It also found `/playlists/sorts`' sixth row, two real endpoints behind the
  legacy dispatcher (`/dlna`, `/manage`), the asset-404 connection state and the photo
  transcoder's refusal header order.
* **Territory B, the connection and transport layer** — every cell from a raw socket. Eight
  findings, three of them serious: one request could produce two responses on plexsim and
  put every later response on that connection off by one; fifty dribbling clients held fifty
  threads where a real PMS dropped all fifty at 20.6 s; and a client that vanished
  mid-request COMMITTED a write a real PMS discards.
* **Territory C, dual-format equality** — 1,331 dual-format pairs, 23,674 element nodes and
  1,298 JSON scalar-type cells, which found exactly one type mismatch and one ordering
  mismatch. That is the useful negative result. Four more findings came out of the tag-rich
  row it had to MAKE to be non-vacuous, and one of them — `Style`'s slot in
  `_CHILD_ORDER` — is a correction to a table cycle 23 declared settled, because no
  reference library holds a movie carrying a `Style` at all.

**And the cycle found that both references were broken instruments.** `GET /devices` and
`GET /statistics/bandwidth` answer **500** on kids and on zen, because a device row whose
`platform` column parses as a number crashes PMS's own serialiser (`std::bad_cast`), and
that row is written by an ordinary **GET** carrying `X-Plex-Platform: <number>` with no
client identifier — the shape a uniform-zero header sweep sends. kids' row was healthy in
the byte copy taken 2026-09-13 and is corrupt now, so the damage happened inside this
programme. kids has been repaired to its exact original (`LG TV`/`iOS`, read out of
plexscratch); **zen has not**, because it is production, it takes GET and HEAD only, and its
original value predates the copy and is unknown. See §9.

### Cycle 48 — the stopping repeat reports no findings on every covered axis

Cycle 48 froze the repaired implementation/test head `768377a` and repeated the finite Cycle-46
axes rather than choosing a fresh hunting direction. This is the required clean cycle after Cycle
47's daily-renewal and harness-order findings.

Territory A replayed the complete query-option universe: **116 names × 76 safe container families ×
3 hosts = 26,448 cells**, with duplicate per-host baselines and the same three volatile statistics/
updater leaves excluded. The final partition is 19 common moving options, four version/content
splits, 92 no-ops, the one deeply-analysed `includeBandwidths` bound and **zero actionable mismatches**.

Territory B exactly replayed **61 normalized request headers / 88 exact source spellings** over 44
safe families, three hosts and absent/source-shaped/zero states: **24,156 logical cells and 16,842
physical requests** including 474 targeted controls. All 44 baselines were stable on all hosts;
61/61 primary and 61/61 zero-control movement rows were unchanged from clean Cycle 47; the
Pipeline and Neuter fixes remain exact; reference-agreement mismatches are zero. Its report and
machine classification both end `no findings`.

Territory C freshly processed the frozen **1,829-exchange** outbound corpus: 1,620 reads plus 209
counted-but-unreplayed writes. The target decrypts all **140/140** captured scrobbling service
ciphertexts, and the latest preceding plaintext equals the Bearer on **309/309** state requests.
All 42 observed rating cases partition exactly into absent/preserve 12, explicit -1/clear 20 and
nonnegative/set 10. The daily user → 11-second feature refresh, 86,400-second suppression/due
behavior, combined persistence and last-good preservation were independently driven. Every read
was re-dispositioned; the report ends `no findings`.

Territory D rebuilt the durable first-party universes on the final head: **745** behavior-bearing
history rows across 29 current source files and 11 maintained regression files, including all
**24/24** Cycle-41 fixes; the unchanged complete current client graph (**205 assets, 9,314,084
bytes**); **24,452** sidecar advertisement leaves; **49 direct controls / 39 normalized families /
29 explicit method predicates / 2 mutating actions / 1 readback**. Forty-seven controls join
current implementation and regression evidence; the two music-transcode controls are terminally
outside the advertised movie/show boundary; no join is unresolved. Historical dispositions are
743 pass, two terminal credential-bound media rows, zero not-run/actionable. Its independent fresh UI run
passes **1,629/1,629**, the two new cloud commits pass their maintained account-state regression,
and its report ends `no findings`.

Root independently repeated UI **1,629/1,629**, catalog features, mediainfo **35/35**, decisions
**2,340/2,340**, sessions, heartbeat **13/13**, account state **15/15**, part bytes **11/11** and
navigation **28/28**. The checked-in fixture's account-scoped media links remain the named conformance/decode
bound; queue and decision behavior are still covered, and the independent local-CDN part suite
closes the media byte grammar without a credential. Protected-reference state is byte-identical to
the pre-cycle snapshot at `e0502d2163bd45c5e2e54bf4ef3fff5ca855423d9ac875bc3caa18c78ec803f3`;
exact-value secret scans are clean, and all owned listeners are stopped.
Every territory report ends exactly **`no findings`**, so the loop stops here until a new instrument
or a changed client/server version creates a new finite axis.

### Cycle 47 — repetition found daily renewal and a test-order defect

Cycle 47 did not broaden the target surface. It repeated the moving Cycle-46 axes and repaired the
evidence universe whose scratch-only inputs Cycle 46 had found missing.

The complete header replay was clean at **24,156 logical cells / 16,842 physical requests**: both
Cycle-46 `/web` findings were exact and no new mismatch appeared. The outbound capture had grown
to a frozen **1,829 exchanges**. Its 26 new consent/state pairs proved the watch importer against
real ciphertext — 13/13 service credentials decrypted, 26/26 state requests used the latest
Bearer — but exposed one product residual: a real PMS renews the owner profile and feature table
daily. Commit `3cff10e` adds claimed-startup/post-claim and 86,400-second renewal, in measured
user-then-features order with an 11-second gap and atomic last-good persistence.

The finite-census territory refused to pretend the deleted Cycle-41–43 scratch rows still existed.
It reconstructed durable denominators from first-party git ancestry, checked-in measured sidecars
and the complete current public client graph: 743 then-current history rows, all 24 Cycle-41 fixes,
205 official-client assets, 24,452 scalar advertisement leaves, 49 direct controls and 39 normalized
families. That reconstruction found no product mismatch, but found one audit-harness defect:
six Cycle-46 option checks ran after unrelated edits, unmatches and deletes had consumed the rows
that distinguish them. Fresh-server discriminators passed all six. Snapshotting the exact read-only
responses while the fixture is pristine makes the same product pass the whole UI suite
**1,629/1,629**; the final repair is `768377a`.

### Cycle 46 — query/header namespaces, live availability, Desktop and outbound state

Cycle 46 executed all six directions Cycle 45 left open.

1. The query-option vocabulary was mechanically derived from PMS and official-client literals, not
   hand-written: 116 names over 76 safe families and three hosts, 26,448 cells. Four findings came
   from the opening pass and ten from the expanded pass: file-availability sibling grammar,
   `includeGuids`, `includeMeta`, provider `includeStorage`, five full-row global-hub expansions,
   search `includeCollections`, Shared `includeSummary`, and source-level `skipCount`. The final
   pass has zero mismatches; `includeBandwidths` is the sole deliberate analysis-data bound.
2. The header census exhausts Desktop framework strings, both complete web bundles, PMS strings and
   all plexlab corpora: 61 normalized names / 88 spellings. Its 8,052 broad candidate cells plus
   474 controls found the `/web` escapes for exact `X-Plex-Http-Pipeline: disable` and
   `X-Plex-Neuter-Redirect: 1`; both were fixed and later exact-replayed.
3. The live/dead provider check made `checkFiles`, `asyncCheckFiles` and
   `checkFileAvailability` agree with the byte path: live is true/true, `unavailable_file` is
   false/false and a 404, and other resolver failures remain 502. `test_part_bytes.py` now drives
   both outcomes locally and has 11 checks.
4. Plex Desktop 1.115.0 was completely inventoried. Its installed Plex Web 4.160.0 runtime has
   202 runtime assets and adds no request family to the complete 205-asset public graph. Seven
   native network/IPC classes were classified; native mpv consumes the URL Web already chose.
   The signed-in live profile could not be isolated from plex.tv/account/device traffic safely,
   so actual mpv Range cadence, timeline cadence and native decode are explicit terminal safety
   bounds, not unvisited cells.
5. The first 1,665 outbound exchanges were classified completely. Two actionable reads landed:
   exact account feature UUID acquisition (`d75f48a`) and cloud watch/rating import (`a4bd148`).
   The latter reproduces the measured consent, scrobbling-service credential, opaque cursor and
   rating semantics. The PMS binary's static 32-byte decrypt secret is not checked in and must be
   injected through `PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64`; without it sync stays off.
6. The finite-census replay passed all materializable product atoms and all 24 Cycle-41 cross-fix
   audits, but correctly reported one evidence-process finding: the old 332-row/1,746-row base
   matrices and builders had been deleted by an unrelated `/private/tmp/plexsim-*` cleanup. Cycle
   47 replaced that scratch dependency with the durable first-party universes above.

Nineteen product findings landed in nineteen behavior commits: **14 query, two header, one live
availability and two outbound account-state findings**. The separate secret-sentinel update and UI
harness-order commits are infrastructure, not server findings. Every product finding has a
fail-on-parent/pass-on-child regression.

### Cycle 45 — the path name space, swept from the servers' own words

Cycle 44 reopened the loop by recognising a new instrument. Cycle 45 generalised the *method*
that had found cycle 44's `/rating`. That finding came out of a hand-written list of section
leaves, which is the weakest kind of sweep: it can only find what someone thought to type. So the
driver built its candidate vocabulary from the **references' own emitted words** — every key,
attribute name, element name and hub identifier the two servers write, 227 names — and asked all
three servers for each, in every position a path can put it.

| namespace | cells | findings |
|---|---|---|
| item leaves `/library/metadata/{id}/{name}` | 908 | 0 |
| section, top-level, `/library/{name}`, `/hubs/{name}` | 1,135 | **3** |
| twelve more prefixes (`/:/`, `/system`, `/media`, `/status`, `/statistics`, `/services`, `/hubs/home`, `/library/all`, `/library/sections`, `/updater`, `/security`, `/butler`) | 2,724 | 0 |
| two-segment leaves, item and section | 1,816 | **2** |
| section leaf + extra segment | 185 | **1** |
| item leaf + extra segment | 212 | (the same finding) |
| GET and HEAD over 76 reachable paths | 152 | **1** |
| eighteen server-side `X-Plex-*` request headers on three container paths | 108 | 0 |

**8,240 cells, 8 driver findings.** Two hunts on top of that produced 23 more, and the cycle's own
re-measurement of what the hunts reported produced four more corrections. **27 commits, every one
proved fail-then-pass by `sdrive.py`.**

What landed, largest first:

- **`/library/sections/{id}/{name}/top` is a real leaf** — the fifth two-segment section leaf, and
  plexsim 404'd every form of it. PMS's own route table (read out of the binary's string block)
  spells it `/:tagType/top` with no constraint on the segment, which is why any single word
  matches. It answers the section's TAG TABLE for the type the name resolves to, ascending by tag
  id, with the `key` built from the REQUESTED name and the `filter` naming the tag's OWN type.
  Eleven names resolve in six pairs — a tag name and a metadata COLUMN name sharing each number —
  and the 200/404 partition over 91 swept names on four servers is exact: the filter/axis
  vocabulary answers, the eleven edit-path-400 names and the section's own leaves do not.
- **`includeAlternates=1` inlines the agent's remote artwork on a FULL row and nothing else** —
  and the hunt's "1.43.4-only" framing was refuted by its own instrument (§3.2).
- **`Accept-Language` ranked the whole list**, and **`X-Plex-Accept-Language` is a third
  localisation input** ranked between the two plexsim knew. 39 cells, identical on both
  references, including a `q` grammar that is the OPPOSITE of the `Accept:` parser cycle 27
  measured: any malformed member disqualifies the entire header.
- **`/library/metadata/{id}/theme/{v}/metadata` is a GET that MINTS A ROW**, measured on the
  disposable copies because measuring it on the lab is what left four orphan rows there. The
  `guid` is `library://` + the SHA-1 of the request path itself.
- **`/library/shared` is a whole subtree** behind the one leaf plexsim answered, and an empty
  `?sort=` under it **crashes a real PMS** (§9).
- **`ownerFeatures` and `subscriptionFeatures` are the same string**, which plexsim answered two
  different ways.
- Eleven more cross-type axes, an axis bucket that resolves against the global tag table, the
  bare `/sync`, `/library/arts`, the two Grandparents subsets, four more routed leaves, the
  container focus key, the pipeline header, six artwork PUTs, `includeSummary`,
  `asyncCheckFiles`, the `/security/token` value grammar, a `playbackTime` bound, an item leaf's
  extra-segment 404, a version segment's leading `+`, `HEAD` on `/common`, a `.webm` container
  corrected at LOAD, and a play that completes on a sub-minute crossing beat.

**Two regressions the cycle's own suites caught in its own patches.** The item-leaf extra-segment
404 broke `/users/top`, which the UI suite caught inside the same run; and the `extraType` refusal
reached the section EXTRAS leaf — the one listing that really reads the field — which only
`test_catalog_features.py` could see, because that is where the clip fixture lives. Both were
fixed with follow-up commits.

**And the cycle lost its own worktree.** The disk filled and the session scratchpad was removed
between two tool calls, taking the worktree, the patch file and the sweep outputs with it. Every
commit survived, because a git worktree keeps its objects and its branch ref in the main
repository's `.git` — which is the whole argument for one atomic commit per finding. Twenty
minutes of rebuilding, nothing lost but scratch.

### Cycle 44 -- the first cycle with real bytes, and the loop is open again

Cycles 42 and 43 both reported `no findings` and the protocol-conformance loop was declared
stopped. It was reopened by an **instrument change**, which is the only thing that has ever
reopened it: on 2026-09-17 the private `catalog.json` was refreshed with five real, playable,
account-owned Real-Debrid parts. Cycle 44 found **20** differences and implemented every one.

**The headline is the one the whole programme had been unable to see.** For fifteen consecutive
cycles `tests/test_all.py` reported `0/11 served`, because every Real-Debrid link in the
checked-in fixture is account-scoped and dead. Every rule about a media body was therefore
measured on a *reference* and implemented on faith, and plexsim's own byte path had never been run
against a single real byte. It applied **no `Range` grammar at all** -- the client's header went
to the debrid CDN and whatever came back was echoed -- so 13 of the 20 cells kids and zen agree on
were wrong. `bytes=-1` read the FIRST byte where a real PMS reads the LAST, which is how ffmpeg,
mpv and every embedder locate an MP4 `moov` or a Matroska Cues block; `bytes=<N+10>-` answered
`Content-Length: -10`, which is not a legal field value; a one-byte probe pulled the whole 121 MB
file from Real-Debrid; and a reversed or past-the-end range answered 206 where a real PMS answers
416. `_range_window` now holds the measured grammar once and both `_ranged` and the part path ask
it, so the answer no longer depends on the CDN's own leniency either.

`tests/test_part_bytes.py` is the cycle's instrument contribution: it stands up a local CDN and a
one-route Real-Debrid stub behind a new `RD_API` override and drives the REAL handler, so the byte
path is testable with no credential at all -- the reason it went fifteen cycles unmeasured.

**Two recorded beliefs were overturned.** A 416 of a MediaContainer DOES name its connection state
(only a binary body's does not), which corrects a rule recorded as universal since cycle 33 -- and
it was only visible with a KEEP-ALIVE request, because a probe that sends `Connection: close` is
measuring its own header. And **a `.webm` file's container is `mkv`**, measured on the identical
571,346,576-byte file on zen; the override that made it `webm` was justified as protecting Plex
Web's direct-play rule, and driven across all 78 profiles it changes no browser outcome and costs
one (`Sony Bravia 2013`). The corpus had recorded the right answer since cycle 10 --
`tests/fixtures/media/fun-1052407-0.json` is a `.webm` file whose recorded Media says `mkv` -- and
nothing read it, because **`test_mediainfo.py` compared every stream field and never the
container**. That is a suite carrying the refutation of its own subject for thirty-four cycles.

The two hunts were the live-media axis and the real-content-row axis; the driver took the
server-to-plex.tv axis, an advertised-key self-consistency sweep and a section-leaf name-space
sweep. Hunt B's second half read **Plex Desktop 1.115.0** -- the first non-Plex-Web official client
this programme has ever read -- and its bundle's path literals produced the
`/media/providers/metadata/{id}` family. Both hunts independently found `/library/sections/{id}/
rating` and the four 1.43.4-only codec axes, which the driver had found the same afternoon.

The full measurement record is in `docs/findings.md`, "The forty-fourth pass". Twenty findings in
sixteen commits:

| | |
|---|---|
| media bytes | the `Range` grammar (13 cells), the bare `bytes=-`, `X-Plex-Protocol` on a part read, the `Content-Type` keyed on the FILE's extension, the byte counter counting what was written, `POST`/`PUT` on the three-segment media path, and a 404 rather than a 502 for a file the provider says is gone |
| envelope | the container 416 versus the binary 416, `Cache-Control: no-cache` on every 416, and the CORS pair's place in all eight response families including the `/web` 302 |
| per version | four media-codec AXES that are 1.43.4-only, and `builtIn` being the string `"1"` on 1.42.2 |
| advertised | `/library/sections/{id}/rating` (advertised by plexsim and 404'd), the star bucket rounding its key down to an even star, and the per-id metadata-provider tables |
| decision | the missing "selected embedded subtitle is not the first embedded subtitle" refusal, and the `.webm` container |
| catalog | a season's `parentYear`, and season and episode `Guid` |
| plex.tv | the announce form, and `/myplex/claim` being POST-only |
| grammar | the two stream-select parameters, and an empty transcode `session=` |

**What this says about the stopping rule.** Cycles 42 and 43 were honest: both finite censuses --
the 332-atom historical universe and the complete official-client advertised-control graph -- were
clean, and they still are. Neither census could see any of this. The historical census asks "is
every recorded claim built?" and nothing had recorded the byte path, because nothing could measure
it. The advertised census asks "does the server answer everything the client's bundle can ask?"
and a `Range` header is not a control in a bundle. **A clean cycle bounds the axes it swept, not
the surface.** The loop stops when a cycle sweeps a NEW axis and still reports nothing.

### Programme status after cycle 48

Cycle 46 ran every item that had been listed here; Cycle 47 repaired the residuals its repeats
found; Cycle 48 replayed every finite denominator clean. There is no scheduled Cycle 49. Reopen the
loop only when there is a genuinely new instrument or changed external universe — for example a
new PMS/client build, a safely isolated Desktop runtime profile, a deeply analysed Part, a shared
account-state corpus, or a live catalog whose account-scoped links are current. Do not relabel one
of those named capability/safety prerequisites as an implementation guess merely to create work.

### Next: cycle 45 (superseded, run)

Cycle 44 reopened the loop by changing the instrument, and the same lever is still there.

1. **Finish the live-media axis.** Cycle 44 swept the part path, the envelope and the byte
   counters. Not swept with real bytes: a real PLAYBACK driven by an official client end to end
   (the decision the client actually sends, its timeline cadence, its range pattern, the session
   rows it produces), `checkFiles=1` against a live and a dead link, `/statistics/*` over a real
   session, and whether an unservable part should answer 404 rather than 502 (§3.3).
2. **The non-Plex-Web client axis, properly.** Hunt B read Plex Desktop's bundle statically and it
   produced a finding on the first sweep. What has never been done is DRIVING a non-web official
   client against plexsim and diffing what it asks for. Plex Desktop 1.115.0 is installed on this
   Mac; its native shell plays through mpv, so its part reads and timeline beats are its own.
   Its `PlexMediaServer.framework` is an 84 MB binary carrying PMS's own route templates and 48
   `X-Plex-*` names as string literals, several of which plexsim has never seen
   (`X-Plex-Strict-Ranges`, `X-Plex-Indeterminate-Ranges`, `X-Plex-Chunked`,
   `X-Plex-Container-Focus-Key`). That is a map of where to look, in the same way
   `Resources/Profiles` was for cycle 32.
3. **The server-to-plex.tv axis, the rest of it.** Cycle 44 swept the device announce out of
   `capture/outbound.jsonl` and found four differences. The capture holds 1,665 exchanges over
   twenty-odd hosts and paths -- `/api/v2/server/users`, `scrobbles.plex.tv/state`,
   `metadata.provider.plex.tv/library/metadata/matches`, `/markers` -- and nothing has read them.
   Most are out of scope as writes, but the READ side shapes what a claimed server knows about
   itself, and none of it has been compared with what plexsim answers.
4. **Re-run the two finite censuses against the new head.** Cycles 42 and 43 were clean against
   Cycle-41's tree; twenty findings have landed since. Territory A's 332 atoms and Territory B's
   1,221 advertisements should both be replayed, and the atom count widened by this cycle's rules.

### Next: cycle 36 (superseded, run)

1. **The FRAMES of the write surface, the rest of the list.** Cycle 35's hunt reached the library
   create and the butler table and reported the other seven items as already correct; take its
   report at §3.1 class F and re-measure the two that matter most -- `showOrdering`'s 45-frame
   cascade and the type-15/18 list pairs -- against both copies before believing them closed.
2. **The hub matcher's fuzzy BAND** (§3.3), now measured by word position and word length but not
   built. Decide whether plexsim should carry a similarity score at all; if it should, the 16-title
   zen sweep in cycle 35's report is the fitting set.
3. **A row that trips `video.separateFields`** (§3.3, §8). zen's only interlaced Stream is on a
   stale Media that answers "File is missing"; `handover/tools/mklangless.sh` shows the shape of the
   answer -- make one on plexscratch with `ffmpeg -flags +ilme+ildct`, scan it, and drive the 78
   profiles with `ddrive.py`.
4. **The dual-layer Dolby Vision disagreement** (§8): zen says one video Stream, the recorded note
   (from fun) says two. One reading of a real file on each side would settle it.
5. **A catalog with extras in it**, which would make cycle 29's clip spec buildable.

### Next: cycle 35 (superseded, run)

1. **The FRAMES of the write surface per version** -- cycle 33's hunt reached the refresh opening
   and cycle 34 never got a hunt to take the rest: the library create and delete counts, a
   show-level refresh, the type-15/18 list pairs beyond a collection create, the subtitle upload,
   the stream `PUT`, the butler tasks and their wrapper, `match`, a `?path=`-scoped scan and
   `showOrdering`'s 45-frame cascade, each on both copies. Use a `tv.plex.agents.none` library on
   both, or say the comparison cannot be made: the 1.42.2 copy's online agent can never finish any
   work.
2. **The hub matcher's CASE passes** (§3.3), narrowed in cycle 33 to "a capital macron fails in
   the FIRST word of a partial query and not in a later one" and untouched since.
3. **Decision limitations no measured row trips** (§3.3). The corpus is now a checked-in file
   with a driver (`handover/tools/ddrive.py`) beside it, so adding rows to it is cheap: zen holds
   interlaced, VFR, 10-bit and multi-Media rows, and each would exercise a limitation the 25 rows
   in the corpus do not.
4. **What the reference picks before a client has chosen an audio track** (§8). Both states of
   one row are recorded; what is missing is a second row of a different shape -- three tracks, or
   two of equal width, or the wider one first -- to tell "the widest default" from "the last one"
   and to find what separates Android, Popcorn Hour and the Roku family from the other 41.
5. **A catalog with extras in it**, which would make cycle 29's clip spec buildable.

### Next: cycle 35 (superseded)

1. **The FRAMES of the write surface per version, properly.** Cycle 33's hunt reached the refresh
   opening and explicitly left the rest: the library create and delete counts, a show-level refresh,
   the type-15/18 list pairs beyond a collection create, the subtitle upload, the stream `PUT`, the
   butler tasks and their wrapper, `match`, a `?path=`-scoped scan and `showOrdering`'s 45-frame
   cascade, each on both copies. Note the confound it found the hard way: the 1.42.2 copy is a fresh
   isolated install whose online agent can never do any work, so a sequence that depends on the agent
   is not comparable between the copies -- use a `tv.plex.agents.none` library on both, or say the
   comparison cannot be made.
2. **A fresh CLIENT-SESSION capture through the harness** (§4.1's plexlab), replayed against plexsim
   with `replay.py`. It is the founding instrument and the last full capture is cycle 27's.
3. **zen 19813's shape, made rather than found** (§3.3): two language-less audio tracks muxed into
   plexscratch's writable media, scanned, and driven against the 78 profiles. zen's fourteen
   language-less rows do not include one whose selected track is not the widest, so making one is now
   the only route.
4. **The hub matcher's CASE passes** (§3.3), which this cycle narrowed to "a capital macron fails in
   the FIRST word of a partial query and not in a later one".
5. **A catalog with extras in it**, which would make cycle 29's clip spec buildable.

### Next: cycle 33 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element order, the
   filter engine, client identity, the cross-endpoint cascade, the playback path, the JSON value-TYPE
   dimension, the HTTP envelope, Home's hub parameters, the search surface, the response BODY, the
   extras/clip surface, the same request twice, the notification channel at scale, the write surface
   per version, the derivation holdout, and now **concurrency**, the **fold tables** and the **decision
   model**. Candidates nobody has taken: the **FRAMES of the write surface per version** (cycle 31's
   hunt A took status codes and stored state and left the notification sequences, and 1.42.2 and 1.43.4
   already disagree on refresh coalescing); the **response HEADER byte form** -- header order, name
   casing, duplicates and the `Date` format on every response class, three servers at once, now that the
   JSON byte form turned out to be unmeasured; and **the rest of the reference image** --
   `Resources/`' scanner and agent bundles, its string tables (which could hold out
   `translations.json`), and the SQLite schema beyond the two columns cycle 31 used.
2. **zen 19813's shape, made rather than found** (§3.3): two language-less audio tracks muxed with
   ffmpeg into plexscratch's writable `long` media, scanned, and driven against the 78 profiles. It
   settles the model's last eight cells.
3. **The hub matcher's case passes** (§3.3): the capital-macron family by word position.
4. **The JSON FLOAT form** (§3.3), a few requests on zen.
5. **A catalog with extras in it**, which would make cycle 29's clip spec buildable.
6. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 32 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element order, the
   filter engine, client identity, the cross-endpoint cascade, the playback path, the JSON value-TYPE
   dimension, the HTTP envelope, Home's hub parameters, the search surface, the response BODY, the
   extras/clip surface, the same request twice, the notification channel at scale, the WRITE surface
   per version, and now the DERIVATION holdout against a real library. Candidates nobody has taken:
   **CONCURRENCY** -- two clients writing the same row, a read during a write, a second session on
   the same item (cycle 31's brief named it and the cycle went elsewhere); the **fresh
   client-session capture** through the harness, which is the founding instrument and has not been
   re-run since cycle 7; and the **frames** of the write surface per VERSION, which hunt A explicitly
   left (it took the status codes and the stored state).
2. **The reference's own FILES are an instrument now (§4.1), and it is barely used.** Two §3.3 entries
   closed on `Resources/Profiles/` in twenty minutes. What else is in that image: the scanner and
   agent bundles, `Resources/Profiles`' `<CodecProfiles>` (the decision model's fourth input), and the
   SQLite schema -- `metadata_items` alone has 52 columns, and cycle 31 used exactly two of them to
   settle a rule the API cannot express. Read, never write.
3. **The `title=` query FOLD table** (§3.3, §8). `c31/D/foldsweep.py` works and is cheap on kids: it
   gives 11 characters folding to `i`, four to `s`, two to `l`. Sweep the remaining letters and the
   entry closes.
4. **The decision model from the profile files** (§3.3): `redirect=`, `<DirectPlayProfiles>`,
   `<TranscodeTargets>`, `<CodecProfiles><Limitations>` against the item's own container, codecs, bit
   depth and channel count. plexsim hard-codes two names where 45 profiles declare direct play.
5. **`/livetv`'s 403** (§3.3): both copies answer 403 to every verb where cycle 18 recorded a three-row
   container from the same copy. Settle which is the surface's.
6. **A catalog with extras in it**, which would make cycle 29's clip spec buildable.
7. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 31 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element order,
   the filter engine, client identity, the cross-endpoint cascade, the playback path, the JSON
   value-TYPE dimension, the HTTP envelope, Home's hub parameters, the search surface, the response
   BODY, the extras/clip surface, the same request twice, and now the notification channel at scale.
   Candidates nobody has taken: the **fresh client-session capture** through the harness (cycle 27's
   hunt A had it -- read that report first, it is the only dimension on this list with a written
   brief and no cycle); **CONCURRENCY** -- two clients writing the same row, a read during a write,
   a second session on the same item, which is the natural sequel to the notification hunt and is
   where the write hang was actually found; and **the ORDER of a sequence of writes**, as against
   the frames of one.
2. **plexscratch42 is new and under-used (§4.6).** Two §3.3 entries name it as their method now and
   neither has been run: the eleven-character fold TABLE (write the eleven titles, read the sort
   titles back) and a `Common`'s string `userRating` (rate every row of one library). Both are an
   hour. And every notification finding this cycle is single-witness on plexscratch (1.43.4) -- the
   ping cadence and the backlog shape should be re-driven on 1.42.2.
3. ~~**plexsim's own ~256 s eventsource close**~~ **REFUTED in cycle 31**: a probe with no read
   timeout of its own sees the channel open at 460.2 s. The number was the old capture client's.
4. **A catalog with extras in it**, which would make cycle 29's clip spec buildable: `clip` rows,
   `primaryExtraKey`, the three projections and the section extras query all wait on it.
5. ~~**`tests/conformance.py`'s nine failures.**~~ **ANSWERED in cycle 31, and the recorded cause
   was wrong**: all nine are downstream of one part read, which the harness's `RD_TOKEN=dummy`
   turns into a 401 and which a real token turns into `unavailable_file` on the fixture's
   placeholder link. They cannot be made to pass, because a working `/d/` link is a credential
   the repo may never hold. Stop asking.
6. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 30 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element order,
   the filter engine, client identity, the cross-endpoint cascade, the playback path, the JSON
   value-TYPE dimension, the HTTP envelope, Home's hub parameters, the search surface, and now the
   response BODY and the extras/clip surface. Candidates nobody has taken: the **notification
   channel at scale** (which frames a library-wide operation really pushes on a 3,887-row library,
   and in what order); the **fresh client-session capture** through the harness (cycle 27's hunt A
   had it -- read that report first); and the **time dimension** -- what a real PMS answers to the
   same request twice, which is how cycle 29 found the transcoder's cache tell by accident.
2. **The instrument, not the effort.** Three §3.3 entries across cycles 21, 25, 27 and 28 all say
   "a writable 1.42.2 copy". plexscratch is 1.43.4 because it is a copy of the lab; a SECOND
   disposable server pinned to 1.42.2 -- fresh libraries, no plex.tv, the same socat hop -- would
   close the five media-codec edit fields, `PROFILE_NAMES`' 23 aliases, 1.42.2's Continue Watching
   projection and the scrobble/timeline pair in one cycle. It is the highest-leverage instrument
   work left.
3. **A catalog with extras in it**, which would make cycle 29's clip spec buildable: `clip` rows,
   `primaryExtraKey`, the three projections and the section extras query all wait on it.
4. **`tests/conformance.py`'s nine failures**, re-confirmed in cycle 27 as the fixture's dead
   Real-Debrid links. Prove that or find the real cause.
5. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 29 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element
   order, the filter engine, client identity, the cross-endpoint cascade, the playback path, the
   JSON value-TYPE dimension, the HTTP envelope, and now Home's hub parameters and the search
   surface. Candidates nobody has taken: the **response BODY below the container** -- the bytes
   of an artwork asset, a `/web/*` asset and a part read, which only cycle 16 has looked at; the
   **fresh client-session capture** through the harness (cycle 27's hunt A had it -- read that
   report before re-commissioning it); and the **non-Plex-Web client parameter sets** driven end
   to end.
2. **Re-measure every single-witness shape against plexscratch first.** Cycle 28's On Deck
   finding is the model: zen alone said "the detail row", the copy said otherwise, and the
   disagreement was the finding. §3.3's cycle-28 block lists four more shapes standing on one
   instrument, and the copy is a 1.43.4 witness for each of them.
3. **`tests/conformance.py`'s nine failures**, untouched since cycle 7 and re-confirmed in cycle
   27 as the fixture's dead Real-Debrid links. Prove that or find the real cause.
4. **The serialiser sweep's single-witness half** (cycle 27's item 2): `playQueueItemID` leading
   a play-queue row, `playlistItemID` mid-row and the decision row's string ids, all on the copy
   alone and all 1.43.4.
5. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 28 (superseded)

1. **Run the hunts on a dimension nobody has swept.** Closed and not to be re-swept: path,
   method/parameter, header, row, container, child element, attribute order, child-element
   order, the filter engine, client identity, the cross-endpoint cascade, the playback path,
   the JSON value-TYPE dimension and now the HTTP envelope. Candidates nobody has taken: the
   **fresh client-session capture** through the harness, which cycle 27's hunt A was given and
   which is the founding instrument (see its report before re-commissioning it); the
   **non-Plex-Web client parameter sets** driven end to end; and the **response BODY below the
   container** -- the bytes of an artwork asset, a `/web/*` asset and a part read, which only
   cycle 16 has ever looked at.
2. **The serialiser sweep's single-witness half.** Cycle 27's hunt found `playQueueItemID`
   leading a play-queue row, `playlistItemID` mid-row and the decision row's string-typed ids
   on the copy alone. All three are the family cycle 26 found on `/status/sessions`; all three
   need a writable reference, and the copy is 1.43.4.
3. **`tests/conformance.py`'s nine failures**, which nobody has looked at since cycle 7: are
   they plexapi limitations, catalog differences or real server differences?
4. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample.

### Next: cycle 27 (superseded)

1. **Run the hunts, on a dimension nobody has swept.** Two cycles in a row have now come back
   with a double-figure count from exactly that, and cycle 26 found the largest functional gap
   since the build-out in a surface everybody assumed worked. Closed and not to be re-swept:
   path, method/parameter, header, row, container, child element, attribute order,
   child-element order, the filter engine, client identity, the cross-endpoint cascade, and
   now the playback path. Candidates nobody has taken: **a fresh CLIENT-SESSION capture
   through the harness, replayed against plexsim** -- the founding instrument, not re-run
   since cycle 7 and the one thing that would have caught the `hasMDE` gap on its own; and the
   **non-Plex-Web client parameter sets** the decision finding exposes, driven end to end
   (Roku's, plex-for-kodi's, python-plexapi's), since every check in the suites was written by
   someone sending Plex Web's.
2. **`/status/sessions`'s serialiser, swept rather than sampled.** Cycle 26 found the
   alphabetical order and four string-typed values by comparing ONE session row with ONE
   detail row. Which other projections have a serialiser of their own has not been asked, and
   `/status/sessions/history/all` already answers a string `librarySectionID` beside a
   non-alphabetical order, so there are at least two shapes there.
3. **The `Role` order inside a session row** (§3.3), which is alphabetical after `filter` and
   is the one element the new rule does not fit.
4. **The session cutoff's bisection** (§3.3), if it is ever worth ~200 s a sample: the cutoff
   is bracketed at or below 184 s and the reaper tick at 26 s or more.

### Next: cycle 26 (superseded)

1. Run the hunts on a dimension nobody has swept -- the PLAYBACK surface was named here and
   is what cycle 26 took.
2. The three butler behaviours still unmeasured -- two are now built, the third recorded.
3. The collection and playlist `/refresh` and `/unmatch` FRAME sequences -- CLOSED by
   revalidation: plexsim already reproduced all four.
4. `/hubs/sections/{id}`'s own in-progress shelf -- built, and cycle 25's reading of it
   corrected: the rule is per LIBRARY TYPE.

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

**Cycle 64** (recorded, not built):

- `/playlists/{id}/items?includeMeta=1`'s `Meta.Type` titles stay English (the container carries a
  `ratingKey`, so the localiser skips it as a row); Plex Web never sends `includeMeta` there.
- A header value's leading whitespace: a real PMS consumes ONE optional space after the colon and
  keeps the rest in the value (`X-Plex-Platform:  Roku` is the platform ` Roku`), and a leading
  TAB is a 400; plexsim strips all of it. No client sends either.

**Cycle 63** (recorded, not built):

- `/library/sections/{id}/sorts?type=8|9|10|13` leaves Album Artist, Date Played, Date Rated,
  Popularity and Date Taken in English where zen localises them. plexsim cannot hold a music or
  photo library and Plex Web asks for the section's own type.
- A decision that asks to burn (`subtitles=burn`/`auto`/absent) while the SELECTED subtitle is an
  uploaded sidecar is a 400 on 1.42.2 and a 200 on plexsim; Plex Web sends `subtitles=sidecar`
  there, which is a 200 on both.

**Cycle 61** (recorded, not built):

- A collection renamed through its ITEM path and then deleted leaves its ORIGINAL tag on its
  members on both copies; plexsim removes it. No client renames a collection through that path
  (python-plexapi's `editTitle` and Plex Web's Edit dialog use the section path).

**Cycle 60** (recorded, not built):

- Manage Recommendations' writes: unmeasurable, see §3.3.
- `X-Plex-Client-Profile-Extra` on a PLAIN-form decision is honoured by a real PMS (1,018 of 1,027
  cells identical across versions; limitations, added direct-play profiles and transcode targets)
  and ignored by plexsim. Every official client that sends it sends `hasMDE=1`, where it changes
  nothing plexsim could answer differently. hunt B's scratch `regress60b.py` F2 is written for the day a
  plain-form client is seen.
- A beat whose `playQueueItemID` names no live item is echoed in its `playing` frame.
- A session row's `User` carries only `id` on the unclaimed copy (claim).
- `/system/scanners/{N}` answers XML whatever `Accept` says; the `playbackState` latch after a
  stopped beat; `/hubs/promoted?includeGuids=1`; the `tagId` value grammar (`-1`, `1.5`, `+1`,
  `/library/all`, `/hubs`); `/query/parse` `fieldTitle`s in 932 cells; `/library`'s three Directory
  titles in sl/sr/da/uk; `/butler`'s `Backup Database` in sr.

**Cycle 59** (recorded, not built):

- **Optimize through python-plexapi** (CLIENT-VISIBLE to a python-plexapi user, kept on the Optimize
  bound): `DELETE /playlists/generators?type=42` is a 200 on both copies (400 for any other type)
  and `GET /library/tags?type=42` is a fixed three-row `mediaProcessingTarget` table on all four
  servers; plexsim advertises no encoder, so `Video.optimize()` and `optimizedItems()` stay
  unsupported.
- A real PMS echoes `Origin` into `Access-Control-Allow-Origin` only for `*.plex.tv`,
  `localhost:<port>` and `127.0.0.1:<port>`, and answers `https://app.plex.tv` to any other;
  plexsim echoes every origin. No official client is served from another origin.
- Collection row attribute order (`librarySection*` placement, `maxYear`/`minYear`).
- A smart playlist's `content` on `/library/metadata/{id}` (plexsim) and not there (1.42.2).
- The unpause `backgroundProcessingQueue {queueRefreshed}` frame (Optimize bound).
- A split row's media delete resolving no Media id (the copies cannot delete media at all).
- A match's children after a COMPLETED match on a real PMS is unwitnessed (neither copy completes
  a match); plexsim now cascades the rename as an edit does, which the parent-read derivation
  implies.

**Cycle 58** (recorded, not built):

- **The section Continue Watching answer is cached per exact URL** on plexscratch42 (CLIENT-VISIBLE,
  deliberately not built). `/hubs/sections/{id}/continueWatching/items?contentDirectoryID=` and
  the exact `/hubs/sections/{id}?count=12&...` page serve a stale list while any variant URL
  answers the current one. A timeline beat and a season scrobble refresh it; an episode
  scrobble or unscrobble, a rating and a season unscrobble do not. Only 11 write types were
  sampled, so the refresh rule is incomplete; build it only once that rule is.
- Under `X-Plex-Sync-Version: 2`, the optimized-versions playlist (`type=42`) row and its items
  container carry `allowSync="1"` (an over-reach of cycle 57's fix), and `GET /playlists/{id}` for
  that playlist is a 404 where zen and kids answer 200. No client reads either.
- A collection renamed through the ITEM path and then deleted leaves its members tagged on s42;
  plexsim removes the tag. No client renames a collection that way.
- `PUT /library/collections/{id}/children` is a 400 on s42 and a 200 with an empty container on
  plexsim. The client's inline field edit is not shown to reach a collection.
- `/statistics/media`'s `Device` rows are exactly the devices its rows name on the references;
  plexsim lists every device.

**Cycle 57** (malformed-only, or invisible to a client):

- The legacy agents' JSON `hasPrefs`/`primary` booleans and their XML attribute order.
- A collection's `/hubs/metadata/{id}/related` container's librarySection triple (the hub list
  is empty either way), and playlist row attribute order.
- A preplay read's `async*` options pushing a `library.refresh.items` activity that lives under
  ten milliseconds and persists nothing.
- Notification frame JSON key order, and a playlist's locked `Field` on the listing row.

**Cycle 56 -- MALFORMED-ONLY under the client-impact rule** (each measured on zen and kids;
reachable only with input no client sends, or a difference no client reads):

- `/clients/profiles?type=42` marks `Universal TV` `selected` (the Optimize dialog, hidden here).
- A show row puts `titleSort` directly after `title`; an item's `/extras` carries no `allowSync`.
- The metadata-provider tables' Personal Media id (5) and type lists, and `order` in XML.
- `/hubs/home/recentlyAdded?includeMeta=1`'s Type key and `active`, and no Meta under `personal=1`.
- Negative hub `count` (four rules per path) and `/hubs?count=2147483647` as a 500.
- The `index=` grammar (`1.5` an empty 200; a space 200 on 1.42.2; `abc` a 500 on `/playlists/all`
  and a show library's `/onDeck`).
- `sort`/`group`/`sourceType` on `recentlyViewedShows`, `sourceType` on a section `allLeaves`,
  `group=`/`limit` on `/playlists`, a `contentDirectoryID` past int32.
- `limit` on `/grandchildren` (caps), `/library/recentlyAdded?limit=+1`, the extras leaf (zen
  ignores it) and the section search (caps).
- History and statistics value bounds (`+1`, past int32, `lan=01`).
- `/system/agents?mediaType=` empty or past int32 (400); `type=` on `/search` and `/common`;
  `/common?includePreferences=1` carrying none.
- `+`- and space-led `X-Plex-Language`, and the separator rule on `X-Plex-Accept-Language`.
- A repeated `type=` on `/library/all` takes the last; `sort=viewedOrInProgressLeafCount`.
- Hidden-column comparisons: `guid<=` as a prefix, `isAdult` defaulting to 0,
  `mediaAnalysisVersion`, `dovi` values, an unset stream language compared as `''`.
- 55 filter names refused on a show-scoped hub page (plexsim refuses fifteen), and seven columns
  that empty a hub page's TV shelf on the references.
- `/hubs/metadata/{id}/postplay?includePreferences=1` carrying `Preferences`.

**Cycle 55.**

- **How `/hubs/search?sectionId=` reorders its hubs** for an id naming a section (its own types
  first, rows unchanged). The refusal is built.
- **Search `limit` at the int32 boundary**: kids 500s `2147483647` and `9223372036854775807`
  and answers `2147483648`, and `4294967296` crashed it. Only "outside int64 is a 500" is built.
- **Which genre the genre shelf picks** (random per request query string); the rows' order is
  built.
- **A parent's `lastViewedAt` derived as `max(children)`** when its own state is absent -- state
  written without the cascade, which this server never has.
- **1.43.4's refusal of an empty `group=`/`sourceType=` on a hub page**; 1.42.2 drops it and
  plexsim follows 1.42.2.

**Cycle 54.**

- **The tagging join** (`tagId`, `tagType`, `taggingIndex`, `taggingCreatedAt`). Real query
  columns on both references (a word is a 500), but every kids movie -- Big Buck Bunny, with
  no visible tag, included -- carries hidden `tagType=318` taggings that `taggingIndex`
  matches on. plexsim holds no such rows; modelling the match would be a guess.
- **A `YYYY-MM-DD` value on an INTEGER column on 1.42.2.** 1.43.4 refuses it; 1.42.2 takes it
  with a meaning that shifts per column, and `2/3`, `0x1`, `1e3` pass where `5d` and `1abc`
  refuse. Not one grammar, and no client sends it.
- **What `/search`'s `count` and `/statistics/bandwidth`'s `bytes` do to the rows.** Both
  are refused as measured; `count=2` answers ten rows on zen and `bytes=N` ten rows where
  three carry `N`, so neither is one rule.
- **`pinnedContentDirectoryID` scoping zen's hubs.** plexscratch42 (1.42.2, unclaimed) and
  kids ignore it; the claim and the version cannot be separated here.
- **zen's section `recentlyAdded?unwatched=1` keeping `totalSize` 98** where kids drops
  5 to 4. Recorded, not explained.
- **The shallow analysis of the container probes.** plexscratch42 leaves `bitDepth`, colour
  and `codecID` off the seventeen probe files even after an analyze, so only their
  container-level fields are compared; `.vob`, `.f4v` and `.mp4v` are not scanned at all,
  and the `.ts` AAC probe came back with no `channels`.

**Cycle 53.**

- **The pixels of every bundled image.** The 742 media flags, each plug-in bundle's
  `icon-default.png` and `attribution.png`, and cycle 52's 68 `/:/resources` files are
  served at the file's own media type and geometry with plexsim's placeholder inside.
  Shipping several megabytes of a product's artwork to reproduce the bytes is not done.
- **A populated `/system/channels/*` row.** No instrument hosts a channel, so only the
  empty container is measurable; `get_directory`'s `DirectoryItem` shape is readable in
  `peerservice.py` and not reachable here.
- **`/system/messaging/{register_for_event,broadcast_event}`'s semantics.** They mutate
  the System bundle's event dictionary; only their status and body were measured, on the
  disposable copies.
- **The mechanism behind the `Cache-Control` split.** Five framework routes put it second
  and every other `/system` response last; the set separates cleanly along the
  peer-service routes plus one across four servers and does not fall out of
  `construct_response`. The SET is measured; the rule is not identified.
- **The fully-supplied framework call.** `/system/agents/search?identifier=x&mediaType=1&
  lang=en` runs the real agent function and answers a value-dependent traceback
  (`KeyError: 'x'`). The under-supplied forms are reproduced exactly from the signatures;
  this one is PMS's own internals.
- **The legacy agents' WORK.** Listing them, their attributions and their preference
  documents are fixed tables in the image. Identifying with one, its contributor chain and
  its per-agent pane beyond the captured document are not claimed.
- **`includedAt`'s independence from `lastViewedAt`.** Equal in all five witnesses; what
  makes them differ is not reachable on any instrument, so plexsim stamps the same value.
- **`deletedAt` and the trash.** zen lists 2,234 trashed movies whose rows carry it; kids
  holds none and making one needs a file removed inside the container plus a scan. One
  witness, one version -- and plexsim has no trash state.

**Cycle 52.**

- **The plug-in framework's kwarg arithmetic.** `/system/agents/{action}` passes the query
  through to PMS's own Python function, so the TypeError sentence moves with it:
  `?mediaType=1` makes `media_get` "(2 given)", `?primaryAgent=x` makes it "got an
  unexpected keyword argument", and `search` DROPS a name it does not know where the other
  three raise on it. The recognised names and that split are implemented as measured over
  twelve cells; the general rule is the signatures of functions inside
  `Framework.bundle` and is not derivable from the API.
- **The pixels of the 68 bundled `/:/resources/*` images.** Their names, media types and
  geometries are measured and served; reproducing 4.7 MB of a product's artwork to
  reproduce the bytes is not done. The two files that are not images are carried verbatim.
- **A movie library's `/hubs/sections/{id}/popular`.** zen answers a 200 empty container and
  kids a **500**, and the driver is not identified — not history, not watch state, not the
  version, not the claim. plexsim answers zen's, being the version it reports.
- **`/library/parts/{id}/indexes/{sd|hd}[/{offset}]`** (the BIF seek-preview index) and
  **`/library/media/{id}/chapterImages/{n}`**: 404 on every instrument, because no
  instrument holds a generated index or a chapter.
- **`/connections`' acceptor pool, ordinals and request allocator** — cycle 50's bound,
  unchanged. plexsim now answers the measured `text/plain` shape with its OWN live
  connections; PMS's `??? - Uninitialized` slots are not modelled.

**Cycles 50–51.**

- **`/connections`, after the full disposable-only matrix.** The requested follow-up is complete:
  78 successful observer snapshots and 26 bounded raw sockets across unclaimed 1.43.4 and 1.42.2.
  Partial requests are Pending; a partial pipelined request is Paused/Pipelined; a close becomes
  Finished; the incomplete-request cleanup falls just after 19.78 seconds. An exact response is
  still terminally underivable: the leading integers and row order are snapshot-unstable, the
  hexadecimal request allocator consumes invisible events, Finished cleanup is coupled to an
  opaque sweep, and the byte-count form needs excluded media traffic. A partial implementation
  would fabricate client-visible state, so the route remains absent.
- **The background generator/job subtree.** The served client calls generator and generator-item
  paths only after an Optimize/conversion row exists. plexsim advertises no encoder, its permanent
  background queue is empty, and no safe read creates that state. The queue/list/children surface
  is built; deeper job CRUD remains an explicit capability/state bound.
- **Live zen cells.** zen was listening but HTTP and TLS were unresponsive during Cycle 50. It was
  not retried or restarted. Retained two-version evidence remains valid; no new result is inferred
  from a broken instrument.

**Cycle 49.**

- ~~**The legacy dispatcher's PREFIX listing.**~~ **BUILT in Cycle 50 from the complete retained
  executed registry.** A single segment that is a proper prefix of one
  or more registered plug-in routes answers the LAST SEGMENTS of every registered route whose
  path begins with it: `/dev` names `devices`, `/me` names media's three children, `/serve`
  names `server` and `servers`, `/s` names seventeen. Derived from a 38-cell prefix sweep on
  zen and consistent on every one. The old report said 57 paths; the durable command contains
  58, replayed as 174 exact reference GETs. plexsim serves those captured ordered shapes from a
  finite sidecar rather than fitting an algorithm to its own router.
- **`/connections`.** Superseded by the expanded Cycle-50 bound above.
- **`transcoderVideoRemuxOnly`.** zen's alone among four servers -- the 1.42.2 copy omits it
  too -- so it is that server's own state and not a rule.
- **`authToken` on `/myplex/account`.** zen (1.42.2, claimed and mapped) sends it; kids does
  not, and kids' whole plex.tv mapping block is empty as well, so version and mapping state
  are confounded on the only two claimed servers this project has. plexsim's omission is now
  a DELIBERATE deviation rather than a measurement: its token gate defaults OFF, so publishing
  the claim token there would hand it to every peer that can reach the port.
- ~~**PMS's `std::bad_cast` 500.**~~ **BUILT in Cycle 50.** A device row whose `platform` column parses as a number makes
  `GET /devices` and `GET /statistics/bandwidth` a 500 on a real PMS. plexsim now stores the
  normalized numeric JSON value on the direct row and reproduces both list failures until
  a nonnumeric overwrite repairs them. The spelling matrix is local-only; it is never sent to a
  shared reference.
- **The GDM sender's own consumers.** plexsim searches the LAN every five seconds and lists
  what answers on `/servers`, as a real PMS does, but it does nothing else with the peers: it
  cannot browse or proxy another server, and nothing on its surface pretends it can.

**Cycles 46–48.**

- **`includeBandwidths=1` on a deeply analysed Part.** The complete query census proves the
  option moves both references only where PMS has produced deep-analysis bitrate-distribution
  data. An unanalysed zen Part is unchanged and every plexsim Part is unanalysed. The existing
  Cycle-45 bound therefore remains exact; no synthetic bandwidth vector is emitted.
- **Three Plex Desktop runtime cells.** Static Web/native ownership is completely inventoried,
  but the already signed-in live Desktop profile could not be isolated from plex.tv account,
  resource and device traffic without modifying the user's running profile. Actual native mpv
  Range order, Desktop timeline cadence and native decode against plexsim remain explicit safety
  bounds. Web decision, media/subtitle/artwork paths, Range grammar, timeline state and real decode
  are independently covered by the complete web graph and local/live harnesses.
- **Cloud forms absent from the corpus.** None of 309 state pages has `hasNextPage=true`, none
  begins from an empty cursor, and the recorded account has no shared users/subscriptions. Those
  pagination/initialization/multiuser forms are not inferred. The observed single-owner grammar,
  including every credential relationship and rating case, is implemented exactly.
- **The scrobbling transport secret is a deployment input.** PMS embeds a static 32-byte secret;
  copying it into this repository would turn measured proprietary credential material into source.
  `PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64` supplies it at deployment; absent or malformed input
  disables cloud watch/rating sync rather than guessing.
- ~~**Account-scoped feature UUIDs.**~~ **BUILT in Cycle 46 and renewed in Cycle 47.** The outbound
  read census found their exact authenticated `/api/v2/features` source, removing Cycle 45's
  instrument bound. The private `owner_features.txt` remains an optional union input.
- **Proprietary/external outbound classes.** Metadata agents, provider configuration, external
  image bodies, updater, codec/model lookup, geoip and external control-plane discovery remain the
  deliberate architecture/capability boundary. Every one of the 1,620 captured reads has a named
  disposition; none is an unclassified client-protocol gap.

**Cycle 45.**

- **`includeBandwidths=1`.** It adds `deepAnalysisVersion: 6` and a comma-joined eight-value
  `requiredBandwidths` to a **Part**, and `requiredBandwidths` to a Stream — but only on a Part a
  real PMS has **deep-analysed**. The proof that ignoring it is faithful rather than lazy is that
  zen's `/library/metadata/1`, whose Part has no deep analysis, is **byte-identical with and
  without the option**. plexsim runs no analysis, so every one of its Parts is that Part. The
  values are the analysis's own output over the file's bitrate distribution and are not derivable
  from anything plexsim holds; its value grammar is the `+?0*1` family and nothing refuses.
- ~~**The ~180 plex.tv feature UUIDs** inside `ownerFeatures`/`subscriptionFeatures`.~~
  **Superseded by Cycles 46–47:** they are now acquired from `/api/v2/features`, persisted and
  renewed daily; they are still never invented or checked in.
- **An answer to `/library/shared/all?sort=`.** The reference CRASHES on it, so there is nothing to
  copy. plexsim reads the empty value as absent, which is the convention `/collections` already
  has for an empty sort, and the crash is recorded in §9 rather than emulated.
- **A real theme's `Media` on the minted `/theme/{v}/metadata` row.** A row that really has a theme
  at that version answers the theme's `duration` and its `Media`/`Part`/`Stream`; plexsim holds no
  theme audio and no row names one, so it always answers the bare form. Not a deviation — the same
  branch a themeless reference row takes.
- **Storing the minted theme row in `catalog.items`.** A real PMS puts it in its metadata table;
  plexsim keeps it beside the catalog so a `type: "track"` row with no `librarySectionID` cannot
  leak into the tag tables, the spelling index or `/library/all`. The reference's own minted rows
  are invisible in all of those, so the observable answer is the same — listed in §3.3 because
  "invisible in every container I swept" is not "invisible everywhere".

**Cycle 44.**

- **`POST servers.plex.tv/servers.xml`.** A real PMS posts a `MediaContainer` naming its
  `<Server>` and a `<Directory>` per library, 57 times in the recorded capture against three
  device announces; that is how plex.tv learns what a server holds. It is a plex.tv WRITE and out
  of scope under §9. Recorded with its shape, headers and cadence in `docs/findings.md`.
- **`Connection: close` on a part read** stays the recorded ffmpeg-seek deviation: with keep-alive
  ffmpeg reuses the socket behind an undrained body and reports the stream unseekable. Both
  references send `Keep-Alive`/`timeout=20` there.
- **`Part.accessible`/`exists`** stay hard-coded true (inherited from cycles 1-6). The deviation
  now has a client-visible consequence a dead-link era could not show, and an unservable part is a
  plain **404** on a real PMS where plexsim answers 502. Listed in §3.3 as a decision to take.
- **The non-GET verb table of `/myplex/claim`.** GET and HEAD were measured on kids and zen and
  the whole table on plexscratch42; POST is the claim, so only the copies can answer it, and
  plexsim gates on the one verb the references route.
- **A season's own `year` where an agent really gave one.** The load-time correction keys on the
  stored sort title and leaves such a row alone; only the builder's own shape is corrected.

**Cycle 43.**

- **No new deliberate deviation or unresolved behavior was added.** The historical replay retains
  the same 40 external/instrument and 11 safely unreachable atoms with their exact per-atom bounds;
  actionable and unbounded unresolved remain zero.
- **The current-client static boundary is closed for the app build actually served now.** The
  anonymous manifest-rooted Plex Web 4.160.0 graph is complete at 205 JavaScript and 16 CSS assets,
  with 207/207 SRI checks. The 177 JavaScript assets beyond Cycle 40's recovered app subset contain
  no PMS call sites or request builders. The distinct historical gateway variant remains absent at
  176 mapped bodies; that is provenance about an old cache, not a limit on the complete current app
  universe and not a source for invented bytes.
- **The advertised-control graph retains exactly six bounded actions:** two forbidden media reads,
  hidden Optimize and Sync actions, and the external/auth claim-token, claim and unclaim surface.
  All 39 reachable 4.147.1 chains have fresh route/assertion/postcondition witnesses. The complete
  4.160.0 delta adds only the already implemented provider settings lifecycle.

**Cycle 42.**

- **No new deliberate deviation or unresolved behavior was added.** The clean historical replay
  retains the same 40 external/instrument and 11 safely unreachable atoms, each with its existing
  exact bound; actionable and unbounded unresolved are zero.
- **The advertised-control graph retains exactly six bounded actions:** two forbidden media reads,
  hidden Optimize and Sync actions, and the external/auth claim-token, claim and unclaim surface.
  All 39 reachable chains have current route and postcondition witnesses.
- **The 176 absent mapped Plex Web 4.160.0 chunks remain external.** Cycle 42 completely covers the
  locally served 4.147.1 bundle and does not broaden that claim.

**Cycle 41.**

- **The historical census has no unbounded remainder.** Its 40 external/instrument atoms and 11
  safely unreachable atoms are individually named in the authoritative census with the capability
  or state that prevents observation. They are not actionable mismatches on an advertised path.
- **C139's random shelf prerequisite is not makeable with the available pair.** The 1.43.4 copy has
  matched rated rows but omits the shelf; the target 1.42.2 copy exposes the shelf but has no matched
  provider row. No reversible public write mints that provider metadata, so no rank was invented.
- **The reference notification backlog's byte threshold is a host TCP-buffer property.** Dead- and
  slow-reader tests on both copies confirm nonblocking producers and correct filters/pings; plexsim
  keeps its measured drop behavior but does not hard-code either host's buffer size.
- **`[Unknown Season]` scanner creation remains state-bounded.** Specials/season zero is measured
  and built. Three isolated filename forms failed to create an index-less season, so the existing
  database witness is retained without guessing an ingestion grammar.
- **The advertised-control census has six bounded actions, not gaps:** two forbidden media-body
  reads, hidden Optimize and Sync actions because plexsim advertises no encoder/sync capability,
  and the external/auth claim-token, claim and unclaim surface. The other 39 action/readback pairs
  have exact route and postcondition assertions.
- **The complete-current-client static universe is still unavailable.** The local 4.147.1 bundle is
  complete; 176 mapped 4.160.0 chunks remain absent from the retained cache.

**Cycle 40.**

- ~~**The legacy `/system/services/url/lookup` traceback.**~~ **BUILT in cycle 40** as the exact
  464-byte 1.42.2 plug-in XML failure, including GET gzip, HEAD length, Accept indifference and
  connection close. A valid `url=` lookup remains deliberately unprobed because it is an external
  fetch/SSRF-shaped request.
- ~~**Metadata-agent group writes.**~~ **BUILT in cycle 40** because the recovered Plex Web 4.160.0
  provider UI exposes Add Agent after its two table reads. Create, rename, incompatible membership
  refusal, absent membership deletion and group deletion all have measured postconditions.
- **The recovered Plex Web 4.160.0 static universe is incomplete.** Only 29 of the gw manifest's
  205 JavaScript assets survive in the existing cache; 176 mapped chunks are absent. Every surviving
  call site is classified, but no clean-cycle claim may call that a complete current-client census.

**Cycle 39.**

- **The official Plex Web 4.160.0 walk had no finding.** Its 20 unique safe request signatures are a
  strict subset of cycle 27's 95, and exact replay found no same-version behavioral difference.
- ~~**Hub matcher case handling.**~~ **BUILT in cycle 39** as separate raw-query partial-tier and
  broader whole-title passes, from a complete 659-cell sweep with a zero-miss holdout.
- ~~**Reference-only technical tag ranks.**~~ **BUILT in cycle 39** from every generic row source
  plus the complete measured 28-string static remainder. No reference library token dump is needed.
- **Historically stale external spellfix state remains deliberately external.** A reference can
  retain old tokens until database optimization. plexsim reproduces that schedule for its own
  catalog; it does not copy another server's historical database state.

**Cycle 38.**

- ~~**Spellfix rebuild timing**~~ -- **BUILT in cycle 38** at the three database-optimization
  entry points plexsim owns. The reference's separate automatic Butler timer is documented rather
  than invented as a new in-memory scheduler.
- ~~**Final equal-score API ordering**~~ -- **BUILT in cycle 38** from both binaries and 331
  reconstructed hub inputs; the exact unstable sort has zero misses in that scope. Full permutation
  validation of capped/tag-associated responses still needs the pre-cap input vector the wire omits.
- **Persistent-storage failures.** SQLite, `Preferences.xml`, metadata bundles, cache and encoder
  failures were measured on isolated reference copies. plexsim has none of those backends, so no
  synthetic disk-failure switch or guessed cross-backend status mapping was added.
- ~~**Reference-only technical tag ranks.**~~ **BUILT in cycle 39** from the full generic row-source
  inventory and the measured 28-string static remainder.

**Cycle 37.**

- ~~**Spellfix rebuild timing.**~~ **BUILT in cycle 38** after the callers, optimization triggers
  and automatic Butler eligibility were recovered from both binaries.
- ~~**Reference-only technical tag ranks.**~~ **BUILT in cycle 39** without a library-specific
  sidecar: the technical strings live on generic row shapes, and only 28 server-static strings do not.
- ~~**Final equal-score API ordering.**~~ **BUILT in cycle 38** as the reference's unrounded-score
  unstable hub sort and no-op flat wrapper sort.
- **Historically stale external spellfix state.** The current reference table cannot always be
  reconstructed from current rows. plexsim reproduces the algorithm for its own catalog; it does
  not attempt to reproduce a reference server's old snapshot.

**Cycle 36.**

- ~~**The search fuzzy BAND and prune**~~ -- **BUILT in cycle 37** after reading both live tables,
  both binaries and the spellfix source; 822/822 fresh held-out row cells.
- ~~**`partIndex` on a multi-part Media**~~ -- **BUILT in cycle 37** with a measured `- cdN`
  catalog row and per-Part debrid links.
- ~~**A Dolby Vision profile-5 source's wire refusal on a row plexsim does not hold**~~ -- the
  focused cycle-37 fixture now reaches and locks the already-correct 2003 path.
- **`/match` on a SHOW**: both copies open with a bare `state 3` carrying the OLD title and then run
  that show's refresh cascade, whose DEPTH is the version's. plexsim's seven-frame show form is
  1.42.2's shape and the version it reports, so what is left is a frame count on a version it does
  not claim.
- **The show cascade's frame ORDER on 1.43.4** -- the activity `started` after the queued frame, the
  per-season `updated` at the season's END, no invented `updated 49`, the media pass only on the
  matched season and only once. Single-witness by construction: no matched show can be written on a
  1.42.2 copy.
- **A 1.43.4 library DELETE's fourteen extra `state 9` frames** on a show library, where 1.42.2
  pushes the eight activity frames alone. plexsim reports 1.42.2 and matches it.
- **`video.frameRateMode` is still untripped** on a VFR row made for it: the analysis calls a
  jittered `-vsync vfr` encode `cfr`, and `Sony Bravia 2010` -- the only profile naming it --
  direct-plays the row.
- **A NUL in a filter VALUE**, 40 cells on both references: it refuses on the operators that build a
  substring comparison and not on the rest, keyed on an interaction of column kind and operator that
  no model explains. No Plex client can put one in a query.

**Cycle 35.**

- ~~**The hub matcher's fuzzy BAND**~~ -- **BUILT in cycle 37.** The apparent position/length
  continuum was spellfix membership followed by the separately measured four-tier API score.
- **`video.separateFields` read as a constant 0** in `decision.py`, where every neighbouring value
  comes off the stream, and `mediainfo` never derives it. zen holds exactly one interlaced h264
  Stream carrying it, on a stale Media that answers `2012 File is missing` to all 78 profiles, so the
  refusal cannot be witnessed and no check can be proven fail-then-pass. Recorded with the citation.
- **A dual-layer Dolby Vision file is ONE video Stream on zen** (`DOVIELPresent: true`) where the
  recorded note, measured on fun and out of scope, says the enhancement layer is a second stream. One
  witness each side.
- **`video.frameRateMode` remains untripped.** ~~A multi-part Media was unfound~~ until cycle 36 made
  the stacked row; cycle 37 made it reachable from plexsim's own catalog.

**Cycle 34.**

- **What the reference picks before a client has chosen an audio track.** Both states of the made
  row are in `tests/fixtures/decisions/cells.jsonl` and the suite counts the 156 pre-selection cells
  without failing on them. plexsim's rows carry a `selected` stream and nothing else, so the
  distinction cannot be expressed from a catalog row -- and the rule itself is not settled: the 41
  profiles that refuse the row as "not the first audio stream" are every profile with a matching
  `<VideoProfile>` except Android, Popcorn Hour and the Roku 5.x/6.x/7.x family, and nothing in those
  files separates them. Asus Transformer Prime TF201 and Roku-7.x list the same audio codecs in mp4,
  agree on every other measured row, and disagree here.
- **The `Sec-WebSocket-Extensions` the client offers** (`permessage-deflate`): neither side echoes
  it in the capture, so there is nothing to build; recorded with the handshake order.

**Cycle 33.**

- **`/system/search?query=`'s header order**, **the two socket handshakes' header order**, and
  **whether an isolated 1.42.2 server's refresh really stops** -- each in §3.3 with its reason.
- **The hunt's version framing of the refresh truncation, REFUTED**: the same three-frame opening
  appears on 1.43.4, and the 1.42.2 copy cascades happily for a `tv.plex.agents.none` library. What
  was built is the part that survives both controls, the never-matched row's opening pair.

**Cycle 32.**

- **The delete window.** Two concurrent deletes of one playlist or collection are both successes on
  the references (20/20 and 17/20 for a playlist) and a success and a 404 on plexsim; a read racing
  the delete can see `{"size":0,"totalSize":3}` there. The difference is the WIDTH of a 2-5 ms
  window, and matching it means inventing a grace period in which a deleted row answers success.
- **"Conversion OK".** The decision model predicts it exactly and plexsim still answers "neither",
  because it has no encoder -- the recorded deviation, now made per item rather than for every
  non-listed profile.
- **The refresh coalescing difference, zen 19813's audio selection, and the hub matcher's case
  passes.** §3.3 has each and why it is not built.
- **`/activities` mid-write.** A real PMS can be caught with a running activity; plexsim's writes
  finish in well under a millisecond, so there is nothing to catch. Not a rule difference.
- **s42's unmatched-movie refresh pushing `state 5` before `state 3/queued`.** One server, unmatched
  rows only, outside the territory that saw it; noted, not chased.

**Cycle 31.**

- **The `title=` query FOLD table.** Measured to be a per-character table rather than NFD and swept
  for three letters only (11 characters fold to `i`, four to `s`, two to `l`). plexsim still folds by
  NFD, which over-folds; a partial table would break the accented rows that work, so the sweep script
  is recorded and nothing changed. §3.3 has the method.
- **An episode with no still carrying the SEASON's poster path.** Reported with 21 live witnesses and
  427 trashed ones; re-measured over 1,500 live zen episodes as **0**. Recorded, not built.
- **`/livetv`'s 403.** Both copies answer 403 to every verb today where cycle 18 recorded a three-row
  container from the same copy. plexsim keeps the container until the contradiction is settled.
- **The decision model from the profile files.** Its four inputs are now named and 45 of 78 profiles
  declare direct play where plexsim hard-codes two names. Hunt-sized; §3.3 has it.


**Cycle 30.**

- **The row-CREATION frame sequence.** Eight frames per new row on the copy -- `state 0` with an
  empty title, five `state 1`s, `state 4 loading`, `state 5` -- plus one
  `library.update.item.metadata` activity per new row. plexsim's catalog is loaded at start and it
  has no scanner that adds rows, so the sequence is unreachable. It is recorded because it
  corrects two documents: `state 1` was missing from the state alphabet, and cycle 19's "no scan
  does a metadata activity per row" is true only of a scan that finds nothing new.
- **The reference's notification BACKLOG bound.** It buffers ~415 KB at a dead consumer and then
  stops queueing, never slowing a request. plexsim drops the channel after a two-second write
  budget instead: it drops where the reference buffers. The measured half -- that a stalled
  consumer must not reach the request path -- is built and proved (20.00 s -> 2.08 s over 260
  writes); the buffering half is not.
- **The `Analyzing Media` per-leaf progress of a three-season show's unmatch.** Measured on
  plexscratch42 and NOT built: the show was already unmatched and the server is 1.42.2, where
  cycle 25 measured a matched show on 1.43.4, so the two are not comparable. See §3.3.
- **plexsim's own ~256 s eventsource close** -- **which cycle 31 refuted: re-measured with a probe
  that imposes no read timeout, the channel is open at 460.2 s with 46 pings at a steady 10 s.**
  The 300-second constant is gone and nothing replaced it; the 256 s belonged to the old capture's
  client. The fix still carries **no suite check**, deliberately: a check cheap enough for the
  suite (35 s, three pings) cannot fail against a re-introduced 300-second deadline, and one that
  can costs five minutes of suite time. The proof is the hand capture, now kept as
  `c31/D/sse_life.py` and its log rather than in a transcript.


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
`guid==`/`guid!=`; ~~`/system/services/url/lookup`'s plug-in traceback~~ (built in cycle 40); mixed folder levels; fun's
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

**Cycle 29:** the photo transcoder's GEOMETRY, now pinned and still unbuildable -- the default
scales to FIT inside the box, `minSize=1` scales to COVER it uncropped, without `upscale=1` the
result is capped at the source's own size, `quality=` is the real JPEG quality and `opacity=` is
accepted and discarded -- because plexsim has no resizer (the `width`/`height` 500s were already
held out in cycle 27); a section `/composite/{v}` being a **512x512 JPEG mosaic** on both
references, where plexsim has no compositor and serves one member's own poster; the transcoder
omitting `Accept-Ranges` on the FIRST (uncached) transcode and sending it on every later one, a
cache tell plexsim has no cache to key on; the 416's own **129-byte HTML body** and the fact that
the static writer's refusals name no connection state and CLOSE the socket (plexsim's text/plain
error bodies are cycle 1's deliberate deviation, and keeping a connection the reference drops is
the forgiving direction); zen's plain `filename=` substituting `_` for `+` where `filename*`
escapes it, on one witness; the malformed-`Range` table (38 specs both references agree on, of
which the three status rules ARE built and the rest fits no parser model -- `bytes=-0` answers a
206 whose `Content-Range` says `0-0/N` and whose body is the WHOLE file, which is the reference's
own lying header); the BIF index family (`/library/sections/{id}/indexes/thumbnails` and friends
are 404s on both references because neither has generated an index, so the populated shape cannot
be seen); and **the whole `clip` surface** -- the row, `primaryExtraKey`, the three projections and
the section extras query's own rows -- whose build spec is measured in full in `docs/findings.md`
and whose build waits on a catalog with extras in it.

**Cycle 28:** the `/library/search` flat list's ORDER, which is the reference's own relevance
engine -- for `comedy` zen answers movie, show, episode and THEN the two genre rows, which is
neither the hub base order (`show` first) nor a global score sort (the genre rows score 0.92 and
come last), while for `c26act` plexscratch answers the collection rows before the playlist rows at
an identical 0.52; in the same family, a fuzzy person row scoring 0.32 is DROPPED from the bare
list and kept when `searchTypes=people` asks for it alone (kids and the copy, `sintel` ->
Katherine Kendall). Both need Plex's own scorer, so plexsim keeps its hub order and the three-band
score model cycle 15 recorded. Held out because the references DISAGREE, which is the same test
applied everywhere else: hunt A's Related-shelf composition, the `tv.moreingenre` slot included --
kids and zen do not carry the same shelves on a preplay page. Held out deliberately: **1.43.4's On
Deck projection**, which drops `librarySectionKey`, `Guid`, `Rating` and `Producer` and stamps
`studio`, because plexsim reports 1.42.2 and follows zen (§3.2, §3.3); and
`searchTypes=music`/`photos`, which name libraries plexsim cannot hold.

**Cycle 27:** the `width`/`height` 500s of `/photo/:/transcode` (they depend on the image's
own aspect ratio and on a resizer plexsim does not have, where the five other parameters'
validation IS built); the `format=` parameter of the same endpoint (`format=png` answers
`image/png` and an unknown one `application/octet-stream` on both references -- plexsim has no
image encoder, and labelling upstream JPEG bytes `image/png` would be a lie a browser acts on);
a lower-case request method HOLDING the connection to the reference's own 20-second idle close
(plexsim closes it at once -- the same silence, sooner, because a twenty-second thread per
malformed line is a self-inflicted denial of service); conditional request headers
(`If-None-Match`, `If-Modified-Since`, `If-Range`, `If-Match`), which a real PMS IGNORES on
every path including a part read that carries an `ETag` -- measured and recorded so the
dimension is not re-swept; `Part.accessible`/`exists` being gated on `checkFiles=1`, re-confirmed
and left as the cycles-1-6 deliberate deviation; and the write hunt's four single-witness
observations on the copy (`playQueueItemID` leading a play-queue row, `playlistItemID` mid-row,
and the transcode decision row's string-typed ids and one-element `UltraBlurColors`).

**Cycle 26:** which butler tasks are DUE (the counts are measured, the mechanism is invisible
from the surface -- no `GET /butler` row carries a last-run or next-run time -- so plexsim runs
every enabled task); the butler's per-task COUNTS (unchanged from cycle 17: content, not rule);
a valid transcode START once a decision exists (plexsim has no encoder, and the reference's own
answer there depends on what it can convert -- its `start.mpd` is a 404 where the same
session's `start.m3u8` is a 200); the reaper TICK behind the session cutoff (plexsim prunes
lazily on read, so its row goes at the cutoff); and the session row's `Role` attribute order
(alphabetical AFTER `filter`, which the plain rule does not explain and one element cannot
settle).

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

**A GET wrote to both references and broke two read families (found cycle 49).** `GET /devices`
and `GET /statistics/bandwidth` answer **500** on kids and on zen. The cause is PMS's own
serialiser: a device row whose `platform` column parses as a NUMBER raises `std::bad_cast`
(`ERROR - Got exception from request handler: std::bad_cast` in its log) when the row is
listed, while `GET /devices/{that id}` stays a clean 200. The row is written by an ordinary
**GET** carrying `X-Plex-Device-Name` and `X-Plex-Platform` with **no**
`X-Plex-Client-Identifier` — it lands on the anonymous device row, id 1 — and `0 1 2 3 5 9 10
11 12 20 100 007 +1 -0 1.0 " 1" "1 " 01 0000 999999999 2147483647 2147483648 1e3` all do it,
storing the NORMALISED number. Reproduced and repaired end to end on both writable copies.

kids' row 1 was `name="LG TV" platform="iOS"` in the byte copy taken 2026-09-13 and was
`0`/`0` when cycle 49 looked, so the write happened inside this programme's lifetime. A
uniform-zero header sweep — sending `X-Plex-Platform: 0` to every path — is exactly the shape
that does it, and this project has run one.

**kids has been repaired** to its exact original, with one GET carrying those two headers;
`/devices` and `/statistics/bandwidth` are 200 again. **zen has NOT been touched.** It is
production, it takes GET and HEAD only, and its original device-row values predate the copy
and are unknown, so any repair there would invent a string. That is the user's call, not this
programme's: the repair is `GET /identity` with `X-Plex-Device-Name: <the original name>` and
`X-Plex-Platform: <the original platform>` and no client identifier.

**The lesson for every future sweep: a GET can write.** A header census that sends a uniform
value for every header name is not a read-only sweep. Put device-identity headers
(`X-Plex-Device-Name`, `X-Plex-Platform`, `X-Plex-Device`, `X-Plex-Client-Identifier`) on the
sweep's own exclusion list for the shared references, or send them only to a disposable copy.

**A misquoted Cycle-50 `/connections` wrapper touched the shared lab.** The first remote
multiline command was not passed as one quoted argument, so exactly **six tokenless
`GET /connections` requests and two blank TCP opens** ran against kids host-loopback instead of
inside the disposable container. The blank sockets sent zero bytes and were closed immediately.
There was no token, device identity, media read, or write. The contaminated observations were
discarded, all shared-reference connection work stopped, and the complete replacement matrix ran
only through `docker exec` to each disposable PMS's own loopback on an `internal: true` network.
Quote a remote program as one argument and prove the container/network target before opening the
first socket.

**Cycle 49 also put a transient row into zen's `/servers`.** Starting plexsim with `--gdm on`
on the same LAN made zen discover it, exactly as any Plex server on that wire would; the row
aged out on its own and nothing on disk changed. It is recorded because it is a state change
to production that a reader of this file should expect to see in the logs.

**`GET /library/shared/all?sort=` crashes a real PMS (cycle 45).** An EMPTY sort value on that
path dropped the connection on kids and took zen's Plex down outright — `systemd: Main PID …
(code=killed, signal=SEGV)`, the service stuck in `deactivating` behind a surviving plug-in child
until systemd's stop timeout, then restarted clean. Both references went down inside one probe
batch of ten query values; the culprit was isolated afterwards by sending the other nine to kids
alone, one at a time with a health check between, and `?sort=` was the first and only one to
reproduce. zen was verified healthy afterwards: three sections, 3,887 movies, 507 shows, no
sessions, identical to before. **Never send that request to zen.** It is not a plexsim finding —
plexsim answers the default order — but zen is production on a build that crashes on it.

**A token printed to the terminal a second time (cycle 45).** Dumping `/myplex/account` from a
reference prints its `authToken`; zen's went to the terminal once. It was not written to disk,
a fixture or a commit, and `handover/tools/…/req.py` now redacts `authToken` and `X-Plex-Token`
on the way OUT of the request helper so no probe can print one by accident. Cycle 45 had already
had one leak, through `capture/outbound.jsonl`'s query pairs. **Redact in the instrument, not in
the probe** — every probe is written fresh and every one of them will forget.

**zen follows a `file://` artwork candidate (cycle 29, not acted on).** Cycle 29's body hunt sent
`GET /library/metadata/{id}/file?url=file%3A%2F%2F%2Fetc%2Fhostname` to both references as part of
a fifteen-shape `url=` table. kids (1.43.4) answers 400. **zen (1.42.2) answered 200 `image/jpeg`
with the file's four bytes** -- its artwork-candidate reader follows the scheme and serves an
arbitrary local file to any request carrying a token. The hunt stopped at that single request,
probed nothing further and read nothing else off the filesystem, which is the right instinct and is
recorded here so the next sweep does not repeat it: **do not probe that path on zen again.** It is
not a plexsim finding -- the two references disagree, and plexsim 404s a `file://` url -- but zen is
production on a build that still does it, and the operator should know.

The rules:

- **zen and fun are production: GET/HEAD only, always.** fun is out of scope entirely.
- **kids is read-only for hunting agents since cycle 7.** Every write needs its undo listed
  first, and a snapshot before and after. Never delete a section or an item; never merge, split,
  match or unmatch; never run maintenance endpoints; never change network or auth prefs; **never
  write server, library or item settings**.
- **Several PMS GETs WRITE.** Guard every request to a reference with `probe.py`'s `WRITE_GET`
  regex. Cycle 45 adds `/library/metadata/{id}/theme/{v}/metadata`, which MINTS a metadata row:
  measure it on a disposable copy only.
- **Two GETs take a reference DOWN.** `GET /library/shared/all?sort=` (an empty sort) SEGVs a real
  PMS -- never send it to zen. And an UNPAGED full-library sweep has crashed zen twice: always
  send `X-Plex-Container-Start`/`-Size`, and put the default in the request helper rather than in
  each probe.
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
| Cycle 50 | A misquoted `/connections` wrapper sent six tokenless GETs and opened two blank sockets on kids host-loopback | No write or identity; sockets closed, evidence discarded, all later work confined to isolated container loopback; both copies reset/recreated and re-proved isolated |

Local processes **not** belonging to this work, left alone on purpose (re-checked at the end of
cycle 15): `32400` is another session's plexsim; `8787` another session's `http.server`; `32502`
a plexsim on `catalog.example.json`; `32595`/`32596` plexsims of unverified origin. `32511` and
`32512` were here and their processes are gone.

---

**Cycle 55: a search `limit` crashed kids.** `/library/search?query=the&limit=4294967296`,
sent by the implementation agent while settling the limit grammar, dropped kids' PMS; the
process restarted within about five seconds, `/identity` came back `claimed="1"` and the
refcheck snapshot was byte-identical. Treat a search `limit` past int32 as crash-shaped on a
reference, like cycle 45's empty `sort=` on `/library/shared`.

## 10. Tooling and environment gotchas

**Cycle 45 added these.**

- **The session scratchpad can be removed between two tool calls.** The disk filled during cycle
  45 and the whole `scratchpad/c45/` tree went — the worktree, the patch file, the sweep outputs
  and `tokens.env`. **Every commit survived**, because a git worktree keeps its objects and its
  branch ref in the main repository's `.git`. After any tool failure that looks like the
  environment (`ENOSPC`, a missing scratch path), check `git branch` and `git log <cycle-branch>`
  in the main repo before assuming anything is lost; recover with `git worktree prune && git
  worktree add <scratch>/wt<N> cycle<N>`, then re-link `webclient/` and `catalog.json` — both
  gitignored, so a fresh worktree carries neither and every `/web/*` 404s without the symlink.
  Keep nothing in the scratchpad that is not either committed or re-derivable.
- **When the disk is full, every tool that writes fails, including `Write` and `Edit`.** They
  write a temp file first. There is no way to free space from inside the session; say so and stop.
- **`nohup … &` inside a backgrounded tool call does not survive it either.** CLAUDE.md records
  this for a plain tool call; it is also true of one run with `run_in_background`, and `setsid`
  does not save it. Start the server and run the suite **in the same command**, so the server
  lives exactly as long as the suite does.
- **A probe that sends a raw space in a query string measures the request LINE, not the
  parameter.** `?type= ` becomes `GET /path?type=  HTTP/1.1` — two spaces — which is a malformed
  request line, and the connection dies with no response. Percent-encode every probe value.
- **An EMPTY answer on plexscratch42 is not a negative.** It is a fresh install with nothing
  matched, so it holds no agent artwork, no real metadata and no counts. CLAUDE.md already said
  "use it for grammar, never for a count"; cycle 45's `includeAlternates` claim shows the same
  trap from the other side — a version difference inferred from a copy that had nothing to show.
- **`sdrive.py` runs the UI suite and nothing else.** A patch can pass its own fail-then-pass proof
  and the whole 1,595-check UI surface and still break `test_catalog_features.py`, which is where
  the clip and Dolby Vision fixtures live. Run the full suite set on the cycle's head before
  merging, not only at the end of each patch.
- **`test_navigation.py` is written against the LIVE catalog, not `catalog_ui.json`.** Pointed at
  the UI fixture it reports 12 failures that are the wrong show. Start its server with no
  `--catalog`.
- **`test_all.py`'s two `public` checks need the Cloudflare tunnel.** Without it every row reports
  `FAIL public prefix HTTP 502` and the summary says `0/5 served correctly` while every local byte
  read, magic check, seek and decode passes. Compare against `main` before treating it as a
  regression.

**Cycle 44 added these.**

- **`git archive` does not carry a gitignored directory, and `webclient/` is gitignored.** A
  frozen tree therefore 404s every `/web/*` path that both references answer 200. Two hunters were
  pointed at such a tree this cycle; the artifact was caught and announced before either reported
  it. Start a frozen server with
  `--web-root /Users/bensarmiento/debridmediamanager/plexsim/webclient`, or say in the brief that
  `/web/*` is out of bounds on it.
- **A probe that sends `Connection: close` cannot measure a connection state.** Cycle 33 and cycle
  36 both recorded "a 416 names no connection state at all" from a probe that had asked for the
  socket to close. Re-measured with a KEEP-ALIVE request the container 416 names it and the binary
  416 does not. **When the question is what the SERVER chose, do not tell it what you want.**
- **`decision.PROFILES[name]` is not `decision.resolve(name)`.** 23 of the 78 profile files carry
  a `redirect=`, so reading the dict directly scores a profile that declares nothing. Nine of this
  cycle's first "misses" -- `Roku`, `Roku-6.x` and the iOS family -- were exactly that, and they
  looked like real findings until the scorer was corrected.
- **A suite that never compares a field cannot fail on it.** `test_mediainfo.py` compared every
  stream attribute, the Media's geometry and thirty derived rules, and never the `container`. The
  corpus had held the refutation of plexsim's webm rule since cycle 10. When a rule is about a
  field, check that some suite actually reads that field.
- **The writable copies' token files go stale.** `/home/ben/plexscratch42/token.txt` was wrong this
  cycle and `scratch42.sh` does not exist; the working token is
  `docker exec plexscratch42-plex cat '/config/Library/Application Support/Plex Media Server/.LocalAdminToken'`,
  and the same for `plexscratch`.
- **A check whose fixture row is unrealistic will fight a correct fix.** `tests/catalog_ui.json`
  holds seasons carrying BOTH `year` and `parentYear`, a shape no real PMS emits, and a UI check
  asserted the year half of it. The load-time correction had to be keyed on something both
  references witness -- the STORED sort title -- rather than widened until the fixture passed.

**Cycle 43 added these.**

- **A previous cycle's cleanup report is evidence about that instant, not the next cycle's starting
  state.** Cycle 42 recorded every listener stopped, but Cycle 43 startup still found Cycle 41's
  frozen-tree processes listening on 32587 and 32588. Before allocating any new port, list every
  `server.py --port` process, resolve its cwd and command line, and kill only a process whose
  ownership is proved. Repeat the same global inventory after the agents finish; checking only the
  ports named by the current cycle misses exactly this failure.
- **Two official-client builds can share the same version/build label and still have different
  webpack bodies.** Plex Web 4.160.0 build `75ddd7b` from the historical gateway and the current
  public app intersect on 202 mapped chunk ids but differ in hashes and one id on each side. Close a
  static denominator from one manifest-rooted build; never fill a missing historical chunk with a
  same-named current body. Cycle 43's complete-current claim is for the public app graph whose first
  28 cached bodies match byte-for-byte.

**Cycle 35 added these.**

- **A sweep that compares DECODED bodies cannot see the encoding.** Cycle 30 asked "is a response a
  pure function of its request?" and answered yes; cycle 35 asked the same question of the BYTES on
  the wire and found the gzip header carrying a clock. When an axis is about the wire, compare the
  wire.
- **The suite's `eventsource()` helper hands back the data line as a STRING**, not a parsed row --
  `json.loads` it. A check that treats it as a dict silently collects nothing and reports an empty
  sequence, which reads exactly like a server that pushes no frames.
- **A `%` in a patch BLOCK is literal.** The blocks are written verbatim into the suite, so `%%20`
  in a URL stays `%%20` and the request goes out malformed. The escaping rule is the opposite of the
  one that bit cycle 32's generator, where a backslash had to be doubled.

**Cycle 34 added these.**

- **A rate limit can outlast a cycle.** Cycle 33 lost both hunts to an account-level WEEKLY limit
  and cycle 34 lost both again, on every model tried. Two cycles in a row have therefore been the
  driver's own territories, and both delivered -- but plan for it: the driver's list should always
  hold a territory that needs no agent, and the write-up should say a cycle had no hunts rather than
  reading as a thin one.
- **An answer that is a pure function of the request is an ASSUMPTION, and here it is false.** The
  decision endpoint reads a per-user audio selection that no read of the item exposes: the same GET,
  the same row, the same bytes back from `/library/metadata/{id}`, and 41 of 78 profiles answer
  differently depending on whether a client has ever written `PUT /library/parts/{id}?audioStreamID=`.
  Cycle 30 swept "the same request twice" and could not see this, because the state does not change
  on its own. **Record which state a corpus was taken in**; `handover/tools/ddrive.py` says so in
  its docstring for that reason.
- **A check proven with the driver is proven for ONE BLOCK.** `sdrive.py` runs the patch's own block
  against the parent and the fixed tree, which is the fail-then-pass proof -- and says nothing about
  what the block leaves behind for the 1,500 checks after it. Cycle 34's first commit left a
  lower-case summary on the fixture's first movie and starved a cycle-20 check three thousand lines
  down. **Run the whole suite before the cycle's documentation commit**, and put back what a check
  writes when the value itself is what a later check counts.
- **Do not run two copies of `test_ui_surface.py` at once.** Its own gate checks bind **32599**, so a
  second run racing the first produces failures in both that look like real findings -- three of them
  in this cycle, all of which evaporated on a sequential re-run.

**Cycle 33 added these.**

- **A hunt can die on an account-level WEEKLY rate limit, not just a session one.** Both of cycle
  33's hunters stopped inside a minute with `429 … weekly limit · resets <date>` for the two models
  this programme has always used. A one-line probe agent on another model shows in seconds whether
  anything is available; relaunching there costs a smaller hunt, not a lost cycle. Say in the write-up
  which model a cycle's hunts ran on -- it is part of reading the finding count.
- **Read the response header block off a RAW SOCKET.** `urllib` and `http.client` normalise header
  case and hide order, which is exactly what a byte-form question is about -- and drain each response
  by its `Content-Length` before reading the next (cycle 30's rule, and it applies here too).
- **A code path that runs before the request line is parsed has no `self.path`.** Anything hooked into
  `end_headers`, `send_response` or `log_request` must use `getattr(self, "path", "")`, or every
  malformed-line refusal answers nothing at all. The suite's raw-socket checks are the only thing that
  can see it.
- **When a hunt reports a VERSION difference, test both halves on both servers.** Cycle 33's hunt
  compared a `local://` row on one copy with `tv.plex.agents.none://` rows on the other and read the
  difference as 1.42.2 versus 1.43.4. The control -- the same guid state on the other server -- takes
  two minutes and dissolved it.
- **The 1.42.2 copy's ONLINE agent can never do any work.** It is a fresh install with no metadata
  bundles and no route to plex.tv, so any sequence that depends on the agent finishing is not
  comparable with the 1.43.4 copy, whose rows carry bundles from the lab. Use a
  `tv.plex.agents.none` library on both when the frames must be compared.

**Cycle 32 added these.**

- **A race needs a barrier, not two calls in a row.** Release both requests from one
  `threading.Barrier` and repeat the pair tens of times: the play-queue window was under a
  millisecond, 4 of 30 at a 0 ms offset and 0 of 30 at 1 ms. Put 150 pairs in a suite check -- the
  unfixed server collides at about one pair in twenty -- and read the RESPONSES, not the final state,
  because the 28-pair mutation matrix's final states agreed while the answers did not.
- **A fix that claims to close a sibling defect has to re-run the sibling on its own tree.** Cycle
  32's first race fix said it also fixed concurrent adds; the add probe against that tree still
  collided 9 times in 180.
- **Make each half of a check DISCRIMINATE.** A check proved fail-then-pass can still carry a half
  that both trees answer the same way: `Egghead` against a stored `Éggęhead` is false under NFD and
  under the table alike (NFD makes it `Eggehead`). Pick the query the two rules disagree on
  (`Eggęhead`) and re-prove.
- **Taking over another session's cycle.** Its branch and worktree are in the object store and its
  scratchpad; its hunt reports are the last assistant message of each
  `~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl`, and its own progress is the
  assistant text in `<session>.jsonl`. Check whether that transcript is still being written before
  touching anything, ask the user, then `git worktree remove` (a clean worktree loses nothing),
  re-add it under your scratchpad, rebase on `main`, and push the branch.
- **zsh ties a variable named `path` to `$PATH`.** A loop `for path in …` in a Bash tool call empties
  the command search path mid-loop, and every command after it is "not found". Name it anything else,
  or write the loop in Python.
- **The Bash tool's wait tops out at ten minutes.** A full UI suite is ~14: run it in the background,
  and wait for the completion notification or poll for the `N/M checks passed` line.
- **A module-level search in a scratch script runs again on import.** Guard it with
  `if __name__ == "__main__"`, or an import from a second script silently spends minutes redoing it.

**Cycle 31 added these.**

- **A COUNT is a measurement and deserves the same re-measurement rule as everything else.** Cycle
  31's first census of the profile files grepped `<DirectPlayProfile ` where the children are
  `<VideoProfile>` inside `<DirectPlayProfiles>`, and reported 2 where the answer is 45 -- with a
  tidy, plausible story attached ("four profiles declare nothing, 2-for-2 with the neither triple")
  that survived until it was re-run. Re-run every count that carries an argument.
- **Read the reference's own FILES and DATABASE when the API cannot express the question** (§4.1).
  `ssh ben@kids docker exec plexlab-plex …` is read-only inspection; copy a database out with
  `docker cp` and open it `file:…?mode=ro` rather than opening it in place. It settled in twenty
  minutes what six cycles could only infer -- and it is the only way to see a column the API does not
  publish, which is what "nothing on the row tells the two apart" always means.
- **A version-specific REFUSAL can take a capability the suite depends on.** Making the artwork
  DELETE a 404 (1.42.2's answer) removes the only route to a cleared-artwork state that eight
  cycle-27/29 derivation checks need. Before implementing such a refusal, find the version's OWN way
  to reach the state -- here an empty `thumb.value=`, which both versions answer identically -- and
  re-express the checks through it. Answering "it cannot be tested any more" is not an option.


**Cycle 30 added these.**

- **A probe that reuses a socket must DRAIN each response.** A bare `recv()` returns whatever the
  kernel coalesced, so a response whose headers and body land in two segments leaves the body
  unread -- and the next `recv` then returns that body where the second response's status line
  should be. The probe reports a LIVE socket as closed, deterministically per body size, so
  retrying cannot paper over it. Read the `Content-Length` and drain exactly that many bytes.
  Cycle 30's connection checks passed on a small body and failed on a large one for this reason,
  which cost a suite run to find.
- **A sample pasted into a document is a measurement only if it came off the reference.** §3.1's
  new class G: the 401 printed in `docs/findings.md` since cycle 16 was plexsim's own answer, and
  once it is in the document it reads like a capture for ever. Capture to a file, then quote the
  file.
- **Never open a websocket or an eventsource on kids or zen.** It was already the rule; cycle 30
  is the cycle that needed it, because its hunt could only run against the copy. A socket is a
  write to a shared server's resources even when every frame it carries is read-only.
- **A DoS in this server looks like a passing test.** The ping storm (903,480 pings in 35 s) is
  invisible to every probe that does not PONG, and a probe written by hand never pongs; the write
  hang is invisible unless a consumer stops reading while writes continue. When you emulate a
  channel, drive it with a client that behaves like a real one -- and then with one that behaves
  badly.
- **A patch whose `return` comes before its own tail ships nothing.** Cycle 30's collection-search
  `guid` sat under an unconditional `return` and was dead code; the suite caught it, one cycle
  later than the patch. `python3 -W error -m py_compile` will not: unreachable code is legal.


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
- **Never run two suites at once, and never start the next one until the last has printed its
  summary line.** Cycle 27 killed a suite's server, started a new one on the same port and
  launched a second run -- and the FIRST run's process, which had not exited, carried on driving
  the new server. It reached the library-DELETE block and took the show library out from under
  the second run, which came back with 31 failures and a crash in an unrelated check. Nothing
  was wrong with the tree: the same commit scored 1421/1421 when run alone. Two runs also fight
  over 32598 and 32599, which the suite binds itself. Wait for `N/M checks passed`, then check
  the process is gone, then start the next.
- **Never probe the server a suite is currently running against.** Writing one title to the
  fixture mid-run cost a whole suite pass and read as a collapse — 889/926 with eleven extra
  skips, every one of them "need three movies". Use a second port for probes, and if a run
  looks catastrophic, check the fixture's row count before believing the failures.
- **A raw `+` in a query string is a SPACE. Send `%2B` when you mean a sign.** Cycle 22 recorded
  "`includeExtras=+1` does not turn the block on" and cycle 23 "a 0/1 option takes four values" --
  both from sweeps that sent a bare `+`, which `parse_qsl` and every real PMS decode as a space. A
  properly encoded `%2B1` turns the block on and passes the boolean gate on both references, so two
  cycles' grammars were wrong in the same way for the same reason, and cycle 29 found them in the
  same hour. Encode every value you sweep with, and when a finding says "this value is refused",
  check what the server actually received.
- **A check that starts its OWN server cannot be proved by `sdrive`.** `sdrive` serves the parent
  tree and the fixed tree and runs the block against each, but a block that spawns a server of its
  own (a `--web-root` instance, a token-gated one) starts it from the SUITE's tree -- the fixed one
  both times -- so the check passes on both and the proof is empty. Prove that patch by hand:
  export the parent, start both servers with the flag the check needs, run the requests, and put
  the two tables in the commit message's place (this cycle's `/web/*` patch did). The same applies
  to any check whose result does not come from `base`.
- **A single-witness SHAPE must be checked against plexscratch before it is implemented.** Cycle
  28 implemented "all three On Deck paths answer the full detail row" from zen alone, because
  kids' On Deck is empty -- the §3.3 note said so -- and the cycle-21 suite check then went red.
  The copy answered the question in one request: 1.43.4 drops `librarySectionKey`, `Guid`,
  `Rating` and `Producer` there and stamps `studio`. The copy is not only the WRITE reference; it
  is a **second version-bearing instrument**, and for any shape only one reference can show, it
  is the difference between a single-witness guess and a measured per-version split. Ask it first,
  not after the suite disagrees.
- **A check proved with `sdrive` can still fail inside the suite: prove it, then MAKE its
  state.** The driver runs a block against a freshly started fixture server, where the catalog's
  own tags and titles are intact. By the time the same block runs inside the whole suite, ~1,400
  checks have rewritten them -- an indexed tag write REPLACES the list on the item path -- so
  cycle 28's four search-tag checks queried a genre no row carried any more, and four proved
  findings read as failures. Write the state the check needs inside the check, with a
  MERGE-path write, and re-prove the edited block by hand (`runblock.py` against the pre-patch
  tree and HEAD), because `sdrive`'s parent is the fix itself once the patch is committed.
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
| `spellfix.py` + `spellfix_static_tags.json` (repository root) | Cycle 37's stdlib port of the measured SQLite `spellfix1` path; cycle 38 supplies the measured rebuild points; cycle 39 expands the rebuild over every generic row source and the exact 28 server-static optimize/show-ordering strings |
| `tests/catalog_features.json` + `tests/test_catalog_features.py` | Cycle 37's token-free focused fixture, now 25 regressions after cycle 39 added pass-specific score folding, generic vocabulary sources, the exact static sidecar and GUID-rank candidate protection |
| `refcheck7.py` + `ref_cycle5_before.json` | Read-only comparison of kids' library state against the baseline |
| `capture_agent_languages.py` | Cycle 25: `/system/agents`' per-language `Language` list, read from the XML (the JSON writes the rows as repeated KEYS and a parser keeps one) in each of the 28 languages plus English plus the no-header default. Writes `agent_languages.json`, which `server.py` loads beside itself. Re-run it if the reference's language list ever changes |
| `envelope.py` | Cycle 27: the HTTP ENVELOPE prober -- 30 raw-socket cases `urllib` will not send (a 200 KB header block, a 32769-byte URL, `GET  /identity` with two spaces, an HTTP/0.9 request line, `Content-Length: abc`), three servers at a time, flagging only where kids and zen AGREE and plexsim differs. Read-only by construction: every case is a GET or HEAD of `/identity`, and its one POST targets the plug-in dispatcher |
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
| `hdr.py` | Cycle 33: the RESPONSE HEADER prober -- twenty-six response classes off a raw socket on three servers, printing the status line and the header names in order, and flagging where kids and zen agree and plexsim differs. GET/HEAD/OPTIONS only, with the `WRITE_GET` list asserted per case |
| `frames.py` | Cycle 33: capture the frames one write pushes, on one server -- an eventsource listener with no `filters=`, a control write first, and a one-line summary per frame |
| `ddrive.py` | Cycle 34: drive all 78 client profiles' PLAIN-FORM decision against one row and record the four codes and their texts -- the file `tests/fixtures/decisions/cells.jsonl` is built from. GET-only, so it may be pointed at kids or zen; but the answer depends on per-user state the row does not show, so record WHICH state (`audioStreamID=<id>` sets it, `-1` clears it) |
| `rangesweep.py` | Cycle 35: the `Range` grammar off a raw socket -- 43 header values against both references over a container and a `/web` asset, 172 cells. `urllib` will not send most of them, and a ranged container answers its length twice |
| `sortfold.py`, `hubfold.py` | Cycle 32: the SORT-title fold swept by writing titles on both copies (writes to the copies only), and the HUB matcher's fold swept by GET on kids, word start and inside a word. `hubfold.py` reads `sortfold.py`'s output as its hypothesis |
| `race.py` | Cycle 32: two play-queue adds released from one barrier, N pairs, against one server; counts the pairs answered with the same container |
| `c31/D/foldsweep.py` (scratch, not tracked) | Cycle 31: the `title=` QUERY fold, swept character by character over U+0080..U+024F by substituting each codepoint for one letter of a known title on kids. Five rows, ~400 requests a letter, and it is what the §3.3 entry needs to close |
| `c31/D/backlog.py`, `c31/D/sse_life.py` (scratch) | Cycle 31: how much notification backlog plexsim absorbs before it drops a sink (400,667 bytes), and how long its eventsource channel really lives (open at 460 s, which refuted cycle 30's 256) |
| `harness/scratch42/` (tracked, not in `handover/tools/`) | Cycle 30's instrument: `compose.yml` and a README for **plexscratch42**, the writable 1.42.2 reference (§4.6). `docker compose up -d plex`, wait for `startState` to clear, then `loopback` and `relay` |

Per-cycle scratch directories are ephemeral. Each holds the hunt prompts, the re-measurement
clients, the patch module, the frozen trees and the suite logs. The transcripts are the
authoritative record of a hunt's reasoning:

- cycle 55: session scratch `.../scratchpad/c55/`: `A/` (hunt A's sweeps, `regress55.py`, `FINDINGS.md` -- the hunter's report, recovered from its transcript because the harness refused the file), `B/` (hunt B's `REPORT.md`, the numbered SQL reads and every request), `patches55*.py` and `pA/` (the implementation agent's diffs), `impl_A.md` (its brief), `D/notes.md`.
- cycle 54: session scratch `-Users-bensarmiento-debridmediamanager-plexsim/8c029687-4cba-4d75-bc2d-78af7d5bc4db/scratchpad/c54/`. It holds both PMS binaries and their strings (`pms_kids.bin`, `pms_zen.bin`, `*_strings.txt`), the parameter vocabulary and every sweep file of hunt A (`A/`, including `regress.py`, 53 checks of which 52 pass on the merged tree -- the one left is the bounded tagging join), zen's full TV and movie row dumps used for the fits (`D/zen_tv.json`, `D/zen_movies.json`), the container probe fixtures before they were marked shallow (`D/fixtures/`), the four patch files (`patches54{m,s,t,u}.py`) and the driver's notes (`D/notes.md`). Hunt B's report is in this file's §7 and in `docs/findings.md`; the harness refused its report file. The binaries and tokens are private scratch and never committed.
- cycles 52–53: session scratch `-Users-bensarmiento-debridmediamanager-plexsim/35af8374-d9d9-4580-b895-2a28f3d0fd9d/scratchpad/{c52,c53}/`, transcript `~/.claude/projects/-Users-bensarmiento-debridmediamanager-plexsim/35af8374-….jsonl`. `c52/` holds the two PMS binaries and their strings (`pms_kids.bin`, `pms_zen.bin`, `kids_strings.txt`, `zen_strings.txt`), the extracted route templates (`kids_routes.txt`), the 146-name single-segment vocabulary (`segs.txt`) and the 658-path route sweep. `c53/` holds a read-only copy of kids' live database and its 71-table schema (`kids_live.db`, `kids_live_schema.sql`), the 130-family JSON scalar census of three servers (`census_{kids,zen,sim}.json`), the media-flag file and geometry listings, and both hunt reports (`huntA.md`, `huntB.md`). The binaries, the database copy and the tokens are private scratch material and must never be committed; what is durable is in the checked-in fixtures (`media_flags.json`, `legacy_agents.json`, `attributions.json`, `plugin_bundles.json`, `plugin_prefs.json`, `resource_assets.json`) and in the rules in this file and `docs/findings.md`.
- cycles 50–51: `/private/tmp/plexsim-c50.58ZRIO/`. `A/REPORT.md` reconstructs the recovered
  branch and audits every deferred Cycle-49/Cycle-50 entry; `territory-a-implementation-report.md`
  maps the catalog fixes; `C/REPORT.md` holds the claim, dual-format, client-graph and expanded
  `/connections` results; `F/` holds the 58-path registry and redacted disposable connection
  evidence. The stopping reports are `A/CYCLE51_REPORT.md`, `CYCLE51_CATALOG_REPORT.md`, and
  `C/CYCLE51_REPORT.md`; all end `NO FINDINGS`. `ui-serial.log` is the uncontended final UI proof.
  The directory is ephemeral. Durable behavior lives in the checked-in fixtures/tests and the exact denominators/rules in this handover and
  `docs/findings.md`; no token or raw unredacted external body belongs in the repository.
- cycles 46–48: session scratch `6646e767-4912-4dac-bbe0-eea51f838555/scratchpad/{c46,c47,c48}/`.
  `c46/A` holds the complete
  116-name/76-family query-option census and its final zero-mismatch output; `c46/B`, `c47/B` and
  `c48/B` hold the 61-name/88-spelling header matrices and exact repeats. `c46/C` holds the
  installed Desktop inventory and first 1,665-exchange outbound census; `c47/C` and `c48/C` hold
  the frozen 1,829-exchange re-audits, ciphertext/Bearer proofs and daily-refresh proof. `c46/D`
  records the deleted-input evidence finding; `c47/D` contains the durable replacement builders,
  complete 205-asset graph and harness-order reproduction; `c48/D` is the clean 745-row historical
  and advertised-control repeat. `c46/head46` is frozen `d4a4b14`; `c46/wt46` carried the cycle
  branch through final `768377a`. Raw outbound bodies, tokens, copied PMS binary material and the
  decrypt secret are private scratch material and must never be committed. The protected-reference
  before/after snapshots are byte-identical and final exact-value secret scans are clean.
- cycles 5–8: `~/.claude/projects/-Users-bensarmiento-debridmediamanager-plexsim/2bf061c1-….jsonl`
- cycle 9: `…/5e5b69a0-….jsonl`
- cycle 10: `…/ebe127ef-….jsonl` plus its `subagents/agent-*.jsonl`
- cycles 11–12: `…/778e8a59-81ef-4a89-a46b-16ef32b5343d.jsonl`
- cycle 13: `…/75a28f3e-2e35-4b90-b25b-8aba31a8d0cd.jsonl`
- cycle 14: `…/03c44878-09cf-46fb-b529-806106f49f09.jsonl` plus its `subagents/agent-*.jsonl`
- cycle 15: `…/744f97c4-4f4c-41ea-a881-f16ca8d3e4c2.jsonl` plus its `subagents/agent-*.jsonl`
- cycle 43: `/private/tmp/plexsim-c43.pvIENe/`. `A/` holds the complete manifest-rooted public Plex
  Web 4.160.0 app graph (205 JS, 16 CSS, every asset hash and 207 verified SRI values), the
  old/current build comparison, request-builder census, provider reconciliation and clean report.
  `B/` holds the rebuilt 332-atom census, 24-fix cross-audit, targeted current-head replays and
  clean report. `C/` holds the fresh 49-family advertised-control matrix, 7,149-request disposable
  trace, complete UI transcript and clean report. `head43/` is frozen `5874d01`; tokens, public
  bundle bodies and cache-derived artifacts are private scratch material and must never be
  committed. All protected snapshots are byte-identical and all exact-value secret scans are clean.
- cycles 41–42: `/private/tmp/plexsim-c41.LqHDTd/` and `/private/tmp/plexsim-c42.V8haEa/` hold the
  authoritative 332-atom historical census, 49-family advertised-control graph, both clean replay
  reports and their machine-readable matrices. Cycle 43 reused only their token-free builders and
  retained-evidence hashes after auditing the inputs; it did not inherit their dispositions.
- cycle 40: `/private/tmp/plexsim-c40.doMJSX/`. `A/` holds the complete local 4.147.1 client
  inventory, surviving 4.160.0 cache recovery, provider call-site trace, write/undo ledger and
  exact lifecycle evidence. `B/` holds the machine-readable 867-branch/308-family inward census,
  line-trace coverage, guarded 90-cell two-version residual matrix and exact lookup response.
  `head40/` is frozen `bcda2f9`; `wt40/` is the cycle branch. Tokens, copied cache entries and
  provider evidence are private and must never be committed; reference snapshots are byte-identical.
- cycle 39: `/private/tmp/plexsim-c39.bELXsP/`. `A/` holds the guarded Plex Web 4.160.0
  capture, its discarded unsafe first attempt, exact replays and the byte-identical reference
  snapshots. `B/` holds the complete 2,076-codepoint fold/case sweep, exact vocabulary rebuild,
  697,334-query candidate comparison, live propagation witnesses and report. `head39/` is frozen
  `7d98293`; `parent-tag/` is finding 2's immediate parent; `wt39/` is the cycle branch. The raw
  corpora, copied database material and tokens are private and must never be committed.
- cycle 38: `/private/tmp/plexsim-c38.Z8VaTR/`. `A/` holds the slow-reader, notification,
  three-client race and withheld-body matrices plus the complete write/undo ledger; `B/` holds the
  disposable-copy storage inventory, database/preferences/bundle/cache fault cells and recovery
  proofs; `C/` holds both binaries' caller/comparator disassemblies, migration/schedule evidence and
  the 331-hub plus 51-flat-response validation tools. `head38/` is frozen `9403d3d`; `wt38/` is the
  cycle branch. Private copied databases/binaries and tokens must never be committed. Both
  `ref-before.txt`/agent snapshots and their after copies compare byte-identical.
- cycle 37: `/private/tmp/plexsim-c37.tYGOau/`. `A/` holds the temporary measured-shape catalogs and
  frozen-server results; `B/` holds the complete spellfix report, 376 guarded HTTP captures, copied
  private databases/binaries, literal schema/disassembly evidence, candidate/rank/holdout tools and
  the original plus rescored misses. `wt37/` is the cycle branch worktree; `head37/` is the frozen
  hunter tree. The copied databases and binaries are private measurement artifacts and must never be
  committed. `ref-before.txt` and `ref-after.txt` are byte-identical.
- cycle 29: the same session as cycles 27 and 28 (`…/eba8c320-…jsonl`) plus its
  `subagents/agent-*.jsonl`. Its scratch directory (`c29/`) holds both hunt prompts, `cl.py`,
  `wsl.py` (the driver's own notification listener -- `guarded.py`'s wants a `scratch.token` file),
  `patch1.py` to `patch6.py`, the doc and handover edit scripts, `zen_leaves.json` (all 9,287 zen
  episode stamps, fetched for the Recently Added roll-up question), and the suite logs. Hunt A's
  workspace (`c29/A/`) holds its guarded client and ~30 measurement tables; hunt B's (`c29/B/`) its
  extras census of 3,904 zen clips
- cycle 28: the same session as cycle 27 (`…/eba8c320-…jsonl`). Its scratch directory
  (`c28/`) holds both hunt prompts, `cl.py` (the driver's guarded client), `patch1.py` to
  `patch9.py`, `fix_c21_check.py`, the doc and handover edit scripts, and the suite logs --
  `ui28.txt`, the run a harness timeout killed at check 1376, and `ui28b.txt`, the full one.
  The copy keeps one artefact of this cycle: a `C28TVPL` playlist of a Firefly episode, made to
  settle which `searchTypes` token owns a playlist. `./scratch.sh reset` removes it
- cycle 27: `…/eba8c320-d873-4c99-aafd-f3823d6c0fe0.jsonl` plus its `subagents/agent-*.jsonl`.
  Its scratch directory (`c27/`) holds both hunt prompts, `cl.py` (the driver's guarded client),
  `profiles.json` (the 78-name `Player.profile` sweep), the thirteen patch modules, the doc and
  handover edit scripts, and five suite logs -- including `ui_dbg.log`, the contaminated run
  §10's new rule is written from. Hunt A's own workspace (`c27/A/`) holds its CDP harness, the
  four capture passes, the 190 KB replay report and its screenshots; the merged corpus is on
  kids at `/home/ben/plexlab/capture/sessions/c27-walk.jsonl` with the four raw passes beside it.
- cycles 25 and 26: `…/b59cf966-bebc-4d11-b0a6-beb9e8e6f6a5.jsonl` plus its
  `subagents/agent-*.jsonl`. Cycle 26's scratch directory (`c26/`) holds both hunt prompts,
  `remeasure_a.py`/`remeasure_b.py`, `sse.py`, the TTL harnesses (`ttl4.py`-`ttl7.py`, ten
  runs), `rm_lists.py` (the four list frame sequences driven side by side), `rm9.py` (the
  session-row shape diff) and fifteen patch modules.
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
