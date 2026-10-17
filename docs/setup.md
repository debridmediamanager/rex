# Setting up rex

The README has the short version. This is the full one with every option.

## The steps

A rex server is four things: the binary, a **catalog** (its identity and two empty
libraries, made on its first start), the **sources** that register with it, push the
library and serve its bytes, and, if it is to appear in your Plex apps, a **claim** on your
Plex account with an HTTPS address. Setting one up is one command:

```bash
rust/target/release/rex --catalog rex.db
```

The steps below go through it in order.

### What you need

| | |
|---|---|
| A Linux, macOS or Windows host | rex is one binary and its `data/` sidecars, with no other runtime dependencies. The same binary writes the catalog (`rex catalog`) and runs several servers (`rex manager`) |
| Rust, or Docker | to build it (below) |
| One or more byte sources | [zurg](https://github.com/debridmediamanager/zurg) (a build with the rex feed: `rex_url` in its `config.yml`), or any HTTP server that serves the files plus something that pushes them to rex's feed |
| A TMDB API key | free from [themoviedb.org](https://www.themoviedb.org/settings/api); it names what is pushed and powers Fix Match |
| Your Plex account token | not needed. A claimed rex (step 6) asks plex.tv and Plex's own metadata agent with the server token the claim gives it. Only an unclaimed server that should still match with Plex's agent takes `PLEX_TOKEN`: the `X-Plex-Token` in the URL of any "View XML" link in Plex Web ([Plex's guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| An HTTPS address | only for use outside your LAN and in app.plex.tv: a certificate for a hostname you control (a `tailscale cert`, Let's Encrypt) |
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

rex reads no disk and lists nothing, and nothing about a source is set on rex's side. Each
source **registers** itself (`PUT /rex/sources/<name>`: the URL its files are under, the
credential for it, its mount) and **pushes** its items to `/rex/feed/<name>/...`; when
someone plays a file rex fetches its bytes with a GET of `<url>/<path>`. A source can be
zurg's WebDAV, nginx, a NAS, object storage or `python3 -m http.server`. `Range` support and
redirects are handled, and as many sources as you like feed the same libraries. rex keeps
each registration, credential included, in `rex.db`, so a restart keeps playing before the
source registers again.

zurg does both with nothing but `rex_url` in its `config.yml` (step 5). Anything else
registers and pushes the same way: [`docs/feed-api.md`](feed-api.md) is the full contract,
for writing a pusher of your own.

Registering and pushing take no token and check no network, claimed or not, so a source may
name rex by address or host name. Anyone who can reach rex's port can register a source and
delete one with everything it pushed: keep the port off the open internet
([`docs/feed-api.md`](feed-api.md), "Who may call it").

Registering is the only way a source exists: `ZURG_URL` is no longer read, and a zurg build
from before the sources API, which never registers, needs updating. Rows rex already has keep
their source and play again once it registers. `DELETE /rex/sources/<name>` removes a source
and everything it pushed from rex, after a confirmation ([`docs/feed-api.md`](feed-api.md)).

### 3. Catalog: the server's identity and libraries

The first start makes the catalog when the `--catalog` file does not exist yet: rex seeds it
as `rex catalog --seed` would, named by `--friendly-name` (default: the machine's host name, as a real PMS names itself), its libraries
under `--zurg-mount`, and logs `seeded rex.db: ...`. Or seed it yourself before the first
start, which is how to name more directories (below):

```bash
rust/target/release/rex catalog --seed --name den --out rex.db
```

Either way this is a SQLite database holding a fixed `machineIdentifier` (random, as a real
Plex Media Server's is, and kept if you re-seed), a movie library titled Debrid Movies holding the `movies`
directory and a show library titled Debrid TV holding `shows`. A pushed item lands in the library holding the
directory its `path` starts with (`movies/<release>/<file>`), whichever source sent it
and whatever path prefix its `file` carries. An item in no library's directory goes to
the first library of its kind: an episode (by the source's `kind`, else an episode number
in its name) to a show library, anything else to a movie library. To give a directory a
library of its own, name it when seeding:

```bash
rust/target/release/rex catalog --seed --name den --out rex.db \
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
history, preferences), and each source's registration, in the same `rex.db`, written as it
changes. It holds the sources' credentials, so keep it private. Back it up with the
server stopped, or with `sqlite3 rex.db ".backup rex.db.bak"`.

### 4. First start, on your LAN

```bash
export TMDB_KEY=...
rust/target/release/rex --catalog rex.db --port 32400
```

`--owner-username` and `--owner-email` are only display names before a claim; a claimed
server takes both from the claim.

The first lines of the log say whether it seeded the catalog, name each source it already
knows (`source rd: registered, feed at /rex/feed/rd/`, or `no source yet`) and the state
file.

- **A metadata provider.** `REX_METADATA_URL` points rex at a server that answers TMDB's
  reads for it ([metadata-api.md](metadata-api.md)), with `REX_METADATA_KEY` if it takes a key.
  DMM is one for its sponsors (`https://debridmediamanager.com` and the 64-character key from
  gatekeeper): the same answers from DMM's cache, so a library's shows and seasons are mostly
  already there. rex asks the provider first and TMDB itself only when it does not answer, so
  with `TMDB_KEY` as well a lapsed key changes nothing; with the provider alone no `TMDB_KEY`
  is needed.
- **Anime and long shows.** Their TMDB season and episode come from the source: zurg with a
  sponsor `dmm_api_key` asks DMM and pushes each file placed; rex numbers anything pushed
  without a placement by its name. Upgrading: `REX_ANIME_LISTS` and `REX_IMDB_EPISODES` are no
  longer read, so an install that still sets them starts as before, and the
  `anime-list-master.xml`, `anime-offline-database.jsonl`, `title.episode.tsv.gz` and
  `imdb-episodes.db` they kept beside the catalog are ignored (delete them if you like). On the
  first start, episodes they had renumbered and no source placed go back to the numbers their
  names give, on the same keys, until the source pushes them placed.

Check it answers:

```bash
curl -s http://127.0.0.1:32400/identity      # machineIdentifier, version
curl -s http://127.0.0.1:32400/rex/sources            # {"size":0} until a source registers
curl -s http://127.0.0.1:32400/rex/feed/rd/items       # what rd pushed, once it has registered
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
rex_url: http://<rex's address>:32400   # turns on zurg's ffprobe and IMDb lookups for new releases
rex_source: rd                          # optional: the name it registers as (default "zurg")
```

That is all rex needs from zurg: an address or a host name (`http://rex:32400` from a
container) both work. Restart zurg. Once its library has loaded it registers with rex,
pushes everything it holds, then each change as it happens, and the whole library again
every `rex_snapshot_every_hours` (6), registering again at the start of each. Watch it
arrive in rex's log (`SOURCES rd registered ...`, `FEED POST /rex/feed/rd/items -> 200`) and with the
`/rex/sources` and `/rex/feed/rd/items` calls above.

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

1. Start rex, with or without `--announce-url` (below): an unclaimed rex announces nothing.
2. Open [plex.tv/claim](https://www.plex.tv/claim) signed in as the owner and copy the
   `claim-...` token. It is valid for four minutes.
3. Redeem it on rex itself:

   ```bash
   curl -s -X POST "http://127.0.0.1:32400/myplex/claim?token=claim-xxxxxxxxxxxxxxxx"
   ```

4. `curl -s http://127.0.0.1:32400/identity` now reports `claimed="1"`, and
   `claim_state.json` exists beside the catalog.

From the claim on rex asks plex.tv with the server token it stores there: the announce
(step 7), and Plex's own metadata agent, which then matches what sources push alongside TMDB
with no restart. `PLEX_TOKEN` is not used once there is a claim.

A claimed rex refuses another claim (409). To move it to another account, stop it, remove
`claim_state.json` and claim again. rex also refuses to store a claim when plex.tv answers
with the account's own `PLEX_TOKEN` instead of a server token, which happens when the
server's identity already has a device row on the account. An unclaimed rex announces
nothing, `PLEX_TOKEN` or not, so it never writes that row: plex.tv applies an announce to the
device that owns its token, and an account token's device is another of your devices.

### 7. Reach it from outside: HTTPS and the announce

Plex apps outside your LAN, and app.plex.tv in a browser, connect only to an HTTPS
address that plex.tv lists for the server. rex has no `*.plex.direct` certificate, so
give it your own hostname, with rex terminating TLS:

- **rex terminates TLS**, on the same port as plain HTTP:
  `--tls-cert host.crt --tls-key host.key`. The key must be PKCS#8 or RSA PEM; an EC
  key, which is what `tailscale cert` and many ACME clients write, needs converting
  first: `openssl pkcs8 -topk8 -nocrypt -in host.key -out host.pkcs8.key`. A
  `tailscale cert` hostname works for apps on your tailnet.

Do not put a Cloudflare tunnel in front. Every byte would pass through your host and then
Cloudflare. Cloudflare also drops the `Range` of a file's first read. rex then serves the
whole file and every play start pulls all of it from your source (measured in
docs/findings.md under "The unavoidable trade-off").

Then restart rex with the addresses to register, the HTTPS one first and, optionally, a
LAN one:

```bash
--announce-url https://rex.example.com:32400,http://192.168.1.10:32400
```

They can be there from the first start: rex logs that it waits for a claim and registers
them the moment it is claimed. With `--announce-url` a claimed rex registers with plex.tv at start, with its server token,
holds the event stream plex.tv reads as "online", and re-announces every 10 minutes. The
server then shows in app.plex.tv and the apps' server list. If it does not, look for the
`plex.tv` lines in rex's log: a missing claim (`waiting for a claim`) or an address plex.tv
cannot reach are the usual causes.

### 8. Run it as a service

`examples/run.sh.example` is the whole command line as a script: copy it to `run.sh` at the
top of the checkout or release, beside `rust/` and the catalog, and fill it in. It reads the
keys from `rex.env` (`TMDB_KEY=`, and `PLEX_TOKEN=` only if you want it; mode 600) and adds
`--announce-url`. `run.sh` and `rex.env` are gitignored.
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
several servers on one host, use `rex manager` (below).

rex logs a request's query string, so a client that sends its token as
`?X-Plex-Token=` leaves it in the log. Keep the log private.

### Windows

The Windows build is `rust\target\release\rex.exe` in the zip, and every step above works
the same in PowerShell with `rust\target\release\rex.exe` for `rust/target/release/rex`. Three things differ:

- `examples/run.sh.example` and `rex manager` are Linux and macOS only. Start `rex.exe` from a
  scheduled task (Task Scheduler, "At startup", "Run whether user is logged on or not") to
  keep it running.
- `claim_state.json` and `rex.db`, which holds the sources' credentials, are protected by
  the folder they sit in, not by file modes, so keep the rex folder inside your user profile.
- LAN discovery cannot share its port with a Plex Media Server on the same Windows machine.
  Run rex with `--gdm off` there, as you would with another port on Linux.

### Docker

`ghcr.io/debridmediamanager/rex` is each release's Linux build (amd64 and arm64) in an image,
published by `.github/workflows/docker.yml`: `latest`, `X.Y.Z` and `X.Y`. It is private, as
the repository is: log in with a classic GitHub token that has `read:packages` and `repo`,
the same as for zurg's image.

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

Everything rex writes (`rex.db`, `claim_state.json`, the TMDB cache) goes to `/config`.
Arguments that start with a flag run the server on `/config/rex.db` (`REX_CATALOG` moves
it); a subcommand runs as given, so naming more directories before the first start is:

```bash
docker compose run --rm rex catalog --seed --name den --out /config/rex.db \
  --movies movies --shows shows --shows anime
```

```yaml
services:
  rex:
    image: ghcr.io/debridmediamanager/rex:latest
    container_name: rex
    restart: unless-stopped
    network_mode: host        # LAN discovery (GDM), and zurg's registered address reachable as on the host
    environment:
      TMDB_KEY: your_tmdb_key
    volumes:
      - ./rex:/config
```

The image starts rex on `/config/rex.db` with `--zurg-mount /zurg_mnt/zurg`, where zurg's
Docker setup mounts, so the compose file needs no `command:`; one given adds to those.

zurg's `rex_url` is then `http://<the host's LAN IP>:32400`, and the claim (step 6) is the
same `curl` on the host. Certificates for step 7 go in `/config` too:
`--tls-cert /config/host.crt --tls-key /config/host.pkcs8.key`. Without host networking,
publish `32400:32400` and add `--gdm off`; apps then find rex only once it is claimed, and
rex must still reach the address zurg registers. The image carries no FFmpeg, so
`REX_TRANSCODER` needs one mounted in. rex saves its state on `docker stop`.

### Troubleshooting

| Symptom | Cause |
|---|---|
| A push answers 404 `no such source: PUT /rex/sources/... first` | The source has not registered: a zurg from before the sources API never does, update it (step 2) |
| Every part read answers 502 | No source is registered (`curl -s http://127.0.0.1:32400/rex/sources`), or rex cannot reach the one that is |
| A push answers `rejected: no movie or show library holds ...` | The catalog has no movie or show library at all: name one with `--movies`/`--shows` before the first start, or Add Library on it (step 3) |
| zurg pushed but the library stays empty | Items are held until zurg probes them (step 5) |
| A part read answers 502, or 401/403 | rex cannot reach the source's `url` (502), or the source refused its credential (its own status): the log line `PART ... upstream` says which |
| The server is not in the Plex apps | Not claimed, no `--announce-url`, or the announced HTTPS address is not reachable from the internet (step 7) |
| The log says `--announce-url: waiting for a claim` | The server is not claimed: claim it (step 6) and it announces at once |
| A second server on the same catalog exits 1 | The state file is locked by the first: one catalog per server |

### Running several servers: rex manager

`rex manager` is a control service on top of the server. It runs **no** rex server
until you start one: you create a server from a configuration (port, keys,
zurg's mount, catalog, options), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts.

```bash
rex manager serve                        # API + page on http://127.0.0.1:32300; prints the admin token once
export TMDB_KEY=...                      # keys come from the environment, never the command line
rex manager create living-room --port 32400 --catalog "$PWD/catalog.json" --gdm on
rex manager start living-room
rex manager list                         # status | logs | stop | restart | delete [--purge] | build

# or from its keys alone: an empty library at zurg's mount, for zurg to fill
# (zurg registers with it and pushes once its rex_url names the port)
rex manager create den --port 32401 --gdm on --zurg-mount /mnt/zurg
rex manager build den --start            # rex catalog --seed, then start (restart if running)
```

Each server gets `managed/servers/<name>/`, whose `rex.db` holds its catalog (copied
in; a JSON catalog is converted) and, on the native server, its state. Keys are stored in
`managed/manager.db` (mode 600) and passed to each server through its environment; the
API redacts them. `http://127.0.0.1:32300/` is a small page for the same operations (paste
the admin token from `managed/admin.token`). `build NAME` runs `rex catalog --seed`
into the server's database as a background job.

Run the manager itself under the init system, so it outlives any shell. macOS (launchd,
`~/Library/LaunchAgents/com.rex.manager.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.rex.manager</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/rex/rust/target/release/rex</string><string>manager</string><string>serve</string>
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
ExecStart=/path/to/rex/rust/target/release/rex manager serve
KillMode=mixed
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=multi-user.target
```


## Transcoding

Off by default: rex then answers a conversion request the way a server with no
encoder does. Point `REX_TRANSCODER` at an FFmpeg to turn it on. jellyfin-ffmpeg is
the one it is measured with. Install its `.deb` as a package: it lands in
`/usr/lib/jellyfin-ffmpeg` beside any system FFmpeg, with its own Intel driver. Unpacked
anywhere else (`dpkg -x`) its libva still looks for that driver under
`/usr/lib/jellyfin-ffmpeg/lib/dri`, ignores `LIBVA_DRIVERS_PATH`, and falls back to the
system's older iHD, which on a J4105 could not convert 10-bit HEVC (measured 2026-10-17).

```bash
curl -LO https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/jellyfin-ffmpeg8_8.1.3-1-jammy_amd64.deb
sudo apt install ./jellyfin-ffmpeg8_8.1.3-1-jammy_amd64.deb
export REX_TRANSCODER=/usr/lib/jellyfin-ffmpeg/ffmpeg LIBVA_DRIVER_NAME=iHD
sudo usermod -aG render "$USER"      # the GPU's /dev/dri/renderD128; log in again after
```

A systemd user service only gets the new group once its user manager restarts (a
reboot, or the last login session ending); until then start rex under `sg render`.

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

Each stream is decided on its own: what the client can play is copied, the rest is
converted. A DTS or TrueHD track the client refuses becomes AAC (or the profile's own
audio codec) beside the copied video, and a quality below Original (the client's
`maxVideoBitrate` and `videoResolution`) converts the video down while the audio is
copied. On cellular the mobile apps cap quality at 720p and 4 Mbps (`rust/src/gate.rs`),
so a 1080p title there needs one or the other: `REX_TRANSCODER`, or the app's cellular
quality raised to Original. Without an encoder rex refuses such a play (decision
2000/4005).


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
