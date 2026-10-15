# The feed API: sources push items to rex

rex does not list, scan or query any library. A **source** (zurg, or anything else
that serves files over HTTP) pushes the items it holds, and rex turns
them into library rows on keys that never change. The feed is rex's own API, not an
emulated Plex route: nothing here was measured on a real PMS except what a row does when
its file goes away (the trash rules below, `tests/fixtures/missing_file_scan_s43.json`).

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

The feed and the sources API answer the networks an unclaimed rex answers (`local_request`;
findings, "The gate an unclaimed server keeps"), claimed or not: the peer and every
`X-Forwarded-For` hop must be loopback or a private address (10/8, 172.16/12, 192.168/16,
fc00::/7), or once `allowedNetworks` is set, in it, and `Host` must be `localhost` or the
address the request reached. Anything else is a `403`
`{"error": "the feed answers only local networks"}`. No token is read; Plex tokens and the
Plex gate play no part. Two consequences for a source:

- Name rex by its address (`http://127.0.0.1:32400`, `http://192.168.1.10:32400`), not by a
  host name: a container that reaches rex as `http://rex:32400` sends `Host: rex` and is
  refused.
- Tailscale's 100.64/10 is not a private range. A source that reaches rex over Tailscale
  needs it in `allowedNetworks` (Settings > Network on rex, from rex's own machine).

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

Every endpoint is under `/rex/feed/{source}`, for a source registered above, and answers
only the networks above. A name nothing registered is a `404`
`{"error": "no such source: PUT /rex/sources/{source} first"}`. Bodies are JSON, at most
16 MiB a request (`413` above that).

A request is applied before it is answered, one feed request at a time per server. New
releases are identified on the way in (TMDB and Plex's matcher, a few lookups each, cached
on disk after the first), so a first fill of a large library is slow: send chunks of a
few hundred items and allow minutes per request.

### `POST /rex/feed/{source}/items` -- upsert

```json
{"items": [ITEM, ...]}
```

Answers `200` with what happened:
`{"created": 1, "updated": 0, "held": 1, "unchanged": 0, "rejected": [{"id": "...", "reason": "..."}]}`.

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
  "ids": {"imdb": "tt0103827"},
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
| `ids` | recommended | `imdb` (`tt…`), `tmdb`, `tvdb`. Exact identification; without them rex identifies by `name`, and so it does when TMDB has no title for them (below). `tvdb` is kept, not used |
| `name` | no | The release name identification reads. Default: the folder above the file |
| `release` | no | Groups a release's files, identified together (a season's episodes). Default: the folder above `path` |
| `file` | no | The path clients see in Get Info, and what zurg's matcher reads back (`<mount>/<directory>/<release>/<file>`). Default `<mount>/<path>`, or `/<path>` without a mount |
| `library` | no | A section key, or `movie`/`show`. Default: the library whose `Location` holds `file`, else one with a folder ending in a directory `path` starts with (`/mnt/zurg/movies` holds `movies/...` whatever prefix `file` has); with a type, that type's library holding `file`, else its first. With no `library`, a release a movie library would take whose `name` carries an episode number (`E0221`, `EP1161`, `Ep.14`, `Name-15`) goes to a show library, the one beside it; a row already shown moves when its library changes |
| `folder` | no | The folder the provider lists the file in inside its release (`Smallville/Stagione 6`), `""` at its top level. A pack of several seasons that numbers each season's files from `01` says the season only here; without it rex leaves such a pack's files unplaced rather than fold its seasons onto one. Kept on the row |
| `hash` | no | The release's info hash. Kept on the row; nothing reads it yet |

What the fields do:

- **Held until probed.** An item without `media_info` is stored but not shown: it is in no
  listing, hub, search or count, and logged `FEED <source> held <id>`. The push that
  brings its `media_info` shows it, stamped as added then, so it arrives in Recently Added
  when it becomes playable. A row already shown never goes back to held. rex never probes
  a file itself, so a source must run ffprobe and send the result: measured on 2026-10-12,
  Plex Web will not play an item without it (findings, "An item without a probe cannot
  play").
- **Identification.** `ids` first (an IMDb id is exact: TMDB's `/find`, then Plex's matcher
  by external id; an episode's or a season's IMDb id names its show, which is how DMM knows
  most season packs), else the release name (`identify.rs`: Plex's matcher and TMDB, their
  agreement the confidence; when they name different shows and the release's season is one
  only TMDB's has, TMDB's wins; a film Plex knows by no TMDB id loses to TMDB's search). An
  id TMDB has no title for -- Plex's TMDB id for it is tried first -- or one of the other kind
  than the library's leaves it to the name. A title only Plex knows takes Plex's own artwork.
  A release no source names stays unmatched, for Fix Match. An episode is numbered from
  `SxxEyy`, `NxNN`, a bare `E06`, `E0221`, `EP1161` or `Ep.14`, a number glued to the title
  (`Gintama-340`, `Shichi-12 END`), ` - 01v2 -`, or the third of all-bracketed names
  (`[group][Samurai Champloo][13]`), in the file's name or its release's.
- **Late ids.** A push whose `ids` name what the row was not identified as re-identifies
  it on the same ratingKey, keeping its watch state (an episode moves to the show its new
  identity names). So does any push of a row whose ids named nothing when they came. A
  season takes the identity its episodes bring -- its guid and ids, summary and poster, the
  show's poster where TMDB has none for it -- whatever its first episode lacked, unless a
  person locked the field; seasons left without one are given theirs when the server starts.
- **Retried.** A row on the blank tile -- a film with no poster, an episode whose show has
  none, or a film whose release names an episode -- is identified again when the server
  starts and every ten minutes after, up to 50 films or shows a pass, as its source pushed it.
  One still unnamed waits an hour, then twice as long each time, to about five days. A TMDB or
  Plex answer that found nothing or has no poster is asked again after a day.
- **A person's match wins.** A Fix Match is recorded on the row: no push and no retry
  identifies it, or the episodes of a show a person matched, again. Unmatch hands it back to
  its source's ids.
- **Versions.** A release identified as a title already here -- the same agent guid in the
  same section, and for an episode the same season -- is no new row: it joins that row as
  another Media, as a real PMS offers several files of one movie as versions (2026-10-11,
  on one real library 407 movies and ~1,370 episodes were listed twice). The row someone watched
  survives, else the oldest; a pushed row that is unmatched (`local://`), trashed or
  awaiting a probe never folds. Each id stays addressable: the listing names every id on
  the row's key, a push updates its own Part, a part read streams from its own source and
  path. Rows from before this are folded when the server starts. A row of several releases
  is not re-identified by one of them.
- **Only video.** A `file` that is not a video (by extension) or is a sample is rejected,
  as Plex's scanners skip them.
- **Migration.** The first time an id is seen, a row the old WebDAV scanner made for the
  same `file` (no feed id yet) is adopted with its ratingKey, watch state and
  identification; nothing is re-identified unless the item's ids differ from the ones the
  row was identified by.

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
