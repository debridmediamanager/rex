# rex

rex is a Plex Media Server that owns no files.

Your movies and shows live on a debrid service or on any web server. rex shows them to the official Plex apps as a normal Plex server. Nothing is downloaded first. When you press play rex streams the file straight from where it lives.

rex gets its library from whatever you point at it. Each source sends rex the list of files it has along with their ffprobe details and keeps it up to date as things are added or removed. [zurg](https://github.com/debridmediamanager/zurg) does this for you today. Anything else works too if it can run ffprobe and send the result to rex's [feed API](docs/feed-api.md). The files themselves can sit on any web server.

## What you get

- Once you add it to your Plex account it shows up in the Plex apps and in app.plex.tv next to your other servers
- Movies and shows get posters, summaries, cast, seasons and episodes from TMDB and from Plex's own metadata
- Watched state, Continue Watching, playlists, collections, edits and Fix Match all work and survive a restart
- Apps on your home network find it on their own when it runs on port 32400
- Files play in their original quality. With FFmpeg it also converts what a device cannot play and a GPU makes that fast. A converted film starts in about 2 seconds even on a small Intel box
- Several sources can fill the same Movies and TV libraries. Two zurgs on two debrid accounts end up in one library
- When a source reports a file gone it leaves the library the same way it would in Plex

## Setup

This is the short version. [docs/setup.md](docs/setup.md) has every option and a troubleshooting table.

You need a Linux, macOS or Windows machine with Python 3, at least one source such as zurg, a free [TMDB API key](https://www.themoviedb.org/settings/api) and your [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

**1. Get it**

Download the build for your machine from [Releases](https://github.com/debridmediamanager/rex/releases) and unpack it. There are builds for Linux (amd64 and arm64, glibc 2.35 or newer, so Ubuntu 22.04 and up), for macOS (Intel and Apple Silicon) and for Windows (64 bit).

```bash
tar xzf rex-v0.1.1-linux-amd64.tar.gz && cd rex-v0.1.1-linux-amd64
```

Or build it yourself. No Rust on the machine? The full guide has a Docker build.

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml
```

Both put rex at `rust/target/release/rex`.

On Windows unpack the zip and use `python` where the steps say `python3` and `rust\target\release\rex.exe` where they say `rust/target/release/rex`. `run.sh.example` and `manager.py` are for Linux and macOS only.

**2. Tell rex where your files are**

Write `sources.json` with one entry per source. Each source gets its own token that it pushes with. Make one with `openssl rand -hex 24`.

```json
{
  "rd": {
    "url": "http://127.0.0.1:9999/dav",
    "token": "<a long random token>"
  }
}
```

`url` is where rex fetches the files from. If the server needs a username and password add `user` and `password` too.

**3. Make the library**

```bash
python3 build_catalog.py --seed --name myrex --out rex.db
```

This makes an empty Movies and TV library. Files under a `movies` directory go to Movies and files under `shows` go to TV, whichever source they come from. If your sources use other directory names like `anime` add `--shows anime` now. rex keeps its library folders from the first start on.

**4. Start it**

```bash
export TMDB_KEY=... PLEX_TOKEN=...
rust/target/release/rex --catalog rex.db --sources sources.json \
  --owner-username <your Plex username> --owner-email <your Plex email>
```

Until you add it to your Plex account in step 6 anyone who can reach port 32400 can browse it. Keep it on your home network until then.

**5. Point zurg at it**

Add this to zurg's `config.yml` and restart zurg.

```yaml
rex_url: http://<rex host>:32400
rex_feed_token: <the token from sources.json>
rex_source: rd
```

zurg sends rex its whole library and then every change as it happens. A movie or episode shows up once zurg has analyzed it. For a library you already had press **Analyze & Tag Library** on zurg's dashboard once. A big library takes a few hours to fill.

**6. Add it to your Plex account**

Open [plex.tv/claim](https://www.plex.tv/claim) and copy the code. It expires in four minutes.

```bash
curl -X POST "http://127.0.0.1:32400/myplex/claim?token=claim-xxxxxxxxxxxx"
```

Do this once per server. rex saves it in `claim_state.json` next to `rex.db`. Back that file up.

**7. Use it outside your home**

app.plex.tv only connects to an HTTPS address and the other Plex apps prefer one. rex has no relay so the address has to be reachable from the internet. Give rex a certificate for your own hostname or put a Cloudflare tunnel in front. Then restart it with that address.

```bash
--announce-url https://rex.example.com:32400
```

Your Plex apps can then reach it from anywhere.

To keep it running copy `run.sh.example` to `run.sh` and start it as a service. The full guide has a systemd unit and a way to run several servers on one machine.

## For developers

rex is written in Rust. A Python version stays in the tree as the reference the Rust one is checked against. [CLAUDE.md](CLAUDE.md) covers the code, the tests and how every behaviour was measured against a real Plex Media Server. [docs/findings.md](docs/findings.md) is the record of those measurements. [docs/feed-api.md](docs/feed-api.md) is the API a source uses to push its library.

## Status

It started as an experiment and got further than expected. It is not a product. It has no multi user story and it does not try to replace Plex.
