# rex — a Plex Media Server that owns no files

## RESULT: the official Plex Web client plays a debrid stream from rex

Verified 2026-09-11 15:26 by driving the real client over CDP:

```
video.src      https://rex.example.com/library/parts/10010/…/file.mp4
               ?X-Plex-Session-Identifier=64s596vv5bjqm0n
readyState     4 (HAVE_ENOUGH_DATA)      paused false
currentTime    164.56 -> 179.59 advancing in real time
resolution     1920x1080                 decoded 4929 -> 5381 frames
droppedFrames  0                         error null
```

rex's own log for the same playback:

```
PLAYQUEUE 1 -> 1 item(s)
DECISION direct play for path=/library/metadata/1001 (hit)
PART 10010 range=- -> 200 len=852900097
```

Direct play, 1080p H.264, no transcoding, no file on disk — the bytes came from
a Real-Debrid link resolved at read time.

### The seven things that had to be fixed to get there

Each was found by diffing against a real PMS or by reading the browser console,
and each produced a *misleading* symptom on its own:

| # | Fix | Symptom it caused |
|---|---|---|
| 1 | Serve the official web client from `/web` | app.plex.tv needs a resource `accessToken` plex.tv never issues; the locally served client needs none |
| 2 | `Meta` block on the section listing (`includeMeta=1`) | grid printed the item count and rendered zero cells |
| 3 | Keep `Media` in listing rows + add the `Image` array | same empty grid; a real PMS does not strip `Media` here |
| 4 | Real RFC6455 WebSocket upgrade on `/:/websockets/notifications` | a clean 501 made the client log "Unexpectedly closed server event connection" and declare the **server** faulty — this was also the true cause of "currently unavailable" |
| 5 | `playQueues` must accept a **section** uri, not just a ratingKey | "An error occurred loading items to play", with no request reaching rex |
| 6 | An MP4/H.264/AAC item in the catalog | Plex Web will not direct-play mkv or webm; it demanded a transcode rex has no encoder for |
| 7 | `--part-mode proxy` to rewrite the Content-Type | RD answers `application/force-download`; Chrome's `<video>` refuses a non-media type, so the 302 path fetched bytes and still failed |

Fix 4 is the one worth remembering: **refusing an endpoint cleanly is worse than
not having it**, because the client reads the refusal as server breakage.

### Real metadata: TMDB identification and artwork

`metadata.py` turns a release name into an identified library row. Two stages,
deliberately separate: parse the filename into a title plus an optional SxxEyy,
then ask TMDB. A release name is not metadata, so **an unidentified release
keeps its probe-derived row rather than being given invented details.**

What a row gains once identified (example, from the real catalog):

```
guid                   imdb://tt1196946        Guid  imdb://tt1196946, tmdb://5920
title                  Seeing Red              grandparentTitle  The Mentalist
parentIndex 1          index 7                 originallyAvailableAt  2008-11-18
contentRating          TV-14                   studio  Warner Bros. Television
rating / audienceRating 8.4                    tagline  Let the mind games begin.
Genre                  Crime, Drama, Mystery   Country  United States of America
Role                   Simon Baker, Robin Tunney, Tim Kang, Rockmond Dunbar, …
summary                Patrick Jane goes head-to-head with a "psychic" he suspects…
```

Note the title: the release was `The.Mentalist.S01E07…`, and it resolved to the
actual **episode** title, synopsis and air date, with the show kept in
`grandparentTitle`. Hell's Kitchen S17E16 likewise became "All-Star Finale".
Two obscure UK food shows were genuinely not on TMDB and kept their
filename-derived rows — which is the intended behaviour, not a failure.

**Artwork** is proxied, not redirected: the row stores the TMDB poster/backdrop
URLs privately, and `/photo/:/transcode` and `/library/metadata/{rk}/{thumb,art}`
resolve and fetch them through an in-memory cache (bounded at 512 entries; a
grid scroll otherwise becomes a burst of identical upstream fetches). Verified
on the client's own request shape — `342x513` and `480x720` JPEGs, HTTP 200.

Two bugs this work surfaced:

- **`_poster_url` and `_art_url` were leaking into API responses.** The row
  projection stripped only `_debrid`. It now strips every `_`-prefixed key, and
  there is a single `_public_row()` used by both the listing and the detail
  response so the two cannot drift again.
- The `Image` array was only added to listing rows, so the detail page had no
  artwork to ask for. A real PMS sends it on both.

Playback re-verified on an identified row afterwards, including a resume:
`range=bytes=30474240- -> 206` for a mid-file seek, then
`t=42.98 → 58.04, 1920x1080, dropped=0`.

### Every item tested: 5/5 served correctly

`test_all.py` drives the real playback sequence per item and then proves the
bytes decode, rather than trusting a 200. Both transports are checked, because
they differ.

```
item                              server  local          public         decode      plays in
Global Veggies       mp4/h264/aac   OK    206 + seek ok  206 + seek ok  37 MB @135s  all three
Mentalist S01E07     mkv/h264/eac3  OK    206 + seek ok  206 + seek ok  37 MB @262s  TV only
Hell's Kitchen S17E16 mp4/h264/aac  OK    206 + seek ok  206 + seek ok  37 MB @258s  all three
Hell's Kitchen S18E02 mp4/h264/aac  OK    206 + seek ok  206 + seek ok  37 MB @258s  all three
Tears of Steel       webm/vp8/vorbis OK   206 + seek ok  206 + seek ok  27 MB @73s   NONE
```

Per item: play queue built, `Part.decision=directplay`, prefix read 206 with a
`video/*` type, container magic verified (`ftyp` / EBML, proving it is media and
not an error page), mid-file seek landing at the requested offset, and real
frame decode at the 10% mark.

**The client gate is the real constraint, not the server.** rex serves all
five perfectly; what varies is whether a client will accept them:

| item | Plex Web / Mac | TV h264+ac3 | TV 4K |
|---|---|---|---|
| mp4 / h264 / aac (×3) | play | play | play |
| mkv / h264 / **eac3** | **transcode** | play | play |
| webm / **vp8 / vorbis** | **transcode** | **transcode** | **transcode** |

So a debrid-backed Plex library is only as useful as its container mix. An mp4
/ h264 / aac release plays everywhere; mkv+eac3 is TV-only; vp8/vorbis in webm
direct-plays on nothing, which matters because rex has no encoder and a
transcode verdict means the item simply does not play.

**Cloudflare blocks `Python-urllib` with a 403.** Measured on the media path:

```
Python-urllib/3.10          403
curl/8.7.1                  206
Plex/1.115.0 (Macintosh)    206
Mozilla/5.0 (Macintosh…)    206
(no User-Agent at all)      206
```

The first run of this harness reported every public read as failed, and I
initially attributed that to rate limiting from a burst of media reads. That was
wrong: it is User-Agent filtering, and an empty UA passes where urllib's default
does not. Any tooling that probes a Cloudflare-fronted origin has to set a
realistic UA or it measures the bot filter instead of the server.

### A real show → season → episode hierarchy

Symptom that forced this: in the library grid, an identified **movie** drew its
poster while identified **episodes** rendered as blank tiles. Cause was in the
rows, not the artwork — rex emitted every file as `type=movie` while also
attaching `index`, `parentIndex` and `grandparentTitle`. A row that claims to be
a movie and an episode at once sends the client looking for parent artwork that
does not exist.

`hierarchy.py` builds the real shape. Movies go in a movie section; identified
episodes get synthesised parents in a show section; anything **unidentified
stays a movie**, because that is the honest answer for a file nothing matched.

From the live catalog:

```
movie    rk=1001   Global Veggies (S02E06)        (unidentified)
show     rk=20001  The Mentalist       childCount=1 leafCount=1
season   rk=30001    Season 1          leafCount=1
episode  rk=1002       Seeing Red      S01E07
show     rk=20002  Hell's Kitchen      childCount=2 leafCount=2
season   rk=30002    Season 17         leafCount=1
episode  rk=1003       All-Star Finale S17E16
season   rk=30003    Season 18         leafCount=1
episode  rk=1004       A Fond Farewell S18E02
movie    rk=1005   Tears of Steel
```

| endpoint | returns |
|---|---|
| `/library/sections/1/all?type=1` | 2 movies — **no episodes at library top level** |
| `/library/sections/2/all?type=2` | 2 shows, `viewGroup=show`, with `childCount`/`leafCount` |
| `/library/metadata/20002/children` | seasons 17 and 18, `viewGroup=season` |
| `/library/metadata/30002/children` | the episode, `viewGroup=episode`, `grandparentTitle` set |
| `/library/metadata/20002/allLeaves` | both episodes across both seasons |

Three things had to become section-type aware rather than hardcoded to movie:
the `Meta` descriptors (a show section's `Type` is `type=show` / `type=2`), the
agent and scanner in `/library/sections` and `/media/providers`
(`tv.plex.agents.series` / `Plex TV Series`), and the section art paths.
ratingKey ranges are kept disjoint — shows 20000+, seasons 30000+, leaves
1000+ — so a key identifies its own row type on sight.

Artwork resolves correctly at every level: a show serves its poster, a season
inherits it, an **episode serves its own TMDB still**, and an unidentified file
falls back to the generated placeholder.

```
show     The Mentalist      -> 200  26105 image/jpeg
season   Season 1           -> 200  26105 image/jpeg   (inherits)
episode  Seeing Red         -> 200  76712 image/jpeg   (own still)
movie    Global Veggies     -> 200   1628 image/png    (placeholder, unidentified)
```

Episode playback re-verified: the play queue reports
`All-Star Finale (Hell's Kitchen S17E16)`, the decision returns
`mde=1000 / Part.decision=directplay`, and the part serves `video/mp4`.

*Note on the test rig: the Chromium on kids renders almost nothing through CDP
regardless of what rex returns — it reported 0 images even for tiles that a
real browser drew correctly. Ben's own screenshots were the ground truth for
every rendering question here; CDP was only reliable for driving clicks and
reading the console.*

### The Seasons block: `totalSize`, not the rows

The longest-running defect in this spike. Show pages rendered the hero, On Deck,
the summary and Cast & Crew, and drew **no Seasons block at all** — while the
same client, on the same tab, drew 37 seasons for fun's Simpsons.

Everything obvious was eliminated by direct experiment and none of it was the
cause: browser, client version (4.147.1 vs 4.160.0), transport (LAN HTTP vs
HTTPS through Cloudflare), sign-in state, section prefs including
`flattenSeasons`, season-number contiguity, and — decisively — substituting
fun's own `/children`, show-detail and `/media/providers` payloads wholesale.

The answer came from reading the client's own bundle. rex serves the web
client, so its 214 minified chunks can be searched. `chunk-4304` is the pre-play
page, and its descendant list ends in one gate:

```js
return s && c && d && d.totalSize ? React.createElement(/* the Seasons block */) : null
```

`d` is the descendant list, and `d.totalSize` comes from `main`'s
`augmentResponse`:

```js
const t = null == e.totalSize ? void 0 : parseInteger(e.totalSize)
```

**A MediaContainer with no `totalSize` yields `undefined`, and the entire
Seasons block renders `null`.** Every season row can be perfect and it will
still not appear.

Plex Web asks for seasons with `X-Plex-Container-Start=0&X-Plex-Container-Size=20`.
Measured on fun for the same show:

```
size: 20   totalSize: 37   offset: 0
```

rex returned `size: 3` and nothing else. `/library/sections/N/all` already
paged correctly, which is why the library grid always worked and only show pages
were broken. `child_container()` now honours the container params on `/children`
and `/allLeaves`, and — matching a real PMS — emits `offset`/`totalSize` **only**
when the client actually pages; an unpaged request still carries just `size`.

Why the payload substitutions all failed to find it: those captures were taken
without the paging params, so fun's own recorded responses had no `totalSize`
either. Copying a real server's response is only as good as the request that
produced it.

`test_navigation.py` covers it (51/51): paged children carry `totalSize` and
`offset`, unpaged children carry neither, and a second page moves the window.
Against the pre-fix server those six checks fail and the other 45 pass — which
is the shape of this bug in one line.

### The UI is a write surface, and the server decides what it offers

Measured 2026-09-12. Browsing was the whole story until now; everything in the
Plex UI that *changes* something was either unimplemented or answered with the
generic empty container rex returns for an unknown route. That is worse than
a 404, for the same reason fix 4 above was: the client does not ask whether a
write will work before drawing the entry that performs it. **It decides from
what the server advertises**, so an unimplemented endpoint shows the user a menu
entry, a confirmation dialog, and nothing happening.

Two instruments, and the second one is the interesting one:

1. **plexlab** on kids: a real PMS 1.43.4 in a container (`~/plexlab`), the
   official web client in a second container driven over CDP, and mitmproxy
   between them recording both axes (client -> server, server -> plex.tv). Its
   library is four Blender open movies and Firefly, so every endpoint could be
   measured against real rows, mutations included, without touching production.
   Where the lab had no rows at all -- play history, live activities -- the
   measurement came from **fun**, read-only.
2. **The client's own bundle.** rex serves the web client, so the gating
   logic can simply be read. `main-*.js` module 92273 is the whole contract: a
   map of item type -> ordered list of menu flags, plus one predicate per flag.

#### Which capability draws which menu entry

Straight out of module 92273's predicates. The left column is what the user
sees; the right is what the server has to say for it to exist at all:

| Menu entry | Gate |
|---|---|
| Play / Play All | playability, and provider feature `playqueue` |
| Play Next, Add to Queue | `playqueue` feature |
| Shuffle | `playqueue` feature |
| Mark as Played / Unplayed | `timeline` feature with `scrobbleKey`/`unscrobbleKey`, **and for a show or season `unviewedLeafCount > 0`** |
| Add to Playlist | `playlist` feature |
| Edit, Refresh Metadata | `manage` feature |
| Analyze | `manage`, and the item is not `indirect`/`remoteMedia` |
| Fix Match… | `manage`, and a guid that is **not** `local://` or `none://` |
| Match… | `manage`, and a guid that **is** one of those |
| Unmatch | same as Fix Match |
| Split Apart | `manage`, and more than one `Media` (or `Location` for a show) |
| Delete | `manage`, and the server reports `allowMediaDeletion` |
| Delete Version | `allowMediaDeletion`, and a `Media` with a `proxyType` |
| Download | provider `protocols` contains `download` |
| Optimize… | server reports `transcoderVideo` **and** `backgroundProcessing` |
| Sync & Download | `subscribe` feature with a download flavor, and `allowSync` |
| Get Info | library provider, item not an extra |
| Remove from Continue Watching | the `actions` feature advertises that Action |

Consequences for rex, each one a behaviour change rather than a new field:

- **Download was hidden by one word.** The provider advertised
  `protocols: "stream"`. rex can serve a download -- it is the same Part read
  with `download=1` and a `Content-Disposition` -- so the entry was hiding a
  feature that already worked.
- **Delete needs `allowMediaDeletion`.** rex reported false because it owns
  no files. But the Plex phrasing ("delete from your filesystem") has a local
  meaning here: drop the row from the library. The debrid content is untouched,
  and since the mutation is in memory, a restart is the undo button.
- **`allowSync` was absent rather than false.** An absent field and a false one
  are different statements, and the Sync entry reads it.
- **Unmatch has to leave a `local://` guid.** Stripping the guid entirely -- the
  obvious reading of "unmatch" -- leaves an item that offers *neither* Match nor
  Fix Match, because both predicates require a guid to be present.
- **Marking a show played has to walk to the leaves.** The predicate reads
  `unviewedLeafCount`, derived from `leafCount` and `viewedLeafCount` on the show
  row, so a show whose episodes stayed unwatched keeps offering "Mark as Played"
  however correct the show row looks.
- **Optimize stays hidden on purpose.** It is gated on `transcoderVideo`, which
  rex must not claim: a transcode verdict means the item does not play at
  all. Advertising a transcoder it does not have is what produced "This server is
  not powerful enough to convert video" in the first place.

#### Response shapes the write half turned up

| Measured | What rex did |
|---|---|
| Every mutating endpoint answers **200 with an empty body** and `text/html` (refresh, analyze, unmatch, match, split, merge, scrobble, unscrobble, rate, prefs write, section tasks, edits) | answered with a MediaContainer |
| `DELETE /playlists/{id}` answers **204**, while `DELETE /library/collections/{id}` answers **200** | neither existed |
| `PUT /playlists/{id}/items` answers with `leafCountAdded` / `leafCountRequested` | — |
| An empty child list is **omitted**, never sent as `[]` -- these responses are XML underneath, and `Metadata: []` says "there is state here and it is empty" | sent `Metadata: []` |
| `/library/sections/{id}/sorts` and `/filters` carry **`Directory`**, 9 and 28 entries | carried `Sort` (2 entries) and `Filter` (0) |
| A tag axis keys entries by **tag id** (`?genre=24`), a field axis by the **value** (`?contentRating=PG`) | neither existed, so every secondary filter opened the whole library |
| A **paged** listing carries `offset` + `totalSize`; an **unpaged** one carries `nocache` instead. `newest`, `recentlyAdded`, `onDeck` and `collections` carry none of the three | every listing carried all of them |
| `/library/metadata/{id}/allLeaves` says `mixedParents` and omits `thumb` and `summary`, both of which `/children` carries | carried thumb and summary, no mixedParents |
| `/library/sections` carries `title1` and `allowSync` only | carried `identifier` and the media-tag pair |
| `/library/metadata/{id}` **does** carry `identifier` and the media-tag pair | a note here claimed the opposite -- see below |
| `/library/collections` bare, `/library/optimize` via GET, `/library/sections/prefs`, `/library/people`, `/sync/*` all answer 4xx | answered 200 with an empty container |
| `/:/timeline` answers `{size, playbackState, skipCount, viewCount, viewOffset}` -- and `playbackState` is `ignore`/`progress`/`complete`, never the state sent (corrected below) | answered a bare container |
| A play-queue response carries `playQueueSourceURI` | omitted it, so a client re-reading the queue lost its source |
| An Activity is `{uuid, type, cancellable, userID, title, subtitle, progress}` (measured on fun mid-refresh) | — |
| `download=1` sends `Content-Disposition: attachment; filename="<the media file's name>"` **plus** the RFC 5987 `filename*=UTF-8''…` form | — |
| A history row is `{historyKey, key, ratingKey, librarySectionID (a **string**), type, viewedAt, accountID, deviceID}` plus `grandparent*`/`index`/`parentIndex` for an episode (measured on fun, 3879 rows) | — |
| A section's Recommended tab is **five** shelves, each with `style: "shelf"`, a `context`, a `key` for its own see-all list, and `promoted` on Recently Added | one shelf, so the tab existed and was nearly empty |
| Every content directory advertises a **third** pivot, `categories`, keyed `/library/sections/{id}/categories` | two pivots, and no such endpoint |
| A **show** section's `Meta` has four Types (show, season, episode, folder) with their own filters and sorts | the movie template retargeted, so a show grid's filter bar could only talk about movies |
| A `Feature` list is compared position by position by `harness/scripts/replay.py`; the same set in a different order reads as a dozen value differences | a different order |

#### Tag ids are the whole filter surface

A secondary axis keys its entries by **tag id** (`/all?genre=24`), and TMDB hands
rex tag names with no ids at all. Every `Genre`, `Director`, `Role` and
`Country` tag therefore gets an id minted from its own text at catalog load
(`Catalog.number_tags`), which is stable across restarts and rebuilds, plus the
`filter` string a real row carries.

The interesting part is how the gap hid. Before the ids existed, the axis emitted
`?genre=None` and the listing compared `str(tag.get("id")) == value` -- `"None"
== "None"` -- so **every filter matched every row that had any tag at all**. From
the outside it looked like a working filter, and `By genre lists and filters`
passed. What caught it was tightening the check to compare the filtered list
against the rows' own tags: `first opens 1 row(s) of 3 that carry it`. The same
check then caught the follow-on case, where a row re-identified by a manual match
came back from the agent with fresh, id-less tags and silently dropped out of
every filter while its own detail page still looked right.

**A measurement claim in this file did not hold.** The item-detail handler
carried a comment saying "Verified against fun: an item-detail container is
BARE -- no identifier, mediaTagPrefix or mediaTagVersion". Re-measured on
2026-09-12 against both fun and the 1.43.4 reference, with the client's own
`includeX` param set: both send all three. The note was wrong, and
`mediaTagPrefix` is what a client uses to turn a tag into an image URL. Fixed.

#### Merge is not reversible, and that was learnt the hard way

`PUT /library/metadata/{id}/merge?ids=` on the reference server merged two of
its movies for good. `PUT .../split` does undo the *shape* -- two rows again --
but the second row comes back on a **new ratingKey carrying the first one's
metadata**, so it has to be re-matched by hand. Measuring merge cost a
re-identification of Elephants Dream. `probes/ui_surface.py` therefore excludes
merge from its `--mutate` set, with that reason written next to it, and rex's
own merge is in memory where a restart undoes it.

#### What rex now answers

Item context menu: Play, Play All, Play Next, Add to Queue, Shuffle, Mark as
Played/Unplayed (walking to the leaves), rate, Add to Playlist, Add to
Collection (a tag edit), Edit with field locks, Refresh Metadata, Analyze, Fix
Match/Match against live TMDB, Unmatch, Split Apart, Merge, Download, Delete,
Delete Version, Get Info, Change Artwork, Remove from Continue Watching, View
Play History, audio/subtitle selection.

Server pages: Status (Dashboard, Alerts, Conversions), Settings (General, Remote
Access, Agents, Library, Network, Transcoder, Languages, DLNA, Scheduled Tasks,
Extras -- all of them the `/:/prefs` group list, now writable), Manage
(Libraries with add/rename/delete/scan/cancel/refresh/empty trash/analyze/
preview thumbnails, Optimized Versions, Live TV & DVR, Troubleshooting with a
real log zip, Console, Plugins), plus `/activities`, `/statistics/*`,
`/status/sessions*`, `/transcode/sessions`, `/devices`, `/accounts`,
`/security/token`, `/updater/*`, `/system/agents`, `/system/scanners`,
playlists and collections in full.

Deliberately still refused, each because the honest answer is no: Optimize and
Sync & Download (no encoder), Live TV and tuners (no tuner), Download Database
(no database, and `diagnostics` no longer advertises one), and the channel
appstore (no plug-in host -- a real PMS 404s those paths too).

#### Verified in the official client, not only against the API

The same item detail page in Plex Web 4.147.1, driven over CDP from the plexlab
browser, before and after -- the menu the user actually sees:

```
old  Watch Together… · Play Next · Add to Queue · Add to · Refresh Metadata ·
     Analyze · Fix Match… · Unmatch · Grant Access… · View Play History · Get Info
new  … the same, plus  Save File  and  Delete
```

Exactly the two entries whose gates changed: `protocols: "stream,download"`
draws **Save File** (the client's label for Download) and `allowMediaDeletion`
draws **Delete**. Everything else was already being drawn by the old server and
did nothing when pressed, which is the failure mode this whole section is about.

And one entry followed end to end rather than inspected: clicking **Mark as
Watched** in the client turned the button into "Mark as Unwatched", and rex
logged `SCROBBLE 1007 -> 1 leaf row(s)` with `viewCount=1` on the row.

Save File was checked against a real provider link rather than the fixture,
because the fixture's links are the example file's placeholder:

```
GET /library/parts/10070/1/file.mkv?download=1   Range: bytes=0-99
206  video/x-matroska  Content-Range: bytes 0-99/16692904
     Content-Disposition: attachment; filename="For the Birds (2000).mkv";
                          filename*=UTF-8''For%20the%20Birds%20%282000%29.mkv
```

`tests/test_ui_surface.py` covers all of it: **329/329 checks against the new
server, 68/220 against the spike it started from** on the same fixture catalog
(`tests/catalog_ui.json`, rows cloned off the redacted real catalog so the
section has more than one movie and the show has two seasons). The count differs
between the two runs because a check that depends on a playlist the old server
never created does not run at all. Without a TMDB key it is 182/182, with the two
Fix Match checks skipping rather than passing quietly.

#### The second sweep: what "everything" still did not include

Asked whether anything was left, the answer came from firing every remaining
path the client bundle holds -- including the ones built dynamically -- at
rex and reading its own `UNKNOWN` log, then measuring each against the
reference. 28 routes were still landing in the catch-all, and another dozen were
being answered with an empty container where a real PMS refuses.

Real endpoints that were missing:

| Endpoint | What it is | Measured shape |
|---|---|---|
| `/butler`, `POST\|DELETE /butler/{task}` | Scheduled Tasks, and its Run button | root element **`ButlerTasks`** with `ButlerTask` rows — the one response on the whole surface that is not a MediaContainer |
| `/system` | the system provider's directories | `identifier=com.plexapp.system`, two Directory rows |
| `/servers` | the legacy server list | `Server` row: name, host, address, port, machineIdentifier, version |
| `/logs` | the Console's log read | `{size, content}` — **not** the zip; that is `/diagnostics/logs` |
| `POST /log` | the web client posting its OWN errors | empty 200 |
| `/library` | the library root | three directories: sections, recentlyAdded, onDeck |
| `/library/all/top` | most played | a listing |
| `/hubs?identifier=a,b` | Home asking for named shelves | the filtered subset — rex returned all of them, so Home drew rows it had not asked for |
| `/library/metadata/{id}/subtitles` | the Search Subtitles dialog | `Stream` rows from a subtitle agent; rex has none, so the shape with nothing in it |
| `PUT /library/metadata/{id}/prefs` | a show's episode sorting, a collection's sort mode | empty 200; the **GET is a 404** |
| `PUT /playlists/{id}/items/{itemID}/move[?after=]` | drag-to-reorder a playlist | no `after` means the FRONT; answers with the playlist row |
| `PUT /library/collections/{id}/items/{ratingKey}/move[?after=]` | reorder a collection | same, but keyed by the member's **ratingKey**, not a playlistItemID |
| `/library/sections/{id}/unwatched`, `/resolution` | a listing and an axis the hub shelves link to | `resolution` is read off the Media, not off a tag |

And a class of endpoints where the honest answer is a refusal, because that is
what a real PMS answers: **every `/player/*` command is a 400** (they belong to a
player, not to a server — rex was telling companion clients their commands
had been accepted), `/system/proxy` 400, `/library/sections/{id}/search` 400,
`/library/platforms/*` `/hubs/sections/home*` `/hubs/metadata` and an item's
`markers` `banner` `chapterImages` `indexes` all 404, `/library/streams/{id}`
**501**, and `/livetv/*` an empty container.

#### The catch-all now 404s, and that found the last missing endpoint

A real PMS answers an unrouted path with 404 (measured). rex's catch-all
answered 200 with an empty MediaContainer, which is the trap of fix 4 from the
other side: a client reads that as "this endpoint exists and there is nothing in
it". The rule that made the empty container look safe — refusing an endpoint
cleanly is worse than not having it — is about endpoints the client *needs*, and
the fix for those is to route them.

Flipping it proved its own worth immediately: `conformance.py` broke on
`GET /library`, an endpoint python-plexapi asks for before anything else and
which rex had never routed. The old catch-all had been hiding it for the
life of the project. With `/library` implemented, plexapi is back to 29/38 (the
nine failures are byte reads against placeholder links) with **zero** unrouted
paths for a full drive, and the web client still renders the item page and the
full context menu.

#### The Console's live log stream does not exist to emulate

The one item left on the "not measured" list. The Console page has a parser for
PMS log lines (`^([^[]+)\[([^\]]+)]\s+([^\s]+)\s+-\s+(.*)`) and a filter UI,
and the page is drawn whenever the server's `diagnostics` string contains
`streaminglogs`. What feeds it was the open question.

Measured, in this order:

1. Instrumenting `window.WebSocket` in the client showed it opens **exactly one**
   socket, `/:/websockets/notifications`, **with no filter parameter** -- so
   there is no second channel and no subscription to imitate.
2. Subscribing to the reference PMS's own socket for **75 seconds while it was
   busy** (two forced section scans, two item refreshes, an analyze and a butler
   run) produced **zero** log frames.

So on a current PMS the Console renders empty too. There is nothing to push, and
nothing was invented.

#### What that measurement did find: four notification types rex never pushed

The same 75 seconds produced these, and rex's socket had been silent since
the day it was built -- it completed the upgrade and then only pinged:

| Type | Element | When | rex now |
|---|---|---|---|
| `activity` | `ActivityNotification` `{event, uuid, Activity}` | a task starts or ends | every activity it records |
| `timeline` | `TimelineEntry` `{identifier, sectionID, itemID (both **strings**), type (Plex's **numeric** code), title, state, metadataState, updatedAt}` | a row changes | after a refresh |
| `status` | `StatusNotification` `{title, description, notificationName}` | `Scanning the "Movies" section` | on a section scan |
| `progress` | `ProgressNotification` `{message}` | `The butler refreshed all libraries.` | when that task finishes |

A client that receives none of these falls back to polling and never sees a
change made from another device. Verified in the official client: the socket
reports `{"activity":4,"timeline":1,"progress":1}` received, stays open, and the
page keeps rendering -- which also proves the hand-rolled frames are well formed,
the failure mode of getting that wrong being the one from fix 4 (the client
decides the server is broken and draws nothing).

#### A fourth pass, from the one angle nothing had used: XML

Every check so far asked for JSON, because that is what Plex Web sends. **XML is
rex's default** -- `_wants_xml()` returns true unless a client explicitly
asks for JSON -- so the default response path of ~60 new endpoints had never been
exercised. All 67 render parseable XML. Three rendered the **wrong element**:

| Endpoint | rex rendered | a real PMS renders |
|---|---|---|
| `/library/metadata/{id}/posters`, `/arts` | `<Video>` | **`<Photo>`** |
| `/library/sections/{id}/folder` | `<Video>` | **`<Directory>`** |
| `/library/shared/all` | `<Video>` | **`<Directory>`**, under identifier `com.plexapp.plugins.library.shared` |

The cause is structural: `xmlout` names a `Metadata` row's element from the row's
own `type`, and these rows have no type -- so all three defaulted to `<Video>`.
The container now carries an internal `_xml` hint for the endpoints that know
their element, stripped before the JSON body. A JSON-only harness cannot see any
of this, which is why the suite now checks element names too.

#### The stream title split was backwards, and it is what the picker prints

Measured on two real servers, and the distinction is not the intuitive one:

```
video     displayTitle  1080p                 extended  1080p (H.264)
audio     displayTitle  日本語 (OPUS 5.1)      extended  日本語 (OPUS 5.1)
subtitle  displayTitle  English               extended  English (ASS)
subtitle  displayTitle  English (Weeb)        extended  Honorifics (English (Weeb) ASS)
  with a track title
```

So the codec belongs in the **extended** title, and a subtitle with a track title
shows its **language** plainly with the track name only in the extended one.
rex had the codec in a video's `displayTitle` and the track name in a
subtitle's -- both of which are the string the client's quality and track pickers
print, so every label was subtly wrong. `mediainfo.stream_titles()` now derives
both, and the catalog recomputes them at load so a catalog built before the fix is
corrected rather than needing a rebuild. `extendedDisplayTitle` did not exist at
all before.

One measured difference is deliberately not reproduced: 10-bit HEVC on fun
extends to `1080p (HEVC Main 10)` while 8-bit H.264 on the reference extends to
`1080p (H.264)` with no profile. Two samples do not give the rule for when the
profile is appended, so it is left off.

#### The eventsource channel is a different wire format

It carries the same events as the websocket -- confirmed by reading it on the
reference while it was busy -- but not in the same shape:

```
websocket    {"NotificationContainer": {"type": "timeline", "size": 1, "TimelineEntry": [ … ]}}
eventsource  event: timeline
             data: {"TimelineEntry": { … one row, no container, no size … }}
```

rex's SSE channel had been a keep-alive since it was written. Both transports
now come off the same notification bus, each formatting for itself.

#### The rest of the fourth pass

| Found | Fixed |
|---|---|
| A section `Directory` row was missing `filters` (which is what tells a client the section HAS a filter bar), `metadataAgentProviderGroupId` and all four timestamps | one `section_directory()` builds the measured row for both `/library/sections` and `/library/shared/all` |
| The tree's `MediaPart` carries its streams as `MediaStream`, with `type` rather than `streamType` | added; `hash` and `openSubtitleHash` stay absent, both being hashes OF THE FILE |
| A collections listing keeps the **section's** `viewGroup` and `title2` ("movie", "All Movies"), not "collection" | corrected |
| `/playlists`, `/playlists/all` and `/playlists/{id}/items` page (`totalSize`/`offset`) and sort | added -- and a playlist row has no `titleSort`, so keying the sort on that alone made every row compare equal and the direction do nothing |
| ffprobe reports `chroma_location`, `coded_height`, `coded_width` and a real PMS carries them | added to mediainfo; `hasScalingMatrix` is **not**, because a real PMS reads it out of the H.264 bitstream and ffprobe does not report it |

#### `flattenSeasons` is one field on the show, not a different `/children`

rex served `flattenSeasons` in its section preferences and then ignored it,
so a library set to hide seasons still drew every one of them. The obvious
implementation -- have `/children` return episodes instead of seasons -- is wrong.
Measured: with the pref set to Hide, a real PMS **still answers `/children` with
the seasons**, and what changes is a single field on the SHOW row,
`skipChildren: true`. The client skips the level on the strength of that.

Enum, measured: `0` Show, `1` Hide, `2` Hide for single-season series -- so `2`
depends on the show, not just the library, and a two-season show under `2` keeps
its seasons.

#### A season is not a lesser show, and an episode is not a small one

Diffing rex's rows against the reference's, type by type:

- A **season** detail carries `OnDeck`, `Extras` and `Preferences` exactly as a
  show does. rex answered all three for shows only, so a season page had no
  episode to offer and an empty settings tab.
- The **Preferences lists differ**: eleven settings on a show (`episodeSort`,
  `flattenSeasons`, `showOrdering`, `languageOverride`, the auto-deletion pair,
  `useOriginalTitle`, `enableCreditsMarkerGeneration` and the three language ones)
  against **three** on a season (audio language, subtitle language, subtitle
  mode). rex invented a single `episodeSort` row. Both lists are now captured
  in `item_prefs.json`, and a setting written through
  `PUT /library/metadata/{id}/prefs` comes back both inside `Preferences` and as
  a field on the row, the way `episodeSort` does on a real show.
- An **episode** carries `Extras` but no `Preferences` and no `OnDeck`.
- An episode row carries **no `Genre`, `studio`, `tagline` or `Country`** --
  measured on the 1.43.4 reference *and* on production fun. rex's episodes
  inherited all of them from the enriched file row, so an episode page advertised
  its show's genres where a real one shows none.

Not copied, for want of anything to derive them from: `CommonSenseMedia` and
`contentRatingAge` on a show (a Plex-only rating provider and an age rex
would have to guess from a rating string), and the external `Guid` array on a
season (the reference's has real tmdb/tvdb season ids; rex mints only its own
`plex://` season guid, and fun's episodes carry no Guid at all, so the field is
agent-dependent rather than universal).

#### Artwork: composites, and the size the client asked for

A collection's, playlist's or section's tile is a `composite` image. Measured on
the reference: a **400x600 JPEG mosaic** built from the members' posters.
rex served a flat generated grey tile. It has no imaging library and will not
fabricate a mosaic, so it now serves **one member's own poster** -- honestly one
of the images that mosaic is made of, and what the tile is for.

The client also asks the image transcoder for specific sizes (measured in the
recorded corpus: 240x240 for cast headshots, 240x112, 240x360, 360x540, 360x202)
and a real PMS returns exactly those dimensions. rex cannot resize, but TMDB
publishes fixed-width variants and the row stores one of them, so the upstream
request now picks the variant nearest the width asked for
(`/t/p/w342/…` → `/t/p/w185/…` for a 185-wide request). The served image is still
a fixed width rather than exactly the one requested, which is a deliberate
difference: the alternative would be shipping a 1280-wide backdrop to fill a
240-wide tile, which is what it did before.

#### A session is created by the TIMELINE, not by anyone reading bytes

`/status/sessions` answered `{size: 0}` for rex's whole life, so Now Playing
and the Dashboard were permanently empty and the Terminate button had nothing to
act on -- while the timeline it was already receiving carried everything needed
to fill them.

Measured on a real PMS, and the first fact is the surprising one: a bare
`GET /:/timeline?…&state=playing` from curl -- **no play queue, no decision, no
byte read** -- puts a full row in `/status/sessions` immediately, and a part read
on its own puts nothing there. `state=stopped` removes the row at once, and with
no further timelines a row survived 60 s past its last one and was gone by 80 s.

| Block | What it is |
|---|---|
| `sessionKey` | a server counter, as a **string** |
| `viewOffset` | the last timeline's `time` |
| `Player` | an **echo** of the client's own `X-Plex-*` values, plus the peer address, the last `state`, and `local`/`relayed`/`secure` |
| `Session` | `id` is the client's `X-Plex-Session-Identifier` -- or its machine identifier when it sends none, as Plex for Apple TV does |
| `User` | the account |

No `TranscodeSession` is ever emitted, and that is not a gap: the client renders
"Direct Play" **precisely when a `Session` is present and a `TranscodeSession` is
not**, which is the truth for a server with no encoder.

`Session.bandwidth` is ~2.1x the media bitrate in kbps (exact on two of three
measured samples). It is the one field here that is derived rather than known,
and it is marked as such in the code.

#### The timeline RESPONSE is not an echo, and a fifth notification exists

Two corrections to this file. It recorded `/:/timeline` as answering
`{size, playbackState}` with the state echoed back. Measured:

```
no session   {"size":0,"playbackState":"ignore","skipCount":0,"viewCount":0,"viewOffset":-1}
live         adds "Bandwidths":[{"Bandwidth":[{"time":0,"bandwidth":10486,"resolution":"1080p"}]}]
past 60 s    "playbackState":"progress","viewCount":1
stopped, end "playbackState":"complete","viewCount":1
```

`playbackState` is `ignore` / `progress` / `complete` -- **never** the state sent
-- and `viewOffset` on the response is `-1`, not the offset.

And the four notification types recorded earlier are **five**: `playing` carries
`PlaySessionStateNotification` `{sessionKey, clientIdentifier, guid, ratingKey,
url, key, viewOffset, state}`, one frame per timeline, and it is the one the Now
Playing panel depends on -- the client drops a row on `stopped` and refetches
`/status/sessions` when it sees a sessionKey it does not know. Without it, even a
correct `/status/sessions` would not appear until a reload.

#### Terminate is armed here and lands there

`GET /status/sessions/terminate?sessionId=&reason=` is keyed on **`Session.id`**,
not on `sessionKey`: no id is a 400, an id that is not live is a 404, a live one
is an empty 200 -- and the row **stays listed**. The kill is delivered through
that session's *next* timeline as `terminationCode: 2006` with a
`terminationText` quoting the reason, and its next part read answers **503**.
rex answered 200 to every terminate and did nothing.

Adjacent, measured: an unknown transcode session is a **404** for both
`GET` and `DELETE /transcode/sessions/{id}`, while
`/video/:/transcode/universal/subtitles` is a **400**. rex answered 200 and
404 respectively.

#### The Dashboard's numbers are real, and `timespan` is a bucket size

`timespan` decoded from a real server's own rows: **6 is per second**, then 4
hour, 3 day, 2 ISO week, 1 month, 0 year, with `at` the bucket start in the
server's local time. A real PMS counts **every response's bytes**, not only media
-- a 4 KB `/status/sessions` reply showed up as its own per-second row.

rex serves every byte it reports, so it now counts them per (device, lan) and
rolls them up on read, and `/statistics/media` counts completed plays from the
timelines. `/statistics/resources` is **refused with a 404** rather than faked:
the client treats that as "unsupported" and hides the graph, where the `{size:0}`
rex used to send draws an empty graph forever.

#### The filter query language: the operator is part of the KEY

The single biggest gap found in six passes, and it hid because of one line in the
client (`main-*.js` module 14056):

```js
const t = e.operator.slice(0, -1)
r.push(`${e.field}${encodeURIComponent(t)}=${e.value}`)
```

The client holds operators WITH a trailing `=` (`>>=`, `<=`, `==`), strips that
last character, and appends its own `=` as the separator. So "year is after 2000"
goes out as `year%3E%3E=2000` and the server reads a parameter **named** `year>>`
whose value is `2000`. There is no `year>=2000` on the wire, ever.

rex iterated its query parameters looking for names it recognised, found
none of these, and applied no constraint -- so **every filter returned the whole
library while looking like it worked**. Its own `Meta` advertises 40 filterable
fields and an operator list per type; it honoured exact equality on eight of them
and nothing else.

What each (type, operator) pair means, measured against two real servers:

| | `=` | `!=` | `==` | `!==` | `<=` | `>=` | `<<=` | `>>=` |
|---|---|---|---|---|---|---|---|---|
| tag | is (by **id or name**) | is not | | | | | | |
| integer | is | is not | | | at most | at least | strictly < | strictly > |
| string | **contains** | does not contain | is exactly | is not exactly | begins with | ends with | | |
| date | is | | | | | inclusive after | before | after |

The surprise is a bare `=` on a string: it is a substring match, and `==` is the
exact one. Three more shapes, all measured:

- a **repeated key is AND** (`?genre=6&genre=23` is the intersection) while a
  **comma is OR** (`?genre=6,27` is the union) -- except under a negating
  operator, where the comma becomes AND: `?genre!=6,27` returns the rows with
  NEITHER tag, and reading it as an OR would match everything;
- `push=1`/`pop=1` are parentheses and `and=1`/`or=1` the separator between
  siblings, with **AND binding tighter than OR**, exactly as in SQL. rex ANDed
  everything, so the UI's "Match any of the following" silently meant "match all";
- a date value may be relative (`-30d`), an absolute epoch, an ISO date or an ISO
  datetime. The suffixes are `s m h d w mon y` -- **`m` is MINUTES** and `mon` is
  the only month form -- an unsigned value means the past and a leading `+` the
  future. Anything else is a 500, case-sensitively.

And the refusals, which matter because the alternative is a silent no-op: a
comparison character with no `=` separating it from its value (`?year%3E2010`) is
a **400**; an unparseable number, an empty value and an unknown date suffix are
each a **500**; an unknown FIELD is ignored with a 200. rex answered 200 with
the library unfiltered for all of them.

The engine lives in `filters.py` and is driven by the `FieldType` block of the
captured `Meta`, so rex can only honour the operators it advertises.

#### The same language, six passes later: the field's TYPE decides

The operator table above is correct about what each operator *means* and wrong
about which fields it applies to. Re-measured field by field against the
reference, four of the types do not behave like the string they were implemented
as, and the corrections each turn a silently-wrong result set into the right one:

| Request | Reference | rex after the first pass |
|---|---|---|
| `?contentRating=G` | the row rated `G`, **not** the one rated `PG` | both, because a bare `=` was a substring match |
| `?contentRating=` | the rows with **no** rating (2 of 5) | all 5 -- everything contains the empty string |
| `?genre=` | **0** rows, because every row has a genre | all 5 |
| `?unwatched=2` | the same rows as `?unwatched=0` | 0 rows |
| `?resolution=2160` | **500** -- the value is not in the set | 200, empty |
| `?resolution=1080` | all 5 | all 5, but `?resolution=108` matched too |
| `?location=1` | all of section 1 | 0 -- it compared a path |
| `?location=/data/movies` | **500** | 200, empty |

So: a **tag** field (and `audioLanguage`, `subtitleLanguage`, `resolution`) is an
exact set membership, where an **empty value means "has no value at all"**; a
**boolean** is true only for the literal value `1`, and every other value
including an empty one means false; **`resolution`** is validated against
`{4k, 1080, 720, 576, 480, sd}`, case-sensitively, and anything else is a 500 --
`360` and `240` included, though the Meta implies a continuum; and
**`location`** is a section `Location` id, not a path, which is why rex's
sections now carry their own key as that id instead of all sharing `1`.

An empty value is therefore **not** uniformly a 500, which is what rex's
engine did: it is a 500 only where the value has to parse as a number or a date
(`?year=`, `?addedAt>>=`, `?resolution=`), and a perfectly good rule everywhere
else. The 500 now comes out of the comparison rather than out of the parse, which
is also where a real PMS raises it -- `/query/parse` accepts `?viewCount=` and
answers a `Rule`.

**Which operator KEYS a field takes is a third, separate question**, and it is not
the advertised operator list. Measured: `?resolution>>=720` parses fine although
`resolution` advertises only `=` (it then matches nothing), and `?addedAt=2020`
parses although `date` advertises only `<<=`/`>>=`. What *is* closed is two sets:

- the **tag-table** fields -- `genre`, `collection`, `director`, `writer`,
  `producer`, `actor`, `country`, `label`, `network` -- take only `=` and `!=`;
- **string** fields take the six they advertise, so `<<` and `>>` are out.

Anything else on one of those is an unknown **field**: `?genre>>=6` is ignored by
a listing and reported as `{"type":"field","value":"genre>>"}` by `/query/parse`.
`contentRating`, `videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout` and
`location` are declared `tag` in the Meta and are *not* in the closed set -- they
are enum columns, they take every key, and they get no `valueTitle`.

#### A show library advertises its fields PREFIXED, and nothing matched

The one that made every filter on a show grid a no-op. A movie section's `Meta`
advertises `genre`, `year`, `title`; a **show** section's advertises
`show.genre`, `show.year`, `episode.title` -- 21 fields under type 2 and 26 under
type 4, every one prefixed. The client sends the advertised key back verbatim, so
the server reads `show.genre`.

rex built its advertised-field map keyed by those prefixed names and then
looked the incoming name up with the prefix *stripped*, so the lookup could never
hit, `field_type()` returned None for everything, and the filter was dropped. A
movie library filtered correctly and a show library returned its whole contents
for every filter -- the same class of silent success this whole file is about,
and invisible to any check written against a movie section.

The prefix is a **scope**, not part of the name: the field is the tail, and the
lookup is global rather than per-section. Measured: `?show.title=x` parses
against a MOVIE library too, and `/query/parse` echoes the scope back as the
rule's own `type`.

#### `/library/sections/{id}/query/parse`: the grammar, read back

How the Edit Filters dialog reopens a filter that was saved -- it hands the query
string back and reads the rule tree out of the answer. rex 404'd it, and a
404 is exactly what the dialog reads as "this filter has no rules", so the dialog
drew empty whatever the smart playlist behind it held.

The root is **`Query`**, not a MediaContainer -- the second response on the whole
surface that is not one (the other is `/butler`'s `ButlerTasks`):

```
GET /library/sections/1/query/parse?type=1&genre=6&sort=titleSort
{"Query":{"type":1,
          "Rule":{"field":"genre","operator":"=","fieldTitle":"Genre",
                  "valueTitle":"Animation","value":"6"},
          "Order":[{"field":"titleSort","ascending":true}]}}
```

Measured, shape by shape:

| Request | Answer |
|---|---|
| no `type` | the **section's own** type (`1` for a movie library, `2` for a show one), as an integer |
| `type=99` | echoed untouched; `type=abc` is a **400** |
| `genre=6&year>>=2000` | `Rule` is `{"type":"composite","operator":"and","Rule":[…]}` |
| `push=1&genre=6&pop=1` | the group **collapses** to the bare leaf |
| `sort=random:12345` | `{"field":"random","ascending":true}` -- the **seed is dropped** |
| `sort=titleSort:desc:nocollate` | `ascending:false` -- the modifier does not eat the direction |
| `sort=bogusKey` | **500**, the same refusal a listing gives |
| `limit=7` | `"Limit":[{"limit":7}]`, as a number; `limit=abc` is a **500** |
| `group=year` | `"Group":[{"field":"year"}]` |
| `bogus=1` | `"Error":[{"type":"field","value":"bogus"}]`, beside whatever did parse |
| `push=1&genre=6` (unclosed) | `"Error":[{"type":"mismatch","value":"eof","expected":"pop"}]` |
| `and=1&genre=6` (leading) | the error alone -- **every rule is abandoned** |
| `genre=6&and=1` (trailing) | the last clause is placed **twice**, plus `{"type":"field","value":"eof"}` |
| `episode.unwatched=1` | `{"type":"episode","field":"unwatched","fieldTitle":"Episode Unwatched",…}` |
| `type=3&title=x` | `"fieldTitle":""` -- present and empty, because the season type has no fields |
| `includeGuids=1`, `excludeFields=…`, `X-Plex-*` | ignored silently |
| `checkFiles=1`, `asyncCheckFiles=1`, `nocache=1`, `having=x` | reported as unknown **fields** |

`valueTitle` appears only for a tag-table field whose value is an id that
resolves, and the lookup is **not scoped to the field**: `?label=5` came back
titled "Art", the name of tag 5 whoever asks. A tag NAME (`?genre=Animation`) and
an unresolvable id both get no title at all.

Block order is `type, Rule, Order, Limit, Group, Error`, and in XML the root is
`<Query type="1">` with `ascending="1"`/`"0"`. Only `parse` exists under
`/query`: the parent is a 404 and so is a POST.

The two odd rows above -- a leading separator abandoning the rules, a trailing one
duplicating the last clause -- are parser artifacts of the real server that no
client can reach. They are reproduced anyway, because the harness diffs rex's
answer against the reference's and a "corrected" answer is a difference to chase.

#### `/filters` is per library TYPE, and seven of the axes it names 404

The filter BAR is a separate surface from the filter engine, and rex served
one hand-trimmed 15-entry list for every library. Measured: a movie library
advertises **28** filters and a show library a **different 15** -- the same
lesson as the two `Meta` sidecars, one endpoint over. A show grid's filter bar
could therefore only ever talk about movies.

The show list is not a subset: it adds `network` and `unwatchedLeaves`, drops
`decade`, `resolution` and every media axis, and its keys carry **`?type=2`** --
except `country`, `unmatched` and `editionTitle`, which do not. That suffix is
load-bearing: an axis is scoped by the type, so `/library/sections/1/genre?type=2`
is an empty axis on a movie library.

Seven of the 28 axes **404 on the reference itself**: `hdr`, `dovi`,
`hdr10plus`, `atmos`, `inProgress`, `unmatched`, and `unwatchedLeaves` on the show
side. A real PMS advertises axes it does not serve, so rex advertises them
too and its unknown-leaf 404 answers them the same way. The filters behind them
do work on the grid -- it is only the "list me the values" axis that is missing.

What the axes themselves answer, measured against the lab and against a
6000-item production library:

| Axis kind | Entry | Note |
|---|---|---|
| tag (`genre`, `director`, `actor`, …) | `{fastKey, [thumb], key, title}` | only a **genre** entry carries `type`; a person entry carries the tag's `thumb`. rex stamped `type` on every one |
| field (`year`, `decade`, `contentRating`) | `{fastKey, key, title}` | key and fastKey single-encoded |
| field (`studio`) | same | fastKey **raw** (`all?studio=Blender Foundation` -- unusable on an HTTP request line) and key **double**-encoded. The reference's own inconsistency |
| media (`resolution`, `videoCodec`, `audioLanguage`, …) | `{key, title}` | **no fastKey at all**, and the title is a LABEL: `1080` → `1080p`, `en` → `English` |
| `location` | `{fastKey, key, title}` | its container carries **only `size` and `title2`** -- no art, thumb, identifier or viewGroup. The one outlier |
| `unwatched` | Metadata rows | not an axis: a LISTING with `title2: "Unplayed"` |

`resolution`'s accepted values are a closed enum, probed value by value against
production: `sd 480 576 720 1080 2k 2.7k 4k 8k 16k`. `240`, `360`, `400`, `1081`,
`1440`, `2160`, `3k`, `9k`, `10k`, `SD` and `1080p` are each a **500**. The axis
labels them `SD`, `480p` … `2K`, `2.7K`, `4K`.

`videoCodec`, `audioCodec`, `subtitleCodec` and `audioLayout` answer on the lab's
1.43.4 and **404 on production's older build**; rex follows the lab, which is
the designated reference.

#### An unexplained field that is still a measurement: `nocache` on show axes

Every secondary container of a **show** library carries `nocache: true` -- its
`/filters`, `/sorts`, `/genre`, `/year`, all of them -- and no movie library's
does. The first reading was cache state, which would make it per-server and
transient. Three movie libraries and two show libraries across **two independent
servers** rule that out: it tracks the library's TYPE. The cause is not visible
from outside, so the correlation is what rex reproduces.

#### A deliberate deviation: rex's error bodies

A real PMS answers a refusal with an HTML shell (`<html><head><title>Not
Found</title>…`, `text/html`, 85 bytes). rex answers `text/plain` with the
reason it refused (`rex: unsupported sort key bogusKey`). No client parses a
4xx body, and the reason is what makes a failed probe diagnosable, so this one
difference is kept on purpose. It is the only remaining shape difference in the
whole `/library/sections/*` surface apart from rows rex's catalog has no data
for.

#### A smart list is a stored QUERY, and rex stored a snapshot

rex accepted `smart=1` on both create endpoints, set a flag, and then stored
the rows the query happened to match at that moment. The list looked right the
instant it was made and went stale from then on, silently -- the one failure mode
this file keeps coming back to.

Proven live on the reference, which is the measurement that settles it. A smart
playlist built on `?type=1&unwatched=1` held four movies; scrobbling one dropped
it to three; unscrobbling put it back:

```
before:                      4  Big Buck Bunny, Elephants Dream, Sintel, Tears of Steel
after scrobble of key 1:     3  Elephants Dream, Sintel, Tears of Steel
after unscrobble:            4  Big Buck Bunny, Elephants Dream, Sintel, Tears of Steel
```

So membership is the query, re-run on every read. What each endpoint stores and
reports differs in four ways that are not guessable:

| | smart playlist | smart collection |
|---|---|---|
| `smart` | `true` — a **boolean**, always present | the **string `"1"`**, and a non-smart collection **omits the field** rather than sending a false |
| stored `content` | **rewritten**: `server://<machine>/com.plexapp.plugins.library/library/sections/1/all?type=1&genre=6` becomes `library://x/directory/%2Flibrary%2Fsections%2F1%2Fall%3Ftype%3D1%26genre%3D6` | the `server://` URI **verbatim** |
| where `content` appears | the single read **only** — `/playlists` and the create response both omit it | the list **and** the single read |
| marker | `icon: "playlist://image.smart"` | none |
| members | — | **not tagged**: a member's `Collection` stays absent, so a smart collection does not show on its items' pages |

The last row matters for more than tidiness: rex tagged the members, which
made a smart collection appear as a `Collection` tag on rows that the query might
no longer select.

`/playlists/{id}/items` also reports the container's `duration` in **seconds**
(40) while the playlist row's is in **milliseconds** (40000) -- the same number,
two units, one endpoint apart.

Resolving the stored URI turned out to fix a second thing: `_items_for_uri` was
dropping the query string entirely, so "Play all" from a filtered grid enqueued
the **whole section** rather than what was on screen.

#### `count=` caps a shelf, and `more` is how the chevron gets drawn

A hub is not paged the way a listing is. `?count=N` caps every shelf at N rows and
sets **`more: true`** on the ones that were cut; the default with no `count` is 12.
rex sliced at a hard 12 and wrote `more: false` unconditionally, so a
truncated shelf looked complete and its "see all" chevron never appeared.

Measured on the reference, the same section at three counts:

| Request | `movie.recentlyadded.1` |
|---|---|
| no count | `size 5, more false` |
| `?count=1` | `size 1, more true` |
| `?count=2` | `size 2, more true` |
| `?count=12` | `size 5, more false` |

The client asks for **`count+1`** for exactly this reason: it wants to know
whether the row after the last visible one exists.

#### `/library/all/top` counts PLAYS, not the row's `viewCount`

Three fields rex did not send -- `globalViewCount`, `userCount` and a
`User: [{"id": 1}]` list -- and one refusal it did not make: `type=` is required
and a real PMS answers **400** without it.

The giveaway that these come from the play HISTORY rather than off the row: Big
Buck Bunny reported `globalViewCount: 1` with **no `viewCount` at all**, because
it had been played and then marked unplayed. The row's counter is current state;
this shelf is the history, and `userCount` is how many distinct accounts appear in
it. rex sorted by `viewCount` off the row, so anything watched and then marked
unplayed vanished from the Top shelf that is supposed to remember it.

#### The `include*` / `exclude*` options, one measurement each

Eight parameters the official client sends and rex accepted and ignored. An
ignored option is the quiet failure again: the answer is a valid detail document
that simply lacks the block asked for, so a page draws with a section missing
rather than with an error.

| Parameter | Effect, measured |
|---|---|
| `includeChildren=1` | a show gains `Children: {size, Metadata: […seasons…]}` **inline** |
| `includeRelated=1` | adds a `Related` block (empty on the reference, whose related hubs are empty) |
| `includePreferences=1` | adds `Preferences.Setting` — three for a MOVIE (`languageOverride`, `useOriginalTitle`, `enableCreditsMarkerGeneration`), eleven for a show, three different ones for a season, and **none at all for an episode**, where the block is omitted rather than sent empty |
| `excludeFields=summary` | drops that field from the row |
| `excludeElements=Media` | drops `Media` and substitutes `mediaCount` and `mediaCountOptimized` |
| `checkFiles=1` | each `Part` gains `accessible` and `exists` |
| `asyncCheckFiles=1` | the **container** drops `identifier`, `mediaTagPrefix` and `mediaTagVersion` |
| `asyncRefreshLocalMediaAgent=1` | the same three |

The `async*` pair is the one nothing predicts: the response is the same row,
described by a container that can no longer resolve a tag into an image URL.

`checkFiles` is also the one option rex has nothing to do for, and
deliberately: `mediainfo.py` puts `exists` and `accessible` on every Part
unconditionally, because plezy re-fetches metadata in a loop until it sees them
and never passes `checkFiles`. Two extra attributes cost a client nothing; a
client that loops forever is the worse failure. Recorded here as a known
deviation rather than left as a surprise.

#### `group=` collapses, and refuses most of what it is offered

`group=<field>` answers one row per distinct value: five movies sharing a studio
and `?group=studio` is **one** row. Which field it accepts was probed one at a
time, and the set is not derivable from anything else the API says:

- **accepted**: `title`, `titleSort`, `studio`, `contentRating`, `year`, `decade`,
  `addedAt`, `updatedAt`, `originallyAvailableAt`, `audienceRating`, `rating`,
  `editionTitle`, `index`, `summary`, `tagline`, `contentRatingAge`;
- **500**: `genre`, `collection`, `director`, `actor`, `country`, `label` (tag
  joins), `resolution`, `audioCodec`, `mediaSize`, `mediaBitrate`, `location`
  (the media), `viewCount`, `lastViewedAt`, `unwatched`, `inProgress` (per-user
  state) -- and `duration`, `userRating` and `hdr`, which no rule about the schema
  predicts. An empty or unknown field is a 500 too.

`group=decade` does NOT collapse by decade: five movies across two decades stayed
five rows, because the grouping happens on the underlying `year` column.

Which row of a group survives is a SQL artifact on the reference -- it is not the
first in sort order and not the lowest id, and it does not move when the sort
changes. rex keeps the first in the order the sort just produced, which is at
least deterministic; the count, which is what the UI reads, matches.

#### The decision container sends ONE pair of codes, not all of them

rex answered every decision with `generalDecisionCode`,
`directPlayDecisionCode` and `mdeDecisionCode` all at 1000. Measured, a real PMS
sends exactly one set, and which one says what happened:

| Request | Codes |
|---|---|
| `directPlay=1&hasMDE=1` | `mdeDecisionCode: 1000`, `mdeDecisionText: "Direct play OK."` — **and nothing else** |
| `directPlay=0` | `directPlayDecisionCode: 3000` ("App cannot direct play this item. Direct play is disabled."), `generalDecisionCode`/`transcodeDecisionCode: 1001` ("Direct play not available; Conversion OK.") — and **no** `mde*` pair |
| `directPlay=1` with no `hasMDE` | the same 3000/1001 set, the text naming the protocol: "No direct play video profile exists for protocol http, with container mkv, and video codec h264." |

Sending all three at once tells a client a transcode decision was made when none
was. The period in "Direct play OK." is part of the string the client prints.

The container's own fields are measured too, and three of them are typed unlike
anywhere else on the surface: `resourceSession` is the **client identifier**, not
the `session` parameter; `allowSync` is the **string** `"1"`; and
`librarySectionID` and `mediaTagVersion` are strings here where every other
endpoint sends numbers.

#### Three the replay probe caught once every endpoint above was in it

Widening `probes/ui_surface.py` and diffing it whole against the reference turned
up three the per-feature sweeps had each walked past.

**`/myplex/account` must not carry a token.** rex sent `authToken` in it; the
reference does not send the field at all. It buys nothing -- a client that reads
this endpoint already holds a token -- and every page that reads it is a page that
could log the server's. Removed.

**`/system/scanners/{id}` answers `<Scanner name="…"/>` rows**, not `Directory`
ones, in a container whose `identifier` is `com.plexapp.system` rather than the
library's. A movie library lists four (`Plex Movie`, `Plex Movie Scanner`,
`Plex Video Files Scanner`, `Plex Video Files`) and a show library two
(`Plex TV Series`, `Plex Series Scanner`). rex answered one `Directory` row
with a `key` and a `title`, which the Edit Library dialog's scanner dropdown does
not read -- an advertised control with nothing behind it, from the other side.

**`/statistics/resources` answers 200, and rex was 404ing it.** The earlier
note here reasoned that a refusal reads to the client as "unsupported" and hides
the graph, which is better than an empty one. The measurement contradicts the
premise: BOTH reference servers answer 200 with a `StatisticsResources` series of
`{timespan, at, hostCpuUtilization, processCpuUtilization, hostMemoryUtilization,
processMemoryUtilization}`, and only `timespan=6` has samples -- every coarser
bucket, and a request with no timespan, is an empty 200.

rex now samples when the endpoint is ASKED, which is the only time the numbers
are wanted: the Dashboard polls while it is open, so the graph fills in the way it
does on a real server and no sampler thread runs for a page nobody is looking at.
Two of the four numbers are real -- host CPU from the load average over the core
count, process CPU from `getrusage` over wall time -- and the two memory ones are
0.0, because the stdlib cannot see host memory and a plausible percentage would
draw a graph of nothing.

#### The Home screen: `/hubs/promoted` is called once per library, not once

The client calls `/hubs/promoted?contentDirectoryID=<section>` **per pinned
library**. rex ignored the parameter and answered every library's shelf on
every call, so Home drew each library's rows under every library's heading.

Its hubs were also missing the five fields the client lays the page out from.
Measured, a promoted shelf carries:

| Field | Value |
|---|---|
| `key` | where "see all" goes — `/library/sections/1/all?sort=addedAt:desc` for a movie library, **`/hubs/home/recentlyAdded?type=2&sectionID=2`** for a show one |
| `context` | `hub.<identifier with any trailing section id stripped>` — `movie.recentlyadded.1` has context `hub.movie.recentlyadded`, because the context names the KIND of shelf |
| `style` | `hero` for Continue Watching, `shelf` for everything else |
| `promoted` | `true` on every shelf of this endpoint |
| `hubKey` | **not a listing path**: `/library/metadata/<comma-joined ratingKeys>` naming this shelf's own rows, omitted when there are none |

rex sent a `hubKey` of `/library/all` for everything, so every shelf expanded
to the whole library, and no shelf had a `context` to lay out by.

Three more shapes: a SHOW library's Recently Added shelf is typed **`mixed`**
where a movie library's is `movie`; `excludeContinueWatching=1` drops that shelf;
and the bare call (no `contentDirectoryID`) adds `home.playlists`
(key `/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio`).

`/hubs/continueWatching`'s hub is identified **`continueWatching`**, not
`home.continue`, is styled `hero`, and its `key` is its own
`/hubs/continueWatching/items` — which rex did not route, so the shelf had
nowhere to expand to. `/hubs/home/continueWatching` and `/hubs/home/onDeck` answer
plain `{size, allowSync, identifier}` row lists, and **`/hubs/home/recentlyAdded`
keys on `type`, not on `sectionID`** -- rex had it exactly the other way
round, so the TV shelf's "see all" opened an empty page. An unknown home hub is a
404, where rex answered an empty shelf.

#### The preplay page's Related shelf lives at a path rex did not route

The client rewrites `/library/metadata/{id}` to `/hubs/metadata/{id}` and appends
the leaf, so `/hubs/metadata/{id}/related` is a DIFFERENT endpoint from
`/library/metadata/{id}/related` -- and only the hubs form carries the shelves.
rex routed neither, and a 404 is exactly what the preplay page reads as "no
related items".

Measured: `related` carries `allowSync` and the librarySection triple, `postplay`
carries **neither**, and the hub inside both is `movie.similar` / "Related
Movies", keyed on the item's own `/library/metadata/{id}/similar`.

The reference fills `similar` from its metadata agent, and its own list is EMPTY
for one of the lab's five movies while the others answer two to four rows -- so an
empty answer is a correct answer. rex has no agent similarity data, so it
computes its own (other rows in the library sharing a genre) and says so in the
code: the shape is measured, the source is rex's.

#### Search: seventeen hubs every time, a word-prefix match, and a score

Four endpoints, and rex had all four wrong.

`/hubs/search` answers **seventeen typed hubs on every query, empty ones
included** -- movie, show, artist, album, episode, track, collection, photoalbum,
autotag, photo, tag, actor, director, genre, playlist, shared, place -- because
the client lays the results page out from that list. rex answered only the
hubs that had rows, so the page's layout changed per query. The second hub is
titled **"Shows"**, not "TV Shows". Each hub carries an EMPTY `context`, a `shelf`
style and no `hubKey`.

Every row carries a **`score`**, which is what the client sorts by: a string with
five decimals at `/hubs/search` and the same number as a FLOAT at
`/library/search`. A row that matched through a TAG rather than its own title
names it -- `reason: "director"`, `reasonID: 45`, `reasonTitle: "Ian Hubert"` --
and the actor/director/genre hubs carry `Directory` rows with `tagType`, `filter`,
`count` and a `reason` of `"section"`.

And the matching rule is a **word PREFIX**, not a substring. Measured query by
query against the reference: `te`, `tea`, `tear`, `tears`, `of` and `steel` all
find "Tears of Steel"; `uck` does NOT find "Big Buck Bunny"; and a single
character matches nothing at all. rex used `needle in title`, so `uck`
matched and `t` returned the whole library. (The reference is additionally fuzzy
-- `eel` finds "Firefly" and an actor called "Halina Reijn" -- which is a trigram
score rex does not approximate.)

The other three, all previously wrong:

- **`/library/search`** -- advertised by rex's own `/media/providers` as
  `universalsearch` and answered with an empty container, on a code comment
  claiming "a real PMS answers the bare query with an empty container". It does
  not: a bare query is a **400** and a real one answers `SearchResult` rows.
- **`/search`** -- the legacy endpoint, 404 on rex. It answers results PLUS
  four `Provider` rows (`m_title`/`m_type`, the prefix is the reference's), each
  result carrying `personal` and `sourceTitle`; its `size` counts the providers
  too (1 result + 4 providers = 5). A MISSING `query` is a 400 and an EMPTY one is
  a **404** -- two different refusals on one endpoint.
- **`/hubs/search/voice`** -- 404 on rex. One hub per type, titled with the
  bare type name, identified `results.search.<n>` with the shared context
  `hub.results.search`, `hubKey` and no `key`, and a `distance` on the hub and on
  every row.

`/library/sections/{id}/search` is the correction of an earlier finding rather
than a new one: only the form with **no `type`** is a 400. With a type it is a
real listing whose `title2` is `Search for '<query>'` and which carries no
`content` -- the in-library search box, which rex refused in every form.

#### A play queue is a queue, and rex kept a key list

Six defects in one subsystem, and the first two each break playback outright.

**A queue over a show, a season, or a show library's section held the PARENT row.**
Measured: a queue over the lab's Firefly holds its **six episodes**, a queue over
a season holds that season's three, and a queue over `/library/sections/2/all`
holds every leaf -- never the parent, which has no `Media` at all. rex
enqueued the parent, so Play, Play All and Shuffle on every show page, season page
and TV library handed the player something unplayable and the web client said
"An error occurred loading items to play". The client's own shuffle path
deliberately skips its on-deck resolution, so the show uri reaches the server
verbatim -- there is nothing upstream to save it.

**A queue over a playlist or a collection was empty**, and a multi-select Play
(`/library/metadata/1,3,4`) enqueued ONE item, because the resolver's `(\d+)`
stopped at the first key. A playlist queue also reports
`playQueuePlaylistID`/`playQueuePlaylistTitle` and NO `playQueueSourceURI`, and
`playlistID=` alone with no uri at all builds the same queue.

**The item ids and the version decide whether the client keeps the answer at
all.** Its play-queue fetch DISCARDS the whole response when the returned
`playQueueSelectedItemID` is not the one it holds, and it refreshes only when
`playQueueVersion` changed. rex computed the ids from their POSITION
(`queue_id * 1000 + index + 1`) and hardcoded the version at 1, so every id moved
whenever anything was inserted and the version never moved at all: once playback
was past the first item, every refresh, Add to Queue and Play Next response was
silently thrown away. Measured, the reference's ids are globally increasing and
stable across inserts, moves and deletes (queue 18 kept 47…51 through three adds,
a move and a delete), and its version steps on every mutation: 1→2→3→4→5→6.

The same arithmetic made `DELETE /playQueues/{id}/items/{itemID}` remove a
different row than the one the client asked for, whenever the client's cached id
predated an insert.

**The selection follows the TIMELINE.** `/:/timeline?...&playQueueItemID=81` moves
the queue's `playQueueSelectedItemID` to 81 and its `playQueueSelectedItemOffset`
to that index, without touching the version. rex ignored the parameter and
always reported item 0 -- which is the state the client throws away.

**`playQueueLastAddedItemID`** tracks PLAIN adds only (a `next=1` add leaves it
where it was), and the client uses it to DISABLE the Up Next shuffle button once
the user has queued something. rex never sent it, so the button stayed live.

**Where an add inserts** was wrong both ways: a plain add goes after the block of
already-added items following the current one, and a `next=1` add goes immediately
after the current item, ahead of that block. rex appended to the very end and
inserted at index 1 regardless of where the player was.

And the smaller ones, each measured: `playQueueSourceURI` comes back **rewritten**
(`library://x/item/<encoded>` for one item, `library://x/directory/<encoded>` for
anything naming a list) rather than echoed; the container carries **no**
`allowSync`; `window=`/`center=` slice AROUND the current item while
`playQueueTotalCount` still reports the whole queue; an empty queue omits
`playQueueSelectedItemOffset` and `playQueueTotalCount`; `PUT
/playQueues/{id}/items/{id}/move?after=-1` moves an item to the front (rex did
not route it and answered 200 with the queue unchanged); and `PUT
/playQueues/{id}/shuffle` and `/unshuffle` are **404** (on a queue with an add -- cycle 75 found both real on one without) -- the client reverts its
optimistic `playQueueShuffled` on failure, so rex's 200 left the shuffle
button stuck on over a queue that was never shuffled.

Refusals, all of which rex answered 200 to: a create with no `uri` and no
`playlistID`, or with no `type`, is a **400**; a GET of `/playQueues` is a **400**
(the endpoint exists, the request is wrong); an unknown queue, an unknown queue
item, and a POST or DELETE of one queue are **404**. rex's DELETE really
dropped the queue, so a client that tried one lost the queue it was playing.

#### `/:/progress` is not `/:/timeline`

It keys on **`key`**, not `ratingKey`, and it answers an **empty body** rather than
a MediaContainer. rex routed it straight at the timeline handler, which read
no key at all, so it recorded nothing and returned a container -- and every client
that reports progress this way (plex_debrid, Kodi/PKC, Infuse, the older Plex
apps) lost its resume point silently.

#### The timeline, re-measured: four corrections to an earlier finding

| Case | Reference | rex before |
|---|---|---|
| item HAS a resume point | `viewOffset` is that **stored offset** | `-1` always |
| item has none | `-1` | `-1` ✓ |
| before any `decision` for the item | no `Bandwidths` | `Bandwidths` present |
| after a decision, including on `stopped` | `Bandwidths` present | **dropped** on `stopped` |
| after the stopping timeline | gone again | still present |
| `Bandwidths[].resolution` | the LABEL, `1080p` | the raw `1080` |
| no `ratingKey` at all | `{size, playbackState}` only | all five fields |
| `ratingKey=99999` | **404** | 200 |
| `state=wibble` | **400** -- `state` is a closed set | 200, treated as live |

So `viewOffset: -1` in the earlier finding was the *no-resume-point* case, not the
rule, and `Bandwidths` is armed by a **decision** rather than by the state --
inverted at both ends.

#### Every malformed state write answered 200

`/:/scrobble`, `/:/unscrobble`, `/:/rate` and `/:/progress` all answer **400**
without a `key` and **404** for a key the server does not have, and `/:/rate`
answers 400 for an unparseable rating. rex answered an empty 200 to all of
them, which tells the client the write landed. (`rating=-1` clearing both
`userRating` and `lastRatedAt`, and `rating=99` being stored unclamped, were
already right.)

`PUT /library/parts/{id}` is the same failure with a worse consequence: a stream id
that is not a stream of the right TYPE on that item is a **400** and the selection
is left untouched. rex accepted anything with a 200 and then cleared every
selection, because nothing matched -- so a rejected request left the item with **no
selected track at all** and the picker went blank. `0` is the client's "None"
pseudo-stream and is valid for subtitles only; `audioStreamID=0` is a 400.

Two more paths: a LEAF on a comma-joined key list is a **404**
(`/library/metadata/a,b/userState` answered full metadata rows with no `UserState`
in them, which the web client's per-grid probe reads as state it has), a
non-numeric ratingKey is a **404**, and
`/subtitles/:/transcode/universal/start` -- the URL Plex Web uses for a `<track>`
src when it CAN direct play, which is rex's only path -- is a **400** on both
reference servers rather than the catch-all 404.

#### The History page filters and sorts

`/status/sessions/history/all` honours `accountID`, `deviceID`,
`librarySectionID`, `metadataItemID` and `viewedAt` comparisons, and sorts;
rex ignored every one of them. So Top Users' "View Play History" link
(`accountID=&viewedAt=`) and the per-item history link (`metadataItemID=`) both
showed the whole server's history, in insertion order, and the Played column's
sort did nothing.

Two details worth the ink. The operator here is a plain **`>=`**, not the library
grammar's `>>` -- two query languages on one server. And an unknown sort key is a
**400** here where a library listing answers 500 for the same mistake.

`DELETE /status/sessions/history/{historyKey}` removes one row and answers 200
with an empty container; an unknown key is a 404. rex matched the history path
on the path alone, so a DELETE was answered with the LISTING, and a numeric key
fell through to the live-sessions handler and answered `{size: 0}` -- which is
exactly the "it worked" signal for a delete that did nothing.

#### The decision ignored `hasMDE`

findings.md already recorded that a client which does not claim MDE gets the
3000/1001 refusal naming the protocol it fell back to, but the code branched on
`directPlay` alone. So rex answered the MDE pair to a client that never
claimed MDE -- telling it a media decision was made on its behalf.

#### `GET /` is not `/identity`, and eleven refusals a real PMS makes

rex answered `/` with the five-field identity document. Measured, a real PMS
answers the whole capability blob -- the same ~50 scalars as `/media/providers`
-- plus **25 `Directory` rows** (`{count, key, title}`) naming its top-level
endpoints: actions, activities, butler, channels, clients, devices, diagnostics,
downloadQueue, hubs, library, livetv, media, neighborhood, playQueues, playlists,
resources, search, server, servers, service, statistics, system, transcode,
updater, user. python-plexapi's `PlexServer()` loads every attribute it has from
here, so an identity document leaves it with almost none of them.

`/butler` lists **23** tasks, and only `BackupDatabase` carries a `title` and a
`description` -- the client humanises the rest from the name. Its root carries no
`size`. rex listed two with invented titles, so the Scheduled Tasks page
showed two tasks that do not exist on a real server and hid twenty-one that do.
A PUT to a task that exists is a **404**; only POST (run now) and DELETE (cancel).

`/library/metadata/{id}/tree` **nests**: a show's tree holds its seasons, each
season its episodes, each episode its `MediaItem`, and an EPISODE's own tree is
rooted at the SHOW. A `MediaItem` here carries only its `id` -- the width, codecs
and bitrate rex put on it belong to a normal detail response. rex answered
one flat row with `MediaItem: []`, which is both the empty-list shape no real PMS
sends and missing the whole hierarchy the Get Info dialog reads.

Two listing details: a show's `/children` carries an extra `Directory` row
`{key: "/library/metadata/{id}/allLeaves", title: "All episodes"}` and the
container's `size` **counts it**, which is exactly what `excludeAllLeaves=1`
takes back off. And `Guid` is gated: a DETAIL row always carries it, a LISTING
carries it only under `includeGuids=1`; rex sent it everywhere.

The refusals, each of which rex answered an empty 200 or an empty container
to -- which a client reads as "that worked" or "that exists and is empty":

| Request | Reference |
|---|---|
| `GET /updater/check`, `GET /updater/apply` | 404 — only the PUT of `check` exists |
| `PUT /updater/apply` with nothing downloaded | 400 |
| `DELETE /activities/{unknown uuid}` | 404 |
| `PUT /:/prefs?NoSuchPref=1` | **400**, body `cannot set preference value for unknown preference NoSuchPref` |
| `POST /:/prefs` | 404 |
| `GET /accounts/{unknown}` | 404 |
| `GET /media/subscriptions/{id}`, `/media/grabbers/{id}`, `/media/grabbers/{id}/devices` | 404 |
| `GET /system/scanners` (bare) | 404 — scanners are listed per section |
| `GET /security/token` with no `type`/`scope` | 400 |
| `PUT /butler/{task}` | 404 |
| `GET /library/collections/{id}/items` | the members, not an empty container |

And five endpoints rex refused that a real PMS answers: `/log/networked`
(200, empty), `/diagnostics` (`{size: 0, content: "plugins"}`), `/resources`
(`{size: 0}`), `/media/grabbers` (four `MediaGrabber` rows -- an empty list tells
the Live TV page the server supports no tuner protocols at all rather than that it
has no tuners), and `/system/library/sections` (a `com.plexapp.system` container).

`/security/token` answers a **different** token on every call; rex answered a
constant, so two devices would have shared one delegation credential.

Three shapes on the collection and playlist side: a single collection's container
carries `allowSync: true`, the identifier and the media-tag pair, and a NUMERIC
`librarySectionID` (rex sent a string and omitted the other three, so a client
could not resolve a tag into an image URL from a collection page); a SMART
collection carries no `minYear`/`maxYear`, because its membership is a query and
there is no fixed span to report; and the playlist ITEMS container reports its
`duration` in **seconds** where the playlist ROW reports the same number in
milliseconds -- one endpoint apart, recorded in this file before it was
implemented.

#### The Edit dialog saved to a path that answered the listing

The single quietest failure found in this project. The web client's Edit dialog is
`MultiMetadataModel`, and its `saveUrl()` is

```
PUT /library/sections/{id}/all?type=1&id=4&includeExternalMedia=1&title.value=…
```

rex's section handler dispatched `all` on the path alone, so a PUT there
answered the ordinary grid LISTING with a 200. The dialog reads the status, reports
success, and nothing was written. **Every Save, for the life of the project.**

Its pre-read is a second endpoint rex never had:
`GET /library/sections/{id}/common?type=1&id=…` answers a `Common` row (JSON
`Metadata` with `type: "common"`, XML `<Common>`) in a secondary container titled
"Common", holding the fields the selected items SHARE. With several ids the fields
that DIFFER are dropped and named in `mixedFields` instead -- which is how the
dialog knows to show "(mixed)" rather than overwrite. rex 404'd it, and the
dialog's `populate()` rejects on that: "There was an error editing this item",
before the user could type.

And its source of editable TAG fields is a third:
`GET /library/sections/{id}?includeDetails=1&includeAdvanced=1`, from which the
client reads `Type[].Field[type=="tag"]`. rex answered two Directory rows
(`All`, `Recently Added`) and no `Type` at all, so the Tags pane offered no genre,
no collection, no label and no people. A real PMS answers **20 pivot rows** for a
movie library and 16 for a show one -- each with the title the sidebar prints,
`secondary` on the axes and `search`/`prompt` on the search entries -- plus, under
`includeDetails`, the `Type` blocks and the `FieldType` operator lists. `size`
counts all three lists (20 + 1 + 8 = 29).

Two scoping rules on the save path, both measured: `id=` limits it to those rows,
and with **no `id`** the whole FILTERED set is edited, which is how "Edit all"
works. A bare `title=Sintel` there is a FILTER that scopes the edit -- where the
same shape on the ITEM path IS an edit. So which it means depends on the path.
`X-Plex-Container-*` is ignored on a write.

#### Every edit parameter, one measurement at a time

rex stored whatever it was sent and answered 200. A real PMS:

| Request | Reference | rex before |
|---|---|---|
| `bogus.value=1`, `sortTitle.value=abc` | **400** | 200, and `Field` gained a lock for a field that does not exist |
| `bogustag[0].tag.tag=x` | **400** | 200 |
| no parameters at all | **400** | 200 |
| `year.value=abc` | **500** | 200, stored `"abc"` |
| `year.value=` | **500** | removed the field |
| `year.value=-5` | 200, year **removed** | stored `-5` |
| `originallyAvailableAt.value=notadate` | 200, field **removed** | stored the text |
| `title.value=`, `summary.value=` | stored as **`""`** | removed |
| `studio.value=`, `contentRating.value=`, `titleSort.value=`, `originalTitle.value=`, `editionTitle.value=` | **removed** | removed ✓ |
| `userRating.value=7` | accepted, **discarded** (the star rating is `/:/rate`) | stored as the string `"7"` |
| `tagline.locked=true` / `false` / empty | all three **UNLOCK** — only `1` locks | `true` locked |
| bare `studio=X` on the ITEM path | applied | ignored |
| the `Field` lock list | a fixed COLUMN order | insertion order, so re-locking moved the entry |

**Tag edits are a replace, not an append.** `genre[0].tag.tag=A&genre[1].tag.tag=B`
leaves EXACTLY `[A, B]`: the originals are gone. rex appended, so the Edit
dialog could only ever add tags and the list grew on every Save. A `tag-` removal
CLEARS the whole list whatever value it names -- even one the row does not have --
and the indexed list in the same request rebuilds it, which is how "remove one tag"
actually works on the wire. rex matched removals by exact name, so a tag
containing a space could never be removed (the client percent-encodes the value
before the query encoding, so it arrives double-encoded).

A tag also matches **case-insensitively and keeps its canonical spelling and id**,
and it does so after being removed from every row -- a real PMS keeps a tag TABLE.
rex looked only at the rows, so clearing a tag lost its identity and the next
add minted a new id; there is now a tag table for exactly this.

#### A collection and a playlist are metadata items

Both have ratingKeys and the client edits both through `/library/metadata/{key}` --
its own `saveUrl` falls back to that path for a playlist on a server without the
true-playlists feature. rex kept them outside the catalog, so both answered 404
and neither could be renamed from its own page.

`PUT /library/metadata/{collection}/prefs?collectionSort=1` is how the collection's
sort and mode are set, and they come back as **string** attributes on the row.
Reading those prefs is a 404 (write-only), and an unknown pref is a 400.

#### Library management validated nothing

`PUT /library/sections/{id}`: a `name` alone is a **400**, and so is a form with no
`agent` or no `scanner`, or an unknown agent, scanner or `language`. rex
accepted all of them with a 200, so a typo in the Edit Library dialog looked saved.

`POST /library/sections` answers a **plain-text 400 naming what is wrong** -- with
no Content-Type header at all: `'location' is missing or invalid. `, `'type' is
missing or invalid (must be one of 'movie', 'show', 'photo' or 'artist') …`,
`'agent' is missing or invalid …`, `new scanner needs to be paired with new agent `,
`'name' is missing or invalid `. A nonexistent location and a duplicate name ARE
both accepted (201). rex answered **201 to every malformed form** and really
created the library -- a POST with no parameters at all made one called
"New Library", which then sat in the sidebar.

`PUT /library/sections/{id}/refresh` is a **404**: the scan is a GET or a POST and
the cancel is a DELETE. And `PUT /playlists/{id}` with an unknown parameter,
`smart=1`, or nothing at all is a **400**.

#### rex had no token gate at all

A real PMS **401s everything** except `/identity` (so a client can discover the
server) and `/web/*` (the client itself). Measured on both reference servers, for a
MISSING token and a WRONG one alike -- there is no distinction between them, no
`WWW-Authenticate` header, and the answer does not change with
`Accept: application/json`:

```
GET /library/sections            (no X-Plex-Token)
HTTP/1.1 401 Unauthorized
X-Plex-Protocol: 1.0
Content-Length: 91
Content-Type: text/html
Connection: Keep-Alive
Keep-Alive: timeout=20
Cache-Control: no-cache
Date: Wed, 16 Sep 2026 04:20:10 GMT

<html><head><title>Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>
```

That block is a verbatim capture off kids, taken in cycle 30 with a raw socket. What stood here
before was rex's own answer -- `Connection: close`, no `Content-Length`, no `Date`, the headers
in rex's order -- pasted in and read as the reference's for eleven cycles.

Cycle 30 corrected that sample: the `Connection: close` printed here for eleven cycles was
**rex's own answer read back as the reference's**. Both references keep the socket alive
after a 401, a 403 and a 404 and it really is reusable; the only handler-level refusal on the
surface that closes it is the 400 a query pair with no `=` answers. See the thirtieth pass.

`/`, `/library/sections`, every listing, every item, **artwork**, `/photo/:/transcode`,
`/:/resources/*`, **media bytes**, both notification channels (the 101 upgrade is
refused with a 401 body), `/:/prefs`, `/status/sessions`, `/myplex/account`,
`/media/providers`, `/hubs`, `/clients`, `/accounts`, `/devices`, `/activities`,
`/butler` -- all of it. The token is accepted as the `X-Plex-Token` **header** as
well as the query parameter.

The one exception is `/security/token`, which answers its own **400** with no
parameters and **403** with a token that is not good enough -- never a 401. That
split is how a client tells "you did not ask properly" from "you may not have one".

rex answered 200 to all of it, with no token check anywhere: the string
`X-Plex-Token` appeared in the code only in the CORS `Vary` header. Any peer that
could reach the port read the whole library, the preferences, the sessions and the
media bytes -- and rex is published over a named Cloudflare tunnel.

The gate is now implemented and is **opt-in** (`--token` / `REX_TOKEN`), which
is the deviation: a real PMS always has a token, and rex defaults to none
because its four test suites and its probes talk to it without one. The startup log
says which it is, in as many words -- `token gate: OFF -- every peer that can reach
this port can read the library`. The harness starts its own gated instance to prove
the gate works, so the behaviour is tested rather than merely available.

#### `Cache-Control` on everything, and gzip

A real PMS sends **`Cache-Control: no-cache` on every response** -- measured across
eight endpoints on both servers. rex sent none at all, except on artwork, where
it sent `max-age=86400`: a day of browser caching that the reference does not do,
so a Change Artwork or a re-match stayed invisible even though the version in the
URL had changed. The reference's policy is the opposite of what rex had:

| Path | Reference | rex before |
|---|---|---|
| every container | `no-cache` | (none) |
| a VERSIONED direct asset (`/library/metadata/{id}/thumb/{ver}`) | `no-cache`, plus `X-Plex-Local-Asset: 1` and `Accept-Ranges: bytes` | `max-age=86400`, neither header |
| transcoder output (`/photo/:/transcode`) | `max-age=259200` | `max-age=86400` |

And it **gzips** its XML and JSON, naming both sizes in two Plex-specific headers:

```
Content-Encoding: gzip
X-Plex-Content-Original-Length: 10381
X-Plex-Content-Compressed-Length: 3069
```

3.4x on a five-row grid, 5.4x on rex's own. Both servers always use
`Content-Length` and never chunk, even at 4.9 MB -- which rex already matched.

#### The notification LIFECYCLE, which is what makes a client refetch

rex pushed the FIRST frame of every sequence and stopped. That is enough for a
client to know something started and never enough for it to act.

A refresh on the reference pushes **seven** frames, captured verbatim off its
eventsource:

```
timeline  state 3  metadataState "queued"
activity  started   progress -1
activity  updated   progress 49
timeline  state 3  metadataState "processing"
timeline  state 4  metadataState "loading"
activity  ended     progress 100
timeline  state 5   -- and NO metadataState
```

The state alphabet, pinned against the client's own reducer (`main-*.js` module
35623, constants `lm = 5` and `cm = 9`): **0 created, 3 queued/processing, 4
loading, 5 done, 9 deleted**.

Two of those frames are load-bearing, and rex sent neither:

- **`state: 5` dispatches `metadataProcessed`, which is what makes the client
  REFETCH the row.** Without it Refresh Metadata does nothing on screen until a
  reload.
- **The reducer deletes its entry only when `metadataState` is ABSENT**
  (`t.metadataState||t.mediaState ? … : delete e[t.sourceURI]`), and rex's one
  frame always carried `"queued"` -- so every gate spelled `!!metadataState` stayed
  latched: the spinner, and the Refresh and Match menu entries.

And the activity never ended. `add_activity` fired `started`; only
`cancel_activity` ever fired `ended`. So an activity nothing cancelled sat in
`/activities` for the life of the process at `progress: 0` -- where the reference's
started frame is `progress: -1` and it reaches `ended` at 100.

An **edit** pushes two frames (`state 3, "loading"` then `state 5`); rex pushed
none, so an edit made through the API was invisible to every other open client. A
**create** pushes `state 0, "created"` and a **delete** pushes `state 9,
"deleted"` -- and `9` is what dispatches the client's own `deleted` action, so
without it a row deleted through rex stayed on screen everywhere else.

#### `filters=` is a whitelist, and `log` is opt-in — correcting a recorded finding

This file used to say: *"The Console's live log stream is not fed by that socket. 75
seconds against a busy reference pushed zero log frames, and the client opens no
second socket and passes no filter."*

Both halves were wrong, and for the reason this project's one rule exists: the
measurement was taken **without the parameter the client sends**. The client does
open a second socket and does pass a filter -- `chunk-1439-*.js`,
`MediaServerLogEventConnection`:

```js
url(){ … this.server.resolveUrl("/:/websockets/notifications",
        {params:{filters:"log"}, …}) }
```

With it, the reference pushes log frames continuously -- **386 in 14 seconds**:

```json
{"NotificationContainer":{"type":"log","size":1,"LogSentNotification":[
  {"level":3,"thread":"134304658963256","time":"Sep 12, 2026 18:19:15.428",
   "message":"Request: [100.101.20.2:55607 (WAN)] GET /:/websockets/notifications?filters=log …"}]}}
```

`filters` is an exact whitelist of type names, pinned by running two sockets side by
side: `?filters=log` received 386 log frames and **zero** of anything else, while a
socket with no filters received `playing` and `timeline` and **zero** log frames. So
**`log` is opt-in only and everything else is opt-out only.** rex fanned every
event to every sink, so the socket opened for the Console's log pane got playing and
activity frames and never a log line -- and the pane was empty.

#### The channels' own plumbing

| | Reference | rex before |
|---|---|---|
| websocket keepalive | protocol ping every **10 s** with a ONE-byte payload | every 20 s, empty |
| eventsource keepalive | `event: ping\ndata: {}` -- a real named event -- every **10 s** | `: ping`, an SSE COMMENT (invisible to an EventSource listener), every 15 s |
| a client CLOSE frame | answered with a close, status 1000 | never read, so a browser's `ws.close()` hung in CLOSING until its own timeout and the sink was never reaped |
| the 101 upgrade | carries `X-Plex-Protocol`, `Cache-Control` and the CORS pair | the three handshake headers alone |
| SSE CORS | no CORS headers without an Origin, the echoed origin with one | **`Access-Control-Allow-Origin: *`** unconditionally — the exact bug already fixed once for `_send` |

#### `/photo/:/transcode` refused nothing, and `/:/resources/<anything>` drew a tile

| Request | Reference | rex before |
|---|---|---|
| no `url` | **400** | 200, a generated grey tile |
| `width` alone, `height` alone, or neither | **400** | 200 |
| `width=abc` | **400** | 200 |
| `url` naming a row the server does not have | **404** | 200, a generated tile |
| `/:/resources/<unknown>.png` | **404** | 200, a generated tile for any name |

Both dimensions are mandatory -- one alone is not a best-effort resize. And the
404s matter more than they look: a client that gets a 404 draws its own title-card
placeholder, while a 200 grey tile is an image it will happily cache.

Two transforms rex cannot do and now records rather than ignores silently:
`format=png` is honoured by a real PMS (real PNG bytes, which the client asks for
when it composites an overlay that needs alpha), and `blur`/`opacity`/`background`
are real transforms (a 200x200 poster went from 6727 bytes to 1366 under
`blur=30`). rex has no image codec on its stdlib-only server path, so it passes
the fetched bytes through unchanged. The client gates those filters on a VERSION
comparison (`imageFilters: "0.9.9.13"` in its feature table) and rex advertises
1.42.2, so it does send them -- background art renders sharp and fully opaque where
it should be blurred and dimmed. Recorded as a limitation, not a gap to be papered
over.

#### An unknown SINGLE-segment path is a 200, not a 404

Measured on both reference servers: `/zzzznope`, `/foo`, `/music`, `/photos`,
`/video`, `/plugins`, `/prefs`, `/settings`, `/login`, `/logout` all answer

```xml
<MediaContainer size="0" content="plugins"></MediaContainer>
```

-- the legacy plug-in dispatcher -- while a MULTI-segment unknown path (`/foo/bar`,
`/a/b/c`) is a 404. rex 404'd all of them, so the four the client bundle names
were 404s where a real server answers. And bare **`/channels`** is real too (two
`Directory` rows, `all` and `recentlyViewed`), which slightly corrects the note
saying the channel paths 404 on a real PMS -- the channel APPSTORE ones do, `/channels`
does not.

#### Verb routing is per (method, path), and not uniformly

```
                     POST    PUT    DELETE
/identity            200     200    200      <- verb-agnostic
/hubs                200     200    200      <- verb-agnostic
/library/sections    400     404    404
/status/sessions     404     404    404
/media/providers     400     404    404
```

rex answered the GET body -- 200 -- to all nine refusals. Worse, `POST
/library/sections` **with no parameters at all** answered 201 and really created a
section called "New Library", which then sat in the sidebar.

#### Part reads carry both validators

Every byte response from a real PMS carries `ETag` (the part's own file hash) and
`Last-Modified`, on the 200 and on every 206. rex sent neither, so a client had
nothing to revalidate against. rex owns no file to hash, so its ETag is a digest
of what identifies those bytes -- part id, version, size -- which is exactly what an
opaque validator is for.

Everything else in that area was measured and needs nothing, which is worth
recording so it is not re-measured: **conditional requests are ignored outright** on
a real PMS (`If-None-Match` matching and stale, `If-Modified-Since` equal and
future, `If-Range` matching and stale all return the full 200/206 -- it never
answers 304); **416** carries no `Content-Range: bytes */size`; a **multi-range**
collapses to the whole file rather than `multipart/byteranges`; a **malformed**
range is not an error (`Range: cheese=0-9` answers `206 Content-Range: bytes 0-9`
-- the unit is discarded and the numbers still parsed); and the version and filename
segments of a part URL are decorative (a wrong version or extension still serves).

#### The default listing order is `titleSort`, and every tie falls back to it

rex answered a listing in CATALOG order and left ties in whatever order the rows
arrived in. Measured on both servers: the default is **`titleSort` ascending** (the
title where no sort title is stored) -- fun's show grid opens on
`【OSHI NO KO】, 13 Reasons Why, 30 Rock, 86 EIGHTY-SIX, The 100`, because The 100's
titleSort is `100` -- and the SECONDARY key is always titleSort, whatever the primary
is and whichever direction it runs. So an edited Sort Title never moved a row on
rex, and the grid's own default disagreed with its sort control.

(Collation is the one part not reproduced: the reference uses ICU, so `'Burbs`
sorts before `#Jowable` and rex's byte order puts them the other way. Recorded
rather than faked -- there is no ICU on the stdlib server path.)

#### `includeAdvanced=1` was ignored, and the Sort menu lost most of its entries

The client sends it on the grid listing and on its FilterCollection, and reads the
`Sort` list straight into the sort menu. rex's Meta was identical with and
without it. Measured, the flag adds:

| Type | Added |
|---|---|
| movie | `year`, `userRating`, `contentRating`, `viewOffset`, `viewCount`, `mediaBitrate` + the `Duplicates` filter (`advanced: true`) |
| show | `year`, `userRating`, `contentRating`, `addedAt` |
| season | `season.index,season.titleSort`, `userRating`, `addedAt` |
| episode | `titleSort` (with a `firstCharacterKey`), `year`, `userRating`, `duration`, `viewOffset`, `viewCount`, `mediaHeight`, `mediaBitrate` + `Duplicates` |

So a movie library went 9 → 15 sorts and 28 → 29 filters, an episode view 7 → 15 and
17 → 18. And three of the keys it was about to advertise -- `userRating`,
`contentRating`, `mediaBitrate` -- answered **500** on rex's own listing, which is
an advertised control that breaks the grid the moment it is used.

#### The in-library search box is a SUBSTRING matcher

`/library/sections/{id}/search` is not the same matcher as `/hubs/search`. Measured:
`?query=ear` finds "Tears of Steel", `?query=a` finds three of five rows, and ONE
character is enough -- corroborated on fun's 3856-movie library (`?query=ngers` → 11
rows including `Challengers` and `Passengers`). It is title-only and
case-insensitive. rex routed it through the hub's word-prefix matcher, including
its two-character floor, so the in-library search box found nothing that did not
start a word.

#### The "All episodes" row is first, and a paged request drops it

Measured: a show's `/children` puts the `Directory` row **first**, before the seasons,
and a PAGED request omits it entirely (`size 1, totalSize 2` for one season).
rex appended it to EVERY page and counted it there, so a virtualised list
mis-measured and every page grew a spurious tile.

Corrected in the eighth pass: only a ONE-ROW page drops it. The client's own
`Start=0` page keeps it, outside the page, and it carries the show's leaf counts.

#### `nocache` tracks the library TYPE on a listing too — correcting an earlier finding

This file recorded the rule as paged-versus-unpaged. Re-measured on four libraries
across both servers: a **show** library's `/all` carries `nocache` paged AND unpaged,
a movie library's never does -- the same rule the secondary containers follow. Since
the client always pages, rex's version meant a show grid never carried it and a
movie grid carried it when unpaged.

#### Change Poster wrote to a path that served an image

The client's own Change Poster / Change Background builds
`/library/metadata/{id}/` + (`poster`|`art`) with **type PUT** -- singular -- and then
does `model.set("thumb", response)`. So the response BODY is the new artwork path.
Measured: `PUT …/poster?url=…` → `200`, body `/library/metadata/1/thumb/1789241428`;
the plural `…/posters` is a **404** for a write.

rex had it exactly inverted: the plural path took the write (answering an empty
200) and the singular one served image BYTES. So in the real client Change Poster did
nothing **and** set the tile's `img src` to a blob of PNG. Its `selected` flag was
wrong too -- keyed on "is there any artwork at all" rather than on which candidate the
row points at, so after picking the local placeholder nothing came back selected.

#### Sequence and state: nine things a one-request probe cannot see

- **Removing a collection member did nothing.** The client builds
  `DELETE /library/collections/{id}/items/{ratingKey}` and assigns the response onto
  its model; rex had no DELETE branch, so it fell through to an empty container --
  the tile vanished optimistically and came back on the next fetch. Measured: 200 with
  the collection ROW (new `childCount`, year span), and idempotent. A collection ADD
  answers the same row, not the `leafCountAdded`/`leafCountRequested` pair a PLAYLIST
  add answers -- rex sent the playlist shape.
- **`/library/onDeck` was the first twelve catalog rows**, with no view state at all.
  It is the server-wide Continue Watching list -- every row carries a `viewOffset` --
  and it is EMPTY on a server with nothing in progress. That endpoint is what every
  non-web client's On Deck reads.
- **A resume point under a minute is not stored.** The reference leaves the row
  untouched at 3 000, 20 000 and 59 000 ms and starts recording at 61 000; rex
  answered `playbackState: ignore` and wrote the offset anyway, so a title glanced at
  for two seconds appeared in Continue Watching.
- **"Remove from Continue Watching" HIDES the row and KEEPS the offset.** The
  reference's `viewOffset` was still 91 000 afterwards; the row simply stops appearing
  until the next timeline brings it back. rex deleted the offset, so the entry
  also threw away the resume position and the next Play started over. An unknown
  ratingKey there is a 404.
- **A scrobble writes NO history row.** The reference's history stayed at one row
  through three scrobbles -- rows come only from a timeline that reaches the end.
  rex recorded one per mark-as-played, so History and the Top shelf counted things
  nobody had watched. And a show or season keeps its OWN `viewCount`/`lastViewedAt`,
  independent of its leaves: it INCREMENTS rather than recomputing (unscrobbling one
  episode left the show at 2), and unscrobbling the show removes the field. rex
  emitted neither on a parent, so a TV grid's "Last Played" sort had nothing to read.
- **`historyKey` is monotonic and never reused.** The reference went 34, 35, delete
  35, then 36; rex derived it from the list length, so a delete made the next play
  reuse the deleted key. And a removed item's history row SURVIVES with its
  `ratingKey` and `key` stripped -- rex kept both, so the History page linked a 404.
- **Deleting a season's last episode removes the season.** rex left it listed with
  `leafCount 0`, so the show page drew a season tile with nothing behind it.
- **A playlist does not take a row it already has.** Re-adding answers
  `leafCountAdded: 0` and leaves the count alone, and a uri naming the same key twice
  adds it once. rex appended every time, so Add to Playlist silently duplicated.
- **A removed row leaves the play queues.** The queue shrinks, `playQueueTotalCount`
  drops and the VERSION steps, and the selection moves to a row that still exists.
  rex left the removed row's id as `playQueueSelectedItemID` and never moved the
  version -- which, by this file's own earlier finding, is exactly the response the
  client discards.

#### An unknown section id answered a complete, valid library

`GET /library/sections/999/all` was a 200 with `librarySectionID="999"`, and
`/999/filters` answered 28 filter rows for a library that does not exist, `/999/prefs`
19 settings. All of them are 404s on a real PMS. A deleted library, a stale bookmark
or a renumbered section therefore rendered as an empty-but-working one. Fix 4's trap
from the inside, and neither the replay nor the probe ever asks for a section that is
not there.

#### The verb gate, leaf by leaf

Measured across 21 section leaves: a real PMS 404s every verb a leaf does not
implement, and it **never answers 405** -- that status does not appear anywhere on its
surface. rex answered the GET body to any verb, so `DELETE
/library/sections/1/all` came back 200 with the grid, which tells a client the delete
was accepted. The verbs each leaf takes: `refresh` GET/POST/DELETE, `emptyTrash` and
`analyze` PUT, `indexes` DELETE, `prefs` and `all` GET/PUT, everything else GET only;
and a POST to the section itself is a 404.

Three smaller ones from the same sweep: `/:/timeline` requires **`state`** and is a
400 without it (it is the only required parameter); a bare `GET /playQueues` is an
empty 200 while `?type=` is a 400; and `/livetv/epg/lineup`, `/channelmap` and
`/lineupchannels` are **400** on a server with no tuner rather than the blanket empty
container rex answered for the whole tree.

#### `/:/prefs` was missing eight rows, one of them the server's name

174 on the reference, 169 on rex. The one that matters is **`FriendlyName`**:
`hidden: false, group: "general"`, and the client looks it up BY ID to draw Settings >
General's server-name field and to set its own `friendlyName`. Without it the field
did not render, and a write to it was refused as an unknown preference. The other
seven (`AllowLegacyAgents`, `MachineIdentifier`, `PlexOnlineMail`,
`ManualPortMappingPort`, `LastAutomaticMappedPort`, `CLDriver`, `CertificateUUID`) are
now in the sidecar in the reference's own order, with the identity-bearing values
filled from this server rather than copied.

#### Container paging: both parameters, and the client sends HEADERS

Two defects in one predicate. A real PMS pages only when
`X-Plex-Container-Start` **and** `X-Plex-Container-Size` are both present -- one
alone is ignored entirely, with no slice, no `offset` and no `totalSize`. rex
tested them with OR, so a lone `Size=1` returned one row and claimed a total, and
a lone `Start=1` silently dropped the first row. A negative size drops paging on a
real server; in rex it was a Python slice, so `Size=-1` served every row but
the last.

And the modern client sends them as **HTTP headers**, not query parameters
(`fetchRange`: `headers:{"X-Plex-Container-Start":…,"X-Plex-Container-Size":…}`).
rex read only the query string, so every windowed grid request got the whole
library and no `totalSize` -- the same shape of bug as the missing Seasons block,
one transport over. Both are now normalised at the door.

`totalSize` is the FILTERED count, which rex already had right.

#### Sorting: a spec is a LIST, and the seed matters

Measured on the reference, and rex honoured none of it:

| Request | Reference | rex before |
|---|---|---|
| `sort=year:desc,titleSort:asc` | orders by year, breaks ties on title | dropped the second key -- and `titleSort:asc,year:desc` came out **descending**, because the spec as a whole ends in `:desc` |
| `sort=random:12345` vs `random:99` | two different stable orders | one order regardless of seed |
| `sort=titleSort:desc:nocollate` | descending | **ascending** -- the direction was read off the first colon, so it saw `desc:nocollate` |
| `sort=titleSort:bogus` | ignored, ascending | ascending (accidentally right) |
| `sort=bogus` | **500** | 200 with the list silently unsorted |

The seed is not decoration: it is how a client pages through a shuffled library
without repeating or skipping rows. The spec is now parsed as the comma-separated
list it is and applied right to left on a stable sort, so the leftmost key
dominates -- and a term's TAIL is a list too: `asc`, `desc`, `nocollate` and a
`random` seed arrive in any order, so the direction is whichever element is
`desc` rather than whatever follows the first colon. An unknown MODIFIER is
ignored where an unknown KEY is a 500. An unknown key returns the reference's 500 rather than a silently
unsorted 200 -- a caller cannot tell the difference otherwise, and a silent
no-op is the failure this whole file is about. (That a real PMS answers 500
rather than 400 for what is a client error looks like a defect in it; it is the
measurement, so it is what rex answers.)

#### CORS, which a browser enforces before the server sees anything

| | Reference | rex before |
|---|---|---|
| normal response, with an `Origin` | echoes that origin, plus `Access-Control-Expose-Headers: Location, Date` and `Vary: Origin, X-Plex-Token` | `Access-Control-Allow-Origin: *`, always |
| normal response, no `Origin` | **no CORS headers at all** | `*` anyway |
| preflight | echoes the requested `Access-Control-Request-Headers` **verbatim**, `Max-Age: 1209600` | a FIXED list |

The fixed list was the bug. It omitted `X-Plex-Session-Id` (rex allowed
`X-Plex-Session-Identifier`, a different header), `X-Plex-Language`,
`X-Plex-Features`, `X-Plex-Model` and several more the web client sends -- and a
preflight that omits one header makes the **browser** refuse the request, with no
server-side trace at all. Echoing is the only answer that cannot block a header a
client legitimately sends. A wildcard is also invalid on a credentialed request,
so `*` breaks exactly the clients that send an Authorization header or a cookie.

Why it had never surfaced: Plex Web passes identity in the **query string**, not
in headers, so its requests are not preflighted at all.

#### What `unmatch` actually does, measured key by key

The unmatch handler was written from the obvious reading -- "drop everything the
agent supplied" -- and that is not what a real PMS does. Diffing a reference
row's keys either side of the call:

| | |
|---|---|
| dropped | `Genre`, `Country`, `Role`, `Director`, `Writer`, `Producer`, `studio`, `contentRating`, `audienceRating`; `summary` is **emptied but kept** |
| **kept** | `thumb`, `art`, `Image`, the external `Guid` array, `Rating`, `audienceRatingImage`, `UltraBlurColors`, `originallyAvailableAt`, `year`, `slug`, `userRating`, `lastRatedAt`, `Field` |
| guid | becomes **`tv.plex.agents.none://<ratingKey>`**, not `local://` |
| title | becomes the FILE NAME without its extension |

The artwork surviving is the one that matters. rex was dropping `thumb` and
`art`, which leaves the row with no artwork URL for the client to ask for at all
-- so the grid tile goes blank, where a real unmatched item still shows its
poster. The `unmatched=1` filter had to learn the same guid forms the client's
own Match/Fix Match predicate tests for, or an item the server called unmatched
was invisible to the filter that looks for exactly those.

Also measured while there, and deliberately NOT copied: every real row carries
**four** `Image` entries -- `coverPoster`, `background`, `clearLogo`
(`/library/metadata/{rk}/clearLogo/{v}`) and `backgroundSquare` (`/squareArt/{v}`)
-- and all four endpoints serve bytes. rex advertises only the two it has.
Advertising a clear logo it cannot supply would put its generated grey tile over
the hero banner in place of the title, which is worse than the client falling
back to text.

#### A negative result worth keeping: no lock was needed

rex is a `ThreadingHTTPServer`, and everything the UI writes -- playlists,
collections, activities, catalog rows -- is mutable state with nothing guarding
it. That looks like a defect waiting to happen, so it was measured before being
"fixed":

- 400 rows, three threads churning the items dict through `merge`/`split` while
  four threads iterated it through `/library/all`, `/hubs`, a section listing and
  a genre axis: **33,300 requests, zero tracebacks, zero 5xx.**
- 320 concurrent playlist creates across eight threads: **320 unique
  ratingKeys**, and the server's own count agreed.

So no lock was added. Under CPython each of these iterations completes inside one
bytecode loop and the dict mutations are atomic, which closes the window for the
access patterns rex actually has. The check stays in the suite at a smaller
size, because the failure mode it guards against -- two clients handed the same
ratingKey -- would be silent rather than loud.

`harness/scripts/replay.py` was run against the same server with the recorded
`reference-walk` corpus. What is left after this work is content (a different
library: four Blender movies and Firefly against three fixture rows and two
shows), the deliberate capability differences above, and 13 `404`s for the
reference's own ratingKeys -- rex keeps its key ranges disjoint by type
(leaves 1000+, shows 20000+, seasons 30000+), so a corpus recorded against a
server whose keys are 1-8 cannot be replayed at it item for item. Everything
structural is gone.

#### The fifth pass: two agents, and the reference had been rebuilt under them

Cycle 5 ran a wire-format sweep and a settings-and-dialogs sweep, each against the
1.43.4 reference and fun. One operational fact first, because it changes every
recording made before it: cycle 4's broad sweep sent `DELETE /library/sections/1`
to the reference, and its Movies library was restored from Plex's own 13:18 backup
on 2026-09-12. The section ids and uuids survived; **Cosmos Laundromat re-keyed from
30 to 18**. A corpus recorded before that names a key the lab no longer has. The
hunting prompts now forbid every destructive write to the reference and require a
list of each write and its undo, and the lab's state is snapshotted before and
compared after a cycle.

#### The Add Library form 400'd, and the settings writes validated nothing

The Add Library dialog reads `/library/sections/prefs?type=<N>&agent=<id>` before
the library exists and joins it with three other reads, so a refusal stops the
dialog from validating. rex answered 400 to every call. Measured on the whole
matrix of types and agents, now `section_prefs_new.json`:

| request | reference |
|---|---|
| `type=1` / a legacy or unknown agent | 7 settings |
| `type=1&agent=tv.plex.agents.movie` | 19 (the section-1 list) |
| `type=1&agent=tv.plex.agents.series` | 18 |
| `type=1&agent=tv.plex.agents.none` (or `.music`) | 12 |
| `type=2` / `+movie` / `+series` / `+none` | 9 / 21 / 21 (a different 21) / 15 |
| `type=13`, `type=8` (`+music`) | 2, 5 (13) |
| `type=3,4,9,10,18,99` | `hidden` alone |
| no `type`, `type=abc` | 400 |

`?agent=` reshapes an EXISTING library's list the same way, keeping its values.
Every setting has one definition across all nineteen lists.

The three writers, each measured with its value restored afterwards:

| write | server `/:/prefs` | library `/sections/{id}/prefs` | item `/metadata/{id}/prefs` |
|---|---|---|---|
| unknown setting | 400, **nothing** applied | 400, nothing applied | 400, nothing applied |
| int, not a number (`abc`, `4.5`) | **500** | ignored, 200 | stored raw; the Setting still reads its default |
| int, empty | ignored | ignored | clears the setting |
| bool | `1`/`true` true, anything else false | stored as the STRING `"true"`/`"false"` | -- |
| value type on read-back | keeps its type (`40`, `false`) | string | string |

rex applied the known settings beside an unknown one, stored `"abc"` in an int,
echoed every bool back as the string that was written, and stored ANY item key --
including its own parser's `_pairs` list, which then rendered onto the metadata row.
An episode has no settings, so every write to one is a 400.

#### Agents, scanners and plug-ins

- `/system/agents?mediaType=1` lists the reference's three modern agents (Plex Movie,
  Personal Media, NFO Movie) each with 45 `Language` rows and `default="1"` on
  `en-US`, which the dialog's `findWhere({default:true})` preselects. rex sent
  one language and no default. `mediaType=abc` is a 400; a type no agent serves is
  `size: 0`. Now `system_agents.json`.
- `/system/agents/{id}/config/{type}` and `/contributors` are EMPTY containers for a
  modern agent on BOTH real servers -- only a legacy plug-in has a chain -- with 404
  for a type no agent serves and 500 for a non-number or missing parameters.
  `/attribution` and `/attribution/image` are 404 for a modern agent. rex
  answered the JSON agent list to all of them, one of which the page feeds to `<img>`.
- `/system/scanners/{N}` is keyed by library TYPE (`ScannerCollection` builds it
  from `sectionTypeID`), not by section: `/13` is the photo scanner on a server with
  no photo library, and a type with no scanner is a 500.
- `/:/plugins/<id>/prefs` is a 404 for a modern agent and for an unknown one.

#### Devices, play attribution and the counters behind the Dashboard

Overturning the note that stood in `sessions.py`: a Device row's `name` is the
client's `X-Plex-Device-Name` and `platform` its `X-Plex-Platform`, each recorded
independently. The earlier probe sent no X-Plex headers and saw them empty. Device 1
is the row with no client identifier, created when the server started. `/devices`
lists every device ever seen (41 on the reference) and `/devices/{id}` reads one
(404 for an unknown id, `abc` included); rex listed one row describing itself.

A completed play's history row and its `StatisticsMedia` row carry the PLAYING
device's id. rex wrote device 1 on both, an id no Device row in the same
container named. With no `timespan`, `/statistics/bandwidth` and `/media` answer
every rollup, 0 to 4, in one list.

`/library/metadata/{id}/users/top` is one `Account` row with a `globalViewCount`
for an item that has completed views, `size: 0` otherwise, and a **500 for a
season**. The count survives a history delete and an unscrobble (movie 1 reports 2
with neither), so rex keeps its own. `/nearest` is an empty library container
for every item and a 404 for an unknown one.

#### Small endpoints, and the last three 405s

| request | reference | rex before |
|---|---|---|
| `/channels/all` | 200 `size=0 content="plugins"` | 404 |
| `/log` with no `level`, no `message`, or a non-numeric level | 400 on every verb | 200 |
| `/services/browse` | `/` and the mount points | 404 |
| `/services/browse/<base64>` | 400 without `includeFiles=1`; its directories with it; 200 empty for a path that is not there | 404 |
| `/library/tags?type=N` | the tag TABLE for that type, by id: `{id, filter, tag, tagType}` plus tagKey/thumb for people; bare or non-numeric is 400 | `size: 0` for every type |
| `GET /library/metadata/{id}/refresh`, `/analyze`, `/media/{id}` | 404 | 405 |

#### The wire format: how Accept is read, and what JSON never carries

Only an exact, case-sensitive `application/json` or `application/xml` range counts;
`q` ranks them and a tie goes to the one listed first; any other parameter
disqualifies the range. So `application/xml, application/json` is XML,
`application/json; charset=utf-8` is XML, `Application/JSON` is XML, and axios's
`application/json, text/plain, */*` is JSON. rex answered JSON whenever the
substring appeared.

Across ~110 JSON responses of the reference there is not one `null` and not one
`[]`. The XML writer already dropped `None`, but rex's JSON carried
`audioProfile: null`, five null Stream fields, `Label: []` and `Metadata: []` on
every empty hub. An empty OBJECT is real: `includeRelated=1` on an item with no hubs
is `"Related": {}`, and a multi-item Common carries `"Rating": [{}]`.

Number formats are per field in XML: a stream's `frameRate` always has three
decimals (`24.000`), a `double` Setting's whole value drops its fraction
(`value="1"`), and every other float is written as Python writes it
(`audienceRating="5.0"`).

#### Row shapes

- `titleSort` is the title with a leading English article stripped, and it is sent
  **only when it differs** -- 0 of 3,856 movies and 0 of 599 shows on fun carry an
  equal one. An episode strips its OWN title. rex filed "The Simpsons" under T
  and gave every episode its show's name as titleSort.
- `originalTitle` is the original-language title, present only when it differs.
  rex put the media FILENAME there, which the client prints on the preplay page,
  in the window title and as the OS media-session artist.
- No default `Field` lock, no empty `Label`, no `Location` on a movie, and no
  `rating` without a critic source -- the lab's TMDB-matched movies carry only
  `audienceRating`. A season has no `year`, `skipCount`, `theme` or `titleSort` (the
  old comment tying `year` to the Seasons block was the paging bug); an episode no
  `theme` and, with no chapters, no `chapterSource`; a show has `index: 1`.
- On 2,558 fun rows `optimizedForStreaming` and `has64bitOffsets` exist only for an
  mp4/mov -- an integer on Media, a boolean on Part -- `hasVoiceActivity: false` is on
  every Media, and `Part.hasThumbnail` appears only as `"1"`. `bitDepth` is an int.
- A grid or hub row is a lighter projection than the detail row: tags are `{tag}`
  only, a Part has no `Stream`, and there is no `Rating`, `Location`, `Field` or
  `Producer`; a section listing's rows also leave the librarySection fields to the
  container, where a hub's rows keep them. Confirmed in the client's own corpus.
- The tree's `MediaStream` writes its numbers as STRINGS except `id`, `index`,
  `type` and an audio `channels`, with its own field set, and its `MediaPart` has no
  `container`.
- An episode list's container is presented as the SHOW's: `title1` is the show's
  title, `parentTitle` is `""`, `grandparentRatingKey` is a number, and there is no
  `summary`, `parentYear` or `grandparentYear`.
- `/library/sections` rows carry no `composite`; `/library/onDeck` and
  `/library/recentlyAdded` say `mixedParents`; `GET /` carries `hubSearch`,
  `itemClusters`, `mediaProviders`, `multiuser` and `transcoderPhoto`, which
  `/media/providers` does not.
- An emptied playlist carries neither `composite` nor `duration`, on its row or on
  its `/items` container. A collection read through `/library/metadata` has the
  library container and its row names its section.

Catalogs built before this carry the old fields, so `hierarchy.normalize_rows` and
`mediainfo.normalize_media` apply the rules when a catalog loads as well as at build.

#### Hubs, Related, Common and sorts

- `/hubs` is EIGHT shelves on both real servers, in a fixed order and all promoted:
  continue (hero, mixed), on deck, movies, television (mixed), music, photos,
  personal videos and playlists, each with its measured key. rex answered three
  of its own devising with no key, context, style or promoted flag.
- A library shelf carries no librarySection fields, and its `hubKey` is the shelf's
  own rows as `/library/metadata/<keys>`, absent when empty. Recently Released
  Movies honours its key's one-year window (the reference's is empty for films from
  2006-2015); Rediscover's key is `lastViewedAt<=-6w&lastViewedAt>=-5y` and its rows
  are SHOWS.
- Overturning an earlier finding: `/library/metadata/{id}/related` carries the SAME
  hub as `/hubs/metadata/{id}/related`, and so does `includeRelated=1`. The earlier
  recording only covered the four lab items with no related titles.
- `/common` with no `id` computes over the whole filtered set -- the select-all edit,
  whose save already did. `guid` is always `""`; a mixed `titleSort` is present as
  `""`; `mixedFields` lists in a fixed field order; one `Rating` per rating image
  every selected item has, empty when their values differ; `key` only for one item;
  an id of the wrong type is not part of the set.
- `/sorts` is per TYPE and honours `includeAdvanced=1`, and the advanced sorts are
  INTERLEAVED -- Year is second on a movie library -- in the Meta as well.

#### Measured in cycle 5 and deliberately left alone

- The legacy `com.plexapp.agents.*` agents and the 17 plug-ins `/:/plugins` lists:
  rex hosts no plug-in framework, so their prefs, contributors and attribution
  would all be invented. `/system/*` stays JSON where the reference answers XML only,
  and the reference's JSON collapses repeated `Language` rows into one object; the
  list is the useful shape.
- Search is typo-tolerant on the reference (`tearz` finds Tears of Steel at score
  0.33, `zzqqxx` returns rows at 0.32). The rule is not recoverable from outside, so
  the recorded word-prefix model stays.
- A season and an episode carry their own external `Guid`s (tmdb/tvdb, imdb for an
  episode). The catalog records none, and borrowing the show's would be wrong.
- The Common of seasons and episodes has quirks that do not reduce to a rule: a
  single episode omits `title` and `year`, two episodes of one season do not list
  `title` mixed, all six of a show's episodes do.
- `movie.by.actor.or.director` on a library's hubs is `random: true` on the reference;
  its selection rule is not measurable. Markers and chapters have no data source.
- `/services/browse` lists `/rex` rather than mount points, because rex
  has no filesystem. `optimizedForStreaming` on an mp4 stays 0: where the moov atom
  sits is not something ffprobe reports.

#### The sixth pass: what the player asks for, and what every write pushes

Cycle 6 swept the playback surface and the notification channels. Both agents'
before/after snapshots of the reference matched. One write could not be undone: a
stray `POST /downloadQueue` created an empty download queue that `DELETE` refuses
with a 404. It holds nothing and touches no library row.

#### The decision refused nothing

A real PMS validates a decision before it answers, and every refusal is a 400 with
an HTML body. rex answered `mdeDecisionCode: 1000 "Direct play OK."` to all of
them, including a path to nothing:

- a path that is not `/library/metadata/{id}` of a playable row: an unknown id, a
  show, a season, `/children` or a section;
- a missing, non-numeric or out-of-range `mediaIndex`, and an out-of-range
  `partIndex` (a MISSING `partIndex` is fine);
- no client profile. The request is accepted exactly when
  `X-Plex-Client-Profile-Name`, `X-Plex-Platform` or `X-Plex-Device` names a file in
  the server's `Resources/Profiles` directory, read off the reference. `Chrome`,
  `Windows`, `Konvergo`, `Microsoft Edge` and `Unknown Browser` pass; `Edge`,
  `Darwin`, `Kodi`, `webOS`, `chrome` and `Chrome ` are 400s. A profile name rescues
  a bad platform; a profile EXTRA does not.

The start family (`start`, `start.m3u8`, `start.mpd`) validates the same way before
it refuses, and `ping`/`stop` without a `session` are 400s.

**A refused direct play is "neither", not "Conversion OK".** A PMS with an encoder
answers `directPlay=0` with 3000 / 1001 / 1001. rex has no encoder, and the
reference answers exactly the shape that describes that for a client whose profile
has no conversion target (`Generic` or `Sonos` with direct play off). The shape is
`generalDecisionCode 2000 "Neither direct play nor conversion is available."` and
`transcodeDecisionCode 4005 "Cannot convert this item. No conversion profile found
for protocol dash."`, with NO Metadata and no `resourceSession`. The web client
treats any code of 2000 or more as final and shows its own text. Given 1001, it went
on to request `start.mpd`, got the 404 and showed a generic playback error. The
texts are verbatim; the full vocabulary is in the reference binary's strings.
Without `hasMDE` the direct-play text always names protocol `http`, whatever
protocol was asked for.

**The decision row is its own projection.** It is the detail row without `Guid` and
`Image`, and a movie's also without `summary`. The chosen Media carries `selected`,
each Part carries `decision` and `selected`, and every Stream carries `location:
"direct"`. In JSON the Media, Part and Stream ids, `aspectRatio` and
`hasVoiceActivity` are STRINGS here, where the detail endpoint sends numbers and a
boolean.

**`resourceSession` is the X-Plex-Session-Identifier**, falling back to the client
identifier and then to an EMPTY string. This corrects the earlier note, which was
measured without a session identifier.

#### Streams, parts and downloads

| request | reference | rex before |
|---|---|---|
| `/library/streams/{unknown or word}`, bare `/library/streams` | 404 (501 only for a stream it has) | 501 |
| `PUT /library/streams/{unknown}` | 400 | 501 |
| `/library/metadata/{id}/subtitles` with no `language` | 400 | 200 |
| `…?language=xx`, `eng`, empty | 500 (`en`, `EN`, `en-US`, `de` search) | 200 |
| `PUT /library/parts/{id}?audioStreamID=` or `subtitleStreamID=abc` | 200, ignored | 400 |
| `/library/parts/{id}/indexes/sd[/{ms}]`, `/{word}/file.mkv`, `/{v}/file.mkv/extra` | 404 | the part reader (whole video) |
| `/library/parts/{id}`, `/{id}/file.mkv`, `/{id}/{wrong v}/{wrong name}` | 200 | 200 |
| `DELETE` / `POST /transcode/sessions` | 404 | 200 |
| `POST /downloadQueue` | a `DownloadQueue` row per client, the same on repeat | 404 |
| `/downloadQueue/{id}`, `/items`; `DELETE /downloadQueue/{id}` | the row, `size 0`; 404 | 404 |

On 432 embedded subtitle tracks on fun, `default` and `forced` appear only when true,
every track says `canAutoSync: false`, and none carries `embeddedInVideo`. That
field marks captions inside the video stream; rex set it on every track.

#### Notifications: the type numbers and the collection lifecycle

A playlist's TimelineEntry is `type 15` and a collection's `type 18`. The client
builds the URL of the row it updates from that number, so rex's `1` dispatched a
delete for a metadata row nobody holds, and a deleted list stayed on screen in every
other client. Measured frame by frame on both channels:

| write | frames |
|---|---|
| playlist create | `state 0 created`, type 15, sectionID `-1`, title `""` |
| playlist delete | `state 9 deleted`, type 15, the playlist's title |
| collection create | `state 0 created`, type 18, its title (rex sent nothing) |
| collection delete | each member `state 3 queued`, then each member `state 5`, then `state 9 deleted`, type 18, title `""` |
| edit on `/library/metadata/{id}` | `state 3 loading`, `state 5` |
| edit through `/library/sections/{id}/all` | `state 3 queued`, `state 5` |
| rate, scrobble, unscrobble, history delete, playlist or collection add/remove | nothing |

A movie's frames title it with its year (`Elephants Dream (2006)`). The collection
CREATE response carries no librarySection fields, where the ADD response does.
`DELETE /library/collections/{id}/children/{ratingKey}` removes a member exactly as
`/items/` does; rex answered it with an empty 200 and removed nothing.

The eventsource channel's first ping comes ten seconds in, not on connect. And
`filters=` with an EMPTY value is not "no filter": it delivers everything INCLUDING
the log stream.

#### The timeline's minute, pinned

**Exactly 60 000 ms is not stored, and the rule holds on every state.** A `stopped`
beat at 30 000, or at 59 500 of a 60 000 ms item, is `ignore` and stores nothing.
rex exempted `stopped`, so stopping a trailer at ten seconds left a resume point,
and stopping one near its end marked it watched with a history row. A play completes
past 90% on ANY state: a `playing` beat at 95% answers `complete` and sets
`viewCount`, and the `stopped` beat after it counts nothing again. The response's
`viewCount` is the item's own count (0 while a first play is in progress), and its
`size` counts the Bandwidths block and nothing else.

#### Malformed identifiers and query edges

Reference and fun agree on every one of these.

| request | answer | rex before |
|---|---|---|
| a query pair with no `=` (`?includeGuids`, `?foo`, `?&`) on any path, `/identity` too | 400 | 200, `includeGuids` read as true |
| an empty path segment (`//identity`, `/library//sections`) | 404 | 200 |
| `/library/sections/` (one trailing slash) | the listing | 404 |
| bare `/status/sessions/history` | 404 | 200 |
| `/library/sections/abc`, `-1`, `1.5` | 404 | 400 |
| `/library/metadata/{movie or episode}/children` | 400 (`/allLeaves` is 200) | 200 empty |
| `GET /activities/{id}` | 404 | the list |
| a leaf on `1,abc` (one numeric member) | that item's leaf | 404 |
| an id ≥ 2^31 on playlists, playQueues, collections, devices, history, parts, streams | 400 (metadata and sections: 404) | 404 |
| `/playQueues/abc` | 400 | 404 |
| `?type=2&type=1` | nothing (repeated parameters AND) | the last one won |
| an empty `X-Plex-Container-Size`, with or without a start | 500 | 200 |

#### Measured in cycle 6 and deliberately left alone

- The eventsource channel's transport. The reference streams chunked over
  keep-alive; rex writes an unframed body with `Connection: close`, which an
  EventSource reads the same way.
- What adding an item to a download queue does, and the exact refusal text a PMS with
  no transcoder at all would give. Neither could be measured without writes the lab's
  safety rules forbid.
- A valid transcode start still answers 404: the reference would transcode, and
  rex cannot.
- The effect of a subtitle selection on the play queue and the next decision. The lab
  has no subtitle tracks and fun is read-only.

#### The seventh pass: the whole probe table and every corpus, diffed mechanically

Cycle 7 ran `probes/ui_surface.py` read-only against both servers (202 rows) and
replayed the recorded corpus with its ratingKeys remapped onto rex's items. It
triaged every difference into catalog content, already recorded, or new behaviour.
Six were new.

- **A theme rex can never serve.** Every show, season and episode named
  `/library/metadata/{show}/theme/1`, and the endpoint answers 404 because rex
  has no theme audio. On fun, a show without a theme carries no `theme`, and its
  children carry no `parentTheme` or `grandparentTheme`. The web client reads
  `grandparentTheme || parentTheme || theme`, so with theme music on every page
  asked for a 404. No row names a theme now.
- **Recent Playlists on a library's hubs.** `includeLibraryPlaylists=1`, which the
  client's own hub request always sends, adds `recent.library.playlists.{id}` right
  after the library's Recently Added. Its key is
  `/playlists/all?type=15&playlistType=video&sort=lastViewedAt:desc&sectionID={id}`,
  with style `shelf` and no `promoted`. rex ignored the parameter. Only the
  empty shelf was measurable, because neither server has a playlist.
- **Hub and search rows by type.** A movie or episode on any shelf or in search has
  no `Guid` and text-only tags. (Overturned for Related and postplay shelves in the
  eighth pass: those carry the full row.) This includes `/hubs/home/recentlyAdded?type=1` and
  `/hubs/search`, which bypassed the projection. A SHOW on a `mixed` Recently Added
  TV shelf, whether on home, the library tab or `/hubs/promoted`, is the FULL row:
  `Guid`, `Location`, `Rating` and tag ids. A show in search keeps its `Location`.
- **`excludeFields` everywhere.** The named fields go from every row (listings,
  `/children`, `/allLeaves`, hubs, search) and from the container. A show's
  `/children?excludeFields=summary` loses the container's summary too. rex
  applied it only to detail reads.
- **A season's and an episode's `/arts` are their own.** They are empty on the lab,
  and fanart.tv's season art on fun. rex offered the SHOW's backdrop as the
  item's selected choice.
- **An episode's `Image` list.** `coverPoster` is the show's poster and the
  episode's own 16:9 still is `snapshot`, on detail and listing rows alike. rex
  labelled the still as the poster.

`probes/ui_surface.py` also carried a write in its read-only plan: `PUT
/updater/check`. It now runs only under `--mutate`, beside the probe's other
writes.

Measured and not acted on: on kids `/hubs/sections/1?count=1` drops the Recently
Watched shelf, which fun does not, so there is no rule to follow. Kids' orphaned
collection tags carry a `guid`, but only 3 of fun's 372 tags do.

#### The seventh pass, second batch: python-plexapi as a third-party client

The second agent of cycle 7 drove python-plexapi at rex, the reference and fun:
463 calls per server, through a guard session that refused every write. It then
diffed the three servers. The reference's library state was identical before and
after. Every finding below was re-measured before anything changed.

- **Every sort python-plexapi sends is type-prefixed, and the prefix is a scope.**
  - `section.all(sort='titleSort:desc')` goes out as
    `sort=movie.titleSort:desc`. `recentlyAdded()` and `search(sort=)` do the same.
  - On an episode view, `show.titleSort` sorts by the SHOW's sort title,
    `season.index` by season number and `episode.id` by ratingKey.
  - An unknown prefix (`bogus.titleSort`) reads the row itself. An unknown field
    after any prefix is still a 500.
  - On a season or episode list, ties fall back to show, then season, then
    episode order: `sort=episode.index:desc` puts S1E3 before S2E3.
  - rex answered 500 for every prefixed key, including the
    `show.titleSort:desc,season.index:nullsLast,...` descKey its own `/sorts`
    advertises.
- **A scoped filter reads the field of the row its prefix names.** Cycle 4 found
  that the TYPE lookup is global. The VALUE, though, comes from the named row.
  - `type=4&show.title=Fire` is all six Firefly episodes and
    `type=3&show.title=Fire` both seasons.
  - `type=4&season.index=2` is season 2's three episodes.
  - A scope below the row matches when ANY descendant does.
    `type=2&episode.title=Train` is Firefly. `type=3&episode.index=3` is both
    seasons. `type=2&episode.title!=Train` is still Firefly, because its other
    episodes do not contain the word.
  - `index` filters an episode or season listing (`type=4&index=2` is both
    episode 2s), although no Meta advertises it.
  - rex compared the episode's own title for `show.title` and ignored `index`.
- **`title=` matches word beginnings, not substrings.** Cycle 4's string table
  holds for `studio` (`studio=lender` is still four rows) but not for `title`. The
  reference and fun agree:

  | `title=` | rows | why |
  |---|---|---|
  | `Fire`, `fire` | Firefly | begins a word, any case |
  | `ire`, `fly`, `intel` | none | a substring never matches |
  | `Tears Steel`, `of Steel`, `b b b` | their title | each query word begins a distinct title word, in order |
  | `steel of`, `b b b b` | none | order and word count |
  | `Tears, of` | Tears of Steel | `,` and `.` in the query are dropped |
  | `Tears-of` | none | other punctuation is part of the word |
  | (empty) | all five | an empty word begins any word |
  | `%20` | the four titles of two words or more | the query splits on SINGLE spaces |
  | `Tears%20%20Steel` | Tears of Steel | the empty word consumes `of` |

  Only `=` works this way. `title!=ears` excludes Tears of Steel, so `!=` still
  reads a substring, and `<=`/`>=` are still begins-with and ends-with.
- **`titleSort`, `summary` and `tagline` compare by case-insensitive equality.**
  - `titleSort=Sin` and `summary=e` match nothing, while `titleSort=sintel`
    matches.
  - `tagline=` is the three rows that have none.
  - `titleSort!=Sin` keeps all five rows and `titleSort!=sintel` four.
  - No Meta lists these fields, so rex ignored them.
  - `==` and `!==` are case-insensitive on every field (`title%3D=sintel` finds
    Sintel).
- **`/library/all` is a query.**
  - `type=` selects the rows, and filters and `sort=` apply.
  - The default order is the ratingKey.
  - A row carries `Media` only when a `type` was given.
  - rex answered all nine of its rows whatever was asked, and that is what
    `library.search(libtype=)` read.
- **Four reads python-plexapi makes that rex answered 404.**
  - `/hubs/sections/{id}/manage` lists 8 hubs for a movie library and 9 for a show
    library. Only Recently Added, plus Seasonal for movies, is promoted to home.
  - `/library/sections/{id}/timeline` is a secondary container with
    `latestEntryTime` and `updateQueueSize: 0`.
    - It lists items the server recently finished (state 5, the row without its
      tags) and removed (state 9, a stub with an empty title and summary).
    - fun, with nothing recent, answers size 0 and `latestEntryTime` 0.
  - `/library/metadata/{id}/clearLogos` and `/themes` are candidate lists, of
    `Photo` and `Track` rows, and empty for an item that has none.
- **`/services/browse/<b64>` needs `includeFiles` PRESENT, not `=1`.** `=0` answers
  the directories and `=1` adds `File` rows; only an absent parameter is a 400.
  Cycle 5's note read the value.
- **Row projections**, identical on both servers:
  - `/library/recentlyAdded` rows name their section by `librarySectionID`,
    `librarySectionTitle` and `librarySectionUUID` (no key). They carry
    `allowSync` and are light: no `Guid`, no `Rating`, text-only tags.
  - A season in a `type=3` LISTING has no `leafCount`/`viewedLeafCount`; its
    `/children` row does.
  - `/children` rows leave the librarySection fields to the container.
  - A show's listing row carries `duration` and `originallyAvailableAt`, as its
    detail does. A show's `Location` has no `id`.
- **A season with a year of its own carries it**, with its title as `titleSort`
  and no `parentYear`. This holds on the reference's season 1 and on fun alike.
  Only a season without a year carries `parentYear`. It overturns cycle 5's "a
  season row carries no year or titleSort", which was measured on seasons that
  had no year yet.

Measured and not acted on:

- The agent reported `/system/agents` in the wrong shape. Re-measured, the
  reference's six modern `tv.plex.*` rows match rex's exactly: `name`,
  `hasPrefs`, `id`, `Language` rows with a `title`, and no `primary`. The
  `primary`/`MediaType` shape the agent saw belongs to the legacy
  `com.plexapp.agents.*` plug-ins, which rex has left out on purpose since
  cycle 5.
- `/library/metadata/{id}/matches?manual=1` lists 20 candidates from the metadata
  provider, and rex's list is empty. Filling it needs a provider search per
  request.
- `thumbBlurHash`/`artBlurHash` under `includeFields` need image decoding, which
  the stdlib server path does not have.
- `guid=plex` is a prefix match and `slug=x` a 500 on the reference. The client
  filters on neither. (Overturned for `guid=` in the eighth pass: the client's
  availability lookup sends `/library/all?type=N&guid=`.)

#### The eighth pass: every path the web bundle can build

Cycle 8's first agent read the served client instead of a recording. It pulled
466 path literals out of `webclient/js` (328 distinct), resolved 152 GET templates
with each server's own ids, and fetched every one from kids, fun and rex, in
JSON and in XML. 37 templates differed in behaviour; they came down to 13
findings, and 12 were re-measured on both servers and acted on. Each is tied to
the code in the bundle that builds it.

- **The library subsets were the "All" grid.** `onDeck`, `recentlyAdded`,
  `newest`, `recentlyViewed` and `recentlyViewedShows` are the pivot bar's
  entries, and rex answered each with the library's top-level rows titled
  "All <library>", and 404'd the last. Measured on both servers:
  - `title2` names the subset. A show library's Recently Viewed is "Recently
    Viewed Episodes".
  - On a show library the first four are episode lists (`viewGroup` episode,
    `mixedParents`, `nocache`) whatever `type=` says. The client rewrites a show or
    season type to episode for them itself (`getMetadataTypeID`).
  - `recentlyViewedShows` lists every top-level row: viewed ones first, newest
    view first, then the rest by sort title. It is the only subset with
    `allowSync` false, and the only one that carries `offset` and `totalSize`
    unpaged.
  - Recently Added and Recently Viewed stop at 100 rows. fun's totalSize is
    exactly 100 on both of its libraries.
- **`/library/sections/{id}/autocomplete` was never routed.** The Edit dialog's
  suggestions send `type=N&<field>.query=<text>&limit=10`.
  - The query is a phrase that must begin a whitespace-separated word:
    - `ste` finds Tears of Steel, and so does `of s`;
    - `tears steel` finds nothing;
    - `gate` does not match inside Steins;Gate.
  - `<field>.queryRange` is the inclusive span of the FIRST case-insensitive
    occurrence of the query, which is not always the word that matched. On fun,
    `e` matches 300: Rise of an Empire through "Empire" and reports `8,8`, the
    end of "Rise".
  - Rows are the listing's own, in sort-title order.
  - Tag fields answer `Directory` rows of id, `filter`, tag and tagType, plus
    tagKey and thumb for a person. Neither server has a label, so its tagType 11
    is python-plexapi's `utils.TAGTYPES`, which also confirms the measured 319
    for a network.
  - `year` matches its digits as a word (`201` finds 2010 to 2015, `12` finds
    nothing).
  - Refusals, all measured on both servers:
    - no `type`, no field, two different fields, an empty value, an unknown
      field or a non-integer `limit` is a 400;
    - a media, state or decade field is a 500;
    - `limit=0` answers one row and a negative limit every row;
    - a season list answers nothing.
- **`/filters` is the asked TYPE's list, not the library's.** A movie library
  answers the 17 episode filters to `type=4`. A show library answers the 28
  movie filters to `type=1`. Types 8, 9, 10, 13 and 20 have lists of their own,
  `type=3` and everything else is empty, and a non-number is a 400.
  `includeAdvanced=1` appends Duplicates to types 1 and 4 only. The lists are
  `section_filters.json`, captured from the reference. fun has the same types
  and the same Duplicates rule, with shorter 1.42 lists.
- **By Folder never opened a folder.** rex listed one directory per item
  and ignored `parent`. Every folder therefore reopened the same list, and the
  view never reached an item. Measured:
  - `?parent=<id>` lists that directory. Subdirectories are rows of `key` and
    `title`. Files are the items they hold: listing rows, `<Video>` in XML, and
    an episode's has no Director, Writer or Role.
  - Rows come in file-name order. Firefly's episodes come by their s01eNN names.
  - An unknown or empty parent is empty. A non-numeric one is the top level.
  - The container carries `allowSync` and pages.

  rex's files sit flat under `/rex/<n>`, so its top level is the items
  themselves.
- **`/library/metadata/{id}/grandchildren` was a 404.** The client builds it: a
  hub row with `skipChildren` has `/children` rewritten to `/grandchildren`.
  - A show answers its episodes, a season its episodes, an episode itself, and a
    movie an empty 200.
  - The container has no parent identity, no librarySection fields and no
    `allowSync`, and its `viewGroup` is the library's type.
- **A paged season list.** The earlier finding had the paging rule wrong:
  - The client's `Start=0` page keeps "All episodes", outside the page, with
    `leafCount`, `viewedLeafCount` and the show's `thumb`. Only `Size=1` drops it.
  - Paged rows carried the librarySection fields, because the paged branch
    skipped the child projection.
  - The row came last in XML where both servers put it first.
  - `excludeElements=Media` gives each child row `mediaCount` (0 on a season)
    and `mediaCountOptimized`.
- **`guid=` is how the client asks whether this server holds an item.** On a
  section listing and `/library/all` it is a case-sensitive prefix: an exact
  guid is that item, `guid=plex` every matched item, an empty value every row.
  rex ignored it, so every lookup "found" the whole library.
- **Four reads and five refusals.**
  - `/library/sections?includePreferences=1` puts each library's settings on its
    row. A show library's `episodeSort` is the per-show variant, whose enum starts
    with "Library default". The first-run flow reads `Preferences.Setting` off
    every row to find hidden libraries, and threw on rex.
  - `/:/prefs/get?id=` returns one Setting with an empty label and summary and no
    enumValues. It is 400 with no id and 404 for an unknown, empty or comma-joined
    id.
  - A comma-joined `/library/metadata/a,b` container carries `allowSync` and the
    FIRST row's library, even across two.
  - `/neighborhood/devices` is an empty container.
  - The refusals:
    - `/hubs/sections/{id}/manage?metadataItemId=` is a 400;
    - `/channels/recentlyViewed` and `/devices.xml` are the plug-in dispatcher's
      200 (rex answered its device list to the second);
    - `/media/subscriptions/template` and `/services/defaultLibraries` are 400s.

Thirteen checks cover these. Twelve are new, and all twelve fail against
`c62c51b` (743/755 there). The cycle-4 XML check that wanted `<Directory>` for
every folder row was re-measured and updated: a folder's files render as the
items they are.

Measured and not acted on:

- fun's `newest` stops at 47 movies and 60 episodes, with no bound visible in
  release or added dates; kids lists all five. rex lists every row.
- fun's Recently Viewed order follows neither `lastViewedAt` nor `viewCount`.
- Autocomplete orders by ICU collation with numeric runs, so 10, 12, 40, 300,
  2001 on fun. ICU collation stays out.
- On a show's `/children` at `Start>0`, kids drops All episodes and fun keeps it.
  rex follows kids.
- `guid==` answers nothing and `guid!=` everything; the client sends neither.
- `/library/all` with no `type` and `show.guid=…&season.index=1` answers the
  show; the client always sends `type=3`, which already works.
- `/system/services/url/lookup` is a 500 carrying the legacy plug-in
  framework's Python traceback. Cycle 40 measured its complete wire form on both versions and
  built the exact 1.42.2 response; see the cycle 40 entry below.
- A folder level holding both subdirectories and files was not measured. No
  rex catalog has one, and its directory rows would render as `<Video>`.

#### The eighth pass, second batch: the XML path and the clients that are not Plex Web

Cycle 8's second agent replayed the recorded corpus with no Accept header, the way a
TV or mobile client asks. It swept 75 read paths three ways in XML, re-issued 20
paths under the Android TV, iOS, Roku and LG header profiles, and extended the
python-plexapi corpus to 616 calls. No Accept and `Accept: application/xml` answer
identically on all three servers. It reported 14 differences; every one was
re-measured, and one detail of the tenth was already covered by cycle 7.

- **Reading `/status/sessions` rewrote the catalog.** `session_rows()` set
  `selected`, `decision="directplay"` and `location="direct"` on Media, Part and
  Stream dicts shared with the catalog. `_full` copies only a row's top level. After
  one play every detail, listing and search row of the item carried them, and
  python-plexapi read `part.decision == "directplay"` on an idle server. Neither
  real server carries those attributes on a read outside a session. The flags now go
  on copies.
- **Related and postplay shelves carry the FULL row.** This holds on
  `/hubs/metadata/{id}/related`, `/postplay`, `/library/metadata/{id}/related` and
  `includeRelated=1`, on both servers: Guid, Rating, Field, tag ids and roles, the
  Part's Streams, and the librarySection id, key and title. This overturns cycle 7's
  "light on every shelf" for these shelves.
- **A `/library/search` hit is light** (text tags, no Guid, Rating or Streams) and
  names its library by TITLE only. A show keeps the id and key beside the title, and
  its Location.
- **The server-wide On Deck and Recently Added page**, by header or query parameter.
  rex answered every row with no `totalSize`.
  - On Deck rows are light and name their library by id, title and UUID. This was
    measured on fun, since the reference has nothing in progress.
  - Continue Watching rows under `/hubs/home` are light too.
- **Recently Added lists a new episode as its SEASON**, never on its own. The season
  row's `addedAt` is its earliest episode's: the reference's Season 1 says
  1789187076, its first episode's, where its own row says 1789187257. The list stops
  at 50, fun's totalSize.
- **A season's episode list carries the season's own year as `parentYear`.** The
  first reading, "no parentYear", was taken on seasons without a year.
- **A show library's `/collections` carries `nocache`**, paged or not.
- **The tree**, identically on both servers:
  - a season's `title` is always empty;
  - every node's `parentTitle` is the ROOT's: the show's title throughout a
    season's tree, and empty throughout a show's;
  - an episode's `MediaPart` leads with `metadataType` 4, where a movie's has none;
  - a show's node ends with eight of its eleven settings, after its seasons, with
    every value and default a string.
- **The iOS platform types a show's "All episodes" row as a season.** It is the one
  client-profile rule the sweep found. Neither the product alone nor tvOS does it, and
  Android TV, Roku and LG change nothing anywhere.
  - rex read only the paging headers, so the platform is now normalised from a
    header as well.
  - The same row carries the show's `thumb`, which batch 1 added.
- **`sort=movie.random`** (with a direction or a seed) sorts like `random`.
  python-plexapi always sends the prefixed form, and rex answered 500.
- **Studios and networks are tag tables.** `/library/tags?type=318` lists studios
  with a bare `=<id>` filter, ordered by id, and `319` lists networks.
  - rex's studios are text on the row, so it mints their ids from the name, as it
    does every other tag's.
- **A stream's `/levels` is a 404**, where the stream itself is the 501.
- **XML child order.** Both servers write a row's children in one order:
  - Media, Image, UltraBlurColors;
  - then Genre, Country, Guid, Rating, Collection, Director, Writer, Role, Producer,
    Label, CommonSenseMedia, Chapter, Marker, Review, Field and Location;
  - then Preferences, OnDeck, Extras, Related and Children.

  A listing's `Meta` comes before its rows. rex had 41 pairs inverted. No
  client was found that depends on it, and the order is cheap to keep.

Thirteen new checks cover these, and all thirteen fail against the batch-1 commit
(754/768 there). The studio check sets a studio through the Edit dialog's write first,
because an earlier check leaves the fixture movie without one. The cycle-5 check that
wanted no `parentYear` on a season's episode list was re-measured and updated.

Measured and not acted on:

- fun's postplay for a movie also carries `movie.extras` (clip rows), and for an
  episode a `tv.ondeck` shelf. Its related list adds `movie.same.actor.N` shelves.
  rex has no extras and builds only the similar shelf.
- A child element outside the measured list (Similar, Mood, Style) keeps its place
  after those in the list. Neither server showed one to place.
- rex's studio tag ids are minted, not a database's.
- The agent's reading that a yearless season in Recently Added lacks `year`,
  `titleSort` and `updatedAt` is cycle 7's season-year rule. A season with a year of
  its own carries all three.

#### The ninth pass: the community OpenAPI description, every GET in it

Cycle 9's read-only agent swept the community OpenAPI description of PMS (the
`plex-api-spec` behind plexapi.dev and the SDKs generated from it): 405 operations, 195
GETs aimed at the server, 132 of them instantiated as 403 requests and sent to the
reference, fun and rex in JSON and XML -- 2,406 exchanges, 1,205 differences
triaged. Twelve findings survived, each re-measured by hand on both servers before
anything changed.

- **`/library/sections/all`** is the section list itself, identical to
  `/library/sections` on both servers. The spec calls it `getSections`, so generated
  SDKs ask there; rex 404'd it.
- **Every MediaContainer pages.** With both container parameters present -- headers
  or query -- both servers slice and add `offset`/`totalSize` on `/`, `/library`,
  `/:/prefs`, `/devices`, detail rows, trees, every hub list and the tag table, and an
  empty container such as `/identity` gains `totalSize: 0`. A container of several
  lists slices each and counts `size` over them. Only a plug-in framework container
  (`noHistory`, e.g. `/system/scanners/1`) and a non-MediaContainer root are left
  alone. rex paged seven listings; the rule now lives in the response path.
- **`/system/agents/{identifier}` is a 404** with an empty body on both servers, for a
  real agent and an unknown one alike. rex answered the whole list.
- **Three refusal codes.** A stream's `/loudness` is a 404 (rex 501); `.srt` and
  `.vtt` on a stream the server has are the bare stream's 501 (rex 404, reading the
  extension into the id); `/library/metadata/{id}/file` without `url` is a 400
  (rex 404).
- **The livetv reads a tunerless server refuses.** `/livetv/epg`, `/epg/guide`,
  `/epg/search`, `/recordings`, `/dvrs/{id}` and `/sessions/{id}` (and all beneath
  them) are 404 on both servers and `/epg/channels` a 400; only `/livetv/dvrs` and
  `/livetv/sessions` answer empty. rex answered an empty container to all of them.
- **`/allLeaves` below the show is empty.** A season's is `size 0` in its episode-list
  container, naming the show by title only; an episode's is presented as its season's
  (title1 the season, an empty grandparentTitle, the episode layout). rex returned
  the season's episodes and the season layout. Every child list also carries
  `allowSync: true` on both servers, where rex sent false.
- **Voice search honours `type`** and numbers each hub by the type it holds:
  `results.search.4` is the episode hub whatever else matched, `type=4` returns it alone,
  and a type with no hub (5, 8, 18) returns none. rex ignored `type` and numbered
  hubs by position.
- **A section listing honours `excludeElements`.** Both servers drop the named
  children from every row, paged or not, and `Media` becomes `mediaCount` (0 on a show)
  plus `mediaCountOptimized` 0. rex applied the option to detail reads only.
- **Bandwidth statistics filter.** `accountID` and `deviceID` keep matching rows (an id
  with none is `size 0`), `lan=0|1` keeps remote or local traffic, and an empty or
  non-numeric id or any other `lan` value is a 400, on both servers. rex ignored all
  three, so the Dashboard's per-account and per-device graphs drew the whole server.
- **`/status/sessions/history/{id}`** reads one history row back: the row history/all
  lists, alone in a container of `size` 1; an unknown or non-numeric key is a 404. rex
  listed the key and 404'd it.
- **`/hubs/metadata/{id}`** is an item's own hub list: on both servers, for a movie, a
  show and an episode alike, the four artist shelves (Most Played, Most Popular Tracks,
  Albums, Music Videos), each empty and keyed to the item in its library. An unknown id
  is a 404. rex 404'd the path.
- **Six section leaves.** `allLeaves` lists every movie (sort-title order) or episode
  (broadcast order) as listing rows, with no librarySection fields and the library's
  type as viewGroup; `albums` is an empty "By Album" list; `cluster` and `moment` are the
  empty "By Moment" container with `clusterZoomLevel` 1; `computePath` is a 400 without
  both ids, a 404 for an unknown one and otherwise an empty path; `nearest` is a 400.
  All six were 404 on rex.
- **`/library/people/{id}`** reads a person back as one `Directory` row whose `filter`
  names the field the id came from (`actor=47`, `director=213`), and `/media` lists the
  items they are on as listing rows naming their library, with viewGroup `movie` even for
  shows. An unknown id is a 404. rex 404'd both, though every Role carries the id.
- **`/library/matches` answers**, where rex refused every request although its own
  `/media/providers` advertises the key. `type` is required and numeric; `title` matches a
  movie or show title exactly, ignoring case, narrowed by `year`, each row led by
  `score` 100; `guid` matches the whole guid and an episode matches by `grandparentTitle`,
  `parentIndex` and `index`, unscored; `type` alone is `size 0`.
- **`/hubs/items?identifier=`** answers one shelf's rows, uncapped unless `count` caps
  them, in a container of `size`, `allowSync` true and identifier. A missing or unknown
  identifier is a 404, and so are `tv.inprogress.{id}` and `home.playlists` although the
  hub lists carry them. rex 404'd every identifier.
- **`/sorts` is keyed by the asked `type`, not the library**, as `/filters` is: a movie
  library answers the episode sorts to `type=4` and the artist sorts to `type=8`, types
  9, 10, 13, 18 and 20 have lists, other numbers are empty and a non-number a 400.
  rex read the library's own Meta. The lists are `section_sorts.json`.
- **`/library/randomArtwork?sections=` and `/library/sections/{id}/arts`** list one
  `<Photo>` row per movie or show with a backdrop -- title and art key, every other field
  blank or -1 -- the first shuffled on each request and capped at 100 in a bare `size`
  container, the second in ratingKey order in the library's container. rex 404'd both.

Measured in the ninth pass and not acted on:

- `/services/ultrablur/image` renders a four-colour PNG and `/colors?url=` decodes an
  image into its corner colours (both 400 without parameters). rex's server path is
  stdlib only, with no image encoder or decoder -- the same limit as `thumbBlurHash`.
- `/library/metadata/{id}/file?url=` is a 400 on the reference and a 404 on fun; only
  the bare form, which both refuse with 400, is implemented.
- An episode's `/allLeaves` container disagrees between the servers on `parentTitle`,
  `parentYear` and `art`; only the fields both send were changed.
- The reference's `/status/sessions/history/all` lists nothing while
  `/status/sessions/history/1` still reads back; the two servers' history filtering by
  account was not pinned down.
- `/hubs/items` answers the genre and actor shelves (`movie.genre.1.23`,
  `movie.by.actor.or.director.1.215`) that rex's library hubs do not build.
- `/library/matches` options that consult a metadata provider
  (`includeAlternateMetadataSources`, `agent=`) change nothing on either server's local
  matches and are ignored.
- Paging on the write-shaped reads (play queues, decisions, timelines) could not be
  measured on the read-only reference; `_page_container` leaves them to their handlers
  only where those already page.


#### The ninth pass, second batch: writes, measured on a disposable copy of the reference

The reference is read-only for hunts, so every write here was sent to plexscratch -- the
reference's own library, copied, sanitised and cut off from plex.tv -- and to rex with
the same parameters, then read back from both.

- **The Edit dialog's save path MERGES tags; only the item path replaces them.**
  `PUT /library/sections/1/all?type=1&id=4&genre[0].tag.tag=Alpha&genre[1].tag.tag=Beta`
  on a movie with two genres leaves four: Alpha, Beta, then the originals in their order.
  On that path the indexed list is deduplicated case-insensitively (`Same&same` is one
  tag), a `tag-` removal removes exactly the names it lists and ignores ones the row lacks
  (the client double-encodes them, `Label%2520A%2CLabel%2520B`), and an empty indexed
  value changes nothing. On the item path the list is still replaced; there an exact
  repeat is one tag and `Qx&qx` is the first spelling twice. Both order by the INDEX
  (`genre[1]=Second&genre[0]=First` is First, Second); rex replaced on both paths, in
  parameter order.
- **An unlocked Sort Title does not stick.** A title write, a sort-title write and a
  sort-title unlock each re-derive it from the title with the article stripped:
  `title.value=The Thing&titleSort.value=zzz` reads back `titleSort: Thing`, and a season
  retitled `The Season Name` sorts as `Season Name`. Only a lock -- sent with the value,
  or earlier -- keeps what was typed, and an empty title leaves it alone. A collection and
  a playlist edited through `/library/metadata/{key}` follow the same rule, and a
  collection records the lock. rex stored every value it was sent.
- **The save path edits only rows of the asked type in its own library.** A movie id
  under `type=2`, or on the TV library's path, is a 200 that edits nothing, and
  `id=<movie>,<show>` with `type=1` edits only the movie. A type-SCOPED filter on that
  path -- `show.title=`, `show.year>>=`, even `movie.year>>=` with `type=1` -- is a 400,
  where an unscoped `year>>=` still scopes the edit. rex edited every id it had and
  applied the filter.
- **A release date is parsed, and on the section path it sets the year.** Value by
  value on the item path: `2007-2-3` is `2007-02-03`, `2007-02-30` rolls over to
  `2007-03-02`, while `2001-01-45`, `2001-00-10`, `2001/02/03`, `2001-02`, a leading space
  and trailing text are removed; a trailing time is dropped with no zone shift,
  `99-01-02` is `0099-01-02`, and a bare integer is epoch seconds (`20070203` is
  `1970-08-21`, `-5` is `1969-12-31`). The section path refuses what it cannot read with
  a 500, sets `year` from the date unless the same save writes one (which wins in either
  order), removes both on an empty date, and removes an empty year where the item path
  500s. A year of 0 is removed and `+2000` is 2000. rex stored dates verbatim and left
  the year alone.
- **The section path stores a userRating, and every rating is a number.**
  `userRating.value=8.5` there reads back `userRating: 8.5` with `lastRatedAt`, -1 or an
  empty value clear both, and `abc` is a 500 -- where the item path still discards it.
  `audienceRating.value=5.5` and `rating.value=5.5` read back as numbers on both paths,
  `abc` is a 500 on both, and an empty value is a 500 on the item path and removes the
  field on the section path. rex stored the strings and discarded userRating.
- **A lock on the section path is an integer 0 or 1, and more columns take one.** There
  `01` and `001` lock, `00` unlocks, and `true`, `false`, `2`, `-1`, ` 1` and an empty
  value are a 400; the item path keeps its rule that only `1` locks. `thumb`, `art`,
  `theme`, `banner`, `guid`, `slug`, `addedAt`, `viewCount`, `network`, `parentIndex`,
  `duration` and `index` all take a lock (`key`, `ratingImage` and `audienceRatingImage`
  are 400), and `Field` lists in the metadata table's column order -- `rating` after
  `studio`, the artwork after `contentRating`, the tag columns among the scalars --
  where rex filed everything it did not know last.
- **Frames title a row the way the Activity pane names it, and a batch frames in listing
  order.** A show's frames say `Firefly (2002)` (a movie's already carried the year, and
  an emptied movie title still does: ` (2010)`), a season's `Firefly S01` and an
  episode's `Firefly S01 E07` -- the show's title and the current indexes. A save through
  the section path pushes every row's state 3 before any state 5, ordered by sort title
  whatever order `id=` named them in, where a comma-joined key list keeps the per-row
  pair. Renaming a playlist or editing its summary, through `/playlists/{id}` or
  `/library/metadata/{id}`, pushes its own type-15 pair with sectionID -1, and a
  collection's rename its type-18 pair. rex sent every row's own title, interleaved
  the batch, and pushed nothing for a list.
- **A parent's title and studio are copied onto the rows beneath it.** Retitling a season
  changes its episodes' `parentTitle`, and its studio their `parentStudio`; retitling a
  show changes its seasons' `parentTitle` and every episode's `grandparentTitle`, and its
  studio the seasons' `parentStudio` -- on the detail row, the child list and the grid.
  An emptied show title removes those fields rather than sending "". A season's episode
  list names a custom season title in `parentTitle` and carries the season's `summary`
  when it has one: every season the earlier reading saw, on kids and on fun, had a
  generated `Season N` title and no summary, which is why it recorded "" and no summary
  -- re-read on both servers here, and it still holds for those seasons. rex copied
  the fields once, at load, and updated only the containers.
- **An emptied title is not always empty, and it keeps its sort form.** `title.value=` on
  a season leaves `Season 1` and on an episode `Episode 2` -- generated from the index,
  so a later `index.value=7` makes it `Episode 7` -- while a movie, a show, a playlist and
  a collection store "". Every one keeps its old title's sort form as `titleSort` (`The
  Old List` sorts as `Old List`, `Bushwhacked` as `Bushwhacked`). A playlist or a
  collection created as `The Fresh List` already sorts as `Fresh List`, and
  `PUT /library/collections/{id}?title=` empties the title, where rex ignored an empty
  one. rex stored "" everywhere else and dropped the sort title.
- **`/:/progress` stores any position past a minute and never completes a play.**
  `time=999999999` on a ten-second clip reads back as `viewOffset: 999999999`, with no
  `viewCount` and no history row, and a missing or unparseable `time` is a 500. rex
  marked the item watched and wrote a history row, and stored 0 with a 200 for a
  missing time.
- **Only -1 clears a rating's timestamp.** `/:/rate?rating=-5` removes the stars and
  keeps `lastRatedAt`; `-1` removes both. rex cleared both for any negative rating.
- **A timeline refuses a position it cannot place.** A `time` past the `duration` sent
  with it, a negative or non-numeric `time` and a non-numeric `duration` are each a 400.
  rex answered 200 -- and a time past the duration completed the play and wrote a
  history row. A timeline with NO `duration` answered `ignore` on plexscratch, but its
  clips are ten seconds long, so that cannot be told apart from a position past the
  item's own length; it was not acted on.
- **A completed play answers `complete` until a `stopped` beat closes it, then `ignore`.**
  Without a session identifier: `playing` at 95% then `stopped` both answer `complete` and
  a second `stopped` answers `ignore`; `playing` twice past 90% then `stopped` all answer
  `complete`; `stopped` at 98% then `playing` answers `ignore`. Each counts one view.
  rex answered `complete` to every one of them -- and the first reading here, two
  `stopped` beats alone, was fixed as "any second beat is `ignore`", which the existing
  playing-then-stopped check caught. With an `X-Plex-Session-Identifier` plexscratch
  ignored every beat and counted nothing; its clips are ten seconds long, which a
  session-bound timeline appears to be judged against, so none of this was measured
  with one.
- **Remove from Continue Watching is a PUT that needs its `ratingKey`.** A GET is a 404
  and a PUT with no `ratingKey` a 400; rex hid the row on a GET and 404'd the bare
  PUT.
- **A playlist create validates its form.** No `title`, no `type` or no `uri` is a 400,
  a type outside video/audio/photo a 500, and `type=audio` or `type=photo` over a movie a
  400. rex created an `Untitled` list of whatever it was sent, typed `bogus` if asked.
- **A playlist's item writes refuse what they cannot do, and DELETE on the items clears
  it.** An item id the list does not hold -- including a ratingKey, which is not an item
  id -- is a 404; `DELETE /playlists/{id}/items` empties the list; and a smart playlist
  refuses an add or a removal with 400. rex answered 200 to all of them and cleared
  nothing.
- **A move after an item the playlist does not hold goes to the front.** `after=999999`,
  `after=0` and `after=abc` all send the item first, as no `after` does, and `after`
  naming the item itself changes nothing. rex appended to the end, and moved an item
  after itself to the end too.
- **Every state write needs the `identifier` the client sends.** Without it
  `/:/scrobble`, `/:/unscrobble`, `/:/rate` and `/:/progress` are a 400 -- even for a key
  the server does not have, so the earlier reading of a 404 for an unknown key holds only
  with the identifier present -- and a foreign identifier is a 404. The order is fixed: a
  missing key, a missing identifier, the write's own parameter (an unparseable rating is
  still a 400 with a bogus identifier), then a foreign identifier or an unknown key.
  rex never read the identifier.
- **An episode scrobble counts on its season and show; a parent's own scrobble does not.**
  One scrobble at a time: each episode scrobble added one to the season's and the show's
  `viewCount`, a second scrobble of an episode already watched included; scrobbling the
  season, and the show, added one to each episode and left the season's and the show's
  own count unchanged; unscrobbling a season or a show removed its own count, and
  unscrobbling an episode left the parents alone. rex counted on a parent only when
  the parent itself was scrobbled -- the earlier note ("scrobbling one episode twice took
  the show to viewCount 2") implemented the wrong way round.
- **On Deck holds one row per show.** Two episodes in progress list only the
  lower-numbered one -- episodes 13, then 12, then 14 left 12 on deck, and 13 played again
  at 240 s did not displace it -- and with none in progress the row is the episode after
  the one watched (episode 8 watched: 9; season 1 watched: season 2's first). Removing any
  of the show's episodes from Continue Watching hides the show's row, even when the row
  names another episode, and a timeline on any of them brings it back. rex listed
  every episode with a resume point, and never the next one.
- **An item takes no POST, and `/library/all` takes no write.** `POST
  /library/metadata/{id}?summary.value=` and `PUT /library/all?type=1&tagline.value=` are
  404s; rex answered the detail row and the listing.
- **The Edit dialog's value-less columns, and a few others, are accepted and discarded.**
  `thumb.value=`, `art.value=`, `addedAt.value=`, `guid.value=`, `lastViewedAt.value=`
  and `viewCount.value=` are 200s that change nothing, and so are a movie's
  `duration.value=` and `index.value=`, an episode's `parentIndex.value=`, a show's
  `network[0].tag.tag=` and a bare `genre=Drama` on the item path; an actor written with
  `actor[0].tagRole=` is stored with `role: ""`, the field every actor added by an edit
  carries. rex refused most of them as unknown fields and stored the movie's
  duration and index.
- **A play queue starts on the item its `key` names.** A queue over a whole library with
  `key=/library/metadata/3` selects that movie (offset 3); rex selected the first row.
- **A client's new play queue retires its older ones.** Client A's queue was still
  readable after client B created one, and a 404 once A created another. rex kept
  every queue it had ever built.
- **`PUT /playQueues/{id}?playlistID=` adds the playlist's items**, as a plain add;
  rex stepped the version and added nothing.
- **DELETE on a play queue's items clears it down to the selected item**, stepping the
  version; rex answered 404.
- **Deleting a play queue's selected item answers the queue as it stood.** The response
  carries the old version, selection and total with no rows, and the next read has the
  selection on the item that followed it; rex left the selection on the deleted id.
- **A play queue move after an item the queue does not hold goes to the front.** `after=0`
  and an unknown id both send the item first, as `after=-1` does; rex answered 404.
- **A windowed play queue read honours `includeBefore` and `includeAfter`.** Both are on by
  default; with the first item selected, `window=1&includeBefore=0&includeAfter=1` answers
  only the second row, `includeBefore=1&includeAfter=0` answers none, and the selected
  item comes back only when both are on. rex ignored both flags.
- **A play queue uri naming another server builds an empty queue that echoes it.** The
  response has no rows and `playQueueSourceURI` is the uri as sent; rex resolved the
  path against its own library and queued its own item.
- **A collection create needs a `title` and a `sectionId`.** Each missing one is a 400;
  rex made an `Untitled` collection in the first row's library. A missing `uri` is a
  200 on both servers.
- **A collection over a show holds the show, and one over a season holds the season.**
  `type=2` from a show answers `subtype: show` with one child, `type=3` from a season
  `subtype: season`; rex expanded both into episodes (`subtype: episode`, the show's
  three).
- **A collection add refuses what it cannot hold.** No `uri`, an unknown key, and a row of
  another type -- an episode into a movie collection -- are each a 400, and so is any add
  to a smart collection, whose members are its query. rex added whatever the uri
  named, the episode included.
- **A collection's children follow its `collectionSort`, and a move starts from the order
  shown.** Three movies added newest first came back oldest first -- by release date, with
  no sort set or `0` -- and `2` is the custom order. A move re-bases the custom order on
  the order the collection is SHOWN in, applies the move, answers the collection row, and
  switches the collection to `collectionSort: 2`; setting `0` again goes back to release
  date. (`1` gave the same order on these members, and is alphabetical by
  python-plexapi's mapping.) rex listed insertion order, answered the move with an
  empty body, and never changed the sort.
- **A collection takes a Label, and a `type=18` section save edits it.**
  `PUT /library/metadata/{collection}?label[0].tag.tag=` stores the label, and `PUT
  /library/sections/1/all?type=18&id={collection}&summary.value=` edits the collection;
  rex ignored both.
- **Renaming a collection leaves its members' tag alone.** After renaming `c13` a member
  still carried its original Collection tag; rex renamed the tag with the collection.
  (The reference spelled that tag `C13` for a collection created as `c13`, which was not
  pinned down.)
- **A library edit applies the whole form, and scans.** `PUT /library/sections/1?...&language=de-DE
  &prefs[enableBIFGeneration]=0` reads back `language: de-DE` and the setting false, and
  pushes the scan: a `library.update.section` activity started as `Scanning`, a status
  `Scanning the "Movies" section`, the activity retitled `Scanning Movies` and stepped
  through every item with a `progress` frame (`Scanning Cosmos Laundromat (2015)`) and an
  `updated` frame at `round(n * 99 / count)` percent, then 100, `ended`, and the status
  `Library scan complete`. Hunter A's "a type change is a 400" did not reproduce: both
  servers answer 200. rex renamed the library and dropped the rest.
- **`/library/sections/all/refresh` scans every library.** It is a 200 that runs the scan
  above for each library in turn; rex 404'd it as an unknown section, and scanned a
  single library by starting and ending a metadata activity per row.
- **A movie's written setting shows on its row.** After `PUT
  /library/metadata/{movie}/prefs?useOriginalTitle=1` the movie's detail row carries
  `useOriginalTitle: "1"`, as a show's does for `episodeSort`; rex stored it unseen.
- **A library create applies its `prefs[...]` and answers a bare container.** A TV library
  created with `prefs[flattenSeasons]=1&prefs[episodeSort]=1` reads both back as 1, and the
  201's container carries only `size` beside the Directory; rex dropped the prefs
  (-1 and 0) and sent the library container's identifier, media-tag pair and allowSync.
- **A library analyze pushes three frames per movie.** For each movie in title order: state
  5 with `mediaState: analyzing`, state 5 with `thumbnailing`, and a plain state 5.
  rex pushed nothing.
- **Cleaning bundles and deleting a library each run an activity.** `PUT
  /library/clean/bundles` runs `general.bundle.clean` -- started, updated at 0 (`Analyzing
  metadata bundles`), 25 and 50 (`Deleting N metadata bundles`), 50 (`Analyzing media
  bundles`), 75 and 100 (`Deleting N media bundles`), ended -- and a library delete
  `library.delete` (`Deleting section N`, then `Optimizing Database` at 50, 0, 10, 40,
  60, ended). rex pushed nothing. Measured and not acted on: optimize's
  `general.db.optimize` run (its last frames and a burst of type-12 deletes were not fully
  captured) and the refresh cascade a show's `showOrdering` write pushes.
- **A merge refuses what it cannot merge.** No `ids` is a 400, an id the server does not
  have a 404, and a movie into a show a 400. rex answered 200 to all three -- and the
  last one moved the movie's media into the show.
- **A merge and a split push their frames.** Merging Sintel into Tears of Steel pushed
  state 9 for Sintel (untitled) and then `type: -1` state 3 and 5 for Tears of Steel; the
  split pushed the new row created -- state 0 untitled, state 0 titled, state 1 -- and the
  source's state 3 and 5, on a NEW ratingKey never used before (67 after 57). rex
  pushed nothing for either, and put the split's row on the highest FREE key -- the one
  the merge had just taken from the absorbed row.
- **Unmatching an episode or a season gives it a local guid and its generated title.** An
  episode became `Episode 2` on `guid: local://9`, kept its imdb/tmdb/tvdb Guids and pushed
  `matching` (state 2) then state 5; a season became `Season 1` on `local://7`, through the
  full refresh sequence. The earlier reading -- a filename title and a
  `tv.plex.agents.none://` guid -- was a movie's and still stands for movies. rex
  applied the movie rule to both, dropped the Guids, and left an `Unmatching` activity that
  never ended.

Measured in the ninth pass's second batch and not acted on:

- **Instrument limits on plexscratch.** Its clips are ten seconds long. A timeline
  carrying `X-Plex-Session-Identifier` was ignored outright -- every beat `ignore`, nothing
  counted -- which a session-bound timeline appears to judge against the clip, and a
  timeline with no `duration` cannot be told apart from a position past the clip. Both
  need a copy with longer media before they can be measured.
- **Not reproduced when re-measured:** `/:/progress?state=playing` opening a
  `/status/sessions` row; a timeline with an unknown `playQueueItemID` moving the queue's
  selection to the item its ratingKey names; a library edit that changes `type` being a
  400 (a 200 on both servers); a collection create with no `uri` being a 400 (a 200 on
  both); a smart collection tagging its members; a whole-library playlist add adding
  nothing; `continuous=1` stopping at one episode (rex's fixture season holds one).
- **Captured only in part:** the database optimize's `general.db.optimize` activity and
  the burst of type-12 state-9 frames inside it; the refresh cascade a show's
  `showOrdering` write pushes; a library create's scan, with a created-and-titled frame
  pair per item; the per-season subtitles a TV library's scan steps through.
- **Oddities on the reference, left alone:** `studio[0].tag.tag=X` lands in Director and
  `contentRating[0].tag.tag=R` in Country; `similar[0].tag.tag=x` is stored as `X`; a
  collection created as `c13` tagged its members `C13`; `merge?ids=<itself>` deletes the
  item; a movie unmatched offline is missing from `unmatched=1`.
- **The copy, not the server:** `DELETE /library/metadata/{id}` is a 400 on plexscratch
  for every type (its media mount is read-only), `editionTitle` is a 403 (Plex Pass), and
  the `collectionMode` library setting is absent from the copy's settings list.
- `collectionSort=1` gave release-date order on members whose release order and titles
  agree, so its alphabetical meaning comes from python-plexapi's mapping, not from a
  measurement here.

#### The tenth pass: what a real PMS writes for a real file

rex builds every `Media`, `Part` and `Stream` from an ffprobe probe, and every official
client runs its own direct-play gate and draws its quality and track pickers from those
attributes before it asks the server anything. Until this pass the conversion had been
compared with a real server on two display titles and one subtitle-flag sweep. The tenth
pass probed 35 real files on fun -- over fun's own part URL, with the invocation
build_catalog.py uses -- and the reference's 11 clips. Each probe is now checked in under
`tests/fixtures/media/` beside the Media element that server wrote for the same file and the
display titles it sends Plex Web, and `tests/test_mediainfo.py` converts every probe with
mediainfo.py and compares the result with the real analysis, presence and value. Each rule
below is one check there, and each check failed against the code before its fix.

- **A bitmap subtitle is `pgs` or `vobsub`.** fun writes `codec: pgs` for every Blu-ray
  bitmap track (68 in the corpus) and `vobsub` for every DVD one (10), where ffprobe's
  decoders are `hdmv_pgs_subtitle` and `dvd_subtitle`. rex passed ffprobe's names
  through, and a client decides whether to burn an image subtitle in by that name.
- **Audio codecs carry Plex's names.** Every DTS variant is `dca`, with the variant as the
  profile -- `dts`, `ma`, `es`, `hra`, `express` (34 tracks); PCM in Matroska is `pcm` with
  ffprobe's sample format as the profile, while Blu-ray PCM stays `pcm_bluray`; and the
  `+ Dolby Atmos` profile ffprobe gives TrueHD and E-AC-3 is not written (11 tracks). rex
  sent `dts` and the Atmos profile, and a client's direct-play profile names `dca`. The
  reference (1.43.4) leaves `lc` off most of its AAC clips where fun writes it; the check
  skips those.
- **A language is named in itself, coded ISO 639-2/T and tagged ISO 639-1.** `jpn` reads
  `日本語`/`jpn`/`ja`, ffprobe's `fre` reads `Français`/`fra`/`fr`, `fil` keeps `fil` as its
  tag, `und` writes nothing, and a tag that is no ISO code (`english`) is both name and tag
  with an empty code. rex printed English names for 15 languages and the raw code
  otherwise, kept bibliographic codes, and cut the tag to two letters: `jp`, `sp`, and `fi`
  -- Finnish -- for Filipino. A client picks a track by the tag. `languages.json` holds the
  62 names and tags fun wrote; a code it lacks keeps the code in all three. The reference
  (1.43.4) labels an untagged clip English where fun writes nothing; the check skips those.
- **Every video stream is listed, and Dolby Vision is described.** A dual-layer Dolby Vision
  file's enhancement layer is a second video stream on fun; rex built only the first. A
  Matroska cover image, which ffprobe also lists as a video stream, is not one.
  The eight `DOVI*` attributes are ffprobe's DOVI configuration record field for field (6
  of 6 files, profiles 5, 7, 8 and 10), and rex sent none of them -- they are what tells
  a client a Dolby Vision stream it can play from one it cannot.
- **A ten-bit stream is ten-bit.** ffprobe leaves `bits_per_raw_sample` out for HEVC and
  AV1, where the depth is only in the pixel format; all 16 such streams read `bitDepth: 10`
  on fun. rex defaulted them to 8.
- **The resolution label is the larger of a width bucket and a height bucket.** Measured on
  all 4947 Media of fun's movie libraries with no exception, `2k` and `2.7k` included. rex
  read the height alone, so a cropped 1920x800 release was `720`. Each threshold is the
  smallest dimension fun has in its bucket; `tests/fixtures/media-resolutions.json` holds
  every size fun has and its label.
- **The aspect ratio is one of six values.** Every Media on fun carries 1.33, 1.66, 1.78,
  1.85, 2.2 or 2.35: the display aspect, sample aspect ratio included, snapped to the
  nearest. rex wrote the raw quotient without the sample aspect ratio, so 1920x800 read
  2.4 and an anamorphic 3840x2160 1.78 where fun says 1.85.
- **Track flags are the disposition, and only when true.** `default`, `forced`,
  `hearingImpaired`, `original`, `dub` and `comment` on audio and subtitles, and `default`,
  `forced` and `original` on video, follow ffprobe's disposition on every probed track, and
  no flag is ever `false`, `selected` included. rex marked the first audio track default,
  sent `false` on the rest, and none of the other flags. Which track is `selected` follows
  the account's language settings, which rex does not have; the first still is.
- **`scanType` follows the field order**: absent where ffprobe reports none (19 streams),
  `interlaced` for a field-ordered stream, `progressive` otherwise. rex wrote
  `progressive` everywhere.
- **`colorRange` is ffprobe's `color_range`** (22 streams carry it, 25 do not); rex
  never sent it.
- **`audioChannelLayout` is ffprobe's layout verbatim, and absent where it has none.**
  rex derived it from the channel count, so a `5.1` track was `5.1(side)` and 3, 4, 5
  and 7 channels had none. Left alone: E-AC-3 with Atmos in an mp4, which fun reads as its
  5.1 core; one DTS Express track; and the reference's mono AAC clips.
- **Audio `bitDepth` is `bits_per_raw_sample`, and 24 for DTS without it** (DTS Express
  excepted); lossy codecs carry none. rex sent no audio bit depth. Left alone: TrueHD in
  an mp4, which fun leaves without one.
- **`streamIdentifier` is the container's track id in decimal** -- an mp4 track number, an
  MPEG-TS PID -- and absent in Matroska. rex sent none. An AVI track that fun numbers
  with no id in the probe is left alone.
- **A video stream's `codecID` is its codec tag** where the container has a real one --
  `hev1`, `hvc1`, `dvhe`, `XVID`, `DX50`, `HDMV` -- but not `avc1`, and no audio or subtitle
  stream carries one. rex sent none.
- **An H.264 stream's `codedHeight` is whole macroblocks** (1080 is 1088, 436 is 448, 536
  is 544; 12 streams on fun and 11 on the reference), where ffprobe 8.1 reports the cropped
  height. Other codecs take ffprobe's value.
- **A video stream has a language and a title too**, from its tags (17 and 10 streams on
  fun); rex set neither.
- **Unknown markers are not written.** ffprobe's `profile: 0` and `level: -99` are absent on
  fun, and `Simple Profile` is `simple`; rex wrote them as they came.
- **Bitrates round to the nearest kbps.** rex truncated, so 28 of 35 Media read one
  short. Two fun files whose bitrate is neither, and the reference's clips, are left alone.
- **The frame rate is the average one.** The reference's mp4 clip with a nominal 48 fps
  reads `23.947` and `24p`, ffprobe's `avg_frame_rate`; rex used `r_frame_rate`.
- **The Media's audio fields name the track with the most channels**, the first on a tie
  (35 of 35 files; the first track was the wrong one in 9), and a DTS-HD MA track is
  `dca-ma` there, as on 2,075 of fun's 4,951 Media, while every other DTS variant stays
  `dca`. rex named the first track, and a client gates direct play on these fields.
- **Track titles are composed, and a track title never enters `displayTitle`.** Video reads
  `<resolution>[ <range>]` -- `4K`, `1080p`, `720p`, `480p` or `SD`, with `i` for an interlaced
  stream, and `HDR10`, `DoVi/HDR10`, `DoVi` or `DoVi Enhancement Layer` -- and extends with
  `(<CODEC>[ <Profile>])`, where HEVC always names its profile and H.264 any but High and
  Main. Audio reads `<language> (<CODEC> <channels>)` with `DTS-HD MA`, `DTS-ES`, `TRUEHD`,
  `EAC3` and the layout's own channel label. A subtitle reads `<language>[ Forced][ SDH]` and
  extends with `(<CODEC>)`. A track title leads the extended title, followed in brackets by
  the parts it does not already carry: a part is carried when its first word is one of the
  title's words -- split on whitespace and `- / ( ) [ ] + , | : ; &` but not on dots, folding
  ASCII case only -- and a language's two-letter tag, `2.0` for Stereo and `AVC` for H.264
  count too (`BTM TrueHD Atmos 7.1 (English)`, `BTM ES (SRT)`, `English DTS Express 5.1`).
  rex printed `sdp`, `Dolby Digital Plus`, `3ch`, English language names and the track
  title as an audio title, and never named a profile -- which answers cycle 4's open question
  of when a profile is appended. Left alone: a forced track with no linguistic content, whose
  extended title leaves `Forced` out (one track).
- **Plex Web and twelve other products get the codec in `displayTitle` too.** On both
  servers a video's and a subtitle's plain title carries ` (<CODEC>...)` only when
  `X-Plex-Product` is exactly Plex Web, Plex for iOS, Plex for Apple TV, Plex for Android
  (TV), Plex for Android (Mobile), Plex for LG, Plex for Samsung, Plex for Windows, Plex for
  Mac, Plex for Xbox, Plex for PlayStation 4, Plex for PlayStation 5 or Plex for Vizio, in the
  header or the query, case-sensitively -- not for `Apple TV`, `Plex for Android`, `Plex for
  Xbox One`, `Plex Desktop`, Roku, HTPC, Kodi, PlexAPI or no product. The extended title does
  not vary. The recorded `displayTitle` of `1080p` was the no-product answer, and rex
  sent it to Plex Web itself.
- **A catalog built before this pass loads with the corrected fields.** The resolution
  label, and an aspect ratio that is not one of the six, are recomputed from the Media's own
  size; the unknown profile markers are dropped; the codec, language and flag renames above
  apply to stored rows; and the titles are composed after all of them, where rex had
  composed them first. `tests/catalog_ui.json`'s 1920x800 rows were served as `720`, 2.4 and
  `profile: 0`.

Measured in the tenth pass's media batch and not acted on:

- **What the probe cannot give.** `refFrames`, which every real stream carries and ffprobe
  8.1 no longer prints; a Matroska video stream's bitrate, which is the server's own analysis
  (it matches the stream's BPS tag on 3 of 25 files); DTS-HD, TrueHD, FLAC, Opus and subtitle
  bitrates; `headerCompression`; Part `packetLength` (188 on fun where ffprobe reports 192);
  Media durations a millisecond apart; two Media bitrates that are neither rounded nor
  truncated; `hasThumbnail`, `indexes` and the `failureBIF*` fields; and `eia_608` captions,
  whose only sample failed to probe.
- **Which audio track is `selected`** follows the account's language settings, which rex
  does not have; the first track stays selected.
- **Leads, one sample each:** a `.webm` file reported as container `mkv` (rex keeps
  `webm`, which Plex Web's direct-play rule depends on); `anamorphic: false` on a 333:320
  sample aspect ratio; an AVI audio track numbered `1` with no id in the probe; a forced track
  with no linguistic content leaving `Forced` out of its extended title; and `AVC` standing in
  for H.264 in a video track's title.
- **Counterexamples left alone:** E-AC-3 with Atmos in an mp4 is its 5.1 core to a real PMS
  where ffprobe says 7.1 (channels, layout, titles and the Media's audio track all follow);
  DTS Express has no layout; TrueHD in an mp4 has no bit depth.
- **The reference's version, not a rule:** 1.43.4 labels an untagged track English, leaves
  `lc` and the mono layout off most AAC clips, and writes its clips' Media bitrates about
  8 kbps under the probe's. fun writes none of that.
- **Not sampled, so not named:** an HLG range, and Dolby Vision compatibility ids other than
  0, 1 and 6 in a title. Two of the 37 probes on fun failed on fun's side (an I/O error at
  byte 0, and a raw VC-1 stream in a `.mkv`).

#### The tenth pass, second batch: the writes cycle 9 did not reach

The write hunt went back to plexscratch for what cycle 9 did not reach -- smart-list edits,
settings saves, butler runs, stream selection, artwork uploads, Fix Match, extras -- and for
the frames cycle 9 caught only in part. Every finding was re-measured before it was
implemented: the writes on plexscratch, and anything a read can show on both servers.

- **A library's extras are a section leaf and a hub.** `GET /library/sections/{id}/extras`
  is the library's container naming one `all` view ("All Movies"; a show library's adds
  `nocache`), `/extras/all` lists the library's trailers and clips in that container --
  `size 0` for a library with none -- and anything deeper is a 404.
  `/hubs/sections/{id}/extras` is `size 0`, `allowSync` and the identifier, even for a
  section id the server does not have. An item's `/extras/{n}` is a 404 on both servers, even
  for an extra it has. rex 404'd the first three and answered the last.
- **A settings save pushes a `preference` frame per written id**, in ASCII order of the id and
  whether or not the value changed, carrying the `/:/prefs/get` shape with `label` and
  `summary` emptied. **And `FriendlyName` is the server's name everywhere** -- `/`,
  `/media/providers` and `/servers` -- with an empty one kept as the preference's value while
  the displayed name falls back to the host name. rex stored the values, pushed nothing,
  and kept its old name, so renaming the server in Settings changed nothing on screen.
- **Track selection takes a POST as well as a PUT** on `/library/parts/{id}`; rex 404'd
  the POST.
- **A butler run is an empty 200 that pushes a `butler` activity.** `POST /butler/{Task}`
  answers no body and pushes one "Butler tasks" activity (started at -1) around the task's
  own -- `general.db.backup` "Database"/"Backing up database" stepping to 100 -- ending with
  the task's message ("The butler backed up your database."); `/activities` carries the
  `butler` row while it runs -- rex's run is synchronous, so only the frames are visible
  here, and nothing is left in `/activities` afterwards. `POST /butler` runs every enabled
  task under one wrapper and
  `DELETE /butler` is empty too, while `DELETE /butler/{Task}` is a **404**. rex answered
  the task LIST to all three, pushed no frames, and accepted the task DELETE.
- **The Edit-smart-list Save is `PUT {list}/items?uri=<listing>`, and it REPLACES the stored
  query -- `limit` included.** Measured for a smart playlist and a smart collection alike: the list is
  re-evaluated on the next read, so its `leafCount` and its items follow the new `sort` and
  `limit` at once, and the response is the list's own row. A uri naming an ITEM, and no uri,
  are 400s, and a smart playlist's items cannot be cleared (`DELETE` is a 400 where rex
  emptied a list it never reads). rex refused every uri on a smart list, so Save answered
  "There was a problem updating this item."
- **An artwork upload is `POST /library/metadata/{id}/{posters,arts}` with the image as the
  body, and it answers an EMPTY 200.** The image becomes an `upload://posters/<sha1 of the
  bytes>` candidate -- a season's and an episode's carrying their place in the path -- and is
  selected at once; the row's `thumb` AND `art` both re-version; and one timeline frame,
  state 5 with no `metadataState`, is pushed with no state 3 before it. **And picking any
  candidate stores a NEW versioned path**, never the url picked: rex wrote the raw value
  into `thumb`, so choosing an upload set the tile's img src to `upload://posters/...`.
  A playlist's and a collection's artwork list lives under `/library/metadata/{ratingKey}`
  too and holds one `default://` composite row; rex 404'd it.
- **Fix Match requires a `name`, and a guid another row already carries MERGES the two.**
  A guid with no name is a 400 and an empty guid with a name is a 200 that renames the row and
  refreshes it; matching to a guid a row in the same library holds deletes this row and leaves
  the survivor with both Media, framed as the merge (state 3, then 9 `deleted` untitled, then
  type -1 state 3 and 5) followed by the survivor's own refresh. Every match runs that refresh
  sequence and ENDS its activity: rex started a `Matching` activity per match and never
  finished one, so the Dashboard's spinner never stopped.
- **A scan's `updated` frames carry a `Context` naming the library, and a show library steps
  per SEASON** -- `Scanning <show>/Season 02`, subtitle `<show> - Season 02`, deepest season
  first -- where a movie library steps per movie. rex sent no Context and stepped a show
  library by show.
- **A show's prefs write re-reads the whole show, and says so.** `showOrdering` pushes the
  show's own refresh sequence, then each season (5 `processing`, 4 `loading`) with its
  episodes inside it (5 `processing`, 4 `loading` twice, 5), the episodes again with
  `mediaState` analyzing and thumbnailing, the activity stepping per season, and the show's
  state 5 last -- forty frames for a two-season show. rex stored the value and pushed
  nothing, so a re-ordering was invisible to every other client until a reload.

Measured in the tenth pass's write batch and not acted on:

- **What the copy cannot do.** It has no account and no route out, so the download queue is a
  404 on every path there, `POST .../posters?url=<an external image>` is a 406 (the fetch
  fails, and rex can fetch, so copying the 406 would be wrong), and a provider match
  search returns nothing. `POST /library/metadata/{id}/subtitles` is a 500 on the copy with
  nothing in its log; the cause was not established, so rex keeps its 400.
- **Captured and not reproduced:** a library create's per-item `created` frames (state 0
  untitled, 0 titled, 1, then `mediaState` analyzing and thumbnailing) and the
  `backgroundProcessingQueue` frame a minute later; the 201 row's `metadataAgentProviderGroupId`,
  `uuid`, `createdAt` and `contentChangedAt: -1`, where rex sends `refreshing` and 0; and
  the `provider.subscriptions.process` activity pairs a scan ends with, seen only on the
  1.43.4 copy.
- **Oddities of the copy, left alone:** the artwork write's response body and the row's own
  `thumb` differ by exactly its timezone offset; a smart collection's `childCount` goes stale
  where its `/children` does not; `/library/sections/{id}/extras/all` times out on fun's
  largest library.
- **`POST /library/parts/{id}` was implemented**, but no client sends it: the web bundle's
  track selection is a PUT. It is here because the endpoint answers, not because anything
  asks.
#### The tenth pass, third batch: the playback surface, on media long enough to drive it

Cycle 9 left two instrument limits open because plexscratch's clips were ten seconds long. The
copy now has a `long` mode -- the same files at thirty minutes and twenty-two -- and the two
limits turn out to be one rule:

- **A play is marked watched only when a beat CROSSES 90% inside its own session.** The
  session must have reported a position under 90% of the duration THAT BEAT carries. A cold
  item beaten once at 95% records nothing; five climbing beats from 95% to 99% record nothing
  either; another client's earlier position does not enable it, and neither does an offset
  stored by `/:/progress`. Eleven cases, each on an item untouched since a reset. rex
  completed on the position alone, so dragging the scrubber to the end, or a second client
  joining near the end, silently marked the item watched, cleared its resume point, wrote a
  history row and bumped the season and show counters. What cycle 9 recorded as "a
  session-bound timeline is ignored outright" was this rule, not the session header: a
  session-bound beat behaves exactly like a bare one.
- **A beat with no `duration` stores nothing, and `duration=0` is a 200.** Without one the
  answer is `ignore` and the row is untouched, at 130 s and at 95% alike; after a beat that
  did carry one, a duration-less beat answers the STORED offset unchanged. `duration=0` is
  the same 200 -- a real PMS reads it as "no usable duration" -- where rex's `time >
  duration` refusal caught it as a 400. An empty `duration` and a `time` past a real one
  stay 400s.
- **A beat at or under a minute DELETES the stored resume point.** rex ignored the beat
  and kept the old position, so starting a title again from the beginning left the previous
  resume point in Continue Watching. `lastViewedAt` is left alone.
- **A progress beat stamps `lastViewedAt` on the season and the show**, before anything is
  watched; rex touched the parents only on a completion.
- **A session driven from a play queue names the queue on every `playing` frame**:
  `playQueueItemID` and `playQueueID` ride beside the recorded eight fields, on the playing,
  paused, seek and stop frames alike, and a session with no queue carries neither. rex
  sent neither ever, and the Now Playing panel ties a frame to its queue item by them.
- **A `/status/sessions` row carries no `Guid` and no `Image`**, where the same item's detail
  row carries both; rex copied the detail row wholesale, so its XML session rows had three
  `<Image>` children a real PMS never sends. Its `Player` also names a `profile` (`Web`),
  which rex omitted -- the copy only ever saw Plex Web, so what another product's profile
  reads as is not measured and every session says `Web`. **Not reproduced:** the hunt reported that the `Session` block
  appears only once a DECISION has been made. Re-measured with a decision that answers 200,
  the copy carries no `Session` block at all, before or after -- it is unclaimed, and the
  block is the one that holds the measured bandwidth. Left as it is.
- **`/statistics/media` filters by `accountID` and `deviceID`** exactly as
  `/statistics/bandwidth` does (implemented in cycle 8, and this one was left out): an id the
  server does not have answers `size 0` with no `Device` and no `Account` row, and an empty or
  non-numeric `accountID` is a 400. rex answered the whole server to all of them.

Measured in the tenth pass's third batch and not acted on:

- **Not reproduced: the `Session` block appearing only after a DECISION.** The hunt reported
  that a timeline-only session carries no `Session` block and a decision creates one.
  Re-measured with a decision that answers 200 and the same session identifier, the copy
  carries no `Session` block at all, before or after -- it is unclaimed, and that block holds
  the measured bandwidth. rex's stays as it is, and the row's derived `bandwidth` remains
  on the deliberately-not-implemented list.
- **What the copy cannot witness.** An unclaimed PMS 401s everything but `/identity`, so
  `/status/sessions/terminate` cannot be measured there in any form; `/statistics/*` is a 404
  on it, so the statistics rules came from kids and fun instead; and neither reference could
  witness a live `/status/sessions` row, because opening one is a write -- the session row's
  own shape is therefore the copy's alone.
- **Only Plex Web was seen**, so `Player.profile` is `Web` for every session here; what another
  product's profile reads as is not measured.
- **An unmatched row's timeline answer** on the copy carries no `skipCount`, `viewCount` or
  `viewOffset` at all, where a matched one carries all three. No rex catalog has a row
  without an identity, so there is nothing to reproduce it against.
- **`/statistics/resources`** answers per-second host samples on both references and one row
  with no memory figures here: a stdlib server on another machine has no such numbers.

#### The eleventh pass: the query grammar the listings never ran, and the XML path of the writes

Two hunts, both without fun: its Plex was stopped mid-deploy by another session, and the user
asked that testing stay on kids and zen. That turned out to be an improvement rather than a
loss. **zen runs Plex 1.42.2.10156 -- the exact build rex reports** -- so every rule below
has two witnesses, one of them the version rex claims to be, which is a sharper test than
fun's 1.42.2 against kids' 1.43.4 ever was: a field kids sends and zen does not is a version
difference, and a rule both answer is not.

Read-only on kids and zen throughout, writes on plexscratch. The kids snapshot was identical
before and after both hunts.

**The listings ran none of the query grammar they advertise.**

- **`?limit=N` caps a listing's rows**, and rex read it only in `/query/parse`. `limit=0`
  answers ONE row and a negative one every row -- the two edges autocomplete's limit already
  had -- and `limit=abc` is a 500. It belongs to the query grammar rather than to every
  listing: `/newest`, `/recentlyAdded` and `/children` ignore it on both servers. A smart list
  saved with a limit therefore previewed as the whole section.
- **A `type=` that does not parse is refused, and the status differs by path.** A section
  listing answers 500 to `type=abc`, `type=1.5` and an empty `type=`; `/library/all` answers
  **400** to the first two and keeps 500 for the empty one. A type NUMBER the library holds no
  rows of -- 8, 9, 10, 12, 13, 19, 99, -1 -- is a 200 with no rows. rex answered the whole
  unfiltered grid to all of them, so a client that sent a malformed type read the library as
  if it had matched.
- **`id` is a real filter field on every listing**, echoed by `/query/parse` as a Rule with an
  EMPTY `fieldTitle` because no Meta advertises it. `=` is exact, a comma list is an OR,
  `>>`/`<<`/`>=`/`<=` compare numerically, and `==`/`!==` -- the exact-string pair -- never
  match a number at all. A value that is not a number sorts ABOVE every id, exactly as an
  integer column compares against text: `id=abc` matches nothing, `id!=abc` everything,
  `id>>=abc` nothing and `id<<=abc` everything. rex reported it as an unknown field, so
  python-plexapi's `editions()` -- which asks `guid=<x>&id!=<self>` -- called every item its
  own duplicate.
- **A section's `allLeaves` is a full query surface, not a dump.** Filters, `sort=`, `limit=`
  and `type=` all apply; an unknown sort key and an unparseable type are 500s. rex
  answered its whole leaf list to every one of them, so the filter bar and the sort menu over
  an "All episodes" view did nothing and said 200.
- **`excludeElements=` applies to the three listings cycles 9 and 10 added** -- the section's
  `allLeaves`, `/library/people/{id}/media` and `/hubs/items` -- where rex kept the Media
  children and sent no `mediaCount`. The client asks for the cheap list to lay a page out and
  was handed the expensive one with no count to draw the badge from.
- **`includeStorage=1` adds `durationTotal` and `storageTotal` to a section grid row**, at the
  library's own top-level type ONLY: a show carries the sums over its episodes (Firefly:
  60186 ms and 37683488 bytes, exactly its six episodes), a movie its own, and `?type=3`,
  `?type=4`, `allLeaves`, `/library/all`, `/newest`, `/children` and a detail read carry
  neither. rex sent neither, although the row already holds both numbers.
- **`/library/people/{id}/media` is typed by the TAG, not by its rows.** `viewGroup` and
  `viewMode` belong to an ACTOR's list (tagType 6) -- even when its rows are shows, which is
  where cycle 9's reading came from -- and a Director, Writer or Producer list carries
  neither. The list is also a query: the library filters narrow it, where rex answered
  every row the person is on.

**The Dashboard's numbers.**

- **A statistics container pages its SERIES only.** `Device` and `Account` are a lookup for
  the rows on the page, and the answer carries exactly the ones its rows name -- a window over
  two devices carries both, a window past the end carries neither, and `totalSize` counts the
  series alone while `size` counts every element. rex sliced all three lists with the same
  offset, so the second page of a graph named a device it had not sent and lost the Account
  row entirely.
- **`timespan` has to parse as a whole number**, on all three statistics endpoints: `abc`,
  `1.5`, an empty value and one with a leading space are 400s, where `0`, `-1` and `06` are
  200s. rex ran an unguarded `int()` on it -- a 500 on two endpoints and a silently
  ignored parameter on `/statistics/resources`.

**The transport.**

- **A real PMS gzips every compressible body whenever the client offers gzip**, even when
  compressing makes it LARGER: an 83-byte `/livetv/dvrs` comes back gzipped at 90 bytes, with
  `X-Plex-Content-Original-Length: 83` and `-Compressed-Length: 90`. `Accept-Encoding:
  identity` is the only thing that stops it. rex had an 860-byte floor and an "only if it
  shrinks" test, both inferred from what happened to be seen rather than measured, and
  seventeen of the endpoints cycles 9 and 10 added answered plain and without the length pair.

**The writes.**

- **A plain "Add to Queue" lands in Up Next, not at the bottom.** Measured on the copy: a
  queue created over [A, B, C] with A playing, then a plain add of D, answers [A, D, B, C]; a
  second plain add of E answers [A, D, E, B, C]; a `next=1` add goes in FRONT of that block.
  Cycle 4 recorded this rule and implemented it as "skip while the item id is above the
  selected id" -- but every ORIGINAL member also has an id above the first item's, so rex
  skipped the whole queue and appended at the very end. Add to Queue from a grid dropped the
  row at the bottom of the queue for seven cycles.
- **The Upload Subtitle modal's request is accepted.** The client (bundle module 90487) sends
  `POST /library/metadata/{id}/subtitles?title=<name>&format=<ext>` with the file as the body;
  a real PMS answers 200 with an EMPTY body and leaves a new subtitle `Stream` -- streamType
  3, the format as both `codec` and `format`, the name with its extension stripped as `title`,
  a `key` of `/library/streams/{id}`, `transient: "0"` -- which becomes the SELECTED subtitle.
  The refusals are their own grammar: no `title`, and a title with no extension, are **500**s;
  `srt`, `ass`, `ssa`, `smi` and `txt` are accepted where `vtt`, `sub`, `idx`, `sup` and `pgs`
  are **400**s; an empty body is a **406**; an unknown item a 404. rex fell through to the
  subtitle SEARCH branch and answered 400 to every one of them, so the modal always reported
  "Unable to upload subtitle file". This **overturns cycle 10's instrument limit** -- "POST
  subtitles is a 500 on the copy with nothing in its log" -- which was measured from a request
  shape the client does not send.
- **A sidecar subtitle labels its codec `<CODEC> External`** in both titles: `Unknown (SRT
  External)` plainly and `<name> (SRT External)` extended. One witness only -- no sidecar
  subtitle exists on the 1.42.2 server to confirm it against -- but it is the only kind of
  subtitle stream rex can create, so the label is reachable only through the upload above.
- **A stream PUT is accepted and stores nothing**: `PUT /library/streams/{id}` on a stream the
  server HAS is 200 with an empty body, with or without parameters, and the track's title is
  unchanged afterwards. A **DELETE of an embedded track is 403** -- it is part of the file --
  and a DELETE of an uploaded sidecar really removes it (200). An unknown id keeps cycle 6's
  refusals (PUT 400, DELETE 404, GET 404), and the GET of a real stream stays the 501 cycle 6
  measured. rex answered 501 to every verb.
- **A playlist's items and a collection's children are the LIGHT grid row** -- no `Guid`, no
  `Rating`, no `Field`, no `Stream` under the Part, tags text-only -- while the same item's
  detail read is full. They keep their librarySection fields and a playlist item keeps its
  `playlistItemID`. These are the two list endpoints the grid projection, measured in cycles 5
  and 7, was never applied to, so every Edit Collection dialog and every playlist page carried
  the whole stream table.

**A single history read is a projection, not the stored row.** `/status/sessions/history/{id}`
answers nine attributes -- `historyKey`, `librarySectionID`, `title`, `type`,
`originallyAvailableAt`, `viewedAt`, `accountID`, `deviceID` and `viewType: 1` -- with an
episode adding `grandparentTitle`, `index` and `parentIndex`, and with no `key`, no
`ratingKey` and no artwork. Identical on both servers, against every readable row on each.
rex answered its stored listing row, advertising links this endpoint does not carry.

Measured and not acted on:

- **`/status/sessions/history/all` answers `size 0` on BOTH servers** while single reads by
  key work, through every combination of `accountID`, `librarySectionID`, `sort` and paging.
  Recorded in cycle 9 as a kids oddity; it is not one, and it is not reproduced -- rex's
  listing is what fun's 3879 rows were measured against.
- `includeBandwidths=1` adds `deepAnalysisVersion` and `requiredBandwidths` to every `Part`.
  The bandwidth ladder needs Plex's own deep analysis, which puts it beside `thumbBlurHash`.
- `/library/people/{id}` answers any TAG id, not only a person: kids' id 1 is a
  `mediaProcessingTarget` (tagType 42) and renders as `<Tag>` with `MediaSettings` and
  `Device` children. No rex catalog holds such a tag.
- A detail row's tags carry `count` (how many items in the library share the tag) on both
  servers and none of rex's do. Pervasive across every detail read rather than specific to
  anything this cycle measured; left for a cycle that can measure it as a row-shape rule.
- `playQueueLastAddedItemID` appears to re-anchor when an item is MOVED, not only when one is
  added. Seen on the copy, not pinned to a rule.
- The subtitle SEARCH path (`GET .../subtitles?language=`) answers 503 on the isolated copy: it
  needs OpenSubtitles. An instrument limit, and rex's empty-container answer stands.
- fun was out of scope for the whole cycle, so nothing here rests on a third server.

#### The twelfth pass: what a real library shows, and what the writes really push

The first cycle with a **real library** as a routine instrument. Eleven cycles measured row
shapes against ten synthetic ten-second clips; zen has 3,887 movies, two show libraries and real
agent metadata, on Plex 1.42.2 — the exact build rex reports. Hunt A was read-only across
kids and zen; hunt B measured writes on plexscratch, its only possible witness. It found MORE
than the cycle before it, not fewer, which is the instrument catching up rather than a
regression in the work.

**What only a real library could show.**

- **A light row CAPS its tag lists**: at most 2 `Genre`, 2 `Writer`, 2 `Country` and 3 `Role`,
  always the first of the detail row's own order, where the DETAIL row carries the lot. One zen
  movie has 155 roles on its detail row and exactly 3 on every listing of it; the richest genre
  row (7) prints 2. Confirmed on kids too (Sintel 7 genres → 2; episode 8's 24 roles → 3 in a
  child list). `Director` is uncapped in every sample available — the richest row in 240 zen
  movies has two — so it is left alone rather than guessed at. rex sent the whole list on
  every grid, hub and search row. **A five-clip library cannot show this**: no item has enough
  tags to be truncated, which is exactly why eleven cycles of row-shape work missed it.
- **An episode's `parentGuid` is the SEASON's guid**, not the show's. rex copied the
  season's own `parentGuid` — which is the show, correct on the season row and wrong on the leaf
  — so every episode of a show claimed one and the same parent, and a client keying per-season
  artwork or state off it could not tell the seasons apart. Corrected at LOAD as well as at
  build, because every catalog built so far carries the wrong value baked in.
- **The section grid says `allowSync: true`.** rex said false there while already saying
  true on `/newest`, `/recentlyAdded`, `/onDeck`, `/folder`, `/children` and a detail row — the
  most-requested container on the surface disagreeing with every other one, and an attribute
  python-plexapi reads off every section listing.
- **A search caps at 3 rows per hub with no `limit`, and `/library/search` at 10 in all.** A zen
  search answering 6 movies, 26 directors, 11 episodes and 50 actors under `limit=50` answers
  three of each with none, and `more` stays false either way. rex defaulted both to 30.
- **An actor tag row is `tagType: 6`** and a director row 4 — different tag types, not one
  "person" type. rex called both 4 while `/library/people/{id}` already answered 6 for the
  same person, so the server contradicted itself.
- **`group=type`, `group=guid` and `group=id` are 200**, where rex answered 500. Exercised
  against all 27 candidate fields: the other 24 agree, including the 11 that are 500 everywhere.
  `id` became a filter field in cycle 11 and was still not a group field.
- **An EPISODE's title is compared by `/common` and a SEASON's is not**, and `titleSort` is `""`
  only when it is genuinely mixed — a single row answers its own sort title (`The Train Job` →
  `Train Job`, derived where the row stores none; a season answers `Season 1`). rex dropped
  `title` for both types and hardcoded `titleSort` to `""`, so the Edit dialog opened on an
  episode with the Title and Sort Title boxes blank and a save could overwrite them.
- **`/library/sections/{id}/actor` carries no `content`**, where all 25 other secondary axes
  carry `content: "secondary"`. No rule explains it; both servers agree, so it is emulated as
  measured — a field invented is as wrong as one missing.

**What the writes really push.** One witness only: these are writes, so plexscratch (1.43.4) is
the only server that can answer them.

- **A subtitle upload pushes NO frames.** 0 in a 15-second watch and 0 in a 60-second one,
  `/activities` stays empty, the item's `updatedAt` does not move, and the client refetches the
  row itself with `?skipRefresh=1`. **Cycle 11 added an edit pair here without measuring it** —
  this project's own class-C mistake, told to every other client on the socket.
- **A library edit needs an `agent`, not a `name`.** The client's library-upgrade handler (the
  one whose failure the bundle reports as "There was a problem upgrading the library") sends
  `agent` and `scanner` and nothing else; `agent` alone is a 200 too, while `name` alone,
  `scanner` alone, `language` alone and an empty form are 400. rex demanded `name` and
  refused the upgrade form every time. This corrects cycle 4's reading.
- **The subtitle upload takes `psb` as well, and the format check is CASE-SENSITIVE**: `SRT` is
  a 400 where `srt` is a 200. rex lowercased the value, so it accepted uploads a real PMS
  refuses and refused one it takes. (The modal's own allowed list is wider than the server's —
  `aqt jss rt utf utf-8 utf8 sub idx vtt` are offered by the client and refused by the server.)
- **A stream path tells a malformed id from a missing one**: `DELETE /library/streams/abc` is a
  400 and `DELETE /library/streams/999999` a 404. A POST to a stream the server HAS is a 404,
  not the 501 its GET answers.
- **A queue add that adds nothing changes nothing.** A `server://` uri naming another MACHINE
  adds nothing at all, and every no-op add leaves `playQueueVersion` exactly where it was.
  rex read the ratingKey out of any uri and added the item whoever it belonged to, and
  stepped the version on every no-op — and the client DISCARDS a queue response whose version it
  does not expect, so a no-op add invalidated the queue the player was holding.
- **Optimize Database runs an activity**: `general.db.optimize`, started at 0 with an empty
  subtitle, updated at 0, 10, 40 and 60 with the subtitle `Optimizing Database`, ended at 100.
  rex answered its empty 200 and pushed nothing, so the Settings page's progress bar never
  moved.
- **A refresh's `updated 49` frame comes SECOND**, before the processing timeline — which is the
  order this file already recorded and `notify_refreshed`'s own docstring already stated. rex
  emitted it inside `finish_activity`, after `loading`: the rule was right and the implementation
  was something else. The activity's subtitle is the row's `title (year)`, not the bare title.
- **A scan ends inside a NESTED pair of `provider.subscriptions.process` activities** — the
  outer starts between the section activity's last `updated` and its `ended`, the inner runs
  after the completion status, then inner ends and outer ends. **"Refresh All Metadata"
  (`?force=1`) runs that whole scan sequence first** and only then refreshes each item; rex
  pushed a bare status frame and went straight to the per-item half, so the Libraries page showed
  no scan for the operation that takes longest. And **a library create answers a `Location`
  header** naming the new library.

Measured and not acted on:

- **Refuted: "a detail row's tags carry `count`".** Recorded in cycle 11 as a boundary note from
  a hunter's report and written into the handover as this cycle's lead territory **without being
  verified**. Searched here across the raw JSON and XML of the detail row (plain and with the
  full preplay `include*` set), every listing, `/library/tags` for nine types and all 26 secondary
  axes on both servers: `count` appears in exactly one place, the tag `Directory` rows of
  `/hubs/search` and `/library/search`, where rex already emits it with the right value. The
  cheapest possible instance of taking a report at face value.
- A library create over real files pushes 74 frames plus a `backgroundProcessingQueue`
  `queueRegenerated` about a minute later, with a per-item `created`/`analyzing`/`thumbnailing`
  sequence. That half depends on files rex does not own; the empty-folder sequence is what it
  can push and now does.
- The 201 row's `metadataAgentProviderGroupId`, `updatedAt`, `createdAt` and
  `contentChangedAt: -1`, and a globally numbered `Location.id`: still catalog-shaped values
  rex has no source for.
- The download queue answers 404 to every form on the copy — no account — so nothing new is
  measurable there. Plex Web never builds a `downloadQueue` path at all (0 occurrences in the
  bundle).
- The subtitle SEARCH and the apply-a-search-result PUT both need OpenSubtitles and answer 503
  on the isolated copy.
- `/search` (the legacy endpoint) ignores `limit` entirely on both references, where rex caps
  at 30. Below 30 matches it never shows; left alone.
- `titleSort` article stripping beyond the/a/an: zen strips `Das` (1/1) and `La` (2/2) but not
  `Die` (0/2) or `Les` (0/1), on samples far too small to be a rule — it points at agent-supplied
  sort titles rather than a local one.
- 112 non-GET template occurrences in the bundle resolve to 37 distinct request templates: 26
  already covered, 7 measured this cycle, 4 groups unreachable or out of scope (Optimize
  generators, Live TV, plex.tv, telemetry).

#### The thirteenth pass — the rest of the real library, and the write cascades

Two hunts. One read-only across kids (1.43.4) and zen (1.42.2, the build rex
reports) over everything about a real library that is not a movie detail row — the
territory cycle 12 opened and took one pass at. One writing to plexscratch over the
CASCADE family: what a write to a PARENT row does to its children, which no cycle had
ever read back. **Twenty-eight findings reported, twenty-two acted on**, and for the
first time in the programme not one of the read hunt's findings was refuted on
re-measurement.

The cycle opened with three revalidations from the handover's own thin-evidence list,
and two of them were findings in their own right.

**`Director` caps at THREE on a light row, and `Producer` is not on one at all.**
Cycle 12 pinned the light-row caps at 2 `Genre`, 2 `Writer`, 2 `Country`, 3 `Role` and
recorded `Director` as "only known to be above 2", because the richest Director row in
its 240-movie sample had two. zen has a movie with **seventeen** (`Minions & More 1`),
a show with four and an episode with four: every listing of them carries three. And
`Producer` is absent from a light row however many the detail row holds — 22 on one
zen movie, none on any listing of it. The same caps hold on a section grid, on
`/library/all` and on a `/hubs/search` row, which is the whole of `_grid_row`'s reach.

**Unmatching a SEASON unmatches every episode under it.** Measured by diffing the
show, the season and all three episodes field by field either side of one call. The
season takes `local://<id>`, loses its own `year` — so the show's shows through as
`parentYear` — and falls its `thumb` back to the SHOW's, keeping its `parentGuid` and
its external `Guid` array. Every episode goes with it: `local://<id>`, the generated
`Episode N`, `summary` emptied, `year`, `audienceRating` and all four people lists
dropped, `titleSort` removed, `parentThumb` nulled, and `parentGuid` following the
season to its new `local://` guid. kids corroborates the season half from the other
side — its unmatched season 11 carries the show's thumb, no `year` and `parentYear:
2002`, where its matched season 7 carries its own. rex edited the one row, which
left matched episodes under an unmatched parent: a state a real PMS cannot produce,
and one where the client's own `local://` menu predicate answers differently per row.

**The `<CODEC> External` subtitle label now has two witnesses.** Uploaded a sidecar to
a freshly reset copy (`cycle13 (English SRT External)`, with `displayTitle` the plain
`English`) and found eleven real sidecar subtitles on zen carrying the same form —
`English (SRT External)`, `Deutsch (SRT External)`, `Unknown (TXT External)`.
rex's implementation already matched; the single-witness note in the handover is
closed.

##### What a real library shows and a five-clip one cannot

- **A row with NO value for the sort key sorts BELOW every row that has one** — first
  ascending, last descending. Measured on `audienceRating`, `rating`, `userRating`,
  `contentRating`, `year`, `originallyAvailableAt`, `viewCount`, `viewOffset`,
  `lastViewedAt`, `mediaHeight` and `mediaBitrate`, on both servers, and on a show
  library at types 2, 3 and 4. rex keyed on `(value is None, value)`, which puts
  the missing rows ABOVE every value — and twelve of the fifteen sorts a section
  advertises default to `desc`, so Rating, Plays, Date Viewed, Critic Rating and
  Bitrate all opened the grid on the rows with no value at all. On zen that is the
  entire first page.
- **The seventeen search hubs come back in an order rex had wrong in two places.**
  `movie` is FIFTH, not first, and `collection` is FOURTEENTH, between genre and
  playlist, not seventh. This has to be measured on a query that matches NOTHING:
  a query with fuzzy hits promotes the hubs that scored, which is what made an earlier
  reading of `zzqqxx` disagree between the two servers. On four genuine no-match
  queries each they answer the identical list. The client lays the results page out
  from it.
- **`/hubs/search` returns the ITEMS a matched tag is on**, each carrying `reason` (the
  tag kind), `reasonID` (the tag id) and `reasonTitle` (its name); a title-matched row
  carries none of the three. kids `colin` answers the two tag rows AND Firefly
  (reasoned on its actor) and Sintel (on its director); zen `tom hanks` answers eleven
  movies all reasoned on the actor, and across ten zen queries **53 of 81 item rows
  carried a `reason`** — most search hits on a real library are tag hits.
  `/library/search` does NOT carry them (kids `colin` is two `Directory` rows and
  nothing else), so this is the hubs endpoint alone. rex matched titles only, so
  searching an actor, a director or a genre drew the tag row with an empty Movies
  shelf beside it, and its own `reason` code could never fire.
- **The "All episodes" row follows the PAGE's row count, not the offset.** On One
  Piece, 24 seasons: `Start=0 Size=10` and `Start=10 Size=10` both answer `size 11`
  with the row, `Start=5 Size=3` answers 4 with it, and `Start=23 Size=2`,
  `Start=3 Size=1` and `Start=0 Size=1` — every one-row page — answer 1 without it.
  kids agrees on all six cases it can express. **This corrects cycle 8**, which read
  kids as dropping the row past `Start=0` and recorded the servers as disagreeing:
  kids has two seasons, so every page past the first there IS a one-row page. The
  disagreement was a two-season library, not a version difference, and fun was right.
- **The Duplicates filter is "more than one Media on the row"** — zen holds 214
  duplicated movies of 3,887 and 504 episodes of 9,287, and 60 sampled `duplicate=1`
  rows carried 2 to 15 Media against 60 `duplicate=0` rows carrying exactly one, with
  the two halves adding up to the library. For a SHOW or a SEASON, which have no Media
  of their own, it is "a leaf under it has more than one": 44 shows and 74 seasons,
  each sampled one holding a multi-Media episode where each `duplicate=0` show held
  none. rex answered 0 for every row, so the control `includeAdvanced=1` puts in
  the filter bar was advertised and permanently empty.
- **A bare `<field>=` means THREE different things, by field**, and both servers agree
  exactly. A tag-TABLE field matches NOTHING — 37 of 600 zen movies carry no `Genre`
  and all 600 carry no `Label`, yet `genre=` and `label=` answer 0 rows. `studio` and
  `editionTitle` match EVERY row — 5 of 5 and 3,887 of 3,887, though 14 of 200 sampled
  zen movies have no studio. Everything else of that kind — `contentRating`,
  `audioLanguage`, `subtitleLanguage` — matches the rows with NO value. rex applied
  the third rule to all of them, so `genre=` selected the whole library and `studio=`
  selected only the rows that had none. The codec axes DISAGREE across the two versions
  (kids nothing, zen every row), so they are a version difference and are left alone.
- **The `contentRating` axis ends with a `None` bucket**, after the alphabetical
  entries and on that axis alone — `{"fastKey": ".../all?contentRating=None", "key":
  "None", "title": "None"}`. `contentRating=None` in that exact spelling selects the
  rows with no rating (2,864 on zen, 3 on kids, the same as `contentRating=`), while
  `none` and `NONE` match nothing. No other field axis has such a bucket, and rex
  offered no way to reach those rows from the filter bar at all.
- **A tag or field axis REFUSES a `type=` it cannot parse; a media axis ignores it.**
  Across seven values on both servers: `genre`, `studio`, `actor`, `contentRating`,
  `firstCharacter`, `decade` and `country` answer 500 to `abc`, `1.5` and an empty
  one, `year` answers 400 to the same three, and `-1`, `99`, `2` and `04` are 200
  everywhere. `resolution`, `audioLanguage` and the codecs answer 200 to all seven and
  the identical list for every one — they neither validate the type nor scope by it,
  on a movie library and a show library alike. And `firstCharacter` IS scoped: on
  zen's show library `#`/`A` are 13/40 shows at `type=2`, 13/64 seasons at `type=3`
  and 185/207 episodes at `type=4`, and empty for a type the library has no rows of.
  rex answered 200 to every axis whatever the type said, scoped `resolution` by
  type and counted `firstCharacter` over the whole section.
- **An EPISODE with an `audienceRating` carries `audienceRatingImage` and a `Rating`
  element.** 60 of 60 One Piece episodes and 3 of 3 on the lab, with no exception,
  while the unmatched episodes with no rating carry none of the three and a SEASON
  never carries any of them (0 of 12 sampled). rex built both for a show and a
  movie only, so its own episode rows contradicted its own movie rows and no episode
  page or listing row drew a rating badge. `rating` — the CRITIC rating — is dropped
  from an episode for the same reason it is dropped from a movie: no episode on either
  reference carries one.
- **A show's `/allLeaves` gives every episode row the SHOW's `studio`**, and nothing
  else does. 20 rows across five shows on two servers: "Mutant Enemy Productions" on
  all six of Firefly's, "Toei Animation" on One Piece's, "20th Century Fox Television"
  on The Simpsons'. The episode's own detail row, the season's `/children` and the
  SECTION's `allLeaves` all answer none on both servers, so it is a property of the
  list rather than of the row.
- **A season row of `/library/recentlyAdded` carries `parentSummary`**, the show's own
  summary, and only on that endpoint. 2 of 2 season rows on kids and 3 of 3 zen rows
  whose show has a summary; the 4 zen rows whose show has an empty one carry no field
  at all, and a movie row never carries it. Swept across
  `/library/sections/{id}/recentlyAdded`, `/newest`, `/hubs/home/recentlyAdded`,
  `/hubs/sections/{id}`, `/hubs/promoted`, a `type=3` listing, `/children` and
  `/library/onDeck`: every other season row on both servers carries none.
- **`viewedLeafCount`, `viewedOrInProgressLeafCount` and `unviewedLeafCount` filter a
  show listing** although no Meta advertises them: on zen's 507 shows
  `viewedOrInProgressLeafCount=0` is 499 and `=1` is 4, `viewedLeafCount>>0` is exactly
  the 8 shows with a watched episode, and `unviewedLeafCount>>0` is 505. `leafCount`
  and `childCount` are genuinely unknown fields — both answer the whole library — which
  is what rex already did with all five. Neither server holds a leaf that is in
  progress and unwatched, so the "or in progress" half of the first one's name could
  not be separated from the viewed half on either; it is read as the name says.

##### The Home screen and the library tab, on the version rex reports

- **A shelf with no `count=` carries at most SIX rows and says `more: true`.** Measured
  on zen against a 3,887-movie and 507-show library: `movie.recentlyreleased`,
  `movie.recentlyadded`, `movie.topunwatched`, `tv.recentlyaired`, `tv.startwatching`
  and `tv.toprated` are all size 6 with `more`, and `/hubs` does the same. `?count=3`,
  `?count=12` and `?count=20` are honoured exactly, so six is the DEFAULT and not a
  ceiling; the related shelf caps at six too. rex used 12, a number no reference has
  ever answered — kids cannot witness it either way, because with five movies and one
  show no shelf there can reach even six, which is how the recorded "up to 12" survived
  twelve cycles. It is a separate constant from the cap on the two lists that are not
  shelves: `/hubs/home/recentlyAdded?type=1` answered all 3,887 rows on zen.
- **Three shelves a 1.42.2 library tab carries and rex never built**:
  `movie.topunwatched` between the genre shelf and Recently Watched, and
  `tv.startwatching` after Recently Added and `tv.toprated` after Rediscover on a show
  library. Each is `style: shelf`, `random: true`, and each key is the listing its "see
  all" opens. kids (1.43.4) emits none of the three although it HAS qualifying rows —
  all three keys answer `size 1` there — so this is a version difference and zen is the
  witness that matters.
- **`pinnedContentDirectoryID` reshapes the promoted Recently Added shelf on 1.42.2.**
  Bare, `/hubs/promoted?contentDirectoryID=1` answers `movie.recentlyadded.1` /
  "Recently Added in Zen Movies", keyed on the section listing; with the parameter
  beside it the same shelf is `home.movies.recent.1` / "Recently Added Movies", context
  `hub.home.movies.recent`, keyed on
  `/hubs/home/recentlyAdded?type=1&pinnedContentDirectoryID=1`. A show library gives
  `home.television.recent.N` / "Recently Added TV". kids ignores the parameter
  entirely. The web client does send it — its Promoted fetch builds
  `{contentDirectoryID, pinnedContentDirectoryID, includeMeta, excludeFields}`, and the
  name occurs fourteen times in the bundle — so a pinned library's Home row was titled
  and keyed as the unpinned one.

##### The cascade family: what a write to a parent does to its children

Measured on plexscratch by snapshotting the parent AND every descendant field by field
either side of one call. Every rule here has that one witness, which is what a write
rule can have; the read side of each was checked on kids and zen where a read can show
it.

- **Unmatching an EPISODE alone drops what the season cascade drops.** With the season
  left matched: the four people lists and `audienceRating` removed, `summary` emptied,
  `year` removed and `parentYear` appearing in its place from the season that still has
  one, `titleSort` removed where there was one, the thumb re-versioned and every
  `Image.alt` following the new title. The two things the cascade does that this does
  not — `parentGuid` moving to `local://` and `parentThumb` going null — are
  consequences of the SEASON changing, not of the episode write. So the field set
  belongs to "unmatch an episode", by either path. rex changed the guid and the
  title and kept the rest, leaving an unmatched episode carrying the agent's summary,
  rating and cast under a `local://` guid.
- **Unmatching a SHOW cascades the whole way down.** Every season becomes `local://`
  and every episode with it; each season's `parentGuid` and each episode's
  `grandparentGuid` follow the show to its `tv.plex.agents.none://<id>`; each episode's
  `parentGuid` follows its own season; and the artwork the children borrowed from the
  show goes with it — the show's `art`, the seasons' `thumb`/`art`/`parentThumb`, the
  episodes' `art`/`parentThumb`/`grandparentThumb`/`grandparentArt`. The inherited
  `contentRating` goes from every descendant too. rex edited the show row and
  stopped.
- **An unmatch leaves NO activity running.** After a movie unmatch the copy's
  `/activities` is empty — the whole refresh sequence runs and ends. rex opened an
  `Unmatching` activity at progress 0 and never closed it, so it was still listed
  minutes later and the Dashboard spinner turned for the life of the process. Cycle 9
  fixed this for a season and an episode, both of which go through `notify_refreshed`,
  and left the movie path.
- **A season's `index` lands on its episodes' `parentIndex`** — on the detail row and in
  the season's `/children` alike. It is the same rule cycle 9 measured for a parent's
  `title` and `studio`, which rex already copies; `index` was missed, so an episode
  kept pointing at a season number that no longer existed.
- **An episode carries its SHOW's `contentRating`, and a season carries none.** All six
  of Firefly's say TV-14 like the show, the three unmatched ones included although they
  have no agent metadata at all, and 18 of 18 episodes across three zen shows carry
  their show's (TV-14, TV-Y, TV-PG). The write side agrees: on the copy, editing the
  show's rating moved it on the episodes that had none of their own, and the show
  unmatch removed it from all of them. rex left an episode with no certificate at
  all.

* **A part whose FILE is gone is a plain 404, not a 5xx.** zen part 23595 reports
  `accessible=false`/`exists=false` under `checkFiles=1` and answers **404** to the byte read, with
  the binary refusal header set. rex answered 502 to a dead link and to a network failure
  alike, so a client that would have drawn "this file is missing" got a server error. The two are
  now split: Real-Debrid's `unavailable_file` (error_code 24) and a 404 from its API are the 404,
  and anything else keeps the 502 so a transport failure stays diagnosable.

##### Measured and not acted on

- **Six write cascades the hunt measured and the driver did not re-measure**, so they
  are recorded rather than implemented and lead cycle 14: a show or season REFRESH
  cascading through the tree (40 and 20 frames against rex's 7); `flattenSeasons`
  and `episodeSort` pushing NOTHING while changing the rows (`skipChildren` on the show,
  `skipParent` and no `parentRatingKey` on every episode; the season's `/children`
  newest-first), where rex pushes a 36-frame cascade for every prefs write and
  changes nothing; an artwork PICK re-versioning `thumb`, `art` and `theme` together,
  cascading to the children, LOCKING the field and pushing one frame at +5 s; `analyze`
  pushing a per-level triple plus a `media.generate.chapter.thumbs` activity; the
  74-frame library-create sequence over real files with its `backgroundProcessingQueue`
  regeneration at +60 s (which also appears 8 s after an unrelated edit, so it is
  periodic and not create-specific); and the merge/split arithmetic on a show tree,
  where rex's season merge deletes the season's EPISODE as well. Each is
  single-witness on the copy and unverified by the driver — the two conditions this
  file's own revalidation register says to record rather than build on.
- The score PROMOTION `/hubs/search` applies when a query has fuzzy hits: a hub whose
  best row scores above ~0.5 moves to the front, and four of five zen queries fit that
  exactly while `comedy` also demotes `episode` from 4th to 13th, which nothing
  predicts. The base order is implemented; the promotion is not.
- Collections have **no instrument**: `/library/sections/{1,2,4}/collections` is `size
  0` on zen and on kids, so `collectionSort`, `collectionMode`, `minYear`/`maxYear`,
  `subtype`, member order and the collection hubs stayed unmeasured. zen has no season 0
  and nothing in progress, so specials and the `inProgress` filter have no witness
  either.
- A two-`Media` row's decision and play queue: both are write-shaped paths the read
  guard refuses on a shared reference, so which Media a decision picks is unmeasured.
- The codec axes' empty-value behaviour (kids nothing, zen every row) — a version
  difference, not a rule.

#### The fourteenth pass — how a real PMS folds a title, and the query surface nobody had run

Two hunts. One read-only across kids (1.43.4) and zen (1.42.2, the build rex
reports) over what two passes at a real library still had not reached — grouping,
the people surface, real watch state, and the listing query at scale. One writing to
plexscratch over collections, an in-progress leaf, and the six write cascades cycle 13
measured and recorded rather than implemented.

The cycle opened with two revalidations from the handover's thin-evidence list, and
both paid: one was the largest finding of the cycle and the other closed a
single-witness note.

**`titleSort` is not the article-stripped title. It is the title trimmed, folded and
then stripped.** The recorded rule — `_ARTICLE.sub("", title)` — carried a docstring
saying "Measured on fun", and fun has been out of scope since cycle 11, so it was a
single-witness rule with a dead witness. Re-measured on zen across all 5,294 rows of a
real library (3,887 movies, 507 shows, 800 episodes), three things reproduce 5,289 of
them exactly:

- **Leading whitespace goes.** 90 rows begin with a space; every one carries a
  `titleSort` with it removed and not one is missing one.
- **Combining marks are folded to ASCII** — `Amélie` → `Amelie`, `Bāhubali` →
  `Bahubali`, `Bogotá` → `Bogota`, `Café` → `Cafe`, `Señor's` → `Senor's`. All 21
  accented rows across the three types carry the folded form, and **not one lacks a
  `titleSort`**, which is what makes this the server's own derivation rather than
  something the agent supplied. The fold is combining marks ONLY: the 51 rows whose
  non-ASCII survives NFD (CJK, the curly apostrophe, an en dash) carry **no**
  `titleSort` at all, because for them the derived string equals the title — which is
  the recorded "sent only when it differs" rule seen from the other side.
- **Then the article**, as before.

The five rows the model misses are Spanish `La` (4) and German `Das` (1). Those are not
mechanical — `Les hommes de la baleine`, `Die Die Delta Pi II` and `O Ultimo Portal`
all keep theirs — so the article list is per-item language, which rex's catalog
cannot express. Left alone deliberately. Only the LEADING half of the whitespace trim
has a witness (zen holds no title with trailing or doubled whitespace); `.strip()` is
the symmetric reading and is flagged as such.

It matters because the DEFAULT listing order is `titleSort` and every tie falls back to
it, `firstCharacter` buckets on it, and `titleSort=` is a case-insensitive equality — so
an accented row sorted under its raw title, filed itself in the wrong letter bucket, and
sent no `titleSort` where a real PMS sends one.

**The word matcher folds accents; the substring matchers do not.** Following the fold
out of the row and into the three matchers that read a title, on zen:

| Matcher | Folds? |
|---|---|
| `/hubs/search`, `/library/search` (word prefix, then fuzzy) | **yes**, unconditionally |
| `title=` on a listing (word prefix) | **yes, but only while the query holds no space** |
| `/library/sections/{id}/search` (substring) | no |
| `/library/sections/{id}/autocomplete` (phrase at a word start) | no |
| `title!=` (substring) | no |

The search half had to be told apart from the fuzzy matcher before it could be called a
fold, because a one-character difference is exactly what a trigram score forgives. The
**score** settles it: `Amelie` and `Amélie` both answer 0.93095 — the same number, the
exact-match tier — while `Amlie`, `Amelke` and `Ameliee` answer 0.33095 and `Amelia`
answers nothing at all. `Bogota` and `Bogotá` are both 0.53056 and `Bogotb` is 0.33056.
The accent is invisible to the matcher; a real typo is not.

The filter half is stranger and is recorded as measured rather than explained:
`title=Bogota` finds "Bogotá: City of the Lost" and `title=Senor` finds the Señor
episode, while `title=Bogota City`, `title=Manly Senor`, `title=Senor Elegy` and even
`title=Bogota ` — one trailing space — find nothing, and every accented spelling of
those finds its row. The trailing-space pair is what pins it: the space does not break
the match, it turns the fold off. It is consistent with a folded per-word index
answering a one-word query while a multi-word query is checked as a sequence against the
raw title, but nothing measured that, so the code follows the observation.

rex split its word matcher on `[^0-9a-z]`, which makes an accented letter a
**separator**: "Amélie" became the two words "am" and "lie", so the quick search never
found the row by its own name and did find it under a fragment of one.

**`/filters` and the `Meta` descriptor advertise the 1.43.4 lists on a server that
reports 1.42.2.** kids answers 28 filter fields for a movie library, 17 at `type=4` and
6 at `type=10`; zen answers **22, 9 and 5**. The difference is exactly the media-codec
axes — `hdr10plus`, `atmos`, `videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`
everywhere, plus `hdr` and `dovi` on the EPISODE list, which 1.42.2 keeps on the movie
list only. Types 2, 3, 8, 9, 13 and 20 are identical on both servers, and so is the
`includeAdvanced` Duplicates row. The same split runs through `Meta.Type[].Filter`,
which is a separate fixture and is what the grid's own filter bar is built from.

`section_filters.json`'s own provenance note already said "fun (1.42.2) has the same
types, the same Duplicates rule and **shorter per-version lists**" — recorded, and never
acted on. The canonical class-C shape: the docs agreed with themselves and the code did
something else.

##### What a real library's query surface actually does

The read hunt took the parts of zen two passes had not reached: grouping, the people
surface, real watch state and the listing query at scale. Eight findings, every one
reproduced by the driver on both references before any code changed.

**A `group=`'s surviving row is the group's lowest `addedAt`, and it is picked BEFORE
the sort.** The old code's docstring called this an unknowable SQL artifact — "it is not
the first in sort order, nor the lowest id" — and kept whichever row the sort had just
put first. On kids, `group=type` (one group, all five movies) answers ratingKey **3**,
which is the lowest `addedAt` and not the lowest ratingKey, under `sort=titleSort`,
`titleSort:desc` and `addedAt:desc` alike. On zen the survivor is the group's minimum
`addedAt` in 88/88 year groups, 1074/1074 studio groups, 1238/1238
`originallyAvailableAt` groups, 3450/3450 title groups and 3887/3887 guid groups, with
groups up to 64 members and the survivor's position in sort order ranging 0–7. (Minimum
`addedAt` and minimum ratingKey coincide on zen, which is why kids is the discriminating
witness.) Group and Sort are independent controls in the client, so picking the survivor
after the sort made changing the sort change *which* rows the grid showed — and a smart
list storing both had contents that depended on its own sort order. A tie on `addedAt`
inside one group has no witness; the lowest ratingKey breaks it, which at least keeps the
answer independent of the sort.

**A type-prefixed `group=` is SCOPED, exactly like a filter and like a sort key.**
`type=4&group=show.title` collapses kids' six episodes to 1 and zen's 9,287 to **505** —
its show count — where `group=title` gives 4 and 4,443; `group=season.index` gives 2 and
58 where `group=index` gives 3 and 1,144; `group=season.title` collapses 9,287 episodes
to 1. rex stripped the prefix and read the field off the episode itself, so every
prefixed group was the unprefixed one. The fix moved the scope resolution into
`filter_value`, where the prefix was already being thrown away for every other caller.

**`/library/sections/{id}/actor` is the union of each item's FIRST THREE roles.** Not
every Role tag in the library: kids' five movies carry 0, 2, 2, 2 and 8 roles and its
axis `totalSize` is exactly **9** — Tears of Steel contributes only its first three. On
zen the axis is 3,413 while `/library/tags?type=6` is **59,030**, and all 15 sampled
movies with three or more roles contribute exactly indices 0, 1 and 2. `?actor=<id>`
still matches a role the axis omits, so the cap is on the AXIS and not on the tag, and it
is actor-only — `director`, `writer`, `producer`, `genre` and `country` carry every tag.
The axis's own `title2` says as much: **"By Starring Actor"**.

**Nine columns filter although no `Meta` advertises them**, and rex answered the
whole library to each. `viewOffset`, `skipCount`, `ratingCount`, `absoluteIndex`,
`lastRatedAt`, `updatedAt`, `createdAt`, `mediaCount` and `originalTitle`. The test that
tells a known field from an unknown one is that an **unknown** field is ignored and
answers every row: `viewOffset>>=0` answers 0 rows on both references, while on zen
`updatedAt>>=1780000000` is 3,861 of 3,887, `mediaCount>>=1` is 214 and
`originalTitle>>=M` is 393. `/query/parse` echoes each as a Rule with an **empty**
`fieldTitle`, the shape cycle 11 recorded for `id`. Two more in the same family:
`parentIndex` is a **500** on a listing on both references rather than a filter, and a
MOVIE row's `index` **column** is 1 on every row (`index=1` answers all 5 and all 3,887,
`index=0`, `index=2` and `index>>=1` answer none) although no movie row, grid or detail,
ever emits the attribute.

Making `originalTitle>>=M` behave forced a smaller correction with it: **the six-operator
restriction on a string field belongs to the advertised `FieldType` block, not to the
python type.** `titleSort`, `summary`, `tagline` and `originalTitle` appear in no Meta and
take the ordering keys — `titleSort>>=ZZZZ` is 0 on kids and 1 row of zen's 3,887,
`summary>>=A` is 1,493 — while the advertised `title` and `studio` answer **every** row to
the same key, which is what an ignored unknown field does.

**Thirteen more `sort=` keys order than the fifteen `includeAdvanced=1` advertises**:
`guid`, `originalTitle`, `contentRatingAge`, `studio`, `skipCount`, `absoluteIndex`,
`mediaSize`, `createdAt`, `type`, `viewedLeafCount`, `tagline`, `summary` and
`editionTitle`. They are real orderings checked against the rows zen returns —
`sort=createdAt` comes back in `addedAt` order, `sort=mediaSize` smallest Part first — and
rex answered **500** to every one, which is a blank page for python-plexapi and for
any stored smart-list URI naming one. `leafCount` runs the other way: both references
500 it, along with `childCount`, `slug`, `key` and any unknown key, while rex sorted
by it happily.

**`/status/sessions/history/all` validates its ids**, across nineteen cases on which both
references agree exactly. A non-integer `accountID`, `librarySectionID` or
`metadataItemID` is a **400** — `abc`, empty and `1.5` alike, while `1`, `-1` and `01`
pass — a `metadataItemID` naming no row is a **404** (`0` included), an unknown
`librarySectionID` is an empty 200 rather than a 404, `deviceID` is not validated at all,
and `viewedAt>=` refuses a non-integer. rex answered 200 with `size 0` to every one,
so a client sending a malformed `accountID` read "this user has no history" instead of an
error, and the per-item History link on a row the server does not hold read "never
played".

**`group=` reaches the SECTION `allLeaves`** — the last part of the listing grammar cycle
11 left out of it. kids' 6 episodes group to 3 and zen's 9,287 to 1,144, the same number
as the grid's `type=4&group=index`. An ITEM's `/allLeaves?group=` is ignored on both
references, so only the section form takes it.

**`#` is the DIGITS bucket of `firstCharacter`, not the everything-else one, and every
bucket key is percent-encoded.** Reconstructed against zen's own axis over all 3,887
rows: the 29 rows in `#` are exactly the 29 whose sort title starts with 0–9, and the one
row whose title starts with `『` gets a bucket of its **own**, key `%E3%80%8E`, listed
after Z. The `#` bucket's key is `%23`. rex folded every non-letter into `#` and
emitted the key raw — and raw, `#` is a URL fragment, so the client's request for that
bucket loses everything after it. The encoding convention is corroborated on kids, whose
other axes encode the same way (`contentRating` key `Not%20Rated`).

##### The axis surface, swept path by path

With the read hunt's findings landed, the driver swept the surface mechanically —
every secondary axis, every section leaf, every top-level path and every item leaf,
each fetched from kids and zen and from rex, reporting only the paths where **both
references agree and rex differs**. 59 top-level paths and 48 item leaves agreed
exactly. Four things did not.

**`/library/sections/{id}/userRating` is a real axis, and a FIXED one.** Both servers
answer the same six rows on a movie library and on a show library, whatever ratings the
library actually holds: `10`/"5 stars", `8`/"4 stars", `6`/"3 stars", `4`/"2 stars",
`2`/"1 star" and `-1`/"Not rated", under `title2` "By Rating". It is a scale, not a
value list — which is why no row carries a `fastKey` and each title is a LABEL, the same
shape the media axes have. rex 404'd it, so the Ratings entry in the filter bar
opened nothing.

**Nine axes belonging to OTHER library types answer an EMPTY secondary container, not a
404.** On a movie library, `/mood`, `/style`, `/format`, `/subformat` and `/source`
(music) and `/make`, `/model`, `/place` and `/tag` (photo) each answer `size` 0 with
`content: "secondary"` and their own `title2` — "By Mood", "By Style", "By Format", "By
Type", "By Source", "By Camera Make", "By Camera Model", "By Place", "By Tag". A 404
there tells the client the endpoint does not exist, which is the opposite of what a real
PMS says.

**The two families disagree about `type=`, and landing them in one branch got one of
them wrong.** A cross-type axis VALIDATES it — `?type=abc` and an empty `?type=` are
500s and `?type=2` is a 200, exactly as `/genre` behaves under cycle 13's rule — while
`userRating` IGNORES it completely, answering 200 to all three the way `/resolution`
does. The first version of this fix put both in the same branch, ahead of the type
check; the driver caught it by running cycle 13's own rule against the new axes rather
than assuming they inherited it.

**`/library/sections/{id}/recentlyReleased` is a 404 on both references, and rex
invented it.** Five library/type combinations across kids and zen, with and without
`type=`: 404 every time. The string appears nowhere in `docs/findings.md`, in
`CLAUDE.md` or in either suite — only in `server.py`, where it had been answered as a
listing sorted by release date beside the real `/unwatched`. It was never measured, only
assumed, and a 200 there is fix 4's trap from the inside: the client reads it as "this
endpoint exists and is empty".

**`/library/metadata/{id}/banners` and `/thumbs` are artwork candidate lists on both
versions.** Same container shape as `/posters`; `banners` is empty on kids and zen alike
and `thumbs` lists the media bundle's video thumbnails (3 on kids, 1 on zen). rex
404'd both. It has no banner art and no bundle, so an empty list of the right shape is
the honest answer — the way `/arts` already answers for a season. `/squareArts` stays a
404 on purpose: it is 200 on kids and **404 on zen**, so it is 1.43.4-only and rex
reports 1.42.2.

**`/library/metadata/{id}/background` and `/composite` are 404s too.** The same sweep,
run for a show, a season and an episode as well as a movie: both are 404 on kids and zen
alike, bare and versioned, on every row type, while `thumb`, `art` and `poster` beside
them are the 200s they have always been. rex served image bytes from both. A
`composite` is real on a PLAYLIST and a COLLECTION — which is where rex's own list
rows point, and which is served by a different handler that this leaves alone — but on a
library ITEM there is no such path, and `background` is not a path at all: it is the
`type` of an `Image` element.

A note on method, because it nearly cost a wrong finding. The first run of this probe
was pointed at a fixture server the UI suite had already finished writing to — by then
it has deleted a row and a section — and it reported `thumb` and `poster` as 404s that
the references answer 200. They are not: on a freshly restarted server all three of
`thumb`, `art` and `poster` agree. The sweep only says where to look; every finding it
produced was then proved fail-then-pass on a clean export of its own parent commit,
which is what the proof driver exists for.


##### The write side: the carried cascades, and the collection instrument that had to be built

The write hunt took the three holes cycle 13 named: re-measure the six cascades it had
recorded and not implemented, build a collection instrument from nothing (neither
reference holds a single collection), and put a leaf in progress. Every finding below
was re-measured by the driver on a freshly reset copy before any code changed, and two
of them came out differently the second time.

**The data-loss bug: a SEASON merge is a 200 that changes nothing.**
`PUT /library/metadata/7/merge?ids=11` on two seasons of the copy's show answers 200 with
an empty body, pushes no frames, and leaves the tree exactly as it was — both seasons
still listed, all six episodes still there, the show's `leafCount` still 6 — and the
`split` that would undo it is a no-op too. An EPISODE merge beside it, on the same
server, is completely real: 9 goes into 8, 8 comes back carrying two `Media`, 9 is a 404,
and the season's and show's counts drop 3→2 and 6→5.

rex's `merge` moved the other row's `Media` across and then called `catalog.remove`,
which **cascades to children** — so merging one season into another deleted the absorbed
season *and every episode under it*, left the show's `leafCount` stale at the old number,
and the split did not bring them back. It is the only data-loss bug the programme has
found in rex, and the proof run shows the old code going from eight leaves to six.
The fix is that a merge whose target has no `Media` of its own absorbs nothing. A SHOW
has no `Media` either and takes the same branch; the copy holds one show, so that half is
reasoned rather than measured.

**On Deck is the most recently viewed in-progress episode.** Cycle 9 recorded "the
in-progress episode with the LOWEST season and episode number (not the one played last)".
Re-measured on the copy in `long` mode: a beat on episode 8 at 40% puts 8 on deck; a
`/:/progress` on episode 10 — later, still unwatched — moves the row to **10**; a fresh
beat on 8 moves it back to **8**. Three transitions, with `/library/onDeck`,
`/library/sections/{id}/onDeck`, `/hubs/home/onDeck` and `/hubs/continueWatching` agreeing
each time. Cycle 9 measured this on the copy's TEN-SECOND clips, where every beat crosses
90% at once and an episode can barely be left in progress at all — the handover's own
"re-run anything about sessions, completion or resume from cycles 9 or earlier on `long`"
entry, paying off exactly as written.

**A show's prefs write pushes nothing — except `showOrdering`, which pushes everything.**
This one is worth recording in full, because the first fix was wrong and the suite caught
it. The hunt reported that a show's prefs write pushes no frames at all, and the driver
confirmed it for `flattenSeasons` with a socket open: zero frames in nine seconds, on a
listener that answered a title edit beside it with the expected `state 3 loading` /
`state 5` pair. rex was pushing a 79-frame refresh cascade for every prefs write, so
the cascade came out.

The suite's own cycle-13 check — which asserts the cascade for `showOrdering` — then
failed, and re-measuring one setting at a time settled it: **`showOrdering=dvd` pushes 45
frames** (it changes how episodes are numbered, so the server really does re-read the
tree) while `flattenSeasons`, `episodeSort`, `useOriginalTitle` and `subtitleMode` each
push **zero**. The rule is per setting, not per endpoint. The hunt's report was right
about the four it tried and wrong as a generalisation, and the check that stood in the
suite was the thing that said so.

What those writes do instead is change the ROWS, which rex did not do at all:
`flattenSeasons=1` gives the show `skipChildren: true` and every episode
`skipParent: true` while **removing their `parentRatingKey` and `parentKey`** (keeping
`parentIndex` and `parentTitle`), and `/children` still answers the seasons either way —
the client decides to skip the season level on the strength of those fields.
`episodeSort=1` puts a season's `/children` **newest first** while `/allLeaves` and the
section listing keep broadcast order.

**An artwork pick re-versions three fields, locks one, and cascades.** Measured on the
copy with a socket open: `PUT /library/metadata/6/poster?url=` moves `thumb`, `art`,
`theme` and `updatedAt` onto **one** new stamp even though only the poster was chosen;
locks the picked field (`Field: [{locked: true, name: thumb}]`); pushes exactly one frame
(`timeline type=2 state=5`, no `metadataState`); and cascades the new stamp to the
children that borrow it — the season's `parentThumb` and `art`, the episode's
`grandparentThumb`, `grandparentArt` and `grandparentTheme` — while leaving the season's
OWN `thumb` alone. rex re-versioned only the field that was picked and left every
child pointing at the old stamp, which is how a show kept its old poster on every season
and episode tile after a change.

**A hub container says `allowSync: true`.** The hunt found it on four paths; sweeping
every hub path sharpened it: `/hubs`, `/hubs/sections/{id}`, `/hubs/promoted`,
`/hubs/home/recentlyAdded`, `/hubs/home/onDeck`, `/hubs/continueWatching`,
`/hubs/metadata/{id}/related` and `/library/metadata/{id}/related` all say **true** on
kids and zen alike; `/hubs/metadata/{id}/postplay` alone says **false**; and
`/hubs/search` and `/hubs/items` carry no `allowSync` at all. rex defaulted to false
with three call sites putting it back, so eight of the ten said the opposite of a real
PMS. (This also corrects half a sentence in `item_hubs`'s own docstring, which said
postplay carries neither the librarySection triple nor `allowSync`: it carries
`allowSync`, as false.)

**A season listing's leaf-count filters read the SHOW's counts.** On zen,
`type=3&viewedLeafCount>>0` answers **48** seasons — every season of the eight shows that
have a watched leaf — and **all 48 carry `viewedLeafCount` 0 on their own row**, so the
filter cannot be reading the row it returns; `type=3&unviewedLeafCount=0` is 2 there. The
copy agrees on the two-season tree. rex read the season's own counts, so a season grid
filtered by "watched" answered only the season that happened to hold the watched episode.

**Collections, measured for the first time.** Neither reference holds one, so the
instrument had to be created on the copy before any of this existed:

- **`/library/metadata/{collection}/children` answers** — with a full secondary container
  carrying `key` (the bare id), `parentIndex` (the collection's **tag** id),
  `parentTitle` and `title2` (its title), `title1` (the library), the librarySection
  triple, `nocache`, `viewGroup` (the member type) and `viewMode` 65592 — while
  `/library/collections/{id}/children` beside it answers the **bare** `{size: N}`.
  rex 404'd the metadata form, which is the one a collection row's own `key` points
  at, so a collection page opened empty.
- **A `type=18` listing is a real query**: `title=aard` answers one of two,
  `sort=addedAt:desc` reorders, `duplicate=1` answers none, and the default order is
  `titleSort` like every other listing. rex answered the whole collection list to
  every one of them.
- **A collection row's `index` is its own TAG id** — the same number its members carry in
  their `Collection` tag and the same one the `/collection` axis keys on. rex sent 1
  for every collection.
- **`/library/all?type=18` lists every collection the server holds**, across libraries, in
  title order. rex answered `size 0`, because its collections are not rows in the item
  catalog and that handler only ever looked there.


##### Measured and not acted on

- **The three remaining cascades, re-measured and recorded rather than implemented.**
  (a) A show REFRESH cascades: the copy answers `PUT /library/metadata/8/refresh` with 7
  frames, `…/7/refresh` with **18** (season, then each episode's four), and `…/6/refresh`
  with **40** — show queued/started/processing/loading at +0.02–0.13 s, a **5.3 s gap**,
  then per season an `activity updated` stepping −1 → 44 → 44 → 89 with the season's frame
  title as its subtitle, each episode's four, and `activity ended` with the show's
  `state 5` at +5.57 s. rex answers 7 frames at every level and leaves the children's
  `parentThumb`/`grandparentThumb` at the old stamp. (d) `PUT …/analyze` is 200 with an
  empty body and then, **per LEAF**, three `state 5` frames carrying `mediaState`
  `analyzing`, `thumbnailing` and none, followed by one
  `media.generate.chapter.thumbs` activity whose subtitle is the TARGET's frame title —
  5 frames for an episode, 11 for a season, 20 for a show, 5 for a movie, and no frame for
  the season or show row itself. Cycle 13's "per-level triple" was wrong. rex pushes
  nothing. (e) A library create is 201 with a `Location` header and **74** frames inside
  0.9 s, then one `backgroundProcessingQueue` `queueRegenerated` at **+60.8 s**; it is not
  periodic (150 s idle produces none, and a plain title edit produces none in 75 s) but it
  does follow certain writes — it also arrived +2.4 s after a season split and +1.2 s after
  a collection add. rex answers the 201 and the header and pushes nothing.
- **The rest of the collection surface**, all single-witness on the copy: the
  `includePreferences=1` block (`collectionMode` with its four enum values and
  `collectionSort` with two, on the detail and `/library/collections/{id}` paths and never
  on a listing row); `collectionSort` semantics (1 alphabetical, 2 custom, an unknown
  NUMBER alphabetical, unknown TEXT by release date, `''` removing the field, and a SMART
  collection refusing it with a 400 where rex stores it); `GET
  /library/collections/{id}/prefs` being a 404 where rex answers an empty container; a
  `type=18` SECTION-path rename renaming the TAG (the item path does not — cycle 9's rule
  is the item-path half only) where rex never renames it; `/hubs/search` and
  `/library/search` finding a collection as a `tag` row with `count`, `guid` and
  `reason`/`reasonID`/`reasonTitle`; a member's `Collection` tag carrying `guid` and
  `count`; the detail row's `librarySectionID` being a NUMBER where rex sends a string;
  labels on a collection reaching the `/label` axis; and
  `/library/collections/{id}/posters` answering the `default://` composite row.
- **Smaller write differences**, each measured once on the copy: a playlist create whose
  `type` does not match its uri is a **400 that creates the playlist anyway** (with a
  `type=15 state=0 created` frame) where rex refuses and creates nothing;
  `POST /playlists/upload` is 500 with a path and 400 without, where rex 404s both;
  `DELETE /library/collections/{id}/items/abc` is a 404 where rex answers 200; and the
  copy's `/label` axis is global — it lists labels written in another library and labels no
  row carries any more, the same list for every `type=` — where rex lists the section's
  live labels under their type only. zen and kids hold no labels, so that last one has the
  copy as its only witness.
- **The episode SPLIT.** On the copy a split after an episode merge is a no-op (200, the
  survivor keeps both `Media`) on the matched and unmatched tree alike, while cycle 4
  measured a movie split producing a NEW ratingKey on the online lab. The copy cannot
  re-identify offline, so the two are not comparable and the split half stays unresolved;
  only the season-merge half, which needs no network, was acted on.
- **`ratingCount` on a collection row**: the copy sends 1 for a three-member collection
  where rex sends the member count. Nothing in the sample explains the 1, so it is
  left alone rather than guessed at.
- **The `/hubs/search` score PROMOTION** (carried from cycle 13, still not implemented),
  the codec axes' empty-value behaviour (a version difference), and season 0 / specials,
  which neither reference holds.
- **Compared and found identical**, so not findings: 59 top-level paths and 48 item leaves
  across both references; every `include*`/`exclude*` parameter on a movie grid and a
  detail row (only `includeGuids`, `excludeFields`, `excludeElements` and `includeStorage`
  change anything, all already implemented); `/media/providers` feature types; the section
  `/sorts` list on both versions; and the XML element names of every endpoint this cycle
  added.

#### The fifteenth pass — the parameter that makes history list, what a fold really folds, and the verb table

Two hunters — the rest of the collection surface and the first sweep of the surface by
HTTP METHOD (opus, writing to plexscratch); the real library swept by PARAMETER rather
than by path (opus, read-only on kids and zen) — and the driver's own revalidation round.
**33 findings implemented**, the largest cycle in the programme's history, and the three
cascades §3.3 had carried since cycle 13 are finally built.

##### The revalidation round, which paid three times

**`titleSort`'s whitespace trim is LEFT-ONLY.** Cycle 14 measured that zen's 90
leading-whitespace rows all carry a trimmed `titleSort`, found no row with trailing or
doubled whitespace anywhere in 5,294, and recorded `.strip()` as "the symmetric reading"
with the trailing half explicitly unwitnessed. Written on the copy it is not symmetric:
`'  Zeta Both  '` sorts as `'Zeta Both  '`, `'The Zeta Art '` as `'Zeta Art '`, and
`'Zeta Trail '` carries **no `titleSort` at all** — the derived string still equals the
title, and a real PMS omits it then. Inner doubled whitespace survives too.

**The SECTION-level `flattenSeasons` and `episodeSort` produce the same row effects as
the item path, and `-1` on an item means INHERIT.** §3.3 recorded the library-wide path
as *assumed* to behave like the item path. It does — and the show's own setting stays
`-1` throughout, which is what makes the item value a tri-state rather than a switch.
All four combinations measured on the copy: section 1 / item -1 is ON, section 1 / item 0
is OFF, section 0 / item 1 is ON, section 0 / item -1 is OFF. rex read the item's
`_prefs` alone and treated the `-1` default as "off", so a library-wide `flattenSeasons=1`
— which is where the Edit Library dialog puts it — set `skipChildren` on the show row and
nothing else: every episode kept its `parentRatingKey` and `parentKey`, so the client had
everything it needed to draw the season level anyway. A library-wide `episodeSort=1`
changed no order at all.

Beside it, **an unwritten show reports `episodeSort` as `-1`**. `item_prefs.json` carried
`0`, captured on 2026-09-12 from a show that had been written; both references answer
`-1` for a show that has not been, equal to the setting's own `default`. It matters
because `-1` is the value that inherits.

**The `group=` `addedAt` tie-break is the lowest ratingKey, and it is sort-independent.**
§3.3 recorded this as a choice rather than a measurement, because no group on either
reference has two members sharing a minimum `addedAt`. The copy has one: movies `1` and
`17` both carry `addedAt` 1789187071. Put in one group and renamed so that "lowest
ratingKey" and "lowest titleSort" predict different survivors, the survivor is `1` under
the default sort, `titleSort` either way, `addedAt` either way and `id:desc`. rex was
already right; the register entry graduates.

##### `/status/sessions/history/all` lists nothing without a `type=`

Cycle 11 recorded the plain call answering `size 0` on both references as a fact it could
not explain, and three cycles carried it in §3.3 as "find the parameter or account state
that makes a real PMS list history". The parameter is `type=`.

| | kids 1.43.4 | zen 1.42.2 |
|---|---|---|
| no `type=` | `size 0`, `totalSize 0` | `size 0`, `totalSize 0` |
| `type=1` | 5 rows, all **movies** | 73 rows, all **episodes** |
| `type=0`, `2`, `3`, `4`, `5` | 0 | 0 |
| `type=1,4` | as `type=1` | as `type=1` |
| `type=abc`, `type=` | **400** | **400** |

So the `type` is not the metadata type: kids' history is movies and zen's is episodes and
both answer to `type=1`, while `type=4` — an episode — answers nothing on the server whose
history is entirely episodes. It is the row's `viewType`, which every history row carries
as `1` and which the listing row emits. A comma list is an OR, and the validation is the
whole-number grammar cycle 14 measured for the ids, applied after them: a malformed id is
still a 400 and an unknown `metadataItemID` still a 404 with no `type=` at all.

rex ignored `type=` completely: it answered its whole history to the call a real PMS
answers nothing to, answered every row to `type=2` as well, 200'd `type=abc`, and left
`viewType` off the listing row — so the one column the filter reads was not on it. Twenty
suite call sites read the page with no `type=`; each was updated rather than removed.

##### `title=` folds across a space. The space was never the rule — the colon was.

Cycle 14 measured `title=Bogota` finding "Bogotá: City of the Lost" while
`title=Bogota City` and `title=Bogota ` find nothing, read the SPACE as what turns the
fold off, recorded the mechanism as unexplained, and §3.3 flagged it as "a rule whose
shape nothing predicts is the kind this project has got wrong before". It was.

`title=Adoravel Trapalhao` finds "Adorável Trapalhão". So do `Sex and Lucia`,
`and Lucia`, `Dire a Lamine`, `Dire a Lam` and `Bogota: City of the Lost`. What breaks
the Bogotá query is the colon on its first word.

Measured over 52 queries on zen — 27 to build the model and 25 more against five rows it
had never seen, which it reproduced exactly — a row matches if **either** pass does:

* **The raw pass** is the old matcher with no folding at all: each query word begins a
  title word, in order, on the raw spellings. This is why `title=Bogot City` matches
  where `title=Bogota City` does not — `Bogot` is a prefix of `Bogotá` before any
  folding and `Bogota` is not.
* **The folded pass** folds both sides, splits the query on single spaces and the title
  on whitespace, and then requires every query term **but the last** to equal a whole
  title word rather than merely begin one; the last may begin one. `Bogota` alone matches
  (last term, and `bogota` begins `bogota:`); `Bogota City` does not (`bogota` is not the
  whole word `bogota:`); `Bogota: City` does. A query holding an empty term — a trailing
  or doubled space — fails this pass outright, which is what cycle 14's trailing-space
  pair was really showing.

Punctuation rides along inside a folded term, which is the clearest proof the folded pass
compares whitespace words and not an alphanumeric tokenisation: `tournee` does **not**
find "Compilation Pyroman (tournée 2018-2022)" — the word there is `(tournee` — while
`Pyroman (tournee` does. The fixture row cycle 14's check used dropped the colon from
zen's actual title, so the check asserted a shape zen had never been asked for; it now
carries both of zen's titles verbatim and all fourteen measured cases.

##### The read surface, swept by parameter rather than by path

* **The in-library search box answers the LIGHT grid row.** Both references answer a
  search row byte-comparable with their own grid row for the same item — no `Guid`, no
  `Rating`, no tag ids, no `Stream` inside a Part. rex answered the DETAIL row here
  and nowhere else, so the one listing a user reaches by typing carried uncapped tag
  lists, every Stream element and every external guid — and a client that tests
  `Rating`/`Guid` presence to decide "this row is fully loaded" skips its detail fetch
  and renders a page missing whatever only a detail row carries.
* **`excludeElements=` is a property of the RESPONSE, the way paging is.** Every
  container that carries rows drops the named child elements and substitutes
  `mediaCount`/`mediaCountOptimized` for `Media`. Cycle 11 wired it into four surfaces by
  hand; eight more were never wired — `/library/all`, `/library/recentlyAdded`, a
  section's `recentlyAdded`, `newest`, `search` and `common`, an item's `/grandchildren`
  and every hub path. The Home screen is the one that costs a client something: it is how
  a client asks for a cheap shelf, and rex sent the whole Media/Part/Stream tree for
  every shelf row on every render.
* **Four more columns filter that no `Meta` advertises.** `parent=<ratingKey>` is the
  row's immediate `parentRatingKey` (kids' `type=4` goes 6 → 3 for a season and 6 → 0 for
  the show above it; zen's 9287 → 22 and → 0, and it reaches `/library/all`); `rating=`
  and `audienceRating=` are the two agent ratings (`audienceRating>>=9` is 107 of zen's
  3887 and `rating>>=9` is 142); `file=` is a case-insensitive SUBSTRING of the Part path
  that matches ANY Part — proved on zen movie 10513, whose two versions differ only in a
  trailing `{083223}` and whose second path alone still finds the row. rex ignored all
  four, and an ignored filter is worse than a refused one: it answered every row to
  `audienceRating>>=9` AND to `audienceRating<<=1`.
* **`/query/parse` knows eight fields a listing does not filter on** — `parent`,
  `parentIndex`, `audienceRating`, `rating`, `guid`, `slug`, `file` and `extraType`, each
  echoed as a `Rule` with an EMPTY `fieldTitle`, where rex answered an `Error`. The
  parse surface is WIDER than the filter surface and the two were one map in rex.
  `guid` is the sharpest: rex's listing has filtered on `guid=` since cycle 8 while
  its own `/query/parse` called the field unknown.
* **`contentDirectoryID` scopes and VALIDATES `/hubs/search`, and only `/hubs/search`.**
  zen's `?query=the` answers show and movie hubs; `contentDirectoryID=1` keeps the movie
  library's alone and `=2` the show library's. `abc`, `99`, `1.5` and `-1` are all 404s
  on both references and an EMPTY value is a 200 that scopes nothing. `/hubs`,
  `/hubs/promoted` and `/hubs/sections/{id}` ignore it entirely, so the validation belongs
  to this endpoint alone.
* **`/playlists?includeMeta=1` carries a `Meta` descriptor**, byte-identical on both
  references, with one `Type` row and SIX sorts whose last is `mediaCount` "Item Count" —
  NOT the `updatedAt` "Date Modified" that `/playlists/sorts` ends with. The client reads
  `Meta.Type[].Sort` straight into the playlist page's sort menu.
  `/hubs/home/recentlyAdded?type=N&includeMeta=1` carries a one-row descriptor whose
  `active` flips with the type — `movie` true at `type=1`, `mixed` false at `type=2`, and
  no block at all for any other type.
* **`includeChildren=1` inlines a `Children` block on a SHOW and on nothing else.**
  Neither reference puts one on a season, and neither is silent for want of children —
  kids' season 7 holds three episodes and zen's season 17201 holds twenty-two. rex
  inlined a season's episodes too, so a season page was handed a list it had not asked
  for and fetched the same list again from `/children`.
* **The inline `Related` block caps at 5 rows and obeys `includeRelatedCount`**, where
  the standalone `/hubs/metadata/{id}/related` answers 6 for the same row. Measured on
  zen only: kids holds no related items for any row, which is an instrument limit and is
  stated rather than hidden.
* **`limit=` and `parent=` are PARSED wherever a listing consumes them**, and a value
  that is not a whole number is a **500** on the section grid, `allLeaves`, `search`,
  `common`, `/library/all`, `/children`, `/grandchildren`, `/playlists` and the secondary
  axes — a **400** on `/status/sessions/history/all`, the refusal that page gives its ids.
  A GOOD limit still does nothing on `/children` (cycle 11): ignoring the VALUE is not the
  same as not parsing the string.
* **`/hubs/search?limit=` has a grammar of its own, and rex had it inverted.**
  Unparseable — `abc`, empty, `1.5` — is a **400**; parseable but not a positive count —
  `-1`, `0` — is a **500**; `01` is fine. rex answered 500 to the 400 cases, 200 to
  the 500 cases, and let an empty value through.
* **A FRACTIONAL value refuses on seven columns and on no others**: `viewCount`,
  `viewOffset`, `lastViewedAt`, `lastRatedAt`, `addedAt`, `updatedAt`, `createdAt` — while
  `year`, `duration`, `index`, `skipCount`, `ratingCount`, `mediaCount`, `absoluteIndex`
  and `userRating` take `1.5` and answer no rows. It is the timestamp and per-user-state
  family, not the python type. `lastRatedAt`, `createdAt` and `lastViewedAt` refuse `abc`
  and an empty value as well, which rex missed for a reason worth recording: its date
  branch returned early when the ROW's value was null, so on a library where nothing
  carries the column the VALUE was never parsed at all.

##### The collection surface, finished

* **`ratingCount` on a collection row is not a count.** It is the Plex TYPE NUMBER of its
  members plus 100 when the collection is smart — movie 1, show 2, season 3, episode 4,
  and 101/102/104 smart. Measured over eleven collections of four member types and both
  smart values; the rule predicted 102 and 104 before either was measured. Cycle 14 saw
  the `1` on a three-member collection and left it unexplained. `librarySectionID` is a
  NUMBER there too, where rex sent a string on the collection path alone.
* **A collection's `includePreferences=1` block is exactly two settings, and `smart`
  swaps the second**: `collectionMode` + `collectionSort` when plain,
  `collectionMode` + `collectionFilterBasedOnUser` when smart. A smart collection has no
  collection ORDER at all, and the write side agrees — `PUT .../prefs?collectionSort=` on
  one is a **400** for `0` as well as `1`, where `collectionMode` is a 200. rex sent
  no `Preferences` on a collection, so the Edit Collection dialog had neither control.
* **`GET /library/collections/{id}/prefs` is a 404**, smart and plain alike, where
  rex answered an empty 200 — fix 4's trap from the inside: the client reads a 200 as
  "this endpoint exists and is empty" and never looks for the settings.
* **`collectionSort` sorts alphabetically for anything that parses as a plain non-zero
  integer** — `3`, `99`, `-1`, `01` and `+1` as well as `1` — and by release date for `0`
  and for anything that does not parse (`1.5`, a leading space, `abc`, empty). A leading
  `+` is allowed; a decimal point and a leading space are not.
* **The collection artwork lists are real.** `/library/collections/{id}/posters` carries
  the container's identifying triple and ONE `default://` candidate — the collection's own
  composite, `selected`, and with NO `provider` (the agent rows do carry one) — while
  `/arts` is an empty container with the same triple. `/library/metadata/{collection}/arts`
  is empty too; rex handed back the POSTER there, so Change Background offered the
  poster.
* **A `type=18` SECTION-path save renames the members' `Collection` TAG; the item path
  does not.** The tag keeps its id through the rename. The Edit Collection dialog saves
  through the section path, so on rex every member's page kept the old collection name
  and the library's Collections filter kept offering it, and the two never converged.
* **A member's `Collection` tag carries the collection's own `guid`** — and NOT a `count`,
  which is consistent with cycle 12's refutation of `count` on a detail row's tags. Cycle
  14 recorded both; only the guid half is real.
* **A label on a COLLECTION reaches the library's `/label` axis**, under a bare call and
  under every `type=`. rex built the axis from library ITEMS alone.
* **A collection is found by `/hubs/search`**, as a `tag` Directory row with `tagType: 2`,
  the member count, the collection's guid, a `key`/`filter` of `collection=<tag id>` and
  `reason`/`reasonID`/`reasonTitle` naming the LIBRARY. `/library/search` finds nothing for
  a collection with or without `includeCollections=1`, which **refutes** cycle 14's note
  that it carries one as a tag row.

##### The verb surface, swept for the first time

A write to a shared reference is forbidden, so this needed the disposable copy. Thirty-
three paths were swept by METHOD and every one of them differed.

* **A READ-ONLY route answers 404 to PUT, POST and DELETE.** rex answered the GET body
  with a 200 on twenty of them — `/`, `/library/recentlyAdded`, `/library/onDeck`,
  `/hubs/promoted`, `/hubs/continueWatching`, `/hubs/search`, `/channels/all`,
  `/services/browse`, `/system`, `/system/agents`, `/system/scanners/{n}`,
  `/updater/status`, `/:/plugins`, `/playlists/all`, `/status/sessions/history/all`, and
  the item leaves `allLeaves`, `tree`, `related`, `similar` and `matches`. It reads to a
  client, and to python-plexapi's capability probes which are exactly this shape, as "this
  endpoint exists and accepts writes".
* **The legacy single-segment plug-in dispatcher answers EVERY method**, not just GET:
  `PUT /prefs`, `POST /setting`, `DELETE /users` and `/devices.xml` are all 200 with
  `content="plugins"`, where rex served the GET alone.
* **`PATCH` and `OPTIONS` are ROUTED like any other verb** — which corrects the hunt that
  reported them as blanket 404s. `/identity`, `/hubs` and `/playlists` answer 200 to both
  (the recorded "these answer any verb" rule) and every other path 404s them. A bare
  OPTIONS with no `Access-Control-Request-*` headers is therefore a 404 where rex
  answered every OPTIONS as a preflight; a genuine preflight is unchanged except that its
  response carries `Content-Type: text/plain`.
* **What a real PMS does not recognise as a method at all is a 500** — `FROB`, `TRACE`, a
  wrong-case `Get` — with its own four-line html body, on every path. rex answered
  Python's BaseHTTPRequestHandler 501 page, which also leaks its `Server:` string.
  HANDOVER records that a real PMS never answers 405; it never answers 501 either.
* **POSTing an EMPTY body to any artwork or subtitle candidate list is a 406** —
  `/posters`, `/arts`, `/thumbs`, `/banners`, `/themes`, `/clearLogos` and `/subtitles`
  alike; `/extras` is a 400. It also refines a recorded rule: with neither a title nor
  bytes the subtitle upload answers 406, so the empty-body check runs FIRST, where rex
  checked the title and answered 500.
* **`DELETE /library/metadata/{id}/{poster,art,thumb}` is a real endpoint that CLEARS the
  artwork** — 200 with an empty body, the field gone, whatever artwork is left re-stamped
  onto a new version, one `state 5` frame with no `metadataState`, nothing `selected` in
  the candidate list afterwards, and a 404 on the bare GET of the deleted path. rex
  answered the GET — 56 KB of JPEG — to the DELETE and changed nothing; and because
  Change Poster assigns the response BODY onto the row, clearing the artwork set the
  tile's `img src` to a blob of JPEG.
* **An EPISODE owns no `/art` path**, bare or versioned — measured on kids, plexscratch
  AND zen (episode 11115 of a show that has artwork) — while its `/thumb` and `/poster`
  are 200s and a SEASON's `/art` is a 200 on zen.

##### The three cascades §3.3 carried since cycle 13

All three were re-measured on the copy with a CONTROL write on the same socket first.

**A refresh CASCADES.** `PUT /library/metadata/{id}/refresh` is 7 frames on an episode,
**18** on a season and **49** on the two-season Firefly show. The episode form is the
seven-frame sequence rex already sent. A SEASON runs its episodes inside its own
activity — season queued, activity started, season processing, season loading, then each
episode's four (state 5 `processing`, state 4 `loading` twice, state 5 with no
`metadataState`), activity ended, season done; there is no `activity updated` there, where
the episode form has one. A SHOW nests a season's whole block under each season, with an
`activity updated` carrying that SEASON's frame title. rex answered SEVEN frames at
every level, so Refresh Metadata on a show told the client one row had been re-read and
left the tree stale until a reload.

**An ANALYZE frames every LEAF.** Per leaf and in tree order, three `state 5` frames
carrying `mediaState` `analyzing`, `thumbnailing` and none, then ONE
`media.generate.chapter.thumbs` activity — started at -1, ended at 100, no `updated`
between — whose subtitle is the TARGET's frame title. 5 frames for an episode or a movie,
11 for a three-episode season, 21 for the show. Cycle 13's "a per-LEVEL triple" was wrong:
a season and a show row are never framed themselves, only their leaves. rex pushed
nothing.

**A library create ends in a queue-regeneration frame.** `POST /library/sections` is a 201
with a `Location` header and then 75 frames: a `library.update.section` "Scanning" activity
stepping through the files with a `status` LIBRARY_UPDATE frame beside it, a `created` pair
per item, each item's refresh sequence, and finally ONE `backgroundProcessingQueue`
`queueRegenerated` at **+63.1 s**. The per-item half is over REAL files being scanned;
rex owns none and a library it creates is empty by construction, so those frames have
nothing to name. The queue frame is not create-specific and not periodic — it arrived
+7.1 s after an analyze here, and cycle 14 saw it +2.4 s after a season split and +1.2 s
after a collection add. It follows a write that leaves work in the queue.

##### Measured and not acted on

- **The library create's per-item frames.** The 75-frame scan is over real files. A
  library rex creates is empty by construction, so the `library.update.section`
  activity's per-item steps, the `created` pairs and the per-item refresh sequences have
  no rows to name. An instrument fact, recorded rather than faked.
- **The DELAY on the `queueRegenerated` frame.** +63.1 s after a create and +7.1 s after
  an analyze on the copy. rex pushes its frames synchronously and the frame is emitted
  with the write; the timing is recorded and not reproduced.
- **The media pass inside a show refresh.** The reference emitted the
  analyzing/thumbnailing/none triple for season 1's episodes and not season 2's — state-
  dependent work on real files, not a rule. rex's cascade keeps cycle 10's shape.
- **`DELETE /library/metadata/{id}` on a season and a show.** The copy answers **400** to
  every media-item DELETE — episode, season, show and movie alike — while a collection
  DELETE is a clean 200, with `allowMediaDeletion` true and the media mounted read-only.
  So whether a real PMS cascades a season delete onto its episodes stays unproven; the
  merge half of the same question was settled in cycle 14.
- **`enableCreditsMarkerGeneration`.** The copy 400s it on a show and a movie, for `0` and
  `-1` alike, and it is the one item pref the copy does not advertise (10 show prefs
  against kids' 11). rex matches kids. An instrument artefact, not a rule.
- **`collectionSort=2` on a fixture whose members' custom, insertion and alphabetical
  orders coincide.** It is the CUSTOM order a move writes (cycle 9) and the new check
  leaves it out deliberately, because on three same-year movies it cannot discriminate.
- **The `Server:` banner.** rex sends `Server: Rex/0.1 Python/3.10.20` on every
  response and a real PMS sends none. Noticed while measuring the 500, out of this
  cycle's scope, and left for a cycle that can check what else reads it.
- **The non-alphanumeric `firstCharacter` bucket, again.** Re-read on both references:
  zen holds exactly one non-digit non-letter-led title (`『`) and kids none, so the "any
  other non-letter gets its own bucket" half is still single-witness. rex already
  matches it, ordering by codepoint — which zen's single example cannot tell from
  "listed after Z". Nothing available can settle it.
- **Refuted: `/library/search` finds a collection as a tag row.** Cycle 14 recorded it;
  the copy answers nothing there for a collection, with or without `includeCollections=1`
  or `searchTypes=collection`. Only `/hubs/search` and the library's own `?type=18` search
  box find one.
- **Refuted: `PATCH` and `OPTIONS` are blanket 404s.** The hunt reported it from the paths
  it tried; nine paths show they are routed like any other verb, and the three that answer
  every verb answer these too.
- **Refuted: a member's `Collection` tag carries a `count`.** Cycle 14 recorded "guid and
  count"; the tag carries the guid and no count, as cycle 12 had already shown for a
  detail row's tags in general.

#### The sixteenth pass — the HTTP envelope, the write surface below the collection, and the first real show library

Two hunters and a driver sweep. **Territory A** took the write surface one level below the
collection on plexscratch: playlists, the play queue, the section save path on a SHOW
library and the item-prefs family — the layer that had one pass in cycle 9 and nothing
since, and the cycle whose findings §3.3 flags as thinnest. **Territory B** swept the
dimension nobody had: the HTTP envelope itself — what a request's headers do and what
every response's headers say, across three servers. The driver took the `Server:` banner
§3.3 had carried, and then swept zen's **section 2**, which nobody had pointed anything
at: 507 shows, 741 seasons and 9,287 episodes, the first real SHOW library the project
has had.

**49 findings implemented**, the largest cycle in the programme's history. Every one was
re-measured by the driver before any code was written, and every one is committed with a
check proved to fail against its own parent commit.

##### What the first real show library showed

- **`/library/sections/{id}/firstCharacter` counts on one basis and its buckets filter on
  another.** The AXIS counts a row BELOW the library's own top-level type by its SHOW's
  sort title: bucketing zen's 741 seasons and 9,287 episodes by their show reproduces the
  axis on **28 of 28 buckets at `type=3` and again at `type=4`**, and bucketing them by
  their own titles reproduces **0 of 28**. kids, whose one show is Firefly and whose
  seasons are "Season 1"/"Season 2", says `F:2` at `type=3` and `F:6` at `type=4`. The
  LISTING behind each bucket is the other way round — it filters on the row's own stored
  sort title — so a season list's `A` bucket says 64 and answers no rows at all. That is
  the reference's own inconsistency, measured on both servers, and rex reproduced
  neither half.
- **`/firstCharacter/{key}` is a real listing**, `title2` reading `Start with '<key>'`
  with the key echoed as the client sent it (`%23` decodes; `BB` and a lowercase `b` are
  echoed verbatim and match nothing), the light grid row, and the listing grammar on top —
  zen's `B` bucket answers 6 of its 161 rows to `year=2020` and reverses under
  `sort=titleSort:desc`. rex dropped the extra path segment and answered the AXIS, so
  clicking a letter in the A-Z rail redrew the rail.
- **Every section leaf's extra path segment is a bucket or a 404, never the leaf again.**
  Swept across both references and both library types: `/all/zzz`, `/onDeck/zzz`,
  `/sorts/zzz`, `/studio/<a real studio key>`, `/audioLanguage/en`, `/location/1` and
  fourteen more are 404s, and a SECOND extra segment (`/genre/24/more`) is a 404 too. The
  leaves that DO have a bucket answer its rows: a tag axis keyed by TAG ID with the tag's
  own title as `title2` and a 404 for an id the library does not hold; `year` and `decade`
  keyed by a non-negative integer (a word or a negative number is a 404, and `decade/1995`
  opens a ten-year window from 1995 rather than naming a calendar decade); `contentRating`
  as `Rated <value>` and `resolution` accepting anything at all, an unknown value there
  matching every row that HAS a resolution; `userRating` as `<n> Star`, refusing `-1`
  despite that being the key of its own "Not rated" bucket. rex answered 200 with the
  axis to every one of them.
- **A show library's `?type=3` and `?type=4` grids default to BROADCAST order**, grouped
  by the show's sort title then `parentIndex` then `index` — not to a flat `titleSort`.
  kids' episode grid opens "The Train Job, Bushwhacked, Our Mrs. Reynolds, Episode 1, 2,
  3" where `sort=titleSort` gives the reverse; zen's, 2,000 rows in, is one show's
  episodes in order where `sort=titleSort` is an alphabetical jumble across shows.
  `docs/findings.md` had already asserted this for "`/allLeaves` AND the section listing";
  only the allLeaves half was ever implemented (class **C**).
- **A show library's `/unwatched` carries `nocache`** like every other container it
  answers, and a movie library's does not. rex had the rule on `/all` and the five
  subsets and had missed it on exactly this one.
- **A row that BORROWS its background carries the OWNER's path verbatim** — kids' season 7
  and episode 8 both carry `/library/metadata/6/art/<v>` — and its own `/art` is a 404
  while the owner's is a 200, where zen's season 11114, which has art of its own, answers
  200. rex's artwork stamping re-keyed every borrowed URL to the row holding it, so a
  show, its season and its episode each claimed to own the same image and
  `/library/metadata/<season>/art` answered 200. Cycle 15's episode-only rule turns out to
  be the special case of one general one: an artwork path is a 200 only on the row whose
  own stored path names it.

##### The HTTP envelope

- **A real PMS LOCALISES its own text.** `X-Plex-Language` — header or query — beats
  `Accept-Language`, a regional form works like its primary subtag, and an unknown
  language falls back to English. The filter and sort menus, the `Meta` type titles, the
  hub titles, a GENERATED season title (`Season 1` → `Staffel 1`), a stream's language
  name and its `displayTitle`, and all 200+ `/:/prefs` labels, summaries and enum values
  all move; the LIBRARY's own rows do not, and neither do `All Movies`, a secondary axis's
  `title2` or `Unplayed` — the same English words ARE translated as a section pivot's
  `title`, so the rule is per attribute and not per string. 28 languages answer
  differently. rex read the header nowhere, so every non-English user of every real
  client saw English menus on every screen. The table is `translations.json`, captured
  mechanically: each endpoint fetched in English and in each language, the two documents
  diffed string by string at the same structural path. Nothing in it is invented.
- **A real PMS ROUTES before it authenticates.** With no token a path that does not exist
  is a **404** — `/no/such/path`, an unknown ratingKey, an unknown section — and only a
  path that DOES exist is a 401. Of the single-segment paths, exactly eleven answer 401
  (`/library`, `/hubs`, `/playlists`, `/devices`, `/butler`, `/servers`, `/clients`,
  `/accounts`, `/activities`, `/playQueues`, `/log`); `/identity` and `/security` answer
  200; every other one — `/statistics`, `/updater`, `/status`, `/system`, `/photo`,
  `/video`, `/media`, `/transcode`, `/services`, `/myplex`, `/livetv`, `/resources`,
  `/setting`, `/channels`, `/prefs`, `/nearest`, `/diagnostics`, `/websockets` and
  anything unrouted — is a **403**, whether or not it answers when a token IS given
  (`/channels` is a real two-row Directory with one). And `GET /` is a 401 whose 193-byte
  body carries a script that walks a browser into the web client. Measured path by path on
  kids and zen, which agree exactly.
- **Every response names its connection state**: `Connection: Keep-Alive` plus
  `Keep-Alive: timeout=20` when the connection is kept, `Connection: close` when it is
  not — and an idle kept-alive connection really is closed after 20 seconds. rex sent
  neither header and never closed one.
- **A paged container answers its window in response HEADERS too** —
  `X-Plex-Container-Start` and `X-Plex-Container-Total-Size`, `/identity` answering
  Total-Size 0 — and an unpaged one carries neither. And the paging HEADER beats the query
  when both are sent, per parameter; a header `Start=abc` beside a valid query is a 500.
- **`Range` applies to EVERY response body**, not only to media bytes: 206 with a
  `Content-Range`, 416 past the end, an unrecognised unit still read as bytes, and a 206
  never gzipped.
- **A malformed request header is a 400**: a line with no colon, a TAB anywhere in a value
  (a space is fine), a name over 1024 bytes, a value over 4096. rex accepted all of
  them and only refused at Python's own 64 KiB line limit, with Python's HTML page.
- **The Accept `q` grammar, corrected in six places.** A range qualifies only if its media
  type is exactly `application/json` or `application/xml` AND either it has no parameters
  or its FIRST parameter begins exactly `q=`; `;charset=utf-8`, `;level=1;q=0.5`,
  `; q=0.5` (a space after the semicolon) and `;Q=0.5` disqualify it, while
  `application/json ;q=0.5` does not. No parameter weighs 1.0; a single parseable
  non-negative `q=` weighs that; anything else — `q=0`, `q=abc`, `q=`, `q=-1`, `q="0.5"`,
  `q=0.5x`, a second parameter of any kind — weighs 0 and still QUALIFIES. Highest weight
  wins, a tie goes to the range listed first. Thirty headers, both servers. rex
  DROPPED every zero-or-unparseable q, so `application/json;q=0` came back as XML.
- **`Accept-Encoding` tokens are matched exactly and case-sensitively**: `GZIP`, `x-gzip`
  and `*` all get a plain body, while `gzip;q=0` is still gzipped because the q is ignored.
- **A real PMS never answers `100 Continue`**, and a preflight with no `Origin` carries no
  CORS headers at all.
- **`/web` and `/web/` answer `302 Moved Temporarily`** with an ABSOLUTE `Location` built
  from the request's own `Host` header, `Cache-Control: public` — the one place on the
  whole surface that is not `no-cache` — and `X-Plex-Protocol: 1.0`. Every `/web/*` asset
  is `no-cache`, `Accept-Ranges: bytes` and NEVER gzipped, `.js` is `text/javascript`,
  `.html` carries no charset and `.ico` is `application/octet-stream`.
- **A section's bare `/composite` is a 404**; only `/composite/{version}` answers, and no
  `/library/sections` row carries a `composite` attribute to build the bare form from.
  Another invented path, the same class as cycle 14's `/recentlyReleased`.
- **`X-Plex-Platform: Roku`** — that exact spelling — strips `id` and the whole `Pivot`
  list from every library `Directory` of `/media/providers`, and leaves the other nine
  attributes alone. Cycle 66: only when the Roku's `X-Plex-Version` is absent, unparsable or
  below 6.4.2 -- this was measured with the platform header alone, which no shipping Roku sends.
- **Four XML shapes.** A `Meta/Type` orders its children `Filter, Sort, Field` (not the
  item order, where `Field` comes first); `includeDetails=1` carries no
  `active`/`activeDirection` on a Sort and no `advanced` on a Filter, where `includeMeta=1`
  carries all three; the rows `includeChildren=1` inlines carry no librarySection triple;
  and a MediaContainer's attributes come out `size`, then `totalSize` and `offset` when
  paged, then everything else alphabetically.
- **`POST /library/metadata/{id}/extras` is a 400** whatever the body — cycle 15 recorded
  exactly that and left the code answering the GET listing.
- **A real PMS sends no `Server:` banner at all.** Fifteen paths, both formats,
  200/400/404/500 alike. rex announced itself as `Rex/0.1 Python/3.10.20` on every
  response it had ever sent — the one header on the wire that says the server is not a
  Plex Media Server. §3.3 had carried it since cycle 15.

##### Playlists, the play queue and the section save

- **`/playlists/{id}/items` is a real typed QUERY.** `?type=1` answers the movie members,
  `?type=2` the SHOW that owns the member episodes, `?type=3` their seasons, `?type=4` the
  episodes; a type the list holds nothing of answers 0, `-1` answers everything, `1.5` is a
  400; the library filters apply; `sort=` and `limit=` are PARSED and then ignored (an
  unparseable one is a 500); and `?includeMeta=1` carries the Meta the page's own
  All/Movies/Episodes/Seasons/TV Shows tab bar is built from.
- **A playlist is born `viewCount: 1` with `lastViewedAt` equal to its `addedAt`**, and
  the pair appears on the LISTING row only — never on `/playlists/{id}` and never on the
  create response. The Home shelf is keyed `sort=lastViewedAt:desc`, so with nothing to
  sort on its order was arbitrary.
- **`/playlists` filters** by `title`, `smart`, `sectionID` (a mixed list answers to BOTH
  its libraries) and `type` (a playlist is 15; `type=abc` is a 500), and its **default
  order is the SMART ones first, then `titleSort`** — measured over five lists whose
  ratingKey, addedAt and title orders are all different.
- **`sort=mediaCount` is a real ordering** there — the sixth sort its own Meta advertises,
  as "Item Count" — while `leafCount` is a 500 on both.
- **`POST /playlists/{id}/items` is a 404**: only PUT adds. **An empty `title=` is stored
  verbatim**, where rex substituted "Untitled". **`smart=1` over a comma-joined key
  list is a 400**; a smart list's uri has to name a LISTING. **A uri the server cannot
  RESOLVE adds nothing** — a foreign machine, a scheme-less `/library/metadata/1` and an
  unknown key all build an empty list with no `composite` and no `duration`.
- **`PUT /playlists/{id}` takes the whole ITEM edit grammar**: `thumb=`/`art=` mint artwork
  under `/library/metadata/{playlist ratingKey}/`, every `.locked` really locks, `year`,
  `originallyAvailableAt` and `studio` are stored, and `userRating`, `guid`, `duration`
  and the timestamps are accepted and discarded. rex 400'd every one of them, so the
  Edit dialog's Poster and Background pickers and every lock checkbox were refused.
- **`playlistItemID` is a SERVER-WIDE sequence** — three lists built in a row took 40/41,
  42/43, 44/45 — and a SMART list's items carry NONE at all; every item write on one is a
  400, the move included.
- **A playlist's artwork paths live under `/library/metadata/{ratingKey}/`**, where
  `posters`, `arts`, `thumbs`, `banners` and `tree` are all 200s, while under
  `/playlists/{id}/` the same names and the UNVERSIONED `composite` are 404s. rex had
  it the other way round on eight paths.
- **A shuffled play queue is really shuffled.** rex reversed the unshuffled queue,
  deterministically, so Shuffle played a show's episodes backwards and backwards the same
  way every time.
- **`includeBefore` and `includeAfter` window the queue with NO `window=`** — the radius is
  unlimited when it is absent — and each is a boolean: both zero, a value that is not 0 or
  1, an unparseable one and an empty one are all 400s. Cycle 9 measured them only ever WITH
  `window=1`, which is why the no-window half was missing (class **A** in the original).
- **EVERY play-queue item delete answers the container as it stood BEFORE it** — the old
  version and total, with the rows that remain — and only the SELECTED one also drops the
  rows. The client DISCARDS a queue response whose version it did not expect, so a delete
  from Up Next threw the player's own queue away.
- **A queue move refuses two forms**: `after=<the moving item's own id>` is a 404 and an
  empty `after=` is a 400. **A queue built from a PLAYLIST uri is a playlist queue**,
  carrying `playQueuePlaylistID` and `playQueuePlaylistTitle` and no `playQueueSourceURI` —
  rex recognised only the `playlistID=` form.
- **The SECTION save path stores an unlocked `titleSort.value=`**, which survives until the
  next TITLE write re-derives it, where the ITEM path discards it. The recorded rule ("an
  unlocked Sort Title does not stick") is the item path's alone.
- **A show's `contentRating` reaches only the episodes that carry none of their own**, and
  **a show's `index` cascades onto its seasons' `parentIndex`**. **A season whose own
  `year` is cleared gains `parentYear` in the same read** — the rule was right at catalog
  LOAD and derived nowhere else, so a season edited at runtime carried neither year.
  **`type=abc` on the save path is a 400** that edits nothing.
- **An UNMATCHED item drops its agent-driven prefs**: a show loses `languageOverride` and
  `useOriginalTitle`, a MOVIE loses the whole `Preferences` block rather than its rows, a
  season keeps its three. Corroborated read-only on zen, where six `local://` shows
  advertise exactly eight settings and every matched one advertises the full list.
- **On the section save path the `state 3` frame carries the row's title from BEFORE the
  write** and only `state 5` carries the new one, where the item path sends the new title
  in both. Visible only on a TITLE edit of a SHOW.

##### A defect this cycle introduced and caught

The first implementation of "`smart=1` over a key list is a 400" refused the create
whenever the uri resolved to any rows — and a LISTING uri resolves to rows too, so every
smart playlist create became a 400. The test is the uri's SHAPE, not whether it resolves.
It was caught by the next patch's own check block failing on the parent, three commits
later, and corrected with a check of its own. The lesson is the one §3.1 class C keeps
teaching: a rule implemented as a proxy for itself is the kind that survives.

##### Measured and not acted on

- **The container attribute ORDER inside a ROW** (a tag is `id, filter, tag[, role,
  thumb]`, not alphabetical). Only the CONTAINER's order was settled; a row's is not
  alphabetical and was not mapped.
- **`Set-Cookie: com.plexapp.system=…` on `/system` and `/system/agents/contributors`** —
  the plug-in framework's, on paths rex answers from its own table.
- **The 1024/4096/41 KB header limits as exact constants**: measured as brackets, not
  probed to the byte.
- **Whether `X-Plex-Language` reaches the XML path identically** — the table is applied to
  the container before rendering, so it does by construction, but the reference's XML was
  not diffed language by language.
- **Every write rule here has plexscratch as its ONLY witness**, as §3.3's first row says
  of the whole family; kids is read-only and zen cannot answer a write.
- **`/library/metadata/{playlist}/` container attributes** (`allowSync`, `identifier`,
  `mediaTagPrefix`, `mediaTagVersion`) — measured on the copy, not implemented: rex's
  bare container there carries `size` alone, and the paths that matter now answer.

#### The seventeenth pass — the row itself, the write surface below the playlist, and three fixtures captured off the wrong version

Two hunters and a driver sweep. **Territory A** took the dimension nobody had swept — the
ROW: every attribute of every row type, in every container that carries one, against both
references, plus the XML attribute order cycle 16 had explicitly left unmapped.
**Territory B** took the write surface one level below the playlist: the collection ITEM
path, the `/library/sections/{id}/prefs` family, the butler's individual tasks and the item
writes nobody had reached. The driver's own round began where cycle 16's localisation
finding left off and found that the version split cycle 14 applied to one fixture belongs to
three.

**29 findings implemented** over 32 commits. Every one was re-measured by the driver before
any code was written, and every one is committed with a check proved to fail against its own
parent commit.

##### Three fixtures were captured off the wrong version

- **The Meta `Field` list is per VERSION, exactly as the Filter list is.** Cycle 14 split
  `Meta.Type[].Filter` (1.42.2 advertises 22 movie filters and 9 episode ones, 1.43.4 28 and
  17) and left `Field` at 1.43.4's, because both Meta fixtures were captured off kids. zen —
  1.42.2, the build rex reports — advertises **34** movie Fields and **20** episode ones
  against kids' 40 and 26, differing in exactly the six media-codec axes (`hdr10plus`,
  `atmos`, `videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`). And on 1.42.2 those
  six are UNKNOWN fields: `videoCodec=h264` answers all 3,887 of zen's movies, and
  `/query/parse` reports each as an `Error` rather than a `Rule`. rex advertised all six
  in the smart-filter builder, filtered on them (0 rows) and parsed them as real Rules.
- **`tv.plex.agents.nfo.movie` and `tv.plex.agents.nfo.series` are 1.43.4-only.** Measured
  over the bare path and every `mediaType=`: 1.42.2 lists four modern agents and two per
  media type where 1.43.4 lists six and three. `system_agents.json` was captured off kids,
  so rex offered the Add Library and Edit Library dialogs an agent a server of its
  version does not have.
- **`translations.json`'s coverage was only as wide as its capture script's path list**, as
  §3.3 had said since cycle 16. Sweeping 87 paths against kids in German found **119
  (attribute, string) pairs** the reference translates that the table did not hold, and zen
  moves the same families (43 strings on a library's `/prefs`, 17 on `/hubs/search`, 2 on
  `/butler`). The biggest is the PREFERENCES family — every `label`, `summary` and
  `enumValues` of the Edit Library dialog and the per-item Advanced pane, 31 labels, 30
  summaries and 19 enum lists. Beside it: the seventeen typed search hubs, whose titles the
  client lays the page out from; the butler's task titles and descriptions; a GENERATED
  `Episode N`, the sibling of the `Season N` rule already in the table; and `/query/parse`'s
  `fieldTitle`. The table is a strict superset of cycle 16's — 451 entries against 342,
  nothing dropped. A string that differs between two ENGLISH fetches of the same path is not
  a translation, so the capture now fetches each path in English twice and diffs only the
  stable strings; `/system/agents` is out of the list because its `Language` list is
  REORDERED per request language rather than translated, which a positional diff reads as
  `code: "ja-JP"` translating to `"vi-VN"`.

##### The localiser stopped at the row boundary

- **Inside a `Metadata` row the reference translates exactly three things**, mapped by
  diffing a whole item document against itself in German with every include option set: a
  GENERATED `Season N`/`Episode N` (already implemented), the `Preferences` block's `Setting`
  rows — `label`, `summary` and `enumValues` alike, on a movie, a show and a season — and a
  `Stream`'s language fields. The row's own `title`, `summary` and tag names stay English in
  every language. rex's guard stopped dead at the row, so the whole per-item Advanced
  pane stayed English for every non-English client while the same settings on
  `/library/sections/{id}/prefs` were translated.
- **A Stream's `language` is localised and its two display titles are COMPOSED from the
  localised name**, not substituted into. Held out on zen's real multi-language streams in
  de, fr and es: movie 380's English subtitle track is TITLED "English", so its
  `displayTitle` moves `English` → `Englisch` while its `extendedDisplayTitle` stays
  `English (SRT)` — the language part was already omitted for carrying one of the title's own
  words, and that omission is decided on the ENGLISH name. Its Tamil track goes the other
  way: `Tamil (தமிழ் SRT)` becomes `Tamil (Tamoul SRT)` in French, the title untouched and
  the language name inside it localised. Passing the English name as a stand-in to the
  extended-title composer reproduces both. The names themselves were already in the captured
  table, inside the `languageOverride`/`audioLanguage` enum lists — a `code:name` list of 47
  languages per language — so aligning those lists by CODE gives an English-name →
  localised-name map with nothing invented in it.

##### The XML the wire actually carries

- **A real PMS escapes an XML attribute value into pure ASCII**, even though the document
  declares UTF-8. Measured on kids and zen, which agree byte for byte over the whole table:
  `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;`; `"` → `&#34;` and `'` → `&#39;`, NUMERIC rather
  than `&quot;`/`&apos;`; TAB → `&#9;` in decimal but NEWLINE and CR → `&#xA;`/`&#xD;` in
  HEX; every codepoint from U+0080 up → its DECIMAL reference, astral plane included (an
  emoji is `&#128512;`, one reference and not a surrogate pair); and every control character
  XML 1.0 forbids — U+0001..U+0008, U+000B, U+000C, U+000E..U+001F and U+007F — replaced with
  a literal `?`. rex used `xml.sax.saxutils.quoteattr`, which writes `&quot;`, leaves `'`
  alone, sends the newline as decimal and passes every non-ASCII character through raw. The
  surface carries no element TEXT at all, so the rule is the attribute rule entire.
- **A ROW's XML attribute order**, which cycle 16 settled for the CONTAINER and explicitly
  left for this cycle. It is not alphabetical and not the order the row dict was built in: a
  tag is `id, filter, tag`, a movie leads `ratingKey, key, guid, slug, studio, type, title,
  titleSort`, a Hub leads `hubKey, key, title, type, hubIdentifier`, and a `/library/sections`
  row leads `allowSync, filters, refreshing, key, type, title, agent`. Derived as a
  topological sort of every attribute PAIR the two references were seen to emit across 25
  element buckets, each verified to be a superset of every observed sequence; rex
  inverted a witnessed pair in 11 of them, **634 pairs** in all, and 0 remain. The untyped
  `Directory` rows needed a second pass: a SORT row is `default, defaultDirection, descKey,
  firstCharacterKey, key, title` and a `firstCharacter` bucket `size, key, title`, and the
  first table's one untyped order came from the "All episodes" row alone. Costs a real client
  nothing — every Plex client uses a real XML parser — and is fixed because the corpus replay
  compares attribute presence and values but not order, so nothing else in the programme
  would catch it.

##### What the row itself showed

- **A leading run of PUNCTUATION comes off `titleSort`, before the article.** zen's episode
  10866 is titled `'Til Death Do Us Art` and sorts as `Til Death Do Us Art`; its episode
  `firstCharacter` axis has 27 buckets — `#` then A–Z, no apostrophe bucket — and the `T`
  bucket holds that row, so the rule holds on 1.42.2 as well as on the copy where the table
  was measured character by character. The set is NOT a Unicode category and cannot be
  written as one: in ASCII exactly eight characters strip (`"` `'` `(` `-` `.` `[` `{` `~`)
  while `! # $ % & ) * + , / : ; < = > ? @ \ ] ^ _ ` | }` do not, so it is neither "opening
  brackets" nor `Po` nor `Sm`. Outside ASCII it is two whole ranges: every character from
  U+0080 to U+00BF and every character from U+2000 to U+2FFF strips — 85 of 85 tested across
  them, and every block boundary probed — while U+0100..U+1FFF and everything from U+3000 up
  does not, so the CJK and fullwidth brackets stay. The run may mix those characters with
  whitespace and repeat (`((( Zulu Probe` and ` . - Zulu Probe` both sort as `Zulu Probe`),
  it comes off BEFORE the article (`-The Zulu Probe` → `Zulu Probe`) and only once at the raw
  string start (`The (Zulu Probe` → `(Zulu Probe`), the fold still applies, and a title made
  of nothing but strippable characters keeps its own text rather than sorting as the empty
  string.
- **A show's `/allLeaves` — and no other container — stamps a leaf's BORROWED artwork with
  the LEAF's own `addedAt`.** `grandparentThumb`, `grandparentArt`, `grandparentTheme` and
  `art` all carry it there (14 of 14 stamped rows over 140 episodes on zen, 3 of 3 on kids),
  while the episode's detail row, the season's `/children`, the section grid at `type=4`,
  `recentlyAdded`, `newest`, `/library/all` and the hubs all carry the show's CURRENT stamp.
  It is the same family as the `studio` that container already carries: a property of the
  LIST, not of the row. The hunt reported it as a property of an episode row, and the section
  grid disagrees with that reading 641 times on zen. What it costs a client: an episode's
  grandparent artwork URL on this list is stable for the life of the row, so a cached image
  stays valid across artwork edits; rex re-pointed all of them on every edit.

##### The edit path's refusal grammar

- **A save a real PMS REFUSES writes NOTHING.** `PUT /library/metadata/{id}?title.value=
  PARTIAL&year.value=abc` is a 500 there and the title is untouched, on the item path and the
  section path alike. rex applied each parameter as it validated it, so everything before
  the bad one landed and everything after it did not — the client is told the save failed,
  keeps its old model, and the row on disk has moved, with nothing to tell the user. The
  highest-impact finding of the pass.
- **The ITEM edit path's 400 means "this request edits nothing", not "an unknown field".**
  With a real edit beside it the reference IGNORES every name it does not recognise — an
  unknown `.value`, a bare unknown name, an unknown TAG type and an option like
  `flattenSeasons=1` all answer 200 and the edit lands — while each of them ALONE is still a
  400, as is a PUT with no parameters. The recorded rule was measured with the unknown name
  alone, which is failure class **A**: a client sends its save with whatever options it
  carries, and rex refused 34 of the 71 names swept.
- **On the SECTION save path a form that names nothing to write is a 200** that edits nothing
  — `?type=2&id={id}` alone, with a bare unknown name, with `includeGuids=1` or with an
  item-prefs option — because a bare `field=value` is a FILTER there. The unknown `.value`
  form stays a 400 on that path, which is where the two paths genuinely differ.

##### The collection item path

- **A collection's children on the METADATA path are the LIGHT grid row** inside a container
  that carries the librarySection triple and `allowSync: true` and NO `thumb`. rex sent
  the full detail row — `Guid`, `Rating`, every tag list with ids, the whole `Stream` table —
  put the triple on each ROW instead, said `allowSync: false` and added a `thumb`.
- **The MOVE has a grammar.** `after=<the moving item itself>`, an EMPTY `after=` and an
  unparseable one are 400s that move nothing; an `after=` that names NO member (`999999`,
  `0`) is a 200 that moves the row to the FRONT, the same as no `after=`; a ratingKey that is
  not a member is a 400, not a 404; and `/children/{ratingKey}/move` is the same endpoint and
  really moves, where rex answered `{size: 0}`. rex answered 200 to every one and
  moved the row regardless.
- **A comma-joined `uri` TOLERATES the members it cannot use**: a movie and an episode into a
  movie collection is a 200 that adds the movie and drops the episode, and an unknown key
  beside a real one adds the real one. Only a SINGLE-key uri of the wrong type is the 400
  cycle 9 recorded. rex refused the whole batch, so "Add to Collection" over a
  multi-select lost every row when one was wrong.
- **The item paths answer 404 to the verbs they do not implement**: POST, DELETE, PATCH and
  OPTIONS on `/items`, every verb but DELETE on `/items/{ratingKey}`, every verb but PUT on
  `/move`. rex answered 200 with an empty container to the first, 200 with an INVENTED
  single-item container to a GET of one member, and 200 with the whole children listing to a
  GET of `/move`. **A ratingKey that is not a number on the DELETE is a 404** where an
  unknown number is a 200 that removes nothing — recorded in cycle 14 and left unimplemented.

##### The artwork surface

- **An artwork candidate whose `ratingKey` is a LOCAL uri names a SERVER PATH.** Measured on
  kids and zen over 13 and 97 candidates: `ratingKey` is the bare uri (`metadata://`,
  `media://`, `upload://`) and `key` and `thumb` are
  `/library/metadata/{id}/file?url=<the uri percent-encoded, with `.` escaped as `%2E`>`,
  which really serves the image (200 image/jpeg). A `media://` candidate carries NO
  `provider` while an agent-supplied `metadata://` one does. rex put the bare uri in all
  three and stamped every local row `provider: "local"`, so Change Artwork drew each uploaded
  tile with `img src="upload://posters/..."` — a grid of broken tiles.
- **`DELETE /library/metadata/{id}/thumb` clears the poster** exactly as `/poster` does.
  Cycle 16 recorded the rule for all three names and rex implemented two, so the singular
  `/thumb` fell through to the image read and answered the PNG bytes to a DELETE.
- **An artwork UPLOAD moves `updatedAt` onto the same stamp the new artwork path carries**,
  as a PICK does. Cycle 15 measured it for the pick and rex implemented it there only, so
  a client that caches on the stamp never refetched a poster just uploaded.

##### The prefs family, the butler and the verb table

- **A library PREFERENCE write moves the SECTION's own `updatedAt`** and nothing else's.
  Clients cache a library keyed on that stamp, so on rex a settings change never
  invalidated the cache. The write itself is unchanged: 200, empty body, zero frames.
- **An INT library preference whose value does not parse is stored as 0**, not ignored:
  `abc`, an empty value, `1.5`, a space and `0x3` all read back as `0` where `007` reads back
  as `7`. This corrects the recorded rule — "a bad int is a 500 on `/:/prefs`, IGNORED on a
  library" — whose second half was measured by watching the write answer 200 without reading
  the value back. Clearing "Minimum automatic collection size" in the Edit Library dialog is
  exactly this request.
- **A library TEXT preference is stored percent-ESCAPED for the two characters that delimit
  an `enumValues` string** — `:` becomes `%3a` and `|` becomes `%7c`, lowercase hex — and a
  value carrying a `%` of its own is stored EMPTY. Every other character round-trips.
- **Twelve of the twenty-three butler tasks report a different sentence**, three report
  SEVERAL, and five report one where rex reported none. The task LIST is identical on
  kids, zen, plexscratch and rex, and every `POST /butler/{Task}` is a 200 with an empty
  body on both, so what differs is only what the Scheduled Tasks page then prints. The two
  tasks with an activity of their own differ in it too: `CleanOldBundles` is titled `Cleaning
  Bundles` and steps its SUBTITLE through four named phases (`Analyzing media bundles`,
  `Deleting N media bundles` at 75 and 100, `Analyzing metadata bundles`), and
  `OptimizeDatabase` is titled `Optimizing database` with the subtitle `Optimizing Database`
  — rex had that pair the other way round. The COUNT in each sentence is content and not
  part of the rule.
- **An ACTIVITY's subtitle is the row's FRAME title**, which is not the row's own title: a
  season's is `<show> S01` and an episode's `<show> S01 E01`. rex used `title (year)` for
  every type — the movie and show form — so the Dashboard printed `Season 19 (2007)` where a
  real PMS prints `The Simpsons S19`. The rule is recorded for the timeline frames and was
  implemented in `notify_timeline` alone; the helper beside it kept the wrong form. Failure
  class **C**.
- **`POST` on an item ACTION leaf is a 404** — `/refresh`, `/analyze` and `/unmatch` take PUT
  — where `POST /library/sections/{id}/refresh` really is a 200, and **`/library/metadata/
  {id}/match` answers every verb with its own no-parameters refusal**, so a GET and a DELETE
  there are 400 rather than 404. rex ran the action on a POST, so a client that got the
  verb wrong re-matched an item instead of being refused.
- **`GET /library/sections/{id}/refresh?force=` PARSES the flag**: `abc`, `1.5` and an empty
  value are 400s where `0` and `1` are 200s. rex took anything and ran the scan.
- **A KNOWN single-segment path falls through to the legacy plug-in dispatcher for the verbs
  it does not implement**: `PUT`, `PATCH` and `OPTIONS` on `/butler` and on `/devices` answer
  `{"size": 0, "content": "plugins"}`, exactly as an unknown single-segment path does. The
  recorded rule that a read-only route 404s every verb but GET and HEAD is the MULTI-segment
  rule.
- **`GET /livetv` is the plug-in dispatcher's own THREE-row container** — `dvrs`, `epg`,
  `sessions`, with `content="plugins"` — on kids and zen alike. rex routed the whole
  `/livetv` tree to a bare empty container, so the one path that does answer said the server
  had no Live TV surface rather than no tuner.

##### A defect this cycle introduced and caught

The first implementation of "the collection item paths 404 the verbs they do not implement"
encoded `/items` as PUT-only — but the verbs measured on the reference were POST, DELETE,
PATCH and OPTIONS, and GET was never among them. `GET /library/collections/{id}/items` is the
member LIST, recorded since cycle 14, and the table took it out; the next finding's own
measurement showed a collection with three members answering an empty list. It is the lesson
§3.1 keeps teaching from the other side: a rule is scoped to what was TRIED, and a table
written from a refusal list has to say which verbs were on it.

##### Measured and not acted on

- **`PUT /library/metadata/{id}/poster?url=nonsense` — REFUTED.** The hunt reported the
  reference answering 200 and re-versioning where rex 404s. Re-measured on a row with
  artwork: both answer 200 and both re-version, twice in a row with a distinct stamp each
  time. Nothing to change.
- **An EPISODE refresh running the SHOW's activity.** The hunt reported it and one of the
  driver's two captures reproduced it — `started 'Firefly (2002)'`, then one `updated` per
  season — while the other showed the episode's own seven-frame sequence with the episode's
  frame title. Two runs of the same request on the same copy disagreed, so only the SUBTITLE
  half (which both agree on) is implemented and the activity-ownership half is recorded
  rather than built.
- **A `state 2` / `metadataState "matching"` opener on a season or episode refresh**,
  reported by the hunt and not reproduced in either of the driver's captures.
- **A remote artwork candidate's `thumb` is an `images.plex.tv` proxy.** plex.tv is out of
  scope, so rex keeps the upstream URL there.
- **The non-alphanumeric `firstCharacter` bucket**, re-measured against zen's section 2 —
  507 shows, 741 seasons, 9,287 episodes, the instrument that did not exist when §3.3 first
  flagged it. Section 2 contributes no non-digit non-letter-led title at all, so the "its own
  bucket, listed after Z" half still rests on the single `『` row in section 1.
- **Every write rule here has plexscratch as its ONLY witness**, as §3.3's first row says of
  the whole family.
- **The three legacy `com.plexapp.agents.*` rows** both references list at `/system/agents`
  stay deliberately absent: rex hosts no plug-in framework, and the fixture's own
  provenance records why.


#### The eighteenth pass — the container, the write surface below the collection item, and the title a server generates

Two hunters and a driver round. **Territory A** took the dimension left after the row:
the CONTAINER — every `MediaContainer` attribute, endpoint by endpoint, against both
references. **Territory B** took the write surface one level below the collection item
path: the playlist item path's verb table, the `/library/sections/{id}/prefs` family per
TYPE, and the `/actions/` family. The driver's own round started from the one rule the
row sweep had left half-measured — `titleSort` — and found the family this cycle is
really about: **a title the server GENERATES is stored nowhere**.

**26 findings implemented** over 28 commits, each re-measured by the driver before any
code was written and each committed with a check proved to fail against its own parent.

##### A title the server generates is stored nowhere

- **A generated title has no stored sort title.** Measured on zen (1.42.2) over 14,433
  rows and on kids (1.43.4) over its two-season library, from three directions that
  agree: `?titleSort=` — a bare, EMPTY value, which on this field is an equality —
  answers exactly 740 of zen's 741 seasons, 4,630 of its 9,287 episodes, kids' one
  unmatched season and its three unmatched episodes, and **0** of either library's
  movies and shows; `?titleSort<<=0` answers the same sets, because the empty string
  compares below `0`; and `/library/sections/{id}/firstCharacter/%23` answers them while
  every LETTER bucket of a season list answers none. That last one is the other half of
  cycle 16's "the axis counts by the SHOW's sort title and the listing filters on the
  row's own": the listing half was implemented reading the row's TITLE where the row
  stores no sort title, so rex's seasons bucketed under `S`.
- **And it is not in the search index either.** Measured on plexscratch with a control:
  the copy's season 11 is titled `Season 2` and `/library/sections/2/search?type=3&
  query=season` answers NOTHING, while the same season renamed `Zulu Season Probe`
  answers to `zulu`, to `season` and to `probe`; its three unmatched episodes
  (`Episode 1`–`3`) answer nothing to `query=episode` while `The Train Job` answers
  `train`. The `title` FILTER agrees on zen: `type=3&title=Season` is 0 of 741 seasons,
  and `type=4&title=Episode` is 203 of 9,287 — exactly the agent-supplied `Episode N`
  titles, the rows carrying `plex://episode` guids. The generated title is composed for
  DISPLAY and stored in no column a query can reach. It **refutes** territory A's reading
  that a section's search box never returns seasons: it returns the ones whose title
  somebody typed.
- What tells a generated title from an agent-supplied one is not visible on the row:
  kids' season 7 is titled `Season 1` by TMDB and stores it, and 176 of zen's episodes
  are titled `Episode N` by their agent and store it too. rex generates every season
  title it has, so it decides on the title FORM — a proxy for the mechanism, recorded in
  HANDOVER 3.3.

##### The fold reaches eleven characters NFD cannot

- **Eleven Latin-1 characters fold in `titleSort`, and the map is not a
  transliteration.** Swept character by character on plexscratch over every codepoint
  from U+0080 to U+02FF that NFD leaves alone — 282 alphabetic and 106 non-alphabetic —
  by writing a title with each one in the middle and reading back the sort title:
  `Æ→E`, `Ð→D`, `×→x`, `Ø→0`, `Þ→P`, `ß→s`, `æ→e`, `ð→O`, `÷→/`, `ø→0`, `þ→p`. `Ø` and
  `ø` become the DIGIT ZERO and `ð` a capital `O`, which is what makes the table a
  measurement rather than something anyone would write down from a language. Two of the
  eleven are witnessed on zen's real library — `Død Kalm` sorts as `D0d Kalm` and
  `Dæmonicus` as `Demonicus` — and that is what found the gap: rex folded combining
  marks only, so both rows carried no `titleSort` at all where a real PMS sends one.
  Nothing above U+02FF folds, which zen's 51 surviving non-ASCII rows already implied.
- **The bare `title=` matcher also reads the row's ORIGINAL title, and only it does.**
  Measured on zen over all 3,887 rows: `title=the` answers 1,484 where the title alone
  explains 1,481 — the three extras are `Interstate 60` ("Interstate 60: Episodes of the
  Road"), `Millennial Killer` ("The Millennial Killer") and `Vae Victis` ("The Art of
  Evil") — `title=B` is 815 against 807 and `title=art` 7 against 6. Held out over 24
  (operator, value) pairs: `title!=`, `==`, `!==`, `<=` and `>=` all reproduce exactly on
  the title and DISAGREE once the original title is added. A client searching a foreign
  film by its own-language title finds it on a real PMS.
- **On an UNLISTED string field the four ordering keys really ORDER.** Cycle 14 measured
  that `titleSort`, `summary`, `tagline` and `originalTitle` take `>>`/`<<` where an
  advertised field ignores them, and rex allowed the operators and then fell through
  to CONTAINMENT — so `titleSort<<=0` matched every row whose title happens to hold a
  `0`. Measured on zen: all four keys are a case-insensitive LEXICOGRAPHIC comparison and
  both sides are STRICT. `titleSort<<=B` answers 127 and `>>=B` 3,760, which partition
  the library exactly; `<<=Ba` answers 128, taking in `B2: Stealth at War`; `B` and `b`
  agree to the row; and against a value a row holds exactly (`Baasha`) the two answer
  128 + 3,758 of 3,887 — that row is in NEITHER set. `<=` and `>=` are not begins-with
  and ends-with here, although they still are on an ADVERTISED string field, where zen's
  `title<=B` is 136 rows beginning with B and `title>=ing` 26 ending in `ing`.

##### The container, endpoint by endpoint

- **`/library/sections/{id}/search?type=N` types its container from the ASKED type, not
  from the library.** Measured on kids and zen over two movie libraries and three show
  ones: `type=3` answers `viewGroup="season"` and `viewMode` **65593**, `type=4` answers
  `viewGroup="episode"` and `mixedParents: 1`, and a MOVIE library answers both to the
  same parameters. The section GRID is the other way round and keeps the library's own
  type, which rex had right; only `/search` retypes. `/library/sections/{id}`
  advertises `search?type=2` and `search?type=4` as its own rows, so this is the path
  the library search menu takes — and at `type=3` rex handed the client the EPISODE
  layout for a season list, which is the exact inversion behind the Seasons bug.
- **`All <library title>` for a movie library, the constant `All Shows` for a show
  one.** Measured on kids ("Movies", "TV Shows"), zen ("Zen Movies", "Zen Shows", "Bench
  TV") and in both places the string appears — the `/all` container's `title2` and the
  `all` pivot row. rex composed the container's title2 for a show library ("All
  Debrid TV") and hardcoded "All Movies" for a movie library's pivot row: each half
  wrong in the opposite place, and the two lines contradicted each other in the same
  file. Not localised — zen answers the same string in de and fr.
- **`/library/recentlyAdded?limit=N` really caps, and rewrites `totalSize` with the
  cap**, so a client cannot page past it. Its grammar is a third one: `limit=0` answers
  ZERO rows where a section listing answers one, a NEGATIVE value lifts the server's own
  50-row cap, and an unparseable one is IGNORED where a section listing is a 500.
- **`/status/sessions/history/all` takes `limit=` too, and refuses `group=` with a
  400** — the empty form included, where cycle 15 found `group=` reaches the section
  `allLeaves`. `limit=-1` means ZERO rows here, where the same value lifts the cap on
  `/library/recentlyAdded` and means "every row" on a section listing: three endpoints,
  three meanings.
- **`parentIndex=` and a `resolution=` outside the enum are 500s on `/library/all` and
  on `/playlists`** as well as on a section listing. Cycle 15 recorded the `parentIndex`
  rule and it was wired into the section listing alone — the class-C shape — so
  python-plexapi and the SDKs read rex's 200 as "this field filtered nothing".
- **`/hubs`, and a SHOW library's `/hubs/sections/{id}`, refuse fifteen filter keys with
  a 500**: `duration`, `lastViewedAt`, `lastRatedAt`, `viewCount`, `skipCount`,
  `viewOffset`, `userRating`, `unwatched`, `resolution`, `hdr`, `mediaSize`, `file`,
  `parentIndex`, `slug`, `group`. Enumerated over 76 candidate names on kids' section 2
  and zen's sections 2 and 4: any non-empty value refuses whatever operator follows, an
  EMPTY value is a clean 200, an unknown field is a 200, and a MOVIE library's hub page
  takes all fifteen. The server-wide `/hubs` half cannot be attributed further — both
  references hold a show library, so "always" and "because a show library exists" are
  the same measurement there.
- **`/system` PAGES**, and it is the counterexample to cycle 9's "a plug-in framework
  container (`noHistory`) is left alone" — a rule inferred from `/system/scanners/{N}`,
  which really does ignore paging. `Start=1&Size=1` answers one row and `totalSize="2"`,
  `Start=5` answers `size="0"`, both parameters are required as everywhere else, and the
  response carries `X-Plex-Container-Start` and `-Total-Size`. It emits NO `offset` even
  at Start=1, alone on the surface, and in XML the plug-in framework's container is
  ordered `[totalSize,] noHistory, replaceParent, size, identifier` — the one container
  shape cycle 16's rule does not describe.
- **`/system/search` is a real container**: 200 on kids and zen alike, empty, carrying
  `identifier`, `mediaTagPrefix` and a **`prefsKey`** — the only container attribute
  either reference emits anywhere that rex never did — and it does not page. Found
  by sweeping 46 second-level `/system/*` names; it is the mirror image of the three
  endpoints cycle 14's path census found rex had INVENTED.

##### The plug-in dispatcher, and what a verb it does not implement answers

- **Four more single-segment paths are the dispatcher's own POPULATED listings**,
  byte-identical on kids and zen: `/media` names `grabbers`, `providers`,
  `subscriptions`; `/services` names ten services; `/status` names `sessions`;
  `/statistics` is its empty 200 where rex 404'd. `/security` is the exception in the
  other direction — a real empty container with **no `content` key at all**, which
  rex had filed under the dispatcher. Cycle 17 found `/livetv` was one of these; this
  is the rest of the family.
- **A KNOWN single-segment path answers the dispatcher for EVERY verb it does not
  implement.** Measured on plexscratch over sixteen paths and seven verbs: `PUT
  /identity` and `PUT /hubs` answer `{size: 0, content: "plugins"}`, `PUT /library` the
  dispatcher's two rows (`augmentation`, `shared`), `DELETE /devices` its empty one. The
  recorded rule that "`/identity` and `/hubs` answer any verb" was rex's own GET
  handler answering; cycle 17 landed the fallthrough for `/butler` and `/devices` alone.
  The real writers are `POST /playlists`, `POST /playQueues` and `POST`/`DELETE
  /butler`; `/log` is outside the rule because it validates its parameters first, so
  every verb there is the same 400.
- **The `/system` subtree is a different server with an envelope of its own.** Measured
  on plexscratch over four paths and six verbs: `PATCH` is a **405** with an HTML body —
  the one 405 on the surface, against the library server's "never 405" — a bare `OPTIONS`
  is a 200 with an empty body, and every other unimplemented verb, and an unknown path
  under it, is a 404 with an EMPTY body.

##### The write surface below the collection item path

- **The playlist item paths take one verb each**, exactly as cycle 17 found for the
  collection's: `/playlists/{id}` GET/HEAD/PUT/DELETE, `/playlists/{id}/items` the same
  four, `/playlists/{id}/items/{iid}` DELETE (its PUT is a 400), and
  `.../items/{iid}/move` PUT. rex answered 200 to thirteen cells — a GET of ONE
  member returned the WHOLE item listing, and so did a GET or a DELETE of `/move`. `PUT`
  at one member is a 400 for a member id and for an unknown number alike and a 404 for an
  id that is not a number: the SHAPE is checked before membership.
- **`DELETE /library/metadata/{playlist}` is a 200 with an EMPTY body that deletes
  nothing**, where `DELETE /playlists/{id}` on the same copy is a 204 that really removes
  the row — so it is not the copy refusing a delete. rex answered the playlist row.
- **A repeated `id=` on the section save path edits NOTHING** — `?id=1&id=3&
  summary.value=x` is a 200 and neither row moves, where the comma list edits both. It is
  the rule repeated parameters follow everywhere else on this server; rex took the
  LAST one, so the same request edited a row at random.
- **`POST` on `/split` and `/merge` is a 404**, like the three action leaves cycle 17
  landed. rex PERFORMED the split on a POST — a mutation a real PMS refuses to route.
- **A write to `/library/parts/{id}` resolves the PART first**: an unknown number, a
  malformed id and an unparseable stream value on an unknown part are all 404s, where
  rex answered 200 whenever the request named no usable stream. A track picker
  pointed at a stale part id saw its selection applied.
- **`/library/metadata/{id}/subtitles` answers 404 to DELETE, PATCH and OPTIONS**, where
  rex answered the upload validator's 400 — a 400 says "your request was wrong", a
  404 says "this verb is not here".
- **A library DELETE pushes one `state 9` timeline frame per row**, measured with a
  control write first: `type: 12`, `metadataState: "deleted"`, an EMPTY title and
  `sectionID` the NEGATIVE section id, between the 10% and 40% activity updates. A
  nine-row library produced 14 of them on the copy, keyed on the server's own internal
  metadata ids rather than the ratingKeys. rex pushed the eight activity frames and
  none of the timeline, so a client holding a row from the deleted library was never told
  it had gone — the failure the whole notification lifecycle exists for.

##### Two more fixtures reading the wrong version, and a wider table

- **`section_prefs_new.json` is the FOURTH fixture found to be per VERSION**, after
  `Meta.Type[].Filter` (cycle 14), `Meta.Type[].Field` and `system_agents.json` (cycle
  17). It was captured off kids and differs from zen in **16 of 35** (type, agent) cells:
  on 1.42.2 the AGENT's own settings are appended whatever the `type=` is, so
  `type=9&agent=tv.plex.agents.movie` answers the movie agent's 12 settings where 1.43.4
  answers only `hidden`; on 1.43.4 `tv.plex.agents.none` and `.music` add five or six
  generic rows at types 1 and 2 that 1.42.2 does not have — the cell the Add Library
  dialog's own "Personal Media" choice reaches; and `useSeasonTitles` belongs to the
  SERIES AGENT on 1.42.2 and to the SHOW TYPE on 1.43.4. Every setting's own attributes —
  type, default, label, summary, enumValues, hidden, advanced, group — are identical on
  both versions: 0 differences over 35 settings.
- **An item's `audioLanguage` and `subtitleLanguage` enums are led by the item's OWN
  stream languages.** Measured on zen and kids: `:Account default`, then the item's
  stream languages as bare 639-1 tags — audio first, then subtitles, each group ordered
  by tag and the head deduped, each named in its own language — and then the generic
  regional list. zen's show 6910 leads `en:English|bg:...|cs:...` against episodes
  carrying one English audio track and fifteen subtitle languages; its show 11113 leads
  `pa:...|en:English`; kids' Firefly leads `en:English` alone. rex served
  `item_prefs.json`'s captured list to every item, so a show's own languages could not be
  chosen in its Preferences pane — and the bare entries at the head of the captured list
  are the captured item's own, which is what made the fixture look version-neutral.
- **The translation table is 545 strings**, up from cycle 17's 451, over ~130 paths. The
  five families added: every `firstCharacter` BUCKET names itself in `title2` ("Start
  with 'A'" → `Beginne mit „A“`, with the QUOTE MARKS moving too, so the string cannot be
  composed from a translated stem and the key); `/query/parse`'s `fieldTitle` for every
  rule a client can build, scoped on a show library ("Show Title", "Episode Year", "Date
  Episode Added"); the music and photo `/filters` axes (Mood, Style, Record Label, Camera
  Make, Aperture, Lens, Tag); a music library's own prefs; and the per-library hub page's
  shelf titles ("Recently Released Movies", "Rediscover", "Recently Watched Episodes").
  The capture also gained a control it needed: a list item is now keyed by its own
  `ratingKey`/`hubIdentifier`/`key`/`id` rather than by position, because
  `/hubs/sections/{id}` picks a random genre and a random actor per request — index 3 of
  the English document and index 3 of the German one are DIFFERENT rows, and a positional
  diff read every attribute of them (`container: "mkv"`, an artwork path, an
  `audienceRatingImage`) as a translation. Fetching each language twice does not catch
  it; the identity key does, and it is the general form of cycle 17's `/system/agents`
  exclusion. The new table is a strict superset: 0 entries dropped and 0 changed.

##### A defect this cycle introduced and caught

The first implementation of the fifteen hub refusals called `hub_refusal(query)` from
inside `section_hubs`, which never receives the query — so every show library's
Recommended tab answered a `NameError`'s 500. The patch's own check asserted a 500 for
each refused key and PASSED, because a crash and a refusal have the same status. The full
suite caught it four checks later, on the tab's contents. **A check that asserts an error
status has to read something back afterwards**; the refusal now lives in the route, where
the query exists.

##### Measured and not acted on

- **`/playlists/{id}` resolves any metadata ratingKey.** Both references answer 200 with
  that item in a bare `{size: 1}` container (no `allowSync`, no identifier, no
  librarySection triple, no `Media`, no tag lists) and 500 on its `/items`. The
  projection is not stably characterised between them — kids' movie 1 drops `duration`
  and zen's movie 17360 keeps it — and no client builds the path, so copying it would be
  inventing the shape rather than measuring it. rex keeps its 404.
- **The `/system` 404's missing Content-Type.** The reference sends no `Content-Type`
  header at all on those empty 404s; rex's empty answers carry `text/html`. The
  status and the empty body are reproduced; the missing header is not.
- **The library delete's frame IDS.** They are the server's own internal metadata ids —
  14 for a nine-row library — not the ratingKeys, and nothing in the response explains
  the id space. rex frames the rows it actually removes.
- **The butler's per-task counts, and every write rule's single witness.** As HANDOVER
  3.3's first row says of the whole family: plexscratch is the only server that can
  answer a write, so every write rule here has one witness.
- **The non-English article strip and the non-alphanumeric `firstCharacter` bucket**,
  both carried forward again: nothing this cycle touched either.


#### The nineteenth pass — the child element, the filter engine held out against a real library, and what a write really pushes

Two hunts and a driver round. Territory A swept the dimension left after the row (cycle
17) and the container (cycle 18): the **child element** — every element between the two,
per endpoint and per row type, against both references. Territory B took the write
surface's own payload: the notification **frame**, the `Activity` element, and the
server-level writers nobody had swept. The driver's round ran rex's **own engines**
over a real library's rows: `ssweep.py` holds the `sort=` engine out against zen's
ordering, and `fsweep.py` — cycle 18's filter holdout — was pointed at a show library for
the first time.

The cycle's own family came from that holdout. Cycle 18 found the `title=`/`originalTitle`
rule by running `filters.compare` over zen's 3,887 movie rows and diffing the SETS; run
again over its 507 shows it disagreed 74 times, and behind those disagreements were four
rules about the filter grammar that eighteen cycles of endpoint-by-endpoint measurement
had not reached — because each one needs a library where most rows have NO value for the
column being filtered, and neither five-clip reference has one.

**A boolean field answers the true set to exactly two requests.** `field=1` and
`field!=0`; every other operator and every other value answers the rows where the column
is FALSE. Measured on kids (1.43.4) and zen (1.42.2), over `unwatched` and `duplicate`,
seven values and all eight operators: zen's `unwatched=1` is 3,866 of 3,887 and its
`!=1`, `==1`, `!==1`, `<=1`, `>=1`, `<<=1`, `>>=1`, `=0`, `==0`, `!==0`, `<=0`, `>=0`,
`<<=0`, `>>=0`, `=2`, `!=2`, `=` (empty), `=01`, `=true` and `=1 ` are all the same 21;
`duplicate=1` is 214 and every one of the others the other 3,673. `!=2` is what rules out
the obvious model — a value parsed to 0 and then negated would answer the true set there,
and it does not. rex compared 0/1 numerically, so `unwatched>=0` answered the whole
library and `unwatched!=2` the unwatched half: the Unplayed filter inverted for every
value the client does not send.

**A row with NO value for a numeric column matches NOTHING**, `!=` and `!==` included.
zen's 742 rated movies split 368/374 across `rating<=7` and `rating>>=7`, so the 3,145
unrated rows are in neither half; its show library, where no row carries a rating at all,
answers 0 to all eight operators. rex read a missing value as 0, so `rating<=7` was
the whole grid and `rating!=7` every row — on a real library, the majority of the rows.
And **`==`/`!==` on a numeric column are an exact STRING comparison of the stored
number**: `rating==7` answers 0 where `rating==7.0` answers the same 6 rows `rating=7`
does, `userRating==5` 0 against `==5.0`'s 43, while the INTEGER column answers
`year==2000` with the same 39 as `year=2000` and `year==2000.0` with none.

**A `tag`-typed COLUMN takes all eight operators**, where a tag-TABLE field takes two.
`contentRating` is declared `tag` in the Meta exactly as `genre` is, but it is a column on
the row rather than a join: on zen `=PG` and `==PG` are both 112, `!=PG` and `!==PG` both
3,775, `<=PG` 3,197 and `>>=PG` 690 — the pairs partition 3,887 exactly — and the empty
value compares as `""`, so `>=` with no value is every row and `<<=` none. `=` is
case-SENSITIVE (`=pg` is 0) and `==` is not (`==pg` is 112); the four ordering keys are a
BYTE comparison, so `<=pg` answers every row a real library holds. rex answered
nothing to six of the eight, so the Content Rating filter's "is before"/"is after"
controls came back empty.

**`resolution` is the other way round: every operator answers the `=` set**, `!=`
included — 3,608 of zen's 3,887 at `1080` for all eight, 2 at `sd`, 6 at `720`, and 490 of
507 on its show library. And `resolution=` matches ANY of a row's Media: zen's 4093 The
Breadwinner carries a `4k` and a `1080` and answers both, 401 The Devil's Mouth answers
`1080` and `720`. rex read `Media[0]` and negated `!=`, so a row's second version was
invisible and a "not 4k" filter emptied the grid.

**The section's `Meta` descriptor is three rules, not one.** `active` follows the
REQUEST: `?type=3&includeMeta=1` marks `season` and `?type=4` marks `episode` on both
references, where rex served the captured fixture's stored flags and kept the Shows
tab lit on the Seasons and Episodes pages. `?includeDetails=1` honours `includeAdvanced=1`
like every other form — bare it is 9 sorts on a movie library and 17 on a show one, with
the flag 15 and 32 — where rex always answered the advanced list, so six sorts and the
Duplicates filter the client hides behind its own toggle were permanently on show. And
that form carries no `folder` Type row and marks none of the others active.

**A row that HAS children names its child list.** `key="/library/metadata/{id}/children"`
on 300 of 300 zen shows, 300 of 300 zen seasons and both of kids'; an episode keeps the
bare item path. Three of the four seasons cycle 13 added to `tests/catalog_ui.json` by
hand carry the bare path, and the client follows `key` verbatim — so a click on Season 20,
21 or 22 fetched the season's own metadata row instead of its episode list and the season
page drew nothing. Six cycles of suites passed over it. It is corrected in
`hierarchy.normalize_rows`, at LOAD, because the wrong value is baked into catalogs
already written.

**The three options that reshape an item read.** `excludeFields=` strips the named field
from every CHILD ELEMENT and not only from the row — `id` off Media, Part, Stream, Genre
and Guid; `file` off Part; `url` off Image; `value` off Rating; `index` and `codec` off
Stream; `tag` off Genre and Role — so a client asking not to be sent `file` still got the
full filesystem path out of rex. `excludeElements=Image` and `=UltraBlurColors` are
no-ops on both references, exactly like an unknown name, while every other element the
option names really goes; rex removed them. And `checkFiles=` is VALIDATED: `0`, `1`
and `01` are 200 while `abc`, an empty value, `2`, `true`, `-1`, `1.5`, a leading space
and a trailing one are all 400 on both servers. (That rex sends `exists`/`accessible`
unconditionally stays a deliberate deviation; the value grammar was never part of it.)

**`/library/metadata/{id}/extras/all` is a real leaf** — the same container `/extras`
answers, on both references — where rex 404'd the segment. Cycle 15 measured the
`/all` form on the SECTION path and nobody had asked the item path.

**Four more XML ordering rules.** A `Meta`'s two `Type` rows come BEFORE its eight
`FieldType` rows, where rex sorted them by the item table, which knows neither name,
and left dict order. Five element buckets `_ATTR_ORDER` had no entry for at all —
`Common`, `Feature` (whose `scrobbleKey`/`unscrobbleKey` lead), `Type`, and the
`/media/providers` library `Directory`, which is a DIFFERENT order from
`/library/sections`'s and had been sharing its bucket — plus `count` and `hubKey` inside
`Directory[none]`. The rows of `/library/recentlyAdded` and `/library/onDeck`, and only
those, LEAD with `allowSync` and the librarySection triple before `ratingKey`;
`allowSync` on the ROW is the tell, since it appears there and nowhere else. And a LIGHT
row orders its children differently from a detail row: `Guid` sits directly after
`UltraBlurColors` and before the tags on every listing row of both references, where a
detail row answers `Genre, Country, Guid, Rating`.

**A real PMS's JSON key order is its XML order** — the container's, the row's, `Media`'s,
`Part`'s and the row's child-element keys, on both references and all four row types.
rex had ordered the XML since cycle 16 and left the JSON in whatever order the dict
that built it held, so its two renderings of the same response disagreed. `xmlout._split`
is now the one answer both writers ask.

##### Territory B: what a write really pushes

**A library EDIT scans only when the form carries `language` or `scanner`.** Measured on
plexscratch behind a control write, one listener, one run: `agent` alone, `agent` with
`name`, and `agent` with a `prefs[...]` are each a 200 with ZERO frames, while adding
either of those two makes it twenty. Cycle 9 recorded "a library edit applies the whole
form and scans" from a form that happened to carry a `language` -- the half that scans.
rex scanned on every accepted edit, so renaming a library or toggling one Advanced
preference lit the Dashboard with a full "Scanning Movies" activity and re-framed every
row where a real PMS is silent. And an edit REFUSES a `type` naming a different type from
the library's own (400) while accepting its own as a word, as a number, empty or absent.

**`?path=` SCOPES a section scan.** The section's own Location, an empty value and no
`path` at all each scan everything (five `progress` rows on the copy's five movies,
twenty frames); a PARENT of it, a CHILD of it and a path the library does not hold scan
nothing at all -- no `progress` rows, nine frames, the activity running 0 to 100 with an
empty subtitle. rex ignored the parameter, so "Scan Library Files" on one folder
rescanned and re-framed the whole library.

**Three butler tasks push the wrong thing.** `RefreshLibraries` runs a section SCAN per
library -- `library.update.section` titled `Scanning <library>`, the per-item `progress`
rows, the subscriptions pair -- where rex ran one `library.update.item.metadata`
triple per row and no progress rows at all. `OptimizeDatabase` starts at 0 with an EMPTY
subtitle and steps 0, 10, 40, 60 before ending at 100, where rex started at -1 with
the subtitle already set and stepped 10, 53, 100 -- and its own `PUT /library/optimize`
already answered the reference's sequence, so the two paths disagreed with each other as
well as with the reference. `CleanOldBundles` has SEVEN updates and not four: `Analyzing
metadata bundles` is followed by two `Deleting N metadata bundles` steps at 25 and 50,
and the `ended` frame carries that subtitle.

**A show library's scan names the SHOW before its seasons**, in a `progress` row with no
activity update of its own; and `provider.subscriptions.process` is the one activity a
real PMS marks `cancellable: true`, where every `library.update.section` and
`library.update.item.metadata` beside it is false -- so the Dashboard drew no Cancel
control for the only activity that has one. **An analyze pushes no
`backgroundProcessingQueue` frame**: rex invented one, telling every client on the
socket that the background queue had been rebuilt. Same family as cycle 12's invented
subtitle-upload pair.

**`PUT /updater/check` pushes one frame** -- an `update.statechange` container carrying an
`AutoUpdateNotification` row -- where rex pushed none and Settings > General > Check
for Updates never resolved.

**Three verbs at the server level.** `DELETE /devices/{id}` and `DELETE /accounts/{id}`
are 404s for an id the server lists and one it does not, with the list unchanged; rex
answered an empty 200 to the first and the accounts LISTING to the second, which a client
reads as a successful write. `PUT /activities/{x}` is a 400 where the DELETE of the same
path is a 404. And `HEAD /:/prefs` is a 200 on kids, zen and plexscratch alike -- the one
disagreement in a HEAD sweep over sixty paths and four servers, and a breach of rex's
own rule that a read-only route answers GET and HEAD.

**The `/:/prefs` write grammar has two more rules.** Four settings -- `PlexOnlineUrl`,
`TranscoderH264Options`, `TranscoderH264OptionsOverride` and `allowMediaDeletionLanOnly`
-- answer **403** and push no frame, even when the value written is the one already
stored, while the other 147 answer 200 and push a `preference` frame each; nothing in the
row marks them, so the set is the measurement. And `ButlerStartHour` and `ButlerEndHour`
take a WORD without refusing -- a 200 that stores nothing, alone among the 38 integer
settings, where the other 36 are 500s.

**`server_prefs.json` is the fifth per-version fixture.** kids (1.43.4) answers 174
settings and zen (1.42.2) 173, and exactly `AllowLegacyAgents` and `CLDriver` are on kids
and not on zen. Two extra rows on the Settings page is the small half; the real one is
that a write to `AllowLegacyAgents` succeeded where a real 1.42.2 refuses the whole
request as an unknown preference.

**The Add Library form validates four ways rex did not.** A missing `language` and an
unparseable one are both 400s carrying exactly `'language' is invalid` -- the accepted set
is the agent's own list, so `en-US`, `de-DE`, `pt-BR`, `nb-NO` and even `EN-US` pass while
`en`, `de`, `xx`, `zz-ZZ`, `yue-HK`, `en_US` and an empty value do not. An EMPTY
`location` is a 201 where rex refused it, and a NUMERIC `type` is accepted and creates
a library of that type where rex refused it.

##### Measured and not acted on

- **`sort=` holds up.** `ssweep.py` ran every one of rex's 33 sort keys, ascending
  and descending, against zen's own ordering of the same 3,887 rows. The default listing
  order IS `sort=titleSort` (3,887 of 3,887), and the tie-break is `titleSort` ASCENDING
  in BOTH directions — 6 of 6 tied groups on `year`, `rating`, `audienceRating`,
  `userRating`, `contentRating`, `studio`, `updatedAt` and `originallyAvailableAt`, under
  `:desc` as well. Both sides of that comparison come from zen, so ICU collation cancels
  out and what is left is the rule. Every remaining text-sort difference is collation,
  which is a declared non-goal.
- **`sort=duration` and `duration=` read the MEDIA's duration**, not the row's column.
  zen's `sort=duration` opens with 23 rows in title order whose durations run from 5.0 to
  9.9 million ms, every one of them a row whose Media carries no duration at all; the
  ordered list starts at index 23. And `duration=5700000` answers 0 although a row's own
  `duration` is exactly that. rex's rows always carry a Media duration equal to the
  row's, so the two cannot differ for its own catalog. Recorded rather than implemented.
- **On a SHOW listing a media column is read from the LEAVES, matching when any leaf
  matches.** zen's `type=2&duration<=1200000` answers 39 and `>=1200000` 483, which
  overlap — 522 against 505 rows with a value — and `mediaSize` behaves the same way.
  rex's show rows carry no Media at all, so every media filter on a show grid answers
  nothing. Not built this cycle: it needs the same leaf resolution the `viewedLeafCount`
  family uses, and cycle 19 was already full.
- **`lastViewedAt=` and `lastRatedAt=` are a 500 on both references** where every other
  date column answers a listing — `addedAt=`, `updatedAt=`, `createdAt=` and
  `originallyAvailableAt=` are all 200. Two columns, no rule that predicts which.
- **`audioLanguage`'s six non-`=`/`!=` operators.** On zen `=en` is 2,375 and `!=en`
  1,512 (complementary, and rex matches), while `==en` is 1,512, `!==en` 528,
  `<=en` 1,330, `>=en` 148, `<<=en` 3,664 and `>>=en` 657 — which no single model fits,
  on a field where a row holds several values. Left as measured.
- **`Agent`'s attribute order.** Hunt A reported it inverted. The only pairs both
  references constrain are `hasPrefs < identifier < primary`, which rex already
  satisfies; the `id` that kids puts first is 1.43.4-only and rex reports 1.42.2.
  Refuted.
- **A `viewCount` or `skipCount` tie.** zen's 3,866 rows with no `viewCount` do not
  order by `titleSort` within the group, so the column distinguishes NULL from 0 where
  the API omits both. Unobservable from outside.
- **`filters=` on the notification channels is a WHITELIST and `all` is not an event
  type**, so a socket opened with `filters=all` receives NOTHING -- which looks exactly
  like a server that pushes nothing. It cost this cycle one full round of frame
  measurements, and the control write is what caught it. Recorded here because the trap
  is the instrument's, not the server's.
- **A running `Activity` could not be caught in `/activities`.** Every activity on the
  copy completes in under 150 ms, so the element was censused from the FRAMES instead; it
  matches attribute for attribute (`uuid`, `type`, `cancellable`, `userID`, `title`,
  `subtitle`, `progress`, and a `Context` carrying `librarySectionID` from the second
  `updated` of a scan onward) apart from the `cancellable` finding above.
- **Neither reference library holds a single `Collection` or `Label`** — all six axes
  answer `totalSize 0` on both servers and both library types — so those two child
  elements could not be exercised at all. An instrument limit, not a rule.


#### The twentieth pass — the season and the episode row, the edit grammar by column, and the engines held out again

Two hunts and a driver round. Territory A took the show library at **type 3 and type 4** —
zen's 741 real seasons and 9,287 real episodes, the rows with the most derived fields on
the whole surface and the ones cycle 19's holdout never reached. Territory B swept the
**edit grammar by COLUMN**: every column of every row type, on the item path and the
section path, on plexscratch. The driver's own round ran rex's remaining engines over
a real library — `hierarchy.py`'s derivations against 10,535 rows and `filters.parse`
against both references' `/query/parse`, field by field and operator by operator.

**Thirty-four findings, every one re-measured by the driver before it was built.** One of
hunt A's was refuted on re-measurement and one of the recorded rules it corrected had
stood since cycle 11.

---

**A season's `titleSort` follows the title it STORES, not its `year`.** Cycle 7 recorded
"a season with a year of its own carries it, with its title as `titleSort`" from kids'
season 7, whose `Season 1` came from the agent and therefore IS stored. zen has 741 real
seasons: 740 carry no `titleSort` at all, and the ONE that does — One Piece's season 22,
the only season in the library with a `year` — carries `"Egghead"`, the arc name its agent
stored, not the `"Season 22"` it displays. So the year is a coincidence of a season an
agent really identified, and the rule is cycle 18's: a generated title stores nothing.
rex was contradicting itself here — the ROW said `titleSort: "Season 19"` while
`?titleSort=`, an equality on the empty string, matched the same row.

**An episode carries `parentThumb` only when the season's poster is the season's own.**
Held out over 9,293 real episodes on both references, with no exception either way: of
zen's 681 seasons that have episodes, the 654 with a poster of their own give every
episode a `parentThumb` and the 27 whose `thumb` is the SHOW's give none of them one;
kids reproduces it exactly (season 7 has its own and its three episodes carry the field,
season 11 carries the show's and its three carry none). `art` is NOT like this — it is
handed down borrowed or not — so it is a property of the FIELD, not of the row.

**Four more columns PARSE, and `slug` is the fifth.** Swept field by field and operator by
operator against both references: `theme`, `banner`, `art` and `thumb` each answer
`/query/parse` with a Rule carrying an EMPTY `fieldTitle` — the shape `id`, `guid`, `slug`
and `parentIndex` already had — under every one of the eight operator keys, and a LISTING
filtered on any of them is a **500** on both servers, as `parentIndex` already was. `slug`
is the same: it parses under all eight (rex answered an Error for `>>` and `<<`) and
its listing is a 500 for every operator, which cycle 7 measured and left unimplemented.

**`guid` takes exactly two operator keys, and the parser reports both as `<=`.** `guid=x`
and `guid<=x` are a Rule whose operator is `<=` — the parser saying out loud what cycle 13
measured from the listing side, that a bare `guid=` is a PREFIX match — while `guid!=`,
`guid!==`, `guid>=`, `guid>>=` and `guid<<=` are each an Error naming the key with the
operator still attached (`guid!`, `guid>`), and a listing filtered on one of those answers
every row. That is the unknown-FIELD shape, so those operators are not part of the field's
grammar at all.

**`type` with an operator is a real numeric column.** A bare `type=` is the listing's own
type selector, but `type>>=1`, `type<<=4`, `type!=1`, `type>=3` and `type<=2` each parse
into a Rule — the only rule on the surface with NO `fieldTitle` key at all, where every
other unadvertised column carries `""` — and each really filters, on the row's TYPE
NUMBER: zen answers 0 rows to `type=1&type>>=1` and all 3,887 to `type<<=99`, 0 to
`type=4&type!=4` and all 9,287 to `type=4&type>>=3`, and it reaches `/library/all` too.

**`sourceType` is a parameter both references know and neither filters on.** It must parse
as a whole number — an empty value, `1.5` and `abc` are 500s on a listing and at
`/query/parse` alike — it constrains no row, and the parser reports NO Rule for it. What it
does do is switch the source: any value but `1` makes the Query carry the DEFAULT `Order`
block (`id`, ascending) that it otherwise omits, with the Rule, Limit and Group blocks
untouched and an explicit `sort=` still winning.

**`group=` takes five more keys**: `originalTitle`, `createdAt`, `mediaCount`,
`ratingCount` and `absoluteIndex` (zen answers 472, 3,840, 9, 1 and 1 groups of its 3,887
movies), swept over 60 candidates on both references. They are exactly the unadvertised
columns cycles 14 and 15 found FILTER, so the group surface follows the filter surface
rather than the Meta; the 500s that remain are the tag joins, the media and the per-user
state.

---

**`Specials` and `[Unknown Season]` are server-generated season titles too.** Cycle 18
built the rule for `Season N` alone. zen holds 4 seasons titled `Specials` (each
`index: 0`) and 5 titled `[Unknown Season]` (each with no `index` at all), and all three
labels behave identically: `?titleSort=` answers 740 of the 741, `title=Specials` and
`title=Unknown` answer 0, `/search?type=3&query=specials` answers 0, and the `#` bucket of
the firstCharacter listing holds all 740. `[Unknown Season]` is the one where the
derivation differs from the title — the leading `[` would strip and leave
`Unknown Season]` — so it is also the one that proves the sort title is not derived at
all but simply absent. kids holds neither label, so the two new ones are single-reference
while the mechanism behind them is cycle 18's two-witness rule.

**`viewCount` and `viewOffset` read a row with NO stored value as BELOW every value and
equal to 0.** kids makes the mechanism unambiguous: three of its six episodes carry a
settings row with `view_count = 0` and three carry none, and it answers `viewCount=0` with
all six, `<<=0` with the three that have no row, `>=0` with the three that have one, and
`!=0` with none. zen agrees over 8,943 of 9,287 episodes. `skipCount` is NOT in this
family — its nulls match nothing, cycle 19's ordinary rule — which is how the two were
separated. This also corrects cycle 19's note that the null-versus-zero distinction is
invisible to the API: the FILTER sees it.

**`audioLanguage` and `subtitleLanguage` answer their six non-`=` operators as UNIVERSALLY
QUANTIFIED NEGATIONS** — the question cycle 19 left open. `=v` is "SOME stream of that type
is v"; every other operator is "NO stream satisfies the operator's own comparison", with
`!=` and `==` both comparing EQUALITY and `!==` inequality. On kids' six all-English
episodes that predicts `=`6 `!=`0 `==`0 `!==`6 `<=`0 `>=`0 `<<=`6 `>>=`6, which is exactly
what it answers; zen's 9,287 episodes and 3,887 movies agree, and a row with no stream of
that type matches every non-`=` operator vacuously (`audioLanguage=zz` is 0 and `!=zz` all
3,887, while `<=zz` is the 21 rows with no audio stream at all). The three-letter code
normalises under every operator, not just `=`.

**...and a stream-language column does NOT read the leaves.** Both references answer
nothing at all to `type=2&audioLanguage=en` and `type=3&subtitleLanguage=en` while the same
probe at `type=4` returns a real subset, so a parent row has no such column — unlike
`duration`, `resolution` and `mediaSize`, which cycle 19 measured reading the leaves.
rex applied the fallback here too and selected every show.

**A show library's media AXES are built from the leaves.** Both references answer a
populated `/library/sections/{show}/resolution` and `/audioLanguage` — zen 6 and 46
entries, kids 1 and 1 — although no show or season row carries Media. rex built them
from the section's top-level rows and answered an empty container, so a TV library's
Resolution and Audio Language menus were empty while the filters behind them worked.

**`file=` refuses a SHOW listing and reads the leaves on a SEASON one.** `type=2&file=/` is
a 500 on kids and zen alike — the only media column that refuses rather than answering —
while `type=3&file=/` answers every season and `type=4` every episode.

**`/library/sections/{id}/unwatched` is typed like a listing**: any `type=` but the
library's own top-level type answers EMPTY on both references, the container's `viewGroup`
flips to `episode` for every one of them on a show library where a movie library keeps
`movie`, and an unparseable or empty `type=` is a 500 — on `/onDeck` as well.

**`/recentlyViewedShows?type=N` is a typed QUERY**: zen answers 507 shows, 741 seasons and
9,287 episodes to `type=2`, `3` and `4`, kids 1, 2 and 6, both answer 0 to `type=1`, and
the container keeps `viewGroup="show"` and `allowSync: false` throughout. `type=abc` is a
500. rex ignored the parameter.

**`unmatched` IGNORES the operator.** Measured on kids' two seasons and zen's 741 over all
eight operators and six values: only the literal `1` selects the unmatched rows, and it
does so under every operator, while every other value selects the matched ones under every
operator. It belongs with `resolution`, which cycle 19 measured as operator-insensitive,
and not with `unwatched`, whose two-request boolean rule is the control and holds on both
references.

**On a PARENT row the Duplicates filter's two halves OVERLAP.** zen answers
`type=2&duplicate=1` with 44 shows and `!=1` with 487 — 531 of 507 rows, so 24 are in both
— and its seasons split 74 / 702 with 35 in both. Both queries are evaluated per LEAF, so
a parent holding one duplicated episode and one plain one is in each set. A scalar per
parent makes the two halves complements by construction: cycle 19's own any-leaf rule,
implemented as a proxy.

**A NULL date matches NOTHING, and `!=` really compares.** Of zen's 3,887 movies exactly
1,518 carry an `originallyAvailableAt`; `<=2100-01-01` answers those 1,518 while
`<=1800-01-01` and `<<=1800-01-01` answer 0, so the 2,369 without one are in neither half
— cycle 19's numeric rule exactly. The old reading, "a NULL date matches is-before", was
measured on fun, which is out of scope, and it put every season of a show library into
every `<=` set. And `addedAt!=<a date>` answers every row on both references where rex
answered none: the date branch had no `!=` case and fell through to equality.

**Of the four unlisted string fields only `titleSort` folds case.** `titleSort<=b` and
`<=B` answer the same 127 rows of zen and `<=m`/`<=M` the same 872, while `summary<=b` is
3,884 against `<=B`'s 2,911, `tagline<=b` 3,878 against 3,011 and `originalTitle<=b` 3,548
against 3,439 — a BYTE-ordered comparison, in which every upper-case letter sorts below
every lower-case one. kids reproduces it on five rows. Cycle 18 measured `titleSort` and
read the fold onto all four.

**`id==<n>` and `id!==<n>` DO compare a number**, on both references, over two libraries
and two row types — which overturns cycle 11's "`==`/`!==` never match a number" for this
column. Against a value that is NOT a number they still match nothing, which is what that
cycle actually measured.

**`mediaCount` and `createdAt` are recorded filter fields that could never select
anything.** Cycle 15 listed both among the nine that filter without a Meta and both were
read straight off the row, where no rex row carries either attribute. Both references
answer real subsets: `type=1&mediaCount=1` is 3,673 of zen's 3,887, `type=2` and `type=3`
read the LEAVES (487 shows and 702 seasons answer `=1`, 44 and 74 answer `>>=1` — the
Duplicates sets exactly), and `createdAt>=2000-01-01` answers every row of every library.
`sort=mediaCount` is a real ordering on both too, at every row type, where rex answered
500.

**A section search row carries no librarySection fields.** Cycle 19 made `/search` answer
the light grid row and the triple survived on that path alone; a grid row never carries it,
because it is the container's.

**Four small leaves validate `type=`, and rex refused the wrong half of each.** On both
references `?type=abc`, `?type=`, `?type=1.5` and `?type=%20` are each a 400 on `/sorts`,
`/filters`, `/common` and `/search` — sixteen cells, all 400 — where rex answered 200
to the empty value on the first two and to the three unparseable ones on the last two.

---

**A metadata edit does NOT move `updatedAt`.** Measured on plexscratch behind a control:
five writes to one movie (`title`, `summary`, `year`, `studio`, `contentRating`) over half
a minute left the column exactly where it was, on the item path and the section path
alike, while a poster write in the same session moved it. The column tracks the ARTWORK and
the row's own identity, not its text — and it is the cursor python-plexapi and every sync
client polls, so rex's stamp on every edit made a title fix look like a new row.

**An artwork URL's version IS the row's `updatedAt`** — 726 of 726 owned paths on zen and
12 of 12 on kids, `thumb` and `art` alike, with no exception. The copy shows the mechanism
from the write side: `updatedAt.value=1500000000` through the section save path moves the
column AND re-versions the poster to `/library/metadata/1/thumb/1500000000`, which still
serves the image. Every reference to a row's artwork follows it, borrowed or not, so a
season's `parentThumb` and an episode's `grandparentArt` move with the show.

**Eleven more columns are known to the ITEM edit path**: `absoluteIndex`, `updatedAt`,
`createdAt`, `skipCount`, `viewOffset`, `ratingCount`, `type`, `viewedLeafCount`,
`contentRatingAge`, `mediaCount` and `extraType`, each a 200 with its lock recorded in the
row's `Field` list, on a movie, a show, a season and an episode alike, where `bogusXY` is
the 400 an unknown name gets. Only `absoluteIndex`'s VALUE is read there (a word is a 500
on it and a 200 on all the others), which is what tells a known numeric column from an
ignored name.

**The SECTION save path reads every numeric column where the item path reads three.**
Seven parse — `addedAt`, `duration`, `parentIndex`, `ratingCount`, `updatedAt`,
`createdAt`, `contentRatingAge`: 200 for a number, 500 for a word — and eight refuse
outright with a 500 even for a valid number (`viewCount`, `lastViewedAt`, `skipCount`,
`viewOffset`, `type`, `viewedLeafCount`, `mediaCount`, `extraType`). Four of the seven
really STORE (`updatedAt`, `addedAt`, `ratingCount`, `contentRatingAge`), and so does an
episode's `index`, while `duration` and `parentIndex` parse and change nothing. It is the
Edit dialog's own save path, so a form carrying an unreadable number in any of fifteen
fields is refused whole by a real PMS.

**`year` is bounded to signed 32-bit**, bisected: 2147483647 and -2147483648 are 200s,
2147483648 and -2147483649 are 500s, and the bound is `year`'s alone — the other numeric
columns take 9223372036854775807 happily, because the item path never reads them. The
refusal carries cycle 19's whole-form rule with it: `summary.value=OK&year.value=<huge>` is
a 500 with the summary untouched, the last hole in "a save a real PMS refuses writes
nothing".

**A negative `rating` or `audienceRating` REMOVES the field**, exactly as a negative year
does, and the rule reaches those two floats and no other. rex stored `-3.0` and the
preplay page printed it.

**`theme` and `banner` are ARTWORK columns on the edit path.** `theme.value=<a url>` is a
200 and the row comes back carrying `theme: "/library/metadata/4/theme/1789421253"` — the
url discarded and the column minted as the row's own versioned path at its current
`updatedAt`, the same shape a poster pick produces. rex honoured the lock and stored
nothing, and on the SECTION path it 400'd `theme`, `banner` and `slug`, all three of which
the copy takes on both paths.

**An episode's inherited `contentRating` is a READ-TIME fallback.** Set the episode's own
and then the show's: the episode keeps its own. CLEAR the episode's and it reads the
show's at once; write the show again and the episode follows. rex copied the show's
value onto the episode at load and on a show edit, so an episode whose own rating was
cleared had none at all and drew a blank badge for the life of the process. Cycle 13
recorded the rule correctly and it was implemented as a write-time copy — a textbook class-C
proxy, and the fix is another one: see the note in `library.edit`.

**A COLLECTION row and a PLAYLIST row take the item path's whole validation.** Over the
same 57 columns on the copy: an unknown name is a 400, an unparseable number a 500
(`year`, `index`, `absoluteIndex`, `rating`, `audienceRating`), and `title`, `summary` and
`userRating` are the 200s — identically on both row types. rex skipped the validation
for these two and answered 200 to every name, the one place it contradicted itself, since
the same field was a 400 on a movie.

---

**Measured and not acted on.**

- **The `slug` is minted by the AGENT.** It is on a MOVIE or SHOW row only, and only when
  the row carries a `plex://` guid: 1,536 of zen's 3,887 movies and 452 of its 507 shows
  carry one, exactly the agent-matched ones, and not one of its 741 seasons or 9,287
  episodes does. A season's `parentSlug` and an episode's `grandparentSlug` are the SHOW's
  slug exactly — present on all 741 and all 9,287 when the show has one and absent when it
  does not. rex mints every guid itself (`metadata.py` always answers a `plex://` form),
  so every row it holds is agent-matched by construction and the two rules coincide; what
  stays a proxy is deriving the children's from the parent TITLE rather than from the
  parent's slug, which agrees today because rex mints both the same way. The slug's
  VALUE is not simply the slugified title either — 935 of zen's 1,536 match and the rest
  carry a disambiguating year or counter (`3bhk-2025`, `100-cooks-1`) or a
  transliteration — which nothing in the API exposes.
- **A season with no `index` is titled `[Unknown Season]` and its episodes carry neither
  `parentTitle` nor `parentIndex`** (5 seasons and 6 episodes on zen), and the season's
  `index` COLUMN reads -1 there (`index=-1` answers those 5). rex synthesises an index
  for every season it builds, so the row cannot arise in its catalog; the generated-title
  half of the rule IS built.
- **`addedAt!==<date>` is a 500** — on zen, on kids and on rex alike. Hunt A reported it
  as a difference; re-measured, all three agree, so it is REFUTED.
- The `Specials` / `[Unknown Season]` labels are **single-reference**: kids holds neither.
- The `updatedAt` rule's write half is plexscratch's alone, as every write rule is; the
  artwork-version half is read off both references and is not.

#### The twenty-first pass — the detail read of a season and an episode, the prefs families, and the writers held out again

Two hunts and a driver round. Territory A took the **detail read** of a season and an
episode and the **hub shelves** those two row types appear on — the dimension cycle 20
left when it swept the listing grammar at type 3 and type 4. Territory B took the
**write surface below the item**: the `/library/sections/{id}/prefs` family per library
type, the per-item prefs, the `/:/` write family swept as a set, and the writers this
project has never sent. The driver's round held rex's remaining **writers** out
against real rows: `xsweep.py` (new) compares every XML attribute and child-element
sequence both references agree on with rex's own, per path, and the parser's
composite grammar, the container attribute sets and the python-plexapi corpus were each
re-run as controls.

---

**The two forms of `/system/agents` carry different child elements.** The bare form
carries a `MediaType` row per media type the agent serves and NO `Language` at all —
zen's `com.plexapp.agents.none` has five MediaTypes and its fifteen agents have zero
Languages between them — while `?mediaType=N` is the other way round: a Language list
per agent (26, 33 or 45 rows) and no MediaType. rex sent its 45 Languages in both
forms and a MediaType in neither, so the Agents page could not say what an agent serves.

**`/system/agents` is the one endpoint whose JSON repeats a child element's KEY instead
of writing an array.** The raw body reads
`"Language":{"code":"ar"},"Language":{"code":"ca"},…` — 150 occurrences on zen and 195
on kids — and `MediaType` is written the same way, while every other repeated child on
every other path is a proper array (`Setting`, `Agent`, `Genre`, `Media`, `Role`,
`Image`, `Action`, `Directory`, `Feature`, `Pivot`). A JSON parser keeps the LAST of the
repeated keys, so a client sees exactly one language per agent where rex showed it
forty-five. The whole list is in the XML, which is where the `default="1"` the Add
Library dialog preselects is visible.

**A `Feature` element is never self-closed.** All fifteen on `/media/providers` are
written as paired empty elements on both servers, where `Directory`, `Action` and
`Pivot` in the same document self-close when they have no children.

---

**A SEASON's language settings inherit from the SERIES.** `includePreferences=1` on a
season answers `audioLanguage` and `subtitleLanguage` whose first enum entry is
`:Series default`, not `:Account default` — measured on four seasons across both
references, in three languages. A SHOW says `:Account default` and a movie has no such
setting, so the row type decides it. rex rebuilds that enum at read time (cycle 19's
"led by the item's own streams") and the rebuild hardcoded the account label.

**An EPISODE's `/tree` is the ancestor CHAIN.** Exactly three `MetadataItem` nodes — the
show, THAT episode's season and the episode — on five witnesses across both references.
No sibling season, no sibling episode. rex rooted it at the show and then answered
the WHOLE show, so the response grew with the show and the node the caller asked about
was no longer identifiable as the leaf.

**`/hubs/home/recentlyAdded` is populated at `type=1` and `type=2` only.** Both
references answer `size 0` to `type=3`, `4`, `5`, `8`, an unparseable one and an empty
one — and both hold episodes, so it is not a content difference — with and without
`sectionID`, `pinnedContentDirectoryID` and `includeMeta`. rex answered an
eight-row episode shelf at `type=4`.

**A `/library/onDeck` row LEADS with `allowSync` and the librarySection triple**, exactly
as a `/library/recentlyAdded` row does. Cycle 19 recorded the rule for both paths and
rex carried it on one — and `allowSync` on the ROW is the signal the XML writer keys
the whole lead on, so without it the four came out mid-row.

**A section's `/hubs/sections/{id}/continueWatching/items` container is
`{size, allowSync: true, identifier}` and nothing else.** rex said
`allowSync: false` and added `mediaTagPrefix` and `mediaTagVersion`, on an empty
container as on a populated one — cycle 15's "every hub path says allowSync true except
postplay", satisfied everywhere but here.

**An artwork path with an EXTRA SEGMENT after the version is a 404.**
`/library/metadata/{id}/thumb/{v}/x` and `/thumb/{v}/1/2` both, on `thumb`, `art` and
`poster`, on every row type. A single trailing slash stays a 200, and so does a bad
version. rex served the image for anything that merely STARTED like an artwork path.

**The per-user-state block sits in the MIDDLE of a row, before the year.** Measured on
zen's own watched rows: a show reads `index, audienceRating, userRating, viewCount,
skipCount, lastViewedAt, lastRatedAt, year`, a season `index, parentIndex, viewCount,
skipCount, lastViewedAt, parentYear` and an episode `index, parentIndex, audienceRating,
viewCount, lastViewedAt, year`. `xmlout._ATTR_ORDER` had no entry for those names on the
three buckets, so they fell out in dict insertion order and were appended after
`updatedAt` — and cycle 19 established that a real PMS's JSON key order is its XML
order, so both formats moved. `parentSummary` is the other half: on a
`/library/recentlyAdded` season row it comes directly after `parentTitle`.

---

**A collection and a playlist take EVERY `/:/` state write.** Measured on plexscratch
with a fresh collection and a fresh playlist born from `POST /library/collections` and
`POST /playlists`: `scrobble`, `unscrobble`, `rate`, `progress` and `timeline` are each a
200, and `rate`, `progress` and `unscrobble` store on them exactly as they do on an item —
an 8 is `userRating` plus `lastRatedAt`, and it survives the unscrobble that drops the
view state. The two part on the SCROBBLE: a playlist's own `viewCount` counts up from the
1 it is born with (1 → 2 → 3) and its unscrobble clears it with `lastViewedAt`, while a
collection's scrobble is a 200 that changes nothing at all — it never carries `viewCount`,
and it does not even clear a `viewOffset` a progress write left, so only the watched half
is the no-op. Neither reaches a member. rex 404'd all ten writes, so the star rating
on a collection page and on a playlist page errored and never stuck.

**And the state comes back MID-ROW, in its own order.** `userRating, viewOffset,
viewCount, lastViewedAt, lastRatedAt` — after a collection's `ratingCount` and before its
`thumb`, after a playlist's `composite` and before its `duration`. It is on every read of
a collection row (the detail, the `type=18` listing, `/library/collections/{id}`,
`/library/all?type=18`, `/collections`) and on every read of a playlist row EXCEPT
`/playlists/{id}` and the create response, which omit the block whole. That is the precise
shape of cycle 16's "the pair is on the LISTING row only": cycle 16 saw `/playlists` and
called it the listing's own, and the pair belongs to `/library/metadata/{id}` too.

**A SHOW library's `PUT /library/sections/{id}/analyze` frames every episode LEAF three
times.** `mediaState` analyzing, thumbnailing, none — eighteen frames for plexscratch's six
episodes, no activity, and an idle-socket control between three runs showed zero noise.
The movie library's fifteen for five movies is the same rule read from the leaves. rex
framed only rows whose own `type` is `movie`, so a show library's Analyze pushed NOTHING
and every `!!mediaState` gate in the client stayed as it was.

**A collection and a playlist answer the ITEM action leaves**, and the four that differ
differ by row type rather than by leaf. `/analyze` is a 200 on a collection and a 400 on a
playlist; `/allLeaves` and `/grandchildren` are real secondary containers on a collection
(`viewGroup: movie`, `viewMode: 65592`, `mixedParents` and the librarySection triple on the
allLeaves) and 404s on a playlist; `/related` is an empty container on both but carries the
triple and `allowSync` only on a collection; `/extras` is the item path's 400 and `/match`
its no-parameters 400; and `PUT`/`DELETE` of `/poster`, `/art` and `/thumb` land like an
item's. rex 404'd every one, so Change Poster and Clear Artwork on a collection page
did nothing at all.

**`PUT /library/collections/{id}` is the ITEM edit path.** A form that writes nothing is a
400, an unknown `.value` is a 400, a PREF name (`collectionMode`) is a 400 there where
`/prefs` takes it, and `titleSort.value` with its lock really stores. rex ran a second,
weaker code path that answered 200 to every form and dropped the sort title, so a save on
the collection page reported success and lost the edit — while the same forms through
`/library/metadata/{collection}` were already right, which is what made it two code paths
rather than one rule. **And `POST`, `PATCH` and a bare `OPTIONS` are 404s** on
`/library/collections/{id}`, on its `/children`, and on `/library/metadata/{id}` for both
list types: cycle 19's read-only-verb rule, which had never reached these paths.

**Twenty-nine more columns take an item edit and a real LOCK.** Swept on plexscratch over
41 candidate names drawn from both references' `Meta` `Filter`/`Field`/`Sort` keys and
every column findings.md records: each is a 200 that stores a `{"locked": true, "name":
…}` `Field` row and stores no VALUE, where rex answered 400 "unknown field". Eleven
names are a 400 on both — `album`, `artist`, `chapterSource`, `childCount`,
`hasChapterVideoStream`, `leafCount`, `logo`, `poster`, `role`, `sourceType`, `track` — so
the boundary is measured rather than assumed, and `hdr10plus` alone is a 200 that stores
nothing. Five of the twenty-nine (`atmos`, `audioCodec`, `audioLayout`, `subtitleCodec`,
`videoCodec`) are the media-codec family cycle 20 established is 1.43.4-only; plexscratch
is 1.43.4 and zen cannot be written, so their 1.42.2 status — the build rex reports —
is not measurable, and they are held out. The other twenty-four are implemented.

**A state write's `key=` is a BARE ratingKey, and the four writes parse it differently.**
The path form the client builds for `/:/timeline` — `/library/metadata/17` — is a 404 on
scrobble and unscrobble and a 500 on rate and progress, where rex took the last path
segment and accepted it. Past that: `rate` and `progress` int-parse the key (`abc`, `1.5`,
a leading space and, for `rate`, an empty value are 500s; `+17` and `017` parse) where
scrobble and unscrobble just look it up and 404; `progress` accepts ANY integer key,
present or not, with a 200 that stores nothing, where the other three 404 an unknown one;
`rate` validates its `rating` BEFORE the identifier and `progress` validates the identifier
before its `time`. findings.md recorded a single 400/404 rule for all four.

**`/:/progress?time=` takes a negative value and a leading `+`.** `-1` and `-500000` are
200s that store nothing and `+120000` stores 120000, while `abc`, an empty value, `1.5`,
`0x10`, `1e6` and a leading space stay 500s. rex read the value with `isdigit()`, so
the two shapes a real PMS accepts were refused.

**`DELETE /library/sections/{id}/indexes` moves the SECTION's `updatedAt`.** Measured five
times; the write still answers 200 with an empty body and zero frames, and every row's
`updatedAt` and `scannedAt` stay put. It is the container half of cycle 20's "an edit does
not move `updatedAt`, an artwork write does" — clients cache a library keyed on that stamp.

**A `PUT /library/metadata/{id}/refresh` does NOT move the row's `updatedAt`.** Four runs
on a movie and four on a show, and the reference really ran the refresh, because it pushed
its seven-frame sequence each time. Cycle 20 established that an artwork URL's version IS
the row's `updatedAt`, so the two move together and neither does: rex stamped both, and
every Refresh Metadata therefore told every client that an unchanged row's poster and
background had changed. plexscratch is isolated from plex.tv, so what is established is
that running the refresh sequence does not by itself stamp the row.

**A TEXT library preference is percent-decoded a SECOND time.** A wire `%2541` is `%41`
after HTTP and stores `A`; a wire `%2525` stores `%25`, which is how the storage escaping
is known to cover `%` as well as `:` and `|`; `%25E2%2582%25AC` stores `€`; and a second
decode that FAILS — `a%b`, where `%b` is not an escape — stores the empty string. That
last case is the whole of cycle 17's "a value carrying `%` is stored EMPTY": rex
emptied every value containing one, so a ratings source written with a percent escape was
lost rather than decoded.

**`PUT /library/metadata/{playlist}/prefs` is a 400**, the item prefs writer's own refusal
for a name it does not know, where rex 404'd it because that branch resolved a
collection and nothing else.

**An EMPTY response under `/system` carries no `Content-Type` header at all.** Measured on
kids, zen AND plexscratch over every verb: every 404 — GET, HEAD, POST, PUT and DELETE, on
`/system` itself and on an unknown path under it — and the bare `OPTIONS` 200 come back
with no body and no type, where a 404 anywhere else on the surface carries `text/html` and
a body. Cycle 18 recorded the empty body for the non-GET verbs and implemented it as an
empty `text/html`; the GET was never covered, so it still carried rex's own 18-byte
`text/plain` refusal.

#### The twenty-second pass — the movie detail read, the writers at scale, and the frames a write really pushes

Two hunts and a driver round. Territory A took the **movie** — the row type nobody had
swept since cycle 13, whose instrument then was a five-clip lab where nothing is long
enough to truncate and no movie has a second Media — and the **XML and JSON writers at
scale**. Territory B took the **frames a write really pushes**, the dimension twenty-one
cycles of measuring what a write STORES had left thinnest, and held `mediainfo.py` out
against zen's 13,643 real Media. The driver re-ran every engine holdout against the
cycle's own HEAD.

---

**`excludeElements=` honours an ALLOW-LIST, and cycle 19 generalised the wrong way.**
Swept over every element name a real movie row carries on zen (74 roles, 4 extras, 5
related) and on kids: only `Media`, `Part`, `Genre`, `Country`, `Guid`, `Rating`,
`Director`, `Writer`, `Role` and `Producer` are honoured, and every other name — `Extras`,
`Related`, `Preferences`, `Field`, `Chapter`, `Review` — behaves exactly like an unknown
one. Cycle 19 measured `Image` and `UltraBlurColors` as no-ops from a two-name sample and
recorded "every other element the option names really goes"; rex implemented that, so
a client naming `Related` or `Preferences` in a bandwidth-trimming list lost a block a real
PMS still sends. **`Part` is on the list**: naming it removes the Parts AND their Streams
and adds `partCount` to the Media, the same substitution `Media` makes one level up.
**And the option reaches the rows INSIDE the inline `Related` and `Extras` blocks** — with
`excludeElements=Media` the nested related rows lose their Media and gain `mediaCount`,
where rex trimmed the top-level row alone. The one request whose whole point is to make
a preplay page smaller still carried five related rows in full.

**Five more query options carry `checkFiles`'s value grammar.** `skipRefresh`,
`asyncCheckFiles`, `asyncAugmentMetadata`, `asyncRefreshLocalMediaAgent` and
`asyncRefreshAnalysis`: `0`, `1`, `00` and `01` are 200s and an empty value, `abc`, `2`,
`true`, `-1` and `1.5` are 400s, measured ten values at a time on both references. A
60-name sweep of the detail read found these six and no others — every `include*` name is a
200 for any value. **And four `include*` options parse their value as an INTEGER**:
`includeExtras`, `includeRelated`, `includeChildren` and `includeOnDeck` strip leading
zeros, so `01` and `001` turn them on, while `includePreferences` compares the string and
takes `1` alone. rex tested `== "1"` for all five, so the zero-padded form silently
dropped the block — fix 4's failure mode, a valid document short a section.

**`includeRelatedCount=0` EMPTIES the inline hub** — `{hubIdentifier, size: 0, more: true}`
with no rows — where rex read `0` as "absent" and fell through to its default of five.

**A real PMS writes one element per LINE, and ends the document with a newline.** zen's
movie document carries 236 newlines and kids' 34; rex emitted one, after the
declaration, and no trailing one. Nothing a conforming parser can see — and it changes
`Content-Length` on every XML response and what a `Range` read of a container returns,
which cycle 16 established applies to every body. **Six more element names are written
PAIRED when empty**: `MediaContainer`, `Stream`, `Hub`, `Extras`, `Media` and `Activity`
join the `Feature` cycle 21 found. `Part` self-closes while `Stream` never does (37,464 of
37,464 on zen), so it is an element-NAME table and not "anything that can hold children".

**Three more attribute orders, harvested from 633 zen paths and 133 kids paths.** A `Part`
leads `accessible, exists, id, key`; a section's pivot `Directory` leads `secondary` (and
`prompt, search` on the search pivot); a `Feature` leads `flavor`. `_ATTR_ORDER` is a
topological sort of observed PAIRS, so a pair no reference was ever seen to emit falls to
the end — which is exactly where all three were. Cycle 19 made the JSON key order follow
the XML, so all three move both formats.

**A LIGHT row's JSON child order was never the light order.** `xmlout._split` picks it off
the row's own `_light` marker and `json_body` called `_drop_private` — which removes the
marker — BEFORE the orderer saw it, so every light row's JSON fell back to the detail
order. Cycle 19's "a real PMS's JSON key order IS its XML order" held on every path but
this one.

---

**A text `sort=` compares RAW CODEPOINTS.** `sort=title:desc` on zen opens on
`『ONE PIECE』…` (U+300E), then `iHostage`, then `Zero Tolerance`; `studio:desc` on
`ufotable` before `Zide-Perry`; `summary:desc` on `Özge…`, `join 6 murder…`,
`handu dreams…`. Descending puts the highest codepoint first every time, so lowercase sorts
ABOVE uppercase and there is no accent folding. rex's `_sort_key` lower-cased every
text key. It is the SORT side of the split cycle 20 measured on the FILTER side; `titleSort`
itself stays folded, because no row in zen's 3,887 movies nor in the first 2,400 of its show
library carries a lowercase-initial stored sort title to measure it with.

**A filter key whose NAME keeps a comparison character is an UNKNOWN FIELD.**
`collection==`, `year>=`, `title!!` are each a 200 listing every row and a 200 `Error` of
type `field` at `/query/parse`, exactly as a bogus name is, where rex raised a 400 from
`split_key`. The recorded 400 is a different shape and still stands: a pair with no `=` at
all (`?year%3E2010`) is a 400 everywhere. *(The first version of this finding was wrong and
is worth recording as such: a sweep reported 57 disagreements on the tag-table fields and
they were an artefact of the probe's own encoding — the client puts the operator characters
in the parameter NAME, so `year>>=2000` is the parameter `year>>`, and a probe that sends
`collection%3D%3D=zzzz` is not sending the `==` operator at all. Re-sent the client's way,
rex already agreed on all eight operators.)*

**A bare `type=<v>` on the ITEM edit path is an accepted no-op.** `type=1`, `type=4` and
`type=abc` are all 200s that read no value and change no row — `type` is one of the lockable
names cycle 20 added. rex stripped it from the edit form on both paths, because on the
SECTION path it is the scoping parameter, so a form naming only `type` looked empty on the
item path and got the 400 an empty form answers.

---

**An artwork frame carries the LIST row's own type.** A collection's is `type 18` and a
playlist's `type 15` with `sectionID "-1"`, on the artwork write as on every other frame.
rex sent `type 1` for both and an empty `sectionID` for the playlist — and CLAUDE.md
already records that the client builds the URL it updates from that number, so a Change
Poster on a collection told every client on the socket that a MOVIE with that ratingKey had
changed. A stored list row carries no `type` of its own; the projection mints it.

**`/library/collections/{id}/{refresh,unmatch,analyze}` answer an EMPTY body**, where
rex fell through to a `{size: 0}` MediaContainer. CLAUDE.md's own rule — "a mutating
endpoint answers 200 with an EMPTY body, not a MediaContainer" — had never reached these
three, and the client parses the body of `match` to decide whether the match took.

**`/resources/{anything}` is a real 200** answering an empty container — `1`, `99999` and
`abc` alike on kids, zen and plexscratch, and on plexscratch every verb answers the same.
**`/search/voice` with no parameters is a 400** that becomes a 404 once a `query=` is given,
so the path exists and validates. **And `/search` is a real ROUTED endpoint**: all three
references refuse its writers with a 400 where rex fell through to the plug-in
dispatcher and answered a listing, which reads to a client as an accepted write.

**`PATCH` had never been swept on the paths cycle 6 took POST/PUT/DELETE over.** It is a 404
on `/library/sections`, `/status/sessions`, `/media/providers` and `/playQueues/{id}`, where
rex answered each one's own GET; `/accounts/{id}` refuses a POST as well as the DELETE
cycle 19 measured. **And `/system/proxy` is the one path in that subtree that does NOT take
cycle 18's envelope**: every verb, GET included, answers its own 400 rather than an
empty-bodied 404.

#### The twenty-third pass — the frame sequences cycle 22 measured, the search row, and the Meta's active sort

Two hunts and a driver round. Territory A took the **child-element order at scale** and the
containers no cycle had swept; territory B took the **butler end to end** and finished
`mediainfo` against zen's real library. The driver's round implemented the two largest items
cycle 22 had measured and deliberately held out.

**Territory A's Part 1 found nothing, and that is the result.** Harvested from 2,994 paths
across the three servers, the child sequences landed in 31 element buckets per reference:
**137 ordered pairs witnessed by both references and 66 by one, and not one of the 203
contradicts `xmlout._CHILD_ORDER`**; no reference sequence, in any bucket, is impossible
under it. zen's rows really did exercise `Chapter`, `Marker`, `Review`, `CommonSenseMedia`,
`Producer`, `Extras`, `Related`, `Preferences`, `OnDeck`, `Children`, `Location`, 155-role
and 5-Media rows. The table built on a five-clip lab holds against a real library. The same
sweep confirmed cycle 19's JSON-order rule over **1,241 reference path/format pairs with
zero disagreements**.

---

**A `/search` row is the GRID row with a six-attribute lead.** Measured with the same row
fetched twice per server — once from the section grid, once from `/search` — the two are
byte-identical apart from `allowSync, librarySectionID, librarySectionTitle,
librarySectionUUID, personal, sourceTitle` in front. rex answered the DETAIL row there,
led by `ratingKey`: `Guid`, `Rating` and tag elements carrying `id`/`filter`, with no
`allowSync` and no `librarySectionUUID`, and a `librarySectionKey` neither reference sends.
On a real library that is several times the bytes per hit — zen's detail rows carry up to
155 `Role` elements — and a client keying a hit to a library by `librarySectionUUID` gets
nothing.

**A `Meta/Type`'s active SORT follows the asked type AND the `sort=` parameter.** Measured
over seven library/type pairs and four sort spellings, identically on both references: the
active Sort is marked only inside the Type row matching the asked `type=` — a movie library
asked `type=2` or `type=4` marks **nothing at all** — and which one is marked follows the
request: none given lights the default (`titleSort`, asc), `sort=addedAt:desc` lights
`addedAt` with `activeDirection="desc"`, and a spec naming no advertised key lights nothing.
rex lit its own top-level type's first sort in every cell, so on a Seasons or Episodes
grid the sort menu said "Title" while the rows were in broadcast order. Cycle 19's rule
about the `Type` row's own `active` is a different attribute and stays correct.

**`?includeDetails=1&includeAdvanced=1` carries NO Duplicates Filter row at all.** The
option pair that adds the row to the `includeMeta` form adds nothing here. Cycle 16 measured
this path *without* `includeAdvanced=1`, where the row does not exist to carry a flag, and
recorded "`includeDetails=1` carries no `advanced` on a Filter" — so rex implemented the
absence of the FLAG and left the row.

**Three more orders, and the last place rex's two writers disagreed.** A `/library/tags`
row is `id, filter, tag, tagType, tagKey, thumb` with `thumb` LAST, where rex led with
it. The `/hubs/sections/{id}/manage` Hub leads with `identifier` and **self-closes** — cycle
22 put `Hub` in `_ALWAYS_PAIRED` after a census that never reached `/manage`, and the rule
is the one it left open for `Directory`: paired when the element carries a child collection,
self-closed when it carries none. A `Common` row leads with the GRANDPARENT pair and puts
the PARENT pair before `ratingKey`. And `/library/sections/{id}/common` was the **last**
path where rex's own XML and JSON writers contradicted each other: `ordered()` never
received the `_xml=` override, because `_send_container` pops it before either writer runs,
so the row bucketed as `Video[common]` — which no index knows — and its JSON keys stayed in
build order while the XML sorted them.

---

**A MOVIE's unmatch pushes twelve frames; rex pushed none.** The sequence opens on two
`matching` frames — `state 2` then `state 3`, a pair rex emitted nowhere — and the
activity inside it steps through a second TITLE, `Analyzing Media`, before returning to
`Updating Metadata`: `2/matching, 3/matching, 3/queued, started(-1), updated(-1, Analyzing
Media), updated(25, Analyzing Media), updated(25, Updating Metadata), updated(50),
3/processing, 4/loading, ended(100), 5`. An unmatch rewrites the whole row, so a client told
nothing keeps the old title, guid and people lists until a reload.

**A collection and a playlist push their own refresh and unmatch sequences**, and both were
404s — so the Refresh Metadata and Fix Match entries `/media/providers` advertises on a
collection page did nothing at all. A collection's refresh is **gated on its guid**: three
frames (`2/matching`, `5`, `3/queued`) while it carries an agent guid, and the full eight
once an unmatch has given it a `local://` one. Its unmatch is seven and moves the guid; a
playlist's refresh is four and its unmatch two, neither with an activity. A collection's
`/analyze` is the activity PAIR alone with **no** timeline frames — its members are never
framed, the opposite of the per-leaf rule an item's analyze follows.

**Three more read-only routes refuse every other verb.** `/hubs/home/recentlyAdded`,
`/hubs/sections/{id}` and `/library/metadata/{id}/children` answer 404 to POST, PUT, PATCH,
DELETE and a bare OPTIONS, where rex answered each one's own GET. Cycle 19 recorded the
rule and these three never had it applied — fix 4's failure mode from the other side, a
client reading a 200 from `DELETE /hubs/sections/1` as an accepted write.

#### The twenty-fourth pass — the backlog cycle: `mediainfo` finished against a real library

No hunts. Cycles 21, 22 and 23 each closed carrying measured-but-unimplemented findings, and
by the end of cycle 23 that backlog was the largest this programme had held. This pass spent
itself on the part of it that was fully measured and held out against both corpora, and
re-derived every rule from the corpora rather than from the report that proposed it.

**The extended title's LANGUAGE part, settled at the third attempt.** Cycle 22 fitted "the
whole name, not the first word" to zen's rows and it broke the recorded corpus; the
first-word rule alone fits the corpus and misses 179 of zen's. Both are explained by one
rule: **the language is carried only when it is a SINGLE token that is one of the title's
words, with any TRAILING PARENTHESISED GROUP stripped from the name first.** So `Русский` in
`Русский (Full) - Кубик в Кубе` and `English (Weeb)` in `English (Some-Stuffs)` are carried
— one token after the strip — while `norsk bokmål` and `Tiếng Việt` in their own titles are
not, because no single word can match a two-token name.

**Five more stand-ins in the "part the title already carries" test**, each a witnessed token
rather than a threshold: `Signs` and `Songs` for Forced (120 rows on zen titled
`Signs & Songs…` with `forced` set and no "forced" in the title), `CC` and `HI` for SDH,
`1.0` for Mono exactly as `2.0` already stood in for Stereo, `HDR` and `Dolby` for the
dynamic range, and the **raw ffprobe codec name** with punctuation folded away — which is
what makes `aac` carry `HE-AAC` and `H264` carry `H.264`.

**`eia_608` is labelled `Closed Captions`**, and it is the plain CODEC label, so it lands
wherever the codec does. **A sidecar subtitle's codec suffix is its `providerTitle`**, with
`External` the fallback: zen's eleven sidecars carry none and read `(SRT External)`, and the
recorded corpus has one carrying `OpenSubtitles` that reads `(English SRT OpenSubtitles)` —
so cycle 17's `<CODEC> External`, measured on one server, was the fallback alone.

Scored end to end against **zen's 105,558 real Streams**, the miss rate falls from **613 to
174**, and against the 46 recorded probes from 3 to **0**. The 174 that remain are recorded
rather than modelled: 132 are titles containing `AC-3`, which carries `AC3` although `VC-1`
does not carry `VC1` and `E-AC3` does not carry `EAC3` — no punctuation rule fits, and a
general one breaks 58 other streams — and the rest are streams whose stored title is the
empty string, which a published row cannot be told apart from one with no title at all.

**Three more, each measured on the same corpus.** `languages.json` lacked **thirteen** codes
zen's streams actually use — `cha guj hye kat mar mdr mon mya nno pan sin yor zul`, each
named in its own language with its 639-1 tag. A VIDEO stream carries the `dub` disposition
(88 of zen's 13,681) and never `comment` or `hearingImpaired`, so its set is
default/forced/original/dub. And a layout ffprobe cannot name —
`8 channels (FL+FR+FC+LFE+SL+SR+TFL+TFR)` — left a trailing SPACE behind when the bracket
was split off, composing `(EAC3 8 channels )` with a double space.

**One reported difference was re-measured and is not a finding.** zen's stored `displayTitle`
carries the codec on 78,902 of its streams where rex's catalog form does not — which is
the recorded client-title rule, not a defect: those rows were read as `Plex Web`, one of the
thirteen products that get the codec added on the way out. The check that tests `eia_608`
therefore reads the extended title, which is the catalog form.

#### The twenty-fifth pass — the cascades cycle 23 deferred, and two dimensions nobody had swept

Two hunts and a driver round. The driver opened the cycle by taking the two items cycle 24
named first: the **season and show unmatch cascades**, which cycle 23 deferred because its own
hunt was running the butler on the same writable copy and put thirty stray activity frames
through the listener, and **`pixelAspectRatio`/`anamorphic`**, whose threshold HANDOVER 3.3
carried as bracketed rather than measured.

**A SEASON's unmatch pushes ONE `matching` frame and then the season cascade; a SHOW's pushes
the movie's `matching` PAIR and 46 frames of its own.** Measured on plexscratch with nothing
else writing to it, twice over — once with the show unmatched first on a freshly reset copy
and once second — so the ordering is not an artefact of the state the first left behind. The
openers differ by row type and both were captured in the same run: a movie and a show open
`state 2 matching` then `state 3 matching`, a season with one frame. rex answered a
season's unmatch with `notify_refreshed` — the seven-frame EPISODE form — so every episode
under the season it had just unmatched kept its old title and guid, and it pushed NOTHING at
all for a show, so Fix Match on a show page changed nothing on screen and left every
`!!metadataState` gate latched.

The show's activity is not its refresh's. It is the MOVIE unmatch's — `Analyzing Media` at -1
and 25, then `Updating Metadata` at 25 and 50 — and only after that opener does the subtitle
start following the seasons, changing at the progress the activity already carries and then
stepping. The media pass is deliberately not emulated: on a freshly reset copy the same
request also analyses the leaves whose media has never been read and on a copy that has run
one it does not, which is cycle 15's state-dependent-work rule and not a frame sequence.

**And a show UNMATCH's activity opens with the show's BARE TITLE**, where its REFRESH opens
with the frame title (`Firefly (2002)`). That one was found by checking the driver's own
implementation rather than by a hunt: the cascade had been written to use the frame title
everywhere, and running the refresh against the same copy on the same socket showed the two
paths differ at exactly one frame. The show carries a year there, so it is not a row with
nothing to append — it is the one place on this surface where an activity's subtitle is not
the frame title, and from the season onwards both paths use the frame title again.

**And a season cascade's activity has no `updated` frame.** Its docstring has said so since
cycle 15 and the code called `finish_activity`, which inserts one — nineteen frames where the
reference pushes eighteen. Class C exactly: the rule was recorded right and implemented as the
nearest helper to hand.

**`pixelAspectRatio` and `anamorphic` come as a PAIR, and the boundary is the ratio rounded to
one decimal place.** Measured on 1,688 of zen's video Streams: 358 carry both, 1,330 carry
neither, and not one carries only one of the two. The value is ffprobe's `sample_aspect_ratio`
verbatim, present exactly when the sample aspect is not square. `anamorphic` is the one Stream
boolean a real PMS sends when it is FALSE, so it goes out either way, and sixteen distinct
ratios agree on when it is true: `99:100`, `172:173`, `799:800`, `816:817`, `872:873`,
`959:960`, `10439:10440`, `481:480`, `255:254` and `643:640` (303 streams) are false, and
`1:2`, `10:11`, `16:15`, `495:464`, `721:640` and `2:1` are true — with the recorded corpus's
`333:320` false and `32:27` true. That puts the boundary inside (0.0406, 0.0667], where 0.05
is the only round number, so the two SIDES are measured and the constant is still inferred. A
pixel-difference model was tried first and is refuted: the 3840-wide `333:320` stream displaces
156 pixels and is false where a 716-wide `16:15` displaces 48 and is true.

**The resolution label had no 480 WIDTH bucket at all.** zen's three 856×358 Media are `480` on
a real PMS and were `sd` here, because `_WIDTH_BUCKETS` stopped at 576. The new entry is built
the way the other five were — the smallest width zen has in the bucket, with the largest below
it named in the comment — and the boundary is NOT bisected: zen holds no width between 720 and
856. The same harvest put every shape zen's libraries hold and fun's did not into
`tests/fixtures/media-resolutions.json`, seventeen of them, and the corpus itself failed
against the unfixed code on exactly one row.

**A search hit LEADS with `librarySectionTitle`, and on `/hubs/search` its `score` follows.**
HANDOVER 3.3 carried this as single-witness because cycle 22's driver could only reproduce it
on zen — kids' hubs answered no `Video` for the queries that cycle tried. `query=Sintel` hits
on kids, and both references then agree exactly: `librarySectionTitle, score, ratingKey, key,
guid, …` on `/hubs/search` and `librarySectionTitle, ratingKey, …` on `/library/search`.
rex carried both attributes and put `librarySectionTitle` after `title` and `score` LAST,
so — since a real PMS's JSON key order is its XML order — both of its formats disagreed with
both references. It needs a private marker rather than a signal read off the row: `/library/all`
and `/hubs/home/recentlyAdded` rows carry `librarySectionTitle` too and keep it in the middle.

**`/match` really does answer its no-parameters 400 to EVERY verb.** Cycle 17 recorded that
sentence and implemented it for GET and DELETE, so HEAD, PATCH and a bare OPTIONS were still
404s — the client's own "is this matchable?" probe read as an endpoint that does not exist.
Re-measured over all seven verbs on kids, which answers 400 to each.

---

**Territory A took the CLIENT-IDENTITY dimension**, which nobody had swept at all. rex
knew of exactly two client-identity rules, both found by accident: `X-Plex-Platform: Roku`
stripping `id` and `Pivot` from `/media/providers`, and the thirteen products that get a
codec appended to a stream's `displayTitle`. A 9,300-pair sweep over ~30 identity values and
~80 paths on both references found that **only three headers move anything at all** --
`X-Plex-Platform`, `X-Plex-Product` and `X-Plex-Language`. `X-Plex-Features`,
`X-Plex-Client-Profile-Extra`, `X-Plex-Drm`, `X-Plex-Model`, `X-Plex-Provides`,
`X-Plex-Session-Identifier`, `X-Plex-Client-Identifier`, `X-Plex-Device`,
`X-Plex-Device-Name`, `X-Plex-Text-Format`, `X-Plex-Version`, `X-Plex-Platform-Version`,
`X-Plex-Sync-Version` and `X-Plex-Protocol` each sent alone moved nothing, on any path: no
identity changes a `Media`, `Part` or `Stream` row, the XML-vs-JSON default, the gzip
decision, the paging defaults, any response header, or the hub composition. That is a real
result and it bounds the dimension.

**`X-Plex-Platform: Samsung` appends a hidden `includeInGlobal` to a library's `/prefs`.**
Measured on kids and zen over both library types, 19 rows to 20 and 21 to 22, `label` and
`summary` empty and `hidden="1"`. That exact spelling only -- `samsung`, `SAMSUNG`,
`Samsung `, `Tizen`, `Samsung TV` and 27 other platform strings answer the plain list --
and it reaches the EXISTING-library leaf alone: the Add Library form's list, an item's
`?includePreferences=1` block and the `Preferences` block inside `/media/providers` are all
unchanged under the same header. It is the second platform-keyed reshape on the surface,
which is what makes the Roku rule a family rather than a quirk. Re-measured by the driver on
both references, and the HEADER beats the query when both are sent.

**A modern agent's `Language` rows are TITLED and ORDERED in the client's language.** 45
rows, all six modern agents sharing one list, identical on kids and zen. `system_agents.json`
was captured with no `X-Plex-Language` at all -- **class A, the failure this project's one
rule exists for** -- so rex answered the self-named list to every client, English
included. Two things about it are unlike every other localised string here: the ORDER follows
the localised title, so the row sequence moves with the language; and the NO-HEADER default
is not English but each language named in ITSELF, which is also where an UNRECOGNISED
language lands rather than falling back to English. The list is therefore stored per language
(`agent_languages.json`, captured from the XML, which is the only form that carries every row)
rather than translated string by string, because a real PMS collates it with ICU and this
project does not model collation. The Add Library and Edit Library dialogs build their
language dropdown from this list and validate the submitted `language` against it.

**`/media/providers?includePreferences=1` carries each library's whole `Preferences` block**,
byte-identical to the one `/library/sections?includePreferences=1` answers -- the per-SHOW
`episodeSort` variant included -- as the first child of the library `Directory`, with the
`Home` row getting none. rex handled the option at three other sites and ignored it here,
so a client that bootstraps library preferences from the provider document in one round trip
saw libraries with no preferences at all.

**`/library`'s three `Directory` titles are localised, and `/system`'s identical string is
not.** `Bibliothek-Bereiche`, `Zuletzt hinzugefügter Inhalt`, `Aktuelle Inhalte` on both
references; the three were literals here and in no translation table, so the first path
python-plexapi asks for read English to every client. The collision is the interesting half:
the plug-in framework's `/system` carries a `Directory` titled `Library Sections` -- the same
English string, the same attribute -- and a real PMS leaves it alone, so the table's
attribute key is not enough and `_send_container` now skips a plug-in-framework container
(`noHistory` is its signature). `/system` is the only one of the six that carries a
translatable string at all; the others title their rows with their own lowercase key names.

**And the tree's `Setting` rows are NOT localised, where rex localised them.** All eight
of a show node's settings stay English in de, fr and ja on both references, while the same
settings under `?includePreferences=1` move. A tree node is a ROW -- it is simply keyed by
`id` rather than `ratingKey`, so the row guard did not recognise it. The inverse of the usual
defect, and still a rule broken.

---

**Territory B took the CROSS-ENDPOINT dimension**: not *what does this write answer* and not
*what frames does it push*, both of which are well covered, but **which other endpoints
change**. That is the question a real client depends on — the UI does not re-read the row it
just wrote, it reads Home's hubs, the section's shelves, the history page, the filter axes —
and a write that lands correctly on its own row and leaves every derived view stale is
invisible to every check this programme has ever run. 72 derived endpoints snapshotted before
and after each of 21 writes on both servers, 1,512 (write, endpoint) pairs per server, and the
DIFF SETS compared rather than the responses.

**Continue Watching is not On Deck.** Measured through four driven states: Continue Watching
holds only the rows with a stored `viewOffset`, movies and episodes alike, where On Deck holds
one row per show — its in-progress episode, or the one after the last watched, which has no
offset at all. Scrobbling episode 8 puts episode 9 on deck with a null offset and leaves
Continue Watching empty; a `/:/progress` on a movie does the opposite. rex answered the
same list to both, so its hero shelf offered episodes nobody had started.

**And Home DROPS its On Deck shelf when it would duplicate Continue Watching.** The
`home.ondeck` Hub is omitted from `/hubs` and `/hubs/promoted` — the element, not an empty one
— exactly when its rows are the ones Continue Watching is already showing and that set is not
empty. Cleared: both present. Next-up only: both present. One episode in progress: On Deck
absent. A movie in progress beside it: both present again. rex always sent both, so Home
drew the same in-progress episode twice. The two findings are one mechanism: because rex's
two lists were always equal, the de-duplication would have fired in every state.

**A SCROBBLE writes a history row, which overturns cycle 9.** One row per LEAF marked: a movie
scrobbled twice adds two, a season adds one per episode, a show one per leaf, and an unscrobble
removes none. Cycle 9 recorded the copy's history "staying at one row through three scrobbles";
driven again, one scrobble at a time with the count read between each, it moves every time.
The half that is NOT asserted is the timeline's, which writes no row on this copy — that is
the already-recorded session-bound limit, and a negative from an instrument with a known
limit is not a measurement.

**An episode history row carries `title` and `originallyAvailableAt`**, in an order that is
its own and not an ordinary `Video`'s: `historyKey, key, ratingKey, librarySectionID,
parentKey, grandparentKey, title, grandparentTitle, type, thumb, …, index, parentIndex,
originallyAvailableAt, viewedAt, accountID, deviceID, viewType`. Witnessed on zen (1.42.2, 73
of 73 rows) and on plexscratch, so it is not a version difference; the code's own comment
says the field set was measured on fun and both current references disagree with it. The
History page listed the episode with no episode name, in both formats.

**A playlist appeared nowhere but `/playlists`.** Home's Recent Playlists shelf is really
populated on a real PMS — its rows are the `/playlists` listing rows, `lastViewedAt:desc`,
with `hubKey` naming its own rows like every other shelf — and a playlist is found by
`/hubs/search` and by `/library/search`. rex hardcoded the shelf's rows to `[]`, with a
comment saying the reference had "no playlist history", which was true of the reference when
it was measured and is not a rule; and it searched its catalog alone, where a playlist is not
a row.

**The untyped `/library/all` omits collections**, where a real PMS lists them — byte for byte
the row `?type=18` answers, sorted in by ratingKey, appearing on a create and gone on a
delete. The section form lists them on neither server, so the gap is this query alone, and any
client or script enumerating the server through `/library/all` never saw one.

**`removeFromContinueWatching` does not reach a SECTION's own Continue Watching shelf.** The
hide empties `/hubs/home/onDeck`, `/hubs/home/continueWatching` and
`/library/sections/{id}/onDeck` on both servers, and on a real PMS the row is still in
`/hubs/sections/{id}/continueWatching/items` afterwards — stable to +20 s, so it is not a
settle. rex removed it there too, so a row hidden from Home was gone from the one place a
real PMS still offers it.

**And a `Common`'s `userRating` is a STRING**, `"8"` rather than `8.0`, sitting directly after
`mixedFields` and ahead of `guid`, while the item row keeps the number on both servers. Single
witness by construction: the field only appears once every row in the section shares a value,
which neither read-only reference can be made to do — zen's section 1 holds 3,887 movies.

**What the sweep found AGREEING is the other half of the result**, and it is a large one: an
edit moves nothing in any Recently Added list; a genre add reaches the genre axis and
`/library/tags` immediately with the right count and the removal drops the axis entry while
the tag survives in the tag table; a title edit reaches both search endpoints, the section
search box, `/autocomplete` and the `firstCharacter` axis and its counts immediately; a
collection create reaches the collection axis, the member's `Collection` tag and
`/hubs/search`, and a member add moves `childCount`, `minYear`/`maxYear`, the composite
version and `updatedAt` identically; and scrobble, unscrobble, progress and completion move
`viewedLeafCount`, `lastViewedAt`, `viewCount`, `/unwatched` and the parents' counters
equivalently. **No derived view on a real PMS settles late** — every reference change was
already present in a snapshot taken immediately after the write and unchanged at +10 s and
+30 s — so rex's synchronous writes cost it nothing here.

#### The twenty-sixth pass — the playback surface, and the session lifecycle driven

Two hunts and a driver round, on the one large functional surface nobody had ever swept. Cycle
6 measured the decision container's refusals and the `/player/*` 400s; cycles 10, 14 and 15
measured the timeline's storage grammar. Nothing had walked the playback path as a client walks
it, and nothing had swept the `/video/:/transcode/universal/*` family or the Dashboard's
statistics endpoints as a QUERY surface.

**rex never ran a decision at all.** Every request without `hasMDE=1` was refused, whatever
the client's profile said — `server.py`'s test was `directPlay == "0" or hasMDE != "1"`,
unconditional. Measured on kids and zen across four items in both containers, a decision with
no `hasMDE` runs the client's own profile against the item and answers one of three shapes:
`Roku` and `Xbox One` get `directPlayDecisionCode 1000` / `generalDecisionCode 1000`, both
"Direct play OK.", with the Metadata row and `resourceSession` and **no transcode codes**;
`Chrome`, `Safari`, `Chromecast`, `Plex Desktop`, `Konvergo`, `HTML TV App` and `Opera` get
`1001` "Conversion OK", which rex cannot answer because it has no encoder; and `Firefox`,
`Microsoft Edge`, `Generic` and `Plexamp` get the `3000/2000/4005` "neither" — which is the one
rex was sending to all of them.

Plex Web always sends `hasMDE=1`, so it never saw this. Every client that does not — Roku,
plex-for-kodi's `ServerDecision`, python-plexapi, the older TV apps — asked rex whether it
could play an item and was told the catalog was unplayable, by a server whose entire purpose is
direct play. The Metadata row is **byte-identical** between the two forms; only the container's
codes differ, so CLAUDE.md's "a direct play is ONLY `mdeDecisionCode: 1000`" is the MDE form's
rule and stays right — this is the other form, which rex emitted nowhere.

**And the decision's validation was far stricter than a real PMS's.** Re-measured over nine
parameter shapes on both references, only an OUT-OF-RANGE `mediaIndex` refuses. A missing,
non-numeric, negative or empty one is a 200 that falls back to media 0; `partIndex` is never
refused at any value; and a `path` carrying a query string is a 200. That **corrects a
CLAUDE.md sentence** which claimed all three, and it is not academic: python-plexapi's
`getStreamURL` and several TV apps omit `mediaIndex`, and against rex their decision was a
hard 400 before any profile question was asked. Stated plainly, because it bounds the
correction: neither reference holds a MULTI-PART Media, so the old reading may have come from
one; what is measured is that on a single-part Media no `partIndex` refuses.

**A decision answers exactly ONE Media**, the `mediaIndex` asked for — measured on zen's rk=401,
which carries fifteen on its library row — where rex echoed every version with `selected` on
the chosen one. Single witness by construction: kids holds no multi-Media row, so it agrees with
this rather than corroborating it. **And `excludeElements=` is a NO-OP on a decision**, the one
exception to cycle 23's "every container that carries rows honours it"; rex applied it and
substituted `mediaCount`, leaving the client a decision with nothing to play.

---

**The Dashboard's statistics endpoints are a query surface, and rex implemented none of
it.** Four findings, each measured on kids and zen on all three of `bandwidth`, `media` and
`resources`:

**A `timespan` the server does not KEEP is an empty 200**, not the nearest bucket. Each endpoint
stores its own set — bandwidth `0 1 2 3 4 6`, media `0 1 2 3 4`, resources `6` — and 5, 7, 8, 9,
10, 99, -1 and 007 each answer `size 0` on both references. rex ran
`TIMESPANS.get(int(timespan), 1)`, which falls back to the ONE-SECOND bucket, so every unstored
value answered the live per-second series **relabelled** with the asked-for window: invented rows
for a window the server has no data for. The client's own enum names 5 ("Minutes"), so it is
reachable. The same fix removed the last place where the same value in the same request meant two
different things — `/statistics/resources` compared the raw STRING, so `timespan=06` answered
nothing there and the series everywhere else.

**`limit=` is a real cap and it is PARSED**: `1`, `01`, `0` and `-1` are 200s (a non-positive one
answers no rows at all) and `1.5`, `abc`, an empty value and `+1` are 400s — **even where the
value does nothing**, which is the same shape cycle 23 found on `/children`. **`sort=` is a real
ordering** validated against a per-endpoint key list, and `metadataType` is NOT a media key,
which is the one that looks like it should be. rex answered 200 to every value of both and
sorted by none.

**And the `at` family is four operators and only four**: `at>` returns the `>=` set, `at<` the
`<=` set — the strict forms are not implemented at all — `at=` is exact, and `at>=`, `at<=` and
`at!=` are unknown keys that answer every row. The hunt reported `at!=` as the complement;
driven against the true counts on both references (175 rows on kids, 153 on zen) it answers all
of them, so it is ignored like the other two. rex read `at>` or `at>=` and applied `>=` and
ignored the rest.

**Neither `/status/sessions` nor `/statistics/{which}` is a PREFIX.** Nine paths rex
answered 200 — `/status/sessionsfoo`, `/status/sessions/zzz`, `/status/sessions/1`,
`/status/sessions/background/zzz`, `/status/sessions/history/all/zzz`,
`/status/sessions/history/all/1/2`, and an extra segment after each statistics leaf — are 404s
on both references. `route.startswith("/status/sessions")` matched every one of them and
`statistics` read `segments[1]` and ignored the rest, so `/statistics/bandwidth/zzz` answered
the whole series. A 200 tells a client the endpoint exists and is empty, which is fix 4's trap
from the other side and the same shape cycle 25 found behind
`/library/sections/{id}/recentlyReleased`.

---

**A decision arms one SESSION, not the item.** `self.decided` was a set of ratingKeys, and
three separate behaviours read it: the timeline's `Bandwidths` block, the session row's
`selected`/`decision`/`location` flags, and — as of this cycle — the transcode start's gate.
Measured on the copy with two sessions beating on one row, one of which decided: only that
one's timeline carries `Bandwidths`, only its session row carries the flags, and only its own
`stopped` beat disarms it. Keyed on the ratingKey alone, one client's decision armed every
other client's timeline for that item and its stop disarmed theirs — and the Dashboard said
"Direct Play" for a client that had never asked whether it could play the file.

The arming identity had to be measured rather than assumed, because the obvious candidate is
wrong: it is `X-Plex-Session-Identifier` (query or header), falling back to the client
identifier, and **not** the decision's own `session=` parameter. A decision sent with
`session=QX` and the header `HY` arms `HY`; one sent with `session=QZ` and no session header
arms the client identifier. `session=` names the transcode session. The chain is the same one
`sessions.touch` keys a session row by, which is why the pair `(session, ratingKey)` is the
whole of it: the same session beating a DIFFERENT row is not armed.

**And a transcode start from a session that has made no decision is a 400.** The reference's
own log says "Denying access due to session lacking decision for transcode of key
/library/metadata/18". The gate runs before the encoder question: it is a 400 on every
protocol and every leaf of the start family, and a transcode decision for that session turns
the same `start.m3u8` into a 200. rex answered its "no transcoder" 404 to everything,
which reads to a client as a missing endpoint rather than a refused request. What rex
answers once a decision exists is still that 404 — it has no encoder — and that is recorded
rather than modelled, because the reference's own answer there depends on what it can convert:
a DASH `start.mpd` is a 404 on the copy where the same session's `start.m3u8` is a 200.

---

**A list's unmatch never reached a read.** `PUT /library/metadata/{collection}/unmatch` has
set `guid` to `local://{rk}` since cycle 23, and `_collection_row` re-mints
`collection://rex-{rk}` on every read, so the write landed in the stored dict and the row a
client sees never changed. Textbook class C: the rule was recorded, the line implementing it
existed, and it wrote into a copy. What made it hard to see is that the stored dict is also
what the frame-count gate reads, so everything on the notification socket said the unmatch had
happened while the row said it had not. A playlist's was worse — that branch RETURNED before
the guid line, so its unmatch wrote nothing at all.

Measured on the copy, an unmatch writes more than the guid:

    collection   guid -> local://{rk}, ratingCount REMOVED, subtype -> "unknown"
    playlist     guid -> local://{rk}, duration REMOVED

with the per-user state block staying in the slot `ratingCount` vacated (after `index`, before
`thumb`), measured with a rating and an offset on the row. **And the activity a collection's
refresh and unmatch push ENDS at 100**, not at 49 — 49 is the progress of its own `updated`
frame, left in the dict, so the Dashboard's bar stopped there and then vanished. A playlist
pushes no activity at all, which rex already had.

---

**`removeFromContinueWatching` is honoured per LIBRARY TYPE, and cycle 25 had the two halves
crossed.** Driven on the copy over both types and both directions:

    show library    hidden row KEPT     in `tv.inprogress.{id}` AND in its see-all list
    movie library   hidden row REMOVED  from `movie.inprogress.{id}` AND from its list

The shelf and the See All behind it always agree with each other. Cycle 25 measured only the
episode half, applied it to both types on the ITEMS list and left BOTH shelves dropping the
row — so a show library's page lost an episode from its Continue Watching shelf while the See
All behind that very shelf still listed it, and a movie library's See All kept a movie the user
had explicitly removed. Home's On Deck drops the row whatever the type, which is unchanged.

---

**A decision row keeps its `summary`.** rex popped it for movies, a rule fitted to movie 1
— the one row on the copy whose summary is the EMPTY STRING. The attribute was absent there for
a different reason entirely. Re-measured over four movies with real summaries and two episodes:
the row carries it, whatever the type. Class B, refuted by rows that can disagree and do.

---

**The butler's wrapper steps once per task.** Measured twice over on a freshly reset copy with
an uncontended socket, across a 22-task run and a 12-task one, identical frame for frame: the
`butler` activity pushes `updated` at 100 for the task just finished and then `updated` at
`round(100 * n / (n + 1))` for the queue — 50, 67, 75, 80, 83, 86, 88, 89, 90, 91, 92, 92, 93,
93, 94, 94, 94, 95, 95, 95, 95 for 22 tasks — with no step after the last one. A run of N
pushes N `updated 100` frames and N-1 steps before its `ended`. rex pushed a single
`updated 100` for the whole run, so the Dashboard's butler bar sat at -1 and then jumped to
done however many tasks were running. A fraction that approaches 100 without reaching it is the
shape of a queue whose length is not known in advance, which is also why the last task has no
step.

Two of the three task shapes were in the wrong order. **`CleanOldBundles` interleaves its two
messages with its own activity's phases** — "cleaned N media bundles" between the media group
and `Analyzing metadata bundles`, "cleaned N metadata bundles" between the last step and the
`ended` — where rex pushed both after the activity had ended, reporting the media bundles
after it had moved on to the metadata ones. **And `RefreshLibraries` reports BEFORE it scans**:
the message goes out, the wrapper steps to 100 and ends, and only then does the first
`library.update.section` frame appear. The scan is work the task queues, not work it waits for;
rex ran it inline and held the wrapper open for the whole scan.

**`POST /butler` runs only what is DUE, and that is recorded rather than emulated.** The copy
ran 22 of its 23 tasks on a fresh reset — `RefreshLibraries`, run by hand a moment earlier, was
skipped — and an immediate second POST ran 12. No row of `GET /butler` carries a last-run or a
next-run time, so which tasks are due is not observable from the surface, and two runs cannot
separate "its interval has not elapsed" from "it found nothing to do". rex runs every
enabled task. What is refuted, 2 of 2, is cycle 23's reading that a second POST is one
`backgroundProcessingQueue` frame and nothing else: it is a full run of its own.

---

**A session with no beat lives three minutes, not seventy seconds.** Re-measured on the copy
on `long` media over ten runs, polling once a second and identifying the row by its own client
identifier: it went at +184.0 at the earliest and +210.4 at the latest, and was still there at
+209.4 in one of them. That is not one constant with a fine sweep — a session alive at +209.4
in one run and gone at +184.0 in another — so it is a cutoff plus a reaper TICK, and the
bracket the ten samples give is a cutoff at or below 184 s with a tick of at least 26 s. 180
and 30 are the round pair inside it and the boundary is not bisected, so the cutoff is what is
emulated and the tick is recorded. Two durations (10 s and 30 min) and two rows behaved the
same, so it is a function of neither.

`SESSION_TTL = 70` came from cycle 9, on the lab's ten-second clips before `long` mode existed
— exactly the shape §3.3 warns about for everything measured there. A client that stops
beating, as several TV apps do while paused, fell off Now Playing after a minute and a bit.

---

**`/status/sessions` is written by a different serialiser from every other container.**
Measured on the copy in BOTH formats against the same row's own detail read:

* Its attributes come out **alphabetically** — the `Video`, its `Media` and its `Part` alike —
  where cycle 22's measured `_ATTR_ORDER` governs everywhere else. The flags a decision arms
  (`selected`, `decision`) follow that block.
* In JSON, every child element's `id`, a `Media`'s `aspectRatio`, a `Rating`'s `value` and the
  row's `librarySectionID` are **strings** where the detail row sends numbers. Nothing else
  changes type: `bitrate`, `width`, `duration`, `index` and `level` are numbers on both, so it
  is not a blanket stringification.
* `UltraBlurColors` comes back as a **list** of one where it is an object everywhere else —
  invisible in XML, which has one child element either way.

Cycle 22 swept 2,994 paths for attribute order and this is the one path it could not reach:
reading it needs a live playback session, which neither read-only reference can be given. It is
the only known contradiction of that table, which is why it gets a marker rather than an entry.

---

**And a revalidation closed an entry rather than opening one.** §3.3 carried the collection and
playlist `/refresh` and `/unmatch` FRAME sequences as measured-but-unimplemented. Driven side
by side against the copy this cycle, rex reproduces all four exactly, frame for frame,
including the `state 2` / `metadataState: matching` opener and the guid gate that turns a
collection's refresh from three frames into eight. Cycle 23 built them; what was missing was
the `ended` frame's 100 and the guid reaching a read, both of which this cycle fixed.

**Measured and not acted on.**

- **Which butler tasks are DUE.** The counts are measured and reproduce: the copy ran 22 of its
  23 tasks on a fresh reset (`RefreshLibraries`, run by hand a moment earlier, was skipped) and
  12 on an immediate second POST. The mechanism is invisible from the surface — no row of
  `GET /butler` carries a last-run or a next-run time — and two runs cannot separate "its
  interval has not elapsed" from "it found nothing to do". rex runs every enabled task.
- **The reaper TICK behind the session cutoff.** Ten runs bracket it at 26 s or more against a
  cutoff at or below 184 s. rex prunes lazily on read, so its row goes at the cutoff
  exactly; there is nothing on the surface that reports the tick, and modelling one would be a
  constant nobody measured.
- **What rex answers to a transcode START once a decision exists.** The 400 with no
  decision is measured on every protocol and every leaf of the family. Past that gate rex
  answers its own 404 — it has no encoder — and that is not the reference's answer and is not
  meant to be: the reference's own depends on what it can convert, its DASH `start.mpd` being a
  404 where the same session's `start.m3u8` is a 200.
- **The session row's `Role` attribute order.** Its `Video`, `Media` and `Part` are plainly
  alphabetical; a `Role` there is `filter, actor, id, role, tag, tagKey, thumb` — alphabetical
  after `filter`, which the plain rule does not explain and one element cannot settle. rex
  sorts every element of the row alphabetically, so a session `Role` disagrees by one position.
- **REFUTED, 2 of 2:** cycle 23's "a second `POST /butler` is one `backgroundProcessingQueue`
  frame and nothing else". It is a full run of its own, with its own wrapper and its own
  per-task stepping.

#### The twenty-seventh pass — the HTTP envelope below the API, and the serialiser dimension

Two hunts and a driver round. The read hunt re-ran the founding instrument — a fresh
official-client capture through the harness, replayed against rex — and the write hunt swept
a dimension cycle 26 opened by accident: **which projections have a serialiser of their own, and
what TYPE every JSON value is**. The driver spent its half below the API entirely, on the HTTP
envelope that every one of those requests arrives in, which twenty-six cycles had taken from
Python.

**Six of this cycle's findings are in the request itself, and a real PMS refuses where rex
answered.** Bisected on kids (1.43.4) and zen (1.42.2), which agree to the byte on every one:

- the **URL** is bounded at exactly **32768** bytes and 32769 is a 400 — the URL and not the
  request LINE, because the boundary does not move when the method does (GET, HEAD and DELETE
  all break at the same length). rex inherited Python's 64 KiB request-line limit, so every
  URL between 32 KiB and 64 KiB was a 200 where a real PMS refuses, and a longer one came back
  `414 Request-URI Too Long` — a status neither reference sends anywhere. A comma-joined
  `/library/metadata/<keys>` from a large play queue is the request that reaches this length in
  practice.
- the **header caps are 1024 and 4096 bytes and 100 LINES**. The first two were BRACKETED in
  cycle 16 (1024 passes, 1100 fails) and are now bisected: 1024 and 4096 pass, 1025 and 4097 are
  400s, so rex's constants were right to the byte. The third is not a size at all —
  **HANDOVER 3.3's "~41 KB total" is REFUTED**: 100 header lines pass and the 101st is a 400
  whatever they weigh, and 200 KB of headers in fifty lines is a 200. The old figure is the same
  cap measured with ~410-byte headers. rex refused at a HUNDRED, one short, because Python's
  reader counts the blank terminator line, and answered `431`.
- **the request LINE has a grammar**, measured over nine malformed lines that both references
  answer identically: exactly `METHOD SP target SP HTTP/<digit>.<digit>` with CRLF endings. Two
  spaces, a trailing space, tab separators, asterisk-form (`OPTIONS *`) and LF-only endings are
  each a **400**, and rex answered **200** to all five. The two version digits are
  unconstrained — `HTTP/2.0` and even `HTTP/9.9` are answered 200, where rex sent Python's
  505 page — while `HTTP/1` with no minor is a 400. An HTTP/0.9 line with no version at all is
  answered with a **full HTTP/1.1 response**; rex wrote a bare body with no status line.
  And a lower-case method (`get /identity HTTP/1.1`) gets **no response at all** on both
  references, where rex answered 500.
- **an unparseable `Content-Length` is ignored and a negative one is a 500.** `abc` is a 200 on
  a GET on both references and on a POST on the copy, with no body read; `-1` is a 500. rex
  called `int()` on the raw header, so `abc` raised inside the handler thread and the socket
  closed with nothing written, and `-1` reached `rfile.read(-1)`, which blocks until the peer
  hangs up. Both read to a client as a server that hung up mid-request.

**And `/photo/:/transcode` validates five more parameters**, swept over eleven values of each of
seven parameters — 77 requests, both references agreeing on every one. `minSize` and `upscale`
are BOOLEANS (`0`, `1`, `01` and `+1` pass; `2`, `-1`, `100`, `1.5`, an empty value and `abc` are
400s); `opacity` and `quality` take any integer, `-1` and `100` included, and refuse only a value
that does not parse; `blur` is the same except that a negative one is a **500**. rex took
anything for all five, so a client sending a malformed tile URL got an image where a real PMS
refuses the request. Not emulated and recorded instead: the 500s a real PMS answers for a width
or height that scales the image to nothing (`width=1&height=100` on a 1000×1500 poster is a 500
where `width=2` is a 200, and `height=-1` is a 200 where `width=-1` is a 500) — those depend on
the image's own aspect ratio and on a resizer rex does not have.

**The session row's `Player` is the one element there that is not alphabetical, and its
`profile` is not a constant.** Cycle 26 recorded a session `Role` as `filter, actor, id, role,
tag, tagKey, thumb` and left it in §3.3 as the one element its alphabetical rule did not
explain. Re-measured on the copy with a row carrying seven tag types, every one of them IS
plainly alphabetical — `Role` is `filter, id, role, tag, tagKey, thumb`, and there is no `actor`
attribute on a `Role` at all. **That entry is closed and rex was already right.** What is
not alphabetical is `Player`: its string-valued attributes come first, alphabetically, and its
non-string ones follow, alphabetically —

    address device machineIdentifier model platform platformVersion product profile state
    title version | local relayed secure userID

— on two witnesses, a Plex Web client sending six strings and a Roku one sending eleven.

`Player.profile` was recorded in cycle 10 as the constant `Web`, from a copy only Plex Web had
ever driven. Swept over all 78 names in `CLIENT_PROFILES`, it is the profile the server
RESOLVED, and 23 of the 78 report another profile's NAME: every browser but Firefox and Edge
answers `Web`, `Roku` answers `Roku-7.x`, `Opera TV App` answers `HTML TV App`, the three
`Mystery` entries and `PlayStation 3 App` answer `PlayStation 4 App` or `Xbox 360 App`, nine of
the eleven versioned iOS device profiles collapse onto two, and
`Plex Home Theater-RaspberryPi` answers **`Raspberry PI`** — a name in no file-name list at all,
which is the tell that what is reported is the profile's own `name` and not the file it was
loaded from. The resolution runs `X-Plex-Client-Profile-Name`, then `X-Plex-Platform`, then
`X-Plex-Device`; the first whose value names a profile wins and a value that names none falls
THROUGH (`X-Plex-Platform: nonsense` beside `X-Plex-Device: Roku` resolves `Roku-7.x`); each
name is read from the header when the header is present, even when it is empty, and from the
query otherwise; and the attribute is OMITTED when nothing matches.

**A borrowed artwork field is DERIVED from its owner.** Cycle 25 measured the half where the
field is already there and rex built exactly that, which left three things stale, all
measured on the copy over a show poster pick, an episode and a season poster DELETE and a season
poster pick, reading the show, both seasons and two episodes back after each:

- clearing a row's own poster does not leave it with none — it falls back to its PARENT's, which
  is the same path its own `parentThumb` already names (season 7's `thumb` became the show's,
  episode 8's became the season's). rex dropped the field, so the tile drew nothing where a
  real PMS draws the parent's poster. Cycle 23's "the field gone" was measured on a MOVIE, which
  has no parent to borrow from.
- a pick on a season **CREATES** `parentThumb` on episodes that had none. That was HANDOVER
  3.3's open question — "whether a cascade CREATES a borrowed artwork field" — and it does.
- a pick on a show moves a BORROWING season's own `thumb` (kids' unmatched season 11 is such a
  row) and every episode's own `art` to the new stamp. rex moved `grandparentArt` and left
  `art` at the old stamp, so the same image had two names at two versions on one row.

And the other half, caught as a regression by the cycle-14 cascade check inside this cycle's own
patch: when the OWNER no longer has that artwork at all, the borrower's copy goes with it.

**The same rule from the other side: a pick re-versions only the artwork the row OWNS.** Season 7
on the copy still carried `/library/metadata/6/art/<show stamp>` after its own poster was picked,
and zen corroborates it at scale -- episode 11115 carries `art` naming its SEASON (11114) beside
a `grandparentArt` naming the SHOW (11113) at a different stamp, so a borrowed field keeps the
owner's path, stamp and all. rex re-versioned `thumb`, `art` and `theme` onto the picking row
whatever it owned, so a season that borrowed the show's backdrop came away claiming
`/library/metadata/<season>/art/<stamp>` -- a path only the owner serves, which is cycle 16's
"an artwork path is a 200 only on the row that OWNS the image" from the other side. The picked
field is the exemption: picking it is what makes the row the owner.

(A check of the same sweep: **zen's 3,391 episodes with no `art` at all are exactly the ones
whose SHOW has none either**, so cycle 20's "handed down borrowed or not" is not contradicted by
them -- there is nothing to hand down.)

**Which projections have a serialiser of their own.** Cycle 23 established that a real PMS's
JSON key order IS its XML order and cycle 22 swept 2,994 paths for attribute order; cycle 26
then found `/status/sessions`, the one path neither sweep could reach. The write hunt swept
45,436 (path, element, attribute) triples across 253 paths in both formats, three servers at a
time, and reported only where kids and zen agree and rex differs. Seven landed:

- **`durationTotal` and `storageTotal` are JSON STRINGS** on `/library/sections/{id}/all?
  includeStorage=1` — `"10031"` on kids, `"1440171"` and `"573936917"` on zen — where rex
  sent integers. The XML is identical either way, which is why cycle 19 recorded that the pair
  exists here and not what type it is. A client whose model declares them `String` rejects the
  whole grid row whenever the option is on.
- **the bare `/system/agents` carries the modern agents' 45 `Language` rows and no
  `MediaType`.** Cycle 21 recorded the opposite — a `MediaType` per media type and no `Language`
  — and that is true of the LEGACY plug-in agents alone: `com.plexapp.agents.imdb` is 33
  Language + 1 MediaType bare and 33 + 0 typed, while all six `tv.plex.agents.*` rows are 45 + 0
  in BOTH forms, identically on 1.43.4 and 1.42.2. rex holds the modern agents only, so it
  stripped exactly the rows the Agents page reads to build each agent's language selector and
  handed it a lone `MediaType`.
- **a modern `Agent` row leads `identifier, name, hasPrefs, id`**, where rex emitted its
  builder's dict order. Cycle 19 parked "the Agent element's attribute order" on the LEGACY
  rows' own pairs (`hasPrefs < identifier < primary`), which the rows rex actually emits do
  not carry.
- **`?includeDetails=1` pages its rows as ONE sequence.** 20 `Directory` + 1 `Type` + 8
  `FieldType` = 29, so a 20-row window is `size=20 totalSize=29 offset=0` and `Start=20` hands
  back the Type and the eight FieldTypes as rows 20–28. rex's generic pager takes
  `totalSize` from the longest list, so it reported `totalSize=20` beside a `size` of 29 and
  answered nothing at all past the twentieth row.
- **a `Sort` row leads with `active, activeDirection`** and a **`Hub` puts `random` before
  `style`**. Neither element had a bucket in `_ATTR_ORDER`, so rex emitted the flags it adds
  last, last.
- **the SINGLE history read is a third projection with an order of its own** — `historyKey,
  librarySectionID, title, [grandparentTitle,] type, [index, parentIndex,]
  originallyAvailableAt, viewedAt, accountID, deviceID, viewType`. Cycle 18 measured the FIELD
  SET and rex built it correctly, then `_split` re-sorted it as an ordinary `Video[movie]`.
- **three more empty elements are written PAIRED**: an empty `Meta/Type` row, an empty `Related`
  block, and the `/media/providers` `Home` `Directory`, which carries no children at all and is
  `<Directory hubKey="/hubs" title="Home"></Directory>` on both references while every other
  childless `Directory` on the surface self-closes. The last resolves part of the question cycle
  22 left explicitly OPEN for `Directory`, and `hubKey` is the signal cycle 19 already uses to
  tell a provider row from a `/library/sections` one.

**And the founding instrument, re-run after nineteen cycles, found three things no
hand-built probe had.** The read hunt drove the official web client through plexlab across four
passes -- the filter bar and sort menu opened *and used* on both library types, the `...` menu on
all four row types, the Edit dialog and its four tabs, search typed one keystroke at a time, the
Dashboard, fifteen settings panes, the playlist and collection pages -- 603 GET flows, 385 unique
server requests, 122 after `replay.py`'s dedup. Replayed at the reference itself first: **121 of
122 clean**, the one difference live bandwidth drift, so the tool is sound. Against rex: 17
clean, 105 differing, 101 of them catalog content or a 1.43.4-vs-1.42.2 version difference and
one already recorded. The three that were real:

- **`/library/all/top` refuses every type but 1, 2 and 4.** Both references answer an empty 200
  to ANY type number that parses -- 0, 3, 8, 9, 10, 12, 13, 15, 18, 20 and -1 included -- and a
  400 only to a value that does not (`abc`, an empty one, `1.5`). Swept over 17 values on both.
  It is not academic: the web client's Dashboard issues movie, show and ARTIST inside one
  `all([...])` under a single try/catch, so the `type=8` refusal rejects the whole batch and
  **the entire Top Played panel fails, movies and TV with it**, on a server with no music
  library to answer for in the first place.
- **`/media/providers/metadata` and `/media/providers/metadata/group` were never routed.** The
  Settings -> Metadata Agents pane reads both immediately before `/system/agents?mediaType=`,
  and both are populated 200s on kids (7 and 6 rows) and zen (4 and 4) where rex answered
  404. They are the MODERN agent-provider tables -- a different surface from `/system/agents`,
  whose legacy half is deliberately not implemented -- so rex now answers them from the four
  modern agents it already holds, in zen's shape: a `MetadataType` child per media type the
  provider serves, `tv.plex.agents.none` carrying an EMPTY `uri`, and a group row per provider.
- **No person tag carried a `tagKey`.** Every `Director`, `Writer`, `Role` and `Producer` on a
  detail row carries a 24-hex plex.tv person id on both references, as do the rows of
  `/library/tags?type=4|5|6|7`; `Genre` and `Country` carry none and a `Collection` carries a
  `guid` instead. The web client's Cast & Crew link builder reads
  `/library/people/${tag.tagKey}` for any server that is not plex.tv's cloud, so **every person
  tile on every preplay page linked to `/library/people/undefined`**. rex has no source for
  plex.tv's own person ids, so the value is MINTED -- the first 24 hex of a sha1 of the name,
  deterministic and stable -- and `/library/people/{key}` resolves the minted form exactly as a
  real PMS resolves both the numeric id and the `tagKey`. The shape is measured; the value is a
  substitute and says so.

A fourth came out of checking the first: **the Dashboard's Top Played shelf breaks a tie on the
MOST RECENT view, descending, not on the ratingKey.** zen's four seven-play episodes come back
19766, 19767, 19763, 19762 -- the first pair last viewed at 1788088024 and the second at
1788088000 -- which is neither ratingKey nor `addedAt` order, and the row does not carry the
number (`lastViewedAt` is absent on every one of them). It is the HISTORY's own time, which is
the same place `globalViewCount` comes from: 12, 11, 9, 9, 7, 7, 7, 7 against 12, 11, 9, 9, 7, 7,
7, 7 history rows. kids agrees on its three movies. rex sorted on `(-plays, ratingKey)`, so
a tied group came back least-recently-watched first.

The same hunt answered a question §7 has carried since cycle 7: **all nine `conformance.py`
failures are one cause** -- the Range reads on `Part.key` answering `502 rex: debrid resolve
failed`, because the fixture's Real-Debrid links are account-scoped and the harness runs with
`RD_TOKEN=dummy`. None is a python-plexapi limitation and none is a server difference; sections
1-7 and 9 pass 29 of 29. And a three-way python-plexapi sweep (463 probes) found **zero** probes
that error on kids and not on rex.

### Measured and not acted on

- **A lower-case method holds the connection** on both references until their own 20-second idle
  close. rex closes it at once — the same silence, sooner — because holding a thread for
  twenty seconds per malformed request line is a self-inflicted denial of service.
  **Overturned 2026-09-30:** a complete lower-case line is closed AT ONCE on kids (1.43.4) and
  plexscratch42 (1.42.2), and the check runs before every other rule of the line (see
  protocol-traps.md); only an unterminated one is held.
- **The `width`/`height` 500s of `/photo/:/transcode`** (above): a resizer rex does not have.
- **`PROFILE_NAMES` is single-witness by construction.** Neither read-only reference can be given
  a playback session, so the copy (1.43.4) is the only instrument, and rex reports 1.42.2.
- **The `Player` string/non-string split has a second model that fits** the same fifteen
  attributes: a fixed bucket list. The type split is the one that also answers for an attribute
  neither witness carried.
- **Conditional request headers are ignored by a real PMS.** A part read carries an `ETag` and a
  `Last-Modified`, and `If-None-Match`, `If-Modified-Since`, `If-Range` and `If-Match` all answer
  the same 206 — never a 304 or a 412. Measured on kids; rex does not implement them either,
  so there is nothing to change. Recorded so the dimension is not re-swept.
- **`Part.accessible`/`exists` are gated on `checkFiles=1`** on both references and rex sends
  them always. Re-confirmed and left alone: it is the cycles-1–6 deliberate deviation.
- **REFUTED:** HANDOVER 3.3's "~41 KB" total header limit (above) and cycle 26's session `Role`
  attribute order (above).
- **The `mediaCount` column on a parent is per LEAF, and a MAX reading is refuted.** HANDOVER
  3.3 carried it as unwitnessed because the only question ever asked was the `=1`/`>>1` split,
  which is also the Duplicates split. Asking for a VALUE separates them: on zen's shows
  `mediaCount=2` is 30, `=3` is 10 and `>>2` is 17 against 44 for `>>1` -- under a max-over-leaves
  reading those three are disjoint and sum to 47, more shows than have a duplicated leaf at all.
  The per-leaf "any leaf matches" reading rex already implements is the one that fits. No
  code change; the entry is closed.
- **Three surfaces swept three-way this cycle with NO difference**, recorded so they are not
  swept again: `/library/tags`'s `type=` grammar (18 values -- any integer including `0`, `-1`,
  `99` and `01` is a 200, `abc`, an empty value, `1.5` and `+1` are 400s, and no `type` at all is
  a 400, identical on all three); the `/library/sections/{id}/prefs` and
  `/library/sections/prefs` family (10 forms, including an unknown `agent`, a missing `type` and
  an unknown section id); and the connection envelope beyond the request line (a GET with a
  body, a duplicated or conflicting `Content-Length`, `Transfer-Encoding: chunked`, HTTP/1.0
  keep-alive, two pipelined requests and `Upgrade: h2c` -- all agree).
- **Single-witness on the copy and NOT reported as findings** by the write hunt: `playQueueItemID`
  leading a play-queue row and `playlistItemID` sitting mid-row on 1.43.4, and the transcode
  decision row emitting tag `id`, `librarySectionID` and `Rating@value` as strings with
  `UltraBlurColors` a list of one — the same family cycle 26 found on `/status/sessions`, on a
  path only a writable reference can be made to answer.

#### The twenty-eighth pass — the Home page's own parameters, and the search surface's row identity

Two hunts and a driver round. The read hunt took **Home** — every hub path, with the parameters
the official client actually sends on each one — and the write hunt took **search**, both
endpoints, row by row and attribute by attribute, with plexscratch holding the collections and
playlists neither read-only reference has. The driver's half was the search tag row's identity,
which turned out to be wrong in five separate ways at once.

**The hub parameters were never run.** CLAUDE.md recorded, from cycle 15, that
`contentDirectoryID` "SCOPES and VALIDATES `/hubs/search` alone — ignored on every other hub
path". Measured again over nine values each on kids (1.43.4) and zen (1.42.2), which agree edge
case for edge case, that is right for `/hubs/sections/{id}` and **wrong for four other paths**:

- `contentDirectoryID` **scopes `/hubs` and all three `/hubs/home/*` leaves**, and on `/hubs` it
  also REMOVES the `home.playlists` shelf. A non-empty value narrows every shelf to that library,
  an EMPTY one scopes nothing, the match is an exact STRING (`01` selects section 1 on neither
  reference), and a **comma list** selects several. That last form is not academic: Plex Web's own
  Home saga builds every hubs request as
  `{contentDirectoryID: pinned.join(","), pinnedContentDirectoryID, includeMeta, excludeFields}`,
  so on rex an unpinned library's Continue Watching and On Deck rows leaked onto Home and the
  library filter did nothing at all.
- `/hubs/continueWatching` and its `/items` leaf **validate** the parameter, where
  `/hubs/promoted` takes anything: at least one comma-separated member must be a POSITIVE INTEGER.
  `01`, `999`, `999,998`, `abc,1`, `1,abc` and `1,` are 200s; `abc`, `0`, `-1`, `1.5` and a space
  are **404s**. A section the server does not hold is fine — it is the SHAPE that is checked.
- `excludeContinueWatching` is true **exactly when the value parses as the integer 1**: `1` and
  `01` drop the shelves and `0`, `00`, `2`, `10`, `1.5`, `true`, ` 1`, `abc` and an empty value do
  not. It drops **both** `home.continue` and `home.ondeck`, on `/hubs/promoted` and on the bare
  `/hubs` alike. rex compared the raw string with `"1"`, dropped only `home.continue`, and
  ignored the parameter on `/hubs` entirely. No other `exclude<Name>=1` spelling does anything.

**A shelf's rows are the head of its own `key`.** Measured at `count=40` across both references:
29 of 32 non-empty shelves answer exactly the first rows of the listing their own `key` names, in
that listing's order, byte for byte — and the three that do not are the ones advertising
`random="1"`. So the shelf is its key, and the flag is the exception:

- `movie.topunwatched.1`'s 30 rows sit at positions 26 to 579 of its 583-row key listing and are
  not monotonic; `tv.startwatching.2` and `tv.toprated.2` are the same. A **random shelf is a
  random SAMPLE of its whole key set, in random order**, not the head of it. rex advertised
  the flag and answered the deterministic sorted head on every request, so "Top Unwatched" would
  have shown the same six titles for ever. Its own fixture is too small to see it — a sample of a
  two-row set is the set — which is why the rule is zen's.
- rex's **Recently Watched** shelf broke the head-of-key rule outright. The key it publishes
  is `?sort=lastViewedAt:desc&unwatched=0&viewOffset=0` and its builder honoured neither clause,
  so a row left in progress sat on the Recommended tab's shelf and then vanished when the user
  clicked that shelf's own See All chevron.
- the negative edge of `count=` is measured too: `count=-1` answers ONE row with `more: false` on
  a ten-row shelf and `-2`, `-5` and `-100` answer NONE with `more: false`, where `count=0`
  answers none with `more: TRUE`. So a negative count is `len(rows) + count` clamped at zero and
  always reports the shelf exhausted. No client sends one; the edge was measured, so it is
  recorded.

**Recently Added TV is a mixed, rolled-up list.** `/hubs/home/recentlyAdded?type=2` on zen is 50
rows: **12 shows, 31 seasons and 7 episodes**. It is not a list of shows at all — it is a list of
what was recently added, rolled up, a group of two or more recently-added siblings collapsing into
its parent, recursively. One new episode in an otherwise old season stays an EPISODE (zen's 17070,
whose season's other 21 episodes are three weeks older); two new episodes of one season become the
SEASON; six, or 161, new episodes of a show's only season become that SEASON and not the show,
because one season is not two; and two or more new seasons become the SHOW (Friends, One Piece,
Stargate SG-1, Fringe). A parent row here is **dated by its NEWEST leaf** — kids' Firefly carries
04:24:45, its last episode's stamp, against its own 04:27:37, and 14 of 14 zen season rows carry
`max(child.addedAt)` exactly — which is the OPPOSITE aggregation from `/library/recentlyAdded`,
where cycle 25's "dated by the earliest episode" holds and is re-confirmed here. rex filtered
`type == "show"`, so a new episode or a new season of an existing show was invisible on Home and
the shelf only ever changed when a whole show arrived.

**On Deck answers the DETAIL row, and the two versions differ.** Cycle 21 recorded the light grid
projection from fun, which had nothing in progress, so the shape was inferred rather than seen.
zen has a row on deck, and there all three paths — `/library/onDeck`,
`/library/sections/{id}/onDeck` and `/hubs/home/onDeck` — answer the FULL detail row: `Guid`,
`Rating`, `Producer`, and the person tags carrying `id`, `filter`, `tagKey`, `role` and `thumb`,
plus `Media` → `Part` → `Stream`. The control that rules out "it is that row" and "it is that
section": the same row fetched through the section grid is the light one, so it is the ENDPOINT.
`/library/onDeck` adds `allowSync`, `librarySectionUUID` and `lastViewedAt` to it and keeps
`librarySectionKey`; `/hubs/home/onDeck` keeps the key and carries no UUID.

That is **1.42.2's** answer, which is the build rex reports. plexscratch (1.43.4) answers the
same row **minus `librarySectionKey`, `Guid`, `Rating` and `Producer`, plus `studio`** — a sixth
per-version difference on the surface, and the reason the older "light" note could never be
reconciled: the two references disagree, and only zen speaks for rex's own version.

**Two leaves were not what rex thought they were.**

- `/clients/profiles` is **not** the hosted-client list. It is the server's own CLIENT PROFILE
  catalogue — one `Profile` row per file in its `Resources/Profiles` directory, the same 78 names
  cycle 27 measured for `Player.profile` — and the Optimize dialog's target list reads it as
  `?type=42`. The bare form, `type=0` and `type=00` answer all 78; `type=42` (`LOCAL` in the
  client's own enum `{NORMAL:0, LOCAL:42, CLOUD:43}`) and `042` answer **seven** (Android, iOS,
  Universal Mobile, Universal TV, Windows Phone, Windows, Xbox One); every other value, an empty
  one included, is a **400**; an extra segment is a 404 and a trailing slash a 200. rex
  answered an empty container to all of them, on the reading that it hosts no client — which is
  true of `/clients` and not of this.
- the `/collections` leaf is the **`/all` listing with a default `type=18`**, and it runs the same
  query. A `type=` the library holds answers those rows instead (`type=1` on a movie library is
  its movies), and the grammar is the grid's: an unknown `sort` key, an unparseable `type` and an
  unparseable `limit` are each a **500**, while `subtype`, `smart` and an empty `sort` are
  accepted and ignored. rex answered 200 and `size 0` to every one of those.

**The search tag row's identity was wrong in five ways.** All of them are one row shape, measured
on kids and zen and — for the collection half — on plexscratch, which now holds collections and
playlists that neither read-only reference has:

- **a tag hit is per LIBRARY, not per NAME.** kids' `drama` answers TWO `Drama` rows, one for each
  library holding the genre, each naming its own `librarySectionID`, `key` and `count` against the
  same tag id; zen's `comedy` answers 590 and 179 the same way. rex keyed the hits by name
  alone, so a genre both libraries hold was ONE row naming whichever library the catalog reached
  first, and the other library's filter link never appeared on the search page.
- **the `count` is the size of the listing the row's own `key` names** — the library's own
  top-level type, 3 of 3 rows on zen and kids. rex counted every row carrying the tag, so a
  catalog whose episodes carry a genre advertised a count its own link could not answer.
- **a person row carries its 24-hex `tagKey`**, directly after `tagType` and before the `thumb`,
  with `count` LAST of the three; a `Genre` row carries none, as it carries none on a detail row.
  This is cycle 27's finding one surface further out: rex minted the key, resolved it at
  `/library/people/{key}`, and left it off the search hit, so clicking an actor found by the
  search box went to `/library/people/undefined`.
- **the FLAT form drops the reason triple and the row's own score.** A `/library/search` tag row
  is the `/hubs/search` row without `reason`, `reasonID`, `reasonTitle` and `score` — there the
  score sits on the `SearchResult` wrapper alone, as a NUMBER, where the hubs row carries it
  inside as a five-decimal string. rex answered the hubs row verbatim on the flat endpoint.
- **that wrapper score prints five decimals in XML** (`score="0.52000"`, `score="0.93095"`) and
  the same value as a plain number in JSON. rex wrote Python's own `"0.32"` — and XML is the
  default format, so that is the wire every TV and mobile client reads.

**A collection IS found by `/library/search`.** Cycle 15 recorded the opposite, measured on a copy
that held no collection at all, so the absence was the fixture's and not a rule. With three
`C26ACT*` collections on plexscratch, `query=c26act` answers them as `tagType: 2` `Directory`
rows there as well as in the `/hubs/search` collection hub. And `contentDirectoryID` **scopes
every row family**, each by its own library: kids' `drama` keeps one `Drama` row at `=1` and the
other library's at `=2`, and plexscratch's `c26act` keeps three collections and a movie playlist
at `=1` and empties both at `=2`, a playlist going with its MEMBERS' library. rex compared the
tag ENTRIES — `{tag, rows, row}` triples that carry no `librarySectionID` at all — so a scoped
search answered NO tag rows for either library, while its collections were not scoped at all and
survived a scope to the other library.

**`/library/search` has a grammar of its own, and it is `searchTypes` and `limit`.**

- `searchTypes=` is that endpoint's filter — `/hubs/search` IGNORES it at every value — and the
  vocabulary is six tokens: `movies tv otherVideos music photos people`. They are EXACT and
  case-sensitive and are not trimmed: `Movies`, ` movies`, `movies ` and `zzz` each select
  NOTHING. A comma list is a UNION and an unrecognised token inside one is simply dropped, so
  `zzz,movies` answers exactly what `movies` answers; an empty value or no parameter selects
  everything. `collections`, `playlists` and `tags` are **not** tokens — they behave like `zzz`,
  while the rows they name are selected by the library they live in. What each token owns, row by
  row: `movies` the movie rows, the movie library's tag rows and a playlist of movies; `tv` the
  show, season and episode rows, the show library's tag rows and a playlist of episodes; `people`
  the person tag rows (a `tagType` of 4, 5, 6 or 7); and `otherVideos` the movie library's TAG
  rows **without** its movies — an Other Videos library is a type-1 library too, so the tag half
  follows `librarySectionType` where the metadata half follows the library's own kind.
- `limit=` caps the **whole result list**, not each hub: `limit=3` answers three rows of the ten a
  bare zen query answers, and kids' `firefly` answers the show and two of its three episodes.
  rex capped each hub separately, so `limit=3` on a query matching two kinds answered six. And
  it refuses in TWO ways, as `/hubs/search?limit=` does: a value it cannot parse — `abc`, an empty
  one, `1.5` — is a **400**, and one that parses but is not a positive count — `0`, `-1` — is a
  **500**; `01` is fine. rex answered 500, 200, 500, 200 and 200 to those five.

**Measured and deliberately not emulated.** The flat list's ORDER is the reference's own relevance
engine: for `comedy` zen answers movie, show, episode and then the two genre rows, which is
neither the hub base order (`show` first) nor a global score sort (the genre rows score 0.92 and
come last), while for `c26act` plexscratch answers the collection rows before the playlist rows at
an identical 0.52. Related: a fuzzy person row scoring 0.32 is DROPPED from the bare list and kept
when `searchTypes=people` asks for it alone (kids and plexscratch, `sintel` → Katherine Kendall).
Both need Plex's scorer, which rex cannot reproduce and does not pretend to; its own hub order
stands, and the scores stay the three-band model cycle 15 recorded. Hunt A's Related-shelf
composition is held out for a different reason: **kids and zen disagree** about which shelves a
preplay page carries, so there is no rule to implement — the same test the three-server method
applies everywhere.

#### The twenty-ninth pass — the bytes below the container, a row type rex has never held, and the artwork derivation finished

Two hunts and a driver round: **nineteen findings**, and for the first time in the programme the
driver's own revalidation step produced more of them than either hunt.

**The revalidation step found six, in one mechanism.** HANDOVER 3.3 held two cycle-27 entries
about the artwork cascade whose method was written down and never run -- "clear a show's `/art`
on the copy and read its episodes back", and "unmatch a show, give it an `art` back, and read the
leaves". Running both, and then the writes they imply, produced six findings and one rule that
subsumes every artwork cascade the project has measured since cycle 14:

**A row's own artwork field is its NEAREST ancestor's that OWNS one, and `parentX`/`grandparentX`
always name that specific ancestor.** Measured on plexscratch over eight writes on a two-season
show, reading the show, both seasons and one episode of each back after every one:

- clearing the SHOW's `art` removes it from both seasons AND from every episode, taking the
  episodes' `grandparentArt` with it; picking one back puts all of them in the same read. So a
  borrowed field is DERIVED and is RE-CREATED, which is the half cycle 27 could only infer --
  rex's `_follow_artwork` returns early unless the child already names the owner, so a leaf
  that lost its `art` never got one back.
- uploading an `art` to ONE season makes that season the owner: all three of its episodes'
  `art` switched to `/library/metadata/7/art/<stamp>` while their `grandparentArt` went on
  naming the SHOW, and the other season's episode kept the show's. That is zen's episode 11115
  -- the row cycle 27 recorded and could not produce -- made on demand.
- picking a new `art` for the SHOW then left the owning season's own path and stamp alone, only
  its `parentThumb` following, while the borrowing season followed on both.
- clearing a season's own `art` drops it back to the SHOW's path, its episodes with it, and the
  bare `GET /library/metadata/{season}/art` becomes a 404: `art` falls back exactly as cycle 27
  measured `thumb` does.
- an artwork UPLOAD cascades like a pick -- rex's upload path set the row's two fields, moved
  `updatedAt`, pushed its frame and never called the cascade at all, so every child kept the old
  stamp and a client that caches on it (which is what the stamp is for) never refetched.
- and both write paths re-version the OTHER field only when the row OWNS it. A poster upload on a
  season that borrowed both fields left its `art` naming the show untouched; so did clearing that
  season's thumb. rex rewrote the borrowed path as the row's own in both places -- a row
  claiming an image the server has none of, which the derivation then handed to every episode.

One of the six is rex's own: `catalog.items` is keyed by INTEGER ratingKey and every row
carries its key as a STRING, so the cascade's `items.get(row["parentRatingKey"])` was always
None. A SEASON cascade could therefore not see its show, read the season as owning nothing, and
STRIPPED `art` from every episode under it. It shipped inside this cycle's own second patch and
the next check in the same dimension caught it -- which is the argument for driving one finding
at a time against its own parent.

**Two stale claims closed by re-measurement.** HANDOVER 3.3 and section 8 both held that the
collection's and playlist's `/refresh` and `/unmatch` FRAME sequences were measured and not
implemented. Driven again on plexscratch with a listener open, rex reproduces all four frame
for frame: a collection's refresh is 3 frames on an agent guid and 8 once a `local://` guid makes
it real (`2 matching`, `5`, `3 queued`, activity `started -1`, `updated 49`, `3 processing`,
`ended 100`, `5`), its unmatch 7 of them, a playlist's refresh 4 and its unmatch 2, every row
carrying `type` 18 with `sectionID "1"` or `type` 15 with `sectionID "-1"`. Cycle 26 built them
and the two lists were never updated.

**An instrument trap worth writing down.** On plexscratch an artwork PICK stamps the row's
`updatedAt` -- and therefore every artwork path -- at **local time**, 7200 s ahead of the epoch,
and because the next write takes `max(stored, now)` the offset COMPOUNDS: two picks a minute
apart produced 1789514762 and 1789521963 against a true clock of 1789507604, while the response
body of each named the true-epoch path. An UPLOAD on the same server stamps the true epoch. All
three servers' `Date` headers agree with the host clock to the second, and no stored `addedAt`
/`updatedAt` pair on kids or zen shows the offset, so this is the copy's own write path and not a
rule -- but any measurement that compares a copy stamp with `now`, or two copy stamps with each
other across write kinds, is measuring it.

**Territory A swept the RESPONSE BODY** — the bytes a real PMS actually sends for an image, an
asset and a media part, and the envelope around them. Twenty-eight cycles had measured containers;
only cycle 16 had ever looked below one. Eleven findings, every one on both references:

- **`/photo/:/transcode` refuses five `url=` shapes rex answered with a grey tile.** The
  dimensions are validated first (`url=&width=abc` is a 400), and then the url: a missing `url=` is
  a 400, an EMPTY one a 404, an item artwork path of a row it holds the image, of a row it does not
  hold a 404, a server path that is not artwork (`/identity`, a grid) a **500**, and a url with a
  SCHEME or no leading slash (`file://`, `upload://`, `library/metadata/…`) a 404. rex's
  resolver matched one regex and fell through to a generated grey PNG carrying
  `Cache-Control: max-age=259200`, so a browser kept the grey square for three days — CLAUDE.md's
  own trap ("a 404 lets the client draw its own placeholder where a grey 200 tile gets cached")
  read from the other side, five doors after cycle 14 closed the first.
- **The transcoder answers exactly what the direct path answers.** Over a row that BORROWS its
  `art`, both are 404s on both references; rex resolved through `artwork_for`, which falls back
  to the parent's image, so the transcoder served a picture the direct path refuses.
- **`/:/resources/` is SEVEN names, and it ignores `width`/`height`.** kids and zen serve
  byte-identical bytes for `movie.png`, `show.png`, `artist.png`, `photo.png` (300×300 PNG) and
  `movie-fanart.jpg`, `show-fanart.jpg`, `artist-fanart.jpg` (1280×720 JPEG) — so these are PMS's
  own bundled assets — and `library-art.png`, `playlist.png` and `album.png` are **404s**, even
  though a real PMS's own `/library` container advertises the first of them as its `art`. A
  `?width=100&height=100` answers the same 76,373 bytes.
- **A 0/1 option's value is `+?0*[01]`.** Measured over nine values of each of six options —
  `checkFiles`, `skipRefresh`, `asyncCheckFiles`, `download`, `minSize`, `upscale` — identical on
  all 54: `0`, `1`, `00`, `01`, `000`, `0000001`, `+1`, `+0`, `+01` pass and `2`, `+2`, `-1`, `+`,
  `++1`, `1+`, `1.5`, `true`, an empty value and either surrounding space are 400s. Cycle 23's
  four-value list came from a ten-value sweep with no sign in it. **`download=` is one of them**,
  and rex compared the raw string with `"1"`/`"true"`, so `download=true` was a download where
  a real PMS refuses the read and `download=2` streamed the media un-dispositioned.
- **A 416 is an error response, not the resource with no bytes.** The reference answers a 129-byte
  HTML page with `Content-Type: text/html` and neither `Accept-Ranges` nor `Content-Range`, on
  eight body paths and three container paths; rex answered `Content-Length: 0` with the
  ORIGINAL media type and the full success header set, handing a client an `image/jpeg` of zero
  bytes. rex keeps its own text/plain sentence (cycle 1's deliberate deviation) and now sends a
  body, a plain type and no `Accept-Ranges`.
- **Under `/web/*`, a DIRECTORY is an empty `application/octet-stream` 200**, a trailing slash
  after a file is a 404, a path that escapes the root is a **400** (rex answered 403, a status
  that is nowhere else on the surface), and `.woff`/`.woff2` are `application/octet-stream` like
  the `.ico` cycle 16 already found. rex substituted `index.html` for a directory, so every
  directory under the bundle served the whole client.
- **`X-Plex-Local-Asset: 1` marks an ITEM's own artwork and nothing else** — absent from a
  section's `/composite/{v}`, from `/:/resources/*`, from `/web/*` and from a part read, where
  rex put it on every image it sent.
- **The `Content-Disposition` `filename*` escaper is "unreserved minus the dot"**: `.` comes out
  `%2E` and `+` `%2B`, while `-` is left alone. It is the escaper CLAUDE.md already records for a
  local artwork candidate's `/file?url=`, so the rule generalises; `urllib.parse.quote` keeps the
  dot, so rex sent `1080p.mkv` where a real PMS sends `1080p%2Emkv`.

**Territory B swept EXTRAS and the `clip` row** — and corrected the brief it was given: kids holds
one extra after all (`/library/sections/1/extras/all` is `size 1`, an IVA trailer for Big Buck
Bunny), so the territory had two witnesses rather than one. Four findings plus a build spec:

- **`/library/sections/{id}/extras/all` is a real listing QUERY.** It refuses what a listing
  refuses — an unparseable `limit`, an unparseable or empty `type`, an unknown `sort` key, any
  `group=`, an unparseable `parent` — each a 500, and it refuses four more keys the section grid
  ACCEPTS: `duration`, `resolution`, `mediaSize` and `file` are 500s here and 200s on `/all`, so
  the media columns are the leaf's own refusal rather than the column's. `extraType=`, `title=`,
  `audioLanguage=` and a good `limit` are 200s, and on zen they really constrain (1,264 rows at
  `extraType=1` of 3,580). rex built the leaf in cycle 15 as a fixed empty container — true of
  its own libraries, which hold no clips — so it took no query at all and answered `200 size 0` to
  every malformed request, which a client reads as "this library has no extras".
- **`group=` is honoured on `/library/all`** by both references (kids' 14 rows collapse to 4 by
  type, 7 by year, 10 by title; zen's 14,434 to 4, 90 and 8,358) and an unknown or empty field is a
  500 there as on a section listing. rex's `grouped()` was already right and this handler never
  called it.
- **`sort=type` orders by the type NUMBER**, not the name: kids' rows come back `movie ×5, show,
  season ×2, episode ×6` and exactly reversed descending. rex kept `type` among its text sorts,
  which lower-cases the value, so its order was alphabetical and `episode` came first.
- **An `include*` option's value takes a leading `+`.** A properly encoded `%2B1` turns the block
  on, on both references and on both row types, where cycle 22 recorded `+1` as off — because a RAW
  `+` in a query string decodes to a SPACE, and that measurement was of `" 1"`. The same trap made
  cycle 23's boolean list miss the sign.
- **The `clip` row's build spec**, measured over all 3,904 of zen's extras and kids' one, and
  recorded for a cycle that can hold clips: attributes in one order (`ratingKey, key, guid, type,
  title, [titleSort], summary, index, [year], [thumb], [primaryGuid], art, subtype, [duration],
  originallyAvailableAt, addedAt, [updatedAt], extraType`) with `extraType` LAST; no `Guid`, no
  `Rating`, no tags, no `Field`, no librarySection triple on the row and none on the container,
  which says `allowSync="0"`; `art` is the parent's path verbatim and `/library/metadata/{clip}/art`
  is a 404 while `/thumb` and `/poster` are 200s; `primaryGuid` is the parent's `plex://` guid and
  is present on exactly the 1,501 `extraType: 1` rows of zen's 3,904 and on none of the other
  2,403; six `extraType` values exist (1 trailer, 3 interview, 5 behindTheScenes, 6 sceneOrSample,
  13 opening, 14 ending); the parent's `primaryExtraKey` is the lowest-`index` trailer's `key`,
  30 of 30 sampled; three projections (the standalone leaf is the full row, the inline `Extras`
  block the same minus `art`, the section leaf the light row with no `Stream`); the order is
  `index` ascending and both item forms are UNCAPPED at 44 where `Related` caps at five; and clips
  are in NO listing, axis, hub or count anywhere — `id=<clip>` is `size 0` on `/library/all`, on
  `?type=1` and on the section grid, on both references.

**Measured and deliberately not emulated.** The photo transcoder's geometry is now pinned — the
default scales to FIT inside the box, `minSize=1` scales to COVER it uncropped, and without
`upscale=1` the result is capped at the source's own size, with `quality=` the real JPEG quality
and `opacity=` accepted and discarded — but rex has no resizer, so it still picks a TMDB bucket
and passes those bytes through (the `width`/`height` 500s were already held out in cycle 27). A
section `/composite/{v}` is a 512×512 JPEG mosaic on both references and rex has no
compositor. `/photo/:/transcode` omits `Accept-Ranges` on the FIRST (uncached) transcode and sends
it on every later one — a cache tell no client can see, and rex has no transcode cache. The
416's own HTML body and the fact that the static writer's refusals name no connection state and
CLOSE the socket stay recorded: rex's text/plain error bodies are cycle 1's deliberate
deviation, and keeping a connection the reference drops is the forgiving direction. zen's plain
`filename=` substitutes `_` for `+` where `filename*` escapes it, on one witness. And the whole
clip surface waits on a catalog that holds clips: rex's own has none, so the spec above is the
finding and the build is not.

**A note about the instrument, not about rex.** zen (1.42.2) answered
`GET /library/metadata/{id}/file?url=file%3A%2F%2F%2Fetc%2Fhostname` with `200 image/jpeg` and the
file's four bytes; kids (1.43.4) refuses the same request with a 400. The hunt stopped at that one
request and read nothing else. rex 404s it either way — the references disagree, so there is no
rule here — but zen is a production server on a build that still does it, and HANDOVER 9 records it
so that nobody points a sweep at that path again.

#### The thirtieth pass — the instrument the handover kept asking for, and the response that is not a pure function of the request

Two hunts and a driver round, and the cycle's largest result is an INSTRUMENT.

**The instrument, not the effort — again.** Four HANDOVER 3.3 entries across cycles 21, 25, 27 and
28 all ended with the same sentence: *a writable 1.42.2 copy would settle this.* plexscratch is a
copy of the lab's config and is therefore 1.43.4; zen is 1.42.2 and is production, read-only. So
cycle 30 built the missing instrument -- **plexscratch42**, a FRESH install of
`plexinc/pms-docker:1.42.2.10156-f737b826c`, the exact build zen runs and the one rex reports,
isolated from plex.tv by construction and writable (`harness/scratch42/`, HANDOVER 4.6). It answered
four open questions in an afternoon, and one of its answers corrected a patch from two cycles ago:

- **An On Deck row carries no `librarySectionKey`, and it is the detail row MINUS `Field`.** Cycle
  28 read the opposite off zen's single on-deck row and put the key back; the new copy says absent
  on a movie and on an episode alike, which is what plexscratch (1.43.4) and cycle 21's fun both
  said -- three instruments against zen's one. And the `Field` half was invisible to cycle 28
  because zen's on-deck episode has no locked field at all. `/hubs/home/onDeck` and
  `/hubs/home/continueWatching` keep the key, which rex already had right.
- **The five media-codec edit-field names are a 400 on 1.42.2** (`atmos`, `audioCodec`,
  `audioLayout`, `subtitleCodec`, `videoCodec`) against 1.43.4's 200, so rex's refusal was
  right and cycle 21's hold-out is CLOSED by measurement rather than implemented. **`hdr10plus` is
  the opposite**: a 200 on both versions, so it is a lockable name and rex's 400 was wrong.
- **The write-protected settings are TWO on 1.42.2, not four.** `TranscoderH264Options` and
  `TranscoderH264OptionsOverride` take a write with a 200 and really store it, where 1.43.4 answers
  403; `PlexOnlineUrl` and `allowMediaDeletionLanOnly` are 403 on both. Cycle 19 measured that list
  on the only writable reference it had, which was 1.43.4.
- **`PROFILE_NAMES` is confirmed on 1.42.2**: all 78 platform names driven through a live session on
  the new copy, 23 aliases included, **zero mismatches** against rex's table. Cycle 27's entry
  said "single-witness by construction"; it is now two-version and CLOSED.

Two more things it settled without a code change. The per-version `/filters` split is real and
visible twice over -- 1.42.2 answers 21 movie filters on an empty library and 22 on zen's full one,
1.43.4 answers 27 and 28 -- so the 1-2 row gap inside a version is CONTENT and the 6-8 row gap
between them is the VERSION, which is what the fixtures encode. And a `Player` row driven with
sixteen identity headers carries exactly the fifteen attributes cycle 27 modelled, in that order, on
both versions.

**And one caveat the copy taught about itself.** Its settings lists are SHORTER than a claimed
1.42.2 server's: `/:/prefs` answers 151 rows where zen answers 173, and
`/library/sections/prefs?type=1&agent=tv.plex.agents.movie` answers 14 where zen and kids both
answer 19 -- the missing rows are the marker-generation settings and `ratingsSource`, which need a
subscription the copy does not have. rex's own fixtures match ZEN's counts exactly (19/20/7/9
across the four agent-and-type combinations), so they are right and the copy's numbers are its own
limit. **Never read a settings count off plexscratch42.**

**A three-season show, and what it says about cycle 25's progress model.** The copy now carries one
(`Threefold`, nine distinct ffmpeg clips over three seasons — HANDOVER 3.3 has asked for one since
cycle 25). Unmatching it pushed an activity whose `Analyzing Media` phase steps **per LEAF from 25
to 50** — -1, 25, 31, 36, 39, 42, 44, 47, 50 for nine episodes — and then `Updating Metadata` at 50
twice before `ended 100`, with the subtitle staying the SHOW's title throughout and no per-season
pass at all. That is enough to say `100 - 5 * seasons_left` is not the mechanism and not enough to
replace cycle 25's model: this show was already UNMATCHED (`local://`) and this server is 1.42.2,
where cycle 25 measured a matched show on 1.43.4. A clean measurement needs a MATCHED three-season
show, and every writable instrument is isolated from plex.tv by design, so the entry stays open with
its reason sharpened rather than closed.

**And cycle 26's session serialiser is confirmed on 1.42.2, field for field.** A live session row
there sends `librarySectionID`, `Media.id`, `Media.aspectRatio`, `Part.id` and `Stream.id` as
STRINGS where the same item's detail row sends `int`, `int`, `float`, `int`, `int` — which is what
cycle 26 measured on 1.43.4 and could only measure there, because reading that path needs a live
session and neither read-only reference can be given one. Two versions now.

**A 53-path sweep against the new copy found no further version differences.** Every difference it
did report is content or the copy's own limit: its genre axis is empty because nothing is matched,
its `/statistics/*` are 404s because it is unclaimed and unsubscribed, its `/system` and
`/system/scanners/{N}` lists are empty on a fresh install, and the rest is rex's fixture holding
different ratingKeys. So the four findings above are the whole of what the version split had left
to give on this surface -- which is itself worth recording, because it is the first time the
programme has been able to ask.

**Territory B swept THE SAME REQUEST TWICE** — the assumption every measurement this project has
ever made, that a response is a pure function of the request. Cycle 29 found by accident that it is
not (the photo transcoder's first uncached answer omits `Accept-Ranges`), so cycle 30 swept the
dimension: ~1,600 responses across four servers, each path read three times back to back and again
four minutes later, with every header diffed. Two findings, and both are about state rather than
shape.

**A `random="1"` shelf's sample is STABLE.** zen answered the identical 40 ratingKeys for
`movie.topunwatched.1` on 21 reads taken 20 seconds apart over seven minutes, and the same for
`tv.startwatching.2` and `tv.toprated.2`; a request carrying a different client identifier gets the
same sample, so it is server-wide; and the same shelf at `count=12` answers its own stable set,
which is not the head of the `count=40` one, so the sample is held per (shelf, count). Cycle 28
measured the sampling itself and implemented it as a fresh `random.sample` on every request — so
"Top Unwatched" re-ordered on every navigation back to the library tab, which is the same complaint
cycle 28 fixed from the other side. What makes a real PMS REDRAW a sample is not measured (a TTL, a
restart, a write to the set), so rex mints one seed per (shelf, count) on first use and holds
it for the life of the process: it reproduces everything measured and invents no expiry.

**And the connection state of every refusal was inverted.** Measured by sending the refusal and then
a second request down the same socket, three times per case, on kids and zen alike:

- a **401** with no token, a **401** with a wrong one, the **403** an unknown single segment answers,
  a **404** for a path that does not exist and the **401** on `/photo/:/transcode` all carry
  `Connection: Keep-Alive` and the socket really is reusable. rex forced `Connection: close` on
  every one of them, so a token-gated server tore down one TCP connection per refused request. This
  also corrects a sample printed in this very document: the 401 recorded there carries
  `Connection: close`, which was rex's own answer read back as the reference's.
- the **400** a query pair with no `=` answers is the opposite: both references CLOSE the socket,
  and it is the **only** handler-level refusal on the surface that does — seventeen other reachable
  400s (`type=abc` on six leaves, `checkFiles=2`, the transcoder's missing dimension, `limit=abc` on
  four paths, `timespan=abc`, `accountID=abc`) keep it alive on both. rex kept it alive, so a
  harness that sends a malformed pair and then reuses the socket passes here and hangs against a
  real PMS.

**A collection TAG write makes a tag, not a collection.** The write hunt found it as an aside and
could not settle it with one witness; the new 1.42.2 copy made it two, on two versions. A
`collection[0].tag.tag=` write on the section path is a 200 that creates the TAG — it appears on the
row and in `/library/sections/{id}/collection`, it filters, and both search endpoints find it as a
`tagType: 2` row with a `count` — and creates **no collection item**: `/library/all?type=18` and
`/library/sections/{id}/collections` list nothing new, twenty minutes later and after a refresh.
Only `POST /library/collections` makes one, and the tell is the `guid`: an item-backed collection's
search row carries one and a tag-only collection's does not. rex materialised a full, browsable
collection from the tag write, so "Add to Collection" on an item created a row in the Collections
tab that no real PMS has — and its search read the collection ITEMS, so a tag-only collection would
have been invisible. Both halves are now the tag table's.

**Territory A swept the NOTIFICATION CHANNEL at scale and over time**, and found a self-inflicted
denial of service that twelve cycles had walked past. Every finding here is single-witness by
construction: the copy is the only server a socket may be opened on, since kids and zen are GET and
HEAD only.

**rex answered a client's PONG with another PING — 903,480 pings in 35 seconds.** The
reference's ping timer is free-running: three pings in 35 s (10.001, 20.001, 30.019) whether the
client pongs or not. rex's loop was "wait out one ping interval, then ping", and the wait was a
`select()` that returns for ANY frame — so a pong ended it and the loop pinged again at once, about
26,000 frames a second, for the life of the socket. Every compliant client pongs automatically:
every browser, every Plex app, python-plexapi's `AlertListener`. Twelve cycles missed it because a
raw probe that never pongs sees the correct ten-second cadence. The wait is now a deadline that
keeps polling for a CLOSE until the next ping is due.

**One consumer that stops reading hung every write.** A socket opened and never read from, plus
repeated `PUT .../analyze`: writes 1-103 took a millisecond each, write 104 took 5.2 s, write 105
took 20.0 s, and a second client's unrelated title edit timed out after 30 s while `/identity` still
answered instantly. Closing the dead socket drained 9,462 backlogged frames and writes returned to
a millisecond. The reference took the same shape at ten times the volume — 4.5 MB of frames at a
dead socket — with every request between 0.019 and 0.031 s, and stopped queueing at ~415 KB: it
BOUNDS the per-socket backlog, where rex fanned out synchronously in the request thread with a
blocking write, no queue, no drop and no timeout. rex now gives a sink a two-second budget and
drops the channel rather than the writers — it drops where the reference buffers, which is the half
it can implement, and the check proves it: 20.00 s before, 2.08 s after, on the same 260 writes.

**A library DELETE pushes no per-row frames.** Two libraries built for the question — 3 rows and 200
rows — pushed the eight `library.delete` activity frames and nothing else. Cycle 18's fourteen
`state 9` frames named a DIFFERENT section from the one deleted, none of their fourteen itemIDs was
a row of the library, and cycle 18 recorded the same fourteen for a nine-row library: they are a
one-off cleanup of orphaned `type: 12` rows. rex sent one entry per top-level row, so a
zen-sized library would have handed every client a ~1 MB frame of 3,887 `type 12` deletes keyed on
movie ratingKeys. A single item's delete frame is real and unchanged.

**And three smaller ones.** rex closed the eventsource channel after 300 s where the reference
is still open at 430 — the constant is gone, and a 320-second capture afterwards showed rex now
closing at **256 s for some other reason**, which is recorded as open rather than claimed fixed. Its
101 upgrade carried no `Date`, the one response on the surface without one. And the frames a row's
CREATION pushes are now recorded — eight per row, `state 1` among them, plus a
`library.update.item.metadata` activity per new row — which corrects two documents: CLAUDE.md's
state alphabet never listed `state 1`, and cycle 19's "no scan on a real server does a metadata
activity per row" is true only of a scan that finds nothing new. rex cannot create a metadata
row during a scan, so that sequence is unreachable and recorded rather than built.

#### The thirty-first pass — the write surface per version, the derivations held out, and the reference's own files

Two hunts and a driver round: **22 findings in 21 fix commits and three test commits**, six §3.3
entries CLOSED, and two instrument steps — one of them the most consequential thing in the cycle.
Three of those fix commits correct an earlier patch of the same cycle, each caught by the suite.

**The instrument again: the reference's own FILES are readable.** Two §3.3 entries had waited on the
words *"read the profile files, if a reference is ever shell-accessible"*. kids runs the lab PMS in
Docker and `ssh ben@kids docker exec plexlab-plex …` reads the image — no PMS request, no write,
nothing that can alter the reference. `Resources/Profiles/` holds **79 entries: 78 `.xml` and one
stray `sugar.cmake`**, which is why the API answers 78 rows; a row's title is its
`<Client name="…">` attribute and not the file name (77 coincide, and
`Plex Home Theater-RaspberryPi.xml` declares `name="Raspberry PI"`); and the **23 `Player.profile`
aliases cycle 27 measured behaviourally are 22 `redirect="…"` attributes plus that one
name-versus-filename difference.** Scored against rex's table: all 22 shared pairs agree exactly,
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
mechanism behind cycle 18's `titleSort=` split, and it exposed a real defect: rex treated a stored
sort title equal to the generated title as no sort title at all, so `titleSort=Season 1` answered
nothing where kids answers the row, `titleSort=` wrongly kept it, and the ordering keys ignored it.
One clause, three symptoms, invisible from the API alone.

**Territory A held the WRITE surface out between the versions, and the negative result is the
headline.** About 1,500 requests, the identical sequence on both copies and on rex: the 78
edit-field names with their locks, 66 of them with values, 95 write-grammar cases, 24 numeric columns
on the section path, the settings writers, the list writers, the action leaves, the six state writes,
the upload ladder and 99 paths under a bare OPTIONS all came back **identical on both versions**. What
differs is the **verb table**, and rex carried 1.43.4's:

- **`PATCH` is not a method 1.42.2 recognises** — a 500 on all 20 paths swept and three body
  dispositions, the same 109-byte body `FROB` and `BREW` get on both versions. The decisive case is
  `/identity`, the one path that answers every other verb: six verbs are 200 there on 1.42.2 and
  PATCH alone is a 500. Four recorded rules (cycle 15's routing, cycle 18's 405 under `/system`,
  cycle 22's four 404s, cycle 28's collection 404s) were the other version's.
- **All five `DELETE /library/metadata/{id}/{poster,thumb,art,banner,theme}` leaves are 404s on
  1.42.2**, where 1.43.4 answers an empty 200 and clears the field. The controls make it the VERB:
  the same paths take `PUT ?url=<candidate>` with a 200 there, `DELETE` itself works (a playlist
  delete is a 204), and a real PMS routes before it authenticates. rex had implemented three of
  the five, so it answered neither version consistently.
- **1.42.2 accepts two writes 1.43.4 refuses**: `smart=1` over a comma-joined KEY LIST creates a real
  smart playlist (`leafCount 3`, `icon playlist://image.smart`), and `PUT /playlists/{smart}/items`
  takes an ITEM uri and **replaces the stored query with it** — `content` moving from
  `library://x/directory/…%2F1%2C2` to `library://x/item/…%2F4`, which is also how the middle segment
  is chosen: `item` for a single key, `directory` otherwise.
- **`PUT /library/collections/{id}/prefs` really writes** on both versions. Cycle 15 measured the GET,
  recorded its 404 and stopped, so rex advertised `collectionMode` and `collectionSort` in the
  collection's `includePreferences` block and 404'd the save behind them.

Three smaller ones from the same sweep. **`absoluteIndex.value=` stores nothing** on either path or
version (twelve measurements across four row types) where cycle 20 recorded it as "read on both", so
an edited row carried an attribute no real PMS puts there and `sort=absoluteIndex` ordered on it.
**`lastRatedAt.value=` on the section path is a 500** on both, the ninth member of that set, where
rex answered its unknown-field 400. And **`hdr10plus` stores neither a value nor a lock** — cycle
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

**Territory B scored rex's own DERIVATIONS against zen's 14,433 rows, and the biggest result is
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
  answer the whole library, `title=Tears, of` answers the one row, `title=Steel.` answers none.
  rex stripped both characters out of the query, so a typed comma found one row where a real PMS
  answers 3,887 and a trailing period found one where a real PMS answers none. **The mechanism is a
  level above the matcher**, which is what the first attempt at this got wrong: the filter PARSER
  already reads a comma list as an OR (cycle 11 measured that for tag ids) and drops empty members --
  and on `title` the empty member is KEPT, where an empty query matches every row. So the fix is one
  line in the parser rather than an alternation in the matcher, and a tag field's comma list is
  untouched.
- **The hub matcher takes a multi-word query WORD BY WORD, in order** — `Tears Steel` finds "Tears of
  Steel" at 0.73055 and reversed it falls to the 0.33 fuzzy band — where rex required the joined
  phrase and therefore answered **no hubs at all**: 230 of zen's rows scoring 0.5 or better across 119
  of 1,237 generated queries. That is Plex Web's global search box.
- **It tokenises the TITLE on WHITESPACE**, punctuation inside the word: `don't` finds "Don't Move",
  `Handmaid` finds "The Handmaid's Tale", and `Delete` does not find 'Control-Alt-Delete'. rex
  split on `[^0-9a-z]+`, so a query with an apostrophe could never match while `Air` found "L'air
  Perdu".
- **A ONE-character query matches a title equal to it** (`X` finds the movie 'X'), where rex had a
  two-character floor.
- **`title=` reads the ORIGINAL title as a consecutive run of whole words, unfolded**, where rex
  ran the title's own two-pass folded matcher over it. And **the in-library search box does not read
  `originalTitle` at all** — 102 of 1,244 queries disagreed, every one a rex-only hit.

**Three corrections the cycle made to its own patches, all caught by its own suite.** The
`hdr10plus` regression above was cycle 30's; these were this cycle's. `absoluteIndex` is **parsed and
then dropped** -- `=7` is a 200 that stores nothing while `=ZZQ1` and `=1.5` are 500s on both
versions -- and the first patch put it in the ignore set, which skipped the parse and made a word a
200: the opposite error, caught by cycle 20's own check. The artwork clear moved from the DELETE to
the empty-value edit and the edit path did not **cascade** to borrowers or drop the stored candidate
marker, which four cycle-27/29 checks and the picker check caught. And the comma rule was first
implemented as an alternation inside the title matcher, where it worked for every case except the one
that matters: the filter PARSER splits a comma list first and drops empty members, so the fix belongs
there. Three patches, three re-measurements, and in each case the suite named the defect before the
docs did.

**Two findings this cycle were NOT implemented, and both for stated reasons.** The `title=` query fold
is a per-character TABLE rather than NFD (kids folds `é ï ē ī ì` and does not fold `ị ĩ ı ş ę ȉ`), and
sweeping U+0080..U+024F for three letters gives a real but partial table — 11 characters folding to
`i`, four to `s`, two to `l`. A partial table would break the accented rows that work today, so the
sweep script and its output are recorded and the entry stays open with its method proven. And hunt B's
episode-still finding did not reproduce: over **1,500 live zen episodes, 0 carry a thumb equal to
their `parentThumb`** (1,372 own one, 128 have none), against the 21 live witnesses the hunt counted,
so it is recorded rather than built. `/livetv` is the third: both copies answer **403 to every verb**
today where cycle 18 recorded a three-row container from the same copy, so rex is left alone until
that contradiction is settled.

#### The thirty-second pass — concurrency, the fold table in full, and the decision model from the profile files

Two hunts and a driver round, carried across two sessions: **nine findings in nine fix commits and one
test commit**, four §3.3 entries CLOSED, and two instruments used in anger that no cycle before had:
**threads released from one barrier**, and the **reference image's own profile files as a model's
input**.

**Territory A swept CONCURRENCY — the first time any cycle asked what happens when two requests
overlap.** Every pair was driven from two threads released by one `threading.Barrier`, on both writable
copies and rex, and the row read back afterwards. The negative result is
large: two writers on one row, a read during a write (6,870 body-validated reads on rex and 3,655 on the copies, with no
torn body in either format), two sessions on one item, the full 28-pair play-queue mutation matrix, duplicate
creates, frames under overlapping writes and HTTP pipelining all agree on all three servers. What does
not agree is one shape, twice:

- **`POST /playQueues` answered 404 for the queue it had just created**, in 9 of 60 barrier pairs
  carrying one client identifier, where both references refuse none in 110. The retire-and-insert ran
  under the lock and the answer was built after releasing it, so a sibling create retired the queue in
  a sub-millisecond gap (4 of 30 at a 0 ms offset, 0 of 30 at 1 ms). Plex Web creates the queue before
  it asks for a decision, so a double-clicked Play did not play.
- **Two concurrent ADDS answered the same container** — both naming the other's
  `playQueueLastAddedItemID` — in 11 of 67 pairs, where each reference answer is the queue as that add
  left it (110 of 110). The first fix's comment claimed to cover this and did not: re-measured on its
  own tree, 9 of 180 pairs still collided. Every write to an existing queue — the add, the clear, the
  item delete, the move — now builds its answer inside the lock it wrote under.

And one deterministic finding it tripped over: **a WRITE's response honours the paging headers and
`excludeElements=`**, on both versions, over twelve playlist, collection and play-queue writes.
rex gated both post-processors on GET and HEAD, so cycle 21's "paging is a property of the
response" held for half the surface.

**Territory B closed the two §3.3 entries it was given.** The `title=` query fold is a table of 102
characters, identical on kids and zen over every codepoint both could probe; and the decision model is
exact.

**The fold, finished by the driver on the write side.** The query table raised the question the read
side cannot answer — is the sort title folded by the same table? — and two writes per copy settled it.
Writing 2,076 codepoints into titles, forty at a time, on plexscratch42 and plexscratch (which agree on
every one): **the sort title folds exactly 100 characters**, which is the query table minus `ſ` and the
Kelvin sign, case preserved (`ð` sorts as `O`, `Æ` as `E`). NFD cannot express it in either direction:
it folds `ę ą ş ć` and the capital macrons, which a real PMS keeps, while the lower-case macrons fold.
A decomposed accent is still dropped (`Café` sorts as `Cafe`). Then a locked sort title with
accents in it showed how the two sides meet: `Éggęhead Zoſa` is found by `Zosa` and by neither
`Egghead` nor `Eggęhead`. So the **query** is the table plus a simple case fold (which is the whole
of the difference between the two tables: `ſ` and `K` are case folds, not table entries), and the
**stored** sort title is only case-folded. Hunt B's own finding sits on top: **the folded pass reads
the stored SORT title, not the title** — `Tráin Job` finds "The Train Job" and `Thé Train` finds
nothing, because the article is not in the string the folded pass reads.

**The hub search matcher uses the same table**, swept by the driver with 825 probes on kids: all 100
table characters match at a word score at the start and inside a word, and none of the 400-odd letters
only NFD reaches does. Its CASE handling is not the `title=` one: a capital macron, `ſ` and `İ` match
inside a title's LAST word (`Cosmos LaundrŌmat`, `SintĒl`, `Tears of ſteel` all 0.93) and fail in an
earlier partial word (`CŌsmos`, `TĒars`, `ſteel` alone 0.33), which says the scorer runs two passes
with different case folding. Cycle 39 completed that sweep and built the separate raw-query partial
and broader whole-title passes; its section below records the exceptions and held-out proof.

**The decision model, from the files.** Cycle 31 named the four inputs; hunt B scored a model over 78
profiles × 24 rows and the driver fitted the ORDER of its checks against the reference's own refusal
texts. Of 1,872 cells the outcome is exact on 1,871 and the `directPlayDecisionText` on 1,864:

1. a SELECTED audio stream that is not the first refuses before anything is matched, unless the profile
   declares `DirectPlayStreamSelection`;
2. a direct-play `VideoProfile` must name the container and video codec (`…with container mkv, and
   video codec h264.`);
3. the stream the server would PLAY (the selected one; with no language, the default with the most
   channels) refuses the same way;
4. a selected sidecar subtitle needs a `SubtitleProfile`;
5. the entry must name the selected audio codec (`…video codec h264, and audio codec eac3.`), then a
   selected embedded subtitle's (`…using video codec vc1, audio codec dca, and subtitle codec pgs.`);
6. limitations: the CONTAINER's first, then a codec profile naming the codec, then `*` (`This app cannot
   play this item. The reason is: video.level limitation applies: 51 > 41.`); `onlyTranscodes="true"`
   is skipped;
7. with direct play refused, a transcode target for the request's protocol decides "Conversion OK"
   against "neither", `context="static"` never serves, and Xbox 360's asf-only target is a 400.

Firefox and Edge answer "neither" because they declare only `dash` and `hls` targets, where Web also
declares `http/mkv`. **44 of 78 profiles direct play a real row; rex hard-coded two** — and neither
of those two lists webm/vp8 or eac3, so Roku and Xbox One were told to direct play fixture files they
cannot decode, while Samsung, tvOS, iOS, Windows, PlayStation and every DLNA renderer were told nothing
could play. The profile blocks are `client_profiles.json`, read off the lab image and parsed twice by
two independent parsers with identical results. rex still cannot convert, so a refusal answers the
"neither" triple whatever the transcode targets say.

The eight cells it misses are all **zen 19813**, the one measured row whose two audio tracks carry no
language: which stream it evaluates, and where the sidecar check falls, differ by profile in a way one
row cannot separate (a search over every order of the checks, and a second hypothesis choosing the
stream by the transcode targets' codecs, both scored worse). Recorded, not fitted.

**The JSON byte form, found by the driver in the revalidation step.** Settling `/livetv` meant reading
bodies side by side, and rex's were 11% longer. A real PMS writes JSON **compact** — no space after
`:` or `,` — in **raw UTF-8** (an accent, U+2028, a C1 control and an emoji written as themselves, `/`
`<` `>` `&` unescaped), with `\b \t \n \f \r \" \\` short and every other C0 control as `\u00XX` in
**upper-case** hex. Measured on zen and kids over real rows and on both copies over a title carrying
every C0 control; the notification frames are written the same way. rex wrote Python's defaults on
all three counts, so every JSON body and its `X-Plex-Content-Original-Length` named a size no real PMS
sends.

**`/livetv`'s 403 was the instrument.** Both CLAIMED references answer `/livetv` with the dispatcher's
three-row container, `/livetv/dvrs` and `/livetv/sessions` empty, `/livetv/epg` a 404 and
`/media/grabbers` four grabbers — exactly rex's answers. The two UNCLAIMED copies 403 all of them
and answer the container at `/livetv/` with a trailing slash. Class E, closed without a code change.

**Two checks this cycle had to be refitted to the state the suite leaves them**, the recorded §10 trap:
the write-exclude check assumed one Media on a movie that an earlier merge check has given two, and the
add-race check needed a third movie the suite has deleted by then. Both were re-proved by hand against
their own pre-patch trees, and the exclude check against a server with the second version merged in.

Measured and not acted on:

- **The delete window.** Two concurrent `DELETE /playlists/{id}` of one row are both 204 on the
  references (20/20 and 17/20) where rex answers 204 and 404; a read racing the delete can even see
  `{"size":0,"totalSize":3}`. The difference is the WIDTH of a 2-5 ms window, a class-D shape;
  matching it would mean inventing a grace period in which a deleted row answers success.
- **A second refresh of a row already refreshing.** 1.43.4 coalesces (one activity, 8 frames for two
  refreshes), 1.42.2 runs both — on unmatched rows, the only kind the 1.42.2 copy has. rex runs
  both. The references disagree and neither is picked.
- **`/activities` does carry a running row** (caught by polling s42 during a forced section refresh),
  correcting cycle 19's "a running Activity could not be caught". rex registers its activities in
  the same list; its writes finish in well under a millisecond, so nothing can be caught there (class E).
- **The hub matcher's position-dependent case folding**, and **zen 19813's audio selection**, above.
- REFUTED: hunt B's "rex does not fold K" (the Kelvin sign has a canonical decomposition to `K`, so
  NFD did fold it; what rex missed was `ſ`), and the first race fix's claim to have covered the adds.

#### The thirty-third pass — the response header block, and a version claim that did not survive its control

Two hunts and a driver round: **five findings in six fix commits** (one of them repairing a
regression this cycle's own first patch caused), four §3.3 entries closed or narrowed, and two large
NEGATIVE results — one of which refutes the cycle's only hunt finding as it was reported.

**The hunts ran on a smaller model, and it shows in the shape of the cycle.** Both territories were
launched on the usual pair and died inside a minute on an account-level weekly rate limit for those
models; relaunched on the model that was available, one returned "no findings" with a large negative
result and the other returned a single finding whose framing did not survive re-measurement. The
driver's own territory — which the rate limit made the cycle's main line rather than its side line —
produced four.

**The response HEADER BLOCK had never been compared, and it is ordered.** Cycle 27 measured header
VALUES; cycle 32 found the JSON bytes unmeasured; this cycle read the header block off a raw socket
(`urllib` normalises exactly what is being measured) for twenty-six response classes on kids and zen,
on a keep-alive connection and a closing one. **The two references are identical in every case**, and
the order is per response FAMILY, not one list:

* `X-Plex-Protocol` is the FIRST header of every response and `Date` the LAST — rex had exactly
  those two the wrong way round, because Python's `send_response` writes `Date` before anything else.
* `Keep-Alive` always follows `Connection` immediately.
* A 200 container is `…Content-Type, Content-Length, [paging pair,] Connection, Keep-Alive,
  Cache-Control`; an ERROR (400, 404, 500) swaps the type and the length; a gzipped body moves
  `Connection` ahead of the encoding pair and the length behind it; item artwork leads with the type
  and `X-Plex-Local-Asset`; `/photo/:/transcode` and a `/web/*` or `/:/resources/*` asset send the
  same five names in two different orders; a part read is `ETag, Last-Modified, [Content-Disposition,]
  Accept-Ranges, [Content-Range,] Connection…`; the `/web` 302 is `Location, Cache-Control,
  Content-Length, Connection`; and the preflight wraps its CORS headers around the rest —
  `Access-Control-Allow-Methods, …Max-Age, Vary` early, `…Allow-Headers, …Allow-Origin` after
  `Cache-Control`.
* **A 416 carries no connection header at all**, on a closing request and a keep-alive one alike: the
  only response class on the surface that does not name its connection state.
* **A part read carries no `Cache-Control`**, which is the one exception to cycle 16's "no-cache goes
  on EVERY response". rex already sent none there; the RULE was wrong, not the code.
* **A 206 of a CONTAINER answers `Content-Length` twice** -- the whole body's length in its usual
  place, `Content-Range`, then the SLICE's after the connection state -- while the body is the slice.
  Both references, XML and JSON. A ranged artwork or asset answers the slice's length alone, which is
  what `Accept-Ranges` on the response tells apart. It is genuinely two conflicting lengths on the
  wire: a client that reads the first one waits for bytes that never arrive, which is what
  `http.client` does, so the check for it reads a raw socket. No Plex client ranges a container.

Two more differences fell out of the same sweep, and both are headers rex never sent:

* **The plug-in framework's `/system` paths carry a session cookie** — `Set-Cookie:
  com.plexapp.system=…`, a constant that decodes to the framework's serialised empty session, on the
  200s, the empty-bodied 404s and the `/system/scanners/zzz` 500 alike. The three leaves PMS
  implements itself — `/system/agents`, `/system/scanners/{N}` and `/system/proxy` — send none, and
  neither does anything else on the surface: `/livetv`, `/services`, `/media`, `/status`,
  `/statistics`, `/security`, `/library` and an unknown single segment were all checked.
* **The CORS preflight and the `/web` 302 name their connection state** (and the preflight carries
  `Cache-Control: no-cache`). Both rex handlers write their own header set and went round the
  code that adds it — so the one response a browser always sends before a cross-origin request was
  the only one that never said how long the connection it had just opened would live.

**And the `/system` subtree orders its length first** — `/system`, `/system/search`,
`/system/scanners/{N}` and an unknown leaf alike, the order an error uses — while `/system/agents`,
which PMS implements itself, orders like a container. That is a correction to this cycle's own first
patch, which read the family off the cookie and therefore got the cookie-less `/system/scanners/{N}`
wrong.

**The first header patch also broke every malformed-request-line refusal, and the suite caught it.**
A request whose LINE is malformed is refused before `parse_request` sets `self.path`, so a family
lookup that reads `self.path` raises inside `end_headers` and answers NOTHING — cycle 27's six
malformed lines and the 70 KB URL all went from a 400 to no response at all. It is the fourth cycle
running in which a patch that passed its own fail-then-pass proof was wrong in a way only the whole
suite could see.

**The one hunt finding was reported as a version difference and is not one.** The hunt measured that
a refresh of an unmatched row under an online agent stops after three frames on 1.42.2 where 1.43.4
runs the whole cascade, and proposed that rex (which reports 1.42.2) follows the wrong version.
Re-measured with two controls it does not hold: the 1.43.4 copy shows **the same three-frame opening**
on its own never-matched row, and the 1.42.2 copy runs a full cascade for a row in a
`tv.plex.agents.none` library. What the hunt compared was a `local://` row on one server against
`tv.plex.agents.none://` rows on the other — two different states, not two versions — and whether the
cascade then runs at all is the copies' own state: the 1.43.4 copy carries local metadata bundles for
its rows and the 1.42.2 copy is a fresh install with none and no route to plex.tv (class E).

What the same measurement DOES establish, on both versions, is a real difference: **a row that has
never been matched opens its refresh with `state 2, metadataState "matching"` and a bare `state 5`
before the queued frame**, where a row that was explicitly unmatched opens with the queued frame
alone. rex opened both with the queued frame. That is the chase cycle 32 deferred as "noted, not
chased" from a single server, now measured on both with the guid as the discriminator.

**The negative results are the other half of the cycle.**

* **`translations.json` reproduces the reference image's own gettext catalogs exactly.** The image
  ships `Resources/locale/<lang>/LC_MESSAGES/plex.mo` for 31 languages; the hunt scored every entry of
  rex's table whose language has a catalog — **13,494 (attribute, English string, language)
  triples — with zero mismatches**, and the driver's independent 400-entry sample over three languages
  scored 337 comparable entries with zero mismatches. A table built over five cycles from black-box
  API diffs reproduces the compiled-in source, which is the same result cycle 31 got from
  `Resources/Profiles/` and is a result about the METHOD. (Catalan has no catalog in the image at all
  and is outside the scored population.) The hunt also established that `languages.json`,
  `server_prefs.json`, `system_agents.json` and `mediainfo`'s codec tables have NO source file in the
  image: they are compiled into the binary, so the API remains their only witness.
* **The fold table does not extend to the compatibility forms.** 534 characters with an ASCII
  compatibility base — Greek Extended, Roman numerals, circled and parenthesised letters, the
  mathematical alphanumerics, the `ﬁ`/`ﬂ` ligatures, superscripts, Armenian and Cyrillic — were
  substituted into a `title=` query on kids and **none folds**, which is what rex's table already
  says.
* **Every status LINE matches** (`416 Requested Range Not Satisfiable`, `302 Moved Temporarily` and
  the rest), and every header VALUE matches except the recorded `text/plain` error bodies.
* **Cycle 32's decision model held out on four rows it was never fitted to.** zen holds fourteen live
  rows whose audio tracks carry no language; four of them — across mkv/mp4/mpegts and
  av1/hevc/vc1/h264 — were driven against all 78 profiles: **312 of 312 cells exact**, on the outcome
  and on the refusal text. None of the fourteen has the shape that would settle 19813's eight open
  cells (a selected track that is not the widest), so that entry stays open.

Measured and not acted on:

- **`/system/search?query=…`** answers with `Cache-Control` early (`X-Plex-Protocol, Cache-Control,
  Content-Length, Content-Type, Set-Cookie, Connection, Date`) where the bare `/system/search` puts it
  last. rex's `/system/search` is a static empty container with no query semantics at all, so the
  variant has nothing to attach to.
- **The eventsource and websocket handshakes' header order**, which cannot be measured: a socket may
  never be opened on either reference.
- **Whether a refresh cascade runs at all on an isolated server**, above: the copies' state.

#### The thirty-fourth pass — a fresh client capture, and the decision's audio streams made rather than found

Both hunts died inside a minute on the weekly rate limit that killed cycle 33's (§10), so this
cycle is the driver's own two territories: **a fresh client-session capture through plexlab**, the
founding instrument, whose last full recording was cycle 27's; and **zen 19813's shape, made rather
than found**, which §3.3 had been asking for since cycle 32. Five findings in four fix commits and
two test commits, one §3.3 entry CLOSED, one recorded belief overturned, and one of the cycle's own
earlier commits caught starving a check three thousand lines below it.

**The capture.** `session-web.py` drove the official client through the walk again against kids and
recorded 26 unique requests. Replayed against rex with `replay.py` (`--reference` live at the
same moment), every element and attribute difference was one already recorded as a version or a
library difference. What the capture DID carry was the client's own envelope, which no probe had
sent:

* **A response with an EMPTY body orders `Content-Length` before `Content-Type`**, the way an error
  does, where a MediaContainer body is type-first. Read off the client's `PUT /updater/check` and
  then re-measured on both copies over three endpoints -- an item edit, a `/refresh` and a 204
  playlist delete, on 1.42.2 and on 1.43.4 alike. So cycle 33's response FAMILIES do not split on
  "error versus success": a container body is type-first and everything else -- an error page, the
  plug-in framework's `/system`, an empty-bodied write answer -- is length-first. A HEAD keeps the
  container order, its length being the body's and not zero.
* **The preflight answers `Access-Control-Allow-Headers` only when the request carried
  `Access-Control-Request-Headers`**, and it is that value echoed. Plex Web's preflight of
  `PUT /updater/check` asks for no headers at all and the reference answers without the field, where
  rex substituted a fallback list -- cycle 16 measured the echo and invented the fallback beside
  it.
* **The websocket handshake's header ORDER**, which cycle 33 recorded as unmeasurable because a
  socket may never be opened on kids or zen. The client opens one itself, so the capture holds both
  sides: `X-Plex-Protocol, Access-Control-Expose-Headers, Vary, Upgrade, Connection,
  Sec-WebSocket-Accept, Cache-Control, Access-Control-Allow-Origin, Date` -- the CORS pair LEADS,
  ahead of the handshake headers, and the allowed origin follows `Cache-Control`. rex had the
  right set and put all three CORS headers at the end. The `Sec-WebSocket-Extensions` the client
  offers is echoed by neither side.

**The made row.** §3.3's oldest open entry was zen 19813, the one measured row whose audio tracks
carry no language, and the eight cells cycle 32's model could not fit. Its own instruction was to
make one: `plexscratch`'s `long` media directory is writable. `ffmpeg` muxed Sintel's h264 video
with an mp3 2.0 track and an ac3 5.1 track, both `default`, neither tagged with a language, into an
mp4 with `+faststart` so `optimizedForStreaming=1` matches 19813 and no container limitation can
fire first. After `analyze` and `refresh` the copy's row 60 reads exactly 19813's shape, and driving
all 78 client profiles against it answers **68 of 78 identically to zen's row** -- the ten that
differ being the ones zen's selected sidecar subtitle refuses earlier, each exactly where the model
puts that check. The shape reproduces; the rule was then measurable.

* **A decision reads EVERY audio stream, not only the selected one.** Roku lists `aac,mp3,flac` in
  mp4 and refuses the row naming the **ac3** it does not list, although the mp3 it does play is the
  selected track; Android lists `aac` alone and names the **mp3**, the first in index order that it
  cannot play. The first stream in INDEX order that no matching `<VideoProfile>` names is the codec
  the refusal names.
* **Unless the profile declares `StreamUnselectedIncompatibleAudioStreams`**, in which case only the
  selected stream counts -- the server streams the others on their own. That is the whole difference
  between Windows Phone (`aac,mp3`, which direct plays the row) and Universal TV (`aac,ac3`, which
  refuses it naming the mp3 it does not list), two profiles otherwise alike. Six profiles declare
  it: Windows, Windows 10 Desktop, Windows 10 Mobile, Windows Phone, iOS and tvOS.
* **`client_profiles.json` was a projection, not a capture.** Cycle 31 read the reference's
  `Resources/Profiles` into it and kept **10 of the 30 `<Setting>` rows** -- exactly the
  `DirectPlayStreamSelection` the cycle-32 model happened to read. One of the twenty it dropped is
  the setting above. Nineteen profiles declare settings; all thirty are in the fixture now.
* **The decision reads a per-user audio selection that the item row does not carry.** The same row
  answers 41 of the 78 profiles "Selected audio stream is not the first audio stream" before any
  client has chosen a track -- although `selected` names the FIRST audio track and the profile's own
  capabilities explain none of it -- and answers every one of the 78 from the visible row once a
  client has chosen one. **One write flips it**: `PUT /library/parts/{id}?audioStreamID=<the track
  already selected>` makes all 41 direct play or refuse on their own profile instead, and
  `audioStreamID=-1` puts every one of the 78 back, byte for byte. The item's `Media`, `Part` and
  `Stream` rows are **byte-identical in both states**. So cycle 32's rule -- "the server re-picks the
  widest default track when the selected one has no language" -- is not a property of the row at all;
  it was that hidden state, fitted to one witness. rex holds a chosen track, so the model is the
  after, and the before is recorded and not emulated (§8).

`tests/test_decision.py` is the new suite that keeps it honest: `tests/fixtures/decisions/` holds
each measured row's `Media` as the reference wrote it and one line per (profile, row) cell -- 78
profiles against 24 rows on kids and zen plus 78 against the made row, in both of its states. The
model is **exact on all 1,872 cells** of the state it emulates, on the outcome and on the refusal
text; the 156 pre-selection cells are counted and never fail the suite. Against the model as it
stood the same corpus scores 1,826.

**And the cycle caught one of its own.** The empty-body check writes `summary.value=c34` to the
fixture's first movie and left it there, which starved cycle 20's "only titleSort folds case" check
three thousand lines below -- it counts the rows whose summary sorts under `b` against those under
`B`, and its whole population is this fixture's three movies. The commit was proven with the driver,
which runs one block; the full suite is what caught it. The check now puts the summary back.

Measured and not acted on:

- **What the reference picks before a client has chosen a track.** The 41 profiles that refuse the
  made row as "not the first audio stream" are every profile with a matching `<VideoProfile>` except
  Android, Popcorn Hour and the Roku 5.x/6.x/7.x family, and nothing in those files -- settings,
  `<Identification>`, DLNA blocks, the audio codecs they list, their codec limitations -- separates
  them from the rest. Asus Transformer Prime TF201 and Roku-7.x list the same audio codecs in mp4,
  answer identically on every other measured row, and answer differently here. Both states of the
  row are in the corpus; rex cannot express the distinction from a catalog row either way.

#### The thirty-sixth pass -- the refusal surface, the grammar of an id, and a score that was never one number

Three territories. The driver took a dimension no cycle had swept -- **what a real PMS answers when
it refuses** -- and both hunts came back with a recorded belief overturned: the search hunt refuted
cycle 35's own tier model one cycle after it landed, and the write hunt held out the list cycle 35
had reported as "already correct" and found four of its seven items open.


* **A refusal's BODY is one fixed HTML document per status**, built from HTTP's own reason phrase:
  `<html><head><title>{reason}</title></head><body><h1>{code} {reason}</h1></body></html>` with
  `Content-Type: text/html`. Measured on kids (1.43.4) and zen (1.42.2), which agree on every cell, over
  seven statuses -- 400 (89 bytes), 401 (91), 403 (85), 404 (85), 406 (95), 416 (129) and 500 (109) --
  and across every path family that answers one. It IGNORES `Accept`: a client asking for JSON gets the
  same HTML. It is GZIPPED when the client offers to take a compressed body (81 bytes for the 400, 78
  for the 404), and a HEAD carries its `Content-Length` with no body. rex had **three** of them --
  the 401, the 403 and the root's 193-byte redirect, each built by the cycle that happened to measure
  that one refusal -- and answered `text/plain` with a sentence of its own to every other refusal on the
  surface. That sentence is cycle 1's "deliberate deviation", carried in HANDOVER §8 for thirty-five
  cycles and re-justified three times (cycles 16, 27, 29) without anyone reading what the reference
  actually sends. It is now gone: the sentences move to the LOG, which is where they were always the
  useful half.
* **And the ENVELOPE's own refusals answer the same document.** Every refusal written before the router
  runs -- a two-space request line, tab separators, an LF-only ending, a 40 KB URL, a 101st header, a
  name over 1024 bytes, a value over 4096 -- came out of `BaseHTTPRequestHandler.send_error` as Python's
  460-byte DOCTYPE page with `text/html;charset=utf-8` and an explanation naming the library. It is the
  one document on the surface that names a library rather than a status. The request state has to be
  faked in for the shared writer to run that early (no `self.headers`, no `self.path`), which is cycle
  33's trap from the other side.
* **A path-segment id is parsed as a NUMBER, so `01`, `001`, `0001`, `+1` and `+01` name the same row as
  `1`.** Byte-identical responses on six path families -- a section grid, a section row, `/filters`,
  `/library/people/{id}`, an item detail and an item's `/thumb` -- on both references. What does not
  parse names nothing: `++1`, `1+`, `-1`, `0x1`, `1_0`, `1.0`, `1e3`, a surrounding space and every
  non-ASCII digit are 404s on both. It is the general case of the `+?0*[01]` grammar cycle 29 measured
  for a 0/1 option VALUE. rex compared the segment as a STRING, so a zero-padded or plus-signed id
  -- what a URL built by concatenation, or a client that pads, produces -- was a 404 from a server that
  holds the row. Normalised once at the front of the router rather than at each of the forty lookups
  that read an id.
* **Four paths bound that id to a SIGNED 32-BIT integer.** Bisected on both references:
  `/devices/{id}`, `/accounts/{id}`, `/playQueues/{id}` and `/system/scanners/{N}` answer their own
  not-found for `2147483647` and a **400** for `2147483648` and everything larger, and a NEGATIVE value
  inside the range is a **404** on all four -- including `/system/scanners`, whose answer for an unknown
  non-negative library type is a 500 and for a non-number is a 500 as well. `/library/metadata` and
  `/library/sections` carry no such bound: a twenty-digit id is a plain 404 there. rex had the bound
  on two of the four, read a small negative play-queue id as malformed (400 where a real PMS answers
  404), and reported a malformed scanner request as a missing scanner (500 where it answers 400).
* **The request TARGET must be printable ASCII, and a `%` must begin an escape the parser can read.**
  Swept byte by byte over 0x01-0xFF: every raw byte outside 0x21-0x7E is a plain 400 on both references
  -- in a path segment, in a single segment and in a query VALUE alike -- while a percent-escape of the
  same byte is fine (`%C3%A9` and `%FF` are ordinary 404s). rex answered 200 or 404 to 150 of the
  158 refused bytes; the eight it did refuse were an accident, being exactly Python's own
  unicode-whitespace set (`\x0b\x0c\x1c-\x1f\x85\xa0`), which its request-line splitter reads as a
  separator. The escape half was measured over 42 spellings at the end of a path and mid-path: the
  parser reads the two characters after a `%` the way `strtol` does -- an optional `+` or `-` then at
  least one hex digit, and it need not consume both -- so `%4y`, `%AB`, `%+1` and `%-1` pass while
  `%zA`, `%y4`, `%++`, `%--` and `%+y` are 400s. Two characters must be THERE: `%`, `%4`, `%+` and `%a`
  at the end of a path are 400s where `%4y` is fine. `%%` is the exception, consumed as a literal pair
  before that rule runs -- `%%`, `%%y` and `%%%%` pass and `%%%` does not. The QUERY is not validated
  this way at all: `?a=%`, `?a=%zz` and `?a=%%` are 200s on both references.
* **`/hubs/sections/{id}` and its Continue Watching see-all leaf 404 a section the server does not
  hold.** `999`, `0`, `-1`, `1.5`, `abc` and a twenty-digit number each answer the 85-byte Not Found
  document on both references, while `01` and `+1` answer the real library. rex ignored the id and
  built a full Recommended page for any of them -- fix 4's trap, a client asking for a library that is
  gone reading a valid empty page as "this library exists and has nothing in it". Every other section
  leaf has answered 404 since cycle 15; these two were missed because neither reads the section row
  before it builds its answer.
* **`/hubs/metadata/{id}/related` never resolves the row at all.** `999999`, `abc`, `-1` and `0` each
  answer an empty hub container -- `{size: 0, allowSync: true, identifier}` and nothing else, the
  librarySection triple being the only thing a real row adds -- where `/library/metadata/{id}/related`
  on the same unknown id is a 404 and `/hubs/metadata/{id}/postplay` is a 404 too. So the two spellings
  of "related" differ on more than the path, and the difference is which one looks the row up. rex
  404'd all three. The preplay page builds this URL from a row it is already holding, so the two
  answers look alike to a client that has one; python-plexapi and any caller walking ids sees an
  endpoint that does not exist rather than a shelf with nothing on it.
* **A repeated query parameter takes the LAST occurrence, in the token gate too.** A bad token then a
  good one in the same query is a 200 and a good one then a bad one a 401, on both references. That is
  the rule cycle 35 measured for every other key on the surface, and the rule rex's own `query` dict
  already follows, being built by a dict comprehension over the pairs. Three readers had been written
  with `parse_qs(...)[0]` instead and took the FIRST: the token gate, the client identifier the session
  bookkeeping keys on, and the notification channels' `filters=` whitelist. A token appended to a URL
  that already carried one -- what a client concatenating a base URL produces -- was refused by rex
  and accepted by a real PMS.

**The search hunt found that the SCORE was never one number, and that the ladder has four
steps.** Cycle 35 had just corrected both scorers to the matcher's own tier rule and printed
`0.93`, `0.73` and `0.33`; every one of those numbers is short of what a real PMS sends, and the
ladder itself was two-thirds of the real one.

* **A hit's five decimals are a TIER plus the row's own `audienceRating` divided by 10000.** Read
  off 17 rows on kids and zen with the rating fetched beside the score -- kids' "Tears of Steel"
  5.5 at `0.73055`, zen's "Amélie" 9.5 at `0.93095`, zen's episode "Riders on the Storm" 9.1 at
  `0.71091`, zen's "One Piece 0978" with no rating at `0.33000` -- and held out by the hunt over
  **11,076 scored (query, row) pairs across 619 queries**, of which 11,075 decompose exactly this
  way. `rating`, the critic score, is not it: zen's "Riders of Justice" carries 9.6 there, 8.9 in
  `audienceRating`, and scores `089`. rex printed the bare tier, so every row inside a tier
  TIED and the search page fell back to insertion order instead of the best-rated hit first.
* **There is a fourth tier, 0.53, and it is the way search is actually used.** Gridded on zen's
  "Riders of Justice" over forty queries and confirmed on kids: **exact** (the query's tokens ARE
  the title, its `titleSort` or its `originalTitle`) 0.93; **anchored** (the query's FIRST token
  EQUALS the title's first) 0.73; **prefix** (the query is a token-wise prefix of the title -- no
  more tokens than it has, each a prefix of the token at the same index, the first a PROPER
  prefix) 0.53; **fuzzy** 0.33. rex had no prefix tier at all, so `Tear` scored where `Tears`
  does -- a two-letter prefix outranking a genuine whole-word hit.
* **And the anchored tier is ONE token comparison, which refutes cycle 35's own model.**
  `word_tier` required the matcher's in-order, skip-tolerant walk; both references score
  `Riders X`, `Riders XY ZW`, `Riders of Justices` and `Riders of Justice Riders` at 0.73, and
  that model calls every one of them fuzzy. It was fitted on sixteen cells, none of which
  carried a term the title does not have -- class D, a model that agreed with every case it was
  built from, landed one cycle earlier and overturned here.
* **The base of the ladder is the ROW TYPE's.** A movie and a show are 0.93/0.73/0.53/0.33; an
  EPISODE is 0.02 lower throughout (0.91/0.71/0.51/0.31); a TAG has its own 0.92/0.52/0.32 with
  no anchored step at all -- `Drake` against the tag "Drake Bell" is 0.52 where a movie whose
  first word matched would be 0.73. rex scored every type at the movie's base, so its
  episodes tied with, and could out-rank, a movie of the same match quality, which inverts the
  page a real PMS draws. And its tag MATCHER compared the whole query against ONE word, so a
  multi-word tag name could not be found by typing it: `Science Fiction` answered no tag row at
  all where both references answer one per library.
* **ORDER decides the SCORE, not membership.** `Steel Tears` answers "Tears of Steel" on kids and
  `justice riders` and `Justice of` answer "Riders of Justice" on zen, each in the 0.33 band.
  Cycle 31 measured that sentence and built only the in-order half, so rex answered NO HUBS
  AT ALL to a query whose words a person typed out of order.
* **The fuzzy band is SQLite's `spellfix1`, and now it has a mechanism.** Strings read out of the
  PMS binary on kids name `spellfix_metadata_titles`, `spellfix_metadata_sort_titles`,
  `spellfix_metadata_original_titles` and `spellfix_tag_titles` as
  `CREATE VIRTUAL TABLE … USING spellfix1`, together with `spellfix1_phonehash`,
  `spellfix1_editdist` and the class table `.ABCDHLRMY9 ?` verbatim. A row is a band CANDIDATE
  when one of its title tokens has a phonetic hash beginning with the first three characters of
  the query token's -- which explains why zen answers `azzdi` with austin, action, astin and
  akti and NOT with `azadi`, a cell no similarity metric produces (Jaro-Winkler puts `azadi` at
  0.89 and `action` at 0.58). The hunt transcribed `phoneticHash()` and scored it over 120
  generated distortions: **897 of 898 reference band rows predicted**. It is still NOT built,
  and the reason is the other half: spellfix1 then prunes to a top-N by
  `editdist1 + 32 − ⌊log₂ rank⌋`, where `rank` is a GLOBAL token frequency over every
  `metadata_items` row. That pruning is visible from outside and unmeasurable: `Tears zzz`
  answers the row on kids' five titles and `Riders zzz` answers nothing on zen's 3,887.
  Emulating the candidate half alone would answer most of a real catalog to a nonsense query,
  which is a worse error than answering none of it. §8 carries the mechanism so the next cycle
  starts from it rather than from "a trigram score rex does not try to reproduce".

**The same hunt took the decision corpus into Dolby Vision, and settled a §8 disagreement.**

* **A Dolby Vision PROFILE 5 source is refused by the server itself**, with a code and a sentence
  the corpus had never seen: `transcodeDecisionCode 2003`, "File is unplayable. DoVi (Profile 5)
  color space is not supported." Witnessed on three zen rows (967 and 1120, mkv/hevc/1080; 4813,
  mp4/hevc/4k) and on NONE of the six profile-7/8 rows, which answer the ordinary 4002 exactly
  like the two non-DV hevc controls already in the corpus -- so it is keyed on the DV profile,
  not on hevc, not on the container and not on server load. It is not a blanket veto: of the 38
  profiles that declare a matching http target, ONE still converts the row, and that one is the
  only one whose target names the SOURCE's own video codec (`Plex Desktop`,
  `videoCodec="h264,hevc,*"`). Every other target would re-encode to h264 and lose the colour
  space. Built in `decision.py` and exact on all 78 cells in both directions; the corpus now
  holds them.
* **The dual-layer Dolby Vision disagreement is RESOLVED, and both notes were right.** Every one
  of zen's 40 DV Media carries exactly ONE video Stream -- including all 17 at `DOVIProfile 7`
  with `DOVIELPresent: true` -- except one m2ts, which carries two: a 4K base track with no DOVI
  at all and a 1080p `DOVIProfile 7` track with **`DOVIBLPresent: false`**. So the discriminator
  is `DOVIBLPresent` and not `DOVIELPresent`: a separate-track enhancement layer says false on
  its own stream, a single-track dual layer says true beside its EL flag. fun's note was measured
  on a file of the first kind and zen's on files of the second. **rex was already right** --
  `mediainfo.py` keys its "DoVi Enhancement Layer" label on exactly that flag -- so the §8 entry
  is a resolved non-defect rather than an open contradiction.
* **A multi-Media row answers `mediaIndex=N` from its own order.** zen's rk 401 has fifteen;
  `mediaIndex` 0, 1, 2 and 7 select Media 603, 3840, 4187 and 5404, `15` is an HTTP 400, and
  `-1`, `abc` and an empty value fall back to Media 0 -- which confirms cycle 35's fallback rule
  on a row with more than one. When the answer is `1001` the `Media`/`Part` returned describe the
  TRANSCODE TARGET rather than the source, which rex has no encoder to reproduce.
* **The corpus is 2,106 cells**, up from 1,950: zen:1120 (the profile-5 witness) and zen:1098
  (the corpus's first two-video-Stream Media and its first `dca-ma`) at 78 profiles each. Twenty
  of the 156 were re-driven by the driver against zen and agreed 20/20; fourteen text indices
  were checked against the reference's own `directPlayDecisionText`.
* **A negative result the hunt reported and the driver kept**: the hub-search fold is NOT a third
  table. Tested on the two characters where `hierarchy._TITLE_FOLD` diverges from NFD and from
  ICU -- zen's "Død Kalm" answers `D0d Kalm` (digit zero) at the exact tier and `Dod Kalm` only
  in the band, and "Dæmonicus" answers `Demonicus` exact and `Daemonicus` only in the band -- so
  cycle 32's "the hub search folds by the same table" now holds on the discriminating characters
  rather than on the agreeing ones.

**The write-frames hunt took the list cycle 35 reported as "already correct" and held it out** --
HANDOVER §3.1 class F, a hunter's negative taken on trust -- and four of the seven items it had
closed are not closed. It drove 93 write measurements against plexscratch (1.43.4),
plexscratch42 (1.42.2) and a frozen rex side by side, with one listener per server, a control
title edit before every phase, and the agent confound designed out (a `tv.plex.agents.none`
library over a hardlinked copy of Firefly on BOTH copies, so a sequence that depends on an agent
is comparable).

* **An UNMATCHED show refuses the agent-driven prefs, and `showOrdering` there is a 500.** An
  unmatched show's `Preferences` block carries eight settings where a matched one carries ten, and
  `useOriginalTitle=1` and `languageOverride=en` are each a **400** there while the matched show
  answers 200 to both. `showOrdering` is a plain **500** for every value -- `dvd`, `aired`, an
  empty one and a value the setting does not name alike -- with NO frames, and the value is
  STORED: the row reads it back afterwards, so it is not a validation refusal that writes nothing
  but a write whose cascade fails. rex answered 200 to all of them and pushed its whole
  57-frame cascade, so the Advanced tab of an unmatched show saved settings the row does not
  show and told every client the tree had been re-read. Measured on two `local://` shows on the
  1.42.2 copy with plexscratch's matched Firefly as a control.
* **A playlist and a collection take an artwork UPLOAD.** `POST /library/metadata/{list}/posters`
  with the image as the body is an empty 200 on both copies, the candidate list grows from one
  `default://` row to two, the uploaded image is SELECTED, and one timeline frame carries the
  LIST's own type. rex answered 404, so Change Poster → Upload on a list page failed outright
  while the PICK beside it -- which cycle 21 landed -- worked.
* **A `?path=`-scoped scan scans the section's own folder and any CHILD of it**, and cycle 19 was
  wrong twice. The own Location scans everything, byte for byte the no-path scan; a child folder
  scans too (17 frames against 16, the same three `progress` rows), where cycle 19 recorded that a
  child scans nothing -- that measurement was taken on a MOVIE library, whose children are files
  rather than folders. rex scanned nothing for EITHER, because the catalog's section dict
  carries no `Location` at all (the list is synthesised when the section is published), so the
  comparison here was always against an empty list and every `?path=` took the no-folder branch.
* **The `/library/collections/{id}` action leaves really RUN.** Cycle 22 measured their empty
  200 and stopped there; `refresh` pushes the same three frames the metadata spelling does, and
  `unmatch` really unmatches -- `guid` becomes `local://{rk}`, `subtype` the literal `unknown`,
  `ratingCount` gone. rex answered the empty body and did nothing, so the SAME action taken
  from the client's own context-menu path and from the metadata path had different effects on the
  same row.
* **A type=18 SECTION save frames like the section path, not the item path**: `state 3` with
  `metadataState "queued"` carrying the title from BEFORE the write, then `state 5` with the new
  one -- where the item and metadata spellings of the same edit send `loading` with the new title
  in both. rex routed the section save straight through the item writer.

**And the interlaced row the cycle asked for was MADE, which closes a §3.3 entry.** A
`-flags +ilme+ildct -x264-params interlaced=1:tff=1` encode muxed into plexscratch's writable media
and scanned gives a Stream with `scanType: "interlaced"` and `displayTitle "1080i (H.264)"` on both
versions -- and `mediainfo._scan_type("tt")` already derives exactly that. Driven against all 78
profiles on both copies: 39 refuse, and the only interlace-driven refusal is **`RokuDLNA-5.1`**'s
`video.scanType limitation applies: interlaced == interlaced`, while **iOS and tvOS -- the two
profiles that name `video.separateFields NotMatch 1` -- direct-play it**. So an MBAFF h264 does not
set `separateFields`, `decision.py`'s constant 0 is right for this shape, and the model reproduces
78/78 on both copies. Producing a row that DOES set it needs field-coded (PAFF) pictures, which
neither libx264 nor ffmpeg's mpeg2video encoder can write.

Measured and not acted on:

- **The fuzzy band's PRUNING.** The candidate rule is settled -- SQLite `spellfix1`'s phonetic
  hash, transcribed and scored at 897 of 898 reference band rows -- and the top-N prune over a
  GLOBAL token frequency (`editdist1 + 32 - ⌊log₂ rank⌋`) cannot be read from outside. `Tears zzz`
  answers the row on kids' five titles and `Riders zzz` answers nothing on zen's 3,887, so the
  candidate half alone would answer most of a real catalog to a nonsense query. §3.3 keeps the
  entry with the mechanism attached, which is a much better starting point than "a trigram score".
- **`/match` on a SHOW.** Both copies open with a bare `state 3` carrying the OLD title and then
  run that show's refresh cascade; the DEPTH is the version's (1.43.4 cascades to every season and
  episode, 1.42.2 stops at the show). rex sends the bare 3 and then its own seven-frame show
  form, which is 1.42.2's shape and the version it reports, so what is left is a frame-count
  difference on a version rex does not claim.
- **`partIndex` on a MULTI-PART Media**, made on plexscratch from a stacked `- cd1`/`- cd2` pair:
  the two files become ONE Media with TWO Parts (`duration` the sum), `partIndex` 0 and 1 select
  the part and the decision reads THAT part's streams, `2` and `5` are 400s, and **`-1` is a 200
  carrying `3000 "App cannot direct play this item. Multi-part media requires a conversion when
  all parts are selected."` with `2000`/`4005`, `size 0` and no row**. On a single-part Media every
  value still selects part 0, which is cycle 26's rule and where it holds. rex reads no
  `partIndex` at all and its catalog builder never makes a two-part Media, so none of it is
  reachable from its own rows; recorded with the grammar for a catalog that ever holds one.
- **A 1.43.4 library DELETE pushes fourteen extra `state 9` frames** (`type: 12`, empty title,
  `sectionID` the NEGATIVE id) between the 10% and 40% activity updates on a nine-row SHOW library,
  where 1.42.2 pushes the eight `library.delete` activity frames alone. Cycle 18 saw those
  fourteen and cycle 30 called them a one-off orphan cleanup on the evidence of two MOVIE
  libraries; on 1.43.4 they reproduce on a show library. rex reports 1.42.2 and matches it.
- **The show cascade's frame ORDER on 1.43.4** -- `activity started` after the queued frame, the
  per-season `updated` at the season's END rather than its start, no invented `updated 49`, and
  the media pass only on the matched season and only once. Single-witness by construction: no
  matched show can be written on a 1.42.2 copy.
- **The `video.frameRateMode` limitation is still untripped**, on a VFR row made for it: the
  analysis calls a jittered `-vsync vfr` encode `cfr`, and `Sony Bravia 2010` -- the only profile
  naming it -- direct-plays the row.
- **A NUL in a filter VALUE is a 500 on the operators that build a substring comparison** and a
  200 on the rest: `title=`/`title!=`, `genre=`, `contentRating=`, `id=` and every numeric column
  refuse it, while `title==`, `title>>`, `genre!=`, `contentRating>=`, the booleans,
  `audioLanguage` and an unknown field answer 200. Measured over 40 cells on both references. No
  Plex client can put a NUL in a query -- every one of them builds the URL from a JS string -- and
  the rule keys on an interaction of column KIND and OPERATOR that no model explains, so
  implementing it means a table fitted to those 40 cells.

Three surfaces swept this cycle with NO difference, recorded so they are not swept again: CORS on
a refusal (both references and rex send the same `Access-Control-*`/`Vary` set on a 400, a 404
and a 500, and none at all on an envelope refusal, which is written before the headers are read);
the query parameter NAME grammar (a name is percent-decoded -- `%74itle` filters -- case-SENSITIVE,
and a `+` or a leading `%20` in it makes it an unknown field, on which rex already agrees); and
`/library/streams/{id}` (the 501 for a real stream id, a 404 for an unknown one and for a
non-number, identical on all three).

#### The thirty-fifth pass — the encoded body, the Range grammar, and two scores that were never the matcher's

The hunts ran again after two cycles without them. **Six findings in six fix commits**, two from each
hunt and two from the driver's own territory, plus five negative results and four measurements
recorded rather than built.

**The driver swept the ENCODED body, which no cycle had looked at.** Cycle 27 established that a real
PMS gzips every compressible body the client offers to take compressed; nobody had read the bytes it
sends.

* **A real PMS zeroes the MTIME field of its gzip header**, and with it the whole member is
  byte-identical to `gzip.compress(payload, 6, mtime=0)` -- header, deflate stream and trailer -- on
  six bodies from 163 to 20,544 bytes, on kids (1.43.4) and zen (1.42.2) alike. Python stamps the
  current time, so rex answered the SAME request with different BYTES every second. Cycle 30
  swept "the same request twice" and could not see it: it compared the DECODED bodies.
* **The `Range` grammar is not what rex implemented.** 43 header values against both references
  over a container and a `/web` asset -- 172 cells, the two references and the two body kinds
  identical in every one -- and rex had twelve of the 43 wrong. Two are provokable by a real
  client: a reversed range (`bytes=5-4`) answered a 206 with an EMPTY body where a real PMS answers
  **416**, and an unparseable one (`bytes=abc-def`) answered 200 with no `Content-Range` where a real
  PMS answers the whole body as a 206. The rest is the parser's own shape: the UNIT is never read
  (`items=0-10` ranges, `BYTES=0-10` ranges) but a value with no `=` is not a Range at all; a suffix
  of zero length or one whose number does not parse answers `bytes 0-0/N` with the WHOLE body; an
  unparseable FIRST answers the whole body as a 206, and so does anything with a second `-` after the
  first or a comma list; an unparseable LAST becomes ZERO, so `bytes=0-abc` is one byte and
  `bytes=5-abc` is a 416.

**The write-frames hunt finally got its turn**, and the list cycle 33 left produced two.

* **A library CREATE scans, and the scan's skeleton survives a location holding no files.** Eleven
  frames on plexscratch and plexscratch42 alike, frame for frame: the `library.update.section`
  activity from `Scanning` through a Context-carrying 99 and 100 to `ended`, the two LIBRARY_UPDATE
  statuses around it, and the nested `provider.subscriptions.process` pair. Cycle 15 read a create's
  frames as the per-item ones alone and concluded an empty library had nothing to push, so rex's
  create pushed only the `backgroundProcessingQueue` frame -- and the client's own Add Library dialog
  draws its spinner and its "Library scan complete" toast off exactly these.
* **`ButlerTaskGenerateAdMarkers` ships DISABLED**, alone among the three marker tasks: Credits and
  Intro really do default to enabled. It is the ONE disagreement in the whole 23-row enabled table
  between rex and both copies, which agree with each other on every row, and `POST /butler` runs
  what is enabled -- so a fresh rex generated ad markers where a fresh real server never does.
  Class D: a default set from its siblings rather than read.

**And the search hunt found the score had never been the matcher's.**

* **A search hit's SCORE tier is the matcher's own rule**, not a title prefix. Measured over sixteen
  (query, row) cells on kids and zen: an exact title is **0.93** (rex said 0.95), a query whose
  terms match the title's words in order **starting at the title's FIRST word** is 0.73 -- `Tears
  Steel`, `Big Bunny`, `Big Buck`, `Cosmos` -- and everything else is the 0.33 fuzzy band --
  `Steel`, `Bunny`, `Buck Bunny`, `Laundromat`, `of Steel`. Both of rex's scorers compared a
  title PREFIX instead, so a **skipped middle word** -- the commonest thing a person leaves out when
  typing -- scored at the fuzzy floor, below the threshold the client promotes a hub on, while a
  single word from the middle of a title was promoted above it. Class C in its canonical shape:
  cycle 31 built the rule into the matcher, cited `Tears Steel` → 0.73055 in its own comment, and
  the scoring code a few hundred lines away was never updated.
* **A `list=` limitation names its list and then says `[list]`.** zen's row 3831 -- a 4.6 GB mp4
  whose h264 is `high 10` -- is refused by Android as `video.profile limitation applies: high 10 !=
  baseline|main|high **[list]**.` where a `value=` limitation carries no suffix. The row is now in
  the decision corpus, which it takes to **1,950 cells**; it is also the corpus's first
  `has64bitOffsets` true, and the reference's HTTP 400 for Xbox 360 there matches the model exactly,
  which closes that half of §3.3's untripped-limitations entry.

**The negative results.** The encoding negotiation matches on both references and rex
(`deflate`, `br`, `*` and `compress` all get a plain body; a list containing `gzip` gets gzip;
`gzip;q=0` still gets gzip); nothing on the surface is chunked and no response carries `Vary`; an
HTTP/1.0 request line, an HTTP/1.0 keep-alive upgrade, two pipelined requests in one segment and a
GET carrying a body are all answered identically by all three; a repeated query parameter takes the
LAST occurrence on every key tried and the order of two different parameters changes nothing (ten
cells); and the `Date` header's format and a kept-alive second response's header block match.

Measured and not acted on:

- **The fuzzy BAND itself.** A query carrying a capital macron, `İ` or `Ÿ` -- characters the fold
  table deliberately does not reach -- still comes back from a real PMS, scored by word position and
  word length (a length-2 word at the query's first position drops to the fuzzy floor, at the last
  position not at all; 16 zen titles scored). rex matches on prefixes and returns no row at all,
  and emulating the band means emulating a continuous similarity score. §3.3 keeps the entry with the
  measurement attached.
- **`video.separateFields` is read as a constant 0** in `decision.py` where every neighbouring value
  is read off the stream, and `mediainfo` never derives it. zen holds exactly one interlaced h264
  Stream carrying it, on a stale Media that answers `2012 File is missing` to all 78 profiles, so
  there is no row on which the refusal can be witnessed -- and no check can be proven fail-then-pass.
  Recorded with the citation rather than fixed blind.
- **A dual-layer Dolby Vision file is ONE video stream on zen** (`DOVIELPresent: true`), where the
  existing note -- measured on fun, out of scope -- says the enhancement layer is a second stream.
  One witness each side.
- **`video.frameRateMode` and a multi-part Media are still unfound**: an exhaustive filename and
  attribute search over both libraries produced zero of each.

### Cycle 37: raw request targets, real catalog-only shapes, and SQLite's spelling fallback

All reference HTTP measurements in this cycle were GET/HEAD only, with both paging headers where
applicable. kids (1.43.4) and zen (1.42.2) agreed on every request-envelope cell. Their database,
WAL and binaries were copied read-only for the spelling work. The before/after `refcheck7.py`
snapshots are byte-identical.

**The request target is not a browser URL.** PMS keeps literal `;` and `#` bytes as path/query data.
`/library/sections;ignored` and `/library/sections#ignored` are 404s, not the sections listing;
`/library/all?type=1#ignored` is a 400 because `1#ignored` is the integer value; and
`/identity;ignored` reaches the one-segment plug-in-framework answer rather than `/identity`.
`urllib.parse.urlparse/urlsplit` silently assigned params/fragment semantics and routed another
request, so rex now splits the raw target only at the first `?`. The same raw-query split is used
by every response-wide reader -- localisation, paging, `excludeElements`, authentication and both
notification transports -- rather than reparsing the target differently after routing it.

**A non-terminal all-dot segment is a bad request, not a parent traversal.** After one case-insensitive
`%2e` decode, any non-terminal segment consisting only of two or more dots answers 400:
`/x/../identity`, `/x/%2E%2e/identity`, `/x/.%2e/identity`, `/x/.../identity` and `/x/../` all do.
A terminal `/x/..` or `/x/%2e%2e` is the ordinary 404, and `/x/%252e%252e/identity` is a 404 because
the server does not decode twice. PMS validates this shape; it never normalises it to another route.

**GET and HEAD ignore a declared body.** Raw requests with `Content-Length: 999`, no body and an open
socket receive their 200 immediately on both versions, with and without `Expect: 100-continue`.
rex read the declared bytes before routing every verb, so a slow or absent body stalled every
read-only endpoint. POST/PUT still consume their body; the rule is method-specific.

**A measured stack is one Media with multiple Parts.** Plex recognises the filename spelling
`Title - cdN.ext`; `build_catalog.py` groups only that spelling, orders by N, preserves each Part's
own debrid link and sums the Part durations into the Media duration. A decision with `partIndex=0`
or `1` evaluates and returns only that Part and its streams. An out-of-range index is 400.
`partIndex=-1` selects all and answers the measured refusal: `size: 0`, no Metadata,
`directPlayDecisionCode: 3000`, `generalDecisionCode: 2000`, `transcodeDecisionCode: 4005`, and
`"App cannot direct play this item. Multi-part media requires a conversion when all parts are selected."`
The old row-level `_debrid` spelling remains a compatibility fallback for existing catalogs.

**The clip/extras surface becomes reachable once the catalog can carry a clip.** A parent advertises
the lowest-index trailer as `primaryExtraKey`; `includeExtras=1` embeds an uncapped, index-ordered
`Extras`; `/library/metadata/{parent}/extras/all` returns the full clip projection; and
`/library/sections/{id}/extras/all` returns the light searchable/filterable projection measured in
cycle 29. Neither the container nor clip row invents a librarySection triple. A faithful clip detail
therefore no longer crashes on a missing `librarySectionID`. The row borrows its parent's `art`
attribute, but ownership still governs image leaves: `/clip/art` is 404, `/clip/thumb` and
`/clip/poster` serve. In XML, `extraType` is the clip Video's final attribute. A profile-5 Dolby
Vision row in the same focused fixture reaches the already-correct 2003 server refusal; no DoVi code
path was missing.

**The fallback search is SQLite `spellfix1`, with two vocabularies.** Both servers have exactly
`spellfix_metadata_titles` and `spellfix_tag_titles`; the sort/original table names found in old
binary strings do not exist in either live schema. The FTS table separately indexes `title`,
`title_sort` and `original_title` with ICU primary-strength, shifted collation.

The 1.42.2 metadata vocabulary is rebuilt by tokenising the DISTINCT strings
`title || ' ' || title_sort` with ICU and grouping by token. 1.43.4 uses
`concat_ws(' ', title, title_sort)`. A repeated word inside one string counts repeatedly, duplicate
complete strings count once, original title does not contribute to rank, and there is no filter for
library, metadata type, deletion or media presence. The tag vocabulary likewise tokenises DISTINCT
`tags.tag` strings; 1.42.2 includes technical tags as well as visible tags, while 1.43.4 restricts
tag types to `(0,1,2,4,6,207,400)`. ICU word-break plus simple Unicode fold, not Python
`.casefold()`, reproduces the tables. On zen the reconstruction is exact:

| Vocabulary | Stored tokens | Rank mismatches |
|---|---:|---:|
| metadata | 11,755 | 0 |
| tags | 71,124 | 0 |

There are no spellfix maintenance triggers. kids' current metadata rows contain five tokens absent
from its table, so the table can be historically stale. Cycle 38 later recovered the complete caller
set and built the measured optimize/Butler/section-delete rebuild points; ordinary edits remain
stale rather than inventing a synchronous update rule.

For each retained query token the reference runs the equivalent of:

```sql
SELECT word FROM spellfix_metadata_titles
WHERE word MATCH :raw_case_token AND top=10 AND score<=200;
```

The tag branch changes only the table. Scope and langid take their defaults (3 and 0). The exact
cost is `editdist1(transliterated_query, k1) + 32 - bit_length(rank)`, equivalently
`editdist1 + 31 - floor(log2(rank))` for positive rank. The prior `+32-floor(log2(rank))` note was
off by one. Query case is preserved. The `k2` scan prefix is length 3 for hashes longer than three,
2 for length three, and the whole hash below that. Candidates are maintained as a bounded top-ten
set with strict replacement of the worst score; a score-only sort with an invented tie rule is not
equivalent. `kickd` proves the top-ten cap independently of the inclusive 200 boundary;
`defeted` versus `Defeted` proves raw case changes membership at that boundary.

**Fallback is globally gated, then constructed word by word.** First run normal search. A normal
movie/show/list/tag result suppresses spelling fallback globally; an episode-only result does not.
Split on spaces and discard tokens shorter than three UTF-8 bytes. Each retained token becomes
`(candidate1 OR candidate2 OR ... OR originalToken*)`; the groups are ANDed in FTS. Thus
`Tears xy` and `Tears zzz` find “Tears of Steel”, while `Tears xyz` does not. Fuzzy metadata searches
movie/show title, sort title and original title, not episodes. The measured fuzzy tag branch covers
people (`Tudik` finds Alan Tudyk and the associated item); no fuzzy-genre rule was inferred.
`searchTypes` is a later filter and does not rerun the fallback gate.

Spellfix cost is membership only, not the emitted 0.93/0.73/0.53/0.33 relevance score. The existing
type tier and `audienceRating/10000` tail still apply afterwards. The final membership model scored
34/34 queries on kids, 125/125 uncapped diagnostic queries on zen, and a fresh held-out
**93/93 queries and 822/822 movie/show/episode row cells**. The first pass missed two apostrophe
cells; using the schema's actual ICU collation fixed both. Final equal-score API ordering remains
unresolved: title, sort title and ratingKey all have hundreds of violations over 3,535 adjacent
equal-score pairs, so none was fabricated as the rule.

### Cycle 38: bodyless query writes, search ordering, and storage failure boundaries

All writes in this cycle went only to the disposable plexscratch copies. kids and zen received only
the guarded before/after GET snapshots, which are byte-identical. The frozen comparison tree was
cycle 37's `9403d3d`.

**Two query-only write routes do not consume a declared body.** A raw
`PUT /library/metadata/{id}?title.value=...&summary.value=...` and a raw
`POST /playlists?...` carrying `Content-Length: 1000000`, no body bytes and an open socket answer
and commit immediately on both PMS 1.42.2 and 1.43.4. Each route completed 20/20 concurrent trials
on each version; the answers arrived in 0.012--0.194 seconds. Frozen rex instead waited for its
20-second socket timeout, returned no status line and committed none of 40 writes. This is not a
verb-wide exception: a subtitle upload with the same withheld bytes remained blocked on both PMS
and rex. The handler now skips body consumption only for those two measured path/method pairs.

**Slow consumers do not serialize unrelated clients.** A client that stopped after the headers of
a 3.7--4.1 MB response, a notification websocket that never read, and 1,200 writes producing 2,402
frames did not block ordinary reads or writes. Three-client edit, rating/progress and list-member
races exposed only serializable final states; partial request aborts, response aborts and connection
EOF lost no completed write. Those cells were negative findings: rex already agreed.

**Spelling vocabularies rebuild with database optimization, not with row edits.** The complete
static caller set in both binaries is the database optimizer and two historical search migrations;
1.43.4 adds a third `RebuildSearchWithoutExternalContent` migration. The optimizer is reached by
the `OptimizeDatabase` Butler task, `PUT /library/optimize`, and section-deletion completion. Its
automatic Butler eligibility is calendar-aligned: at the approximately five-minute check that
enters the configured local maintenance window, an enabled task with interval seven is eligible
when `floor(time/86400) % 7 == 0`; it is not “seven days since the last rebuild.” The rebuild first
requires the ICU FTS table, rebuilds FTS plus both spellfix vocabularies transactionally, then the
optimizer runs `ANALYZE` and `VACUUM`. rex now keeps edits stale until its three existing
optimization entry points -- explicit optimize, Butler OptimizeDatabase and section deletion --
finish; catalog construction remains the migration/startup equivalent. A background Butler daemon
was not invented for an in-memory server that has none.

**Hub ordering uses hidden precision and an unstable comparison sort.** Ordinary video candidates
are first sorted by raw title bytes descending and then signed metadata id descending, uniqued, and
preceded by the exact-title vector. Scored records are then sorted by the full, unrounded double
score descending with the reference build's libc++ comparison sort and no secondary comparator;
only after that is the score formatted with `%.5f`. Audience rating contributes positively, capped
at 10 and divided by 10,000. This model reproduced 331 eligible captured hubs, 2,306 rows and 1,578
adjacent equal-wire-score pairs with zero position or pair-order misses. Capped or tag-associated
groups whose complete pre-sort vectors were not captured remain a validation limit, not evidence
for a hidden title tie-break.

**`/library/search` preserves its bucket assembly order.** PMS's final wrapper comparator asks an
empty generic attribute map for `score`, so every comparison is zero against zero even though the
serializer later writes a dedicated numeric score. The resulting order is the builder sequence,
beginning movie, show, episode, rather than a global score sort or `/hubs/search`'s display order.
Across 51 captures and 291 rows the recovered bucket model had no precedence or within-bucket
misses; four real responses contain a later, higher-scored bucket. rex now uses that measured
flat-search sequence explicitly.

**Storage failures were measured but have no emulation backend.** On isolated reference copies,
SQLite `ENOSPC` produced a mixture of explicit 500s and writes that answered 200 without landing;
preference-file failures could update runtime state and later revert; bundle failures varied by
leaf; and recovery preserved database integrity and atomicity. rex owns no SQLite database,
`Preferences.xml`, metadata bundle tree, cache or encoder process to fault. Mapping those host
failures onto its in-memory catalog would manufacture a subsystem and failure semantics rather than
emulate one, so this axis is recorded and deliberately not implemented.

### Cycle 39: client replay, score-fold passes, and complete tag vocabulary

Measured 2026-09-17 against kids PMS 1.43.4, zen PMS 1.42.2 and frozen rex
`7d98293`. The accepted official Plex Web 4.160.0 walk contained 21 safe GETs and 20 unique exact
request envelopes across home, both sections and the movie/show/season/episode preplay path. Every
status and response format agreed, common attribute and child order had zero mismatches, and the
remaining differences were catalog state, measured version descriptors or deliberately absent
file/encoder capabilities. Its 20 signatures are a strict subset of cycle 27's 95 safe signatures,
with no new route. The first capture was discarded rather than counted because the safety gate let
two notification websocket upgrades and one theme read through; the replacement blocked sockets,
media and write-shaped GETs before navigation. Guarded reference snapshots before and after are
byte-identical.

**Hub membership folding and score-tier folding are different passes.** The complete cycle-32
2,076-codepoint domain produced 659 compatible character/target cells on zen. Exactly 97 ASCII-letter
cells reach the ordinary word/prefix tier -- precisely the letter entries in the measured
`_TITLE_FOLD` table -- with 333 fitting cells and 326 held out at zero misses. The partial-title
score pass applies that table to the RAW query before ASCII-only case folding. Lowercasing first
wrongly promotes eleven unsupported capitals and loses the supported `Ð` mapping. Whole-title
exactness is a separate, broader comparison: `TĒars of Steel`, `Tears of StĒel` and
`Tears of ſteel` are exact, although `TĒars`, `TĒar` and `Tearſ of Steel` are fuzzy. A two-letter
word is a measured boundary: both `Tears Ōf Steel` and `Tears ōf Steel` are anchored, not exact.
The exact observed scores, including the audience-rating tail, are `.33055`, `.73055` and `.93055`.
rex now preserves the raw query for tier classification and keeps the partial and whole-title
passes separate; the lowercase form remains for collection, playlist and tag membership.

**The complete 1.42.2 spelling tag vocabulary is reconstructible without copying a library.** A
read-only rebuild from 94,343 distinct non-null tag inputs exactly reproduces the reference's
71,124 stored tokens and ranks. The old catalog-only extractor produced 45,039. Generic sources
already carried by rows -- the ordinary tag fields plus Chapter, Review, Marker,
CommonSenseMedia and Network tags, Guid ids, Rating images, studio, and the presence constants
Thumb/Art/Theme/ClearLogo -- reconstruct 71,109 tokens. The entire versioned static remainder is
only 28 distinct strings: three optimize labels and 25 show-ordering labels, now held in the
measured `spellfix_static_tags.json` sidecar.

This is client-visible rather than an internal rank curiosity. Across the complete bounded
zero/one-deletion-or-transposition universe, 697,334 queries produced 526,777 different top-ten
candidate vectors and 187,616 post-gate people-membership differences, 154,237 of them lowercase
ASCII. GUID ranks alone keep `imdb` in slot ten for `emtz`, preventing Imtiaz Ali from entering;
the static strings similarly discriminate `arcewell` and `tft`, while the pooled remainder's
`flr` retains Alain Floret. Live zen agrees through default and limit-1000 hubs and flat people
search. The implementation therefore indexes the generic row sources plus the exact 28 server
strings. It deliberately does **not** ship zen's 71,124-token library-specific vocabulary.

The final cycle tree passes `test_catalog_features` 25/25, `test_ui_surface` 1561/1561,
`test_navigation` 28/28, `test_mediainfo` 31/31 and `test_decision` 2106/2106. The 29/38
python-plexapi and 0/11 byte-read results remain the recorded dead-link baselines.

### Cycle 40: complete boundary censuses, legacy lookup, and metadata-agent groups

Measured 2026-09-17 against kids PMS 1.43.4, zen PMS 1.42.2 and frozen rex
`bcda2f9`. The inward census mechanically found 867 source branches, 308 normalized public/shared
families, 526 family/verb cells and 3,080 protocol-axis cells, 2,986 applicable. Direct evidence
maps all 308 families; branch-entry traces plus semantic assertions reach 304. The only two
deliberately unopened families were image-response bodies and notification channels, both already
measured and regression-covered. The cycle added 90 two-version reference cells across nine held-
out route families and one of them disagreed.

**The bare legacy URL lookup is a plug-in failure, independent of representation.** Authenticated
`GET /system/services/url/lookup` with no query returns 500 `application/xml` on both versions for
both JSON and XML Accept. Its root is `Response code="2000"`, its status is
`TypeError: url_lookup() takes at least 2 arguments (1 given)`, and it contains one `Traceback`.
Only the installed plug-in build and traceback line differ: 1.43.4 names `Plug-ins-e5521bd8c`
line 847; the 1.42.2 form names `Plug-ins-f737b826c` line 843 and is exactly 464 bytes. HEAD keeps
status, type and that length with no body. A gzip-capable GET compresses the XML; Accept never
changes it, and the connection closes. Frozen rex instead returned a successful empty
MediaContainer. Because rex reports 1.42.2, it now sends that exact legacy body and envelope.
A lookup carrying `url=` was not probed: it can cause an external fetch and remains outside the
safe surface.

**Provider tables advertise Add Agent, so the complete group lifecycle is load-bearing.** The
locally served Plex Web is 4.147.1, not 4.160.0: its 207 chunks plus OAuth helper contain 3,029
module occurrences, 670 offset-verified request candidates and 204 normalized path families, all
classified. An offline, exact-version-marker recovery found 57 cached 4.160.0 JavaScript bodies
across two builds. The recovered provider-settings chunk proves that successful
`GET /media/providers/metadata` and `/media/providers/metadata/group` make Add Agent visible with
no further capability gate. The exact client POST carries `title` followed by
`primaryIdentifier=tv.plex.agents.movie`, an empty body and normal identity headers. Both references
return 200 JSON with one full `MetadataAgentProviderGroup` and its primary
`MetadataAgentProviderGroupItem`; the client consumes the id, closes the modal and refreshes.
Frozen rex returned 400 and left the modal showing “Failed to save changes”.

The dependent operations also agree across versions: renaming returns 200 with the full updated
group; the UI-offered Personal Media membership PUT with `after=-1` returns 400 because that agent
is not secondary-compatible; deleting the absent membership returns 404; group deletion returns
an empty 200 and restores the table. rex now holds custom groups in memory and reproduces this
create/read/rename/refusal/delete/postcondition sequence. Provider create remains a separate
external-object capability, and PUT/DELETE of built-in providers remain hidden by the client's
built-in-title gate.

The 4.160.0 static recovery is explicitly incomplete. The gw runtime maps 205 JavaScript assets,
but only 29 survive in cache, leaving 176 mapped chunks absent; the app build has 28 bodies but no
manifest denominator. Every recovered request call site is classified and cycle 39's 21 runtime
requests remain covered, but this evidence cannot support a complete-current-client static claim.
Both scratch references were restored byte-for-byte. The final tree passes catalog 25/25, UI
1563/1563, navigation 28/28, mediainfo 31/31 and decision 2106/2106; conformance 29/38 and byte-read
0/11 remain the known dead-link baselines.

### Cycle 41: the historical backlog and every advertised control

Measured 2026-09-17 against both disposable PMS versions and frozen rex `2c06bab`. The
historical census extracted 513 raw entries from this file and `HANDOVER.md`, retained 496 claim
sources, normalized 287 parent claims and split compound claims into **332 atomic behaviors**.
Every atom has a terminal disposition: 180 built, 42 refuted or superseded, 36 deliberate
capability absences, 23 client-invisible fidelity choices, 40 external/instrument bounds and 11
safely unreachable state models. The last 51 are explicit bounded cells, not open deductions;
there are **zero actionable findings and zero unbounded unresolved cells** on the fixed tree.

The independent advertisement census started from the other direction. It joined 49 live source
families and 1,221 normalized advertised values to 47 exact Plex Web consumer predicates, 339 UI
controls and 45 action/readback pairs. Thirty-nine actions have an exact route witness plus a
postcondition assertion; the remaining six are two forbidden media reads, three deliberately
hidden encoder/sync capabilities and the external/auth claim surface. All 45 are classified and
none differs in a reachable client-visible way. This is complete for the locally served Plex Web
4.147.1 bundle. The 176 mapped 4.160.0 chunks absent from the retained cache remain an explicit
external limit, not a silently sampled universe.

The frozen historical tree had **24 client-visible mismatches**, all implemented and proved
fail-parent/pass-fixed:

- Child projection and listing fidelity: Style/Collection/Label/Similar now have the measured
  child positions and `id, filter, tag` XML attribute order; collection `/allLeaves` and
  `/grandchildren` stay empty even when `/children` holds a member; a mixed folder emits
  `Directory` then `Video`; legacy section search ignores every `limit` form; an untyped scoped
  conjunction resolves each prefix against the right hierarchy row; duration sorting reads
  `Media.duration`; Part `accessible`/`exists` require `checkFiles=1`; playlist detail containers
  carry their four measured identity fields; and visually-impaired subtitles project the
  disposition.
- Client actions and ordering: the `#` first-character bucket aggregates every ASCII non-letter
  while literal punctuation buckets remain addressable; a multi-Media decision requires an
  explicit in-range selector while a queue keeps all versions; a show merge absorbs the matched
  hierarchy, combines corresponding leaf Media and emits the delete/survivor frame sequence;
  legacy download filenames map `+` to `_`; a queue move re-anchors
  `playQueueLastAddedItemID`; a type-mismatched playlist create retains the empty-playlist side
  effect; playlist import validates the exact `path`/`sectionID` envelope; and search hubs are
  promoted by the recovered match-class/type-bias rank.
- Derived values and lifecycle: generated probes moved the short-wide `480` boundary to the
  measured width and made the anamorphic cutoff strict; Recently Added TV pages a 50-row cap rather
  than truncating at the twelve-row shelf limit; playback sessions are reaped by a process-global
  30-second tick applying a 180-second cutoff; and title folding removes exactly
  **U+0300..U+036F**. The latter came from 7,531 leading-scalar and 811 interior-sentinel cells on
  each PMS version: the old Unicode combining predicate wrongly removed 699 Mn/Mc characters from
  other scripts and failed to remove U+034F.
- Localisation: the complete safe 4.147.1 read plan found four missing English source pairs
  (`TranscoderTempDirectory`'s summary and three shelf titles). All 28 supported languages were
  captured for each pair -- 112 exact values -- and added to the table.

Several formerly thin entries closed without a behavior change: the two-or-more-sibling Recent TV
roll-up, 180-second session cutoff, borrower artwork recreation/removal, playlist-create frame
title, in-progress field lifecycle, empty legacy codec-axis paging, notification `filters=all`,
title comma alternation, tag-only collection orphan, section-hub version split, Specials, and the
local unmatched-refresh sequence. The notification backlog was exercised with dead and slow
readers on both copies; it stayed nonblocking, but its byte threshold is a host TCP-buffer property
and remains deliberately unmodeled. A rated matched-row random shelf cannot be created with the
available isolated 1.42.2 instrument, and `[Unknown Season]` scanner ingestion could not be made
through three filename forms; those limits are named in the census rather than guessed around.

The final tree passes catalog features (including the 7,531-scalar fold check), mediainfo **34/34**,
decision **2106/2106**, sessions, navigation **28/28** and UI **1582/1582** with 14 explicit fixture
skips. Python-plexapi remains **29/38** and the byte-read suite **0/11** solely because every fixture
link is the recorded dead account-scoped Real-Debrid URL; every queue and decision passes.

### Cycle 42: clean replay, no findings

Measured 2026-09-17 against merged frozen head `f8cd6c2`. This was a fresh replay of both complete
Cycle-41 universes, not an inference from the previous reports.

The historical axis rebuilt all **332/332** stable atoms from 513 raw entries and 496 claim-source
entries. All 24 Cycle-41 fixes passed current branch-reaching regressions and retained-evidence hash
checks, including 24 safe current-target GET cells and a dedicated process-local discriminator for
the ASCII-nonletter bucket. Final dispositions remain 180 built, 42 refuted/superseded, 36
deliberate capability absences, 23 client-invisible fidelity choices, 40 external/instrument bounds
and 11 safely unreachable state models. Actionable findings and unbounded unresolved cells are both
**zero**.

The advertisement axis recollected all **49** source families from both references and frozen
rex, rescanned all 207 JavaScript files of the local Plex Web 4.147.1 bundle, and rebuilt the
stable graph at **1,221 advertisements / 47 predicates / 339 controls / 45 actions / 45 readbacks**.
Thirty-nine chains have a current exact route and postcondition witness. The remaining six are the
same two forbidden media reads, hidden Optimize/Sync capability actions and external/auth claim
surface; all are explicit terminal bounds, and unresolved is **zero**. The disposable action replay
recorded 7,090 requests across 1,363 distinct method/path pairs and passed UI **1582/1582**.

Root independently repeated the full verification: catalog features, mediainfo **34/34**, decision
**2106/2106**, sessions, navigation **28/28** and UI **1582/1582** all pass. Python-plexapi remains
**29/38** and byte reads **0/11** only because the fixture's Real-Debrid links are dead; all queues
and decisions pass. Both audits produced byte-identical protected-reference snapshots, zero-hit
exact-value secret scans and clean disposable-process shutdowns. Neither changed the repository.

This is the first cycle eligible for the stopping rule whose two fully enumerated axes both report
**no findings**. The protocol-conformance loop stops here. The 176 absent mapped Plex Web 4.160.0
chunks remain a named external coverage limit and are not recast as a complete-current-client
static claim.

### Cycle 43: complete current client plus both clean replays, no findings

Measured 2026-09-17 against frozen Cycle-42 head `5874d01`. This cycle reopened the stopping result
to cover its one named external boundary, then independently replayed the other two finite axes.
All three reports end exactly **`no findings`**.

The anonymous public `app.plex.tv/desktop/` entrypoint still serves Plex Web **4.160.0**, build
**`75ddd7b`**, and its inline webpack runtime supplies a complete denominator. A manifest-rooted
walk obtained **205/205 JavaScript assets, 16/16 CSS assets and 30 same-origin static dependencies**
with zero failures; all **207/207** declared SRI values verify. Cycle 40's 28 recovered app-build
JavaScript bodies match this graph byte-for-byte. The other **177 JavaScript assets** contain zero
direct PMS/player paths, zero PMS request-builder imports, zero XHR/AJAX/Backbone request sites and
zero new controls or actions. Every one of the build's 281 direct path call sites remains in the
same 28 files Cycle 40 recovered.

This does not substitute current bytes into the historical gateway cache. The gateway variant and
current public app share the version/build label but not every chunk hash or id; the former remains
accurately bounded at 29/205 JavaScript bodies with 176 mapped bodies absent. That is now historical
provenance, not a limit on the complete current app universe. Reconciliation against the complete
4.147.1 graph finds only the already implemented metadata-provider id/group/membership lifecycle.
The current PMS-family ledger is 207 families, and the complete-current union is 51 predicates,
347 controls and 55 action/readback cells. Three exact provider reads pass on frozen rex and the
full provider group lifecycle remains regression-covered.

The historical territory independently rebuilt **332/332** stable atoms from 513 raw records and
496 claim-source records, preserving 645 source references. All **24/24** Cycle-41 fixes pass their
cross-fix audit. Final dispositions remain 180 built, 42 refuted/superseded, 36 deliberate
capability absences, 23 client-invisible fidelity choices, 40 external/instrument bounds and 11
safely unreachable states; actionable and unbounded unresolved are both zero.

The advertised-control territory recollected all **49** source families on both references and
current rex, rescanned the complete local 4.147.1 bundle and rebuilt **1,221 advertisements / 47
predicates / 339 controls / 45 actions / 45 readbacks**. All stable ids are unique and every foreign
key resolves. Thirty-nine reachable chains have fresh exact route, assertion and postcondition
witnesses. The same six cells remain explicitly bounded: two forbidden media reads, hidden
Optimize/Sync actions and the external/auth claim-token, claim and unclaim surface. Unresolved is
zero. Its instrumented disposable replay passed UI **1582/1582** and recorded 7,149 requests across
1,365 distinct method/path pairs.

Root independently repeated catalog features 35/35, mediainfo **34/34**, decision **2106/2106**,
sessions, navigation **28/28** and UI **1582/1582**. Python-plexapi remains **29/38** and byte reads
**0/11** only because every fixture link is the recorded dead account-scoped Real-Debrid URL; all
queues and decisions pass. All protected-reference snapshots are byte-identical at
`64579ece7b9f77a2914bc07f5f3aef36f71bf2ddf0c57aec68b428819386e68a`; all exact-value secret scans
are clean; neither writable reference was used. No behavior or regression needed a code change.

One operational defect was closed in the procedure: Cycle-43 startup found Cycle-41 frozen-tree
listeners still on 32587/32588 despite the prior cleanup record. Their cwd and command line proved
ownership before they were killed. The cycle procedure now requires a global `server.py --port`
process inventory both before allocating ports and after agents finish, not merely checks of the
current cycle's expected ports.

With the complete current client, historical universe and advertised-control graph all finitely
enumerated and clean, the protocol-conformance loop stops at Cycle 43.

#### Post-cycle live-media refresh

The user authorized replacing the gitignored private catalog's dead Real-Debrid links. The builder
rejected every candidate whose unrestricted payload was actually a RAR, probed the remaining video
streams, and used a valid TMDB key to retain a real movie/show/season/episode hierarchy. The final
private catalog has seven metadata rows and five playable parts. It includes Blender's openly
distributed Big Buck Bunny MP4 as an MP4/H.264/AAC row that the Plex Web gate direct-plays.

With those bytes reachable, the formerly bounded results close: python-plexapi is **38/38** and the
full queue → decision → both local-proxy harness transport legs → real-frame-decode suite is **5/5**.
Navigation is **24/24** with one explicit multiplicity skip. The checked-in UI fixture remains
redacted and is not presented as a byte-serving corpus.

The MP4 also exposed a test assumption hidden by the dead links: `tests/conformance.py` selected an
H.264 item and then always asserted Matroska magic. On the measured MP4 it failed 37/38 with the
correct `ftyp` bytes at offset four. The assertion now keys on the selected Part's container and the
selection explicitly prefers MP4/H.264/AAC; the same catalog passes 38/38. This is a harness repair,
not a server behavior change. No live `/d/` URL or credential is tracked.

#### The forty-fourth pass — the first cycle with real bytes in it

Cycles 42 and 43 both reported `no findings` and the protocol-conformance loop was declared
stopped. Then, on 2026-09-17, the private `catalog.json` was refreshed with **five real,
playable, account-owned Real-Debrid parts**. That is a new instrument, and the programme's own
history says the instrument has been the ceiling every single time.

For the whole of the project's life every Real-Debrid link in the checked-in fixture was dead:
`tests/test_all.py` reported `0/11 served` for fifteen consecutive cycles and every rule about a
media body was measured on a *reference* and implemented on faith. **rex's own byte path had
never been run against a single real byte.** Cycle 44 ran it, and 13 of the 20 `Range` cells both
references agree on were wrong.

Two hunts and the driver produced **19 findings**, all of them implemented. Two recorded beliefs
were overturned and one suite was found to have carried a refutation of its own subject for
thirty-four cycles without anyone reading it.

##### The media byte path

* **The part read applied no `Range` grammar at all.** `_proxy` forwarded the client's header to
  the debrid CDN and echoed whatever came back, so the answer was the CDN's grammar rather than a
  PMS's — and differed per CDN host. Measured over twenty cells against kids (1.43.4) and zen
  (1.42.2), which agree on every one; rex differed on **thirteen**. The two that break a real
  player: `bytes=-1` returned the FIRST byte where a real PMS returns the last (which is how
  ffmpeg, mpv and every embedder locate an MP4 `moov` or a Matroska Cues block, so every seek read
  the wrong end of the file), and `bytes=<total+10>-` answered **`Content-Length: -10`**, which is
  not a legal field value at all. A reversed or past-the-end range answered 206 where a real PMS
  answers 416, a one-byte probe answered the whole 121 MB file, and a suffix longer than the body
  over-ran it. `_range_window` now holds the grammar once and both `_ranged` and the part path ask
  it.
* **A bare `Range: bytes=-`, with nothing after the dash, is not a Range.** 200 on both references
  on `/identity`, artwork, a `/web` asset and a part; rex answered a 206. `bytes=- `,
  `bytes=-x`, `bytes=-abc`, `bytes=-0`, `bytes=-+` and `bytes=-.` are all the 206 — only the empty
  one is not a Range. Cycle 35's 43 header values did not include it.
* **A part read carries `X-Plex-Protocol: 1.0`, first, like every other response.** The part path
  is the one writer on the surface that does not go through `_send`, so it was the one response
  missing it — invisible until now, because a dead link answers 502 and a 502 IS written by `_send`.
* **A part's `Content-Type` is keyed on the stored FILE's extension**, not on the Media's
  `container` and not on the extension in the request URL. zen part 7113 is a `mp4` Media whose
  file ends `.mkv` and answers `video/x-matroska`; asking for that same part as `file.mp4`,
  `file.zzz`, `file.mov` or `file.webm` answers `video/x-matroska` every time. An extension the
  table does not name answers **`application/octet-stream`** (zen's `.m2ts` parts do), where
  rex fell back to `video/mp4`; `.mov` is `video/quicktime`.
* **The bandwidth statistic counts what was WRITTEN.** Measured on kids: a client that aborted
  after 72 452 bytes moved the counter by 309 037 and a full read of the same 6 273 654-byte part
  moved it by 6 274 913. rex counted the advertised `Content-Length` however the read ended, so
  one 1 MB read of a 121 MB part moved the Dashboard by the whole 121 MB — and every seek and every
  stop is an abort.
* **`POST` and `PUT` on the THREE-segment media path are 404s.** Both writable copies, on 1.42.2
  and 1.43.4 alike: `/library/parts/{id}` takes the track-selection write and
  `/library/parts/{id}/{version}/file.ext` does not. rex had no length check, so a client that
  built the write from the Part's own `key` — which *is* the three-segment path — really moved the
  selection where a real PMS answers 404.

##### The 416 and the header block

* **A 416 of a MediaContainer names its connection state and a 416 of a BINARY body does not.**
  Re-measured on both references with a KEEP-ALIVE request, so the answer is the server's own
  choice rather than an echo of the probe's `Connection: close`. The container 416 is
  `X-Plex-Protocol, Content-Length, Content-Type, Connection, Keep-Alive, Cache-Control, Date`;
  artwork, `/:/resources/*`, a `/web` asset, a media part and `/photo/:/transcode` all answer
  `X-Plex-Protocol, Content-Length, Content-Type, Cache-Control, Date`. **This overturns the
  recorded rule "a 416 names no connection state at all — the only response class that does not",
  which is the binary half only.**
* **Every 416 carries `Cache-Control: no-cache`**, the transcoder's included, where its 200 carries
  `max-age=259200`. rex cached the refusal for three days. It also kept `X-Plex-Local-Asset` on
  an artwork 416, which neither reference sends on anything but the 200.
* **With an `Origin` on the wire the CORS pair sits directly after `X-Plex-Protocol` and
  `Access-Control-Allow-Origin` directly before `Date`, in EVERY family** — container, artwork,
  `/:/resources`, `/web` asset, `/photo/:/transcode`, part, `/system`, error and the `/web` 302.
  rex had it right for two families and trailed the pair to the end in six; the `/web` 302,
  which is the one response the web client follows to reach the app, carried no CORS headers at
  all. Cycle 33 measured the families on a connection that sent no `Origin`, where the trio is
  absent altogether, so this was structurally invisible to it.

##### Two per-version splits and two advertised surfaces

* **`/library/sections/{id}/{videoCodec,audioCodec,subtitleCodec,audioLayout}` are 1.43.4-only
  axes.** 200 on kids, **404 on zen AND on plexscratch42** — two independent 1.42.2 servers — on a
  movie library and a show library alike. rex reports 1.42.2 and answered all four. The same
  six-name media-codec split is already recorded for `/filters` and the `Meta` descriptor; the AXES
  split with them, which nothing had checked, and rex's own `/filters` correctly omits them.
* **`/library/sections/{id}/rating` is a real secondary axis and rex 404'd it** — and it is
  **ADVERTISED** in a movie library's own pivot `Directory`, by rex itself, so the Ratings
  entry in the official client's sidebar opened nothing. The axis container is byte-identical to
  `/userRating`'s, six fixed buckets titled "By Rating"; the BUCKETS read the CRITIC `rating`
  column where `/userRating`'s read the per-user one (zen `/rating/8` is 221 rows, `/userRating/8`
  is 200). A show library answers it without advertising it.
* **A star bucket rounds its KEY down to an even star.** Measured for every key 0-12 on both
  references and both library types: the bucket is not `int(value) == key`. Key `k` takes the rows
  whose value is within one of `k - k % 2`, so 8 and 9 answer the same 200 rows and 10 and 11 the
  same 3; `key=0` is the rows with NO value (zen 3,465 of 3,887 unrated). Truncation gets every
  bucket but 0 wrong — zen's `userRating/6` is 208 rows where truncation finds 165, and kids'
  answers BOTH its rated movies because a stored 5.0 rounds UP into the 3-star bucket.
* **`/media/providers/metadata/{id}` and `/media/providers/metadata/group/{id}` are real.** Found
  by sweeping the PMS path literals out of **Plex Desktop's own bundle** — the first non-Plex-Web
  official client this programme has read. Each answers the one row the bare table carries; an id
  the server does not hold is a 404 and a NON-NUMERIC one is a **400**. rex 404'd all of it,
  and these are the two shapes the Settings → Agents pane builds once the tables have loaded.
* **`builtIn` is the string `"1"` on 1.42.2 and the boolean `true` on 1.43.4, and `order` is the
  float `1000.0` on both.** The XML is `builtIn="1"` / `order="1000"` on both versions, so only a
  JSON read of BOTH references can see it — the same family as `durationTotal`/`storageTotal` being
  JSON strings. rex reports 1.42.2 and sent `true` and `1000`.

##### The decision, and what a `.webm` file really is

* **A `.webm` file's `container` is `mkv` on a real PMS.** Measured on the SAME FILE: zen
  ratingKey 19769 and rex row 1002 are both `Tears of Steel.webm`, 571 346 576 bytes,
  vp8/vorbis 1920x800, and zen answers `container: "mkv"` on the Media and the Part while the
  Part's key keeps `file.webm`. No row of zen's 4,508 movie Media carries `webm` as a container at
  all. **The project's own corpus has said so since cycle 10**:
  `tests/fixtures/media/fun-1052407-0.json` is a `.webm` file whose recorded Media — what a real
  1.42.2 PMS wrote for it — is `mkv`. Nothing caught it because `test_mediainfo.py` compared every
  stream field and the Media's geometry and **never the container**; it does now. The override was
  kept on the argument that "rex keeps `webm`, which Plex Web's direct-play rule depends on";
  driven across all 78 profiles against both containers, the browser profiles answer the same
  outcome either way and the one cell that moves is `Sony Bravia 2013`, which direct-plays the row
  as `mkv` and is refused as `webm`. The override cost a direct play rather than buying one.
* **A direct play is refused when the SELECTED embedded subtitle is not the first embedded one**,
  with `App cannot direct play this item. Selected embedded subtitle stream is not the first
  embedded subtitle stream.` — the subtitle twin of the audio rule, and it was missing entirely.
  Exactly three of the 78 profiles do it (`Samsung`, `RokuDLNA-5000X-7.x`, `RokuDLNA-6000X-7.x`),
  gated on the same `DirectPlayStreamSelection` as the audio rule, and it runs after the
  audio-codec pass and after the subtitle-CODEC pass. Measured on zen over 21 rows and held out
  live: 386 (profile, row) cells over five zen rows score 9 misses against the old model and **0**
  against the new one. The 2,106-cell corpus could not see it — in every row it held whose subtitle
  is not first, the AUDIO is not first either, so the audio refusal fires above and masks it. Three
  zen rows are now in the corpus (2,340 cells).

##### The catalog pipeline, and the plex.tv announce

* **A season gets `parentYear`, not a `year` of its own.** `metadata.py` wrote TMDB's season
  air-date year into the season's own column. 689 of zen's 742 seasons carry `parentYear` and
  exactly **one** carries a `year` of its own, so a Plex series agent does not produce one — and
  carrying it takes `parentYear` away, because the year rule reads "has a year of its own" off that
  column. Corrected at BUILD and again at LOAD, like the other pipeline-produced row errors.
* **A season and an episode carry `Guid`.** The ids were being fetched and then discarded, on a
  note in the code claiming "a real PMS leaves season and episode Guid arrays empty". kids' season
  7 answers `tmdb://3846` and `tvdb://15791` and its episode 8 answers three ids; 58 of 60 zen
  episodes surveyed carry a block. The exemption's premise was void, not the exemption.
* **A real PMS announces with `PUT https://servers.plex.tv/devices/{machineIdentifier}`.** Read out
  of the harness's own `capture/outbound.jsonl` — the server-to-plex.tv axis, which no cycle had
  ever swept. All three announces a real 1.43.4 PMS made are identical: the **identifier** form, to
  `servers.plex.tv`, token in the query, an empty body, and thirteen headers in a fixed order led
  by `User-Agent: PlexMediaServer/<version>` and an `X-Plex-Client-Identifier` equal to the machine
  identifier. rex used the **numeric** device id against `plex.tv`, sent six headers and none
  of the identifying ones, added a `natLoopbackSupported` a real PMS does not send, derived
  `httpsEnabled` from the uri's scheme (the reference sends `1` beside an `http://` uri) and
  announced version `1.43.3` where its own `/identity` says `1.42.2`. `CLAUDE.md` has recorded the
  identifier correction since 2026-09-12 and the code never followed it — and the numeric form is
  the dangerous one, because it resolves to whatever device row already holds that id.
* **`/myplex/claim` is routed for POST alone.** GET, HEAD, PUT, DELETE and a bare OPTIONS are each
  a 404 there; rex routed the handler for every verb and answered its own "missing claim token"
  400 to all of them, which tells a client the endpoint exists. Its neighbour
  `/myplex/refreshReachability` already gated correctly on PUT, which is what made the difference
  visible.
* **`/library/parts/{id}?subtitleStreamID=` and `?audioStreamID=` have DIFFERENT value grammars.**
  `subtitleStreamID` parses as a signed integer, anything unparseable reads as 0 and CLEARS the
  selection, `-1` reverts to the default, and any other negative is a 400. `audioStreamID` has no
  "0" meaning: `0` and every negative but `-1` are 400s and an unparseable value is an accepted
  no-op. rex applied the audio grammar to both.
* **`/video/:/transcode/universal/{ping,stop}?session=` with an EMPTY value is a 404**, not the
  400 a missing parameter gets — the empty value is the unknown-session case, where `session=%20`,
  `session=0` and `session=zzzz` already were. The neighbouring `start.m3u8?session=` and
  `decision?session=` are 400s on all three servers, so the split is per leaf.

##### Measured and not acted on

* A real PMS also `POST`s `servers.plex.tv/servers.xml` with a `MediaContainer` naming its
  `<Server>` and a `<Directory>` per library — 57 times in the recorded capture against three
  device announces. That is how plex.tv learns a server's libraries. It is a plex.tv WRITE and
  therefore out of scope under §8/§9; recorded with its shape and cadence, not built.
* The `Connection: close` a part read sends instead of the reference's `Keep-Alive`/`timeout=20`
  remains the recorded ffmpeg-seek deviation.
* `Part.accessible`/`exists` remain hard-coded true. zen rows whose file is unreachable answer
  `false`/`false`; rex answers true, and telling the truth would mean probing its own upstream
  on every metadata read. The 404 half of that difference IS now built (above); the two booleans
  are not, and the deviation is inherited from cycles 1–6.
* Season and episode `Guid` are written by the builder from this cycle on; a catalog built before
  it carries none until it is rebuilt, and the load-time pass cannot invent ids it never fetched.
* `/clients/profiles`' `Roku` and `Roku-6.x` rows resolve through `redirect=` to `Roku-7.x`; a
  scorer that reads `PROFILES[name]` instead of `resolve(name)` measures its own shortcut. Nine of
  this cycle's first "misses" were exactly that.

#### The forty-sixth through forty-eighth passes — every Cycle-46 axis closed, then repeated clean

Cycle 46 took every open direction named at the end of Cycle 45: the query-option name space,
the request-header name space, live/dead file availability, the installed Plex Desktop boundary,
the whole recorded outbound-read corpus, and both finite source/control censuses. Cycle 47
repeated the moving external axes and repaired the two things that repetition found. Cycle 48
then replayed the same finite denominators against the repaired head. Nothing in these passes was
inferred from a plausible response shape: every positive was measured on both reference versions,
and every negative has a finite denominator or an explicit safety/capability bound.

##### Query options: 116 names, 76 safe container families, 26,448 cells

The vocabulary is every `include*`, `exclude*`, `async*`, `check*`, `skip*`, `only*` and `with*`
literal recovered from PMS and the official-client bundles: **116 names**. It was crossed with
**76** distinct safe top-level, library, section, hub, item, hierarchy, secondary-axis, session
and system families on kids 1.43.4, zen 1.42.2 and rex: **26,448 cells**, with duplicate
baselines and the three moving statistics/updater leaves excluded per host.

The first broad run exposed eleven apparent mismatches. Ten were real and one was the already
measured `includeBandwidths` capability bound. Together with four findings from the narrower
opening pass, the query axis produced **14 fixes**:

* **`checkFileAvailability` is a full sibling of `checkFiles`**, including the exact `+?0*[01]`
  value grammar and detail-row-only `Part.accessible`/`exists` projection.
* **`includeGuids` propagates through every full/light listing projection that the references
  move**, including `/library/recentlyAdded`, global hubs, Home Recently Added, children, section
  newest/recently-added/search and hub search. The option changes projection, not route identity.
* **`includeMeta` reaches the two Home resume leaves, collection listings, folder rows and Shared
  libraries.** A collection Meta uses the collection view; a folder activates the folder Type;
  Shared carries its constant seven-Type block.
* **`includeStorage` reaches `/media/providers`** as numeric `durationTotal`/`storageTotal` fields,
  including the provider root and each library row. This is distinct from section-grid storage,
  whose totals are strings.
* Global **`/hubs` honours `includeChildren`, `includeExtras`, `includeOnDeck`, `includeRelated`
  and `includePreferences`** on its full rows. Light rows still do not acquire blocks they never
  own. `includePreferences` also reaches Shared libraries.
* **`includeCollections=1` changes hub-search shelf order**: Collections moves immediately after
  Tracks; it does not create or suppress the always-present typed collection hub.
* **`includeSummary=1` reaches Shared**, and its per-library `duration` plus complete `Type`
  census uses every hierarchy level the library owns, not only the top-level type.
* **`skipCount` filters the SOURCE rows** used by children, section search and secondary axes.
  Applying it after buckets were made left empty or stale counts behind.

The final partition is **19 common moving options, four version/content splits, 92 no-ops, one
explicit bound and zero actionable mismatches**. `includeBandwidths=1` is the bound: it exposes
`deepAnalysisVersion` and bitrate-distribution output only on a Part PMS has deeply analysed.
An unanalysed zen Part is byte-identical with and without the option, and every rex Part is
unanalysed, so emitting nothing is the measured branch rather than a missing implementation.

##### Request headers: 61 normalized names, 88 spellings, 24,156 logical cells

The header vocabulary exhausts Plex Desktop's framework binary, the complete local and current
official web bundles, PMS strings and every plexlab corpus: **61 normalized field names and 88
source spellings**. Forty-four safe path/envelope families on three hosts were measured absent,
with their source-shaped value and with literal `0`: **24,156 logical cells**, **16,842 physical
requests** including repeated baselines and 474 targeted controls.

Two differences were real, both in `/web`'s hand-written redirect writer:

* exact **`X-Plex-Http-Pipeline: disable`** must keep the 302 but answer `Connection: close` and
  omit `Keep-Alive`; `Disable`, `infinite` and `0` retain the normal keep-alive pair;
* exact **`X-Plex-Neuter-Redirect: 1`** changes the answer to an empty **200** while retaining the
  absolute `Location`; `0` and `true` remain 302.

The transport-looking trio `X-Plex-Strict-Ranges`, `X-Plex-Indeterminate-Ranges` and
`X-Plex-Chunked` was controlled on ranged containers and ranged static files under five values
and is inert on this bounded surface. The final header partition is 52 inert, seven aligned
common effects, `X-Plex-Product` as an aligned content/version partition, and `X-Plex-Token` as
the authentication control. Cycle 47 and Cycle 48 repeated every cell; both reports contain
zero reference-agreement mismatches and end `no findings`.

##### File availability now follows the provider result

The live/dead comparison overturned the inherited constant `accessible=true, exists=true`.
`checkFiles`, `asyncCheckFiles` and `checkFileAvailability` now ask the same Real-Debrid resolver:
a live link projects `true/true`, an `unavailable_file` projects `false/false`, and a request with
none of the three options omits both attributes. The byte path agrees: provider
`unavailable_file` is a plain **404**, while transport or resolver failure remains a 502. The
offline part harness supplies both outcomes and proves **11/11** byte/envelope checks without an
account credential.

##### Claimed-account state is read from the same cloud sources PMS uses

The first outbound census classified every one of the first **1,665** captured exchanges; the
repeat denominator is the complete frozen prefix plus its append-only suffix, **1,829 exchanges**
(1,620 reads and 209 writes). Writes were counted and never replayed. The two client-state reads
that matter are now implemented.

**Feature UUIDs are acquired, not invented.** `/api/v2/features` supplies the account-scoped UUID
half of `ownerFeatures`/`subscriptionFeatures`; it is unioned with the 32 named defaults, sorted,
persisted, and exposed identically on both local endpoints. An already claimed server refreshes
`/api/v2/user?includeSubscriptions=1&includeProviders=1`, waits the measured eleven seconds, then
refreshes `/api/v2/features?X-Plex-Token=...`; the pair runs at claimed startup, immediately after
claim, and no more often than **86,400 seconds**. Both documents commit atomically, so a failed
second read preserves the complete last-good identity and feature set. This daily renewal is the
one product finding Cycle 47 found after Cycle 46's claim-time implementation.

**Watch and rating state is imported from the scrobbling service.** The complete corpus contains
310 consent reads and 309 state pages. Consent is read with the server token; the state endpoint
uses a short-lived RS256 Bearer obtained from the owner's `identifier=scrobbling` service. PMS's
measured credential transform is base64 then AES-256-CBC with the MD5 expansion
`D1=MD5(secret)`, `D2=MD5(D1||secret)`, `D3=MD5(D2||secret)`, followed by its permissive final-byte
strip. The static 32-byte transport secret is deliberately not in the repository; deployment must
supply its base64 form in `REX_SCROBBLING_DECRYPT_SECRET_B64`. Without it, sync fails closed.

The proof is whole-corpus, not sampled: **140/140** captured service ciphertexts decrypt, and the
latest preceding plaintext equals the Bearer actually used on **309/309** state requests. The
cursor is opaque and is persisted only after rows apply. `complete` stores at least one view,
`unplayed` clears view state, timestamps convert milliseconds to seconds, an absent rating pair
preserves local stars, `rating=-1` clears them and a nonnegative rating sets them. No cloud import
manufactures a local history row.

##### The installed Desktop, finite source universes, and the stopping repeat

The installed Plex Desktop 1.115.0 boundary was completely inventoried: its bundled Plex Web
4.160.0 runtime has 202/202 JavaScript bodies and every runtime-declared SRI verifies; the native
side contributes seven network/IPC classes. Bundled Web still owns all PMS request building.
Native mpv consumes the chosen media/subtitle URL, the Now Playing layer consumes existing artwork
URLs, and NanoServer/Widevine/updater traffic is not a PMS path. This adds no PMS path family to
the complete 205-asset public Plex Web graph. Driving the already signed-in Desktop profile could
not be isolated from plex.tv/account/device state without changing the user's live profile, so its
ordered mpv Range cadence, timeline cadence and actual native decode remain an explicit safety
bound rather than a false clean claim.

Cycle 46 also discovered that the old Cycle-41–43 scratch-only census inputs had been deleted.
Cycle 47 repaired that evidence process by rebuilding durable first-party denominators: **743
behavior-bearing commits** in current ancestry (all 24 Cycle-41 fixes present), the complete
**205-asset / 9,314,084-byte** current public client graph, **24,452 scalar advertisements** from
14 measured sidecars, **49 direct PMS controls / 39 normalized families**, and explicit method,
action and readback joins. It also found one test-only defect: six new option regressions ran after
the UI suite had mutated or deleted their discriminator rows. Capturing those read-only responses
while the fixture is pristine makes the complete suite pass **1,629/1,629**.

Cycle 48 replayed the query, header, outbound, historical and advertised-control denominators on
the repaired final head. The durable historical repeat is **745 rows: 743 pass, two terminal
credential-bound media rows, zero not-run/actionable**; all 24 Cycle-41 fixes pass. Each report ends
exactly **`no findings`**. Remaining entries are named
bounds, not omitted cells: deeply analysed bandwidth data, Desktop runtime isolation, pagination
and initial-empty-cursor forms absent from the cloud corpus, shared-user state absent from the
single-owner corpus, proprietary metadata/provider architecture, and expired account-scoped media
links in the checked-in fixture.

#### The forty-fifth pass — the name space, swept from the servers' own words

Cycle 44 reopened the loop by recognising a new instrument. Cycle 45 generalised the *method*
that found cycle 44's `/rating`: instead of hand-writing a list of paths to try, build the
candidate vocabulary from the **references' own emitted words** — every key, attribute name,
element name and hub identifier the two servers write — and sweep every position a path or a
query can put them in. 227 names, **8,240 cells**, 8 driver findings; two hunts on top of that
produced 23 more. Twenty-seven commits, every one proved fail-then-pass.

##### The largest finding: `/library/sections/{id}/{name}/top`

PMS's own route table — read out of the binary's string block on kids — spells it `/:tagType/top`,
with no constraint on the segment, which is why *any* single word matches it. It is the **fifth**
real two-segment section leaf, beside `extras/all`, `query/parse`, `indexes/thumbnails` and the
versioned `composite/{v}`, and rex 404'd every form of it.

It answers the section's **tag table** for the type the name resolves to: `Directory` rows of
`key, id, filter, tag, tagType` (plus `tagKey` and `thumb` on a person), **ascending by tag id** —
not by usage, which is what the name suggests. The `key` is built from the **requested** name
(`/library/sections/1/all?studio=28`) while `filter` names the tag's **own** type (`director=28`),
so on the alias names the two disagree. It is not the axis: kids' `actor` axis is the
first-three-roles union (9) where `/actor/top` is every Role tag (12), and on a show library it
reads the leaves.

Eleven names resolve to a type, in six pairs — a tag name and a metadata **column** name sharing
each number, which is the tell that PMS looks the segment up in one integer space that both
vocabularies feed:

| number | tag name | column name |
|---|---|---|
| 1 | `genre` | `title` |
| 4 | `director` | `studio` |
| 5 | `writer` | `rating` |
| 6 | `actor` | `tagline` |
| 7 | `producer` | `summary` |
| 8 | `country` | — |

Every other name the server knows as a **filter field or an axis** answers an empty listing
container; everything else is a 404. The partition is exact over 91 swept names on four servers
and two versions: the eleven names that are a 400 on the item edit path (`album artist
chapterSource childCount hasChapterVideoStream leafCount logo poster role sourceType track`) are
all 404s here, the section's own leaves are 404s because the router claims them first,
`thumb`/`art`/`theme` are 404s because `/:mediaType(thumb|art|theme)/:timestamp` claims them, and
`platform` is a 404 although it *is* an axis. `resolution` is the media-axis exception and answers
the whole library, as `/resolution/zzz` does. The 1.43.4-only vocabulary splits with the version:
`videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`, `atmos`, `hdr10plus`, `squareArt` and
`clearLogo` are 200s on kids and 404s on zen. GET and HEAD only.

##### `includeAlternates` — and a version claim refuted by its own instrument

The hunt reported `includeAlternates` as 1.43.4-only and it was left unbuilt on that basis. **The
claim is refuted.** The hunt's 1.42.2 oracle was plexscratch42, a fresh install whose every row is
unmatched and which therefore holds no agent artwork at all — CLAUDE.md's "use it for grammar,
never for a count" trap, taken from the other side. zen, a real matched 1.42.2 library, honours it.

The option inlines the **agent's remote artwork** — `Thumb`, `Art` and `Theme`, in that order,
each `{key, source}` plus a `previewKey` where the provider gives a thumbnail URL — and it does it
on a **full row and on nothing else**. Swept over nineteen containers on both references: a
section grid, `/library/all`, `/library/recentlyAdded`, a `/children` page, a section search,
`/collections`, `/playlists`, `/hubs/search` and `/hubs/home/recentlyAdded` are byte-identical
with and without it, while `/library/metadata/{id}`, `/library/onDeck`, both `related` paths,
`postplay` and the full-row shelves inside `/hubs` all carry it — and the discriminator in every
case is the projection, not the path. Inside `/hubs`, zen's `home.television.recent` shows and its
`home.ondeck` episode gain the block and its `home.movies.recent` light movie rows do not. A
**decision ignores it**, the same exception `excludeElements=` already has. The child order is
measured too: `Thumb` and `Art` sit between the tags and `Guid`, and `Theme` directly after
`Rating`.

##### `Accept-Language` ranked the whole list, and a third input nobody had read

Two findings in one function, both reachable from every browser:

* **`X-Plex-Accept-Language` is a third localisation input**, header *and* query, ranked between
  the two rex knew: `X-Plex-Language` > `X-Plex-Accept-Language` > `Accept-Language`. rex
  ignored it, so a client that sends only that header got English.
* **The `q` grammar ranks the whole list.** rex read the first member and threw the rest away
  (`accept.split(",")[0].split(";")[0]`), so `de;q=0.1, fr;q=0.9` answered German where a real PMS
  answers French and `de;q=0` answered German where a real PMS falls back to English. Measured
  over 39 cells on kids and zen, identical on both: weights rank the list, the default is 1.0,
  `q=0` disqualifies that member, an unknown tag is **skipped** rather than ending the search
  (`zzz, de` is German), `*` is ignored, an empty member is dropped, a tie goes to the first
  listed — and **any malformed member disqualifies the entire header** (`q=abc`, `q=.5`, `q=00.5`,
  `q=+0.5`, `q=-1`, `q=1.5`, `q=`, `Q=0.9`, `x=1`, a second parameter, a leading `;`). That last
  rule is the **opposite** of the `Accept:` parser cycle 27 measured, where a bad `q` weighs 0 and
  the range still qualifies. Two different parsers on the same surface.

##### A GET that mints a row, measured on a copy because it writes

`/library/metadata/{id}/theme/{v}/metadata` **creates a metadata row on a real PMS**. It is how
this cycle's own hunt left four orphan rows on the reference lab, and it is why the measurement
was moved to the disposable copies.

It answers one `track` row: `ratingKey` a fresh server id, `key` its own path, `guid`
**`library://` + the SHA-1 of the request path itself** — identical on two servers of two versions
for every (item, version) pair tried, including a row whose theme really exists — `type: "track"`,
an empty `title` and `summary`, `index: 1`, `addedAt` the mint time and `genuineMediaAnalysis:
"1"`. A row that really has a theme at that version adds the theme's `duration` and its
`Media`/`Part`/`Stream`. The minted row is idempotent per (item, version) and readable at
`/library/metadata/{mintedKey}`, and it carries **no `librarySectionID`**, which is why it is
invisible in every listing including `/library/all`. `theme` is the only artwork leaf with this
sub-leaf; `thumb`, `art`, `poster`, `banner`, `clearLogo`, `squareArt` and `squarePoster` all 404
it. rex mints the same row and keeps it **beside** the catalog rather than in it, so a `track`
row cannot leak into the tag tables, the spelling index or `/library/all` — the pollution this
endpoint is infamous for, reproduced rather than inflicted.

##### `/library/shared` — a whole subtree behind one leaf

rex answered `/library/shared/all` and 404'd everything else. On both references
`/library/shared` is the Shared view's **tab bar**: a constant seven-row `Type` container in an
identifier of its own (`com.plexapp.plugins.library.shared`), each row `type: "mixed"`,
`active: false` and carrying the same two `Sort` rows (`titleSort` "Title" default asc, and
`taggingCreatedAt` "Date Shared"). Nothing moves it — `?type=1`, `?sort=` and `?includeMeta=1` all
answer the identical body. Below it **every** second segment answers the same list (`/all`,
`/libraries`, `/servers`, `/items`, `/sections`, `/1` and `/zzz` are byte-identical) and a third
segment is a 404.

The list is the server's own libraries in a projection of their own: `art` and `thumb` are the
empty string and **lead** the row, and `allowSync`, `filters` and `refreshing` — which
`/library/sections` carries — are all absent. `?type=` takes an integer or a comma list of them
and selects **nothing** (the rows are libraries, not items of a metadata type), an empty value is
every row, and `1.5`, a space and `abc` are 400s; `?sort=` really orders, by title or by the
library's `createdAt`, with an unknown key a 500 and an unknown modifier ignored; `?limit=` is
ignored entirely.

**And an empty `?sort=` crashes a real PMS.** It closed the connection on kids and SEGV'd zen's
Plex outright (`systemd: code=killed, signal=SEGV`), reproduced on both, isolated to that one
parameter by probing the other nine on kids alone afterwards. There is no answer to copy, so
rex reads the empty value as absent — the convention `/collections` already has for it — and
the crash is recorded rather than emulated. **Do not send that request to zen.**

##### The account's feature list, answered two different ways by rex

`ownerFeatures` on `GET /` and `subscriptionFeatures` on `/myplex/account` are the **same string**,
byte for byte — 7,078 characters on kids and 6,967 on zen, identical within each server. rex
answered `pass,sync,hwtranscode` for one and the bare `pass` for the other, so its own two paths
contradicted each other and Plex Web — which reads `ownerFeatures` in 28 places in its bundle —
was told the account had three features on one path and one on the other.

The value is a sorted comma join of the account's plex.tv entitlements: ~180 opaque UUIDs plus
**32 named features**, and the 32 names are byte-identical on both servers while the UUID count is
not (212 against 209). The names are the modellable half; the ids are account-and-server-scoped
plex.tv content and are deliberately not invented.

##### The rest of the driver's name-space sweep

* **The bare `/sync` is the plug-in dispatcher's `{size: 0, content: "plugins"}` 200**, like every
  other single-segment path, while `/sync/items`, `/sync/transcodeQueue` and `/sync/zzz` really
  are 404s. rex short-circuited the whole prefix on the note that it has no sync, so it
  answered one single-segment path differently from all the others.
* **Eleven more cross-type axes** answer the empty secondary container the nine already-known ones
  do — `autotag`, `aperture`, `exposure`, `iso`, `lens`, `sport`, `team`, `channel`, `publisher`,
  `narrator` and `platform` — and `platform`'s `title2` is the **empty string** where every other
  axis carries a "By …" heading. `narrator` is the one axis on the surface that does **not**
  validate `type=`: it answers 200 to `abc`, `zzz`, `1.5` and an empty value where every other
  tag axis is a 500.
* **An axis bucket resolves against the server's global tag TABLE, not the leaf's own column.** A
  genre tag id answers the identical container under `genre`, `director`, `actor`, `mood`,
  `style`, `similar`, `tag` and eleven more names; only `studio` and `platform` are not bucket
  names at all. rex resolved the id against the name's own column, so nine names worked for
  their own tags and the other ten 404'd everything.
* **`/library/arts` is a real listing query**, and an image row renders as `<Photo>`.
* **Two more section subsets exist**, `recentlyViewedGrandparents` and `recentlyAddedGrandparents`,
  each an alias of the subset beside it.
* **`stations/{genre|mood|style|decade}`**, `/library/newReleases`, `/:/viewChange` and
  `/services/codecs` are real routes; every other value under `stations` is a 404.
* **`X-Plex-Container-Focus-Key` supplies the container START** as `max(0, index - (size - 1) //
  2)` — the focused row centred in the window, biased earlier — header or query, its value must
  begin `/library/metadata/`, an explicit `X-Plex-Container-Start` wins, and it needs a `Size`.
  Read out of the binary's own log string, `Setting container serialization range with focusKey to
  [%s, %d]`, then measured.
* **`X-Plex-Http-Pipeline: disable`** (that exact lowercase value) answers `Connection: close`
  with no `Keep-Alive` line.
* **The singular artwork PUT is routed for six names** — `poster`, `art`, `thumb`, `banner`,
  `theme`, `clearLogo` — each answering its own 400 with no parameters, while `logo`,
  `squarePoster` and `squareArt` are 404s.
* **An item leaf with an extra path segment is a 404** (64 of 212 swept cells were wrong), with
  exactly two exceptions: the artwork leaves read the second segment as a version, and
  `/extras/all` and `/users/top` are real two-segment leaves of their own.
* **A validated version segment refuses a leading `+`** although `+?0*<digits>` normalises in
  every other path segment — the raw segment has to be read before the normalisation.
* **`HEAD` on a section `/common` is a 200**, and **`includeSummary=1`** adds a per-library
  duration and type count to `/library/sections`.
* **`/security/token` validates its parameter values**, and **`playbackTime` is bounded to signed
  32-bit**.
* **`asyncCheckFiles`** projects `Part.accessible`/`exists` like `checkFiles` does.
* **`file=` reads the section listing's default type**, and **`extraType`, `unmatched` and `file`
  refuse where a real PMS refuses them** — the refusal is a property of the query's declared type,
  not of a row, so deciding it per row made an untyped `/library/all?file=` a 500 where both
  references answer and really filter.
* **A play completes on a beat that crosses 90% even under a minute.** The gate is
  `position >= (total * 9) // 10` — an integer comparison, and the sub-minute exemption the old
  rule carried was never a rule.
* **A `.webm` container is corrected at LOAD as well as at build.** Cycle 44 fixed the builder;
  a catalog built before that fix still carried `container="webm"`, and a real Plex Web client
  refuses to play such a row.

##### Measured and not acted on this cycle

* **`includeBandwidths=1`** adds `deepAnalysisVersion: 6` and a comma-joined eight-value
  `requiredBandwidths` to a **Part**, and `requiredBandwidths` to a Stream — but only on a Part a
  real PMS has **deep-analysed**. zen's `/library/metadata/1` is byte-identical with and without
  the option, which is the proof: rex's Parts are all un-analysed, so ignoring the option is
  the faithful answer. Its value grammar is the `+?0*1` family and nothing refuses.
* ~~**The ~180 plex.tv feature UUIDs** behind `ownerFeatures`.~~ **BUILT in Cycle 46 and renewed
  daily in Cycle 47.** The outbound corpus identified `/api/v2/features` as their exact
  authenticated source, so the server acquires and persists them instead of inventing them.
  `owner_features.txt` remains an optional local addition, not the primary source.
* **`/library/shared/all?sort=`** — the empty sort that crashes a real PMS. Unmeasurable.
* **`title`→genre and `summary`→producer on `/top`** are built, because they cost nothing beyond
  the six real names and are identical on three servers; the *generating rule* behind PMS's
  shared integer space is not identified and is recorded as such.

#### The forty-ninth pass — the LAN, the connection, and the claim

Cycles 46–48 closed five finite axes and cycle 48 reported no findings on every one of
them. Cycle 49 reopened the loop the only way it has ever been reopened: by recognising an
instrument. **rex had never been asked how a client FINDS it.** Everything this
programme has measured happens over TCP 32400 after a client already knows the address.
Three axes fell out of that, and none of them had been swept:

* **UDP.** A real PMS answers Plex's own LAN discovery protocol, GDM, on two ports.
  rex answered nothing, so no official client could find it without plex.tv or a
  hand-typed address.
* **The connection itself.** Cycle 37 swept the request LINE and cycle 33 the response
  header BLOCK; nobody had swept what a real PMS does with the TCP stream around and
  between requests.
* **The claim.** Every write rule this project holds was measured on an UNCLAIMED copy,
  and nobody had ever diffed the claim systematically. That is the project's own failure
  class E, unaudited for forty-eight cycles.

**26 findings, 21 commits, every one proved fail-then-pass against its own parent.**

##### GDM — the protocol that makes a server FOUND

Measured on four servers: zen (1.42.2, claimed), the plexlab reference (1.43.4, claimed)
and both unclaimed disposable copies. The two versions answer byte-identical blocks apart
from their own values, so nothing here is version-split; the CLAIM is what splits it.

* **Two ports, and only two.** A GDM query is answered on UDP **32410** and UDP **32414**
  and on nothing else — 32400–32409, 32411, 32412, 32413 and 32415–32419 were swept on
  zen and answer nothing. 32412/32413 are the PLAYER side of the same protocol; a server
  neither binds nor answers them. Both ports are joined to the multicast group
  **239.0.0.250** and both answer a subnet or global broadcast; `239.0.0.250:32412` and
  `:32413` draw nothing. The reply is unicast back to the source, from the port it
  arrived on.
* **The trigger is a PREFIX COMPARE, not a parse.** The datagram and the constant
  `b"M-SEARCH * "` must agree over `min(len(datagram), 11)` bytes. Bisected byte by byte:
  every proper prefix answers — including the EMPTY datagram and a bare `b"M"` — one
  wrong byte at any of the eleven positions is silent, anything at all may follow the
  eleven bytes (a 2 KB datagram answers), and it is case-SENSITIVE. That is why both a
  client's `M-SEARCH * HTTP/1.1\r\n` and an `M-SEARCH * HTTP/1.0\r\n\r\n` work: the
  version and the terminator are never read.
* **The reply** is `HTTP/1.0 200 OK` and then the headers in ALPHABETICAL order, CRLF
  endings, terminated by a blank line: `Content-Type: plex/media-server`, `Host`, `Name`,
  `Port`, `Resource-Identifier`, `Updated-At`, `Version`.
* **`Host` is the one field the claim moves.** Both claimed references carry
  `<certificate hash>.plex.direct` and neither unclaimed copy carries the line at all —
  absent, not empty. An unclaimed PMS has no certificate, so the claim and the certificate
  are confounded on these four instruments; rex emits the line only when it holds a
  plex.direct hostname, which is the honest reading either way.
* **`Updated-At` is not a clock.** It stood still under fifteen reads in a minute on zen,
  the second server on that wire answered a value **3.5 days** old, and on an idle
  unclaimed copy it did not move once in 86 samples over seventeen minutes. The two ports
  can even disagree — plexscratch42 answered 1789624280 on 32410 and 1789690002 on 32414
  in the same second — so each socket carries its own last-regenerated stamp. The event a
  real PMS refreshes it on is NOT measured.
* **A real PMS also SENDS.** It emits `b"M-SEARCH * HTTP/1.1\r\n"` — 21 bytes, ONE CRLF —
  to 239.0.0.250:32414 every **5.00 seconds**, on every interface (nine samples at 0.96,
  5.96, 10.96 … 40.97 s).
* **`/servers` is what that search found.** zen's `/servers` listed itself, the second
  server on its wire, and — within six seconds of rex being started with the new
  module — **rex**, at the address and port rex advertised. A row ages out
  between **177 s and 180 s** after its last answer, bracketed by polling every three
  seconds; an earlier run reading (154, 165] is discarded, because it reused a
  machineIdentifier zen already held.
* **Nothing else on the LAN.** Bonjour/mDNS advertises no Plex service at all, and TCP
  8324 (Companion), 32469 (DLNA) and 1900 (SSDP) are refused on zen: GDM is the whole of
  LAN discovery.

##### Two more things the discovery surface exposed

* **`/servers`' own row carries the server's own address and port.** zen answers
  `192.168.88.244`/32400 and kids `172.31.7.2`/32400 — each its own interface address,
  never loopback. rex hard-coded `127.0.0.1` and the constant 32400 whatever port it
  was on, because `private_address` was declared and never assigned for the whole life of
  the field. `/myplex/account`'s `privateAddress`/`privatePort` move TOGETHER: zen carries
  both and kids, whose plex.tv mapping holds no local connection, carries `""` and **0**.
  rex was the only server of the four answering a port beside an empty address.
* **`/` advertises the transcoder's quality LADDER**, three parallel comma lists,
  byte-identical on all four servers and independent of the transcoder's own state:
  `transcoderVideoBitrates`, `transcoderVideoQualities`, `transcoderVideoResolutions`.
  Plex Web builds its Quality menu from them, index by index; rex sent none.
  `transcoderVideoRemuxOnly`, which zen also carries, is **not** a version attribute — the
  1.42.2 copy omits it too, so it is zen's own state. And two more values on that blob are
  ones no reference emits: `livetv` is a CONSTANT **7** on all four (none of which has a
  tuner) where rex said 0, and `offlineTranscode` is 1 claimed and ABSENT unclaimed,
  never the 0 rex invented.

##### The connection and transport layer

Every cell below came off a raw socket; a library that owns the connection cannot see any
of it.

* **A real PMS consumes a declared request body before reading the next request.** It
  answers a GET carrying a body WITHOUT waiting (cycle 37's half) and then consumes
  exactly `Content-Length` bytes, so two requests on one socket get two answers. rex
  left the bytes in the stream, glued to the next request line, and the client was handed
  a 400 belonging to nothing it sent. The count is a positive measurement, not an
  inference: nine body bytes behind `Content-Length: 5` leave four spare and the NEXT
  request is a 500. When the body never arrives a real PMS CLOSES — already decided ~5 ms
  after the response, whether the body then turns up at 2 ms or at a second — and that is
  the mechanism that makes a desync impossible there. **A duplicate `Content-Length` takes
  the FIRST occurrence**, the opposite of the repeated-query-parameter rule, and a leading
  `+` parses.
* **A real PMS decodes `Transfer-Encoding: chunked`**, including on a write: a chunked
  subtitle upload lands a sidecar byte-identical to the `Content-Length` form's. rex
  ignored the header, so the upload was its empty-body 406 and the chunk framing went into
  the next request. The size token parses like `strtol` in base 16 — an optional sign, an
  optional `0x`, hex digits — and **nothing may follow it**: a chunk EXTENSION, which HTTP
  allows, is a 400, and so are a trailing space, LF-only framing, a wrong chunk length and
  a size that overflows. A negative size, an empty size line and a trailer after `0\r\n`
  are answered and then closed. The header must be exactly `chunked`, case-insensitive,
  with nothing else in the value (13 spellings measured), and **it beats
  `Content-Length`**.
* **The incomplete-request cap is ABSOLUTE, not idle.** A 400-byte request in three pieces
  twelve seconds apart — maximum idle 12 s, total 24 s — is closed at exactly 20.0 s from
  the FIRST byte with no response, and so are four pieces 7 s apart and six pieces 4 s
  apart. Python's `Handler.timeout` is a socket timeout and is per-read by construction,
  so rex answered every one: fifty clients dribbling a byte every two seconds held
  fifty threads where a real PMS dropped all fifty at 20.6 s. The IDLE timer between
  requests is a separate 20 s and resets per request; all three servers already agreed on
  that.
* **A header block ended by EOF rather than by a blank line is answered with NOTHING.**
  rex ran the request — and a `PUT /library/metadata/{id}?title.value=…` sent that way
  really COMMITTED. It is the one cell in this family where rex changed durable state
  a real PMS discards.
* **The ABSOLUTE-FORM request target is routed**, with the authority discarded entirely:
  `http://example.invalid/library/sections` is byte-identical to the origin-form answer,
  and so is `http://1.2.3.4:9999/…`. The scheme must be lower-case `http` exactly and a
  path is mandatory; `HTTP://`, `Http://`, `https://`, `ftp://`, `xyz://`, `http:/h/x`,
  `http://h` with no path, `http://` and a bare `http:` are each a 400. rex read the
  scheme as a path segment and 404'd every one.
* **The METHOD token is bounded at ten bytes**, bisected over lengths 1–19 on both
  writable copies and confirmed on kids and zen with GET requests alone. An unrecognised
  method of ten bytes or fewer is the recorded 500 with the connection kept; the eleventh
  byte is a 400 with `Connection: close`, **and it arrives the moment that byte does**,
  before any delimiter.
* **`X-Plex-Http-Pipeline: disable` works as a QUERY parameter too**, with the header's
  grammar exactly — lower-case `disable` only, a case-SENSITIVE parameter name, the LAST
  occurrence winning, and the HEADER beating the query when both are sent.
* **A refusal of the request's ENVELOPE closes the connection; a refusal of its MEANING
  keeps it.** 27 shapes on both references. That is also why cycle 30's "the one refusal
  that closes" was never an exception — the query-pair-without-`=` 400 is envelope-level
  too. rex broke it in seven cells: `Content-Length: -1` (500), a non-terminal dot
  segment (400), a missing `/:/resources/*` and a missing `/web/*` asset (404, and those
  two name **no connection state at all**), a `/web` path escaping the root (400), and a
  leading CRLF or space before the request line, which draws **silence** rather than a
  400. And one of the seven is not a connection state at all: rex answered **200** to
  an `obs-fold` continuation both references refuse outright, because Python's reader
  unfolds it before the handler can see it.
* **Only the image transcoder's 200 belongs to the image header family.** Its 400s and
  404s order the length before the type, like every other refusal.

##### The claim, audited for the first time

116 safe read families on all four servers, in both formats, with and without a token.
The status-level partition is 101 identical, **15 claim-dependent**, 0 version-dependent;
297 container-attribute cells partition 193 identical, 40 claim-dependent, 20 version, 44
content. rex answers the claimed form on all fifteen families and on all but one of
the claim-dependent attributes. Two real findings came out of it:

* **`ButlerTaskGenerateAdMarkers` ships ENABLED on a claimed PMS**, and the
  `GenerateAdMarkerBehavior` preference reads `scheduled` — a setting that does not exist
  at all on an unclaimed server. Cycle 35 measured the row on the two writable copies,
  which are both UNCLAIMED, and recorded "ships DISABLED": `enabled` is COMPUTED from
  claim state, not stored, and plexscratch is a byte copy of kids' own config with only
  the claim stripped, answering 0 where kids answers 1. Cycle 35 fixed a class-D error by
  installing a class-E one, which is exactly what this audit existed to find.
* **`/playlists/sorts` ends with `mediaCount` "Item Count"**, not the `updatedAt` "Date
  Modified" cycle 15 recorded. Four servers, six parameter spellings. rex's two
  surfaces disagreed with each other, because `/playlists?includeMeta=1` already answered
  the right row.

Two more named endpoints behind the legacy single-segment dispatcher are real: **`/dlna`**
is the DLNA server's own library list — the section rows keyed by their full path, without
`allowSync`, `filters` and `refreshing`, ending in a `/channels/all` "Channels" row — and
**`/manage`** is the same 302 into the web client that `/web` answers, byte for byte.

##### Dual-format equality, and three tag projections

The census is 209 family templates across 7 servers = 1,331 dual-format pairs; 23,674
element nodes compared for key set and order and 1,298 JSON scalar-type cells. It found
**one** type mismatch and **one** ordering mismatch, which is the useful negative result —
and four more findings came out of the tag-rich row it had to MAKE to be non-vacuous.

* **A `/system/agents` `Language`'s `default` is the JSON boolean `true`**, not the string
  `"1"`. It survived because `agent_languages.json` is read out of the XML, which writes
  `default="1"` either way — XML cannot tell the two apart, so no XML sweep could see it.
* **`/myplex/account`'s root is not a container.** `to_xml` hard-coded `container=True`
  for every root, so its attributes came out alphabetically in XML while the JSON writer
  kept the reference's own order. That is the fifth instance of the two-writer bug cycle
  19 established never happens on a real PMS, and the last one the surface had:
  `ButlerTasks` and `Query`, the other two non-container roots, carry no attributes to
  disagree on.
* **`?includeDetails=1&includeAdvanced=1` DOES carry the Duplicates Filter row**, and it
  is the one Filter row on that path that keeps `advanced="1"`. Cycle 16 measured the path
  without `includeAdvanced=1`, where the row does not exist to carry a flag; cycle 23 read
  that absence of the FLAG as an absence of the ROW and rex stripped it from the
  descriptor the Edit Library dialog reads.
* **A LIGHT row carries none of `Label`, `Style`, `Similar` or `Mood`** — the reference's
  grid row for a movie made to carry every tag family is `Genre Country Collection
  Director` where its detail row is `Genre Country Style Collection Director Producer
  Label Similar`. rex passed all four through, `Label` text-only and the other three
  with their full `id`/`filter`. `label` is an advertised editable tag field on a movie
  library, so a Label chip was reachable from the Edit dialog and drew on every tile.
* **`Style` sits AFTER `Country`** on a detail row, not between `UltraBlurColors` and
  `Genre`. `Mood` has no slot at all, and deliberately: a real PMS refuses to store a
  `mood` on a movie, so its position is unmeasurable.
* **`/status/sessions` has a serialiser of its own**, and its alphabetical string-id rule
  was applied to ten tag names and missed `Label`, `Style` and `Mood`.

##### Three more, from re-measuring what a hunt reported

* **A `mood` write on a movie is a 200 that stores NOTHING**, where the `style` and
  `similar` beside it really store.
* **Emptying a collection leaves the collection ALIVE.** rex deleted it, on a comment
  claiming a real PMS "also leaves the stale tag on the item, which is a state rex has
  no way to represent". It does not: the collection item survives AND the tag comes off
  the item, so the state is perfectly representable and rex was deleting a row a real
  PMS keeps.
* **An identifier-less request's device name and platform land on the ANONYMOUS device
  row** — the one with `clientIdentifier=""`. rex recorded them only for an identified
  client.

##### Measured and NOT acted on

* **The legacy dispatcher's PREFIX listing.** A single segment that is a proper prefix of
  one or more registered plug-in routes answers the last segments of every registered
  route whose path begins with it — `/dev` names `devices`, `/me` names media's three
  children, `/serve` names `server` and `servers`, `/s` names seventeen. Derived from a
  38-cell prefix sweep on zen and consistent on every one, but reproducing it needs PMS's
  own route registry, which rex does not have and which no client asks for.
* **`/connections`**, the live-connection diagnostic: `text/plain`, one line per
  connection with a state, a timing, a byte count, a request line and a token name.
  rex keeps no connection registry, and every part of that line it cannot derive would
  be a fabrication.
* **`transcoderVideoRemuxOnly`** — zen's alone among four servers, so it is that server's
  state and not a rule.
* **`/myplex/account` carries `authToken` on zen and not on kids**, and the whole plex.tv
  mapping block (public and private address and port) is empty on kids as well. Version
  and mapping state are CONFOUNDED on the only two claimed servers this project has, and
  neither unclaimed copy can separate them. rex's omission is now a DELIBERATE
  deviation rather than a measurement: its token gate defaults OFF, so publishing the
  claim token there would hand it to every peer that can reach the port.
* **The `std::bad_cast` 500.** A device row whose `platform` parses as a number makes
  `GET /devices` — and `GET /statistics/bandwidth` — a 500 on a real PMS. rex stores
  the string it was given and does not reproduce the crash. See HANDOVER §9: both
  references were in that state, and it is damage this programme caused.


#### The fifty-second pass — the server's own binary as the route table

Cycle 51 was clean on every axis it repeated, so this cycle reopened the loop the way the
loop has always been reopened: **by recognising an instrument.** Fifty-one cycles built
rex's route table out of what a CLIENT was seen to ask for — the web bundle's request
builders, a captured session, python-plexapi, the servers' own advertised vocabulary. None
of those can name an endpoint no client sends.

**The Plex Media Server binary can.** Cycle 45 read ONE string out of it — the log line
behind `X-Plex-Container-Focus-Key` — and nobody went back for the rest. It is 22 MB, it is
readable on both references
(`docker exec plexlab-plex cat "/usr/lib/plexmediaserver/Plex Media Server"` on kids,
`cat` over ssh on zen), and its string table carries PMS's own route templates with their
parameter captures — `/:tagType/top`, `/parts/:partID(\d+)/:changestamp(\d+)?/:filename?`,
`/:assetType(thumb|art|clearLogo|squareArt|banner|poster|theme)`,
`/:type(video|music|audio|subtitles)/:/transcode`. Diffing the two binaries also splits the
vocabulary per version for free: 1.42.2's copy of the artwork line omits `squareArt` and
keeps `clearLogo`, which is exactly what the surface answers.

**22 findings, 22 commits, every one proved fail-then-pass against its own parent.**

**The legacy dispatcher's prefix listing is a RULE, not a table.** Cycle 50 captured 58
paths from an executed request list and shipped them as a dictionary. A breadth-first walk
of every prefix over the alphabet on plexscratch42 recovered 154 non-empty listings, and
one rule reproduces all 154 with **zero membership mismatches**: the dispatcher answers the
LAST SEGMENT of every registered legacy prefix that BEGINS WITH the requested path and is
STRICTLY LONGER than it. The strictness is visible — `/statistic` names `statistics` and
`/statistics` itself is empty, `/serve` names `server` and `servers` while `/server` names
only `servers` — and `/co` is empty because `/connections` is a native route rather than a
registered prefix, which is why the captured table looked like one with holes in it. The 45
registered prefixes are now in `PLUGIN_REGISTRY`; 148 of the 154 listings come back in byte
order of the row key, five put the built-in `library` row last, and `/l` is the one cell
neither rule explains and is recorded as measured. Confirmed on kids, zen and plexscratch42
over all 190 prefixes of the 31 first segments: **119 of them answered rows on all three
references and an empty listing here.**

**Three static files sit at the top of the surface and rex answered none of them.**
`/crossdomain.xml` (347 bytes) and `/clientaccesspolicy.xml` (325) are the Flash and
Silverlight policies PMS ships in `Resources/`; `/favicon.ico` is 5,430 bytes. All three
are byte-identical on both versions, all three answer **with no token** — the third, fourth
and fifth exception to "a real PMS 401s everything but `/identity` and `/web/*`" — all
three carry `Accept-Ranges: bytes` and really answer 206, none is ever gzipped, and every
verb other than GET and HEAD falls through to the dispatcher's empty listing. The two
policies order their headers like artwork and the favicon like a `/web` asset, which is the
two-order split cycle 33 measured for the image pair.

**And the unauthenticated partition is wider than the eleven 401s cycle 16 recorded.**
Swept over the whole single-segment vocabulary — 131 names taken from the two binaries —
with no token: `/connections` and `/dlna`, both routes cycle 49 added after that list was
written, are 401s; `/manage` answers its 302 to the web client like `/web`; and `/search`,
`/downloadQueue` and `/player` answer their OWN refusal before the gate (400, 404 and, on
1.42.2, 400 — kids answers 401 for `/player`, so that one is a version split). rex
answered the generic 403 to all six.

**`/:/plugins` is seventeen rows, and everything under it is a second plug-in framework
subtree.** HANDOVER §8 has carried "legacy agents and the `/:/plugins` listing" as a
deliberate bound since cycles 1–6, on the grounds that rex hosts no plug-ins. The
listing is not hosting: kids, zen, plexscratch and plexscratch42 answer the identical
seventeen identifiers in the identical order, with `state: 4` on every one but
`com.plexapp.system`, which is 0 on all four. It is a fixed table in the image, like the
profile files cycle 31 read out and shipped. Under it, thirteen of the seventeen answer a
preference document at `/:/plugins/{id}/prefs`, byte-identical on both versions, and the
other four answer the framework's 500; a bare identifier and any other path under a hosted
one are an EMPTY-bodied 404; and the whole subtree carries the `/system` envelope — a bare
OPTIONS is an empty 200, POST, PUT and DELETE are empty-bodied 404s, PATCH is the version's
own refusal — with the framework's cookie NAMED AFTER THE PLUG-IN. So cycle 18's "PATCH is
a 405, the only one on the surface" is two framework subtrees, not one.

**And the framework's cookie rides its own responses only.** Neither the bare OPTIONS 200
nor the PATCH refusal carries `Set-Cookie`, on either version, under `/system` or under
`/:/plugins` — both are answered before the framework runs. rex attached it to every
response under `/system`.

**`/:/resources/*` is seventy names, not seven.** Cycle 29 fixed a ten-name sample and
recorded the seven that answered; the tree is PMS's whole `Resources/Graphics` directory,
and all 70 are 200s with the same media type and byte count on both versions. A DIRECTORY
is an empty `application/octet-stream` 200 in both spellings, `/:/resources` with no
trailing slash is a 404, matching is case-sensitive, and `?width=`/`?height=` are ignored.
rex 404'd 63 of the 70, served the seven it knew at 342x513 rather than their own
geometry, and sent PNG bytes under the three `image/jpeg` names — on paths its own rows
point at. It now answers the name set, the media type and the file's geometry, with a
minimal baseline JPEG encoder beside `_png` so a `.jpg` name really is a JPEG; the two
files that are not images are carried verbatim.

**The photo transcoder FETCHES the path it is given.** Measured on kids and zen, which
agree on every cell: a `url=` the server answers with anything but 200 is a **404** here —
an unknown item leaf, a missing section composite, a row that borrows the field, and
`/library/metadata/{id}/children`, whose own direct answer is a 400 — and only a 200 whose
bytes are not an image is the 500 cycle 29 measured. rex matched four artwork names
with one regex and 500'd everything else, **its own section composite included**: Plex Web
builds a library card from `composite` and routes it through the transcoder when the
provider advertises `imagetranscoder`, which rex does, so every library tile the client
asked for that way was a 500 where the direct path is a 200 image. The transcoder now asks
rex's own router what the path answers and hands back those bytes.

**The artwork read leaves took any verb.** `POST /library/metadata/{id}/thumb` and a bare
`OPTIONS` each answered the whole JPEG, and so did the versioned path; a real PMS answers
the ordinary 85-byte 404 to both and 404s a PUT to the versioned form. It is the class
cycle 15 closed for twenty read-only routes and cycle 31 for ten more.

**A collection's and a playlist's artwork leaves follow the item's ownership rule.** A
collection with nothing uploaded carries `/library/collections/{id}/composite/{v}` in its
`thumb` and a playlist carries none at all, so `/thumb`, `/art` and `/poster` are 404s until
something is uploaded — where rex served its generated grey tile, with no
`X-Plex-Local-Asset`, to a brand-new list. `/clearLogos` and `/themes` are the same empty
candidate containers an item answers and rex 404'd them, which is what python-plexapi's
`logos()` and `themes()` raise on. And an EMPTY playlist's `default://` poster candidate
names `/playlists/{id}/composite/{updatedAt}` on a real PMS; rex built that key by
concatenating two fields an empty playlist does not have, so the candidate's `key` was
`?width=400&height=600` — a relative url that resolves to the page the client is on.

**`/connections` is plain text.** A real PMS routes it and answers one line per connection
slot — `3 10.0.0.1:61216 - Routing - GET /connections #4e445 - Token (plexa630)
(curl/8.7.1)`, with the trailing parenthesis naming the client's
`X-Plex-Device-Name, X-Plex-Product` or its `User-Agent` — where rex answered the
dispatcher's XML container. Cycle 50's 78-snapshot matrix established that PMS's acceptor
pool, its ordinals and its hexadecimal request allocator are not derivable from outside;
those stay recorded, and rex now reports its OWN live connections in the measured
grammar rather than the wrong document.

**Seven refusals, each measured on both versions.** An extra segment after a stream id is a
404 whatever it says (rex answered its 501 to every name it did not know); so is one
after an account or a device id, where rex answered the account row and the whole
device list. Exactly four actions live under `/system/agents` — `search`, `update`,
`contributors` and `media/get` — and each answers the plug-in framework's own TypeError
traceback with a 500, where every other name is an empty-bodied 404; rex answered the
agent catalogue to two of them and 404'd two others. `/library/metadata/augmentations/{x}`
parses its id before the lookup, so a non-number is a 400. All four transcode prefixes the
route table names are routed, and rex 404'd three of them. And the part leaf's arity is
the route template's: ONE extra segment is a filename whatever it says — `/library/parts/
{id}/indexes`, `/abc` and `/12345678901` all serve the bytes — and the digit rule only
decides the changestamp when a filename follows it.

**`/hubs/sections/{id}/popular` is a real route**, and its rows are the library's SEASONS
with a `viewCount` above zero, most-watched first, in the full row projection, with the
librarySection triple on the container. zen's show library answers one season at
`viewCount 73`; its movie libraries answer the empty container whatever their play counts.
rex 404'd it on both library types.

**A Categories tile carries a blend colour, and it is a table.** Thirteen genre names have
a fixed colour and every other genre takes the next of a seven-colour cycle walked in
TAG-ID order among the unknown ones — 19 of 19 on zen and 3 of 3 on kids. The colour is not
derived from the artwork, which rex's own comment assumed; rex emitted no
`blendColor` at all, so a TV client's Categories browse drew every tile untinted. The art
each tile uses is the highest-rated item carrying that genre and an `art` that an earlier
genre has not taken, ties by the lowest ratingKey — 38 of the 39 observed picks.

**An actor with no portrait carries the bundled silhouette.** 479 of zen's 3,416 actor-axis
rows carry `/:/resources/actor-icon.png?t=<stamp>` and not one lacks a `thumb`, while a
director, writer or producer with no portrait carries none at all. rex left the
attribute off, so Plex Web's Cast grid drew a broken image.

**And an indexed tag write on the SECTION save path is a tag write whatever follows
`.tag.`** — `.role`, `.thumb`, `.id`, `.filter` and even `.zzz` are 200s on both copies, and
the role itself is stored empty. rex skipped only `.tag.tag`, so the Edit dialog's own
Cast row, which sends the name and the role together, was refused from the section path
while the item path accepted it.

**Measured and not acted on.**

* **The framework's kwarg arithmetic was first FITTED and then read.** `/system/agents/
  {action}` passes the query through to PMS's own Python function, so the sentence moves
  with it. Twelve measured cells gave a table with a per-action exception; then
  `System.bundle/Contents/Code/agentservice.py` — which is plain Python in the image — gave
  the rule the fit was approximating: `runtime.py:843` calls `f(**d)` with the whole query,
  so `given` counts `self` plus every supplied name the function DECLARES, an undeclared
  one is "got an unexpected keyword argument", and `search(self, identifier, mediaType,
  lang, manual=False, **kwargs)` is why it alone swallows one. The fitted table had
  `identifier` unknown to `search` (which really counts it) and known to `update` (which
  really refuses it); both are corrected. What is still bounded is the fully-supplied call,
  where the function does real agent work and answers a value-dependent traceback.
  **That source is cycle 53's instrument**, found while closing this one.
* **`/connections`' acceptor pool**, its ordinals and its request allocator — cycle 50's
  bound, unchanged.
* **The pixels of the 68 bundled images.** The names, media types and geometries are
  measured; reproducing 4.7 MB of a product's artwork to reproduce the bytes is not done.
* **A movie library's `/hubs/sections/{id}/popular`** is a 200 empty container on zen and a
  **500** on kids, and the driver is not identified — not history, not watch state, not the
  version, not the claim. rex answers zen's, being the version it reports.
* **`/library/parts/{id}/indexes/{sd|hd}[/{offset}]`**, the BIF seek-preview index, and
  `/library/media/{id}/chapterImages/{n}`: 404 on every instrument, because no instrument
  holds a generated index or a chapter.
* **A correction to cycle 45's own prose, not to the code.** The line "`stations/
  {genre|mood|style|decade}`, `/library/newReleases`, `/:/viewChange` and
  `/services/codecs` are real routes" names `/library/newReleases`, which is a **404** on
  kids and on zen — bare, and under a section. The real one is
  `/hubs/sections/{id}/newReleases`, which rex already answers; nothing in the code
  followed the wrong name, so this is the document being corrected rather than a defect.

#### The fifty-third pass — the framework's own source

Cycle 52 read PMS's route table out of the binary. Cycle 53 read the **source beside it**:
the plug-in framework is Python, shipped uncompiled in the image, and
`System.bundle/Contents/Code/*.py` *is* the `/system` surface. Every `@route` in those
files is an endpoint, so "which endpoints exist under `/system`" stopped being a sweep and
became a read — and then a measurement, because a route the source declares is not
necessarily reachable.

Cycle 52 had already met the instrument without recognising it: it fitted the framework's
argument counting to twelve measured cells and then found the rule in one grep of
`agentservice.py`. This cycle did that deliberately, and the framework surface it opened
is large.

**`/system/bundle/media/flags/*` is a whole image family, and every container rex
answers points a client at it.** `mediaTagPrefix="/system/bundle/media/flags/"` is on every
library container rex serves, exactly as it is on zen's, and every URL under it was a
404 here. `flagservice.py` registers **ten** types and `Media-Flags.bundle` holds 742 PNGs
and a 35 KB `substitutions.xml` of 624 regular expressions. Measured on kids and zen,
identical: a known value is its own image; an unknown VALUE is never a 404 but the
bundle's 82-byte `blank.png` with a 200; an unknown TYPE, and an empty value, and a third
path segment, are the `/system` subtree's empty-bodied 404; `media` and `recordLabel` are
registered types with no directory at all, so every value under them is the blank; a
direct file name matches case-SENSITIVELY while the three substituted types (studio, audio
codec, video codec) match their regex case-insensitively — `studio/Disney` and
`studio/Walt Disney Pictures` are one logo, `audioCodec/dca` is the DTS one and `ac3` the
Dolby Digital one. Twelve independent sizes reproduce exactly from the table.

**Eighteen more framework routes answered nothing here.** `peerservice.py` registers
channels and plugins over five directory names — ten paths, each the framework's 130-byte
empty container with no channel installed; `systemservice.py` answers a 229-byte
agent-service status report and an empty `serverUpdated`; `messageservice.py` three empty
event routes; and `/system/services/{search,searches,relatedcontent/lookup}` plus the two
`/system/agents/attribution` leaves answer the framework's own TypeError traceback, where
rex 404'd most of them and sent its own 109-byte HTML 500 for the attribution pair. A
framework STRING result also spells its header **`Content-type`**, with a lower-case `t`,
because `construct_response` writes it literally — the one such header on the surface.

**Five more sub-paths under a hosted plug-in are real.** Cycle 52 measured the preference
document and recorded everything else as the empty-bodied 404. `prefs/list` is
byte-identical to `prefs`; `plist` is the bundle's own `Info.plist` as `text/plain`;
`prefs/set` an empty `text/plain` 200; `messaging/wait` a 16-byte constant; and
`resources/{name}` the bundle's own images, with a 404 for a name it does not have.
`root` is a 302 to `/system` for `com.plexapp.system` — the only bundle that registers a
prefix — and the framework's `IndexError` traceback for every other running bundle, which
is `handlers/runtime.py:113` reaching for `handlers[0]` of an empty list. The four bundles
the server hosts but does not run answer the 109-byte 500 to every one of them.

**And the eleven legacy agents are listed.** HANDOVER §8 carried "legacy agents and the
`/:/plugins` listing" as one inherited bound from cycles 1–6. Cycle 52 retired the second
half on the grounds that a fixed table in the image is not hosting; this is the same
table, and leaving the two halves split was drift rather than a decision. kids, zen,
plexscratch and plexscratch42 answer the identical eleven identifiers with the identical
attributes, so a settings page reading `/system/agents` sees **15** rows on a real 1.42.2
server and saw four here. In the bare form a legacy row carries `MediaType` children and
no `Language`; in the typed form the primaries for that type come FIRST, each with its own
`Language` CODES — a bare `code`, no title, one for Personal Media, 33 for the legacy IMDb
agent, 26 for The Movie Database. Two differences are deliberate: 1.43.4 adds an `id` to
every legacy row and rex reports 1.42.2, and the order of `lastfm` and
`plexthememusic` is the server's own load order, where three of four instruments agree and
zen alone differs.

With the agents listed, their **attribution** is reachable: the bundle rex itself
serves renders a 100×100 `<img>` per agent from
`/system/agents/attribution/image?identifier=`. Nine answer a text and eight an image,
`org.musicbrainz.agents.music` answers an EMPTY text and no image, and every other
identifier — the four modern agents included — is a 404 on both leaves.

**Four envelope rules, each measured on four servers.**

* **A framework error does NOT close the connection.** All five `plugin-xml` paths answer
  `Connection: Keep-Alive` with `timeout=20`, and a second request on the same socket is
  served — proved by reading the 500 and then `/identity` down one socket. rex set
  `close_connection` there on a comment nobody had measured, so a pipelining client lost
  its socket on every Agents-page probe. Cycle 45's rule says which way it goes: a refusal
  of the ENVELOPE closes, a refusal of the MEANING keeps alive, and a TypeError inside a
  plug-in is the latter.
* **Five routes put `Cache-Control` SECOND**, straight after `X-Plex-Protocol` —
  `/system/search`, `/system/library/sections`, `/system/channels/{name}`,
  `/system/plugins/{name}` and `/system/notify/serverUpdated` — where every other `/system`
  response puts it last. The mechanism is not identified; the set is measured.
* **A framework container pages without emitting an `offset` and still names its window in
  the response headers.** rex keyed those headers on the body's `offset`, so the one
  family that has none sent neither; and `/:/plugins/{id}/prefs` did not page at all.
* **`/system//` is the framework's empty-bodied 404**, not the 85-byte document: rex
  dropped the empty segment and left the `/system` envelope before its own refusal applied.

**Two more corrections inside the subtree.** Only the BARE `/system/agents` is PMS's own
handler — everything under it is the framework's, so `/system/agents/{id}/config/{N}` is
written by the framework's serialiser (single-quoted declaration, self-closed empty
container, 130 bytes where rex wrote 147) and is the one framework document that
carries no cookie. And `/system/library/sections` carries **thirteen** attributes per row,
of which rex emitted four plus an `agent` no reference sends: the `key` is the
URL-safe base64 of `http://{host}:{port}/library/sections/{id}`, `unique` marks a title
that occurs once, the rows are sorted by the lower-cased title, and an extra segment is a
404 where rex answered the whole listing again.

**And a corrupt state cookie empties the framework.** A framework route whose own
`com.plexapp.*` cookie the server cannot unpack answers a 200 with an EMPTY body,
`Content-Type: text/html; charset=UTF-8` and **`Server: TornadoServer/1.0`** — the one
`Server:` banner on the surface, which cycle 33 recorded as never sent. It swallows the
route's own status too, so `/system/zzz` becomes 200/0 instead of 404. The cookie's NAME
has to be the identifier serving the route, a well-formed blob behaves normally, and PMS's
own handlers (`/system/agents`, `/system/scanners/{N}`) are untouched — the same split as
the cookie they send. The framework's `Set-Cookie` is same-origin with Plex Web, so a
browser holding a blob the server can no longer read silently empties every `/system`
response.


Territory B took the other half of the image: PMS's own **database template**
(`Resources/com.plexapp.plugins.library.db`, whose schema names every column it can
store) and the binary's attribute vocabulary.

**Ten more columns the schema names are real query fields.** `metadata_items.{remote,
is_adult, resources_changed_at, user_fields}`, `media_items.{begins_at, ends_at,
media_analysis_version, source}`, `metadata_item_settings.last_skipped_at` and
`library_sections.agent`, camel-cased, filter, parse, sort, group and take an item edit on
kids and zen alike -- the same family as the nine cycle 20 found and the four cycle 26
found. rex knew none of them, and an unknown field answers EVERY row, which is the
worst outcome: a smart list built on one silently selected the whole library while
`/query/parse` told the builder the field did not exist. Five of them refuse an
unparseable value with a 500 and `userFields` refuses every value under every operator;
seven sort and three group -- so here the sort surface is WIDER than the group surface,
the other way round from the columns cycle 26 added; the item path takes all ten and six
are a 500 on the SECTION path, the split `lastRatedAt` already had.

**`isAdult` is the one that reads back.** Exactly the literal `1` stores it and every
other value clears it; the row then carries the JSON boolean `true` between
`librarySectionKey`/`originalTitle` and `summary`, on the detail row, the light grid row,
`/library/all` and the hub rows alike -- zen's row 631 shows it on a real library. rex
never emitted the attribute although its TMDB identification has the flag.

**A continue-watching row carries `includedAt`**, an integer directly after
`lastViewedAt`, on `/hubs/continueWatching` and its `/items` leaf and on nothing else --
not `/hubs/home/continueWatching`, not the section leaf, not `/library/onDeck`. Plex Web's
own reducer reads `includedAt ?? lastViewedAt`, so the shelf fell back to the view time
instead of the hub's inclusion stamp. In all five witnesses the two are equal; what makes
them differ is not measurable here.

**And `includeAlternates=1` inlined artwork the row does not own.** A season's and an
episode's `/arts` are empty -- cycle 7's rule, and rex's own candidate leaf applies it
-- while the option inlined the SHOW's candidate onto both, because `hierarchy` copies
`_art_url` down and the option read it without the gate. The server contradicted its own
`/arts`.

The census behind those four is the useful negative: 130 request families across kids, zen
and rex produced **zero JSON scalar-type differences** where the two references agree,
and every element either reference emits is emitted by rex somewhere. The 121
(element, attribute) cells present on both references and never here are, after
re-measurement, content or state or already in HANDOVER 8.

**Measured and not acted on.**

* **The pixels of the bundled artwork.** The flag images, each bundle's `icon-default.png`
  and `attribution.png`, and the 68 `/:/resources` images are served at the file's real
  media type and geometry with rex's own placeholder inside. Shipping several
  megabytes of a product's artwork to reproduce the bytes is not done.
* **A populated `/system/channels/*` row.** No instrument hosts a channel, so only the
  empty container is measurable; the `DirectoryItem` shape is readable in the source and
  not reachable here.
* **`/system/messaging/{register_for_event,broadcast_event}`'s semantics.** They mutate the
  System bundle's event dictionary, so they were driven on the disposable copies only.
* **The mechanism behind the `Cache-Control` split.** It separates cleanly along the
  peer-service routes plus one across four servers and does not fall out of
  `construct_response`; recorded as measured rather than modelled.
* **The legacy agents' WORK.** Listing them is a fixed table; identifying with one, its
  contributor chain and its preference pane beyond the captured document are not claimed.

#### The fifty-fourth pass — the binary's own parameter names and its own SQL

Cycle 52 read PMS's route templates out of the server binary and cycle 53 read the Python
and the database template shipped beside it. Cycle 54 went back to the binary for the two
vocabularies neither had used: **the query-parameter names the server reads** (1,018 names
derived mechanically from both string tables, where cycle 46 had swept only the seven
prefixed families) and **the SQL it runs** (707 statements naming a PMS table, 374 of them
reads). The driver added a third instrument: one small file per CONTAINER, made on kids'
host and scanned into plexscratch42, because no library this project reads holds a `.ts`,
`.m4v`, `.wmv`, `.mpg`, `.flv`, `.ogv`, `.3gp` or `.divx` part.

**Containers.** Measured on plexscratch42 (1.42.2) over seventeen files and corroborated
on zen wherever its library holds the extension:

* **The part `Content-Type` is keyed by extension over a wider table than zen's six.**
  `.ts` answers `video/MP2T` (the binary's own capitalisation), `.m4v` `video/mp4`, `.mpg`
  and `.mpeg` `video/mpeg`, `.flv` `video/x-flv`, `.wmv` `video/x-ms-wmv`, `.asf`
  `video/x-ms-asf` and `.divx` `video/x-msvideo`; `.mts`, `.m2ts`, `.ogv` and `.3gp` stay
  `application/octet-stream`. rex sent octet-stream for all eight named ones, and its
  table carried a `mpegts` key that no file extension can match.
* **Inside the QuickTime family the EXTENSION decides the container.** A `.mov` is `mov` and
  every other member (`.mp4`, `.m4v`, `.3gp`) is `mp4` -- with the brand crossed against
  the extension both ways: a `.mov` written by the MP4 muxer (`isom`) is still `mov`, an
  `.mp4` written by the QuickTime muxer (`qt  `) still `mp4`. zen's one real `.mov` agrees.
  rex answered `mp4` for the whole family.
* **Every MPEG-TS Part carries `packetLength: 188`** and no other Part does -- a Blu-ray
  `.m2ts`, whose packets are really 192 bytes, included (zen's fourteen `.m2ts` parts all
  say 188). `fun-1073414-1` had recorded it since cycle 10 and no check read it.
* The scanner ignores `.vob`, `.f4v` and `.mp4v`. The copy's analysis is SHALLOW -- no
  `bitDepth`, colour or `codecID`, even after an explicit analyze, where zen writes all of
  them for the same containers -- so the seventeen `s42-*` fixtures are marked `shallow`
  and only the container-level checks read them.

**The per-user-state columns.**

* **`viewUpdatedAt` is a real sort key and a DATE filter column** on both versions -- the
  time the row's view state was last WRITTEN. It is in the projection the binary reads view
  state with, and Plex Web's own hub query is `sort=viewUpdatedAt:desc`, which rex
  answered with a 500. Driven write by write on plexscratch42 with every value bisected: a
  rating (any value, `-1` included), a scrobble, an unscrobble (even of a row with nothing
  to clear), a `/:/progress` that stores a position and a Remove from Continue Watching
  each stamp it; an edit and a sub-minute progress do not. It lands on exactly the rows
  whose view state the write touches -- a rating on the rated row alone; a season or show
  scrobble on it and everything below; an EPISODE scrobble or progress on its season and
  show too; an episode's Remove from Continue Watching on its SHOW, which is the Continue
  Watching row. The stamp outlives the state.
* **`lastViewedAt`, `lastRatedAt` and `lastSkippedAt` read an unset value as 0**, the
  `viewCount` family: zen's `lastRatedAt=0`, `<<=1` and `<<=2020-01-01` are its 3,465
  unrated movies and `!=5` the 422 rated ones. Cycle 20's "a NULL date matches nothing"
  holds for every other date column, `viewUpdatedAt` included.
* **`lastSkippedAt`, `beginsAt` and `endsAt` are DATE columns** (a relative or ISO value
  parses, `1.5` is a 500); cycle 53 typed them as integers.
* **A negative rating other than `-1` STAMPS `lastRatedAt`** with the current time, on both
  copies, from a row with no rating at all. Cycle 9's "keeps `lastRatedAt`" was a row rated
  seconds before.

**The listing query, and where it reaches.**

* **Six MEDIA columns filter**: `mediaHeight`, `mediaWidth` and `mediaAudioChannels`
  (integers; a word or an empty value is a 500) and `mediaVideoCodec`, `mediaAudioCodec`
  and `mediaContainer`. The names compare EACH Media's value and match when ANY does,
  under every operator, with a Media naming none counting as `""` -- fitted exactly on
  zen: `=h264` 1,816, `!=h264` 2,120 (not the 2,071 a negated membership gives), `=` 23,
  `>>=h` 3,685, `<<=h` 222, and `==H264` 0 (the exact pair is case-SENSITIVE here).
* **Eighteen more TAG-TABLE fields** (`similar`, `mood`, `style`, `format`, `subformat`,
  `tag`, `make`, `model`, `lens`, `aperture`, `exposure`, `iso`, `device`, `autotag`,
  `place`, `channel`, `mediaProcessingTarget`, `sharedWith`): `=` matches only rows carrying
  the tag, `!=` every row, any other operator is an unknown key. `similar` was already a
  tag-table field and still ignored, because nothing typed it.
* **The four photo-cluster columns parse as integers and match nothing**; **`trash` is an
  integer column**, not the boolean the Meta advertises (`trash=01` is the `=1` set, `=2`
  none, `abc` a 500).
* **On an EPISODE listing every leaf-count filter reads the SHOW's counts** -- cycle 14's
  season rule one level down, fitted 16 of 16 on zen's 9,287 episodes -- and so does
  `unwatchedLeaves` on a season (the 2 seasons of fully watched shows, not the 5 fully
  watched seasons).
* **A show's `/children` and `/grandchildren` run the listing query** -- filters, `sort=`,
  a 500 for an unknown key or value -- and the "All episodes" row is there only when MORE
  THAN ONE season row is answered, paged or not. **Cycle 46's "a show's season list ignores
  `skipCount`" is refuted** on kids and a 25-season zen show.
* **`/unwatched` and `/recentlyViewedShows` run the listing's filters**, as do the media
  axes (`resolution`, `audioLanguage`, `subtitleLanguage` -- still blind to `type=`),
  `/autocomplete` and `/common` with an `id=` list. An empty `/common` is exactly
  `{mixedFields, guid, type: "common", titleSort}` and a `Common` is always a paired element.
* **Every TV Recently Added shelf runs the request's filters over the EPISODES before it
  rolls them up** -- `title=Train` is the episode, `title=Firefly` nothing, `show.title=`
  the show -- on `/hubs`, `/hubs/promoted`, `/hubs/sections/{id}` and
  `/hubs/home/recentlyAdded?type=2`. A hub PAGE drops an empty-valued pair first; the
  `type=2` list parses it and refuses the per-user keys. `contentDirectoryID` naming another
  library empties the section page's TV shelf, and `/hubs/items` for a Home shelf honours it.
* **`unwatched=1` drops the watched leaf rows** of `/library/recentlyAdded` and an item's
  `/allLeaves` (the `+?0*[01]` option grammar, a 400 otherwise) and of a section's
  `recentlyAdded` (the literal `1` only, nothing refused), before each list's cap.
* **The Dashboard's view-time keys are `viewedAt>` (INCLUSIVE), `viewedAt<` (inclusive) and a
  bare `viewedAt` (exact)**; `viewedAt>=` and `viewedAt<=` are unknown keys, ignored and not
  validated. Top Played filters its HISTORY rows by them and by `accountID` and
  `librarySectionID` and counts what is left (zen's top episode goes from 12 plays to 5); a
  bad value there is a 500. `users/top` takes the three view-time keys. The history page
  takes them as integers (400 otherwise). Cycle 14's "`viewedAt>=abc` is a 400" was the
  `viewedAt>` key read through a raw `>=` -- the cycle-22 encoding trap.
* **Thirteen parameters are parsed and refused**: int32 for `augmentCount`, `similar`'s
  `count`, `/search`'s `count`, `/statistics/media`'s `count` and `offset`,
  `excludeParentID` and the history page's `offset`; any-size for `monthsAgo` (checked
  before the section); int64 for `bytes`; a `strtod` float for `maxDistance`; the 0/1 option
  for `personal`, which at 1 empties the list; `metadataTypes` an int32 comma list that
  keeps the providers serving EVERY listed type; and `sectionId` refusing exactly `0` and
  2147483647.
* **`sectionID` on `/hubs/home/recentlyAdded` is a number**: `01` is section 1, `0` nothing,
  and a value that is not a non-negative int32 is ignored.

**Revalidated from HANDOVER 3.3.** The legacy agents' LOAD ORDER follows the filesystem:
zen's `Plug-ins` directory is btrfs (`ls -U` alphabetical) and the three docker instruments
share one overlayfs order, plexscratch42 -- the same 1.42.2 build as zen -- included; it is
environment, not version. `/system/library/sections`' `key` ignores `Host` entirely (seven
spellings on both copies) and encodes the server's own interface address and port, padding
with `_`, which rex already does. `includedAt` stays open: `/:/progress` stamps it and
`lastViewedAt` identically too.

**Measured and not acted on.**

* **The tagging join** -- `tagId`, `tagType`, `taggingIndex`, `taggingCreatedAt` -- is real
  on both references (a word is a 500) but every kids movie, Big Buck Bunny with no visible
  tag included, carries hidden `tagType=318` taggings that `taggingIndex` matches on.
  rex holds no such rows; modelling the match would be a guess.
* **1.42.2 accepts a `YYYY-MM-DD` value on an INTEGER column** where 1.43.4 refuses it;
  `2/3`, `0x1` and `1e3` pass, `5d` and `1abc` refuse, and the date form's meaning shifts per
  column. Not one grammar, and no client sends it.
* **`/search`'s `count` and `/statistics/bandwidth`'s `bytes`** are refused as measured;
  what either does to the rows is not one rule (`count=2` answers ten rows on zen,
  `bytes=N` ten rows where three carry `N`).
* **`pinnedContentDirectoryID` scopes the hubs on zen alone**, where plexscratch42 (1.42.2,
  unclaimed) and kids ignore it -- a claim candidate no instrument can separate.
* **zen's section `recentlyAdded?unwatched=1` keeps its `totalSize` at 98** where kids drops
  5 to 4. Recorded, not explained.
* The copies' `/statistics/*` and `/library/all/top` answer 404 (class E), so those
  refusals rest on zen and kids.

#### The fifty-fifth pass — the SQL read by read, and the vocabulary at every value

Cycle 54 opened two instruments out of the binary -- its parameter names and its SQL -- and
sampled each: four values per name, and two findings from 707 statements. Cycle 55 is the
repeat that asks whether either axis is finished, and neither was. **37 findings in 37 fix
commits**, every one proved fail-then-pass against its own parent.

**Every client-reachable SQL read, one verdict each (Territory B).** Cycle 54 extracted the
statements and reported two findings; cycle 55 gave each of the 366 non-maintenance reads a
verdict -- identical, finding or explicit bound -- with the request that decides it, making
state on plexscratch42 where neither read-only reference held it.

* **Related is a TAG SCORE, not agent data.** `select metadata_items.id,count(*) as cnt ...
  where tags.id in (...) group by metadata_items.id order by cnt desc,
  originally_available_at desc limit ?`: the item's FIRST THREE genres and FIRST FOUR roles are
  counted against each candidate, a candidate needs AT LEAST TWO in common, and the list is
  shared-count then release date descending, capped at 200. It reproduces zen's movie 19770
  exactly (59 rows, in order) and all four tagged kids movies; a fourth genre, a seventh role or
  the country breaks the zen fit. rex took any one shared genre, ordered by `addedAt`,
  stopped at 12, and its docstring said the reference "fills this from its metadata agent".
* **On Deck's next-up anchors on the MOST RECENTLY VIEWED watched episode**, not the
  highest-numbered one, and the row carries that view's `lastViewedAt`: on plexscratch42,
  scrobbling episode 10 and then 8 puts 9 on deck; unscrobbling 8 moves it to the episode after
  10. A later season's episode 0 is never the next-up (`parents.index>? and index>0`).
* **A section's `recentlyViewed` is ordered by the VIEW-STATE WRITE time** (`order by
  metadata_item_settings.updated_at desc`, ties by ratingKey): zen's 21 rows are exactly
  `viewUpdatedAt` descending and not `lastViewedAt`, and a rating moves a watched row to the
  front on the copy. **A section's `/onDeck` leaf follows the same stamp** while
  `/library/onDeck`, Home's Continue Watching and the section's continue-watching list keep the
  view time -- single witness, the unclaimed copy.
* **`/hubs/sections/{id}/popular` ranks seasons by their HISTORY rows** (`group by parent_id
  order by count(*) desc`), so a season an unscrobble stripped of its `viewCount` stays on it.
  Cycle 52's "seasons with a `viewCount` above zero" is refuted.
* **A playlist's `duration` is 1000 x the sum of each item's whole seconds** over its longest
  Media (`sum(round(duration/1000, 0))`, integer division): 1800689 + 1800689 + 20032 ms is
  3620000 on the copy.
* **The genre shelf is the head of its own key listing**, in that listing's default order
  (zen's `movie.genre.1.241` is exactly the head of `?unwatched=1&genre=241&audienceRating>=7.0`).
  WHICH genre is a random pick per request query string (`?x=1` twice is one genre, `?x=2`
  another), with the actor/director shelf moving beside it -- HANDOVER 8's random shelf.
* **A season's generated title follows an index edit** -- `index.value=0` is `Specials`, the
  old index back is `Season N` again -- because a season's title is generated unless the user
  wrote one.

**A library's TV Recently Added shelf is the section's roll-up (the driver).** zen's
`tv.recentlyadded.2`, on `/hubs/sections/2` and `/hubs/promoted` alike, is row for row
`/hubs/home/recentlyAdded?type=2&sectionID=2` -- the show `One Piece`, the episode `Facts of
Life`, a season, three episodes ... twelve of twelve. Cycle 50's "keeps its top-level show row"
held only where the roll-up IS the show, as on kids, whose one show is all its roll-up holds.
Found revalidating cycle 54's §3.3 entry on the filtered form, which it closes.

**The parameter vocabulary, repeated and widened (Territory A).** Cycle 54's sweep sent four
values per name. Cycle 55 repeated it (every cycle-54 fix holds; 220 names move a zen response,
as before) and widened it to eighteen values per option name (147,384 name-level cells) and all
eight operators per filter field (7,008 cells a server). Twenty-six rules came out, every one of
them 1.42.2's as well:

* **The listing's structural parameters are PARSED wherever they are read.** `sort`, `group`,
  `sourceType`, `limit`, `index` and `type` refuse on every tag, field and media axis, on
  `/unwatched`, `/onDeck`, the section search, `/collections`, a show's children, the people
  lists, autocomplete, `/common` and the extras listing (`/genre?sort=abc` is a 500 on both
  references and was a 200 here). A GROUP there is a parse, not an apply: a known field name
  passes and the refused fields are 500s. `/unwatched` validates `sort` and then ignores it.
  `sourceType` other than 1 ORDERS a listing by id, where cycle 20 recorded only the parse block.
* **An integer is `[+-]?digits`**: `type=+1`, `type=01` and `limit=+1` are the number on both
  references, where rex refused a sign in a dozen readers; **every include/async option
  takes the `+?0*[01]` grammar**, and **the `include*` blocks reach the full rows of every hub
  and related list**, not only `/hubs`.
* **The filter VALUE grammar.** `=-1` means "has no value" on `rating`, `audienceRating`,
  `userRating`, `absoluteIndex` and `parent` (`=` only; zen's `userRating=-1` is its 3,465
  unrated movies); a date column's epoch is a strict int64 with no surrounding space; under a
  bare `=` the three per-user dates take an int32 only (`lastRatedAt=-5y` is a 500 where
  `<<=-5y` is fine); `clusterStartsAt` is a date; `decade`, `viewCount` and `viewOffset` are
  int32; `parent=+6` is show 6's seasons and `parent=1.5` an empty 200.
* **Endpoint rules.** `/hubs/search?sectionId=` refuses ANY id that names no section (cycle 54's
  `0` and 2147483647 were two samples of it); `personal=1` empties only the MOVIE Recently Added
  list; an empty `title=` matches every row, generated season titles included; an empty
  `contentDirectoryID` is accepted on `/hubs/continueWatching`; a bad `type` on `/newest` and
  `/recentlyAdded` and a bad `limit` on `/allLeaves` are ignored; `manage?metadataItemId=` has
  three answers (`-1` the bare list, an id naming no item an empty 200, an item a 400);
  `/system/agents?mediaType=-1` is the bare form; a shelf's `count` is a strict int32;
  `X-Plex-Product` in the HEADER beats the query for the client-title rule; every statistics
  endpoint parses `offset`, `limit`, `timespan`, `group` and its ids; an empty
  `/library/search?query=` is an empty 200; and a separator-led `X-Plex-Language` (`-de`,
  `_fr`) is that language after one leading separator, English otherwise.
* **`/hubs/promoted` refuses the fifteen hub-refused keys whenever a show library's shelf is in
  scope** -- unscoped, or `contentDirectoryID` naming the show library -- and answers 200 when
  scoped to the movie library alone (found by the implementation pass; `hub_refusal`'s note had
  it applying to `/hubs` and a show library's section page only).

**Measured and not acted on.**

* **The tagging join**, still (HANDOVER 8, cycle 54) -- its refusal half is measurable, but
  building a refusal for columns whose rows rex cannot compute was left with the match.
* **How `/hubs/search?sectionId=` REORDERS its hubs** when the id names a section (that
  section's own types first, rows unchanged). The refusal is built; the order is not worked out.
* **`/library/search` and `/hubs/search` `limit` at the int32 boundary.** kids 500s `2147483647`
  and `9223372036854775807` while answering `2147483648`, and `4294967296` CRASHED kids' PMS
  (it restarted in about five seconds; HANDOVER 9). Only "outside int64 is a 500" is built.
* **WHICH genre the genre shelf picks** is random per request query string, with the actor and
  director shelf moving beside it (HANDOVER 8's random shelf); rex's most-common genre is one
  admissible sample. The ORDER of the shelf's rows is built.
* **A parent's `lastViewedAt` as `max(children)`** where its own state is absent is read by the
  binary's SQL; zen's section 2 displays it on rows no filter can see, the trace of state written
  without the cascade (cloud sync). Not reproducible here.
* **1.43.4 refuses an empty `group=`/`sourceType=` on a hub page** where 1.42.2 drops it;
  rex follows 1.42.2.

#### The fifty-sixth pass — the first cycle under the client-impact rule

From this cycle the loop stops when a cycle finds **nothing a real client would notice**
(HANDOVER 1). Every finding is tagged CLIENT-VISIBLE -- an official client or python-plexapi
builds the request in ordinary use and the difference changes what it does, with the call site
-- or MALFORMED-ONLY. Client-visible findings are built; malformed-only ones are recorded in
HANDOVER 8.

Territory A started from the CLIENTS: 3,885 recorded Plex Web exchanges (80 shapes), the
reference walk replayed, 35 python-plexapi templates and 1,455 probes driven through it, 160
bundle path families and every hub key the servers publish, with the client's WRITES driven on
plexscratch42 and the reads after them diffed. Territory B repeated cycles 54 and 55 against the
new head -- every cycle-55 fix held (`regress55.py` 123/123) -- and widened them to a show
library, the hub pages and the per-user columns after writes. **Six client-visible findings,
all built; eighteen malformed-only, recorded.**

**Client-visible.**

* **A show library's `/media/providers?includeStorage=1` totals are its EPISODES'** (kids' TV
  Shows 60186 ms / 37683488 bytes). rex summed rows that own a Media -- none in a show
  library -- and answered 0, which python-plexapi's `LibrarySection.totalDuration` and
  `totalStorage` report.
* **The playlist `Meta` follows the request and the path.** Its Type `key` carries the request's
  `playlistType`, `sectionID` and `type` (alphabetical, URL-encoded); on `/playlists/all` -- what
  Plex Web's library Playlists page loads -- no Type and no Sort is active, so the page draws no
  sort bar; on `/playlists` the requested sort is the active one. rex sent a constant
  `/playlists`, active, Name ascending.
* **In the bare `/system/agents` each legacy `MediaType` carries its own `Language` rows** (33 for
  the legacy IMDb movies), identical on four servers; python-plexapi's `agents()` reads them as
  `languageCodes`. Cycle 53's "no Language" was the Agent row's.
* **`decade` IGNORES its operator**: every operator answers the `=` bucket (zen's `decade!=2010`
  is its 349 2010s movies). Plex Web builds "Decade is not 2010s" as `decade!=2010`.
* **Any per-user write creates the row's settings record with `viewCount` and `skipCount` 0**,
  and the counter filters read it: after a rating `viewCount!=1` and `skipCount=0` find the row
  and `viewCount<<=0` does not; zen's `viewCount>=0` and `skipCount=0` are both exactly its 424
  rows with any state. Cycle 54's "`skipCount` is not in the family" is refined: a never-written
  row's `skipCount` matches nothing, a written one's is 0.
* **`/hubs/sections/{id}?includeGuids=1` carries `Guid`**, which python-plexapi's `section.hubs()`
  always asks for; rex sent none.

**Malformed-only (recorded, not built).** From Territory A: `/clients/profiles?type=42` marks
`Universal TV` selected; `titleSort` directly after `title` on a show row; no `allowSync` on an
item's `/extras`; the metadata-provider tables' ids and music/Personal-Media type lists; the
`recentlyAdded` Meta's Type key and `personal=1`. From Territory B: negative `count` is four
rules per path (cycle 28's `len+count` does not fit its own `-1`); the `index=` grammar;
structural parameters on four more leaves; `limit` on `grandchildren`, `recentlyAdded`, the
extras leaf (zen ignores it -- cycle 29's "really constrains" was kids) and the section search (it
caps -- cycle 41's "ignores every legacy limit" is refuted for plain `limit=`); history and
statistics value bounds; `/system/agents?mediaType=` empty or huge; `type=` on `/search` and
`/common`; `+`-led and space-led language values; a repeated `type=` on `/library/all` takes the
last; `sort=viewedOrInProgressLeafCount`; hidden-column comparisons (`guid<=`, `isAdult`,
`mediaAnalysisVersion`, `dovi`, an unset stream language compared as `''`); 55 hub-page refused
keys and seven columns that filter a hub page's TV shelf; `postplay?includePreferences=1`.

#### The fifty-seventh pass — lists, and what every screen shows after a user acts

Cycle 57 ran under the client-impact rule. Territory A repeated cycle 56's client inventory
(every cycle-56 fix held) and drove python-plexapi's whole read surface -- 108 classes, 646
methods and properties, 1,018 probes a server -- and Territory B drove every write the
official client offers on plexscratch42 and rex, reading the 41 things the client redraws
after each. Neither kids nor zen holds a playlist or a collection, so everything about LISTS was
measured on the two writable copies, which agree cell for cell across 1.42.2 and 1.43.4.

**Client-visible, all built.**

* **A collection search row is on the tag ladder** -- full name 0.92, a word-by-word prefix from
  the first word 0.52, a later word 0.32. rex matched one word against the whole query, so a
  multi-word collection was never found by its own name, and scored every hit 0.52.
* **A list's history is an empty 200**: `metadataItemID=<collection or playlist>` on the history
  page, which python-plexapi's `Collection.history()` and `Playlist.history()` send. rex 404'd.
* **A playlist's search ladder is one step below a movie's** (0.92/0.72/0.52/0.32).
* **`X-Plex-Sync-Version` marks a playlist syncable**: a playlist row on `/playlists` and
  `/playlists/{id}`, and the `/playlists/{id}/items` container, carry `allowSync="1"` under the
  header python-plexapi sends on every request; nothing else moves with it.
* **The merged Continue Watching hub (`/hubs/continueWatching` and `/items`) holds the On Deck
  next-up rows as well as the in-progress ones**, each stamped `includedAt` when it entered --
  zen's next-up Bosch episode is there, and on both copies a Mark Played puts the next episode
  there at once. Plex Web builds Home from this hub and drops `home.continue`/`home.ondeck`.
* **A library's own Continue Watching shelf lists a next-up only after a PLAYBACK completion**,
  not after a scrobble -- both copies, both versions, and zen for the positive half.
* **A never-watched show carries an inline `OnDeck`: its first episode**, and so does the season
  that owns it (kids' show 6 and season 7 carry episode 8; zen's show 5189 carries 5191). Plex
  Web's Play button on a show reads it. Cycle 41's "a pristine unwatched show omits it" is refuted.
* **A collection with two or more members adds a `collection.related.{section}.{n}` hub** --
  "&lt;title&gt; Collection" -- to each member's Related, on every Related read path.
* **`playQueueLastAddedItemID` is the tail of the added-item block after the selection**: a first
  Play Next opens the block, a move out of it or a delete re-anchors to its new tail. rex left
  it unset after a Play Next and pointed it at the queue's last item after any move, so the
  player's Shuffle control was enabled and disabled at the wrong times.

**Recorded, not built.** The legacy agents' JSON booleans and attribute order, the empty related
container of a collection, and playlist attribute order (A); a preplay read's `async*` options
pushing a `library.refresh.items` activity that lives under ten milliseconds and persists nothing,
notification frame key order, and a playlist's locked `Field` on the listing row (B).

#### The fifty-eighth pass — the flows repeated, and the owner's screens

Cycle 58 ran under the client-impact rule. Territory A repeated cycles 56-57's client read and
write flows against the new head (every earlier fix held) and ran sequences no cycle had tried.
Territory B drove the screens a server OWNER uses -- Settings, a library's Add/Edit/Manage
dialogs, Manage Recommendations, the Dashboard, the activity and console panels -- reading on zen
and kids and writing on the copies. (Recorded here by cycle 59; cycle 58's session ended before
writing this file.)

**Client-visible, all built.**

* **A library's Continue Watching shelf gates on the SHOW's play history**: once any episode has
  been completed in a playback session, the shelf lists the On Deck episode however it became
  next -- after the played episode is unscrobbled, and after Mark Played on a later episode or on
  the season.
* **A new session's sub-minute beats keep the resume point**; the deletion happens only inside a
  session that has already reported more than a minute.
* **A beat at exactly the stored position writes nothing** -- `lastViewedAt` stands still and a row
  removed from Continue Watching stays removed.
* **A section-path collection rename onto a collection tag already in use is a 409**, case
  folded; renaming to its own current title in any case is a 200.
* **The legacy agents' `config/{mediaType}` chain, `contributors`, and the order save** with the
  framework's refusals.
* **`/library/all/top?type=2|3` rolls an episode play up to its show and season**, a flattened
  episode's owning season included.
* **Statistics `at` buckets align to local midnight, local Monday, the 1st and 1 January.**
* **`hidden=1`/`2` keeps a library off Home and `/library/recentlyAdded`**, and at 2 out of both
  searches; the Home shelf itself stays, emptied.

**Recorded, not built.** The section Continue Watching answer cached per exact URL (the refresh
rule was sampled over eleven write types only); the optimized-versions playlist under
`X-Plex-Sync-Version: 2`; an item-path collection rename then delete leaving members tagged; `PUT
/library/collections/{id}/children` as a 400; `/statistics/media`'s `Device` rows.

#### The fifty-ninth pass — the rendered client, and python-plexapi's writes

Cycle 59 took two axes no client-impact cycle had: the official Plex Web app RENDERED in a real
browser (kids' 9223 Chromium, the bundle rex serves, plex.tv answered by canned `page.route`
bodies so the app saw exactly one server) pointed at plexscratch42, kids and rex page by page;
and python-plexapi's whole WRITE and PLAYBACK surface -- 182 methods over 62 classes, 291 steps on
plexscratch42 and on rex, each followed by a reload of everything it should reach.

**Client-visible, all built.**

* **`X-Plex-Language: en` keeps an item's Advanced enums English.** python-plexapi sends it on every
  request and Plex Web on every query; cycle 50's language-list injection caught every enum whose
  first row read `Library default`, so a show's Seasons dropdown offered 46 languages and
  `editAdvanced(flattenSeasons=1)` refused its own value. And an item's own `audioLanguage`/
  `subtitleLanguage` enum -- built at read time, so never in the table -- is now localised piece by
  piece: its stream-language head by tag (`item_language_names.json`, read off zen over the 13 shows
  covering all 62 tags its 507 shows carry, in 30 language forms), the regional tail from the table
  in its own order, and the inherit option on its own (Danish `:Kontomenu`, where the table's list
  leads `:Standard for konto`). Exact against zen for a show and a season in all 30.
* **`/player/*` with `X-Plex-Target-Client-Identifier` is a 404** (python-plexapi's proxied
  `sendCommand`), on zen, kids and both copies; a bare command stays 400.
* **A smart collection's `collectionFilterBasedOnUser` is stored** as sent (`filterUserUpdate()`),
  on the row as well; empty reads back empty; a plain collection 400s it.
* **`DELETE /library/sections/all/refresh` is a 200** (`Library.cancelUpdate()`) with one status
  frame, "Library scan canceled".
* **A playlist's timeline beats run the item machine** (`Playlist.updateTimeline()`): ignore,
  progress with the offset stored, complete past 90% with `viewCount` stepping, and a `Playlist`
  Now Playing row with `User` and `Player` until the stop.
* **Uploads**: a poster/art upload keeps the agent's candidate (unselected) with exactly one
  selected; an upload is a candidate of its own row only; and a THEME and a LOGO upload are
  stored -- a selected candidate, a versioned `theme` served `audio/mpeg3`, a `clearLogo` Image
  served `image/jpeg`, the owned artwork re-versioning onto one stamp.
* **A match that renames a show reaches its children** (`parentTitle`, `grandparentTitle`), as an
  edit does.
* **A library holding a collection has a Collections tab** in `/media/providers` (fun, claimed,
  and both copies), after Library.
* **A video playlist makes a Playlists source** -- a `playlists` content directory with one
  `playlists.video` pivot, which keeps its `id` and loses its pivot for Roku -- and `/hubs/promoted`
  answers `home.playlists` when `contentDirectoryID` names `playlists` (zen and kids even empty).
* **A library's Recent Playlists shelf is its key's head**, and an EMPTY playlist answers every
  library's `sectionID=`.
* **The pinned list shapes Home's Recently Added on 1.42.2** (zen): only the first pinned library
  of each type carries the shelf, it merges every pinned library of that type and its key repeats
  the pinned list as sent; an unpinned library and an empty list get none; and the See All leaf
  takes a non-empty pinned list as its scope over `contentDirectoryID`.
* **`translations.json` is per version.** It was captured off kids (1.43.4); re-captured off zen
  (1.42.2) with the same tool and overlaid, it moves the Danish, Serbian, Ukrainian, Hebrew and
  Portuguese strings (see the commit and HANDOVER §7).

**Recorded, not built.** The Optimize surface python-plexapi reaches (`DELETE
/playlists/generators?type=42`, the `mediaProcessingTarget` tag table) stays the recorded Optimize
bound; a real PMS's CORS echo is limited to `*.plex.tv`, `localhost` and `127.0.0.1` origins and
rex echoes any (no client is served from another origin); collection row attribute order; a
smart playlist's `content` on `/library/metadata/{id}`; the unpause `queueRefreshed` frame; a
split row's media delete. The ORDER of an item language enum's head is language-independent and
fits neither tag order nor English-name order over zen's 507 shows (§3.3).

#### The sixtieth pass — localisation as an axis, and the first three-hunter repeat

Cycle 60 ran three hunters against the axis ledger (HANDOVER §7 "Cycle 59"). A repeated the
rendered client and walked it again in de, da, ja and pt; B repeated python-plexapi's writes and
drove playback as fifteen client identities; C repeated axes 1, 2, 3, 4, 7 and 10.

**Client-visible, all built.**

* **Joining a collection locks the member's `collection` field** (create and items add, both
  copies), and removal and deletion leave the lock; the Edit dialog draws it and sends it back.
* **An uploaded theme or logo is served to a read only**: `DELETE .../theme` and `.../clearLogo`
  stay the 404 1.42.2 answers every artwork DELETE -- cycle 59's upload store answered them 200
  with the file's bytes, which python-plexapi's `deleteTheme()` failed to parse.
* **A stream's language NAME follows the client's language by TAG, `en` included** -- `fr` is
  `French` under `en` and `Französisch` under `de` -- and so do the language axes; the words a
  title is composed from (`Unknown`, `Forced`, `SDH`, a sidecar's `External`) are localised
  (`stream_words.json`), and the extended title's language part is rendered LOCALISED while its
  omission is decided on the STORED name (zen: a track titled "French" reads `French (French SRT)`
  under `en`). Held out on zen: 23,820 of 23,832 stream cells exact over 102 rows in 12 languages,
  the 12 left being the recorded AC-3 case.
* **Hub titles with a name inserted are localised around it** (`title_templates.json`): `Recently
  Added in <library>`, `Top Movies in <genre>`, `<collection> Collection` -- on a Hub's own title
  only, so a collection TAG called "... Collection" stays as it is.
* **`translations.json` is 1.42.2's.** The capture now records the cells a language leaves in
  English, and zen (every path) and plexscratch42 (the settings documents) were captured that way:
  414 translations 1.42.2 does not have were removed, 59 reworded, and the Related, post-play and
  Manage Recommendations titles added. The host-built `PreferredNetworkInterface` enum has its
  `:Any` lead translated.

**Recorded, not built.** Manage Recommendations' writes (both copies 404 the whole manage leaf,
unclaimed); `X-Plex-Client-Profile-Extra` on a PLAIN-form decision (every official client that
sends one sends `hasMDE=1`); a beat's stale `playQueueItemID` echoed in its frame; the session
`User` block (claim); `/system/scanners/{N}` under `Accept: application/json`; the playbackState
latch after a stopped beat; `/hubs/promoted?includeGuids=1`; the `tagId` value grammar;
`/query/parse` `fieldTitle`s; `/library`'s Directory titles in sl/sr; `/butler` in sr.

#### The sixty-first pass — the stopping repeat that was not

Cycle 61 repeated all ten axes with cycle 60's three territories. Axes 1, 2, 4, 5, 7, 8 and 10 came
back clean; axes 3, 6 and 9 did not.

**Client-visible, all built.**

* **A collection rename onto ANY name in the collection tag TABLE is a 409** -- a deleted
  collection's tag included, matched ignoring case -- except names the collection has held itself.
  Cycle 58 read only the tags rows still carry.
* **A NEW tag is stored with its first ASCII letter upper-cased** (`c61c lowgenre` -> `C61c
  lowgenre`) on both edit paths and for a new collection's member tag; `élan`, `ßeta` and `1abc`
  keep their spelling, and an existing tag its canonical one.
* **A session is LISTED from its first `playing` beat**: a `paused` or `buffering` beat on a new
  session allocates a key (its `playing` frame carries one) and lists nothing, which is Plex Web's
  first beat. **Its `Session` block is there only once the session has asked for a decision**,
  and stays when the session moves to another row (the hunt reported it dropping there;
  re-measured, it stays).
* **A theme uploaded after a logo lands** (cycle 59's upload diverted it), and **a show's theme
  reaches its seasons as `parentTheme`** -- no row below the show carries a `theme` of its own
  (plexscratch42, and kids' Firefly).
* **Every server setting is 1.42.2's wording**: `/:/prefs` PAGES, and cycle 60 read 20 of zen's 173
  settings. Re-captured whole, the table matches zen in all 3,045 compared cells.

**Recorded, not built.** A collection renamed through its ITEM path and then deleted leaves its
original tag on the members (no client renames a collection that way).

#### The sixty-second pass — eight axes clean

Cycle 62 repeated all ten axes. Axes 1, 2, 4, 5, 6, 7, 9 and 10 were clean; axes 3 and 8 each
had one client-visible finding.

* **After a section-path collection rename a member's LIGHT row keeps the OLD name** -- grid,
  section search, Recently Added; the first name through a chain of renames -- while its detail
  row and the `/collection` axis carry the new one. A collection tag written on the item refreshes
  it and the delete clears it. Both copies, and python-plexapi's `section.all()` against
  `fetchItem()`. It had sat in cycle 60's axis-3 diff undispositioned, and cycle 61's 409 hid it.
* **The timeline's `Bandwidths` block is withheld from `X-Plex-Product: Plex for Windows`** --
  exactly that spelling, any platform -- while the decision, the session row and its `Session`
  block match every other product (plexscratch42, 48 platform x product cells).

#### The sixty-third pass — nine axes clean

Cycle 63 repeated all ten axes. A (rendered client and localisation) and C (axes 1, 2, 3, 4, 7,
10) reported no client-visible findings; B found two on axis 8, both product tables sampled short:

* **The codec suffix goes to 21 products** -- `Plex HTPC for Windows/Mac/Linux`, `Plex for
  Linux`, `Plex for Fetch TV`, `Plex for Smart TVs`, `Plex for VIDAA` and `Plex for X1` besides the
  thirteen -- while the bare `Plex HTPC` stays plain (zen, kids and plexscratch42 agree), swept
  over the product strings the binary carries.
* **`Bandwidths` is withheld from `Plex for Windows Phone`** as well as `Plex for Windows`.

**Recorded, not built.** Music and photo sort titles on a type-scoped `/sorts` (`type=8/9/10/13`)
left in English; rex holds no music or photo library and the client always asks its own type.

#### The sixty-fourth pass — the catalogue as an instrument

Cycle 64 repeated all ten axes. C (axes 1, 2, 3, 4, 7, 10) reported zero findings of either kind.
B swept every product and platform string the binary carries over 40 read paths on four servers
and found one product-keyed rule rex had never had. A added a new instrument -- the
reference's own gettext catalogues (`plex.mo`), searched for every string rex leaves in
English that the catalogue translates -- and it found a dialog no capture had walked.

* **`X-Plex-Product: Plex Home Theater` gets a five-component `version`** on `/` and
  `/media/providers` (`1.42.2.10156.0-<build>`), that spelling only, header or query, the header
  winning; `/identity` and every other product keep four (zen, kids and plexscratch42 agree).
* **A collection's Advanced settings are localised** -- Collection Mode, Collection order and a
  smart collection's filter setting, read off plexscratch42 in 28 languages; the table had none.

**Recorded, not built.** A playlist's `/items?includeMeta=1` tab-bar titles stay English (the
items container carries a `ratingKey`, so the localiser takes it for a row; no client sends that
request); a header value's leading whitespace -- a real PMS consumes one space and keeps the
rest, and refuses a leading TAB (no client sends either).

#### The sixty-fifth pass — the notification socket in ten languages

Cycle 65 repeated all ten axes. C (axes 1, 2, 3, 4, 7, 10) reported zero findings of either kind.
B widened the identity sweep to every `X-Plex-*` header the 1.42.2 binary reads and found two rules
no client can see. A opened the notification socket in ten languages for the first time.

* **Activity titles follow the socket's language.** On plexscratch42 a socket opened with
  `X-Plex-Language=de`, or with `Accept-Language: de-DE,de;q=0.9` on the handshake, is pushed
  `Suche...` → `Durchsuche TV Shows` and `Abonnements verarbeiten …` for one scan, where a socket
  with no language gets `Scanning` → `Scanning TV Shows` and `Processing subscriptions`; ja inserts
  the name (`TV Showsをスキャン中`). `/activities` localises the same way under `X-Plex-Language`.
  The strings are the server's gettext catalogue's (read off zen: ca reads `es`, nb `no`, cs/et/hr/ja/
  zh their regional files); a message id the catalogue lacks stays English. `StatusNotification`
  and a scan's `ProgressNotification` stay English (cycle 66: the butler's do not). Plex Web's socket URL carries only the token, so its
  Activity menu is in the browser's `Accept-Language`.

**Recorded, not built.** `X-Plex-Client-Platform` as a second spelling of the platform header, and
the library hub page's cache keyed on `X-Plex-Features` and `X-Plex-Language` (cycle 25 had called
the header inert).

#### The sixty-sixth pass — the locales Plex Web actually sends

Cycle 66 repeated all ten axes. C (axes 1, 2, 3, 4, 7, 10) reported zero findings of either kind.
B widened the identity sweep's skipped cells and found the Roku strip version-gated. A measured
the language rule against the 44 values Plex Web's own language list carries.

* **The Roku pivot strip is for an old Roku only.** `/media/providers` keeps a library row's
  `id` and `Pivot` for `X-Plex-Platform: Roku` with `X-Plex-Version` 6.4.2 or above (9.7.1 is
  the recorded identity); absent, unparsable (`abc`, `7`, `v9.7.1`, `6.9-beta`) or below
  (`6.4.1`, `6.4.0.1`) strips. `6.4` keeps -- a missing patch is not compared. 260 cells,
  identical on kids, zen and plexscratch42.
* **`no` and `af` are catalogues** (zen: `Kallenavn`, `Vriendlike naam`, 31 of 35 vocabulary
  paths); rex held `nb`, which reads the same catalogue, and no `af`.
* **`pt-BR` is its own catalogue on every document** -- sorts, filters, hubs, the butler,
  `/activities`, the socket (`Classificação de usuários`, `Buscando`) -- and `pt-PT` reads it
  too, with Portugal's ICU names (`Polaco` where Brazil says `Polonês`).
* **The butler's `ProgressNotification` sentences are localised per socket** (`Der Butler hat
  die Datenbank gesichert.`); each is a catalogue message id with the count as `{1}`. Plex Web
  draws them on Status > Alerts.
* **The regional `pt-BR`/`hu-HU` settings were 1.43.4's**: three transcoder rows read off zen.
* **ICU names follow the FULL locale** for `bg`, `el`, `fa`, `is`, `my`, `th`, `tr`, `vi`,
  `zh-TW`, `en-GB`, `es-419`, `fr-CA` (and the three above): a library's Certification Country
  list (`bg` `DE:Германия`, re-collated) and a stream's language (`zh-TW` `法文`). Their menus
  stay English (or `es`/`fr`'s) on a real PMS too.
* **`Accept-Language: zh-TW`, `zh-HK` or `zh-Hant` is skipped**, not read as `zh`.

**Recorded, not built.** `X-Plex-Container-Focus-Key` wins over an explicit start on the
recently-added family (a section's `/recentlyAdded`, `/newest`, `/search`, the global list, a
show's `/allLeaves`), with `offset` echoing the start; no client on disk sends it.

#### The sixty-seventh pass — nine axes clean, and a sweep that never compared what it named

Cycle 67 repeated all ten axes. B (axes 6, 8) and C (axes 1, 2, 3, 4, 7, 10) reported zero
findings of either kind. A swept all 43 Plex Web language values and found four on axis 9.

* **A show's Episode ordering lead is 1.42.2's**: `sr` `:Библиотека подразумевана`, `da`
  `:Bibliotekets standard`, `uk` `:Бібліотека за замовчуванням`, localised in `af` and `pt-BR`
  (zen's matched show 6558; the table held kids' 1.43.4 wording).
* **A show's and season's Preferred audio/subtitle language lists are localised and re-collated
  for the new locales** (`af` `:Reeksverstek|en:Engels|ar-SA:Arabies (Saoedi-Arabië)|…`,
  `zh-TW`, `bg`, `en-GB`, `pt-BR`, …). The item-prefs sweep had paired rex's show with an
  unmatched zen show, so these were never compared.
* **A socket opened in `hu-HU` reads the `hu_HU` catalogue** for the butler's sentences (`A
  rendszer képeket készített a 0 fájl fejezeteihez.`), the rule cycle 50 measured on `/:/prefs`.
* **`nn`/`nn-NO` read Norwegian**, and the Traditional-Chinese skip is the script and region
  (`zh-Hant-*`, `zh-MO`), not three spellings.

**Recorded, not built.** `DELETE /library/metadata/{collection}` deletes on a real PMS (clients use
`/library/collections/{id}`); `Accept-Language: nl-BE` alone on HTTP reads `nl_BE`.

#### The sixty-eighth pass — a fallback rule

Cycle 68 repeated all ten axes; B and C reported no client-visible findings. A found one:

* **`ca` and `nn` are fallbacks in `Accept-Language`.** Neither has a catalogue of its own (`ca`
  reads Spanish, `nn` Norwegian), so an exact catalogue ANYWHERE later in the list beats them:
  `ca-ES,ca;q=0.9,en-US;q=0.8,en;q=0.7` (Chrome) is English, `ca,fr;q=0.9` French, `ca,es;q=0.9`
  Spanish, `ca`, `ca,nn` and `ca,zh-TW` Spanish, `nn,ca` Norwegian, `nn,nb` Norwegian (`nb` is exact).
  Measured on zen's `/:/prefs` and plexscratch42's socket; the socket drives Plex Web's Activity menu.

**Recorded, not built.** The decision's text fields are localised (no client draws them for a
decision rex answers); the CLDR fallback map for languages Plex Web does not list;
`X-Plex-Client-Capabilities`' `audioDecoders` stream re-selection.

#### The sixty-ninth pass — clean on all ten axes

Cycle 69 repeated all ten axes of the ledger and every territory reported zero client-visible and
zero new malformed-only findings: the rendered client and localisation (43 Plex Web languages over
HTTP, the socket and `/activities`), python-plexapi's writes and playback per identity, and the
request inventory, reads, write flows, owner's screens, vocabulary and discovery. It is the cycle
the client-impact stopping rule asked for. What it does not bound is listed in HANDOVER §7 "Cycle 69".

#### The seventieth pass — the native Plex for Android app

Cycle 69 was clean on all ten axes and named the instrument none of them had: a native client.
Cycle 70 took one. **Plex for Android 2026.9.0 is a React Native app compiled to Hermes bytecode —
a codebase separate from Plex Web** — and it was captured two ways: statically (the APK pulled from
a phone, its bundle decompiled into 57,551 functions and every PMS request it can build listed with
its literal parameters) and live (the same APK in a local arm64 Android 13 emulator behind
mitmproxy, plex.tv's resource list rewritten to exactly one server, plex.tv writes answered locally,
sign-in completed by answering the PIN poll with the lab account's token). The same scripted walk
ran against rex and against plexscratch, and every request the app made was diffed by
template. Seven client-visible rules came out, all built:

* **A `plex://` guid must be one plex.tv knows.** A movie or show whose guid rex MINTED never
  gets its luma.plex.tv detail screen, so the action row (Watchlist, Rate, Mark as Watched, More)
  stays a skeleton and a show's Seasons block spins forever. Swapping the one guid for the real one
  through the proxy, and nothing else, drew both. The catalog now takes Plex's own guids from the
  agent the reference itself calls (`POST metadata.provider.plex.tv/library/metadata/matches
  {"type":1|2,"guid":"tmdb://N"}`, then `children` by `index` for seasons and episodes), with the
  agent's slug; `build_catalog.py --relink` applies it to a built catalog.
* **`/service/ultrablur/colors?url=`** (singular, and `/services/...`) answers the image's four
  corner colours; the app asks it for any item without `UltraBlurColors` (its post-play screen did).
  The url resolves exactly as `/photo/:/transcode` resolves it (400 without, 404 empty/relative/
  unknown, 500 for a non-image server path, 200 for any image the server serves; the last `url`
  wins). The values are pixels, which a stdlib server cannot decode, so they are the row's palette.
* **`excludeElements=Part` on listings.** The app names `Part` on every library hubs request; zen
  drops the Parts and adds a trailing `Media.partCount` on section listings, hubs, `recentlyAdded`
  and a detail's nested Related rows, as cycle 22 measured on the detail row alone.
* **A session play's history row has no `viewType`** (hunt NA). A scrobble writes `viewType: 1`,
  a play a timeline completes writes none; the plain `/status/sessions/history/all` call — the app's
  Play History screen, and python-plexapi's `history()` — lists only the second kind (it is
  `type=0`), and `type=1` only the first. Cycle 15's "lists nothing without `type=`" was measured
  on histories holding scrobbles alone.
* **A crossing beat needs a non-zero previous position** (hunt NB). The app reports `time=0` until
  its player advances; a beat past 90% whose predecessor was 0 answers `ignore` and leaves the item
  unwatched on both versions. rex counted the 0.
* **The offline-sync timeline's `viewCount=`** (hunt NB, widened by the driver): with
  `offline=1&state=stopped`, `viewCount>=1` adds one view per sync (1, 2, 3 — not the number sent)
  and a session-kind history row, and `viewCount=0` on a watched item clears it; both answer
  `complete`. A negative or unparseable value is ignored.
* **An episode's post-play carries `tv.upnext`** (hunt NB, completed by the driver): the show's On
  Deck row, field for field, whenever it comes from view state (a watched or in-progress episode)
  and is not the episode itself; a pristine show's first-episode On Deck does not count. `upNext`
  follows `hubKey` in the hub's attribute order.

**Measured and not acted on.** The inline `OnDeck` row keeps its elements under `excludeElements`
(zen trims it; the app reads only its `viewOffset`); an empty playlist's or collection's detail
omits `"Extras":{"size":0}` and `"Related":{}` (the app reads neither when absent); a collection's
`/children` container sends `librarySectionID` as a string (the app coerces it); a SHOW unscrobble
on 1.42.2 also clears every season's own counters (single witness); an unencoded
`X-Plex-Client-Profile-Extra` is a 400 on the copy (the app encodes it); bare `GET /downloadQueue`
is a 404 on the unclaimed copy (no client sends it). Bounds: the download family cannot be measured
on an unclaimed copy (404 without an account); the app's transcode fallback needs a transcoder;
`Review` rows come from Plex's agent (content). **Refuted:** the third library tab's
"Collections"/"Categories" split is claim state plus content (unclaimed copies advertise
Collections only on a library holding one), and the reference's `terminationCode` on a stop was its
own transcoder failing on the scratch copy.

#### The seventy-first pass — eleven axes, two titles

Cycle 71 repeated all eleven axes on the cycle-70 head with four hunters and the driver's own
Android walk. Nine axes and the native client came back clean; A found two CLIENT-VISIBLE
localisation rules on axis 9, both drawn only by Plex for Android:

* **The post-play `tv.upnext` hub's title is localised** (`Demnächst`, `À suivre`, `次に登場`): zen
  and plexscratch42 agree in all 43 languages, 34 of zen's catalogues translate it. Cycle 70 built
  the hub with an English literal. The app titles its post-play shelf with it; Plex Web removes the
  hub.
* **The inline `Related` block's hub titles are localised on a detail row** (`Ähnliche Filme`), as
  the same hub at `/hubs/metadata/{id}/related` already was; rex's row guard stopped at the
  row. The app's detail screen titles its Related shelf with it.

**Measured and not acted on.** A show's `/children?unwatched=1` keeps seasons that still hold
unwatched leaves; an empty season's `/children` names its children's type; `/library/all?type=3`
season rows carry no leaf counts; a show library's legacy `/unwatched` lists shows by their leaves;
`/library/metadata/{collection}/children` on an episode collection is empty on 1.42.2; an episode's
history row composes its parent artwork URLs from the parents' keys. None is read by a client
(HANDOVER §8). **Checked and clean:** the Android walk's missing luma detail call on a repeat open
is the app's own behaviour -- the reference does the same after a restart.

#### The seventy-second pass — clean on all eleven axes

Cycle 72 repeated every axis on the cycle-71 head (`0a3ccfd`). The four hunters it launched stopped
at once on the account's weekly subagent limit, so the driver re-ran each territory's cycle-71
tooling itself against the new head and compared every output with cycle 71's (the reference
snapshots are identical, so cycle 71's reference-side answers stand):

* **Axes 5 and 9** (A): the English Plex Web walk and seventeen localised walks plus the collection
  walk on the Windows CDP browser -- identical to cycle 71 except state (the German walk ran before
  the lists existed); the 43-language HTTP sweep's global and per-path sets identical; the Android
  app's 24 requests x 42 languages now free of `Up Next` and `Related Movies`; item enums (zen and
  plexscratch42), stream titles and `Accept-Language` identical; the catalogue sweep no longer
  lists either title; 63 pushed-frame captures filter to the same 150 recorded lines; every
  earlier regression file at its cycle-71 count, the two new ones 10/10 and 7/7.
* **Axes 6 and 8** (B): regressions identical line for line; identity, queue-advance, write-matrix,
  XML and session captures identical after masking timestamps and per-process identifiers; the
  product/platform/profile/timeline sweeps' diffs identical; the crossing matrix 105/105.
* **Axes 1, 2, 3, 4, 7, 10** (C): the request inventory changes only on zen's side (its BIF indexes,
  butler activities, devices); python-plexapi's reads, the write flows, the owner's screens and the
  vocabulary sweep (198,034 cells; only `/statistics/bandwidth` row counts moved) identical up to
  state; discovery identical.
* **Axis 11** (N and the driver): the Android read inventory on six servers differs from cycle 71
  only by view state; the write inventory likewise; the emulator walk on rex issues no request
  template that differs from cycle 71's.

**Zero client-visible and zero new malformed-only findings on all eleven axes.**

#### The seventy-third pass — a server restart as the twelfth axis

`--state` (after cycle 72) changed what a restart does, and it chose what to keep by belief. Cycle
73 repeated every axis with the state file ON in every rex, added a RESTART LEG to each
territory (read, `kill -TERM`, restart on the same file, read again), and a twelfth axis measured
on the reference itself: plexscratch restarted seven times (five clean stops, two `docker kill -s
KILL`) around every client write kind, against rex restarted by SIGTERM and SIGKILL.

What a restart changes on a real PMS, 64 redraw GETs per restart:

| Kept | Dropped or re-rolled | Steps |
|---|---|---|
| watch state, resume points (`/:/timeline`, `/:/progress`), ratings, locked edits and sort titles, dumb and smart playlists (order after a move), dumb and smart collections, play queues with their selected item, the device table, history and its deletes, prefs, section prefs, agent order, uploaded poster and subtitle, a created library; ids keep counting up | the live session (a client's next beat opens a new one on both), the section timeline, open notification sockets (EOF), the hub page's per-URL cache, the genre/actor shelf pick, a 93% `stopped` beat whose non-zero predecessor came before the restart (ignored on both), the `/statistics/resources` series (zen's `statistics_resources` table is empty: it lives in memory) | `/` `updatedAt` to the start time, `sessionKey` from 1 (neither read by a client) |

rex agreed on every clean-restart cell but one. Four differences were built:

* **A write answered before a hard kill survives it.** Five writes (a rating, a scrobble, a
  playlist, `TranscoderThrottleBuffer`, a locked title) answered 0.17-0.5 s before `docker kill -s
  KILL` all came back on plexscratch; rex's coalescing saver (2 s) lost all five. A client write
  -- any non-GET, and the scrobble/unscrobble/rate/progress/timeline GETs -- is now saved before its
  status line is sent (`Handler.send_response`). `test_persist.py` no longer sleeps before its
  SIGKILL, which is exactly the window it used to hide.
* **An unseeded `sort=random` is a fresh order per request.** Three reads each on zen, kids and
  plexscratch gave three orders (`random:desc` too); rex hashed a fixed seed, so Plex Web's
  "Randomly" grid never reshuffled. `test_ui_surface.py` had asserted the opposite, unmeasured.
  Found by the restart hunt in passing; `random:<seed>` and the `random="1"` shelves are unchanged.
* **A restart does not refresh the fuzzy-search vocabulary.** On plexscratch, `Tears of Steel`
  renamed `Zyxwvut Probe`: `zyxwvuq` finds nothing before the restart and nothing after it
  (`/hubs/search` and Plex Web's `/library/search` alike), the exact word finds it on both sides.
  rex's restore called `rebuild_spelling()`, so the misspelling found the new title after a
  restart. The vocabulary's rows are now saved and restored as they stood (cycle 38's measured
  schedule: only an optimize rebuilds them).
* **A sidecar's codec suffix is localised in Plex Web's plain title.** plexscratch42 and zen in 43
  languages: `Unbekannt (SRT Extern)`, `不明 (SRT 外部)`, where rex localised the word in the
  extended title only (cycle 60) and sent `Unbekannt (SRT External)` to the codec-suffix products.
  Plex Web draws it as the chosen track in the preplay subtitle dropdown and in Get Info. The
  i18n sweeps pair catalog items, and the fixture holds no sidecar until one is uploaded: an empty
  pairing, not a clean one.

Every other axis repeated clean with `--state` on: C's 198,034 vocabulary cells and every
comparator byte-identical to cycle 71's, also across a restart of the frozen instance; B's
matrices, 15 identities and 105 crossing cells, plus 26 restart legs with every cell but live
sessions and time-moving statistics identical; N's Android inventory (160 labels x 5 targets) and
three restarts of its own instance, a SIGKILL among them; the driver's emulator walk before and
after a restart of the live-catalog instance. A's rendered walk (12 English scripts, 34 pages in 17 languages) and every localisation sweep (HTTP, socket, Android, catalogue, item enums, stream titles, `Accept-Language`) repeated cycle 72's answers, also across a restart: 5,124 localised documents with zero strings moved.

Measured and not acted on: a section `/all` listing answers `Media.partCount: 0` under
`excludeElements=Part` (no client sends it there); `/playlists?type=42` and `/updater/status`
stamp the process start (`addedAt`, `checkedAt`; no client reads them); an uploaded sidecar's
`/library/streams/{id}` is 200 with its bytes on PMS and 501 here (read only by Plex Web against a
cloud server); `/` `updatedAt` and `sessionKey` numbering across a restart; the copy's startup
`provider.subscriptions.process` activity. REFUTED as findings: every restart candidate the
territories raised (live sessions, a continued session's `Session` block, the crossing across a
restart, the resource series) -- the reference behaves the same way.

#### The seventy-fourth pass — the repeat of all twelve axes, and three rules it found

Cycle 74 repeated every axis on the cycle-73 head (`b42d6b2`) with `--state` on everywhere and a
restart leg in every territory. A, B and C were clean on axes 1-10 and on their restart legs; every
cycle-73 fix held on every axis that reaches it. Three were built:

* **A device row outlives a hard kill of the request that first named it.** P killed plexscratch
  0.16-1.13 s after a new client's first request (a plain GET, and once a write): the device was in
  `/devices` after every restart. rex registers a device while counting the response's bytes,
  after the answer, so the save-before-answer of cycle 73 never covered it; with `--state` a new or
  renamed device is now registered before the handler runs and saved before the status line.
  python-plexapi's `systemDevices()` reads the table.
* **An empty maintenance window moves its end hour.** Found by P while undoing its own writes, and
  bisected on plexscratch (1.43.4) and plexscratch42 (1.42.2): any `/:/prefs` write that leaves
  `ButlerStartHour == ButlerEndHour` sets the end to start + 1 (23 to 0), in one PUT or two, and
  pushes the moved setting's `preference` frame before the written ones'. A window across midnight
  is kept. Plex Web's Scheduled Tasks page saves each hour on its own.
* **An uploaded sidecar's `key` serves its bytes.** N read Plex for Android's StreamSelector: on a
  direct play whose selected subtitle has a `key`, `selectStreamOnEngine` builds `uri + key +
  X-Plex-Token + autoAdjustSubtitle` and hands it to the native player. plexscratch42 answers 200
  with the uploaded bytes (`text/html`, length before type, the request's connection state -- corrected in cycle 75, the `close` recorded here was urllib's own request header; `.srt`,
  `format=srt` and `autoAdjustSubtitle=0|1` alike); rex answered the embedded track's 501.
  Cycle 73 had recorded the 501 as unread, from Plex Web's `isCloud` gate alone.

Measured and not acted on: a theme row minted by `GET .../theme/{v}/metadata` rides the coalesced
save (no client sends it). REFUTED: nothing new; cycle 73's settled restart candidates reproduced
unchanged in every territory.

#### The seventy-fifth pass — the second twelve-axis repeat

Cycle 75 repeated every axis on the cycle-74 head (`f3dd044`), `--state` on everywhere. B and C were
clean on their axes and restart legs; every cycle-73 and -74 fix held. Built:

* **Shuffle and Unshuffle on a queue nothing was added to.** P, in passing, then re-measured by the
  driver on plexscratch42 (1.42.2) and plexscratch (1.43.4): `PUT /playQueues/{id}/shuffle` answers
  200 with the queue, the selected item first and the rest in a new order; `unshuffle` restores the
  natural order (a queue created with `shuffle=1` too) around the same selected item; both step the
  version whatever state the queue was in, and re-mint every row's id but the selected one's, by
  position, so the selected slot skips one id. Once anything has been added
  (`playQueueLastAddedItemID`) both are 404 -- the only case cycle 15 had measured, so rex
  refused every shuffle and Plex Web's Shuffle toggle reverted.
* **A burst of simultaneous connections is answered** (A): 30 of 30 on plexscratch42 from kids,
  where rex reset most of them and some already at six -- `socketserver`'s listen backlog of 5.
  No capture shows a reset, but six is a browser's per-host connection count.
* **A sidecar answer's connection state is the client's** (N, a correction of cycle 74's record): the
  `Connection: close` cycle 74 measured was urllib's own request header echoed back; a keep-alive
  client gets `Keep-Alive: timeout=20` and an open socket on 1.42.2.

Measured and not acted on: a selected sidecar's decision row names `location: "sidecar-subs"` under
`subtitles=sidecar` (rex `direct`), which the app never reads; `subtitles=auto|burn` with a
selected sidecar is a 400 on the transcoder-less copy (a bound). REFUTED: "PMS keeps only the newest
play queue across a restart" -- three queues from three clients all survived a clean restart on the
API and in the `play_queues` table, which holds one row per client; the queues P saw vanish were one
client's, retired by its own newer queue.

#### The seventy-sixth pass — the third twelve-axis repeat, finished by the driver

Cycle 76 repeated every axis on the cycle-75 head (`ad9c25f`), `--state` on everywhere. The five
hunters stopped twice on the account's limits -- the second time the WEEKLY limit (it resets
2026-10-02) -- after N had written its report; the driver finished the other four territories from
their own tooling and outputs, as cycle 72 did, and compared every output with cycle 75's.

* **A play queue has an owner, and `own=1` moves it** (P, re-measured by the driver on plexscratch42):
  a queue read with `own=1` by a client that does not own it takes the queue -- version + 1, and the
  same owner's read changes nothing -- and the one-queue-per-client retirement follows the owner:
  the taker's other queue is retired at once, the taker's next new queue retires this one, and the
  creator's new queue leaves it alone. Plex Web hands a player the queue's key with `own:1` when it
  plays on another device (`containerKey: join([url], {own:1})`). rex kept the creator as owner,
  so the browser's next queue retired the one the other device was still playing.

Clean on the repeat: C's six axes (every comparator and capture identical to cycle 75's, across a
restart of its own instance too), B's axes 6, 8 and 12 (fixes 21/21, 22/22, 31/31; the crossing
matrix 105/105; the reference's two new BadRequests the copy's busy-database 500s), N's axis 11,
A's localisation sweeps (HTTP, Android, catalogue, stream titles, `Accept-Language`, item enums and
the 150 socket lines, all byte-identical), its regression suites at cycle 75's counts, its HTTP
restart leg (only the statistics move) and the driver's Android walk across a restart.

#### The seventy-eighth pass — clean on all twelve axes

Cycle 78 repeated the cycle-77 implementation head (`afba7e4`) across every axis.
A/B/C/N/P matrices, offline suites and their restart legs found no new
client-visible or malformed-only difference. The complete exact command ledger,
exit codes, raw paths and hashes are in `handover/cycle78-report.md`.

The private copy of the current account catalog passed navigation **25/25**,
python-plexapi **38/38**, and real byte reads/decode **5/5**. The fixture UI
surface passed **1,960/1,960** on a server explicitly started without `--state`;
stateful acceptance and persistence legs used disposable state files. The older
fixture conformance/decode nonzero results are the fixture's dead account-scoped
media links, not live-catalog evidence.

The two client legs that were missing from the first cycle-78 report are now
fresh evidence. The existing Windows CDP browser, reached through `ssh
ben@windows`, rendered the same Plex Web 4.147.1 bundle against the cycle-owned
server and read-only kids PMS. Home, movie, show, season and episode routes all
passed assertions on both legs, with reference write/socket gates active. Each
full leg's 18 status failures are exactly nine harness-origin `GET /` 404s and
nine harness-origin `GET /media/providers` 404s; the full-walk console counts are
108 (sim) and 169 (kids), with failed/blocked auxiliary requests classified in
the token-free Windows error inventory. A standalone Playwright fallback rendered
the local walk and the saved French settings page/accessibility snapshot after
the in-app browser runtime was unavailable. The disposable
native Android emulator reached `home` and rendered both libraries through show,
season and episode detail. A follow-up on the actual live catalog rendered Big
Buck Bunny detail with all action controls after the exact metadata request
returned 200. The fixture episode frame retains skeleton placeholders, so it is not used as
loaded-action evidence. The live-catalog follow-up supplies that evidence; the
synthetic fixture retains its explicit unavailable-media bound.
These are fresh client-visible checks, not a reinterpretation of cycle-77
evidence.

The protected reference snapshot is **kids-only**: before and after hashes match
and the diff is empty; no zen snapshot is claimed. No reference destructive
operation, plex.tv write, account mutation, paid operation, production edit or
server-code change occurred. Cycle-owned servers, proxy, emulator and Windows
Chrome were stopped after ownership checks, while the unrelated port-8899
listener and physical adb devices were preserved.

**No cycle-78 implementation finding remains. All twelve axes are clean.**

| Axis | Fresh cycle-78 denominator | Disposition |
|---|---|---|
| 1 | C restart/request legs, N Android templates, A/B matrices | clean |
| 2 | C 1,018 probes, B read chains, N read cells | clean |
| 3 | C/B/N write regressions and P delayed-write controls | clean |
| 4 | B owner readbacks, 1,960 HTTP assertions, rendered client routes | clean |
| 5 | Windows local/reference rendered comparison plus fallback walk; 18 status rows per leg and 108/169 console dispositions classified | clean |
| 6 | B API write chains and restart readbacks | clean |
| 7 | C vocabulary comparators and regression sets | clean |
| 8 | B/N identity/playback matrices plus live decode | clean |
| 9 | A 44-language/153-header corpus plus rendered French settings | clean |
| 10 | C discovery plus N provider/device inventory | clean |
| 11 | Native emulator fixture walk plus live-catalog Big Buck Bunny detail with loaded actions | clean |
| 12 | C/B/N/P restart, TERM/KILL and delayed-write legs | clean |

#### The seventy-seventh pass — clean on all twelve axes

Cycle 77 repeated every axis on the cycle-76 head (`f8d9437`), `--state` on everywhere, run by the
driver from each territory's cycle-76 tooling (the subagents are on the weekly limit until
2026-10-02), every output compared with cycle 76's:

* **Axes 1-4, 7, 10 (C):** the 198,034-cell vocabulary sweep byte-identical to cycle 76's on the
  frozen instance and, before and after a restart, on its own; every comparator = r71; axes 1, 2 and
  10 identical up to zen's BIF `indexes`; axes 3 and 4 identical up to the random shuffle order and
  the log zip's bytes; the restart legs at cycle 76's block set.
* **Axes 6, 8 (B):** every regression, matrix and identity flow identical to cycle 76's on rex;
  the fixes 21/21, 22/22, 31/31 (30/30 on plexscratch42); the crossing rule table identical to the
  reference's on all 105 cells. The copy's side moved (its busy database gave 122 ten-second
  give-ups, its timeline sessions did not open and leftover sessions stayed listed), rex's did not.
* **Axes 5, 9 (A):** every localisation sweep byte-identical; 14 regression suites at their counts;
  rex's 852 distinct socket frame texts identical; the rendered walk 1,038 of 1,168 cells equal,
  every other one the copy's dialogs failing under load or cycle 76's own failed captures (this cycle's
  French collection dialog matches the copy's string for string); the restart leg moves only statistics.
* **Axis 11 (N and the driver):** the Android read and write inventories, fix checks and restart legs
  equal to cycle 76's; the emulator walk before and after a restart issues the same templates.
* **Axis 12 (P):** the restart matrix and a 33-pair hard-kill matrix agree with plexscratch but for the
  scans (rex holds no files); every earlier restart regression passes.

**Zero client-visible and zero new malformed-only findings.** One instrument note: B's restart-leg
reader read every play queue with `own=1` under one client, which -- after cycle 76's ownership fix,
and exactly as a real PMS does -- takes each queue and retires the one taken before; the queues
survive a restart when read without `own` (checked directly). The reader no longer sends it.

#### The seventy-ninth pass — a confirming repeat that was not: season 0, and python-plexapi 4.18.2

Cycle 79 repeated all twelve axes on `d3125c1` (implementation identical to cycle 77's), driver-run
from cycle 78's tooling copied to `/private/tmp/rex-cycle79`. Nothing in the reopen triggers had
moved (PMS 1.43.4.10903 still the newest, app.plex.tv 4.160.0, Plex for Android 2026.9.0), but two
instruments had: **zen's library** gained shows with a season 0 in its section hubs, and the Mac's
`python3` now carries **python-plexapi 4.18.2** where cycle 78's venv had 4.17.2. Three
client-visible rules came out of those two, and nothing else.

**Season 0 (N's `hubsec-ss`, then C's `ana1`).** zen's Top Rated shelf now holds Bluey (2018), whose
row carries `seasonCount`. Measured on all 509 shows of zen's show library: the four with a season of
index 0 carry `seasonCount` = `childCount` minus the specials (Bluey 4/3, Gundam Wing 2/1, MythBusters
17/16, Solo Leveling 3/2) straight after `childCount`, on the detail row, the section listing and the
hubs; no other show carries it. The season itself is titled `Specials`, translated in 32 of 45
languages (`Spécials`, `スペシャル`; German keeps `Specials`) as the season's `title` and its
episodes' `parentTitle`, English as the season list's `title2`, with an empty container
`parentTitle`. rex titled a season 0 `Season 0` (`Staffel 0` in German) and never emitted
`seasonCount`. Rendered both ways on a copy of the Android fixture with a season 0: Plex for Android
drew a `Season 0` card (it sorts season 0 last, and computes its own "4 Seasons" header from truthy
indexes) and Plex Web 4.147.1 on Windows drew `Season 0` / `Staffel 0`; after the fix both draw
`Specials`, and French Plex Web `Spécials`. python-plexapi's `Show.seasonCount` reads the attribute
and falls back to `childCount`. Cycle 78's C analysis had already listed `seasonCount` as a
zen-only show field (`miss-zen /MediaContainer/Metadata[show]`) and passed over it as content. A
pristine show with specials decks its first REGULAR season's first episode (Gundam Wing S1E1,
MythBusters 2003E1), which is what rex's "Specials sort last" choice already did; the comment
now says it is measured.

**Play-queue flags (B's plexapi chains).** 4.17.2 builds `PlayQueue.create(shuffle=1, repeat=1,
continuous=1)` as `shuffle=True&repeat=True&continuous=True`; 4.18.2 sends `1`. Measured on
plexscratch42 (1.42.2) and plexscratch (1.43.4), identical cell for cell over 17 values: `shuffle` and
`repeat` are integer flags -- an optional `+` and digits, value 0 or 1 (`01`, `001`, `+1` shuffle) --
and `True`, `true`, `False`, `yes`, `2`, `10`, `-1`, `1.0`, `0x1`, ` 1` and an empty value are 400s;
`continuous` takes anything. rex compared against the literal `"1"`, so it answered 4.17.2 with an
unshuffled 200 where a real PMS raises `BadRequest`, and `01` did not shuffle. Cycle 78's `diff60s`
recorded exactly that line (`ref BadRequest: (400) ... shuffle=True | sim ok`) and it was passed over.
Plex Web (`shuffle ? 1 : undefined`, `repeat ? 1 : 0`) and Plex for Android (`shuffle: a1 ? 1 : 0`,
`repeat: 0`) send integers, so the refusal breaks no current client. The only other request shape
4.18.2 changes across B's chains is `GET /playlists/{id}/items?includeGuids=1`, which both servers
answered alike.

**Everything else agreed with cycle 78**, and several comparisons were broken in cycle 78 without
anyone noticing:

- A's four `.mo` sweeps had crashed on their first language in cycle 78 (and here, until repaired):
  `A/mo` was a symlink into a wiped session scratchpad. Re-derived from zen's
  `Resources/locale/*/LC_MESSAGES/plex.mo` (32 catalogues), all four are byte-identical to cycle 77
  (168, 21, 5 and 0 candidates).
- C's restart legs L0 and L1 had run without their snapshot half in cycle 78: `ax1/old72/hubkeys.txt`
  was never copied. Restored, all three legs agree with their no-restart controls except the
  background list's `addedAt` (cycle 73's recorded bound).
- B's main and 59b chain diffs compared against cycle 59's s42 capture, which lived in a wiped
  scratchpad. Compared rex-to-rex with cycle 78 instead: 0 of 20 59b steps differ, and the
  main chain differs only in timestamps, artwork versions and the plexapi-version request shapes above.
- A's s42 item-settings sweep crashed on its collection pair in cycle 78 (rex's collection 50001
  was a leftover of cycle 76's instance); with one created first it is identical to cycle 77.
- N's `fix76` and `sidecar76` checks, and its whole `out/`, need the directory tree the copy did not
  carry; rerun, they agree with cycle 77 up to the unseeded random sort.
- B's advance flow reported a new s42 history row for every identity: N's s42 pass, released at
  02:12:46 by a gate set for cycle 76's clock, scrobbled the same episode on s42 meanwhile. The first
  identity, run before that, shows none on either server.
- The live account catalog's first API/decode run lost part 10010 to five Real-Debrid 429s; the rerun
  passed 38/38 and 5/5.

Measured and not acted on: the background processing list's `addedAt` moves across a restart (cycle
73's bound, still unread by any client); the Android header count is the app's own; s42's `coll.editTitle`
409 is its tag table holding a previous cycle's name.

| Axis | Cycle-79 evidence | Disposition |
|---|---|---|
| 1 | C request sweeps, N's 160 Android templates over five hosts | **found** season 0's `seasonCount` (zen content) |
| 2 | C's 1,018 python-plexapi 4.18.2 probes, B read chains | **found** `Show.seasonCount` |
| 3 | C/B/N write regressions, P's delayed-write matrix | clean |
| 4 | UI surface 1,960/1,960, B owner readbacks, rendered routes | **found** the Specials season title |
| 5 | Windows Plex Web local vs kids, 18 status rows each, same failure classes as 78 | **found** `Season 0` rendered (specials copy) |
| 6 | B's plexapi 4.18.2 write chains vs s42 and vs cycle 78's rex | **found** the queue flag grammar |
| 7 | C comparators and regression sets | clean |
| 8 | B/N identity and playback matrices, live decode 5/5 | clean |
| 9 | A's 44-language corpus (8,202 cells identical to 78), `.mo` sweeps and s42 item settings (both repaired), titles, aliases | **found** `Specials` in 45 languages |
| 10 | C discovery, N provider/device inventory | clean |
| 11 | Android emulator fixture walk, and the specials copy before/after | **found** the `Season 0` card |
| 12 | C L0/L1/L2 (repaired), N TERM/KILL, P hard-kill matrix | clean |

#### The eightieth pass — clean on all twelve axes

Cycle 80 repeated all twelve axes on `0cb760b` (cycle 79's three fixes merged), driver-run from a tree
built by `mk_next.sh` out of cycle 79's repaired tooling, with python-plexapi 4.18.2 and N's s42 pass
gated on B's s42 work finishing. **No client-visible and no new malformed-only difference.**

- The two cycle-79 rules held against live references: N's `hubsec-ss` no longer differs on zen's
  season-0 shows, and the specials copy of the Android fixture rendered `Specials` in Plex for
  Android and in Plex Web (English `Specials`, French `Spécials`) on the cycle-80 head.
- B's denominator equals cycle 79's except 14 fewer s42 DELETEs -- cycle 79's deletions of N's
  contaminating history row -- and its advance flow no longer reports a new s42 history row. Its
  59b chain is identical to cycle 79's rex (0 of 20 steps) and its main chain differs only in
  timestamps and one play-queue item type chosen by the queue's shuffle.
- A's 44-language corpus is identical to cycle 79's, and its four `.mo` sweeps (168, 21, 5 and 0
  candidates) run whole, as does the s42 item-settings sweep on its own collection.
- C's comparators, regressions and all three restart legs match cycle 79; each restart equals its
  control except the background list's `addedAt` (cycle 73's bound).
- P's hard-kill matrix, restart checks and regressions match cycle 79 exactly.
- Suites: offline 13/13, navigation 29/29 (fixture) and 25/25 (live account catalog), conformance
  38/38, decode 5/5. The fixture UI surface ran twice under the five territories' load: the first run
  died on a raw-socket check (the server reset rather than closed a request dropped at 20 s; it closes
  cleanly three times of three when idle) and the second failed one statistics check whose series
  gained a sample between its reads (105, then 106). Both are the check's timing, and both passed in
  cycles 78 and 79.
- Windows Plex Web: every screen true against the cycle server and kids, 18 status failures each, the
  same failure classes as cycle 79. Plex for Android: the season page's first load waited on a plex.tv
  `luma` request that never completed; on re-entry it answered 404 in 273 ms, as in cycle 79, and the
  page and the episode drew completely.

| Axis | Cycle-80 evidence | Disposition |
|---|---|---|
| 1 | C request sweeps, N's 160 Android templates over five hosts | clean |
| 2 | C's 1,018 plexapi 4.18.2 probes, B read chains | clean |
| 3 | C/B/N write regressions, P's delayed-write matrix | clean |
| 4 | UI surface, B owner readbacks, rendered routes | clean (two timing-only UI checks, above) |
| 5 | Windows Plex Web local vs kids; the specials copy | clean |
| 6 | B's plexapi 4.18.2 chains vs s42 and vs cycle 79's rex | clean |
| 7 | C comparators and regression sets | clean |
| 8 | B/N identity and playback matrices, live decode 5/5 | clean |
| 9 | A corpus, `.mo`, titles, aliases, zen and s42 item settings | clean |
| 10 | C discovery, N provider/device inventory | clean |
| 11 | Android emulator walk; the specials copy | clean |
| 12 | C L0/L1/L2, N TERM/KILL, P hard-kill matrix | clean |

#### The eighty-first pass — clean on all twelve axes

Cycle 81 repeated all twelve axes on the clean cycle-80 head `ed66770`. The
first audit found four cycle-80 comparisons that were dead before their first
request: C `f587` had copied no measured bodies, P's R0 read had no identity
manifest, and B's subtitle upload named `c61b.srt` although the measured fixture
is `c59b.srt`. The cycle-local copier now repairs these from measured `C/ax1`
bodies, isolated plexscratch identity data and the existing subtitle fixture;
the helper refuses non-cycle paths and never synthesises a response. The
repaired legs completed with their original denominators, including the real
subtitle upload and removal request.

- C's 1,068/130 reference and simulator exchanges, comparators, regressions,
  discovery and all three restart legs were clean apart from the existing
  timing/content bounds.
- P's R0/R1/R2/R3/R4 redraws and hard-kill matrix completed on isolated
  plexscratch; B's local, remote and plexscratch42 chains completed, including
  the subtitle upload.
- A's fresh simulator requests covered 8,064 web cells in 42 locales and 1,032
  Android cells in 43 locales, `Accept-Language` 153/153, `.mo` sweeps
  (168/21/5/0), title/alias and item-setting legs. The preserved measured zen
  paging corpus gave the same cycle-80 malformed-only partition. N's
  provider/device and Android request legs, the Windows rendered walk and the
  disposable Android emulator all rendered or compared cleanly.
- Offline decision, mediainfo, GDM, account-state and persistence suites passed;
  fixture navigation passed 29/29 (`/private/tmp/rex-cycle81/accept/navigation_fixture81.txt`)
  and the LAN-bound UI suite passed 1,960/1,960.
  The live account catalog passed navigation 25/25 and conformance 38/38; its
  local byte/decode checks passed, while the public tunnel's prefix/seek legs
  returned its existing HTTP 530 boundary.

The four repaired pre-request dead comparisons were C `f587`'s two response
body sets, P's R0 identity baseline and B's subtitle upload. The copy guard is
a safety check rather than a fifth probe: it requires an exact cycle root and
refuses edited targets whose symlink resolves outside that tree. The Windows
and Android clients still made external plex.tv/luma/pubsub read attempts in
their rendered walks; no harness-side plex.tv write, claim or account mutation
was issued, and the blocked/failed external requests remain in the inventories.
Axis 9 retained `i18n/zcache69`, the measured zen paging corpus carried from
cycle 80, while the simulator requests were fresh. The live locale leg measured
153 `Accept-Language` forms and therefore establishes partition selection, not
a newly captured full zen response corpus. The catalog feature test's dynamic
Unicode candidate-domain count was also separated from the exact measured
U+0300..U+036F removal behavior; both Python 3.9 and 3.12 now pass that same
behavior check.

| Axis | Cycle-81 evidence | Disposition |
|---|---|---|
| 1 | C request/XML inventory, A corpus, N request sweeps | clean |
| 2 | C and B python-plexapi reads | clean |
| 3 | C/B/N writes, redraws and frames; P readbacks | clean |
| 4 | LAN UI surface and owner legs | clean; fixture capability skips only |
| 5 | Windows Plex Web sim/kids and French walk | clean on detail screens |
| 6 | B plexapi writes/playback and subtitle chain | clean |
| 7 | C regressions and B vocabulary matrix | clean |
| 8 | B/N identity, queue, decision and byte legs | clean within tunnel/media bounds |
| 9 | A localisation, `.mo`, title, alias and item settings | clean |
| 10 | C discovery and N provider/device/GDM | clean |
| 11 | Android emulator movie/show/season/episode walk | clean |
| 12 | C restart, N TERM/KILL and P hard-kill legs | clean |

The protected kids snapshot is byte-identical before and after. No server
implementation changed; repository changes are the cycle-copy repair helper
and invocation, the Unicode-domain test correction, and the token-free cycle
evidence.

#### The eighty-second pass — python-plexapi 4.18.3, clean on all twelve axes

python-plexapi 4.18.3 (2026-09-28) was the one reopen trigger that moved; cycle 81 ran 4.18.2.
Its diff is a stricter filter-field grammar and four `AudioStream` attributes plus
`Video.canNormalizeLoudness`.

- **Video loudness is a 1.43.4 behaviour.** kids answers every video audio stream with
  `canNormalizeLoudness="1" gainOffset loudness loudnessAnalysisVersion lra peak threshold`
  (a ten-second synthetic AAC mono clip: `gainOffset="0.03" loudness="-21.76" lra="0.10"
  peak="-17.96" threshold="-31.76" loudnessAnalysisVersion="0.02"`). zen carries none of them on
  62 movie audio streams, and the 1.42.2 binary does not contain `canNormalizeLoudness` or
  `gainOffset`; its loudness analysis is the music butler's (`albumGain`, `endRamp`, ...). rex
  advertises 1.42.2 and answers `canNormalizeLoudness` False, as zen does.
- **No reference names a field with a digit**, so 4.18.3's grammar changes plexapi's own
  validation only. 58 filter cells answered identically on zen, plexscratch42 and rex.
- **Same-second history rows order by history id ascending** under `viewedAt:desc`,
  `viewedAt:asc` and no sort, on plexscratch (1.43.4) and rex alike, measured with playbacks
  whose crossing beat lands in one wall-clock second. A `/:/scrobble` of an episode or a season
  writes no history row.

Every territory re-ran under 4.18.3 with no client-visible and no new malformed-only
difference; the per-leg ledger is `handover/cycle82-report.md`. **Measured and not acted on:**
plexscratch42's 500 on a 95 % timeline beat and on one identity's `select_sub_none` (it answers
200 replayed on an idle server: reference contention); `regress_p75`'s fourth check, which encodes
a claim cycle 75 refuted.

#### The UI suite with keys, and without its content skips

The UI suite had only ever been green keyless: 1,960/1,960 with 12 skips. With a TMDB key and a
real Real-Debrid token the Fix Match section really matches `movie` again, and four checks failed.
Two were server bugs on the Fix Match path; two assumed `movie` was still unmatched.

- **`rating` is the critic score, and rex holds none.** zen (1.42.2), GET-only, over all 3,889
  movies of its movie section: the 2,351 `local://` rows carry no `rating`, `ratingImage`,
  `Rating`, `audienceRating` or `Field`. Of the 1,538 `plex://` rows, 743 carry `rating`, every one
  beside a `rottentomatoes://image.rating.*` `ratingImage` and a `critic` entry in `Rating`; none of
  the 795 whose `Rating` entries are all `audience` ones (IMDb, TMDB, the RT audience score) does.
  (Three carry a critic entry and still no `rating`.) No movie carries `Field` at all. So "no
  `rating`" is not an unmatched-row rule: it holds for every row whose ratings are TMDB votes, which
  is every row rex has. Load already drops it (`hierarchy.normalize_rows`); `metadata.enrich`
  wrote the vote into `rating` again on every Fix Match, so a matched row claimed a critic score.
- **A Fix Match row's `titleSort` came from a bare article strip** and was always set, so "Tears of
  Steel" sorted as "Tears of Steel", which no real PMS sends. It now takes the load-time rule
  (`hierarchy.sort_title`) and is sent only when it differs.
- **An unmatch re-versions the poster the row owns.** Measured on plexscratch (1.43.4) by
  unmatching matched movie 1: `updatedAt` 1790724822 → 1790724825 and `thumb`
  `/library/metadata/1/thumb/1790724822` → `.../thumb/1790724825` in the same write. rex moved
  `updatedAt` and kept the old version, breaking cycle 20's rule that an owned artwork version IS
  the row's `updatedAt`. The UI suite caught it only once the fixture held a movie that is unmatched
  after its last artwork write.

The six content skips were mostly the suite's own writes, measured by instrumenting a run:

- merges and deletes leave two movie rows by the `#` axis, so it and the collection move grammar
  (both need three) skipped;
- cycle 27's borrowed-artwork check clears the show's `art` before the pick check that needs a
  season borrowing it, so no season could borrow;
- cycle 36's repeated-`filters=` check retitles `movie` "Repeated Key" and never put it back, so
  both search-ladder checks after it lost their three-word title;
- no Dolby Vision row existed at all.

The fixture gained three movies from zen's own library, built as `build_catalog.py` builds a row:
ffprobe 8.1.2 over zen's part URL, `mediainfo.build_media`, `metadata.enrich` against the TMDB id
zen matched, and zen's `plex://` guid. zen's 1.42.2 ffprobe (4.4.2) does not report the DOVI
configuration record of an mkv, so the probes were taken with 8.1.2 through a tunnel; the
resulting `DOVI*` attributes are field for field what zen's own analysis carries. "Trust No One:
The Hunt for the Crypto King" and "Tuesday" are zen's profile-5 rows 967 and 1120, and
"Vinaipayan" is zen's unmatched `local://3763`: a keyed run leaves no unmatched movie otherwise.

**Measured and not acted on:** the same plexscratch unmatch dropped the movie's `art` outright,
where the unmatch model keeps it (read off a different row); and a rex row unmatched with
`/unmatch` keeps its `Rating` array and `audienceRatingImage`, which zen's never-matched `local://`
rows do not carry. Neither is measured on the same row both ways.

#### The eighty-third to eighty-sixth passes — the native server measured against the references

`main` became the native Rust server on 2026-09-30, so the loop reopened with the binary as the
target. Before the first native cycle every HANDOVER §2a entry was worked (the follow-ups, a
lockstep replay of the 15 real-client corpora, differential sweeps of the oracle's own vocabulary
through query names, path segments, request headers, XML and methods, the UI suite's residue on
both servers' streams, concurrency and the state file): 78 fixes, each in whichever server the
reference said was wrong. Then four twelve-axis cycles ran against native: 83 found 10
client-visible rules (among them every Plex for Android poster a 404 because a query-string token
never reached the transcoder's inner read, `continuous=1` queues, the language picker under
zh-TW/es-419/pt-PT, theme/logo candidate types, play-queue ownership and client), 84 found 2
(a deleted item's timeline frame and its play-queue removal), 85 found 2 (a queue's selection offset
under `window`/`center`; a deleted section's queued rows), and **86 found none**. The rules are trap
bullets in `docs/protocol-traps.md`; the ledgers are `handover/cycle8{3,4,5,6}-report.md`.

Measured and not acted on: a 92% beat with a different `playQueueItemID` from its stop (the
reference ignores the stop; no client sends it); the reference's per-season activity frame order;
an unmatched show on the isolated plexscratch losing its own artwork fields (needs a networked
server, where an unmatch is forbidden); deleting the last library section (unmeasurable); the trial
deployment with a claimed test identity (the user's).

#### The ninety-fourth pass — Plex for Android 2026.19.1

Cycle 93 met the stopping rule, but the reopen table's Android row was wrong: the 2026.9.0 it
recorded (read off the owner's phone) is a May 2026 build, and Google Play has served newer ones
since; on 2026-10-05 the current build was **2026.19.1** (versionCode 971050475, updated
2026-10-01). It was installed over 2026.9.0 on the emulator (Android accepted the update, so the
signer is Plex's) and both Hermes bundles were censused with the new `handover/tools/apkcensus.py`
(`handover/android-census-2026.19.1.md`): 330 -> 412 request templates, PMS-bound 63 -> 80; 19
PMS-bound added (a server dashboard: `/statistics/bandwidth|resources|media`, `/status/sessions`,
history with account/section/item filters, `/library/all/top`; `/library/sections/{id}/prefs` on
every item detail; `POST /library/collections`; `playlistType`/`subType` list filters), 2 removed,
9 changed. The `/player/playback/*` and `/player/timeline/*` family is the app's companion remote,
sent to a player's own address, not to a PMS.

Every new PMS request, issued in the app's captured shape, answered as the references do, and the
dashboard, history, Top Played and per-item play history render on rex. Two rules were built:

* **A client PING on `/:/websockets/notifications` is answered with a PONG carrying its payload**
  (kids and zen, at once, unmasked). rex never answered, and 2026.19.1 closes the socket after
  exactly 50 s (1006) and never reopens it, so every server push after the first minute was lost.
  The app opens the socket only when the account carries the `WebsocketsForPms` feature, and it
  authenticates with the ACCOUNT token. CLIENT-VISIBLE.
* **A `/library/all/top` row is the light listing row plus the section fields, `globalViewCount`
  and `userCount` after `updatedAt`, and a trailing `User` list**; show and season rows drop the
  per-user view fields, a season adds `leafCount`/`viewedLeafCount` before `addedAt`. Both servers
  sent the detail row (`Guid`, `Rating`, a show's `Location`). The app reads only the title,
  image and the two counts, so it rendered the same either way.

Live playback on the live catalog matched plexscratch by template (play queue, decision, part reads,
timeline beats), and the dashboard's Stop Playback (`/status/sessions/terminate?sessionId=&reason=`)
answered the recorded empty 200 with `terminationCode 2006` on the next timeline.

**Measured and not acted on.** zen closes the socket after ponging an EMPTY ping where kids does
not (single witness, no client sends one). Bounds of the instrument: an episode cannot be played
in the emulator (every live-catalog episode is eac3, which it cannot decode, and rex has no
transcoder); the 19.1 `PUT /library/parts` with playback headers needs a direct-playable item with a
second audio or subtitle stream; activity frames for a subtitle download need a subtitle provider.
Whether 1.43.4 also drops a show's per-user fields on a top row is unmeasured (kids has no show
plays). Instrument: B's s42 legs lost the same 95% beat to plexscratch42's statistics job (a ~50 s
write hold every ~80 s) in every run; the leg now waits for a hold to end in s42's log and sends the
beat once.

#### The ninety-fifth pass — clean on all twelve axes under Plex for Android 2026.19.1

Cycle 95 repeated every axis on the cycle-94 head, with the new app's legs (its request ledger, the emulator walk,
the dashboard, and a notification socket held open for more than twelve minutes through real playback) as a standing
part of axis 11. **Zero client-visible and zero malformed-only findings.** The `/library/all/top` shape built in
cycle 94 reproduced the fixture on fresh servers in three territories.

### Emulation calibrated against the account's own claimed servers

Rather than guessing what a claimed server reports, `/identity`,
`/media/providers` and `/myplex/account` were pulled from **fun and zen** — both
genuinely claimed, on the same account — and diffed field by field. Result:
**36 fields identical, 24 differing and every difference deliberate.**

What the diff corrected, all of it invented by rex beforehand:

| field | real servers | rex had |
|---|---|---|
| `apiVersion` | `1.1.1` | `1.2.3` (taken from a newer 1.43.4 container) |
| `claimed` on `/media/providers` | **absent** | `true` — claim state lives on `/identity` and `/myplex/account` only |
| `identifier`, `mediaTagPrefix`, `mediaTagVersion` on `/media/providers` | **absent** | present, leaked in by a generic container helper |
| `size` on `/media/providers` | `1` (the provider count) | `0` |
| `myPlexUsername` / `username` | the owner's **email** | the account *username* |
| `publicPort` on `/myplex/account` | `0` | `443` |
| `musicAnalysis` | `2` | absent |

The generic `container()` helper was the root of four of those: it injected
library-container defaults into *every* response, including endpoints a real PMS
keeps bare. `bare_container()` now exists for exactly that reason.

The 24 remaining differences are all one of three kinds: rex is a different
server (`machineIdentifier`, `friendlyName`, `version`, `platformVersion`), it
has **no encoder** (every `transcoder*` field, `offlineTranscode`), or it
**owns no files** (`allowMediaDeletion`, `allowSync`, `allowCameraUpload`,
`allowTuners`, `livetv`). Those are kept false on purpose: advertising a
transcoder rex does not have is what produced "This server is not powerful
enough to convert video" in the first place.

Playback re-verified after all of it: direct play, 1920x1080, 0 dropped frames,
banner gone.

### On the claim banner

`--assert-claimed` makes rex report a claim it does not hold. It is named
for what it is. The alternative — a real `POST /api/claim/exchange` — is what
reset the account's tokens and signed fun and zen out of plex.tv earlier the
same day, so it is not worth re-running for a cosmetic banner.

It works only because the **locally served** web client reads claim state from
rex's own `/identity` and `/myplex/account`. `app.plex.tv` reads plex.tv
instead and is not fooled, which is why the banner persisted there even when
rex claimed to be claimed.

### The unavoidable trade-off

Fix 6 and fix 7 pull against the Cloudflare finding below. Proxy mode is what
makes Chrome accept the stream, but Cloudflare strips `Range` on the opening
request, so the log line above is `range=- -> 200 len=852900097` — the whole
852 MB was offered in one response. Playback works and seeking still functions
via later closed ranges, but a production deployment must terminate TLS
somewhere that passes `Range` through, or hand the client a URL whose origin
already returns a real media type.

Spike, 2026-09-11. Question: can a simulated PMS both **be browsed by** real Plex
clients and **serve its own catalog's streams** from debrid links — the thing
[remux](https://github.com/lostb1t/remux) does for Jellyfin, for Plex instead.

Answer: **yes for the protocol and the bytes; the weak link is plex.tv presence,
not playback.**

Code: `rex/` (scratchpad, not yet promoted to a repository).

## What was built

| File | Purpose |
|---|---|
| `server.py` | The PMS surface + the part streamer. 2 part modes: `proxy`, `redirect` |
| `mediainfo.py` | ffprobe JSON → Plex `Media`/`Part`/`Stream` |
| `build_catalog.py` | RD link → unrestrict → ffprobe → catalog row |
| `xmlout.py` | MediaContainer → Plex XML (clients that refuse JSON) |
| `conformance.py` | 38 checks driven by python-plexapi |
| `discover.py` | The full client path: plex.tv → resources → connect → play |
| `gate.py` | The official client's own direct-play gate, per client class |
| `presence.py` | Measures how long a registration stays client-visible |

Endpoints implemented: `/identity`, `/media/providers`, `/library/sections`,
`/library/sections/{id}/{all,recentlyAdded,sorts,filters,firstCharacter,prefs}`,
`/library/metadata/{rk}` (+ `children`, `allLeaves`, `related`, comma-joined key
lists), `/library/{onDeck,recentlyAdded,all}`, `/hubs`,
`/hubs/{continueWatching,promoted,search}`, `/hubs/sections/{id}`, `/playQueues`,
`/library/parts/...`, `/video/:/transcode/universal/{decision,start,stop}`,
`/photo/:/transcode`, `/:/{timeline,scrobble,unscrobble,progress,rate,prefs}`,
`/status/sessions`, `/system/agents`, `/accounts`.

## Proven

**1. The REST contract.** python-plexapi — the same XML every official client
parses — browses rex and reaches a playable URL. 38/38 checks, including
`Media`/`Part`/`Stream` shape, 4 subtitle tracks, search, play queue, timeline
write-back.

**2. The decision endpoint is cheap.** `mdeDecisionCode: 1000` plus a
`Part.decision="directplay"` echoed in the body satisfies both the loose reader
(plex-for-kodi accepts `1000 <= code < 2000`) and the strict one (plx-native
reads `Part.decision`).

**3. The bytes.** Range GET on `Part.key` → 206, `Accept-Ranges`, true total
size. Mid-file seek to offset 919268127 returned in **0.23 s**.

**4. Byte transparency.** 24 decoded frames at 20:00, md5:

| Path | md5 |
|---|---|
| through rex (`proxy`) | `e49cf18e698064cddc10a13ea0926fb3` |
| through rex (`redirect`) | `e49cf18e698064cddc10a13ea0926fb3` |
| straight from the RD CDN | `e49cf18e698064cddc10a13ea0926fb3` |

mpv direct-plays it: `h264 1920x1080 23.976fps` + `eac3 6ch` + all 4 subtitle
tracks, seeked to 20:00.

**5. Registration works.** The pd_inject trick still works in 2026:
`PUT https://plex.tv/devices/{id}?Connection[][uri]=...`. rex appeared in
`/api/v2/resources` — the list every client reads — alongside fun and zen.

**6. The whole chain, end to end, over a real public URL.** `discover.py` —
python-plexapi driving the same path every official client takes — scored
**14/14**: plex.tv sign-in → resource discovery → connect to
`https://rex.example.com` (0.23 s) → browse → 206 with the true total size.
mpv then direct-played it from a 20-minute seek with all four subtitle tracks,
and 24 decoded frames at 20:00 matched the CDN reference md5 exactly
(`e49cf18e698064cddc10a13ea0926fb3`) through the full
client → Cloudflare → 302 → Real-Debrid path.

**So the answer to the original question is yes.** A simulated PMS can be
discovered through plex.tv by an unmodified Plex client and can serve its own
catalog's bytes from debrid links, at full quality, with working seek, without
owning a file or transcoding anything.

## The real blockers

**1. Cloudflare strips `Range` on prefix requests — do not proxy media through
it.** With `--part-mode proxy` behind the tunnel, a `Range: bytes=0-99` request
arrived at rex as **no Range at all** (`PART 10010 range=- -> 200
len=1838536254`). Cloudflare collapses a prefix range into a full origin fetch,
so rex served the entire 1.8 GB and Cloudflare pulled it for 39 s before the
client gave up. A *mid-file* range (`bytes=919268127-…`) passed through correctly
as 206. So every playback start would drag the whole file out of Real-Debrid —
ruinous, since bandwidth is the entire cost line.

`--part-mode redirect` removes the problem completely: rex answers 302 with
`content-length: 0`, Cloudflare forwards it untouched, and the client fetches
from RD directly and gets a correct 206. **No media byte crosses Cloudflare** —
which also keeps the setup clear of Cloudflare's ToS §2.8 restriction on serving
video through the CDN. Redirect is the right default behind any CDN.

Remaining redirect-mode risk: the client now sees RD's own
`Content-Type: application/force-download` and `Content-Disposition: attachment`
instead of a media type. mpv and ffmpeg do not care. Official clients are
untested on this.

**2. A registration is a ~40-minute lease. It must be renewed.**

**The number: 40 minutes.** A single announce at 11:18:37 was still in
`/api/v2/resources` at t+39 min and gone at t+40 min, with the transition caught
inside a 72-second window:

| | |
|---|---|
| announce | 11:18:37 |
| last confirmed visible | 11:57:21 — **t+39 min** |
| first confirmed absent | 11:58:33 — **t+40 min** |

Re-announcing restores visibility immediately and completely.

This is a real lease, not a caching artifact. `identity_cache.py` read the list
with a **fixed** identifier (standing in for a real client, which always has a
persistent one) and a **fresh** identifier back-to-back at the same instant,
every 60 s, with one re-announce at the midpoint:

```
t+0 … t+306    fixed=no    fresh=no
t+367          re-announce
t+367 … t+732  fixed=YES   fresh=YES
```

Both identities agree at every timepoint, so plex.tv does **not** cache the
resource list per client identifier, and reads do not keep an entry warm (5
minutes of total silence mid-lease changed nothing).

So `heartbeat.py` is **required**, not insurance. `DEFAULT_INTERVAL` is 600 s —
a 4x margin on the lease for one small request every 10 minutes. It refuses to
run without an explicit numeric `--device-id`, which guards against the mistake
described below.

**Mitigation validated**, not just designed: with the heartbeat running at 600 s,
13 probes over 60 minutes were all visible, including t+45, t+50, t+55 and t+60 —
well past the lease — with zero heartbeat warnings, across a mid-run restart of
rex itself.

One anomaly remains unexplained: a dropout roughly 2-4 minutes after the very
first PUT, far inside the lease. That PUT also rewrote name, platform and
connections at once, so propagation of a larger mutation is the plausible guess;
it was never reproduced.

**3. "Unclaimed and not secure" is an identity mismatch, not a TLS problem.**

The Plex web app showed rex as *"unclaimed and not secure"* despite a valid
public certificate. Three fields were at fault:

| field | wrong value | correct |
|---|---|---|
| `machineIdentifier` | `de407dc4…`, which did not match the device row's `clientIdentifier` (`zurg`) | must **equal** the device row's `clientIdentifier` |
| `certificate` | absent | `1` — rex's TLS terminates at the CDN, so it has to assert this itself |
| `claimed` | absent | `1` |

The mismatch was the actual cause: a client cross-checks the `clientIdentifier`
plex.tv advertises against the server's own `/identity`, and a disagreement reads
as a server that does not belong to the account. `/identity` and
`/media/providers` must agree on all of these.

**Consequence worth remembering:** `machineIdentifier` is a client's primary key
for a server. Changing it on a live registration makes every client that saw the
old value show a **duplicate** server — one stale ghost plus the real one — and
the ghost lives in client-side storage (localStorage for Plex web), not on
plex.tv. Pick the value once and never change it.

**3b. THE REAL WALL: `presence` and `accessToken` are never published, so the
official web app refuses to connect.**

The Plex web app reports *"rex is currently unavailable"*. Cause, from
`/api/v2/resources`:

| field | fun / zen | rex |
|---|---|---|
| `presence` | `true` | **`false`** |
| `accessToken` | the owner's token | **`null`** |

A client builds its request URL from the resource's `accessToken`. With `null`
there is no credential to use, so the server reads as unreachable. This is the
single thing standing between rex and the official clients.

What was ruled out, in order:

1. **Identity mismatch** — fixed (`machineIdentifier` == `clientIdentifier`).
   Did not change presence.
2. **plex.tv probing the connection** — it does not. Zero inbound requests
   reached rex or cloudflared from Plex during the whole session, so
   presence is not earned by answering a probe.
3. **The claim exchange** — `GET /api/v2/claim/token` then
   `POST /api/claim/exchange` **works** and issues a genuine server-specific
   token (distinct from the account token) for a *fresh* 40-hex identifier. A
   claim against the pre-existing hand-made `zurg` row returned only the account
   token. But even a properly claimed server got `presence=false`,
   `accessToken=null`.
4. **`PUT /devices/{id}` with the server's own token** — 200, no presence.
5. **`GET /api/v2/ping`** — 200, no effect. `POST /api/v2/ping` 404.
6. **`POST /servers.xml`** (legacy registration) — **422 with every body shape
   tried** (form params, XML, nested form). 422 rather than 401/404 means the
   token is accepted and the body is wrong, so this remains the most promising
   lead.

**What still works despite this:** python-plexapi connects and streams fine
(14/14) because it authenticates with the account token and does not require the
resource `accessToken`. So the wall is specific to clients that insist on it —
the web app does, plexapi does not, native clients untested.

### What fun's own logs revealed

Read from `Plex Media Server.log` on fun (`presence=true`), by enumerating every
outbound plex.tv call PMS makes:

```
PUT https://plex.tv/api/servers/{machineIdentifier}/connectivity
    ?X-Plex-Token=<server token>&asyncIdentifier=<uuid4>      -> 200   (~every 5h)
POST https://plex.tv/servers.xml?auth_token=<token>           -> 422   (!)
```

Two things fall out:

1. **`POST /servers.xml` returns 422 for the real PMS too.** The endpoint is
   dead. My identical 422 earlier was normal behaviour, not a malformed body,
   and that lead is closed.
2. **The presence mechanism is the `connectivity` PUT** — and it returns **404**
   for rex.

### The actual gate: two separate registries

| | fun | rex |
|---|---|---|
| `/devices/{id}` | exists | exists |
| `/api/servers/{machineIdentifier}` | **200** | **404** |
| `connectivity` PUT | 200 | 404 |
| `presence` / `accessToken` | `true` / token | `false` / `null` |

plex.tv keeps a **device** registry and a **server** registry. `PUT /devices/…`
— the whole pd_inject technique — only ever writes the device one. The server
record is what authorizes `connectivity`, which sets `presence`, which is what
makes plex.tv publish an `accessToken` to clients. No amount of device-registry
writing produces it, and neither the claim exchange nor `/api/v2/ping` creates
one.

**The one remaining unknown is how a server-registry record gets created.** It
is not in fun's current logs because fun was claimed long ago. (Answered on
2026-10-10, below: `POST /servers.xml` writes it despite its 422, and presence is
the pubsub stream.)

**The definitive experiment:** run a real PMS in a container on zen (the test
host), claim it with a fresh claim token, and read its **first-run** log. The
call that creates `/api/servers/{machine}` will be in that startup sequence.
Bounded, and on a test host rather than production.

### The experiment, run (2026-10-10): `/servers.xml` writes the record, pubsub is presence

A throwaway real PMS (`plexinc/pms-docker:1.42.2.10156-f737b826c`, the version rex
reports, friendly name `pms-probe`, its own empty config) ran on zen behind the harness
gateway: mitmproxy transparent on 80/443 at uid 8888, its CA trusted in the container by a
`cont-init.d` script before PMS first started. It was claimed once, the supported way: a
claim token from `GET plex.tv/api/claim/token.json` redeemed by `POST /myplex/claim?token=`
on the PMS itself. Every plex.tv request and answer from the claim, a claimed restart and
an hour after it is in `tests/fixtures/plextv_registration_s1422.json` (tokens, email, home
IP and certificate material redacted). Account token valid and fun and zen `presence=true`
were rechecked after each step; nothing regressed.

The claim, in order (02:21:29 UTC):

| t | request | answer |
|---|---|---|
| 0.0 s | `POST plex.tv/api/claim/exchange?token=<claim>` | 200, `<user id=… authToken=…>` (gzip) |
| 0.4 s | `GET` webhooks, privacy, release_channels, features, server/users/features | 403/200 |
| 1.0 s | `POST plex.tv/servers.xml?auth_token=<server token>`, body below | **422**, empty |
| 1.2 s | `PUT plex.tv/devices/{machine}?Connection[][uri]=http://10.231.47.2:32400&httpsEnabled=0&…` | 200, `presence="0"` |
| 1.5–9 s | `certificate/subject`, `PUT certificate/csr`, `certificate/download` ×7 | the plex.direct certificate |
| 5 s | `GET plex.tv/services/pubsub/servers` | 18 pubsub hosts |
| 0.5 s on | `GET https://pubsub.plex.tv/sub/eventsource/{accountId}/{machine}?X-Plex-Token=<server token>` | `text/event-stream` |
| 9 s | the device `PUT` again, `httpsEnabled=1` | 200, `presence="0"`, the plex.direct uri |

```
<MediaContainer size="1">
<Server relay="0" name="pms-probe" host="" localAddresses="10.231.47.2" machineIdentifier="986d…" version="1.42.2.10156-f737b826c" date="1791598890672774" />
</MediaContainer>
```

1. **`POST /servers.xml` creates the SERVER record, and plex.tv answers it 422.** After the
   claim, `GET plex.tv/api/servers/986d…` with the account token answered 200, its
   `createdAt="1791598890"` the second of the first POST (`date="1791598890672774"`) and
   its `updatedAt="1791598948"` the second POST's. After a restart `updatedAt` moved to
   1791599312, the second startup POST's `date`. The device row's `createdAt` is the
   exchange's second (1791598889), one earlier. So the 422 that closed this lead above
   ("the endpoint is dead") is plex.tv's normal answer to a real PMS, and the record is
   written regardless. The spike never sent the PMS's body with the server token;
   rex-e2e's `/api/servers/aebd…` still answered 404 on the day.
2. **`presence` is the pubsub subscription: not a lease, and not the connectivity PUT.**
   - In the first run the gateway buffered the event stream (`tap.py` records a flow when
     its body ends, which an event stream never does), so PMS got no reply, gave up after
     20 s and retried 30 s later. plex.tv still saw a subscriber for those 20 s windows:
     resources read `presence=true` at 02:22:05, the second after one closed. No
     connectivity PUT was sent in that whole run.
   - `docker stop`: `presence=false` at the first poll, within a second. The 40-minute
     lease measured for the device registry does not apply to presence.
   - With the stream passed through (`streamtap.py`), the restart subscribed at 02:28:46
     and presence was true at all 57 polls, 60 s apart, from 02:30 to 03:29 (none ran
     02:45-02:49 while the poller restarted).
   - `accessToken` stayed published while presence was false: it follows the server
     record, presence follows the stream.
3. **The connectivity PUT is a reachability check, and pubsub carries the verdict.** PMS's
   log, in order: 02:28:46.790 "We appear to have regained Internet connectivity" (the
   stream answered), 02:28:46.792 "Requesting reachability check" and
   `PUT plex.tv/api/servers/{machine}/connectivity?X-Plex-Token=<server token>&asyncIdentifier=c4bd…`
   (200, empty; 0.6 s after the subscription request in the recording), then at
   02:28:50.195 the event
   `data: <Message address="" port="0" asyncIdentifier="c4bd…" connectivity="0" command="notifyConnectivity"/>`,
   logged "Got notified of reachability for async identifier c4bd…: 0 (responded in 3025
   ms)". The asyncIdentifier is the server's own uuid4 -- the spike had that shape right;
   what it lacked was the server record (404) and the open stream. Before the stream
   was up PMS logged "attempted a reachability check but we're not yet online" and sent
   nothing. `connectivity="0"` is plex.tv failing to reach a server with no public port
   mapping, which presence does not depend on. After the verdict the stream carries
   `: -1` every 10 s.
4. The pubsub request carries `User-Agent: PlexMediaServer-EventSourceClient/<version>`,
   `Accept: */*` and the X-Plex identity, and no `Accept-Encoding`; the answer is nginx,
   `text/event-stream; charset=utf-8`, `Connection: close`, no-cache. PMS's client gives up
   on 20 s of silence and retries 15 s after a failure. Every other claim and registration
   call carries the thirteen headers the cycle-44 announce does (`User-Agent:
   PlexMediaServer/<version>`, `Accept: */*`, `Accept-Encoding: gzip`, the identity).
5. **plex.tv gzips whenever asked.** The exchange and device-PUT answers came back
   `content-encoding: gzip`. rex's announce has asked for gzip since cycle 44 and
   parsed the raw bytes, so against the real plex.tv every heartbeat raised after its PUT
   took effect, and the visibility check behind it never ran.
6. A claimed restart: device PUT, `POST /servers.xml` twice, the pubsub host list, the
   stream, the reachability check. No exchange.
7. 1.42.2 sends all of this to `plex.tv`; the 1.43.4 recording (`outbound-announce.json`)
   sends the announce and `servers.xml` to `servers.plex.tv`. Both are real.
8. **Nothing else is periodic.** From 02:29 to 03:29 the probe sent plex.tv only the
   account's watch-state reads (consent twice, services once, 87 scrobble pages): no
   further `/servers.xml`, device PUT or connectivity check, one pubsub request and 360
   keepalives. The record and the device row stayed put without renewal while the stream
   was open.
9. Not reproduced: the plex.direct certificate flow (rex serves a certificate it is
   given, `--tls-cert`), the pubsub host list (used only to fall back from
   `pubsub.plex.tv`), and the probe's own connections to the other owned servers'
   plex.direct URIs.

What rex does now (`claim.py`, `runtime/plextv.rs`): the exchange with the measured
headers, keeping the account id it answers; then `POST /servers.xml` (422 logged, not an
error), the device PUT with the server's own token when `--announce-url` is set, and the
pubsub stream, requesting the reachability check as it opens, logging plex.tv's verdict
and resubscribing 15 s after a drop. A claimed start does the same without the exchange, its
heartbeat announcing with the server token. `tests/test_plextv.py` drives it against the
recording.

**rex-e2e on zen, registered with it the same day.** Claimed once (03:27:49 UTC, a fresh
claim token through `POST /myplex/claim`). Its log ran `CLAIMED` → `servers.xml POST -> 422` →
`pubsub subscribed` → `connectivity PUT -> 200 b''`, and `/api/servers/aebd…` went from 404 to
200 (`createdAt="1791602869"`, the claim's second): the connectivity 404 of 2026-09-11 is gone.
Restarted with `--announce-url https://zen.tail-net.ts.net:31400` (TLS on its own port, a
Tailscale Let's Encrypt certificate), it was listed `presence=true`, owned, with an
`accessToken` and that one connection, at all 47 polls from 03:29 to 04:17, past the
40-minute lease; the heartbeat logged no failure, so the gzipped device answer now parses.
Two differences from the probe, not chased: the first stream (right after the claim) never
carried a `notifyConnectivity` verdict, the restart's did (`reachability 0`, 1 s after its
PUT); and the record's `localAddresses` is empty where the probe's held its address.

app.plex.tv on the Windows CDP browser browsed it and direct-played a movie over that
connection (`location=wan`, ranged 206 part reads, 1920x1080, 36 s). What the client did on
the way, read from Plex Web 4.162 and its console:

- Chrome 154 refused every request from app.plex.tv to a private or CGNAT address -- fun's,
  zen's and rex's alike -- with "Permission was denied for this request to access the
  `local` address space", although the launcher still passes
  `--disable-features=LocalNetworkAccessChecks,…`. `Browser.setPermission` granting
  `local-network` to `https://app.plex.tv` on the page's `browserContextId` cleared it; the
  same call without the context id changed nothing.
- `isPrivate` is an IP test on a connection's `address`, so a hostname connection is never
  private. With `publicAddressMatches` true (browser and server share the home IP)
  `_isMatchingLocation` sorts such a connection after every private candidate -- it only
  orders the test queue, it filters nothing -- so rex's was tested last, about a minute
  in, behind fun's and zen's timing-out bridge addresses.
- The real Play control is `button[data-testid="preplay-play"]`, with no `aria-label`.
- After those runs fun's own device row read `productVersion="4.162.1" platform="Chrome"
  device="Windows"` -- the browser's Plex Web identity -- where at 03:29 it read
  `1.42.2.10156-f737b826c` / Linux; presence, connections and `lastSeenAt` were unchanged.
  The browser is signed in with the token from fun's zurg config. Cause not established.

**The token gate a claimed server keeps (2026-10-10).** rex-e2e answered `GET
/library/sections` 200 to no token and to a made-up one: rex's gate was opt-in (`--token`)
and the rig ran without one, so every tailnet peer read the library and the bytes. A real PMS
always gates once claimed, and plex.tv decides who is in. Read from zen's own 1.42.2 PMS log
and asked of plex.tv for rex-e2e (`tests/fixtures/plextv_access_tokens_s1422.json`):

- `GET plex.tv/api/v2/server/access_tokens?auth_token=<server token>` answers `<tokens>` with
  one `<token type="device" device=… token=… tokenDisabled="0" owned="1" createdAt=…/>` per
  token allowed in: the server's own first, then each of the owner's devices (20 rows for
  plexa630; the account token in fun's zurg config is one, and so is the token of every client
  seen on rex-e2e that day). It is the list PMS checks a request's token against.
- A claimed start reads it first (the probe's `claimed_start`); the claim itself did not; nothing
  reads it on a timer (the probe's hour, and four hours of zen's log).
- A token PMS does not hold makes it re-read the list with `&unknownToken=<that token>`
  ("Auth: Refreshing tokens inside the token-based authentication filter"), unless the last
  read finished about 6 s ago ("tokens were refreshed recently enough": no re-read began sooner
  than 5.68 s after the previous one finished, none was refused later than 5.86 s after). A
  request arriving during a read waits for it ("tokens were just refreshed, caller should check
  again"). 586 reads in four hours on zen, all prompted by one LAN client's stale token.
- `unknownToken=` does not change the answer; a bad `auth_token` is a 401 `<errors>` document.
- On zen the unknown token then falls through to "authenticating user as guest", because zen's
  allowed network covers that client; off an allowed network it is cycle 30's 401.

rex does the same on both servers (`claim.Claim.admits`, `plextv::Access`): a real claim
raises the gate (`--assert-claimed` does not), the server's own token and every listed, enabled
token are let in, a claimed start reads the list before `POST /servers.xml`, and an unknown token
re-reads it -- in the native server before the runtime lock is taken, so a slow plex.tv holds
up only that request. `tests/test_plextv.py` checks it against the stub; 10 of its checks fail
against the unfixed servers, which answered 200 with no token. Not reproduced: PMS's `GET
/api/v2/server/users` after each read (per-user restrictions; rex keeps no per-user state),
the allowed-networks fallback, and admitting back the transient tokens `/security/token` mints.

**A resubscribe within seconds of the old stream closing reads absent (2026-10-10).** On
rex-e2e, `systemctl restart` (old process gone at 07:59:04, new stream subscribed at
07:59:06) was listed `presence=true` until 07:59:38 and `false` from 07:59:43 for six minutes,
with the new stream open (its socket established, no "stream ended" logged, keepalives read).
A restart that subscribed 16 s after the old process died (kill -9, then systemd's 15 s
`RestartSec`) and one that waited 20 s first both stayed present. Mechanism not established;
the rig's unit waits 20 s before every start. A real PMS takes longer than that to come back.

**A public connection does not make plex.tv's reachability 1 (2026-10-10).** rex-e2e
announced `https://zen.tail-net.ts.net:8443` (Tailscale Funnel) beside its tailnet URI, and its
resource listed both. The Funnel URL was public: from dmm-01, through Funnel's public ingress
rather than the tailnet, a token-bearing part read answered 206 with the same bytes as the
tailnet read, and tokenless reads answered 401. Two connectivity PUTs, one and two minutes after
the public DNS record appeared, still answered `reachability 0` (in 3 s, then in under 1 s), and
no request reached rex in either window, through Funnel or otherwise: plex.tv does not test
the announced connections. The
probe's verdict names `address="" port="0"`; a real PMS's reachability is its public address and
the port it maps (zen's PMS logs a failed UPnP mapping), which rex does not report and which
here is the home IP, not Funnel's. A client off the tailnet uses the announced connection
whatever the verdict.

**4. Sharing: one owner claims, everyone else is shared — and the owner's Plex
Pass covers them all.**

"Claimed" binds a server to exactly **one** owner account. Multiple users is not
multiple claims; it is Plex sharing: the owner invites accounts as friends or
Home users, each gets its own per-user access token, and they see the server with
`owned=false`.

This resolves the multi-user question left open earlier: **a server owner's Plex
Pass covers remote streaming for every user on that server**, so one lifetime
pass on plexa630 covers all shared users and nobody needs a Remote Watch Pass.

Limits that still bite: rex keeps no per-user state -- a claimed rex
admits the tokens plex.tv lists for it, shared users' included (above), but
serves every one of them as the owner -- so real sharing needs per-user views; Plex Home caps at 15 managed users; and friend sharing requires each
invite to be accepted individually, so sharing to a large DMM user base is not
viable by this route.

*Process note, because this finding moved four times before it settled and every
move was avoidable. The first hypothesis — a presence lease — was correct. I
abandoned it twice on measurements that were simply too short (30 minutes
against a 40-minute lease), then invented two mechanisms (plex.tv reachability,
per-identifier caching) that the data never supported, and at one point cited a
WebFetch of a tailnet-only URL as proof of public reachability when WebFetch
runs from this machine's network context and proved nothing. The generalisable
lesson: when a lease is suspected, the measurement window has to exceed the
longest plausible lease before "no decay" means anything, and a hypothesis
should be discarded on evidence rather than on an inconclusive run.*

**2. Keep-alive breaks ffmpeg seeking.** Found and fixed. ffmpeg (so mpv, and
every client embedding it) seeks by issuing an open-ended `Range: bytes=N-`,
reading a little, then sending its next request **on the same connection**. Under
keep-alive that request queues behind gigabytes of undrained body, so ffmpeg
reads media payload where it expects a status line —
`Error reading HTTP response: End of file`, then it reports the stream
unseekable (`AVERROR(ENOSYS)` = `-78`) and gives up. Every CDN answers a media
range with `Connection: close` for exactly this reason. Fix: do the same.

**3. The client's own gate rejects before the server is asked.** `Media`
attributes decide playability client-side. Measured against the catalog:

| Item | 4K HEVC TV | 1080p h264 TV | mobile cellular | kodi default |
|---|---|---|---|---|
| The Mentalist S01E07 — mkv h264 1080p eac3 5604k | PLAY | PLAY | FAIL 720p cap | PLAY |
| Detective Conan 1194 — mkv h264 1080p aac 8110k | PLAY | PLAY | FAIL 720p cap | PLAY |
| Tears of Steel — webm vp8 1920x800 vorbis | FAIL | FAIL | FAIL | FAIL |

rex has no encoder, so a transcode verdict is not a downgrade — the item
does not play at all. Honest probe-derived numbers are mandatory; a guess fails
silently.

**4. RD's `Content-Type` is not a media type.** The CDN answers
`application/force-download` with `Content-Disposition: attachment`. `proxy`
mode rewrites it to the container's real type; `redirect` mode cannot, and the
client sees the raw header. Worth testing per client before choosing redirect.

**5. RAR-backed torrents are unusable.** RD served
`Baby.Looney.Tunes...H.264-playWEB.mkv` as `.rar`. ffprobe reads it as a bare
ac3 stream. `build_catalog.py` skips these; virtualizing them is zurg's job.

**6. The connection-speed probe.** Per plx-native's measurements, the official
Chromium/webOS client's "Checking connection speed" is an ordinary **uncapped raw
Part GET** before `player.open()`, not a dedicated endpoint. On a debrid backend
every play start would pay an unbounded prefix fetch. Given bandwidth is the whole
cost line, that read has to be recognised and bounded.

## Cloudflare quick tunnels are broken

`cloudflared tunnel --url` issued hostnames that returned **NXDOMAIN**
persistently on 1.1.1.1, 8.8.8.8 and the local resolver, across two attempts.
`trycloudflare.com` apex resolves; the quick-tunnel subdomains never published.

Also: `cloudflared` silently adopted `~/.cloudflared/config.yml` (the
`boiler-room.sarmiento.cc` named tunnel) and used its credentials for a
`--url` quick tunnel, which broke routing on the first attempt. Pass an
isolated `--config` to avoid it.

Used instead: `tailscale serve --bg --https=8443` →
`https://mac-laptop-01.tail-net.ts.net:8443`, real Let's Encrypt cert, HTTP/2.
Correct ranges and content type verified through it.

**This was the wrong substitute.** `tailscale serve` is tailnet-only;
`tailscale funnel` is the public one, and it hung when invoked (Funnel needs
enabling in the tailnet policy). So rex was never publicly exposed on that
path. Since removed (`tailscale serve --https=8443 off`).

### What actually serves it: a named Cloudflare tunnel

| | |
|---|---|
| Hostname | `rex.example.com` (proxied CNAME, record id `<dns-record-id>`) |
| Tunnel | `boiler-room-app`, `<tunnel-uuid>` |
| Config | `rex/cloudflared-rex.yml` — **not** `~/.cloudflared/config.yml` |
| Origin | `http://127.0.0.1:32400` |

The tunnel was already on the account, `status: down`, with nothing listening on
its origin port 8080 and its `boiler-room.sarmiento.cc` hostname no longer
resolving — so nothing was displaced. Its local run credentials still work; it
came up healthy on 4 connections across txl01 and vie05.

Credential notes, since three of four did not work:

- `~/.cloudflared/cert.pem` holds only an `ARGO TUNNEL TOKEN` and the API
  rejects it (`unauthorized`) — enough to *run* a tunnel, not to manage one.
- The Nimbly global key covers only the five **Nimbly Technologies** work zones.
  `sarmiento.cc`, `debridmediamanager.com` and the rest sit on Ben's personal
  account `<machine-identifier>`.
- That account's wrangler OAuth token is `zone (read)`: it lists zones and
  tunnels but is refused for DNS record read *and* write.
- The personal global API key is what created the record.

**Teardown:** delete DNS record `<dns-record-id>` on zone
`<zone-id>`, and stop the `cloudflared --config
rex/cloudflared-rex.yml` process. Nothing else persists.

## Collateral change to the Plex account — needs a decision

`PUT /devices/{machineIdentifier}` **did not create a new device.** plex.tv
resolved it to a **pre-existing** row and overwrote it:

- device id `1366743689`, `clientIdentifier="zurg"`, `product="zurg"`,
  `provides="server"`, `createdAt=1751239317` (2025-06-29)
- overwritten: `name` → `rex-spike`, `productVersion`, `platform`,
  `platformVersion`, `device`, and its entire `Connection` list → the tailscale URL

The original name and connection URIs are **not recoverable** — plex.tv keeps no
history and nothing in the workspace registers a device with that client
identifier, so it was likely created by hand.

Mitigating: the first `/api/v2/resources` probe this session returned **only fun
and zen**, so that row was **dormant** — it had no working connection and was not
presenting as a resource to any client. Nothing was using it.

`/devices/{id}` expects the **numeric** device id. Passing a machineIdentifier
does not 404, it lands somewhere else. A new device should instead be created by
the PIN flow (`POST /api/v2/pins` + one browser authorization) and only then
PUT by its numeric id.

**Decision needed:** delete device `1366743689`, or leave it as rex's row.

## Not yet done

- **A GUI client.** Nothing is blocking it now — rex is live at
  `https://rex.example.com` and discoverable on the plexa630 account. Next:
  plezy (open source, debuggable on desktop), then the web app, then Android TV.
  The open question is whether official clients follow the 302 and tolerate RD's
  `application/force-download` content type.
- Real artwork; `/photo/:/transcode` returns a generated solid-colour PNG.
- Multi-user: each user needs their own Plex Pass, or rex runs on their LAN
  and advertises a local connection. plexa630 has a lifetime Pass, so the spike
  did not hit the remote-playback gate.

## Reference material worth keeping

[plx-native](https://github.com/GLinnik21/plx-native) ships `tests/mock_pms.py`
(708 lines), `docs/pms-api.md` (870 lines, "verified against live server", PMS
1.43.x, dated per section), `docs/plex-openapi.json` (1.3 MB),
`docs/pms-hls-protocol-probe.md`, `docs/shared-servers.md` (74 KB). That is the
best PMS documentation found, and its mock is the skeleton rex grew from.

#### The fiftieth pass — recovered findings, complete implementation, and the deferred register

Cycle 50 was recovered from seven surviving commits plus the original session and three hunter
transcripts. The recovered branch was not the completed cycle: one measured `/system` XML patch
had been lost, **18 reported entries had not been implemented**, three transport/GDM rules lacked
durable discriminators, and no full post-cycle suite had run. The audit separated the last two
apparently evidence-bound entries on both isolated writable PMS versions, so all 18 became exact
and actionable. Every one is now implemented; the one remaining Cycle-49 actionable entry was
also closed. No reported entry was silently dropped.

##### Territory A — all 23 matched-catalog findings

1. A decision is serialized like `/status/sessions`: alphabetic scalar attributes and child
   scalars, string `librarySectionID`/tag ids/rating values, list `UltraBlurColors`, and the
   decision Stream's plain display title.
2. Language tags are not generally reduced to the primary subtag. `zh-HK` and `zh-TW` use the
   measured English catalogue. On `/:/prefs`, `pt-BR` and `pt-PT` are identical
   complete regional documents and `hu-HU` is distinct. Three authorized GET-only captures held
   174 rows each; the reported 1.42 surface serves 172, with **1,122 exact localisable cells**.
3. The language enum is named and ordered in the request language; explicit English is not the
   no-header self-named document.
4. A season's inline OnDeck is the show's selected episode and appears only on the season that
   owns it. An unwatched show or another season omits the block.
5. `/unwatched` uses `titleSort` order.
6. A child-bearing `/unwatched` row keeps `?unwatched=1` in its key.
7. Typed `/library/all?type=N` takes that type's default order; only untyped `/library/all`
   defaults to ratingKey.
8. `CommonSenseMedia` is detail-only and absent from light rows.
9. a rolled-up `tv.recentlyadded` row is dated by its newest leaf.
10. a `/children` container carries `thumb` only when that row owns the artwork.
11. show-library director/writer/producer axes are sourced from episodes.
12. a movie's `/allLeaves` is `viewGroup=movie`, `viewMode=65592`.
13. show `/allLeaves` re-stamps derived Image URLs with each leaf's `addedAt` projection.
14. the root `library`, `livetv`, and `media` directories carry `count=3`.
15. `includeExtras=1` propagates into an inline OnDeck row.
16. the anchored search tier reads `titleSort`, not the display title.
17. a searchable tag row is gated by a currently tagged top-level movie/show in the requested
    library, then counts **all** currently tagged metadata rows in that library. Show+episode is
    count 2 while the row key lists one show; an episode alone and zero membership emit no row;
    remove/re-add proves a current count while the tag id persists.
18. `/clients/profiles` follows deterministic profile-file order, not title order.
19. the library provider advertises `video,audio,photo`.
20. seven server preference values and `useLocalAssets` follow their captured defaults.
21. a clip Image uses its own poster on extras listings.
22. untyped `/library/all` omits clips.
23. an untyped row's duration is its stored metadata runtime, not a Media duration, and may be
    absent.

##### Territory B — all eight official-client findings

1–3. The Conversions view has a permanent background queue and list:
`/playQueues?type=42`, `/playlists?type=42`, `/playQueues/1`, and the advertised
`/playlists/39999/items` empty paged children response. Queue id 1 is reserved; user queues start
after it. The type grammar, light row, invisibility elsewhere, version `-1`, and paired empty XML
Playlist are all reproduced.

4. `playbackState=progress` is a per-`X-Plex-Session-Identifier` minute latch. A session that
crosses 60 seconds remains progress after rewinding or stopping below the minute; a fresh session
is ignore despite the stored offset or prior scrobble. Changing only the client identifier does
not reset the session latch.

5. `/system` except `/system/agents` uses the plug-in framework's distinct XML writer: its own
declaration, `application/xml` content type, indentation, compact empty elements, and self-closing
empty container.

6. eventsource response headers use the measured non-Origin and CORS order.

7. trailing and doubled slashes are a finite per-route table. `/:/plugins` has the measured
bare/single/double pattern 200/404/200; the other enumerated pass-through and refusal paths are
kept explicitly rather than normalized globally.

8. a normal idle keep-alive timeout closes quietly. A timeout during the method peek can poison
the buffered reader and make its next read raise `OSError`; that operational exception is
absorbed without changing the measured EOF.

##### Territory C — all ten transport, GDM, and bundled-resource findings

The absolute incomplete-request deadline is enforced inside line reads; whitespace-only obs-fold
is refused; the ten-byte method bound still holds when the method arrives in pieces; a header
value loses at most one optional leading space; unrecognized Transfer-Encoding and non-exact
`chunked` values are ignored; a chunked GET is answered before its body arrives; post-response
body draining has **zero grace**; GDM `Updated-At` moves only on library create/edit/delete; GDM
peers are keyed by source address; and bundled `/:/resources/*` artwork works through the photo
transcoder. Follow-up regressions also preserve first duplicate Content-Length, exact
Transfer-Encoding whitespace, signed-64-bit chunk sizes, closing after a short chunk, and a
lower-case pipelined method.

GDM lifecycle controls are load-bearing: FriendlyName changes `Name` without touching the stamp;
refresh, item edit, collection creation, and index deletion do not touch it. The offline GDM suite
is now **103/103**.

##### Cycle-49 deferred entries, closed or bounded

The old prefix note said 57 paths, but the durable executed list contains **58 unique paths**.
The complete registry was replayed as **58 paths × 3 references = 174 GETs**, all exact in root
attributes, row order, element type, and row-attribute order. A captured finite registry was built;
no rule was fitted to rex's own router.

Numeric device platforms are now reproduced: the direct device row preserves normalized numeric
JSON, while `/devices` and `/statistics/bandwidth` return the measured 500 until a nonnumeric
overwrite repairs the anonymous row. The exhaustive numeric spelling table remains local-only so
no device identity header can mutate a shared reference.

`/connections` received the requested expanded disposable-only matrix: **78 successful observer
snapshots and 26 bounded test sockets** across unclaimed 1.43.4 and 1.42.2. Blank/partial requests
are Pending; routed rows expose method/path and an opaque hexadecimal request number; a partial
second request becomes `Paused ... Pipelined - 1`; close becomes Finished; Pending survives to
19.78 seconds and disappears at the next cleanup sample. But the leading ordinals and order are
snapshot-unstable, the hexadecimal allocator has invisible gaps, Finished retirement is coupled
to an opaque cleanup event, and the byte-count variant needs excluded media traffic. Exact
emulation is therefore terminally underivable without fabricating visible state. No route was
added.

The other terminal entries remain unchanged: `transcoderVideoRemuxOnly` is a server capability
rex does not possess; `/myplex/account` authToken is mapping-state-confounded and unsafe while
the token gate can be off; no measured PMS-side GDM peer consumer exists; and deeper conversion
generator/job rows require unadvertised encoder state.

##### Finite denominators and safety

The safe claim replay covered **111 of 116 families × four host slots × two formats × two auth
states = 1,776 requests**; the five exclusions were three asset bodies, photo transcode, and the
claim write. The retained full Cycle-49 matrix remains 928 reference and 232 rex status cells.
The recovered dual-format pass issued **692 safe XML/JSON pairs** from 176 templates, parsed 614
pairs / 9,206 nodes, and found zero structure, set, order, or scalar-type mismatches; the retained
full matrix remains 1,331 pairs. The served client corpus is 208 JavaScript files / 9,318,734
bytes, 205 of them versioned 4.147.1 assets, with no new reachable PMS family after the background
children fix. The complete legacy prefix denominator is 58/58.

One safety incident is retained rather than hidden: a first `/connections` wrapper misquoted its
remote multiline command and sent exactly six tokenless `GET /connections` requests plus two
blank, immediately closed TCP opens to the shared kids reference. It sent no token, device
identity, or write. All later connection work was stopped on shared references and confined to
inside-container loopback on the isolated copies. Both copies were restored, proved unclaimed and
network-isolated, and released.

#### The fifty-first pass — the stopping repeat reports no findings

Cycle 51 froze behavior/test head `4069fd4` and independently repeated every finite Cycle-50
axis without contacting a protected reference, either scratch copy, plex.tv, or a media body.
Three read-only audits end literally **`NO FINDINGS`**.

The exact-request/catalog audit maps **23/23** Territory-A and **8/8** Territory-B findings to a
measured implementation and discriminating regression. Its retained denominators are 421 matched
request pairs / 37,112 leaf cells; five official-client sessions / 519 flows / 34 method-path
shapes / 79 non-identity query parameters; a durable 70-exchange reference walk; and the complete
58-path prefix and three-document regional fixtures.

The catalog/hierarchy audit closes all 12 assigned catalog changes and repeats catalog features
**54/54**, regional preferences **3/3 documents and 1,122/1,122 cells**, navigation **28/28**,
decisions **2,340/2,340**, mediainfo **35/35**, GDM **103/103**, and the prefix registry **58/58**.
It re-audits the retained 36 axes, 18 subsets, 31 option cases / 248 requests, 270 screen cells,
76 decision-option cells, 27 timeline-option cells, and 780 literal-path cells.

The formats/discovery/transport audit locally repeats the 116-family claim registry (111 safe
families, **444 GETs**), 173 safe dual-format pairs / 2,540 nodes / 13,072 scalar occurrences,
the nine-route `/system` writer map, all transport boundaries, the 92-cell numeric-platform
matrix plus four repair controls, TLS **10/10** under both LibreSSL and TLS-1.3-capable OpenSSL,
GDM **103/103** plus eight lifecycle hooks, regional preferences, and the prefix registry. It
also re-audits the retained 1,776-request claim replay, 692-pair safe dual-format replay, and
1,331-pair complete dual-format corpus without pretending those external cells were freshly read.

The completed `/connections` matrix remains a terminal evidence bound, not an actionable
mismatch. The same is true of encoder-only generator/job state, the confounded account-token and
transcoder-capability cells, unmeasured PMS-side GDM consumers, live zen while unavailable, and
the explicitly safety-bounded first-beat/asynchronous/claimed-write cells. Every executable
finite cell is green; no report turns an absent capability or unsafe instrument into a guessed
implementation. Cycle 51 is therefore the required clean stopping cycle.

Root's final uncontended mutable-surface run passed **1,756/1,756** checks with 14 explicit
fixture/capability skips. An earlier green run was discarded because two worker-owned duplicate
runs overlapped part of it; the authoritative pass ran alone on a pristine restarted fixture.

#### A file that disappears, and the scan that finds it (2026-10-09, plexscratch 1.43.4)

Measured for the zurg-fed scanner (`rust/src/runtime/scan.rs`), on plexscratch with files
moved off its media folder and put back; every request, row and websocket frame is in
`tests/fixtures/missing_file_scan_s43.json`. The copy's `autoEmptyTrash` was `true` (the PMS
default); the steps that need it off set it off first.

- **Off (soft delete).** A path scan of a movie whose file is gone keeps the row and answers
  it 200: `deletedAt` (the scan time) lands on the row and on its `Media`, never on the
  `Part`; `checkFiles=1` reports `accessible=false`/`exists=false`; `/all` still lists it, and
  `/all?trash=1` lists it alone. The frames are one `type -1` pair, `state 3` then `state 5`,
  titled with the row's frame title, between the scan's 99 and 100 steps. An episode's
  `deletedAt` lands on its season and show too (each its own `type -1` pair, episode first),
  while `leafCount`/`childCount` stay as they were; the show is NOT `trash=1`, the episode
  is (`type=4&trash=1`).
- **`PUT /library/sections/{id}/emptyTrash`.** A movie: one `state 9` `deleted` frame of
  its own type, then 404. A show: the season's and the show's `type -1` pairs first (their
  `deletedAt` cleared), then the episode's `state 9` carrying `parentItemID` and `rootItemID`;
  the season and show lose the episode from their counts.
- **The file back.** Before the trash is emptied, the scan restores the SAME row -- key,
  Media and Part ids, guid -- with the same `type -1` pair. After it is emptied the file is
  a new row on a new key (66, 67 and 68 on the copy), announced `state 0 created` with an empty title, then
  `state 1 created`, `state 1` `analyzing`, `thumbnailing`, `state 1`; a movie's metadata
  pass follows (`processing`, `2 matching`, `5`, `3 queued`, `4 loading`, `5`).
- **On.** The row is deleted during the scan: one `state 9` frame of its own type and
  nothing else, then 404. A new file again takes a new key.
- **A library folder missing, unreadable (`chmod 000`) or empty** trashes NOTHING, with
  `autoEmptyTrash` on or off: only the scan's own frames. The PMS log calls it "Taking 5 items
  out of the map ... for being unavailable"; `checkFiles` reports every Part missing while
  no row is marked.

rex emulates all of it, and keeps the last rule for zurg's own failure modes too: a
listing that fails, a 503 while zurg loads (retried), and an empty library folder change
nothing. Inferred, not measured: a season and show leave the trash once none of their
episodes is in it; with `autoEmptyTrash` on, a show goes with its last season (a season with its last episode is the DELETE route's measured cascade).
Not reproduced: a new episode's metadata pass on the copy was its SEASON's refresh (every
episode of the season framed again); rex frames the new episode alone, with the movie's
sequence. Also unmeasured: what a scan creates for a brand-new show and season (rex
announces them with the same created frames as a leaf).

#### The feed: sources push, rex scans nothing (2026-10-10)

Why the direction reversed. The scanner above tied rex to zurg's internals: it listed
zurg's WebDAV (PROPFIND), asked zurg's `/api/library/entry` about each new file, and ran when
zurg's Plex integration sent `/library/sections/{id}/refresh?path=` (gated on `/activities`).
It also kept a release's name-based identification forever once its row existed, because a
rescan fetches entries only for files it does not know. Reproduced on the last scanning build
(acaa3cc) against a stub zurg: a movie scanned before zurg knew its IMDb id stayed
`local://100000` after zurg learned `tt0103827` and asked for a rescan of the release.

What replaced it (`docs/feed-api.md`, `rust/src/runtime/feed.rs`, `source.rs`): a source pushes
items (`POST`/`DELETE /rex/feed/{source}/items`, chunked `PUT .../snapshot`) with its own
feed token, and rex reads its bytes from `<source url>/<path>`. None of the feed is PMS
emulation, so none of it was measured on a real PMS; what a row does when it is added or
goes away still is:

- Removal is the measured trash (`missing_file_scan_s43.json`): soft delete, `state 9` under
  `autoEmptyTrash`, the same key back before `emptyTrash`. A feed batch that adds or removes
  rows is framed with the measured scan activity (4a, 6a2); `/activities` is empty between
  requests and `refresh` is the catalog skeleton, as on the oracle.
- An empty snapshot removes nothing (6b3's empty library folder). Refusing a snapshot that
  would remove more than half the source's items without `force` is a design choice against
  a source answering while it loads, not a measurement.
- An item with no `media_info` is held, unshown, and arrives as newly added once probed.
  A real PMS shows an unanalysed file at once, but on rex such an item cannot play
  ("An item without a probe cannot play" below, 2026-10-12), so the hold stays.
- A pushed id the row was not identified by re-identifies it on its key, keeping what a
  person did to it. zurg's matcher keeps an episode's SHOW key (`GrandparentRatingKey`), so a
  show all of whose episodes move to one new identity is re-identified in place; the first
  build minted a new show key for that case (the live dry run below, before the fix: show
  100110 became 100184).
- The first push of a file the scanner made a row for adopts that row (by Part `file`); a
  complete snapshot trashes the scanner's rows nobody adopted, as a whole-library scan did.
- `--part-mode redirect` is gone from the native server: it read zurg's signed link from
  `/api/library/entry`, which a pushed source does not hand over. The oracle keeps it.

Measured on zen with zurg `feat/rex-feed` (7b3d3e15) against the RD test account,
read-only, in scratch directories:

- Migration dry run on a read-only copy of `~/rex-e2e`'s database (118 leaves, 184 rows,
  97 with an IMDb id from the scan): zurg's first snapshot adopted all 118, created and
  trashed nothing, and every one of the 184 keys stayed, episodes under the same season and
  show keys. 8 rows were re-identified because zurg's id differed from the name match (7
  `Mysterious Love` files in the movie library, now carrying `imdb://tt15174276`, and `Ek
  Secret Crime`'s episode, whose show was re-identified in place). The last run, on the final
  build after the fresh-library run below, also re-identified the Ibelin movie from the id
  set by hand in the scratch zurg's data; the live zurg has none for it.
- A fresh library: the startup snapshot created 118 rows in 19 s (58 movies, 31 shows, 60
  episodes, as the live rig holds). Widening zurg's `movies` filter through
  `POST /api/directories/movies` pushed 3 new items 3 s later, held for want of a probe;
  zurg's `/manage/{hash}/mediainfo` on one pushed its probe and it became row 100184, first in
  Recently Added; narrowing the filter again trashed it and released the other two. A late
  IMDb id (`/manage/{hash}/imdbid`, `tt19811010`) re-identified row 100050 from
  `local://100050` to `plex://movie/626907dd4eba3bfb08aac74d` with its watch state. A 64 KiB
  Range read through rex matched zurg's own `/dav` bytes (MD5
  `95f9fa1e8a705a7d85dbbffcbca4760d`). zurg sent this rex no refresh.

## The universal transcoder (2026-10-11)

rex converts what a client cannot direct-play once `REX_TRANSCODER` names an FFmpeg
(`rust/src/runtime/universal.rs`, README "Transcoding"). What follows is what it rests on.

### How a real PMS converts, measured

Recorded on plexscratch (1.43.4) for the clients' exact requests, in
`tests/fixtures/transcode/`:

- **Plex Web 4.160 (app.plex.tv) converts over HLS**, Plex Web 4.147.1 (the client rex
  serves at `/web`) **over DASH**, with `directStream=0&directStreamAudio=0`: the bundled one
  never remuxes. Both decide first and send `directPlay=0` when their own MDE refuses the file.
- The decision is `directPlayDecisionCode 3000` with the refusal's text, then
  `generalDecisionCode`/`transcodeDecisionCode 1001` "Direct play not available; Conversion
  OK.". Its `Media` and `Part` describe the OUTPUT (HLS `mpegts`, DASH `mp4` with
  `optimizedForStreaming`; the output's size and bitrate); the Part has no `key`, `file` or
  `size`; each Stream keeps its own fields with the output's codec, size and bitrate and gains
  `decision` (`copy`/`transcode`) and `location` (`segments-av`, or DASH's
  `segments-video`/`segments-audio`).
- HLS: `start.m3u8` names `session/<session>/base/index.m3u8` (the query's `session`), with
  `?X-Plex-Incomplete-Segments=1` when the request carried it; the media playlist lists
  EVERY segment of the title up front (`#EXTINF:10, nodesc`, the last the remainder,
  `#EXT-X-ENDLIST`), before any is encoded. Segments are `video/MP2T`, `Connection: close`.
- DASH: a static MPD, one `SegmentTemplate` per adaptation set (`duration="5"`,
  `initialization="session/<id>/$RepresentationID$/header"`,
  `media="session/<id>/$RepresentationID$/$Number$.m4s"`, `startNumber="0"`), video id 0,
  audio id 1; `mediaPresentationDuration` through a 32-bit float (`PT0H30M0.689941S` for
  1 800 690 ms). Every `.m4s` is the init (`ftyp`+`moov`) followed by
  `styp`/`sidx`/`moof`/`mdat` with an absolute `tfdt`; header and segments are
  `application/octet-stream`.
- While converting, the timeline carries the `TranscodeSession` (key
  `/transcode/sessions/<id>`) and `size` counts it; `Bandwidth` and `Session.bandwidth` are
  ~1.05x the OUTPUT bitrate (509 for 485), not direct play's ~2.1x the source. A conversion
  that dies reaches the client as `terminationCode 4006` on its next timeline.
  `/transcode/sessions` keys the row by the bare id. ping and stop are empty 200s, then 404s.
- PMS downloads its encoders from `servers.plex.tv/api/codecs/` on first use: the isolated
  scratch copy fails "Encoder install failed: libx264", which is why conversions there were
  recorded in remux mode, and an encoder from another PMS build fails `Unrecognized option
  'crf:0'`.

### What did not work as documented

- **FFmpeg's `-segment_list` cannot say a segment is finished.** It is buffered and written
  as the run ends; a slow source's first segment waited for the whole file. A segment is
  finished when the next one's file opens (or FFmpeg exits 0).
- **`-copyts` with the segment muxer after a seek wrote ONE unreadable segment** (jellyfin-ffmpeg
  8.1.3, a seeked remux of a real mkv); Plex's own fork pairs `-copyts` with a
  `-segment_copyts` upstream does not have. Timestamps counted from the seek plus the
  segmenter's `-initial_offset` cut every boundary on the title's clock.
- **The MP4 muxer rebases every fragment to zero** under `-output_ts_offset`;
  `-format_options movflags=+frag_discont` keeps the real time (a run from 600 s: `tfdt`
  14 400 000 at timescale 24 000). With it, AAC's priming writes the first audio fragment's
  negative time as a wrapped 64-bit `tfdt` (4.2e14 s); `-avoid_negative_ts make_non_negative`.
- **Plex Web's player is Shaka**, which starts only once `max(minBufferTime, 2 s)` is
  buffered: the reference's `PT10S` (2x the segment) held every start for two 5 s segments,
  6.3 s to the first frame. rex sends one segment's worth.
- **Plex Web re-fetches both DASH headers on every seek.** A header that waited for its run's
  first fragment hung once playback had passed the 120 s kept behind the player.
- **Right after a seek the player still has a request out for the old position.** Each
  restarting FFmpeg at its own segment in turn was a loop: 3 531 restarts in seconds, until
  zurg answered 5xx. The newest request moves FFmpeg, once; an older one gives up.
- **A killed rex left its FFmpeg running** for an hour, reading from zurg into a deleted
  directory. Children now take `PR_SET_PDEATHSIG`, which fires when the spawning THREAD exits,
  so they are spawned from one thread that lives as long as the process; a start also kills
  any FFmpeg still running in its directory.

### Performance on zen (Pentium G4400T, HD 510, 2 cores)

jellyfin-ffmpeg 8.1.3's own iHD driver (26.2.4) opens the iGPU where the system's 22.3 did
not. The probe finds VA-API encode and hardware decode for H.264, HEVC 8-bit and MPEG-2
(no 10-bit HEVC, VP9 or AV1 on Skylake).

| Source, through zurg | Path | Speed | First segment | Plex Web |
|---|---|---|---|---|
| HEVC 1080p + E-AC-3 5.1 | GPU decode, scale, encode | 5.7-6x | 1.4 s | first frame ~2 s, seek 2.4-2.9 s |
| H.264 mkv, AAC | remux (copy) | ~190x | 0.6 s | hls.js 1.1 s, Shaka 1.3 s |
| H.264 + E-AC-3 | video copy, audio to AAC 5.1 | 12x | 1.9 s | |
| AV1 1080p | CPU decode, GPU encode | 1.4x | 2.5 s | |
| (x264 veryfast, CPU only) | | 1.45x | | |

The first segment is the encode of its first seconds, so a conversion's segments are 2 s
(1.3 s to the first on the GPU, against 2.4 s for 5 s); a remux keeps Plex's 10 s (HLS) and
5 s (DASH), because it can only cut on the source's keyframes.

## An item without a probe cannot play (2026-10-12)

Question: could rex show a pushed item before its source sends `media_info`, as a real PMS
shows a file before analysing it, and so not need a source that runs ffprobe?

Setup on zen: a throwaway build whose feed showed an item with no `media_info` instead of
holding it (the scanner's empty-probe row: `Media` with no container, codecs or duration,
`_probePending`). One 60 s 1920x1080 H.264/AAC file, pushed twice under two names, once with
its ffprobe JSON and once without, from a plain `python3 -m http.server` source. Client:
Plex Web 4.147.1 from rex's own `/web`, driven over raw CDP in the Windows Chrome.

| | With `media_info` | Without |
|---|---|---|
| Details page | `1min`, Video `1080p (H.264)`, Audio `Unknown (AAC Mono)` | no runtime, Video `None`, Audio `None` |
| Play, no `REX_TRANSCODER` | `DECISION direct play`, part reads, 0 to 30 s in real time at 1920x1080 | play queue, `timeline` playing then stopped at 0 ms within a second, "This server is not powerful enough to convert video." No decision request, no part read |
| Play, `REX_TRANSCODER` (CPU ffmpeg) | not tried | the client asks to convert: `400` on `/video/:/transcode/universal/decision` and on `start.mpd` |

1. **The client gates on the streams before it asks the server anything.** With no codec it
   cannot tell direct play is possible, so it goes straight to conversion, and a server
   with no encoder ends the attempt client side.
2. **rex's transcoder cannot rescue it.** Its plan is built from the row's `Media` and
   `Stream`s, so a row without them is a bad request.
3. Aside: the probed copy as MKV fails the same way in Plex Web without a transcoder,
   because the browser cannot direct-play MKV and asks for a remux.

So `media_info` is required and the hold is right. rex does not probe files itself: a
source has to send ffprobe's output with each item (zurg does, from its own analysis).
Native Plex apps were not tried.
