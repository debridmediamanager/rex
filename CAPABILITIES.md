# plexsim capabilities

This is the feature inventory of the current movie/TV server, based on the code
and recorded measurements through **cycle 95** (reopen triggers re-checked
2026-10-06, none fired). It describes
implemented behavior and its prerequisites, not the current health or
configuration of a deployed instance.

plexsim presents a Plex library backed by Real-Debrid links and streams the
original media without keeping complete media files locally. Its largest limit
is **direct play only**. Library changes last across restarts: a state file
beside the catalog is kept by default, and `--no-state` turns that off.

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

## Client access and discovery

- Serve an extracted official Plex Web bundle at `/web` and answer the requests
  used by its library and administration screens. The bundled build is 4.147.1;
  the hosted 4.162.1 build can be laid out for `--web-root` and is covered too.
- Support the measured native Plex for Android (2026.19.1) browsing, detail,
  playback-state, history, post-play, server-dashboard, history-filter and Top
  Played request flows, and keep its notification socket open. Its detail pages require genuine Plex
  metadata IDs; the catalog builder can obtain them.
- Support the tested `python-plexapi` read, write, and playback workflows.
- Answer Plex LAN discovery through GDM on UDP ports 32410 and 32414.
- Expose server identity, library providers, capabilities, client profiles, and
  server/account information in the forms Plex clients expect.
- Redeem a Plex claim and retain claim credentials across restarts.
- Register a configured public URL with plex.tv and renew that registration.
- Operate behind a configured public tunnel or reverse proxy.
- Serve HTTP and, with a supplied certificate/key, HTTPS on the same port.
- Require a configured Plex token for protected requests. Authentication is
  optional and is off unless configured; public bootstrap routes remain
  accessible according to the measured protocol.

Sources: [server.py](server.py), [gdm.py](gdm.py), [claim.py](claim.py),
[heartbeat.py](heartbeat.py), [Android harness](harness/android/README.md).

## Catalog creation and metadata

- Build `catalog.json` from existing restricted RD links or links belonging to
  specified RD torrent IDs.
- Resolve playable CDN URLs on demand and cache them with an expiry.
- Probe remote media using `ffprobe` without storing the complete file locally.
- Derive real container, duration, bitrate, resolution, frame-rate, codec,
  audio-channel, language, and subtitle-stream information from the probe.
- Recognize stacked multipart filenames and order their parts numerically.
- Skip archive-backed content and files without a video stream during import.
- Build separate movie and television sections, with synthesized show and
  season parents, episode ordering, child counts, and leaf counts.
- Represent multiple media versions and parts where supplied by the catalog.
- Enrich titles, episode names, summaries, release information, ratings, genres,
  people, posters, and backgrounds using TMDB.
- Obtain genuine Plex IDs from Plex's metadata provider when configured, including
  relinking an existing catalog with `build_catalog.py --relink`.
- Cache metadata locally and retain a stable server identity in the catalog.
- Search match candidates and support Match, Fix Match, and Unmatch. Candidate
  searches and TMDB re-identification require a configured TMDB key.
- Inspect likely direct-play compatibility using the `gate.py` helper; its
  client-class checks are advisory, not universal device certification.

Sources: [build_catalog.py](build_catalog.py), [metadata.py](metadata.py),
[mediainfo.py](mediainfo.py), [hierarchy.py](hierarchy.py), [debrid.py](debrid.py),
[gate.py](gate.py).

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

- Direct-play original media by proxying RD CDN bytes through plexsim or
  redirecting the client to the CDN.
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
- Provider availability checks and distinct responses for unavailable content
  versus upstream transport failures.

Playback requires the client to support the original container, codecs,
bitrate, and selected tracks. There is no transcoding or remuxing fallback.
Original-file downloads are separate from the incomplete mobile offline-download
workflow. Subtitle selection does not imply subtitle extraction or delivery.

Sources: [server.py](server.py), [decision.py](decision.py), [debrid.py](debrid.py),
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
across restarts); it does not delete the RD content or rewrite `catalog.json`. Edit support follows the measured Plex
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
otherwise process-local. Live sessions never outlive the process. Redirected CDN traffic is outside plexsim's byte
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

These controls have varying depth. Scans work over the existing catalog rather
than discovering new RD content. Refresh/Analyze and many maintenance controls
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

The server saves what clients change to one file and restores it at start: by
default `state.json` beside the catalog (the Python server's is `state.pickle`), or
`--state FILE` / `PLEXSIM_STATE`. A client write (any non-GET request, and the
scrobble, rate, progress and timeline GETs), and a client's first request or a
changed device name or platform, is on disk before it is answered, so a
crash loses nothing a client was told succeeded -- a real PMS keeps writes answered a
fraction of a second before a hard kill. Everything else is saved within about two
seconds, every minute regardless, and on SIGTERM or Ctrl-C. `--no-state` (or an empty
`PLEXSIM_STATE`) turns it off, which is how the test suites run: they rely on a restart
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
| Play history and view counts (Top/popular shelves) | The `catalog.json` input, which is never rewritten |
| Created, renamed, and deleted sections; section and show settings | |
| Server preferences (including the server name) and agent settings | |
| Uploaded posters, art and subtitles; minted theme rows | |
| The fuzzy-search vocabulary as it stood (a real PMS rebuilds it at optimize, not at start) | |
| Play and download queues | |
| The device table and bandwidth/play statistics | |

The file belongs to the catalog it was saved over. When `catalog.json` changes,
its rows and keys can no longer be overlaid, so only each row's view state and
ratings (matched by guid, then by part file) and the server preferences, agent
settings, device table, and statistics carry over. Edits, lists, history, and
library changes stay in the previous file, kept as `<file>.stale`. An unreadable
file is kept as `<file>.corrupt` and the server starts fresh; the file admits only
plain data, so it cannot name code to run.

Restored rows keep the shape they had when the file was first written. A later
change to the load-time row or media rules reaches them only after the file is
removed or the catalog is rebuilt, because re-running those rules would undo user
edits and locks.

Sources: [persist.py](persist.py), [persistence checks](tests/test_persist.py).

## Configuration requirements

| Capability | Requirement |
|---|---|
| Start the server and resolve RD content | `RD_TOKEN` and a suitable catalog |
| Probe media while building the catalog | `ffprobe` available to the builder |
| TMDB enrichment and live match candidates | `TMDB_KEY` (or the builder's `--tmdb-key`) |
| Genuine Plex metadata IDs for Android details | `PLEX_TOKEN` available to the catalog builder/relink step |
| Serve Plex Web | An extracted bundle at `--web-root` |
| Enforce request authentication | `--token` or `PLEXSIM_TOKEN` |
| Report claimed state for isolated tests | `--assert-claimed`; this does not perform a real claim |
| Register a public endpoint with plex.tv | `--announce-url`, `PLEX_TOKEN`, and a reachable configured endpoint |
| Proxy or redirect media | `--part-mode proxy` or `--part-mode redirect` |
| Answer LAN discovery | `--gdm on`, or `auto` when serving on port 32400 |
| Serve HTTPS | `--tls-cert` and the corresponding key via `--tls-key` or the certificate file |
| Keep library changes across restarts | On by default (`state.json` beside the catalog); `--state FILE` or `PLEXSIM_STATE` to move it, `--no-state` to turn it off |
| Import owner cloud watch/rating state | Real claim, account consent, and `PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64` service-token setup |

Plex and RD account credentials serve different purposes. Possessing a Plex
token for catalog identification does not by itself claim or publish the server.
These are supported configurations, not assertions that the live instance has
them enabled.

## Known limits and unavailable capabilities

| Area | Current boundary |
|---|---|
| Transcoding/remuxing | No video/audio conversion, bitrate reduction, subtitle burning, or transcoded streaming fallback |
| Persistence | On by default; with `--no-state`, watch state, edits, lists, library changes, uploads, preferences, and statistics last until restart. With it on, a catalog rebuild keeps only view state and server-level settings. There is no PMS database, backup format, or export. Claim state and metadata caches have separate persistent files |
| Acquisition | No built-in torrent search, magnet acquisition, continuous RD-library synchronization, or automatic discovery of new media |
| Scanning/analysis | No complete filesystem scanner, media-analysis pipeline, generated thumbnails, intro/credits detection, or actual PMS database backups |
| Subtitle delivery | Embedded extraction is unimplemented; uploaded subtitles are served, embedded tracks are not |
| Mobile offline downloads | Original-file downloading works; the mobile queue/add/media/decision workflow is incomplete |
| Multi-user operation | No complete sharing, managed-user, permission-isolation, or independent per-user watch-state system |
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
| UI surface (2026-09-30) | 1,979/1,979 with a TMDB key and a real RD token, no skips; 1,975/1,975 without them, 3 skips |
| Navigation (cycle 95, live account catalog, real RD) | 25/25 on both servers |
| python-plexapi conformance (cycle 95) | 38/38 on both servers |
| Playback/byte/decode suite (cycle 95) | 5/5 on both servers |
| `--state` persistence (`test_persist.py`, cycle 92) | 27/27 on both servers, default-state checks included |
| Cycle 95 compatibility repeat | Zero client-visible and zero malformed-only findings across all 12 audit axes, against the native server, under Plex Web 4.147.1 and 4.162.1, Plex for Android 2026.19.1 and python-plexapi 4.18.3 |

Without credentials the UI suite skips exactly three checks: the two Fix Match
checks, which need `TMDB_KEY`, and Download, which needs a real `RD_TOKEN` and a
catalog whose links that account owns.

The twelve axes are the eleven audit axes of cycle 72 plus, from cycle 73, what a
client sees across a server restart under `--state`. The axis ledger is in
[HANDOVER.md](HANDOVER.md).

The 12-axis stopping criterion applies to the defined audit coverage. It does
not remove the limitations above or establish parity with every PMS version.

Evidence: [current status and coverage](HANDOVER.md),
[cycle 95 record](handover/cycle95-report.md),
[test suites](tests/), [reference harness](harness/README.md).

## Implementation

The server is the native Rust binary built from `rust/`. The Python server
(`server.py` and the modules it imports, linked as sources above) stays in the
tree as the measured oracle the native server is ported from; the black-box
suites run against either (`PLEXSIM_SERVER=python` selects the oracle). The last
Python-only `main` is the `python-server` branch.

On 2026-09-30 the native server matched the oracle check for check on the UI
surface (1,979/1,979 with real RD and TMDB credentials, 1,975/1,975 with 3 skips
without), and passed python-plexapi conformance 38/38, the real-media
playback/decode suite 5/5, the plex.tv suite 47/47 (claim, announce, account
import and a restart, against a local plex.tv stub), navigation 29/29, `--state`
persistence 17/17, and the TLS, GDM, prefix-registry, regional-preference and
part-byte suites. Cycles 83-95 then ran every audit axis against it (above). It has
not yet run as the deployed instance: that trial needs a claimed test identity on
plex.tv.

Known native differences from the oracle: the native state file is JSON
(`state.json`) and does not read the oracle's pickle (`state.pickle`), and a
refused CDN URL is dropped at once where the oracle keeps it for its expiry (the
same answer, one fewer dead read).

## Keeping this inventory current

When behavior changes, update the affected capability, its prerequisites and
limits, and the validation baseline together. Distinguish a working feature from
an accepted compatibility request or descriptor. Keep recorded test results
separate from fresh test runs and live deployment checks. The underlying
measurements remain in [docs/findings.md](docs/findings.md) and
[docs/protocol-traps.md](docs/protocol-traps.md).
