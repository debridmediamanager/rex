# rex

A Plex Media Server that owns no files. It answers the PMS API well enough that
the **official** Plex clients treat it as a real server, and serves a library its
sources push to it -- zurg's, streamed from zurg -- instead of local media.

Not a Plex fork and not a client. It is the server side of the protocol,
written from measurement against a real PMS.

**rex is the Rust binary in `rust/`**, and nothing else: the server, the catalog
writer (`rex catalog`) and the manager (`rex manager`). It was ported from a Python
server, the **oracle**, which was removed on 2026-10-14; the last Python-only `main` is
the `origin/python-server` branch, and the last tree holding both is `758bd5f`. Code comments
and dated findings that name a Python file (`server.py`, `persist.py`, `library.py`, ...)
or "the oracle" mean that server; read it there with `git show 758bd5f:oracle/<file>`.
What it answered, measured against a real PMS, is kept as recorded values: the parity
suites' expectations and `tests/fixtures/oracle_golden/`.

## The one rule that matters

**Every emulated response is a measurement, not a guess.** When something does
not render, capture what a real PMS answers *for the client's exact request*
and diff it. Never hand-write what you think the shape should be.

That is not style advice. The longest bug in this project — show pages drawing
no Seasons block — survived substituting a real server's recorded `/children`
payload wholesale, because the recording was taken without the paging params
the client actually sends, so it was missing the same field. Replaying the
client's exact request is the difference between finding it in an hour and not
finding it at all. See `docs/findings.md`.

## rex identifies nothing

**rex has no identification of its own.** What a file is -- which film or show, which season
and episode, whether it is an extra and of which kind -- is decided by its source (zurg,
asking DMM's placement) and pushed in the feed. rex's only part is exchanging the content id it
is given (the pushed TMDB/IMDb id, or the title a person picks in Fix Match) for Plex's guid, and
showing what Plex's metadata agent says of that guid: rex reads no TMDB, no `TMDB_KEY` and no
metadata provider, and trusts what its source pushed without checking it (Ben, 2026-10-17). Never add name parsing, number reading, folder or tag rules,
title searches or `/find` lookups to rex; a misfiled item is fixed in zurg or in DMM's
placement. Ben's decision, 2026-10-17.

Corollary: when the payload looks right and the client still renders nothing,
**read the client**. rex serves the web client bundle, so its chunks can be
searched on disk. The gate is usually one truthiness check in minified code.

## Layout

| Path | What it is |
|---|---|
| `rust/` | rex (`runtime.rs` and `runtime/*.rs` hold the routes). Build with `cargo build --release --manifest-path rust/Cargo.toml` |
| `data/` | The measured sidecars the server reads (below) |
| `examples/` | `run.sh.example` and `catalog.example.json`, the catalog's shape |
| `tests/` | Conformance and end-to-end suites, plus `catalog_ui.json` and `fixtures/decisions/`, the measured decision corpus |
| `tests/fixtures/` | Redacted corpora recorded from a real PMS through a real client; `media/` pairs real files' probes with a real PMS's analysis of them |
| `harness/` | plexlab — the reference PMS and the recorder. See below. |
| `probes/` | Measurement scripts: the recorders behind checked-in fixtures (`legacy_scan_fixture.py`, `record_session_gate.py`, `record_shared_user.py`), `ui_surface.py`, the plex.tv presence probes and `apkcensus.py` |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |
| `docs/protocol-traps.md`, `docs/setup.md`, `docs/feed-api.md`, `docs/rex-direct-api.md` | The protocol's edge cases, the operator's guide, the API sources push with, and the one a rex-direct provider answers |

Sidecar fixtures are captured from a real PMS -- the later ones read straight out of its
image -- and live in `data/`. The descriptors (`server_prefs.json`, `regional_server_prefs.json`,
`section_prefs.json`, `section_meta.json`, `section_meta_show.json`, `section_meta_artist.json`, `item_prefs.json`,
`section_prefs_new.json`, `section_filters.json`, `section_sorts.json`, `system_agents.json`,
`legacy_agents.json`, `agent_languages.json`, `languages.json`, `translations.json`, `refusal_strings.json`,
`client_profiles.json`, `spellfix_static_tags.json`, `legacy_agent_config.json`); the strings a
real PMS localises per `X-Plex-Language` (`activity_strings.json`, `item_language_names.json`,
`stream_words.json`, `title_templates.json`); the image's own tables
(`plugin_bundles.json`, `plugin_prefs.json`, `attributions.json`, `resource_assets.json`,
`media_flags.json`); and three files served at the top of the surface with no token
(`crossdomain.xml`, `clientaccesspolicy.xml`, and `favicon.ico`, which is rex's own icon:
rex ships none of Plex's artwork). Runtime files never go in
`data/`: `plex_cache.db`, `claim_state.json` and `rex-direct.json` are resolved beside the
**catalog**, because they are state rather than measurement.
The state file is state too: a SQLite database beside the catalog by default
(`rex.db`), the catalog itself when the catalog is a database, or wherever `--state`
names.

The native server finds the same sidecars in `data/` beside the catalog, then one directory
up, then in its working directory, trying each directory itself after its `data/` so a
deployment that keeps them beside its catalog still works (`sidecar_roots` in `runtime.rs`).
Run it from the repository root or keep the catalog there -- `run.sh`, copied to the root from
`examples/run.sh.example`, `cd`s to its own directory.
`client_profiles.json`, `languages.json` and the decision corpus are compiled into the
binary with `include_str!`, so a change to them needs a rebuild. `plex_cache.db` and
`claim_state.json` sit beside the catalog.

Where things live in `rust/src/`:

| Path | What it is |
|---|---|
| `main.rs` | The CLI: the server flags (argparse's, refusals included), the `catalog` and `manager` subcommands, and the test-only adapters the suites drive (`test_native_phase2.py`, `test_mediainfo.py`, the corpus recorders) |
| `catalog_tool.rs` | `rex catalog`: `--seed` an empty library at each source's mount (`--mount`, `--movies` and `--shows` repeat; a re-seed keeps the machineIdentifier), `--convert` a JSON catalog into a database, `--relink` Plex's guids |
| `manager.rs` | `rex manager`: the control service that runs no server until one is started, then supervises the ones it started (API on 127.0.0.1:32300, CLI, a page); Linux and macOS only; docs/setup.md "Running several servers" |
| `runtime.rs` | The request loop, the route table and most projections; `serve` starts GDM, the state saver and the plex.tv threads |
| `runtime/wire.rs` | The response envelope: header order, Range/416, CORS, gzip, websocket handshake |
| `runtime/upstream.rs` | The HTTP/HTTPS client (rustls, system roots) behind zurg, provider redirects, Plex's metadata agent and artwork |
| `runtime/source.rs`, `runtime/feed.rs`, `runtime/scan.rs` | Sources (registered over `PUT /rex/sources/{name}`, kept in the state file, deleted with a confirmation: where a Part's bytes are), the feed API sources push items to, open to any peer on a private network (`feed::source_peer`, `docs/feed-api.md`), and the rows those items become: added, held until probed, filled, re-identified, trashed, keys minted once (`mint_key`); a show's file its source did not place is numbered in its release's file order (`number_unplaced`), never by its name |
| `runtime/universal.rs` | The video transcoder (`REX_TRANSCODER`): the conversion decision's plan and Media, `start.m3u8`/`start.mpd`, segments served outside the runtime lock, one FFmpeg per session with the hardware ladder, seeks, throttling and the idle reaper, and the `TranscodeSession` rows |
| `runtime/plexmeta.rs`, `identify.rs` | Plex's metadata agent (the claim's server token), and which title a release is: the ids its source placed it with, or a TMDB id set by hand at the source (`placed`; `scan::identified_by`), asking Plex's agent for its guid by external id, whose answer and children rows are all a row shows (`metadata::describe`); nothing is read off a name, and what arrives with neither stays for Fix Match, which searches Plex's agent |
| `runtime/plextv.rs` | claim/heartbeat/account state (`REX_PLEXTV_URL` points plex.tv and Plex's agent at a stub) |
| `runtime/rexdirect.rs` | rex-direct, a plex.direct of rex's own (`docs/rex-direct-api.md`): on by default, once claimed it registers the machineIdentifier with the claim's server token at its provider (`REX_DIRECT_URL`, DMM's by default, `off`, https or loopback only; `REX_DIRECT_KEY` an optional bearer key), serves the certificate it answers by SNI beside `--tls-cert`'s (`Certificates`, the TLS port's cert resolver), appends `https://<name>:<REX_DIRECT_PORT or --port>` to the announce, keeps the answer in `rex-direct.json`, and registers again every 6 h (10 min after a failure, a 202's `retryAfter` within 15 s..10 min); unclaimed it waits in `PlexTv::direct` for the claim (`plextv::finish_claim`) |
| `runtime/accounts.rs` | Which plex.tv account each request comes from (the access-token list: owner 1, a share its plex.tv id, with its libraries, filters, flags and track settings) and what that account keeps and sees: its watch state, ratings, hides and track picks shown over the rows while its request runs (`rows::Overlay`), its own playlists and history, the share's libraries and filters pruned from every answer, the owner's routes refused (findings, "Each account its own") |
| `platform.rs` | Everything asked of the OS beyond `std`: local time, CPU time and load, the host name, thread ids, pausing and killing FFmpeg, the GDM socket. A Unix half and a Windows half; nothing else may call `libc` for these |
| `runtime/*.rs` (the rest) | One subsystem each, named after it: search, listing, axes, edit, collections, playlists, artwork, butler, framework, i18n, resume (On Deck and hubs), logging and so on |
| `filters.rs`, `hierarchy.rs`, `metadata.rs`, `mediainfo.rs`, `decision.rs`, `xmlout.rs`, `gate.rs`, `catalog.rs` | Plex's filter grammar (the operator is part of the KEY), flat leaves to show/season/episode, Plex's description of a title and `plex://` guids, ffprobe JSON to `Media`/`Part`/`Stream`, a plain-form decision against the client's own profile, Plex XML, the direct-play gate, the catalog |
| `store.rs` | The state file: a SQLite database written by the row, holding the catalog too when the catalog is a database |
| `rows.rs` | Where the rows are: in the state file once a start has restored them (`Catalog::attach`), memory holding the rows written since their save reached the file and a byte-capped cache of rows read (`REX_ROW_CACHE_MB`, trimmed after each request); a listing's rows read in parallel without their streams and kept nowhere (`read_listed`, `prefetch_listed`); another account's view fields shown over the rows for its request, a row changed only once written (`Overlay`). With `--no-state` every row stays in memory |
| `hotrow.rs`, `corpus.rs` | The fields of a row that passes over the whole library read (`HotRow`; `Lazy` reads the row for any other), and each row's search entry: both kept in the catalog's index beside its lookups (`catalog::By`), re-derived for a row written |

A movie library and a show library have **different**
`Meta` descriptors, which is why there are two: retargeting one onto the other
gives the client a grid whose filter bar can only talk about the wrong type.

## Running it

```bash
cargo build --release --manifest-path rust/Cargo.toml
rust/target/release/rex --catalog rex.db   # seeds rex.db on its first start, named after the host, under /mnt/zurg
cp examples/run.sh.example run.sh   # keys go in rex.env beside it (mode 600)
```

Sources push the library; rex reads no media itself, lists and queries nothing, and
has no provider code of its own (`docs/feed-api.md`). A `--catalog` that does not exist is
seeded on start as `rex catalog --seed` seeds one (`--friendly-name`, default the host name; its
libraries under `--zurg-mount`); `rex catalog --seed` stays for naming more directories or
re-seeding. A source registers itself -- `PUT /rex/sources/{name}`: its byte url, `user`/
`password` or `bearer`, `mount` -- and rex keeps the registration in the state file; it then
pushes to `/rex/feed/{name}`. Both take no token, claimed or not and with or without
`--token` (a feed token per source, set by hand on both sides, was the setup's longest
step), and answer only a peer on a private network: loopback, RFC 1918, 100.64/10
(Tailscale), link-local, fc00::/7 and fe80::/10, IPv4-mapped ones included, read off the
socket alone and never `X-Forwarded-For` or `Host`; any other peer is a 403 (`feed::source_peer`,
which adds Tailscale and link-local to `private_address`, the unclaimed gate's rule). The
unclaimed gate itself refused zurgs naming rex by host name or over Tailscale, so it is not
this one. rex-direct forwards rex's port from the internet, and this keeps the feed off it. Registering is the only way a source
exists (`ZURG_URL` is not read): a row keeps its `_source`/`_path`, and a catalog row from
the old WebDAV scanner reads through the registered source whose `mount` holds its file,
so either plays again once its source registers. `DELETE /rex/sources/{name}` answers a
one-use code valid 300 s; `DELETE .../confirm/<code>` removes the source and what it pushed
from rex, never its files. With no source a part read is the 502 an unreachable source is. Plex's
matcher names and describes what its source placed, or gave a TMDB id by hand, and nothing else is
identified; it is asked with the claim's server token, and is off without a claim (under
`--assert-claimed`), which leaves every title its pushed name and Fix Match no candidates. `--zurg-mount` is zurg's
`mount_path` (default `/mnt/zurg`): a Part's `file` is `<mount>/<directory>/<release>/<file>`,
and each library's `Location` is `<mount>/<directory>` -- set in the catalog's sections
(`rex catalog --seed`, or the seed on start), or through Add Library's `location`.

Point zurg at the server in zurg's `config.yml`: `rex_url` (this server's URL, an address
or a host name) is all the feed needs; zurg registers itself as the source `zurg` (or its
`rex_source`). Set `mount_path` (`--zurg-mount`). Leave zurg's `plex_server_url` on a real
Plex, if there is one: pointed at rex, zurg's matcher learns rex's own matches and pushes
them back as exact ids, so a wrong match can never be corrected (`docs/setup.md`). zurg then pushes a snapshot of its library once it has
loaded and every few hours, and every change as it happens: `POST`/`DELETE
/rex/feed/zurg/items`, `PUT /rex/feed/zurg/snapshot` (`runtime/feed.rs`). An item
with a `kind` and ids is the title those ids name, placed where zurg says (TMDB's season and
episode; `_placed` on the row); one with no `kind` but a TMDB id (set by hand in zurg) is that
title in its library's kind, checked against nothing; anything else is unmatched, a film titled as its file and an
episode season 1 of a show named after its release, in file order, until zurg places it. rex
reads no name: no pass at start renumbers, unfolds or moves a row, and the blank-tile retry asks
again only for a row its source placed, by the ids it came with (older rows hold ids zurg's own
Plex gave, an episode's among them, and stay as they are). An item
with no `media_info` is held, not shown, until zurg probes it; a late placement re-identifies
its row on the same key; a file that is gone is trashed as measured on plexscratch
(`tests/fixtures/missing_file_scan_s43.json`: soft delete, deleted at once under
`autoEmptyTrash`, back on its key until `emptyTrash`); a snapshot that names nothing, or
would remove more than half the source's items, removes nothing without `force`. A row's
ratingKey is minted once (100000 and up) and never reused: zurg drops a release's IMDb id
when the key it matched it to disappears. The first push of a file the old WebDAV scanner
made a row for adopts that row and its key. `/library/sections/{id}/refresh` and
`/activities` are what they are on a library with nothing to read; old zurg builds still
send refreshes and change nothing.

`REX_TRANSCODER` (an FFmpeg; docs/setup.md "Transcoding") turns on conversion: a decision the
client cannot direct-play becomes the reference's 3000/1001/1001 with the converted Media,
and `start.m3u8`/`start.mpd` start one FFmpeg per session reading the Part from its source.
Without it rex is the encoder-less server every suite measures. The transcoder, like the
feed, is rex's own, and only its wire shapes are the reference's (`tests/fixtures/transcode/`, recorded on plexscratch for Plex Web's exact
requests). Segment lengths, encoder settings and the MPD's `minBufferTime` are ours, chosen
by measurement (findings, "The universal transcoder").

`--announce-url` announces with the claim's server token only; unclaimed it waits for the
claim and announces the moment it lands, no restart (findings, "A
claimed server needs no account token"). A comma-separated list announces each URI as its own
connection, as a PMS with several does. The feed is rex's own API, not PMS emulation.

rex-direct is a plex.direct of rex's own, not a PMS's (`runtime/rexdirect.rs`), and a
documented provider API (`docs/rex-direct-api.md`), no longer a DMM sponsor perk: rex reads no
`DMM_API_KEY` (Ben, 2026-10-19). It is on by default with no key; `REX_DIRECT_URL` names the
provider (default DMM's `/api/rex-direct/register`) or `off`, and must be https, or plain http
to loopback only, since the answer carries a private key and the request the claim's token.
The provider knows the caller by the claim, so rex registers only once claimed: a claimed start
POSTs its machineIdentifier before the port opens, with `X-Plex-Token` (the claim's server
token) and `X-Plex-Client-Identifier`, plus `Authorization: Bearer
<REX_DIRECT_KEY>` when that is set; an unclaimed one registers the moment `/myplex/claim` lands,
in the background before the claim's device PUT, no restart. The provider checks the token
with plex.tv's `/api/v2/resources`, points its name at the request's source IP and answers the
name and a certificate for it (DMM: one per server). rex serves that certificate to a client
whose SNI is that name or, for a wildcard, under it, and `--tls-cert`'s to every other (to every
name when there is no `--tls-cert`; with neither, the port is plain only as before), and appends
`https://<name>:<REX_DIRECT_PORT or --port>` to `--announce-url`'s list, the heartbeat reading
its target each beat. The answer is kept in `rex-direct.json` beside the catalog (0600: it holds
the private key) and serves a start the provider does not answer. A 202 is a provider still
issuing the certificate: an INFO, asked again after its `retryAfter` (15 s..10 min). Any other
refusal or an unreachable provider is a WARN and never fatal. A thread registers again every
6 h, which is the dynamic DNS and swaps in a renewed certificate without a restart, and every
10 min after a failure. `tests/test_rex_direct.py` drives it against a stub provider and plex.tv.

A real claim -- `POST /myplex/claim?token=` with a token from `plex.tv/api/claim/token.json`,
once per machineIdentifier -- makes rex register the way a real PMS does (findings, "The
experiment, run"): `POST /servers.xml` writes plex.tv's server record (422 is its normal
answer), and the pubsub stream it then holds open is what clients read as `presence`. The
claim is kept in `claim_state.json`, so a restart re-registers without claiming again. A
claimed server gates every request as a real PMS does: its own token, the tokens
`plex.tv/api/v2/server/access_tokens` lists for it (re-read for an unknown token, and kept in
`claim_state.json` so a failed read after a restart locks no one out), and the `transient-`
tokens its own `/security/token` minted (findings, "The token gate a claimed server keeps").
To plex.tv a claimed server names itself the newest public PMS, read from plex.tv's release
list (`/api/downloads/5.json`) before the claim and every 24 h, with `X-Plex-Device-Vendor:
Docker` as the recorded PMS sends; clients keep the measured `1.42.2.10156-f737b826c`.
A conversion's own playlist and segments (`/video/:/transcode/universal/session/...`) are
keyed to the session, not the token, as on a real PMS (findings, "A conversion session is its own key").
An unclaimed server without `--token` serves no client (Ben, 2026-10-19): with no claim it
has no token to ask Plex's agent with. It answers the always-open paths (`/identity`, `/web`,
the source API) and takes `POST /myplex/claim` only from its own networks, as an unclaimed PMS
gates every request (`local_request`; findings, "The gate an unclaimed server keeps").
`--assert-claimed`, the suites' stand-in for a claim, keeps that gate for every route. rex reads
no account token: `PLEX_TOKEN` is not read since 2026-10-19. An unclaimed server announces
nothing: plex.tv applies a device PUT to the device that owns the token, so the 2026-09-11
spike's unclaimed announce with zurg's account token rewrote the account's "zurg" device row
(findings, "Collateral change to the Plex account"). A claimed server answers another claim
with 409.

`rex.db`, `rex.env`, `run.sh` and `rex-direct.json` are gitignored: the catalog and
`rex.env` carry account-scoped credentials, `rex-direct.json` a private key. `examples/catalog.example.json` keeps the shape.

Under launchd, so it outlives the shell that starts it:

```bash
launchctl submit -l com.rex.dev -o rex.log -e rex.err -- /bin/zsh "$PWD/run.sh"
launchctl remove com.rex.dev
```

A plain `nohup ... &` from an agent tool call **does not survive** the call.

Several servers, started and stopped on demand: `rex manager serve`, then
`rex manager create NAME --port P [--catalog PATH] [--zurg-mount M] [--friendly-name N]` (keys from
`REX_TOKEN`/`REX_DIRECT_KEY`, and `REX_DIRECT_URL`, in the environment; a saved `dmm_api_key`
or `plex_token` loads and reaches no server; sources register with each server) and `start|stop|list|logs NAME`; a server with no catalog seeds one on its first start
(`build NAME` seeds it ahead of time). Its data
lives in `managed/` at the repository root (gitignored, mode 700); `tests/test_manager.py` drives it end to end and
starts its own manager on a temporary root.

The state file keeps what clients change -- watch state, ratings, edits, deletes,
playlists, collections, history, sections, preferences, uploads, queues, and the device
and statistics tables -- across restarts; `rust/src/store.rs` says what is and is not kept.
It is **on by default** since 2026-10-05: a SQLite database written by the row since
2026-10-06 (`rust/src/store.rs`), `rex.db` beside the catalog or the catalog's own
database when the catalog is one (`rex catalog --convert`); an old
`state.json` is imported once and kept as `state.json.migrated`. `--state FILE` / `REX_STATE` move it. A second server on the
same catalog finds the default locked and exits 1, as a second PMS on a served data
directory does (cycle 92). Every suite relies on a
restart being the undo button, so **a test instance runs with `--no-state`**:
`tests/server_command.py` adds it to every server a suite starts, so only a leg that names
`--state` keeps one. The file is tied to the
catalog's digest; after a catalog rebuild only view state (by guid, then part file) and
the server-level settings carry over, and the old file is kept as `<file>.stale` --
unless the saved rows include scanned ones (`_scanned`): a library a source pushed is the
state's own and is kept row by row whatever the catalog file says, its new rows joining.
Held items (`feedHeld`) are kept with it. Its rows are where the catalog keeps them (`rust/src/rows.rs`):
the file's shape is unchanged, so every release reads what another wrote
(`tests/test_state_compat.py` against an older release's binary).

## The reference harness

`harness/` is **plexlab**: a real Plex Media Server, the official web client,
and a recording of everything that passes between them and plex.tv. It is the
instrument for the rule above — it is how you find out what a real PMS answers
for the client's exact request, instead of guessing.

It runs on a lab host; the private operations notes say which, and where the live lab
lives. The repo holds the source; the deployed directory holds the live instance, its
captures and its `.venv`. Below, `LAB` is the lab host's name and `LAB_IP` its address.

Two capture axes, deliberately separate. Both write one JSON object per
exchange with method, path, **every query param in order**, every header and
both bodies. Media bodies are recorded by length only — a 1.8 GB part read is a
fact about status and headers, not something to keep on disk.

| Axis | File | What it holds |
|---|---|---|
| client → server | `capture/sessions/<label>.jsonl` | what the official client asks for and what a real PMS answers. The corpus rex must match. |
| server → plex.tv | `capture/outbound.jsonl` | how a real PMS registers and re-announces itself — the traffic that makes a server *detected* rather than merely reachable. |

| Endpoint | What |
|---|---|
| `http://LAB:32400` | the reference PMS |
| `http://LAB:9081` | mitmweb, client axis (password `plexlab`) |
| `http://LAB:7902` | the browser's desktop, for watching a session run |
| `http://LAB_IP:9224` | CDP, for driving it. **Not** `http://LAB:9224`: Chrome refuses a `Host` that is not an IP or localhost, and nothing rewrites it on this port. Verified 2026-09-22. |

### Setting it up from scratch

Needs Docker with the compose plugin, `ffmpeg`, and `python3-venv` on the host.

```bash
./scripts/make-media.sh                     # the reference library
python3 -m venv .venv && .venv/bin/pip install websocket-client requests
cp .env.example .env                        # identifiers filled in after the claim
./lab.sh up
```

The library is ten ten-second synthetic clips named as real titles — four
Blender open movies and two Firefly seasons. Plex matches on the **filename**,
so its real metadata pipeline engages: the reference server ends up holding
genuine `plex://movie|show|season|episode/<24hex>` guids and real TMDB episode
titles, with no real media on disk and nothing downloaded.

Then create the libraries and claim the server:

```bash
P=http://127.0.0.1:32400
curl -s -X POST "$P/library/sections?name=Movies&type=movie\
&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US&location=%2Fdata%2Fmovies"
curl -s -X POST "$P/library/sections?name=TV%20Shows&type=show\
&agent=tv.plex.agents.series&scanner=Plex%20TV%20Series&language=en-US&location=%2Fdata%2Fshows"
curl -s "$P/library/sections/1/refresh"; curl -s "$P/library/sections/2/refresh"

CLAIM=$(curl -s https://plex.tv/api/claim/token.json -H "Accept: application/json" \
        -H "X-Plex-Token: $ACCOUNT_TOKEN" -H "X-Plex-Client-Identifier: plexlab-probe-1" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -X POST "$P/myplex/claim?token=$CLAIM"      # expect claimed="1" at /identity
```

Section creation answers **400 while `/identity` still reports
`startState="startingPlugins"`**. Wait for that to clear rather than concluding
the parameters are wrong.

Collect the four values the harness needs afterwards:

```bash
curl -s $P/identity                                        # machineIdentifier -> .env
docker exec plexlab-plex sh -c \
  "grep -o 'PlexOnlineToken=\"[^\"]*\"' \
   '/config/Library/Application Support/Plex Media Server/Preferences.xml'"   # server token
# certificate hash: the <hash> in the server's own
# https://<ip-with-dashes>.<hash>.plex.direct:32400 connection URI -> .env
```

`FriendlyName` is set through `/:/prefs`, not `hostname:` in compose — see the
traps below.

### Running the harness

```bash
./lab.sh up | status | restart | down
./lab.sh tail                       # follow both capture files live
```

Capture a client session. The walk goes home → each section → grid → a movie →
show → season → episode, which is the path whose `/children` requests carry the
paging params that matter:

```bash
.venv/bin/python scripts/session-web.py --label reference-walk \
    --token "$ACCOUNT_TOKEN" --server-token "$SERVER_TOKEN" --machine "$MACHINE_ID"
```

Diff a server against a captured corpus. `--reference` re-fetches the reference
live at the same moment, which removes the state drift you otherwise see between
capture time and replay time:

```bash
.venv/bin/python scripts/replay.py --corpus capture/sessions/reference-walk.jsonl \
    --target http://127.0.0.1:32500 --target-token "$REX_TOKEN" \
    --reference http://127.0.0.1:32400 --reference-token "$SERVER_TOKEN" \
    --out capture/diffs/rex.md
```

`replay.py` reissues each request byte-for-byte — same path, same query params
in the same order, same `X-Plex-*` headers — and compares element structure,
attribute presence **and attribute values**. Values are not optional: a
key-level comparison alone would have missed `viewMode`, where both sides carry
the field and only the values are swapped. Fields that differ per server by
construction are listed explicitly in `VOLATILE` rather than guessed at, and
fields whose value has been load-bearing in a past rendering bug are flagged in
the report.

Turn a capture into a checked-in fixture. This keeps only the server-directed
exchanges and strips credentials from the query, the headers **and the bodies** —
a token also rides inside the nested `url=` parameter of `/photo/:/transcode`:

```bash
python3 scripts/redact.py --corpus capture/sessions/reference-walk.jsonl \
    --out ../tests/fixtures/reference-walk.jsonl
```

Sanity-check the tooling by replaying a corpus at the reference server itself.
23 of 27 came back clean when this was built, and all four differences were
genuine state drift — a rename, geoip resolving, the Recently Added hub
settling. A larger number than that means the harness, not the target.

### Harness traps

- **Start order is load-bearing.** `plex` shares `gw`'s network namespace and
  `cdp-relay` shares `browser`'s. A sharer that starts before its host, or
  survives its host being recreated, lands in a namespace that no longer exists:
  it reports `Up`, listens on nothing, and refuses every connection, which reads
  exactly like a firewall problem. `lab.sh up` encodes the order — use it rather
  than `docker compose up`.
- **Never flush `nat/OUTPUT` in the gateway.** Docker's embedded DNS
  (`127.0.0.11`) is implemented as DNAT rules in that chain inside the container
  namespace. Flushing it leaves every hostname unresolvable while TCP to a
  literal IP still works. That asymmetry — `https://1.1.1.1/` answers,
  `https://plex.tv/` does not — is the tell.
- **mitmproxy must not share PMS's uid.** The owner match that stops the proxy
  intercepting its own upstream connections is a uid exclusion, and the image
  default is 1000, the same as `PLEX_UID`. The obvious setup exempts PMS too and
  captures zero flows while looking correct. The gateway image creates uid 8888
  for that reason alone.
- **Blocking the other servers' hostnames is not enough.** plex.tv still
  *lists* them, so a signed-in client picks one as the active server and the
  session never reaches the lab at all — measured, 46 requests to a production
  server for every 8 to the lab. `isolate.py` filters the `/api/v2/resources`
  response itself so the client sees exactly one owned server. Without it the
  first baseline sent 50 flows at production.
- **The PMS container's CA trust does not survive a recreate.** It lives in the
  image filesystem, not a volume. Without it every plex.tv call fails TLS, the
  server signs itself out, and `/identity` drops to `claimed="0"` — which looks
  like a claim problem and is a certificate problem. `lab.sh up` re-applies it.
- **`hostname:` conflicts with `network_mode: service:`.** Removing it makes PMS
  name itself after the container id. Set `FriendlyName` through `/:/prefs`; it
  persists in `Preferences.xml`.
- **Chrome ignores `--remote-debugging-address`** and binds `127.0.0.1:9222`
  inside the container, which no port mapping can reach; hence `cdp-relay`.
- **`app.plex.tv` is a public origin and the server is a private IP.** Chrome
  blocks that pairing in a way that looks like a dead network and is *not* CORS.
  The browser launches with `LocalNetworkAccessChecks` and friends disabled.
- **Do not `chown -R` the capture directory.** The gateway runs as 8888 and
  everything else as 1000; a recursive chown takes the tree from every other
  writer and the next session dies on `PermissionError`.

- **A hunting agent must never write destructively to the reference.** Cycle 4's
  sent `DELETE /library/sections/1` and the Movies library had to be restored from
  Plex's backup, re-keying a movie. Forbid section and item deletes, merge/split/
  match/unmatch, maintenance endpoints and network/auth prefs in the prompt, ask for
  every write and its undo, and snapshot the lab's state before and after.

`harness/README.md` carries the same list with the measurements behind it.

### The disposable write reference: plexscratch

The lab is read-only for hunts. **Writes are measured on `plexscratch`**, a sanitised
copy of the lab's config on the lab host (`harness/scratch/`, deployed beside the lab),
answering at `http://LAB_IP:32410` (`SCRATCH_BIND=LAB_IP`; it publishes on loopback otherwise):

```bash
./scratch.sh make     # once: pause the lab for the copy, sanitise it into pristine/
./scratch.sh up       # start from config/ (created from pristine/)
./scratch.sh reset    # throw the writes away; never reads the lab again
./scratch.sh token    # the token clients send -- it CHANGES on every start
```

- **It must never reach plex.tv.** The copied config names the lab's machine and held its
  token; a second server announcing that identity would rewrite the lab's registration.
  `make` strips the token, account and certificate, skips Logs and Cache (the only other
  places they appear) and mints new machine identifiers, and PMS sits on an
  `internal: true` network with no route out. `status` proves the isolation each time.
- **An unclaimed PMS checks the source and the `Host`.** The peer and every
  `X-Forwarded-For` hop must be loopback or on `allowedNetworks` (private addresses while it
  is empty), and `Host` must be localhost or PMS's own address; `Host: LAB_IP:32410` is
  neither, so the relay's requests 401 (re-measured 2026-10-13, findings "The gate an
  unclaimed server keeps"). So a socat hop
  inside PMS's namespace makes every request loopback, and clients send the copy's own
  `.LocalAdminToken`. A session's client address therefore reads 127.0.0.1 there.
- **Do not put the hop on 32401.** PMS binds that port itself and exits at start with
  "Error binding acceptor: Address in use".

## Testing

The black-box suites start or target the server (`tests/server_command.py` runs
`rust/target/release/rex`, so build it first; `REX_SERVER` names another executable). They
need no network beyond zurg and Plex's agent. The `test_parity_*` suites assert what the oracle
answered, itself measured against a real PMS.

```bash
python3 tests/test_navigation.py            # client request replay, shape conformance
python3 tests/test_ui_surface.py            # context menu + every server page, end to end
python3 tests/test_all.py                   # play queue → decision → byte reads → real decode
python3 tests/conformance.py                # drives rex with python-plexapi
python3 tests/test_mediainfo.py             # the mediainfo adapter against a real PMS's analysis; no server
python3 tests/test_persist.py               # --state across SIGTERM, SIGKILL and a rebuilt catalog; starts its own
python3 tests/test_sqlite_store.py          # the state/catalog database: rows written per save, legacy import; starts its own
REX_OLD_SERVER=<an older release's rex> python3 tests/test_state_compat.py
                                            # one state file across releases, up and back down; skips without REX_OLD_SERVER
python3 tests/test_perf_part_read.py        # a byte read finds its Part without reading the library
python3 tests/test_perf_search_typo.py      # a misspelt search answers in milliseconds and holds no other request
python3 tests/test_perf_search_rank.py      # a search for "the" ranks thousands of hits keyed once a row (the web client's request)
python3 tests/test_perf_feed_listing.py     # a source's listing and a snapshot's end read no row from disk and hold no Plex request
python3 tests/test_manager.py               # rex manager end to end: create, start, crash restart, give-up, restore; starts its own
python3 tests/test_concurrency.py           # barrier races, 32-thread load, backlog, --state kills, reaper, slow sockets; ~4 min
python3 tests/test_plextv.py                # claim, announce, account import and restart against a LOCAL stub plex.tv
python3 tests/test_session_reaper.py        # Now Playing's 180 s + 30 s-tick reaper; ~3.5 min of wall clock
python3 tests/test_paused_limit.py          # MinutesAllowedPaused ends a paused session through its timeline; ~70 s
python3 tests/test_webhooks.py              # a claimed server posts the owner's plex.tv webhooks: play, pause, resume, scrobble, stop
python3 tests/test_cli.py                   # main()'s argparse: --help anywhere, refusals, --flag=value, prefixes, --web-root
                                            # default, a missing catalog seeded on start
python3 tests/test_server_log.py            # every route's log line and level, Download Logs, filters=log, SSE/websocket channels
python3 tests/test_parity_listings.py       # section listings, /library roots, Add Library, section prefs: the oracle's values
python3 tests/test_parity_metadata.py       # item detail, children, tags, top leaves, alternates: the oracle's values
python3 tests/test_parity_hubs.py           # hubs, search, voice search, autocomplete, matches: the oracle's values
python3 tests/test_parity_playback.py       # play queues, sessions, timeline, statistics (TZ=Europe/Berlin): the oracle's values
python3 tests/test_parity_server.py         # server-level routes, the plug-in dispatcher, resources, Live TV: the oracle's values
python3 tests/test_parity_lists.py          # playlists, collections, list artwork, empty answers: the oracle's values
python3 tests/test_parity_residue.py        # what a lockstep replay of the whole UI suite still found: the oracle's values
python3 tests/test_parity_vocabulary.py     # cycle 83's differential vocabulary sweep: the filter grammar, include*, paging
python3 tests/test_parity_sweep_vb.py       # the path-segment and request-header sweeps (unknown leaves, /web, idle close)
python3 tests/test_parity_verbs.py          # the measured verb table per route family
python3 tests/test_parity_leftovers.py      # the paging grammar, repeated keys, empty collections, clips, uploads
python3 tests/test_parity_residue_ra.py     # the UI suite's residue on both servers' own request streams
python3 tests/test_parity_persist.py        # every kind of state --state keeps, across a restart and a rebuild
python3 tests/test_parity_split_guid.py     # a split-off row; no Guid on an unmatched row
python3 tests/test_parity_inner_token.py    # a query-string token reaches the transcoder's own reads (Plex for Android)
python3 tests/test_parity_cycle83_*.py      # cycle 83's territory findings (xa, xb, xn, cp)
python3 tests/test_followups_f{a,b,c,d}.py  # parity follow-ups, each against its measurement
python3 tests/test_merge_unpaired.py        # a show merge moves the episodes the target has no partner for (1.43.4)
```

The native-only suites, all black-box unless noted:

```bash
python3 tests/test_native_runtime_smoke.py  # native routes and CLI refusals
python3 tests/test_native_gdm.py            # native GDM answers and the /servers self row
REX_NATIVE_BIN=rust/target/release/rex python3 tests/test_native_phase2.py
                                            # the core modules against the oracle's recorded answers, row by row
python3 tests/test_tls.py                   # HTTPS and plain HTTP on one port
python3 tests/test_rex_direct.py            # rex-direct against a LOCAL stub provider and plex.tv: the claim's token sent, registered
                                            # only once claimed (a runtime claim, no restart), its certificate by SNI beside
                                            # --tls-cert's, the announce, rex-direct.json kept and reused, a 401 runs on without,
                                            # a 202 asked again after 15 s (~15 s), a non-loopback http:// provider and off refused
python3 tests/test_prefix_registry.py       # 58 measured prefix shapes
python3 tests/test_regional_prefs.py        # the regional prefs documents, cell by cell
python3 tests/test_part_bytes.py            # part reads against a local zurg stub
python3 tests/test_native_transcode.py      # the transcoder against generated clips behind a local zurg; needs ffmpeg
                                            # (REX_TRANSCODER, else PATH's); with an Intel GPU REX_TRANSCODE_HW=vaapi and jellyfin-ffmpeg
python3 tests/test_download_queue.py        # offline downloads: Plex for Android's queue flow, each item converted to one mp4
                                            # and served by Range, as a claimed 1.42.2 PMS answered it; needs ffmpeg
python3 tests/test_feed.py                  # the feed API against a stub source: no gate, upsert, late ids, held
                                            # items, trash and return, snapshot reconcile and guards, migration
python3 tests/test_feed_versions.py         # two releases of one title: one row, a Media each (a real library's 127 Hours)
python3 tests/test_feed_season_art.py       # a season takes its identification once an episode has it (a real library's Black Mirror S6)
python3 tests/test_feed_blank_tiles.py      # what a real library drew the blank tile for gets named by its source's ids, a row with none is never retried; a person's Fix Match stays theirs
python3 tests/test_feed_source_decides.py   # the source decides: unplaced pushes unmatched in file order with no lookup, a placement exactly as pushed, a hand-set TMDB id trusted as pushed, held rows untouched by a start
python3 tests/test_feed_names.py            # releases pushed with no id stay for Fix Match, however cleanly they read: a.K.A., THEATRiCAL.CUT, a release in another language than its files
python3 tests/test_feed_placement.py        # a file its source placed (`kind`, TMDB season/episode) stays there through a start and the retry; Fix Match wins; a season-0 special is Plex's episode (Firefly's)
python3 tests/test_feed_delete.py           # a delete removes the files at the source first, as a real PMS's does (zurg's WebDAV DELETE)
python3 tests/test_feed_rejected.py         # every item the feed rejects is logged with its reason (a real snapshot's sample)
python3 tests/test_feed_extras.py           # a file pushed as `kind: extra` is a clip of its release's title by its `extra_type`, one without it is no clip whatever its folder; rows held so stay (Motto To Love-Ru, To Love Ru)
python3 tests/test_feed_versions_1017.py    # one row per episode whatever its guid, other files of one release apart, a trashed row folded or brought back (SpongeBob S10E64, Freakazoid)
python3 tests/test_zurg_contract.py        # zurg's own feed requests (its internal/rexfeed/testdata/rex-contract.json, from
                                            # ../zurg or $ZURG_CONTRACT) replayed in order, and the two 409s zurg tells apart
python3 tests/test_auto_collections.py      # "Minimum automatic collection size" set later makes the agent's collections (a real library's Matrix trilogy)
python3 tests/test_sources.py               # registering sources, and sources that are not zurg: a server ignoring Range,
                                            # a redirect to a CDN, a restart, rows before registration, the two-step delete
python3 tests/test_server_command.py        # the launcher's default and REX_SERVER override
python3 tests/test_unclaimed_gate.py        # what an unclaimed server answers: no client, its claim from local networks only, Host names, allowedNetworks
python3 tests/test_photo_ssrf.py            # /photo/:/transcode?url= dials nothing on rex's own networks (loopback, LAN, 0.0.0.0)
python3 tests/test_track_prefs.py           # a real Part starts on its account's plex.tv audio/subtitle languages; a hand pick stays that account's
python3 tests/test_accounts.py              # plexlab's owner and shared user replayed: per-account watch state, ratings, playlists,
                                            # history and sessions, a share's libraries, filters and flags, the owner's routes
python3 tests/parallel_suites.py --workers 4 test_cli test_feed ...
                                            # several suites at once, as CI runs them: each gets its own
                                            # REX_TEST_PORTS range (tests/test_ports.py), output kept per suite
cargo test --manifest-path rust/Cargo.toml  # the Rust unit tests: the decision corpus (1,872 measured decisions),
                                            # GDM against four real servers
```

Credentials unlock the rest. With a copy of `catalog_ui.json` whose Part
files a zurg serves (made on the test host, never committed) and that zurg registered with
the test server (`PUT /rex/sources/zurg` with its url, login and `mount`), the UI suite skips
only its two Fix Match checks (1,979/1,979 with none skipped when it was RD-backed and read
`PLEX_TOKEN`): they need Plex's agent, which only a real claim gives, and `--assert-claimed`
is none (2026-10-19).
Zurg-less it is 1,975/1,975 with exactly three skips (two Fix Match, Download):
with no source registered a part read is the routed 502 its checks expect.
`test_all.py` and `conformance.py` need a live catalog and the same credentials.
A provider account behind zurg is read-only in tests: read bytes, never add or delete.

Where to run them: service and end-to-end tests run on a test host, never on a production
one, with the native binary built in Docker where there is no Rust toolchain --
`docker run --rm --platform linux/amd64 -v "$PWD":/src -v <target dir outside the repo>:/target -e CARGO_TARGET_DIR=/target -w /src/rust rust:1-bullseye cargo build --release`
-- and copied to `rust/target/release/rex` in the test host's copy. Keep every test port
**below 32768** (`tests/test_ports.py` reads a `REX_TEST_PORTS=low-high` range): Linux's ephemeral range starts there by default, so a port inside it is taken now and
then by an outgoing connection and the server fails to bind (the cause of the old
"flaky" `--web-root` sub-server crash). The UI suite binds three sub-servers of its own
on fixed ports (`gated_port`, `web_port_c16y`, `port_c29l`, 32597-32599); two suites at
once need those remapped in the test host's copy. Point `REX_PLEXTV_URL` at a
closed local port for any run that must not reach plex.tv. `tests/server_command.py` sets
`REX_DIRECT_URL=off` unless a suite names a provider, so no claimed test server registers with DMM.

`test_ui_surface.py` **writes**: it edits rows, builds and deletes playlists and
collections, and finally deletes a row and a section. Everything it touches is in
memory, so restarting the server is the undo button — provided it was started
with `--no-state` — but run it against a test instance, started on the fixture the
checks are written against:

```bash
rust/target/release/rex --port 32500 --catalog tests/catalog_ui.json --assert-claimed --no-state &
python3 tests/test_ui_surface.py --base http://127.0.0.1:32500
```

That fixture exists because multiplicity is a prerequisite, not a detail: on a
one-movie section the Shuffle, Play Next, Merge and second-page checks have
nothing to compare and quietly become no-ops. `probes/ui_surface.py` is the other
half — replay the same surface against a real PMS and diff the two tables.

`test_navigation.py` is the regression suite. Every fixed rendering bug gets a
check here, and the check must **fail against the unfixed server** — prove it,
by running the suite against the old code on another port, before claiming a
fix. A key-level comparison is not enough on its own: the `viewMode` bug was
invisible to one, because both sides had the field and only the values were
swapped.

`test_all.py` decodes real frames rather than trusting a 200, because a 200
that returns an error page still has a status a naive check accepts.

New fixtures come from the harness, never from hand. `tests/fixtures/` holds
corpora recorded from a real PMS *through a real client*, so they carry the
client's own paging params; a payload transcribed by hand cannot, and that is
precisely how the Seasons bug survived a fix attempt.

## CI and releases

`.github/workflows/ci.yml` runs on every push to `main` and every pull request: a release
build on GitHub's `ubuntu-22.04` (glibc 2.35, so the binary runs on 22.04 and newer), `cargo test`, and the
black-box suites that need no credentials. A tag or a manual run (Actions, "Run workflow")
also builds linux arm64 (cross-compiled, smoke-tested under qemu) and macOS arm64 and amd64
on `macos-14` (the arm64 one runs suites natively), and Windows amd64, cross-compiled with
MinGW on Linux and then tested on `windows-2022` (the unit tests and every suite but
`test_manager`, since `rex manager` is Unix only). Those are the platforms zurg's own
releases are downloaded for. It never runs on the org's self-hosted runner, which is a
production host.

A nightly goes out beside zurg's: `/home/ben/zurg-nightly-dispatch.sh` on dmm dispatches
CI with `nightly=true` at 00:40 Berlin (a `19 1 * * *` UTC cron is the fallback), and it
publishes every platform as the prerelease `YYYY.MM.DD.HHMM-nightly`, notes being the commits
since the previous release, plus the `nightly` image, and posts it to Discord's #general
through the webhook zurg's nightlies use (`DISCORD_RELEASE_WEBHOOK`, `.github/announce_nightly.py`;
`--dry-run` prints the post); `latest` stays on the last tag. The
`gate` job skips a night whose `main` the last nightly already carries.

A release is a tag. Set `version` in `rust/Cargo.toml`, push that to `main`, wait for CI,
then tag the tested commit with the release notes as the tag message and push the tag:

```bash
git tag -a v0.2.0 -F notes.md <tested commit> && git push origin v0.2.0
```

The tag's run tests again, checks the tag matches `Cargo.toml`, and publishes
`rex-vX.Y.Z-{linux,darwin}-{amd64,arm64}.tar.gz`, `rex-vX.Y.Z-windows-amd64.zip` and `SHA256SUMS` as the GitHub release, titled `rex X.Y.Z`
with the tag message as its notes. Its `docker` job then calls `.github/workflows/docker.yml`,
which packages that release's Linux archives into `ghcr.io/debridmediamanager/rex`
(`latest`, `X.Y.Z`, `X.Y`; private, like the repository) without compiling; run it by hand
with a tag to give an older release an image. The archive keeps the repository's layout (the binary at
`rust/target/release/rex`, the sidecars in `data/`), so every command in the
docs works the same from a release as from a checkout.

## Protocol reference

The measured endpoint, query, projection, error, and client-rendering edge cases
are in [`docs/protocol-traps.md`](docs/protocol-traps.md). Search that file for
the path or field before changing behavior. [`docs/findings.md`](docs/findings.md)
holds the underlying captures and investigations. When the two appear to differ,
replay the official client's exact request against the reference PMS and record
the result before changing the emulation.

The witnesses those documents and the code comments name: **plexlab**, the reference lab PMS
(1.43.4, ten synthetic clips); **the 1.42.2 reference**, a real claimed PMS on 1.42.2.10156 --
the build rex reports -- with a large matched library (3,887 movies, two show libraries),
read-only; **the production server**, a real 1.42.2 library that was the early cycles' second
witness and later ran a production rex; **plexscratch** and **plexscratch42**, the disposable
write copies; **the lab host** and **the test host**, the machines plexlab and rex's own test
rigs run on.

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- rex keeps its existing crate set. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a provider download link, zurg's credentials, a Plex token, or
  a tunnel credential. The repo is public.
- Setup instructions live in five places that must say the same thing: `README.md`,
  `docs/setup.md`, the notes guides `guides/rex.md` and `guides/rex-any-source.md`
  (repo `debridmediamanager/notes`, notes.debridmediamanager.com) and the Setup and
  Limits sections of `public/index.html` in `debridmediamanager/rex-site`
  (rex.debridmediamanager.com). A change to any step, flag, env var, version floor or
  limit lands in all five in the same task, and a code change that alters setup is not
  done until they do.
- Those five describe the simplest setup there is, and in the same order: rex starts **once**,
  with every flag a later step needs already on that first start (`--announce-url` with the
  LAN address, an own hostname's `--tls-cert`/`--tls-key`). No step says "add this flag and
  restart" for something that could have been on the first start: the claim starts the
  announce and rex-direct, which is on by default and needs only the port forward, neither a
  restart. Say each thing once per page and link back to it instead of repeating it (Ben,
  2026-10-19).
- One name for one thing. rex's address is **rex's LAN IP** in every setup command: the
  announce, the claim `curl`, zurg's `rex_url` and the checks (`<rex's LAN IP>` in the README,
  setup.md and the site; `192.168.1.20`, said once, in the notes guides). Never mix in
  `127.0.0.1`, `<rex host>` or `<rex's address>`: an unclaimed rex answers its LAN IP from its
  own machine as it does loopback (checked 2026-10-19), so one address serves every step.
