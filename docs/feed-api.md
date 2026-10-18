# The feed API: sources push items to rex

rex does not list, scan or query any library. A **source** (zurg, or anything else
that serves files over HTTP) pushes the items it holds, and rex turns
them into library rows on keys that never change. The feed is rex's own API, not an
emulated Plex route: what was measured on a real PMS is marked where it applies (what a row
does when its file goes away, `tests/fixtures/missing_file_scan_s43.json`; a delete; Versions).

## Sources

A source has a name, a byte base URL, optional credentials for it, and optionally the
prefix its files are shown under. The source registers them itself, over this API, before
it pushes (`PUT /rex/sources/{name}`, below), and rex keeps them in its state file
(`rex.db`), so a source's rows keep their bytes across a restart before it registers again.
Nothing about a source is set on rex's side: no file, no token. rex used to take them from a
sources file with a feed token per source, which had to be made up and copied to both sides
by hand.

What rex needs from a byte server is a GET of a file, nothing more, so a source can be
zurg's WebDAV, nginx, a NAS, object storage or `python3 -m http.server`
(`tests/test_sources.py` runs two such sources side by side):

- `Range` is optional. A server that ignores it and answers 200 with the whole file
  still serves seeks: rex skips to the first byte asked for itself (slowly, for a seek
  deep into a large file: every byte before it is read).
- Redirects (301/302/303/307/308) are followed. The source's `user`/`password` or
  `bearer` goes only to the source's own origin, never to the host a redirect names,
  so a presigned CDN link works and does not see the credential.
- As many sources as needed, each registered under its own name. Each pushes to
  `/rex/feed/{its name}`, its rows remember it, and the same title from two sources is one
  row with a Media each. An item's `file` decides which library holds it, not its source.

### Who may call it

Any source on your own networks. The feed and the sources API read no token and are not
behind the Plex gate, claimed or not, with or without `--token`, so a source may name rex by
address or host name (`http://rex:32400` from a container, a Tailscale name). They answer
only a peer whose address is this machine (127.0.0.0/8, `::1`), a home network (10/8,
172.16/12, 192.168/16, IPv6 fc00::/7), Tailscale (100.64/10) or link-local (169.254/16,
fe80::/10), an IPv4-mapped IPv6 address of those included. Any other peer gets 403
`{"error": "the source API only answers private networks"}` on every route and method,
the delete confirmation included.

rex reads the connection's own peer address, never `X-Forwarded-For` or `Host`, so a header
cannot open it. A reverse proxy on the same machine therefore reaches it as a local peer:
block `/rex/` there. Forwarding rex's port for rex-direct (setup step 7) puts Plex's API on the
internet, which needs a token once rex is claimed, but not the feed.

### Rows keep their source

Registering is the only way a source exists: rex is configured with none, and `ZURG_URL`
(which used to define a source `zurg`) is no longer read. A row the feed made names its
source and its path there (`_source`, `_path`); a catalog row from before the feed (the old
WebDAV scanner's) names none, and reads from the registered source whose `mount` holds its
Part's `file`. Either has no bytes -- a part read answers 502 -- until that source
registers, and plays the moment it does.

## Registering a source

### `PUT /rex/sources/{name}`

```json
{"url": "http://127.0.0.1:9999/dav", "user": "zurg-user", "password": "zurg-password", "mount": "/mnt/zurg"}
```

| Field | Meaning |
|---|---|
| `url` | Required, `http` or `https`. A part's bytes are `GET <url>/<path>` with the client's `Range` forwarded; each path segment is percent-encoded. No `user:password@` in it: the url is answered back, so credentials go in their own fields |
| `user`, `password` | Basic auth for `url` (optional) |
| `bearer` | `Authorization: Bearer` for `url` instead (optional) |
| `mount` | Optional and rarely needed: the prefix of the `file` clients see for an item pushed without one (`<mount>/<path>`, else `/<path>`). Nothing is read from it. It also lets a catalog row from the old WebDAV scanner, with no feed id, find its bytes |

`{name}` is letters, digits, `-` and `_`. A registration creates the source or replaces the
one of that name, and the next part read streams from what it names, without a restart.
Sending the same registration again changes nothing, so a source can send it every time it
starts a push session; zurg does. Answers `200` with the source as `GET` shows it, or `400`
`{"error": ...}` for a bad name, a url that is not http(s) or carries credentials, or a body
that is not a JSON object.

### `GET /rex/sources` and `GET /rex/sources/{name}`

```json
{"size": 1, "sources": [{"name": "zurg", "url": "http://127.0.0.1:9999/dav", "auth": "basic", "mount": "/mnt/zurg"}]}
```

Every source, or one (`404` for a name nothing registered). A credential is never answered,
only its kind: `auth` is `basic` or `bearer`. As in every JSON answer rex gives, a field
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
`{"created": 1, "updated": 0, "held": 1, "unchanged": 0, "rejected": [{"id": "...", "reason": "..."}]}`.
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

### `GET /rex/feed/{source}/items` and `GET /rex/feed/{source}/held`

What rex holds for the source: the shown items with their ratingKeys
(`{"id", "ratingKey", "file", "deleted"}`), and the held ones (`{"id", "path", "name",
"size", "since"}`). For operators and tests; read-only.

## An item

```json
{
  "id": "movies/Blame.It.On.The.Bellboy.1992.1080p/Blame.It.On.The.Bellboy.1992.1080p.mkv",
  "path": "movies/Blame.It.On.The.Bellboy.1992.1080p/Blame.It.On.The.Bellboy.1992.1080p.mkv",
  "name": "Blame.It.On.The.Bellboy.1992.1080p.BluRay.x264",
  "size": 4567890123,
  "kind": "movie",
  "ids": {"imdb": "tt0103827", "tmdb": 31639},
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
| `size` | recommended | Bytes. Shown as the Part's size and used to answer a client's `Range`; without it rex passes the client's `Range` to the source as it is |
| `ids` | recommended | `imdb` (`tt…`), `tmdb`, `tvdb`. With `kind`, the title: `imdb` and `tmdb` are the movie's or the **show's** ids, never an episode's. Without `kind`, only `tmdb` names anything (a TMDB id set by hand at the source): a title of its library's kind, when TMDB has one; `imdb` alone and `tvdb` are kept on the row and name nothing (below) |
| `kind` | no | `movie`, `episode` or `extra`: what the source knows the file is. `movie` or `episode` **places** the file (below): it picks the movie or show library, and its `ids` are the title. `extra` makes it a clip of its release's title (below). Anything else, or absent: nothing places it, and rex does not guess from its name |
| `extra_type` | with `kind: extra` | The clip's type: `trailer`, `deleted`, `featurette`, `behindthescenes`, `interview`, `scene`, `short`, `opening`, `ending` or `other` (Plex's local-extras types and creditless openings and endings). Anything else, or absent, is `other` |
| `season`, `episode` | no | With `kind: episode`, the episode on **TMDB's** numbering of the show `ids.tmdb` names (`season` 0 is Specials). The row is placed exactly there. Absent: season 1, in the release's file order (below) |
| `episode_end` | no | The last episode of a file holding several (`E01E02` -> 2). Kept on the row; the file is placed on `episode`, as rex places every multi-episode file on its first |
| `name` | no | The release name, shown as it is: an unplaced episode's show is titled with it. Nothing is read out of it -- no id, no title, no season or episode. Default: the folder above the file |
| `release` | no | Groups a release's files, identified together (a season's episodes). Default: the folder above `path` |
| `file` | no | The path clients see in Get Info, and what zurg's matcher reads back (`<mount>/<directory>/<release>/<file>`). Default `<mount>/<path>`, or `/<path>` without a mount |
| `library` | no | A section key, or `movie`/`show`. Default: the library whose `Location` holds `file`, else one with a folder ending in a directory `path` starts with (`/mnt/zurg/movies` holds `movies/...` whatever prefix `file` has); a directory no library holds is **made a library** (below). With a type, that type's library holding `file`, else its first. With no `library`, an item placed as an episode that a movie library would take goes to a show library -- one holding its folder, else the one beside it -- and one placed as a movie that a show library would take goes to a movie library, one holding its folder first; a row already shown moves when its library changes |
| `folder` | no | The folder the provider lists the file in inside its release (`Smallville/Stagione 6`), `""` at its top level. Kept on the row; nothing reads it as an extra |
| `hash` | no | The release's info hash. Kept on the row; nothing reads it yet |

Embedded chapters are optional: include ffprobe's `-show_chapters` output in
`media_info.chapters`. rex keeps the timings and titles with that file version and
answers clients requesting `includeChapters=1` on metadata or play queues. A later
push updates chapters on an existing version too. Omitting `chapters` preserves its
saved chapters; an explicit `"chapters": []` clears them. rex does not generate
chapter thumbnails.

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
  when it becomes playable. A row already shown never goes back to held. rex never probes
  a file itself, so a source must run ffprobe and send the result: measured on 2026-10-12,
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
  is; on the production server 0.9% of episodes and 1.1% of movies arrive unplaced
  (2026-10-17). Plex's matcher is asked for its guid by the title's external ids, and its answer
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
- **Late ids.** A push whose placement or hand-set TMDB id names what the row was not identified
  as re-identifies it on the same ratingKey, keeping its watch state (an episode moves to the
  show its new identity names, on the numbers it was placed on, else the ones it had). So does
  any push of a row whose ids named nothing when they came. A
  season takes the identity its episodes bring -- its guid and ids, summary and poster, the
  show's poster -- whatever its first episode lacked, unless a
  person locked the field; seasons left without one are given theirs when the server starts.
- **Retried.** A row on the blank tile -- a film with no poster, an episode whose show has
  none -- or an episode named as a film (a film's guid, which every season-0 episode got before
  2026-10-16) is identified again when the server starts and every ten minutes after, up to 50
  films or shows a pass, as its source pushed it -- when it came with a placement. A row without
  one is left as it is: rows pushed before DMM placed anything still carry ids zurg's own Plex
  gave, an episode's among them. One still unnamed waits an hour, then twice as long each time,
  to about five days. A Plex answer that found nothing is asked again after a day. No other pass at start touches a
  row by its name: none renumbers, unfolds, moves or re-identifies one.
- **A person's match wins.** A Fix Match is recorded on the row: no push and no retry
  identifies it, or the episodes of a show a person matched, again. Unmatch hands it back to
  its source's ids.
- **Versions.** A release of a title already here -- a film with the same agent guid in the
  same section, an episode on the same season and index, whatever guid identification gave it
  -- is no new row: it joins that row as another Media, as a real PMS offers several files of
  one movie as versions (2026-10-11, on one real library 407 movies and ~1,370 episodes were
  listed twice; 2026-10-17, SpongeBob's S10E64, which TMDB does not list, was five rows). Other
  files of the same release stay rows of their own: a collection that numbers several shows'
  episodes alike, a batch's specials on one number. The row someone watched survives, else one
  not in the trash, else the oldest. A row in the trash folds into the one that is there and
  brings no Media, its files being gone; when it is the one someone watched, it takes the
  other release's files and leaves the trash, as a real PMS's item takes a new file of its
  episode. An unmatched film (`local://`) and a row awaiting a probe never fold. Each id stays
  addressable: the listing names every id on the row's key, a push updates its own Part, a
  part read streams from its own source and path. Rows from before this are folded when the
  server starts. A row of several releases is not re-identified by one of them.
- **Only video.** A `file` that is not a video (by extension) or is a sample is rejected,
  as Plex's scanners skip them.
- **Extras are the source's to name.** A file pushed with `kind: extra` is a clip of the title
  its release's other files are (the film, or the episodes' show; unmatched or not) with its
  `extra_type`, in that title's `Extras` and `extras/all` and in no listing, axis or count. One
  whose release has no title here yet waits for a later push. A file pushed without it is no
  clip, whatever folder it sits in or its name says: rex read folder names (`Extras`, `NC`,
  `Featurettes`) and `NCOP`/`NCED` tags itself until 2026-10-17. Rows made of extras before then
  stay as they are, for a person to delete.
- **Migration.** The first time an id is seen, a row the old WebDAV scanner made for the
  same `file` (no feed id yet) is adopted with its ratingKey, watch state and
  identification; nothing is re-identified unless the item's placement or ids differ from the
  ones the row was identified by.

## Libraries: one per directory

The first directory of an item's `path` is its library. rex makes one the first time a
directory no library holds is pushed (zurg's `anime`, `kids`): titled with the directory's name,
of the type the item is (a show library for `kind: episode`, a movie library otherwise), on the
folder its `file` shows the directory as (`/mnt/zurg/anime`, else `/anime`). A directory
holding both films and episodes is two libraries of that name, one of each type, the second
made when the first item of its type arrives. Nothing else makes a library: a clip goes where its
title is, and an item naming its `library` goes there.

- **A library that already holds the folder is used**, whichever made it: the seed's Movies and
  TV, one added in Add Library, or one rex made. A person can rename either; the folder, not
  the title, is what matches. Two sources pushing `anime/` fill one library.
- **Deleting a library stops its directory.** Its folders are remembered in the state file, and
  an item of its directory is then rejected (`no movie or show library holds ...`), not given a
  new library. Adding a library with that folder in Add Library takes it again.
- **Rows from before 2026-10-19 stay where they are.** Until then a directory no library held
  went to the first library of its kind. At every start, a library holding rows from a
  directory no library holds takes that directory as a folder, so those rows -- and later items
  there -- keep the library they had instead of moving to a new one. rex-fun's `recent`,
  `music`, `audiobooks` and `OnePieceLatest` stayed in Movies and TV Shows this way.

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

## What happened to the scan

`GET /library/sections/{id}/refresh` and `/activities` are what they are on a library
with no folders to read: the measured scan frames, no activity left running. Old zurg
builds still send refreshes; they succeed and change nothing.
