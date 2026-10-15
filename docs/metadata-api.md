# Metadata providers: reading TMDB through another server

rex names what its sources push from TMDB: who a release is, the show's seasons, an
episode's title and still. It reads all of that from TMDB itself (`TMDB_KEY`), or from a
**metadata provider**: any server that implements the open
[Metadata API](https://github.com/debridmediamanager/debrid-media-manager/blob/main/docs/metadata-api.md).
That document is the API's specification, for anyone building a provider. This one says
what rex does with one. DMM is the first provider, for its sponsors; anyone can run
another, such as a cache in front of TMDB.

## Settings

| Variable | Meaning |
|---|---|
| `REX_METADATA_URL` | The provider's base URL (DMM's is `https://debridmediamanager.com`). Set, rex asks it first for every TMDB read |
| `REX_METADATA_KEY` | Sent as `x-api-key`, when set. DMM's is the sponsor's 64-character key from gatekeeper; a provider that takes no key leaves it unset |
| `TMDB_KEY` | Optional beside a provider: rex asks TMDB itself, with this key, whenever the provider gives no answer |

With a provider and no `TMDB_KEY`, rex reads everything through the provider. With both, a
provider that is down, refuses the key or has spent its rate limit costs nothing: rex asks
TMDB. Answers are kept in `tmdb_cache.db` under TMDB's own URLs whoever gave them, so a
server that changes provider, or drops one, keeps what it has read.

## What rex asks

`runtime/tmdb.rs` (`provider_request`) turns each TMDB read into the provider's request and
takes TMDB's own JSON out of the source named beside it. It always sends `sources=tmdb`: a
start reads one season list per season of every show it places, and a provider should not
ask its other sources each time.

| rex's TMDB read | Provider request | Source read |
|---|---|---|
| `/search/movie`, `/search/tv` (`query`, `year` / `first_air_date_year`) | `/api/metadata/search?query=&type=movie\|show&year=` | `tmdb` |
| `/find/{tt…}` | `/api/metadata/imdb:{tt…}?sources=tmdb` | `tmdbFind` |
| `/movie/{id}`, `/tv/{id}` | `/api/metadata/tmdb:{movie\|tv}:{id}?sources=tmdb` | `tmdb` |
| `/tv/{id}/season/{n}` | `/api/metadata/tmdb:tv:{id}?season={n}&sources=tmdb` | `tmdbSeason` |
| `/tv/{id}/season/{n}/episode/{e}` | `...&season={n}&episode={e}` | `tmdbEpisode` |

| The provider answers | rex |
|---|---|
| 200 with the source | Uses it, and keeps it |
| 200 with the source `null` | Asks TMDB |
| 404 | No such title, as TMDB's own 404 says; for a `find`, TMDB's empty find (TMDB's find does not know some IMDb episode ids DMM releases carry, Courage's `tt0549680` among them) |
| 400, 401, 429, 503, no answer | Asks TMDB |

## Checking a provider

`tests/test_feed_pack_folders.py` identifies and places real packs' files, and recording it
with a provider and no TMDB key runs every read above through the provider alone:

```bash
cp tests/fixtures/feed/pack_folders.json /tmp/pack_folders.json
REX_RECORD_ANSWERS=1 REX_METADATA_URL=https://provider.example REX_METADATA_KEY=... TMDB_KEY= \
  python3 tests/test_feed_pack_folders.py
cp /tmp/pack_folders.json tests/fixtures/feed/pack_folders.json   # recording rewrote it
```

Every check passes against DMM (18/18, 2026-10-15). The unit test
`the_provider_answers_first_and_tmdb_only_when_it_does_not` (`runtime/tmdb.rs`) holds DMM's
measured answers for each case in the table.
