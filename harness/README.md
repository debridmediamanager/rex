# plexlab — the reference harness

A real Plex Media Server, the official web client, and a full recording of
everything that passes between them and plex.tv. It exists to answer one
question: *what does a real PMS actually say here?*

`CLAUDE.md` states the rule this serves — every emulated response is a
measurement, not a guess. This is the instrument that takes the measurement.
Fixtures come from the implementation being modelled, never from ours.

Runs on **kids** at `/home/ben/plexlab`.

## What it records

Two axes, deliberately separate, both written as one JSON object per exchange
(`scripts/tap.py`) with method, path, every query param in order, every header,
and both bodies.

| Axis | File | What it holds |
|---|---|---|
| client → server | `capture/sessions/<label>.jsonl` | what the official client asks for, and what a real PMS answers. The corpus plexsim must match. |
| server → plex.tv | `capture/outbound.jsonl` | how a real PMS registers and re-announces itself — the traffic that makes a server *detected* rather than merely reachable. |

Media bodies are recorded by length only. A 1.8 GB part read is a fact about
status and headers, not something to keep on disk.

## Layout

| Path | What it is |
|---|---|
| `compose.yml` | the whole stack: PMS, egress gateway, client proxy, browser |
| `lab.sh` | start/stop in the order the shared namespaces require |
| `scripts/tap.py` | mitmproxy addon; writes the JSONL corpus |
| `scripts/isolate.py` | keeps the harness off every other server on the account |
| `scripts/cdp.py` | minimal CDP driver for the containerised browser |
| `scripts/session-web.py` | drives the official client through a real browse walk |
| `scripts/replay.py` | replays a corpus at a server and diffs the answers |
| `scripts/make-media.sh` | builds the reference library |

## Setup from scratch

```bash
./scripts/make-media.sh            # synthetic clips named as real titles
cp .env.example .env               # fill in after the first claim
./lab.sh up
```

Then create the libraries and claim the server:

```bash
P=http://127.0.0.1:32400
curl -s -X POST "$P/library/sections?name=Movies&type=movie\
&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US&location=%2Fdata%2Fmovies"
curl -s -X POST "$P/library/sections?name=TV%20Shows&type=show\
&agent=tv.plex.agents.series&scanner=Plex%20TV%20Series&language=en-US&location=%2Fdata%2Fshows"

CLAIM=$(curl -s https://plex.tv/api/claim/token.json -H "Accept: application/json" \
        -H "X-Plex-Token: $ACCOUNT_TOKEN" -H "X-Plex-Client-Identifier: plexlab-probe-1" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -X POST "$P/myplex/claim?token=$CLAIM"
```

The library is ten ten-second clips named as real titles — four Blender open
movies and two Firefly seasons. Plex matches on the filename, so its real
metadata pipeline engages: the reference server holds genuine
`plex://movie|show|season|episode/<24hex>` guids and real TMDB episode titles,
with no real media on disk.

## Capturing and diffing

```bash
.venv/bin/python scripts/session-web.py --label reference-walk \
    --token "$ACCOUNT_TOKEN" --server-token "$SERVER_TOKEN" --machine "$MACHINE_ID"

.venv/bin/python scripts/replay.py --corpus capture/sessions/reference-walk.jsonl \
    --target http://127.0.0.1:32500 --target-token "$PLEXSIM_TOKEN" \
    --reference http://127.0.0.1:32400 --reference-token "$SERVER_TOKEN" \
    --out capture/diffs/plexsim.md
```

`replay.py` reissues each request byte-for-byte — same path, same query params
in the same order, same `X-Plex-*` headers. It compares element structure,
attribute presence **and attribute values**: a key-level comparison alone would
have missed the `viewMode` bug, where both sides carried the field and only the
values were swapped. Fields that differ per server by construction are listed
explicitly in `VOLATILE` rather than guessed at, and a set of fields whose value
has been load-bearing in a past rendering bug is flagged in the report.

Run it against the reference server itself to sanity-check the tool: 23 of 27
requests came back clean, and all four differences were genuine state drift
between capture and replay (a rename, geoip resolving, the Recently Added hub
settling) — not false positives.

## What the corpus already contains

The reference walk records the client's own paging parameters, including the one
that matters most:

```
GET /library/metadata/6/children -> 200  [X-Plex-Container-Start=0 X-Plex-Container-Size=20 excludeAllLeaves=1]
GET /library/metadata/6/children -> 200  [excludeAllLeaves=1]
GET /library/sections/1/all      -> 200  [X-Plex-Container-Start=0 X-Plex-Container-Size=50]
```

`docs/findings.md` records that substituting a real server's `/children`
payload wholesale did not fix the missing Seasons block, because the recording
had been taken without the paging params the client actually sends. This corpus
is taken *through* the client, so it cannot have that defect.

## Traps that cost real time here

- **Start order is load-bearing.** `plex` shares `gw`'s network namespace and
  `cdp-relay` shares `browser`'s. A sharer that starts before its host, or
  survives its host being recreated, lands in a namespace that no longer exists.
  It reports `Up`, listens on nothing, and refuses every connection — which
  reads exactly like a firewall problem. `lab.sh up` encodes the order.

- **Never flush `nat/OUTPUT` in the gateway.** Docker's embedded DNS
  (`127.0.0.11`) is implemented as DNAT rules in that chain inside the container
  namespace. Flushing it leaves every hostname unresolvable while TCP to a
  literal IP still works — `curl https://1.1.1.1/` returns 301 and
  `curl https://plex.tv/` returns nothing. That asymmetry is the tell.

- **mitmproxy must not share PMS's uid.** The owner match that stops the proxy
  intercepting its own upstream connections is a uid exclusion. The image's
  default user is 1000 and `PLEX_UID` is 1000, so the obvious setup exempts PMS
  as well and captures zero flows while looking correctly configured. The
  gateway image creates uid 8888 for this reason alone.

- **Chrome ignores `--remote-debugging-address`.** It binds `127.0.0.1:9222`
  inside the container, which no port mapping can reach; hence `cdp-relay`.

- **`app.plex.tv` is a public origin and the server is a private IP.** Chrome
  blocks that pairing in a way that looks like a dead network and is *not* CORS.
  The browser launches with `LocalNetworkAccessChecks` and friends disabled.

- **Blocking the other servers' hostnames is not enough.** plex.tv still *lists*
  them, so the client selects one as the active server and the session never
  reaches the lab at all. Measured: a hostname-only block drew fun's sidebar with
  unreachable warnings and issued 46 requests to fun for every 8 to the lab. So
  `isolate.py` filters the resource list itself, and the client then sees exactly
  one owned server. Without it the first baseline sent 50 flows to production.

- **The PMS container's CA trust does not survive a recreate.** It lives in the
  image filesystem, not a volume. Without it every plex.tv call fails TLS, the
  server signs itself out, and `/identity` drops to `claimed="0"` — which looks
  like a claim problem and is a certificate problem. `lab.sh up` re-applies it.

- **`hostname:` conflicts with `network_mode: service:`.** Removing it makes PMS
  name itself after the container id. Set `FriendlyName` through `/:/prefs`
  instead; it persists in `Preferences.xml`.

- **mitmproxy runs with `--ssl-insecure`.** The lab server's Let's Encrypt cert
  chains through the `YR2` intermediate, whose root the image's CA bundle
  predates; `fun`'s chains through `YR1` and verified fine. Upstream verification
  is not what this harness measures, but the asymmetry is worth knowing before
  concluding a server is misconfigured.

- **Do not `chown -R` the capture directory.** The gateway runs as 8888 and
  everything else as 1000; a recursive chown takes the tree away from every other
  writer and the next session dies on `PermissionError`.

## plexscratch -- the disposable write reference

`scratch/` builds a second PMS from a sanitised copy of `config/`, for measuring writes
the shared lab must never receive. It is deployed beside this directory on the lab host
(`../plexscratch`) and answers on the Tailscale address, port 32410.

| Measured while building it | Consequence |
|---|---|
| The lab's database writes its WAL continuously | `make` pauses `plexlab-plex` for the seconds the copy takes, then checkpoints and integrity-checks the copy |
| The plex.tv token and certificate id appear in `Preferences.xml` and `Logs/` only; `Cache/` holds encrypted account state and the plex.direct certificate | the copy skips Logs, Cache, `.LocalAdminToken` and `Setup Plex.html`, strips the token, account and certificate, and gets new machine identifiers |
| An unclaimed PMS 401s an allowed network, and 401s loopback whose `Host` is not 127.0.0.1/localhost | a socat hop in PMS's namespace makes requests loopback, and clients send the copy's own `.LocalAdminToken` (`./scratch.sh token`, new on every start) |
| PMS binds 32401 itself | the hop listens on 32499; on 32401 PMS exits with "Address in use" |
| An `internal: true` network has no route and no DNS | TCP to 1.1.1.1 is "Network is unreachable"; `./scratch.sh status` re-proves it |

`./scratch.sh reset` discards every write by restoring `pristine/`; it never reads the lab.

## On plex.tv

The harness claims through the supported flow only:
`GET /api/claim/token.json` then `POST /myplex/claim?token=…` against the
server's own API. Account state was snapshotted before and after
(`capture/resources-before.json`, `-after.json`): `plexlab` was added, nothing
was renamed or removed, and `fun` and `zen` were untouched.

One measurement corrects `docs/findings.md`. A real PMS announces itself with
`PUT /devices/{machineIdentifier}` — the *identifier* form — carrying its full
`<Device>` element and its `plex.direct` connection. The numeric-id form that
silently overwrote a dormant device is not what a real server uses. The
announce repeats on a timer; that repeat is what keeps a server listed.

`POST /servers.xml`, which publishes the section list, answers **422 with an
empty body** on 1.43.4. Recorded rather than explained — it has not been
compared against a second real server, and doing so would mean intercepting one
that is in production.
