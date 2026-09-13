# plexsim — a Plex Media Server that owns no files

## RESULT: the official Plex Web client plays a debrid stream from plexsim

Verified 2026-09-11 15:26 by driving the real client over CDP:

```
video.src      https://plexsim.example.com/library/parts/10010/…/file.mp4
               ?X-Plex-Session-Identifier=64s596vv5bjqm0n
readyState     4 (HAVE_ENOUGH_DATA)      paused false
currentTime    164.56 -> 179.59 advancing in real time
resolution     1920x1080                 decoded 4929 -> 5381 frames
droppedFrames  0                         error null
```

plexsim's own log for the same playback:

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
| 5 | `playQueues` must accept a **section** uri, not just a ratingKey | "An error occurred loading items to play", with no request reaching plexsim |
| 6 | An MP4/H.264/AAC item in the catalog | Plex Web will not direct-play mkv or webm; it demanded a transcode plexsim has no encoder for |
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

**The client gate is the real constraint, not the server.** plexsim serves all
five perfectly; what varies is whether a client will accept them:

| item | Plex Web / Mac | TV h264+ac3 | TV 4K |
|---|---|---|---|
| mp4 / h264 / aac (×3) | play | play | play |
| mkv / h264 / **eac3** | **transcode** | play | play |
| webm / **vp8 / vorbis** | **transcode** | **transcode** | **transcode** |

So a debrid-backed Plex library is only as useful as its container mix. An mp4
/ h264 / aac release plays everywhere; mkv+eac3 is TV-only; vp8/vorbis in webm
direct-plays on nothing, which matters because plexsim has no encoder and a
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
rows, not the artwork — plexsim emitted every file as `type=movie` while also
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
regardless of what plexsim returns — it reported 0 images even for tiles that a
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

The answer came from reading the client's own bundle. plexsim serves the web
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

plexsim returned `size: 3` and nothing else. `/library/sections/N/all` already
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
generic empty container plexsim returns for an unknown route. That is worse than
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
2. **The client's own bundle.** plexsim serves the web client, so the gating
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

Consequences for plexsim, each one a behaviour change rather than a new field:

- **Download was hidden by one word.** The provider advertised
  `protocols: "stream"`. plexsim can serve a download -- it is the same Part read
  with `download=1` and a `Content-Disposition` -- so the entry was hiding a
  feature that already worked.
- **Delete needs `allowMediaDeletion`.** plexsim reported false because it owns
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
  plexsim must not claim: a transcode verdict means the item does not play at
  all. Advertising a transcoder it does not have is what produced "This server is
  not powerful enough to convert video" in the first place.

#### Response shapes the write half turned up

| Measured | What plexsim did |
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
plexsim tag names with no ids at all. Every `Genre`, `Director`, `Role` and
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
merge from its `--mutate` set, with that reason written next to it, and plexsim's
own merge is in memory where a restart undoes it.

#### What plexsim now answers

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
Watched** in the client turned the button into "Mark as Unwatched", and plexsim
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
plexsim and reading its own `UNKNOWN` log, then measuring each against the
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
| `/hubs?identifier=a,b` | Home asking for named shelves | the filtered subset — plexsim returned all of them, so Home drew rows it had not asked for |
| `/library/metadata/{id}/subtitles` | the Search Subtitles dialog | `Stream` rows from a subtitle agent; plexsim has none, so the shape with nothing in it |
| `PUT /library/metadata/{id}/prefs` | a show's episode sorting, a collection's sort mode | empty 200; the **GET is a 404** |
| `PUT /playlists/{id}/items/{itemID}/move[?after=]` | drag-to-reorder a playlist | no `after` means the FRONT; answers with the playlist row |
| `PUT /library/collections/{id}/items/{ratingKey}/move[?after=]` | reorder a collection | same, but keyed by the member's **ratingKey**, not a playlistItemID |
| `/library/sections/{id}/unwatched`, `/resolution` | a listing and an axis the hub shelves link to | `resolution` is read off the Media, not off a tag |

And a class of endpoints where the honest answer is a refusal, because that is
what a real PMS answers: **every `/player/*` command is a 400** (they belong to a
player, not to a server — plexsim was telling companion clients their commands
had been accepted), `/system/proxy` 400, `/library/sections/{id}/search` 400,
`/library/platforms/*` `/hubs/sections/home*` `/hubs/metadata` and an item's
`markers` `banner` `chapterImages` `indexes` all 404, `/library/streams/{id}`
**501**, and `/livetv/*` an empty container.

#### The catch-all now 404s, and that found the last missing endpoint

A real PMS answers an unrouted path with 404 (measured). plexsim's catch-all
answered 200 with an empty MediaContainer, which is the trap of fix 4 from the
other side: a client reads that as "this endpoint exists and there is nothing in
it". The rule that made the empty container look safe — refusing an endpoint
cleanly is worse than not having it — is about endpoints the client *needs*, and
the fix for those is to route them.

Flipping it proved its own worth immediately: `conformance.py` broke on
`GET /library`, an endpoint python-plexapi asks for before anything else and
which plexsim had never routed. The old catch-all had been hiding it for the
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

#### What that measurement did find: four notification types plexsim never pushed

The same 75 seconds produced these, and plexsim's socket had been silent since
the day it was built -- it completed the upgrade and then only pinged:

| Type | Element | When | plexsim now |
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
plexsim's default** -- `_wants_xml()` returns true unless a client explicitly
asks for JSON -- so the default response path of ~60 new endpoints had never been
exercised. All 67 render parseable XML. Three rendered the **wrong element**:

| Endpoint | plexsim rendered | a real PMS renders |
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
plexsim had the codec in a video's `displayTitle` and the track name in a
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

plexsim's SSE channel had been a keep-alive since it was written. Both transports
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

plexsim served `flattenSeasons` in its section preferences and then ignored it,
so a library set to hide seasons still drew every one of them. The obvious
implementation -- have `/children` return episodes instead of seasons -- is wrong.
Measured: with the pref set to Hide, a real PMS **still answers `/children` with
the seasons**, and what changes is a single field on the SHOW row,
`skipChildren: true`. The client skips the level on the strength of that.

Enum, measured: `0` Show, `1` Hide, `2` Hide for single-season series -- so `2`
depends on the show, not just the library, and a two-season show under `2` keeps
its seasons.

#### A season is not a lesser show, and an episode is not a small one

Diffing plexsim's rows against the reference's, type by type:

- A **season** detail carries `OnDeck`, `Extras` and `Preferences` exactly as a
  show does. plexsim answered all three for shows only, so a season page had no
  episode to offer and an empty settings tab.
- The **Preferences lists differ**: eleven settings on a show (`episodeSort`,
  `flattenSeasons`, `showOrdering`, `languageOverride`, the auto-deletion pair,
  `useOriginalTitle`, `enableCreditsMarkerGeneration` and the three language ones)
  against **three** on a season (audio language, subtitle language, subtitle
  mode). plexsim invented a single `episodeSort` row. Both lists are now captured
  in `item_prefs.json`, and a setting written through
  `PUT /library/metadata/{id}/prefs` comes back both inside `Preferences` and as
  a field on the row, the way `episodeSort` does on a real show.
- An **episode** carries `Extras` but no `Preferences` and no `OnDeck`.
- An episode row carries **no `Genre`, `studio`, `tagline` or `Country`** --
  measured on the 1.43.4 reference *and* on production fun. plexsim's episodes
  inherited all of them from the enriched file row, so an episode page advertised
  its show's genres where a real one shows none.

Not copied, for want of anything to derive them from: `CommonSenseMedia` and
`contentRatingAge` on a show (a Plex-only rating provider and an age plexsim
would have to guess from a rating string), and the external `Guid` array on a
season (the reference's has real tmdb/tvdb season ids; plexsim mints only its own
`plex://` season guid, and fun's episodes carry no Guid at all, so the field is
agent-dependent rather than universal).

#### Artwork: composites, and the size the client asked for

A collection's, playlist's or section's tile is a `composite` image. Measured on
the reference: a **400x600 JPEG mosaic** built from the members' posters.
plexsim served a flat generated grey tile. It has no imaging library and will not
fabricate a mosaic, so it now serves **one member's own poster** -- honestly one
of the images that mosaic is made of, and what the tile is for.

The client also asks the image transcoder for specific sizes (measured in the
recorded corpus: 240x240 for cast headshots, 240x112, 240x360, 360x540, 360x202)
and a real PMS returns exactly those dimensions. plexsim cannot resize, but TMDB
publishes fixed-width variants and the row stores one of them, so the upstream
request now picks the variant nearest the width asked for
(`/t/p/w342/…` → `/t/p/w185/…` for a 185-wide request). The served image is still
a fixed width rather than exactly the one requested, which is a deliberate
difference: the alternative would be shipping a 1280-wide backdrop to fill a
240-wide tile, which is what it did before.

#### A session is created by the TIMELINE, not by anyone reading bytes

`/status/sessions` answered `{size: 0}` for plexsim's whole life, so Now Playing
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
plexsim answered 200 to every terminate and did nothing.

Adjacent, measured: an unknown transcode session is a **404** for both
`GET` and `DELETE /transcode/sessions/{id}`, while
`/video/:/transcode/universal/subtitles` is a **400**. plexsim answered 200 and
404 respectively.

#### The Dashboard's numbers are real, and `timespan` is a bucket size

`timespan` decoded from a real server's own rows: **6 is per second**, then 4
hour, 3 day, 2 ISO week, 1 month, 0 year, with `at` the bucket start in the
server's local time. A real PMS counts **every response's bytes**, not only media
-- a 4 KB `/status/sessions` reply showed up as its own per-second row.

plexsim serves every byte it reports, so it now counts them per (device, lan) and
rolls them up on read, and `/statistics/media` counts completed plays from the
timelines. `/statistics/resources` is **refused with a 404** rather than faked:
the client treats that as "unsupported" and hides the graph, where the `{size:0}`
plexsim used to send draws an empty graph forever.

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

plexsim iterated its query parameters looking for names it recognised, found
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
  siblings, with **AND binding tighter than OR**, exactly as in SQL. plexsim ANDed
  everything, so the UI's "Match any of the following" silently meant "match all";
- a date value may be relative (`-30d`), an absolute epoch, an ISO date or an ISO
  datetime. The suffixes are `s m h d w mon y` -- **`m` is MINUTES** and `mon` is
  the only month form -- an unsigned value means the past and a leading `+` the
  future. Anything else is a 500, case-sensitively.

And the refusals, which matter because the alternative is a silent no-op: a
comparison character with no `=` separating it from its value (`?year%3E2010`) is
a **400**; an unparseable number, an empty value and an unknown date suffix are
each a **500**; an unknown FIELD is ignored with a 200. plexsim answered 200 with
the library unfiltered for all of them.

The engine lives in `filters.py` and is driven by the `FieldType` block of the
captured `Meta`, so plexsim can only honour the operators it advertises.

#### The same language, six passes later: the field's TYPE decides

The operator table above is correct about what each operator *means* and wrong
about which fields it applies to. Re-measured field by field against the
reference, four of the types do not behave like the string they were implemented
as, and the corrections each turn a silently-wrong result set into the right one:

| Request | Reference | plexsim after the first pass |
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
**`location`** is a section `Location` id, not a path, which is why plexsim's
sections now carry their own key as that id instead of all sharing `1`.

An empty value is therefore **not** uniformly a 500, which is what plexsim's
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

plexsim built its advertised-field map keyed by those prefixed names and then
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
string back and reads the rule tree out of the answer. plexsim 404'd it, and a
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
client can reach. They are reproduced anyway, because the harness diffs plexsim's
answer against the reference's and a "corrected" answer is a difference to chase.

#### `/filters` is per library TYPE, and seven of the axes it names 404

The filter BAR is a separate surface from the filter engine, and plexsim served
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
side. A real PMS advertises axes it does not serve, so plexsim advertises them
too and its unknown-leaf 404 answers them the same way. The filters behind them
do work on the grid -- it is only the "list me the values" axis that is missing.

What the axes themselves answer, measured against the lab and against a
6000-item production library:

| Axis kind | Entry | Note |
|---|---|---|
| tag (`genre`, `director`, `actor`, …) | `{fastKey, [thumb], key, title}` | only a **genre** entry carries `type`; a person entry carries the tag's `thumb`. plexsim stamped `type` on every one |
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
1.43.4 and **404 on production's older build**; plexsim follows the lab, which is
the designated reference.

#### An unexplained field that is still a measurement: `nocache` on show axes

Every secondary container of a **show** library carries `nocache: true` -- its
`/filters`, `/sorts`, `/genre`, `/year`, all of them -- and no movie library's
does. The first reading was cache state, which would make it per-server and
transient. Three movie libraries and two show libraries across **two independent
servers** rule that out: it tracks the library's TYPE. The cause is not visible
from outside, so the correlation is what plexsim reproduces.

#### A deliberate deviation: plexsim's error bodies

A real PMS answers a refusal with an HTML shell (`<html><head><title>Not
Found</title>…`, `text/html`, 85 bytes). plexsim answers `text/plain` with the
reason it refused (`plexsim: unsupported sort key bogusKey`). No client parses a
4xx body, and the reason is what makes a failed probe diagnosable, so this one
difference is kept on purpose. It is the only remaining shape difference in the
whole `/library/sections/*` surface apart from rows plexsim's catalog has no data
for.

#### A smart list is a stored QUERY, and plexsim stored a snapshot

plexsim accepted `smart=1` on both create endpoints, set a flag, and then stored
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

The last row matters for more than tidiness: plexsim tagged the members, which
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
plexsim sliced at a hard 12 and wrote `more: false` unconditionally, so a
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

Three fields plexsim did not send -- `globalViewCount`, `userCount` and a
`User: [{"id": 1}]` list -- and one refusal it did not make: `type=` is required
and a real PMS answers **400** without it.

The giveaway that these come from the play HISTORY rather than off the row: Big
Buck Bunny reported `globalViewCount: 1` with **no `viewCount` at all**, because
it had been played and then marked unplayed. The row's counter is current state;
this shelf is the history, and `userCount` is how many distinct accounts appear in
it. plexsim sorted by `viewCount` off the row, so anything watched and then marked
unplayed vanished from the Top shelf that is supposed to remember it.

#### The `include*` / `exclude*` options, one measurement each

Eight parameters the official client sends and plexsim accepted and ignored. An
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

`checkFiles` is also the one option plexsim has nothing to do for, and
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
changes. plexsim keeps the first in the order the sort just produced, which is at
least deterministic; the count, which is what the UI reads, matches.

#### The decision container sends ONE pair of codes, not all of them

plexsim answered every decision with `generalDecisionCode`,
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

**`/myplex/account` must not carry a token.** plexsim sent `authToken` in it; the
reference does not send the field at all. It buys nothing -- a client that reads
this endpoint already holds a token -- and every page that reads it is a page that
could log the server's. Removed.

**`/system/scanners/{id}` answers `<Scanner name="…"/>` rows**, not `Directory`
ones, in a container whose `identifier` is `com.plexapp.system` rather than the
library's. A movie library lists four (`Plex Movie`, `Plex Movie Scanner`,
`Plex Video Files Scanner`, `Plex Video Files`) and a show library two
(`Plex TV Series`, `Plex Series Scanner`). plexsim answered one `Directory` row
with a `key` and a `title`, which the Edit Library dialog's scanner dropdown does
not read -- an advertised control with nothing behind it, from the other side.

**`/statistics/resources` answers 200, and plexsim was 404ing it.** The earlier
note here reasoned that a refusal reads to the client as "unsupported" and hides
the graph, which is better than an empty one. The measurement contradicts the
premise: BOTH reference servers answer 200 with a `StatisticsResources` series of
`{timespan, at, hostCpuUtilization, processCpuUtilization, hostMemoryUtilization,
processMemoryUtilization}`, and only `timespan=6` has samples -- every coarser
bucket, and a request with no timespan, is an empty 200.

plexsim now samples when the endpoint is ASKED, which is the only time the numbers
are wanted: the Dashboard polls while it is open, so the graph fills in the way it
does on a real server and no sampler thread runs for a page nobody is looking at.
Two of the four numbers are real -- host CPU from the load average over the core
count, process CPU from `getrusage` over wall time -- and the two memory ones are
0.0, because the stdlib cannot see host memory and a plausible percentage would
draw a graph of nothing.

#### The Home screen: `/hubs/promoted` is called once per library, not once

The client calls `/hubs/promoted?contentDirectoryID=<section>` **per pinned
library**. plexsim ignored the parameter and answered every library's shelf on
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

plexsim sent a `hubKey` of `/library/all` for everything, so every shelf expanded
to the whole library, and no shelf had a `context` to lay out by.

Three more shapes: a SHOW library's Recently Added shelf is typed **`mixed`**
where a movie library's is `movie`; `excludeContinueWatching=1` drops that shelf;
and the bare call (no `contentDirectoryID`) adds `home.playlists`
(key `/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio`).

`/hubs/continueWatching`'s hub is identified **`continueWatching`**, not
`home.continue`, is styled `hero`, and its `key` is its own
`/hubs/continueWatching/items` — which plexsim did not route, so the shelf had
nowhere to expand to. `/hubs/home/continueWatching` and `/hubs/home/onDeck` answer
plain `{size, allowSync, identifier}` row lists, and **`/hubs/home/recentlyAdded`
keys on `type`, not on `sectionID`** -- plexsim had it exactly the other way
round, so the TV shelf's "see all" opened an empty page. An unknown home hub is a
404, where plexsim answered an empty shelf.

#### The preplay page's Related shelf lives at a path plexsim did not route

The client rewrites `/library/metadata/{id}` to `/hubs/metadata/{id}` and appends
the leaf, so `/hubs/metadata/{id}/related` is a DIFFERENT endpoint from
`/library/metadata/{id}/related` -- and only the hubs form carries the shelves.
plexsim routed neither, and a 404 is exactly what the preplay page reads as "no
related items".

Measured: `related` carries `allowSync` and the librarySection triple, `postplay`
carries **neither**, and the hub inside both is `movie.similar` / "Related
Movies", keyed on the item's own `/library/metadata/{id}/similar`.

The reference fills `similar` from its metadata agent, and its own list is EMPTY
for one of the lab's five movies while the others answer two to four rows -- so an
empty answer is a correct answer. plexsim has no agent similarity data, so it
computes its own (other rows in the library sharing a genre) and says so in the
code: the shape is measured, the source is plexsim's.

#### Search: seventeen hubs every time, a word-prefix match, and a score

Four endpoints, and plexsim had all four wrong.

`/hubs/search` answers **seventeen typed hubs on every query, empty ones
included** -- movie, show, artist, album, episode, track, collection, photoalbum,
autotag, photo, tag, actor, director, genre, playlist, shared, place -- because
the client lays the results page out from that list. plexsim answered only the
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
character matches nothing at all. plexsim used `needle in title`, so `uck`
matched and `t` returned the whole library. (The reference is additionally fuzzy
-- `eel` finds "Firefly" and an actor called "Halina Reijn" -- which is a trigram
score plexsim does not approximate.)

The other three, all previously wrong:

- **`/library/search`** -- advertised by plexsim's own `/media/providers` as
  `universalsearch` and answered with an empty container, on a code comment
  claiming "a real PMS answers the bare query with an empty container". It does
  not: a bare query is a **400** and a real one answers `SearchResult` rows.
- **`/search`** -- the legacy endpoint, 404 on plexsim. It answers results PLUS
  four `Provider` rows (`m_title`/`m_type`, the prefix is the reference's), each
  result carrying `personal` and `sourceTitle`; its `size` counts the providers
  too (1 result + 4 providers = 5). A MISSING `query` is a 400 and an EMPTY one is
  a **404** -- two different refusals on one endpoint.
- **`/hubs/search/voice`** -- 404 on plexsim. One hub per type, titled with the
  bare type name, identified `results.search.<n>` with the shared context
  `hub.results.search`, `hubKey` and no `key`, and a `distance` on the hub and on
  every row.

`/library/sections/{id}/search` is the correction of an earlier finding rather
than a new one: only the form with **no `type`** is a 400. With a type it is a
real listing whose `title2` is `Search for '<query>'` and which carries no
`content` -- the in-library search box, which plexsim refused in every form.

#### A play queue is a queue, and plexsim kept a key list

Six defects in one subsystem, and the first two each break playback outright.

**A queue over a show, a season, or a show library's section held the PARENT row.**
Measured: a queue over the lab's Firefly holds its **six episodes**, a queue over
a season holds that season's three, and a queue over `/library/sections/2/all`
holds every leaf -- never the parent, which has no `Media` at all. plexsim
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
`playQueueVersion` changed. plexsim computed the ids from their POSITION
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
to that index, without touching the version. plexsim ignored the parameter and
always reported item 0 -- which is the state the client throws away.

**`playQueueLastAddedItemID`** tracks PLAIN adds only (a `next=1` add leaves it
where it was), and the client uses it to DISABLE the Up Next shuffle button once
the user has queued something. plexsim never sent it, so the button stayed live.

**Where an add inserts** was wrong both ways: a plain add goes after the block of
already-added items following the current one, and a `next=1` add goes immediately
after the current item, ahead of that block. plexsim appended to the very end and
inserted at index 1 regardless of where the player was.

And the smaller ones, each measured: `playQueueSourceURI` comes back **rewritten**
(`library://x/item/<encoded>` for one item, `library://x/directory/<encoded>` for
anything naming a list) rather than echoed; the container carries **no**
`allowSync`; `window=`/`center=` slice AROUND the current item while
`playQueueTotalCount` still reports the whole queue; an empty queue omits
`playQueueSelectedItemOffset` and `playQueueTotalCount`; `PUT
/playQueues/{id}/items/{id}/move?after=-1` moves an item to the front (plexsim did
not route it and answered 200 with the queue unchanged); and `PUT
/playQueues/{id}/shuffle` and `/unshuffle` are **404** -- the client reverts its
optimistic `playQueueShuffled` on failure, so plexsim's 200 left the shuffle
button stuck on over a queue that was never shuffled.

Refusals, all of which plexsim answered 200 to: a create with no `uri` and no
`playlistID`, or with no `type`, is a **400**; a GET of `/playQueues` is a **400**
(the endpoint exists, the request is wrong); an unknown queue, an unknown queue
item, and a POST or DELETE of one queue are **404**. plexsim's DELETE really
dropped the queue, so a client that tried one lost the queue it was playing.

#### `/:/progress` is not `/:/timeline`

It keys on **`key`**, not `ratingKey`, and it answers an **empty body** rather than
a MediaContainer. plexsim routed it straight at the timeline handler, which read
no key at all, so it recorded nothing and returned a container -- and every client
that reports progress this way (plex_debrid, Kodi/PKC, Infuse, the older Plex
apps) lost its resume point silently.

#### The timeline, re-measured: four corrections to an earlier finding

| Case | Reference | plexsim before |
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
answers 400 for an unparseable rating. plexsim answered an empty 200 to all of
them, which tells the client the write landed. (`rating=-1` clearing both
`userRating` and `lastRatedAt`, and `rating=99` being stored unclamped, were
already right.)

`PUT /library/parts/{id}` is the same failure with a worse consequence: a stream id
that is not a stream of the right TYPE on that item is a **400** and the selection
is left untouched. plexsim accepted anything with a 200 and then cleared every
selection, because nothing matched -- so a rejected request left the item with **no
selected track at all** and the picker went blank. `0` is the client's "None"
pseudo-stream and is valid for subtitles only; `audioStreamID=0` is a 400.

Two more paths: a LEAF on a comma-joined key list is a **404**
(`/library/metadata/a,b/userState` answered full metadata rows with no `UserState`
in them, which the web client's per-grid probe reads as state it has), a
non-numeric ratingKey is a **404**, and
`/subtitles/:/transcode/universal/start` -- the URL Plex Web uses for a `<track>`
src when it CAN direct play, which is plexsim's only path -- is a **400** on both
reference servers rather than the catch-all 404.

#### The History page filters and sorts

`/status/sessions/history/all` honours `accountID`, `deviceID`,
`librarySectionID`, `metadataItemID` and `viewedAt` comparisons, and sorts;
plexsim ignored every one of them. So Top Users' "View Play History" link
(`accountID=&viewedAt=`) and the per-item history link (`metadataItemID=`) both
showed the whole server's history, in insertion order, and the Played column's
sort did nothing.

Two details worth the ink. The operator here is a plain **`>=`**, not the library
grammar's `>>` -- two query languages on one server. And an unknown sort key is a
**400** here where a library listing answers 500 for the same mistake.

`DELETE /status/sessions/history/{historyKey}` removes one row and answers 200
with an empty container; an unknown key is a 404. plexsim matched the history path
on the path alone, so a DELETE was answered with the LISTING, and a numeric key
fell through to the live-sessions handler and answered `{size: 0}` -- which is
exactly the "it worked" signal for a delete that did nothing.

#### The decision ignored `hasMDE`

findings.md already recorded that a client which does not claim MDE gets the
3000/1001 refusal naming the protocol it fell back to, but the code branched on
`directPlay` alone. So plexsim answered the MDE pair to a client that never
claimed MDE -- telling it a media decision was made on its behalf.

#### `GET /` is not `/identity`, and eleven refusals a real PMS makes

plexsim answered `/` with the five-field identity document. Measured, a real PMS
answers the whole capability blob -- the same ~50 scalars as `/media/providers`
-- plus **25 `Directory` rows** (`{count, key, title}`) naming its top-level
endpoints: actions, activities, butler, channels, clients, devices, diagnostics,
downloadQueue, hubs, library, livetv, media, neighborhood, playQueues, playlists,
resources, search, server, servers, service, statistics, system, transcode,
updater, user. python-plexapi's `PlexServer()` loads every attribute it has from
here, so an identity document leaves it with almost none of them.

`/butler` lists **23** tasks, and only `BackupDatabase` carries a `title` and a
`description` -- the client humanises the rest from the name. Its root carries no
`size`. plexsim listed two with invented titles, so the Scheduled Tasks page
showed two tasks that do not exist on a real server and hid twenty-one that do.
A PUT to a task that exists is a **404**; only POST (run now) and DELETE (cancel).

`/library/metadata/{id}/tree` **nests**: a show's tree holds its seasons, each
season its episodes, each episode its `MediaItem`, and an EPISODE's own tree is
rooted at the SHOW. A `MediaItem` here carries only its `id` -- the width, codecs
and bitrate plexsim put on it belong to a normal detail response. plexsim answered
one flat row with `MediaItem: []`, which is both the empty-list shape no real PMS
sends and missing the whole hierarchy the Get Info dialog reads.

Two listing details: a show's `/children` carries an extra `Directory` row
`{key: "/library/metadata/{id}/allLeaves", title: "All episodes"}` and the
container's `size` **counts it**, which is exactly what `excludeAllLeaves=1`
takes back off. And `Guid` is gated: a DETAIL row always carries it, a LISTING
carries it only under `includeGuids=1`; plexsim sent it everywhere.

The refusals, each of which plexsim answered an empty 200 or an empty container
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

And five endpoints plexsim refused that a real PMS answers: `/log/networked`
(200, empty), `/diagnostics` (`{size: 0, content: "plugins"}`), `/resources`
(`{size: 0}`), `/media/grabbers` (four `MediaGrabber` rows -- an empty list tells
the Live TV page the server supports no tuner protocols at all rather than that it
has no tuners), and `/system/library/sections` (a `com.plexapp.system` container).

`/security/token` answers a **different** token on every call; plexsim answered a
constant, so two devices would have shared one delegation credential.

Three shapes on the collection and playlist side: a single collection's container
carries `allowSync: true`, the identifier and the media-tag pair, and a NUMERIC
`librarySectionID` (plexsim sent a string and omitted the other three, so a client
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

plexsim's section handler dispatched `all` on the path alone, so a PUT there
answered the ordinary grid LISTING with a 200. The dialog reads the status, reports
success, and nothing was written. **Every Save, for the life of the project.**

Its pre-read is a second endpoint plexsim never had:
`GET /library/sections/{id}/common?type=1&id=…` answers a `Common` row (JSON
`Metadata` with `type: "common"`, XML `<Common>`) in a secondary container titled
"Common", holding the fields the selected items SHARE. With several ids the fields
that DIFFER are dropped and named in `mixedFields` instead -- which is how the
dialog knows to show "(mixed)" rather than overwrite. plexsim 404'd it, and the
dialog's `populate()` rejects on that: "There was an error editing this item",
before the user could type.

And its source of editable TAG fields is a third:
`GET /library/sections/{id}?includeDetails=1&includeAdvanced=1`, from which the
client reads `Type[].Field[type=="tag"]`. plexsim answered two Directory rows
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

plexsim stored whatever it was sent and answered 200. A real PMS:

| Request | Reference | plexsim before |
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
leaves EXACTLY `[A, B]`: the originals are gone. plexsim appended, so the Edit
dialog could only ever add tags and the list grew on every Save. A `tag-` removal
CLEARS the whole list whatever value it names -- even one the row does not have --
and the indexed list in the same request rebuilds it, which is how "remove one tag"
actually works on the wire. plexsim matched removals by exact name, so a tag
containing a space could never be removed (the client percent-encodes the value
before the query encoding, so it arrives double-encoded).

A tag also matches **case-insensitively and keeps its canonical spelling and id**,
and it does so after being removed from every row -- a real PMS keeps a tag TABLE.
plexsim looked only at the rows, so clearing a tag lost its identity and the next
add minted a new id; there is now a tag table for exactly this.

#### A collection and a playlist are metadata items

Both have ratingKeys and the client edits both through `/library/metadata/{key}` --
its own `saveUrl` falls back to that path for a playlist on a server without the
true-playlists feature. plexsim kept them outside the catalog, so both answered 404
and neither could be renamed from its own page.

`PUT /library/metadata/{collection}/prefs?collectionSort=1` is how the collection's
sort and mode are set, and they come back as **string** attributes on the row.
Reading those prefs is a 404 (write-only), and an unknown pref is a 400.

#### Library management validated nothing

`PUT /library/sections/{id}`: a `name` alone is a **400**, and so is a form with no
`agent` or no `scanner`, or an unknown agent, scanner or `language`. plexsim
accepted all of them with a 200, so a typo in the Edit Library dialog looked saved.

`POST /library/sections` answers a **plain-text 400 naming what is wrong** -- with
no Content-Type header at all: `'location' is missing or invalid. `, `'type' is
missing or invalid (must be one of 'movie', 'show', 'photo' or 'artist') …`,
`'agent' is missing or invalid …`, `new scanner needs to be paired with new agent `,
`'name' is missing or invalid `. A nonexistent location and a duplicate name ARE
both accepted (201). plexsim answered **201 to every malformed form** and really
created the library -- a POST with no parameters at all made one called
"New Library", which then sat in the sidebar.

`PUT /library/sections/{id}/refresh` is a **404**: the scan is a GET or a POST and
the cancel is a DELETE. And `PUT /playlists/{id}` with an unknown parameter,
`smart=1`, or nothing at all is a **400**.

#### plexsim had no token gate at all

A real PMS **401s everything** except `/identity` (so a client can discover the
server) and `/web/*` (the client itself). Measured on both reference servers, for a
MISSING token and a WRONG one alike -- there is no distinction between them, no
`WWW-Authenticate` header, and the answer does not change with
`Accept: application/json`:

```
GET /library/sections            (no X-Plex-Token)
HTTP/1.1 401 Unauthorized
X-Plex-Protocol: 1.0
Content-Type: text/html
Cache-Control: no-cache
Connection: close

<html><head><title>Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>
```

`/`, `/library/sections`, every listing, every item, **artwork**, `/photo/:/transcode`,
`/:/resources/*`, **media bytes**, both notification channels (the 101 upgrade is
refused with a 401 body), `/:/prefs`, `/status/sessions`, `/myplex/account`,
`/media/providers`, `/hubs`, `/clients`, `/accounts`, `/devices`, `/activities`,
`/butler` -- all of it. The token is accepted as the `X-Plex-Token` **header** as
well as the query parameter.

The one exception is `/security/token`, which answers its own **400** with no
parameters and **403** with a token that is not good enough -- never a 401. That
split is how a client tells "you did not ask properly" from "you may not have one".

plexsim answered 200 to all of it, with no token check anywhere: the string
`X-Plex-Token` appeared in the code only in the CORS `Vary` header. Any peer that
could reach the port read the whole library, the preferences, the sessions and the
media bytes -- and plexsim is published over a named Cloudflare tunnel.

The gate is now implemented and is **opt-in** (`--token` / `PLEXSIM_TOKEN`), which
is the deviation: a real PMS always has a token, and plexsim defaults to none
because its four test suites and its probes talk to it without one. The startup log
says which it is, in as many words -- `token gate: OFF -- every peer that can reach
this port can read the library`. The harness starts its own gated instance to prove
the gate works, so the behaviour is tested rather than merely available.

#### `Cache-Control` on everything, and gzip

A real PMS sends **`Cache-Control: no-cache` on every response** -- measured across
eight endpoints on both servers. plexsim sent none at all, except on artwork, where
it sent `max-age=86400`: a day of browser caching that the reference does not do,
so a Change Artwork or a re-match stayed invisible even though the version in the
URL had changed. The reference's policy is the opposite of what plexsim had:

| Path | Reference | plexsim before |
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

3.4x on a five-row grid, 5.4x on plexsim's own. Both servers always use
`Content-Length` and never chunk, even at 4.9 MB -- which plexsim already matched.

#### The notification LIFECYCLE, which is what makes a client refetch

plexsim pushed the FIRST frame of every sequence and stopped. That is enough for a
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

Two of those frames are load-bearing, and plexsim sent neither:

- **`state: 5` dispatches `metadataProcessed`, which is what makes the client
  REFETCH the row.** Without it Refresh Metadata does nothing on screen until a
  reload.
- **The reducer deletes its entry only when `metadataState` is ABSENT**
  (`t.metadataState||t.mediaState ? … : delete e[t.sourceURI]`), and plexsim's one
  frame always carried `"queued"` -- so every gate spelled `!!metadataState` stayed
  latched: the spinner, and the Refresh and Match menu entries.

And the activity never ended. `add_activity` fired `started`; only
`cancel_activity` ever fired `ended`. So an activity nothing cancelled sat in
`/activities` for the life of the process at `progress: 0` -- where the reference's
started frame is `progress: -1` and it reaches `ended` at 100.

An **edit** pushes two frames (`state 3, "loading"` then `state 5`); plexsim pushed
none, so an edit made through the API was invisible to every other open client. A
**create** pushes `state 0, "created"` and a **delete** pushes `state 9,
"deleted"` -- and `9` is what dispatches the client's own `deleted` action, so
without it a row deleted through plexsim stayed on screen everywhere else.

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
   "message":"Request: [100.85.55.10:55607 (WAN)] GET /:/websockets/notifications?filters=log …"}]}}
```

`filters` is an exact whitelist of type names, pinned by running two sockets side by
side: `?filters=log` received 386 log frames and **zero** of anything else, while a
socket with no filters received `playing` and `timeline` and **zero** log frames. So
**`log` is opt-in only and everything else is opt-out only.** plexsim fanned every
event to every sink, so the socket opened for the Console's log pane got playing and
activity frames and never a log line -- and the pane was empty.

#### The channels' own plumbing

| | Reference | plexsim before |
|---|---|---|
| websocket keepalive | protocol ping every **10 s** with a ONE-byte payload | every 20 s, empty |
| eventsource keepalive | `event: ping\ndata: {}` -- a real named event -- every **10 s** | `: ping`, an SSE COMMENT (invisible to an EventSource listener), every 15 s |
| a client CLOSE frame | answered with a close, status 1000 | never read, so a browser's `ws.close()` hung in CLOSING until its own timeout and the sink was never reaped |
| the 101 upgrade | carries `X-Plex-Protocol`, `Cache-Control` and the CORS pair | the three handshake headers alone |
| SSE CORS | no CORS headers without an Origin, the echoed origin with one | **`Access-Control-Allow-Origin: *`** unconditionally — the exact bug already fixed once for `_send` |

#### `/photo/:/transcode` refused nothing, and `/:/resources/<anything>` drew a tile

| Request | Reference | plexsim before |
|---|---|---|
| no `url` | **400** | 200, a generated grey tile |
| `width` alone, `height` alone, or neither | **400** | 200 |
| `width=abc` | **400** | 200 |
| `url` naming a row the server does not have | **404** | 200, a generated tile |
| `/:/resources/<unknown>.png` | **404** | 200, a generated tile for any name |

Both dimensions are mandatory -- one alone is not a best-effort resize. And the
404s matter more than they look: a client that gets a 404 draws its own title-card
placeholder, while a 200 grey tile is an image it will happily cache.

Two transforms plexsim cannot do and now records rather than ignores silently:
`format=png` is honoured by a real PMS (real PNG bytes, which the client asks for
when it composites an overlay that needs alpha), and `blur`/`opacity`/`background`
are real transforms (a 200x200 poster went from 6727 bytes to 1366 under
`blur=30`). plexsim has no image codec on its stdlib-only server path, so it passes
the fetched bytes through unchanged. The client gates those filters on a VERSION
comparison (`imageFilters: "0.9.9.13"` in its feature table) and plexsim advertises
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
`/a/b/c`) is a 404. plexsim 404'd all of them, so the four the client bundle names
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

plexsim answered the GET body -- 200 -- to all nine refusals. Worse, `POST
/library/sections` **with no parameters at all** answered 201 and really created a
section called "New Library", which then sat in the sidebar.

#### Part reads carry both validators

Every byte response from a real PMS carries `ETag` (the part's own file hash) and
`Last-Modified`, on the 200 and on every 206. plexsim sent neither, so a client had
nothing to revalidate against. plexsim owns no file to hash, so its ETag is a digest
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

plexsim answered a listing in CATALOG order and left ties in whatever order the rows
arrived in. Measured on both servers: the default is **`titleSort` ascending** (the
title where no sort title is stored) -- fun's show grid opens on
`【OSHI NO KO】, 13 Reasons Why, 30 Rock, 86 EIGHTY-SIX, The 100`, because The 100's
titleSort is `100` -- and the SECONDARY key is always titleSort, whatever the primary
is and whichever direction it runs. So an edited Sort Title never moved a row on
plexsim, and the grid's own default disagreed with its sort control.

(Collation is the one part not reproduced: the reference uses ICU, so `'Burbs`
sorts before `#Jowable` and plexsim's byte order puts them the other way. Recorded
rather than faked -- there is no ICU on the stdlib server path.)

#### `includeAdvanced=1` was ignored, and the Sort menu lost most of its entries

The client sends it on the grid listing and on its FilterCollection, and reads the
`Sort` list straight into the sort menu. plexsim's Meta was identical with and
without it. Measured, the flag adds:

| Type | Added |
|---|---|
| movie | `year`, `userRating`, `contentRating`, `viewOffset`, `viewCount`, `mediaBitrate` + the `Duplicates` filter (`advanced: true`) |
| show | `year`, `userRating`, `contentRating`, `addedAt` |
| season | `season.index,season.titleSort`, `userRating`, `addedAt` |
| episode | `titleSort` (with a `firstCharacterKey`), `year`, `userRating`, `duration`, `viewOffset`, `viewCount`, `mediaHeight`, `mediaBitrate` + `Duplicates` |

So a movie library went 9 → 15 sorts and 28 → 29 filters, an episode view 7 → 15 and
17 → 18. And three of the keys it was about to advertise -- `userRating`,
`contentRating`, `mediaBitrate` -- answered **500** on plexsim's own listing, which is
an advertised control that breaks the grid the moment it is used.

#### The in-library search box is a SUBSTRING matcher

`/library/sections/{id}/search` is not the same matcher as `/hubs/search`. Measured:
`?query=ear` finds "Tears of Steel", `?query=a` finds three of five rows, and ONE
character is enough -- corroborated on fun's 3856-movie library (`?query=ngers` → 11
rows including `Challengers` and `Passengers`). It is title-only and
case-insensitive. plexsim routed it through the hub's word-prefix matcher, including
its two-character floor, so the in-library search box found nothing that did not
start a word.

#### The "All episodes" row is first, and a paged request drops it

Measured: a show's `/children` puts the `Directory` row **first**, before the seasons,
and a PAGED request omits it entirely (`size 1, totalSize 2` for one season).
plexsim appended it to EVERY page and counted it there, so a virtualised list
mis-measured and every page grew a spurious tile.

Corrected in the eighth pass: only a ONE-ROW page drops it. The client's own
`Start=0` page keeps it, outside the page, and it carries the show's leaf counts.

#### `nocache` tracks the library TYPE on a listing too — correcting an earlier finding

This file recorded the rule as paged-versus-unpaged. Re-measured on four libraries
across both servers: a **show** library's `/all` carries `nocache` paged AND unpaged,
a movie library's never does -- the same rule the secondary containers follow. Since
the client always pages, plexsim's version meant a show grid never carried it and a
movie grid carried it when unpaged.

#### Change Poster wrote to a path that served an image

The client's own Change Poster / Change Background builds
`/library/metadata/{id}/` + (`poster`|`art`) with **type PUT** -- singular -- and then
does `model.set("thumb", response)`. So the response BODY is the new artwork path.
Measured: `PUT …/poster?url=…` → `200`, body `/library/metadata/1/thumb/1789241428`;
the plural `…/posters` is a **404** for a write.

plexsim had it exactly inverted: the plural path took the write (answering an empty
200) and the singular one served image BYTES. So in the real client Change Poster did
nothing **and** set the tile's `img src` to a blob of PNG. Its `selected` flag was
wrong too -- keyed on "is there any artwork at all" rather than on which candidate the
row points at, so after picking the local placeholder nothing came back selected.

#### Sequence and state: nine things a one-request probe cannot see

- **Removing a collection member did nothing.** The client builds
  `DELETE /library/collections/{id}/items/{ratingKey}` and assigns the response onto
  its model; plexsim had no DELETE branch, so it fell through to an empty container --
  the tile vanished optimistically and came back on the next fetch. Measured: 200 with
  the collection ROW (new `childCount`, year span), and idempotent. A collection ADD
  answers the same row, not the `leafCountAdded`/`leafCountRequested` pair a PLAYLIST
  add answers -- plexsim sent the playlist shape.
- **`/library/onDeck` was the first twelve catalog rows**, with no view state at all.
  It is the server-wide Continue Watching list -- every row carries a `viewOffset` --
  and it is EMPTY on a server with nothing in progress. That endpoint is what every
  non-web client's On Deck reads.
- **A resume point under a minute is not stored.** The reference leaves the row
  untouched at 3 000, 20 000 and 59 000 ms and starts recording at 61 000; plexsim
  answered `playbackState: ignore` and wrote the offset anyway, so a title glanced at
  for two seconds appeared in Continue Watching.
- **"Remove from Continue Watching" HIDES the row and KEEPS the offset.** The
  reference's `viewOffset` was still 91 000 afterwards; the row simply stops appearing
  until the next timeline brings it back. plexsim deleted the offset, so the entry
  also threw away the resume position and the next Play started over. An unknown
  ratingKey there is a 404.
- **A scrobble writes NO history row.** The reference's history stayed at one row
  through three scrobbles -- rows come only from a timeline that reaches the end.
  plexsim recorded one per mark-as-played, so History and the Top shelf counted things
  nobody had watched. And a show or season keeps its OWN `viewCount`/`lastViewedAt`,
  independent of its leaves: it INCREMENTS rather than recomputing (unscrobbling one
  episode left the show at 2), and unscrobbling the show removes the field. plexsim
  emitted neither on a parent, so a TV grid's "Last Played" sort had nothing to read.
- **`historyKey` is monotonic and never reused.** The reference went 34, 35, delete
  35, then 36; plexsim derived it from the list length, so a delete made the next play
  reuse the deleted key. And a removed item's history row SURVIVES with its
  `ratingKey` and `key` stripped -- plexsim kept both, so the History page linked a 404.
- **Deleting a season's last episode removes the season.** plexsim left it listed with
  `leafCount 0`, so the show page drew a season tile with nothing behind it.
- **A playlist does not take a row it already has.** Re-adding answers
  `leafCountAdded: 0` and leaves the count alone, and a uri naming the same key twice
  adds it once. plexsim appended every time, so Add to Playlist silently duplicated.
- **A removed row leaves the play queues.** The queue shrinks, `playQueueTotalCount`
  drops and the VERSION steps, and the selection moves to a row that still exists.
  plexsim left the removed row's id as `playQueueSelectedItemID` and never moved the
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
surface. plexsim answered the GET body to any verb, so `DELETE
/library/sections/1/all` came back 200 with the grid, which tells a client the delete
was accepted. The verbs each leaf takes: `refresh` GET/POST/DELETE, `emptyTrash` and
`analyze` PUT, `indexes` DELETE, `prefs` and `all` GET/PUT, everything else GET only;
and a POST to the section itself is a 404.

Three smaller ones from the same sweep: `/:/timeline` requires **`state`** and is a
400 without it (it is the only required parameter); a bare `GET /playQueues` is an
empty 200 while `?type=` is a 400; and `/livetv/epg/lineup`, `/channelmap` and
`/lineupchannels` are **400** on a server with no tuner rather than the blanket empty
container plexsim answered for the whole tree.

#### `/:/prefs` was missing eight rows, one of them the server's name

174 on the reference, 169 on plexsim. The one that matters is **`FriendlyName`**:
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
alone is ignored entirely, with no slice, no `offset` and no `totalSize`. plexsim
tested them with OR, so a lone `Size=1` returned one row and claimed a total, and
a lone `Start=1` silently dropped the first row. A negative size drops paging on a
real server; in plexsim it was a Python slice, so `Size=-1` served every row but
the last.

And the modern client sends them as **HTTP headers**, not query parameters
(`fetchRange`: `headers:{"X-Plex-Container-Start":…,"X-Plex-Container-Size":…}`).
plexsim read only the query string, so every windowed grid request got the whole
library and no `totalSize` -- the same shape of bug as the missing Seasons block,
one transport over. Both are now normalised at the door.

`totalSize` is the FILTERED count, which plexsim already had right.

#### Sorting: a spec is a LIST, and the seed matters

Measured on the reference, and plexsim honoured none of it:

| Request | Reference | plexsim before |
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
measurement, so it is what plexsim answers.)

#### CORS, which a browser enforces before the server sees anything

| | Reference | plexsim before |
|---|---|---|
| normal response, with an `Origin` | echoes that origin, plus `Access-Control-Expose-Headers: Location, Date` and `Vary: Origin, X-Plex-Token` | `Access-Control-Allow-Origin: *`, always |
| normal response, no `Origin` | **no CORS headers at all** | `*` anyway |
| preflight | echoes the requested `Access-Control-Request-Headers` **verbatim**, `Max-Age: 1209600` | a FIXED list |

The fixed list was the bug. It omitted `X-Plex-Session-Id` (plexsim allowed
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

The artwork surviving is the one that matters. plexsim was dropping `thumb` and
`art`, which leaves the row with no artwork URL for the client to ask for at all
-- so the grid tile goes blank, where a real unmatched item still shows its
poster. The `unmatched=1` filter had to learn the same guid forms the client's
own Match/Fix Match predicate tests for, or an item the server called unmatched
was invisible to the filter that looks for exactly those.

Also measured while there, and deliberately NOT copied: every real row carries
**four** `Image` entries -- `coverPoster`, `background`, `clearLogo`
(`/library/metadata/{rk}/clearLogo/{v}`) and `backgroundSquare` (`/squareArt/{v}`)
-- and all four endpoints serve bytes. plexsim advertises only the two it has.
Advertising a clear logo it cannot supply would put its generated grey tile over
the hero banner in place of the title, which is worse than the client falling
back to text.

#### A negative result worth keeping: no lock was needed

plexsim is a `ThreadingHTTPServer`, and everything the UI writes -- playlists,
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
access patterns plexsim actually has. The check stays in the suite at a smaller
size, because the failure mode it guards against -- two clients handed the same
ratingKey -- would be silent rather than loud.

`harness/scripts/replay.py` was run against the same server with the recorded
`reference-walk` corpus. What is left after this work is content (a different
library: four Blender movies and Firefly against three fixture rows and two
shows), the deliberate capability differences above, and 13 `404`s for the
reference's own ratingKeys -- plexsim keeps its key ranges disjoint by type
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
dialog from validating. plexsim answered 400 to every call. Measured on the whole
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

plexsim applied the known settings beside an unknown one, stored `"abc"` in an int,
echoed every bool back as the string that was written, and stored ANY item key --
including its own parser's `_pairs` list, which then rendered onto the metadata row.
An episode has no settings, so every write to one is a 400.

#### Agents, scanners and plug-ins

- `/system/agents?mediaType=1` lists the reference's three modern agents (Plex Movie,
  Personal Media, NFO Movie) each with 45 `Language` rows and `default="1"` on
  `en-US`, which the dialog's `findWhere({default:true})` preselects. plexsim sent
  one language and no default. `mediaType=abc` is a 400; a type no agent serves is
  `size: 0`. Now `system_agents.json`.
- `/system/agents/{id}/config/{type}` and `/contributors` are EMPTY containers for a
  modern agent on BOTH real servers -- only a legacy plug-in has a chain -- with 404
  for a type no agent serves and 500 for a non-number or missing parameters.
  `/attribution` and `/attribution/image` are 404 for a modern agent. plexsim
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
(404 for an unknown id, `abc` included); plexsim listed one row describing itself.

A completed play's history row and its `StatisticsMedia` row carry the PLAYING
device's id. plexsim wrote device 1 on both, an id no Device row in the same
container named. With no `timespan`, `/statistics/bandwidth` and `/media` answer
every rollup, 0 to 4, in one list.

`/library/metadata/{id}/users/top` is one `Account` row with a `globalViewCount`
for an item that has completed views, `size: 0` otherwise, and a **500 for a
season**. The count survives a history delete and an unscrobble (movie 1 reports 2
with neither), so plexsim keeps its own. `/nearest` is an empty library container
for every item and a 404 for an unknown one.

#### Small endpoints, and the last three 405s

| request | reference | plexsim before |
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
`application/json, text/plain, */*` is JSON. plexsim answered JSON whenever the
substring appeared.

Across ~110 JSON responses of the reference there is not one `null` and not one
`[]`. The XML writer already dropped `None`, but plexsim's JSON carried
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
  equal one. An episode strips its OWN title. plexsim filed "The Simpsons" under T
  and gave every episode its show's name as titleSort.
- `originalTitle` is the original-language title, present only when it differs.
  plexsim put the media FILENAME there, which the client prints on the preplay page,
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
  personal videos and playlists, each with its measured key. plexsim answered three
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
  plexsim hosts no plug-in framework, so their prefs, contributors and attribution
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
- `/services/browse` lists `/plexsim` rather than mount points, because plexsim
  has no filesystem. `optimizedForStreaming` on an mp4 stays 0: where the moov atom
  sits is not something ffprobe reports.

#### The sixth pass: what the player asks for, and what every write pushes

Cycle 6 swept the playback surface and the notification channels. Both agents'
before/after snapshots of the reference matched. One write could not be undone: a
stray `POST /downloadQueue` created an empty download queue that `DELETE` refuses
with a 404. It holds nothing and touches no library row.

#### The decision refused nothing

A real PMS validates a decision before it answers, and every refusal is a 400 with
an HTML body. plexsim answered `mdeDecisionCode: 1000 "Direct play OK."` to all of
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
answers `directPlay=0` with 3000 / 1001 / 1001. plexsim has no encoder, and the
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

| request | reference | plexsim before |
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
field marks captions inside the video stream; plexsim set it on every track.

#### Notifications: the type numbers and the collection lifecycle

A playlist's TimelineEntry is `type 15` and a collection's `type 18`. The client
builds the URL of the row it updates from that number, so plexsim's `1` dispatched a
delete for a metadata row nobody holds, and a deleted list stayed on screen in every
other client. Measured frame by frame on both channels:

| write | frames |
|---|---|
| playlist create | `state 0 created`, type 15, sectionID `-1`, title `""` |
| playlist delete | `state 9 deleted`, type 15, the playlist's title |
| collection create | `state 0 created`, type 18, its title (plexsim sent nothing) |
| collection delete | each member `state 3 queued`, then each member `state 5`, then `state 9 deleted`, type 18, title `""` |
| edit on `/library/metadata/{id}` | `state 3 loading`, `state 5` |
| edit through `/library/sections/{id}/all` | `state 3 queued`, `state 5` |
| rate, scrobble, unscrobble, history delete, playlist or collection add/remove | nothing |

A movie's frames title it with its year (`Elephants Dream (2006)`). The collection
CREATE response carries no librarySection fields, where the ADD response does.
`DELETE /library/collections/{id}/children/{ratingKey}` removes a member exactly as
`/items/` does; plexsim answered it with an empty 200 and removed nothing.

The eventsource channel's first ping comes ten seconds in, not on connect. And
`filters=` with an EMPTY value is not "no filter": it delivers everything INCLUDING
the log stream.

#### The timeline's minute, pinned

**Exactly 60 000 ms is not stored, and the rule holds on every state.** A `stopped`
beat at 30 000, or at 59 500 of a 60 000 ms item, is `ignore` and stores nothing.
plexsim exempted `stopped`, so stopping a trailer at ten seconds left a resume point,
and stopping one near its end marked it watched with a history row. A play completes
past 90% on ANY state: a `playing` beat at 95% answers `complete` and sets
`viewCount`, and the `stopped` beat after it counts nothing again. The response's
`viewCount` is the item's own count (0 while a first play is in progress), and its
`size` counts the Bandwidths block and nothing else.

#### Malformed identifiers and query edges

Reference and fun agree on every one of these.

| request | answer | plexsim before |
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
  keep-alive; plexsim writes an unframed body with `Connection: close`, which an
  EventSource reads the same way.
- What adding an item to a download queue does, and the exact refusal text a PMS with
  no transcoder at all would give. Neither could be measured without writes the lab's
  safety rules forbid.
- A valid transcode start still answers 404: the reference would transcode, and
  plexsim cannot.
- The effect of a subtitle selection on the play queue and the next decision. The lab
  has no subtitle tracks and fun is read-only.

#### The seventh pass: the whole probe table and every corpus, diffed mechanically

Cycle 7 ran `probes/ui_surface.py` read-only against both servers (202 rows) and
replayed the recorded corpus with its ratingKeys remapped onto plexsim's items. It
triaged every difference into catalog content, already recorded, or new behaviour.
Six were new.

- **A theme plexsim can never serve.** Every show, season and episode named
  `/library/metadata/{show}/theme/1`, and the endpoint answers 404 because plexsim
  has no theme audio. On fun, a show without a theme carries no `theme`, and its
  children carry no `parentTheme` or `grandparentTheme`. The web client reads
  `grandparentTheme || parentTheme || theme`, so with theme music on every page
  asked for a 404. No row names a theme now.
- **Recent Playlists on a library's hubs.** `includeLibraryPlaylists=1`, which the
  client's own hub request always sends, adds `recent.library.playlists.{id}` right
  after the library's Recently Added. Its key is
  `/playlists/all?type=15&playlistType=video&sort=lastViewedAt:desc&sectionID={id}`,
  with style `shelf` and no `promoted`. plexsim ignored the parameter. Only the
  empty shelf was measurable, because neither server has a playlist.
- **Hub and search rows by type.** A movie or episode on any shelf or in search has
  no `Guid` and text-only tags. (Overturned for Related and postplay shelves in the
  eighth pass: those carry the full row.) This includes `/hubs/home/recentlyAdded?type=1` and
  `/hubs/search`, which bypassed the projection. A SHOW on a `mixed` Recently Added
  TV shelf, whether on home, the library tab or `/hubs/promoted`, is the FULL row:
  `Guid`, `Location`, `Rating` and tag ids. A show in search keeps its `Location`.
- **`excludeFields` everywhere.** The named fields go from every row (listings,
  `/children`, `/allLeaves`, hubs, search) and from the container. A show's
  `/children?excludeFields=summary` loses the container's summary too. plexsim
  applied it only to detail reads.
- **A season's and an episode's `/arts` are their own.** They are empty on the lab,
  and fanart.tv's season art on fun. plexsim offered the SHOW's backdrop as the
  item's selected choice.
- **An episode's `Image` list.** `coverPoster` is the show's poster and the
  episode's own 16:9 still is `snapshot`, on detail and listing rows alike. plexsim
  labelled the still as the poster.

`probes/ui_surface.py` also carried a write in its read-only plan: `PUT
/updater/check`. It now runs only under `--mutate`, beside the probe's other
writes.

Measured and not acted on: on kids `/hubs/sections/1?count=1` drops the Recently
Watched shelf, which fun does not, so there is no rule to follow. Kids' orphaned
collection tags carry a `guid`, but only 3 of fun's 372 tags do.

#### The seventh pass, second batch: python-plexapi as a third-party client

The second agent of cycle 7 drove python-plexapi at plexsim, the reference and fun:
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
  - plexsim answered 500 for every prefixed key, including the
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
  - plexsim compared the episode's own title for `show.title` and ignored `index`.
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
  - No Meta lists these fields, so plexsim ignored them.
  - `==` and `!==` are case-insensitive on every field (`title%3D=sintel` finds
    Sintel).
- **`/library/all` is a query.**
  - `type=` selects the rows, and filters and `sort=` apply.
  - The default order is the ratingKey.
  - A row carries `Media` only when a `type` was given.
  - plexsim answered all nine of its rows whatever was asked, and that is what
    `library.search(libtype=)` read.
- **Four reads python-plexapi makes that plexsim answered 404.**
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
  reference's six modern `tv.plex.*` rows match plexsim's exactly: `name`,
  `hasPrefs`, `id`, `Language` rows with a `title`, and no `primary`. The
  `primary`/`MediaType` shape the agent saw belongs to the legacy
  `com.plexapp.agents.*` plug-ins, which plexsim has left out on purpose since
  cycle 5.
- `/library/metadata/{id}/matches?manual=1` lists 20 candidates from the metadata
  provider, and plexsim's list is empty. Filling it needs a provider search per
  request.
- `thumbBlurHash`/`artBlurHash` under `includeFields` need image decoding, which
  the stdlib server path does not have.
- `guid=plex` is a prefix match and `slug=x` a 500 on the reference. The client
  filters on neither. (Overturned for `guid=` in the eighth pass: the client's
  availability lookup sends `/library/all?type=N&guid=`.)

#### The eighth pass: every path the web bundle can build

Cycle 8's first agent read the served client instead of a recording. It pulled
466 path literals out of `webclient/js` (328 distinct), resolved 152 GET templates
with each server's own ids, and fetched every one from kids, fun and plexsim, in
JSON and in XML. 37 templates differed in behaviour; they came down to 13
findings, and 12 were re-measured on both servers and acted on. Each is tied to
the code in the bundle that builds it.

- **The library subsets were the "All" grid.** `onDeck`, `recentlyAdded`,
  `newest`, `recentlyViewed` and `recentlyViewedShows` are the pivot bar's
  entries, and plexsim answered each with the library's top-level rows titled
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
- **By Folder never opened a folder.** plexsim listed one directory per item
  and ignored `parent`. Every folder therefore reopened the same list, and the
  view never reached an item. Measured:
  - `?parent=<id>` lists that directory. Subdirectories are rows of `key` and
    `title`. Files are the items they hold: listing rows, `<Video>` in XML, and
    an episode's has no Director, Writer or Role.
  - Rows come in file-name order. Firefly's episodes come by their s01eNN names.
  - An unknown or empty parent is empty. A non-numeric one is the top level.
  - The container carries `allowSync` and pages.

  plexsim's files sit flat under `/plexsim/<n>`, so its top level is the items
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
  plexsim ignored it, so every lookup "found" the whole library.
- **Four reads and five refusals.**
  - `/library/sections?includePreferences=1` puts each library's settings on its
    row. A show library's `episodeSort` is the per-show variant, whose enum starts
    with "Library default". The first-run flow reads `Preferences.Setting` off
    every row to find hidden libraries, and threw on plexsim.
  - `/:/prefs/get?id=` returns one Setting with an empty label and summary and no
    enumValues. It is 400 with no id and 404 for an unknown, empty or comma-joined
    id.
  - A comma-joined `/library/metadata/a,b` container carries `allowSync` and the
    FIRST row's library, even across two.
  - `/neighborhood/devices` is an empty container.
  - The refusals:
    - `/hubs/sections/{id}/manage?metadataItemId=` is a 400;
    - `/channels/recentlyViewed` and `/devices.xml` are the plug-in dispatcher's
      200 (plexsim answered its device list to the second);
    - `/media/subscriptions/template` and `/services/defaultLibraries` are 400s.

Thirteen checks cover these. Twelve are new, and all twelve fail against
`c62c51b` (743/755 there). The cycle-4 XML check that wanted `<Directory>` for
every folder row was re-measured and updated: a folder's files render as the
items they are.

Measured and not acted on:

- fun's `newest` stops at 47 movies and 60 episodes, with no bound visible in
  release or added dates; kids lists all five. plexsim lists every row.
- fun's Recently Viewed order follows neither `lastViewedAt` nor `viewCount`.
- Autocomplete orders by ICU collation with numeric runs, so 10, 12, 40, 300,
  2001 on fun. ICU collation stays out.
- On a show's `/children` at `Start>0`, kids drops All episodes and fun keeps it.
  plexsim follows kids.
- `guid==` answers nothing and `guid!=` everything; the client sends neither.
- `/library/all` with no `type` and `show.guid=…&season.index=1` answers the
  show; the client always sends `type=3`, which already works.
- `/system/services/url/lookup` is a 500 carrying the legacy plug-in
  framework's Python traceback.
- A folder level holding both subdirectories and files was not measured. No
  plexsim catalog has one, and its directory rows would render as `<Video>`.

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
  plexsim answered every row with no `totalSize`.
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
  - plexsim read only the paging headers, so the platform is now normalised from a
    header as well.
  - The same row carries the show's `thumb`, which batch 1 added.
- **`sort=movie.random`** (with a direction or a seed) sorts like `random`.
  python-plexapi always sends the prefixed form, and plexsim answered 500.
- **Studios and networks are tag tables.** `/library/tags?type=318` lists studios
  with a bare `=<id>` filter, ordered by id, and `319` lists networks.
  - plexsim's studios are text on the row, so it mints their ids from the name, as it
    does every other tag's.
- **A stream's `/levels` is a 404**, where the stream itself is the 501.
- **XML child order.** Both servers write a row's children in one order:
  - Media, Image, UltraBlurColors;
  - then Genre, Country, Guid, Rating, Collection, Director, Writer, Role, Producer,
    Label, CommonSenseMedia, Chapter, Marker, Review, Field and Location;
  - then Preferences, OnDeck, Extras, Related and Children.

  A listing's `Meta` comes before its rows. plexsim had 41 pairs inverted. No
  client was found that depends on it, and the order is cheap to keep.

Thirteen new checks cover these, and all thirteen fail against the batch-1 commit
(754/768 there). The studio check sets a studio through the Edit dialog's write first,
because an earlier check leaves the fixture movie without one. The cycle-5 check that
wanted no `parentYear` on a season's episode list was re-measured and updated.

Measured and not acted on:

- fun's postplay for a movie also carries `movie.extras` (clip rows), and for an
  episode a `tv.ondeck` shelf. Its related list adds `movie.same.actor.N` shelves.
  plexsim has no extras and builds only the similar shelf.
- A child element outside the measured list (Similar, Mood, Style) keeps its place
  after those in the list. Neither server showed one to place.
- plexsim's studio tag ids are minted, not a database's.
- The agent's reading that a yearless season in Recently Added lacks `year`,
  `titleSort` and `updatedAt` is cycle 7's season-year rule. A season with a year of
  its own carries all three.

#### The ninth pass: the community OpenAPI description, every GET in it

Cycle 9's read-only agent swept the community OpenAPI description of PMS (the
`plex-api-spec` behind plexapi.dev and the SDKs generated from it): 405 operations, 195
GETs aimed at the server, 132 of them instantiated as 403 requests and sent to the
reference, fun and plexsim in JSON and XML -- 2,406 exchanges, 1,205 differences
triaged. Twelve findings survived, each re-measured by hand on both servers before
anything changed.

- **`/library/sections/all`** is the section list itself, identical to
  `/library/sections` on both servers. The spec calls it `getSections`, so generated
  SDKs ask there; plexsim 404'd it.
- **Every MediaContainer pages.** With both container parameters present -- headers
  or query -- both servers slice and add `offset`/`totalSize` on `/`, `/library`,
  `/:/prefs`, `/devices`, detail rows, trees, every hub list and the tag table, and an
  empty container such as `/identity` gains `totalSize: 0`. A container of several
  lists slices each and counts `size` over them. Only a plug-in framework container
  (`noHistory`, e.g. `/system/scanners/1`) and a non-MediaContainer root are left
  alone. plexsim paged seven listings; the rule now lives in the response path.
- **`/system/agents/{identifier}` is a 404** with an empty body on both servers, for a
  real agent and an unknown one alike. plexsim answered the whole list.
- **Three refusal codes.** A stream's `/loudness` is a 404 (plexsim 501); `.srt` and
  `.vtt` on a stream the server has are the bare stream's 501 (plexsim 404, reading the
  extension into the id); `/library/metadata/{id}/file` without `url` is a 400
  (plexsim 404).
- **The livetv reads a tunerless server refuses.** `/livetv/epg`, `/epg/guide`,
  `/epg/search`, `/recordings`, `/dvrs/{id}` and `/sessions/{id}` (and all beneath
  them) are 404 on both servers and `/epg/channels` a 400; only `/livetv/dvrs` and
  `/livetv/sessions` answer empty. plexsim answered an empty container to all of them.
- **`/allLeaves` below the show is empty.** A season's is `size 0` in its episode-list
  container, naming the show by title only; an episode's is presented as its season's
  (title1 the season, an empty grandparentTitle, the episode layout). plexsim returned
  the season's episodes and the season layout. Every child list also carries
  `allowSync: true` on both servers, where plexsim sent false.
- **Voice search honours `type`** and numbers each hub by the type it holds:
  `results.search.4` is the episode hub whatever else matched, `type=4` returns it alone,
  and a type with no hub (5, 8, 18) returns none. plexsim ignored `type` and numbered
  hubs by position.
- **A section listing honours `excludeElements`.** Both servers drop the named
  children from every row, paged or not, and `Media` becomes `mediaCount` (0 on a show)
  plus `mediaCountOptimized` 0. plexsim applied the option to detail reads only.
- **Bandwidth statistics filter.** `accountID` and `deviceID` keep matching rows (an id
  with none is `size 0`), `lan=0|1` keeps remote or local traffic, and an empty or
  non-numeric id or any other `lan` value is a 400, on both servers. plexsim ignored all
  three, so the Dashboard's per-account and per-device graphs drew the whole server.
- **`/status/sessions/history/{id}`** reads one history row back: the row history/all
  lists, alone in a container of `size` 1; an unknown or non-numeric key is a 404. plexsim
  listed the key and 404'd it.
- **`/hubs/metadata/{id}`** is an item's own hub list: on both servers, for a movie, a
  show and an episode alike, the four artist shelves (Most Played, Most Popular Tracks,
  Albums, Music Videos), each empty and keyed to the item in its library. An unknown id
  is a 404. plexsim 404'd the path.
- **Six section leaves.** `allLeaves` lists every movie (sort-title order) or episode
  (broadcast order) as listing rows, with no librarySection fields and the library's
  type as viewGroup; `albums` is an empty "By Album" list; `cluster` and `moment` are the
  empty "By Moment" container with `clusterZoomLevel` 1; `computePath` is a 400 without
  both ids, a 404 for an unknown one and otherwise an empty path; `nearest` is a 400.
  All six were 404 on plexsim.
- **`/library/people/{id}`** reads a person back as one `Directory` row whose `filter`
  names the field the id came from (`actor=47`, `director=213`), and `/media` lists the
  items they are on as listing rows naming their library, with viewGroup `movie` even for
  shows. An unknown id is a 404. plexsim 404'd both, though every Role carries the id.
- **`/library/matches` answers**, where plexsim refused every request although its own
  `/media/providers` advertises the key. `type` is required and numeric; `title` matches a
  movie or show title exactly, ignoring case, narrowed by `year`, each row led by
  `score` 100; `guid` matches the whole guid and an episode matches by `grandparentTitle`,
  `parentIndex` and `index`, unscored; `type` alone is `size 0`.
- **`/hubs/items?identifier=`** answers one shelf's rows, uncapped unless `count` caps
  them, in a container of `size`, `allowSync` true and identifier. A missing or unknown
  identifier is a 404, and so are `tv.inprogress.{id}` and `home.playlists` although the
  hub lists carry them. plexsim 404'd every identifier.
- **`/sorts` is keyed by the asked `type`, not the library**, as `/filters` is: a movie
  library answers the episode sorts to `type=4` and the artist sorts to `type=8`, types
  9, 10, 13, 18 and 20 have lists, other numbers are empty and a non-number a 400.
  plexsim read the library's own Meta. The lists are `section_sorts.json`.
- **`/library/randomArtwork?sections=` and `/library/sections/{id}/arts`** list one
  `<Photo>` row per movie or show with a backdrop -- title and art key, every other field
  blank or -1 -- the first shuffled on each request and capped at 100 in a bare `size`
  container, the second in ratingKey order in the library's container. plexsim 404'd both.

Measured in the ninth pass and not acted on:

- `/services/ultrablur/image` renders a four-colour PNG and `/colors?url=` decodes an
  image into its corner colours (both 400 without parameters). plexsim's server path is
  stdlib only, with no image encoder or decoder -- the same limit as `thumbBlurHash`.
- `/library/metadata/{id}/file?url=` is a 400 on the reference and a 404 on fun; only
  the bare form, which both refuse with 400, is implemented.
- An episode's `/allLeaves` container disagrees between the servers on `parentTitle`,
  `parentYear` and `art`; only the fields both send were changed.
- The reference's `/status/sessions/history/all` lists nothing while
  `/status/sessions/history/1` still reads back; the two servers' history filtering by
  account was not pinned down.
- `/hubs/items` answers the genre and actor shelves (`movie.genre.1.23`,
  `movie.by.actor.or.director.1.215`) that plexsim's library hubs do not build.
- `/library/matches` options that consult a metadata provider
  (`includeAlternateMetadataSources`, `agent=`) change nothing on either server's local
  matches and are ignored.
- Paging on the write-shaped reads (play queues, decisions, timelines) could not be
  measured on the read-only reference; `_page_container` leaves them to their handlers
  only where those already page.


#### The ninth pass, second batch: writes, measured on a disposable copy of the reference

The reference is read-only for hunts, so every write here was sent to plexscratch -- the
reference's own library, copied, sanitised and cut off from plex.tv -- and to plexsim with
the same parameters, then read back from both.

- **The Edit dialog's save path MERGES tags; only the item path replaces them.**
  `PUT /library/sections/1/all?type=1&id=4&genre[0].tag.tag=Alpha&genre[1].tag.tag=Beta`
  on a movie with two genres leaves four: Alpha, Beta, then the originals in their order.
  On that path the indexed list is deduplicated case-insensitively (`Same&same` is one
  tag), a `tag-` removal removes exactly the names it lists and ignores ones the row lacks
  (the client double-encodes them, `Label%2520A%2CLabel%2520B`), and an empty indexed
  value changes nothing. On the item path the list is still replaced; there an exact
  repeat is one tag and `Qx&qx` is the first spelling twice. Both order by the INDEX
  (`genre[1]=Second&genre[0]=First` is First, Second); plexsim replaced on both paths, in
  parameter order.
- **An unlocked Sort Title does not stick.** A title write, a sort-title write and a
  sort-title unlock each re-derive it from the title with the article stripped:
  `title.value=The Thing&titleSort.value=zzz` reads back `titleSort: Thing`, and a season
  retitled `The Season Name` sorts as `Season Name`. Only a lock -- sent with the value,
  or earlier -- keeps what was typed, and an empty title leaves it alone. A collection and
  a playlist edited through `/library/metadata/{key}` follow the same rule, and a
  collection records the lock. plexsim stored every value it was sent.
- **The save path edits only rows of the asked type in its own library.** A movie id
  under `type=2`, or on the TV library's path, is a 200 that edits nothing, and
  `id=<movie>,<show>` with `type=1` edits only the movie. A type-SCOPED filter on that
  path -- `show.title=`, `show.year>>=`, even `movie.year>>=` with `type=1` -- is a 400,
  where an unscoped `year>>=` still scopes the edit. plexsim edited every id it had and
  applied the filter.
- **A release date is parsed, and on the section path it sets the year.** Value by
  value on the item path: `2007-2-3` is `2007-02-03`, `2007-02-30` rolls over to
  `2007-03-02`, while `2001-01-45`, `2001-00-10`, `2001/02/03`, `2001-02`, a leading space
  and trailing text are removed; a trailing time is dropped with no zone shift,
  `99-01-02` is `0099-01-02`, and a bare integer is epoch seconds (`20070203` is
  `1970-08-21`, `-5` is `1969-12-31`). The section path refuses what it cannot read with
  a 500, sets `year` from the date unless the same save writes one (which wins in either
  order), removes both on an empty date, and removes an empty year where the item path
  500s. A year of 0 is removed and `+2000` is 2000. plexsim stored dates verbatim and left
  the year alone.
- **The section path stores a userRating, and every rating is a number.**
  `userRating.value=8.5` there reads back `userRating: 8.5` with `lastRatedAt`, -1 or an
  empty value clear both, and `abc` is a 500 -- where the item path still discards it.
  `audienceRating.value=5.5` and `rating.value=5.5` read back as numbers on both paths,
  `abc` is a 500 on both, and an empty value is a 500 on the item path and removes the
  field on the section path. plexsim stored the strings and discarded userRating.
- **A lock on the section path is an integer 0 or 1, and more columns take one.** There
  `01` and `001` lock, `00` unlocks, and `true`, `false`, `2`, `-1`, ` 1` and an empty
  value are a 400; the item path keeps its rule that only `1` locks. `thumb`, `art`,
  `theme`, `banner`, `guid`, `slug`, `addedAt`, `viewCount`, `network`, `parentIndex`,
  `duration` and `index` all take a lock (`key`, `ratingImage` and `audienceRatingImage`
  are 400), and `Field` lists in the metadata table's column order -- `rating` after
  `studio`, the artwork after `contentRating`, the tag columns among the scalars --
  where plexsim filed everything it did not know last.
- **Frames title a row the way the Activity pane names it, and a batch frames in listing
  order.** A show's frames say `Firefly (2002)` (a movie's already carried the year, and
  an emptied movie title still does: ` (2010)`), a season's `Firefly S01` and an
  episode's `Firefly S01 E07` -- the show's title and the current indexes. A save through
  the section path pushes every row's state 3 before any state 5, ordered by sort title
  whatever order `id=` named them in, where a comma-joined key list keeps the per-row
  pair. Renaming a playlist or editing its summary, through `/playlists/{id}` or
  `/library/metadata/{id}`, pushes its own type-15 pair with sectionID -1, and a
  collection's rename its type-18 pair. plexsim sent every row's own title, interleaved
  the batch, and pushed nothing for a list.
- **A parent's title and studio are copied onto the rows beneath it.** Retitling a season
  changes its episodes' `parentTitle`, and its studio their `parentStudio`; retitling a
  show changes its seasons' `parentTitle` and every episode's `grandparentTitle`, and its
  studio the seasons' `parentStudio` -- on the detail row, the child list and the grid.
  An emptied show title removes those fields rather than sending "". A season's episode
  list names a custom season title in `parentTitle` and carries the season's `summary`
  when it has one: every season the earlier reading saw, on kids and on fun, had a
  generated `Season N` title and no summary, which is why it recorded "" and no summary
  -- re-read on both servers here, and it still holds for those seasons. plexsim copied
  the fields once, at load, and updated only the containers.
- **An emptied title is not always empty, and it keeps its sort form.** `title.value=` on
  a season leaves `Season 1` and on an episode `Episode 2` -- generated from the index,
  so a later `index.value=7` makes it `Episode 7` -- while a movie, a show, a playlist and
  a collection store "". Every one keeps its old title's sort form as `titleSort` (`The
  Old List` sorts as `Old List`, `Bushwhacked` as `Bushwhacked`). A playlist or a
  collection created as `The Fresh List` already sorts as `Fresh List`, and
  `PUT /library/collections/{id}?title=` empties the title, where plexsim ignored an empty
  one. plexsim stored "" everywhere else and dropped the sort title.
- **`/:/progress` stores any position past a minute and never completes a play.**
  `time=999999999` on a ten-second clip reads back as `viewOffset: 999999999`, with no
  `viewCount` and no history row, and a missing or unparseable `time` is a 500. plexsim
  marked the item watched and wrote a history row, and stored 0 with a 200 for a
  missing time.
- **Only -1 clears a rating's timestamp.** `/:/rate?rating=-5` removes the stars and
  keeps `lastRatedAt`; `-1` removes both. plexsim cleared both for any negative rating.
- **A timeline refuses a position it cannot place.** A `time` past the `duration` sent
  with it, a negative or non-numeric `time` and a non-numeric `duration` are each a 400.
  plexsim answered 200 -- and a time past the duration completed the play and wrote a
  history row. A timeline with NO `duration` answered `ignore` on plexscratch, but its
  clips are ten seconds long, so that cannot be told apart from a position past the
  item's own length; it was not acted on.
- **A completed play answers `complete` until a `stopped` beat closes it, then `ignore`.**
  Without a session identifier: `playing` at 95% then `stopped` both answer `complete` and
  a second `stopped` answers `ignore`; `playing` twice past 90% then `stopped` all answer
  `complete`; `stopped` at 98% then `playing` answers `ignore`. Each counts one view.
  plexsim answered `complete` to every one of them -- and the first reading here, two
  `stopped` beats alone, was fixed as "any second beat is `ignore`", which the existing
  playing-then-stopped check caught. With an `X-Plex-Session-Identifier` plexscratch
  ignored every beat and counted nothing; its clips are ten seconds long, which a
  session-bound timeline appears to be judged against, so none of this was measured
  with one.
- **Remove from Continue Watching is a PUT that needs its `ratingKey`.** A GET is a 404
  and a PUT with no `ratingKey` a 400; plexsim hid the row on a GET and 404'd the bare
  PUT.
- **A playlist create validates its form.** No `title`, no `type` or no `uri` is a 400,
  a type outside video/audio/photo a 500, and `type=audio` or `type=photo` over a movie a
  400. plexsim created an `Untitled` list of whatever it was sent, typed `bogus` if asked.
- **A playlist's item writes refuse what they cannot do, and DELETE on the items clears
  it.** An item id the list does not hold -- including a ratingKey, which is not an item
  id -- is a 404; `DELETE /playlists/{id}/items` empties the list; and a smart playlist
  refuses an add or a removal with 400. plexsim answered 200 to all of them and cleared
  nothing.
- **A move after an item the playlist does not hold goes to the front.** `after=999999`,
  `after=0` and `after=abc` all send the item first, as no `after` does, and `after`
  naming the item itself changes nothing. plexsim appended to the end, and moved an item
  after itself to the end too.
- **Every state write needs the `identifier` the client sends.** Without it
  `/:/scrobble`, `/:/unscrobble`, `/:/rate` and `/:/progress` are a 400 -- even for a key
  the server does not have, so the earlier reading of a 404 for an unknown key holds only
  with the identifier present -- and a foreign identifier is a 404. The order is fixed: a
  missing key, a missing identifier, the write's own parameter (an unparseable rating is
  still a 400 with a bogus identifier), then a foreign identifier or an unknown key.
  plexsim never read the identifier.
- **An episode scrobble counts on its season and show; a parent's own scrobble does not.**
  One scrobble at a time: each episode scrobble added one to the season's and the show's
  `viewCount`, a second scrobble of an episode already watched included; scrobbling the
  season, and the show, added one to each episode and left the season's and the show's
  own count unchanged; unscrobbling a season or a show removed its own count, and
  unscrobbling an episode left the parents alone. plexsim counted on a parent only when
  the parent itself was scrobbled -- the earlier note ("scrobbling one episode twice took
  the show to viewCount 2") implemented the wrong way round.
- **On Deck holds one row per show.** Two episodes in progress list only the
  lower-numbered one -- episodes 13, then 12, then 14 left 12 on deck, and 13 played again
  at 240 s did not displace it -- and with none in progress the row is the episode after
  the one watched (episode 8 watched: 9; season 1 watched: season 2's first). Removing any
  of the show's episodes from Continue Watching hides the show's row, even when the row
  names another episode, and a timeline on any of them brings it back. plexsim listed
  every episode with a resume point, and never the next one.
- **An item takes no POST, and `/library/all` takes no write.** `POST
  /library/metadata/{id}?summary.value=` and `PUT /library/all?type=1&tagline.value=` are
  404s; plexsim answered the detail row and the listing.
- **The Edit dialog's value-less columns, and a few others, are accepted and discarded.**
  `thumb.value=`, `art.value=`, `addedAt.value=`, `guid.value=`, `lastViewedAt.value=`
  and `viewCount.value=` are 200s that change nothing, and so are a movie's
  `duration.value=` and `index.value=`, an episode's `parentIndex.value=`, a show's
  `network[0].tag.tag=` and a bare `genre=Drama` on the item path; an actor written with
  `actor[0].tagRole=` is stored with `role: ""`, the field every actor added by an edit
  carries. plexsim refused most of them as unknown fields and stored the movie's
  duration and index.
- **A play queue starts on the item its `key` names.** A queue over a whole library with
  `key=/library/metadata/3` selects that movie (offset 3); plexsim selected the first row.
- **A client's new play queue retires its older ones.** Client A's queue was still
  readable after client B created one, and a 404 once A created another. plexsim kept
  every queue it had ever built.
- **`PUT /playQueues/{id}?playlistID=` adds the playlist's items**, as a plain add;
  plexsim stepped the version and added nothing.
- **DELETE on a play queue's items clears it down to the selected item**, stepping the
  version; plexsim answered 404.
- **Deleting a play queue's selected item answers the queue as it stood.** The response
  carries the old version, selection and total with no rows, and the next read has the
  selection on the item that followed it; plexsim left the selection on the deleted id.
- **A play queue move after an item the queue does not hold goes to the front.** `after=0`
  and an unknown id both send the item first, as `after=-1` does; plexsim answered 404.
- **A windowed play queue read honours `includeBefore` and `includeAfter`.** Both are on by
  default; with the first item selected, `window=1&includeBefore=0&includeAfter=1` answers
  only the second row, `includeBefore=1&includeAfter=0` answers none, and the selected
  item comes back only when both are on. plexsim ignored both flags.
- **A play queue uri naming another server builds an empty queue that echoes it.** The
  response has no rows and `playQueueSourceURI` is the uri as sent; plexsim resolved the
  path against its own library and queued its own item.
- **A collection create needs a `title` and a `sectionId`.** Each missing one is a 400;
  plexsim made an `Untitled` collection in the first row's library. A missing `uri` is a
  200 on both servers.
- **A collection over a show holds the show, and one over a season holds the season.**
  `type=2` from a show answers `subtype: show` with one child, `type=3` from a season
  `subtype: season`; plexsim expanded both into episodes (`subtype: episode`, the show's
  three).
- **A collection add refuses what it cannot hold.** No `uri`, an unknown key, and a row of
  another type -- an episode into a movie collection -- are each a 400, and so is any add
  to a smart collection, whose members are its query. plexsim added whatever the uri
  named, the episode included.
- **A collection's children follow its `collectionSort`, and a move starts from the order
  shown.** Three movies added newest first came back oldest first -- by release date, with
  no sort set or `0` -- and `2` is the custom order. A move re-bases the custom order on
  the order the collection is SHOWN in, applies the move, answers the collection row, and
  switches the collection to `collectionSort: 2`; setting `0` again goes back to release
  date. (`1` gave the same order on these members, and is alphabetical by
  python-plexapi's mapping.) plexsim listed insertion order, answered the move with an
  empty body, and never changed the sort.
- **A collection takes a Label, and a `type=18` section save edits it.**
  `PUT /library/metadata/{collection}?label[0].tag.tag=` stores the label, and `PUT
  /library/sections/1/all?type=18&id={collection}&summary.value=` edits the collection;
  plexsim ignored both.
- **Renaming a collection leaves its members' tag alone.** After renaming `c13` a member
  still carried its original Collection tag; plexsim renamed the tag with the collection.
  (The reference spelled that tag `C13` for a collection created as `c13`, which was not
  pinned down.)
- **A library edit applies the whole form, and scans.** `PUT /library/sections/1?...&language=de-DE
  &prefs[enableBIFGeneration]=0` reads back `language: de-DE` and the setting false, and
  pushes the scan: a `library.update.section` activity started as `Scanning`, a status
  `Scanning the "Movies" section`, the activity retitled `Scanning Movies` and stepped
  through every item with a `progress` frame (`Scanning Cosmos Laundromat (2015)`) and an
  `updated` frame at `round(n * 99 / count)` percent, then 100, `ended`, and the status
  `Library scan complete`. Hunter A's "a type change is a 400" did not reproduce: both
  servers answer 200. plexsim renamed the library and dropped the rest.
- **`/library/sections/all/refresh` scans every library.** It is a 200 that runs the scan
  above for each library in turn; plexsim 404'd it as an unknown section, and scanned a
  single library by starting and ending a metadata activity per row.
- **A movie's written setting shows on its row.** After `PUT
  /library/metadata/{movie}/prefs?useOriginalTitle=1` the movie's detail row carries
  `useOriginalTitle: "1"`, as a show's does for `episodeSort`; plexsim stored it unseen.
- **A library create applies its `prefs[...]` and answers a bare container.** A TV library
  created with `prefs[flattenSeasons]=1&prefs[episodeSort]=1` reads both back as 1, and the
  201's container carries only `size` beside the Directory; plexsim dropped the prefs
  (-1 and 0) and sent the library container's identifier, media-tag pair and allowSync.
- **A library analyze pushes three frames per movie.** For each movie in title order: state
  5 with `mediaState: analyzing`, state 5 with `thumbnailing`, and a plain state 5.
  plexsim pushed nothing.
- **Cleaning bundles and deleting a library each run an activity.** `PUT
  /library/clean/bundles` runs `general.bundle.clean` -- started, updated at 0 (`Analyzing
  metadata bundles`), 25 and 50 (`Deleting N metadata bundles`), 50 (`Analyzing media
  bundles`), 75 and 100 (`Deleting N media bundles`), ended -- and a library delete
  `library.delete` (`Deleting section N`, then `Optimizing Database` at 50, 0, 10, 40,
  60, ended). plexsim pushed nothing. Measured and not acted on: optimize's
  `general.db.optimize` run (its last frames and a burst of type-12 deletes were not fully
  captured) and the refresh cascade a show's `showOrdering` write pushes.
- **A merge refuses what it cannot merge.** No `ids` is a 400, an id the server does not
  have a 404, and a movie into a show a 400. plexsim answered 200 to all three -- and the
  last one moved the movie's media into the show.
- **A merge and a split push their frames.** Merging Sintel into Tears of Steel pushed
  state 9 for Sintel (untitled) and then `type: -1` state 3 and 5 for Tears of Steel; the
  split pushed the new row created -- state 0 untitled, state 0 titled, state 1 -- and the
  source's state 3 and 5, on a NEW ratingKey never used before (67 after 57). plexsim
  pushed nothing for either, and put the split's row on the highest FREE key -- the one
  the merge had just taken from the absorbed row.
- **Unmatching an episode or a season gives it a local guid and its generated title.** An
  episode became `Episode 2` on `guid: local://9`, kept its imdb/tmdb/tvdb Guids and pushed
  `matching` (state 2) then state 5; a season became `Season 1` on `local://7`, through the
  full refresh sequence. The earlier reading -- a filename title and a
  `tv.plex.agents.none://` guid -- was a movie's and still stands for movies. plexsim
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
  nothing; `continuous=1` stopping at one episode (plexsim's fixture season holds one).
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

plexsim builds every `Media`, `Part` and `Stream` from an ffprobe probe, and every official
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
  decoders are `hdmv_pgs_subtitle` and `dvd_subtitle`. plexsim passed ffprobe's names
  through, and a client decides whether to burn an image subtitle in by that name.
- **Audio codecs carry Plex's names.** Every DTS variant is `dca`, with the variant as the
  profile -- `dts`, `ma`, `es`, `hra`, `express` (34 tracks); PCM in Matroska is `pcm` with
  ffprobe's sample format as the profile, while Blu-ray PCM stays `pcm_bluray`; and the
  `+ Dolby Atmos` profile ffprobe gives TrueHD and E-AC-3 is not written (11 tracks). plexsim
  sent `dts` and the Atmos profile, and a client's direct-play profile names `dca`. The
  reference (1.43.4) leaves `lc` off most of its AAC clips where fun writes it; the check
  skips those.
- **A language is named in itself, coded ISO 639-2/T and tagged ISO 639-1.** `jpn` reads
  `日本語`/`jpn`/`ja`, ffprobe's `fre` reads `Français`/`fra`/`fr`, `fil` keeps `fil` as its
  tag, `und` writes nothing, and a tag that is no ISO code (`english`) is both name and tag
  with an empty code. plexsim printed English names for 15 languages and the raw code
  otherwise, kept bibliographic codes, and cut the tag to two letters: `jp`, `sp`, and `fi`
  -- Finnish -- for Filipino. A client picks a track by the tag. `languages.json` holds the
  62 names and tags fun wrote; a code it lacks keeps the code in all three. The reference
  (1.43.4) labels an untagged clip English where fun writes nothing; the check skips those.
- **Every video stream is listed, and Dolby Vision is described.** A dual-layer Dolby Vision
  file's enhancement layer is a second video stream on fun; plexsim built only the first. A
  Matroska cover image, which ffprobe also lists as a video stream, is not one.
  The eight `DOVI*` attributes are ffprobe's DOVI configuration record field for field (6
  of 6 files, profiles 5, 7, 8 and 10), and plexsim sent none of them -- they are what tells
  a client a Dolby Vision stream it can play from one it cannot.
- **A ten-bit stream is ten-bit.** ffprobe leaves `bits_per_raw_sample` out for HEVC and
  AV1, where the depth is only in the pixel format; all 16 such streams read `bitDepth: 10`
  on fun. plexsim defaulted them to 8.
- **The resolution label is the larger of a width bucket and a height bucket.** Measured on
  all 4947 Media of fun's movie libraries with no exception, `2k` and `2.7k` included. plexsim
  read the height alone, so a cropped 1920x800 release was `720`. Each threshold is the
  smallest dimension fun has in its bucket; `tests/fixtures/media-resolutions.json` holds
  every size fun has and its label.
- **The aspect ratio is one of six values.** Every Media on fun carries 1.33, 1.66, 1.78,
  1.85, 2.2 or 2.35: the display aspect, sample aspect ratio included, snapped to the
  nearest. plexsim wrote the raw quotient without the sample aspect ratio, so 1920x800 read
  2.4 and an anamorphic 3840x2160 1.78 where fun says 1.85.
- **Track flags are the disposition, and only when true.** `default`, `forced`,
  `hearingImpaired`, `original`, `dub` and `comment` on audio and subtitles, and `default`,
  `forced` and `original` on video, follow ffprobe's disposition on every probed track, and
  no flag is ever `false`, `selected` included. plexsim marked the first audio track default,
  sent `false` on the rest, and none of the other flags. Which track is `selected` follows
  the account's language settings, which plexsim does not have; the first still is.
- **`scanType` follows the field order**: absent where ffprobe reports none (19 streams),
  `interlaced` for a field-ordered stream, `progressive` otherwise. plexsim wrote
  `progressive` everywhere.
- **`colorRange` is ffprobe's `color_range`** (22 streams carry it, 25 do not); plexsim
  never sent it.
- **`audioChannelLayout` is ffprobe's layout verbatim, and absent where it has none.**
  plexsim derived it from the channel count, so a `5.1` track was `5.1(side)` and 3, 4, 5
  and 7 channels had none. Left alone: E-AC-3 with Atmos in an mp4, which fun reads as its
  5.1 core; one DTS Express track; and the reference's mono AAC clips.
- **Audio `bitDepth` is `bits_per_raw_sample`, and 24 for DTS without it** (DTS Express
  excepted); lossy codecs carry none. plexsim sent no audio bit depth. Left alone: TrueHD in
  an mp4, which fun leaves without one.
- **`streamIdentifier` is the container's track id in decimal** -- an mp4 track number, an
  MPEG-TS PID -- and absent in Matroska. plexsim sent none. An AVI track that fun numbers
  with no id in the probe is left alone.
- **A video stream's `codecID` is its codec tag** where the container has a real one --
  `hev1`, `hvc1`, `dvhe`, `XVID`, `DX50`, `HDMV` -- but not `avc1`, and no audio or subtitle
  stream carries one. plexsim sent none.
- **An H.264 stream's `codedHeight` is whole macroblocks** (1080 is 1088, 436 is 448, 536
  is 544; 12 streams on fun and 11 on the reference), where ffprobe 8.1 reports the cropped
  height. Other codecs take ffprobe's value.
- **A video stream has a language and a title too**, from its tags (17 and 10 streams on
  fun); plexsim set neither.
- **Unknown markers are not written.** ffprobe's `profile: 0` and `level: -99` are absent on
  fun, and `Simple Profile` is `simple`; plexsim wrote them as they came.
- **Bitrates round to the nearest kbps.** plexsim truncated, so 28 of 35 Media read one
  short. Two fun files whose bitrate is neither, and the reference's clips, are left alone.
- **The frame rate is the average one.** The reference's mp4 clip with a nominal 48 fps
  reads `23.947` and `24p`, ffprobe's `avg_frame_rate`; plexsim used `r_frame_rate`.
- **The Media's audio fields name the track with the most channels**, the first on a tie
  (35 of 35 files; the first track was the wrong one in 9), and a DTS-HD MA track is
  `dca-ma` there, as on 2,075 of fun's 4,951 Media, while every other DTS variant stays
  `dca`. plexsim named the first track, and a client gates direct play on these fields.
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
  plexsim printed `sdp`, `Dolby Digital Plus`, `3ch`, English language names and the track
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
  not vary. The recorded `displayTitle` of `1080p` was the no-product answer, and plexsim
  sent it to Plex Web itself.
- **A catalog built before this pass loads with the corrected fields.** The resolution
  label, and an aspect ratio that is not one of the six, are recomputed from the Media's own
  size; the unknown profile markers are dropped; the codec, language and flag renames above
  apply to stored rows; and the titles are composed after all of them, where plexsim had
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
- **Which audio track is `selected`** follows the account's language settings, which plexsim
  does not have; the first track stays selected.
- **Leads, one sample each:** a `.webm` file reported as container `mkv` (plexsim keeps
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
  for an extra it has. plexsim 404'd the first three and answered the last.
- **A settings save pushes a `preference` frame per written id**, in ASCII order of the id and
  whether or not the value changed, carrying the `/:/prefs/get` shape with `label` and
  `summary` emptied. **And `FriendlyName` is the server's name everywhere** -- `/`,
  `/media/providers` and `/servers` -- with an empty one kept as the preference's value while
  the displayed name falls back to the host name. plexsim stored the values, pushed nothing,
  and kept its old name, so renaming the server in Settings changed nothing on screen.
- **Track selection takes a POST as well as a PUT** on `/library/parts/{id}`; plexsim 404'd
  the POST.
- **A butler run is an empty 200 that pushes a `butler` activity.** `POST /butler/{Task}`
  answers no body and pushes one "Butler tasks" activity (started at -1) around the task's
  own -- `general.db.backup` "Database"/"Backing up database" stepping to 100 -- ending with
  the task's message ("The butler backed up your database."); `/activities` carries the
  `butler` row while it runs -- plexsim's run is synchronous, so only the frames are visible
  here, and nothing is left in `/activities` afterwards. `POST /butler` runs every enabled
  task under one wrapper and
  `DELETE /butler` is empty too, while `DELETE /butler/{Task}` is a **404**. plexsim answered
  the task LIST to all three, pushed no frames, and accepted the task DELETE.
- **The Edit-smart-list Save is `PUT {list}/items?uri=<listing>`, and it REPLACES the stored
  query -- `limit` included.** Measured for a smart playlist and a smart collection alike: the list is
  re-evaluated on the next read, so its `leafCount` and its items follow the new `sort` and
  `limit` at once, and the response is the list's own row. A uri naming an ITEM, and no uri,
  are 400s, and a smart playlist's items cannot be cleared (`DELETE` is a 400 where plexsim
  emptied a list it never reads). plexsim refused every uri on a smart list, so Save answered
  "There was a problem updating this item."
- **An artwork upload is `POST /library/metadata/{id}/{posters,arts}` with the image as the
  body, and it answers an EMPTY 200.** The image becomes an `upload://posters/<sha1 of the
  bytes>` candidate -- a season's and an episode's carrying their place in the path -- and is
  selected at once; the row's `thumb` AND `art` both re-version; and one timeline frame,
  state 5 with no `metadataState`, is pushed with no state 3 before it. **And picking any
  candidate stores a NEW versioned path**, never the url picked: plexsim wrote the raw value
  into `thumb`, so choosing an upload set the tile's img src to `upload://posters/...`.
  A playlist's and a collection's artwork list lives under `/library/metadata/{ratingKey}`
  too and holds one `default://` composite row; plexsim 404'd it.

### Emulation calibrated against the account's own claimed servers

Rather than guessing what a claimed server reports, `/identity`,
`/media/providers` and `/myplex/account` were pulled from **fun and zen** — both
genuinely claimed, on the same account — and diffed field by field. Result:
**36 fields identical, 24 differing and every difference deliberate.**

What the diff corrected, all of it invented by plexsim beforehand:

| field | real servers | plexsim had |
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

The 24 remaining differences are all one of three kinds: plexsim is a different
server (`machineIdentifier`, `friendlyName`, `version`, `platformVersion`), it
has **no encoder** (every `transcoder*` field, `offlineTranscode`), or it
**owns no files** (`allowMediaDeletion`, `allowSync`, `allowCameraUpload`,
`allowTuners`, `livetv`). Those are kept false on purpose: advertising a
transcoder plexsim does not have is what produced "This server is not powerful
enough to convert video" in the first place.

Playback re-verified after all of it: direct play, 1920x1080, 0 dropped frames,
banner gone.

### On the claim banner

`--assert-claimed` makes plexsim report a claim it does not hold. It is named
for what it is. The alternative — a real `POST /api/claim/exchange` — is what
reset the account's tokens and signed fun and zen out of plex.tv earlier the
same day, so it is not worth re-running for a cosmetic banner.

It works only because the **locally served** web client reads claim state from
plexsim's own `/identity` and `/myplex/account`. `app.plex.tv` reads plex.tv
instead and is not fooled, which is why the banner persisted there even when
plexsim claimed to be claimed.

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

Code: `plexsim/` (scratchpad, not yet promoted to a repository).

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
parses — browses plexsim and reaches a playable URL. 38/38 checks, including
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
| through plexsim (`proxy`) | `e49cf18e698064cddc10a13ea0926fb3` |
| through plexsim (`redirect`) | `e49cf18e698064cddc10a13ea0926fb3` |
| straight from the RD CDN | `e49cf18e698064cddc10a13ea0926fb3` |

mpv direct-plays it: `h264 1920x1080 23.976fps` + `eac3 6ch` + all 4 subtitle
tracks, seeked to 20:00.

**5. Registration works.** The pd_inject trick still works in 2026:
`PUT https://plex.tv/devices/{id}?Connection[][uri]=...`. plexsim appeared in
`/api/v2/resources` — the list every client reads — alongside fun and zen.

**6. The whole chain, end to end, over a real public URL.** `discover.py` —
python-plexapi driving the same path every official client takes — scored
**14/14**: plex.tv sign-in → resource discovery → connect to
`https://plexsim.example.com` (0.23 s) → browse → 206 with the true total size.
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
arrived at plexsim as **no Range at all** (`PART 10010 range=- -> 200
len=1838536254`). Cloudflare collapses a prefix range into a full origin fetch,
so plexsim served the entire 1.8 GB and Cloudflare pulled it for 39 s before the
client gave up. A *mid-file* range (`bytes=919268127-…`) passed through correctly
as 206. So every playback start would drag the whole file out of Real-Debrid —
ruinous, since bandwidth is the entire cost line.

`--part-mode redirect` removes the problem completely: plexsim answers 302 with
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
plexsim itself.

One anomaly remains unexplained: a dropout roughly 2-4 minutes after the very
first PUT, far inside the lease. That PUT also rewrote name, platform and
connections at once, so propagation of a larger mutation is the plausible guess;
it was never reproduced.

**3. "Unclaimed and not secure" is an identity mismatch, not a TLS problem.**

The Plex web app showed plexsim as *"unclaimed and not secure"* despite a valid
public certificate. Three fields were at fault:

| field | wrong value | correct |
|---|---|---|
| `machineIdentifier` | `de407dc4…`, which did not match the device row's `clientIdentifier` (`zurg`) | must **equal** the device row's `clientIdentifier` |
| `certificate` | absent | `1` — plexsim's TLS terminates at the CDN, so it has to assert this itself |
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

The Plex web app reports *"plexsim is currently unavailable"*. Cause, from
`/api/v2/resources`:

| field | fun / zen | plexsim |
|---|---|---|
| `presence` | `true` | **`false`** |
| `accessToken` | the owner's token | **`null`** |

A client builds its request URL from the resource's `accessToken`. With `null`
there is no credential to use, so the server reads as unreachable. This is the
single thing standing between plexsim and the official clients.

What was ruled out, in order:

1. **Identity mismatch** — fixed (`machineIdentifier` == `clientIdentifier`).
   Did not change presence.
2. **plex.tv probing the connection** — it does not. Zero inbound requests
   reached plexsim or cloudflared from Plex during the whole session, so
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
   for plexsim.

### The actual gate: two separate registries

| | fun | plexsim |
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
is not in fun's current logs because fun was claimed long ago.

**The definitive experiment:** run a real PMS in a container on zen (the test
host), claim it with a fresh claim token, and read its **first-run** log. The
call that creates `/api/servers/{machine}` will be in that startup sequence.
Bounded, and on a test host rather than production.

**4. Sharing: one owner claims, everyone else is shared — and the owner's Plex
Pass covers them all.**

"Claimed" binds a server to exactly **one** owner account. Multiple users is not
multiple claims; it is Plex sharing: the owner invites accounts as friends or
Home users, each gets its own per-user access token, and they see the server with
`owned=false`.

This resolves the multi-user question left open earlier: **a server owner's Plex
Pass covers remote streaming for every user on that server**, so one lifetime
pass on plexa630 covers all shared users and nobody needs a Remote Watch Pass.

Limits that still bite: plexsim currently accepts any token and keeps no
per-user state, so real sharing needs per-user token handling and per-user
views; Plex Home caps at 15 managed users; and friend sharing requires each
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

plexsim has no encoder, so a transcode verdict is not a downgrade — the item
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
`https://enter-macbook.drum-dab.ts.net:8443`, real Let's Encrypt cert, HTTP/2.
Correct ranges and content type verified through it.

**This was the wrong substitute.** `tailscale serve` is tailnet-only;
`tailscale funnel` is the public one, and it hung when invoked (Funnel needs
enabling in the tailnet policy). So plexsim was never publicly exposed on that
path. Since removed (`tailscale serve --https=8443 off`).

### What actually serves it: a named Cloudflare tunnel

| | |
|---|---|
| Hostname | `plexsim.example.com` (proxied CNAME, record id `<dns-record-id>`) |
| Tunnel | `boiler-room-app`, `<tunnel-uuid>` |
| Config | `plexsim/cloudflared-plexsim.yml` — **not** `~/.cloudflared/config.yml` |
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
plexsim/cloudflared-plexsim.yml` process. Nothing else persists.

## Collateral change to the Plex account — needs a decision

`PUT /devices/{machineIdentifier}` **did not create a new device.** plex.tv
resolved it to a **pre-existing** row and overwrote it:

- device id `1366743689`, `clientIdentifier="zurg"`, `product="zurg"`,
  `provides="server"`, `createdAt=1751239317` (2025-06-29)
- overwritten: `name` → `plexsim-spike`, `productVersion`, `platform`,
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

**Decision needed:** delete device `1366743689`, or leave it as plexsim's row.

## Not yet done

- **A GUI client.** Nothing is blocking it now — plexsim is live at
  `https://plexsim.example.com` and discoverable on the plexa630 account. Next:
  plezy (open source, debuggable on desktop), then the web app, then Android TV.
  The open question is whether official clients follow the 302 and tolerate RD's
  `application/force-download` content type.
- Real artwork; `/photo/:/transcode` returns a generated solid-colour PNG.
- Multi-user: each user needs their own Plex Pass, or plexsim runs on their LAN
  and advertises a local connection. plexa630 has a lifetime Pass, so the spike
  did not hit the remote-playback gate.

## Reference material worth keeping

[plx-native](https://github.com/GLinnik21/plx-native) ships `tests/mock_pms.py`
(708 lines), `docs/pms-api.md` (870 lines, "verified against live server", PMS
1.43.x, dated per section), `docs/plex-openapi.json` (1.3 MB),
`docs/pms-hls-protocol-probe.md`, `docs/shared-servers.md` (74 KB). That is the
best PMS documentation found, and its mock is the skeleton plexsim grew from.
