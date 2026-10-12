# rex

rex is a Plex Media Server that owns no files.

Your movies and shows live on a debrid service or on any web server. rex shows them to the official Plex apps as a normal Plex server. Nothing is downloaded first. When you press play rex streams the file straight from where it lives.

The library comes from [zurg](https://github.com/debridmediamanager/zurg). zurg tells rex about every movie and episode it holds and rex keeps up as things are added or removed. Any other web server works too as long as something tells rex what files it has.

## What you get

- It shows up in the Plex apps and in app.plex.tv next to your other servers
- Movies and shows look the way they do in Plex with posters, summaries, cast, seasons and episodes
- Watched state, Continue Watching, playlists, collections, edits and Fix Match all work and survive a restart
- Apps on your home network find it on their own
- Files play in their original quality. With FFmpeg and a GPU it also converts what a device cannot play. A converted film starts in about 2 seconds even on a small Intel box
- Several sources feed the same libraries. Two debrid accounts and a NAS can all fill one Movies and one TV library
- A file that disappears from its source leaves the library the same way it would in Plex

## Setup

This is the short version. [docs/setup.md](docs/setup.md) has every option and a troubleshooting table.

You need a Linux or macOS machine with Rust, a free [TMDB API key](https://www.themoviedb.org/settings/api) and your [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

**1. Build it**

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml
```

No Rust on the machine? The full guide has a Docker build.

**2. Tell rex where your files are**

Write `sources.json` with one entry per zurg or web server. Each one gets its own token that it pushes with. Make one with `openssl rand -hex 24`.

```json
{
  "rd": {
    "url": "http://127.0.0.1:9999/dav",
    "token": "<a long random token>",
    "mount": "/mnt/zurg"
  }
}
```

`url` is where the files are and `mount` is zurg's `mount_path`. If zurg has a username and password add `user` and `password` too.

**3. Make the library**

```bash
python3 build_catalog.py --seed --name myrex --mount /mnt/zurg --out rex.db
```

This makes an empty Movies and TV library. Add a `--mount` for every source now. rex keeps its library folders from the first start on.

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

The Plex apps only connect from outside to an HTTPS address. Give rex a certificate for your own hostname or put a Cloudflare tunnel in front. Then restart it with that address.

```bash
--announce-url https://rex.example.com:32400
```

It then shows up in your Plex apps everywhere.

To keep it running copy `run.sh.example` to `run.sh` and start it as a service. The full guide has a systemd unit and a way to run several servers on one machine.

## For developers

rex is written in Rust. A Python version stays in the tree as the reference the Rust one is checked against. [CLAUDE.md](CLAUDE.md) covers the code, the tests and how every behaviour was measured against a real Plex Media Server. [docs/findings.md](docs/findings.md) is the record of those measurements. [docs/feed-api.md](docs/feed-api.md) is the API a source uses to push its library.

## Status

It started as an experiment and got further than expected. It is not a product. It has no multi user story and it does not try to replace Plex.
