# rex

A Plex Media Server that owns no files.

rex answers the PMS API closely enough that the **official** Plex clients
treat it as a real server — it appears in the account's server list, browses
like a library, and direct-plays 1080p H.264 in Plex Web with zero dropped
frames. The media is never on disk: the library is what a source such as
[zurg](https://github.com/debridmediamanager/zurg) pushes to it, and every part read is a
Range read of that source.

Think [remux](https://github.com/lostb1t/remux), but for Plex instead of
Jellyfin.

## What works

- **Playback in the official client.** `PLAYQUEUE → DECISION direct play →
  PART 200`, verified byte-identical across proxy, redirect and CDN paths
  (`e49cf18e698064cddc10a13ea0926fb3`), with real frames decoded at an offset
  rather than a trusted HTTP 200.
- **Transcoding, on the GPU.** With an FFmpeg configured (`REX_TRANSCODER`), what a
  client cannot direct-play is converted while it plays, over HLS (Plex Web from
  app.plex.tv, the Apple apps) or DASH (the web client rex serves): a remux when the
  codecs fit, H.264 otherwise, with the GPU decoding, scaling and encoding when it can
  (VA-API or NVENC). Plex Web starts a converted HEVC film in about 2 s and seeks
  in about 2.5 s on a 2-core Pentium's iGPU.
- **Discoverable on the LAN.** rex answers Plex's own GDM discovery protocol
  on udp/32410 and udp/32414, so an official client on the same network finds it
  without plex.tv — and a real Plex Media Server on the same wire lists it in its
  own `/servers`.
- **A real library shape.** Movies in a movie section; shows with seasons and
  episodes, synthesised parents, `childCount`/`leafCount`, On Deck, Extras and
  per-section preferences.
- **A live library its sources push.** zurg (or any HTTP server that honours `Range`)
  pushes the items it holds to rex's feed API (`docs/feed-api.md`); rex lists
  and queries nothing. A new file becomes a row with the source's ffprobe, an
  unprobed one waits until its probe arrives, a file that is gone is trashed the way a
  real PMS trashes it, and every ratingKey is kept for good.
- **Real identification.** A pushed IMDb id is exact (and a late one re-identifies the
  row on its key); otherwise the release name
  goes to Plex's own matcher and TMDB, and their agreement is the confidence. TMDB
  gives episode titles, summaries, cast, posters and art, and Plex's metadata agent
  the real `plex://show|season|episode|movie/<24hex>` guids.
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
  a SQLite database (`rex.db` beside the catalog, or `--state FILE`) and
  restored at start; a library zurg built is kept row by row, on the same keys,
  whatever the catalog file says. The catalog can live in the same database
  (`build_catalog.py --seed` or `--convert catalog.json`, `--out rex.db`, then
  `--catalog rex.db`). With `--no-state` a restart reloads the catalog.
- **Published over a named Cloudflare tunnel**, so clients reach it without a
  port forward or a `*.plex.direct` certificate.

rex serves **anyone who can reach the port** unless you give it a token:
`--token <value>` (or `REX_TOKEN`) makes it 401 everything but `/identity` and
`/web/*`, which is what a real PMS does. The startup line says which mode it is in.

## Quick start

rex is a native Rust server (`rust/`) that zurg feeds. Build it, write its catalog --
the server's identity and two libraries whose locations are zurg's -- and run it:

```bash
cargo build --release --manifest-path rust/Cargo.toml
python3 build_catalog.py --seed --name den --zurg-mount /mnt/zurg --out rex.db
#   a movie library at /mnt/zurg/movies and a show library at /mnt/zurg/shows
#   (--movies/--shows name other zurg directories)
export ZURG_URL=http://127.0.0.1:9999 ZURG_USER=... ZURG_PASSWORD=...   # zurg's username/password, if set
export REX_FEED_TOKEN=$(openssl rand -hex 24)   # what zurg pushes with
export TMDB_KEY=... PLEX_TOKEN=...       # identification; without them new items stay unmatched
rust/target/release/rex --catalog rex.db --zurg-mount /mnt/zurg --port 32400
```

`ZURG_URL` makes zurg the source named `zurg`: a part's bytes are read from
`$ZURG_URL/dav/...`. More sources, or one that is not zurg, go in a `--sources` file
(`docs/feed-api.md`). `run.sh.example` is the same as a script. In zurg's `config.yml`:

```yaml
mount_path: /mnt/zurg            # rex's --zurg-mount; zurg needs no real mount for rex
rex_url: http://127.0.0.1:32400   # zurg probes every new release and identifies it before filing
rex_feed_token: <REX_FEED_TOKEN>   # zurg pushes its library to rex
plex_server_url: http://127.0.0.1:32400   # optional: for zurg's matcher; zurg sends rex no scans
plex_token: anything             # rex's --token when it has one
plex_settings_policy: off        # zurg leaves rex's preferences alone
```

zurg pushes a full snapshot once its library has loaded and every few hours, and each
change as it happens. An item lands in the library whose `Location` holds its file
(`<mount_path>/<directory>/...`), and zurg's matcher reads each item's Part `file`
(`<mount_path>/<directory>/<release>/<file>`) and `imdb://` Guid back.
Then open `http://127.0.0.1:32400/web/index.html` if you are serving a web client bundle
with `--web-root`, or point any Plex client at the host.

### Running several servers: manager.py

`manager.py` is a control service on top of the server. It runs **no** rex server
until you start one: you create a server from a configuration (port, zurg, keys,
catalog, options), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts. Stdlib Python only.

```bash
./manager.py serve                       # API + page on http://127.0.0.1:32300; prints the admin token once
export ZURG_URL=... ZURG_USER=... ZURG_PASSWORD=... TMDB_KEY=... PLEX_TOKEN=...
                                         # keys come from the environment, never the command line
./manager.py create living-room --port 32400 --catalog "$PWD/catalog.json" --gdm on
./manager.py start living-room
./manager.py list                        # status | logs | stop | restart | delete [--purge] | build

# or from its keys alone: an empty library at zurg's mount, for zurg to fill
# (its feed: --extra-arg=--sources --extra-arg=FILE, docs/feed-api.md)
./manager.py create den --port 32401 --gdm on --zurg-mount /mnt/zurg
./manager.py build den --start           # build_catalog.py --seed, then start (restart if running)
```

Each server gets `managed/servers/<name>/`, whose `rex.db` holds its catalog (copied
in; a JSON catalog is converted) and, on the native server, its state. Keys are stored in
`managed/manager.db` (mode 600) and passed to each server through its environment; the
API redacts them. `http://127.0.0.1:32300/` is a small page for the same operations (paste
the admin token from `managed/admin.token`). `build NAME` runs `build_catalog.py --seed`
into the server's database as a background job.

Run the manager itself under the init system, so it outlives any shell. macOS (launchd,
`~/Library/LaunchAgents/com.rex.manager.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.rex.manager</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>/path/to/rex/manager.py</string><string>serve</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/rex/managed.log</string>
  <key>StandardErrorPath</key><string>/path/to/rex/managed.log</string>
</dict></plist>
```

Linux (systemd, `/etc/systemd/system/rex-manager.service`); `KillMode=mixed` lets the
manager's own SIGTERM stop its servers, which it restores at the next start:

```ini
[Unit]
Description=rex manager
After=network-online.target

[Service]
User=ben
WorkingDirectory=/path/to/rex
ExecStart=/usr/bin/python3 /path/to/rex/manager.py serve
KillMode=mixed
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### The Python oracle

The Python server (`server.py` and its modules) is kept in the tree as the
measured reference the native server is ported from and compared against. It
runs with the same flags (`python3 server.py ...`), and `REX_SERVER=python`
points the black-box test suites at it. Its state file is a pickle (`state.pickle` by
default) that the native server does not read. The last Python-only `main` is the `python-server`
branch.

## Transcoding

Off by default: rex then answers a conversion request the way a server with no
encoder does. Point `REX_TRANSCODER` at an FFmpeg to turn it on. jellyfin-ffmpeg is
the one it is measured with: its `.deb` carries its own Intel driver, so it runs
from wherever it is unpacked.

```bash
curl -LO https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/jellyfin-ffmpeg8_8.1.3-1-jammy_amd64.deb
dpkg -x jellyfin-ffmpeg8_8.1.3-1-jammy_amd64.deb ~/rex-ffmpeg
J=~/rex-ffmpeg/usr/lib/jellyfin-ffmpeg
export REX_TRANSCODER=$J/ffmpeg LD_LIBRARY_PATH=$J/lib LIBVA_DRIVERS_PATH=$J/lib/dri LIBVA_DRIVER_NAME=iHD
sudo usermod -aG render "$USER"      # the GPU's /dev/dri/renderD128; log in again after
```

| Variable | Default | |
|---|---|---|
| `REX_TRANSCODER` | off | the FFmpeg binary |
| `REX_TRANSCODE_HW` | `auto` | `vaapi`, `cuda` or `none` (`auto` tries VA-API, then NVENC) |
| `REX_TRANSCODE_DEVICE` | `/dev/dri/renderD128` | the VA-API device |
| `REX_TRANSCODE_DIR` | `$TMPDIR/rex-transcode-<port>` | segments; cleared at start |

The startup log says what the probe found (`transcoder: ... hardware vaapi encode=true
decode=[h264,hevc,mpeg2video]`). A conversion runs on the GPU end to end when it can
decode the source there, uploads CPU-decoded frames when it cannot (AV1 on older Intel,
HDR, which is tonemapped on the CPU), and falls back to x264 when there is no device.
Every conversion is one FFmpeg reading the source over HTTP; nothing is downloaded
first.

## Tests

The suites target the native server (`tests/server_command.py` starts
`rust/target/release/rex`); set `REX_SERVER=python` to run them against
the Python oracle.

```bash
python3 tests/test_navigation.py   # replays a client's request sequence, checks shape
python3 tests/test_ui_surface.py   # the context menu and every server page, end to end
python3 tests/test_all.py          # play queue → decision → byte reads → frame decode
python3 tests/conformance.py       # drives rex with python-plexapi
python3 tests/test_persist.py      # --state survives restarts, crashes and catalog rebuilds
python3 tests/test_feed.py         # the feed API against a stub source: upsert, hold, trash, snapshot, migration
python3 tests/test_native_transcode.py  # HLS and DASH conversions of generated clips, decoded segment by segment
```

## Documentation

`docs/findings.md` is the record of what was measured against a real PMS and
against plex.tv — including the failures, which are the useful part.
`docs/protocol-traps.md` collects the measured implementation edge cases.
`CLAUDE.md` is the working contract for anyone (human or agent) changing this.

## Status

A spike that got further than expected. It is not a product, has no multi-user
story, and deliberately does not try to be a Plex replacement.
