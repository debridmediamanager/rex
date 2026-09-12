# plexsim

A Plex Media Server that owns no files. It answers the PMS API well enough that
the **official** Plex clients treat it as a real server, and streams a catalog
backed by Real-Debrid links instead of local media.

Not a Plex fork and not a client. It is the server side of the protocol,
written from measurement against a real PMS.

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
**read the client**. plexsim serves the web client bundle, so its chunks can be
searched on disk. The gate is usually one truthiness check in minified code.

## Layout

| Path | What it is |
|---|---|
| `server.py` | The PMS surface. Every route, ~4400 lines. Start here. |
| `library.py` | The mutable half: watch state, edits, playlists, collections, history |
| `filters.py` | Plex's filter query language: the operator is part of the KEY |
| `sessions.py` | Live playback sessions, and the byte counters behind the Dashboard |
| `hierarchy.py` | Flat leaves → show/season/episode with synthesised parents |
| `metadata.py` | TMDB identification, `plex://` guid minting, artwork URLs |
| `mediainfo.py` | ffprobe JSON → `Media`/`Part`/`Stream` elements |
| `debrid.py` | Real-Debrid unrestrict |
| `gate.py` | Applies a client's direct-play gate to the catalog |
| `xmlout.py` | MediaContainer → Plex XML (clients that refuse JSON) |
| `build_catalog.py` | Builds `catalog.json` from real RD content |
| `claim.py`, `heartbeat.py` | plex.tv claim flow and registration upkeep |
| `tests/` | Conformance and end-to-end suites, plus `catalog_ui.json` |
| `tests/fixtures/` | Redacted corpora recorded from a real PMS through a real client |
| `harness/` | plexlab — the reference PMS and the recorder. See below. |
| `probes/` | One-off measurement scripts that produced findings |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |

Sidecar fixtures (`server_prefs.json`, `section_prefs.json`,
`section_meta.json`, `section_meta_show.json`, `item_prefs.json`) are captured
from a real PMS and
loaded relative to `server.py`. Keep the layout flat — the code resolves them
with `dirname(__file__)`. A movie library and a show library have **different**
`Meta` descriptors, which is why there are two: retargeting one onto the other
gives the client a grid whose filter bar can only talk about the wrong type.

## Running it

```bash
cp run.sh.example run.sh     # fill in RD_TOKEN, then ./run.sh
cp catalog.example.json catalog.json
```

`catalog.json` and `run.sh` are gitignored: both carry account-scoped
credentials. `catalog.example.json` keeps the shape with the links redacted.

Under launchd, so it outlives the shell that starts it:

```bash
launchctl submit -l com.plexsim.dev -o plexsim.log -e plexsim.err -- /bin/zsh "$PWD/run.sh"
launchctl remove com.plexsim.dev
```

A plain `nohup ... &` from an agent tool call **does not survive** the call.

## The reference harness

`harness/` is **plexlab**: a real Plex Media Server, the official web client,
and a recording of everything that passes between them and plex.tv. It is the
instrument for the rule above — it is how you find out what a real PMS answers
for the client's exact request, instead of guessing.

It runs on **kids** at `/home/ben/plexlab`. The repo holds the source; that
directory holds the live instance, its captures and its `.venv`.

Two capture axes, deliberately separate. Both write one JSON object per
exchange with method, path, **every query param in order**, every header and
both bodies. Media bodies are recorded by length only — a 1.8 GB part read is a
fact about status and headers, not something to keep on disk.

| Axis | File | What it holds |
|---|---|---|
| client → server | `capture/sessions/<label>.jsonl` | what the official client asks for and what a real PMS answers. The corpus plexsim must match. |
| server → plex.tv | `capture/outbound.jsonl` | how a real PMS registers and re-announces itself — the traffic that makes a server *detected* rather than merely reachable. |

| Endpoint | What |
|---|---|
| `http://kids:32400` | the reference PMS |
| `http://kids:9081` | mitmweb, client axis (password `plexlab`) |
| `http://kids:7902` | the browser's desktop, for watching a session run |
| `http://kids:9224` | CDP, for driving it |

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
    --target http://127.0.0.1:32500 --target-token "$PLEXSIM_TOKEN" \
    --reference http://127.0.0.1:32400 --reference-token "$SERVER_TOKEN" \
    --out capture/diffs/plexsim.md
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

`harness/README.md` carries the same list with the measurements behind it.

## Testing

Run both against a live server. Neither needs network beyond RD and TMDB.

```bash
python3 tests/test_navigation.py            # client request replay, shape conformance
python3 tests/test_ui_surface.py            # context menu + every server page, end to end
python3 tests/test_all.py                   # play queue → decision → byte reads → real decode
python3 tests/conformance.py                # drives plexsim with python-plexapi
```

`test_ui_surface.py` **writes**: it edits rows, builds and deletes playlists and
collections, and finally deletes a row and a section. Everything it touches is in
memory, so restarting the server is the undo button — but run it against a test
instance, started on the fixture the checks are written against:

```bash
python3 server.py --port 32500 --catalog tests/catalog_ui.json --assert-claimed &
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

## Traps that cost real time

- **`catalog.json` links are account-scoped.** Real-Debrid `/d/` links resolve
  only for the account that created them. A different key answers
  `404 unavailable_file`, which surfaces as HTTP 502 on every part read and
  looks like plexsim is broken.
- **Cloudflare strips `Range` on a prefix request.** `--part-mode proxy` then
  sees `range=-` and fetches the whole file per playback start. Use
  `--part-mode redirect` through the edge.
- **Cloudflare 403s a `Python-urllib/*` User-Agent** on the media path. curl,
  a Plex product string, a browser string and even an empty UA all get 206. A
  harness that leaves urllib's default measures the bot filter, not plexsim.
- **`Connection: close` on part responses is load-bearing.** With keep-alive,
  ffmpeg reuses a connection behind an undrained body and seeking fails with
  `AVERROR(ENOSYS)`.
- **The UI draws a menu entry from what the server *advertises*, not from
  whether the write behind it works.** `/media/providers` decides whether Edit,
  Delete, Download, Optimize and Mark as Played exist at all; an unimplemented
  endpoint behind an advertised capability is a menu entry that does nothing.
  The per-entry gates are in `docs/findings.md`, read out of the client's own
  bundle (`main-*.js` module 92273).
- **A mutating endpoint answers 200 with an EMPTY body**, not a MediaContainer —
  and a playlist delete is 204 while a collection delete is 200.
- **A smart playlist or collection is a stored QUERY, evaluated on every read** —
  proved by scrobbling one movie and watching it leave an `?unwatched=1` smart
  playlist. Storing the matching rows makes a list that is right once and stale
  after. The two endpoints disagree on everything else: a playlist's `smart` is a
  boolean and carries `icon: "playlist://image.smart"`, a collection's is the
  string `"1"` and is OMITTED when not smart; a playlist rewrites the `server://`
  URI into `library://x/directory/<encoded>` and shows `content` only on the
  single read, a collection keeps the URI verbatim and shows it in the list too;
  and a smart collection does **not** tag its members.
- **An unrouted path answers 404**, the way a real PMS does. The catch-all used to
  return an empty container, which is fix 4's trap from the other side: a client
  reads a 200 as "this endpoint exists and is empty". Flipping it is what finally
  surfaced `GET /library`, which python-plexapi asks for first and which had
  never been routed. If something breaks after adding an endpoint, look here.
- **Two responses on the surface have a root that is not a MediaContainer** —
  `/butler` answers `ButlerTasks` and `/library/sections/{id}/query/parse`
  answers `Query`. Neither is guessable; both were measured.
- **The `include*`/`exclude*` options each DO something**, and ignoring one answers
  a valid document with a block missing. `includeChildren` inlines a show's
  seasons, `excludeElements=Media` substitutes `mediaCount`, and the `async*` pair
  makes the CONTAINER drop `identifier` and the media-tag pair. `includePreferences`
  is per TYPE — a movie has 3, a show 11, a season 3, an episode none, and the
  block is omitted rather than sent empty.
- **`group=` collapses to one row per distinct value and 500s for most fields** —
  a tag, the media and per-user state are all refused, and so are `duration` and
  `userRating`. `group=decade` groups on the YEAR column, so it does not collapse
  by decade.
- **The decision container sends ONE pair of codes.** A direct play is only
  `mdeDecisionCode: 1000` / `"Direct play OK."`; the general/directPlay/transcode
  triples appear only when direct play is REFUSED. Sending all of them says a
  transcode decision was made when none was. `resourceSession` is the client
  identifier, and `allowSync`, `librarySectionID` and `mediaTagVersion` are all
  STRINGS here.
- **`/hubs/promoted` is called ONCE PER PINNED LIBRARY** (`contentDirectoryID=`),
  and every shelf carries `key`, `context` (the identifier with a trailing section
  id stripped), `style` and `promoted`. `hubKey` is not a listing path — it is
  `/library/metadata/<comma-joined ratingKeys>` naming that shelf's own rows. And
  `/hubs/home/recentlyAdded` keys on `type`, NOT on `sectionID`.
- **The preplay page's Related shelf is `/hubs/metadata/{id}/related`**, a
  different endpoint from `/library/metadata/{id}/related` — the client rewrites
  the path. `related` carries the librarySection triple, `postplay` carries none of
  it, and a 404 on either reads to the page as "no related items".
- **Search is seventeen typed hubs on EVERY query**, empty ones included, because
  the client lays the page out from that list; each row carries a `score` (a string
  at `/hubs/search`, a float at `/library/search`) and a tag-matched row names the
  tag in `reason`/`reasonID`/`reasonTitle`. Matching is a word **PREFIX**: `uck`
  does not find "Big Buck Bunny" and one character finds nothing.
- **`/player/*` is not a server surface.** A real PMS answers 400 for every
  playback command: they go to a player. Answering 200 tells a companion client
  the command was accepted.
- **A playback session is created by `/:/timeline`**, not by a play queue, a
  decision or a byte read — a bare timeline opens the row and `state=stopped`
  closes it. The timeline's RESPONSE is not an echo: `playbackState` is
  `ignore`/`progress`/`complete` and `viewOffset` is `-1`.
- **Terminate is keyed on `Session.id`** and does not end anything by itself: the
  kill arrives in that session's next timeline as `terminationCode: 2006`, and the
  next part read is a 503.
- **The notification socket has to carry frames, not just pings.** A real PMS
  pushes `activity`, `timeline`, `status`, `progress` and `playing`; a client that gets none
  of them polls instead and never sees a change made elsewhere. Server frames are
  **unmasked**, and a malformed one puts the client back in fix 4's failure mode.
- **A filter's operator is part of the parameter NAME, not the value.** The client
  sends `year%3E%3E=2000`, so the server reads a parameter called `year>>`. A
  server that looks for names it knows sees nothing and filters nothing — which is
  what plexsim did for its whole life, while advertising 40 filterable fields.
  A bare `=` on a string CONTAINS; `==` is the exact one. See `filters.py`.
- **What a bare `=` means depends on the field's advertised TYPE, and three types
  are not strings.** A `tag` field is exact (`contentRating=G` does not match
  `PG`) and an EMPTY value on one means "has no value at all"; a `boolean` is true
  only for the literal `1`, so `unwatched=2` means false; `resolution` is an enum
  and a value outside `{4k,1080,720,576,480,sd}` is a 500; `location` is a section
  Location **id**, not a path. An empty value is a 500 only where it has to parse
  as a number or a date, not everywhere.
- **Which operator KEYS a field takes is not its advertised operator list.**
  `resolution>>=720` parses although only `=` is advertised. What is closed is
  tag-TABLE fields (`=`/`!=` only) and string fields (their six); anything else on
  one of those is an unknown FIELD, ignored by a listing and reported as an error
  by `/query/parse`.
- **A show library advertises its filter fields PREFIXED** (`show.genre`,
  `episode.title`) and the client sends the advertised key back verbatim. plexsim
  keyed its field map by those names and looked them up with the prefix stripped,
  so no filter on a show grid ever constrained anything. The prefix is a scope:
  the field is the tail, the lookup is global, and `/query/parse` echoes the scope
  back as the rule's own `type`.
- **`/filters` is per library TYPE too** — a movie library lists 28, a show library
  a different 15 whose keys carry `?type=2` (except `country`, `unmatched` and
  `editionTitle`). The type SCOPES an axis, so `/genre?type=2` on a movie library
  is empty. Seven of the advertised axes **404 on a real PMS as well**, so
  advertising them is the faithful answer, not the bug.
- **A media axis entry has no `fastKey` and its title is a LABEL** — `1080` prints
  as `1080p`, `en` as `English` — while `location` is the one axis whose container
  carries nothing but `size` and `title2`, and `/unwatched` is a LISTING titled
  "Unplayed" rather than an axis at all.
- **Every secondary container of a show library carries `nocache`** and no movie
  library's does. Measured across two servers and five libraries, so it tracks the
  library's type rather than cache state, even though the cause is invisible.
- **Container paging needs BOTH parameters, and the client sends them as HTTP
  HEADERS.** One alone is ignored by a real PMS; a negative size drops paging
  rather than slicing.
- **A `sort=` spec is a comma-separated LIST, and so is each term.**
  `random:<seed>` differs per seed, and `titleSort:desc:nocollate` is descending —
  splitting the spec on its first colon drops the secondary key and reading the
  direction off the first colon of a term loses it entirely. An unknown key is a
  500 on a real PMS, not a silently unsorted 200; an unknown MODIFIER is ignored.
- **CORS: echo the Origin and echo the requested header list.** A fixed
  allow-list blocks whatever it forgot, and the browser refuses the request with
  no server-side trace. `*` is invalid on a credentialed request. Plex Web hides
  this by passing identity in the query string rather than in headers.
- **XML is the DEFAULT response format**, not JSON — `_wants_xml()` returns true
  unless a client asks for JSON. A JSON-only test proves nothing about the path a
  TV or mobile client takes: three endpoints were rendering `<Video>` where a real
  PMS renders `<Photo>` or `<Directory>`, because `xmlout` names a `Metadata`
  row's element from the row's `type` and those rows have none.
- **The eventsource channel is a DIFFERENT wire format from the websocket**, for
  the same events: `event: <type>` plus a `data:` line holding one bare row, with
  no NotificationContainer and no size.
- **`displayTitle` is the plain label and `extendedDisplayTitle` carries the
  codec** — not the other way round — and a subtitle with a track title shows its
  LANGUAGE plainly, the track name only in the extended title. These two strings
  are what the quality and track pickers print.
- **The Console's live log stream is not fed by that socket.** 75 seconds against
  a busy reference pushed zero log frames, and the client opens no second socket
  and passes no filter — the page renders empty on a real server too.
- **An empty child list is omitted, never sent as `[]`.** These responses are XML
  underneath; `Metadata: []` is a shape no real PMS returns.
- **`PUT .../merge?ids=` on a real library is not reversible.** The `split` that
  undoes it hands the second item back on a NEW ratingKey carrying the first
  one's metadata, so measuring it costs a re-match. It cost one here.
- **`viewMode`: a season list is 65593, an episode list 65592.** Inverted, the
  client is handed the episode layout for a season list and draws no seasons.
- **Artwork URLs must be versioned.** Unversioned `/thumb/1` plus ratingKey
  reuse serves a stale poster — one show wore another's art for a day.
- **Never write to plex.tv's device registry to fake a server.**
  `PUT /devices/{id}` with a **numeric** id resolves to a pre-existing row and
  will silently overwrite it. Repeated claim exchanges with fabricated
  identities reset account-wide tokens and signed real servers out. Read the
  claim section of `docs/findings.md` before touching anything under plex.tv.
  Measured 2026-09-12 in the harness, and correcting that section: a real PMS
  announces with `PUT /devices/{machineIdentifier}` — the **identifier** form —
  carrying its full `<Device>` element and its `plex.direct` connection, and
  repeating on a timer. The numeric form is not what a real server uses.

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- Stdlib only on the server path. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a Real-Debrid `/d/` link, a Plex token, or a tunnel
  credential. The repo is private; that is not a reason to relax this.
