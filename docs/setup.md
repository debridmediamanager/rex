# Setting up rex

The README has the short version. This is the full one.

## The steps

A rex server is four things: the binary, a **catalog** (its identity and two empty
libraries, made on its first start), the **sources** that register with it, push the
library and serve its bytes, and, if it is to appear in your Plex apps, a **claim** on a
Plex account (a fresh one, shared with yours: step 5) and, for use outside your LAN, an
HTTPS address. rex starts once (step 4), and nothing after that needs a restart. The steps below go through it in order.

### What you need

| | |
|---|---|
| A Linux, macOS or Windows host | rex is one binary with no other runtime dependencies |
| rex nightly 2026.10.25 or newer | download it (README step 1), or build it with Rust 1.89 or newer (rustup) or Docker (below) |
| One or more byte sources | [zurg](https://github.com/debridmediamanager/zurg) nightly 2026.10.22 or newer (zurg v1.0.0 has no rex feed), or any HTTP server that serves the files plus something that pushes them to rex's feed |
| Your Plex account token | not needed: rex uses the server token the claim (step 5) gives it |
| An HTTPS address | only for use outside your LAN and in app.plex.tv: rex-direct, on by default once claimed (step 7), or a certificate for a hostname you control (a `tailscale cert`, Let's Encrypt) |
| FFmpeg | optional, for [transcoding](#transcoding) |

### 1. Build

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml       # -> rust/target/release/rex
```

Without a Rust toolchain on the host, build in Docker. The binary runs on any Linux of the
same architecture with glibc 2.31 (Debian 11's) or newer:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/src -w /src/rust \
  -e CARGO_TARGET_DIR=/src/rust/target rust:1-bullseye cargo build --release
```

### 2. Sources: where the library and its bytes come from

rex reads no disk and lists nothing, and nothing about a source is set on rex's side. Each
source **registers** itself (`PUT /rex/sources/<name>`: the URL its files are under and the
credential for it) and **pushes** its items to `/rex/feed/<name>/...`; when
someone plays a file rex fetches its bytes with a GET of `<url>/<path>`. A source can be
zurg's WebDAV, nginx, a NAS, object storage or `python3 -m http.server`. `Range` support and
redirects are handled, and as many sources as you like feed the same libraries. rex keeps
each registration, credential included, in `rex.db`, so a restart keeps playing before the
source registers again.

zurg does both with nothing but `rex_url` in its `config.yml` (step 6), and says what each file
is from its name; add `dmm_api_key` so DMM says it instead. Anything else registers and pushes the same way:
[`docs/feed-api.md`](feed-api.md) is the full contract, for writing a pusher of your own.

Registering and pushing take no token, claimed or not, so a source may name rex by address
or host name. They answer only this machine, your home network and Tailscale (private,
100.64/10 and link-local addresses): any other address gets the answer a path that does not
exist gets, so the source API cannot be found from the internet. So forwarding rex's port
for rex-direct (step 7) puts Plex's API on the internet, which needs a token once rex is
claimed, but not the feed. A reverse proxy on rex's own machine looks local to rex, so block
`/rex/` in it. A router that gives outside visitors its own address makes them look local too, which
opens the feed to the internet ([`docs/feed-api.md`](feed-api.md), "Who may call it").

`DELETE /rex/sources/<name>` removes a source and everything it pushed from rex, after a
confirmation ([`docs/feed-api.md`](feed-api.md)).

### 3. Catalog: the server's identity and libraries

The first start makes `rex.db` in the folder rex starts in and logs `seeded rex.db: ...`. It
names the server after the machine (rename it in Plex's Settings) and holds two empty libraries:
Movies, holding the `movies` directory, and TV Shows, holding `shows`. A pushed item lands in
the library holding the directory its `path` starts with (`movies/<release>/<file>`), whichever
source sent it, unless the source placed it as the other kind: an episode under `movies` goes
to a show library, a movie under `shows` to a movie library. An item in no library's directory
goes to the first library of its kind: one its source placed as an episode to a show library,
anything else, unplaced files included, to a movie library.

**A library of its own for a directory** (anime, wrestling, a 1080p movies folder) is set in zurg,
under that directory in its `config.yml`, beside the directory's filters:

```yaml
directories:
  anime:
    rex_shows: Anime          # a show library titled Anime
    rex_movies: Anime Movies  # and a movie library; either one alone is fine
```

zurg sends it when it registers (step 6), and rex makes the libraries then and moves what it
already shows from that directory into them as it is: watch state, Fix Match and edits included.
An open Plex Web lists them at once under More in its sidebar, where you can pin them. Every
other directory stays in Movies and TV Shows. Rename the libraries in Plex as you like.
Delete one and it stays deleted, its directory going back to Movies and TV Shows, until
you add it again with Add Library, whose folder list shows the directories zurg sent. Removing
the lines from zurg's config keeps the library, and a mistyped entry is skipped and named in
zurg's log (or rex's, for one rex refuses), the rest still applied
([feed API](feed-api.md#libraries-a-source-declares)).

rex keeps everything clients change (watch state, edits, playlists, collections,
history, preferences), and each source's registration, in the same `rex.db`, written as it
changes. It holds the sources' credentials, so keep it and its backups private. Back it up
with the server stopped, or with `sqlite3 rex.db ".backup rex.db.bak"`; in Docker,
`docker exec rex sqlite3 /config/rex.db ".backup /config/rex.db.bak"`.

### 4. First start, on your LAN

```bash
rust/target/release/rex
```

`<rex's LAN IP>` below is the rex machine's address on your home network, such as `192.168.1.20`
(`hostname -I` on Linux). Every command in this guide uses it, from the rex machine or anywhere
else at home: an unclaimed rex takes its claim from private addresses, a claimed one checks tokens instead.
An unclaimed rex logs `waiting for a claim`; once claimed (step 5) it tells plex.tv its LAN
address, so Plex apps signed in as you reach it at home, and starts rex-direct (step 7). With an
HTTPS address of your own (step 7), its flags go here too: a flag or key added later needs a restart.

Plex Media Server runs on the same machine? Add `--port 32500` and use `32500` wherever this
guide says `32400`, the claim and step 7's port forward included (see LAN discovery below).

The first lines of the log say whether it seeded the catalog, name each source it already
knows (`source rd: registered, feed at /rex/feed/rd/`, or `no source yet`) and the state
file.

- **What a title looks like comes from Plex.** rex exchanges the ids its source pushed for Plex's
  own guid and shows what Plex's metadata agent says of it: titles, summaries, posters, seasons
  and episode names. Plex's agent is asked with the token the claim (step 5) gives rex.
- **What a file is comes from the source.** zurg with a sponsor `dmm_api_key` asks DMM what each
  file is and pushes it placed: the title's ids and, for an episode, TMDB's season and episode,
  anime and long shows included. Without the key zurg reads it off the release name: the title
  through DMM's free title search, the season and episode from the file name. rex reads nothing
  off a name. A file pushed without a placement
  is unmatched, for Fix Match: a film titled as its file, an episode in a show named after its
  release, season 1, numbered in the release's file order until the source places it.

Check it answers:

```bash
curl -s http://<rex's LAN IP>:32400/identity      # machineIdentifier, version
curl -s http://<rex's LAN IP>:32400/rex/sources            # {"size":0} until a source registers
curl -s http://<rex's LAN IP>:32400/rex/feed/rd/items       # what rd pushed, once it has registered
```

- **LAN discovery.** On port 32400 rex answers Plex's LAN discovery (GDM) on udp/32410 and
  udp/32414, so apps on the same network find it on their own; a firewall on the rex machine
  must let those two ports in. On another port beside a Plex on the same host it leaves
  discovery to that Plex. Signed-in apps find a claimed rex through the Plex account it is
  shared with either way.
- **Who may connect.** Until it is claimed (step 5), rex serves no Plex app: it answers
  `/identity`, the source API (step 2) and the claim, and takes the claim only from this
  machine and private addresses (10.x, 172.16-31.x, 192.168.x, IPv6 fc00::/7), as an
  unclaimed Plex Media Server does. Tailscale's 100.x addresses and anything arriving through
  a proxy with a public `X-Forwarded-For` are refused, and so is a request whose `Host` is a
  hostname rather than localhost or the address it reached: use `http://<rex's LAN IP>:32400`.
  Once claimed it accepts only its own token and the tokens plex.tv lists for it, as a real
  Plex Media Server does.

### 5. Claim it, so it appears in your Plex apps

rex serves no Plex app until it is claimed. Claiming is at your own risk: rex is not made by
Plex and Plex could take action against an account that registers it, so claim it with a
fresh free Plex account made for rex, not your own, and share it with yours (item 4 below).

Do it before you point a source at it (step 6): titles pushed before the claim keep their
file names for a while.

Claiming registers the server on the Plex account you claim with, the same way a real Plex
Media Server is claimed. **Do it once per server.** The claim is saved in `claim_state.json`
beside the catalog and survives restarts. Keep a backup of that file: a lost claim
means claiming again, and a claim gone wrong has signed every other server on the
account out of plex.tv before.

1. Open [plex.tv/claim](https://www.plex.tv/claim) signed in as the fresh account and copy
   the `claim-...` token. It is valid for about five minutes.
2. Redeem it on rex itself:

   ```bash
   curl -s -X POST "http://<rex's LAN IP>:32400/myplex/claim?token=claim-xxxxxxxxxxxxxxxx"
   ```

3. `curl -s http://<rex's LAN IP>:32400/identity` now reports `claimed="1"`, and
   `claim_state.json` exists beside the catalog.
4. Still as the fresh account, share rex with your own account in app.plex.tv (Settings,
   Manage Library Access, Grant Library Access, every library) and accept the invite as
   yourself.

You then watch signed in as yourself, with your own watch state. Fix Match works only for
the account rex is claimed with: rex refuses it (403) to the accounts it is shared with, as a
real Plex Media Server does.

A claimed rex refuses another claim (409). To move it to another account, stop it, remove
`claim_state.json` and claim again.

### 6. Point a source at it

A source is any program that registers with rex and pushes its files over the feed API
([`feed-api.md`](feed-api.md)); your own app or an addon does it as below for zurg
([rex with your own app](https://notes.debridmediamanager.com/guides/rex-any-source/), [Building rex addons](https://notes.debridmediamanager.com/guides/rex-addons/)).
For a debrid account or Usenet that is zurg. In each zurg's `config.yml` (zurg's [`docs/config.md`](https://github.com/debridmediamanager/zurg/blob/main/docs/config.md) has the details):

```yaml
rex_url: http://<rex's LAN IP>:32400
dmm_api_key: <your DMM API key>         # optional sponsor key: DMM, not the name, says what each file is (step 4)
```

Running two zurgs? Give each a name of its own with `rex_source: rd` and `rex_source: ad`. A
directory that is to be a library of its own gets `rex_shows`/`rex_movies` under it (step 3).

rex's LAN IP works from the same machine, another one and a container alike. Restart
zurg. Once its library has loaded it registers with rex, pushes everything it holds, then
each change as it happens, and the whole library again
every `rex_snapshot_every_hours` (6), registering again at the start of each. Watch it
arrive in rex's log (`SOURCES rd registered ...`, `FEED POST /rex/feed/rd/items -> 200`) and with the
`/rex/sources` and `/rex/feed/rd/items` calls above.

**An item appears only once zurg has probed it with ffprobe**, because rex lists each
file with its streams. Items zurg never probed are held, not shown. zurg probes the whole
library on its own once it has loaded, one release at a time by default (zurg's
`analysis_workers`), then each new release. It downloads ffprobe at start if it has none; its
Docker image carries one, and if the download fails, install ffprobe and set `ffprobe_binary`.
With a `dmm_api_key`, up to about 20 releases a minute come from DMM's stored details instead.
zurg tries a failed probe again a day later, so a large library fills over hours. **Analyze & Tag
Library** on zurg's dashboard (or `POST /torrents/analyze`) retries failed probes at once.

Leave zurg's `plex_server_url` pointing at your real Plex, if you have one. Pointed
at rex, zurg's matcher learns rex's own matches and hands them to DMM as the release's id, so
a wrong match can never be corrected by a later push.

### 7. Reach it from outside: HTTPS and the announce

app.plex.tv connects only to an HTTPS address that plex.tv lists for the server, even at
home, and the other Plex apps outside your LAN prefer one. rex has no `*.plex.direct`
certificate, so app.plex.tv cannot use the LAN address plex.tv lists for it. The
simplest address is rex-direct, DMM's own plex.direct. Your own hostname works too. Either
way rex terminates TLS itself, on the same port as plain HTTP.

- **rex-direct**, on by default: forward rex's port (32400) on your router to the rex
  machine. Nothing to restart: once claimed (step 5) rex gets the name
  `<hash>.rex-direct.debridmediamanager.com` from DMM, pointed at your public IP, and a
  certificate for it, the first one in a few minutes (rex logs that it is being issued), and
  announces it. The log says `rex-direct: https://<name>:32400 -> <ip>`. A new public IP and a
  renewed certificate are picked up without a restart. Keep `rex-direct.json` beside the
  catalog private: it holds the certificate's key. One Plex account can hold up to ten
  rex-direct names. Behind CGNAT it cannot work. At home, Plex apps can pick that name over
  the LAN address, and app.plex.tv has no other address it can use unless you announce an
  HTTPS address of your own. So at home either the router loops the connection back to rex
  or your network's DNS points the name at rex's LAN IP (below). If your router
  forwards another outside port to rex, set `REX_DIRECT_PORT=<that port>`.
  `REX_DIRECT_URL=off` turns rex-direct off.
- **Your own hostname**: `--tls-cert host.crt --tls-key host.key`. The key can be PKCS#8,
  RSA or EC (`BEGIN EC PRIVATE KEY`, which `tailscale cert` and many ACME clients write). A
  `tailscale cert` hostname works for apps on your tailnet. With rex-direct as well, rex
  serves the rex-direct certificate to clients that ask for its name and yours to the rest.

Check rex-direct from both sides with the address from the `rex-direct: https://...` log line:
`curl -s https://<name>:<port>/identity` (curl refuses a bad certificate) should answer on a
machine at home, and the same address with `/identity` should open in a phone's browser on
mobile data with no certificate warning. If only the phone gets it, the router does not loop
connections back: point the name at rex's LAN IP in your network's DNS (allow it in DNS rebind
protection) and rex answers it with the same certificate. If the phone fails too, the port is not forwarded, the connection is behind CGNAT,
or the certificate is still being issued.

Do not put a Cloudflare tunnel in front. Every byte would pass through your host and then
Cloudflare. Cloudflare also drops the `Range` of a file's first read. rex then serves the
whole file and every play start pulls all of it from your source.

No public IP at home (CGNAT): rex has no relay and none is planned. Use `tailscale serve` for
devices on your tailnet, or a small VPS that holds the public IP: frp's `frps` on it (with
`proxyBindAddr = "127.0.0.1"`) and Caddy terminating HTTPS with `respond /rex/* 403`, `frpc`
on the rex machine forwarding rex's LAN IP and port, `--announce-url https://<the VPS's name>:443`
and `REX_DIRECT_URL=off`. The notes guide has the files. Avoid hosted tunnels with bandwidth or data caps (Tailscale
Funnel, ngrok): a film exhausts them, and Funnel also exposes `/rex/`.

With your own hostname, add it to step 4's command with its certificate (rex adds its LAN
address and rex-direct's by itself):

```bash
--announce-url https://rex.example.com:32400 --tls-cert host.crt --tls-key host.key
```

A claimed rex then shows in app.plex.tv and the apps' server list. If it does not, look for the
`plex.tv` lines in rex's log: a missing claim (`waiting for a claim`), or an address your apps
cannot reach. `pubsub: reachability 0` in the log means nothing either way.

Remote playback needs no Plex Pass or Remote Watch Pass on either account (checked in Plex
Web; other Plex apps have not been playback-tested).

### 8. Run it as a service

Under systemd, put step 4's command, with any flags from step 7, in `~/.config/systemd/user/rex.service`,
then `systemctl --user enable --now rex`, and `loginctl enable-linger` so it runs without a login.
Keys, if you set any, go in `rex.env` beside `rex.db`:

```ini
[Unit]
Description=rex
After=network-online.target

[Service]
WorkingDirectory=%h/rex
EnvironmentFile=-%h/rex/rex.env
ExecStart=%h/rex/rust/target/release/rex
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

On macOS, copy `examples/run.sh.example` to `run.sh` beside `rust/`: it is the same command as a
script that loads `rex.env`. Under launchd, with absolute log paths (relative ones make the job exit 78 without a log):
`launchctl submit -l com.rex -o "$PWD/rex.log" -e "$PWD/rex.err" -- /bin/bash "$PWD/run.sh"`.
A submitted job does not survive a reboot; a LaunchAgent plist like the manager's (below)
does. To run several servers on one host, use `rex manager` (below).

To update a release, unpack the newer archive with `--strip-components=1` in the same
folder and restart rex. It replaces `rust/`, `data/`, `examples/` and the docs, and leaves
`rex.db`, `rex.env`, `run.sh`, `claim_state.json` and `rex-direct.json` alone. A checkout
updates with `git pull` and the build in step 1. In Docker, `docker compose pull &&
docker compose up -d`.

rex logs Plex requests only under `--verbose` (source and feed calls always), and masks `X-Plex-Token` values (not the
single-use claim token); the log still names your titles and addresses, so keep it private.

### Windows

The Windows build is `rust\target\release\rex.exe` in the zip, and every step above works
the same in PowerShell with `rust\target\release\rex.exe` for `rust/target/release/rex`,
`curl.exe` for `curl` and `$env:NAME = '...'` for `export`. Three things differ:

- `examples/run.sh.example` and `rex manager` are Linux and macOS only. Start `rex.exe` from a
  scheduled task (Task Scheduler, "At startup", "Run whether user is logged on or not") to
  keep it running.
- `claim_state.json`, `rex-direct.json` (the certificate's private key) and `rex.db`, which holds
  the sources' credentials, are protected only by the folder they sit in, so keep the rex folder inside your user profile.
- LAN discovery cannot share its ports with a Plex Media Server on the same Windows machine.
  Beside one, give rex another port (`--port 32500`), which keeps discovery off.

### Docker

`ghcr.io/debridmediamanager/rex:nightly` is rex's Linux build (amd64 and arm64) in an image.
It is private, as the repository is: log in with a classic GitHub token that has
`read:packages` and `repo`, the same as for zurg's image.

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

Everything rex writes (`rex.db`, `claim_state.json`, `rex-direct.json`, Plex's metadata
cache) goes to `/config`.

```yaml
services:
  rex:
    image: ghcr.io/debridmediamanager/rex:nightly
    container_name: rex
    restart: unless-stopped
    network_mode: host        # LAN discovery (GDM), and zurg's registered address reachable as on the host
    devices:
      - /dev/dri:/dev/dri     # an Intel or AMD GPU for converting; leave out where there is none
    volumes:
      - ./config:/config
```

zurg's `rex_url` (step 6) and the claim (step 5) are unchanged with host networking. A
`command:` adds flags, such as step 7's own hostname; its certificates go in `/config` too:
`--tls-cert /config/host.crt --tls-key /config/host.key`. Without host networking, publish
`32400:32400`, add `command: --announce-url http://<rex's LAN IP>:32400` (inside its container
rex knows only the container's address), rename the server in Plex's Settings (it is named
after the container's ID), and claim from the Docker host with `curl -X POST "http://localhost:32400/myplex/claim?token=claim-xxxxxxxxxxxx"`:
a claim addressed to the host's LAN IP reaches rex at the container's own address, and rex
refuses a `Host` that is not the address it reached. Apps then find rex only once it is claimed, and
rex must still reach the address zurg registers. The image carries jellyfin-ffmpeg and, on amd64,
Intel's OpenCL runtime for HDR, and rex finds them on its own; `devices` hands it the GPU (an
NVIDIA card needs the NVIDIA container toolkit and `gpus: all` instead; untested). rex saves its state on `docker stop`.

### Troubleshooting

| Symptom | Cause |
|---|---|
| A push answers 404 `no such source: PUT /rex/sources/... first` | The source has not registered: update zurg (What you need) |
| Every part read answers 502 | No source is registered (`curl -s http://<rex's LAN IP>:32400/rex/sources`), or rex cannot reach the one that is |
| A push answers `rejected: no movie or show library holds ...` | No library of that item's kind is left (one was deleted in Plex): add one with Add Library (step 3) |
| zurg pushed but the library stays empty | Items are held until zurg probes them (step 6) |
| A directory given `rex_shows`/`rex_movies` has no library | zurg did not send it (`curl -s http://<rex's LAN IP>:32400/rex/sources` lists each source's `directories`), zurg or rex skipped a mistyped entry (its log says why), or a library there was deleted in Plex, which keeps it deleted (the log says `stays deleted`): add it again with Add Library (step 3) |
| A part read answers 502, or 401/403 | rex cannot reach the source's `url` (502), or the source refused its credential (its own status): the log line `PART ... upstream` says which |
| The server is not in the Plex apps | Not claimed (step 5), or, in app.plex.tv and outside your home, no HTTPS address reachable from the internet (step 7) |
| A device at home cannot connect, and rex's `heartbeat ... ->` line shows no LAN address or the wrong one | rex announces the private address of the network it reaches the internet through; behind a VPN or a Tailscale exit node that is not your home network. Add `--announce-url http://<rex's LAN IP>:32400` |
| The log says `waiting for a claim` | The server is not claimed: claim it (step 5) and it announces at once |
| The log says `rex-direct: registration failed: HTTP 401` or `403` | The provider could not confirm the claim with plex.tv: the server was removed from its Plex account, or claim it again (step 5) |
| The log says `rex-direct: registration failed: HTTP 429` | That Plex account already holds ten rex-direct names: claim this rex with another account, or use your own hostname (step 7) |
| A second server on the same catalog exits 1 | The state file is locked by the first: one catalog per server |

### Running several servers: rex manager

`rex manager` is a control service on top of the server. It runs **no** rex server
until you start one: you create a server with a port (and any flags), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts.

```bash
rex manager serve                        # API + page on http://127.0.0.1:32300; prints the admin token once
rex manager create den --port 32401      # an empty library; a source such as zurg fills it once its rex_url names the port
rex manager start den
rex manager list                         # status | logs | stop | restart | delete [--purge]
```

Each server gets `managed/servers/<name>/`, whose `rex.db` holds its catalog and its state. `http://127.0.0.1:32300/` is a small page for the same operations (paste
the admin token from `managed/admin.token`).

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


## Movie and TV capabilities

These work even when the claiming account is free. An app's own checks, such as its Downloads
eligibility, still apply. Music, Live TV and DVR are not supported.

| Capability | What rex implements |
|---|---|
| Movie editions | Edit the edition in Plex Web, or have a source send `edition_title`. Different editions remain separate movies; versions of the same edition fold together. No filename parsing |
| Dashboard | Measured CPU and memory, delivered-byte bandwidth, account-separated plays and completion history. Unsupported OS counters are omitted, not invented |
| Downloads | Account-owned queues, original bytes or real FFmpeg conversions, Range reads, cancellation and restart recovery. A share needs download permission; local restrictions can only reduce it |
| Intro and credits skipping | Real local analysis with FFmpeg. Repeated opening pictures across source-placed episodes of one season identify intros; dark rolling-text endings and authored chapters identify credits. Ambiguous material gets no marker |
| Chapter and seek thumbnails | JPEGs at source-provided chapter boundaries and 10-second seek previews, including a Roku BIF index. Authenticated byte ranges and checksummed artifacts survive a restart |
| Subtitle adjustment | Uploaded and embedded text can be checked against the selected file's text or measured audio. Analyze prepares voice-activity eligibility; silence or an uncertain match is not advertised as successfully synchronized |
| Hardware and HDR controls | "Use hardware acceleration when available" turns all GPU use on or off; with it on, "Use hardware-accelerated video encoding" turns off only the GPU encoder. HDR tone mapping and its selected algorithm affect the actual conversion; capabilities follow the installed FFmpeg probe |
| Audio enhancement | Supported FFmpeg filters implement dialogue enhancement and loudness normalization. Requested processing encodes audio and can preserve copied video. Stereo dialogue enhancement is reported as stereo |
| Remote limits | Per-user stream count and per-stream/total upload budgets use the actual connection, not a client's `location` claim. Concurrent starts reserve capacity; impossible bitrate budgets are refused |
| Sharing and webhooks | Local owner controls can tighten content-rating/label filters and Downloads permission, and configure real multipart webhooks without the cloud subscription UI |
| Cinema trailers and pre-rolls | Queues can prepend trailers supplied by your sources and exact registered media paths. No online theater or Blu-ray trailer catalog is fetched |

### Analysis and disk use

Set up [FFmpeg](#transcoding) first. Analyze on an item or library queues real work and reports
its progress in Activities. Generation defaults to off to avoid reading a large remote library
unasked. The Library preferences `GenerateIntroMarkerBehavior`, `GenerateCreditsMarkerBehavior`,
`GenerateChapterThumbBehavior` and `GenerateBIFBehavior` support manual generation or opt-in
generation when source items are added. Per-library switches limit that automatic generation; Analyze always runs what you ask for.
There is no nightly scheduler; a corresponding Butler task runs only when requested.

Analysis reads source media, consumes CPU and stores generated files in a folder beside
`rex.db`, as do Downloads; keep that disk writable and back the folders up with `rex.db`.
Deleting a source, file version or library retires its unreferenced analysis artifacts.
Deleting preview thumbnails leaves chapter images and markers intact.

### Local owner controls

These APIs need the owner's Plex token and nothing else: unlike the feed
they are not limited to your own networks, so once rex's port is forwarded for rex-direct they
answer from the internet to that token too. A reverse proxy should block `/rex/` from the internet, just as for the feed.
Use `X-Plex-Token` for authentication and `Content-Type: application/json` for JSON bodies.

| Route | Body / result |
|---|---|
| `GET /rex/analysis` | Current job states |
| `POST /rex/analysis` | `{"keys":["1007"],"markers":true,"chapters":true,"previews":true}`; 202 with the number queued |
| `GET`, `PUT`, `DELETE /rex/webhooks` | PUT `{"urls":["https://your-endpoint.example/webhook"]}`; delivery includes the event JSON and a thumbnail when available |
| `GET`, `PUT`, `DELETE /rex/shares/{accountID}/restrictions` | PUT `{"filterMovies":"contentRating=G,PG","filterTelevision":"label!=Hidden","allowSync":false}`. Omitted fields impose no extra restriction; DELETE removes the local layer, never expands the cloud grant |

Cinema pre-rolls use the Extras preference `CinemaTrailersPrerollID`: exact registered Part
paths, commas for an ordered sequence or semicolons for a random choice. Clients must opt in
to cinema trailers. Missing or inaccessible pre-rolls are refused rather than replaced with
another file version.

## Transcoding

rex converts with an FFmpeg it finds on its own, in this order: jellyfin-ffmpeg's package
(`/usr/lib/jellyfin-ffmpeg/ffmpeg`), a portable jellyfin-ffmpeg unpacked into rex's folder as
`ffmpeg/` (the way on Windows and macOS), a Jellyfin server install, then `ffmpeg` on PATH.
The startup log names the one it found. `REX_TRANSCODER` names another and
`REX_TRANSCODER=off` turns conversion off; with none, rex answers a conversion request the way
a server with no encoder does.

### Which FFmpeg

Install **jellyfin-ffmpeg** 8, the build rex is measured with and the only one that does
everything: the GPU through its own Intel driver, HDR tonemapped on the GPU or the CPU, Dolby
Vision profile 5, and subtitles drawn on the GPU. Which file depends on the machine:

| Machine | Install | rex finds it at |
|---|---|---|
| Ubuntu 22.04/24.04/26.04, Debian 12/13 (amd64, arm64) | the `.deb` package, below | `/usr/lib/jellyfin-ffmpeg/ffmpeg` |
| Docker | nothing: the image carries it ([Docker](#docker)) | `/usr/lib/jellyfin-ffmpeg/ffmpeg` |
| Windows | `jellyfin-ffmpeg_8.1.3-1_portable_win64-clang-gpl.zip` (`winarm64` on ARM) | `ffmpeg\ffmpeg.exe` in rex's folder |
| macOS | `..._portable_macarm64-gpl.tar.xz` (Apple Silicon) or `..._portable_mac64-gpl.tar.xz` (Intel) | `ffmpeg/ffmpeg` in rex's folder |
| Any other Linux | `..._portable_linux64-gpl.tar.xz` (`linuxarm64` on ARM) | `ffmpeg/ffmpeg` in rex's folder |

The `.deb` lands in `/usr/lib/jellyfin-ffmpeg` beside any system FFmpeg and carries its own
Intel driver; it needs no `LIBVA_DRIVER_NAME` (encoding and OpenCL measured without it on
Skylake and Gemini Lake, 2026-10-22). Use the package, not a copy unpacked elsewhere: on a
J4105 (2026-10-17) a `dpkg -x` copy fell back to the system's older iHD, which could not
convert 10-bit HEVC.

```bash
DEB=jellyfin-ffmpeg8_8.1.3-1-$(. /etc/os-release; echo "$VERSION_CODENAME")_$(dpkg --print-architecture).deb   # Ubuntu 22.04/24.04/26.04, Debian 12/13
curl -fLO "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/$DEB"
sudo apt install "./$DEB"
sudo usermod -aG render "$USER"      # the GPU's /dev/dri/renderD128; log in again after
```

The portable builds unpack flat (`ffmpeg` and `ffprobe` at the top), so they go straight into
an `ffmpeg` folder beside `rust/` in rex's folder. In PowerShell, in rex's folder:

```powershell
curl.exe -fLO https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/jellyfin-ffmpeg_8.1.3-1_portable_win64-clang-gpl.zip
Expand-Archive jellyfin-ffmpeg_8.1.3-1_portable_win64-clang-gpl.zip ffmpeg
```

On macOS or another Linux, in rex's folder:

```bash
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) B=macarm64 ;; Darwin-*) B=mac64 ;; *-aarch64) B=linuxarm64 ;; *) B=linux64 ;;
esac
mkdir -p ffmpeg && curl -fL "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/jellyfin-ffmpeg_8.1.3-1_portable_${B}-gpl.tar.xz" | tar xJ -C ffmpeg
```

Downloaded with `curl` they carry no macOS quarantine; one downloaded in a browser needs the
`xattr -dr com.apple.quarantine .` the rex download does. Where each one converts on the GPU
is the [platform table](#by-platform) below: Windows uses only an NVIDIA card and macOS never the
GPU, whatever the FFmpeg. The Linux portable build reaches the GPU through the system's own
VA-API library: Ubuntu 22.04's is too old for it (`vaMapBuffer2`, measured on an HD 510,
2026-10-22) and it converts on the CPU there, so use the `.deb` wherever there is one.

Another FFmpeg converts ordinary video, and rex's log says what it lacks. Measured 2026-10-22:

| FFmpeg | Lacks |
|---|---|
| Ubuntu 24.04's `apt install ffmpeg` (6.1), BtbN's static builds | Dolby Vision profile 5 (no `tonemapx`), subtitles drawn on the GPU |
| Ubuntu 22.04's `apt install ffmpeg` (4.4) | the same, and a far seek starts a segment early |
| johnvansickle's static build | the GPU (no VA-API or NVENC), HDR on the GPU, Dolby Vision profile 5 |
| Homebrew's (macOS) | all HDR (no `zscale` or `tonemapx`) |

Restart rex after installing and it finds it. A systemd user service only gets the new group once its user manager
restarts (a reboot, or the last login session ending); until then start rex under `sg render`.

| Variable | Default | |
|---|---|---|
| `REX_TRANSCODER` | found, as above | the FFmpeg binary, or `off` |
| `REX_TRANSCODE_HW` | `auto` | `vaapi`, `cuda` or `none` (`auto` tries VA-API, then NVENC) |
| `REX_TRANSCODE_DEVICE` | `/dev/dri/renderD128` | the VA-API device |
| `REX_TRANSCODE_DIR` | `$TMPDIR/rex-transcode-<port>` | segments; cleared at start |

The startup log says what the probe found (`transcoder: ... hardware vaapi encode=true
tonemapx=true opencl_tonemap=true bounded_http=false alpha_subtitles=true decode=[h264,hevc,mpeg2video]`). A
conversion runs on the GPU end to end when it can decode the source there, uploads CPU-decoded frames when it cannot or should not
(AV1 on older Intel, a burned image subtitle, HDR on a device without OpenCL), and falls back to x264 when there is no device.
HDR and Dolby Vision stay on the GPU, tonemapped through OpenCL with whichever tonemapping
algorithm is set, once the startup probe has tonemapped a frame that way (VA-API devices). Without it
they are tonemapped on the CPU, which on a low-power Intel is a tenth of real time at 4K. The
OpenCL runtime is a package of its own:

```bash
sudo apt install intel-opencl-icd   # Ubuntu 22.04: 22.14, enough for a Gemini Lake J4105
```

A J4105 takes 4K HDR10 and Dolby Vision profile 5 to 1080p at about real time with every
algorithm (0.9-1.2x on 40 Mbit/s samples, 1.2-1.7x on WEB releases), against 0.07-0.2x on its CPU
(findings, "OpenCL tonemapping for every algorithm").
Every conversion is one FFmpeg reading the source over HTTP; nothing is downloaded
first.

"Use hardware acceleration when available" turns GPU decoding and encoding on or off together;
with it on, "Use hardware-accelerated video encoding" turns off only the GPU encoder. `TranscoderToneMapping` turns HDR mapping on or off, and the algorithm setting picks how.
The Normalize Loudness and Boost Dialog playback controls appear only when the app's cloud
rollout and server capability checks allow them. rex implements their requested audio processing
with `loudnorm` and `dialoguenhance`, not a flag-only acknowledgement.


Each stream is decided on its own: what the client can play is copied, the rest is
converted. A DTS or TrueHD track the client refuses becomes AAC (or the profile's own
audio codec) beside the copied video, and a quality below Original (the client's
`maxVideoBitrate` and `videoResolution`) converts the video down while the audio is
copied. On cellular the mobile apps cap quality at 720p and 4 Mbps (`rust/src/gate.rs`),
so a 1080p title there needs one or the other: an FFmpeg, or the app's cellular
quality raised to Original. Without an encoder rex refuses such a play (decision
2000/4005).

A conversion carries the selected subtitle. An image one (PGS, VobSub) is burned into the
picture. A text one inside an MKV or MP4 (SRT, ASS, MP4 timed text) goes into Plex for
Android's and iOS's Matroska stream, an MP4's converted to SRT, and is burned for every other
app: rex reads the track out of the release through its index one line at a time from where
the player is, the first minute before the play starts, and converts only as far as it has
read; a seek reads from the new position first. Lines denser than it can read in time, an
opening song's karaoke, are left out of that play rather than stall it (findings, "Subtitles
through a conversion"). A subtitle
uploaded to rex is copied or burned the same way. A text subtitle in a download's conversion
is dropped. An ASS track is drawn with the host's fonts, not the fonts attached to the
release.


### By platform

| Platform | GPU paths rex knows |
|---|---|
| Linux amd64 | VA-API (Intel, AMD), NVENC (NVIDIA), else the processor (x264) |
| Linux arm64 | the same, which on most ARM boards means the processor |
| macOS | none: the processor always (no VideoToolbox path) |
| Windows | NVENC (NVIDIA), else the processor (no Quick Sync or AMF path) |


On Linux an FFmpeg dies with rex and a restart sweeps up any left over. On macOS and
Windows a stopped rex still stops its FFmpegs, but if rex crashes they run on until their
conversion ends.

## Subtitles

Search Subtitles in a Plex app asks Plex's own subtitle provider once rex is claimed. There is
nothing to set up. Unclaimed, rex asks nobody and the search lists nothing.

The subtitles named most like your file come first. When you pick one, rex checks it against the
file before keeping it: against the file's own text subtitle when it has one, otherwise, with
an FFmpeg, against its sound. When it finds the subtitle running late, or timed for
another frame rate, it moves it onto the file. Every subtitle is kept as UTF-8; one that arrives in another encoding
is read as Western European (Windows-1252).

When a Plex account's subtitle setting is "Always enabled", or "Shown with foreign audio" and the
audio is in another language, and a film or episode has no subtitle in that account's language, rex
starts looking as soon as the title is opened or played, and playback waits up to 3 seconds for it. It checks
the five best candidates and keeps the first it finds in sync, else fixes one it finds off, else
keeps the best named. A title with nothing to find is not searched again for a day.

Checking against the sound reads six 90-second pieces of the file through its source, so rex does
it only when the title is played: opening a title keeps the best-named subtitle, and pressing play
checks it and fixes it in place. A file with a text subtitle of its own needs only a few small reads,
and is checked as soon as the title is opened.

Upload accepts a text subtitle through the app's normal upload dialog. The playback
Auto Sync control also needs the client's rollout and measured voice activity on the selected
Media version. Run Analyze to prepare that measurement. A silent file, another version's audio,
or an uncertain offset never makes that control falsely eligible.

