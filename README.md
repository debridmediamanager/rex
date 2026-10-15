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

You need a Linux, macOS or Windows machine, at least one source such as zurg and a free [TMDB API key](https://www.themoviedb.org/settings/api). rex never needs your Plex token.

**1. Get it**

Download the build for your machine from [Releases](https://github.com/debridmediamanager/rex/releases) and unpack it. There are builds for Linux (amd64 and arm64, glibc 2.35 or newer, so Ubuntu 22.04 and up), for macOS (Intel and Apple Silicon) and for Windows (64 bit).

```bash
tar xzf rex-vX.Y.Z-linux-amd64.tar.gz && cd rex-vX.Y.Z-linux-amd64
```

The steps below need a release newer than v0.2.0: v0.2.0 predates sources registering
themselves and the catalog seeding itself on first start. Until a newer release is out,
build it yourself. No Rust on the machine? The full guide has a Docker build.

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml
```

Both put rex at `rust/target/release/rex`.

On Windows unpack the zip and use `rust\target\release\rex.exe` where the steps say `rust/target/release/rex`. `examples/run.sh.example` and `rex manager` are for Linux and macOS only.

**2. Start it**

```bash
export TMDB_KEY=...
rust/target/release/rex --catalog rex.db --friendly-name myrex \
  --owner-username <your Plex username> --owner-email <your Plex email>
```

The first start makes `rex.db` with an empty Movies and TV library named `myrex`. Files under a `movies` directory go to Movies and files under `shows` go to TV, whichever source they come from. If your sources use other directory names like `anime`, make the library yourself before the first start instead: `rust/target/release/rex catalog --seed --name myrex --shows shows --shows anime --out rex.db`. rex keeps its library folders from the first start on.

Until you add it to your Plex account in step 4 anyone on your home network can browse it. Keep it there until then.

**3. Point zurg at it**

Add this to zurg's `config.yml` and restart zurg.

```yaml
rex_url: http://<rex's address>:32400
```

That is all rex needs. zurg tells rex where its files are and how to log in to them, then sends its whole library and every change as it happens. A movie or episode shows up once zurg has analyzed it. For a library you already had press **Analyze & Tag Library** on zurg's dashboard once. A big library takes a few hours to fill.

Anyone who can reach rex's port can push to its library or delete what a source pushed, so keep that port off the open internet. Running two zurgs? Give each a name of its own with `rex_source: rd` and `rex_source: ad`. Anything else that serves files can register the same way, see the [feed API](docs/feed-api.md).

A zurg from before this does not tell rex where its files are, so update zurg first.

**4. Add it to your Plex account**

This step is optional and at your own risk. rex is not made by Plex and Plex could take action against an account that registers it. Skip it and rex only answers on your own network.

Open [plex.tv/claim](https://www.plex.tv/claim) and copy the code. It expires in four minutes.

```bash
curl -X POST "http://127.0.0.1:32400/myplex/claim?token=claim-xxxxxxxxxxxx"
```

Do this once per server. rex saves it in `claim_state.json` next to `rex.db`. Back that file up.

From then on rex talks to plex.tv with the server's own token from the claim. That token also lets rex match your titles with Plex's own metadata as well as TMDB's. If you skip this step and still want Plex's matches, set `PLEX_TOKEN` to your [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) in step 2. A claimed server does not need it.

**5. Use it outside your home**

app.plex.tv only connects to an HTTPS address and the other Plex apps prefer one. rex has no relay so the address has to be reachable from the internet. Give rex a certificate for your own hostname with `--tls-cert` and `--tls-key`. Then restart it with that address. rex registers the address once it is claimed. Without `PLEX_TOKEN` it can have the address from the first start: it waits and registers the moment you claim it. Do not put a Cloudflare tunnel in front. Cloudflare drops the range of the first read so every play start pulls the whole file from your source.

```bash
--announce-url https://rex.example.com:32400
```

Your Plex apps can then reach it from anywhere.

To keep it running copy `examples/run.sh.example` to `run.sh` next to `rust/` and start it as a service. The full guide has a systemd unit and a way to run several servers on one machine.

## For developers

rex is written in Rust. [CLAUDE.md](CLAUDE.md) covers the code, the tests and how every behaviour was measured against a real Plex Media Server. [docs/findings.md](docs/findings.md) is the record of those measurements. [docs/feed-api.md](docs/feed-api.md) is the API a source uses to push its library.

## Status

It started as an experiment and got further than expected. It is not a product. It has no multi user story and it does not try to replace Plex.

What it does not do:

- Movies and TV only. Music and photo libraries stay empty
- No Live TV or DVR
- People you share it with see it as the owner does, with the owner's watch state
- A converted stream carries no subtitles yet

## License

MIT. See [LICENSE](LICENSE).
