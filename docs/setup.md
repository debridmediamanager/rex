# Setting up rex

The README has the short version. This is the full one.

## The steps

A rex server is four things: the binary, a **catalog** (its identity and two empty
libraries, made on its first start), the **sources** that register with it, push the
library and serve its bytes, and, if it is to appear in your Plex apps, a **claim** on a
Plex account (a fresh one, shared with yours: step 5) and, for use outside your LAN, an
HTTPS address. Setting one up is one command:

```bash
rust/target/release/rex --catalog rex.db
```

The steps below go through it in order.

### What you need

| | |
|---|---|
| A Linux, macOS or Windows host | rex is one binary and its `data/` sidecars, with no other runtime dependencies. The same binary writes the catalog (`rex catalog`) and runs several servers (`rex manager`) |
| Rust 1.89 or newer (rustup), or Docker | only to build it (below); or download a release or nightly archive (README step 1) |
| One or more byte sources | [zurg](https://github.com/debridmediamanager/zurg) (a build with the rex feed: `rex_url` in its `config.yml`), or any HTTP server that serves the files plus something that pushes them to rex's feed |
| Your Plex account token | not needed. A claimed rex (step 5) asks plex.tv and Plex's own metadata agent, which describes every title and powers Fix Match, with the server token the claim gives it. Only an unclaimed server that should still match with Plex's agent takes `PLEX_TOKEN`: the `X-Plex-Token` in the URL of any "View XML" link in Plex Web ([Plex's guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| An HTTPS address | only for use outside your LAN and in app.plex.tv: rex-direct with a DMM sponsorship (step 7), or a certificate for a hostname you control (a `tailscale cert`, Let's Encrypt) |
| FFmpeg | optional, for [transcoding](#transcoding) |

### 1. Build

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml       # -> rust/target/release/rex
```

Without a Rust toolchain on the host, build in Docker. The binary runs on any Linux of the
same architecture with glibc 2.31 (Debian 11's) or newer. The `rust:1-bullseye` image stopped
updating at Rust 1.98 (2026-08-25), which is still new enough:

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

zurg does both with nothing but `rex_url` in its `config.yml` (step 6); add `dmm_api_key` so
it also says what each file is. Anything else registers and pushes the same way:
[`docs/feed-api.md`](feed-api.md) is the full contract, for writing a pusher of your own.

Registering and pushing take no token, claimed or not, so a source may name rex by address
or host name. They answer only this machine, your home network and Tailscale (private,
100.64/10 and link-local addresses): any other address gets 403. So forwarding rex's port
for rex-direct (step 7) puts Plex's API on the internet, which needs a token once rex is
claimed, but not the feed. A reverse proxy on rex's own machine looks local to rex, so block
`/rex/` in it ([`docs/feed-api.md`](feed-api.md), "Who may call it").

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
and whatever path prefix its `file` carries, unless the source placed it as the other kind:
an episode under `movies` goes to a show library, a movie under `shows` to a movie library.
An item in no library's directory goes to the first library of its kind: one its source
placed as an episode to a show library, anything else, unplaced files included, to a movie
library. To add more directories to the movie or show library, name them when seeding
(`--shows anime` makes `anime` a second folder of Debrid TV):

```bash
rust/target/release/rex catalog --seed --name den --out rex.db \
  --movies movies --shows shows --shows anime
```

The folders are shown in Plex as `/mnt/zurg/<directory>`. `--mount` changes that prefix,
and it only matters for zurg's own Plex matcher, which matches a file by its full path,
so set it to zurg's `mount_path` if you point zurg's `plex_server_url` at rex (which step 6
advises against).

**Name every directory before the first start.** Once rex has started, the
libraries it saved in `rex.db` win over the catalog, so re-seeding changes nothing, and
rex does not yet take Plex's "Edit Library → Add folders". A directory added later gets
a library of its own instead: Manage → Libraries → Add Library on it.

rex keeps everything clients change (watch state, edits, playlists, collections,
history, preferences), and each source's registration, in the same `rex.db`, written as it
changes. It holds the sources' credentials, so keep it private: rex creates it, and its
`-wal` and `-shm`, with your umask, which usually lets every user on the host read them. Run
`chmod 600 rex.db*`, or start rex under `umask 077` as the Docker image does (`UMask=0077` in
step 8's systemd unit). Back it up with the server stopped, or with
`sqlite3 rex.db ".backup rex.db.bak"`; in Docker,
`docker exec rex sqlite3 /config/rex.db ".backup /config/rex.db.bak"`.

### 4. First start, on your LAN

```bash
rust/target/release/rex --catalog rex.db
```

Leave `--owner-username` and `--owner-email` unset: a claimed server takes both from the
claim, and a given `--owner-username` still wins over it.

The first lines of the log say whether it seeded the catalog, name each source it already
knows (`source rd: registered, feed at /rex/feed/rd/`, or `no source yet`) and the state
file.

- **What a title looks like comes from Plex.** rex exchanges the ids its source pushed for Plex's
  own guid and shows what Plex's metadata agent says of it: titles, summaries, posters, seasons
  and episode names. It reads no TMDB: `TMDB_KEY`, `REX_METADATA_URL` and `REX_METADATA_KEY` are
  no longer read (2026-10-17), and an install that still sets them starts as before. Without a
  claim (step 5) or `PLEX_TOKEN`, titles keep the names they were pushed with and Fix Match has
  no candidates.
- **What a file is comes from the source.** zurg with a sponsor `dmm_api_key` asks DMM what each
  file is and pushes it placed: the title's ids and, for an episode, TMDB's season and episode,
  anime and long shows included. rex reads nothing off a name. A file pushed without a placement
  is unmatched, for Fix Match: a film titled as its file, an episode in a show named after its
  release, season 1, numbered in the release's file order until the source places it. A TMDB id
  set by hand in zurg names its release too, as a title of its library's kind, trusted as given. Upgrading: `REX_ANIME_LISTS` and `REX_IMDB_EPISODES`
  are no longer read, so an install that still sets them starts as before, and the
  `anime-list-master.xml`, `anime-offline-database.jsonl`, `title.episode.tsv.gz` and
  `imdb-episodes.db` they kept beside the catalog are ignored (delete them if you like). Rows
  already in the state file keep their numbers and titles until the source places them: no
  start renumbers, moves or re-identifies a row by its name.

Check it answers:

```bash
curl -s http://127.0.0.1:32400/identity      # machineIdentifier, version
curl -s http://127.0.0.1:32400/rex/sources            # {"size":0,"sources":[]} until a source registers
curl -s http://127.0.0.1:32400/rex/feed/rd/items       # what rd pushed, once it has registered
```

- **Port 32400 and LAN discovery.** On 32400 rex answers Plex's LAN discovery (GDM) on
  udp/32410 and 32414, so apps on the same network find it. On a host that already runs
  Plex, give rex another port: there the default `--gdm auto` answers no discovery.
- **Who may connect.** Until it is claimed (step 5), rex answers only this machine and
  private addresses (10.x, 172.16-31.x, 192.168.x, IPv6 fc00::/7), as an unclaimed Plex
  Media Server does. Tailscale's 100.x addresses and anything arriving through a proxy with
  a public `X-Forwarded-For` are refused, and so is a request whose `Host` is a hostname
  rather than localhost or the address it reached: use `http://<LAN IP>:32400`. To admit
  other networks, set **List of IP addresses and networks that are allowed without auth**
  (`allowedNetworks`) in Settings > Network from the machine itself; once set, only
  localhost and those networks get in. Or start rex with `--token <secret>`, which makes
  it refuse everything but `/identity`, `/web` and the source API (`/rex/sources`,
  `/rex/feed`) without that `X-Plex-Token`. Once
  claimed it accepts only its own token and the tokens plex.tv lists for it, as a real
  Plex Media Server does.
- **The web client at `/web`** is optional and comes from a Plex Media Server install:
  pass `--web-root .../Resources/Plug-ins-*/WebClient.bundle/Contents/Resources` (on
  Linux under `/usr/lib/plexmediaserver`). app.plex.tv works without it once the server
  is claimed and announced over HTTPS (step 7).

### 5. Claim it, so it appears in your Plex apps

Claiming is optional and at your own risk. rex is not made by Plex and Plex could take
action against an account that registers it, so claim it with a fresh free Plex account
made for rex, not your own, and share it with yours (item 5 below). Skip it and rex only
answers on your own network (step 4 "Who may connect"), or start it with `--token` and give
that token to your clients.

Do it before you point zurg at it (step 6): titles pushed before the claim keep the names
they were pushed with until rex names them in the background, about 300 an hour.

Claiming registers the server on the Plex account you claim with, the same way a real Plex
Media Server is claimed. **Do it once per server.** The claim is saved in `claim_state.json`
beside the catalog and survives restarts. Keep a backup of that file: a lost claim
means claiming again, and a claim gone wrong has signed every other server on the
account out of plex.tv before.

1. Start rex, with or without `--announce-url` (below): an unclaimed rex announces nothing.
2. Open [plex.tv/claim](https://www.plex.tv/claim) signed in as the fresh account and copy
   the `claim-...` token. It is valid for four minutes.
3. Redeem it on rex itself:

   ```bash
   curl -s -X POST "http://127.0.0.1:32400/myplex/claim?token=claim-xxxxxxxxxxxxxxxx"
   ```

4. `curl -s http://127.0.0.1:32400/identity` now reports `claimed="1"`, and
   `claim_state.json` exists beside the catalog.
5. Still as the fresh account, share rex with your own account in app.plex.tv (Settings,
   Manage Library Access, Grant Library Access, both libraries) and accept the invite as
   yourself. Add `--announce-url http://<rex's LAN IP>:32400` (step 7) so Plex apps signed in
   as you know where to reach it.

You then watch signed in as yourself, with your own watch state. Fix Match works only for
the account rex is claimed with: rex refuses it (403) to the accounts it is shared with, as a
real Plex Media Server does.

From the claim on rex asks plex.tv with the server token it stores there: the announce
(step 7), and Plex's own metadata agent, which then names and describes what sources push
with no restart. `PLEX_TOKEN` is not used once there is a claim. A claimed start logs
`claimed-account watch/rating sync is OFF: REX_SCROBBLING_DECRYPT_SECRET_B64 is not configured`.
That is expected: the secret only syncs the owner's watch state with plex.tv, and each
account's watch state in rex works without it.

A claimed rex refuses another claim (409). To move it to another account, stop it, remove
`claim_state.json` and claim again. rex also refuses to store a claim when plex.tv answers
with the account's own `PLEX_TOKEN` instead of a server token, which happens when the
server's identity already has a device row on the account. An unclaimed rex announces
nothing, `PLEX_TOKEN` or not, so it never writes that row: plex.tv applies an announce to the
device that owns its token, and an account token's device is another of your devices.

### 6. Point zurg at it

In each zurg's `config.yml` (zurg's [`docs/config.md`](https://github.com/debridmediamanager/zurg/blob/main/docs/config.md) has the details):

```yaml
rex_url: http://<rex's address>:32400   # turns on zurg's ffprobe analysis (whole library, then new releases)
dmm_api_key: <your DMM API key>         # sponsor key: DMM says what each file is (step 4)
rex_source: rd                          # optional: the name it registers as (default "zurg")
```

An address or a host name (`http://rex:32400` from a container) both work. It needs a zurg
nightly from 2026.10.17 or later: the latest zurg release, v1.0.0, has no rex feed. Restart
zurg. Once its library has loaded it registers with rex, pushes everything it holds, then
each change as it happens, and the whole library again
every `rex_snapshot_every_hours` (6), registering again at the start of each. Watch it
arrive in rex's log (`SOURCES rd registered ...`, `FEED POST /rex/feed/rd/items -> 200`) and with the
`/rex/sources` and `/rex/feed/rd/items` calls above.

**An item appears only once zurg has probed it with ffprobe**, because rex lists each
file with its streams. Items zurg never probed are held, not shown. zurg probes the whole
library on its own once it has loaded, one release at a time by default (zurg's
`analysis_workers`), up to about 20 a minute, then each new release, and
tries a failed probe again a day later, so a large library fills over hours. **Analyze & Tag
Library** on zurg's dashboard (or `POST /torrents/analyze`) retries failed probes at once.

Leave zurg's `plex_server_url` pointing at your real Plex, if you have one. Pointed
at rex, zurg's matcher learns rex's own matches and hands them to DMM as the release's id, so
a wrong match can never be corrected by a later push.

### 7. Reach it from outside: HTTPS and the announce

app.plex.tv connects only to an HTTPS address that plex.tv lists for the server, and the
other Plex apps outside your LAN prefer one. rex has no `*.plex.direct` certificate. The
simplest address is rex-direct, DMM's own plex.direct. Your own hostname works too. Either
way rex terminates TLS itself, on the same port as plain HTTP.

- **rex-direct**: put `DMM_API_KEY=<your DMM API key>` in `rex.env` (Docker: under
  `environment:`; rex does not read `rex.env` itself, `run.sh` (step 8) loads it, so
  started by hand `export` it), forward rex's port (32400) on your router to the rex machine, and
  restart rex. At start rex registers with DMM, which points
  `<hash>.rex-direct.debridmediamanager.com` at the public IP rex called from and hands rex
  a certificate for that name. rex serves it and adds
  `https://<hash>.rex-direct.debridmediamanager.com:32400` after the addresses in
  `--announce-url`, so `--announce-url` needs only step 5's LAN address. The log says
  `rex-direct: https://<name>:32400 -> <ip>`. rex registers again every six hours, which
  picks up a new IP and a renewed certificate without a restart. It keeps DMM's answer in
  `rex-direct.json` beside the catalog (mode 600, since it holds the certificate's key) and
  uses it when DMM does not answer at a start.
  It needs an active DMM sponsorship. A refused key is a `WARNING` in the log and rex runs
  on without rex-direct. The name points at the public IPv4 address rex calls DMM from, so
  behind CGNAT it cannot work. Plex apps at home still use the LAN address. If your router
  forwards another outside port to rex, set `REX_DIRECT_PORT=<that port>` in `rex.env`.
- **Your own hostname**: `--tls-cert host.crt --tls-key host.key`. The key can be PKCS#8,
  RSA or EC (`BEGIN EC PRIVATE KEY`, which `tailscale cert` and many ACME clients write). A
  `tailscale cert` hostname works for apps on your tailnet. With rex-direct as well, rex
  serves the rex-direct certificate to clients that ask for its name and yours to the rest.

Do not put a Cloudflare tunnel in front. Every byte would pass through your host and then
Cloudflare. Cloudflare also drops the `Range` of a file's first read. rex then serves the
whole file and every play start pulls all of it from your source (measured in
docs/findings.md under "The unavoidable trade-off").

With your own hostname, restart rex with the addresses to register, the HTTPS one first and
step 5's LAN one after it, in one comma-separated flag (a second `--announce-url` replaces the first):

```bash
--announce-url https://rex.example.com:32400,http://192.168.1.10:32400
```

They can be there from the first start: rex logs that it waits for a claim and registers
them the moment it is claimed. With `--announce-url` a claimed rex registers with plex.tv at start, with its server token,
holds the event stream plex.tv reads as "online", and re-announces every 10 minutes. The
server then shows in app.plex.tv and the apps' server list. If it does not, look for the
`plex.tv` lines in rex's log: a missing claim (`waiting for a claim`), or an address your apps
cannot reach. plex.tv does not test the addresses, so `pubsub: reachability 0` in the log
means nothing either way.

Playing from outside your home needs a Plex Pass or Remote Watch Pass on the account you
watch with, as with any Plex server: the app asks for it before the play reaches rex
(measured in Plex Web only).

### 8. Run it as a service

`examples/run.sh.example` is the whole command line as a script: copy it to `run.sh` at the
top of the checkout or release, beside `rust/` and the catalog, and fill it in. It reads the
keys from `rex.env` (`PLEX_TOKEN=` only if you want it, `DMM_API_KEY=` for rex-direct; mode 600) and adds
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
UMask=0077
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Under launchd, with absolute log paths (relative ones make the job exit 78 without a log):
`launchctl submit -l com.rex -o "$PWD/rex.log" -e "$PWD/rex.err" -- /bin/bash "$PWD/run.sh"`.
A submitted job does not survive a reboot; a LaunchAgent plist like the manager's (below)
does. To run several servers on one host, use `rex manager` (below).

To update a release, unpack the newer archive with `--strip-components=1` in the same
folder and restart rex. It replaces `rust/`, `data/`, `examples/` and the docs, and leaves
`rex.db`, `rex.env`, `run.sh`, `claim_state.json` and `rex-direct.json` alone. A checkout
updates with `git pull` and the build in step 1. In Docker, `docker compose pull &&
docker compose up -d`.

rex logs requests only under `--verbose`, and masks `X-Plex-Token` values (not the
single-use claim token); the log still names your titles and addresses, so keep it private.

Memory: in builds after 2026.10.18.0753-nightly, the library's rows live in the state file
(`rex.db`), not in memory. rex keeps an index of the fields its lists and searches read, the
rows written since they were last saved, and up to `REX_ROW_CACHE_MB` (default 64) of rows
read recently, which cut the production server's memory to about a third. Upgrading needs
nothing: the file is the one earlier releases wrote and read, so going back to an earlier
release reads it too.

### Windows

The Windows build is `rust\target\release\rex.exe` in the zip, and every step above works
the same in PowerShell with `rust\target\release\rex.exe` for `rust/target/release/rex`,
`curl.exe` for `curl`, `$env:NAME = '...'` for `export`, and quotes around a comma-separated
`--announce-url`. Three things differ:

- `examples/run.sh.example` and `rex manager` are Linux and macOS only. Start `rex.exe` from a
  scheduled task (Task Scheduler, "At startup", "Run whether user is logged on or not") to
  keep it running.
- `claim_state.json` and `rex.db`, which holds the sources' credentials, are protected only by
  the folder they sit in, so keep the rex folder inside your user profile.
- LAN discovery cannot share its ports with a Plex Media Server on the same Windows machine.
  Beside one, give rex another port (`--port 32500`), which keeps discovery off.

### Docker

`ghcr.io/debridmediamanager/rex` is each release's and nightly's Linux build (amd64 and arm64)
in an image, published by `.github/workflows/docker.yml`: `latest`, `X.Y.Z` and `X.Y` for
releases, `nightly` and `<YYYY.MM.DD.HHMM>-nightly` for nightlies. Use `nightly` until the
next release (README step 1). It is private, as the repository is: log in with a classic
GitHub token that has `read:packages` and `repo`, the same as for zurg's image.

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

Everything rex writes (`rex.db`, `claim_state.json`, Plex's metadata cache) goes to `/config`.
Arguments that start with a flag run the server on `/config/rex.db` (`REX_CATALOG` moves
it); a subcommand runs as given, so adding directories to the libraries before the first
start (step 3; `--mount` matches the image's own zurg mount) is:

```bash
docker compose run --rm rex catalog --seed --name den --out /config/rex.db \
  --mount /zurg_mnt/zurg --movies movies --shows shows --shows anime
```

```yaml
services:
  rex:
    image: ghcr.io/debridmediamanager/rex:nightly
    container_name: rex
    restart: unless-stopped
    network_mode: host        # LAN discovery (GDM), and zurg's registered address reachable as on the host
    volumes:
      - ./rex:/config
```

The image starts rex on `/config/rex.db` with `--zurg-mount /zurg_mnt/zurg`, where zurg's
Docker setup mounts, so the compose file needs no `command:`; one given adds to those.

zurg's `rex_url` is then `http://<the host's LAN IP>:32400`, and the claim (step 5) is the
same `curl` on the host. rex-direct (step 7) needs only `DMM_API_KEY` under `environment:`,
and keeps `rex-direct.json` in `/config`. Certificates of your own go in `/config` too:
`--tls-cert /config/host.crt --tls-key /config/host.key`. Without host networking,
publish `32400:32400` and add `--gdm off` and `--friendly-name <name>` (the container's
hostname is its ID); apps then find rex only once it is claimed, and
rex must still reach the address zurg registers. The image carries no FFmpeg. Mounting
one in and setting `REX_TRANSCODER` is untested; to convert video, run rex without Docker. rex saves its state on `docker stop`.

### Troubleshooting

| Symptom | Cause |
|---|---|
| A push answers 404 `no such source: PUT /rex/sources/... first` | The source has not registered: a zurg from before the sources API never does, update it to a nightly from 2026.10.17 or later (step 6) |
| Every part read answers 502 | No source is registered (`curl -s http://127.0.0.1:32400/rex/sources`), or rex cannot reach the one that is |
| A push answers `rejected: no movie or show library holds ...` | The catalog has no movie or show library of that item's kind: name one with `--movies`/`--shows` before the first start, or Add Library on it (step 3) |
| zurg pushed but the library stays empty | Items are held until zurg probes them (step 6) |
| A part read answers 502, or 401/403 | rex cannot reach the source's `url` (502), or the source refused its credential (its own status): the log line `PART ... upstream` says which |
| The server is not in the Plex apps | Not claimed, no `--announce-url`, or the announced HTTPS address is not reachable from the internet (step 7) |
| The log says `waiting for a claim` | The server is not claimed: claim it (step 5) and it announces at once |
| The log says `rex-direct: registration failed: HTTP 401` | DMM refused `DMM_API_KEY`: the key is wrong or the sponsorship has lapsed (step 7) |
| A second server on the same catalog exits 1 | The state file is locked by the first: one catalog per server |

### Running several servers: rex manager

`rex manager` is a control service on top of the server. It runs **no** rex server
until you start one: you create a server from a configuration (port, keys,
zurg's mount, catalog, options), start and stop it, and the manager supervises it -- an unexpected exit
is restarted with a backoff, and one that keeps dying is marked failed. Whatever was
running when the manager stopped is running again when it starts.

```bash
rex manager serve                        # API + page on http://127.0.0.1:32300; prints the admin token once
export PLEX_TOKEN=...                    # optional; keys come from the environment, never the command line
export DMM_API_KEY=...                   # optional: rex-direct (step 7)
# from a catalog seeded first (rex catalog --seed --out catalog.json, step 3)
rex manager create living-room --port 32400 --catalog "$PWD/catalog.json" --gdm on
rex manager start living-room
rex manager list                         # status | logs | stop | restart | delete [--purge] | build

# or from its keys alone: an empty library at zurg's mount, for zurg to fill
# (zurg registers with it and pushes once its rex_url names the port)
rex manager create den --port 32401 --gdm on --zurg-mount /mnt/zurg
rex manager build den --start            # rex catalog --seed, then start (restart if running)
```

Each server gets `managed/servers/<name>/`, whose `rex.db` holds its catalog (copied
in; a JSON catalog is converted) and its state. Keys are stored in
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
`/usr/lib/jellyfin-ffmpeg` beside any system FFmpeg, with its own Intel driver. On a J4105
(2026-10-17), a copy unpacked anywhere else (`dpkg -x`) fell back to the system's older iHD,
which could not convert 10-bit HEVC.

```bash
DEB=jellyfin-ffmpeg8_8.1.3-1-$(. /etc/os-release; echo "$VERSION_CODENAME")_$(dpkg --print-architecture).deb   # Ubuntu 22.04/24.04/26.04, Debian 12/13
curl -fLO "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-1/$DEB"
sudo apt install "./$DEB"
export REX_TRANSCODER=/usr/lib/jellyfin-ffmpeg/ffmpeg LIBVA_DRIVER_NAME=iHD   # iHD: Intel only, leave it out on AMD
sudo usermod -aG render "$USER"      # the GPU's /dev/dri/renderD128; log in again after
```

`export` reaches only the current shell: under run.sh or systemd, put the two variables in
`rex.env` instead. A systemd user service only gets the new group once its user manager
restarts (a reboot, or the last login session ending); until then start rex under `sg render`.

| Variable | Default | |
|---|---|---|
| `REX_TRANSCODER` | off | the FFmpeg binary |
| `REX_TRANSCODE_HW` | `auto` | `vaapi`, `cuda` or `none` (`auto` tries VA-API, then NVENC) |
| `REX_TRANSCODE_DEVICE` | `/dev/dri/renderD128` | the VA-API device |
| `REX_TRANSCODE_DIR` | `$TMPDIR/rex-transcode-<port>` | segments; cleared at start |

The startup log says what the probe found (`transcoder: ... hardware vaapi encode=true
tonemapx=true opencl_tonemap=true bounded_http=false decode=[h264,hevc,mpeg2video]`). A
conversion runs on the GPU end to end when it can decode the source there, uploads CPU-decoded frames when it cannot
(AV1 on older Intel), and falls back to x264 when there is no device. HDR and Dolby Vision
are tonemapped on the GPU through OpenCL when the probe tonemapped a frame that way, else on
the CPU, which on a low-power Intel is a tenth of real time at 4K. The OpenCL runtime is a
package of its own:

```bash
sudo apt install intel-opencl-icd   # Ubuntu 22.04: 22.14, enough for a Gemini Lake J4105
```

On a J4105 that took 4K HDR10 and Dolby Vision profile 5 to 1080p at 1.2-1.7x real time,
against 0.1-0.2x on its CPU (findings, "OpenCL tonemapping on fun's J4105").
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

A conversion keeps a selected image subtitle, burned into the picture, and a subtitle
uploaded to rex (copied, or burned for an MPEG-TS client). A client whose target is `mkv`
(Plex for Android and iOS) gets Matroska HLS with the selected embedded text subtitle
copied in, ASS included. Plex Web and other MPEG-TS clients still lose an embedded text
subtitle: Plex Web's `segments-subs` and burning text for MPEG-TS are not built.


### By platform

Every platform but Linux arm64 runs the transcoder suite in CI
(`tests/test_native_transcode.py`, against jellyfin-ffmpeg 8.1.3), on the CPU path, since the
runners have no GPU.

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
