# Setting up rex

The README has the short version. This is the full one with every option.

## The steps

A rex server is four things: the binary, a **catalog** (its identity and two empty
libraries), the **sources** that push the library and serve its bytes, and, if it
is to appear in your Plex apps, a **claim** on your Plex account with an HTTPS address.
The steps below go in that order.

### What you need

| | |
|---|---|
| A Linux or macOS host | rex is one binary with no runtime dependencies. Python 3 (stdlib only) runs `oracle/build_catalog.py` and `oracle/manager.py` |
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
sidecar files (`data/`) from beside the catalog, one directory up, or its working
directory.

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
    "token": "<a long random feed token>"
  },
  "ad": {"url": "http://127.0.0.1:9998/dav", "token": "<another token>"},
  "nas": {"url": "https://nas.example/media", "bearer": "...", "token": "<another token>"}
}
```

| Field | |
|---|---|
| `url` | where the bytes are: zurg's is `http://<zurg>:<port>/dav` |
| `user`/`password` or `bearer` | the credential rex sends to `url` (zurg's `username`/`password`, if set). Never forwarded to a host it redirects to |
| `token` | what this source pushes with. Make one per source: `openssl rand -hex 24` |

The source's name is the `rex_source` it pushes as. A single zurg can skip the file:
`ZURG_URL` (+ `ZURG_USER`/`ZURG_PASSWORD`), `REX_FEED_TOKEN` and `--zurg-mount` in the
environment define a source named `zurg`. The full contract, for writing a pusher of
your own, is [`docs/feed-api.md`](feed-api.md).

### 3. Catalog: the server's identity and libraries

```bash
python3 oracle/build_catalog.py --seed --name den --out rex.db
```

This writes a SQLite database holding a fixed `machineIdentifier` (random, as a real
Plex Media Server's is, and kept if you re-seed), a Movies library holding the `movies` directory and a
TV Shows library holding `shows`. A pushed item lands in the library holding the
directory its `path` starts with (`movies/<release>/<file>`), whichever source sent it
and whatever path prefix its `file` carries. An item in no library's directory is
rejected, so name every directory your sources use:

```bash
python3 oracle/build_catalog.py --seed --name den --out rex.db \
  --movies movies --shows shows --shows anime
```

The folders are shown in Plex as `/mnt/zurg/<directory>`. `--mount` changes that prefix,
and it only matters for zurg's own Plex matcher, which matches a file by its full path,
so set it to zurg's `mount_path` if you point zurg's `plex_server_url` at rex.

**Name every directory before the first start.** Once rex has started, the
libraries it saved in `rex.db` win over the catalog, so re-seeding changes nothing, and
rex does not yet take Plex's "Edit Library → Add folders". A directory added later gets
a library of its own instead: Manage → Libraries → Add Library on it.

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
- **Who may connect.** Until it is claimed (step 6), rex answers only this machine and
  private addresses (10.x, 172.16-31.x, 192.168.x, IPv6 fc00::/7), as an unclaimed Plex
  Media Server does. Tailscale's 100.x addresses and anything arriving through a proxy with
  a public `X-Forwarded-For` are refused, and so is a request whose `Host` is a hostname
  rather than localhost or the address it reached: use `http://<LAN IP>:32400`. To admit
  other networks, set **List of IP addresses and networks that are allowed without auth**
  (`allowedNetworks`) in Settings > Network from the machine itself; once set, only
  localhost and those networks get in. Or start rex with `--token <secret>`, which makes
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

Claiming is optional and at your own risk. rex is not made by Plex and Plex could take
action against an account that registers it. Skip it and rex only answers on your own
network (step 4 "Who may connect"), or start it with `--token` and give that token to your
clients.

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

A claimed rex refuses another claim (409). To move it to another account, stop it, remove
`claim_state.json` and claim again. rex also refuses to store a claim when plex.tv answers
with the account's own `PLEX_TOKEN` instead of a server token, which happens when the
server's identity already has a device row on the account.

### 7. Reach it from outside: HTTPS and the announce

Plex apps outside your LAN, and app.plex.tv in a browser, connect only to an HTTPS
address that plex.tv lists for the server. rex has no `*.plex.direct` certificate, so
give it your own hostname, in one of two ways:

- **rex terminates TLS**, on the same port as plain HTTP:
  `--tls-cert host.crt --tls-key host.key`. The key must be PKCS#8 or RSA PEM; an EC
  key, which is what `tailscale cert` and many ACME clients write, needs converting
  first: `openssl pkcs8 -topk8 -nocrypt -in host.key -out host.pkcs8.key`. A
  `tailscale cert` hostname works for apps on your tailnet.
- **A named Cloudflare tunnel** in front: [`examples/cloudflared.example.yml`](../examples/cloudflared.example.yml).
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

`examples/run.sh.example` is the whole command line as a script: copy it to `run.sh` at the
top of the checkout or release, beside `rust/` and the catalog, and fill it in. It reads the
keys from `rex.env` (`TMDB_KEY=` and `PLEX_TOKEN=`, mode 600) and adds `--announce-url`
once `claim_state.json` exists. `run.sh`, `rex.env` and `sources.json` are gitignored.
Under systemd (`~/.config/systemd/user/rex.service`, then `systemctl --user enable --now
rex`, and `loginctl enable-linger` so it runs without a login):

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
several servers on one host, use `oracle/manager.py` (below).

rex logs a request's query string, so a client that sends its token as
`?X-Plex-Token=` leaves it in the log. Keep the log private.

### Windows

The Windows build is `rust\target\release\rex.exe` in the zip, and every step above works
the same in PowerShell with `python` for `python3`. Three things differ:

- `examples/run.sh.example` and `oracle/manager.py` are Linux and macOS only. Start `rex.exe` from a
  scheduled task (Task Scheduler, "At startup", "Run whether user is logged on or not") to
  keep it running.
- `claim_state.json` and `sources.json` are protected by the folder they sit in, not by file
  modes, so keep the rex folder inside your user profile.
- LAN discovery cannot share its port with a Plex Media Server on the same Windows machine.
  Run rex with `--gdm off` there, as you would with another port on Linux.

### Troubleshooting

| Symptom | Cause |
|---|---|
| A push answers `rejected: no movie or show library holds ...` | No library holds the item's directory: name it with `--movies`/`--shows` before the first start, or Add Library on it (step 3) |
| zurg pushed but the library stays empty | Items are held until zurg probes them (step 5) |
| A part read answers 502, or 401/403 | rex cannot reach the source's `url` (502), or the source refused its credential (its own status): the log line `PART ... upstream` says which |
| The server is not in the Plex apps | Not claimed, no `--announce-url`, or the announced HTTPS address is not reachable from the internet (step 7) |
| `--announce-url needs PLEX_TOKEN` at start | `PLEX_TOKEN` is not in rex's environment |
| A second server on the same catalog exits 1 | The state file is locked by the first: one catalog per server |

### Running several servers: oracle/manager.py

`oracle/manager.py` is a control service on top of the server. It runs **no** rex server
until you start one: you create a server from a configuration (port, zurg, keys,
catalog, options), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts. Stdlib Python only.

```bash
oracle/manager.py serve                  # API + page on http://127.0.0.1:32300; prints the admin token once
export ZURG_URL=... ZURG_USER=... ZURG_PASSWORD=... TMDB_KEY=... PLEX_TOKEN=...
                                         # keys come from the environment, never the command line
oracle/manager.py create living-room --port 32400 --catalog "$PWD/catalog.json" --gdm on
oracle/manager.py start living-room
oracle/manager.py list                   # status | logs | stop | restart | delete [--purge] | build

# or from its keys alone: an empty library at zurg's mount, for zurg to fill
# (its feed: --extra-arg=--sources --extra-arg=FILE, docs/feed-api.md)
oracle/manager.py create den --port 32401 --gdm on --zurg-mount /mnt/zurg
oracle/manager.py build den --start      # build_catalog.py --seed, then start (restart if running)
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
    <string>/usr/bin/python3</string><string>/path/to/rex/oracle/manager.py</string><string>serve</string>
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
User=youruser
WorkingDirectory=/path/to/rex
ExecStart=/usr/bin/python3 /path/to/rex/oracle/manager.py serve
KillMode=mixed
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=multi-user.target
```


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


### By platform

Each platform runs the transcoder suite in CI (`tests/test_native_transcode.py`, against
jellyfin-ffmpeg 8.1.3), on the CPU path, since the runners have no GPU.

| Platform | GPU paths rex knows | Notes |
|---|---|---|
| Linux amd64 | VA-API (Intel, AMD), NVENC (NVIDIA), else x264 | VA-API measured on an Intel HD 510; the suite runs in CI |
| Linux arm64 | the same, which on most ARM boards means x264 | not run in CI (it is emulated there) |
| macOS | none: x264 always (no VideoToolbox path) | the suite runs in CI on Apple Silicon |
| Windows | NVENC (NVIDIA), else x264 (no Quick Sync or AMF path) | the suite runs in CI and was run on a Windows 11 machine with an AMD GPU |

Use a recent FFmpeg. Ubuntu 22.04's own (`apt install ffmpeg`, 4.4) starts a far seek a
segment early; jellyfin-ffmpeg 8 has portable builds for every platform above
(`jellyfin-ffmpeg_8.1.3-1_portable_*` on its releases page).

On Linux an FFmpeg dies with rex and a restart sweeps up any left over. On macOS and
Windows a stopped rex still stops its FFmpegs, but if rex crashes they run on until their
conversion ends.
