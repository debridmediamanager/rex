# The feed API: sources push items to rex

rex does not list, scan or query any library. A **source** (zurg, or anything else
that serves files over HTTP) pushes the items it holds, and rex turns
them into library rows on keys that never change. The feed is rex's own API, not an
emulated Plex route: what was measured on a real PMS is marked where it applies (what a row
does when its file goes away, `tests/fixtures/missing_file_scan_s43.json`; a delete; Versions).

## Sources

A source has a name, a byte base URL and optional credentials for it. The source registers
them itself, over this API, before it pushes (`PUT /rex/sources/{name}`, below), and rex keeps
them in its state file (`rex.db`), so a source's rows keep their bytes across a restart before
it registers again. Nothing about a source is set on rex's side: no file, no token.

What rex needs from a byte server is a GET of a file, nothing more, so a source can be
zurg's WebDAV, nginx, a NAS, object storage or `python3 -m http.server`
(`tests/test_sources.py` runs two such sources side by side):

- `Range` is optional. A server that ignores it and answers 200 with the whole file
  still serves seeks: rex skips to the first byte asked for itself (slowly, for a seek
  deep into a large file: every byte before it is read).
- Redirects (301/302/303/307/308) are followed. The source's `user`/`password` or
  `bearer` goes only to the source's own origin, never to the host a redirect names,
  so a presigned CDN link works and does not see the credential. Under `--part-mode redirect`
  a client's own byte read stops at the first redirect to another origin and the client is
  sent there, so it downloads from that host itself; a conversion still reads through rex,
  and a source that answers with the bytes is streamed as before.
- As many sources as needed, each registered under its own name. Each pushes to
  `/rex/feed/{its name}`, its rows remember it, and the same title from two sources is one
  row with a Media each. An item's `path` decides which library holds it, not its source.

### Who may call it

Any source on your own networks. The feed and the sources API read no token and are not
behind the Plex gate, claimed or not, with or without `--token`, so a source may name rex by
address or host name (`http://rex:32400` from a container, a Tailscale name). They answer
only a peer whose address is this machine (127.0.0.0/8, `::1`), a home network (10/8,
172.16/12, 192.168/16, IPv6 fc00::/7), Tailscale (100.64/10) or link-local (169.254/16,
fe80::/10), an IPv4-mapped IPv6 address of those included. To any other peer the source
API does not exist: the request takes the ordinary PMS authentication and unknown-route
path, with the same status, headers and body as an unrelated missing PMS path. No token,
including an owner's valid Plex token, opens the source API to a public peer.

rex reads the connection's own peer address, never `X-Forwarded-For` or `Host`, so a header
cannot open it. A reverse proxy on the same machine therefore reaches it as a local peer:
block `/rex/` there. Forwarding rex's port for rex-direct (setup step 7) puts Plex's API on the
internet, which needs a token once rex is claimed, but not the feed.

## Registering a source

### `PUT /rex/sources/{name}`

```json
{"url": "http://127.0.0.1:9999/dav", "user": "zurg-user", "password": "zurg-password",
 "directories": [{"name": "anime", "shows": "Anime", "movies": "Anime Movies"}, {"name": "wrestling", "shows": "Wrestling"}]}
```

| Field | Meaning |
|---|---|
| `url` | Required, `http` or `https`. A part's bytes are `GET <url>/<path>` with the client's `Range` forwarded; each path segment is percent-encoded. No `user:password@` in it: the url is answered back, so credentials go in their own fields |
| `user`, `password` | Basic auth for `url` (optional) |
| `bearer` | `Authorization: Bearer` for `url` instead (optional) |
| `mount` | Optional, a label: the folder Plex apps show its files under in Get Info (`<mount>/<path>`, default `/<path>`) and its libraries under (default rex's own, `/mnt/rex`). Playback of what a source pushes never uses it |
| `directories` | Optional: the directories that get a library of their own ([below](#libraries-a-source-declares)). Each entry is `name`, one directory as the first segment of the source's item paths (non-empty, no `/`), and `shows` and/or `movies`, the title of its show and movie library (non-empty strings; at least one). Absent, `null` or `[]` declares nothing. A malformed entry is ignored, never the registration |

`{name}` is letters, digits, `-` and `_`. A registration creates the source or replaces the
one of that name, and the next part read streams from what it names, without a restart.
Sending the same registration again changes nothing, so a source can send it every time it
starts a push session; zurg does. Answers `200` with the source as `GET` shows it, or `400`
`{"error": ...}` for a bad name, a url that is not http(s) or carries credentials, or a body
that is not a JSON object; a refused registration changes nothing. The answer's `features`
lists what this rex takes beyond the items below, so a source can tell before it relies on one:
`probe_digest` ([Probe digests](#probe-digests)). A rex without `features` takes none.

A malformed `directories` entry never refuses the source: one typo in a config would stop its
whole feed. The entry is left out, the rest register, and the `200` answers each one left out
with why, its reason naming the directory it is about; rex logs each reason once since its
start. A `directories` that is no list is left out the same way. `ignored` is absent when
nothing was:

```json
{"name": "zurg", "url": "http://127.0.0.1:9999/dav", "auth": "basic",
 "directories": [{"name": "anime", "shows": "Anime"}],
 "ignored": [{"entry": {"name": "kids", "shows": ""}, "reason": "directory \"kids\": shows title is empty"}]}
```

### Libraries a source declares

A directory named in `directories` is a library of its own, made when the source registers:
a movie library titled as the entry's `movies` and a show library titled as its `shows`, whichever it has,
on the folder `<mount>/<name>` (with no `mount`, under the parent of the first library's
folder, `/mnt/rex` for a library rex seeded). rex logs `SECTION create Anime (show) for zurg's anime`, and
Plex clients see it as one added in Add Library. Then:

- **A library of that type already on the folder is used**, whoever made it: one added with
  Add Library, or one made by an earlier registration. Nothing is
  made and nothing is retitled, so a rename in Plex sticks; the folder, not the title, is what
  matches, and two sources declaring `anime` fill one library.
- **An episode** of the directory goes to its show library, **a film** to its movie library, and
  **an unplaced file** (no `kind`) to the first library holding the folder. rex makes the movie
  library first, so when it makes both that is the movie library, as unplaced files go to a movie
  library anywhere else; a directory with only a show library takes its unplaced files there. A type the entry does not title goes to the first library of
  its kind, as an undeclared directory's do.
- **Rows already shown move** to the new library on the source's next push, as they are, on
  their keys: identification, a Fix Match, every account's watch state and a person's edits
  stay. An episode goes under its show there, else under a copy of the show and season it
  leaves, which takes what a person did to them (titles, sort title, summary, labels,
  collections, locks, a picked or uploaded poster or background); a show already there keeps
  each edit of its own and takes the rest. A row holding several releases moves only when every
  release's folder belongs to the new library; otherwise it stays, logged once
  (`FEED <key> stays in TV Shows: its releases sit in folders of different libraries`). A row
  placed as something else in the same push is identified again instead.
- **Deleting the library in Plex keeps it deleted.** Its folders and type are kept in the state
  file (`deletedLocations`), a later registration makes nothing there and logs once that it
  stays deleted, and the directory's files go back to the first library of their kind.
- **Add Library brings one back.** Its folder picker lists each directory a source pushed from
  or declared, under the source's mount (`/mnt/zurg/anime`); a library added on a deleted one's
  folder takes the directory's files, and the folder is no longer kept deleted.
- **rex never deletes a library itself.** A directory dropped from the list, or a source that
  stops declaring any, keeps its library.

A directory no entry names, nor any library's folder, goes to the first library of its kind:
TV Shows for an episode, Movies for anything else.

### `GET /rex/sources` and `GET /rex/sources/{name}`

```json
{"size": 1, "sources": [{"name": "zurg", "url": "http://127.0.0.1:9999/dav", "auth": "basic"}]}
```

Every source, or one (`404` for a name nothing registered), with its `directories` as
registered when it declared any. A credential is never answered, only its kind: `auth` is
`basic` or `bearer`. As in every JSON answer rex gives, a field
with no value is left out: a source with no credential has no `auth`, and with nothing
registered the listing is `{"size": 0}`.

### `DELETE /rex/sources/{name}` -- two requests

Deleting a source removes it and every item it pushed from rex, so it takes two requests,
and the first removes nothing:

```json
DELETE /rex/sources/rd                      -> {"confirm": "/rex/sources/rd/confirm/9f2c...e41a", "items": 1234, "expires_in": 300}
DELETE /rex/sources/rd/confirm/9f2c...e41a  -> {"deleted": true, "removed": 1234}
```

The first answers how many items the source has here (its rows' releases and its held
items) and the path that confirms the delete: a random 128-bit code, bound to that source,
good for one use within 300 seconds and held in memory only, so a restart forgets it. Asking
again replaces the code. A wrong, expired, used or other source's code is a `404` that
removes nothing; a name nothing registered is a `404` too. The log shows the confirmation
path with the code as `***`.

The confirmation removes the registration, the source's held items, and its releases from
the library: a row whose every release is this source's is deleted for good, as emptying the
trash deletes one, and a row that also holds another source's release keeps that one and
loses only this source's Media. It is a removal from rex alone: no `DELETE` reaches the
source's files (compare a person's delete, below). Registered and pushed again afterwards,
the source starts with nothing, and its items are new rows on new keys.

## Endpoints

Every endpoint is under `/rex/feed/{source}`, for a source registered above. A name nothing registered is a `404`
`{"error": "no such source: PUT /rex/sources/{source} first"}`. Bodies are JSON, at most
16 MiB a request (`400` above that).

A request is applied before it is answered, one feed request at a time per server. New
releases its source placed are identified on the way in by their ids (Plex's matcher, a few lookups
each, cached on disk after the first), so a first fill of a large library is slow: send chunks of a
few hundred items and allow minutes per request.

### `POST /rex/feed/{source}/items` -- upsert

```json
{"items": [ITEM, ...]}
```

Answers `200` with what happened:
`{"created": 1, "updated": 0, "held": 1, "unchanged": 0, "rejected": [{"id": "...", "reason": "..."}]}`,
plus `"wants": [id, ...]` when an item sent by its `probe` alone has to come whole
([Probe digests](#probe-digests)).
The log names each rejected item and why, `FEED {source} rejected {id}: {reason}`.

### `DELETE /rex/feed/{source}/items` -- remove

```json
{"ids": ["movies/Some.Release/file.mkv", ...]}
```

A removed id's row goes to the trash the way a real PMS trashes a missing file
(measured on a writable copy of a real PMS, `tests/fixtures/missing_file_scan_s43.json`): `deletedAt` on the row and its Media,
announced as a `type -1` state 3/5 pair, or deleted at once with one `state 9` frame
when `autoEmptyTrash` is on. The same id pushed again before the trash is emptied
restores the **same** row and key; after `emptyTrash` it is a new row on a new key. A
held id is simply forgotten. An id that is one of several releases on a row (see
**Versions**) drops only its Media, counted as `deleted`; the row stays. Answers
`{"trashed": n, "deleted": n, "released": n, "unknown": n}`.

### `PUT /rex/feed/{source}/snapshot` -- the complete set

```json
{"session": "2026-10-10T12:00:00Z", "part": 0, "complete": false, "items": [ITEM, ...]}
```

Every chunk's items are upserted as they arrive. The chunk with `"complete": true` ends
the session: every id of this source that no chunk of the session named is removed as
`DELETE` removes it. Without `session` a request is a whole, one-chunk snapshot.

- `part` counts the chunks from 0. A chunk with `part > 0` for a session rex does not
  hold (it restarted mid-snapshot, or a chunk was lost) is refused with `409`: start the
  snapshot again from part 0. A new session replaces an unfinished one.
- **An empty snapshot removes nothing**, as an empty or unlistable library folder trashes
  nothing on a real PMS (6b3). Remove everything with `DELETE` instead.
- **A snapshot that would remove more than half of the source's items is refused**
  (`409`, `{"error": ..., "would_remove": n, "of": total}`) unless the final chunk carries
  `"force": true`. The final chunk's own upserts still apply. This is the guard against a
  source that answers with a truncated library while it is still loading.

Answers the upsert counts, plus `"seen"` (ids so far in the session) and, on the final
chunk, `"removed": {"trashed": n, "deleted": n, "released": n}`.

### Probe digests

A source may name an item's probe instead of sending it: `probe` is a short string of its own
choosing (at most 128 characters) that changes whenever the probe does, such as a hash of the
`media_info` it would send. Sent beside `media_info`, rex keeps it with that file version. Sent
without `media_info`, the item is applied as it is, every other field included, when rex holds
that version with the same `probe`. Otherwise -- a new item, a changed probe, a version kept
from before digests, or a file that would leave its row (an extra, a version renumbered apart)
-- rex changes nothing of the item and answers its id in `wants`: push it again with its
`media_info`. A snapshot counts a wanted item as named, so it is never removed for it.

A snapshot of a library that rarely changes then sends almost no probes, where it used to send
every one. Only a rex whose registration answers `features: ["probe_digest"]` takes it; a
source sending `probe` alone to an older one gets its items held. An item with no `probe` is
handled as before.

### `GET /rex/feed/{source}/items` and `GET /rex/feed/{source}/held`

What rex holds for the source: the shown items with their ratingKeys
(`{"id", "ratingKey", "file", "deleted"}`), and the held ones (`{"id", "path", "name",
"size", "since"}`). For operators and tests; read-only.

## An item

```json
{
  "id": "movies/Blame.It.On.The.Bellboy.1992.1080p/Blame.It.On.The.Bellboy.1992.1080p.mkv",
  "path": "movies/Blame.It.On.The.Bellboy.1992.1080p/Blame.It.On.The.Bellboy.1992.1080p.mkv",
  "kind": "movie",
  "ids": {"tmdb": 31639},
  "media_info": {
    "format": {"format_name": "matroska,webm", "duration": "5032.144000", "bit_rate": "7261654"},
    "streams": [
      {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
      {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}}
    ]
  }
}
```

The `media_info` above is cut down to show its shape. Send ffprobe's whole output.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The source's stable key for the file. A row's ratingKey belongs to (source, id) for good, so keep it when the file is renamed |
| `path` | yes | Where the bytes are: `<source url>/<path>`. Its first directory also picks the library (below) |
| `media_info` | yes, in practice | ffprobe's `-show_format -show_streams` JSON (go-ffprobe's `ProbeData` is the same shape). Absent, `null` or with no `streams`: the item is **held**, because Plex Web will not play an item without streams (findings, "An item without a probe cannot play") |
| `ids` | recommended | `imdb` (`tt…`), `tmdb`, `tvdb`. With `kind`, the title: `imdb` and `tmdb` are the movie's or the **show's** ids, never an episode's, and either one is enough (rex fills in the other from Plex). Without `kind`, only `tmdb` names anything (a TMDB id set by hand at the source): a title of its library's kind, when TMDB has one; `imdb` alone and `tvdb` are kept on the row and name nothing (below) |
| `kind` | no | `movie`, `episode` or `extra`: what the source knows the file is. `movie` or `episode` **places** the file (below): it picks the movie or show library, and its `ids` are the title. `extra` makes it a clip of its release's title (below). Anything else, or absent: nothing places it, and rex does not guess from its name |
| `extra_type` | with `kind: extra` | The clip's type: `trailer`, `deleted`, `featurette`, `behindthescenes`, `interview`, `scene`, `short`, `opening`, `ending` or `other` (Plex's local-extras types and creditless openings and endings). Anything else, or absent, is `other` |
| `season`, `episode` | no | With `kind: episode`, the episode on **TMDB's** numbering of the show `ids.tmdb` names (`season` 0 is Specials). The row is placed exactly there. Absent: season 1, in the release's file order (below) |
| `episode_end` | no | The last episode of a file holding several (`E01E02` -> 2). Kept on the row; the file is placed on `episode`, as rex places every multi-episode file on its first |
| `tmdb_episode` | no | With `kind: episode`, TMDB's id of that episode (an integer, `tv/{show}/season/{n}/episode/{n}`'s `id`). The row is Plex's episode whose `Guid` lists `tmdb://<id>`, in whichever of the show's seasons Plex files it, and sits under Plex's season and number for it: Ben 10: Omniverse's `S02E01 - Showdown Part 1`, sent as S2E1 with 1107002, is Plex's S3E1. Without it, Plex's episode at `season`/`episode`. Kept on the row; a show a person Fix Matches re-describes its episodes by it |
| `name` | no | The release name, shown as it is: an unplaced episode's show is titled with it. Nothing is read out of it -- no id, no title, no season or episode. Default: the folder above the file |
| `release` | no | Groups a release's files, identified together (a season's episodes). Default: the folder above `path` |
| `edition_title` | no | Explicit movie edition label. Different labels stay separate movies; the same label can hold several versions. Omitted preserves the current source label; `""` clears it. A person's locked edition wins. Rejected for episodes or invalid labels; never inferred from a name |
| `size` | no | Bytes. Default: ffprobe's `media_info.format.size` (none for an HLS playlist). Shown as the Part's size and used to answer a client's `Range`; with neither, rex passes the client's `Range` to the source as it is |
| `file` | no | Only the path clients see in Get Info (`<mount>/<directory>/<release>/<file>`). Default `<mount>/<path>`, or `/<path>` without a mount |
| `library` | no | A section key, or `movie`/`show`. Default: the library whose `Location` holds `file`, else one with a folder ending in a directory `path` starts with (`/mnt/zurg/movies` holds `movies/...` whatever prefix `file` has); with a type, that type's library holding `file`, else its first; a folder no library holds goes to the first library of its `kind` (Movies without one). With no `library`, an item placed as an episode that a movie library would take goes to a show library -- one holding its folder ([a declared directory's](#libraries-a-source-declares)), else the one beside it -- and one placed as a movie that a show library would take goes to a movie library, one holding its folder first; a row already shown moves when its library changes |
| `folder` | no | The folder the provider lists the file in inside its release (`Smallville/Stagione 6`), `""` at its top level. Kept on the row; nothing reads it as an extra |
| `probe` | no | The probe's digest, kept per Media version; with it `media_info` may be left out ([Probe digests](#probe-digests)) |
| `hash` | no | The release's content/info hash, kept per Media version. A changed hash invalidates that version's measured subtitle reference and generated analysis; it never borrows another version's hash |
| `display` | no | What the title is called and looks like, for what no metadata agent knows: see **Display** below. Shown only while nothing identifies the row |
| `subtitles` | no | The subtitle files beside it at the source, each an external subtitle of this file's Part: see **Subtitles** below. Absent or `null` keeps the row's; `[]` removes them |

Embedded chapters are optional: include ffprobe's `-show_chapters` output in
`media_info.chapters`. rex keeps the timings and titles with that file version and
answers clients requesting `includeChapters=1` on metadata or play queues. A later
push updates chapters on an existing version too. Omitting `chapters` preserves its
saved chapters; an explicit `"chapters": []` clears them. With FFmpeg, optional Analyze
generates chapter JPEGs from these boundaries and serves them through authenticated metadata routes.

To populate chapters for items already in rex, have the source resend its saved
probes through the existing feed. A normal full snapshot updates existing versions
on their current keys; no database edits or rex restart are needed. With zurg,
wait for its next full snapshot or restart zurg to trigger one after its library
loads. Its saved probes must contain chapter data; probes made without
`-show_chapters` must first be refreshed at the source.

What the fields do:

- **Held until probed.** An item without `media_info` is stored but not shown: it is in no
  listing, hub, search or count, and logged `FEED <source> held <id>`. The push that
  brings its `media_info` shows it, stamped as added then, so it arrives in Recently Added
  when it becomes playable. A row already shown never goes back to held. A source must run ffprobe
  and send the container and stream metadata; optional rex analysis does not replace that feed probe:
  Plex Web will not play an item without it (findings, "An item without a probe cannot
  play").
- **Placement.** An item with a `kind` and ids is placed where its source says: the movie or
  show is the one its ids name, kept even when Plex has no record of it, and an episode sits on
  its `season` and `episode`. That holds on every push and restart. A later push placing it
  elsewhere moves it there on the same ratingKey, watch state kept; a push without a placement
  leaves it where the last one put it. A person's Fix Match still wins over a placement.
- **Identification.** rex identifies a file only by its source's placement or by a TMDB id set
  by hand (`ids.tmdb` with no `kind`: a title of its library's kind), trusted as pushed and
  checked against nothing, and never by its name (`identify.rs`). zurg asks DMM what each file
  is. Plex's matcher is asked for its guid by the title's external ids, and its answer
  -- and, for an episode, its season's and its own row -- is everything the row shows: title,
  summary, artwork, genres, people. rex reads no TMDB. Identification searches nothing (Fix Match,
  a person's, is the only search, through Plex's matcher). An IMDb id with no `kind` names
  nothing: zurg has DMM place a hand-set IMDb id. An identification that names nothing never
  takes away a title a row already has; a title Plex does not know keeps the name it was pushed
  with.
- **Unplaced.** An item nothing identifies is shown unmatched, for Fix Match: a film titled as
  its file (the file name without its extension), an episode in a show titled with its release's
  `name`, season 1, numbered 1..n in the release's file order (its paths sorted; a file arriving
  after its release's others is numbered after them). Nothing is read off either name. The
  numbers stay file order until the source places the files; a placement then moves each row on
  its ratingKey.
- **Corrected names.** A normal same-`id` push or snapshot refreshes a source-owned release
  title when the source corrects `name`; local show grouping and file-order numbering follow
  the correction on the existing show, season and episode keys. No title is identified by
  that name. A metadata-derived title is never handed back to the source, even when it
  equals the previous source name. A person's locked title, persisted title edit (including
  an unlocked edit), or Fix Match wins.
- **Late ids.** A push whose placement or hand-set TMDB id names what the row was not identified
  as re-identifies it on the same ratingKey, keeping its watch state (an episode moves to the
  show its new identity names, on the numbers it was placed on, else the ones it had). So does
  any push of a row whose ids named nothing when they came. A
  season takes the identity its episodes bring -- its guid and ids, summary and poster, the
  show's poster -- whatever its first episode lacked, unless a
  person locked the field; seasons left without one are given theirs when the server starts.
- **Retried.** A row on the blank tile -- a film with no poster, an episode whose show has
  none -- or an episode named as a film is identified again when the server starts and every ten minutes after, up to 50
  films or shows a pass, as its source pushed it -- when it came with a placement. A row without
  one is left as it is. One still unnamed waits an hour, then twice as long each time,
  to about five days. A Plex answer that found nothing is asked again after a day. No other pass at start touches a
  row by its name: none renumbers, unfolds, moves or re-identifies one.
- **A person's match wins.** A Fix Match is recorded on the row: no push and no retry
  identifies it, or the episodes of a show a person matched, again. Unmatch hands it back to
  its source's ids.
- **Versions.** A release of a title already here -- a film with the same agent guid in the
  same section and with the same explicit edition label, an episode on the same season and index,
  whatever guid identification gave it -- is no new row: it joins that row as another Media,
  as a real PMS offers several files of
  one movie as versions. Other
  files of the same release stay rows of their own: a collection that numbers several shows'
  episodes alike, a batch's specials on one number. The row someone watched survives, else one
  not in the trash, else the oldest. A row in the trash folds into the one that is there and
  brings no Media, its files being gone; when it is the one someone watched, it takes the
  other release's files and leaves the trash, as a real PMS's item takes a new file of its
  episode. An unmatched film (`local://`) and a row awaiting a probe never fold. Each id stays
  addressable: the listing names every id on the row's key, a push updates its own Part, a
  part read streams from its own source and path. A row of several releases is not re-identified by one of them.
- **Only video.** An item whose file is not a video (by extension) or is a sample is rejected,
  as Plex's scanners skip them. An `.m3u8` file is a video when its `media_info` is an HLS
  probe (`format_name` `hls`): a source that serves a playlist (a YouTube relay) names it
  so. Such a Part is never direct played; a client's read of it is sent to a conversion.
  One whose probe has no duration is a live channel (a Stremio addon's TV): its conversion's
  playlist grows as the broadcast goes on, with no end, and the row keeps no resume point
  and is never marked watched. A later push of an `.m3u8` id whose probe has its streams at
  other indexes or of other codecs (the channel from another mirror) replaces the row's streams.
- **One file, one id.** A new `id` whose `file` another id of the same source already has, in
  rex or earlier in the same push, is rejected as `<file> is already <id>'s file`. To change
  an item's id, push it with a file of its own, or remove the old id first.
- **Extras are the source's to name.** A file pushed with `kind: extra` is a clip of the title
  its release's other files are (the film, or the episodes' show; unmatched or not) with its
  `extra_type`, in that title's `Extras` and `extras/all` and in no listing, axis or count. One
  whose release has no title here yet waits for a later push. A file pushed without it is no
  clip, whatever folder it sits in or its name says.

## Display

A source that pushes things no metadata agent knows -- a YouTube channel, home video -- names
them itself with `display`:

```json
"display": {
  "title": "Clean Up Song for Kids", "summary": "...", "date": "2019-03-01",
  "thumb": "https://i.ytimg.com/vi/1mjMdudwtRc/maxresdefault.jpg",
  "art": "https://...",
  "show": {"title": "Ms Rachel", "summary": "...", "poster": "https://...", "art": "https://..."},
  "season": {"summary": "...", "poster": "https://..."}
}
```

| Field | On | Meaning |
|---|---|---|
| `title`, `summary` | the title | Shown as sent |
| `date` | the title | `YYYY-MM-DD`: `originallyAvailableAt`, and `year` from it. Or `YYYY` alone, for a title known only by its year (an old film): its `year`, no release day |
| `thumb` | the title | A film's poster, an episode's still |
| `art` | a film | Its background |
| `show.title`, `show.summary`, `show.poster`, `show.art` | an episode's show | Its title, summary, poster and background |
| `season.summary`, `season.poster` | an episode's season | Its summary and poster (the show's poster when absent) |

Artwork is an `http`/`https` URL rex fetches as it fetches Plex's agent artwork: from public
hosts only, never this machine's own networks. The one exception is a picture under the
source's own registered `url`: rex fetches that from the source, with the source's login, as it
fetches its bytes, so an addon can serve its own pictures (a home video's frame) from the same
machine. rex keeps it as `source://<name>/<path>`, and a client naming the same URL through
`/photo/:/transcode` is still refused. Every field is optional and an empty one says
nothing; a field of the wrong type, artwork that is no http(s) URL or a date of another shape
rejects the item, naming the field.

- **Only what nothing identifies.** `display` is shown on a row its `kind` and ids, or a
  TMDB id set by hand, did not name: one Plex's agent describes shows what Plex says. rex
  still identifies nothing; the source says what the title is called, as it says what it is.
- **A person's edit wins.** A later push writes each field again unless a person locked it
  (Plex's edit dialog locks each field it saves); an uploaded poster stays theirs.
- **Later pushes update.** A changed `display` changes the row, its show and its season on
  the same keys; new artwork gets new paths, so clients drop what they cached. An episode's
  show is titled when it is made (the show's title on a later push changes nothing).
- **Placement is separate.** `display` names nothing and numbers nothing: an episode is placed
  by `kind`, `season` and `episode`, and unplaced ones are numbered in file order as before.

## Subtitles

A subtitle file beside the video at the source -- an *arr's import in zurg's `__magic__` with
a subtitle tool's `Movie (2010).en.srt` next to it -- is pushed with the item it belongs to:

```json
"subtitles": [
  {"path": "__magic__/movies/Sintel (2010)/Sintel (2010).en.srt", "language": "en", "size": 50},
  {"path": "__magic__/movies/Sintel (2010)/Sintel (2010).de.forced.srt", "language": "ger", "forced": true, "size": 1834}
]
```

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | Where its text is: `<source url>/<path>`, read as a Part's bytes are (the source's credentials, its redirects). Its extension, lower-cased, is its format: `srt`, `ass`, `ssa`, `smi`, `txt` or `psb`, what an upload takes |
| `language` | no | ISO 639-1 (`en`) or 639-2 (`eng`, `ger` or `deu`), as the source read it. rex reads nothing off the file's name: without one the subtitle has no language, as an upload has none |
| `forced`, `hearing_impaired` | no | `true` flags it as the same flags on an embedded track do (`English Forced`, `English SDH`); a forced one is passed over when tracks are picked by language |
| `size` | recommended | Bytes. The text is read when the path is new to the Part or its `size` changed, never otherwise; without a `size`, once |

Each becomes an external subtitle of the item's own Part (its own version's, on a row holding
several), in the shape a real PMS gives a subtitle file beside the video: `English`, `English
(SRT External)`, a `/library/streams/{id}` key serving its text as UTF-8 (measured on a real
1.43 library, `tests/fixtures/decisions/rows.json`). Clients list, pick, download and burn it in
as they do an upload, and it is selected only where the account's track settings would select
an embedded track.

- **Absent keeps, `[]` removes.** A push without `subtitles`, or with `null`, leaves the row's
  as they are, as an absent `chapters` does; `[]` removes this item's. A source with none sends
  `[]` (a Go nil slice marshals to `null`, and `omitempty` drops the field). Only what a source
  gave is removed: an upload or a subtitle found by Search Subtitles stays.
- **One path, one subtitle.** It keeps its stream id across pushes, a new probe and restarts;
  its language and flags follow each push.
- **Read off the lock.** The texts are read once the push is applied, without holding a Plex
  request, at most 4 MiB each.
- **Nothing costs the item.** An entry with another extension, a field of the wrong type, a
  path with an empty, `.` or `..` segment, or text over 4 MiB is left out and logged once with
  why (`FEED zurg <id>: subtitle <path>: <why>; left out`); the item is taken. A text the source
  does not serve yet (`subtitle <path> not read: the source answered 404`) arrives with a later
  push, and one that fails to read again keeps the text it had.
- **The source's, not a person's.** A client deleting one gets the 403 an embedded track's
  delete gets, since the next push would bring it back. That answer is reasoned, not measured.

## What rex sends back: a delete

A person deleting a row in a client deletes its files at the source, as a real PMS deletes
the files on its disk (measured on plexscratch 1.43.4, `tests/fixtures/media_delete_s43.json`).
For every Part of the row and the rows beneath it, rex sends the source a WebDAV
`DELETE {url}/{path}` with the source's credentials -- what a media server on zurg's mount
sends through rclone -- and removes the rows only when every file is gone (a 2xx, or the
source's 404). zurg marks the file deleted, keeps it so across restarts, removes the release
from the account with its last file, and pushes the removal back. A source that refuses (zurg
answers 403 for a file an *arr placed under `__magic__`) or cannot be reached leaves the rows
as they were and the client gets the reference's 400; with `allowMediaDeletion` off the
delete is the reference's 403 and the source is not asked. Without this a deleted row came
back with the source's next snapshot.

A library refresh (`/library/sections/{id}/refresh`) succeeds and changes nothing: rex has no folder to scan.
