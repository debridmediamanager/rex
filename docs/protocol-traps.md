# Plex protocol and implementation traps

These are measured edge cases and implementation lessons formerly in `CLAUDE.md`.
Use them alongside [`findings.md`](findings.md), which holds the capture evidence
and chronological investigations. Search this file for the endpoint or field you
are changing; replay the client's exact request before altering behavior.

## Traps that cost real time

- **Loudness on a video's audio stream is version-scoped.** 1.43.4 writes
  `canNormalizeLoudness`, `gainOffset`, `loudness`, `loudnessAnalysisVersion`, `lra`, `peak` and
  `threshold` on every video audio stream; 1.42.2 writes none and its binary has no
  `canNormalizeLoudness`. python-plexapi 4.18.3 reads the flag. Emulating 1.42.2, leave them out.
- **A copied rendered walk can name the wrong browser.** Cycle 81's `windows81/*.py` drove kids'
  9224 while its report said Windows. Read `drv.CDP` (and the user agent at `/json/version`)
  before calling a walk "Windows"; `cycle_mk_next.sh` now refuses 9224.
- **A raw-socket check that expects a close must accept a reset.** Late bytes that reach a socket
  the server already closed are answered with RST, so `recv` raises instead of returning `b""`.
- **A comparison's completion marker is not evidence that it sent a request.**
  Cycle 80's C, P and B wrappers each reported a nominal completion path while
  stopping first on C's two missing captured body sets, P's missing R0 identity
  manifest or B's nonexistent subtitle fixture. Cycle 81 repaired those inputs
  from measured captures and the isolated scratch PMS; check the request
  denominator and the source fixture before calling a leg clean.
- **`catalog.json` links are account-scoped.** Real-Debrid `/d/` links resolve
  only for the account that created them. A different key answers
  `404 unavailable_file`, which surfaces as HTTP 502 on every part read and
  looks like plexsim is broken.
- **Cloudflare strips `Range` on a prefix request.** `--part-mode proxy` then
  sees `range=-` and fetches the whole file per playback start. Use
  `--part-mode redirect` through the edge.
- **Cloudflare 403s a `Python-urllib/*` User-Agent** on the media path. curl,
  a Plex product string, a browser string and even an empty UA all get 206. A
  harness that leaves urllib's default measures the bot filter, not plexsim.
- **`Connection: close` on part responses is load-bearing.** With keep-alive,
  ffmpeg reuses a connection behind an undrained body and seeking fails with
  `AVERROR(ENOSYS)`.
- **The Edit dialog saves to `PUT /library/sections/{id}/all`**, reads
  `/library/sections/{id}/common` first, and gets its editable TAG fields from
  `/library/sections/{id}?includeDetails=1`. plexsim answered the grid listing to
  the save (200, nothing written), 404'd the pre-read, and sent two pivot rows and
  no `Type` — so every Save was a silent no-op and the Tags pane was empty. With no
  `id=` the save edits the whole FILTERED set, and a bare `field=value` there is a
  filter that SCOPES it where on the item path it is an edit.
- **An indexed tag write REPLACES the list on the ITEM path** (`genre[0]…&genre[1]…`
  leaves exactly those two), and ANY `tag-` removal there clears the whole list
  whatever it names. **The section save path MERGES**: the indexed list goes first,
  the row's other tags follow, and a removal removes only what it names. The INDEX
  orders the list, not the parameter's position. A tag matches case-insensitively and
  keeps its canonical spelling and id even after being removed from every row — a
  real PMS keeps a tag TABLE, so plexsim does too.
- **Edit parameters are validated**: an unknown field or tag type is 400, an empty
  or unparseable integer is 500, a negative year or a bad date REMOVES the field,
  only `title`/`summary` store `""`, `userRating` is accepted and discarded, and
  only `.locked=1` locks. Library create and edit validate their whole form.
  That is the ITEM path's grammar. **The section save path differs**: a date it
  cannot read is a 500, a date also sets the year (an empty one clears both), and an
  empty year is removed rather than refused. Both parse a date the same way --
  `2007-02-30` rolls over, a bare integer is epoch seconds -- and 0 removes a year.
  The section path also STORES `userRating` (stamping `lastRatedAt`), and every
  rating is a number: an empty one is a 500 on the item path and removed on the
  section path. A lock there is an integer 0 or 1 (`01` locks, `true` is a 400), and
  `thumb`, `art` and the other artwork and identity columns take a lock on both paths.
- **The UI draws a menu entry from what the server *advertises*, not from
  whether the write behind it works.** `/media/providers` decides whether Edit,
  Delete, Download, Optimize and Mark as Played exist at all; an unimplemented
  endpoint behind an advertised capability is a menu entry that does nothing.
  The per-entry gates are in `docs/findings.md`, read out of the client's own
  bundle (`main-*.js` module 92273).
- **A mutating endpoint answers 200 with an EMPTY body**, not a MediaContainer —
  and a playlist delete is 204 while a collection delete is 200.
- **A smart playlist or collection is a stored QUERY, evaluated on every read** —
  proved by scrobbling one movie and watching it leave an `?unwatched=1` smart
  playlist. Storing the matching rows makes a list that is right once and stale
  after. The two endpoints disagree on everything else: a playlist's `smart` is a
  boolean and carries `icon: "playlist://image.smart"`, a collection's is the
  string `"1"` and is OMITTED when not smart; a playlist rewrites the `server://`
  URI into `library://x/directory/<encoded>` and shows `content` only on the
  single read, a collection keeps the URI verbatim and shows it in the list too;
  and a smart collection does **not** tag its members.
- **An unrouted path answers 404**, the way a real PMS does. The catch-all used to
  return an empty container, which is fix 4's trap from the other side: a client
  reads a 200 as "this endpoint exists and is empty". Flipping it is what finally
  surfaced `GET /library`, which python-plexapi asks for first and which had
  never been routed. If something breaks after adding an endpoint, look here.
- **`GET /` is NOT `/identity`.** It answers the whole capability blob plus 25
  `Directory` rows naming the server's top-level endpoints — which is where
  python-plexapi reads every attribute it has. `/identity` stays five fields.
- **`/library/metadata/{id}/tree` NESTS** — a show holds its seasons, each season
  its episodes — and an EPISODE's tree is rooted at the show. A `MediaItem` there
  carries only its `id`.
- **A show's `/children` carries an "All episodes" `Directory` row** and the
  container's `size` counts it; `excludeAllLeaves=1` removes both. `Guid` is on a
  detail row always and on a LISTING row only under `includeGuids=1`.
- **Two responses on the surface have a root that is not a MediaContainer** —
  `/butler` answers `ButlerTasks` and `/library/sections/{id}/query/parse`
  answers `Query`. Neither is guessable; both were measured.
- **The `include*`/`exclude*` options each DO something**, and ignoring one answers
  a valid document with a block missing. `includeChildren` inlines a show's
  seasons, `excludeElements=Media` substitutes `mediaCount`, and the `async*` pair
  makes the CONTAINER drop `identifier` and the media-tag pair. `includePreferences`
  is per TYPE — a movie has 3, a show 11, a season 3, an episode none, and the
  block is omitted rather than sent empty.
- **`group=` collapses to one row per distinct value and 500s for most fields** —
  a tag, the media and per-user state are all refused, and so are `duration` and
  `userRating`. `group=decade` groups on the YEAR column, so it does not collapse
  by decade. (Cycle 14: WHICH row survives is the group's lowest `addedAt`, picked
  BEFORE the sort, and a type-prefixed key is scoped. See the bullet below.)
- **The decision container sends ONE pair of codes.** A direct play is only
  `mdeDecisionCode: 1000` / `"Direct play OK."`; the general/directPlay/transcode
  triples appear only when direct play is REFUSED. Sending all of them says a
  transcode decision was made when none was. `resourceSession` is the X-Plex-Session-Identifier (then the client
  identifier, then `""`), and `allowSync`, `librarySectionID` and `mediaTagVersion` are all
  STRINGS here.
- **`/hubs/promoted` is called ONCE PER PINNED LIBRARY** (`contentDirectoryID=`),
  and every shelf carries `key`, `context` (the identifier with a trailing section
  id stripped), `style` and `promoted`. `hubKey` is not a listing path — it is
  `/library/metadata/<comma-joined ratingKeys>` naming that shelf's own rows. And
  `/hubs/home/recentlyAdded` keys on `type`, NOT on `sectionID`.
- **The preplay page's Related shelf is `/hubs/metadata/{id}/related`**, a
  different endpoint from `/library/metadata/{id}/related` — the client rewrites
  the path. `related` carries the librarySection triple, `postplay` carries none of
  it, and a 404 on either reads to the page as "no related items".
- **Search is seventeen typed hubs on EVERY query**, empty ones included, because
  the client lays the page out from that list; each row carries a `score` (a string
  at `/hubs/search`, a float at `/library/search`) and a tag-matched row names the
  tag in `reason`/`reasonID`/`reasonTitle`. Matching is a word **PREFIX**: `uck`
  does not find "Big Buck Bunny" and one character finds nothing.
- **`/player/*` is not a server surface.** A real PMS answers 400 for every
  playback command: they go to a player. Answering 200 tells a companion client
  the command was accepted.
- **A play queue over a show or season holds its EPISODES**, never the parent row —
  which has no Media, so Play on every show page hands the player something
  unplayable. Same for a show library's section, a playlist, a collection and a
  comma-joined key list.
- **A queue's item ids are GLOBAL and stable and its version steps on every
  mutation.** The client DISCARDS any queue response whose `playQueueSelectedItemID`
  is not the one it holds, and the selection moves from the TIMELINE's
  `playQueueItemID` — so ids computed from position plus a frozen version means
  every refresh is thrown away once playback passes the first item.
  `playQueueLastAddedItemID` tracks plain adds only and disables the Up Next
  shuffle button. `shuffle`/`unshuffle` are 404 ONCE an item has been added (cycle 75: on a
  queue nothing was added to they shuffle and restore it); `items/{id}/move` is real.
- **`/:/progress` is NOT `/:/timeline`** — it keys on `key`, not `ratingKey`, and
  answers an EMPTY body. Routed at the timeline it silently records nothing.
- **The timeline's `viewOffset` is the item's STORED offset**, -1 only when it has
  none, and `Bandwidths` is armed by a DECISION (then present even on `stopped`),
  not by the state. An unknown `state` is 400 and an unknown ratingKey is 404.
- **Every state write refuses properly**: scrobble/unscrobble/rate/progress are 400
  with no `key` and 404 for a key that is not there, and `PUT /library/parts/{id}`
  is 400 for a stream id of the wrong type — answering 200 and clearing the
  selection leaves the track picker blank.
- **`/status/sessions/history/all` filters and sorts**, with a plain `>=` operator
  rather than the library grammar's `>>`, and an unknown sort key is 400 there
  where a listing answers 500. `DELETE .../history/{key}` really deletes.
- **A playback session is created by `/:/timeline`**, not by a play queue, a
  decision or a byte read — a bare timeline opens the row and `state=stopped`
  closes it. The timeline's RESPONSE is not an echo: `playbackState` is
  `ignore`/`progress`/`complete` and `viewOffset` is `-1`.
- **Terminate is keyed on `Session.id`** and does not end anything by itself: the
  kill arrives in that session's next timeline as `terminationCode: 2006`, and the
  next part read is a 503.
- **The notification socket has to carry frames, not just pings.** A real PMS
  pushes `activity`, `timeline`, `status`, `progress` and `playing`; a client that gets none
  of them polls instead and never sees a change made elsewhere. Server frames are
  **unmasked**, and a malformed one puts the client back in fix 4's failure mode.
- **A filter's operator is part of the parameter NAME, not the value.** The client
  sends `year%3E%3E=2000`, so the server reads a parameter called `year>>`. A
  server that looks for names it knows sees nothing and filters nothing — which is
  what plexsim did for its whole life, while advertising 40 filterable fields.
  A bare `=` on a string CONTAINS -- except on `title`, where each query word must
  BEGIN a title word in order, and on `titleSort`/`summary`/`tagline`, where it is
  equality. `==` is the exact one, and it ignores case. See `filters.py`. (Cycle 14:
  the six-operator restriction is the ADVERTISED FieldType's, so the unlisted
  `titleSort`/`summary`/`tagline`/`originalTitle` take the ordering keys as well.)
- **What a bare `=` means depends on the field's advertised TYPE, and three types
  are not strings.** A `tag` field is exact (`contentRating=G` does not match
  `PG`) and an EMPTY value on one means "has no value at all"; a `boolean` is true
  only for the literal `1`, so `unwatched=2` means false; `resolution` is an enum
  and a value outside `{4k,1080,720,576,480,sd}` is a 500; `location` is a section
  Location **id**, not a path. An empty value is a 500 only where it has to parse
  as a number or a date, not everywhere.
- **Which operator KEYS a field takes is not its advertised operator list.**
  `resolution>>=720` parses although only `=` is advertised. What is closed is
  tag-TABLE fields (`=`/`!=` only) and string fields (their six); anything else on
  one of those is an unknown FIELD, ignored by a listing and reported as an error
  by `/query/parse`.
- **A show library advertises its filter fields PREFIXED** (`show.genre`,
  `episode.title`) and the client sends the advertised key back verbatim. plexsim
  keyed its field map by those names and looked them up with the prefix stripped,
  so no filter on a show grid ever constrained anything. The prefix is a scope:
  the field is the tail, the TYPE lookup is global, the VALUE is read from the row the
  prefix names (`type=4&show.title=` tests the show, and a scope below the row matches
  when any child does), and `/query/parse` echoes the scope back as the rule's own `type`.
- **`/filters` is per library TYPE too** — a movie library lists 22, a show library
  a different 15 whose keys carry `?type=2` (except `country`, `unmatched` and
  `editionTitle`). (Cycle 14: 22 is **1.42.2's**, the build plexsim reports; 1.43.4
  answers 28. See the per-version bullet below.) The type SCOPES an axis, so `/genre?type=2` on a movie library
  is empty. Seven of the advertised axes **404 on a real PMS as well**, so
  advertising them is the faithful answer, not the bug.
- **A media axis entry has no `fastKey` and its title is a LABEL** — `1080` prints
  as `1080p`, `en` as `English` — while `location` is the one axis whose container
  carries nothing but `size` and `title2`, and `/unwatched` is a LISTING titled
  "Unplayed" rather than an axis at all.
- **Every secondary container of a show library carries `nocache`** and no movie
  library's does. Measured across two servers and five libraries, so it tracks the
  library's type rather than cache state, even though the cause is invisible.
- **Container paging needs BOTH parameters, and the client sends them as HTTP
  HEADERS.** One alone is ignored by a real PMS; a negative size drops paging
  rather than slicing.
- **The DEFAULT listing order is `titleSort`, and every tie falls back to it** —
  catalog order is not the default, so an edited Sort Title has to move the row.
- **`includeAdvanced=1` adds six Sorts to a movie library and eight to an episode
  view**, plus the Duplicates filter — the client reads that list straight into its
  sort menu, and every key it advertises has to work on the listing.
- **`/library/sections/{id}/search` is a SUBSTRING matcher**, one character is enough
  — NOT the word-prefix matcher `/hubs/search` uses.
- **Change Poster writes to the SINGULAR path** (`PUT /library/metadata/{id}/poster`)
  and the response BODY is the new artwork path, which the client assigns onto the
  row. The plural path is a 404 for a write, and serving image bytes from the
  singular one sets the tile's img src to a blob of PNG.
- **A resume point under a minute is not stored**, "Remove from Continue Watching"
  HIDES the row and KEEPS the offset, a scrobble writes NO history row (only a
  completed timeline does), and a show keeps its own `viewCount` that increments
  rather than recomputing.
- **An unknown section id is a 404 on every leaf** — answering a valid empty library
  with a working filter bar is fix 4's trap from the inside. And a real PMS never
  answers 405: every verb a leaf does not implement is a 404.
- **A `sort=` spec is a comma-separated LIST, and so is each term.**
  `random:<seed>` differs per seed, and `titleSort:desc:nocollate` is descending —
  splitting the spec on its first colon drops the secondary key and reading the
  direction off the first colon of a term loses it entirely. An unknown key is a
  500 on a real PMS, not a silently unsorted 200; an unknown MODIFIER is ignored.
- **A real PMS 401s EVERYTHING but `/identity` and `/web/*`** — artwork, media bytes
  and both notification channels included — for a missing token and a wrong one
  alike, with no `WWW-Authenticate`. plexsim had no token check at all, so any peer
  that could reach the port read the whole library. The gate is `--token` /
  `PLEXSIM_TOKEN` and defaults OFF (the suites and probes carry no token); the
  startup line says which. `/security/token` is the one exception: its own 400 with
  no parameters and 403 with a bad token, never a 401.
- **`Cache-Control: no-cache` goes on EVERY response**, and a versioned artwork
  asset is `no-cache` too — caching it for a day makes a Change Artwork invisible to
  the browser. Only transcoder output caches (`max-age=259200`). A real PMS also
  GZIPS containers and names both sizes in `X-Plex-Content-Original-Length` /
  `X-Plex-Content-Compressed-Length`.
- **CORS: echo the Origin and echo the requested header list.** A fixed
  allow-list blocks whatever it forgot, and the browser refuses the request with
  no server-side trace. `*` is invalid on a credentialed request. Plex Web hides
  this by passing identity in the query string rather than in headers.
- **XML is the DEFAULT response format**, not JSON — `_wants_xml()` returns true
  unless a client asks for JSON. A JSON-only test proves nothing about the path a
  TV or mobile client takes: three endpoints were rendering `<Video>` where a real
  PMS renders `<Photo>` or `<Directory>`, because `xmlout` names a `Metadata`
  row's element from the row's `type` and those rows have none.
- **The eventsource channel is a DIFFERENT wire format from the websocket**, for
  the same events: `event: <type>` plus a `data:` line holding one bare row, with
  no NotificationContainer and no size.
- **`displayTitle` is the plain label and `extendedDisplayTitle` carries the codec** --
  not the other way round -- and a track title never enters `displayTitle`. Except that
  twenty-one products since cycle 63 (Plex Web; Plex for iOS, Apple TV, Android (TV), Android (Mobile), LG,
  Samsung, Windows, Mac, Xbox, PlayStation 4, PlayStation 5 and Vizio; and HTPC for Windows,
  Mac and Linux, Linux, Fetch TV, Smart TVs, VIDAA and X1 -- exact names, header or query) get ` (<CODEC>)` in a video's and a subtitle's `displayTitle` too. The
  server adds it on the way out (`_with_client_titles`), never into the catalog. These are
  the strings the quality and track pickers print.
- **`filters=` on the notification channels is a whitelist, and `log` is opt-in.**
  An earlier note here said the Console's log stream is not fed by that socket —
  measured WITHOUT the `filters=log` the client's own log connection passes. With it
  the reference pushes log frames continuously (386 in 14 s). A socket with no
  filters gets everything EXCEPT log; one with `filters=log` gets nothing else.
- **The notification LIFECYCLE is what makes a client refetch.** A refresh pushes
  seven frames ending in `state: 5` with NO `metadataState` — that frame dispatches
  `metadataProcessed`, and the client's reducer clears its entry only when
  `metadataState` is absent. Sending one `state: 3, "queued"` frame means the row is
  never refetched and every `!!metadataState` gate (the spinner, Refresh, Match)
  latches forever. The states are 0 created, 3 queued/processing, 4 loading, 5 done,
  9 deleted; an edit pushes 3 then 5, a delete pushes 9, and the activity must END.
- **An empty child list is omitted, never sent as `[]`.** These responses are XML
  underneath; `Metadata: []` is a shape no real PMS returns.
- **`PUT .../merge?ids=` on a real library is not reversible.** The `split` that
  undoes it hands the second item back on a NEW ratingKey carrying the first
  one's metadata -- guid, `Guid` list and locks included (cycle 83, below) -- so measuring
  it costs a re-match. It cost one here. (Cycle 14: a merge
  whose TARGET has no `Media` -- a season, a show -- absorbs nothing at all. See below.)
- **`viewMode`: a season list is 65593, an episode list 65592.** Inverted, the
  client is handed the episode layout for a season list and draws no seasons.
- **An unknown SINGLE-segment path is a 200** (`content="plugins"`, the legacy
  plug-in dispatcher) where a multi-segment one 404s — so `/prefs`, `/settings`,
  `/login` and `/logout` all answer. Bare `/channels` is real too.
- **Verb routing is per (method, path) and not uniform**: `/identity` and `/hubs`
  answer any verb; `POST /library/sections` and `POST /media/providers` are 400 and
  their PUT/DELETE are 404; every verb on `/status/sessions` but GET is a 404.
- **`/photo/:/transcode` needs BOTH dimensions** (one alone is a 400, not a
  best-effort resize), 400s a non-numeric one and 404s a `url` naming a row it does
  not have — a 404 lets the client draw its own placeholder where a grey 200 tile
  gets cached. `/:/resources/<unknown>` is a 404 too.
- **Artwork URLs must be versioned.** Unversioned `/thumb/1` plus ratingKey
  reuse serves a stale poster — one show wore another's art for a day.
- **Never write to plex.tv's device registry to fake a server.**
  `PUT /devices/{id}` with a **numeric** id resolves to a pre-existing row and
  will silently overwrite it. Repeated claim exchanges with fabricated
  identities reset account-wide tokens and signed real servers out. Read the
  claim section of `docs/findings.md` before touching anything under plex.tv.
  Measured 2026-09-12 in the harness, and correcting that section: a real PMS
  announces with `PUT /devices/{machineIdentifier}` — the **identifier** form —
  carrying its full `<Device>` element and its `plex.direct` connection, and
  repeating on a timer. The numeric form is not what a real server uses.

- **The Add Library form reads `/library/sections/prefs?type=N&agent=ID`** before the
  library exists, and the list depends on BOTH; `?agent=` reshapes an existing
  library's list too. A 400 there stops the whole dialog. See `section_prefs_new.json`.
- **The three settings writers validate differently**, all measured: an unknown
  setting refuses the WHOLE write everywhere; a bad int is a 500 on `/:/prefs`,
  ignored on a library and stored raw on an item; a bool is true only for `1`/`true`;
  and a server setting keeps its TYPE on read-back while a library's is a string.
- **Accept is read exactly**: only a case-sensitive `application/json` or
  `application/xml` range counts, ranked by `q`, first listed on a tie, and any other
  parameter disqualifies it (`application/json; charset=utf-8` is XML).
- **JSON never carries `null` or `[]`** -- `json_body` strips both -- but an empty
  OBJECT is real (`"Related": {}`).
- **`titleSort` is the article-stripped title, sent only when it differs, and
  `originalTitle` is never a filename.** Catalogs built earlier carry both wrong, so
  `hierarchy.normalize_rows` runs at LOAD as well as at build. (Cycle 14 corrected the
  derivation itself: the title is TRIMMED and combining-mark FOLDED before the article
  comes off. See the bullet below.)
- **A grid or hub row is a lighter projection** (`_grid_row`): text-only tags, no
  `Stream`, no `Rating`/`Location`/`Field`. A check that needs streams or tag ids must
  read the DETAIL row.
- **`/system/scanners/{N}` is a library TYPE, not a section id**, and `/devices`
  lists every client with its X-Plex-Device-Name and X-Plex-Platform.
- **`/library/metadata/{id}/related` carries the same hub as the hubs form** -- an
  earlier note said it was always empty, from a recording of items with no relations.

- **A decision REFUSES what it cannot answer**, with a 400: a path that is not a
  playable `/library/metadata/{id}`, a bad `mediaIndex`, and a request whose profile
  name, platform and device all name no file in the server's `Resources/Profiles`
  (`Chrome` passes, `chrome` and `Darwin` do not). A test harness must send a real
  `X-Plex-Platform`. And plexsim cannot convert, so a refused direct play answers
  2000 / 4005 "neither", never 1001 "Conversion OK".
- **A playlist's timeline frames are type 15 and a collection's 18**, and the client
  builds the URL it updates from that number. A collection delete refetches its
  members (state 3 then 5 each) before its own state 9.
- **The timeline's minute is strict and has no `stopped` exemption**: 60 000 ms
  exactly is not stored, and a play completes past 90% on any state, once.
- **A query pair with no `=` is a 400 everywhere**, an empty path segment is a 404, and
  one trailing slash is harmless. Catch a leading `//` before `urlparse`, which reads it
  as a host.

- **No row names a theme.** plexsim has no theme audio, so `theme`, `parentTheme` and
  `grandparentTheme` stay off every row and container -- a real show without one
  carries none of the three, and the client fetches whichever it finds.
- **A hub row's projection depends on the row's TYPE and the SHELF**: movies and
  episodes are light (no Guid, text-only tags) on every shelf but Related and postplay,
  which carry the full row, and a show on a `mixed` Recently Added shelf is the full row. `excludeFields` applies to every row AND to the container.
- **`probes/ui_surface.py` without `--mutate` must send nothing but GET and HEAD** -- it
  is what gets pointed at the shared reference.
- **A sort key is type-prefixed as well, and python-plexapi always prefixes it**
  (`sort=movie.titleSort:desc`). On an episode list `show.titleSort` sorts by the SHOW
  and ties fall back to broadcast order; an unknown prefix reads the row itself, an
  unknown field after it is still a 500.
- **`/library/all` is a query, not a dump**: `type`, filters and `sort` all apply,
  the default order is the ratingKey, and a row carries `Media` only when typed.
- **A season with a year of its own carries it**, with its title as `titleSort` and
  no `parentYear`; only a season without one falls back to the show's year.

- **`/filters` is keyed by the asked `type=`, not by the library.** A movie library
  answers the episode list to `type=4`, types 8/9/10/13/20 have lists, `type=3` is
  empty and a non-number a 400; `includeAdvanced=1` adds Duplicates to 1 and 4 only.
  See `section_filters.json`.
- **The library subsets are not the grid.** On a SHOW library `onDeck`,
  `recentlyAdded`, `newest` and `recentlyViewed` are EPISODE lists whatever `type=`
  says, each `title2` names the subset, and `recentlyViewedShows` (top-level rows,
  viewed first) is the one with `allowSync` false and an unpaged totalSize.
- **Autocomplete matches a PHRASE at a word start, and reports the first
  occurrence.** `of s` finds Tears of Steel and `tears steel` does not; the
  `queryRange` can point at a different word than the one that matched. Exactly one
  `<field>.query`, and a media or state field is a 500.
- **By Folder follows `?parent=`**: a folder's subfolders are key/title rows and its
  files the items themselves, in FILE order. A view that ignores `parent` reopens
  the same list forever.
- **The client builds `/grandchildren` itself** from a hub row with `skipChildren`.
  A show's are its episodes, an episode's itself, a movie's an empty 200.
- **A paged season list KEEPS "All episodes"** at `Start=0`, outside the page, with
  its leaf counts; only a one-row page drops it. A paged row is still the light child
  projection.
- **`guid=` is the client's "do you have this?" lookup** (`/library/all?type=N&guid=`),
  a case-sensitive prefix. Ignoring it answers yes for every item.

- **Never write into a row `_full` returned.** It copies the top level only; its Media,
  Part and Stream dicts are the catalog's. `/status/sessions` set its playback flags there
  and every later read of the item said `directplay`.
- **Recently Added lists a new episode as its SEASON**, dated by the earliest episode, and
  the server-wide On Deck and Recently Added page. A `/library/search` hit names its
  library by title only.
- **The tree blanks every season title** and gives every node the ROOT's parentTitle; an
  episode part leads with `metadataType` 4 and a show ends with eight string settings.
- **The iOS platform types "All episodes" as a season.** Read the platform from the
  header as well as the query -- only the paging headers used to be normalised.
- **XML children have a fixed order** (Media, Image, UltraBlurColors, the tags, then
  Preferences/OnDeck/Extras/Related/Children) and `Meta` precedes a listing's rows. It
  lives in `xmlout._CHILD_ORDER`.

- **Paging is a property of the RESPONSE, not of a listing.** Every MediaContainer a
  real PMS answers honours `X-Plex-Container-Start/Size` (`/:/prefs`, `/identity`, a
  detail row), except a plug-in framework one (`noHistory`). plexsim applies it in
  `_page_container`, after the handler; a handler that pages itself sets `totalSize`
  and is left alone.
- **An unlocked Sort Title does not stick.** A title write, a sort-title write and an
  unlock each re-derive it from the title, article stripped, on items, collections and
  playlists alike; only a lock keeps what was typed. A check that writes `titleSort`
  without `titleSort.locked=1` is testing a value a real PMS throws away.
- **The section save path is scoped by `type` AND by library**: an `id` of another type
  or library is silently not edited, and a type-scoped filter (`show.title=`) there is a
  400 even though the same key filters a listing.
- **A frame's title is not the row's title**: a show is `Title (year)`, a season
  `<show> S01`, an episode `<show> S01 E07`. A section-path save frames every row's
  state 3 first, in sort-title order, and a list edit pushes its own type-15/18 pair.
- **A parent's title and studio are COPIED onto its children** (`parentTitle`,
  `parentStudio`, `grandparentTitle`) and have to be copied again on every edit; a season
  list's container names only a CUSTOM season title and carries the season's summary.
- **An emptied season or episode title is GENERATED** (`Season N`, `Episode N`, and it
  follows the index), and every emptied title keeps the old title's sort form.
- **`/:/progress` never completes a play**: it stores any position past a minute, even
  one past the end, and a missing or unparseable `time` is a 500.
- **A timeline refuses a position it cannot place**: a `time` past the `duration` sent
  with it, or a negative or non-numeric `time` or `duration`, is a 400.
- **A completed play answers `complete` until a `stopped` beat closes it**, and `ignore` to
  every beat after that -- not to any second beat.
- **Playlist writes validate like the rest**: a create needs `title`, `type` and `uri`
  and a type that fits the uri, and a smart list refuses item writes.
- **A state write needs the library `identifier`**: without it a scrobble, unscrobble,
  rate or progress is a 400 even for an unknown key, and a foreign one is a 404. The
  order is fixed -- key, identifier, the write's own parameter, then identifier value
  and key -- so a refusal check that omits the identifier tests the wrong branch.
- **It is the EPISODE scrobble that counts on its season and show**; scrobbling the season
  or the show leaves its own `viewCount` alone, and unscrobbling it removes it.
- **On Deck is one row per show**: its in-progress episode, else the one after the last
  watched; hiding any of its episodes hides the show until its next timeline. (Cycle 14
  corrected WHICH in-progress episode -- the most recently viewed, not the
  lowest-numbered. See the bullet below.)
- **A client's new play queue retires its older ones** (a 404 afterwards); another client's
  queue does not. A check that creates a second queue cannot read the first.
- **Deleting a play queue's SELECTED item answers the queue as it stood** (old version,
  selection and total, no rows); the next read has the selection on the following item.
- **A collection's children follow its `collectionSort`**: by release date when unset or 0,
  custom at 2 -- and a move starts from the order SHOWN, then switches to custom.
- **Renaming a collection leaves its members' tag alone**: the collection keeps the tag it
  was created under and finds, removes and untags members by it.
- **A library edit applies the whole form and scans**: `language` and `prefs[...]` apply,
  and a scan is ONE `library.update.section` activity stepping through the items, not an
  activity per row.
- **Unmatching an episode or a season gives `local://<id>` and the generated `Episode N` /
  `Season N`**, keeping the external Guids; the filename title and `none://` guid are a
  movie's.
- **A Media, Part or Stream attribute is measured against a real file, never read off
  ffprobe's names.** `tests/test_mediainfo.py` converts 46 recorded probes and compares them
  with what fun and the reference wrote for the same file; add a probe to
  `tests/fixtures/media/` before changing a rule. DTS is `dca` (`dca-ma` on the Media for
  DTS-HD MA), a Blu-ray subtitle `pgs`, a language is named in itself with its 639-2/T code and
  639-1 tag (`languages.json`), the Media's audio fields name the WIDEST track, the resolution
  label buckets width AND height, and the aspect ratio snaps to one of six values.
- **A track title never enters `displayTitle`**, and the extended title leaves out the label
  parts the title already carries: a part is carried when its first word is one of the
  title's words, split on whitespace and `- / ( ) [ ] + , | : ;` `&` but not on dots, folding
  ASCII case only, with a language's two-letter tag, `2.0` for Stereo and `AVC` for H.264
  counting too.
- **ffprobe lists a Matroska cover image as a video stream**, and a real PMS does not; a
  dual-layer Dolby Vision file's enhancement layer IS a second video stream.
- **Catalog media is corrected at load, and the titles are composed LAST.** Composed before
  `normalize_media`, a stored `dts` track is titled from the codec it no longer has.
- **An artwork write stores a VERSIONED path, never the url it was given.** `upload://` and
  remote candidates live beside the row (`_poster_url`, `sim.uploads`); `thumb` and `art`
  stay `/library/metadata/{id}/{thumb,art}/{ts}`, because that string is what a client puts
  in an `img src`.
- **A settings write, a butler run, a match, an artwork upload and a show's prefs write all
  push frames**, and a real PMS answers every one of them with an EMPTY body -- the task list
  or a container there reads to the client as a listing, not an acceptance. `POST /butler`
  and `DELETE /butler` are empty too, while `DELETE /butler/{Task}` is a 404.
- **A match is keyed on its `name`** (a guid with none is a 400, an empty guid with one
  renames and refreshes), and matching to a guid another row in the library already holds
  MERGES this row into that one. Every match runs the refresh sequence, so its activity ends:
  an activity that never ends leaves the Dashboard's spinner turning for the life of the
  process.
- **A smart list's Save is `PUT {list}/items?uri=<listing>`**, which replaces the stored
  query -- `limit` included -- and answers the list's row. Only the uri is stored; `content`,
  `leafCount` and the items are derived from it on every read.
- **A suite check that MERGES or deletes a fixture row starves the checks after it.** The
  match check absorbs the spare movie for that reason; cycle 9's Delete Version check was
  broken the same way, twice.
- **A play is watched only when a beat CROSSES 90% inside its own session.** The session must
  have reported a position under 90% of the duration THAT BEAT carries -- a single beat at the
  end, five climbing beats from 95%, another client's earlier position and a stored
  `/:/progress` offset all complete nothing. Past 90% the answer is `complete` or `ignore`,
  never `progress`. A suite check that wants a completion has to earn it with a beat below the
  threshold first.
- **A timeline beat's own `duration` is the only one that counts**: with none, or with
  `duration=0` (a 200, not a 400), nothing is stored and nothing completes, and the answer
  carries the item's stored offset. `/:/progress` is the exception -- it never sends one and
  still stores any position past a minute.
- **A beat at or under 60 000 ms DELETES the stored resume point** rather than being ignored,
  and a progress beat stamps `lastViewedAt` on the season and the show.
- **Restart a test server by waiting for the PORT to be free**, then check the new pid holds
  it: a kill plus a `/identity` poll can be answered by the process you just killed, and the
  suite then runs against a server it has already written to. The suite's own gate checks bind
  **32599** -- leave it free, or they measure whatever else is listening there.

- **zen runs Plex 1.42.2 — the exact build plexsim reports — and kids 1.43.4.** With both, a
  field only kids sends is a version difference and a rule both answer is not; fun is a third
  opinion, not the only second one. zen is production: GET and HEAD only, like fun.
- **A listing runs a QUERY, and four parts of it were never run.** `?limit=N` caps the rows
  (0 answers ONE row, a negative one every row, `abc` a 500) and `/newest`, `/recentlyAdded`
  and `/children` ignore it; a `type=` that does not parse is a **500** on a section listing
  and a **400** on `/library/all`, an empty one a 500 on both, and a type number the library
  holds no rows of is an empty 200; `id` filters every listing although no Meta advertises it;
  and a section's `allLeaves` takes all of it -- filters, sort, limit and type -- where it used
  to answer its whole leaf list to everything.
- **`id` compares like the column it is.** `=` exact, a comma list an OR, `>>`/`<<`/`>=`/`<=`
  numeric, and `==`/`!==` never match a number. A value that is not a number sorts ABOVE every
  id: `id=abc` matches nothing and `id!=abc` everything. `/query/parse` echoes it as a Rule
  with an EMPTY `fieldTitle`. It is what `guid=<x>&id!=<self>` -- the client's and
  python-plexapi's "another copy?" probe -- rests on.
- **`excludeElements=` reaches the section `allLeaves`, `/library/people/{id}/media` and
  `/hubs/items`** as well as the grid, and **`includeStorage=1`** adds `durationTotal` and
  `storageTotal` to a section grid row at the library's OWN top-level type only -- a show's are
  the sums over its episodes.
- **`/library/people/{id}/media` is typed by the TAG, not by its rows**: `viewGroup` and
  `viewMode` belong to an ACTOR's list (tagType 6), even when its rows are shows, and a
  Director, Writer or Producer list carries neither. The library filters narrow it too.
- **A statistics container pages its SERIES only.** `Device` and `Account` are a lookup: the
  page carries exactly the ones its own rows name, `totalSize` counts the series and `size`
  counts every element. And `timespan` has to parse as a whole number on all three endpoints
  (`0`, `-1` and `06` are fine; `abc`, `1.5`, empty and a leading space are 400s).
- **A real PMS gzips every compressible body the client offers to take compressed**, even when
  that makes it larger (83 bytes in, 90 out) -- `Accept-Encoding: identity` is the only thing
  that stops it. The old 860-byte floor and the "only if it shrinks" test were inferred, not
  measured.
- **A plain queue add lands in Up Next**, after the block of items ALREADY ADDED behind the
  current one and in front of the queue's original members; `next=1` goes ahead of that block.
  Testing "id greater than the selected id" reads every original member as already-added and
  appends to the very end.
- **The Upload Subtitle modal really uploads.** `POST /library/metadata/{id}/subtitles?title=&
  format=` with the file as the body is a 200 with an EMPTY body and leaves a new sidecar
  `Stream` that becomes the SELECTED subtitle; no `title` and a title with no extension are
  **500**s, an unaccepted format a 400 (`srt`/`ass`/`ssa`/`smi`/`txt` pass; `vtt`, `sub`,
  `idx`, `sup`, `pgs` do not), an empty body a **406**. A sidecar subtitle labels its codec
  `<CODEC> External` in both titles.
- **A stream PUT is accepted and stores nothing** (200, empty body, the title unchanged), a
  DELETE of an embedded track is **403**, and a DELETE of an uploaded sidecar really removes
  it. The GET of a real stream stays the 501.
- **A playlist's items and a collection's children are the LIGHT grid row**, like every other
  list -- no `Guid`, `Rating`, `Field` or `Stream`, tags text-only -- while keeping their
  librarySection fields and the `playlistItemID`.
- **A single history read is a projection**: nine attributes with `viewType: 1`, an episode
  adding `grandparentTitle`/`index`/`parentIndex`, and no `key`, `ratingKey` or artwork. It is
  not the row `/status/sessions/history/all` lists.

- **A five-clip library cannot show a row-shape rule that needs real content.** zen's 3,887
  movies are the instrument for those: a LIGHT row caps its tag lists (2 `Genre`, 2 `Writer`,
  2 `Country`, 3 `Role`, always the first of the detail row's order) where the DETAIL row carries
  all 155 of a movie's roles; the search hubs cap at 3 rows with no `limit` and
  `/library/search` at 10; an actor tag row is `tagType: 6` and a director row 4. None of that is
  visible on a library where nothing is long enough to truncate.
- **An episode's `parentGuid` is the SEASON's guid**, not the show's — the season's own
  `parentGuid` is the show, which is right on the season row and wrong copied onto the leaf. A
  catalog built before this was measured carries the wrong value, so `hierarchy.normalize_rows`
  corrects it at LOAD.
- **The section grid says `allowSync: true`**, like every other listing container; only
  `/library/sections`, `/library/all`, the secondary axes and a few subsets say false.
- **`group=` takes `type`, `guid` and `id`** as well as the scalar columns; only tag joins, media
  and per-user state are 500. **`/library/sections/{id}/actor` is the one secondary axis that
  carries no `content`.**
- **`/common` compares an EPISODE's title and not a SEASON's**, and `titleSort` is `""` only when
  genuinely mixed — a single row answers its own sort title, derived where it stores none.
- **A write's frames are their own measurement, and guessing them is a real bug.** A subtitle
  upload pushes NOTHING (cycle 11 added an edit pair without measuring, and told every client on
  the socket that an unchanged item had changed). A refresh's `updated 49` comes SECOND, before
  the processing timeline. A scan ends inside a NESTED pair of `provider.subscriptions.process`
  activities, and `?force=1` runs that whole scan before it refreshes anything. Optimize Database
  runs a `general.db.optimize` activity at 0/10/40/60.
- **A library edit needs an `agent`, not a `name`** — the client's upgrade form sends `agent` and
  `scanner` and nothing else — and a create answers a `Location` header naming the new library.
- **A queue add that adds nothing changes nothing**: a `server://` uri naming another MACHINE
  adds no rows, and no no-op add steps `playQueueVersion`. The client discards a queue response
  whose version it did not expect, so stepping it on a no-op throws away the player's queue.
- **The subtitle upload's format check is CASE-SENSITIVE and takes `psb`**: `SRT` is a 400 where
  `srt` is a 200, and the client's modal offers extensions the server refuses.
- **A stream path tells a malformed id from a missing one**: `DELETE .../abc` is a 400 and
  `DELETE .../999999` a 404; a POST to a stream that exists is a 404, not the 501 its GET answers.

- **A row with NO value sorts BELOW every value** -- first ascending, last descending, on
  every sort key and every row type. Twelve of the fifteen sorts a section advertises
  default to `desc`, so keying on `(value is None, value)` opens Rating, Plays, Date
  Viewed and Critic Rating on the rows that have no value at all.
- **A light row caps `Director` at THREE and carries no `Producer`** however many the
  detail row holds (17 directors and 22 producers on one zen movie). Only a real library
  can show it: no lab item has more than one director.
- **`/hubs/search` returns the ITEMS behind a matched tag**, each naming it in
  `reason`/`reasonID`/`reasonTitle` -- 53 of 81 item rows across ten zen queries -- while
  `/library/search` carries the tag rows and the title hits only. And the seventeen hubs'
  base ORDER has `movie` fifth and `collection` fourteenth; it is visible only on a query
  that matches nothing, because hits PROMOTE the hubs that scored.
- **The "All episodes" row follows the PAGE's row count, not the offset.** It is there
  whenever the page returns more than one season row, wherever the page starts, and gone
  on a one-row page even at `Start=0`. Cycle 8's "the servers disagree past Start=0" was a
  two-season library, not a version difference.
- **`duplicate=1` is "more than one Media"**, and on a SHOW or SEASON "a leaf has more
  than one" (214 movies, 504 episodes, 44 shows and 74 seasons on zen).
- **A bare `<field>=` means three different things.** A tag-TABLE field matches NOTHING
  (37 of 600 zen movies have no Genre and `genre=` still answers none); `studio` and
  `editionTitle` match EVERY row; everything else of that kind matches the rows with no
  value. `contentRating=None`, in that spelling only, is the empty synonym and the key of
  the axis's own trailing bucket -- the one axis that has one.
- **A tag or field axis REFUSES an unparseable `type=`** -- 500, or 400 on `year` alone --
  where a MEDIA axis ignores `type=` completely and answers the same list to `abc`.
  `firstCharacter` is scoped by the type and its counts follow.
- **An EPISODE with an `audienceRating` carries `audienceRatingImage` and a `Rating`
  row**, a SEASON never does, and an episode carries its SHOW's `contentRating`. A show's
  `/allLeaves` -- and no other list -- stamps the show's `studio` on every episode row,
  and a season row of `/library/recentlyAdded` alone carries `parentSummary`.
- **`viewedLeafCount`, `viewedOrInProgressLeafCount` and `unviewedLeafCount` filter a show
  listing** although no Meta advertises them; `leafCount` and `childCount` do not.
- **A shelf with no `count=` carries SIX rows and says `more`**, not twelve -- and 1.42.2
  builds three shelves 1.43.4 does not (`movie.topunwatched`, `tv.startwatching`,
  `tv.toprated`), so zen is the witness. `pinnedContentDirectoryID` reshapes the promoted
  Recently Added shelf into `home.movies.recent.N` / "Recently Added Movies" on 1.42.2 and
  changes nothing on 1.43.4.
- **A write to a PARENT cascades.** Unmatching a season unmatches its episodes; unmatching
  a show unmatches its seasons AND episodes, and every descendant's `parentGuid` /
  `grandparentGuid` follows; an episode unmatch drops the people lists, `audienceRating`
  and `year` and empties `summary` whichever path reaches it; a season's `index` lands on
  its episodes' `parentIndex`. **Read the children back after every parent write** -- the
  rule is never on the parent row alone.
- **An unmatch leaves no activity running.** A real PMS runs the refresh sequence and ends
  it; an activity left open turns the Dashboard spinner for the life of the process.

- **`titleSort` is the title TRIMMED and FOLDED and then article-stripped.** Leading
  whitespace goes, Unicode combining marks are dropped (`Amélie` sorts as `Amelie`,
  `Señor's` as `Senor's`), and only then the/a/an. Measured on zen over all 5,294 rows:
  the model reproduces 5,289, all 21 accented rows carry the folded form and none lacks
  a `titleSort`, and the 51 rows whose non-ASCII survives NFD (CJK, `’`, `–`) carry none
  at all. The five misses are Spanish `La` and German `Das` -- per-item language, not
  mechanical, and deliberately not implemented.
- **The word matcher folds accents and the substring matchers do not.** `/hubs/search`
  and `/library/search` fold unconditionally -- and it IS a fold, not the fuzzy score:
  `Amelie` and `Amélie` both answer 0.93095 where `Amlie` and `Amelke` answer 0.33095
  and `Amelia` answers nothing. `title=` on a listing folds too, but only while the
  query holds NO SPACE: `title=Bogota` finds the accented row and `title=Bogota City`
  and `title=Bogota ` find nothing, while every accented spelling does. The section's
  substring box, `/autocomplete` and `title!=` do not fold at all.
- **`/filters` and the `Meta` descriptor are PER VERSION.** 1.43.4 advertises 28 movie
  filters, 17 at `type=4` and 6 at `type=10`; 1.42.2 -- the build plexsim reports --
  advertises 22, 9 and 5. The difference is the media-codec axes (`hdr10plus`, `atmos`,
  `videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`) everywhere plus `hdr` and
  `dovi` on the EPISODE list, which the movie list keeps. Types 2, 3, 8, 9, 13 and 20
  and the Duplicates row are identical on both. `Meta.Type[].Filter` is a separate
  fixture carrying the same split.
- **A `group=`'s surviving row is the group's LOWEST `addedAt`, picked BEFORE the sort**
  -- so the same SET comes back under every `sort=` and only the order changes. Keying
  on the sort's first row makes changing the sort change which rows the grid shows. And
  a type-prefixed group key is SCOPED like a filter: `type=4&group=show.title` collapses
  to the show count, not to the episode-title count.
- **The `actor` axis is the union of each item's FIRST THREE roles**, not every Role tag
  in the library -- kids' five movies contribute 0+2+2+2+3 and its axis totalSize is
  exactly 9; zen's is 3413 against 59,030 actor tags. Every other tag axis carries every
  tag. The axis's own `title2` says so: "By Starring Actor".
- **Nine columns filter although no Meta advertises them**: `viewOffset`, `skipCount`,
  `ratingCount`, `absoluteIndex`, `lastRatedAt`, `updatedAt`, `createdAt`, `mediaCount`
  and `originalTitle`. `/query/parse` echoes each as a Rule with an EMPTY `fieldTitle`,
  the shape `id` already had. An unknown field answers EVERY row, which is how a known
  one is told apart. `parentIndex` is a **500** on a listing instead, and a MOVIE row's
  `index` column is **1** on every row although the row never emits the attribute.
- **A string field's six operators are the ADVERTISED FieldType's restriction, not the
  python type's.** `titleSort`, `summary`, `tagline` and `originalTitle` appear in no
  Meta and take `>>`/`<<` (`titleSort>>=ZZZZ` is 1 row of zen's 3887), while the
  advertised `title` and `studio` answer every row to the same key -- an ignored field.
- **Thirteen more `sort=` keys work than the fifteen `includeAdvanced=1` advertises**:
  `guid`, `originalTitle`, `contentRatingAge`, `studio`, `skipCount`, `absoluteIndex`,
  `mediaSize`, `createdAt`, `type`, `viewedLeafCount`, `tagline`, `summary` and
  `editionTitle`, each a real ordering. `leafCount` is the other direction -- both
  references 500 it, along with `childCount`, `slug` and `key`.
- **`/status/sessions/history/all` VALIDATES its ids.** A non-integer `accountID`,
  `librarySectionID` or `metadataItemID` is a 400 (`abc`, empty and `1.5` alike, while
  `1`, `-1` and `01` pass); a `metadataItemID` naming no row is a **404**, `0` included;
  an unknown `librarySectionID` is an empty 200; `deviceID` is not validated at all; and
  `viewedAt>=` refuses a non-integer. Answering 200/`size 0` to a bad id reads to the
  client as "no history" rather than as an error.
- **`group=` reaches the SECTION `allLeaves`** -- the last part of the listing grammar
  that surface was missing -- while an ITEM's `/allLeaves?group=` is ignored.
- **`#` is the DIGITS bucket of `firstCharacter`, not the everything-else one**: any
  other non-letter gets a bucket of its OWN, listed after Z, and every bucket `key` is
  percent-encoded (`%23`, `%E3%80%8E`). Raw, `#` is a URL fragment and the client's
  request loses everything after it.
- **`/library/sections/{id}/userRating` is a real axis with a FIXED six-bucket scale** --
  `10` "5 stars" down to `2` "1 star" plus `-1` "Not rated", identical on both servers and
  on both library types whatever the library holds -- and it IGNORES `type=` the way a
  media axis does. The nine axes of OTHER library types (`mood`, `style`, `format`,
  `subformat`, `source`, `make`, `model`, `place`, `tag`) answer an EMPTY secondary
  container with their own `title2`, not a 404, and they VALIDATE `type=` the way `/genre`
  does. The two families split on that, so neither inherits the other's rule.
- **`/library/metadata/{id}/banners` and `/thumbs` are candidate lists too** (200 on both
  versions, `banners` empty on both, `thumbs` from the media bundle). `/squareArts` is
  **1.43.4 only** -- 404 on zen -- so plexsim, which reports 1.42.2, keeps 404ing it.
- **`/library/sections/{id}/recentlyReleased` does not exist.** A 404 on both references,
  every library type, with and without `type=`. plexsim answered a listing there for its
  whole life; the name appears in no findings document and in no suite, so it was assumed
  rather than measured. Sweeping a surface path by path against BOTH references is what
  found it -- 59 top-level paths and 48 item leaves agreed, and this one did not.
- **`/library/metadata/{id}/background` and `/composite` are 404s on an ITEM**, on every
  row type, bare and versioned, while `thumb`, `art` and `poster` are 200s. A `composite`
  is real on a playlist and a collection only; `background` is not a path at all, it is an
  `Image` element's `type`.
- **A sweep must run against a FRESHLY RESTARTED fixture server.** Pointed at one the UI
  suite has finished with -- it deletes a row and a section at the end -- the same probe
  reports `thumb` and `poster` as 404s that both references answer 200.

- **A merge whose target has no `Media` of its own absorbs NOTHING.** Merging one season
  into another is a 200, no frames, and a tree that is completely unchanged -- both seasons
  and every episode still there -- and the `split` that would undo it is a no-op too. An
  EPISODE merge beside it is real (two Media on the survivor, a 404 on the other, the leaf
  counts dropping). plexsim's `catalog.remove` cascades, so a season merge deleted the
  season AND its episodes: the programme's one data-loss bug.
- **On Deck is the MOST RECENTLY VIEWED in-progress episode**, not the lowest-numbered --
  measured on `long` media, where an episode can actually be left in progress. A beat on
  episode 8 puts 8 on deck, a progress write on episode 10 moves it to 10, a fresh beat on
  8 moves it back. Cycle 9 recorded the opposite from ten-second clips.
- **The prefs cascade belongs to `showOrdering` ALONE.** `showOrdering=dvd` pushes 45
  frames -- it renumbers the episodes, so the server re-reads the tree -- while
  `flattenSeasons`, `episodeSort`, `useOriginalTitle` and `subtitleMode` each push ZERO.
  What those four do instead is change the rows: `flattenSeasons=1` sets `skipChildren` on
  the show and `skipParent` on every episode while REMOVING their `parentRatingKey` and
  `parentKey`; `episodeSort=1` puts a season's `/children` newest first while `/allLeaves`
  keeps broadcast order. plexsim cascaded on every prefs write and changed no row.
- **An artwork PICK re-versions `thumb`, `art` and `theme` onto ONE stamp**, moves
  `updatedAt` with them, LOCKS the picked field, pushes one frame (state 5, no
  `metadataState`) and CASCADES the stamp to the children that borrow it -- a season's
  `parentThumb`/`art`, an episode's `grandparent*` -- never the season's own `thumb`.
- **A hub container says `allowSync: true`** on every hub path except
  `/hubs/metadata/{id}/postplay`, which says false; `/hubs/search` and `/hubs/items` carry
  none at all. (`postplay` DOES carry `allowSync` -- an older note here said it carried
  neither that nor the librarySection triple; only the triple half was right.)
- **On a SEASON listing the leaf-count filters read the SHOW's counts.** zen's
  `type=3&viewedLeafCount>>0` answers 48 seasons -- every season of the 8 shows with a
  watched leaf -- and all 48 carry 0 on their own row.
- **A collection answers `/children` on the METADATA path** (a full secondary container with
  `key`, `parentIndex` = its TAG id, `parentTitle`, `title2`, `nocache` and the
  librarySection triple) while `/library/collections/{id}/children` answers the BARE
  `{size: N}`. A `type=18` listing is a real QUERY (`title=`, `sort=`, default titleSort),
  a collection row's `index` is its own tag id, and `/library/all?type=18` lists every
  collection the server holds across libraries in title order.

- **`/status/sessions/history/all` lists NOTHING without a `type=`** -- and the `type` is the
  row's `viewType`, not its metadata type. kids' history is five MOVIES and zen's is 73
  EPISODES and both answer to `type=1`, while `type=0/2/3/4/5` answer none on either;
  `type=abc` and an empty one are 400s and every listing row carries `viewType: 1`. That is
  the whole of the `size 0` cycle 11 could not explain.
- **`titleSort` trims the LEFT only.** A trailing space survives into the sort title -- which
  is why a title written with one carries no `titleSort` at all, the derived string being
  equal to the title. `'  Zeta Both  '` sorts as `'Zeta Both  '`.
- **`flattenSeasons` and `episodeSort` are a TRI-STATE on an item**: `-1` INHERITS the
  library's value and `0`/`1` override it in both directions. The library-level write --
  where the Edit Library dialog puts them -- produces exactly the row effects the item path
  does, and an unwritten show reports `-1`, not the `0` a stale capture recorded.
- **`title=` folds across a SPACE.** Cycle 14's "the fold stops once the query has a space"
  was a colon: a row matches if EITHER the raw word-prefix matcher matches, OR a folded pass
  in which every term but the LAST must equal a whole whitespace word of the title. So
  `Adoravel Trapalhao` finds its row, `Bogota City` does not (the word is `Bogota:`), and
  `Bogot City` does. A query with an empty term -- a trailing or doubled space -- fails the
  folded pass. Punctuation rides inside a term: `tournee` misses `(tournee`.
- **The section search box answers the LIGHT grid row**, like every other listing -- no
  `Guid`, `Rating`, tag ids or `Stream`. It was the one listing plexsim answered in full.
- **`excludeElements=` is a property of the RESPONSE, as paging is.** Every container that
  carries rows honours it, `/library/all`, `/library/recentlyAdded`, a section's
  `recentlyAdded`/`newest`/`search`/`common`, `/grandchildren` and every hub path included.
- **Four more columns filter and eight more PARSE.** `parent=` (the immediate
  `parentRatingKey`), `rating=`, `audienceRating=` and `file=` (a case-insensitive substring
  of ANY Part's path) all constrain a listing; `/query/parse` additionally echoes
  `parentIndex`, `guid`, `slug` and `extraType` as Rules with an empty `fieldTitle`. The
  parse surface is WIDER than the filter surface.
- **`contentDirectoryID` scopes and VALIDATES `/hubs/search`** -- a value naming no section is
  a 404 there. "Ignored on every other hub path" was REFUTED in cycle 28: it scopes `/hubs` and
  all three `/hubs/home/*` leaves as well, and `/hubs/continueWatching` validates it. See the
  cycle-28 bullet below.
- **`/playlists?includeMeta=1` and `/hubs/home/recentlyAdded?type=N&includeMeta=1` carry a
  `Meta`.** The playlist list's six sorts end in `mediaCount` "Item Count", NOT the
  `updatedAt` of `/playlists/sorts`; the hub's one row is `movie`/true at `type=1`,
  `mixed`/false at `type=2` and absent for every other type.
- **`includeChildren=1` inlines a `Children` block on a SHOW and on nothing else.**
- **The inline `Related` block caps at FIVE and obeys `includeRelatedCount`**, where the
  standalone `/hubs/metadata/{id}/related` answers six.
- **`limit=` and `parent=` are PARSED wherever a listing consumes them**: an unparseable
  value is a 500 there and a 400 on `/status/sessions/history/all`. A good `limit` still does
  nothing on `/children` -- ignoring the VALUE is not the same as not parsing the string.
  `/hubs/search?limit=` has its own grammar: unparseable 400, non-positive 500.
- **A FRACTIONAL value refuses on seven columns only** -- `viewCount`, `viewOffset`,
  `lastViewedAt`, `lastRatedAt`, `addedAt`, `updatedAt`, `createdAt` -- and not on `year`,
  `duration` or the rest. `lastRatedAt`, `createdAt` and `lastViewedAt` refuse a word too:
  the VALUE is parsed before the row's own is looked at, so a null column does not excuse it.
- **A READ-ONLY route answers 404 to every verb but GET and HEAD**, while the legacy
  single-segment plug-in dispatcher answers EVERY method. `PATCH` and a bare `OPTIONS` are
  ROUTED like any other verb, so a bare OPTIONS 404s wherever a GET-only route does; a
  method a real PMS does not recognise at all is a **500**, never a 501.
- **POSTing an EMPTY body to any artwork or subtitle candidate list is a 406**, `/extras`
  being the 400. On the subtitle upload the empty-body check runs BEFORE the title check.
- **`DELETE /library/metadata/{id}/{poster,art,thumb}` CLEARS the artwork**: 200 with an
  empty body, the field gone, the artwork left re-stamped, one `state 5` frame, nothing
  `selected` afterwards and a 404 on the bare GET. An EPISODE owns no `/art` path at all.
- **A collection's `ratingCount` is its member TYPE number, +100 when smart** (movie 1, show
  2, season 3, episode 4; 101/102/104), its `librarySectionID` is a NUMBER, its
  `includePreferences` block is two settings -- `collectionMode` plus `collectionSort`, or
  `collectionFilterBasedOnUser` when smart, which also 400s a `collectionSort` write -- and
  `GET /library/collections/{id}/prefs` is a 404. `collectionSort` is alphabetical for any
  plain non-zero integer it can parse and by release date otherwise.
- **A `type=18` SECTION save renames the members' `Collection` TAG and the item path does
  not**; the tag keeps its id. A member's tag carries the collection's `guid` and no `count`,
  a label on a collection reaches the `/label` axis, and a collection is found by
  `/hubs/search` as a `tagType: 2` row -- and by `/library/search` too, which cycle 28
  re-measured once the copy held a collection at all (the old "never" was the fixture's).
- **A refresh CASCADES**: 7 frames on an episode, 18 on a season (its episodes inside its own
  activity, with no `activity updated`), 49 on a two-season show. An **analyze** frames every
  LEAF three times -- `mediaState` analyzing, thumbnailing, none -- and ends in one
  `media.generate.chapter.thumbs` activity; a season and a show row are never framed
  themselves. A library **create** ends in a `backgroundProcessingQueue` `queueRegenerated`
  frame, which follows any write that leaves work in the queue.

- **A real PMS LOCALISES its own text.** `X-Plex-Language` (header or query) beats
  `Accept-Language`, a regional form works like its primary subtag, an unknown language
  falls back to English, and 28 languages answer differently. The filter and sort menus,
  the `Meta` titles, the hub titles, a GENERATED `Season N`, a stream's language name and
  `displayTitle`, and all 200+ `/:/prefs` labels move; the LIBRARY's own rows do not, and
  neither do `All Movies`, an axis's `title2` or `Unplayed` -- the same words ARE
  translated as a section pivot's `title`, so the table is keyed by ATTRIBUTE and then by
  the English string (`translations.json`, captured by diffing each endpoint against
  itself in each language). **Cycle 66 corrected the language model** against the 44 values
  Plex Web actually sends: `no` and `af` have catalogues of their own; `pt-BR` and `pt-PT`
  read the Brazilian one on EVERY document (`pt-PT` with Portugal's ICU names); `es-419` and
  `fr-CA` read `es`/`fr` with their own ICU names; and `bg`, `el`, `fa`, `is`, `my`, `th`,
  `tr`, `vi`, `zh-TW` and `en-GB` have no catalogue (English menus) but ICU country and
  language names in the full locale.
- **A real PMS ROUTES before it authenticates.** With no token an unknown path is a
  **404** and only a path that exists is a 401. Of the single-segment paths exactly eleven
  answer 401 (`library hubs playlists devices butler servers clients accounts activities
  playQueues log`), `/identity` and `/security` answer 200, and every other one is a
  **403** -- whether or not it answers when a token IS given. `GET /` is a 401 whose
  193-byte body redirects a browser into the web client.
- **The `firstCharacter` AXIS and its BUCKETS disagree, on a real PMS.** The axis counts a
  season or an episode by its SHOW's sort title (28 of 28 buckets on zen, both types); the
  listing at `/firstCharacter/{key}` filters on the row's OWN stored sort title. So a
  season list's `A` bucket says 64 and answers 0 rows.
- **A section leaf's extra path segment is a bucket or a 404, never the leaf again.**
  `/all/zzz`, `/sorts/zzz`, `/studio/<real key>`, `/audioLanguage/en` and fourteen more
  are 404s; the tag axes, `year`, `decade`, `contentRating`, `resolution` and `userRating`
  answer that bucket's rows. `extras/all`, `query/parse`, `indexes/thumbnails` and the
  VERSIONED `composite/{v}` are the real two-segment leaves; the bare section
  `/composite` is a 404.
- **A show library's `?type=3`/`?type=4` grid defaults to BROADCAST order** -- the show's
  sort title, then `parentIndex`, then `index` -- not to a flat `titleSort`, and its
  `/unwatched` carries `nocache` like every other container it answers.
- **An artwork path is a 200 only on the row that OWNS the image.** A row that borrows
  carries the owner's path verbatim (kids' season 7 and episode 8 both carry
  `/library/metadata/6/art/<v>`), so comparing the stored path's ratingKey with the
  requested one IS the rule; cycle 15's episode-only `/art` 404 was its special case.
- **The envelope is measured too.** Every response names its connection state
  (`Keep-Alive: timeout=20`, and an idle socket really is closed at 20 s); a paged
  container answers `X-Plex-Container-Start`/`-Total-Size` as HEADERS and the paging
  header BEATS the query; `Range` applies to every body (206/416, never gzipped); a
  malformed header (no colon, a TAB in a value, a name over 1024 or a value over 4096) is
  a 400; `Accept-Encoding` tokens match exactly (`GZIP` and `x-gzip` get plain); no `100
  Continue`; a preflight with no `Origin` carries no CORS headers; and **no `Server:`
  banner at all**.
- **The Accept `q` grammar ranks a zero rather than dropping it.** The FIRST parameter must
  begin exactly `q=` or the range is disqualified (`; q=0.5` and `;Q=0.5` are out,
  `application/json ;q=0.5` is in); no parameter weighs 1.0, a single parseable
  non-negative `q` weighs that, and everything else -- `q=0`, `q=abc`, `q=`, `q=-1`,
  `q="0.5"`, `q=0.5x`, a second parameter -- weighs 0 and still QUALIFIES.
- **`/web` is a `302 Moved Temporarily`** with an ABSOLUTE Location from the request's own
  `Host` and `Cache-Control: public`, the one place on the surface that is not
  `no-cache`; a `/web/*` asset is `no-cache`, `Accept-Ranges: bytes` and never gzipped,
  `.js` is `text/javascript`, `.html` carries no charset, `.ico` is
  `application/octet-stream`.
- **`X-Plex-Platform: Roku`** -- that exact spelling -- strips `id` and `Pivot` from every
  library `Directory` of `/media/providers` and changes nothing else -- **only for a Roku whose
  `X-Plex-Version` is absent, unparsable or below 6.4.2** (cycle 66); every shipping Roku keeps them.
- **Four XML shapes**: a `Meta/Type` orders its children `Filter, Sort, Field`;
  `includeDetails=1` carries no `active`/`activeDirection` on a Sort nor `advanced` on a
  Filter where `includeMeta=1` carries all three; the rows `includeChildren=1` inlines
  carry no librarySection triple; and a container's attributes are `size`, then
  `totalSize` and `offset` when paged, then alphabetical.
- **`/playlists/{id}/items` is a typed QUERY** -- `type=2` answers the SHOW that owns the
  member episodes and `type=3` their seasons -- and `includeMeta=1` carries the page's own
  tab bar. A playlist is born `viewCount: 1` with `lastViewedAt == addedAt`, and that pair
  is on the LISTING row only. `/playlists` filters by `title`, `smart`, `sectionID` and
  `type` (a playlist is 15), its default order is SMART first then `titleSort`, and
  `sort=mediaCount` is a real ordering where `leafCount` is a 500.
- **`POST /playlists/{id}/items` is a 404**: only PUT adds. An empty `title=` is stored
  verbatim; `smart=1` over a comma-joined KEY LIST is a 400 (the test is the uri's SHAPE,
  not whether it resolves -- refusing anything that resolves breaks every smart create);
  a uri the server cannot resolve adds NOTHING. `PUT /playlists/{id}` takes the whole ITEM
  edit grammar, `playlistItemID` is a SERVER-WIDE sequence, a smart list's items carry
  none and every item write on one is a 400, and a playlist's artwork lives under
  `/library/metadata/{ratingKey}/` while `/playlists/{id}/{posters,arts,thumb,art}` and
  the bare `composite` are 404s.
- **A shuffled play queue is really shuffled** (plexsim reversed it, identically every
  time). `includeBefore`/`includeAfter` window the queue with NO `window=` -- the radius is
  unlimited then -- and each is a boolean whose every other value is a 400. EVERY item
  delete answers the container as it stood BEFORE it, and only the SELECTED one drops the
  rows. A move refuses `after=<itself>` (404) and an empty `after=` (400). A queue built
  from a PLAYLIST uri is a playlist queue.
- **The SECTION save path stores an unlocked `titleSort.value=`** until the next title
  write re-derives it, where the ITEM path discards it; `type=abc` there is a 400. A
  show's `contentRating` reaches only the episodes carrying none of their own, a show's
  `index` cascades onto its seasons' `parentIndex`, a season that LOSES its year gains
  `parentYear` in the same read, and the `state 3` frame carries the row's title from
  BEFORE the write.
- **An UNMATCHED item drops its agent-driven prefs**: a show loses `languageOverride` and
  `useOriginalTitle`, a MOVIE loses the whole `Preferences` block, a season keeps its
  three.

- **Three fixtures were captured off 1.43.4 and plexsim reports 1.42.2.** Cycle 14 split
  `Meta.Type[].Filter` per version and left `Field` at 1.43.4's: 1.42.2 advertises **34**
  movie Fields and **20** episode ones against 40 and 26, differing in the six media-codec
  axes -- and on 1.42.2 those six are UNKNOWN fields, so a listing filtered on one answers
  EVERY row and `/query/parse` reports an `Error`. `tv.plex.agents.nfo.movie`/`.series` are
  1.43.4-only too (four modern agents there, not six). When a fixture disagrees with zen,
  suspect the version before the shape.
- **A real PMS escapes XML attribute values into pure ASCII**, `encoding="UTF-8"`
  notwithstanding: `&amp;`/`&lt;`/`&gt;` named, `"` and `'` NUMERIC (`&#34;`, `&#39;`), TAB
  decimal `&#9;` but NEWLINE and CR HEX (`&#xA;`, `&#xD;`), every codepoint from U+0080 up
  its DECIMAL reference (an emoji is one `&#128512;`, not a surrogate pair), and every
  control XML 1.0 forbids replaced with a literal `?`. The surface carries no element TEXT,
  so that is the whole rule.
- **A ROW's XML attributes come out in a measured order too** -- not alphabetical and not
  the order the row was built in. 25 element buckets in `xmlout._ATTR_ORDER`, derived as a
  topological sort of every PAIR both references were seen to emit and verified as a
  superset of every observed sequence. A tag is `id, filter, tag`; a sort row is `default,
  defaultDirection, descKey, firstCharacterKey, key, title`; a `firstCharacter` bucket is
  `size, key, title`.
- **`titleSort` strips a leading run of PUNCTUATION as well as whitespace, before the
  article.** The set is not a Unicode category: in ASCII only `" ' ( - . [ { ~`, and outside
  it every character from U+0080 to U+00BF and from U+2000 to U+2FFF -- while U+0100..U+1FFF
  and U+3000 upwards stay, so the CJK and fullwidth brackets are kept. The run may mix with
  whitespace and repeat, and a title made of nothing else keeps its own text.
- **A show's `/allLeaves` -- and no other container -- stamps a leaf's BORROWED artwork with
  the LEAF's own `addedAt`** (`grandparentThumb`, `grandparentArt`, `grandparentTheme`,
  `art`). Everywhere else they carry the show's CURRENT version. Same family as the `studio`
  that list already stamps: a property of the LIST, not of the row.
- **The localiser reaches INSIDE a row for exactly two blocks**: the `Preferences` block's
  `label`/`summary`/`enumValues`, and a `Stream`'s language fields -- where the two display
  titles are COMPOSED from the localised name and the omission is decided on the ENGLISH one,
  so a track titled "English" keeps `English (SRT)` in German while its plain title moves.
  The row's own `title`, `summary` and tags stay English.
- **A save a real PMS REFUSES writes NOTHING.** Validate the whole form before applying any
  of it: plexsim applied each parameter as it validated it, so a 500 left half the edit on
  disk while telling the client it had failed.
- **The ITEM edit path's 400 means "this request edits nothing"**, not "an unknown field":
  with a real edit beside it every unrecognised name is IGNORED, and each alone is a 400.
  The SECTION path answers 200 to a form that writes nothing (a bare field is a filter
  there) and still 400s an unknown `.value`.
- **A library preference write moves the SECTION's `updatedAt`**; an INT pref that does not
  parse is stored as **0** (not ignored); and a TEXT pref is stored percent-escaped for `:`
  and `|` -- the `enumValues` delimiters -- with a value carrying `%` stored EMPTY.
- **The collection ITEM paths take one verb each** (`/items` PUT plus its own GET/HEAD,
  `/items/{rk}` DELETE, `/move` PUT) and 404 the rest; a non-numeric ratingKey on the DELETE
  is a 404 where an unknown number is a 200. The MOVE refuses `after=<itself>`, an empty
  `after=` and an unparseable one with a 400, sends an `after=` naming no member to the
  FRONT, and works through the `/children/{rk}/move` alias. A comma-joined add TOLERATES the
  members it cannot use; only a single-key uri of the wrong type is a 400.
- **A collection's `/children` on the METADATA path is the LIGHT row**, with the
  librarySection triple on the CONTAINER, `allowSync: true` and no `thumb`.
- **A local artwork candidate names a server path**: `ratingKey` the bare uri, `key` and
  `thumb` `/library/metadata/{id}/file?url=<encoded, `.` as `%2E`>`, which really serves the
  image. A `media://` or `upload://` candidate carries no `provider`. `DELETE .../thumb`
  clears the poster like `/poster`, and an artwork UPLOAD moves `updatedAt` onto the new
  stamp exactly as a PICK does.
- **Twelve butler tasks report a different sentence than plexsim did**, three report
  SEVERAL and five report one where it reported none; `CleanOldBundles` steps its activity
  SUBTITLE through four named phases and `OptimizeDatabase` is titled `Optimizing database`
  with the subtitle `Optimizing Database`. And **an activity's subtitle is the row's FRAME
  title** -- a season's `<show> S01`, an episode's `<show> S01 E01` -- not `title (year)`.
- **`POST` on an item ACTION leaf is a 404** (`/refresh`, `/analyze`, `/unmatch` take PUT),
  while `POST /library/sections/{id}/refresh` is a 200; `/match` answers EVERY verb with its
  no-parameters 400; `?force=` on a section refresh is PARSED; a KNOWN single-segment path
  (`/butler`, `/devices`) falls through to the plug-in dispatcher for the verbs it does not
  implement; and `GET /livetv` is that dispatcher's three-row container, not an empty one.

- **A title the SERVER generated is stored NOWHERE.** A season's `Season N` and an
  unmatched episode's `Episode N` have an EMPTY stored sort title -- `titleSort=` (a bare
  empty value, an equality here) answers 740 of zen's 741 seasons and 4,630 of its 9,287
  episodes, `titleSort<<=0` the same set, and every LETTER bucket of a season
  `firstCharacter` listing answers none while `#` answers them all -- and it is in no
  search index either: the copy's `Season 2` answers nothing to `search?type=3&
  query=season` while the same season renamed answers at once, and zen's
  `type=3&title=Season` is 0 of 741. An agent-supplied `Season 1`/`Episode 1` (kids'
  season 7, 176 of zen's episodes) IS stored, and nothing on the row tells the two apart.
- **Eleven Latin-1 characters fold in `titleSort` that NFD cannot reach**, swept
  character by character over U+0080..U+02FF: `Æ→E Ð→D ×→x Ø→0 Þ→P ß→s æ→e ð→O ÷→/ ø→0
  þ→p`. `Ø`/`ø` become the DIGIT ZERO and `ð` a capital `O`, so it is a table and not a
  transliteration; zen witnesses two of them (`Død Kalm` → `D0d Kalm`). Nothing above
  U+02FF folds.
- **The bare `title=` matcher also reads the row's ORIGINAL title** -- and only it does:
  `title=the` is 1,484 of zen's 3,887 where the title alone explains 1,481. `title!=`,
  `==`, `!==`, `<=` and `>=` read the title alone.
- **On an UNLISTED string field (`titleSort`, `summary`, `tagline`, `originalTitle`) the
  four ordering keys really ORDER**: `<=` ≡ `<<=` and `>=` ≡ `>>=`, case-insensitive
  lexicographic, both STRICT -- a row whose value IS the query is in neither set. They are
  begins-with and ends-with only on an ADVERTISED string field.
- **`/library/sections/{id}/search?type=N` types its container from the ASKED type**, not
  from the library: `type=3` is `viewGroup=season` and `viewMode` **65593**, `type=4` is
  `viewGroup=episode` with `mixedParents`, and a MOVIE library answers both. The section
  GRID is the other way round and keeps the library's own type.
- **`All <library title>` on a movie library, the constant `All Shows` on a show one** --
  in both places the string appears, the `/all` container's `title2` and the `all` pivot
  row. Not localised.
- **Three endpoints, three meanings for `limit=`.** `/library/recentlyAdded?limit=N` caps
  AND rewrites `totalSize` (0 answers none, a negative value lifts the 50-row cap, an
  unparseable one is ignored); `/status/sessions/history/all` caps too and `-1` there
  answers NONE; a section listing's `limit=0` answers ONE row and an unparseable one is a
  500. `group=` on the history page is a 400.
- **`parentIndex=` and an out-of-enum `resolution=` refuse on `/library/all` and
  `/playlists`** as well as on a section listing.
- **`/hubs`, and a SHOW library's `/hubs/sections/{id}`, 500 fifteen filter keys**
  (`duration lastViewedAt lastRatedAt viewCount skipCount viewOffset userRating unwatched
  resolution hdr mediaSize file parentIndex slug group`) for any non-empty value and
  whatever operator; an EMPTY value and an unknown field are 200s, and a MOVIE library's
  hub page takes all fifteen.
- **`/system` PAGES** -- `totalSize` FIRST, no `offset`, and the response headers -- where
  `/system/scanners/{N}` ignores paging; the plug-in framework's container is ordered
  `[totalSize,] noHistory, replaceParent, size, identifier`. **`/system/search` is a real
  empty container** carrying `prefsKey`. And the whole `/system` subtree has its own
  envelope: **PATCH is a 405** (the only one on the surface), a bare OPTIONS an empty 200,
  every other verb an EMPTY-bodied 404.
- **Four more single-segment paths are the plug-in dispatcher's POPULATED listings** --
  `/media` (grabbers, providers, subscriptions), `/services` (ten), `/status` (sessions),
  `/statistics` (empty) -- and `/security` is a real empty container with NO `content` key.
  **A KNOWN single-segment path answers the dispatcher for every verb it does not
  implement**, `PUT /identity` and `PUT /hubs` included; the real writers are `POST
  /playlists`, `POST /playQueues` and `POST`/`DELETE /butler`, and `/log` refuses on its
  parameters first.
- **The playlist item paths take one verb each**, like the collection's: `/playlists/{id}`
  and `/items` GET/HEAD/PUT/DELETE, `/items/{iid}` DELETE (PUT is a 400, a non-numeric id
  a 404), `/items/{iid}/move` PUT. **`DELETE /library/metadata/{playlist}` is an empty 200
  that deletes NOTHING** where `DELETE /playlists/{id}` is a 204 that does.
- **A repeated `id=` on the section save path edits nothing** (the comma list edits both);
  **`POST` on `/split` and `/merge` is a 404** like the other action leaves; **a write to
  `/library/parts/{id}` resolves the part FIRST** (an unknown or malformed id is a 404
  whatever the parameters); and **`/subtitles` answers 404 to DELETE, PATCH and OPTIONS**.
- **A library DELETE pushes one `state 9 deleted` frame per row** -- `type: 12`, an EMPTY
  title, `sectionID` the NEGATIVE section id -- between the 10% and 40% activity updates.
- **`section_prefs_new.json` is per VERSION too** (the fourth such fixture): on 1.42.2 the
  AGENT's settings are appended whatever the `type=`, and 1.43.4 adds five generic rows to
  `tv.plex.agents.none` at types 1 and 2. Every setting's own attributes are identical.
- **An item's `audioLanguage`/`subtitleLanguage` enum is led by the ITEM's own stream
  languages** -- bare 639-1 tags, audio first then subtitles, each named in its own
  language -- before the generic regional list.
- **A check that asserts an ERROR status cannot tell a refusal from a crash.** Cycle 18's
  hub refusal passed its own fail-then-pass proof while raising a `NameError`, because a
  crash is also a 500. Read something back afterwards.

- **A BOOLEAN filter answers the true set to exactly two requests** -- `field=1` and
  `field!=0`. Every other operator and every other value answers the rows where the
  column is FALSE: zen's `unwatched=1` is 3,866 of 3,887 and its `!=1`, `==1`, `<=1`,
  `>=1`, `<<=1`, `>>=1`, `=0`, `==0`, `!==0`, `=2`, `!=2`, `=`, `=01` and `=true` are all
  the same 21. `!=2` is what rules out "parse the value, then negate".
- **A row with NO value for a numeric column matches NOTHING**, `!=` and `!==` included --
  zen's 742 rated movies split 368/374 across `rating<=7` and `rating>>=7`, so the 3,145
  unrated ones are in neither -- and **`==`/`!==` there are an exact STRING comparison of
  the stored number**: `rating==7` is 0 where `rating==7.0` is the 6 rows `rating=7`
  finds, while `year==2000` is the same 39 as `year=2000`.
- **A `tag`-typed COLUMN takes all eight operators** where a tag-TABLE field takes two.
  `contentRating`'s `<=`/`<<=`/`>=`/`>>=` are a BYTE-ordered comparison over the stored
  string (`>=PG` 802 + `<<=PG` 3,085 = 3,887), an absent value compares as `""`, `=` is
  case-SENSITIVE and `==` is not. **`resolution` is the opposite**: every operator, `!=`
  included, answers the `=` set -- and `resolution=` matches ANY of a row's Media.
- **The section `Meta`'s `active` follows the REQUEST**, not the library: `?type=3` marks
  `season` and `?type=4` marks `episode`. **`?includeDetails=1` honours
  `includeAdvanced=1`** (9 sorts bare, 15 with it), carries no `folder` Type row and marks
  none of them active.
- **A row that HAS children names its child list** -- `key="/library/metadata/{id}/
  children"` on every show and season of both references, where an episode keeps the bare
  item path. The client follows `key` verbatim, so a season carrying the bare path opens
  the season's metadata row instead of its episodes and the page draws nothing.
- **`excludeFields=` strips the field from every CHILD ELEMENT** (`id` off Media, Part,
  Stream, Genre and Guid; `file` off Part; `url` off Image; `value` off Rating), while
  **`excludeElements=Image` and `=UltraBlurColors` are NO-OPS** on both references like an
  unknown name. **`checkFiles=` is validated**: `0`, `1` and `01` pass, everything else --
  an empty value, `2`, `true`, `-1`, `1.5`, a leading or trailing space -- is a 400.
- **`/library/metadata/{id}/extras/all` is a real leaf**, the same container `/extras`
  answers; only `/extras/{n}` is the 404.
- **Four more ordering rules.** A `Meta`'s `Type` rows come before its `FieldType` rows;
  `Common`, `Feature` (`scrobbleKey` first), `Type` and the `/media/providers` library
  `Directory` are element buckets of their own (the last shared `/library/sections`'s and
  is a different order); the rows of `/library/recentlyAdded` and `/library/onDeck` LEAD
  with `allowSync` and the librarySection triple, which `allowSync` on the ROW identifies;
  and a LIGHT row puts `Guid` directly after `UltraBlurColors`, before its tags, where a
  detail row puts it after `Genre`/`Country`.
- **A real PMS's JSON key order IS its XML order** -- the container's, the row's,
  `Media`'s, `Part`'s and the row's child-element keys. `xmlout._split` is the one answer
  both writers ask; ordering only the XML left the two formats disagreeing on every
  response.
- **`sort=duration` and `duration=` read the MEDIA's duration**, not the row's column, and
  on a SHOW or SEASON listing a media column is read from the LEAVES and matches when ANY
  leaf does (zen's `duration<=1200000` and `>=1200000` answer 39 and 483 of 505).
- **The default listing order IS `sort=titleSort` and every tie falls back to `titleSort`
  ASCENDING, in both directions** -- re-measured against zen's own ordering of 3,887 real
  rows, so collation cancels out of the comparison.
- **A library EDIT scans only when the form carries `language` or `scanner`** -- `agent`
  alone, with `name`, or with a `prefs[...]` is a 200 with ZERO frames -- and it REFUSES a
  `type` naming a different type from the library's own. **`?path=` SCOPES a scan**: the
  section's own Location, an empty value and no path scan everything; a parent of it, a
  child of it and a path it does not hold scan nothing (no `progress` rows, the activity
  0 to 100 with an empty subtitle).
- **Three butler tasks push the wrong thing.** `RefreshLibraries` runs a section SCAN per
  library, not a metadata refresh per row; `OptimizeDatabase` starts at 0 with an EMPTY
  subtitle and steps 0/10/40/60 to an `ended` at 100; `CleanOldBundles` has SEVEN updates,
  ending on two `Deleting N metadata bundles` steps at 25 and 50.
- **A show library's scan names the SHOW before its seasons**, in a `progress` row with no
  activity update of its own; **`provider.subscriptions.process` is the one `cancellable:
  true` activity**; and **an analyze pushes NO `backgroundProcessingQueue` frame** --
  plexsim invented one. **`PUT /updater/check` pushes one `update.statechange` frame**
  carrying an `AutoUpdateNotification` row.
- **`DELETE /devices/{id}` and `DELETE /accounts/{id}` are 404s**, for an id the server
  lists and one it does not; **`PUT /activities/{x}` is a 400** where its DELETE is a 404;
  and **`HEAD /:/prefs` is a 200**, the one disagreement in a HEAD sweep over sixty paths.
- **Four settings answer 403 to a PUT** (`PlexOnlineUrl`, `TranscoderH264Options`,
  `TranscoderH264OptionsOverride`, `allowMediaDeletionLanOnly`) even when the value written
  is the one already stored, while **`ButlerStartHour` and `ButlerEndHour` take a WORD**
  with a 200 that stores nothing -- alone among the 38 integer settings.
- **`server_prefs.json` is the FIFTH per-version fixture**: `AllowLegacyAgents` and
  `CLDriver` are 1.43.4-only, and a write to one must be refused as an unknown preference.
- **The Add Library form refuses a missing or unlisted `language`** (the agent's own list,
  case-insensitive), **accepts an EMPTY `location`** and **accepts a NUMERIC `type`**,
  creating a library of that type.
- **`filters=` on a notification channel is a WHITELIST and `all` is not an event type**, so
  a socket opened with `filters=all` receives NOTHING and looks exactly like a server that
  pushes none. Always open with a control write.

- **A season's `titleSort` follows the title it STORES, not its `year`.** 740 of zen's 741
  seasons carry none, and the one that does -- the only one with a year -- carries
  `Egghead`, its agent's arc name, not the `Season 22` it displays. Cycle 7 fitted the year
  rule to kids' season 7, whose `Season 1` came from an agent. And **`Specials` and
  `[Unknown Season]` are generated titles too** (season 0 and a season with no index): no
  stored sort title, absent from `title=` and from the section search box, in the `#`
  bucket.
- **An episode carries `parentThumb` only when the season's poster is the season's OWN.**
  654 of zen's seasons hand it down and its 27 that borrow the show's hand down nothing,
  0 exceptions over 9,293 episodes on both references. `art` is the opposite -- handed down
  borrowed or not -- so it is a property of the FIELD.
- **`viewCount` and `viewOffset` read a row with NO stored value as BELOW every value and
  equal to 0** (`=0` and every `<=`/`<<=` match it, nothing else does); `skipCount` is not
  in the family. **A NULL date matches NOTHING**, on every operator -- the old "a null date
  is before everything" came from fun -- and a date's `!=` really compares.
- **`audioLanguage`/`subtitleLanguage`: `=v` is "SOME stream is v" and every other operator
  is "NO stream satisfies the comparison"** (`!=`/`==` compare equality, `!==` inequality),
  so a row with no stream of that type matches all six vacuously. The 3-letter code
  normalises under every operator. **They do NOT read the leaves**: a show or season row
  answers nothing, where `duration`/`resolution`/`mediaSize` do read them.
- **A show library's media AXES are built from the leaves** (`/resolution`,
  `/audioLanguage`), and **`file=` is a 500 on a SHOW listing** while a season's reads the
  leaves like every other media column.
- **`unmatched` IGNORES the operator**: only the literal `1` selects the unmatched rows,
  under all eight. **On a PARENT row `duplicate`'s two halves OVERLAP** -- each is evaluated
  per leaf, so a show with one duplicated episode and one plain one is in both.
- **`id==<n>` and `id!==<n>` DO compare a number** (overturning cycle 11), and **only
  `titleSort` folds case** among the four unlisted string fields -- `summary`, `tagline`
  and `originalTitle` compare BYTES, so every capital sorts below every lower-case letter.
- **`mediaCount` and `createdAt` are real columns**: `mediaCount` is the number of Media,
  read from the LEAVES on a show or season, and `createdAt` tracks `addedAt`.
  `sort=mediaCount` is a real ordering. Both were recorded in cycle 15 and read off an
  attribute no row carries, so they selected nothing.
- **The parse surface is wider than the filter surface, in three more ways.** `theme`,
  `banner`, `art`, `thumb` and `slug` each parse under every operator with an empty
  `fieldTitle` and are a **500** on a listing; `type<op>` parses (the one Rule with NO
  `fieldTitle` key) and filters on the TYPE NUMBER; `sourceType` parses an integer,
  reports NOTHING, constrains nothing, and any value but `1` adds the default
  `Order: id ascending` block. **`guid` takes only `=` and `<=`, both reported as `<=`**,
  and every other operator is an unknown FIELD.
- **`group=` takes five more keys** -- `originalTitle`, `createdAt`, `mediaCount`,
  `ratingCount`, `absoluteIndex` -- so the group surface follows the FILTER surface rather
  than the Meta.
- **`/unwatched` answers only the library's own top-level type** (and flips its `viewGroup`
  to `episode` for any other), **`/recentlyViewedShows?type=N` is a typed query**, and
  **`/sorts`, `/filters`, `/common` and `/search` 400 every unparseable `type=`**, the
  empty value included. **A section search row carries no librarySection fields.**
- **An EDIT does not move `updatedAt`** -- an artwork write does -- and **an artwork URL's
  version IS the row's `updatedAt`**, on 738 of 738 owned paths across both references.
  Every borrowed reference to it follows.
- **Eleven more columns take an item edit and a lock** (`absoluteIndex`, `updatedAt`,
  `createdAt`, `skipCount`, `viewOffset`, `ratingCount`, `type`, `viewedLeafCount`,
  `contentRatingAge`, `mediaCount`, `extraType`), only `absoluteIndex`'s value being read
  there. **The SECTION path parses every numeric column**: seven are 200/500 and eight are
  a 500 even for a valid number, and four really store.
- **`year` is bounded to signed 32-bit**, and that refusal was the last hole in "a save a
  real PMS refuses writes nothing". **A negative `rating` or `audienceRating` REMOVES the
  field.** **`theme` and `banner` are ARTWORK columns**: the url is discarded and the
  column becomes the row's own versioned path.
- **An episode's inherited `contentRating` is a READ-TIME fallback**: clearing the
  episode's own makes it read the show's at once, and a later show write moves it again.
  **A collection and a playlist take the ITEM path's whole edit validation.**


- **The two forms of `/system/agents` carry different children.** The bare form has a
  `MediaType` row per media type an agent serves and NO `Language`; `?mediaType=N` has
  the Language list and no MediaType. And **that endpoint writes its `Language` and
  `MediaType` as REPEATED JSON KEYS** rather than arrays -- 150 occurrences on zen, 195
  on kids -- so a JSON parser keeps exactly ONE per agent while the XML carries all of
  them. Every other repeated child on every other path is a proper array.
- **A `Feature` element is never self-closed**: all fifteen on `/media/providers` are
  paired empty elements on both references, where `Directory`, `Action` and `Pivot` in
  the same document self-close.
- **A SEASON's `audioLanguage`/`subtitleLanguage` inherit option is `:Series default`**
  -- a season inherits from its SHOW, where a show says `:Account default` and a movie
  has no such setting. plexsim rebuilds that enum at read time and hardcoded the account
  label.
- **An EPISODE's `/tree` is the ancestor CHAIN** -- three `MetadataItem` nodes, the show,
  THAT episode's season and the episode, with no siblings. A season's is the season plus
  its own episodes and a show's is the whole tree.
- **`/hubs/home/recentlyAdded` is populated at `type=1` and `type=2` ONLY.** Every other
  type answers `size 0` on both references, with and without `sectionID`,
  `pinnedContentDirectoryID` and `includeMeta` -- and both hold episodes, so it is not a
  content difference.
- **A `/library/onDeck` row LEADS with `allowSync` and the librarySection triple**, like
  a `/library/recentlyAdded` row; **a section's `/hubs/sections/{id}/continueWatching/
  items` container is `{size, allowSync: true, identifier}` and nothing else**; and **an
  artwork path with an EXTRA SEGMENT after the version is a 404** (a single trailing
  slash and a bad version stay 200s).
- **The per-user-state block sits MID-ROW, before the year, on every row type** --
  `viewCount`, `skipCount`, `viewOffset`, `lastViewedAt`, `userRating`, `lastRatedAt`
  after the ratings and before `year`/`parentYear` on a show, a season and an episode,
  as it already did on a movie -- and **`parentSummary` comes directly after
  `parentTitle`**. Both formats move with it: a real PMS's JSON key order is its XML
  order.
- **A collection and a playlist take EVERY `/:/` state write**, and `rate`, `progress` and
  `unscrobble` store on them as on an item -- the rating survives the unscrobble. They part
  on the SCROBBLE: a playlist's `viewCount` counts up from the 1 it is born with and its
  unscrobble clears it, while a collection's scrobble changes NOTHING (it never carries
  `viewCount` and does not even clear a `viewOffset`), so only the watched half is the
  no-op. Neither reaches a member. The block comes back MID-ROW -- `userRating, viewOffset,
  viewCount, lastViewedAt, lastRatedAt`, after a collection's `ratingCount` and after a
  playlist's `composite` -- on every read but `/playlists/{id}` and the create response.
- **A SHOW library's `/analyze` frames every episode LEAF three times** (analyzing,
  thumbnailing, none), with no activity: the section analyze reads the LEAVES, which is why
  a movie library's fifteen for five movies is the same rule.
- **A collection and a playlist answer the ITEM action leaves**, splitting by ROW TYPE:
  `/analyze` 200 on a collection and 400 on a playlist, `/allLeaves` and `/grandchildren`
  real containers on a collection and 404s on a playlist, `/related` empty on both but with
  the librarySection triple and `allowSync` only on a collection, `/extras` and `/match`
  their 400s, and the artwork writes landing like an item's.
- **`PUT /library/collections/{id}` is the ITEM edit path** -- a form that writes nothing,
  an unknown `.value` and a PREF name are each a 400, and `titleSort.value` with its lock
  stores -- and **`POST`, `PATCH` and a bare `OPTIONS` are 404s** there, on its `/children`
  and on `/library/metadata/{id}` for both list types.
- **Twenty-nine more columns take an item edit and a real LOCK** (and no value), while
  eleven names -- `album artist chapterSource childCount hasChapterVideoStream leafCount
  logo poster role sourceType track` -- are a 400 on both and `hdr10plus` is a 200 that
  stores nothing. Five of the twenty-nine are the 1.43.4-only codec family and are HELD
  OUT: plexscratch is 1.43.4 and zen cannot be written, so 1.42.2 is unmeasurable there.
- **A state write's `key=` is a BARE ratingKey**, not a path: `/library/metadata/17` is a
  404 on scrobble/unscrobble and a 500 on rate/progress. The four then diverge -- rate and
  progress int-parse the key (500), scrobble and unscrobble look it up (404); `/:/progress`
  accepts ANY integer key with a 200 that stores nothing; rate validates its `rating`
  before the identifier and progress validates the identifier before its `time`.
- **`/:/progress?time=` takes a negative value and a leading `+`** -- a negative one stores
  nothing -- while `abc`, empty, `1.5`, `0x10`, `1e6` and a leading space stay 500s.
- **`DELETE /library/sections/{id}/indexes` moves the SECTION's `updatedAt`** and nothing
  else's, and **a `/refresh` moves NO `updatedAt` at all** -- so it re-versions no artwork,
  because cycle 20 established the artwork version IS that stamp.
- **A TEXT library preference is percent-decoded a SECOND time**, then escaped for storage
  with `%`, `:` and `|` in lowercase hex; a second decode that fails stores EMPTY, which is
  all cycle 17's "a value carrying `%` is stored EMPTY" ever was.
- **`PUT /library/metadata/{playlist}/prefs` is a 400**, the item writer's own refusal.
- **An EMPTY response under `/system` carries NO `Content-Type`** -- every 404 on every
  verb and the bare OPTIONS 200 -- where a 404 anywhere else carries `text/html` and a body.

- **`excludeElements=` honours an ALLOW-LIST** -- `Media Part Genre Country Guid Rating
  Director Writer Role Producer` -- and every other name is a NO-OP, `Extras`, `Related`,
  `Preferences`, `Field`, `Chapter` and `Review` included. Cycle 19's "every other element
  really goes" was a two-name sample generalised the wrong way. **`Part` is on the list**
  (it takes the Streams with it and adds `partCount` to the Media), and **the option reaches
  the rows INSIDE the inline `Related` and `Extras` blocks**, not just the top-level row.
- **Five more options carry `checkFiles`'s value grammar** (`skipRefresh`,
  `asyncCheckFiles`, `asyncAugmentMetadata`, `asyncRefreshLocalMediaAgent`,
  `asyncRefreshAnalysis`): `0`/`1`/`00`/`01` are 200s and everything else a 400. **And four
  `include*` options parse their value as an INTEGER** -- `includeExtras`,
  `includeRelated`, `includeChildren`, `includeOnDeck` take `01` -- while
  `includePreferences` compares the string. **`includeRelatedCount=0` EMPTIES the hub**
  (`size 0`, `more true`), where reading `0` as absent falls through to the default of five.
- **A real PMS writes ONE element per line and ends the document with a newline** (236 of
  them in one zen movie, 34 on kids). **Six more names are written PAIRED when empty** --
  `MediaContainer`, `Stream`, `Hub`, `Extras`, `Media`, `Activity` -- while `Part`
  self-closes, so it is an element-NAME table and not "can hold children".
- **Three more attribute orders**: a `Part` leads `accessible, exists`, a section's pivot
  `Directory` leads `secondary` (and `prompt, search`), a `Feature` leads `flavor`.
  `_ATTR_ORDER` is a topological sort of observed PAIRS, so a pair no reference emitted
  falls to the END -- that is its documented failure mode, and these were all of it.
- **A LIGHT row's JSON child order was never the light order**: `json_body` stripped the
  `_light` marker before the orderer read it, so cycle 19's "one answer both writers ask"
  held everywhere but there.
- **A text `sort=` compares RAW CODEPOINTS** -- case-sensitive, no accent folding, so
  lowercase sorts ABOVE uppercase (`iHostage` before `Zero Tolerance` descending). It is
  the SORT side of the split cycle 20 measured on the FILTER side; `titleSort` stays folded
  because no zen row carries a lowercase-initial stored sort title to measure it with.
- **A filter key whose NAME keeps a comparison character is an UNKNOWN FIELD** --
  `collection==`, `year>=`, `title!!` are a 200 listing every row and a 200 `Error` at
  `/query/parse`. A pair with NO `=` at all is still the 400 cycle 11 measured.
  **When you probe an operator, send the client's encoding**: `year>>=2000` is the
  parameter `year>>`, so `collection%3D%3D=zzzz` is the parameter `collection==` and not
  the `==` operator -- a whole finding was wrong on that before it was re-sent.
- **A bare `type=<v>` on the ITEM edit path is an accepted no-op** (`type` is one of cycle
  20's lockable names); it is only on the SECTION path that it is the scoping parameter.
- **An artwork frame carries the LIST row's own type** -- a collection's 18, a playlist's 15
  with `sectionID "-1"`. A stored list row carries no `type`, so a lookup keyed on it
  answers the movie's 1, and the client builds the URL it updates from that number.
- **`/library/collections/{id}/{refresh,unmatch,analyze}` answer an EMPTY body**;
  **`/resources/{anything}` is a real empty 200 on every verb**; **`/search/voice` with no
  parameters is a 400** (a 404 once `query=` is given); **`/search` is a real ROUTED
  endpoint** whose writers are a 400, not the plug-in dispatcher's listing.
- **`PATCH` is a 404 on `/library/sections`, `/status/sessions`, `/media/providers` and
  `/playQueues/{id}`**, and `/accounts/{id}` refuses POST too. **`/system/proxy` is the one
  path in that subtree that answers its own 400 to every verb**, GET included, rather than
  cycle 18's empty-bodied 404.

- **A `/search` row is the GRID row with a six-attribute lead** -- `allowSync,
  librarySectionID, librarySectionTitle, librarySectionUUID, personal, sourceTitle` in
  front and nothing else different. plexsim answered the DETAIL row there, which on a real
  library is several times the bytes per hit.
- **A `Meta/Type`'s active SORT follows the asked `type=` AND the `sort=` parameter**: it is
  marked only inside the Type row matching the asked type (a movie library asked `type=2`
  marks NOTHING), and which one is marked is the requested sort -- none given lights the
  default, `sort=addedAt:desc` lights `addedAt` with `activeDirection="desc"`, and a spec
  naming no advertised key lights nothing. Different attribute from cycle 19's `Type@active`.
- **`?includeDetails=1&includeAdvanced=1` carries NO Duplicates Filter row at all.** Cycle
  16 measured that path without `includeAdvanced=1`, where the row does not exist to carry
  a flag, so "carries no `advanced`" was the absence of the FLAG, not of the ROW.
- **A `/library/tags` row ends with `thumb`**; **the `/hubs/sections/{id}/manage` Hub leads
  with `identifier` and SELF-CLOSES** -- so `_ALWAYS_PAIRED` is not a flat name list for
  `Hub`: it is paired when it carries a child collection and self-closed when it carries
  none, the rule cycle 22 left open for `Directory`. **A `Common` row leads with the
  GRANDPARENT pair** and puts the parent pair before `ratingKey`.
- **The `_xml=` override has to reach the JSON writer too.** `_send_container` POPS it
  before either writer runs, so `/library/sections/{id}/common` bucketed as `Video[common]`
  and its JSON keys stayed in build order -- the last path where plexsim's two writers
  contradicted each other, which a real PMS never does (re-confirmed over 1,241 pairs).
- **A MOVIE's unmatch pushes TWELVE frames**, opening on `2/matching` then `3/matching` --
  a pair plexsim emitted nowhere -- with an activity that steps through a second TITLE,
  `Analyzing Media`, before returning to `Updating Metadata`.
- **A collection and a playlist answer `/refresh` and `/unmatch`** with their own sequences,
  and a collection's refresh is GATED ON ITS GUID: three frames on an agent guid, eight once
  an unmatch has given it a `local://` one. A collection's `/analyze` is the activity PAIR
  alone -- its members are never framed.
- **Three more read-only routes refuse every other verb**: `/hubs/home/recentlyAdded`,
  `/hubs/sections/{id}` and `/library/metadata/{id}/children`.
- **`xmlout._CHILD_ORDER` holds against a real library** -- 203 ordered pairs witnessed
  across 2,994 paths, zero contradictions, on rows carrying `Chapter`, `Marker`, `Review`,
  `CommonSenseMedia`, 155 roles and 5 Media. Do not re-sweep it without a new row shape.

- **The extended title's LANGUAGE part has its own rule**, settled at the third attempt: the
  name is carried only when it is a SINGLE token that is one of the title's words, with any
  TRAILING PARENTHESISED GROUP stripped from the name first. `Русский` in `Русский (Full) -
  ...` and `English (Weeb)` in `English (Some-Stuffs)` are carried; `norsk bokmål` and
  `Tiếng Việt` in their own titles are not. Cycle 22's whole-name rule broke the recorded
  corpus and the first-word rule alone missed 179 of zen's rows -- **a model fitted on one
  corpus must be scored on the other before it is written.**
- **Five more stand-ins**: `Signs`/`Songs` for Forced, `CC`/`HI` for SDH, `1.0` for Mono,
  `HDR`/`Dolby` for the dynamic range, and the RAW ffprobe codec name with punctuation
  folded (`aac` carries `HE-AAC`, `H264` carries `H.264`). **`eia_608` is labelled
  `Closed Captions`**, and **a sidecar's codec suffix is its `providerTitle`** with
  `External` the fallback -- cycle 17 measured the fallback alone, on one server.
- **`AC-3` in a title carries `AC3` and is NOT modelled** (132 rows): `VC-1` does not carry
  `VC1` and `E-AC3` does not carry `EAC3`, so no punctuation rule fits and a general one
  breaks 58 other streams. Recorded, not fitted.
- **`languages.json` was missing thirteen codes** zen's streams use (`cha guj hye kat mar
  mdr mon mya nno pan sin yor zul`). **A VIDEO stream carries `dub`** and never `comment`
  or `hearingImpaired`. **A layout ffprobe cannot name** (`8 channels (FL+FR+...)`) must be
  stripped of its trailing space.
- **zen's stored `displayTitle` carries the codec and plexsim's catalog form does not** --
  that is the recorded CLIENT-TITLE rule (those rows are read as Plex Web), not a defect.
  Score `mediainfo` on the extended title, which is the catalog form.

- **A SEASON's unmatch pushes ONE `matching` frame and a SHOW's the movie's PAIR.** The
  season then runs the SEASON cascade (28 frames, 9 of them the state-dependent media
  pass) where plexsim sent the seven-frame EPISODE form; the show runs 46 frames of its
  own where plexsim sent NONE. The show's activity is the MOVIE unmatch's -- `Analyzing
  Media` at -1 and 25, then `Updating Metadata` at 25 and 50 -- and only then does the
  subtitle follow each season. **A season cascade's activity has no `updated` frame**;
  the docstring said so since cycle 15 and the code called `finish_activity`, which adds
  one.
- **A show UNMATCH's activity opens with the show's BARE TITLE**, where its REFRESH opens
  with the frame title (`Firefly (2002)`) -- the one place on the surface where an
  activity's subtitle is not the frame title. From the season onwards both use it again.
- **`pixelAspectRatio` and `anamorphic` are a PAIR** -- 358 of zen's video Streams carry
  both, 1,330 neither, none only one. The value is ffprobe's SAR verbatim, present exactly
  when the sample aspect is not square, and `anamorphic` is TRUE when the ratio rounded to
  ONE decimal place is not 1.0. Sixteen distinct ratios witness it; the boundary is
  bracketed to (0.0406, 0.0667] and 0.05 is the only round number in it. A pixel-
  difference model is refuted: a 3840-wide `333:320` displaces 156 pixels and is false
  where a 716-wide `16:15` displaces 48 and is true.
- **The resolution label had no 480 WIDTH bucket**, so a wide short SD release fell to
  `sd`: zen's three 856x358 Media are `480`. The entry is the smallest width zen has in
  the bucket, as the other five are; 720 is the largest below it and the boundary is not
  bisected.
- **A search hit LEADS with `librarySectionTitle`**, before `ratingKey`, on `/hubs/search`
  (where its own `score` follows it) and on `/library/search`. It needs a marker, not a
  signal read off the row: `/library/all` and `/hubs/home/recentlyAdded` carry the same
  attribute in the middle.
- **`/match` answers its no-parameters 400 to EVERY verb**, HEAD, PATCH and a bare OPTIONS
  included -- cycle 17 recorded the sentence and implemented two of the seven.

- **Only THREE identity headers move anything at all**: `X-Plex-Platform`,
  `X-Plex-Product` and `X-Plex-Language`. `X-Plex-Features`, `X-Plex-Client-Profile-Extra`,
  `X-Plex-Drm`, `X-Plex-Model`, `X-Plex-Provides`, `X-Plex-Session-Identifier`,
  `X-Plex-Client-Identifier`, `X-Plex-Device`, `X-Plex-Device-Name`, `X-Plex-Text-Format`,
  `X-Plex-Version`, `X-Plex-Platform-Version`, `X-Plex-Sync-Version` and `X-Plex-Protocol`
  each sent alone move NOTHING on ~80 paths -- not a Media, Part or Stream row, not the
  XML/JSON default, the gzip decision, the paging defaults, a response header or a hub.
- **`X-Plex-Platform: Samsung` appends a hidden `includeInGlobal` to a library's `/prefs`**
  -- that exact spelling, header or query (the HEADER wins when both are sent), both
  library types, and the EXISTING-library leaf alone. The second platform-keyed reshape
  after `Roku` on `/media/providers`.
- **A modern agent's `Language` rows are TITLED and ORDERED per language** (45 rows, all
  six modern agents sharing one list). The ORDER moves with the titles, so the list is
  stored per language (`agent_languages.json`, read from the XML -- the JSON writes the
  rows as repeated KEYS) rather than translated string by string. The NO-HEADER default is
  NOT English: each language is named in ITSELF, and an UNRECOGNISED language falls back
  there rather than to English, alone on the surface.
- **`/media/providers?includePreferences=1` carries each library's whole `Preferences`
  block**, byte-identical to `/library/sections?includePreferences=1`'s -- the per-SHOW
  `episodeSort` variant included -- as the FIRST child of the library `Directory`. The
  `Home` row, which names no library, gets none.
- **`/library`'s three `Directory` titles are localised and `/system`'s identical string is
  not.** The plug-in framework's container (`noHistory` is its signature) is not localised
  at all, which is why the translation table's attribute key is not enough on its own.
- **A `/tree` node is a ROW and its `Setting` rows stay English**, where the same settings
  under `?includePreferences=1` move. It is keyed by `id` rather than `ratingKey`, so the
  row guard did not see it and plexsim translated what a real PMS leaves alone.

- **Continue Watching is NOT On Deck.** `/hubs/home/continueWatching` holds only the rows with
  a stored `viewOffset` -- movies and episodes alike -- where On Deck holds one row per show,
  its in-progress episode or the one after the last watched, which has no offset at all.
- **Home DROPS the `home.ondeck` shelf when its rows are Continue Watching's** -- the element,
  not an empty one, on `/hubs` and `/hubs/promoted` alike -- and only when that set is
  NON-EMPTY. Cleared: both present. Next-up only: both. One episode in progress: On Deck gone.
  A movie in progress beside it: both again.
- **A SCROBBLE writes one history row per LEAF**, overturning cycle 9: a movie scrobbled twice
  adds two, a season adds one per episode, a show one per leaf, and an UNSCROBBLE removes
  none. The timeline half writes no row on the copy, which is its recorded session-bound limit
  and not a rule.
- **An episode history row carries `title` and `originallyAvailableAt`**, in an order of its
  own: `historyKey, key, ratingKey, librarySectionID, parentKey, grandparentKey, title,
  grandparentTitle, type, thumb, ..., index, parentIndex, originallyAvailableAt, viewedAt,
  accountID, deviceID, viewType`. A movie's row is a subsequence of it.
- **A PLAYLIST is on Home's Recent Playlists shelf and in both search endpoints** -- the
  `/playlists` listing row, `lastViewedAt:desc`, `hubKey` naming its own rows. A COLLECTION is
  found as a `tagType: 2` tag row instead, which is a different shape.
- **The UNTYPED `/library/all` lists collections**, byte for byte the row `?type=18` answers,
  sorted in by ratingKey. The section form lists them on neither server.
- **`removeFromContinueWatching` does NOT reach a SECTION's own Continue Watching shelf.** It
  empties Home's On Deck, Home's Continue Watching and the section's `/onDeck`, and the row is
  still in `/hubs/sections/{id}/continueWatching/items` afterwards -- stable to +20 s.
- **A `Common`'s `userRating` is a STRING** (`"8"`, not `8.0`) and sits directly after
  `mixedFields`, ahead of `guid`, while the ITEM row keeps the number.


- **A decision WITHOUT `hasMDE=1` runs the client's profile and can be a DIRECT PLAY**:
  `directPlayDecisionCode 1000` / `generalDecisionCode 1000`, both "Direct play OK.", with the
  Metadata row and `resourceSession` and NO transcode codes. The row is byte-identical to the
  MDE form's; only the container's codes differ, so "a direct play is only
  `mdeDecisionCode: 1000`" is the MDE form's rule. Measured: `Roku` and `Xbox One` direct play,
  `Chrome`/`Safari`/`Chromecast`/`Plex Desktop`/`Konvergo`/`HTML TV App`/`Opera` get `1001`
  (which plexsim cannot answer), and `Firefox`/`Microsoft Edge`/`Generic`/`Plexamp` get the
  `3000/2000/4005` "neither".
- **Only an OUT-OF-RANGE `mediaIndex` refuses a decision.** A missing, non-numeric, negative or
  empty one falls back to media 0; `partIndex` is never refused at any value; a `path` carrying
  a query string is a 200. This CORRECTS the older bullet that claimed all three -- and note
  neither reference holds a multi-part Media, so the `partIndex` half is measured on
  single-part rows only.
- **A decision answers exactly ONE Media** (zen's rk=401 has fifteen), and **`excludeElements=`
  is a NO-OP on a decision** -- the one exception to "every container that carries rows honours
  it".
- **A statistics `timespan` the server does not KEEP is an EMPTY 200**, not the nearest bucket:
  bandwidth keeps `0 1 2 3 4 6`, media `0 1 2 3 4`, resources `6`, and 5, 7, 99, -1 and 007
  answer `size 0`. `06` parses as 6 on all three.
- **`limit=` caps a statistics series and is PARSED** (`1`, `01`, `0`, `-1` are 200s and a
  non-positive one answers no rows; `1.5`, `abc`, empty and `+1` are 400s) **even where the
  value does nothing**, and **`sort=` is a real ordering validated per endpoint** --
  `metadataType` is NOT a media key.
- **The statistics `at` family is four operators**: `at>` means `>=`, `at<` means `<=`, `at=`
  is exact, and `at>=`, `at<=` and `at!=` are unknown keys that answer every row.
- **Neither `/status/sessions` nor `/statistics/{which}` is a PREFIX.** An extra segment after
  either is a 404 -- `/status/sessionsfoo`, `/status/sessions/1`,
  `/status/sessions/history/all/zzz` and `/statistics/bandwidth/zzz` included.
- **A decision arms one SESSION, not the item.** The timeline's `Bandwidths`, the session row's
  `Media@selected`/`Part@selected`/`Part@decision`/`Stream@location` and the transcode start's
  gate all key on `(session, ratingKey)`: a second session beating the same row is unarmed,
  the same session beating another row is unarmed, and only its own `stopped` beat disarms it.
  The identity is `X-Plex-Session-Identifier` (query or header) then the client identifier --
  **NOT** the decision's `session=` parameter, which names the transcode session.
- **A transcode START from a session with no decision is a 400**, on every protocol and every
  leaf of the family; a transcode decision for that session turns the same `start.m3u8` into a
  200. plexsim has no encoder, so its answer once a decision exists is its own 404 -- recorded,
  not modelled, because the reference's own `start.mpd` is a 404 there while its `start.m3u8`
  is a 200.
- **A decision row keeps its `summary`**, whatever the row type. The rule that dropped it for
  movies was fitted to the one copy row whose summary is the EMPTY STRING.
- **A list's `/unmatch` really writes**: a collection's `guid` becomes `local://{rk}`, its
  `ratingCount` is REMOVED and its `subtype` becomes `unknown`; a playlist's `guid` becomes
  `local://{rk}` and its `duration` is removed. The per-user state block keeps the slot
  `ratingCount` vacated. **A collection's refresh/unmatch activity ENDS at 100**, after an
  `updated` at 49. Beware: `_collection_row`/`_playlist_row` re-mint the guid on every read, so
  a write into the row the handler was handed reaches the FRAME GATE and never reaches a client.
- **`removeFromContinueWatching` is per LIBRARY TYPE**: a show library KEEPS the row in
  `tv.inprogress.{id}` and in its see-all list, a movie library REMOVES it from both. The shelf
  and the See All behind it always agree. Home's On Deck drops it either way.
- **The butler's wrapper steps once per task** -- `updated 100` for the task just finished then
  `updated round(100*n/(n+1))` for the queue, no step after the last, so N tasks push N
  hundreds and N-1 steps before `ended 100`. **`CleanOldBundles` INTERLEAVES its two messages
  with its own phases** and **`RefreshLibraries` reports and closes the wrapper BEFORE it
  scans**. `POST /butler` runs only what is DUE, which no row of `GET /butler` exposes, so
  plexsim runs every enabled task; a second POST is a full run, not one `queueRegenerated`.
- **A session with no beat lives THREE MINUTES**, not seventy seconds: measured ten times on
  the copy, gone between +184.0 and +210.4, which is a cutoff at or below 184 s plus a reaper
  tick of at least 26 s. `SESSION_TTL` is the cutoff; the tick is recorded, not emulated.
  Cycle 9's 70 came from ten-second clips before `long` mode existed.
- **`/status/sessions` is written by a DIFFERENT serialiser.** Its attributes come out
  ALPHABETICALLY -- the row, its `Media` and its `Part` -- with the decision flags after, where
  `xmlout._ATTR_ORDER` governs every other container; in JSON every child element's `id`, a
  `Media`'s `aspectRatio`, a `Rating`'s `value` and the row's `librarySectionID` are STRINGS
  where the detail row sends numbers, and `UltraBlurColors` is a LIST of one. Nothing else
  changes type. Cycle 22's 2,994-path order sweep could not see it: reading this path needs a
  live session, which a read-only reference cannot be given.


- **The URL is bounded at exactly 32768 bytes** and 32769 is a 400 -- the URL, not the request
  LINE, since the boundary does not move with the method. **The header caps are 1024 and 4096
  bytes and 100 LINES**, all three bisected: the third is a COUNT, not a size, so 200 KB of
  headers in fifty lines is a 200 and the old "~41 KB total" was that cap measured with
  410-byte headers. Every one of those refusals is a plain **400**; a real PMS sends `414` and
  `431` nowhere.
- **The request LINE is `METHOD SP target SP HTTP/<digit>.<digit>` with CRLF, and nothing
  else.** Two spaces, a trailing space, tab separators, asterisk-form and LF-only endings are
  each a 400; the two version digits are unconstrained, so `HTTP/2.0` and `HTTP/9.9` are 200s
  where `HTTP/1` is a 400; an HTTP/0.9 line is answered with a FULL HTTP/1.1 response; and a
  LOWER-CASE method gets no response at all.
- **An unparseable `Content-Length` is IGNORED and a negative one is a 500.** `abc` is a 200
  with no body read. Reading it with a bare `int()` kills the handler thread and the client
  sees a server that hung up.
- **`/photo/:/transcode` validates five more parameters.** `minSize` and `upscale` are BOOLEANS
  (`0`, `1`, `01`, `+1` only); `opacity` and `quality` take any integer; `blur` takes any
  integer and 500s a negative one; anything that does not parse as a whole number is a 400. The
  `width`/`height` 500s for a scale that degenerates are the image pipeline's, not a rule.
- **Every tag element inside a `/status/sessions` row is plainly ALPHABETICAL**, `Role`
  (`filter, id, role, tag, tagKey, thumb`) included -- there is no `actor` attribute on one.
  **`Player` is the one element there with an order of its own**: its string-valued attributes
  alphabetically, then `local, relayed, secure, userID`.
- **`Player.profile` is the profile the server RESOLVED, not the constant `Web`.** 23 of the 78
  profile names report another profile's NAME (`Roku` -> `Roku-7.x`, every browser but Firefox
  and Edge -> `Web`, `Plex Home Theater-RaspberryPi` -> `Raspberry PI`, a name in no file
  list). The resolution is `X-Plex-Client-Profile-Name`, `X-Plex-Platform`, `X-Plex-Device`,
  first match wins, an unmatched value falls THROUGH, header before query per name, and the
  attribute is omitted when nothing matches.
- **A borrowed artwork field is DERIVED from its owner, so every write to the owner re-derives
  it.** Clearing a row's own poster leaves it carrying the PARENT's path (a season falls back
  to the show, an episode to the season); a pick on a season CREATES `parentThumb` on episodes
  that had none; a pick on a show moves a BORROWING season's own `thumb` and every episode's
  own `art`; and when the owner's field goes, the borrower's goes with it.
- **A pick re-versions only the artwork the row OWNS.** A borrowed field keeps the owner's path,
  stamp and all -- zen's episode 11115 carries `art` naming its SEASON beside a `grandparentArt`
  naming the show at a different stamp. Only the field being picked becomes the row's own.
- **`durationTotal` and `storageTotal` are JSON STRINGS**, where every other number on that row
  is a number. XML cannot tell the two apart, which is why it took a JSON read on both
  references to see it.
- **The bare `/system/agents` carries a MODERN agent's 45 `Language` rows and no `MediaType`.**
  The `MediaType`-in-the-bare-form rule is the LEGACY plug-in agents' alone. A modern `Agent`
  row leads `identifier, name, hasPrefs, id`.
- **`?includeDetails=1` pages its `Directory`, `Type` and `FieldType` rows as ONE sequence** --
  20 + 1 + 8 = 29, so a 20-row window is `size=20 totalSize=29` and `Start=20` answers the meta
  rows. **A `Sort` row leads with `active, activeDirection`** and a **`Hub` puts `random`
  before `style`**.
- **The SINGLE history read is a third projection with an order of its own** (`historyKey,
  librarySectionID, title, ...`), not the listing row's and not an item row's.
- **Three more empty elements are written PAIRED**: a `Meta/Type` with no children, an empty
  `Related`, and the `/media/providers` `Home` `Directory` -- the one childless `Directory` on
  the surface that is paired, which `hubKey` identifies.
- **A real PMS ignores every conditional request header.** A part read carries an `ETag` and a
  `Last-Modified` and answers 206 to `If-None-Match`, `If-Modified-Since`, `If-Range` and
  `If-Match` alike -- never 304, never 412.
- **`/library/all/top` answers any type NUMBER** -- 0, 3, 8, 9, 10, 12, 13, 15, 18, 20 and -1
  included -- and 400s only a value that does not parse. The Dashboard asks for movie, show and
  ARTIST in one `all([...])` under one try/catch, so refusing the music type fails the whole
  Top Played panel.
- **`/library/all/top` breaks a tie on the MOST RECENT view, descending**, then on the
  ratingKey. The time is the history's own; the row does not carry it.
- **`/media/providers/metadata` and `/media/providers/metadata/group` are real endpoints** -- the
  MODERN agent-provider tables the Settings Agents pane reads, a different surface from
  `/system/agents`. A provider carries one `MetadataType` child per media type it serves and
  `tv.plex.agents.none` an EMPTY `uri`.
- **Every PERSON tag carries a 24-hex `tagKey`** -- `Director`, `Writer`, `Role` and `Producer`
  on a detail row, and `/library/tags?type=4|5|6|7` -- where `Genre` and `Country` carry none.
  The client builds every Cast & Crew link as `/library/people/${tag.tagKey}`, so a tag without
  one links to `/library/people/undefined`. plexsim MINTS the value (sha1 of the name) and
  resolves the minted key on `/library/people/{key}` as a real PMS resolves both forms.

- **`contentDirectoryID` scopes `/hubs` and all three `/hubs/home/*` leaves** -- and on
  `/hubs` it also REMOVES the `home.playlists` shelf. A comma LIST selects several (the form
  Plex Web sends, joining its pinned array), an EMPTY value scopes nothing, and the match is
  an exact STRING, so `01` selects no section. Cycle 15's "`/hubs/search` alone" is true of
  `/hubs/sections/{id}` only. **`/hubs/continueWatching` and its `/items` leaf VALIDATE it**:
  one comma member must be a POSITIVE INTEGER (`01`, `999`, `abc,1`, `1,` pass; `abc`, `0`,
  `-1`, `1.5` and a space are 404s) -- the SHAPE, not whether the section exists.
- **`excludeContinueWatching` is true for the integer 1 alone** (`1` and `01`) and drops BOTH
  `home.continue` and `home.ondeck`, on `/hubs` and `/hubs/promoted` alike. No other
  `exclude<Name>=1` spelling does anything.
- **A shelf's rows are the HEAD of its own `key` listing** -- 29 of 32 non-empty shelves byte
  for byte at `count=40` -- and the three exceptions are the `random="1"` ones, which are a
  random SAMPLE of the whole key set in random order. plexsim's Recently Watched shelf
  published `unwatched=0&viewOffset=0` and honoured neither. A NEGATIVE `count` is
  `len(rows) + count` clamped at 0 and says `more: false`, where `count=0` says `more: TRUE`.
- **`/hubs/home/recentlyAdded?type=2` is a MIXED, ROLLED-UP list**, capped at 50: two or more
  recently-added siblings collapse into their parent, recursively (one new episode stays an
  EPISODE, two become the SEASON, two new seasons become the SHOW), and a parent row is dated
  by its NEWEST leaf -- the opposite aggregation from `/library/recentlyAdded`, which dates a
  season by its earliest.
- **On Deck answers the DETAIL row on 1.42.2**, on all three paths, `librarySectionKey`
  included, plus `allowSync`, `librarySectionUUID` and `lastViewedAt` on `/library/onDeck`
  (the home shelf carries the key and no UUID). 1.43.4 drops the key, `Guid`, `Rating` and
  `Producer` and stamps `studio`: the SIXTH per-version shape. Cycle 21's "light, named by
  UUID" was read off a server with nothing in progress.
- **`/clients/profiles` is the client PROFILE catalogue**, not the hosted-client list: 78 rows
  bare and at `type=0`/`00`, SEVEN at `type=42` (`LOCAL` in the client's own enum) -- Android,
  iOS, Universal Mobile, Universal TV, Windows Phone, Windows, Xbox One -- every other value a
  400, an extra segment a 404, a trailing slash a 200.
- **The `/collections` leaf is the `/all` listing with a default `type=18`** and runs the same
  query: a `type=` the library holds answers those rows instead, an unknown `sort`, an
  unparseable `type` and an unparseable `limit` are each a 500, and `subtype`, `smart` and an
  empty `sort` are accepted and ignored.
- **A search tag hit is per LIBRARY, not per NAME** (kids' `drama` is two `Drama` rows against
  one tag id; zen's `comedy` carries 590 and 179), and its `count` is the size of the listing
  its own `key` names -- the library's top-level type. **A PERSON row carries its 24-hex
  `tagKey`**, after `tagType` and before the `thumb`, with `count` last; a `Genre` row carries
  none.
- **A `/library/search` tag row is the `/hubs/search` row minus `reason`, `reasonID`,
  `reasonTitle` and its own `score`** -- there the score is the `SearchResult` wrapper's alone,
  a NUMBER in JSON and FIVE decimals in XML (`score="0.52000"`).
- **A COLLECTION is found by `/library/search`** too, as a `tagType: 2` row: cycle 15's note
  was a copy that held no collection. And **`contentDirectoryID` scopes every row family** of
  `/hubs/search` -- the tag rows per library, the collections by library, a playlist by its
  MEMBERS' library.
- **`searchTypes=` is `/library/search`'s own filter and `/hubs/search` IGNORES it.** Six
  tokens -- `movies tv otherVideos music photos people` -- EXACT, case-sensitive and never
  trimmed (`Movies`, ` movies`, `zzz` select nothing), a comma list a UNION with unrecognised
  tokens dropped (`zzz,movies` is `movies`), an empty value everything. `collections`,
  `playlists` and `tags` are NOT tokens. `otherVideos` takes the movie library's TAG rows
  WITHOUT its movies, because an Other Videos library is a type-1 library too.
- **`/library/search?limit=` caps the WHOLE list**, not each hub, and refuses twice over like
  `/hubs/search`: a value it cannot parse is a 400, a non-positive one a 500, `01` fine.

- **A row's own artwork field is its NEAREST ancestor's that OWNS one**, and
  `parentX`/`grandparentX` always name that specific ancestor. Clearing a show's `art` removes it
  from every season AND episode (with the episodes' `grandparentArt`) and picking one back puts all
  of them back -- a borrowed field is DERIVED and is RE-CREATED, not moved. Giving ONE season its
  own `art` switches that season's episodes to it while their `grandparentArt` goes on naming the
  show; a later show pick leaves the owning season's own path and stamp alone. Clearing a row's own
  `art` drops it back to the ancestor's, as `thumb` already did, and the bare `/art` becomes a 404.
- **Every artwork write cascades, and both write paths re-version the OTHER field only when the row
  OWNS it.** An artwork UPLOAD cascades exactly as a pick does (plexsim's never called the cascade,
  so children kept the old stamp and a client that caches on it never refetched); a poster upload on
  a row that borrows its `art` leaves that `art` naming the owner, and so does clearing that row's
  thumb.
- **`/photo/:/transcode` validates its DIMENSIONS first and then its `url`**: a missing `url=` is a
  400, an EMPTY one a 404, an item artwork path of a row it holds the image, of a row it does not
  hold a 404, one the row does not OWN a 404 (the transcoder answers what the direct path answers),
  a server path that is not artwork a **500**, and a url with a SCHEME or no leading slash a 404. A
  generated tile there carries `max-age=259200`, so a grey square caches for three days.
- **`/:/resources/` is SEVEN names** -- `movie show artist photo .png` at 300x300 and
  `movie-fanart show-fanart artist-fanart .jpg` at 1280x720, byte-identical on both references, so
  they are PMS's own bundled assets -- and `library-art.png`, `playlist.png` and `album.png` are
  **404s** even though a real PMS's `/library` container advertises the first as its `art`.
  `?width=`/`?height=` are IGNORED there.
- **A 0/1 option's value is `+?0*[01]`**: `0`, `1`, `00`, `01`, `000`, `0000001`, `+1`, `+0` and
  `+01` pass, and `2`, `+2`, `-1`, `+`, `++1`, `1+`, `1.5`, `true`, empty and either surrounding
  space are 400s -- on `checkFiles` and its five siblings, on `minSize`/`upscale`, and on
  **`download=`**, which is one of the family. The four integer `include*` options take the leading
  `+` too. Cycle 22's and 23's "`+1` does not count" was a RAW `+` in a query string, which decodes
  to a SPACE.
- **A 416 is an error response, not the resource with no bytes**: `Content-Type: text/html`, a
  129-byte body, and neither `Accept-Ranges` nor `Content-Range`. plexsim sends its own text/plain
  sentence (cycle 1's deviation) with the header dropped.
- **Under `/web/*` a DIRECTORY is an empty `application/octet-stream` 200**, a trailing slash after
  a file is a 404, a path escaping the root is a **400**, and `.woff`/`.woff2` are
  `application/octet-stream` like `.ico`.
- **`X-Plex-Local-Asset: 1` marks an ITEM's own artwork and nothing else** -- not a section
  composite, not `/:/resources/*`, not `/web/*`, not a part read.
- **The `Content-Disposition` `filename*` escaper is "unreserved minus the dot"** (`.` -> `%2E`,
  `+` -> `%2B`, `-` kept): the same escaper as a local artwork candidate's `/file?url=`.
- **`/library/sections/{id}/extras/all` is a real listing QUERY** -- an unparseable `limit`, an
  unparseable or empty `type`, an unknown `sort`, ANY `group=` and an unparseable `parent` are each
  a 500, and so are `duration`, `resolution`, `mediaSize` and `file`, which the section GRID
  accepts. `extraType=`, `title=`, `audioLanguage=` and a good `limit` are 200s and really
  constrain.
- **`group=` is honoured on `/library/all`** (kids' 14 rows collapse to 4 by type) and an unknown or
  empty field is a 500 there too. **`sort=type` orders by the type NUMBER**, not the name.
- **`/library/metadata/{id}/extras/all` and the section's answer 404 to every verb but GET and
  HEAD** -- the bare `/extras` keeps its POST 400. A five-segment read-only route needs the gate as
  much as a four-segment one.
- **A clip is a row type plexsim does not hold, and its shape is recorded** (attribute order with
  `extraType` LAST, no `Guid`/`Rating`/tags/`Field`, a container with `allowSync="0"` and no
  librarySection triple, `art` the parent's verbatim while `/art` is a 404, `primaryGuid` on trailer
  rows only, six `extraType` values, three projections, uncapped, and in NO listing or axis
  anywhere). See `docs/findings.md`; the build waits on a catalog with extras in it.


- **`plexscratch42` is the writable 1.42.2 reference** (`harness/scratch42/`, kids `:32411`) -- a
  fresh install of the build plexsim reports, isolated from plex.tv, so every item is UNMATCHED and
  every SETTINGS count is short (151 prefs against zen's 173). Use it for grammar, never for a
  count, and never for anything needing real metadata or a claimed server.
- **An On Deck row carries no `librarySectionKey` and is the detail row MINUS `Field`** -- on a
  movie and an episode alike, on both versions. `/hubs/home/onDeck` and
  `/hubs/home/continueWatching` DO carry the key; `/library/onDeck` and the section leaf carry
  `allowSync` and the librarySection triple without it. No resume list carries `Field`.
- **`hdr10plus` takes an item LOCK on both versions**, where the five media-codec names beside it
  (`atmos`, `audioCodec`, `audioLayout`, `subtitleCodec`, `videoCodec`) are a **400 on 1.42.2** and a
  200 on 1.43.4 -- so plexsim, which reports 1.42.2, refuses those five and accepts `hdr10plus`.
- **Two settings answer 403 to a PUT on 1.42.2, not four**: `PlexOnlineUrl` and
  `allowMediaDeletionLanOnly`. `TranscoderH264Options` and `TranscoderH264OptionsOverride` take a
  write there and really store it, where 1.43.4 refuses both.
- **A `random="1"` shelf's sample is STABLE** -- 21 reads over seven minutes on zen answered the
  identical rows, a different client identifier gets the same sample, and the same shelf at another
  `count=` holds its own. What makes a real PMS redraw one is NOT measured, so plexsim mints a seed
  per (shelf, count) once and holds it.
- **An unauthorised refusal KEEPS the connection alive** -- the 401 with no token, the 401 with a
  wrong one, the 403 for an unknown single segment, the 404 for a path that does not exist, all
  `Connection: Keep-Alive` and really reusable. **The one refusal that CLOSES is the 400 for a query
  pair with no `=`**, and seventeen other 400s keep it alive.
- **A `collection[0].tag.tag=` write makes a TAG, not a collection.** The tag appears on the row, in
  the `/collection` axis and in both search endpoints (a `tagType: 2` row with a `count`), and
  `/library/all?type=18` gains NOTHING -- only `POST /library/collections` makes an item. The tell is
  the `guid`: a tag-only collection's search row carries none.
- **The ping timer is FREE-RUNNING: a pong must not trigger another ping.** The reference pings
  every 10 s whether the client pongs or not; plexsim read "a frame arrived" as "the interval
  passed" and answered a pong with a ping -- **903,480 pings in 35 seconds**. Every real client
  pongs automatically, and a probe that never pongs sees the correct cadence, which is how twelve
  cycles missed it.
- **One notification consumer that stops reading must not reach the request path.** A dead socket
  plus repeated writes took plexsim's write 105 to 20 s and timed out a second client's edit at 30 s,
  where the reference bounds the per-socket backlog at ~415 KB and never slows a request. plexsim now
  gives a sink a two-second budget and DROPS the channel -- it drops where the reference buffers.
- **A library DELETE pushes NO per-row timeline frames** -- only its eight `library.delete` activity
  frames, on a 3-row and a 200-row library alike. Cycle 18's fourteen `state 9` frames named another
  section and none of the library's rows: they were a one-off orphan cleanup. A single item's delete
  frame is real, with the row's own type and the POSITIVE sectionID.
- **`state 1` belongs in the timeline alphabet** (0 created, **1 created/processing**, 3
  queued/processing, 4 loading, 5 done, 9 deleted): a row's CREATION pushes eight frames -- `state 0`
  with an empty title, then five `state 1`s, `state 4 loading`, `state 5` -- plus a
  `library.update.item.metadata` activity per new row, so cycle 19's "no scan does a metadata
  activity per row" holds only for a scan that finds nothing new. plexsim cannot create a row during
  a scan, so the sequence is recorded and not built.
- **The eventsource channel has no five-minute deadline** on a real PMS (still open at 430 s), and
  neither transport sends `id:` or `retry:`, so nothing replays across a reconnect. plexsim's 300 s
  constant is gone; it still closes at ~256 s for an unfound reason (HANDOVER 3.3). **And the 101
  upgrade carries a `Date`** -- it was the one response on the surface without one.

- **`plexscratch42` answers the WRITE surface too, and the split is the VERB TABLE.** Held out write
  by write against plexscratch (1.43.4): the edit-field tables, the section save path, the three
  settings writers, the list writers, the state writes and the upload ladder are **identical on both
  versions** -- that is the negative result -- while four VERBS differ. On **1.42.2**: `PATCH` is not
  a method at all (a **500** on every path, `/identity` included, the same body `FROB` gets), all five
  `DELETE /library/metadata/{id}/{poster,thumb,art,banner,theme}` leaves are **404s**, a smart
  playlist accepts a comma-joined KEY LIST as its uri, and `PUT /playlists/{smart}/items?uri=` takes
  an ITEM uri and REPLACES the stored query with it. plexsim reports 1.42.2 and carried 1.43.4's
  answers to all four.
- **An EMPTY `thumb.value=` or `art.value=` clears the column** (200, the field gone, the bare path a
  404) on both versions -- and on 1.42.2 it is the **only** way to clear artwork, since that DELETE
  does not exist there. **A non-empty one is an artwork COLUMN write**: the url is discarded and the
  column becomes the row's own versioned path, exactly as `theme` and `banner` already did. The column
  has to be EMPTY for that to be visible, which is why cycle 20 read `thumb`/`art` as discarded.
- **`PUT /library/collections/{id}/prefs` really writes** (`collectionSort`, `collectionMode`; an
  empty 200, GET/POST/DELETE 404, an unknown setting 400) on both versions. Cycle 15 measured the GET
  alone and recorded the 404, so plexsim advertised both settings and 404'd the save behind them.
- **`absoluteIndex.value=` stores NOTHING** on either path or version (its `.locked=1` does record the
  lock), and **`lastRatedAt.value=` on the SECTION path is a 500** on both -- the ninth member of that
  set. **`hdr10plus` stores neither a value nor a lock**: cycle 30 confirmed the 200 and put the name
  in `LOCK_ONLY_FIELDS`, which records one, so the Edit dialog drew a lock on a field it cannot show.
- **Ten read paths refuse every verb but GET and HEAD** (`/statistics`, its two series,
  `/status/sessions`, `/clients/profiles`, `/playlists/sorts`, `/library/sections/prefs`, and
  `/library/sections`, `/media/providers`, `/media/providers/metadata` -- whose POST is their own 400).
  plexsim answered **200 with the whole GET body** to a bare OPTIONS on each, which is fix 4's trap
  from the outside.
- **A stored sort title EQUAL to a generated title is still stored.** Read off kids' own DATABASE:
  its agent-titled season 7 stores `title = ''` and `title_sort = 'Season 1'`, so the displayed
  `Season N` is generated in every case and what an agent supplies is the SORT title. plexsim treated
  such a value as absent, so `titleSort=Season 1` found nothing, `titleSort=` wrongly kept the row and
  the ordering keys ignored it -- one clause, three symptoms.
- **`title=`: a COMMA is an alternation and a DOT is literal.** `title=Steel,,` and `title=Love,`
  answer the WHOLE library, `title=Tears, of` answers the one row, and `title=Steel.` answers NONE.
  plexsim stripped both characters out of the query. The comma half lives in the filter PARSER, which
  already ORs a comma list and drops empty members -- on `title` the empty member is KEPT, and an
  empty query matches every row.
- **`title=` matches the ORIGINAL title as a consecutive run of whole words, unfolded** -- `Salaire
  de` and `Le Sal` hit, `Salaire peur` and `e Salaire` do not -- where the TITLE's own matcher allows
  gaps and folds. And **the in-library search box does not read `originalTitle` at all**; the hub
  search is the endpoint that does.
- **The hub matcher takes a multi-word query WORD BY WORD, in order** (`Tears Steel` finds "Tears of
  Steel" at 0.73; reversed it drops to the 0.33 fuzzy band), **tokenises the TITLE on WHITESPACE** so
  punctuation stays inside the word (`don't` finds "Don't Move"; `Delete` does not find
  'Control-Alt-Delete'), and **matches a ONE-character query against a title equal to it**. plexsim
  required the joined phrase, split the title on `[^0-9a-z]+` and had a two-character floor -- so two
  non-adjacent words of a title, or any title with an apostrophe, found nothing at all.
- **The reference's own files are readable, and they explain two tables.** `ssh ben@kids docker exec
  plexlab-plex` reads the image: `Resources/Profiles/` holds 78 `.xml` plus a stray `sugar.cmake`
  (hence 78 rows), a row's title is its `<Client name="...">` and not the file name, and the 23
  `Player.profile` aliases are **22 `redirect="..."` attributes plus one name-versus-filename
  difference** -- plexsim's table reproduces all 23 with zero errors. The `type=42` seven carry no
  marker, so that list is PMS's own. The decision's inputs are visible too: `redirect=`,
  `<DirectPlayProfiles>`, `<TranscodeTargets>`, `<CodecProfiles><Limitations>`.

- **A write's RESPONSE pages and excludes like a read's.** `POST /playQueues`, the playlist and
  collection item writes and their moves and deletes all honour `X-Plex-Container-Start/Size` and
  `excludeElements=` on both versions; plexsim gated both post-processors on GET and HEAD.
- **A write must build its answer inside the lock it wrote under.** Two concurrent
  `POST /playQueues` from one client are two 200s on a real PMS and were a 404 for the queue plexsim
  had just made in 9 of 60 pairs; two concurrent adds each answer the queue AS THAT ADD LEFT IT and
  plexsim answered both with the second's container. The window is under a millisecond, so no
  sequential check can see it: fire barrier-released pairs.
- **The title FOLD is a 100-character TABLE, not NFD, and the two sides differ.** Swept on both
  copies over 2,076 codepoints for the sort title and on kids over 2,098 for the query: `ę ą ş ć ł`
  and the capital macrons survive, lower-case macrons and Latin-1 fold, a combining mark is dropped.
  The `title=` QUERY is the table plus a simple case fold (so `ſ` and the Kelvin sign match `s` and
  `k`); the STORED sort title is case-folded only, so a locked `Éggęhead` is not found by `Egghead`.
  The folded pass reads the stored SORT title, not the title (`Thé Train` finds nothing).
- **The hub search folds by the same table**, but its score CASE handling is two distinct passes:
  the raw-query partial pass and a broader whole-title pass. Cycle 39 completed and built the table,
  including the long-s and two-letter macron-`o` boundaries.
- **A plain-form decision runs the client's own PROFILE FILE** (`decision.py`,
  `client_profiles.json`, read out of the lab image): selected audio not first, container and video
  codec, the stream it would play, a sidecar subtitle, the audio codec, an embedded subtitle, then
  the container's limitations before a named codec's before `*`. Exact on 1,864 of 1,872 measured
  cells; 44 profiles direct play a real row where plexsim hard-coded two, and those two do not list
  webm or eac3. A refused direct play still answers "neither", because plexsim cannot convert.
- **JSON is COMPACT and raw UTF-8**: no space after `:` or `,`, every non-ASCII character as itself,
  `/ < > &` unescaped, and a C0 control other than `\b \t \n \f \r` as `\u00XX` in UPPER-CASE hex. The
  notification frames too. Python's defaults differ on all three.
- **`/livetv`'s 403 on the copies is the UNCLAIMED server's**, not the surface's: both claimed
  references answer the three-row container, and the copies answer it at `/livetv/`.

- **The response HEADER BLOCK is ORDERED, per response family.** Measured off a raw socket on both
  references, identical: `X-Plex-Protocol` FIRST and `Date` LAST on every response, `Keep-Alive`
  always straight after `Connection`, and then per family -- a 200 container is type-then-length, an
  ERROR length-then-type, a gzipped body puts `Connection` ahead of the encoding pair, artwork leads
  with the type and `X-Plex-Local-Asset`, `/photo/:/transcode` and a `/web`/`/:/resources` asset send
  the same names in two different orders, and a part read leads `ETag, Last-Modified`. plexsim wrote
  `Date` first, which is Python's. See `HEADER_ORDER` and `end_headers`.
- **A 416 names no connection state at all** -- the only response class that does not -- and **a part
  read carries no `Cache-Control`**, which is the exception to "no-cache goes on EVERY response".
- **The plug-in framework's `/system` paths carry `Set-Cookie: com.plexapp.system=<constant>`** on
  every status, and order their length before their type; `/system/agents`, `/system/scanners/{N}`
  and `/system/proxy` -- the leaves PMS implements itself -- carry no cookie and order like a
  container. Nothing else on the surface sends a cookie.
- **The CORS preflight and the `/web` 302 name their connection state too**, and the preflight
  carries `Cache-Control: no-cache`. A handler that writes its own header set has to say so itself.
- **A ranged CONTAINER answers `Content-Length` TWICE** -- the whole body's length, then
  `Content-Range`, then the SLICE's after the connection state, with the slice as the body. A ranged
  artwork, asset or part answers the slice's length alone. Read one with a RAW SOCKET: a client that
  takes the first length waits for bytes that never come, `http.client` included.
- **A refusal written before the request LINE parses has no `self.path`** -- every malformed-line 400
  answered NOTHING when a header-ordering pass read the attribute directly.
- **A NEVER-MATCHED row (`local://`) opens its refresh with `state 2 matching` and a bare `state 5`**
  before the queued frame, on both versions; an explicitly unmatched (`tv.plex.agents.none://`) row
  opens with the queued frame alone. Whether the cascade then RUNS is the server's own state -- an
  isolated fresh install never gets past the queue -- and not a version rule.

- **A response with an EMPTY body orders its LENGTH before its type**, the way an error does, where
  a MediaContainer body is type-first -- so cycle 33's families split on "does the body carry a
  container", not on the status. A HEAD keeps the container order, its length being the body's.
- **The preflight sends `Access-Control-Allow-Headers` only when the request ASKED for headers**, and
  echoes exactly what it asked for; Plex Web's own preflight asks for none. A fallback list answers a
  question the browser did not put.
- **The 101 handshake LEADS with the CORS pair**: `X-Plex-Protocol, Access-Control-Expose-Headers,
  Vary, Upgrade, Connection, Sec-WebSocket-Accept, Cache-Control, Access-Control-Allow-Origin, Date`.
  It cannot be probed -- a socket may never be opened on kids or zen -- so it was read out of a
  client-session capture, which records both sides of the client's own handshake.
- **A decision reads EVERY audio stream, not only the selected one**: the first in INDEX order that no
  matching `<VideoProfile>` names is the codec the refusal names (Roku plays the selected mp3 and still
  refuses the row, naming its ac3). **Unless the profile declares
  `StreamUnselectedIncompatibleAudioStreams`** -- Windows, Windows 10 Desktop, Windows 10 Mobile,
  Windows Phone, iOS, tvOS -- where only the selected stream counts.
- **The decision reads a per-user audio selection the ITEM ROW DOES NOT CARRY.** Before any client has
  chosen a track, 41 of the 78 profiles refuse a row whose tracks name no language as "not the first
  audio stream" although `selected` names the first one; `PUT /library/parts/{id}?audioStreamID=<that
  same track>` makes all 41 answer from the visible row and `audioStreamID=-1` puts them back, with
  `Media`, `Part` and `Stream` byte-identical throughout. plexsim holds a chosen track and models that
  state; `tests/test_decision.py` scores both against the recorded corpus.
- **A sidecar fixture read out of the reference's image has to be the WHOLE thing.**
  `client_profiles.json` kept 10 of the 30 `<Setting>` rows -- the one the model of the day read --
  and the decision turned on one of the twenty it dropped.

- **A real PMS zeroes its gzip header's MTIME**, so a compressed body is a pure function of the body
  -- `gzip.compress(payload, 6, mtime=0)` reproduces the reference's member byte for byte. Python
  stamps the clock, which makes the same request answer different bytes every second.
- **The `Range` grammar is lenient in a measured shape, and 416 is part of it.** The unit token is
  never read (`items=0-10` ranges) but a value with no `=` is not a Range; an empty spec is a 200; a
  suffix of zero or one that does not parse answers `bytes 0-0/N` with the WHOLE body; an
  unparseable FIRST -- and any second `-`, and a comma list -- answers the whole body as a 206; an
  unparseable LAST becomes ZERO; and a reversed range or a first at or past the end is a **416**.
- **A library CREATE pushes the scan's skeleton even when there is nothing to scan** -- eleven
  frames, the `library.update.section` activity through a Context-carrying 99 and 100, the two
  LIBRARY_UPDATE statuses, and the nested `provider.subscriptions.process` pair. The Add Library
  dialog's spinner and its "Library scan complete" toast are drawn off exactly these.
- **`ButlerTaskGenerateAdMarkers` ships DISABLED**, alone among the three marker tasks, on both
  versions -- the one disagreement in the 23-row enabled table, and `POST /butler` runs what is
  enabled.
- **A search hit's SCORE tier is the MATCHER's rule**: an exact title is 0.93, a query whose terms
  match the title's words in order starting at the title's FIRST word is 0.73, everything else is
  the 0.33 fuzzy band. `Tears Steel` is 0.73 and `Steel` alone is 0.33 -- a title PREFIX test gets
  both backwards.
- **A `list=` limitation's refusal names the list and appends ` [list]`**, where a `value=` one
  carries no suffix.

- **A refusal's BODY is one fixed HTML document per status** -- `<html><head><title>{reason}
  </title></head><body><h1>{code} {reason}</h1></body></html>` with `Content-Type: text/html`, from
  HTTP's own reason phrase: 89 bytes for a 400, 91 for a 401, 85 for a 403 and a 404, 95 for a 406,
  129 for a 416, 109 for a 500. It IGNORES `Accept`, it is GZIPPED when offered, and a HEAD carries
  its length. The exceptions are `GET /`'s 193-byte redirect and `/system`'s empty bodies with no
  `Content-Type` at all. Every refusal below the API answers it too -- a malformed request line, an
  over-long URL, a 101st header -- where `BaseHTTPRequestHandler.send_error` writes Python's own
  460-byte DOCTYPE page. Cycle 1's `text/plain` sentence was never measured; it is now the LOG's.
- **A path-segment id is parsed as a NUMBER**: `01`, `001`, `+1` and `+01` name the same row as `1`,
  byte for byte, while `++1`, `1+`, `-1`, `0x1`, `1.0` and every non-ASCII digit name nothing. It is
  the general case of cycle 29's `+?0*[01]` option grammar. **Four paths bound it to a SIGNED 32-BIT
  integer** -- `/devices`, `/accounts`, `/playQueues`, `/system/scanners`: `2147483648` is a 400 and a
  negative value inside the range a 404, on all four. `/library/metadata` and `/library/sections` have
  no bound.
- **The request TARGET is printable ASCII (0x21-0x7E) and nothing else** -- every raw control byte and
  every byte from 0x7F up is a 400, in a path segment and in a query value alike, while a
  percent-escape of the same byte is fine. **In the PATH a `%` must begin an escape the parser can
  read**: two characters must follow, and they parse like `strtol` in base 16 (an optional `+`/`-`
  then at least one hex digit, not necessarily consuming both), so `%4y`, `%AB`, `%+1` and `%-1` pass
  and `%zA`, `%y4`, `%++`, `%+y`, `%`, `%4` and `%+` are 400s; `%%` is a literal pair consumed before
  that rule. The QUERY is not validated this way at all.
- **`/hubs/sections/{id}` and its `continueWatching/items` leaf 404 a section the server does not
  hold**, like every other section leaf since cycle 15, while **`/hubs/metadata/{id}/related` never
  resolves the row**: an unknown id there is the empty hub container (`{size, allowSync, identifier}`,
  no librarySection triple) where `/library/metadata/{id}/related` and `/hubs/metadata/{id}/postplay`
  are 404s.
- **A repeated query parameter takes the LAST occurrence in the TOKEN GATE too** -- a bad token then a
  good one is a 200 and the reverse a 401 -- and in the client identifier and the notification
  channels' `filters=`. Three readers used `parse_qs(...)[0]` where the server's own `query` dict
  takes the last.
- **A search hit's five decimals are a TIER plus the row's own `audienceRating`/10000**, and a
  row with none scores the bare tier -- held out over 11,076 scored (query, row) pairs. `rating`
  is not it. Without the tail every row inside a tier ties and the page falls back to insertion
  order.
- **The ladder has FOUR steps and a base per ROW TYPE.** exact (the query's tokens ARE the
  title, its `titleSort` or its `originalTitle`) / anchored (the query's FIRST token EQUALS the
  title's first, and nothing else matters) / prefix (the query is a token-wise prefix of the
  title -- no more tokens, each a prefix at the SAME index, the first a PROPER prefix) / fuzzy.
  A movie and a show are 0.93/0.73/0.53/0.33, an EPISODE is 0.02 lower throughout, and a TAG has
  0.92/0.52/0.32 with NO anchored step. Cycle 35's `word_tier` -- the in-order skip-tolerant walk
  -- is REFUTED as the tier rule: `Riders X` and `Riders of Justices` are anchored on both
  references.
- **ORDER decides the SCORE, not membership**: a query whose words are all in the title but out
  of order comes back in the 0.33 band. The rest of the membership rule is SQLite `spellfix1` --
  a phonetic-hash candidate then a top-N prune by a GLOBAL token frequency -- which is why
  `Tears zzz` finds the row on kids' five titles and `Riders zzz` finds nothing on zen's 3,887.
  The candidate half alone would answer most of a catalog to a nonsense query, so it is recorded
  and not built.
- **A Dolby Vision PROFILE 5 source is refused by the SERVER**: `transcodeDecisionCode 2003`,
  "File is unplayable. DoVi (Profile 5) color space is not supported." -- where profile 7 and 8
  rows answer the ordinary 4002. Not a blanket veto: the one profile whose transcode target names
  the source's own codec still converts it. **`DOVIBLPresent` is the dual-layer discriminator**,
  not `DOVIELPresent`: a separate-track enhancement layer says false on its own stream, and that
  settles the one-witness-each-side disagreement in favour of what `mediainfo.py` already does.
- **An UNMATCHED show refuses the agent prefs**: its `Preferences` block carries eight settings, not
  ten, and `useOriginalTitle`/`languageOverride` are 400s there; **`showOrdering` is a 500** for
  every value, with NO frames, while the value is STORED -- the write lands and the cascade behind
  it fails. A matched show answers 200 to all of them.
- **A playlist and a collection take an artwork UPLOAD** on `POST /library/metadata/{list}/posters`
  -- an empty 200, the candidate list going from one `default://` row to two, the upload selected.
  And **the `/library/collections/{id}` action leaves really RUN**: `refresh` pushes the metadata
  path's three frames and `unmatch` gives the row `local://{rk}`, `subtype "unknown"` and no
  `ratingCount`. Cycle 22 measured their empty body and stopped there.
- **A `?path=`-scoped scan scans the section's own folder AND any child of it** -- cycle 19's "a
  child scans nothing" was measured on a movie library, whose children are files. Only a parent, or
  a path the library does not hold, scans nothing.
- **A type=18 SECTION save frames `state 3 queued` with the title from BEFORE the write**, where
  the item and metadata spellings of the same edit send `loading` with the new title.
- **An MBAFF interlaced row sets `scanType: interlaced` and NOT `video.separateFields`**: made on
  the copy and driven against all 78 profiles, only `RokuDLNA-5.1` refuses it (on `video.scanType`)
  and iOS and tvOS -- the two profiles naming `separateFields` -- direct-play it. `decision.py`'s
  constant 0 is right; a row that trips it needs field-coded PAFF pictures no encoder here writes.
- **The request target is RAW data, not a browser URL.** A literal `;` or `#` remains in the path or
  query; `urlparse`/`urlsplit` silently strips it as params/fragment syntax and routes a different
  request. Split only at the first `?`, and pass that same raw query to the token gate, localisation,
  paging, excludes and notification filters.
- **A non-terminal all-dot path segment is a 400 after ONE `%2e` decode**, not a traversal: `..`,
  `.%2e` and `...` inside the path refuse, a terminal `..` is 404, and a double escape stays escaped.
- **GET and HEAD never consume a declared request body.** Both references answer immediately with
  `Content-Length: 999` and no bytes, with or without `Expect: 100-continue`; reading before routing
  turns a read-only request into a trivial server stall.
- **A measured multipart filename is `Title - cdN.ext`.** One Media owns ordered Parts, its duration
  is their sum, and every Part owns its debrid link. `partIndex=0/1` projects that Part,
  out-of-range is 400 and `-1` is the 4005 all-parts conversion refusal.
- **A clip is not an ordinary library row.** It publishes no librarySection triple; the parent owns
  `primaryExtraKey` and inline `Extras`; item and section `/extras/all` use different projections;
  the clip borrows the parent's `art` attribute but its own `/art` route is 404.
- **Fuzzy search is the reference's `spellfix1` pipeline, not a similarity shortcut.** The two live
  vocabularies use ICU-token ranks, candidates are `top=10 AND score<=200`, cost is
  `editdist1 + 32 - bit_length(rank)`, and normal movie/show/list/tag hits suppress fallback
  globally. Per-word candidate groups are ANDed, tokens shorter than three UTF-8 bytes are dropped,
  and the FTS comparison uses ICU primary/shifted collation. Spellfix cost chooses membership; it is
  not the API relevance score. The external table may be stale between database optimizations;
  rebuild it only at the measured optimize/Butler/section-delete completion points, not on edits.
- **Two query-only writes ignore a declared body:** item metadata `PUT` and playlist-create `POST`
  dispatch from their query string even when a large `Content-Length` body never arrives. This is
  path-specific; subtitle upload and other body-dependent writes still wait for their bytes.
- **Search order is not a stable score/title sort.** A hub sorts the full-precision score with the
  reference's unstable comparison sort and formats to five decimals afterwards; its title
  candidates entered in raw-title/id descending order with exact hits prepended. Flat
  `/library/search` preserves the movie/show/episode bucket sequence because its final comparator
  accidentally reads an empty generic score attribute.
- **Hub-search membership folding and score-tier folding are separate passes.** Partial-title
  tiering applies the measured `_TITLE_FOLD` table to the RAW query before ASCII-only case folding;
  lowercasing first promotes eleven unsupported capitals and loses `Ð`. Whole-title exactness is a
  separate, broader comparison, with measured long-s and two-letter macron-`o` boundaries. Do not
  replace either pass with one global Unicode normalizer.
- **The spellfix tag vocabulary is larger than `Catalog.TAG_FIELDS`.** Rebuilds also index the row's
  Chapter/Review/Marker/CommonSenseMedia/Network tags, Guid ids, Rating images, studio and the
  Thumb/Art/Theme/ClearLogo presence strings. The only server-static remainder on 1.42.2 is the
  measured 28-string optimize/show-ordering sidecar. Technical GUID tokens can change the top ten
  and therefore which people reach the wire; never substitute a library-specific token dump.
- **`/system/services/url/lookup` is not an empty collection.** The bare request is the legacy
  plug-in framework's fixed 500 `application/xml` failure, independent of Accept. plexsim reports
  the exact 464-byte 1.42.2 body: GET may gzip it, HEAD carries the uncompressed length without a
  body, and the response closes the connection. A valid `url=` request is external-fetch shaped and
  was not probed.
- **The metadata-provider tables expose Add Agent.** Once both tables load, Plex Web offers group
  create with no further capability gate, then consumes the returned group id. Create and rename
  return the full JSON group; deleting the group is an empty 200. The offered Personal Media
  secondary membership is refused 400 and absent membership deletion is 404. Provider creation is
  a separate external-object capability and remains absent.
- **A historical “measured but not acted on” note is not a disposition.** Cycle 41 normalized 513
  raw notes into 332 atomic behaviors and found 24 reachable mismatches hiding among entries marked
  thin, deliberate or already built. Keep one atomic claim per independently testable behavior,
  record the current code and a branch-reaching regression, and distinguish a bounded external cell
  from an unresolved deduction.
- **Title folding removes one finite block, not every combining character.** Both PMS versions drop
  exactly U+0300..U+036F at the front and between ASCII sentinels. A generic Unicode combining-class
  predicate over-removes 699 Mn/Mc characters from other scripts and misses U+034F. The regression
  sweeps all 7,531 assigned punctuation/symbol/separator/mark/format scalars in the measured gaps.
- **Playback sessions expire on a global reaper phase.** A process-wide tick advances every 30
  seconds and removes rows with `seen < tick - 180`; adjacent rows at age 180.9 and 179.0 split at
  the same tick. Thus a session's visible lifetime is 180-210 seconds, not a lazy per-read 180-second
  timer.
- **Recently Added TV is a 50-row listing, not a twelve-row shelf.** Build the full roll-up, cap at
  50, then apply the ordinary container window. A start of 50 returns zero rows with
  `totalSize=50, offset=50`; legacy `limit=` and `count=` do not truncate it. One new episode stays
  an episode, while two siblings roll up to their season.
- **A show merge is a hierarchy merge.** It deletes the absorbed show, season and episode, appends
  the absorbed leaf Media before the target leaf Media at matching season/episode indexes, retains
  target then absorbed Locations, and pushes three delete frames followed by the survivor's state
  3/5 pair. An unmatched hierarchy shape remains a no-op because no reference measurement defines
  a pairing for it.
- **Collection leaf views are empty even when the collection has members.** `/children` exposes the
  selected member and its collection tag, while `/allLeaves` and `/grandchildren` return empty 200
  containers on both supported versions. Do not apply the generic parent-leaf projection there.
- **Multi-Media selection is path-specific.** A decision over more than one Media requires one
  explicit, in-range `mediaIndex`; absent, negative, nonnumeric and repeated selectors are refused.
  A play queue ignores that selector and keeps every version. Playlist metadata containers carry
  `allowSync=false`, `identifier`, `mediaTagPrefix`, `mediaTagVersion` in the measured order.
- **Moving a queue item can re-anchor `playQueueLastAddedItemID`.** The marker names the item that
  remains the tail of the plain-add block after the move; leaving it on the moved id changes the
  client's Up Next shuffle state. Search hubs likewise have their own recovered ordering: strongest
  match class plus the type bias, sorted with the same unstable comparison routine as result rows.
- **Legacy quirks remain real side effects.** A type-mismatched playlist create returns 400 after
  creating the empty playlist and emitting its frame. `/playlists/upload` is 400 when either `path`
  or `sectionID` is absent and 500 when both are present, irrespective of whether the path exists.
  A section `/search` ignores every legacy limit spelling. The plain download `filename=` maps `+`
  to `_`, while `filename*=` keeps the percent-escaped plus.
- **Projection flags and derived media values are exact gates.** Part `accessible`/`exists` appear
  only under detail `checkFiles=1`; duration sort reads `Media.duration`; visually-impaired subtitle
  disposition is projected only when true; mixed folder XML emits a `Directory` for a folder and a
  `Video` for a file. Generated probes also pin the short-wide 480-resolution transition and the
  strict anamorphic boundary—do not restore the earlier fitted constants.
- **The `#` first-character bucket aliases every ASCII non-letter.** The axis aggregates punctuation
  and digits there, while explicit `/firstCharacter/!` and `/firstCharacter/0` requests remain
  literal buckets. Non-ASCII punctuation such as `。`, `『` and Greek letters retain their own
  buckets; only the measured leading-strip characters disappear before bucketing.
- **Localisation coverage is an emitted-source-pair census.** Cycle 41 walked all 124 safe-read
  families of the local 4.147.1 client plan and found four missing 1.42.2 source pairs: one server
  preference summary and three hub titles. Each was captured in all 28 languages. A table entry is
  complete only when every route context that emits that English source has been joined and checked.
- **A clean cycle rebuilds the denominator; it does not inherit “no findings”.** Cycle 42 recreated
  all 332 historical atoms and the 49/1,221/47/339/45/45 advertised-control graph from current
  source, current tests and fresh live responses, then replayed all 24 preceding fixes. Both reports
  ended `no findings`; this is the stopping gate. Retained evidence may be reused only when the
  exact request/state is unchanged and the relevant source/test blobs are byte-identical and hashed.
- **A Plex Web version/build label does not identify one webpack graph.** The historical gateway
  and current public app both say 4.160.0 / `75ddd7b`, but corresponding chunk hashes differ and
  each has one mapped id the other lacks. Complete one manifest-rooted graph and keep its provenance;
  never fill an absent historical body with a same-named current asset. The complete current app is
  205 JS plus 16 CSS, and the 177 JS assets beyond Cycle 40's recovered subset contain no PMS
  request builders or call sites.
- **Inventory server processes at both ends of every cycle.** Cycle 43 startup found Cycle 41's
  frozen listeners still on 32587/32588 despite a later clean-close record. List every
  `server.py --port` process, resolve cwd and command line, and kill only proved cycle-owned
  processes; checking the current cycle's expected ports cannot catch an older stray listener.
- **A live catalog build needs both playable bytes and identity.** RD torrents whose display name
  ends in a video extension can still unrestrict to a RAR; let `build_catalog.py` reject those.
  Without a valid TMDB key, an SxxEyy filename remains a movie row because no measured external
  identity exists from which to mint its show/season/episode guids. A byte test alone can therefore
  pass while navigation correctly reports no shows.
- **Container magic belongs to the selected Part, not to the test fixture's history.** The first
  live MP4 made `conformance.py` select the right Plex Web direct-play row and then fail its old
  unconditional Matroska assertion. MP4 carries `ftyp` at offset four; Matroska/WebM carries the
  EBML marker at zero. Keep the conformance target and magic assertion container-aware.

- **plexsim's MEDIA BYTE PATH had never been run against a real byte until 2026-09-17**, because
  every link in the fixture was dead and every part read a 502. It applied NO `Range` grammar: the
  client's header went to the debrid CDN and whatever came back was echoed, so 13 of the 20 cells
  both references agree on were wrong — `bytes=-1` read the FIRST byte where a real PMS reads the
  LAST (which is how every player finds an MP4 `moov` or a Matroska Cues block), a reversed or
  past-the-end range answered 206 instead of **416**, a one-byte probe pulled the whole file, and
  `bytes=<N+10>-` answered `Content-Length: -10`. `_range_window` is now the ONE grammar both
  `_ranged` and the part path ask. A bare **`bytes=-`** is not a Range at all (200 on every body);
  `bytes=- `, `-x`, `-abc`, `-0` are the 206.
- **A part read carries `X-Plex-Protocol` first like every other response**, its `Content-Type` is
  keyed on the stored FILE's extension (a `mp4` Media whose file is `.mkv` answers
  `video/x-matroska`; the URL's extension is ignored; an unknown extension is
  `application/octet-stream`, NOT `video/mp4`), and the bandwidth statistic counts what was
  WRITTEN — every seek and stop is an abort, so counting the advertised length moves the Dashboard
  by a whole file per seek. **`POST`/`PUT` on the THREE-segment media path is a 404**; only
  `/library/parts/{id}` takes the track write.
- **A 416 of a MediaContainer names its connection state; a 416 of a BINARY body does not.**
  Re-measured with a KEEP-ALIVE request, which is the only way to see the server's own choice
  rather than an echo of the probe. That corrects "a 416 names no connection state at all" to its
  binary half. **Every 416 is `Cache-Control: no-cache`**, the photo transcoder's included, whose
  200 caches for three days — and no 416 carries `X-Plex-Local-Asset`.
- **With an `Origin` on the wire, `Access-Control-Expose-Headers`/`Vary` sit directly after
  `X-Plex-Protocol` and `Access-Control-Allow-Origin` directly before `Date`, in EVERY response
  family** — the `/web` 302 included, which carried no CORS headers at all. Cycle 33 measured the
  families without an `Origin`, where the trio is absent, so it could not see this.
- **`/library/sections/{id}/rating` is a real axis that a MOVIE library ADVERTISES in its own pivot
  list** (plexsim advertised it and 404'd it, so the client's Ratings entry opened nothing). Its
  container is byte-identical to `/userRating`'s and its BUCKETS read the CRITIC rating.
  **A star bucket rounds its key DOWN to an even star** and takes the values within one of it, so
  8 and 9 answer the same rows; `key=0` is the rows with NO value. Truncation gets every other
  bucket wrong.
- **`videoCodec`, `audioCodec`, `subtitleCodec` and `audioLayout` are 1.43.4-only AXES** — 404 on
  zen and on plexscratch42, 200 on kids. The same six media-codec names already split `/filters`
  and the `Meta`; the axes split with them.
- **A `.webm` file's `container` is `mkv`.** Measured on the identical 571,346,576-byte file: zen
  answers `mkv` on the Media and the Part while the Part key keeps `file.webm`, and no row of zen's
  4,508 movie Media carries `webm` at all. `tests/fixtures/media/fun-1052407-0.json` has recorded
  that since cycle 10 and nothing read it, because `test_mediainfo.py` compared every stream field
  and **never the container**. The override cost a direct play (`Sony Bravia 2013`) rather than
  buying one.
- **A direct play is refused when the SELECTED embedded subtitle is not the FIRST embedded one** —
  `Samsung`, `RokuDLNA-5000X-7.x` and `RokuDLNA-6000X-7.x` only, gated on the same
  `DirectPlayStreamSelection` as the audio rule, and it runs after the audio-codec pass AND after
  the subtitle-codec pass. The 2,106-cell corpus could not show it: in every row it held whose
  subtitle is not first, the AUDIO is not first either, so the audio refusal masked it.
- **A SEASON carries `parentYear`, not a `year` of its own** — 689 of zen's 742 do, exactly one
  does not — and a season and an episode carry `Guid`. `metadata.py` fetched those ids and threw
  them away on a note claiming a real PMS leaves the arrays empty; kids' season 7 answers
  `tmdb://3846` + `tvdb://15791` and its episode 8 answers three.
- **A real PMS announces with `PUT https://servers.plex.tv/devices/{machineIdentifier}`** — the
  identifier form, to `servers.plex.tv`, token in the query, empty body, thirteen headers led by
  `User-Agent: PlexMediaServer/<version>` — and `httpsEnabled` is its own capability, not the uri's
  scheme. The numeric `/devices/{id}` form resolves to whatever device row holds that id and
  overwrites it. The harness's `capture/outbound.jsonl` is the instrument for this axis and no
  cycle had swept it.
- **`/myplex/claim` is routed for POST alone**; every other verb is a 404. **The two stream-select
  parameters have different grammars**: an unparseable `subtitleStreamID` CLEARS the selection and
  a negative other than `-1` is a 400, while `audioStreamID` 400s `0` and every negative but `-1`
  and accepts an unparseable value as a no-op. **An EMPTY `session=` on transcode `ping`/`stop` is
  a 404**, not the 400 a missing one gets.
- **`/media/providers/metadata/{id}` and `/group/{id}` are real** (400 for a non-numeric id), and
  in JSON `builtIn` is the STRING `"1"` on 1.42.2 and `order` is the FLOAT `1000.0` on both — the
  XML cannot tell either apart.
- **A part whose FILE is gone is a plain 404**, not a 5xx: zen part 23595 (`accessible=false`,
  `exists=false`) answers 404 to the byte read. plexsim answered 502 to a dead link and to a
  network failure alike. Real-Debrid's `unavailable_file`/error_code 24 is now the 404 and
  everything else keeps the 502, so a transport failure is still diagnosable.
- **`tests/test_part_bytes.py` stands up its own upstream** (a local CDN and a one-route
  Real-Debrid stub behind `RD_API`) and drives the real handler, so the byte path is testable with
  no credential at all. Use it rather than waiting for a live `/d/` link.


- **`/library/sections/{id}/{name}/top` is a real leaf** -- PMS's own route table spells it
  `/:tagType/top`, so ANY single word matches it -- and it answers the section's TAG TABLE for
  the type the name resolves to, **ascending by tag id** rather than by usage. The `key` carries
  the REQUESTED name (`?studio=28`) and the `filter` the tag's OWN type (`director=28`). Eleven
  names resolve, in six pairs of a tag name and a metadata COLUMN name sharing one number:
  `genre`/`title` 1, `director`/`studio` 4, `writer`/`rating` 5, `actor`/`tagline` 6,
  `producer`/`summary` 7, `country` 8. Every other name the server knows as a filter field or an
  axis is an EMPTY 200 and everything else a 404 -- including the eleven edit-path-400 names,
  the section's own leaves, `thumb`/`art`/`theme` and `platform`. `resolution` answers the whole
  library like any media-axis bucket. It is NOT the axis: kids' actor axis is the
  first-three-roles union and `/actor/top` is every Role tag.
- **`includeAlternates=1` inlines the AGENT's remote artwork on a FULL row and on nothing else** --
  `Thumb`, `Art`, `Theme` in that order, each `{key, source}` with a `previewKey` where the
  provider gives one. A light row never carries it, so the `_light` marker is the whole gate, and
  a DECISION ignores the option exactly as it ignores `excludeElements=`. `Thumb`/`Art` sit
  between the tags and `Guid`, `Theme` directly after `Rating`. **It is not 1.43.4-only**: the
  hunt measured 1.42.2 on plexscratch42, whose unmatched rows hold no agent artwork at all --
  "use the copy for grammar, never for a count", from the other side.
- **`Accept-Language` ranks the WHOLE list and `X-Plex-Accept-Language` is a third input.** The
  precedence is `X-Plex-Language` > `X-Plex-Accept-Language` > `Accept-Language`, header or
  query. Weights rank the list, the default is 1.0, `q=0` disqualifies that member, an unknown
  tag is SKIPPED rather than ending the search, `*` is ignored, an empty member is dropped, a tie
  goes to the first listed -- and **any malformed member disqualifies the entire header**. That
  is the OPPOSITE of the `Accept:` parser, where a bad `q` weighs 0 and the range still
  qualifies: two different parsers on the same surface.
- **`/library/metadata/{id}/theme/{v}/metadata` is a GET that MINTS A ROW.** It is how a cycle-45
  hunt left four orphan rows on the reference lab; measure it on a disposable copy only. The row
  is a `track` whose `guid` is `library://` + the **SHA-1 of the request path itself**, idempotent
  per (item, version), carrying no `librarySectionID` -- so it is invisible in every listing,
  `/library/all` included -- and readable at its own key. `theme` is the ONLY artwork leaf with
  this sub-leaf. plexsim mints it BESIDE the catalog so a `track` row cannot leak into the tag
  tables or the spelling index.
- **`/library/shared` is a whole subtree**: a constant seven-row `Type` tab bar in its own
  identifier, under which EVERY second segment answers the same list of the server's own
  libraries and a third segment is a 404. That row projection leads with an empty `art` and
  `thumb` and drops `allowSync`, `filters` and `refreshing`. `?type=` selects NOTHING for any
  integer (the rows are libraries), `?sort=` really orders by title or `createdAt`, `?limit=` is
  ignored -- and **an empty `?sort=` CRASHES a real PMS** (connection dropped on kids, SEGV on
  zen). Never send that request to zen.
- **`ownerFeatures` on `/` and `subscriptionFeatures` on `/myplex/account` are the SAME string**,
  byte for byte: a sorted comma join of ~180 opaque plex.tv UUIDs and 32 NAMED features. The 32
  names are identical on both servers where the UUID count is not. The UUIDs are NOT invented:
  fetch them from authenticated `/api/v2/features`, persist them with the owner id, and union them
  with the named defaults. A claimed server refreshes user then features at startup/after claim
  and every 86,400 seconds, with the feature read about 11 seconds after the user read; commit the
  pair atomically and retain the whole last-good pair on failure. Plex Web reads `ownerFeatures`
  in 28 places in its bundle.
- **An axis BUCKET resolves against the server's global tag TABLE, not the leaf's own column.** A
  genre tag id answers the identical container under `genre`, `director`, `actor`, `mood`,
  `style`, `similar`, `tag` and eleven more; only `studio` and `platform` are not bucket names at
  all. And **`narrator` is the one axis that does NOT validate `type=`** -- 200 for `abc` where
  every other tag axis is a 500 -- while `platform`'s `title2` is the EMPTY STRING where every
  other axis carries a "By ..." heading.
- **An item leaf with an EXTRA path segment is a 404**, with two exceptions only: the artwork
  leaves read the second segment as a version, and `/extras/all` and `/users/top` are real
  two-segment leaves. A **validated version segment refuses a leading `+`** although
  `+?0*<digits>` normalises in every other path segment -- read the RAW segment before the
  normalisation.
- **A refusal keyed on the QUERY's declared type must not be decided per ROW.** `file=` is a 500
  at `type=2` and `/library/all?file=` untyped is a 200 that really filters; plexsim raised it
  from inside the row walk -- "this row is a show" -- so the untyped whole-library query was a
  500. And **`extraType` is a 500 on every listing EXCEPT the section extras leaf**, which is the
  one path that really reads it (zen's 3,580 clips are 1,264 at `extraType=1`); putting the name
  in the global refusal table broke that leaf, and only `test_catalog_features.py` could see it.
- **A play completes on a beat that CROSSES 90% however short it is.** The gate is
  `position >= (total * 9) // 10`, an integer comparison; the sub-minute exemption the old rule
  carried was never measured.
- **The bare `/sync` is the plug-in dispatcher's 200** like every other single-segment path,
  while `/sync/items` and its siblings really are 404s. A "we do not implement this"
  short-circuit on a whole prefix makes one single-segment path answer differently from all the
  others.
- **File-check options report what the PROVIDER says, not a catalog constant.** `checkFiles`,
  `asyncCheckFiles` and `checkFileAvailability` share the same `+?0*[01]` grammar and resolver
  result: a live part projects `accessible=true, exists=true`, `unavailable_file` projects both
  false, and an unchecked row omits them. The byte path answers the same unavailable file 404;
  a resolver/network failure remains 502.
- **The broad option vocabulary was exhaustively swept: 116 names × 76 safe container families ×
  three servers.** The load-bearing propagation rules are that global `/hubs` passes
  `includeChildren`, `includeExtras`, `includeOnDeck`, `includeRelated` and
  `includePreferences` into FULL rows; `includeGuids` reaches hubs, Home Recently Added, children
  and section subsets; `includeMeta` reaches Home resume leaves, collections, folders and Shared;
  `includeCollections` moves the search collection shelf after Tracks; `includeSummary` reaches
  Shared; and `skipCount` filters the source rows before children/search/axis aggregation.
  `includeStorage` on `/media/providers` is NUMERIC, unlike a section grid's string totals.
- **Two request headers have special `/web` redirect semantics.** Exact
  `X-Plex-Http-Pipeline: disable` keeps the 302 but sends `Connection: close` without
  `Keep-Alive`; exact `X-Plex-Neuter-Redirect: 1` sends an empty 200 while retaining the absolute
  `Location`. Case-mismatched `Disable`, `infinite`, `0` and `true` are controls, not truthy
  synonyms.
- **Cloud watch/rating sync is a consent read plus an opaque-cursor state read.** Consent uses the
  server token; state uses the latest short-lived RS256 Bearer from the owner's `scrobbling`
  service. The encrypted service token is AES-256-CBC under PMS's measured MD5 expansion and
  permissive final-byte strip. The static 32-byte transport secret is NEVER checked in: inject
  base64 through `PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64`, or sync stays off. `complete` stores
  view time, `unplayed` clears it, an absent rating pair PRESERVES stars, `-1` clears them and a
  nonnegative rating sets them. Persist an `endCursor` only after applying the page; import writes
  no local history row.

- **A client FINDS a server over UDP, and plexsim answered nothing.** A real PMS answers
  Plex's own GDM discovery on udp/**32410** and udp/**32414** only (32412/32413 are the
  PLAYER side), on unicast, on broadcast and on the multicast group **239.0.0.250**. The
  trigger is a PREFIX COMPARE against `b"M-SEARCH * "` over `min(len, 11)` bytes -- an
  EMPTY datagram and a bare `M` answer, one wrong byte anywhere in the eleven is silent,
  anything may follow, and it is case-sensitive. The reply is `HTTP/1.0 200 OK` plus seven
  ALPHABETICAL headers, and `Host: <cert hash>.plex.direct` is present only on a CLAIMED
  server -- absent, not empty. A real PMS also SENDS `M-SEARCH * HTTP/1.1\r\n` (one CRLF)
  to the group every **5.00 s**, and `/servers` is exactly what that search found, each row
  ageing out between 177 s and 180 s after its last answer. `Updated-At` is NOT a clock: it
  stands still under polling, is days stale on an idle server, and the two ports carry
  independent stamps.
- **`/servers`' own row names the server's own interface address and its REAL port**, never
  loopback and never a constant 32400; `/myplex/account`'s `privateAddress` and
  `privatePort` move TOGETHER (both set, or `""` and **0**). `livetv` is a constant **7** on
  every server, tuner or no tuner, and `offlineTranscode` is 1 claimed and ABSENT unclaimed
  -- never 0. `/` also carries the transcoder's quality LADDER as three parallel comma
  lists (`transcoderVideoBitrates`/`Qualities`/`Resolutions`), identical on all four
  servers, which is what the client's Quality menu is built from.
- **A real PMS CONSUMES a declared request body after answering, and closes if it never
  comes.** Cycle 37's "GET and HEAD never consume a declared body" is half the rule: they
  answer without waiting AND then swallow exactly `Content-Length` bytes, so two requests on
  one socket get two answers. A duplicate `Content-Length` takes the **FIRST** occurrence --
  the opposite of the repeated-query-parameter rule -- and a leading `+` parses. It also
  really DECHUNKS `Transfer-Encoding: chunked`, including on an upload; the size token is
  `strtol` base 16 and **nothing may follow it**, so a chunk EXTENSION is a 400, and the
  header must be exactly `chunked` with nothing else in the value, beating `Content-Length`.
- **The incomplete-request cap is ABSOLUTE from the first byte, not idle.** Three pieces
  twelve seconds apart is closed at 20.0 s with no answer. A socket timeout is per-read by
  construction, which is why fifty dribbling clients held fifty threads. And a header block
  ended by **EOF** rather than by a blank line is answered with NOTHING -- plexsim ran it,
  and a query-only write really committed.
- **A refusal of the ENVELOPE closes the connection; a refusal of the MEANING keeps it.**
  That is the rule behind cycle 30's lone "exception". A missing `/:/resources/*` or
  `/web/*` asset is a 404 that names **no connection state at all** and closes; an
  `obs-fold` continuation line is a 400 both references refuse where Python's own reader
  unfolds it into an ordinary header; a leading CRLF or space before the request line draws
  **silence**. The METHOD token is bounded at **ten bytes** and the eleventh is refused the
  moment it arrives, before any delimiter. The ABSOLUTE-FORM target (`http://host/path`) is
  ROUTED with the authority discarded, and every other scheme spelling is a 400.
  `X-Plex-Http-Pipeline: disable` is a header-or-QUERY option, last occurrence winning,
  header beating query.
- **Every write rule in this project was measured on an UNCLAIMED copy, and the claim moves
  things.** 116 read families across the 2x2 of claim and version: 15 families and 40
  container attributes are claim-dependent. `ButlerTaskGenerateAdMarkers` ships **ENABLED**
  on a claimed server (with `GenerateAdMarkerBehavior: scheduled`, a setting the copies do
  not have at all) -- cycle 35 read the copies and recorded the opposite, which is class E.
  Before trusting any rule whose evidence is "measured on plexscratch", ask whether the
  claim could be computing it.
- **`/playlists/sorts` ends with `mediaCount` "Item Count"**, the same row
  `/playlists?includeMeta=1`'s `Meta` carries: cycle 15's "the two lists really are
  different" is REFUTED on four servers. **`?includeDetails=1&includeAdvanced=1` DOES carry
  the Duplicates Filter row**, and it is the one Filter there that keeps `advanced="1"`:
  cycle 23 read the absence of the FLAG as the absence of the ROW. **`/dlna` and `/manage`
  are real** -- the DLNA library list and the same 302 `/web` answers.
- **A LIGHT row carries none of `Label`, `Style`, `Similar` or `Mood`**, `Style` sits AFTER
  `Country` on a detail row, and `/status/sessions` writes EVERY tag element alphabetically
  with a string `id`. None of it is visible on a real library: it needed a movie MADE to
  carry the whole tag family at once, which is why cycle 23's 2,994-path order sweep could
  not have found it. A `mood` write on a movie is a 200 that stores nothing.
- **Emptying a collection leaves the collection ALIVE** and takes the tag off the item --
  both, so the state plexsim's deliberate deletion called unrepresentable is not. And an
  identifier-less request's `X-Plex-Device-Name`/`X-Plex-Platform` land on the ANONYMOUS
  device row.
- **XML cannot see a JSON scalar's TYPE**, and that is where the last two-writer bug was
  hiding: a `/system/agents` `Language`'s `default` is the boolean `true` and plexsim sent
  `"1"`, because the fixture is read out of the XML, which writes `default="1"` either way.
  `to_xml` also hard-coded `container=True` for the ROOT, so `/myplex/account` sorted its
  attributes alphabetically in XML and kept the right order in JSON.
- **Inline OnDeck is playback state, not a required detail block.** A show exposes one current
  episode only when its watch state selects one; a season exposes the block only when it owns
  that same episode. A pristine unwatched show and every other season omit it. The timeline's
  `playbackState=progress` is separately latched by `X-Plex-Session-Identifier`: once that live
  session crosses one minute, rewinding it below a minute remains `progress`, while a fresh
  session is `ignore` even though the stored item offset is still above a minute.
- **A searchable tag row has a top-level membership gate but an all-row count.** The tag must be
  present on at least one current top-level movie/show in the requested library before search
  emits a row. Once admitted, `count` is every current metadata row in that library carrying the
  tag, including episodes: show+episode is count 2 while the row's key lists one show. An
  episode alone emits no row; removing and re-adding proves the count is current, not cumulative,
  while the tag id itself persists.
- **Regional language equivalence is surface-specific.** `/:/prefs` has complete measured
  regional catalogues: `pt-BR` and `pt-PT` are identical, `hu-HU` is distinct, and the reported
  1.42 build serves 172 of each captured 174 rows. Do not collapse them to their primary subtag,
  and do not project this exception onto unmeasured endpoints. `zh-HK` and `zh-TW` likewise route
  to their measured English catalogue rather than generic `zh`. (Cycle 66 measured every other
  endpoint: `pt-BR` is its own catalogue everywhere, and `zh-TW` keeps Traditional ICU names.)
- **The legacy single-segment prefix dispatcher is a finite registry, not a prefix algorithm.**
  The durable executed denominator is 58 unique paths (old prose said 57), identical over 174
  reference GETs. Reproduce the captured ordered shapes only; deriving rows from plexsim's own
  router would model plexsim rather than PMS. Double and trailing slashes are another finite
  per-route table: there is no global path normalisation rule.
- **GDM `Updated-At` moves only with library lifecycle.** Section create, edit and delete touch
  the advertisement. FriendlyName changes `Name` without moving the stamp; refresh, item edit,
  collection creation and index deletion do not touch it. A manual `disco.touch()` in a test is
  not evidence for a server lifecycle hook.
- **`/connections` is deliberately absent after a complete disposable-only matrix.** Both PMS
  versions show Pending partial requests, Routing observers, Pipelined pauses, Finished rows and
  a cleanup around the 20-second incomplete-request sweep, but snapshot ordinals/order are
  unstable, the hexadecimal allocator has invisible gaps, Finished cleanup is coupled to an
  opaque scheduler and the byte-count form requires excluded media traffic. Implementing any
  visible row now would fabricate fields or lifecycle.
- **A normal keep-alive idle close must be quiet.** The first timeout can occur in the method
  peek and poison `BufferedReader`, whose following `readline()` raises `OSError: cannot read
  from timed out object`. Close the connection there; never let the handler traceback while the
  wire correctly reaches EOF.


- **The server's own BINARY is a route table, and it names endpoints no client sends.** It
  is 22 MB and readable on both references (`docker exec plexlab-plex cat "/usr/lib/
  plexmediaserver/Plex Media Server"`; `cat` over ssh on zen), and its strings carry PMS's
  route templates with their captures -- `/parts/:partID(\d+)/:changestamp(\d+)?/:filename?`,
  `/:assetType(thumb|art|clearLogo|squareArt|banner|poster|theme)`,
  `/:type(video|music|audio|subtitles)/:/transcode`. Diffing the two binaries splits the
  vocabulary per version for free: 1.42.2 omits `squareArt` and keeps `clearLogo`.
- **The legacy dispatcher's listing is a RULE**: the LAST SEGMENT of every registered legacy
  prefix that begins with the requested path and is STRICTLY LONGER than it. `/statistic`
  names `statistics` and `/statistics` is EMPTY; `/co` is empty because `/connections` is a
  native route, not a registered prefix. Cycle 50's 58-path table was that rule sampled at
  58 points and 119 other prefixes answered rows it had none for. 148 of the 154 listings
  are in byte order of the row key, five put the built-in `library` row LAST, and `/l` is
  recorded as measured.
- **Three static files answer at the top level with NO token** -- `/crossdomain.xml` (347
  bytes), `/clientaccesspolicy.xml` (325) and `/favicon.ico` (5,430), byte-identical on both
  versions, `Accept-Ranges: bytes`, never gzipped, and the dispatcher's empty listing for
  every verb but GET and HEAD. They are the third, fourth and fifth exception to "a real PMS
  401s everything but `/identity` and `/web/*`", and the unauthenticated partition is wider
  than cycle 16's eleven besides: `/connections` and `/dlna` are 401s, `/manage` is the
  `/web` 302, and `/search`, `/downloadQueue` and `/player` answer their own refusal (400,
  404 and, on 1.42.2, 400) before the gate.
- **`/:/plugins` is SEVENTEEN constant rows** -- the bundles in the image, identical on four
  servers, `state` 4 on all but `com.plexapp.system` -- and everything under it is a SECOND
  plug-in framework subtree: thirteen of the seventeen answer a preference document at
  `/{id}/prefs` (the other four the framework's 500), a bare identifier and any other path
  under a hosted one are an EMPTY-bodied 404, the `/system` envelope applies (OPTIONS an
  empty 200, the other verbs empty 404s, PATCH the version's own refusal), and the cookie is
  NAMED AFTER THE PLUG-IN. So cycle 18's "PATCH is a 405, the only one on the surface" names
  two subtrees. **The framework's cookie rides its own responses only**: neither the bare
  OPTIONS 200 nor the PATCH refusal carries it, under either subtree.
- **`/:/resources/*` is SEVENTY names**, PMS's whole `Resources/Graphics` tree, identical on
  both versions -- 59 png, 9 jpeg, 2 octet-stream. A DIRECTORY is an empty
  `application/octet-stream` 200 in both spellings, the bare `/:/resources` is a 404,
  matching is case-SENSITIVE, and `?width=`/`?height=` are ignored. Cycle 29's "seven names"
  was a ten-name sample. An ACTOR with no portrait carries `/:/resources/actor-icon.png?t=`
  -- 479 of zen's 3,416 actor rows, and not one lacks a `thumb` -- where a director, writer
  or producer with none carries no `thumb` at all.
- **The photo transcoder FETCHES the `url=` path**: anything the server does not answer 200
  is a **404** there (an unknown leaf, a missing composite, a borrowed field, and
  `/children`, whose own answer is a 400), and only a 200 whose bytes are not an image is
  the 500. So a section COMPOSITE transcodes to its own jpeg -- which is the path Plex Web
  builds every library card from.
- **An artwork READ leaf takes GET, HEAD, PUT and DELETE only**: POST and a bare OPTIONS are
  the ordinary 404, and the VERSIONED path 404s the PUT. **A collection's and a playlist's
  artwork leaves follow the item ownership rule** -- a list with nothing uploaded 404s
  `/thumb`, `/art` and `/poster` and answers the empty candidate container at `/clearLogos`
  and `/themes` -- and an EMPTY playlist's `default://` candidate still names
  `/playlists/{id}/composite/{updatedAt}`.
- **`/connections` is `text/plain`**, one line per connection slot
  (`3 <peer> - Routing - GET /connections #<hex> - Token (<user>) (<agent>)`), where the
  trailing parenthesis is `X-Plex-Device-Name, X-Plex-Product` or the `User-Agent`. PMS's
  acceptor pool, its ordinals and its request allocator stay unmodelled.
- **`/hubs/sections/{id}/popular` is real**: the library's SEASONS with a `viewCount` above
  zero, most-watched first, in the full row projection. A movie library answers the empty
  container on 1.42.2 (kids 500s it, unexplained).
- **A Categories tile's `blendColor` is a TABLE** -- thirteen fixed genre names plus a
  seven-colour cycle walked in TAG-ID order over the rest (22 of 22 across both references)
  -- and the tile's `thumb` is the FIRST attribute of the row. The art is the highest-rated
  item carrying that genre and an `art` no earlier genre has taken.
- **Four refusal shapes, both versions**: an extra segment after a STREAM id, an ACCOUNT id
  or a DEVICE id is a 404 (not the 501 or the row); `/library/metadata/augmentations/{x}`
  parses its id first, so a non-number is a 400; all four `/:type(video|music|audio|
  subtitles)/:/transcode` prefixes are routed; and the PART leaf's one extra segment is a
  FILENAME whatever it says, the digit rule deciding the changestamp only when a filename
  follows.
- **Exactly four actions live under `/system/agents`** -- `search`, `update`, `contributors`
  and `media/get` -- each the plug-in framework's own TypeError traceback with a 500, and
  every other name an EMPTY-bodied 404. The framework passes the query through to its own
  Python function, so the sentence moves with the parameters; the recognised names are
  measured and the general rule is the bundle's signatures.
- **On the SECTION save path an indexed tag parameter is a tag write whatever follows
  `.tag.`** (`.role`, `.thumb`, `.id`, `.filter`, `.zzz` are all 200s and the role stores
  empty), not a type-scoped filter.


- **The plug-in framework is PYTHON, shipped uncompiled in the image, and
  `System.bundle/Contents/Code/*.py` IS the `/system` surface.** Every `@route` in those
  files is an endpoint (`ssh ben@kids docker exec plexlab-plex sh -c 'ls
  /usr/lib/plexmediaserver/Resources/Plug-ins-e5521bd8c/'`; zen's is `Plug-ins-f737b826c`
  and the two differ). `runtime.py`'s `handle_request` is the `f(**d)` in every framework
  traceback, so the TypeError sentence is the function's own signature: `given` counts
  `self` plus every supplied name the function DECLARES, an undeclared one is "got an
  unexpected keyword argument", and a `**kwargs` tail is why `search` alone swallows one.
- **`/system/bundle/media/flags/{type}[/{country}]/{value}` is a real image family, and
  every container's `mediaTagPrefix` points at it.** TEN types (`aspectRatio audioChannels
  audioCodec contentRating media recordLabel studio videoCodec videoFrameRate
  videoResolution`), 742 PNGs, and a 624-regex `substitutions.xml` for studio, audio codec
  and video codec. An unknown VALUE is the bundle's 82-byte `blank.png` with a **200**; an
  unknown TYPE, an empty value and a THIRD segment are the `/system` empty-bodied 404;
  `media` and `recordLabel` have no directory so every value under them is blank; a direct
  file name is case-SENSITIVE and the substituted types are not (`studio/Disney` and
  `studio/Walt Disney Pictures` are one logo, `audioCodec/dca` the DTS one).
- **Eighteen more framework routes are real**: `/system/channels/{all,music,video,photos,
  applications}` and `/system/plugins/{same five}` (the framework's 130-byte EMPTY
  container), `/system/notify/serverUpdated` (an empty `text/plain` 200),
  `/system/debug/agentservice/status` (a 229-byte report), `/system/messaging/*`, and
  `/system/services/{search,searches,relatedcontent/lookup}` plus both
  `/system/agents/attribution` leaves (the framework's TypeError traceback). **A framework
  STRING result spells its header `Content-type`**, with a lower-case `t`.
- **Five sub-paths under a hosted plug-in are real** beyond cycle 52's `prefs`:
  `prefs/list` (byte-identical to `prefs`), `plist` (the bundle's `Info.plist`, text/plain),
  `prefs/set` (empty 200), `messaging/wait` (a 16-byte constant) and `resources/{name}`.
  `root` is a **302 to `/system` for `com.plexapp.system`** -- the only bundle with a
  prefix -- and the framework's `IndexError` traceback for every other running one. The
  four bundles the server hosts but does not run answer the 109-byte 500 to all of them.
- **The ELEVEN legacy agents are listed beside the modern four** -- 15 rows on 1.42.2,
  identical on four servers, so it is a fixed table in the image and not hosting, the same
  argument that retired the `/:/plugins` listing. A legacy row carries `MediaType` children
  and no `Language` in the BARE form; in the typed form the primaries for that type come
  FIRST with their own `Language` CODES (no titles: 1 for Personal Media, 33 for the legacy
  IMDb agent, 26 for The Movie Database). 1.43.4 adds an `id` to every legacy row; the
  order of `lastfm` and `plexthememusic` is the server's own load order. With them listed,
  `/system/agents/attribution[/image]?identifier=` is reachable: nine texts, eight images,
  `org.musicbrainz.agents.music` an EMPTY text and no image, everything else a 404.
- **A framework error KEEPS the connection alive.** All five `plugin-xml` paths answer
  `Connection: Keep-Alive` and really serve the next request on the same socket -- cycle
  45's rule, a refusal of the MEANING rather than of the ENVELOPE. **Five routes put
  `Cache-Control` SECOND** (`/system/search`, `/system/library/sections`,
  `/system/channels/*`, `/system/plugins/*`, `/system/notify/serverUpdated`) where every
  other `/system` response puts it last. **A framework container pages without an `offset`
  and still names its window in the response headers.** **`/system//` is the framework's
  empty-bodied 404**, not the 85-byte document.
- **Only the BARE `/system/agents` is PMS's own handler**: everything under it is the
  framework's, so `/system/agents/{id}/config/{N}` is written by the framework's serialiser
  (single-quoted declaration, SELF-CLOSED empty container) and is the one framework
  document carrying NO cookie. **`/system/library/sections` carries thirteen attributes**,
  its `key` the URL-safe base64 of `http://{host}:{port}/library/sections/{id}`, `unique`
  set when the title occurs once, rows sorted by the lower-cased title -- and an extra
  segment is a 404.
- **A state cookie the server cannot unpack empties the framework**: a 200 with an EMPTY
  body, `text/html; charset=UTF-8` and **`Server: TornadoServer/1.0`** -- the one `Server:`
  banner on the surface, against cycle 33's "no banner at all". It swallows the route's own
  status, the cookie NAME must be the identifier serving the route, and PMS's own handlers
  are untouched.

- **PMS's own DATABASE TEMPLATE is a column oracle.**
  `Resources/com.plexapp.plugins.library.db` is an empty 311 KB database whose schema
  names every column PMS can store (71 tables; 1.42.2's and 1.43.4's templates are
  identical and the live schemas differ only in `metadata_items.user_square_art_url`).
  **Ten more of its columns are real query fields** -- `remote isAdult
  resourcesChangedAt userFields lastSkippedAt mediaAnalysisVersion beginsAt endsAt agent
  source` -- filtering, parsing, sorting, grouping and taking an item edit on both
  references, where an UNKNOWN field answers every row. Five refuse an unparseable value
  with a 500 and `userFields` refuses every value; seven SORT and three GROUP, so here
  the sort surface is wider than the group surface; six are a 500 on the SECTION path.
- **`isAdult` is a real row attribute**: exactly the literal `1` stores it, every other
  value clears it, and the row then carries the JSON boolean `true` between
  `librarySectionKey`/`originalTitle` and `summary` on every projection.
- **A continue-watching row carries `includedAt`**, an integer directly after
  `lastViewedAt`, on `/hubs/continueWatching` and its `/items` leaf and on NOTHING else.
  Plex Web's reducer reads `includedAt ?? lastViewedAt`.
- **`includeAlternates=1` inlines only the artwork the row OWNS**: a season's and an
  episode's `/arts` are empty, so neither carries an inline `Art` -- the option must apply
  the same gate the candidate leaf does, or the server contradicts its own `/arts`.

- **The part `Content-Type` table is wider than zen's six extensions.** Measured on
  plexscratch42 one container at a time: `.ts` is `video/MP2T`, `.m4v` `video/mp4`, `.mpg`/
  `.mpeg` `video/mpeg`, `.flv` `video/x-flv`, `.wmv` `video/x-ms-wmv`, `.asf`
  `video/x-ms-asf`, `.divx` `video/x-msvideo`; `.mts`, `.m2ts`, `.ogv`, `.3gp` stay
  octet-stream. **A `.mov` is container `mov`** and the rest of the QuickTime family `mp4` --
  the extension decides, not the brand -- and **every MPEG-TS Part carries
  `packetLength: 188`**, a 192-byte `.m2ts` included. The copy's analysis is SHALLOW, so its
  fixtures are marked `shallow` and only container-level checks read them.
- **`viewUpdatedAt` is the view-state WRITE time** -- a date filter and a sort key (Plex Web
  sorts a hub on it), stamped by every rating, scrobble, unscrobble, stored progress and
  Remove from Continue Watching on exactly the rows the write touches, and never on a row.
  **`lastViewedAt`, `lastRatedAt` and `lastSkippedAt` read an unset value as 0**, like
  `viewCount`; every other date column's null matches nothing. **`lastSkippedAt`, `beginsAt`
  and `endsAt` are dates.** A negative rating other than `-1` STAMPS `lastRatedAt`.
- **The binary names filter fields no Meta advertises.** Six media columns
  (`mediaHeight`, `mediaVideoCodec`, ... -- ANY-Media semantics, case-sensitive names), eighteen
  more tag-table fields (`mood`, `style`, `similar`, ...), the photo-cluster columns (parse,
  match nothing) and `trash` as an INTEGER. The tagging join (`tagId`, `tagType`,
  `taggingIndex`) is real but matches hidden `tagType=318` taggings plexsim does not hold.
- **An episode listing's leaf-count filters read the SHOW**, as a season's do, and so does a
  season's `unwatchedLeaves`.
- **The listing query reaches further than the grid**: a show's `/children` and
  `/grandchildren` (filters, `sort=`, 500s -- "a show's season list ignores `skipCount`" is
  refuted), `/unwatched`, `/recentlyViewedShows`, the media axes (still `type=`-blind),
  `/autocomplete` and `/common?id=`. "All episodes" appears only when MORE THAN ONE season
  row is answered, paged or not. An empty Common is `{mixedFields, guid, type, titleSort}`
  and a `Common` is always paired.
- **Every TV Recently Added shelf filters its EPISODES before the roll-up**, on all four hub
  paths; a hub PAGE drops an empty-valued pair first, the `type=2` list parses it and refuses
  the per-user keys, and `contentDirectoryID` empties a section page's TV shelf and scopes
  `/hubs/items`. `unwatched=1` is a flag on `/library/recentlyAdded` and `/allLeaves` (0/1
  grammar) and on a section's `recentlyAdded` (the literal `1` only).
- **The Dashboard's view-time keys are `viewedAt>` and `viewedAt<` (both INCLUSIVE) and a bare
  `viewedAt` (exact)**; `viewedAt>=`/`viewedAt<=` are unknown keys. Top Played filters its
  history by them, `accountID` and `librarySectionID` and counts what is left. Probe with the
  client's encoding: a raw `>=` IS the `viewedAt>` key.
- **Thirteen more parameters refuse what they cannot parse** -- int32 (`augmentCount`,
  `count`, `offset`, `excludeParentID`), any-size (`monthsAgo`), int64 (`bytes`), a float
  (`maxDistance`), the 0/1 option (`personal`), `metadataTypes` (an int32 list that also
  filters) and `sectionId` (`0` and 2147483647 only) -- and an unknown item id still 404s
  first. **`sectionID` on Recently Added is a number** (`01` is section 1).

- **Related is a TAG SCORE computed by the server**: the item's first three genres and first
  four roles, a candidate needs two in common, shared-count then release date descending, cap
  200 -- zen's 59 rows and kids' four lists exactly. It is not agent data.
- **On Deck's next-up anchors on the MOST RECENTLY VIEWED watched episode** (and the row carries
  that view's `lastViewedAt`); **`recentlyViewed` and a section's `/onDeck` leaf order by the
  view-state WRITE time** (`viewUpdatedAt`); **`popular` counts HISTORY rows**, so an unscrobbled
  season stays; a playlist's `duration` sums whole seconds; the genre shelf is the head of its
  own key; a season's generated title follows an index edit (`0` is `Specials`).
- **A library's TV Recently Added shelf IS the section's mixed roll-up** (zen 12 of 12) -- a
  one-episode show is its episode there. Cycle 50's "top-level shows" was kids' one show.
- **Structural parameters are parsed wherever they are read**: `sort`, `group`, `sourceType`,
  `limit`, `index`, `type` refuse on every axis, subset, children list, people list,
  autocomplete, `/common` and extras listing; `sourceType` other than 1 ORDERS by id. **An
  integer is `[+-]?digits`** (`+1` and `01` are 1) and every include/async option takes
  `+?0*[01]`; the include blocks reach every hub's full rows.
- **Filter values**: `=-1` is "no value" on `rating`, `audienceRating`, `userRating`,
  `absoluteIndex`, `parent`; a date epoch is a strict int64 with no spaces; a bare `=` on the
  per-user dates takes an int32 only; `clusterStartsAt` is a date; `decade`/`viewCount`/
  `viewOffset` are int32. An empty `title=` matches everything, generated titles included.
- **`/hubs/search?sectionId=` refuses any id naming no section**; `personal=1` empties only the
  MOVIE Recently Added list; `/hubs/promoted` refuses the fifteen hub keys whenever a show
  library's shelf is in scope; the `X-Plex-Product` HEADER beats the query for client titles.
- **`/library/search?limit=4294967296` crashed kids' PMS.** Treat search limits past int32 as
  crash-shaped on a reference, like `/library/shared/all?sort=`.

- **The loop's stopping rule is client impact** (HANDOVER 1): tag every finding CLIENT-VISIBLE
  (with the client call site) or MALFORMED-ONLY; build the first, record the second.
- **A show library's provider storage totals are its episodes'**; the playlist Meta's Type key
  carries `playlistType`/`sectionID`/`type` and nothing is active on `/playlists/all`; a legacy
  agent's `MediaType` carries its own `Language` rows; `decade` ignores its operator; any
  per-user write makes `viewCount` and `skipCount` read 0; `/hubs/sections/{id}` honours
  `includeGuids=1` (python-plexapi always sends it).

- **Lists were never on kids or zen, so they are measured on the copies.** A collection search
  row is on the tag ladder; a list's history is an empty 200; a playlist scores one step below a
  movie; `X-Plex-Sync-Version` puts `allowSync="1"` on playlist rows and a playlist's items.
- **Home's merged Continue Watching hub holds the On Deck next-up rows too** (stamped
  `includedAt`); a library's own Continue Watching shelf lists a next-up only after a PLAYBACK
  completion, never after a scrobble; a never-watched show carries its FIRST episode as `OnDeck`;
  a collection of two or more adds a `collection.related` shelf to its members' Related, whose key
  filters on `tagId=` (exact `=` only); `playQueueLastAddedItemID` is the tail of the Up Next
  block after the selection.
- **A LEGACY agent has a combination chain, and the Agents page saves it.**
  `/system/agents/{id}/config/{N}` lists every agent serving the type -- the saved (or
  default) order first with `enabled="1"`, the primary appended if the order omits it, then
  the rest by identifier -- and `PUT ?order=` answers the `<combine>` document (`Content-type`,
  lower-case `t`, with the cookie). An empty order is the framework's `split` AttributeError, a
  missing one its TypeError; mediaType 8/9 is a `KeyError` for a non-legacy agent and 20 an
  AttributeError for every one; a non-integer type is a ValueError. Both answer XML whatever
  `Accept` asks. See `legacy_agent_config.json`, zen's 192 cells byte for byte.
- **Top Played counts an episode's play on its SHOW at `type=2` and its SEASON at `type=3`** --
  zen's one show answers `globalViewCount: 73`, its episodes' history rows. The Dashboard's TV
  column asks `type=2`, which never matched a history row's own type.
- **Statistics buckets are CALENDAR buckets in local time**: a day at local midnight, a week on
  a Monday, a month on the 1st, a year on January 1st (zen UTC, kids Europe/Berlin). Flooring
  the epoch to fixed widths put every week on a Thursday. See `sessions.bucket_start`.
- **Edit Library > Visibility (`hidden`) is a real filter.** At 1 the library leaves every Home
  shelf, `/hubs/continueWatching`, `/library/recentlyAdded` and `/library/onDeck`; `/hubs`
  keeps its shelf EMPTY and `/hubs/promoted` drops that library's shelf outright; its own page,
  section hubs and On Deck are unchanged. At 2 it also leaves `/hubs/search` and
  `/library/search`, and a search scoped to it is a 404. The `/library/sections` row reports it.

- **A sub-minute beat deletes the resume point only inside a session that has passed the
  minute.** A NEW playback session's beats under a minute -- "Play from beginning", stopped
  within a minute -- answer `ignore` with the STORED offset and keep it, on both versions.
- **A beat at EXACTLY the stored position writes nothing**: `lastViewedAt` stands still and a
  row removed from Continue Watching stays removed, while 1 ms either side stamps and restores it.
- **A section's Continue Watching gate is the SHOW's play history**: once any episode was
  completed in a playback session, the shelf lists the On Deck episode however it became next
  (Mark Played on a later episode or the season, and after the played one is unscrobbled).
- **A collection rename to a name a row of the library carries as a `Collection` tag is a
  409** on the section path, ignoring case, and changes nothing; its own title in any case is a 200.

- **A library's Continue Watching shelf gates on the SHOW's play history** (cycle 58): once one
  episode has been completed in a playback session the next-up is listed however it became
  next. A new session's sub-minute beats keep the resume point, and a beat at EXACTLY the
  stored position writes nothing -- a removed row stays removed.
- **`hidden=1|2` keeps a library off Home and `/library/recentlyAdded`** (2 also out of both
  searches), with the Home shelf kept and emptied; statistics `at` buckets align to LOCAL
  calendar boundaries; `/library/all/top?type=2|3` rolls an episode play up to its show and
  season.

- **`X-Plex-Language: en` is NOT "no translation", and a language enum is not every enum
  whose first row says `Library default`.** Cycle 50's injection caught Show/Hide and every
  other library-default enum, so under python-plexapi's default header a show's Seasons
  dropdown offered 46 languages. An item's own language enum is built at READ time and is
  localised piece by piece (`item_language_names.json`): the head by tag, the tail from the
  table in its own order, the inherit option on its own.
- **`translations.json` is per VERSION** -- the seventh such fixture. It was captured off kids
  (1.43.4); re-capture it with `capture_i18n.py` against zen (`I18N_BASE`, `I18N_IDS` remapping
  kids' ratingKeys) and overlay, never replace: zen lacks what kids' paths alone reach.
- **An upload belongs to its row, and a theme or logo upload is stored.** A poster upload keeps
  the agent's candidate unselected with exactly one selected; a theme becomes the row's own
  versioned `theme` (served `audio/mpeg3`), a logo a `clearLogo` Image (served `image/jpeg`).
  The candidate lists were keyed server-wide by the bytes' hash.
- **A playlist is a timeline row** (`Playlist.updateTimeline()`): the item machine runs on it,
  `viewCount` steps from its born 1, and Now Playing shows a `Playlist` with `User` and `Player`.
- **What a library OFFERS follows what it holds**: a Collections pivot while it holds a
  collection, a Playlists source (one `playlists.video` pivot) once a video playlist exists, and
  a library's Recent Playlists shelf is its key's head -- an EMPTY playlist answering every
  library's `sectionID=`.
- **On 1.42.2 the PINNED list shapes Home's Recently Added**: only the first pinned library of
  each type carries a shelf, merging every pinned library of that type; an empty pinned list
  gets none, and the See All leaf takes the pinned list over `contentDirectoryID`.
- **A proxied `/player/*` command (with `X-Plex-Target-Client-Identifier`) is a 404**, and
  `DELETE /library/sections/all/refresh` a 200 with a "Library scan canceled" status frame.
- **Drive the RENDERED client, not only its requests.** Cycle 59 pointed the real Plex Web app
  at plexscratch42, kids and plexsim through kids' 9223 Chromium with `page.route` answering
  plex.tv, and found four surfaces no request replay had: a response is only half of what the
  client shows, and whether a tab EXISTS is decided by another endpoint entirely.

- **Localisation is an AXIS, not a table lookup.** A real PMS localises a stream's language by
  its TAG (under `en` too), the WORDS a title is composed from, a hub title AROUND the name it
  carries, and the lead of an enum built from the host -- none of which a table of whole English
  strings can hold. And the table itself is per version: capture it with `I18N_KEEP_SAME=1` so
  the cells a language leaves in ENGLISH remove the other version's translation.
- **Joining a collection LOCKS the member's `collection` field**, and nothing unlocks it but an
  explicit `collection.locked=0`. A check that adds to a collection must unlock afterwards.
- **An artwork leaf's upload is served to GET and HEAD only**; every DELETE on 1.42.2 is a 404.
- **A symlinked media file is re-homed by the next scan**: PMS resolves the link and the rows
  follow the TARGET. Make a test show from re-muxed copies with their own bytes (cycle 60 lost
  cycle 59's `Twofold` that way).

- **The collection tag TABLE outlives every collection**: a rename onto any name in it is a 409,
  and a NEW tag's first ASCII letter is upper-cased. Each rename or add leaves a name behind on
  a copy for good -- use a fresh name per run.
- **A session is listed from its first `playing` beat, and carries `Session` only after a
  decision** -- the key is allocated on a `paused` beat all the same.
- **`/:/prefs` pages like a listing.** A capture sending `X-Plex-Container-Size: 20` reads twenty
  settings and nothing says so; cycle 60 took that for the token's reach.

- **A renamed collection's members keep the OLD name on their LIGHT rows** until a collection
  tag is written on the item itself; the detail row and the axis move at once. A private
  `_collectionLight` map carries it, and only `_public_row`'s whitelist lets it reach `_grid_row`.
- **`Bandwidths` is withheld from `X-Plex-Product: Plex for Windows`** and from nothing else.

- **Every product-keyed rule is a TABLE, and a table sampled short is a finding waiting.** Sweep
  the product strings the binary carries: the codec suffix is 21 products (the bare `Plex HTPC`
  stays plain where its `for <OS>` variants do not), the `Bandwidths` suppressor two.

- **The reference's own translation catalogues are an instrument** (`Resources/locale/*/
  LC_MESSAGES/plex.mo`, readable with `cat` on zen): search them for every string plexsim leaves
  in English. A capture only finds the strings on the pages it walks.
- **`Plex Home Theater` alone gets a five-component server version** on `/` and
  `/media/providers`.
- **Activity titles are localised per notification SOCKET**, fixed at the handshake
  (`X-Plex-Language` query, else `Accept-Language`); status and progress frames are not.
- **`X-Plex-Features` is not inert**: it keys the library hub page's cache, as `X-Plex-Language`
  does, so interleave reads before calling a hub difference drift.
- **Plex Web sends 44 language values** (`ak.languages` in its bundle), not the 28 the table
  was captured for; measure a language rule against that list. Activity titles AND the
  butler's `ProgressNotification` sentences follow the socket's language; a scan's progress
  and status lines do not.
- **Sweep with the client's WHOLE header set.** The Roku pivot strip was measured with the
  platform header alone and is gated on the `X-Plex-Version` every real Roku sends.
- **`Accept-Language` is matched as a LIST**: a language with no catalogue of its own (`ca`, `nn`)
  is a fallback that any exact catalogue later in the header beats (Chrome's Catalan header is
  English).
- **An item-prefs sweep must pair plexsim's item with a MATCHED reference item** (zen show
  6558/6559/6560): an unmatched one has no `showOrdering` enum and another content head, so the
  comparison is silently empty. A socket's regional catalogue (`hu-HU` reads `hu_HU`) answers
  before the one it is based on.
- **A `plex://` guid must be one plex.tv knows.** The native Plex for Android app (2026.9.0,
  cycle 70) never asks luma.plex.tv for a movie's or show's detail screen when the guid is
  minted, so the action row stays a skeleton and a show's Seasons block spins forever. The
  catalog takes Plex's own guids from `metadata.provider.plex.tv` (`POST
  /library/metadata/matches {"type":1|2,"guid":"tmdb://N"}`, then `children` by `index`):
  `build_catalog.py --relink` with `PLEX_TOKEN`. The provider refuses `X-Plex-Container-Size:
  500` with a 400; the agent pages at 50.
- **`/service/ultrablur/colors` resolves its `url` like `/photo/:/transcode`** and answers the
  corner colours as a JSON LIST of one (an element in XML). Plex for Android uses the singular
  `/service/` spelling for any item without `UltraBlurColors`.
- **`excludeElements=Part` is a LISTING rule too**: Parts go and a trailing `Media.partCount` comes,
  on section listings, hubs and nested Related rows, not only on a detail row.
- **History rows come in two kinds.** A scrobble's carries `viewType: 1`; a play a timeline
  completes carries none and is `type=0`, which is what an absent `type=` means. Plex for Android's
  Play History and python-plexapi's `history()` send no `type`.
- **A 90% crossing needs a non-zero previous beat** in the session: Plex for Android beats `time=0`
  until its player advances. A reference beat that 500s after ~10 017 ms is PMS logging "Waited
  over 10 seconds for a busy database" (another writer), not a rule: the failed crossing beat
  still CONSUMES the crossing, so the session's next `stopped` past 90% answers `ignore`,
  `viewCount 0`, no history row. Reproduced on an idle plexscratch42 by holding `BEGIN
  IMMEDIATE` on the library DB across the 92% beat (2/2); without the lock, `paused 0 → playing
  3000 → stopped 93%` completes on the stop (3/3), and a 400-refused beat consumes nothing. Our
  servers never hit a busy DB, so this is not emulated (cycle 86's `advance61` web-firefox leg).
  The OFFLINE timeline (`offline=1&viewCount=`) marks watched (one view
  per sync) or, with 0 on a watched item, clears it.
- **An episode's post-play carries `tv.upnext`**: the show's On Deck row when it comes from view
  state and is not the episode itself.
- **Drive a native client through a proxy that owns plex.tv's resource list.** Plex for Android
  lists every owned server; rewrite `/api/v2/resources` to one entry (with `accessToken` and a plain
  `http://IP:port` connection), answer plex.tv writes locally, and answer the PIN poll with a token.
  The emulator resolves names itself and CONNECTs to an IPv6 literal the Mac cannot route: re-resolve
  the SNI name over IPv4 in `server_connect`.
- **A hub title is localised wherever the hub sits**: the post-play `tv.upnext` (`Up Next` is in the
  catalogues) and the `Related` block INSIDE a detail row, whose member rows keep the row guard.
- **Plex for Android does not re-request a detail screen from luma after an app restart** on either
  server; a walk that restarts the app between steps sees skeleton actions on the second open.
- **After a cold boot the Android emulator's Chrome first-run screen can swallow the sign-in tab**:
  `harness/android/signin.py` retries (back, bring the app forward, repeat while Sign in shows).
- **A real PMS keeps a write it answered, even through a hard kill.** Five client writes (a rating,
  a scrobble, a playlist, a server pref, a locked edit) answered 0.17-0.5 s before `docker kill -s
  KILL` all survived on plexscratch. A coalescing saver loses exactly that window; with `--state`
  plexsim saves a client write (any non-GET, and the scrobble/rate/progress/timeline GETs) before
  its status line.
- **An unseeded `sort=random` is a fresh order on every request** (zen, kids, plexscratch; `random:desc`
  too). Only `random:<seed>` holds one order, and a `random="1"` shelf's sample is a different
  mechanism that stays stable for the process. Plex Web's "Randomly" sends no seed.
- **What a restart changes on a real PMS** (cycle 73, seven restarts of plexscratch): watch state,
  resume points, ratings, locked edits, lists, play queues (with their selected item), devices,
  history and its deletes, prefs, agent order, uploads and created libraries are KEPT, and ids keep
  counting up; the live session, the section timeline, open notification sockets, the hub page's
  per-URL cache and the genre/actor shelf pick are DROPPED or re-rolled; `/` `updatedAt` steps to
  the start time and `sessionKey` numbering restarts (neither is read by a client).
- **A sidecar's codec suffix is localised in the plain title too.** Plex Web (the codec-suffix
  products) gets `Unbekannt (SRT Extern)` on 1.42.2, the same word the extended title carries; a
  catalog with no sidecar hides it, so a pairing sweep over catalog items is empty here, not clean.
- **A restart does not refresh the fuzzy-search vocabulary.** On plexscratch a title edited before
  a restart stays unfound by a misspelling after it (`/hubs/search`, `/library/search`); only an
  optimize rebuilds the spellfix tables. `--state` saves the vocabulary's rows rather than
  rebuilding them at restore.
- **A device row is written by the request that first names it**, and survives a hard kill 0.16 s
  after a plain GET's answer on plexscratch. plexsim registers a device while counting the
  response's bytes, after the answer; with `--state` a new or renamed device is registered first
  and the request is saved before its status line.
- **An empty maintenance window is not stored.** A `/:/prefs` write leaving `ButlerStartHour ==
  ButlerEndHour` moves the end hour to start + 1 (23 wraps to 0) on 1.42.2 and 1.43.4, with a
  `preference` frame for the moved setting first; a window across midnight is kept.
- **An uploaded sidecar's `key` serves its bytes** (200 `text/html`, length before type, the request's
  connection state -- cycle 74's `close` was urllib's own request header echoed; `autoAdjustSubtitle=`, `format=srt` and `.srt` alike) on 1.42.2, and Plex for Android loads
  it on every play of an item whose selected subtitle is a sidecar. Only an EMBEDDED track is the 501.
- **Shuffle on a queue nothing was added to is real** (1.42.2 and 1.43.4): `PUT
  /playQueues/{id}/shuffle` puts the selected item first and reorders the rest, `unshuffle` restores
  the natural order (a queue created with `shuffle=1` too); both 200 with the queue, version + 1, every
  row but the selected one re-minted by position. Cycle 15's 404 was measured after an add only.
- **`urllib` sends `Connection: close` on every request**, and a real PMS answers the connection state
  the client asked for. A header read through it is the client's, not the server's: read an envelope
  over a raw socket (cycle 75 found cycle 74's recorded `Connection: close` was exactly this).
- **A real PMS answers a burst of simultaneous connections** (30 of 30 on plexscratch42 from kids);
  `socketserver`'s default listen backlog of 5 reset most of them, and six -- a browser's per-host
  connection count -- already lost some. plexsim listens with a backlog of 128.
- **A play queue has an OWNER, and `own=1` moves it.** A queue read with `own=1` by a client that does
  not own it takes the queue (version + 1; the same owner's read changes nothing), and the
  one-queue-per-client rule follows the owner: the taker's other queue is retired at once, its next new
  queue retires this one, and the creator's new queue leaves it alone (1.42.2 and 1.43.4). Plex Web hands
  another player the queue key with `own:1`.
- **Season 0 is `Specials`, and only a show that holds one carries `seasonCount`.** On zen (1.42.2,
  509 shows) the four shows with a season 0 answer `childCount` WITH it and `seasonCount` without it
  (Bluey 4 and 3) straight after `childCount` -- detail, section listing, hubs -- and no other show
  carries the attribute. The season's generated title is translated like `Season N` (its `title` and
  its episodes' `parentTitle`; 13 of 45 languages leave it English), stays English as the season
  list's `title2`, and the container's `parentTitle` is empty. Plex for Android draws the server's
  title on the season card (it sorts season 0 last) and computes its own "N Seasons" header from
  truthy indexes; python-plexapi's `Show.seasonCount` reads the attribute and falls back to
  `childCount`. A pristine show with specials puts its first REGULAR season's first episode on deck.
- **`flattenSeasons=1` is a PROJECTION of the episode row and moves no reader.** Re-measured on
  plexscratch (1.43.4, 2026-09-30; `tests/fixtures/flatten_seasons_reference.jsonl`) over 35 paths
  with Firefly unflattened, flattened, and flattened with S1E1 watched. Every episode ROW -- detail,
  `/children`, `/allLeaves`, the section listings and subsets, the three searches, On Deck, the hub
  pages, a play queue -- says `skipParent` and drops `parentRatingKey`/`parentKey`, and the show row
  says `skipChildren`; but no row SET changes. A season's `/children` still lists its episodes,
  `/library/recentlyAdded` still rolls them up to their seasons (`leafCount: 3`),
  `recentlyAddedGrandparents` and the Recently Added shelves (`home.television.recent`,
  `tv.recentlyadded.N`, `/hubs/home/recentlyAdded?type=2`) still answer the one SHOW, a scrobble still
  gives the season `viewCount`, `lastViewedAt` and `viewedLeafCount: 1`, and the season's
  `includeOnDeck=1` is the next episode. A HISTORY row is its own projection: it keeps `parentKey` and
  says no `skipParent`. The oracle took the keys off the stored row, so each reader that finds a
  season's episodes by `parentRatingKey` lost them, and the native server grouped such a show's
  episodes under no season on the Recently Added shelves to match. Two things to know when measuring
  it: a hub page answered BEFORE the prefs write comes back from PMS's hub cache with the old rows
  (ask with a `count=` no earlier request used), and unscrobbling the episode does not clear the
  `viewCount`/`lastViewedAt` the scrobble stamped on its season and show. The UNMATCH cascade is the
  ordinary one too (cycle 84, plexscratch 1.43.4, an A/B on a freshly reset copy each time: Firefly
  flattened then unmatched, against Firefly unmatched unflattened). The 58 cascade frames are
  identical in order, state, `metadataState`, title (`Firefly S01 E01` -- the frame title keeps the
  season either way) and activity progress; only the copy's asynchronous `Processing
  subscriptions` triple moved. The rows after it differ by exactly the projection: the show keeps
  `skipChildren`, every episode `skipParent` without `parentRatingKey`/`parentKey`, and the
  seasons are listed and become `local://` as unflattened. Both servers already did this (each
  server's flattened and unflattened unmatch differ by the same projection alone).
- **A collection over a SECTION uri holds that library's rows at the COLLECTION's level.** Measured
  on plexscratch42 (1.42.2, `tests/fixtures/collection_section_uri.json`): `type=2&sectionId=3` over
  `/library/sections/3/all` holds the library's two shows (`subtype: show`, `childCount: 2`), `type=3`
  its four seasons and `type=4` its twelve episodes; the uri's own `type=` is ignored (`type=2` over
  `all?type=4` is still the shows) while its filters apply, and an items add reads a section uri the
  same way. `type` is an INTEGER naming the members' type: `abc`, `2.0` and an empty value are 400s;
  no `type`, or a negative one, is the library's own type; 5 `trailer`, 8 `artist`, 18 `collection`
  and so on hold nothing. A create keeps only rows of that type in its OWN library -- `type=4` over a
  show's key, `type=2` over an episode's, another library's key or section are empty collections of
  the named type. The oracle expanded a section's shows into episodes as a play queue does; the
  native server listed the shows whatever the type. An EMPTY collection carries no `childCount` on
  1.42.2, where plexsim sent 0 -- see the empty-collection rule below.
- **The paging HEADER beats the query on every route family** (cycle 16, re-measured cycle 83 with
  GETs on kids, zen and plexscratch42, `tests/fixtures/paging_header_precedence.json`): section
  listings, `/children`, `/allLeaves`, `/hubs`, section hubs, `/hubs/search`, On Deck, Recently
  Added, `/library/sections`, history, `/:/prefs`, a detail row, `/identity`, `/playlists`, a
  section's `/collections`, a collection's children and a playlist's items all answer the header's
  start and size, per parameter, and a header `Start=abc` beside a valid query is a 500. The native
  server took a header only when the query had not named it; both now override at the door.
- **An EMPTY body is never gzipped**, whatever the client offers. The official client's own
  `PUT /updater/check` (`tests/fixtures/reference-walk.jsonl`, `Accept-Encoding: gzip, deflate, br,
  zstd`) is answered `Content-Length: 0`, `Content-Type: text/html`, no encoding pair, length before
  type. The oracle compressed nothing into a 20-byte stream with `X-Plex-Content-Original-Length: 0`,
  which also moved every empty write answer, a 204 included, into the gzip family's header order.
- **`/photo/:/transcode` output caches for three days whatever it transcodes.** A bundled resource
  (`url=/:/resources/movie.png`) answers `max-age=259200` on kids, fresh and cached alike, and so does an
  item's artwork; the same files read directly are `no-cache`. The oracle
  served a resource and an uploaded poster through the transcoder with the direct path's `no-cache`.
- **A collection's `/library/metadata/{id}/children` names its section by NUMBER** --
  `librarySectionID: 1` on the container, paged or not, as on every other container (plexscratch42,
  four movie collections). Both servers sent the create request's `sectionId` string.
- **A bare `OPTIONS` on a multi-segment path reaches only the handlers that take any verb.** Swept
  2026-09-30 over every multi-segment route the oracle names, on kids and plexscratch42: the
  transcoders (`/photo/:/transcode`, `/video|subtitles/:/transcode/universal/*`), the `/:/` timeline
  writers, `/:/resources/*`, `/log/networked`, `/security/{resources,token}`,
  `/status/sessions/terminate` and `/services/codecs` answer it exactly as a GET (their own 400, the
  image, an empty 200, the 403), beside the envelopes already recorded for `/system`, `/:/plugins`,
  `/match` and `/:/viewChange`. Every GET-only read route is the ordinary 404 -- the hubs, `/accounts/{id}`,
  `/myplex/account`, `/livetv/*`, `/:/eventsource/notifications` (where the oracle opened an event
  stream that never ended). The oracle routed OPTIONS like any verb everywhere; the native server
  404'd the any-verb handlers too. Not settled: `/search/{type}`, `/services/browse/`, `/web/*`,
  `/manage/` and the empty-segment forms (`/accounts//`) differ from the reference on BOTH servers,
  on their GET as much as their OPTIONS.
- **A lower-case first byte on a connection's first request is checked BEFORE every other rule of
  the line, and the socket goes at once.** `get`, `getgetgetget` (past the method bound), `get
  /id%zz...`, `get ... HTTP/1`, an LF-only `get` line and `get  /identity` all draw nothing and EOF at
  0.0 s on kids (1.43.4) and plexscratch42 (1.42.2), re-measured 2026-09-30 -- which overturns cycle
  27's "held to the 20-second idle close". An unterminated `get ` is held until the line completes.
  The oracle answered the five malformed ones 400; the native server held every one to its read
  timeout and then wrote a 400.
- **An idle keep-alive connection is dropped at 20 s with NOTHING written** (kids, after a completed
  `GET /identity`). The native server turned its read timeout into a parse error and wrote an
  unsolicited 400 onto every idle socket before closing it.
- **"Unplayed" is by LEAVES on two lists and by the row's own counter everywhere else.** On zen
  (cycle 83, `tests/fixtures/unwatched_zen.json`; 509 shows, six partly watched, two fully, none
  carrying a `viewCount` of its own) a show library's `/unwatched` is the 507 shows with an
  unwatched leaf -- the `unwatchedLeaves=1` set -- and a show's `/children?unwatched=1` keeps each
  season that still has one (cycle 71 saw both copies keep seasons whose own `viewCount` was 7). On
  `/children` only the literal `unwatched=1` is that flag: `unwatched!=0` keeps every season and
  `=0`, `!=1`, `==1`, `=2` none. The filter COLUMN stays the row's own counter:
  `all?type=2&unwatched=1` answers all 509, the fully watched shows included, and `=0` none -- so
  "partly watched shows vanish from the TV Unwatched filter" is not the rule to copy, and Plex Web's
  TV filter sends `unwatchedLeaves=1` (the Filter the show Meta advertises) anyway. plexsim read the
  own counter on both lists, and an episode scrobble stamps one on its season and show.
- **`/hubs/sections/{id}/{leaf}` routes four leaves and 404s the rest.** Swept on kids (1.43.4,
  2026-09-30) with 1,450 names under both sections: only `extras`, `manage`, `newReleases` and
  `popular` answer (`popular` 500s on a movie library there, see the route), and every other leaf --
  `foo`, `continueWatching` without `/items`, `extras/x`, `manage/x` -- is the 85-byte Not Found
  document. The native server took the first segment after `sections/` and answered the section's
  Recommended page for any of them. `/hubs/metadata/{id}/` routes `related` and `postplay` alone and
  `/hubs/home/` `continueWatching`, `onDeck` and `recentlyAdded`; both servers already agreed.
  (The sweep that found it showed the native answer "matching" early on: both requests had failed
  identically with the client out of ports, which `ndiff` does not report.)
- **A `/web/*` 404 IS gzipped; only the 200 asset is not.** Measured on kids (1.43.4, 2026-09-30)
  with `Accept-Encoding: gzip`: `/web/zzNoSuchLeaf` and `/web/js/zz.js` answer the 85-byte Not
  Found document as `Content-Encoding: gzip`, `X-Plex-Content-Original-Length: 85`, 78 bytes on the
  wire, and no connection state (the socket closes). The cycle-16 rule "a web asset is never
  gzipped" is about the served FILE; the native server applied it to every `/web` path. `/web/`
  (trailing slash) is the same absolute 302 as `/web`, which the native server 404'd.
- **Every leaf type's `/children` is the 400.** Measured on kids (1.43.4, 2026-09-30): a movie (1),
  an episode (8) and a clip (5) all answer the 89-byte Bad Request document, paged or not. Their
  `/allLeaves` and `/grandchildren` differ by type -- 200 for a movie and an episode, 404 for a clip.
  The oracle refused all four leaf types; the native server refused only a movie and answered an
  episode an empty 200 container. (A clip's 404s are checked on `catalog_features.json`'s trailer --
  see below.)
- **A client's first request registers its device BEFORE the answer.** Measured on kids (1.43.4,
  2026-09-30) with a new synthetic identifier: the very first `GET /devices` it sent already listed
  its own row (`name`, `platform`, `clientIdentifier`). The oracle registered early only under
  `--state` (cycle 74's save-before-answer) and otherwise when it counted the response's bytes, so
  without `--state` the row appeared one request late -- and a numeric `X-Plex-Platform` on that
  first request answered 200 where the row it writes makes the list the measured `std::bad_cast`
  500 (HANDOVER §9). The sweep's "500 on one server, 200 on the other" for `X-Plex-Platform: de` and
  `Connection: de` was that same 500, left behind by a concurrent numeric-platform cell on one server
  before the other; every later `/devices` 500s on both until a non-numeric platform overwrites the
  row, as on a real PMS. Numeric spellings are never sent to a shared reference.
- **An idle keep-alive socket is closed at 20 s with NO bytes -- after a ranged 206 as after
  anything.** Measured on kids (1.43.4, 2026-09-30) on a raw socket: `Range: bytes=0-1` on
  `/identity`, `/accounts` or `/status/sessions` answers 206 with `Content-Length` twice (the whole
  body's, then the slice's) and `Connection: Keep-Alive`; a second request on the socket is served;
  then the socket goes quiet and closes at 20.0 s. A socket that sent nothing, or half a request
  line, closes the same way. The native server answered every such read timeout with a 400 before
  closing, which a client that believed the first `Content-Length` (python's `http.client` does)
  read as the rest of its body -- the sweep's "oracle IncompleteRead, native 206" cells.
- **The multi-id container's `mediaTagVersion` is a NUMBER.** Measured on kids (1.43.4,
  `/library/metadata/1,17` and `/8,9`) and zen (1.42.2, two movies), with `X-Plex-Language: de` and
  `en` alike (2026-09-30): `"mediaTagVersion":1786914632`, as on every other library container. The
  native server sent it as a string on `/library/metadata/{a},{b}` only, in every language; the
  sweep saw it only under the language headers because the family's base already carried it.
- **`/webclient`, `/websocket`, `/webm`, `/web2` are NOT the web client's tree.** Measured on kids
  (1.43.4, 2026-09-30): each is an ordinary single-segment plug-in path -- the dispatcher's
  `{"size":0,"content":"plugins"}`, gzipped when offered, in the container header order
  (`Content-Type`, `Connection`, `Keep-Alive`, the gzip trio, `Content-Length`, `Cache-Control`), a
  403 with no token, and a 404 with an empty trailing segment (`/webclient//`). Both servers tested
  the bare prefix `/web`: the oracle gave these paths the asset header order, let them through the
  token gate and exempted them from the empty-segment rule; the native server also refused to gzip
  them. The web tree is `/web` and what is under `/web/`.
- **The bare `/library/metadata` is a 404.** Measured on kids (1.43.4, 2026-09-30): GET and HEAD,
  with or without paging, answer the 85-byte Not Found document, as the oracle does. The native
  server answered a 400 there, with no measurement behind it.
- **`/history` is the plug-in dispatcher's empty listing, not the history page.** Measured on kids
  (1.43.4, 2026-09-30): `{"size":0,"content":"plugins"}` (49 bytes), with any query --
  `accountID=abc` is not validated there. The history page is `/status/sessions/history/all`; the
  native server also routed the single segment to it, so `/history` listed plays and refused a bad
  `accountID` with a 400.
- **The columns a listing REFUSES are refused wherever the filter grammar runs** (cycle 83, kids
  and zen). `art`, `banner`, `theme`, `thumb`, `slug`, `extraType`, `parentIndex` and `userFields`
  are 500s on a show's `/children` and `/grandchildren`, every tag axis, `/common`, `/search`,
  `/autocomplete`, `/unwatched`, `/firstCharacter`, `/collection` and `/recentlyViewedShows`, not
  only on the grid, `/library/all` and `/playlists`; and the hub pages refuse all eight on the
  fifteen's terms (non-empty value, a show library's shelf in scope), so the hub set is the
  fifteen PLUS the listing's refusals. The section extras leaf still filters on `extraType`.
- **`guid=` is a grammar field on every listing**, not a grid-and-`/library/all` special case
  (cycle 83, kids and zen): `guid=1` empties every tag axis, `/children`, `/grandchildren`,
  `/search`, `/autocomplete`, `/unwatched`, `/recentlyViewedShows` and a hub page's TV shelf. The
  prefix is CASE-SENSITIVE (`guid=PLEX` nothing), the `<=` key is the same prefix, and a comma is
  the grammar's OR (`guid=<a>,<b>` is both rows) -- a private prefix pass read it as one guid.
  kids answers `/library/all?type=1&guid=<a>&guid=<b>` with ONE row where the section grid
  answers none -- the last-occurrence rule below.
- **The filter field table is the Meta's, not a hand list** (cycle 83, kids and zen). `dovi` and
  `network` are advertised Fields and filter (`dovi=1`, `network=1` empty every consumer on
  kids), and `role` is no field at all: `role=1` answers every row where `actor=1` answers none.
  The native table lacked the first two and filtered `role` as the actor tag.
- **A movie-scoped `/hubs/promoted` runs no filter at all** (cycle 83, kids): at
  `contentDirectoryID=1` `resolution=1`, `extraType=1`, `parentIndex=1` and `art=1` are 200s,
  at `=2` 500s -- only a TV shelf parses the request's filters.
- **`/library/metadata/{id}/allLeaves` ignores `sort=`** (cycle 83, kids' show): `title:desc`,
  `titleSort`, an empty value and the unknown key `zzz` all answer the episodes in broadcast
  order with a 200.
- **The `include*` blocks reach a library hub page's FULL rows, and `/common?id=` takes two of
  them** (cycle 83, kids and zen). `/hubs/sections/{id}`'s TV Recently Added show row takes
  `Children`, `Extras`, `OnDeck`, `Preferences` and `Related`; no light row takes any. The Common
  row takes `Extras` and `Related` ONLY -- a movie's and Firefly's alike, never `Children`,
  `OnDeck` or `Preferences`.
- **`includeGuids=1` never gives an unmatched `local://` row a Guid** -- on the grid, on
  `/grandchildren` and on a hub page alike (cycle 83, kids' episodes 12-14), where a matched row
  with no external ids gets the one-entry list. `/grandchildren` honours the option at all.
  Widened below to `tv.plex.agents.none://` and to rows whose detail keeps its ids: one gate.
- **`/recentlyViewedShows` is a DEFAULT window** (cycle 83, zen's 509 shows and kids): with no
  `X-Plex-Container-Size` it answers the first 25 rows at `offset` 0 with the full `totalSize`, a
  lone Start ignored; once a Size is sent the shared rule applies (both page, a lone Size or a
  negative value is every row with neither field). `/folder` follows the shared rule plainly.
  A NON-INTEGER lone Size (`X-Plex-Container-Size=abc`) is a 500 on kids and zen on the grid,
  `/folder` and `/recentlyViewedShows` -- see the paging grammar below.
- **A scope that names no relative of the row reads the ROW ITSELF** (cycle 83, kids' movie
  library): `episode.title=Sintel`, `show.title=`, `season.title=` and even `artist.title=Sintel`
  each find Sintel, and an empty `episode.addedAt=` is the date column's 500. So does an unknown
  scope prefix (`movie.genre!=1` keeps every row). The native resolver answered no rows, matching
  nothing and never parsing the value.
- **`unwatchedLeaves` takes the Meta's BOOLEAN grammar** (cycle 83, kids): on a movie grid it is
  every row for an empty value, `abc`, `-1`, `1,2` and `0`, and none for `1`. Typed as an integer
  it refused the empty and word values with a 500.
- **An `include*` block turns on for the integer ONE only** (cycle 83, kids' hub page): `01` and
  `%2B1` draw the blocks, `-1`, `2` and `0` do not -- the `_include_int` rule cycles 22 and 29
  recorded, which the native full-row passes read as "any non-zero integer".
- **A TAG-TABLE axis under any leaf-count filter is EMPTY** (cycle 83, kids and zen):
  `unwatchedLeaves`, `viewedLeafCount`, `unviewedLeafCount` and `viewedOrInProgressLeafCount`, any
  value and operator, empty the genre, director, actor, country, network and collection axes of a
  movie and a show library (zen's show genre axis: 39 entries, 0 for `unwatchedLeaves=1` and for
  `=0`), while the COLUMN axes -- `year`, `contentRating`, `studio` -- apply the filter to their
  rows (zen's show year axis: 46 at `=1`, 2 at `=0`). A value that does not parse still refuses.
- **An EMPTY `sort=` is ignored on `/search`, `/children` and `/grandchildren`** as on every
  listing (cycle 83, kids: 200s in the default order); only an unknown key refuses.
- **`location=` is the Meta's tag compared as the section Location id, on the grid too**
  (cycle 83, kids): `-1` is no row, `1` and `1,2` every row, an empty value and `abc` 500s. The
  native grid ran a private digits-only pass that refused `-1` and `1,2`.
- **`/folder?parent=` that is not all DIGITS is the top level** (cycle 83, kids: `-1`, `1,2` and
  `abc` answer the library's top folder); a numeric id naming no directory is the empty list.
- **A projected row takes only the `include*` blocks, never a detail read's fills** (cycle 83,
  kids): the show Common row with `includeExtras=1` gains `Extras` and no `duration`, where the
  native pass borrowed the detail read's, which fills a show's absent duration.
- **A response post-pass that changes nothing must keep the route's BYTES** (cycle 83):
  `/system/agents` carries repeated `Language` keys in its JSON on purpose, and a native
  `excludeFields=`/`includeAlternates=` pass that re-serialised the parsed body collapsed them to
  the last one (6,658 bytes to 679), where the oracle leaves the body alone.
- **The multi-id read's `mediaTagVersion` is an INTEGER** (cycle 83, kids'
  `/library/metadata/1,3`), as on the single-id read; the native multi-id container sent a string.
- **The TV Recently Added list refuses a refused column even EMPTY** (cycle 83, kids):
  `/hubs/home/recentlyAdded?type=2` answers `art=`, `slug=`, `thumb=` and `userFields=` with a 500 --
  that list parses empty values, where a hub page drops them (`/hubs?art=` is a 200). The native list
  found no field in the pair and answered every row.
- **The TV Recently Added list and a show-scoped `/hubs/promoted` pass the `include*` blocks
  to their FULL show rows** (cycle 83, kids' Firefly takes all five on both; cycle 55 recorded the
  list on zen) -- the native list and shelf applied none.
- **`/system/agents` orders like a container only when it answers one.** Measured on kids
  (1.43.4, 2026-09-30, raw socket): `GET /system/agents?mediaType=abc` is the 89-byte 400 as
  `X-Plex-Protocol, Content-Length, Content-Type, Connection, Cache-Control, Date` -- the refusal
  family -- while its 200 container is type-first. Both servers sent the container order to every
  status there, the PATCH 500 included.
- **An embedded stream's 501 has a reason phrase.** Measured on kids (1.43.4, 2026-09-30):
  `GET /library/streams/{id}` (and `.srt`, `.vtt`) of a stream the server holds is `HTTP/1.1 501
  Not Implemented`, 97 bytes, length before type. The native server wrote an EMPTY phrase for 501,
  and ordered an uploaded sidecar's 200 type-first where cycle 74 measured it length-first.
- **The transcoder's decode rule covers an item's own artwork.** Cycle 52's "only a 200 whose bytes
  are not an image is the 500" was implemented for server paths only; an uploaded poster whose bytes
  sniff as no image (served `application/octet-stream` directly) went back through the oracle's
  transcoder as a 200 with `max-age=259200`, and `/service(s)/ultrablur/colors` over it as a 200 tint.
  Whether a real PMS STORES such an upload at all is unmeasured (a scratch write); the lockstep
  replay reached it only because the recorded request stream kept binary bodies as the text
  `<binary N bytes>`.
- **A `random="1"` shelf is sampled natively too.** The native server sorted `movie.topunwatched`,
  `tv.startwatching`, `tv.rediscover` and `tv.toprated` by ratingKey and took the head, so on a real
  catalog the same lowest keys showed forever, in key order, across restarts -- neither half of
  cycles 28/30 (a random sample of the whole set in random order, stable per shelf and `count=` for
  the life of the process). It was the whole `/hubs/sections/{id}` residue of the lockstep replay:
  the rows are the same SET on both servers, and every field difference under them was one row
  compared against another.
- **The verb table is a rule per route FAMILY, and each server had a different half of it.**
  Measured on plexscratch (1.43.4, 2026-09-30) with PUT, POST, DELETE, OPTIONS and HEAD over every
  client read path, with kids and zen for OPTIONS and HEAD (`tests/fixtures/verb_table_2026-09-30.json`):
  every SECTION leaf takes cycle 15's table and nothing else -- the eleven tag directories,
  `firstCharacter`, `sorts`, `filters`, `unwatched` and `collections` are GET and HEAD only, the
  section itself refuses POST, and `HEAD /library/sections/{id}/prefs` is the GET; the session and
  search LISTINGS (`/status/sessions/background`, `/library/search`) and the whole `/statistics`
  subtree refuse every verb but GET and HEAD; a key list `/library/metadata/{a},{b}` refuses POST
  like one item; the provider-group LISTING answers PUT and DELETE with a 400 (with or without
  `title=`: a group is edited by id) and only a bare OPTIONS with the 404; `/playlists?type=42` is a
  read, so its POST is the create's 400 and PUT/DELETE/OPTIONS the dispatcher; and under `/player/`
  a bare OPTIONS is the same 400 as every verb. The native server routed the section leaves by name
  alone and answered the GET body to any verb; the oracle had listed read-only PATHS and answered the
  rest. Both follow the families now. A key list's DELETE is held out -- no reference may be sent one.
- **Paging is ONE merge of two transports, per parameter** (cycle 83, kids, GETs over the grid,
  `/folder`, `/recentlyViewedShows`, `/library/sections`, `/children`, `/identity`, `/:/prefs`, a hub
  page and `/hubs/search`; `tests/fixtures/paging_grammar.json`). The effective Start is the header's
  if sent, else the query's, and the same for Size; the container pages when both effective values
  exist. So a lone header or a lone query parameter never pages, a header Size beside a query Start
  pages, and a header half overrides only its own half of a query pair. Cycle 16's "header wins" and
  the handover's "a lone header does nothing" are the same rule; both servers already followed it at
  `27d76b2` (the handover's "native pages on a lone header" did not reproduce on any family).
- **The paging VALUES are 32-bit signed integers, and a lone Size is parsed** (cycle 83, kids and
  zen, the same families and fixture). Wherever a container pages, a Size is read whenever it is
  sent -- `X-Plex-Container-Size=abc` ALONE, header or query, is the 109-byte 500 on `/identity` as on
  the grid -- and a Start only beside a Size (a lone `Start=abc` is ignored; `Start=abc&Size=-1` is
  the 500). The grammar: an optional `+`/`-` and ASCII digits, `-2147483648` to `2147483647`; an
  empty value, `1.5`, `1e2`, `0x10`, `1_0`, a leading or trailing space and `2147483648` are 500s,
  `+1` and `01` page, a negative value still turns paging off. A plug-in container (`noHistory`),
  `/system/search`, a non-container root (`/butler`, `/myplex/account`), an image and an earlier
  refusal (a 404, `/:/timeline`'s 400) are untouched. Both servers read a Size only beside a Start
  and parsed with `int()` / `trim().parse::<i64>()`, so every one of these was a 200.
- **`/folder?parent=1,2` is the TOP folder, not an empty list** (cycle 83, kids, re-measured): a
  parent that is not all digits (`1,2`, `1%2C2`, `-1`, `+7`, ` 7`, `7,abc`) lists the library's top
  folder; only an EMPTY parent and a numeric one naming no directory (`1`, `99999`) are the empty
  200. A repeated `parent` takes its LAST value (`parent=7&parent=8` is folder 8, `parent=7&parent=abc`
  the top). The handover's "empty 200 on kids, 500 on both ours" predates `69f8720`; at `27d76b2` both
  servers already answered this, and the check pins it.
- **`/library/all` reads a repeated parameter's LAST occurrence; the section grid ANDs it**
  (cycle 83, kids and zen, `tests/fixtures/library_all_repeated_keys.json`). `guid=<a>&guid=<b>` is
  the <b> row, `year=2010&type=1&year=2008` the 2008 movie, `type=2&type=1` the movies, and a bad
  value BEFORE a good one is never parsed (`type=abc&type=1` a 200, `type=1&type=abc` the 400;
  `addedAt>=abc&addedAt>=1` a 200, the reverse the 500) -- the whole query is a map. The grid answers
  both guids with no row, as a conjunction. Both servers ran the grid's conjunction here (the oracle
  also emptied `type=2&type=1`, reading every `type`).
- **An EMPTY collection carries no `childCount` at all** (cycle 83, plexscratch42 1.42.2,
  `tests/fixtures/collection_empty_childcount.json`): an episode collection created over a show's key
  holds nothing, and neither its create answer nor section `/collections`, `/library/metadata/{id}`,
  `/library/collections/{id}`, `all?type=18`, `/library/all` or `?includeChildren=1` names the field,
  where a one-member collection says `childCount: 1`. Both servers sent `0`. (The same copy's section
  `search?type=18&query=` finds the empty collection; both ours answer that search with no row --
  measured beside it, not changed here.)
- **A CLIP's `/allLeaves` and `/grandchildren` are the 404** (cycle 83, kids' clip 5 re-measured,
  paged or not): the 85-byte Not Found, beside `/children`'s 89-byte 400, where a movie's two are
  empty 200s. `catalog_ui.json` holds no clip, but `catalog_features.json`'s trailer 201 is one, and
  both servers answered it the movie's empty 200s.
- **An upload is served `image/jpeg` whatever its bytes, and STORED whatever its bytes** (cycle 83,
  plexscratch42 1.42.2, `tests/fixtures/upload_non_image.json`). `POST /library/metadata/{id}/posters`
  with 32 bytes of plain text is the empty 200 of any upload: a selected `upload://posters/<sha1>`
  candidate, the row re-versioned, and the text read back as `image/jpeg` (`no-cache`) on the
  candidate's `/file?url=` and on the row's `thumb` path -- as every PNG upload on the copy reads back
  `image/jpeg` too, through the transcoder included. Only the DECODE refuses it: `/photo/:/transcode`
  and `/services/ultrablur/colors` over that thumb are the 500s the `85996be` rule already answered.
  Both servers sniffed the bytes and served `image/png` or `application/octet-stream`. Playlist and
  collection uploads take the same type here (unmeasured on a list; the oracle has one upload path).
- **`DELETE /library/metadata/{a},{b}` is the 404 and deletes nothing** (cycle 83, plexscratch42
  1.42.2, two rows of a throwaway library, `tests/fixtures/delete_key_list.json`); both rows read
  back unchanged and the key-list GET beside it is the ordinary listing. The oracle answered the
  DELETE with that listing, a 200; the native server already answered the 404.
- **`--state` keeps the play queues and the id counters.** Cycle 73 measured a restart keeping play
  queues with their selected item and ids counting UP. The native state file kept neither: after a
  SIGTERM or a SIGKILL every client's last play queue answered 404, the next queue took id 2 again,
  and `playlistItemID` restarted at 1 beside restored items already holding 1 and 2 (found by
  `tests/test_concurrency.py` under load, 2026-09-30). The oracle pickles all three.
- **A list-member write builds its answer inside the lock too.** Cycle 32's rule, measured on play
  queues, held nowhere else in the oracle: two playlist adds released by one barrier answered
  `leafCount` 3 and 3 (9 to 15 of 30 pairs in five runs, 2026-09-30), because the add and the answer's row ran
  outside `sim.lock` -- as did the create's id and the server-wide `playlistItemID` step. Playlist
  create/add/remove/move/clear and collection create/add/remove now hold it. Not re-measured on a
  reference for lists (this task allowed no reference writes): fire barrier pairs at plexscratch.
- **A list listing is a 200 while other clients create and delete lists.** The oracle walked its live
  playlist and collection tables, so a create or delete landing inside the walk answered the listing
  with a 500 (`dictionary changed size during iteration`): 81 of 292 collection listings and 10 of
  319 playlist listings read beside four churning clients over 300 lists, and 2 in a five-minute
  32-client soak (2026-09-30). A real PMS reads its lists from SQLite; every walk now snapshots.
- **Every answered client write is on disk, whether or not its handler marked the state.** The
  native server saved a client write only when the handler had flagged the state dirty, and the
  play-queue create and add flag nothing: on a quiet server both were answered and then gone after
  an immediate SIGKILL, and under load 1 in 5 kills lost one that no other write's save had
  carried (2026-09-30). `Handler.send_response` in the oracle saves every client write.
- **A write's save must not hold the other clients.** The native server serialised, wrote and
  fsynced the whole state file under its one runtime lock, so with eight clients writing a ninth
  client's `/identity` had a median of 20-23 ms (the oracle, saving outside its request lock:
  0.5 ms), and 32 loaded clients saw it take up to 0.78 s. The snapshot is still taken under the
  lock; the file is written after it, in snapshot order, before the status line (2026-09-30).
- **`--state` keeps every kind `persist.py` keeps, not a list of the ones someone remembered.**
  Checked kind by kind (`tests/test_parity_persist.py`, 2026-10-01): the native file lost the
  section-pref overrides (with them `flattenSeasons`' row effects), custom metadata provider
  groups and their id counter, the BYTES of uploaded posters/art/themes/logos (the candidates
  survived on the row and their `/file?url=` answered 404), download queues, minted theme rows,
  the retired-key set (a split after a restart reused the key a merge had freed), the history
  counter (a restart handed out `/status/sessions/history/1` again beside the restored row
  holding it), Continue Watching's hide and the in-session-view gate of a library's own
  shelf, and re-stamped device 1's `createdAt`. Cycle 73 measured all of these kept by a real
  PMS. Over a REBUILT catalog it carried view state by ratingKey -- which a rebuild reassigns --
  and kept history; `persist.py` matches (type, guid), then (type, first part file), and leaves
  history, edits, lists and section settings in `<file>.stale`, keeping only the server-level
  settings, provider groups and the device/statistics tables.
- **A split-off row is the FIRST item on a new key** (cycle 83, plexscratch 1.43.4,
  `tests/fixtures/split_guid.json`): after `PUT /library/metadata/17/merge?ids=3` and
  `PUT .../17/split` (both empty 200s), the new row 58 equals 17 in every field but `ratingKey`,
  `key` and the artwork URLs built from them -- 17's title, its `plex://movie/...` guid, its whole
  imdb/tmdb/tvdb `Guid` list, its `Field` locks, `addedAt` and `updatedAt` -- and holds 3's Media
  unchanged, while 17 keeps its own Media and its `updatedAt` (neither call moved it); 3 stays a
  404. 1.42.2 agrees on the guid: plexscratch42's row 353 carries the first row's `local://297`.
  Both servers minted `local://<new key>`, dropped the locks and stamped the source.
- **No listing gives an UNMATCHED row a `Guid` key under `includeGuids=1`** (cycle 83, one gate
  with the `local://` bullet above; plexscratch 1.43.4 and plexscratch42 1.42.2,
  `tests/fixtures/split_guid.json`). `local://` and `tv.plex.agents.none://` alike: the grid,
  `?type=1`, `/unwatched`, `/recentlyAdded`, `/children`, `/allLeaves`, `/grandchildren`, the hub
  page, `/hubs` and home's Recently Added answer the row with no `Guid` key at all -- even a
  movie or episode whose DETAIL still answers the imdb/tmdb/tvdb list the unmatch kept (plexscratch
  movie 1 and episode 9). The detail keeps that list; an unmatched row with none (a never-matched
  `local://` episode or show, a none-agent library's movie) has no key on its detail either. Both
  servers kept an unmatched movie's stored ids in every listing, and `/hubs` and the native child
  lists gave `local://` rows a one-entry list.
- **A LEGACY Personal Media row (`com.plexapp.agents.none://`) has no `Guid` key either, and is NOT
  unmatched** (cycle 84; plexscratch42 1.42.2, `tests/fixtures/cycle84_c.json`). A movie library
  created with `agent=com.plexapp.agents.none&scanner=Plex Movie Scanner&language=xn` over the
  reference media gives every row `com.plexapp.agents.none://<sha1>?lang=xn`; under
  `includeGuids=1` the grid, `?type=1`, `/recentlyAdded`, `/unwatched`, the section search,
  `/library/all`, the hub pages, `/hubs`, home's Recently Added and the DETAIL all answer it with no
  `Guid` key, JSON and XML. But `unmatched=1` finds none of the 21 rows and `unmatched=0` all of them:
  the Guid gate takes the prefix and the `unmatched` filter does not. Both servers gave the row a
  one-entry list naming its own guid in every listing; the oracle's filter tested `"none://" in
  guid` and so counted it as unmatched (the native already tested the prefix). No client-built
  catalog row carries this guid (`build_catalog.py` mints `plex://` and `local://`); the
  Background Processing playlist does, and its `/playlists` row already had no `Guid`.
- **A route that reads its own server must carry the client's QUERY token** (cycle 83, axis 11).
  Plex for Android sends `X-Plex-Token` as the last query parameter on every image request
  (`/photo/:/transcode?url=...&width=240&height=240&minSize=1&format=jpeg&X-Plex-Token=...`, no
  `Accept`). The native transcoder resolved `url` by dispatching an inner request through its own
  router with only the outer HEADERS, so under `--token` the inner read was refused and every
  poster tile answered 404 -- the app drew titles where the posters go. `/service(s)/ultrablur/colors`
  goes through the transcoder and the Categories shelf reads its genre axis the same way. The
  oracle answered 200 throughout. No suite caught it: every check sent the token as a header.
- **An item's `languageOverride` picker is the translation table's list for the EXACT language**
  (cycle 83, zen 1.42.2, `GET /library/metadata/{id}?includePreferences=1`). Under `zh-TW` zen
  leads the list with `tr-TR:土耳其文|zh-CN:中文|zh-TW:中文（台灣）` and names Arabic
  `阿拉伯文（沙烏地阿拉伯）`; under `es-419` it names `en-GB` `Inglés (R. U.)`. The native server
  cut the language to its base and drew the agent list for `zh` (simplified, `zh`'s order) and
  `es` (`Inglés (RU)`), where the oracle took the table's own entry first. Only a language the
  table lacks falls back to the agent rows.
- **A `/query/parse` Rule's `fieldTitle` is the advertised Meta `Field` title for (scope, field)**
  (cycle 83, zen 1.42.2, `X-Plex-Language: en`), the scope being the rule's own prefix or the
  one `type=` implies: `episode.addedAt` is `Date Episode Added`, `show.studio` `Studio`,
  `show.genre` `Genre`, `show.unwatchedLeaves` `Unwatched Episodes`, `hdr` `HDR`, `dovi` `DOVI`,
  `episode.mediaSize` `File size` against the movie's `File Size`, and `season.title` (no Meta
  field) the empty string. The native server titled rules from a hand table that composed
  "<Scope> <Field>" (`Episode Date Added`, `Show Studio`, `Season Title`) and knew none of the
  media, `trash`, `location` or `network` fields; it now reads the same two Meta templates the
  oracle's FIELD_TITLES does. Plex Web's filter editor titles a rule from its own `allTypeFields`,
  not from this attribute.
- **`continuous=1` on an EPISODE queues the rest of its season, and a PUT add expands like a
  create** (cycle 83, axis 11's write transcript). Plex for Android's Play on an episode sends
  `POST /playQueues?includeChapters=1&continuous=1&uri=...` (`tests/fixtures/playqueue_continuous.json`).
  Measured on plexscratch42 (1.42.2): the queue holds the episode and the episodes that FOLLOW it
  in its own season, and stops at the season's end (S1E2 -> S1E2, S1E3; S1E3 alone); a PUT add
  honours `continuous=1` the same way (with `next=1` the run goes after the selected item), and a
  season or show uri added to a queue adds its episodes. The native server queued the one episode
  -- the app had nothing to play next -- and its PUT add put the bare season or show row, which has
  no Media, in the queue. The oracle matched the reference; the native port had dropped the flag.
- **A candidate row's ELEMENT is part of the answer.** An item's `/themes` lists `Track` rows and
  its `/clearLogos` `Photo` rows (kids, 1.43.4; `tests/fixtures/candidate_rows_kids.json`).
  python-plexapi's `themes()` and `logos()` keep only rows of their own tag, so the native
  server, whose uploaded candidates fell through to the default `Video`, read back an uploaded
  theme or logo as an empty list while the JSON was identical to the oracle's. Found by cycle 83's
  B regression `regress59b` F7 printing the element; caught by no JSON comparison.
- **A play queue's owner is the QUERY's client identifier too.** Plex Web names its client only in
  the query string, and plexscratch42 (1.42.2) retires that client's older queue on its next
  create and moves ownership on an `own=1` read by that form
  (`tests/fixtures/playqueue_query_client_s42.json`). The native server read the header alone, so
  every Plex Web queue stayed alive and an `own=1` read from Plex Web took nothing; the oracle
  reads the query first. Found by cycle 83's B restart leg listing 19 live queues after `ident60`
  where the oracle lists 16 (the three Plex Web identities' first queues).
- **`continuous=1` on one episode queues the rest of its SEASON** (plexscratch42, 1.42.2: the
  first of three episodes queues three, the second two, the last one;
  `tests/fixtures/playqueue_continuous_s42.json`). The native server ignored the flag and queued
  the episode alone, so the next episode never played; the oracle had it. Invisible to the
  cycles' XML flow diff, which compares a reference episode with a different plexsim episode and
  so always differs in item count; cycle 83 found it by running the oracle and the native server
  through the same flow.
- **A smart collection is read at its OWN level on every read**, not the way a playlist or a play
  queue reads a uri: plexscratch42 (1.42.2) answers python-plexapi's smart show collection over
  `all?type=2&episode.unwatched=1&limit=3` with its two SHOWS (`tests/fixtures/plexapi_writes_s42.json`).
  The oracle resolved it through the play-queue resolver and listed every episode; the cycle's
  write-chain diff keyed collections by ratingKey and so printed it as a REF-ONLY/SIM-ONLY pair
  for cycles without anyone reading the counts.
- **A section uri's own `type=` picks the rows a smart playlist holds.** python-plexapi's smart
  episode playlist over a show library's `all?type=4&episode.unwatched=1&limit=3` holds three
  EPISODES on plexscratch42 (1.42.2) and on the oracle (`typed_rows`). The native server read the
  section's own rows (the one show), capped that at three and expanded it, so the playlist held
  every episode of the library.
- **A playlist's `deleted` frame carries its title; its `created` frame an empty one**
  (plexscratch42, 1.42.2, python-plexapi's `createPlaylist` and `delete`). The native server sent
  an empty title on both; a client that labels the removal from the frame had nothing to show.
- **Adding a show to a play queue adds its episodes.** python-plexapi's `addItem(show)` grows a
  queue by the show's three episodes on plexscratch42 (1.42.2), as a create expands one; the
  native server added the show row itself, which has no Media to play.
- **A decision's streams are NOT localised.** zen (1.42.2) answers Plex Web's MDE decision for a
  movie with a French track selected as `Français (DTS-HD MA 5.1)` under `X-Plex-Language=en` and
  its English subtitle as `English` under `fr`, while the same item's read says `French` and
  `Anglais` (`tests/fixtures/decision_stream_language_zen.json`). The native server kept the stored
  names; the oracle ran the item read's stream localiser over the decision too.
- **`/system/agents` pages its XML like its JSON** (cycle 83, axis 2). zen (1.42.2) answers
  `GET /system/agents?mediaType=1` with request headers `X-Plex-Container-Start: 1` and
  `X-Plex-Container-Size: 2` and no `Accept` -- python-plexapi's `agents()` shape -- with
  `size="2" totalSize="5" offset="1"`, the second and third agents and both `X-Plex-Container-*`
  response headers. The native server wrote this XML itself, past the pipeline's pager, and sent
  all five agents with no `totalSize`, so plexapi's `agents().totalSize` and `.offset` were None.
  The JSON and the oracle were right. Any handler that renders its own XML skips the pager.
- **Only a READ with `own=1` takes a play queue** (cycle 83, axis 12, P's kill matrix).
  plexscratch42 (1.42.2): a queue made by client A and read by client B with `own=1` steps
  1 -> 2 and B owns it; A's own `PUT .../items/{id}/move?after=...&own=1` steps it once (3) and
  B still owns it -- B's next read stays at 3, and A's next new queue leaves B's queue alive
  (200). A `HEAD` with `own=1` takes like the GET (2). The native server took on every method,
  so A's move stepped 2 -> 4, B's read re-took it (5), and A's next queue retired the queue B
  was playing (404); the oracle routed HEAD to the write router and never took on it.
- **A play-queue ADD expands a show or a season like the create** (cycle 83, found confirming
  the take rule). plexscratch42 (1.42.2): a one-movie queue plus `PUT /playQueues/{id}?uri=<show>
  &next=0&own=1` is seven rows, the movie and the show's six episodes; a season with `next=1`
  adds its three. The native server resolved the add's uri without the create's leaf expansion
  and queued the show (or season) row itself, which has no Media -- Play Next and Add to Up Next
  on a show handed the player a row it cannot play. The oracle's `_playable` was right.
- **A playlist's delete frame carries its title** (cycle 83, axis 3, C's `pq.py`). plexscratch42
  (1.42.2): `POST /playlists` pushes type 15, sectionID "-1", title "", state 0 `created`; the
  `DELETE` pushes state 9 `deleted` with the list's TITLE. The native server sent the delete's
  title empty and wrote both frames' fields alphabetically (a `json!` literal keeps its written
  order); every other frame leads identifier, sectionID, itemID, type, title, state. The
  trailing `metadataState`/`updatedAt` pair stays the recorded section-8 order bound.
- **A playlist edited through the ITEM path stores what the list path stores** (cycle 83,
  axis 3, C's `pq.py`). plexscratch42 (1.42.2): `PUT /library/metadata/{playlist}?summary=...
  &summary.locked=1` puts `Field [summary]` on `/playlists/{id}` and on `/library/metadata/{id}`,
  and `titleSort`, `year` and `studio` sent this way keep their values and locks exactly as
  through `PUT /playlists/{id}`. The oracle ran this path's grammar on a copy and kept none of
  it; the native server kept title, summary and sort title and dropped `year` and `studio`.
  (The `/playlists` LISTING row carries no `Field` on the reference and does on both servers:
  the recorded section-8 bound, unchanged.)
- **A decision lists the streams that PLAY, not the file's** (cycle 84, the item cycle 83 left
  measured-not-fixed). Plex Web's exact MDE request (`tests/fixtures/decision_streams_selected.json`)
  on zen (1.42.2): movie 1098 -- two video streams, a selected French and an unselected Spanish
  DTS track, a selected English and four other PGS subtitles -- answers three Streams, the FIRST
  video, the selected audio and the selected subtitle; srt rows 2925 (31 subtitles) and 3069 the
  same. On plexscratch42 (1.42.2) row 272, whose two srt subtitles are both unselected, the
  decision lists video and audio only under `subtitles=none|sidecar|burn|auto`. The selected
  subtitle carries `decision: ignore` and no `location` under `subtitles=none`, and
  `location: sidecar-subs` under `sidecar`. Both servers listed every stream of the file with
  `location: direct`. Measured and NOT acted on: with a subtitle selected, `burn`, `auto`, an
  empty or an absent `subtitles` is a 400 on zen for Plex Web's and Plex for Android's shapes
  alike -- so the "transcoder-less copy" reason recorded for the plexscratch42 400 was not the
  reason; plexsim, which cannot burn, still answers them. Plex Web reads only the decision's
  code pairs (it keeps the body as `decisionXML`, unread), so the stream list is invisible to it.
- **A merge leaves the survivor's `updatedAt` alone** (cycle 84, `tests/fixtures/merge_updated_at_s42.json`).
  plexscratch42 (1.42.2), two rows of a throwaway section: after `PUT /library/metadata/768/merge?ids=769`
  row 768 keeps `updatedAt` and its `thumb` version, read at once and 100 s later, and the split
  that follows puts the new row 770 on the same time; plexscratch (1.43.4) agreed in cycle 83. Both
  servers stamped the merge time on the survivor (and so on the split-off row). Both scratches
  are isolated; a networked PMS runs an agent refresh after a merge that may restamp it --
  plexsim runs no agent. The first merge attempt was a 500 ("Waited over 10 seconds for a busy
  database") that moved nothing: a scratch 500 under another agent's load is not a rule.
- **A media item's DELETE frames the row as it WAS** (cycle 84, B's drive59 `movie.delete()` and
  `episode.delete()`, `tests/test_parity_cycle84_b.py`). The one `state 9` TimelineEntry carries
  the row's own type number, its `sectionID` and `metadataState: "deleted"`, as every measured
  state-9 frame does (playlist, collection, merged row, library delete), and the section's
  `/timeline` lists the removal as a state-9 stub. The native server looked the row up after
  removing it and sent `type 1`, `sectionID ""` and no `metadataState` for every kind of row;
  Plex Web's timeline handler repopulates `/library/sections/{sectionID}` on a state-9 frame
  only for a positive id. The frame's title for a media row is unmeasured: a media-item DELETE
  is a 400 on both copies (read-only media); both servers send the row's frame title.
- **A deleted row leaves the play queues at DELETE time** (cycle 84, B's rlwrite76 restart leg:
  the shuffled show queue read at version 1 on the native server, 2 on the oracle). The recorded
  rule (docs/findings.md, "A removed row leaves the play queues") is that the queue shrinks, its
  version steps and the selection moves to a row that still exists. The native server dropped the
  row only while rendering the queue, so the version never stepped -- the response the client
  discards -- and a deleted selected item stayed `playQueueSelectedItemID`. (Cascades, merges, the
  per-row version and where the selection goes were measured next; see the bullet below.)
- **Every row a write removes leaves the play queues, the version steps PER ROW, and a removed
  selection goes to the FOLLOWING row or nowhere** (cycle 84 F, plexscratch42 1.42.2, a throwaway
  TV library; `tests/fixtures/queue_removal_s42.jsonl`). A season's `DELETE` takes its episodes
  out of every queue, and an episode or show `PUT .../merge` takes out the rows it absorbs (the
  survivor's own queue is untouched). The version steps once per row that leaves -- a season of
  three moves a queue 1 -> 4 -- not once per write. A selected row that leaves hands the
  selection to the next SURVIVING row after it, not to the queue's head; with no row after it
  the queue has no selection at all: `size 0`, no rows, no `playQueueSelectedItemID` or
  `...MetadataItemID`, `playQueueSelectedItemOffset` 0 and `playQueueTotalCount` still counting
  the rows (a `center=` naming a row lists it again). The oracle dropped only the DELETE's own
  key and nothing on `/merge`; both servers stepped once per write and selected the head.
  CLIENT-VISIBLE: Plex Web refetches its queue on the version and discards an answer whose
  selected id it does not hold.
- **A Fix Match that MERGES is a removal too.** Matching a row onto a guid another row of the
  library already holds merges the two (recorded: the matched row is deleted and the survivor
  keeps both Media), so the matched row leaves every play queue by the rule above. The native
  server retired it and left it queued -- version unmoved, selection naming a 404 -- and the
  oracle dropped the matched key even when the merge absorbed nothing (a survivor with no Media
  of its own), taking a row that still exists out of the queue.
- **Every request header loses ONE optional leading space, not all of them** (cycle 84 F, raw
  sockets on kids 1.43.4 and plexscratch42 1.42.2, identical; `tests/fixtures/header_ows_roku.json`).
  Cycle 50 measured the rule for `Content-Length` and `X-Plex-Token`; it holds for the identity
  headers too: `X-Plex-Platform:  Roku` (two or three spaces) is ` Roku`, not Roku, and keeps the
  library pivots on `/media/providers` that `X-Plex-Platform: Roku` and `X-Plex-Platform:Roku`
  lose; `X-Plex-Version:  9.7.1` carries a leading space, does not parse, and strips them; a
  trailing space is kept (`Roku ` is not Roku either). The oracle applied the rule only through
  `_header_value` and left every other reader on Python's parser, which strips all leading
  whitespace; it now rewrites the parsed values off the raw lines. MALFORMED-ONLY: no client
  sends a doubled separator.
- **`window` and `center` never move a queue's reported selection** (cycle 85 CP, plexscratch42
  1.42.2 and plexscratch 1.43.4, identical; `tests/fixtures/queue_section_delete_s42.jsonl`,
  steps `*-F`). A four-row queue with the selection moved to index 2 answers
  `playQueueSelectedItemOffset 2` and the selected row's `playQueueSelectedMetadataItemID` to
  `window=1` (three rows listed), `center=<last>` (all four) and `center=<last>&window=1` (two):
  the parameters pick the rows, the offset stays the selection's index in the WHOLE queue. Both
  servers rebased it into the slice (1, 3, 1); the oracle also read the metadata id at the rebased
  position. CLIENT-VISIBLE: python-plexapi's `PlayQueue.get` reads with `window=50`, so a queue
  whose selection sits past row 50 reported another offset.
- **`DELETE /library/sections/{id}` leaves the section's rows QUEUED** (cycle 85 CP, plexscratch42
  1.42.2, two throwaway TV libraries; `tests/fixtures/queue_section_delete_s42.jsonl`). Unlike an
  item, season or merge removal (the bullets above), a library delete does not touch the play
  queues: the version does not step and the selection stays put, read immediately or a minute
  later. A read lists only the rows whose item still exists, but `playQueueTotalCount` counts every
  row, `playQueueSelectedItemOffset` is the selection's index among all of them (a selected movie
  behind two dead episodes reads offset 2, rows `[movie, movie]`), and
  `playQueueSelectedMetadataItemID` is ABSENT once the selected row's item is gone while
  `playQueueSelectedItemID` still names it. A queue with no live row answers `size 0` with its
  total, selected id and offset intact. `window=` slices the whole queue around the selection and
  only then drops the dead rows (`window=1` around a dead head lists nothing). Both servers counted
  and indexed only the live rows, kept the metadata id, and dropped total, selection and offset
  from a queue with no live row. CLIENT-VISIBLE: Plex Web's queue model sizes itself as
  `max(playQueueTotalCount, rows)` and skips its confirm-playback path when
  `playQueueSelectedMetadataItemID` equals the item asked for.
- **A TAB anywhere after a header's colon is a 400, leading ones included** (cycle 85 B, raw
  sockets on kids 1.43.4 and plexscratch42 1.42.2, identical; `tests/fixtures/header_tab_roku.json`).
  `X-Plex-Platform:\tRoku`, `: \tRoku`, `:\t Roku`, `:  \tRoku`, `Ro\tku`, `Roku\t`,
  `X-Plex-Version:\t9.7.1` and an unknown header's `:\tvalue` are each a plain 400. Cycle 16's
  TAB check read Python's parsed values, which had lost their leading whitespace, so after
  cycle 84's one-space rewrite the oracle still answered a leading TAB with a 200 (B's
  `regress64b_ows`, 5/7); it now reads the raw lines. The native server was already right.
  MALFORMED-ONLY: no client sends a TAB in a header.
- **A show cascade's per-season activity progress rounds half to EVEN** (cycle 86 C+P,
  plexscratch 1.43.4; `tests/fixtures/show_cascade_progress_s43.json`). Two-season Firefly's
  `showOrdering=dvd` write and its undo both step `Updating Metadata` -1 -> 44 -> 44 -> 89 -> 100:
  one season of two, 44.5, is 44. The same show's library scan steps 49.5 to **50**, so neither
  truncation (49) nor half-up (45) explains both; half-to-even does. The oracle's `round` was
  right; the native server's `f64::round` said 45. MALFORMED-ONLY: one transient activity frame.
  The cascade's frame ORDER on 1.43.4 (per-season `updated` at the season's end, no trailing 49)
  stays recorded-not-implemented (findings, "The show cascade's frame ORDER on 1.43.4").
- **The native port 404'd the per-id provider GETs the oracle has carried since cycle 44.**
  Re-measured on zen and kids for Plex Web 4.162.1 (2026-10-01): `GET /media/providers/metadata/{id}`
  and `/media/providers/metadata/group/{id}` are the listing's row as a one-row container, paged
  like the listing (`totalSize=1 offset=0` under paging headers), 400 for a non-numeric id, 404
  for an unknown one. MALFORMED-ONLY: 4.162.1 only PUTs and DELETEs those paths (and only for
  non-built-in providers); it GETs the two listings, the provider one with `metadataTypes=`.
  PUT/DELETE/POST on a built-in id still differ between the servers (the oracle answers the row)
  and are unmeasured writes.
- **A legacy agent row's `hasPrefs` and `primary` are JSON BOOLEANS, and a bare-form `MediaType`'s
  `mediaType` a NUMBER** (cycle 87 W2, kids 1.43.4 and zen 1.42.2, the bare path and every
  `mediaType=` from 1 to 22 under Plex Web 4.162.1's headers; `tests/fixtures/c87_w2_system_agents_zen.json`).
  `legacy_agents.json` was read out of the XML, so both servers sent `"hasPrefs":"1"`,
  `"primary":"0"`, `"mediaType":"8"`. The XML is unchanged (`1`/`0`). MALFORMED-ONLY: Plex Web's
  `AgentModel` reads `hasPrefs` through `parseBoolean` (`1===Number(e)||"true"===e`), which maps
  `"1"` and `true` alike, and does not read `primary` at all; python-plexapi reads the XML.
- **A PAGED `/system/agents` JSON still repeats every `Language` (and bare-form `MediaType`) key**
  (cycle 87 W2, zen 1.42.2 under Plex Web 4.162.1's `X-Plex-Container-Start=0`/`Size=50`, and the
  `Start=1 Size=2` window; `tests/fixtures/c87_w2_system_agents_zen.json`). The native route wrote
  its repeated keys and then went through the pipeline's pager, which parsed the body -- keeping
  the LAST of each repeated key -- and wrote one `Language` per agent back (704 bytes for
  `mediaType=1` against 6,394). It now pages the array form first, as its XML branch already did.
  The oracle was right. MALFORMED-ONLY: `JSON.parse` keeps the last duplicate at the first one's
  position, so Plex Web's parsed object was the same either way; only the wire bytes and length differed.
- **`/system/agents?mediaType=9` and `=10` list Plex Music and Plex Personal Media; `14`, `20`, `21`
  and `22` list Personal Media alone** (cycle 87 W2, kids 1.43.4 and zen 1.42.2 swept over
  `mediaType=1..129`; every other type outside 1-4, 8, 13 is `size 0`; `tests/fixtures/c87_w2_system_agents_zen.json`).
  `system_agents.json`'s `mediaTypes` held only 1, 2, 3, 4, 8 and 13, so both servers answered
  `mediaType=9` with the two legacy rows alone and 10 with nothing, and the provider table's
  `MetadataType` lists (music `8,9,10`, Personal Media `1,2,3,4,8,9,10,13,14,20,21,22` on zen) came
  out short with them. CLIENT-VISIBLE: Plex Web 4.162.1's Agents settings page
  (`chunk-4030`, `populateAgents`) asks for types 1, 2, 8, 9 and 13 and drops only the movie,
  series and music modern agents, so its Albums tab lists Plex Personal Media on a real server and
  did not on plexsim. Personal Media's provider id is 5 on zen, not 4: not settled here.
- **A LEGACY `/system/agents` row is `hasPrefs, identifier, primary` (then `name`), not the
  modern row's `identifier, name, hasPrefs, id`.** Measured on zen (1.42.2, 2026-10-01) in JSON
  and XML over the bare path and `mediaType=1,2,8,9`; kids (1.43.4) agrees after the `id` it
  alone leads legacy rows with. The bucket is told apart by `primary`, which no modern row
  carries. An empty legacy row (fanarttv, htbackdrops, ...) is a PAIRED `<Agent ...>\n</Agent>`
  on both references. plexsim used the modern order for all eleven and self-closed the six
  empty ones. After the fix every typed body is byte-identical to zen's
  (`tests/test_followups_c87_w3.py`). MALFORMED-ONLY: Plex Web reads the rows by key.
- **The metadata PROVIDER table numbers Personal Media 5, not 4; `/system/agents` numbers it 4.**
  Measured 2026-10-01 on kids (1.43.4), zen (1.42.2), plexscratch and plexscratch42 -- the last a
  FRESH 1.42.2 install -- so it is a rule, not one database's row history: providers are movie 1,
  series 2, music 3, Personal Media 5 (kids adds localmedia 6, NFO 7/8); provider **4** is the
  `org.musicbrainz.agents.music` `intermediary` (`MetadataType` 8, 9, 10), which no listing (nor
  `?metadataTypes=8`) shows and `GET /media/providers/metadata/4` answers. Built-in GROUPS are
  numbered 1-4 in agent order, each item carrying the group's id and naming the provider by the
  provider's id: group 4, item 4, `metadataAgentProviderId` 5. plexsim numbered every provider by
  agent position, so `/4` answered Personal Media and `/5` was a 404. The MusicBrainz row's
  `uri`/`online` are zen's (`provider://...`, `true`); kids and the fresh copy say `""`/`false`
  there and on Plex Music alike -- reachability state, not shape. MALFORMED-ONLY: Plex Web
  4.162.1 never GETs a provider by id; a provider id reaches it only as the Add/Edit Agent dialog's
  `additionalProvider`, sent as `PUT /media/providers/metadata/group/{custom}/items/{providerId}`,
  which plexsim answers by the group rather than the id (cycle 40).
- **A timeline beat is for the item its `playQueueItemID` names, its crossing reads the SESSION's
  last position, and `complete` lasts only while the beats name the same item by the same id**
  (cycle 87 e1, plexscratch42 1.42.2, 30 beat sequences in Plex Web's shape and python-plexapi's
  `updateTimeline`; `tests/fixtures/timeline_queue_item_s42.json`). `ratingKey=A` with B's queue
  row id at 92% completes and stores B, not A (3/3); an id no queue holds falls back to
  `ratingKey`. The previous position is the session's last beat whatever its item: 3 000 on A then
  92% on B completes B (python-plexapi too, no queue at all), 3 000 on B then 92% on A completes
  A, 92% on B then a stop at 93% on A completes nothing, and 92% on A, 50% on B, 93% on A counts A
  twice. A completion answers `complete` to later beats past 90% only while each carries the same
  item AND the same raw `playQueueItemID` (absent and empty alike): a stop with no id, a different
  id, or after any other item's beat answers `ignore` with `viewCount` unchanged. Cycle 86's "the
  stop answers `ignore`, viewCount and history agree" missed that the view landed on B. Both
  servers kept the position and the completion per (session, item) and applied the beat to
  `ratingKey`. The id half is MALFORMED-ONLY (Plex Web always pairs the id with its own item);
  the session-wide crossing is CLIENT-VISIBLE: python-plexapi's `Video.updateTimeline` sends no
  session identifier, so one script setting a resume point on one item and then a position past
  90% on another marks the second watched on the reference and did not on ours.
- **Removing a queue's selected LAST row leaves it with no selection, and a selection-less queue
  reports offset 0 to every read** (cycle 87 e1, plexscratch42 1.42.2,
  `tests/fixtures/queue_lost_selection_s42.json`). `DELETE /playQueues/{id}/items/{itemID}` on
  the selected row moves the selection to the row AFTER it, or with none after it to no selection
  at all -- cycle 84's library-removal rule, which the queue-item DELETE did not follow: both
  servers selected the row before it. The DELETE's own answer for a selected row reports
  `playQueueSelectedItemOffset` 0 and no `playQueueSelectedMetadataItemID` whether the selection
  moved on or was lost (both servers reported the new selection's). On a queue with no selection,
  python-plexapi's `PlayQueue.get` (`window=50&includeBefore=1&includeAfter=1`) lists nothing, and
  with `center=` naming a live row lists the rows around it with `window`/`includeBefore`/
  `includeAfter` as usual -- offset still 0, no selected ids. Both servers reported the centre's
  index in the slice. CLIENT-VISIBLE: python-plexapi's `PlayQueue.removeItem` on the playing last
  item, then `PlayQueue.get`/`refresh`, read a selection the reference does not have.
- **`window=0` lists no rows, and neither does a `center=` that names no row of the queue** (cycle
  87 e1, plexscratch42 1.42.2, `tests/fixtures/queue_lost_selection_s42.json`). Around the
  selection or a centre alike, `window=0` answers `size 0` with the selection and counts as ever
  -- not the centre row alone, which both servers listed. A `center=` of a removed row or an id the
  queue never held answers `size 0` too; both servers fell back to the selection and listed its
  window. MALFORMED-ONLY: python-plexapi always sends `window=50` and a centre it took from the
  queue it holds.
- **A write on a BUILT-IN metadata provider or group id is not the row and not a 404.** Measured
  on plexscratch42 (fresh 1.42.2, 2026-10-01; every listing and row re-read before and after,
  nothing changed): `PUT /media/providers/metadata/1` is a 400 with no or an empty `uri` and a
  500 with an unreachable one (PMS fetches the provider; plexsim hosts none, so every `uri` is a
  500 here), and `POST` on the id is a 404. `PUT /media/providers/metadata/group/3?title=` with
  its own title answers the group row, `POST /group/3` is a 404 and `PUT /group/3/items/5?after=-1`
  a 400. The oracle answered the provider row to every verb; the native server 404'd them all,
  groups included. MALFORMED-ONLY: Plex Web 4.162.1 hides Edit/Remove for a provider whose title is
  one of its eight built-in names and for a group with `builtIn`. Unmeasured, left as they were:
  `DELETE` on a built-in provider or group id (destructive; the oracle still answers the provider
  row, native 404s). A built-in group renamed to a DIFFERENT title was measured next (below).
- **A built-in metadata provider GROUP's rename is real.** plexscratch42 (1.42.2, 2026-10-01):
  `PUT /media/providers/metadata/group/3?title=Plex%20Music%20W3` answers group 3 with the new
  title, the group listing and `GET /group/3` keep it, the provider row (`/media/providers/metadata/3`)
  keeps "Plex Music", and `?title=Plex%20Music` restores it (the undo, made). Both servers
  answered the row as held; they now keep the title, and `--state` keeps it over a restart.
  MALFORMED-ONLY: Plex Web hides Edit on a `builtIn` group.
  was implemented in cycle 87 (below).
- **A show cascade's activity steps a season at its END** (cycle 87 E2, plexscratch 1.43.4,
  two-season Firefly; `tests/fixtures/show_cascade_order_s43.json`, seven captures of a
  `showOrdering` write or undo, all the same 40 frames). The show's `queued` frame comes BEFORE
  `activity started`; each season OPENS with an `updated` carrying its subtitle and the PREVIOUS
  progress (`S01 -1`, `S02 44`) and its own step lands between the season's last episode's second
  `loading` and that episode's bare 5; the last season's `89` is followed at once by `ended 100`,
  ahead of the last episode's, the season's and the show's 5s. Both servers stepped each season at
  its start (`S01 44`, `S02 89`), started the activity before `queued` and closed with an invented
  `updated 49`. No media pass ran on the warm copy (state-dependent, as cycle 15 recorded); ours keeps
  its place after each season's episodes. The SEASON cascade's own `started`-before-`queued` was
  not re-measured. MALFORMED-ONLY: transient activity frames.
- **Section `search?type=18` searches the library's COLLECTIONS** (cycle 87 E2, plexscratch 1.43.4
  and plexscratch42 1.42.2; `tests/fixtures/section_search_collections.json`). Empty and populated
  collections alike, by the in-library box's title substring (`mpty8` finds `E2Empty87`, `c52` the
  two `C52A...`), case-blind, scoped to the section, in the search container with `viewGroup`
  `collection` and NO `viewMode` (a show library keeps `nocache`); each row is that collection's
  `/all?type=18` row. Cycle 83 read it as "finds an EMPTY collection"; both servers found NO
  collection there at all, answering from the item rows. Measured beside it on kids and both
  copies: the box uses the query VERBATIM -- `query=TeArS` is titled `Search for 'TeArS'`, and
  `query=%20TEARS%20` is titled with its spaces and finds nothing -- where both servers trimmed and
  lowercased it. No official client or python-plexapi request in the corpora sends `type=18` here
  (plexapi's `section.collection()` reads `/all?type=18&title=`; Plex Web reads `/library/search`
  and `/hubs/search`; `/library/sections/{id}` advertises only `search?type=1|2|4`): MALFORMED-ONLY.
- **A row delete and a merge are list writes too** (cycle 87 E2, closing cycle 83's "the oracle's
  other unlocked list writes"). Both rewrite every playlist's and collection's item list -- read,
  filter, assign -- and the oracle ran them outside `sim.lock`, so a playlist CLEAR (or an item
  remove) landing between the read and the assign was undone: a cleared 40,000-item playlist came
  back whole in 5, 9 and 10 of 300 barrier pairs (`tests/test_followups_c87_e2.py`, a generated
  catalog with two whole-library playlists swept ahead of the target and the clear sent 0-1 ms
  behind the delete; the window is one list comprehension, which a smaller catalog never opens).
  The DELETE and both merge paths now hold the lock; the native server serialises them already and
  answered 0 of 300 before and after. No reference measurement applies: a real PMS keeps its lists
  in SQLite. MALFORMED-ONLY in practice: it needs a delete and a list write inside the same
  milliseconds.
- **A timeline's `progress` answer belongs to the session's current PLAY and outlives a stop**
  (cycle 87 e3, plexscratch42 1.42.2, 42 beat sequences, each in Plex Web 4.160's shape and
  python-plexapi's `updateTimeline`; `tests/fixtures/timeline_play_latch_s42.json`). The play is
  e1's latch tag -- (raw `playQueueItemID`, the item it names), per session. 70 000 on A, a stop at
  75 000, then 3 000 on A answers `progress` and deletes the resume point (65 s and 200 s later
  too); 70 000 on A then 3 000 on B answers `ignore` with no stop between, and a later 3 000 on A
  stays `ignore` and KEEPS A's 70 000; switching the id on the same item resets it as well. A
  stop hands the next play its own position: a stop past the minute (the 93% stop after a
  completion included) keeps the next play at `progress`, a stop at or under it does not. A
  `stopped` beat never deletes the resume point (70 000 then a stop at 3 000 answers `progress`
  and leaves 70 000), where `paused` and `buffering` do. Both servers kept `progress` on the Now
  Playing entry, dropped it at every stop and carried it across items, and deleted on every
  sub-minute beat of an item the session had ever taken past a minute. The answer itself is
  MALFORMED-ONLY (python-plexapi discards the body); the resume point is CLIENT-VISIBLE:
  `video.updateTimeline(70000, 'playing')` then `video.updateTimeline(3000)` (default
  `state='stopped'`) leaves `viewOffset` 70 000 and the item in Continue Watching on the
  reference, and deleted it on ours; so did 70 000 on A, 3 000 on B, 3 000 on A.
- **A playlist's own beats (`Playlist.updateTimeline`) are a separate track** (cycle 87 e3, same
  fixture). 3 000 on A then 92% on the list answers `ignore` and completes nothing, so does 3 000
  on the list then 92% on A; 3 000 on the list, 3 000 on A, 92% on the list completes the list,
  and 70 000 on A, a beat and a stop on the list, then 3 000 on A is still `progress` on A. A
  list's completion steps its `viewCount` and writes NO history row; the oracle wrote one. A
  queue BUILT from a playlist (`playlistID=`) follows the item rules unchanged. Both servers
  crossed between the tracks. CLIENT-VISIBLE: `Playlist.updateTimeline` and `Video.updateTimeline`
  share python-plexapi's client identifier, which is the session, so `video.updateTimeline(3000,
  'playing')` then `playlist.updateTimeline(<92%>)` stepped the list's `viewCount` on ours and not
  on the reference, and the reverse order marked the video played on ours only.
- **A beat with NO `playQueueItemID` moves its CLIENT's queue; an EMPTY one does not** (cycle 87
  e3, same fixture, `queue_selection`). From the client that owns the queue (one queue per client,
  cycle 76), a beat without the parameter selects the queue's FIRST row of the beat's `ratingKey`
  -- in every state, with or without `time`, over a playlist-built queue too -- and leaves the
  selection alone when no row holds the item; `playQueueVersion` does not step. Another client's
  beat moves nothing, and `playQueueItemID=` empty, `0`, `abc` or an id no queue holds moves
  nothing from anyone (all 200). For the play latch and the item a beat names, empty and absent
  stay alike. Both servers moved a selection only by id. CLIENT-VISIBLE: python-plexapi's
  `PlayQueue.create` and `Playable.updateTimeline` send the same client identifier and the latter
  never sends `playQueueItemID`, so `updateTimeline` on the queue's second item moves
  `PlayQueue.refresh()`'s `selectedItem` on the reference.
- **A SEASON cascade's `queued` frame comes before `activity started`, and its activity ends ahead
  of the last episode's 5** (cycle 87 E4, plexscratch 1.43.4, three `PUT .../refresh` on Firefly's
  two three-episode seasons, frame-identical; `tests/fixtures/season_cascade_order_s43.json`). The
  order is season queued, `started -1`, season processing and loading, each episode's four with the
  LAST episode's bare 5 held back, `ended 100`, that 5, the season's 5 -- the show cascade's end one
  level down. Cycle 15 wrote queued first; both servers started the activity first and ended it after
  every episode. The copy's seasons and episodes are `local://` and every refresh of one opened with a
  `state 2 matching` / `state 5` pair, where the same refresh of its `plex://` show (39 frames, beside
  them) did not, and an EPISODE refresh there cascaded through the whole show: the unmatched row's
  match attempt, not the season cascade's, so a catalog season (matched) keeps the 18. A season
  UNMATCH runs the same cascade and was not re-measured for the order. MALFORMED-ONLY: transient
  activity frames.
- **`/all?type=18&title=` was already right** (cycle 87 E4, re-measuring E2's "plexscratch42 answers
  `title=multi` with 0 rows where ours find substrings"; `tests/fixtures/collection_title_filter.json`).
  plexscratch (three probe collections, made and deleted) and plexscratch42 (its standing three, read
  only) answer the listing's own grammar on collections, empty ones included: `title=` is the word
  prefix with the fold (`multi` finds `E4 Alpha multi-Beta` and not `C52Amulti`; `multi-Beta`,
  `the.dot`, `(Saga` and `E4E` begin a word, `Beta`, `dot`, `Saga`, `lpha` and `emile` do not; `zoe`
  finds `Zoë`; a trailing or doubled space finds nothing; `title=a,b` is an OR), `!=` an unfolded
  substring (`!=multi` drops `C52Amulti`, `!=ZOE` keeps `Zoë`), `<=`/`>=` begin/end, `==` finds nothing
  (on the movie grid too: `title==Sintel` is 0 rows) and `<>`, `>>`, `<<` are ignored. Both servers
  answered all 57 exchanges as the references did, before any change; nothing was fixed. The check
  stays as the record. CLIENT-VISIBLE path (python-plexapi `LibrarySection.collections(title=)`,
  `library.py:1271-1275`/`1666-1670`; `collection(title)` adds a client-side `title__iexact`), no
  defect.
- **A merge takes the absorbed row's playlist ENTRY with it** (cycle 87 E4, plexscratch 1.43.4;
  `tests/fixtures/merge_playlist_s43.json`). A list of movies 18, 3, 17 (playlistItemIDs 23, 24,
  25), then `PUT /library/metadata/17/merge?ids=3` (plexapi's `merge()`, `mixins/split_merge.py:19`):
  the list answers 18, 17 with `leafCount` 2, and id 24 is gone -- `PUT .../items/25/move?after=24`
  moves 25 to the FRONT (an unknown `after`), `DELETE .../items/24` and `PUT .../items/24/move` are
  404s, and the `split` that follows leaves the list at two. No playlist frame is pushed. Both
  servers kept the absorbed entry in the stored list and hid it only at render, so the stale id
  still held a slot: the move landed after it and the DELETE answered 200. Beside it, a collection
  holding the absorbed row lists one child after the merge while its `childCount` on
  `/all?type=18` and `/library/collections/{id}` stayed 2 through two reads and the split -- recorded,
  not emulated (one witness). CLIENT-VISIBLE for a client holding the list from before the merge:
  python-plexapi's `Playlist.moveItem(item, after=)` and `removeItems()` send the playlistItemIDs of
  the `items()` it cached (`playlist.py:131-136`, `289-317`).
- **A BUILT-IN metadata provider cannot be deleted; a built-in GROUP can, and its agent goes with it.**
  Measured on plexscratch42 (1.42.2, 2026-10-01, destructive, restored from a snapshot after;
  `tests/fixtures/c87_w3_builtin_deletes_s42.json`). `DELETE /media/providers/metadata/3` is a 400
  that changes nothing, before and after its group is deleted. `DELETE /media/providers/metadata/
  group/3` is an empty 200 even with a music library naming group 3: the group is gone from the
  listing and from `/group/3` (404, a second DELETE too), and `/system/agents` drops
  `tv.plex.agents.music` from the bare list and from `mediaType=8`/`9`. The provider row and the
  library naming the group are untouched. Add Library then refuses the agent:
  `metadataAgentProviderGroupId=3` (what Plex Web 4.162.1 sends -- the agent's `/system/agents` id)
  is a 400 `unable to find entry for provided 'metadataAgentProviderGroupId'\r\n`, and no such
  parameter a 400 `unable to find built-in agent group for provided 'agent'\r\n`, both plain text
  with no Content-Type. Before the deletes the same music create was a 201 echoing
  `agent: tv.plex.agents.music` and `metadataAgentProviderGroupId: 3`. plexsim answered both
  DELETEs with the provider row (oracle) or a 404 (native) and ignored the parameter. It still
  writes its create 400s as the generic HTML page, and refuses `type=artist` outright (no music
  libraries) -- both older gaps, closed by the next bullet. MALFORMED-ONLY: Plex Web hides Remove on a built-in provider and
  group.
- **Add Library refuses in TWO stages, and the second lists EVERY failure in one untyped 400.**
  Swept on plexscratch42 (1.42.2, 2026-10-01) over 135 malformed `POST /library/sections` forms
  and re-run on plexscratch (1.43.4), byte-identical on 134
  (`tests/fixtures/c87_w4_add_library_refusals_s42.json`). An ABSENT `name`, `type`, `agent` or
  `language`, or a non-integer `metadataAgentProviderGroupId`, is the generic 89-byte HTML 400
  before anything else is read. Past that each failing check appends one CRLF-terminated line, in
  a fixed order -- name, type, scanner, group lookup, agent, language, scanner pairing, location
  -- and the list is ONE 400 with NO Content-Type: `type=bogus` with the movie agent is two lines
  (`'type' is missing or invalid (...)` and `'agent' is invalid for the selected library type`).
  Only a `tv.plex.agents.*` agent is looked up in the groups (a legacy agent ignores the client's
  `-1`); a group that exists but names another agent is `agent identifier does not match primary
  identifier of supplied metadata agent provider group` -- which is why `group=5` differs between
  the versions (1.43.4 holds `tv.plex.agents.nfo.movie` there). A legacy agent's languages are
  its own for that type (`en` passes `com.plexapp.agents.imdb` on a movie library), `xn` passes
  every modern agent, and the pairing check wants a new scanner (`Plex Movie`/`Plex Video Files`,
  `Plex TV Series`, `Plex Music`) exactly when the agent is a valid modern one; a photo library
  is never paired. The untyped body is still gzipped on offer (134 to 121 bytes for `type=bogus`)
  and writes `Connection`/`Keep-Alive` before `Content-Length` with or without the encoding trio.
  A valid `type=artist` (or `type=8`) form with `tv.plex.agents.music`, `Plex Music` and group 3
  is a 201 echoing all three. plexsim answered the HTML page to every refusal, stopped at the first
  failure, refused both legacy forms Plex Web's dialog sends, and (oracle) refused music libraries.
  MALFORMED-ONLY for the refusal shapes (Plex Web builds the form from `/system/agents` and
  `/library/sections/prefs`); CLIENT-VISIBLE for the legacy-agent and music creates the dialog offers.
- **Near the end, a play that has passed the minute answers `progress`; a CLOSED completion
  answers `ignore`; an offline sync ignores the latch** (cycle 87 e3b, plexscratch42 1.42.2, both
  beat shapes and test_ui_surface's own c58t/c70 order replayed; `timeline_play_latch_s42.json`
  `near_end`, `ui_surface_replay`, `p90_*`). A beat in the last tenth that completes nothing
  answers `progress` inside a play that had already passed the minute -- 70 000, a stop, 0, then
  95% (refused by cycle 70's zero rule) and the stop at 95% after it -- and `ignore` on a fresh
  play (cycle 45's rule). It does not itself pass the minute (0, 95%, stop at 95% answers the stop
  `ignore`); a completion does. A completion closed by its stop answers later near-end beats
  `ignore` (92%, stop at 93%, then 95%) until a beat under 90% starts a new play (then 0, 95% says
  `progress` again). An `offline=1` sync marks or clears view state whatever the latch says: in
  the UI suite's order Plex for Android's offline beat after its own 95% stop answered `complete`
  1 on the reference and `progress` on ours. The UI suite's c58t check expected the progressed
  session's STOPPED 30 000 beat to delete the resume point; the reference keeps 110 000 (a stop
  never deletes), so the check now asserts that, and its c70 crossing check asserts the refused
  95% beat's measured `progress`. MALFORMED-ONLY for the answer word (python-plexapi discards
  it); CLIENT-VISIBLE for the offline sync, which Plex for Android uses to report downloads
  watched (js:1673773) -- after a 95% play it marked nothing on ours.
- **A library made by Add Library KEEPS the form's agent, scanner, group and folder.** Measured
  on plexscratch42 (1.42.2) with an empty `type=artist` library made from
  `agent=tv.plex.agents.music&scanner=Plex%20Music&metadataAgentProviderGroupId=3&location=...`
  (`tests/fixtures/c87_w5_music_library_s42.json`): `/library/sections`, `/media/providers` and
  the `location` axis read back the music agent, `Plex Music`, group 3 and the folder as sent;
  `createdAt`, `updatedAt` and `scannedAt` are the one create moment and `contentChangedAt` is -1.
  The 201 echoes `key type title agent scanner language metadataAgentProviderGroupId uuid
  updatedAt createdAt content directory contentChangedAt hidden Location`, with no `refreshing`.
  plexsim kept only title and type and re-derived the movie agent, `Plex Movie`, group 1 and
  `/plexsim/<n>` on every read, so the Edit Library dialog and python-plexapi (`section.agent`,
  `.scanner`, `.locations`) read a new music library as a movie one. CLIENT-VISIBLE.
- **A music library has its OWN `Meta`, and its album sorts are not the type's list.** Measured on
  an empty `type=artist` library on plexscratch42 (1.42.2; plexscratch 1.43.4 adds only the track
  `audioCodec` axis): four Types -- `artist` (active, "Artists", `/all?type=8`), `album`, `track`,
  `folder` -- each with its own Sort, Field and Filter lists, captured whole into
  `section_meta_artist.json` (plain and `includeAdvanced=1`, which adds album Year and the track
  Artist, Album and Duration sorts in place). A bare `/filters` or `/sorts` is the type-8 list, the
  Library pivot opens `/all?type=8`, and `/sorts?type=9` in a music library carries `rating`
  ("Critic Rating") after Release Date, which the same type's list on a movie or show library does
  not. Claim moves one row: zen (claimed) lists `source` for types 9 and 10 and both copies do not,
  while on the copy each Type's Filter list equals its `/filters?type=N`, so the album and track
  lists carry zen's row. plexsim served the MOVIE Meta, so Plex Web's Library tab read "Movies"
  with a movie sort and filter bar, and python-plexapi's `listFilters`/`listSorts`/`searchAlbums`/
  `recentlyAddedTracks` raised "Unknown libtype". CLIENT-VISIBLE.
- **A music library's settings ARE the Add Library form's list for type 8 and its agent.**
  Measured on plexscratch42: an empty music library's `/library/sections/{id}/prefs` equals
  `/library/sections/prefs?type=8&agent=tv.plex.agents.music` row for row and value for value, and
  a write (`albumSort=2&artistBios=0`) is a 200 that reads back `2` and `false`. The copy is
  unclaimed and answers nine rows; zen's form (claimed, plexsim's `section_prefs_new.json`) has
  thirteen -- `musicAnalysis`, `augmentWithSharedContent`, `useExternalLyrics` and `concerts` are
  claim-gated, and the copy refuses a write to one as unknown -- so plexsim serves the thirteen.
  plexsim answered an EMPTY list (and refused every write as unknown), so the Edit Library
  dialog's Advanced tab was blank and python-plexapi's `settings()` was []. CLIENT-VISIBLE.
- **A music library's Recommended tab is eight shelves, three of them behind request flags.**
  Measured on plexscratch42 and plexscratch over an empty music library: `/hubs/sections/{id}` is
  always `music.recent.played`, `music.recent.added` ("Recently Added in <library>", `album`),
  `music.vault`, `music.touring` and `music.videos.new`; `includeLibraryPlaylists=1` adds the
  audio Recent Playlists after Recently Added, `includeStations=1` then `music.stations` (no key),
  `includeExternalMetadata=1` then `music.tidal.releases.new` (`attribution="com.tidal"`). Plex
  Web sends all three (8 shelves), python-plexapi's `hubs()`/`stations()` `includeStations=1`
  alone (6). The vault and touring shelves are `random`: a real PMS redraws the vault's period
  (2 months, 4 months, 8 months, 2 years, 3 years seen) and a `taggingCreatedAt` cutoff within the
  last day every few minutes; plexsim draws once per process. `/hubs/promoted` for the library is
  `music.recent.added.<id>` (the copy ignores `pinnedContentDirectoryID`; under it plexsim follows
  zen's swap to `home.music.recent`). `style`/`promoted` are absent on the unclaimed copies for
  every library and kept as the claimed movie and show shelves carry them. plexsim answered a
  MOVIE library's shelves, so `stations()` returned None instead of []. CLIENT-VISIBLE
  (python-plexapi); Plex Web draws an empty library's page the same either way.
- **Every container a music library answers names the ARTIST resources and carries `nocache`.**
  Measured on plexscratch42 over 45 containers of an empty `type=artist` library -- listings,
  subsets, axes, `/sorts`, `/filters`, `/timeline`, `/arts`, `/extras/all`, `/albums`, the
  library's own pivot list: `art=/:/resources/artist-fanart.jpg`, `thumb=/:/resources/artist.png`
  and `nocache=1`, as a show library carries it and a movie library never does. `/all` at every
  type and the collections are `title2="All Artists"`, `viewGroup="artist"`; Recently Added (and
  its Grandparents alias, without `mixedParents`) is the `album` view, On Deck, Recently Released,
  Recently Viewed ("Recently Viewed Episodes"), Unplayed and `/arts` the `track` view with
  `viewMode=65593`; `/library/sections/{id}` lists eleven pivots (All Artists, By Album, Genre,
  Decade, Year, Collection, Recently Added, By Folder, and three searches typed 8, 9, 10).
  plexsim answered the movie library's containers and its twenty movie pivots. MALFORMED-ONLY:
  Plex Web draws an empty library's page from the Meta and the hubs, not from these.
- **Deleting a music library takes its shelves with it, and both servers already agreed.**
  Measured on plexscratch42 and plexscratch: `DELETE /library/sections/{id}` on an empty music
  library is a 200 with an empty `text/html` body and `Connection: close`; afterwards
  `/hubs/sections/{id}` (with or without `includeStations`), the library, its `/all` and its
  `/prefs` are 404s, `/hubs/promoted?contentDirectoryID={id}` keeps only Continue Watching and On
  Deck, and `/hubs` keeps `home.music.recent` -- which zen and the copies list with no music
  library at all. plexsim answered all of it the same way; the check guards it.
- **An EMPTY library's composite is a 404.** Measured on plexscratch42 (1.42.2) and plexscratch
  (1.43.4) for an empty movie, show and music library made with Add Library's form:
  `/library/sections/{id}/composite/{n}` -- bare, and with the `width`/`height`/`rows`/`cols`/
  `border`/`media`/`repeat` set Plex Web's composite view appends -- is the ordinary 85-byte html
  404 under every Accept, and `/photo/:/transcode?url=` of it is the same 404. A library with
  members answers a jpeg (kids). plexsim drew a generated PNG tile. MALFORMED-ONLY: no Plex Web
  4.147.1/4.162.1 call site builds a SECTION composite (its composite view reads a row's own
  `composite`, and no section row on kids, zen or plexscratch42 carries one), and
  python-plexapi's `LibrarySection.composite` reads the same absent attribute.
- **`/system` has no JSON form but the bare `/system/agents`.** Measured on zen, kids and
  plexscratch42 under `Accept: application/json`, `application/xml`, none, and
  `application/json, text/plain, */*`: `/system`, `/system/search`, `/system/library/sections`,
  `/system/scanners/{N}` (types 1, 2, 8, 9, 13, with and without 4.162.1's `action=`), the legacy
  agents' `/contributors` and `/{id}/config/{N}` all answer the same `application/xml` document
  whatever Accept says; `/system/agents` (and `?mediaType=`) is the only JSON under `/system`.
  The three servers' scanner lists are byte-identical (1: four rows, 2: two, 8 and 9: two, 13:
  one), and the MUSIC list is a different writer's -- a double-quoted declaration and an empty
  indented third row, `size="3"`, 220 bytes. plexsim answered JSON to a JSON Accept on the first
  four and 217 bytes for music. MALFORMED-ONLY: Plex Web 4.147.1 and 4.162.1 read the list
  through `ScannerCollection`, whose responseType is the base `xml` (`Accept: application/xml`),
  and parse the body as XML; only a third-party JSON client saw the difference.
- **`/system/services/url/lookup` with a `url` is a 404, not the traceback.** Measured on
  plexscratch42 (1.42.2), kids (1.43.4) and zen (1.42.2), raw keep-alive sockets, 2026-10-01
  (`tests/fixtures/c87_w7_url_lookup.json`): the 464-byte traceback is the bare request and any
  query WITHOUT `url` (`foo=1`, `URL=x`); any `url` value -- empty, `x`, a relative path, an http
  URL and, on the isolated plexscratch42 only, a YouTube watch URL -- is a 404 with an EMPTY body,
  no `Content-Type` and the framework cookie, under JSON Accept, a gzip offer, paging and HEAD.
  (`?url` with no `=` is PMS's own 89-byte 400 before the framework sees it.) Both servers
  answered the traceback to all of them. MALFORMED-ONLY: Plex Web 4.147.1 reads such a key only
  for a channel item (`responseType` special-cases `/system/services/url/lookup`), and with no
  channel installed no row carries one.
- **The framework's ten peer directories PAGE.** `/system/{channels,plugins}/{all,music,video,
  photos,applications}` on plexscratch42, kids and zen (2026-10-01,
  `tests/fixtures/c87_w7_peer_paging.json`): a full window by header or query -- `Size=0`
  included -- answers `totalSize="0"` as the FIRST attribute (144 bytes) and
  `X-Plex-Container-Start` (echoed, `1` past the end too) / `X-Plex-Container-Total-Size: 0`; a
  lone Start or Size, or `Start=abc`, is the unpaged 130-byte document. Both servers served the
  unpaged document whatever was asked. MALFORMED-ONLY: neither Plex Web 4.147.1 nor
  python-plexapi requests these paths.
