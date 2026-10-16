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
| `probes/` | Measurement scripts: the recorders behind checked-in fixtures (`record_identify_corpus.py`, `record_episode_id_corpus.py`, `record_unidentified_corpus.py`, which also tops a corpus up, `legacy_scan_fixture.py`, `record_session_gate.py`), `ui_surface.py`, the plex.tv presence probes and `apkcensus.py`. The corpus recorders run the Rust identifier through `identify_replay.py` (the binary's `identify-replay` adapter) |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |
| `docs/protocol-traps.md`, `docs/setup.md`, `docs/feed-api.md`, `docs/metadata-api.md` | The protocol's edge cases, the operator's guide, the API sources push with, and the API rex reads titles through from a metadata provider (`REX_METADATA_URL`; DMM is one) |

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
`data/`: `tmdb_cache.json` and `claim_state.json` are resolved beside the **catalog**,
because they are state rather than measurement.
The state file is state too: a SQLite database beside the catalog by default
(`rex.db`), the catalog itself when the catalog is a database, or wherever `--state`
names.

The native server finds the same sidecars in `data/` beside the catalog, then one directory
up, then in its working directory, trying each directory itself after its `data/` so a
deployment that keeps them beside its catalog still works (`sidecar_roots` in `runtime.rs`).
Run it from the repository root or keep the catalog there -- `run.sh`, copied to the root from
`examples/run.sh.example`, `cd`s to its own directory.
`client_profiles.json`, `languages.json` and the decision corpus are compiled into the
binary with `include_str!`, so a change to them needs a rebuild. `tmdb_cache.json` and
`claim_state.json` sit beside the catalog.

Where things live in `rust/src/`:

| Path | What it is |
|---|---|
| `main.rs` | The CLI: the server flags (argparse's, refusals included), the `catalog` and `manager` subcommands, and the test-only adapters the suites drive (`test_native_phase2.py`, `test_mediainfo.py`, the corpus recorders) |
| `catalog_tool.rs` | `rex catalog`: `--seed` an empty library at each source's mount (`--mount`, `--movies` and `--shows` repeat; a re-seed keeps the machineIdentifier), `--convert` a JSON catalog into a database, `--relink` Plex's guids |
| `manager.rs` | `rex manager`: the control service that runs no server until one is started, then supervises the ones it started (API on 127.0.0.1:32300, CLI, a page); Linux and macOS only; docs/setup.md "Running several servers" |
| `runtime.rs` | The request loop, the route table and most projections; `serve` starts GDM, the state saver and the plex.tv threads |
| `runtime/wire.rs` | The response envelope: header order, Range/416, CORS, gzip, websocket handshake |
| `runtime/upstream.rs` | The HTTP/HTTPS client (rustls, system roots) behind zurg, provider redirects, TMDB, Plex's metadata agent and artwork |
| `runtime/source.rs`, `runtime/feed.rs`, `runtime/scan.rs` | Sources (registered over `PUT /rex/sources/{name}`, kept in the state file, deleted with a confirmation: where a Part's bytes are), the feed API sources push items to, open to anyone who can reach rex (`docs/feed-api.md`), and the rows those items become: added, held until probed, filled, re-identified, trashed, keys minted once (`mint_key`) |
| `runtime/universal.rs` | The video transcoder (`REX_TRANSCODER`): the conversion decision's plan and Media, `start.m3u8`/`start.mpd`, segments served outside the runtime lock, one FFmpeg per session with the hardware ladder, seeks, throttling and the idle reaper, and the `TranscodeSession` rows |
| `runtime/plexmeta.rs`, `identify.rs` | Plex's metadata agent (the claim's server token, else `PLEX_TOKEN`), and which title a release is: its source's placement (`Identifier::placed`), else an id in its name, a known IMDb id for its hash (zurg's), then Plex's matcher and TMDB, with their agreement as the confidence; it must name as many of `tests/fixtures/identify/` right (`rust/tests/identify_corpus.rs`) |
| `runtime/plextv.rs`, `runtime/tmdb.rs` | claim/heartbeat/account state, and TMDB's reads for Fix Match and identification, through a metadata provider first when one is set (`REX_PLEXTV_URL` points plex.tv at a stub) |
| `platform.rs` | Everything asked of the OS beyond `std`: local time, CPU time and load, the host name, thread ids, pausing and killing FFmpeg, the GDM socket. A Unix half and a Windows half; nothing else may call `libc` for these |
| `runtime/*.rs` (the rest) | One subsystem each, named after it: search, listing, axes, edit, collections, playlists, artwork, butler, framework, i18n, resume (On Deck and hubs), logging and so on |
| `filters.rs`, `hierarchy.rs`, `metadata.rs`, `mediainfo.rs`, `decision.rs`, `xmlout.rs`, `gate.rs`, `catalog.rs` | Plex's filter grammar (the operator is part of the KEY), flat leaves to show/season/episode, TMDB and `plex://` guids, ffprobe JSON to `Media`/`Part`/`Stream`, a plain-form decision against the client's own profile, Plex XML, the direct-play gate, the catalog |
| `store.rs` | The state file: a SQLite database written by the row, holding the catalog too when the catalog is a database |

A movie library and a show library have **different**
`Meta` descriptors, which is why there are two: retargeting one onto the other
gives the client a grid whose filter bar can only talk about the wrong type.

## Running it

```bash
cargo build --release --manifest-path rust/Cargo.toml
rust/target/release/rex --catalog rex.db --friendly-name NAME --zurg-mount /mnt/zurg   # seeds rex.db on its first start
cp examples/run.sh.example run.sh   # keys go in rex.env beside it (TMDB_KEY; PLEX_TOKEN optional; mode 600)
```

Sources push the library; rex reads no media itself, lists and queries nothing, and
has no provider code of its own (`docs/feed-api.md`). A `--catalog` that does not exist is
seeded on start as `rex catalog --seed` seeds one (`--friendly-name`, default `rex`; its
libraries under `--zurg-mount`); `rex catalog --seed` stays for naming more directories or
re-seeding. A source registers itself -- `PUT /rex/sources/{name}`: its byte url, `user`/
`password` or `bearer`, `mount` -- and rex keeps the registration in the state file; it then
pushes to `/rex/feed/{name}`. Both are open to anyone who can reach rex, claimed or not:
no token (a feed token per source, set by hand on both sides, was the setup's longest
step) and no network gate (one refused zurgs naming rex by host name or over Tailscale). Registering is the only way a source
exists (`ZURG_URL` is not read): a row keeps its `_source`/`_path`, and a catalog row from
the old WebDAV scanner reads through the registered source whose `mount` holds its file,
so either plays again once its source registers. `DELETE /rex/sources/{name}` answers a
one-use code valid 300 s; `DELETE .../confirm/<code>` removes the source and what it pushed
from rex, never its files. With no source a part read is the 502 an unreachable source is. `TMDB_KEY` and
Plex's matcher identify what is pushed (Fix Match needs TMDB); the matcher is asked with the
claim's server token, or `PLEX_TOKEN` on an unclaimed server, and is off with neither. `--zurg-mount` is zurg's
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
with a `kind` and ids is placed where zurg says (TMDB's season and episode), not by its name,
and no pass at start renumbers it (`_placed` on the row); the name places only what arrives without. An item
with no `media_info` is held, not shown, until zurg probes it; a late IMDb id re-identifies
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

`--announce-url` announces with the claim's server token, or `PLEX_TOKEN` while unclaimed; with
neither it waits for the claim and announces the moment it lands, no restart (findings, "A
claimed server needs no account token"). A comma-separated list announces each URI as its own
connection, as a PMS with several does. The feed is rex's own API, not PMS emulation.

A real claim -- `POST /myplex/claim?token=` with a token from `plex.tv/api/claim/token.json`,
once per machineIdentifier -- makes rex register the way a real PMS does (findings, "The
experiment, run"): `POST /servers.xml` writes plex.tv's server record (422 is its normal
answer), and the pubsub stream it then holds open is what clients read as `presence`. The
claim is kept in `claim_state.json`, so a restart re-registers without claiming again. A
claimed server gates every request as a real PMS does: its own token, the tokens
`plex.tv/api/v2/server/access_tokens` lists for it (re-read for an unknown token, and kept in
`claim_state.json` so a failed read after a restart locks no one out), and the `transient-`
tokens its own `/security/token` minted (findings, "The token gate a claimed server keeps").
A conversion's own playlist and segments (`/video/:/transcode/universal/session/...`) are
keyed to the session, not the token, as on a real PMS (findings, "A conversion session is its own key").
An unclaimed server without `--token` answers only its own networks, as an unclaimed PMS
does (`local_request`; findings, "The gate an unclaimed server keeps").
With `PLEX_TOKEN` set, start it without `--announce-url` for the claim and add it afterwards:
the unclaimed heartbeat writes a device row with the account token first, and the 2026-09-11
spike's claim of an identity that already had such a row got the account token back, not a
server token. Without `PLEX_TOKEN` nothing is announced before the claim, so `--announce-url`
can stay on. rex refuses to store a token equal to `PLEX_TOKEN`, and a claimed server answers
another claim with 409.

`rex.db`, `rex.env` and `run.sh` are gitignored: the catalog and `rex.env` carry
account-scoped credentials. `examples/catalog.example.json` keeps the shape.

Under launchd, so it outlives the shell that starts it:

```bash
launchctl submit -l com.rex.dev -o rex.log -e rex.err -- /bin/zsh "$PWD/run.sh"
launchctl remove com.rex.dev
```

A plain `nohup ... &` from an agent tool call **does not survive** the call.

Several servers, started and stopped on demand: `rex manager serve`, then
`rex manager create NAME --port P [--catalog PATH] [--zurg-mount M] [--friendly-name N]` (keys from
`TMDB_KEY`/`PLEX_TOKEN`/`REX_TOKEN` in the environment; sources register with each server) and `start|stop|list|logs NAME`; a server with no catalog seeds one on its first start
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
Held items (`feedHeld`) are kept with it.

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
need no network beyond zurg and TMDB. The `test_parity_*` suites assert what the oracle
answered, itself measured against a real PMS.

```bash
python3 tests/test_navigation.py            # client request replay, shape conformance
python3 tests/test_ui_surface.py            # context menu + every server page, end to end
python3 tests/test_all.py                   # play queue → decision → byte reads → real decode
python3 tests/conformance.py                # drives rex with python-plexapi
python3 tests/test_mediainfo.py             # the mediainfo adapter against a real PMS's analysis; no server
python3 tests/test_persist.py               # --state across SIGTERM, SIGKILL and a rebuilt catalog; starts its own
python3 tests/test_sqlite_store.py          # the state/catalog database: rows written per save, legacy import; starts its own
python3 tests/test_manager.py               # rex manager end to end: create, start, crash restart, give-up, restore; starts its own
python3 tests/test_concurrency.py           # barrier races, 32-thread load, backlog, --state kills, reaper, slow sockets; ~4 min
python3 tests/test_plextv.py                # claim, announce, account import and restart against a LOCAL stub plex.tv
python3 tests/test_session_reaper.py        # Now Playing's 180 s + 30 s-tick reaper; ~3.5 min of wall clock
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
python3 tests/test_prefix_registry.py       # 58 measured prefix shapes
python3 tests/test_regional_prefs.py        # the regional prefs documents, cell by cell
python3 tests/test_part_bytes.py            # part reads against a local zurg stub
python3 tests/test_native_transcode.py      # the transcoder against generated clips behind a local zurg; needs ffmpeg
                                            # (REX_TRANSCODER, else PATH's); with an Intel GPU REX_TRANSCODE_HW=vaapi and jellyfin-ffmpeg
python3 tests/test_feed.py                  # the feed API against a stub source: no gate, upsert, late ids, held
                                            # items, trash and return, snapshot reconcile and guards, migration
python3 tests/test_feed_versions.py         # two releases of one title: one row, a Media each (a real library's 127 Hours)
python3 tests/test_feed_season_art.py       # a season takes its identification once an episode has it (a real library's Black Mirror S6)
python3 tests/test_feed_blank_tiles.py      # what a real library drew the blank tile for gets named; a person's Fix Match stays theirs
python3 tests/test_feed_blank_1016.py       # archive members stay versions of their episode, `One Piece 1106` is One Piece, a film zurg files under anime goes to Movies
python3 tests/test_feed_names.py            # releases named in a way identification read wrong: a.K.A., THEATRiCAL.CUT, a release in another language than its files
python3 tests/test_feed_pack_folders.py     # a pack's season folders, which zurg's mount flattens: SNL's and Smallville's 01s each their own season's
python3 tests/test_feed_unfold.py           # each file of a season batch its own episode; rows that folded several episodes unfolded at start
python3 tests/test_feed_placement.py        # a file its source placed (`kind`, TMDB season/episode) stays there through every start pass and retry; Fix Match wins; a season-0 special is TMDB's episode (Firefly's); the retired REX_ANIME_LISTS/REX_IMDB_EPISODES do nothing
python3 tests/test_feed_delete.py           # a delete removes the files at the source first, as a real PMS's does (zurg's WebDAV DELETE)
python3 tests/test_sources.py               # registering sources, and sources that are not zurg: a server ignoring Range,
                                            # a redirect to a CDN, a restart, rows before registration, the two-step delete
python3 tests/test_server_command.py        # the launcher's default and REX_SERVER override
python3 tests/test_unclaimed_gate.py        # what an unclaimed server answers: local networks, Host names, allowedNetworks
python3 tests/test_track_prefs.py           # a real Part starts on the owner's plex.tv audio/subtitle languages; a hand pick stays
python3 tests/parallel_suites.py --workers 4 test_cli test_feed ...
                                            # several suites at once, as CI runs them: each gets its own
                                            # REX_TEST_PORTS range (tests/test_ports.py), output kept per suite
cargo test --manifest-path rust/Cargo.toml  # the Rust unit tests: the decision corpus (1,872 measured decisions),
                                            # GDM against four real servers, the identify corpora
```

Credentials unlock the rest. With `TMDB_KEY`, and a copy of `catalog_ui.json` whose Part
files a zurg serves (made on the test host, never committed) and that zurg registered with
the test server (`PUT /rex/sources/zurg` with its url, login and `mount`), the UI suite runs
with **no skips** (1,979/1,979 when it was RD-backed).
Keyless and zurg-less it is 1,975/1,975 with exactly three skips (two Fix Match, Download):
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
closed local port for any run that must not reach plex.tv.

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
