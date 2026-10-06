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
- **Discoverable on the LAN.** plexsim answers Plex's own GDM discovery protocol
  on udp/32410 and udp/32414, so an official client on the same network finds it
  without plex.tv — and a real Plex Media Server on the same wire lists it in its
  own `/servers`.
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
- **The filter bar, for real.** Plex's filter query language, where the operator
  is part of the parameter NAME (`year%3E%3E=2000` reads as a field called
  `year>>`) and what a bare `=` means depends on the field's type; the 28 filters
  a movie library advertises and the different 15 a show library does, with every
  axis behind them; `/query/parse` so a saved filter can be read back; `group=`;
  and smart playlists and collections that are the query they store rather than a
  snapshot of it.
- **Claimed-server emulation**, calibrated field by field against the account's
  own real servers at `/identity`, `/media/providers`, `/:/prefs` and
  `/library/sections`.
- **Survives a restart**: watch state, edits, playlists, collections, history,
  sections, preferences and uploads are saved as they change, a row at a time, to
  a SQLite database (`plexsim.db` beside the catalog, or `--state FILE`) and
  restored at start. The catalog can live in the same database
  (`build_catalog.py --convert catalog.json --out plexsim.db`, then
  `--catalog plexsim.db`). With `--no-state` a restart reloads the catalog.
- **Published over a named Cloudflare tunnel**, so clients reach it without a
  port forward or a `*.plex.direct` certificate.

plexsim serves **anyone who can reach the port** unless you give it a token:
`--token <value>` (or `PLEXSIM_TOKEN`) makes it 401 everything but `/identity` and
`/web/*`, which is what a real PMS does. The startup line says which mode it is in.

## Quick start

plexsim is a native Rust server (`rust/`). Build it, then run it:

```bash
cargo build --release --manifest-path rust/Cargo.toml
cp catalog.example.json catalog.json     # or build one: python3 build_catalog.py --help
cp run.sh.example run.sh                 # fill in RD_TOKEN
./run.sh
```

Then open `http://127.0.0.1:32400/web/index.html` if you are serving a web
client bundle with `--web-root`, or point any Plex client at the host.

To build the catalog from your whole Real-Debrid account, identifying every torrent
first (a written-in id, its hash in DMM, then Plex's matcher and TMDB):

```bash
RD_TOKEN=... TMDB_KEY=... PLEX_TOKEN=... DMM_API_KEY=... \
    python3 build_catalog.py --library --out plexsim.db   # then --catalog plexsim.db
```

What each torrent was identified as, and how surely, is in `identify_report.json`.

### Running several servers: manager.py

`manager.py` is a control service on top of the server. It runs **no** plexsim server
until you start one: you create a server from a configuration (port, Real-Debrid key,
catalog, options), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts. Stdlib Python only.

```bash
./manager.py serve                       # API + page on http://127.0.0.1:32300; prints the admin token once
export RD_TOKEN=...                      # keys come from the environment, never the command line
./manager.py create living-room --port 32400 --catalog "$PWD/catalog.json" --gdm on
./manager.py start living-room
./manager.py list                        # status | logs | stop | restart | delete [--purge] | build
```

Each server gets `managed/servers/<name>/`, whose `plexsim.db` holds its catalog (copied
in; a JSON catalog is converted) and, on the native server, its state. Keys are stored in
`managed/manager.db` (mode 600) and passed to each server through its environment; the
API redacts them. `http://127.0.0.1:32300/` is a small page for the same operations (paste
the admin token from `managed/admin.token`). `build NAME` runs `build_catalog.py --library`
into the server's database as a background job.

Run the manager itself under the init system, so it outlives any shell. macOS (launchd,
`~/Library/LaunchAgents/com.plexsim.manager.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.plexsim.manager</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>/path/to/plexsim/manager.py</string><string>serve</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/plexsim/managed.log</string>
  <key>StandardErrorPath</key><string>/path/to/plexsim/managed.log</string>
</dict></plist>
```

Linux (systemd, `/etc/systemd/system/plexsim-manager.service`); `KillMode=mixed` lets the
manager's own SIGTERM stop its servers, which it restores at the next start:

```ini
[Unit]
Description=plexsim manager
After=network-online.target

[Service]
User=ben
WorkingDirectory=/path/to/plexsim
ExecStart=/usr/bin/python3 /path/to/plexsim/manager.py serve
KillMode=mixed
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### The Python oracle

The Python server (`server.py` and its modules) is kept in the tree as the
measured reference the native server is ported from and compared against. It
runs with the same flags (`python3 server.py ...`), and `PLEXSIM_SERVER=python`
points the black-box test suites at it. Its state file is a pickle (`state.pickle` by
default) that the native server does not read. The last Python-only `main` is the `python-server`
branch.

## Tests

The suites target the native server (`tests/server_command.py` starts
`rust/target/release/plexsim`); set `PLEXSIM_SERVER=python` to run them against
the Python oracle.

```bash
python3 tests/test_navigation.py   # replays a client's request sequence, checks shape
python3 tests/test_ui_surface.py   # the context menu and every server page, end to end
python3 tests/test_all.py          # play queue → decision → byte reads → frame decode
python3 tests/conformance.py       # drives plexsim with python-plexapi
python3 tests/test_persist.py      # --state survives restarts, crashes and catalog rebuilds
```

## Documentation

`docs/findings.md` is the record of what was measured against a real PMS and
against plex.tv — including the failures, which are the useful part.
`docs/protocol-traps.md` collects the measured implementation edge cases.
`CLAUDE.md` is the working contract for anyone (human or agent) changing this.

## Status

A spike that got further than expected. It is not a product, has no multi-user
story, and deliberately does not try to be a Plex replacement.
