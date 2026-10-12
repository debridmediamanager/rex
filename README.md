# rex

A Plex Media Server that owns no files.

rex answers the PMS API closely enough that the **official** Plex clients
treat it as a real server — it appears in the account's server list, browses
like a library, and direct-plays 1080p H.264 in Plex Web with zero dropped
frames. The media is never on disk: the library is what a source such as
[zurg](https://github.com/debridmediamanager/zurg) pushes to it, and every part read is a
Range read of that source.

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
- **A live library its sources push.** zurg (or any HTTP server, with a pusher of its own)
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

## Setup

A rex server is four things: the binary, a **catalog** (its identity and two empty
libraries), the **sources** that push the library and serve its bytes, and, if it
is to appear in your Plex apps, a **claim** on your Plex account with an HTTPS address.
The steps below go in that order.

### What you need

| | |
|---|---|
| A Linux or macOS host | rex is one binary with no runtime dependencies. Python 3 (stdlib only) runs `build_catalog.py` and `manager.py` |
| Rust, or Docker | to build it (below) |
| One or more byte sources | [zurg](https://github.com/debridmediamanager/zurg) (a build with the rex feed: `rex_url` in its `config.yml`), or any HTTP server that serves the files plus something that pushes them to rex's feed |
| A TMDB API key | free from [themoviedb.org](https://www.themoviedb.org/settings/api); it names what is pushed and powers Fix Match |
| Your Plex account token | the `X-Plex-Token` in the URL of any "View XML" link in Plex Web ([Plex's guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)). rex uses it for Plex's own metadata agent and for registering with plex.tv |
| An HTTPS address | only for use outside your LAN and in app.plex.tv: a certificate for a hostname you control (a `tailscale cert`, Let's Encrypt) or a named Cloudflare tunnel |
| FFmpeg | optional, for [transcoding](#transcoding) |

### 1. Build

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml       # -> rust/target/release/rex
```

Without a Rust toolchain on the host, build in Docker. The binary runs on any glibc
Linux of the same architecture:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/src -w /src/rust \
  -e CARGO_TARGET_DIR=/src/rust/target rust:1-bullseye cargo build --release
```

Run rex from the repository root, or keep its catalog there: it reads its measured
sidecar files (`*.json` at the top of the tree) beside the catalog, one directory up,
or in its working directory.

### 2. Sources: where the library and its bytes come from

rex reads no disk and lists nothing. Each source **pushes** its items to
`/rex/feed/<name>/...` with its own feed token, and when someone plays a file rex
fetches its bytes with a GET of `<url>/<path>`. A source can be zurg's WebDAV, nginx, a
NAS, object storage or `python3 -m http.server`. `Range` support and redirects are
handled, and as many sources as you like feed the same libraries.

Write `sources.json` beside the catalog, readable only by you (`chmod 600`):

```json
{
  "rd": {
    "url": "http://127.0.0.1:9999/dav",
    "user": "zurg-user", "password": "zurg-password",
    "token": "<a long random feed token>",
    "mount": "/mnt/zurg"
  },
  "ad": {"url": "http://127.0.0.1:9998/dav", "token": "<another token>", "mount": "/mnt/zurg_ad"},
  "nas": {"url": "https://nas.example/media", "bearer": "...", "token": "<another token>", "mount": "/nas"}
}
```

| Field | |
|---|---|
| `url` | where the bytes are: zurg's is `http://<zurg>:<port>/dav` |
| `user`/`password` or `bearer` | the credential rex sends to `url` (zurg's `username`/`password`, if set). Never forwarded to a host it redirects to |
| `token` | what this source pushes with. Make one per source: `openssl rand -hex 24` |
| `mount` | the folder its files appear under in the library (zurg's `mount_path`). Nothing has to be mounted there: it is a name, and each library's folders (step 3) must include `<mount>/<directory>` |

The source's name is the `rex_source` it pushes as. A single zurg can skip the file:
`ZURG_URL` (+ `ZURG_USER`/`ZURG_PASSWORD`), `REX_FEED_TOKEN` and `--zurg-mount` in the
environment define a source named `zurg`. The full contract, for writing a pusher of
your own, is [`docs/feed-api.md`](docs/feed-api.md).

### 3. Catalog: the server's identity and libraries

```bash
python3 build_catalog.py --seed --name den --mount /mnt/zurg --out rex.db
```

This writes a SQLite database holding a fixed `machineIdentifier` (derived from
`--name`, and kept if you re-seed), a Movies library at `/mnt/zurg/movies` and a TV Shows
library at `/mnt/zurg/shows`. A pushed item lands in the library whose folder holds its
file, and an item no library holds is rejected, so give every source's mount and every
directory you want shown:

```bash
python3 build_catalog.py --seed --name den --out rex.db \
  --mount /mnt/zurg --mount /mnt/zurg_ad --mount /nas \
  --movies movies --shows shows --shows anime
```

**List every source's mount before the first start.** Once rex has started, the
libraries it saved in `rex.db` win over the catalog, so re-seeding changes nothing, and
rex does not yet take Plex's "Edit Library → Add folders". A source added later gets
a library of its own instead: Manage → Libraries → Add Library on
`<its mount>/<directory>`.

rex keeps everything clients change (watch state, edits, playlists, collections,
history, preferences) in the same `rex.db`, written as it changes. Back it up with the
server stopped, or with `sqlite3 rex.db ".backup rex.db.bak"`.

### 4. First start, on your LAN

```bash
export TMDB_KEY=... PLEX_TOKEN=...
rust/target/release/rex --catalog rex.db --sources sources.json --port 32400 \
  --owner-username <your Plex username> --owner-email <your Plex email>
```

The first lines of the log name each source (`source rd: feed on at /rex/feed/`) and the state file.
Check it answers:

```bash
curl -s http://127.0.0.1:32400/identity      # machineIdentifier, version
curl -s -H "Authorization: Bearer <rd's token>" http://127.0.0.1:32400/rex/feed/rd/items   # {"size":0,...} until it pushes
```

- **Port 32400 and LAN discovery.** On 32400 rex answers Plex's LAN discovery (GDM) on
  udp/32410 and 32414, so apps on the same network find it. On a host that already runs
  Plex, give rex another port and `--gdm off`.
- **Who may connect.** Until it is claimed (step 6), rex serves anyone who can reach the
  port. Keep it on your LAN until then, or start it with `--token <secret>`, which makes
  it refuse everything but `/identity` and `/web` without that `X-Plex-Token`. Once
  claimed it accepts only its own token and the tokens plex.tv lists for it, as a real
  Plex Media Server does.
- **The web client at `/web`** is optional and comes from a Plex Media Server install:
  pass `--web-root .../Resources/Plug-ins-*/WebClient.bundle/Contents/Resources` (on
  Linux under `/usr/lib/plexmediaserver`). app.plex.tv works without it once the server
  is claimed.

### 5. Point zurg at it

In each zurg's `config.yml` (zurg's [`docs/config.md`](https://github.com/debridmediamanager/zurg/blob/main/docs/config.md) has the details):

```yaml
mount_path: /mnt/zurg              # the source's "mount" in sources.json
rex_url: http://<rex host>:32400   # turns on zurg's ffprobe and IMDb lookups for new releases
rex_feed_token: <the source's token>
rex_source: rd                     # the source's name in sources.json (default "zurg")
```

Restart zurg. Once its library has loaded it pushes everything it holds, then each
change as it happens, and the whole library again every `rex_snapshot_every_hours`
(6). Watch it arrive in rex's log (`FEED rd ...`) and with the `/rex/feed/rd/items`
call above.

**An item appears only once zurg has probed it with ffprobe**, because rex lists each
file with its streams. Items zurg never probed are held, not shown. For a library that
existed before you turned rex on, start the probing once with **Analyze & Tag Library**
on zurg's dashboard (or `POST /torrents/analyze`). It runs at about 50 files a minute,
so a large library fills over hours. Re-run it after a zurg restart until it finishes:
it skips what is already probed.

Leave zurg's `plex_server_url` pointing at your real Plex, if you have one. Pointed
at rex, zurg's matcher learns rex's own matches and pushes them back as exact ids, so
a wrong match can never be corrected by a later push.

### 6. Claim it, so it appears in your Plex apps

Claiming registers the server on your Plex account, the same way a real Plex Media
Server is claimed. **Do it once per server.** The claim is saved in `claim_state.json`
beside the catalog and survives restarts. Keep a backup of that file: a lost claim
means claiming again, and a claim gone wrong has signed every other server on the
account out of plex.tv before.

1. Start rex **without** `--announce-url`.
2. Open [plex.tv/claim](https://www.plex.tv/claim) signed in as the owner and copy the
   `claim-...` token. It is valid for four minutes.
3. Redeem it on rex itself:

   ```bash
   curl -s -X POST "http://127.0.0.1:32400/myplex/claim?token=claim-xxxxxxxxxxxxxxxx"
   ```

4. `curl -s http://127.0.0.1:32400/identity` now reports `claimed="1"`, and
   `claim_state.json` exists beside the catalog.

### 7. Reach it from outside: HTTPS and the announce

Plex apps outside your LAN, and app.plex.tv in a browser, connect only to an HTTPS
address that plex.tv lists for the server. rex has no `*.plex.direct` certificate, so
give it your own hostname, in one of two ways:

- **rex terminates TLS**, on the same port as plain HTTP:
  `--tls-cert host.crt --tls-key host.key`. The key must be PKCS#8 or RSA PEM; an EC
  key, which is what `tailscale cert` and many ACME clients write, needs converting
  first: `openssl pkcs8 -topk8 -nocrypt -in host.key -out host.pkcs8.key`. A
  `tailscale cert` hostname works for apps on your tailnet.
- **A named Cloudflare tunnel** in front: [`cloudflared.example.yml`](cloudflared.example.yml).
  Quick tunnels do not work for Plex clients.

Then restart rex with the addresses to register, the HTTPS one first and, optionally, a
LAN one:

```bash
--announce-url https://rex.example.com:32400,http://192.168.1.10:32400
```

With `--announce-url` (and `PLEX_TOKEN`) rex registers with plex.tv at start, holds
the event stream plex.tv reads as "online", and re-announces every 10 minutes. The server
then shows in app.plex.tv and the apps' server list. If it does not,
look for the `plex.tv` lines in rex's log: a missing claim, an expired `PLEX_TOKEN` or
an address plex.tv cannot reach are the usual causes.

### 8. Run it as a service

`run.sh.example` is the whole command line as a script: copy it to `run.sh` beside the
catalog and fill it in. It reads the keys from `rex.env` (`TMDB_KEY=` and `PLEX_TOKEN=`,
mode 600) and adds `--announce-url` once `claim_state.json` exists. `run.sh`, `rex.env`
and `sources.json` are gitignored. Under systemd (`~/.config/systemd/user/rex.service`,
then `systemctl --user enable --now rex`, and `loginctl enable-linger` so it runs
without a login):

```ini
[Unit]
Description=rex
After=network-online.target

[Service]
WorkingDirectory=%h/rex
ExecStart=%h/rex/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Under launchd:
`launchctl submit -l com.rex -o rex.log -e rex.err -- /bin/bash "$PWD/run.sh"`. To run
several servers on one host, use `manager.py` (below).

rex logs a request's query string, so a client that sends its token as
`?X-Plex-Token=` leaves it in the log. Keep the log private.

### Troubleshooting

| Symptom | Cause |
|---|---|
| A push answers `rejected: no movie or show library holds ...` | The item's file is not under any library folder: seed that mount before the first start, or Add Library on it (step 3) |
| zurg pushed but the library stays empty | Items are held until zurg probes them (step 5) |
| A part read answers 502, or 401/403 | rex cannot reach the source's `url` (502), or the source refused its credential (its own status): the log line `PART ... upstream` says which |
| The server is not in the Plex apps | Not claimed, no `--announce-url`, or the announced HTTPS address is not reachable from the internet (step 7) |
| `--announce-url needs PLEX_TOKEN` at start | `PLEX_TOKEN` is not in rex's environment |
| A second server on the same catalog exits 1 | The state file is locked by the first: one catalog per server |

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
