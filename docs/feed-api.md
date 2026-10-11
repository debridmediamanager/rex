# The feed API: sources push items to rex

rex does not list, scan or query any library. A **source** (zurg, or anything else
that serves files over HTTP with `Range`) pushes the items it holds, and rex turns
them into library rows on keys that never change. The feed is rex's own API, not an
emulated Plex route: nothing here was measured on a real PMS except what a row does when
its file goes away (the trash rules below, `tests/fixtures/missing_file_scan_s43.json`).

## Sources

A source has a name, a byte base URL, optional credentials for it, and the feed token
it pushes with. They come from a JSON file named by `--sources FILE` (or
`$REX_SOURCES`); keep it out of the catalog and the repository, mode 600:

```json
{
  "zurg": {
    "url": "http://127.0.0.1:9999/dav",
    "user": "zurg-user", "password": "zurg-password",
    "token": "a long random feed token",
    "mount": "/mnt/zurg"
  },
  "nas": {"url": "https://nas.example/media", "bearer": "...", "token": "..."}
}
```

| Field | Meaning |
|---|---|
| `url` | A part's bytes are `GET <url>/<path>` with the client's `Range` forwarded; each path segment is percent-encoded |
| `user`, `password` | Basic auth for `url` (optional) |
| `bearer` | `Authorization: Bearer` for `url` instead (optional) |
| `token` | What the source sends as `Authorization: Bearer <token>` to the feed. A source without one cannot push |
| `mount` | Optional: the client-visible prefix of the source's files. An item without `file` shows `<mount>/<path>`, and a catalog row with no feed id whose Part file is under `mount` reads its bytes from this source |

Without a sources file, `ZURG_URL` (+ `ZURG_USER`/`ZURG_PASSWORD`) still defines one
source named `zurg`: url `$ZURG_URL/dav`, mount `--zurg-mount`, feed token
`$REX_FEED_TOKEN`. A file entry named `zurg` replaces it.

## Endpoints

Every endpoint is under `/rex/feed/{source}` and takes the source's feed token as
`Authorization: Bearer <token>`. Plex tokens are not accepted and the Plex gate does not
apply. An unknown source, or one with no token configured, is a `404`; a missing or
wrong token is a `401`. Bodies are JSON, at most 16 MiB a request (`413` above that).

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
(plexscratch, `missing_file_scan_s43.json`): `deletedAt` on the row and its Media,
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
  "file": "/mnt/zurg/movies/Blame.It.On.The.Bellboy.1992.1080p/Blame.It.On.The.Bellboy.1992.1080p.mkv",
  "library": "movie",
  "name": "Blame.It.On.The.Bellboy.1992.1080p.BluRay.x264",
  "release": "movies/Blame.It.On.The.Bellboy.1992.1080p",
  "size": 4567890123,
  "hash": "0123456789abcdef0123456789abcdef01234567",
  "ids": {"imdb": "tt0103827"},
  "media_info": {"format": {}, "streams": []}
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The source's stable key for the file. A row's ratingKey belongs to (source, id) for good |
| `path` | yes | Where the bytes are: `<source url>/<path>` |
| `file` | no | The Part `file` clients see, and what zurg's matcher reads back (`<mount>/<directory>/<release>/<file>`). Default `<mount>/<path>`, or `/<path>` without a mount |
| `library` | no | A section key, or `movie`/`show`. Default: the library whose `Location` holds `file`; with a type, that type's library holding `file`, else its first. Fixed when the id is first shown |
| `name` | no | The release name identification reads. Default: the folder above the file |
| `release` | no | Groups a release's files, identified together (a season's episodes). Default: the folder above `path` |
| `size` | no | Bytes |
| `hash` | no | The release's info hash |
| `ids` | no | `imdb` (`tt…`), `tmdb`, `tvdb`. Exact identification; `tvdb` is kept, not used |
| `media_info` | no | ffprobe's `-show_format -show_streams` JSON (go-ffprobe's `ProbeData` is the same shape). Absent or `null`: the item is **held** |

What the fields do:

- **Held until probed.** An item without `media_info` is stored but not shown: it is in no
  listing, hub, search or count, and logged `FEED <source> held <id>`. The push that
  brings its `media_info` shows it, stamped as added then, so it arrives in Recently Added
  when it becomes playable. A row already shown never goes back to held.
- **Identification.** `ids` first (an IMDb id is exact: TMDB's `/find`, then Plex's matcher
  by external id; an episode's or a season's IMDb id names its show, which is how DMM knows
  most season packs), else the release name (`identify.rs`: Plex's matcher and TMDB, their
  agreement the confidence; when they name different shows and the release's season is one
  only TMDB's has, TMDB's wins). A release no source names stays unmatched, for Fix Match.
  An episode is numbered from `SxxEyy`, `NxNN` or a bare `E06`, in the file's name or its
  release's.
- **Late ids.** A push whose `ids` name what the row was not identified as re-identifies
  it on the same ratingKey, keeping its watch state (an episode moves to the show its new
  identity names). So does any push of a row whose ids named nothing when they came.
- **Versions.** A release identified as a title already here -- the same agent guid in the
  same section, and for an episode the same season -- is no new row: it joins that row as
  another Media, as a real PMS offers several files of one movie as versions (2026-10-11,
  fun: 407 movies and ~1,370 episodes were listed twice). The row someone watched
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

## What happened to the scan

`GET /library/sections/{id}/refresh` and `/activities` are what they are on a library
with no folders to read: the measured scan frames, no activity left running. Old zurg
builds still send refreshes; they succeed and change nothing.
