# rex

rex is a media server for the official Plex apps that owns no files.

Your movies and shows live on a debrid service or on any web server. rex shows them to the official Plex apps as a normal Plex server. Nothing is downloaded first. When you press play rex streams the file straight from where it lives.

rex gets its library from whatever you point at it. Each source sends rex the list of files it has along with their ffprobe details and keeps it up to date as things are added or removed. [zurg](https://zurg.debridmediamanager.com/) does this for you today. Anything else works too if it can run ffprobe and send the result to rex's [feed API](docs/feed-api.md). The files themselves can sit on any web server.

## What you get

- Once it is added to a Plex account, shared with yours and given an address (HTTPS for app.plex.tv), it shows up in the Plex apps next to your other servers
- Movies and shows get posters, summaries, cast, seasons and episodes from Plex's own metadata
- Watched state, Continue Watching, playlists, collections, edits and Fix Match all work and survive a restart
- Plex apps on your home network also find it on their own, through Plex's LAN discovery on UDP ports 32410 and 32414 when rex uses port 32400
- Files play in their original quality. With jellyfin-ffmpeg installed, which rex finds on its own ([Which FFmpeg](docs/setup.md#which-ffmpeg) has the build for each system), it also converts what a device cannot play, and a GPU makes that fast. A 1080p film converted on a small Intel GPU starts in about 2 seconds
- Search Subtitles finds subtitles from Plex's own provider. rex checks the one you pick against a text subtitle already in the file, or against the audio when FFmpeg is installed, and fixes its timing when it is off. When an account is set to show subtitles and a title has none in its language, rex looks for one for up to 3 seconds before playback starts. See [Subtitles](docs/setup.md#subtitles)
- Several sources can fill the same Movies and TV libraries. Two zurgs on two debrid accounts end up in one library
- When a source reports a file gone it leaves the library the same way it would in Plex
- Movie editions, the full server Dashboard, Downloads, intro and credits skipping and more work with a free claiming account: see [Movie and TV capabilities](docs/setup.md#movie-and-tv-capabilities)

## Setup

This is the short version. [docs/setup.md](docs/setup.md) has the details and a troubleshooting table.

You need a Linux, macOS or Windows machine with 1 GB of RAM, rex nightly 2026.10.25 or newer, and at least one source such as zurg (a zurg nightly from 2026.10.27 or newer: zurg v1.0.0 has no rex feed). rex never needs your Plex token.

**1. Get it**

There are builds on [Releases](https://github.com/debridmediamanager/rex/releases) for Linux (amd64 and arm64, glibc 2.35 or newer, so Ubuntu 22.04 and up), macOS (Intel and Apple Silicon) and Windows (64 bit). This takes the newest for your machine into `~/rex`:

```bash
mkdir -p ~/rex && cd ~/rex
TAG=$(curl -fsSL 'https://api.github.com/repos/debridmediamanager/rex/releases?per_page=1' | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
P=$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
curl -fL "https://github.com/debridmediamanager/rex/releases/download/$TAG/rex-$TAG-$P.tar.gz" | tar xz --strip-components=1
```

On Windows, in PowerShell:

```powershell
New-Item -ItemType Directory -Force $HOME\rex | Out-Null; cd $HOME\rex
$tag = (Invoke-RestMethod 'https://api.github.com/repos/debridmediamanager/rex/releases?per_page=1')[0].tag_name
curl.exe -fLO "https://github.com/debridmediamanager/rex/releases/download/$tag/rex-$tag-windows-amd64.zip"
tar -xf "rex-$tag-windows-amd64.zip" --strip-components=1; Remove-Item "rex-$tag-windows-amd64.zip"
```

A macOS build downloaded in a browser instead is stopped by Gatekeeper: run `xattr -dr com.apple.quarantine .` in its folder.

To build it yourself instead (Rust 1.89 or newer from rustup, as distro packages are often
older; no Rust on the machine? the full guide has a Docker build):

```bash
git clone https://github.com/debridmediamanager/rex && cd rex
cargo build --release --manifest-path rust/Cargo.toml
```

Both put rex at `rust/target/release/rex` (in `~/rex`, or in the clone).

Or run the Docker image, `ghcr.io/debridmediamanager/rex:nightly`: [docs/setup.md](docs/setup.md#docker) has a compose file. The image is private, so a pull may be refused; the download above works for everyone.

On Windows use `rust\target\release\rex.exe` where the steps say `rust/target/release/rex`, and `curl.exe` where they say `curl` (Windows PowerShell's own `curl` rejects `-X`). Windows Firewall keeps your network out of rex until you let it in, once, in PowerShell run as administrator:

```powershell
New-NetFirewallRule -DisplayName rex -Protocol TCP -LocalPort 32400
New-NetFirewallRule -DisplayName "rex discovery" -Protocol UDP -LocalPort 32410,32414
```

`examples/run.sh.example` and `rex manager` are for Linux and macOS only.

**2. Start it**

```bash
rust/target/release/rex
```

`<rex's LAN IP>` in the next steps is the rex machine's address on your home network, such as `192.168.1.20` (`hostname -I` on Linux, `ipconfig getifaddr en0` on macOS, **IPv4 Address** in `ipconfig` on Windows). Every command on this page uses it, run on the rex machine or anywhere else at home.

The first start makes `rex.db` with two empty libraries, Movies and TV Shows (rename them in Plex, or add more with Add Library), and names the server after the machine (rename it in Plex's Settings). Files under a `movies` directory go to Movies and files under `shows` go to TV, whichever source they come from, unless the source placed a file as the other kind. A file under any other directory, like `anime`, goes to TV if its source placed it as an episode and to Movies otherwise. To give a directory libraries of its own, add both `rex_shows: Anime` and `rex_movies: Anime Movies` under it in zurg's `config.yml` (step 4), since DMM places some of its files as films and some as episodes and a kind with no title of its own goes to Movies or TV Shows: rex makes them when zurg connects and moves what that directory already has into them as it is, watch history, Fix Match and edits included. Rename them in Plex as you like. Delete one and it stays deleted, its files going back to Movies and TV Shows, until you add it again with Add Library, whose folder list shows zurg's directories.

`rex.db` holds your library, watch history and your sources' logins. Back it up with `sqlite3 rex.db ".backup rex.db.bak"` (Windows has no `sqlite3`: stop rex with `Stop-Process -Name rex` and copy `rex.db` with its `rex.db-wal` and `rex.db-shm`), together with `claim_state.json` and `rex-direct.json` once they exist, and keep the backups private. [Later: restore, move, remove](docs/setup.md#9-later-restore-move-remove) puts them back.

Until you add it to a Plex account in step 3 rex serves no Plex app: it answers only its claim and, from this machine, your home network and Tailscale, your sources (step 4).

Plex Media Server runs on the same machine? Add `--port 32500` and use `32500` wherever this page says `32400`. Off port 32400 rex leaves LAN discovery to that Plex, and apps find rex through the Plex account it is shared with in step 3.

**3. Add it to a Plex account**

rex plays nothing until this step. It is at your own risk: rex is not made by Plex and Plex could take action against an account that registers it, so use a fresh free Plex account made just for rex, not your own.

Sign in to plex.tv as the fresh account, open [plex.tv/claim](https://www.plex.tv/claim) and copy the code. It expires after about five minutes.

```bash
curl -X POST "http://<rex's LAN IP>:32400/myplex/claim?token=claim-xxxxxxxxxxxx"
```

Do this once per server. rex saves it in `claim_state.json` next to `rex.db`. Back that file up.

Then, still as the fresh account, share rex with your own account in app.plex.tv (Settings, Manage Library Access, Grant Library Access) and accept the invite as yourself. You watch signed in as yourself, with your own watch history. Fix Match works only for the fresh account: rex refuses it to the accounts it is shared with, as Plex does.

**4. Point a source at it**

A source is any program that sends rex its files over rex's feed API. For a debrid account or Usenet that is zurg: add this to zurg's `config.yml` and restart zurg.

```yaml
rex_url: http://<rex's LAN IP>:32400
dmm_api_key: <your DMM API key>   # optional: DMM's exact matches
```

`rex_url` is that plain `http://` address, never an HTTPS one from step 5: rex takes pushes only from its own networks.

`dmm_api_key` is optional, the key you get by sponsoring DMM. With it zurg asks DMM which movie or episode each file is. Without it zurg works that out from the release name: it looks the title up in DMM's free title search and reads the season and episode off each file name (`S01E02`). That matches fewer titles, and an anime whose episodes are numbered across the whole series can be matched as the show, but its episodes stay in file order. A release it cannot match keeps its file name, with episodes in file order.

zurg tells rex where its files are and how to log in to them, then sends its whole library and every change as it happens. A movie or episode shows up once zurg has analyzed it, which zurg starts as soon as its library has loaded.

Only this machine, your home network and Tailscale can push to rex's library or delete what a source pushed. A tunnel or reverse proxy running on the rex machine (frp, Caddy, Tailscale Funnel) looks local to rex, so block `/rex/` in it. A router that gives outside visitors its own address makes them look local too, which opens the feed to the internet. Running two zurgs? Give each a name of its own with `rex_source: rd` and `rex_source: ad`.

Signed in as the fresh account, deleting a title in a Plex app deletes its file at its source too: for zurg, from your debrid account. Accounts rex is shared with cannot delete. Settings, Library, Show Advanced, **Allow media deletion** turns it off. A file a source removes goes to the library's trash, as in Plex.

Anything else registers itself the same way: your own app ([rex with your own app](https://notes.debridmediamanager.com/rex/any-source/)) or an addon for YouTube channels, home videos or Stremio catalogs ([Building rex addons](https://notes.debridmediamanager.com/rex/addons/)).

**5. Use it outside your home**

app.plex.tv only connects to an HTTPS address, even at home, and the other Plex apps outside your home prefer one. `http://<rex's LAN IP>:32400/web` is not Plex Web: rex has no web app, and that page only says whether it is claimed and where to watch. rex has no relay so the address has to be reachable from the internet.

The simplest way is rex-direct, which is on by default. Forward rex's port (32400) on your router to the rex machine. That is all: once claimed, rex gets a name of its own, `<hash>.rex-direct.debridmediamanager.com`, and a certificate for it from DMM, and announces it. The first certificate can take a few minutes. The name points at your public IP, and a new IP is picked up on its own. One Plex account can hold up to ten rex-direct names. Behind CGNAT it cannot work, because nothing outside reaches your router's port. At home, Plex apps can pick that name over the LAN address and app.plex.tv has no other address it can use unless you announce an HTTPS address of your own. So at home your router has to send the connection straight back to rex, or your home network's DNS has to point the name at rex: the check below shows which you need. If your router forwards another outside port to rex, start rex with `REX_DIRECT_PORT` set to that port. `REX_DIRECT_URL=off` turns rex-direct off.

To check it, open the address from rex's `rex-direct: https://...` log line with `/identity` added, on a computer at home and on a phone with Wi-Fi off. Each should show a short XML page with no certificate warning. If only the phone does, your router does not send connections back inside: point the name at rex's LAN IP in your home network's DNS (a router with DNS rebind protection needs an exception for it) and rex answers it at home with the same certificate. If the phone fails too, the port is not forwarded, your connection is behind CGNAT, or the certificate is still being issued (rex's log says so while it is).

You can use your own hostname instead. Give rex a certificate for it with `--tls-cert` and `--tls-key` (PKCS#8, RSA or EC, so the files `tailscale cert` and ACME clients write work as they are) and announce it:

```bash
--announce-url https://rex.example.com:32400 --tls-cert host.crt --tls-key host.key
```

Known before the first start, these flags go in step 2's command. Added later, they need a restart.

No public IP at home (CGNAT)? rex has no relay and will not get one. Use Tailscale serve for devices on your tailnet, or a small VPS whose public IP forwards to rex through a tunnel (frp, with Caddy for HTTPS): the [notes guide](https://notes.debridmediamanager.com/rex/with-zurg/) has the recipe. Avoid tunnels that cap bandwidth or data (Tailscale Funnel, ngrok), and Cloudflare tunnels, which drop the range of the first read so every play start pulls the whole file from your source.

Your Plex apps can then reach it from anywhere.

**Your watch history from another Plex server**

Once your library is in rex, rex can copy what you watched, started and rated on your old Plex server, which has to be running. In app.plex.tv, signed in as yourself, open any movie on the old server, then **⋯**, **Get Info** and **View XML**, and copy the address of the tab that opens. Do the same on a movie on rex and keep only the token at the end of that address, after `X-Plex-Token=`. Then, from a computer on your home network:

```bash
curl -d "<the old server's address>" "http://<rex's LAN IP>:32400/rex/import-watched?X-Plex-Token=<your token on rex>"
```

rex answers how many titles it marked watched, in progress and rated, and how many of the old server's it does not have. It goes by the id Plex's own agents give a title, so a library on Plex's legacy agents brings nothing over. It never changes what you already watched or rated on rex, so run it again once more of your library has arrived. Everyone you share rex with can do the same with their own token. The [notes guide](https://notes.debridmediamanager.com/rex/import-watched/) has each step.

To keep it running, start step 2's command, with any flags from step 5, as a service: [the full guide](docs/setup.md#8-run-it-as-a-service) has the systemd unit, a LaunchAgent for macOS, a scheduled task for Windows, and a way to run several servers on one machine.

rex never updates itself. To update, unpack a newer build into `~/rex` as in step 1 and restart rex. On Windows stop rex first, as for a backup: Windows will not replace a running `rex.exe`. It replaces `rust/`, `data/`, `examples/` and the docs, and leaves `rex.db`, `rex.env`, `run.sh`, `claim_state.json` and `rex-direct.json` alone. With Docker, `docker compose pull && docker compose up -d`. Restoring a backup, moving rex, a lost Plex account, which build you run and removing rex: [Later: restore, move, remove](docs/setup.md#9-later-restore-move-remove).

## For developers

rex is written in Rust. [CLAUDE.md](CLAUDE.md) covers the code, the tests and how every behaviour was measured against a real Plex Media Server. [docs/findings.md](docs/findings.md) is the record of those measurements. [docs/feed-api.md](docs/feed-api.md) is the API a source uses to push its library.

## Status

It started as an experiment and got further than expected. It is not a product. It does not try to replace Plex. Everyone you share it with gets their own watch state, ratings and playlists.

What it does not do:

- Movies and TV only. Music and photo libraries stay empty
- No Live TV or DVR
- A converted stream keeps the subtitle you picked, except a text subtitle in a file other than MKV or MP4, or in a download for offline viewing. Lines too dense to read in time, like an opening song's karaoke, can be left out of a converted stream. Plex for Android and iOS get a text subtitle as a track; every other app gets it drawn into the picture, as every app gets an image subtitle
- Remote playback needs no Plex Pass or Remote Watch Pass, even when both accounts are free (checked in Plex Web; other Plex apps have not been playback-tested). Converting video, GPU included, needs no Plex Pass

## License

MIT. See [LICENSE](LICENSE). The Docker image also carries [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) (GPL), unmodified, from its own releases.

rex is an independent project, not affiliated with or endorsed by Plex, Inc. Plex is a trademark of Plex, Inc.
