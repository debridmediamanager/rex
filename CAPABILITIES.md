# simplex capabilities

This is the feature inventory of the current movie/TV server, based on the code
and recorded measurements through **cycle 95** (reopen triggers re-checked
2026-10-06, none fired), the zurg backend and plex.tv registration of 2026-10-09 and
2026-10-10, and the feed that replaced the scanner on 2026-10-10. It describes
implemented behavior and its prerequisites, not the current health or
configuration of a deployed instance.

simplex presents a Plex library its sources push to it -- zurg's -- and streams the
original media from them without keeping complete media files locally. simplex lists
and queries no source and has no provider code of its own. Its largest limit is **direct play only**. Library
changes last across restarts: a state file beside the catalog is kept by default,
and `--no-state` turns that off.

`--state` (after cycle 72) made the server's mutable state survive restarts; see
[Persistence](#persistence). Cycle 73 added a server restart as the twelfth audit
axis, and cycles 73-79 fixed what it and the repeats found: writes saved before
they are answered, sidecar subtitle bytes, play-queue ownership and flags, the
Shuffle toggle, and season 0 `Specials`. Cycles 80-82 found nothing further on
any of the twelve axes, the last under python-plexapi 4.18.3.

Since 2026-09-30 the server is the native Rust binary (see
[Implementation](#implementation)). Cycles 83-86 ran the twelve axes against it and
fixed what they found, among them Plex for Android's poster tiles, `continuous=1`
episode queues, delete notifications and play-queue windows. Cycles 87-91 covered Plex
Web 4.162.1: provider and agent pages, music libraries, and collection, list-artwork
and random-sort rules. Cycle 92 made the state file the default. Cycle 94 covered Plex
for Android 2026.19.1: its server dashboard, history filters, Top Played and the
notification socket's ping/pong. Cycles 91, 93 and 95 found nothing further on any
axis.

On 2026-10-09 zurg became the only library and byte source: native Real-Debrid
support was removed, every part is read from zurg's WebDAV, and the native server
scanned its libraries from zurg when zurg asked. On 2026-10-10 a real claim
registered a test identity on plex.tv the way a real PMS registers, and app.plex.tv
direct-played a movie from it over its announced connection. The same day the direction
reversed: sources push their items to simplex's feed API and simplex scans nothing (see
[Catalog creation](#catalog-creation-and-metadata) and [docs/feed-api.md](docs/feed-api.md)).

## Client access and discovery

- Serve an extracted official Plex Web bundle at `/web` and answer the requests
  used by its library and administration screens. The bundled build is 4.147.1;
  the hosted 4.162.1 build can be laid out for `--web-root` and is covered too.
- Support the measured native Plex for Android (2026.19.1) browsing, detail,
  playback-state, history, post-play, server-dashboard, history-filter and Top
  Played request flows, and keep its notification socket open. Its detail pages require genuine Plex
  metadata IDs; the feed obtains them with `PLEX_TOKEN`, and `build_catalog.py --relink`
  for an existing catalog.
- Support the tested `python-plexapi` read, write, and playback workflows.
- Answer Plex LAN discovery through GDM on UDP ports 32410 and 32414.
- Expose server identity, library providers, capabilities, client profiles, and
  server/account information in the forms Plex clients expect.
- Redeem a real claim (`POST /myplex/claim?token=` with a claim token from
  `plex.tv/api/claim/token.json`, once per machine identifier) and register the way a
  real PMS does, as measured on a throwaway 1.42.2: `POST /servers.xml` creates and then
  updates plex.tv's server record (its normal answer is a 422), and the pubsub event
  stream simplex then holds open is what clients read as `presence`. As the stream opens
  it requests plex.tv's reachability check (`PUT /api/servers/{id}/connectivity`) and
  logs the verdict the stream carries back, and it resubscribes 15 s after a drop. The
  claim is kept in `claim_state.json`, so a restart re-registers without claiming again.
- Announce `--announce-url` to plex.tv and re-announce it on an interval, with the
  server's own token once claimed (the account token in `PLEX_TOKEN` before): a
  comma-separated list is announced as one connection per URI, as a PMS with several
  connections does.
- Operate behind a configured public tunnel or reverse proxy.
- Serve HTTP and, with a supplied certificate/key, HTTPS on the same port.
- Gate requests as a real PMS does once there is a gate: a real claim always raises it,
  and so does `--token` (or `SIMPLEX_TOKEN`), whose token is then admitted too;
  `--assert-claimed` does not. A claimed server admits its own token and every enabled token
  `plex.tv/api/v2/server/access_tokens` lists for it (the owner's devices, shared users'
  too), reading that list at a claimed start and re-reading it for a token it does not
  know. `/identity`, `/web`, `/manage`, `/security/token` and the three bootstrap files
  (`crossdomain.xml`, `clientaccesspolicy.xml`, `favicon.ico`) stay open; everything
  else is refused as a real PMS refuses it, a 401 for the library and the other
  measured routes. With neither a claim nor `--token` the server answers anyone who can
  reach it.
- Run several servers under `manager.py`, which starts none by default: each is
  created from a configuration (port, keys, catalog, options), started and stopped
  through an API, a CLI or a page, restarted with a backoff when it exits
  unexpectedly, marked failed when it keeps exiting, and restored after the
  manager itself restarts if it was running then.

Sources: [server.py](server.py), [gdm.py](gdm.py), [claim.py](claim.py),
[heartbeat.py](heartbeat.py), [plextv.rs](rust/src/runtime/plextv.rs),
[plex.tv checks](tests/test_plextv.py), [Android harness](harness/android/README.md).

## Catalog creation and metadata

- Build the library from what its sources push (native server, [feed API](docs/feed-api.md)):
  `POST`/`DELETE /simplex/feed/{source}/items` and `PUT /simplex/feed/{source}/snapshot`,
  each admitted by the source's own feed token (never a Plex token) and applied before it
  is answered, one at a time. An item lands in the library whose `Location` holds its
  file (or the one it names); its Part `file` is what the source says clients see (zurg:
  `<mount>/<directory>/<release>/<file>`, which zurg's matcher reads back). simplex lists,
  scans and queries no source: `GET /library/sections/{id}/refresh` and `/activities`
  answer as a library with nothing to read does, which old zurg builds still ask for.
  `build_catalog.py --seed` writes the empty library to start from.
- Mint each row's ratingKey once, from 100000 up, for its (source, id), and never change
  or reuse it across pushes and restarts (zurg drops a release's IMDb id when the key it
  matched it to disappears). The first push of a file the old WebDAV scanner made a row
  for adopts that row, key and watch state included.
- Hold an item whose `media_info` has not arrived: it is in no listing, hub, search or
  count until a push brings its probe, and then arrives as newly added. Held items are
  listed at `/simplex/feed/{source}/held` and kept across restarts. A row already shown
  that was added before its probe (by the old scanner) gets its streams from the push.
- Re-identify a row when a later push names it by an id it was not identified by (a late
  IMDb id), on the same ratingKey with its watch state; an episode moves to the show its
  new identity names.
- Identify each release (native `identify.rs`, the port of `identify.py`), most exact
  evidence first: an id written into the name, then the IMDb id zurg hands over for the
  release's hash (exact), then its cleaned name to Plex's own matcher (`PLEX_TOKEN`) and
  TMDB (`TMDB_KEY`), with their agreement as the confidence. By name alone, on 200
  labelled torrents of the real library, the two agreed on 173 (86%) and were right on
  169 of those (98%); overall 193 of 200 were right, against 167 for the earlier
  first-search-hit method, and the Rust port names the same 193. A release no source
  names stays unmatched (`local://`) for Fix Match; nothing is guessed.
- Trash what a source removes the way a real PMS trashes a missing file (measured on
  plexscratch): `deletedAt` on the row and its Media, deleted at once when
  `autoEmptyTrash` is on, restored on the same key when the same id returns before the
  trash is emptied, gone at `emptyTrash`. A snapshot removes what it does not name, but
  an empty one removes nothing (as an empty library folder trashes nothing) and one that
  would remove more than half the source's items is refused without `force`.
- Derive real container, duration, bitrate, resolution, frame-rate, codec,
  audio-channel, language, and subtitle-stream information from the pushed ffprobe.
- Represent stacked multipart files where the catalog supplies them; the feed makes
  one row per item, so a `cd1`/`cd2` pair is two rows.
- Build separate movie and television sections, with synthesized show and
  season parents, episode ordering, child counts, and leaf counts.
- Represent multiple media versions and parts where supplied by the catalog.
- Enrich titles, episode names, summaries, release information, ratings, genres,
  people, posters, and backgrounds using TMDB.
- Obtain genuine Plex IDs from Plex's metadata provider when configured (`PLEX_TOKEN`),
  as the feed identifies each release, and relink an existing catalog with
  `build_catalog.py --relink`. `build_catalog.py --convert` moves a JSON catalog into
  a database.
- Cache metadata locally and retain a stable server identity in the catalog.
- Search match candidates and support Match, Fix Match, and Unmatch. Candidate
  searches and TMDB re-identification require a configured TMDB key.
- Inspect likely direct-play compatibility using the `gate.py` helper; its
  client-class checks are advisory, not universal device certification.

Sources: [feed.rs](rust/src/runtime/feed.rs), [scan.rs](rust/src/runtime/scan.rs), [source.rs](rust/src/runtime/source.rs),
[identify.rs](rust/src/identify.rs), [identify.py](identify.py),
[feed checks](tests/test_feed.py), [metadata.py](metadata.py),
[mediainfo.py](mediainfo.py), [hierarchy.py](hierarchy.py), [gate.py](gate.py).

## Browsing and recommendations

- Library grids and individual movie, show, season, and episode detail pages.
- Season children, episode children, grandchildren, and all-leaf listings.
- A season 0 titled `Specials` (localised as a real PMS localises it), and a
  show's `seasonCount` when it holds one.
- Collection and playlist browsing.
- Folder views, alphabetical indexes, categories, tag axes, and people pages.
- Technical Get Info views with media, part, and stream details.
- Pagination, total counts, and the different item projections clients request.
- Home and library Recommended shelves populated from catalog and viewing data:
  Continue Watching, On Deck, Recently Added, Recently Released, Recently
  Watched, Start Watching, Top Rated TV, Top Unwatched Movies, genre-based movie
  shelves, and recent playlists.
- Related/similar items and collection-related shelves where the catalog has
  the metadata needed to populate them.
- Android's post-play Up Next shelf, derived from the show's viewing state.
- Top Played (`/library/all/top`) rows with global view and user counts.
- Extras and other optional detail blocks where the catalog supplies them.

Sources: [server.py](server.py), [hierarchy.py](hierarchy.py),
[navigation checks](tests/test_navigation.py).

## Search, filtering, sorting, and grouping

- Global, library-scoped, legacy, and hub search.
- Autocomplete, people/tag/collection search results, and spelling fallback.
- The server query endpoint used by voice-search clients; speech recognition
  itself belongs to the client.
- Plex's filter language, including operators encoded in parameter names,
  compound conditions, comparisons, dates, and saved-query parsing.
- Filters for titles, years, decades, genres, ratings, people, studios/networks,
  countries, collections, labels, editions, watched state, progress, resolution,
  HDR/Dolby Vision, audio/subtitle languages, and locations, as applicable to the
  library and item type.
- Sorting by title, release date, date added, date viewed, ratings, duration,
  resolution, episode order, and random order, as applicable to the view.
- Supported grouping and cross-type filter scopes.
- Smart playlists and smart collections that evaluate their saved query against
  the current library rather than retaining a snapshot of the original results.

Sources: [filters.py](filters.py), [spellfix.py](spellfix.py),
[server.py](server.py), [filter descriptors](section_filters.json),
[sort descriptors](section_sorts.json).

## Playback, downloads, and queues

- Direct-play original media from its source: a pushed row's bytes are
  `<source url>/<path>` (zurg: `$ZURG_URL/dav/<directory>/<release>/<file>`), and a
  catalog row's Part `file` under a source's `mount` is read the same way. simplex streams
  them with the measured Range grammar, the source's own credential and the content type
  the file's extension names. `--part-mode redirect` is gone from the native server: it
  sent clients to the provider URL zurg's signed link named, which a source that pushes
  its items does not hand over (the Python oracle keeps it).
- Byte-range requests for seeking and partial reads.
- Original-file downloading.
- Playback decisions based on client profiles and the selected media/streams.
- Audio/subtitle stream selections and per-session playback state.
- Play queues built from individual items, shows, seasons, playlists, and
  supported filtered library selections.
- Play Next, append, remove, reorder, clear, shuffle, and unshuffle queue actions,
  including shuffling a queue nothing was added to.
- Queue `shuffle`/`repeat` as 0/1 integer flags; any other value is refused.
- Queue selection, versioning, and update responses used by clients, and moving
  a queue to the client that reads it with `own=1`.
- File checks (`checkFiles` and its two siblings) that ask the source, and distinct
  answers for a missing file versus a failure to reach it: the source's 404 for the file
  is `accessible=false`/`exists=false` and a 404 on the byte read, another status it
  answers is passed on, and a transport failure is a 502. With no source every part
  read is that 502.

Playback requires the client to support the original container, codecs,
bitrate, and selected tracks. There is no transcoding or remuxing fallback.
Original-file downloads are separate from the incomplete mobile offline-download
workflow. Subtitle selection does not imply subtitle extraction or delivery.

Sources: [server.py](server.py), [decision.py](decision.py), [zurg.py](zurg.py),
[part-byte checks](tests/test_part_bytes.py), [playback checks](tests/test_all.py).

## Viewing state, history, and ratings

- Playing, paused, and stopped timeline reports.
- Progress/resume positions and completed-view tracking.
- Mark played/unplayed actions for supported items and parent hierarchies.
- Watched/unwatched leaf counts and associated parent-state updates.
- Personal ratings.
- Continue Watching/On Deck updates and removal from Continue Watching.
- Playback history listing and history deletion.
- Android offline-viewing reports, including their measured `viewCount` behavior.
- Optional import of the claimed owner's Plex cloud watch/rating state.
- Periodic refresh of the claimed owner's account information and feature list.

Viewing state and history persist across restarts with `--state`.
Cloud state import is conditional on claim, consent, and service-token setup.
It is not full bidirectional synchronization or a multi-user state system.

Sources: [library.py](library.py), [sessions.py](sessions.py),
[accountstate.py](accountstate.py), [server.py](server.py).

## Item editing and organization

- Edit supported metadata fields and tags, individually or through supported
  bulk-edit forms.
- Lock and unlock metadata fields.
- Read and change supported advanced item preferences.
- Match, Fix Match, and Unmatch, including the measured hierarchy updates.
- Merge supported items and split media versions where the item type allows it.
- Delete individual media versions or remove catalog items.
- Propagate relevant parent metadata, counts, and artwork to child rows.

Deletion removes the row from the library (and, with `--state`, keeps it removed
across restarts); it does not delete anything from zurg or rewrite `catalog.json`. Edit support follows the measured Plex
route and field rules: an accepted write is not a promise that every supplied
field is stored.

Sources: [library.py](library.py), [server.py](server.py).

## Playlists and collections

| Operation | Playlists | Collections |
|---|---|---|
| Create, rename, edit summary, delete | Supported | Supported |
| Add/remove members | Supported | Supported |
| Clear the item list | Supported | Remove members individually |
| Reorder members | Supported for ordinary lists | Supported with applicable collection ordering |
| Browse and play members | Supported | Supported through collection/library selections |
| Smart query membership | Supported | Supported |
| Artwork | Composite artwork responses; an uploaded poster becomes the list's thumb | Supported artwork selection/upload and composite responses |
| Additional behavior | Recent-playlist shelves and viewing state | Ordering/display preferences, member tags, and related shelves |

Smart-list membership is governed by its query; ordinary item-list mutations
are subject to the measured restrictions. Lists and their edits persist across
restarts with `--state`.

Sources: [library.py](library.py), [server.py](server.py).

## Artwork and subtitle records

- List, select, upload, and serve supported posters, backgrounds, thumbnails,
  banners, logos, and themes, subject to the item's route and artwork type.
- Propagate applicable artwork through show/season/episode relationships.
- Serve library/list composites and cached remote artwork responses.
- Choose available TMDB image sizes for supported image requests.
- Answer Android's background-color requests using the row's stored palette.
- Upload subtitle files, select a subtitle track, and remove uploaded tracks.
- Serve an uploaded subtitle's bytes at its stream key, the URL Plex for Android
  loads when a sidecar subtitle is selected.
- Accept the measured subtitle upload formats: SRT, ASS, SSA, SMI, TXT, and PSB
  (the API format values are lowercase).

The image path is not a general image-processing engine. Embedded subtitle
extraction returns not implemented; only uploaded subtitles have byte delivery.

Source: [server.py](server.py).

## Dashboard, sessions, and diagnostics

- List active playback sessions with player/device identity, playback state,
  media details, and progress.
- Terminate known sessions, report termination on subsequent client timelines,
  and refuse subsequent session-associated part requests.
- List devices the server has seen and expose the configured owner account.
- List and delete playback-history entries.
- Provide bandwidth and media-statistics responses from observed activity.
- Sample process CPU usage and approximate host utilization from system load.
- Show activity progress and cancellation responses for supported operations.
- Stream Console logs and provide a downloadable log archive.

Statistics and the device table persist across restarts with `--state`, and are
otherwise process-local. Live sessions never outlive the process. Redirected CDN traffic is outside simplex's byte
counters; resource-memory values are placeholders. There is no persistent Plex
database to export, and termination cannot recall bytes already delivered or a
CDN URL already handed to a client.

Sources: [sessions.py](sessions.py), [server.py](server.py).

## Server and library administration

- Render server settings groups and read/save supported preference values.
- Render library and item preferences, including advanced settings.
- Create, rename/edit, and delete library sections, music sections included
  (their measured, localised empty-library surface; see the music limit below).
- Expose agent, scanner, and language choices and virtual folder browsing.
- Handle scan and cancellation requests for individual/all libraries.
- Handle Refresh Metadata, Analyze, Empty Trash, and maintenance-task requests
  with the measured responses and activity lifecycle.
- Expose update status and the measured check/apply responses.
- Expose measured agent/provider/plugin descriptors and configuration surfaces.

These controls have varying depth. A scan works over the existing rows on both servers:
what a library holds comes from its sources' pushes, never from a scan.
Refresh/Analyze and many maintenance controls
reproduce client-visible state and notifications rather than implementing a
full scanner, metadata-refresh pipeline, media-analysis backend, or backup system.
Saving a preference for an absent subsystem does not enable that subsystem.
The updater does not install PMS updates, and descriptors do not constitute
plugin installation or execution support.

Sources: [server.py](server.py), [library.py](library.py),
[server preference descriptors](server_prefs.json).

## Notifications, protocol compatibility, and localization

- WebSocket and event-stream notifications for playback, item edits/deletions,
  queues, preferences, scans, activities, and logs. A client's ping on the
  WebSocket is answered with a pong, which Plex for Android needs to keep it open.
- JSON and Plex XML responses with measured element shapes and value types.
- Paging, counts, query projections, and supported include/exclude options.
- Measured authentication, HTTP method, header, compression, range, and error
  behavior for the implemented request surface.
- Bundled resources, media flags, client profiles, and public bootstrap assets.
- Localized settings, menus, shelves, stream descriptions, language names, and
  activity notifications across dozens of Plex languages.
- Localized Android Up Next and inline Related titles.
- Measured product/platform/version-specific response differences.

This is conformance within the tested surface, not a claim of complete PMS
equivalence. Deliberately deferred and unmeasurable cases remain recorded in
[HANDOVER.md](HANDOVER.md).

Sources: [server.py](server.py), [xmlout.py](xmlout.py),
[translations.json](translations.json), [protocol traps](docs/protocol-traps.md).

## Persistence

The server saves what clients change to a SQLite database and restores it at start:
by default `simplex.db` beside a JSON catalog, or the catalog's own database when the
catalog is one (below), or `--state FILE` / `SIMPLEX_STATE`. The Python server keeps
a pickle (`state.pickle`) instead. Each collection -- the catalog rows, history,
lists, devices, statistics -- is stored a row per element, and a save writes only the
elements that changed, in one transaction: one rating on a 20,000-row library writes
a handful of rows, where the earlier JSON file was rewritten whole on every save. An
older `state.json` beside the catalog is imported once and kept as
`state.json.migrated`. A client write (any non-GET request, and the
scrobble, rate, progress and timeline GETs), and a client's first request or a
changed device name or platform, is on disk before it is answered, so a
crash loses nothing a client was told succeeded -- a real PMS keeps writes answered a
fraction of a second before a hard kill. Everything else is saved within about two
seconds, every minute regardless, and on SIGTERM or Ctrl-C. `--no-state` (or an empty
`SIMPLEX_STATE`) turns it off, which is how the test suites run: they rely on a restart
undoing their writes. A second server started on the same catalog does not share the
first one's default file: it refuses to start, printing what a second PMS on a served
data directory prints (`Plex Media Server is already running. Will not start...`) and
exiting 1.

| Kept | Not kept |
|---|---|
| Watch state, progress, ratings, view stamps, and parent counts | Live playback sessions and terminations |
| Metadata edits, locks, item preferences, and artwork selections | Activities, notification sockets, and log lines |
| Match/Unmatch, merges, splits, deleted items and media versions | Random-shelf seeds and match candidates |
| Playlists, collections, the tag table, and their id counters | The resource graph and connection table |
| Play history and view counts (Top/popular shelves) | The catalog input, which the server never rewrites |
| Created, renamed, and deleted sections; section and show settings | |
| Server preferences (including the server name) and agent settings | |
| Uploaded posters, art and subtitles; minted theme rows | |
| The fuzzy-search vocabulary as it stood (a real PMS rebuilds it at optimize, not at start) | |
| Play and download queues | |
| The device table and bandwidth/play statistics | |

The state belongs to the catalog it was saved over. When the catalog changes,
its rows and keys can no longer be overlaid, so only each row's view state and
ratings (matched by guid, then by part file) and the server preferences, agent
settings, device table, and statistics carry over. Edits, lists, history, and
library changes stay in a copy of the previous database, kept as `<file>.stale`. An
unreadable file is kept as `<file>.corrupt` and the server starts fresh; the file
admits only plain data, so it cannot name code to run.

A library its sources pushed is the state's own, and is kept row by row whatever the
catalog file says: a changed catalog file then only adds its new rows. Held items are
kept with it.

The catalog can be a database too. `build_catalog.py --seed --out simplex.db` writes one,
and `build_catalog.py --convert catalog.json --out simplex.db` moves an existing JSON
catalog into one. Run with `--catalog simplex.db`: both servers read it, it serves
exactly what its JSON source serves, and the native state then lives in the same
file. A database records the digest of the JSON it came from, so a state saved over
`catalog.json` carries over unchanged. Rebuilding the catalog into the same database
replaces only the catalog's rows; the state is set aside as above at the next start.

A save looks only at what changed: the catalog tracks the rows a request writes, and
the spelling vocabulary is rewritten only when it is rebuilt. Measured on zen with a
20,000-row catalog, a rating write (saved before it is answered) took a median 2.3 ms
and wrote about 100 KB, where the JSON file took 2.7 s and rewrote 126 MB; at 200 and
2,000 rows it took 4.4 and 4.7 ms. The library is still held in memory while serving,
and a first save after a start, or after rows are added, removed or reordered, looks
at every row once. The database is larger than the JSON was (169 MB against 125 MB
at 20,000 rows).

Restored rows keep the shape they had when the file was first written. A later
change to the load-time row or media rules reaches them only after the file is
removed or the catalog is rebuilt, because re-running those rules would undo user
edits and locks.

Sources: [store.rs](rust/src/store.rs), [catalogdb.py](catalogdb.py),
[persist.py](persist.py), [persistence checks](tests/test_persist.py),
[database checks](tests/test_sqlite_store.py).

## Configuration requirements

| Capability | Requirement |
|---|---|
| Stream media | A source: `ZURG_URL` (with `ZURG_USER`/`ZURG_PASSWORD` when zurg sets a username; bytes at `$ZURG_URL/dav`) or a `--sources` file |
| Build the library from zurg | The native server, the source `zurg` with a feed token (`SIMPLEX_FEED_TOKEN`, or `token` in `--sources`), library `Location`s under zurg's mount (`--zurg-mount`, `build_catalog.py --seed`), and zurg's `simplex_url` and `simplex_feed_token`; for zurg's matcher also `plex_server_url`, `plex_token` (this server's `--token`, or once claimed a token plex.tv lists for it) and `plex_settings_policy: off` |
| Start and stop servers on demand | `manager.py serve`; each server's keys, catalog and options in its configuration |
| Media details of pushed files | The source's ffprobe (zurg probes every new release once `simplex_url` is set; never an archive member or a Usenet release, which stay held) |
| Identify pushed releases | The item's IMDb or TMDB id, else `PLEX_TOKEN` (Plex's matcher) and `TMDB_KEY`; with neither key and no id a new item keeps its release name, unmatched |
| TMDB enrichment and live match candidates | `TMDB_KEY` |
| Genuine Plex metadata IDs for Android details | `PLEX_TOKEN` (the feed, `build_catalog.py --relink`) |
| Serve Plex Web | An extracted bundle at `--web-root` |
| Enforce request authentication | A real claim, or `--token`/`SIMPLEX_TOKEN` |
| Report claimed state for isolated tests | `--assert-claimed`; this does not perform a real claim or raise the gate |
| Claim the server and register it with plex.tv | `POST /myplex/claim?token=` with a claim token from `plex.tv/api/claim/token.json`, made before `--announce-url` is set |
| Announce a public endpoint to plex.tv | `--announce-url` (comma-separated for several), `PLEX_TOKEN`, and a reachable configured endpoint |
| Proxy media | `--part-mode proxy`, the only mode on the native server |
| The UI suite with no skips | `TMDB_KEY` (the two Fix Match checks), and a source serving `catalog_ui.json`'s Part files under its mount (`ZURG_URL`, `--zurg-mount`; Download) |
| Answer LAN discovery | `--gdm on`, or `auto` when serving on port 32400 |
| Serve HTTPS | `--tls-cert` and the corresponding key via `--tls-key` or the certificate file |
| Keep library changes across restarts | On by default (`simplex.db` beside the catalog, or the catalog's own database); `--state FILE` or `SIMPLEX_STATE` to move it, `--no-state` to turn it off |
| Keep the catalog in a database | `build_catalog.py --seed --out simplex.db`, or `--convert catalog.json --out simplex.db`; then `--catalog simplex.db` |
| Import owner cloud watch/rating state | Real claim, account consent, and `SIMPLEX_SCROBBLING_DECRYPT_SECRET_B64` service-token setup |

Plex and zurg credentials serve different purposes. Possessing a Plex
token for catalog identification does not by itself claim or publish the server.
These are supported configurations, not assertions that the live instance has
them enabled.

## Known limits and unavailable capabilities

| Area | Current boundary |
|---|---|
| Transcoding/remuxing | No video/audio conversion, bitrate reduction, subtitle burning, or transcoded streaming fallback. An item the client cannot direct play does not play at all; the client reports that the server "is not powerful enough to convert video" |
| Media details | zurg never probes an archive member or a Usenet release, so it pushes them with no `media_info` and simplex holds them, unshown, until a probe arrives. A row the old scanner added before its probe still carries no codec, resolution or streams until a push brings one |
| Persistence | On by default; with `--no-state`, watch state, edits, lists, library changes, uploads, preferences, and statistics last until restart. With it on, a catalog rebuild keeps only view state and server-level settings. There is no PMS database, backup format, or export. Claim state and metadata caches have separate persistent files |
| Acquisition | No built-in torrent search or magnet acquisition: new media arrives through zurg |
| Scanning/analysis | Neither server scans anything: the native server's library is what its sources push, and the Python oracle reads parts from zurg but has no feed. No media-analysis pipeline of its own (the source's ffprobe), generated thumbnails, intro/credits detection, or actual PMS database backups. A row is its (source, id): zurg's id is the file's path, so a renamed release is trashed under its old key and added again on a new one, and its watch state, edits and list entries stay with the old row. The feed does not stack `cd1`/`cd2` files into one Media |
| plex.tv reachability | plex.tv's reachability verdict stays 0: it tests the server's public address and mapped port, which simplex does not report, not the announced connections. Clients still use an announced connection whatever the verdict. A restart that resubscribes within seconds of the old stream closing was listed absent for minutes; one that waited 16-20 s stayed present |
| Claimed-server tokens | The transient tokens `/security/token` mints are not admitted back by a claimed server's gate; there is no allowed-networks fallback |
| Subtitle delivery | Embedded extraction is unimplemented; uploaded subtitles are served, embedded tracks are not |
| Mobile offline downloads | Original-file downloading works; the mobile queue/add/media/decision workflow is incomplete |
| Multi-user operation | No complete sharing, managed-user, permission-isolation, or independent per-user watch-state system; a claimed server admits the tokens plex.tv lists for shared users and serves each of them as the owner |
| Music/photos | A music section can be created and answers as an empty music library would; there is no music or photo ingestion, so neither ever holds items |
| Live TV/DVR | No tuner, EPG, live-TV streaming, or recording backend |
| Remote-player control | No complete Plex Companion player discovery/control implementation |
| DLNA | A library listing route exists; it is not a complete DLNA service |
| Media optimization | No optimized-version generation or background media conversion |
| Plugins and updates | No general plugin installation/execution or self-updating PMS backend |
| Optional content | Trailers, reviews, extras, chapters, and similar data depend on supplied catalog/provider content; they are not all acquired or generated automatically |
| Images | No general resizing/compositing/pixel-analysis engine; supported artwork responses use available assets, variants, and stored palettes |
| Client coverage | Measured Web and Android coverage does not certify every Plex client/device; iOS and the Android TV UI remain separate coverage opportunities |
| Cloud behavior | Watch/rating import is not full bidirectional synchronization; cloud-owned account/watchlist/device features are not implemented merely because the client displays them |

## Recorded validation

These are the latest recorded results, not tests rerun when this document was
written and not a live deployment health check.

| Check | Recorded result |
|---|---|
| UI surface (2026-10-10, zen, keyless and source-less, the feed build) | 1,975/1,975 on both servers, 3 skips |
| Acceptance (2026-10-10, zen, keyless and source-less, the feed build) | Every suite passes on both servers: navigation 29/29, server log 74/74, `--state` persistence 27/27, parity persistence 55/55, plex.tv 65/65 against the local stub (the native batch run had one claim-log timing miss, 64/65; two re-runs of this build and of the pre-feed build were 65/65), part bytes 13/13, CLI 12/12, every parity and follow-up suite; native only: the feed 57/57 against a stub source, the SQLite store 13/13, the phase-2 module comparison 379 checks, manager 53/53, smoke 15/15, session reaper 5/5; `cargo test` 43. Concurrency is 47/49 on this build and on the pre-feed one alike: zen answers a quiet `/identity` in about 45 ms, past the suite's 5 ms median and 0.1 s for 50 reads |
| Feed end to end (2026-10-10, zen, scratch zurg `feat/simplex-feed` on the RD test account, read-only) | The startup snapshot filled an empty library with 118 rows in 19 s; a widened directory filter pushed 3 items 3 s later, held until a probe; a probe showed one; a late IMDb id re-identified a row on its key; a Range read matched zurg's own bytes |
| Migration dry run (2026-10-10, a read-only copy of `~/simplex-e2e`'s database) | zurg's first snapshot adopted all 118 scanned files: 184/184 rows kept their keys, nothing created or trashed |
| UI surface (2026-09-30, parts still Real-Debrid links) | 1,979/1,979 with a TMDB key and a real RD token, no skips; 1,975/1,975 without them, 3 skips |
| Navigation (cycle 95, live account catalog, real RD) | 25/25 on both servers |
| python-plexapi conformance (cycle 95) | 38/38 on both servers |
| Playback/byte/decode suite (cycle 95, real RD) | 5/5 on both servers |
| `--state` persistence (`test_persist.py`, cycle 92) | 27/27 on both servers, default-state checks included |
| Cycle 95 compatibility repeat | Zero client-visible and zero malformed-only findings across all 12 audit axes, against the native server, under Plex Web 4.147.1 and 4.162.1, Plex for Android 2026.19.1 and python-plexapi 4.18.3 |
| Live trial (2026-10-10, zen, claimed, in front of a scratch zurg) | Registered on plex.tv, present and owned with its announced connection for 47 polls over 48 minutes; app.plex.tv direct-played a movie over it |

Without credentials the UI suite skips exactly three checks: the two Fix Match
checks, which need `TMDB_KEY`, and Download, which needs a zurg serving
`catalog_ui.json`'s Part files (`ZURG_URL`, `--zurg-mount`; a copy made on the test
host, never committed). With no `ZURG_URL` its part read is the routed 502 and the
check skips.

The twelve axes are the eleven audit axes of cycle 72 plus, from cycle 73, what a
client sees across a server restart under `--state`. The axis ledger is in
[HANDOVER.md](HANDOVER.md).

The 12-axis stopping criterion applies to the defined audit coverage. It does
not remove the limitations above or establish parity with every PMS version.

Evidence: [current status and coverage](HANDOVER.md),
[cycle 95 record](handover/cycle95-report.md), [the zurg scan and plex.tv
measurements](docs/findings.md),
[test suites](tests/), [reference harness](harness/README.md).

## Implementation

The server is the native Rust binary built from `rust/`. The Python server
(`server.py` and the modules it imports, linked as sources above) stays in the
tree as the measured oracle the native server is ported from; the black-box
suites run against either (`SIMPLEX_SERVER=python` selects the oracle). The last
Python-only `main` is the `python-server` branch.

On 2026-09-30, when parts were still Real-Debrid links, the native server matched the
oracle check for check on the UI surface (1,979/1,979 with real RD and TMDB
credentials, 1,975/1,975 with 3 skips without), and passed python-plexapi
conformance 38/38, the real-media playback/decode suite 5/5, the plex.tv suite 47/47
(claim, announce, account import and a restart, against a local plex.tv stub),
navigation 29/29, `--state` persistence 17/17, and the TLS, GDM, prefix-registry,
regional-preference and part-byte suites. Cycles 83-95 then ran every audit axis
against it (above).

On 2026-10-10 it ran as a claimed, registered instance on zen in front of a scratch
zurg: plex.tv created its server record and listed it present and owned with its
announced connection past the 40-minute device lease, and app.plex.tv on the Windows
browser browsed it and direct-played a movie over that connection (`location=wan`,
ranged 206 part reads, 1920x1080). See findings, "The experiment, run".

Known native differences from the oracle: the native state is a SQLite database
(`simplex.db`) and does not read the oracle's pickle (`state.pickle`), and only the
native server scans zurg and identifies what it finds; the oracle reads parts from
zurg the same way, and its refresh works over the catalog it was started with.

## Keeping this inventory current

When behavior changes, update the affected capability, its prerequisites and
limits, and the validation baseline together. Distinguish a working feature from
an accepted compatibility request or descriptor. Keep recorded test results
separate from fresh test runs and live deployment checks. The underlying
measurements remain in [docs/findings.md](docs/findings.md) and
[docs/protocol-traps.md](docs/protocol-traps.md).
