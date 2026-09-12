# plexsim

A Plex Media Server that owns no files.

plexsim answers the PMS API closely enough that the **official** Plex clients
treat it as a real server — it appears in the account's server list, browses
like a library, and direct-plays 1080p H.264 in Plex Web with zero dropped
frames. The media is never on disk: every part read resolves a Real-Debrid link
at request time.

Think [remux](https://github.com/lostb1t/remux), but for Plex instead of
Jellyfin.

## What works

- **Playback in the official client.** `PLAYQUEUE → DECISION direct play →
  PART 200`, verified byte-identical across proxy, redirect and CDN paths
  (`e49cf18e698064cddc10a13ea0926fb3`), with real frames decoded at an offset
  rather than a trusted HTTP 200.
- **A real library shape.** Movies in a movie section; shows with seasons and
  episodes, synthesised parents, `childCount`/`leafCount`, On Deck, Extras and
  per-section preferences.
- **Real identification.** TMDB gives episode titles, summaries, cast, posters
  and art; `plex://show|season|episode|movie/<24hex>` guids are minted per item
  with external ids on the show.
- **The whole UI, not just browsing.** Every entry in an item's context menu —
  Mark as Played, Edit, Fix Match, Refresh, Analyze, Split, Merge, Download,
  Delete, Get Info, Add to Playlist, Add to Collection, Play Next — and every
  server page: Status, all ten Settings pages (writable), and Manage with
  library add/rename/delete, scans, playlists and collections.
- **Claimed-server emulation**, calibrated field by field against the account's
  own real servers at `/identity`, `/media/providers`, `/:/prefs` and
  `/library/sections`.
- **Published over a named Cloudflare tunnel**, so clients reach it without a
  port forward or a `*.plex.direct` certificate.

## Quick start

```bash
cp catalog.example.json catalog.json     # or build one: python3 build_catalog.py --help
cp run.sh.example run.sh                 # fill in RD_TOKEN
./run.sh
```

Then open `http://127.0.0.1:32400/web/index.html` if you are serving a web
client bundle with `--web-root`, or point any Plex client at the host.

## Tests

```bash
python3 tests/test_navigation.py   # replays a client's request sequence, checks shape
python3 tests/test_ui_surface.py   # the context menu and every server page, end to end
python3 tests/test_all.py          # play queue → decision → byte reads → frame decode
```

## Documentation

`docs/findings.md` is the record of what was measured against a real PMS and
against plex.tv — including the failures, which are the useful part. `CLAUDE.md`
is the working contract for anyone (human or agent) changing this.

## Status

A spike that got further than expected. It is not a product, has no multi-user
story, and deliberately does not try to be a Plex replacement.
