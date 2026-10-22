# rex

rex is a Plex Media Server that owns no files.

Your movies and shows live on a debrid service or on any web server. rex shows them to the official Plex apps as a normal Plex server. Nothing is downloaded first. When you press play rex streams the file straight from where it lives.

rex gets its library from whatever you point at it. Each source sends rex the list of files it has along with their ffprobe details and keeps it up to date as things are added or removed. [zurg](https://github.com/debridmediamanager/zurg) does this for you today. Anything else works too if it can run ffprobe and send the result to rex's [feed API](docs/feed-api.md). The files themselves can sit on any web server.

## What you get

- Once it is added to a Plex account, shared with yours and given an address (HTTPS for app.plex.tv), it shows up in the Plex apps next to your other servers
- Movies and shows get posters, summaries, cast, seasons and episodes from Plex's own metadata
- Watched state, Continue Watching, playlists, collections, edits and Fix Match all work and survive a restart
- Apps on your home network find it on their own when it runs on port 32400
- Files play in their original quality. With FFmpeg (set `REX_TRANSCODER`, see [Transcoding](docs/setup.md#transcoding)) it also converts what a device cannot play, and a GPU makes that fast. A 1080p film converted on a small Intel GPU starts in about 2 seconds
- Search Subtitles finds subtitles from Plex's own provider. rex checks the one you pick against the file and moves it onto the file when it finds it out of sync, and when an account is set to show subtitles and a title has none in its language, rex looks for one before playback starts. See [Subtitles](docs/setup.md#subtitles)
- Several sources can fill the same Movies and TV libraries. Two zurgs on two debrid accounts end up in one library
- When a source reports a file gone it leaves the library the same way it would in Plex
- Movie editions and the full server Dashboard work with a free claiming account. [Movie and TV capabilities](docs/setup.md#movie-and-tv-capabilities) covers Downloads, real intro/credits analysis, chapter and seek thumbnails, subtitle adjustment, audio enhancement, stream limits and local controls
- The client API, LAN discovery and Plex requests use the same current public PMS release, refreshed automatically; [scope and offline behavior](docs/protocol-traps.md#server-identity)

## Setup

This is the short version. [docs/setup.md](docs/setup.md) has the details and a troubleshooting table.

You need a Linux, macOS or Windows machine and at least one source such as zurg. rex never needs your Plex token.

**1. Get it**

Download the build for your machine from [Releases](https://github.com/debridmediamanager/rex/releases) and unpack it. There are builds for Linux (amd64 and arm64, glibc 2.35 or newer, so Ubuntu 22.04 and up), for macOS (Intel and Apple Silicon) and for Windows (64 bit).

```bash
mkdir -p ~/rex && tar xzf rex-<tag>-linux-amd64.tar.gz -C ~/rex --strip-components=1 && cd ~/rex
```

`<tag>` is the build's name, such as `2026.10.18.0753-nightly` or `v0.3.0`.
Take the newest nightly (`YYYY.MM.DD.HHMM-nightly`) until the next release: v0.3.0 predates
the server naming itself, taking its owner from the claim and trusting what zurg and DMM say
each file is, which the steps below rely on.

On macOS, Gatekeeper will not run a rex downloaded in a browser. After unpacking, run `xattr -dr com.apple.quarantine .` in the unpacked folder, or download it with the [GitHub CLI](https://cli.github.com) (signed in with `gh auth login`), which does not quarantine it (`darwin-amd64` on Intel):

```bash
TAG=$(gh api 'repos/debridmediamanager/rex/releases?per_page=1' --jq '.[0].tag_name')
gh release download "$TAG" -R debridmediamanager/rex -p '*-darwin-arm64.tar.gz'
```

To build it yourself instead (Rust 1.89 or newer from rustup, as distro packages are often
older; no Rust on the machine? the full guide has a Docker build):

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml
```

Both put rex at `rust/target/release/rex` (in `~/rex`, or in the clone).

Or run the Docker image, `ghcr.io/debridmediamanager/rex:nightly` (private, like zurg's): [docs/setup.md](docs/setup.md#docker) has a compose file.

On Windows unpack the zip and use `rust\target\release\rex.exe` where the steps say `rust/target/release/rex`. `examples/run.sh.example` and `rex manager` are for Linux and macOS only.

**2. Start it**

Start it once with everything the later steps need. Nothing below asks for a restart.

```bash
rust/target/release/rex --catalog rex.db --announce-url http://<rex's LAN IP>:32400
```

`<rex's LAN IP>` is the rex machine's address on your home network, such as `192.168.1.20` (`hostname -I` on Linux). Every command on this page uses it, run on the rex machine or anywhere else at home. `--announce-url` tells plex.tv where Plex apps signed in as you reach rex at home. rex announces nothing until it is claimed in step 3, then at once.

The first start makes `rex.db` with two empty libraries, Debrid Movies and Debrid TV, and names the server after the machine (`--friendly-name` picks another name). Files under a `movies` directory go to Movies and files under `shows` go to TV, whichever source they come from, unless the source placed a file as the other kind. A file under any other directory, like `anime`, goes to TV if its source placed it as an episode and to Movies otherwise. To give a directory libraries of its own, add `rex_shows: Anime` and/or `rex_movies: Anime Movies` under it in zurg's `config.yml` (step 4; zurg's next nightly after 2026-10-22): rex makes them when zurg connects and moves what that directory already has into them as it is, watch history, Fix Match and edits included. Rename them in Plex as you like. Delete one and it stays deleted, its files going back to Debrid Movies and Debrid TV, until you add it again with Add Library, whose folder list shows zurg's directories. Once claimed, rex takes the owner's Plex username from the claim.

`rex.db` holds your library, watch history and your sources' logins. Back it up with `sqlite3 rex.db ".backup rex.db.bak"` and keep it private.

Until you add it to a Plex account in step 3 rex serves no Plex app: it answers only its claim and, from this machine and your home network, your sources (step 4).

Plex Media Server runs on the same machine? Add `--port 32500` and use `32500` wherever this page says `32400`. On any port but 32400 rex stays out of LAN discovery, so apps find it through the Plex account it is shared with in step 3.

**3. Add it to a Plex account**

rex plays nothing until this step. It is at your own risk: rex is not made by Plex and Plex could take action against an account that registers it, so use a fresh free Plex account made just for rex, not your own.

Do it before step 4. Titles zurg sends before the claim keep their file names until rex names them in the background, about 300 an hour.

Sign in to plex.tv as the fresh account, open [plex.tv/claim](https://www.plex.tv/claim) and copy the code. It expires in four minutes.

```bash
curl -X POST "http://<rex's LAN IP>:32400/myplex/claim?token=claim-xxxxxxxxxxxx"
```

Do this once per server. rex saves it in `claim_state.json` next to `rex.db`. Back that file up.

Then, still as the fresh account, share rex with your own account in app.plex.tv (Settings, Manage Library Access, Grant Library Access) and accept the invite as yourself. You watch signed in as yourself, with your own watch history. Fix Match works only for the fresh account: rex refuses it to the accounts it is shared with, as Plex does.

The claim gives rex a token of its own. With it rex announces the address from step 2, gets its HTTPS address (step 5) and shows your titles with Plex's own metadata, with no restart. rex needs no Plex token of yours.

**4. Point zurg at it**

Add this to zurg's `config.yml` and restart zurg.

```yaml
rex_url: http://<rex's LAN IP>:32400
dmm_api_key: <your DMM API key>   # optional: DMM's exact matches
```

`dmm_api_key` is optional, the key you get by sponsoring DMM. With it zurg asks DMM which movie or episode each file is. Without it zurg works that out from the release name: it looks the title up in DMM's free title search and reads the season and episode off each file name (`S01E02`). That matches fewer titles, and an anime whose episodes are numbered across the whole series can be matched as the show, but its episodes stay in file order. A release it cannot match keeps its file name, with episodes in file order. Leave `plex_server_url` pointing at your real Plex, or empty: pointed at rex, zurg learns rex's own matches and a wrong one could never be fixed.

zurg tells rex where its files are and how to log in to them, then sends its whole library and every change as it happens. A movie or episode shows up once zurg has analyzed it, which zurg starts as soon as its library has loaded.

Only this machine, your home network and Tailscale can push to rex's library or delete what a source pushed. rex refuses that API to any other address, so forwarding rex's port for rex-direct (step 5) puts Plex's API on the internet, which needs a token once rex is claimed, but not the feed. Running two zurgs? Give each a name of its own with `rex_source: rd` and `rex_source: ad`. Anything else that serves files can register the same way, see the [feed API](docs/feed-api.md).

This needs a zurg nightly from 2026.10.17 or later, so update zurg first. The latest zurg release, v1.0.0, has no rex feed.

**5. Use it outside your home**

app.plex.tv only connects to an HTTPS address and the other Plex apps outside your home prefer one. rex has no relay so the address has to be reachable from the internet.

The simplest way is rex-direct, which is on by default. Forward rex's port (32400) on your router to the rex machine. That is all: once claimed, rex gets a name of its own, `<hash>.rex-direct.debridmediamanager.com`, and a certificate for it from DMM, and announces `https://<name>:32400` after the address in `--announce-url`. The first certificate can take a few minutes. The name points at the public IP rex calls DMM from, and rex checks it every six hours, so a new IP is picked up on its own. Behind CGNAT it cannot work, because nothing outside reaches your router's port. Plex apps at home still use the LAN address. If your router forwards another outside port to rex, set `REX_DIRECT_PORT` to that port. `REX_DIRECT_URL` points rex at another provider ([the API](docs/rex-direct-api.md)), and `REX_DIRECT_URL=off` turns it off.

You can use your own hostname instead. Give rex a certificate for it with `--tls-cert` and `--tls-key` (PKCS#8, RSA or EC, so the files `tailscale cert` and ACME clients write work as they are) and put the address first in step 2's flag, the LAN address after it:

```bash
--announce-url https://rex.example.com:32400,http://<rex's LAN IP>:32400
```

A second `--announce-url` replaces the first, so keep both in one. Known before the first start, these flags go in step 2's command. Added later, they need a restart.

No public IP at home (CGNAT)? rex has no relay and will not get one. Use Tailscale serve for devices on your tailnet, or a small VPS whose public IP forwards to rex through a tunnel (frp, with Caddy for HTTPS): the [notes guide](https://notes.debridmediamanager.com/guides/rex/) has the recipe. Avoid tunnels that cap bandwidth or data (Tailscale Funnel, ngrok), and Cloudflare tunnels, which drop the range of the first read so every play start pulls the whole file from your source.

Your Plex apps can then reach it from anywhere.

To keep it running copy `examples/run.sh.example` to `run.sh` next to `rust/`, give it step 2's flags and start it as a service. The full guide has a systemd unit and a way to run several servers on one machine.

To update, unpack a newer build into `~/rex` as in step 1 and restart rex. It replaces `rust/`, `data/` and `examples/` and leaves `rex.db`, `rex.env`, `run.sh`, `claim_state.json` and `rex-direct.json` alone. With Docker, `docker compose pull && docker compose up -d`.

## For developers

rex is written in Rust. [CLAUDE.md](CLAUDE.md) covers the code, the tests and how every behaviour was measured against a real Plex Media Server. [docs/findings.md](docs/findings.md) is the record of those measurements. [docs/feed-api.md](docs/feed-api.md) is the API a source uses to push its library.

## Status

It started as an experiment and got further than expected. It is not a product. It does not try to replace Plex. Everyone you share it with gets their own watch state, ratings and playlists.

What it does not do:

- Movies and TV only. Music and photo libraries stay empty
- No Live TV or DVR
- A converted stream keeps the subtitle you picked, except a text subtitle in a file other than MKV or MP4, or in a download for offline viewing. Lines too dense to read in time, like an opening song's karaoke, can be left out the first time a title converts. Plex for Android and iOS get a text subtitle as a track; every other app gets it drawn into the picture, as every app gets an image subtitle
- Remote playback without a Plex Pass or Remote Watch Pass is verified in Plex Web 4.163.0 with the October 21 remote-playback update, even when both the claiming and viewing accounts are free. Other Plex apps have not been playback-tested. Converting video, GPU included, needs no Plex Pass. Server capabilities do not grant Plex cloud subscriptions, Home membership or an app's own Downloads eligibility

## License

MIT. See [LICENSE](LICENSE).
Recorded PMS resource assets retain their upstream copyrights; rex's MIT license
does not relicense third-party artwork.
