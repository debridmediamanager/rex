# Running rex without Plex guids: what clients lose

Scope only. Nothing here is implemented. Measured 2026-10-12 against a claimed rex
test instance through the hosted Plex Web 4.162.1 (app.plex.tv) driven over CDP, plus
read-only API counts on a production rex instance. Every state change made while
measuring (watchlist, watch state, rating) was reverted and read back. The screenshots
and request logs stay with the private operations notes.

The question: if rex stops giving items Plex-style guids, what does a person lose?

## Short answer

Playback, watch state, resume, Continue Watching, On Deck, star ratings, server
search, Related hubs, collections and playlists do not depend on the guid. They
behaved the same on `local://` rows as on rows with a Plex guid.

A real `plex://` guid is what lets a Plex client reach Plex's **online** side. That
covers the Watchlist button, Share, Rate & Review and community reviews, Plex's own
cast and person pages, "More Ways to Watch", the "Watch from these locations" row
naming rex on Discover and Watchlist pages, and the merged search result. A row
without one loses all of these, exactly as a Personal Media or unmatched item on a
real PMS does.

The third-party tools people run beside Plex lose more. PlexTraktSync, Kometa and
Overseerr/Seerr check the guid's scheme first and skip a `local://` movie or show
entirely, without reading its external ids. PlexTraktSync skips a `local://` episode
too.

Inside rex the guid is three more things:

- The key that folds several releases of one title into one row. Most multi-version
  rows on a production library fold this way.
- The key Fix Match merges on.
- The only key for the inbound cloud watch-state sync.

Dropping guids means re-keying the first two onto `_identity`. The third has no
replacement.

The guids rex **mints** today (`plex://…/<sha1("plexsim/…")>`) fail every Plex-online
call. Plex answers 404 for each one. A minted movie's Watchlist request gets a 404,
and a minted episode's button can add the show but never offers Remove. Every Fix
Match writes one. Third-party tools, though, treat a minted guid like a real one and read
the external ids, so PlexTraktSync syncs a minted episode and would skip the same
episode on `local://`.

## Where guids come from today

`rust/src/runtime/scan.rs` (`identify_arrivals`) names a release, then
`metadata::enrich` **mints** `plex://<kind>/` + `sha1("plexsim/<kind>/<seed>")[..24]`
for the movie, episode, show and season. The seed is the IMDb id, else `tmdb:N`; for
a season it is TMDB's season id. When Plex's agent knows the title, the feed
overwrites the minted guid with Plex's own (`POST metadata.provider.plex.tv/library/metadata/matches`,
then `children` by index). Fix Match (`route_item_matches`/`match_item` in `runtime.rs`,
and the oracle's twin) offers and writes minted guids only, seeded by the bare TMDB
number, a different seed from the feed's. Unidentified rows get `local://<ratingKey>`
on item, season and show.

On a production library (a census of `/library/sections/*/all?includeGuids=1`):

- Nearly every movie, and about nine in ten shows and seasons, carry Plex's real guid.
- About three episodes in four carry Plex's real guid, about one in thirteen a minted
  one, and the rest `local://`.
- Minted guids on movies are rare.
- Some minted episodes carry a `movie`-kind guid (an episode TMDB does not have keeps
  its minted movie guid).

"Minted" was established exactly, by recomputing `plex_guid(kind, seed)` from the
row's own `Guid` ids. Every sampled real guid (25 per type) answered 200 with the same
guid on `metadata.provider.plex.tv/library/metadata/<id>`. Every sampled minted guid
answered 404.

Folding: most rows holding several releases fold through a real Plex guid. A sizeable
minority of folded episodes (about one folded episode in thirteen, holding about a
sixth of the extra versions) fold **only** through their minted guid. `version_slot`
never folds a `local://` row.

The real-PMS analogue of "no Plex guid": a Personal Media (`tv.plex.agents.none`)
library gives a movie `tv.plex.agents.none://<id>`, and an unmatched episode or season
`local://<id>` (`tests/fixtures/split_guid.json`, plexscratch 1.43.4 and 1.42.2). Plex
Web treats both forms the same (`/(local|none):\/\//`).

## What the client does with each guid kind (measured)

Plex Web 4.162.1 detail pages on the test instance. Each was opened in a fresh tab,
and every request the page sent was recorded. Items are the test library's
ratingKeys.

| | real `plex://` (movie "'83", an episode, a show) | minted `plex://` (two episodes) | `local://` (a movie, an episode, a show) |
|---|---|---|---|
| Requests to plex.tv for the item | discover `/library/metadata/<id>`, `/userState`, `/credits`, `/availabilities` → all 200; community.plex.tv GraphQL | the same four → **404, 204, 404, 404** | **none** |
| Action row | Play, **Add to Watchlist**, Mark as Watched, Edit, **Share with Friends**, More; **Rate & Review** | Play, **Add Show to Watchlist**, Mark as Watched, Edit, More (no Share, no Rate & Review) | Play, Mark as Watched, Edit, More |
| Sections drawn | Cast & Crew (Plex's credits), **More Ways to Watch**, Related (rex's); a show adds **Ratings & Reviews** | Cast & Crew (rex's `Role`), a More Ways to Watch heading over a 404 | Cast & Crew when rex has `Role`; Related (rex's) |
| More menu | Fix Match… and Unmatch (movie, show), More Ways to Watch | More Ways to Watch (episodes never get Fix Match) | **Match…** instead of Fix Match/Unmatch on a movie or show |

The gates were read in the 4.147.1 bundle rex serves and in the 4.162.1 bundle app.plex.tv
serves (module `69249`, the same in both):

- Watchlist add/remove: the scheme of `guid || primaryGuid` must be `plex:/`. This is a
  prefix test, so a minted guid passes it.
- Share: `guid.indexOf("plex://") > -1`. Review: `isPlexGuid(guid)`. "View on Discover"
  and "Copy metadata key": `guid.startsWith("plex:/")`.
- Fix Match and Unmatch need a guid that is not `local://` or `none://`. Match… needs one
  that is.
- Mark played/unplayed, Rate, Play, Queue, Edit and Delete have no guid term.

### Watchlist round trips (all reverted)

- **Real guid ("'83"):** `PUT discover.provider.plex.tv/actions/addToWatchlist?ratingKey=5d7770ddf617c900201a0cc8`
  answered 200 and set `watchlistedAt`. The button flipped to Remove,
  `removeFromWatchlist` answered 200, and `watchlistedAt` read null again.
- **Minted episode guid:** "Add Show to Watchlist" sent the real **show** id and answered
  200, so the show was watchlisted. The button stayed on "Add Show to Watchlist", even
  after a reload, because the client reads the state through the episode's own id, which
  404s. The same show's season page (real guid) offered "Remove Show from Watchlist".
  Reverted with the client's own `removeFromWatchlist` request.
- **Minted movie guid:** the id rex would mint for "'83" without Plex's guid is
  `57e775f3568e772c1f198a8c`. The exact request the button sends answered
  `404 {"message":"MetadataItem for 57e775f3… not found!"}`, and nothing was stored.

### Discover and search

- The Discover page for "'83" asked every server the account owns
  `GET /library/all?type=1&guid=plex://movie/5d7770ddf617c900201a0cc8`. The test instance
  answered with its row, and the page drew **"Watch from these locations"** listing the rex
  instance (and a real PMS holding the film).
- In the client, the `hw` saga sends `guid=` for a movie or episode, and
  `show.guid=<parentGuid>&season.index=N` for a season. It throws "No GUIDs found" without
  one. rex filters on the stored `guid` string, so a `local://` row can never answer.
- Universal search for "83" drew **one** merged row, "'83 · 2021 · Movie · <rex instance> +1",
  carrying the Watchlist bookmark. The client dedupes Discover and server results by
  `guid` equality, so a `local://` copy would be a separate server row.
  - That part is read from the code. The test library holds no `local://` title that
    Plex also knows: in today's rex, `local://` only happens when Plex does not know
    the title.

### Cast and person pages

- **Real guid:** the credits come from Discover (`/credits`, photos from
  metadata-static.plex.tv). A person links to
  `#!/provider/tv.plex.provider.discover/details?key=/library/people/<Plex person id>`,
  which draws the full person page (bio, credits, related).
- **Credits from rex's own `Role`:** these are `local://` rows, and minted rows, whose
  Discover credits 404. They link to `/library/people/<rex tagKey>`. rex mints `tagKey`
  from the name (`hierarchy.rs` `person_tag_key`), and that page drew **"Something went
  wrong. An unexpected error occurred."**
- So person pages are already broken for every row without a real guid. Dropping
  guids would extend that to every row, unless rex also stored Plex's person ids.

### Not tied to the guid (measured on `local://` rows)

- Home's Continue Watching drew a `local://` episode beside a real-guid movie. It is rex's
  own `/hubs/continueWatching`, not plex.tv's.
- A `local://` show page offered Resume (rex's OnDeck) and Seasons.
- Mark as Watched/Unwatched sent only `/:/scrobble`/`/:/unscrobble` to rex, for both kinds.
  **Nothing went to plex.tv**, and Plex's `userState` for "'83" stayed unwatched.
- By API on the production instance: a `local://` item lists, searches, plays (play
  queue), saves progress, appears in Continue Watching, marks watched and unwatched and
  takes ratings like a `plex://` one.

### Cross-server watch-state and rating sync

A real PMS syncs both ways, keyed by the Plex guid. The lab's recorded outbound traffic
(`capture/outbound.jsonl`) holds, in matching numbers:

- `GET servers.plex.tv/api/v2/server/users/<id>/view_state_sync` and
  `GET scrobbles.plex.tv/state/<id>` (the pull);
- a few dozen `POST scrobbles.plex.tv/state {"items":[{"guid":"plex://movie/…","viewState":"complete|unplayed","viewedAt":…,"rating":…,"ratedAt":…}],"user":…}`
  (the push).

rex implements only the pull (`WatchStateSync` in `runtime/plextv.rs`):

- It matches a row by exact `guid` (`import_account_state`).
- It is off unless `REX_SCROBBLING_DECRYPT_SECRET_B64` is set (unset on the test
  instance).
- It never pushes.

So watch state marked on rex reaches no other server today, and without Plex guids
nothing from other servers reaches rex either. rex advertises `webhooks` among its owner
features but sends none, so webhook scrobblers (Trakt's, Simkl's) get nothing from rex
whatever the guid.

## Third-party integrations (read from source, not run)

| Tool (commit read) | `local://` item, ids still in `Guid` | invented `plex://` id, ids in `Guid` | evidence |
|---|---|---|---|
| Seerr/Jellyseerr (seerr-team/seerr `8f2eceea`), Overseerr (sct/overseerr `98ea135f`, archived) | **skipped**: the scanner reads `Guid` only behind a `plex://` test, then tries legacy-agent regexes (`imdb://`, `tmdb://`, `themoviedb://`, `tvdb://`, HAMA). It throws "Unable to find TMDB ID", so the title is never "available" (and, inferred, a watchlist auto-request could ask the *arr for it again) | works: ids from the listing's `Guid` (`includeGuids=1`), else `getMetadata` on the server; never asks plex.tv | `server/lib/scanners/plex/index.ts` lines 25-36, 385-480 |
| PlexTraktSync (`d5692fa7`) | **skipped**: a non-`plex://` guid is its only id, and `local` has no external id, so no watched, rating, collection or scrobble sync | works: reads the item's `Guid` | `plex/PlexLibraryItem.py:33-34, 84-105`, `plex/guid/PlexGuid.py:31-74` |
| Kometa (`e1327dc4`) | **skipped**: `item_type == "local"` raises "No match in Plex", so it drops out of every id-driven collection, overlay and operation | works from `Guid` | `modules/convert.py:296-345, 421-436` |
| Tautulli (nightly `1986e650`) | ids still read from `Guid` (notification links); `{plex_id}` empty. It reads the detail without `includeGuids`, so a row whose detail has no `Guid` loses its ids | `{plex_id}` holds an id that 404s in a user template | `notification_handler.py:631-711`, `pmsconnect.py:137` |
| Maintainerr (`fb1cc247`) | ids fine; its watchlist rule returns "undecidable" | **risky**: the watchlist rule confidently answers "not watchlisted", so a delete rule could remove a title someone watchlisted (inferred) | `plex.mapper.ts:131-190`, `plex-getter.service.ts:982-1050` |
| Ombi (`697d3564`) | movies fine; shows: its TVDB regex matches `local://123` and takes the ratingKey as a TVDB id, so the wrong show unless `Guid` holds `tvdb://` | ignored | `PlexHelper.cs:36-102` |
| WatchState (`40a8e574`) | treated like `plex://` | no difference | `Backends/Plex/PlexGuid.php:59-64` |

Episodes are different. The same commits were read for an episode whose own guid is
`local://` under a show and season with real guids:

- **Kometa and Overseerr/Seerr never read an episode's own guid.** Both map an episode
  through its show (`grandparentRatingKey`) and its season and episode numbers, so a
  `local://` episode works in both (Kometa `modules/plex.py:2653-2671`,
  `builder.py:5820-5824`; Seerr `index.ts:211-221, 297-306, 333-362`).
- **PlexTraktSync skips it.** Its syncable check rejects provider `local` before the
  show lookup, for both watched sync and scrobbles (`media/MediaFactory.py:58-77`,
  `plan/Walker.py:134-153`, `watch/WatchStateUpdater.py:123-125`).
- With a minted `plex://episode/…` and the episode's ids in `Guid`, PlexTraktSync
  matches by show plus `parentIndex`/`index`, then checks the episode's tmdb/imdb id
  against Trakt (`trakt/TraktLookup.py:59-82`).
- A legacy-shaped `com.plexapp.agents.thetvdb://<show>/<s>/<e>` would sync by show and
  numbers (`plex/guid/PlexGuid.py:57-64, 81-90`, `TraktApi.py:248-261`), if rex had the
  show's TVDB id. That is inferred, and Plex clients were not checked with such a
  guid.

## Feature table

| Feature | Depends on | Measured | If rex drops Plex guids |
|---|---|---|---|
| Play, progress, Continue Watching, On Deck, server star rating, mark watched | nothing | same on `local://` | no change |
| Watchlist button on the item | `plex:/` scheme on `guid` | real: add/remove 200, reverted. Minted: 404 for a movie, and an episode button that never offers Remove. `local://`: no button | gone from rex's pages; watchlisting from Discover still works but no longer leads to rex |
| "Watch from these locations" naming rex | rex answering `/library/all?guid=plex://…` | real: drawn | gone, unless rex maps a Plex guid to its rows another way (Option A) |
| One merged search result | `guid` equality | real: one row "+1" | two rows (from the code) |
| Share with Friends | `plex://` in `guid` | real only | gone |
| Rate & Review, community Ratings & Reviews | Plex guid | real only | gone |
| Plex's cast, working person pages | Plex guid (Discover `/credits`) | real: works. rex `Role`: "Something went wrong" | cast still drawn from rex's `Role`; person pages break everywhere |
| More Ways to Watch | Plex guid (`/availabilities`) | real: drawn; minted: a heading over a 404 | gone |
| Trailers and extras | Plex's agent by guid on a real PMS | rex serves `Extras size 0` on every row | already absent |
| Related / More like this | rex's `/related`, `/similar` | drawn from rex | no change while rows keep their genres |
| Fix Match / Unmatch vs Match… | the `local://`/`none://` test | `local://` rows get Match… | every movie and show offers Match… (the same endpoint in rex) |
| Cloud watch-state sync in | `guid` on `scrobbles.plex.tv/state` rows | protocol recorded on the lab; rex's pull is keyed by guid | gone; out is already absent |
| PlexTraktSync, Kometa, Overseerr/Seerr | the `plex://` scheme, then `Guid` (episodes: Kometa and Seerr go through the show) | read from source | every movie and show skipped by all three; every episode skipped by PlexTraktSync |
| Plex for Android detail screen | luma.plex.tv for `plex://` | cycle 70: a minted guid leaves the action row a skeleton and Seasons spinning | `local://` not re-measured on Android here |

## rex's own uses of the guid

| Where | Use | Without guids |
|---|---|---|
| `feed.rs` `version_slot`/`fold_versions` | releases of one title fold into one row by (type, section, season row, **guid**, edition); `local://` never folds | key on `_identity`, which every pushed movie, episode and show carries (`tmdb:N`, `imdb:tt…`, or `title:…` when unidentified); for an episode, the season row and `index`. Otherwise most multi-version rows come apart into duplicates |
| `runtime.rs` `match_item` | Fix Match merges into a row of the same section holding the chosen guid | merge on `_identity`. Today it almost never fires: the Fix Match seed (bare TMDB number) and the feed seeds (IMDb id, `tmdb:N`, or Plex's guid) never coincide |
| `runtime.rs` `route_item_matches`/`identify_guid` | candidates are minted guids, and the chosen one is written on the row; `_identity`/`_identify` are left as they were | the client needs a candidate token to send back; the row takes Plex's guid for that TMDB id (B) or `local://` (A), and `_identity` must be set |
| `feed.rs` `reidentify` | skips a re-push whose `_identity` and guid are unchanged | `_identity` alone |
| `feed.rs` `repair_seasons`, `scan.rs` `refresh_season` | a season's guid from Plex's children of the show guid; "the same TMDB season never moves the guid" | drop the Plex half (A) or keep it (B) |
| `hierarchy.rs` (both servers) | `parentGuid`/`grandparentGuid` copy the season's and show's guid | follow whatever the season and show carry |
| `plextv.rs` `import_account_state` | cloud sync by exact guid | dead under A |
| `runtime.rs` `carry_view_state` | view state after a catalog **file** rebuild, by guid then part file | part file only (pushed libraries are kept row by row anyway) |

## Measured PMS rules that bite a guidless rex

rex emulates these because a real PMS does them, and a `local://` row gets them all:

1. **No `Guid` key in any listing under `includeGuids=1`** (cycle 83, plexscratch 1.43.4 and
   1.42.2): not on the grid, `/children`, `/allLeaves`, hubs or Recently Added. A never-matched
   `local://` show or episode has none on its detail either (`resume.rs` `listing_guids`,
   `detail.rs`). So "`local://` everywhere but keep the external Guid array" is not what a
   PMS does. Keep the rule, and a tool reading ids from listings gets none. Seerr reads
   the listing first, then falls back to the detail; Tautulli reads the detail, which
   keeps a stored list. Break it deliberately, and it needs its own measurement that no
   client minds. The three tools that skip `local://` skip it either way.
2. **`unmatched=1` finds every `local://`/`none://` row**, so the whole library lists as
   Unmatched.
3. **An unmatched movie has no item preferences.** The block is empty and `PUT /prefs`
   answers 400. An unmatched show loses `languageOverride` and `useOriginalTitle` (cycle
   16/36; `showprefs.rs`, `detail.rs`).
4. A never-matched row opens its refresh with `state 2 matching` and a bare `state 5`
   (cosmetic).

## Options

### A. No Plex guids at all

Every row gets `local://<ratingKey>`, or `tv.plex.agents.none://<id>` for movies as a
Personal Media library does. rex stops asking Plex's metadata provider for guids.

- **Gains:**
  - No dependency on Plex's metadata service for identity.
  - No guids that Plex 404s, and no luma or Discover calls per page view.
  - One identity scheme.
- **Loses:**
  - Everything in the Plex-online column above, for every title Plex knows (nearly all
    movies, most episodes): Watchlist on rex's pages, rex as a Discover or Watchlist
    location, the merged search row, Share, reviews, Plex's cast and person pages, More
    Ways to Watch, and cloud sync in.
  - PlexTraktSync, Kometa and Overseerr/Seerr skip every movie and show, and
    PlexTraktSync every episode too.
  - Fix Match becomes Match… everywhere, every row is "unmatched", and movie prefs go.
- **A mitigation worth measuring first:** answer `GET /library/all?guid=plex://X` by
  resolving X to its TMDB or IMDb id (one cached `metadata.provider.plex.tv` read) and
  matching `_identity`. That keeps "Watch from these locations" without storing a Plex
  guid. It still needs Plex's metadata service, and nothing else above comes back.
- **Code:**
  1. `enrich` stops minting (Rust and `oracle/metadata.py`).
  2. `scan.rs` stops writing `identity.plex` and `plex_children` guids.
  3. Placement gives movies and episodes `local://<key>`, as it already does for shows
     and seasons.
  4. Re-key `version_slot` and `match_item` on `_identity`, skipping `title:` identities.
  5. Fix Match sets `_identity`.
  6. `reidentify` compares `_identity` only.
  7. Drop the Plex half of `repair_seasons`/`refresh_season`.
  8. Fix Match candidates carry a token such as `tmdb://N`.
  9. Retire `import_account_state` and `build_catalog.py --relink`.
  10. Decide rule 1.
  11. Change `hierarchy.build` in both servers (`test_native_phase2` compares them).
- **Migration of a production rex.db** (rex stopped, a `sqlite3 .backup` first):
  - Every row's `guid`/`parentGuid`/`grandparentGuid` changes and `_show_guid`/`_season_guid`
    go. The old value is kept in a private `_plex_guid`.
  - Watch state and history live on the ratingKey and do not move.
  - Reversible by copying `_plex_guid` back, or from the backup. Without `_plex_guid`,
    only while `plex_cache.db` still holds Plex's answers.
- **Tests:**
  - `test_feed_versions.py` must fold by identity.
  - `test_feed.py` (late ids, re-identification) and `test_feed_season_art.py` (its
    season guid assertions change).
  - The `scan.rs` unit tests (`an_imdb_id_from_zurg_names_the_release_and_plex_names_its_guids`,
    the minted-season case) and the `test_native_phase2` metadata and hierarchy cases.
  - New: two releases of one `local://` title fold; Fix Match merges by identity; the
    migration on a copy of a production rex.db keeps every row's Media count and view
    state.
  - A Plex Web pass of the measurement above, and a Plex for Android pass before
    shipping.
- **Size:** large. About 2 to 4 days, plus the rule-1 decision and its measurement.

### B. Only Plex's real guids, never minted ones

Keep the guid Plex's agent gives. Give a movie or show Plex does not know
`local://<ratingKey>`, as today. Stop minting guids, and make Fix Match write Plex's
guid.

- **Gains:**
  - Fix Match stops turning a working row into one Plex 404s, and its merge works.
  - Plex for Android's skeleton screen on minted movies and shows (cycle 70) cannot
    happen.
  - Every Plex-online feature stays for every title Plex knows.
  - Minted guids are recomputable, so the migration can be undone by recomputing them.
- **What to do with the episodes Plex lacks** (its season has no such index) is the
  one real choice:

  | episode Plex lacks gets | Plex Web | PlexTraktSync | Kometa, Seerr |
  |---|---|---|---|
  | B1: `local://<key>` | no Plex calls; no Watchlist button on that episode page (its season and show pages keep it) | **skipped** (no watched or scrobble sync) | works |
  | B2: its minted guid, as today | four 404s per page view; "Add Show to Watchlist" adds the show but never offers Remove | works | works |
  | B3: `com.plexapp.agents.thetvdb://<show>/<s>/<e>` | not measured | works if rex has the show's TVDB id (inferred) | works |

  Plex for Android on a minted **episode** was not measured (cycle 70 measured movies
  and shows).
- **Code:**
  1. `enrich` stops minting for movies, shows and seasons, in both servers (episodes
     per B1/B2/B3).
  2. Placement assigns `local://<key>` where Plex gave nothing.
  3. Re-key folding on `_identity` **before** any migration: folded episodes on minted
     guids fold only through them today.
  4. Fix Match keeps a candidate token. On `PUT /match` it resolves the chosen TMDB id
     through the agent call rex already makes (`POST /library/metadata/matches
     {"guid":"tmdb://N"}`), writes Plex's guid (else `local://`), sets `_identity`, and
     merges by `_identity`.
  5. Optional: a pass like `repair_seasons` that upgrades an episode once Plex gains
     its index. Plex does add episodes over time: one minted episode's season guid in
     the test library dates from 2026.
- **Migration:** the minted movie, show and season rows and Fix-Matched rows (plus the
  minted episodes under B1), found exactly by recomputing the seed. Backup first, with
  rex stopped.
- **Tests:**
  - A pushed movie Plex lacks gets `local://`, not `plex://`. This fails on main today.
  - Fix Match writes Plex's guid (stubbed agent) and merges into a feed row of the
    same title.
  - Folding holds after the re-key.
  - The `scan.rs` "a minted guid moves nothing" case and the parity metadata cases.
- **Size:** small to medium. About 1 to 2 days.

### C. Status quo

Keeps everything that works and every minted-guid failure measured above. Fix Match
quietly converts more rows over time.

### Not measured: legacy-agent guids

A real PMS's legacy agents give `com.plexapp.agents.imdb://tt…?lang=en`,
`com.plexapp.agents.themoviedb://N?lang=en` and `com.plexapp.agents.thetvdb://…`.

- Plex Web would treat such a row as matched (Fix Match, prefs, not "unmatched") with
  no Plex-online features.
- Overseerr/Seerr parse these forms (the regexes above), and PlexTraktSync and Kometa
  treat a non-`plex://` guid as a legacy agent's.
- That could make a guidless rex without rules 1 to 3 and without losing the
  third-party tools. It needs a real PMS's listings and details for such rows recorded
  first. The scratch reference has no route out, so its legacy agents cannot match.
  Not recommended until then.

## Recommendation

**B, now, with B2 for episodes.** Every Plex-client failure measured here comes from
minted guids, never from Plex's real ones, and B keeps every Plex-online feature for the
titles Plex knows.

- **Movies and shows:** a minted guid hurts the main clients. Plex 404s it, its
  Watchlist click fails, and Plex for Android draws a skeleton. Its one upside is that
  the three third-party tools read it like a real guid, and would skip the same row on
  `local://`. Once Fix Match writes Plex's guid, those rows are the rare titles TMDB
  knows and Plex does not, so `local://` costs little.
- **Episodes Plex lacks:** keep minting. They are far more common. Their cost is four
  404s, an empty More Ways to Watch heading, and a Watchlist button that cannot offer
  Remove, while `local://` would silently drop them from PlexTraktSync. Move them to
  `local://` (B1) if a Plex for Android pass shows that a minted episode breaks its
  screen the way a minted movie does.

The re-key of folding and Fix Match onto `_identity` that B needs is also the hardest
part of A. So A stays a cheap later step if the goal becomes "no dependency on Plex's
metadata service".

Do not do A only to simplify the code. It trades the Watchlist, Discover, sharing,
reviews, cast pages and three popular third-party tools on nearly every row for the
removal of one lookup.

## Not measured, and why

- **Cross-server sync end to end.** It needs a rex with the scrobbling secret and a
  second claimed server holding the same title. The protocol is recorded on the lab,
  and rex's pull is keyed by guid in code.
- **A natural `local://` copy of a title Plex knows** (search merge, Discover
  availability for it). The test library has none, because `local://` only happens
  where Plex does not know the title. This is read from client code.
- **Plex for Android and other native clients with `local://` rows.** Cycle 70 measured
  the minted case only.
- **Third-party tools live.** They were read from source only (commits above).
- **The UI toast for a minted movie's Watchlist click.** Its request was replayed
  directly and answered 404.

## Evidence

- Client gates: Plex Web 4.147.1 (`main-8792-681b145bfe8a22701a49-…`, the bundle rex
  serves) and 4.162.1 (`app.plex.tv/desktop/js/main-8792-e21a32ad76ad9fa8dc08-…`).
  The menu predicates are module `69249`, the `/library/all?guid=` lookup is the `hw`
  saga, and plex-id extraction is module `38815`.
- Driving app.plex.tv against a rex on a private or tailnet address needs CDP
  `Browser.grantPermissions` with `localNetwork` for `https://app.plex.tv`. Chrome 154
  otherwise refuses every request with `LocalNetworkAccessPermissionDenied`, which shows
  as an endless spinner.
- Prior measurements cited:
  - findings "The seventieth pass" (Plex for Android, minted guid);
  - protocol-traps cycle 83 (the Guid gate), cycles 16/36 (unmatched prefs) and 46
    (the outbound census);
  - `tests/fixtures/split_guid.json`.
