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
| `tests/fixtures/` | Redacted corpora recorded from a real PMS through a real client; `media/` pairs real files' probes with a real PMS's analysis of them |
| `harness/` | plexlab — the reference PMS and the recorder. See below. |
| `probes/` | One-off measurement scripts that produced findings |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |

Sidecar fixtures (`server_prefs.json`, `section_prefs.json`,
`section_meta.json`, `section_meta_show.json`, `item_prefs.json`, `section_prefs_new.json`,
`section_filters.json`, `section_sorts.json`, `system_agents.json`, `languages.json`) are captured
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

- **A hunting agent must never write destructively to the reference.** Cycle 4's
  sent `DELETE /library/sections/1` and the Movies library had to be restored from
  Plex's backup, re-keying a movie. Forbid section and item deletes, merge/split/
  match/unmatch, maintenance endpoints and network/auth prefs in the prompt, ask for
  every write and its undo, and snapshot the lab's state before and after.

`harness/README.md` carries the same list with the measurements behind it.

### The disposable write reference: plexscratch

The lab is read-only for hunts. **Writes are measured on `plexscratch`**, a sanitised
copy of the lab's config on kids (`harness/scratch/`, deployed to `/home/ben/plexscratch`),
answering at `http://100.85.222.1:32410`:

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
- **An unclaimed PMS admits only loopback, and only with a local `Host`.** A request from
  an allowed network is logged "Allowed Network (Subnet)" and still 401s; so is loopback
  with `Host: 100.85.222.1:32410`, and any `X-Forwarded-For` makes it WAN. So a socat hop
  inside PMS's namespace makes every request loopback, and clients send the copy's own
  `.LocalAdminToken`. A session's client address therefore reads 127.0.0.1 there.
- **Do not put the hop on 32401.** PMS binds that port itself and exits at start with
  "Error binding acceptor: Address in use".

## Testing

Run both against a live server. Neither needs network beyond RD and TMDB.

```bash
python3 tests/test_navigation.py            # client request replay, shape conformance
python3 tests/test_ui_surface.py            # context menu + every server page, end to end
python3 tests/test_all.py                   # play queue → decision → byte reads → real decode
python3 tests/conformance.py                # drives plexsim with python-plexapi
python3 tests/test_mediainfo.py             # mediainfo.py against a real PMS's analysis; no server
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
- **The Edit dialog saves to `PUT /library/sections/{id}/all`**, reads
  `/library/sections/{id}/common` first, and gets its editable TAG fields from
  `/library/sections/{id}?includeDetails=1`. plexsim answered the grid listing to
  the save (200, nothing written), 404'd the pre-read, and sent two pivot rows and
  no `Type` — so every Save was a silent no-op and the Tags pane was empty. With no
  `id=` the save edits the whole FILTERED set, and a bare `field=value` there is a
  filter that SCOPES it where on the item path it is an edit.
- **An indexed tag write REPLACES the list on the ITEM path** (`genre[0]…&genre[1]…`
  leaves exactly those two), and ANY `tag-` removal there clears the whole list
  whatever it names. **The section save path MERGES**: the indexed list goes first,
  the row's other tags follow, and a removal removes only what it names. The INDEX
  orders the list, not the parameter's position. A tag matches case-insensitively and
  keeps its canonical spelling and id even after being removed from every row — a
  real PMS keeps a tag TABLE, so plexsim does too.
- **Edit parameters are validated**: an unknown field or tag type is 400, an empty
  or unparseable integer is 500, a negative year or a bad date REMOVES the field,
  only `title`/`summary` store `""`, `userRating` is accepted and discarded, and
  only `.locked=1` locks. Library create and edit validate their whole form.
  That is the ITEM path's grammar. **The section save path differs**: a date it
  cannot read is a 500, a date also sets the year (an empty one clears both), and an
  empty year is removed rather than refused. Both parse a date the same way --
  `2007-02-30` rolls over, a bare integer is epoch seconds -- and 0 removes a year.
  The section path also STORES `userRating` (stamping `lastRatedAt`), and every
  rating is a number: an empty one is a 500 on the item path and removed on the
  section path. A lock there is an integer 0 or 1 (`01` locks, `true` is a 400), and
  `thumb`, `art` and the other artwork and identity columns take a lock on both paths.
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
- **`GET /` is NOT `/identity`.** It answers the whole capability blob plus 25
  `Directory` rows naming the server's top-level endpoints — which is where
  python-plexapi reads every attribute it has. `/identity` stays five fields.
- **`/library/metadata/{id}/tree` NESTS** — a show holds its seasons, each season
  its episodes — and an EPISODE's tree is rooted at the show. A `MediaItem` there
  carries only its `id`.
- **A show's `/children` carries an "All episodes" `Directory` row** and the
  container's `size` counts it; `excludeAllLeaves=1` removes both. `Guid` is on a
  detail row always and on a LISTING row only under `includeGuids=1`.
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
  by decade. (Cycle 14: WHICH row survives is the group's lowest `addedAt`, picked
  BEFORE the sort, and a type-prefixed key is scoped. See the bullet below.)
- **The decision container sends ONE pair of codes.** A direct play is only
  `mdeDecisionCode: 1000` / `"Direct play OK."`; the general/directPlay/transcode
  triples appear only when direct play is REFUSED. Sending all of them says a
  transcode decision was made when none was. `resourceSession` is the X-Plex-Session-Identifier (then the client
  identifier, then `""`), and `allowSync`, `librarySectionID` and `mediaTagVersion` are all
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
- **A play queue over a show or season holds its EPISODES**, never the parent row —
  which has no Media, so Play on every show page hands the player something
  unplayable. Same for a show library's section, a playlist, a collection and a
  comma-joined key list.
- **A queue's item ids are GLOBAL and stable and its version steps on every
  mutation.** The client DISCARDS any queue response whose `playQueueSelectedItemID`
  is not the one it holds, and the selection moves from the TIMELINE's
  `playQueueItemID` — so ids computed from position plus a frozen version means
  every refresh is thrown away once playback passes the first item.
  `playQueueLastAddedItemID` tracks plain adds only and disables the Up Next
  shuffle button. `shuffle`/`unshuffle` are 404; `items/{id}/move` is real.
- **`/:/progress` is NOT `/:/timeline`** — it keys on `key`, not `ratingKey`, and
  answers an EMPTY body. Routed at the timeline it silently records nothing.
- **The timeline's `viewOffset` is the item's STORED offset**, -1 only when it has
  none, and `Bandwidths` is armed by a DECISION (then present even on `stopped`),
  not by the state. An unknown `state` is 400 and an unknown ratingKey is 404.
- **Every state write refuses properly**: scrobble/unscrobble/rate/progress are 400
  with no `key` and 404 for a key that is not there, and `PUT /library/parts/{id}`
  is 400 for a stream id of the wrong type — answering 200 and clearing the
  selection leaves the track picker blank.
- **`/status/sessions/history/all` filters and sorts**, with a plain `>=` operator
  rather than the library grammar's `>>`, and an unknown sort key is 400 there
  where a listing answers 500. `DELETE .../history/{key}` really deletes.
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
  A bare `=` on a string CONTAINS -- except on `title`, where each query word must
  BEGIN a title word in order, and on `titleSort`/`summary`/`tagline`, where it is
  equality. `==` is the exact one, and it ignores case. See `filters.py`. (Cycle 14:
  the six-operator restriction is the ADVERTISED FieldType's, so the unlisted
  `titleSort`/`summary`/`tagline`/`originalTitle` take the ordering keys as well.)
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
  the field is the tail, the TYPE lookup is global, the VALUE is read from the row the
  prefix names (`type=4&show.title=` tests the show, and a scope below the row matches
  when any child does), and `/query/parse` echoes the scope back as the rule's own `type`.
- **`/filters` is per library TYPE too** — a movie library lists 22, a show library
  a different 15 whose keys carry `?type=2` (except `country`, `unmatched` and
  `editionTitle`). (Cycle 14: 22 is **1.42.2's**, the build plexsim reports; 1.43.4
  answers 28. See the per-version bullet below.) The type SCOPES an axis, so `/genre?type=2` on a movie library
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
- **The DEFAULT listing order is `titleSort`, and every tie falls back to it** —
  catalog order is not the default, so an edited Sort Title has to move the row.
- **`includeAdvanced=1` adds six Sorts to a movie library and eight to an episode
  view**, plus the Duplicates filter — the client reads that list straight into its
  sort menu, and every key it advertises has to work on the listing.
- **`/library/sections/{id}/search` is a SUBSTRING matcher**, one character is enough
  — NOT the word-prefix matcher `/hubs/search` uses.
- **Change Poster writes to the SINGULAR path** (`PUT /library/metadata/{id}/poster`)
  and the response BODY is the new artwork path, which the client assigns onto the
  row. The plural path is a 404 for a write, and serving image bytes from the
  singular one sets the tile's img src to a blob of PNG.
- **A resume point under a minute is not stored**, "Remove from Continue Watching"
  HIDES the row and KEEPS the offset, a scrobble writes NO history row (only a
  completed timeline does), and a show keeps its own `viewCount` that increments
  rather than recomputing.
- **An unknown section id is a 404 on every leaf** — answering a valid empty library
  with a working filter bar is fix 4's trap from the inside. And a real PMS never
  answers 405: every verb a leaf does not implement is a 404.
- **A `sort=` spec is a comma-separated LIST, and so is each term.**
  `random:<seed>` differs per seed, and `titleSort:desc:nocollate` is descending —
  splitting the spec on its first colon drops the secondary key and reading the
  direction off the first colon of a term loses it entirely. An unknown key is a
  500 on a real PMS, not a silently unsorted 200; an unknown MODIFIER is ignored.
- **A real PMS 401s EVERYTHING but `/identity` and `/web/*`** — artwork, media bytes
  and both notification channels included — for a missing token and a wrong one
  alike, with no `WWW-Authenticate`. plexsim had no token check at all, so any peer
  that could reach the port read the whole library. The gate is `--token` /
  `PLEXSIM_TOKEN` and defaults OFF (the suites and probes carry no token); the
  startup line says which. `/security/token` is the one exception: its own 400 with
  no parameters and 403 with a bad token, never a 401.
- **`Cache-Control: no-cache` goes on EVERY response**, and a versioned artwork
  asset is `no-cache` too — caching it for a day makes a Change Artwork invisible to
  the browser. Only transcoder output caches (`max-age=259200`). A real PMS also
  GZIPS containers and names both sizes in `X-Plex-Content-Original-Length` /
  `X-Plex-Content-Compressed-Length`.
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
- **`displayTitle` is the plain label and `extendedDisplayTitle` carries the codec** --
  not the other way round -- and a track title never enters `displayTitle`. Except that
  thirteen products (Plex Web; Plex for iOS, Apple TV, Android (TV), Android (Mobile), LG,
  Samsung, Windows, Mac, Xbox, PlayStation 4, PlayStation 5 and Vizio -- exact names,
  header or query) get ` (<CODEC>)` in a video's and a subtitle's `displayTitle` too. The
  server adds it on the way out (`_with_client_titles`), never into the catalog. These are
  the strings the quality and track pickers print.
- **`filters=` on the notification channels is a whitelist, and `log` is opt-in.**
  An earlier note here said the Console's log stream is not fed by that socket —
  measured WITHOUT the `filters=log` the client's own log connection passes. With it
  the reference pushes log frames continuously (386 in 14 s). A socket with no
  filters gets everything EXCEPT log; one with `filters=log` gets nothing else.
- **The notification LIFECYCLE is what makes a client refetch.** A refresh pushes
  seven frames ending in `state: 5` with NO `metadataState` — that frame dispatches
  `metadataProcessed`, and the client's reducer clears its entry only when
  `metadataState` is absent. Sending one `state: 3, "queued"` frame means the row is
  never refetched and every `!!metadataState` gate (the spinner, Refresh, Match)
  latches forever. The states are 0 created, 3 queued/processing, 4 loading, 5 done,
  9 deleted; an edit pushes 3 then 5, a delete pushes 9, and the activity must END.
- **An empty child list is omitted, never sent as `[]`.** These responses are XML
  underneath; `Metadata: []` is a shape no real PMS returns.
- **`PUT .../merge?ids=` on a real library is not reversible.** The `split` that
  undoes it hands the second item back on a NEW ratingKey carrying the first
  one's metadata, so measuring it costs a re-match. It cost one here. (Cycle 14: a merge
  whose TARGET has no `Media` -- a season, a show -- absorbs nothing at all. See below.)
- **`viewMode`: a season list is 65593, an episode list 65592.** Inverted, the
  client is handed the episode layout for a season list and draws no seasons.
- **An unknown SINGLE-segment path is a 200** (`content="plugins"`, the legacy
  plug-in dispatcher) where a multi-segment one 404s — so `/prefs`, `/settings`,
  `/login` and `/logout` all answer. Bare `/channels` is real too.
- **Verb routing is per (method, path) and not uniform**: `/identity` and `/hubs`
  answer any verb; `POST /library/sections` and `POST /media/providers` are 400 and
  their PUT/DELETE are 404; every verb on `/status/sessions` but GET is a 404.
- **`/photo/:/transcode` needs BOTH dimensions** (one alone is a 400, not a
  best-effort resize), 400s a non-numeric one and 404s a `url` naming a row it does
  not have — a 404 lets the client draw its own placeholder where a grey 200 tile
  gets cached. `/:/resources/<unknown>` is a 404 too.
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

- **The Add Library form reads `/library/sections/prefs?type=N&agent=ID`** before the
  library exists, and the list depends on BOTH; `?agent=` reshapes an existing
  library's list too. A 400 there stops the whole dialog. See `section_prefs_new.json`.
- **The three settings writers validate differently**, all measured: an unknown
  setting refuses the WHOLE write everywhere; a bad int is a 500 on `/:/prefs`,
  ignored on a library and stored raw on an item; a bool is true only for `1`/`true`;
  and a server setting keeps its TYPE on read-back while a library's is a string.
- **Accept is read exactly**: only a case-sensitive `application/json` or
  `application/xml` range counts, ranked by `q`, first listed on a tie, and any other
  parameter disqualifies it (`application/json; charset=utf-8` is XML).
- **JSON never carries `null` or `[]`** -- `json_body` strips both -- but an empty
  OBJECT is real (`"Related": {}`).
- **`titleSort` is the article-stripped title, sent only when it differs, and
  `originalTitle` is never a filename.** Catalogs built earlier carry both wrong, so
  `hierarchy.normalize_rows` runs at LOAD as well as at build. (Cycle 14 corrected the
  derivation itself: the title is TRIMMED and combining-mark FOLDED before the article
  comes off. See the bullet below.)
- **A grid or hub row is a lighter projection** (`_grid_row`): text-only tags, no
  `Stream`, no `Rating`/`Location`/`Field`. A check that needs streams or tag ids must
  read the DETAIL row.
- **`/system/scanners/{N}` is a library TYPE, not a section id**, and `/devices`
  lists every client with its X-Plex-Device-Name and X-Plex-Platform.
- **`/library/metadata/{id}/related` carries the same hub as the hubs form** -- an
  earlier note said it was always empty, from a recording of items with no relations.

- **A decision REFUSES what it cannot answer**, with a 400: a path that is not a
  playable `/library/metadata/{id}`, a bad `mediaIndex`, and a request whose profile
  name, platform and device all name no file in the server's `Resources/Profiles`
  (`Chrome` passes, `chrome` and `Darwin` do not). A test harness must send a real
  `X-Plex-Platform`. And plexsim cannot convert, so a refused direct play answers
  2000 / 4005 "neither", never 1001 "Conversion OK".
- **A playlist's timeline frames are type 15 and a collection's 18**, and the client
  builds the URL it updates from that number. A collection delete refetches its
  members (state 3 then 5 each) before its own state 9.
- **The timeline's minute is strict and has no `stopped` exemption**: 60 000 ms
  exactly is not stored, and a play completes past 90% on any state, once.
- **A query pair with no `=` is a 400 everywhere**, an empty path segment is a 404, and
  one trailing slash is harmless. Catch a leading `//` before `urlparse`, which reads it
  as a host.

- **No row names a theme.** plexsim has no theme audio, so `theme`, `parentTheme` and
  `grandparentTheme` stay off every row and container -- a real show without one
  carries none of the three, and the client fetches whichever it finds.
- **A hub row's projection depends on the row's TYPE and the SHELF**: movies and
  episodes are light (no Guid, text-only tags) on every shelf but Related and postplay,
  which carry the full row, and a show on a `mixed` Recently Added shelf is the full row. `excludeFields` applies to every row AND to the container.
- **`probes/ui_surface.py` without `--mutate` must send nothing but GET and HEAD** -- it
  is what gets pointed at the shared reference.
- **A sort key is type-prefixed as well, and python-plexapi always prefixes it**
  (`sort=movie.titleSort:desc`). On an episode list `show.titleSort` sorts by the SHOW
  and ties fall back to broadcast order; an unknown prefix reads the row itself, an
  unknown field after it is still a 500.
- **`/library/all` is a query, not a dump**: `type`, filters and `sort` all apply,
  the default order is the ratingKey, and a row carries `Media` only when typed.
- **A season with a year of its own carries it**, with its title as `titleSort` and
  no `parentYear`; only a season without one falls back to the show's year.

- **`/filters` is keyed by the asked `type=`, not by the library.** A movie library
  answers the episode list to `type=4`, types 8/9/10/13/20 have lists, `type=3` is
  empty and a non-number a 400; `includeAdvanced=1` adds Duplicates to 1 and 4 only.
  See `section_filters.json`.
- **The library subsets are not the grid.** On a SHOW library `onDeck`,
  `recentlyAdded`, `newest` and `recentlyViewed` are EPISODE lists whatever `type=`
  says, each `title2` names the subset, and `recentlyViewedShows` (top-level rows,
  viewed first) is the one with `allowSync` false and an unpaged totalSize.
- **Autocomplete matches a PHRASE at a word start, and reports the first
  occurrence.** `of s` finds Tears of Steel and `tears steel` does not; the
  `queryRange` can point at a different word than the one that matched. Exactly one
  `<field>.query`, and a media or state field is a 500.
- **By Folder follows `?parent=`**: a folder's subfolders are key/title rows and its
  files the items themselves, in FILE order. A view that ignores `parent` reopens
  the same list forever.
- **The client builds `/grandchildren` itself** from a hub row with `skipChildren`.
  A show's are its episodes, an episode's itself, a movie's an empty 200.
- **A paged season list KEEPS "All episodes"** at `Start=0`, outside the page, with
  its leaf counts; only a one-row page drops it. A paged row is still the light child
  projection.
- **`guid=` is the client's "do you have this?" lookup** (`/library/all?type=N&guid=`),
  a case-sensitive prefix. Ignoring it answers yes for every item.

- **Never write into a row `_full` returned.** It copies the top level only; its Media,
  Part and Stream dicts are the catalog's. `/status/sessions` set its playback flags there
  and every later read of the item said `directplay`.
- **Recently Added lists a new episode as its SEASON**, dated by the earliest episode, and
  the server-wide On Deck and Recently Added page. A `/library/search` hit names its
  library by title only.
- **The tree blanks every season title** and gives every node the ROOT's parentTitle; an
  episode part leads with `metadataType` 4 and a show ends with eight string settings.
- **The iOS platform types "All episodes" as a season.** Read the platform from the
  header as well as the query -- only the paging headers used to be normalised.
- **XML children have a fixed order** (Media, Image, UltraBlurColors, the tags, then
  Preferences/OnDeck/Extras/Related/Children) and `Meta` precedes a listing's rows. It
  lives in `xmlout._CHILD_ORDER`.

- **Paging is a property of the RESPONSE, not of a listing.** Every MediaContainer a
  real PMS answers honours `X-Plex-Container-Start/Size` (`/:/prefs`, `/identity`, a
  detail row), except a plug-in framework one (`noHistory`). plexsim applies it in
  `_page_container`, after the handler; a handler that pages itself sets `totalSize`
  and is left alone.
- **An unlocked Sort Title does not stick.** A title write, a sort-title write and an
  unlock each re-derive it from the title, article stripped, on items, collections and
  playlists alike; only a lock keeps what was typed. A check that writes `titleSort`
  without `titleSort.locked=1` is testing a value a real PMS throws away.
- **The section save path is scoped by `type` AND by library**: an `id` of another type
  or library is silently not edited, and a type-scoped filter (`show.title=`) there is a
  400 even though the same key filters a listing.
- **A frame's title is not the row's title**: a show is `Title (year)`, a season
  `<show> S01`, an episode `<show> S01 E07`. A section-path save frames every row's
  state 3 first, in sort-title order, and a list edit pushes its own type-15/18 pair.
- **A parent's title and studio are COPIED onto its children** (`parentTitle`,
  `parentStudio`, `grandparentTitle`) and have to be copied again on every edit; a season
  list's container names only a CUSTOM season title and carries the season's summary.
- **An emptied season or episode title is GENERATED** (`Season N`, `Episode N`, and it
  follows the index), and every emptied title keeps the old title's sort form.
- **`/:/progress` never completes a play**: it stores any position past a minute, even
  one past the end, and a missing or unparseable `time` is a 500.
- **A timeline refuses a position it cannot place**: a `time` past the `duration` sent
  with it, or a negative or non-numeric `time` or `duration`, is a 400.
- **A completed play answers `complete` until a `stopped` beat closes it**, and `ignore` to
  every beat after that -- not to any second beat.
- **Playlist writes validate like the rest**: a create needs `title`, `type` and `uri`
  and a type that fits the uri, and a smart list refuses item writes.
- **A state write needs the library `identifier`**: without it a scrobble, unscrobble,
  rate or progress is a 400 even for an unknown key, and a foreign one is a 404. The
  order is fixed -- key, identifier, the write's own parameter, then identifier value
  and key -- so a refusal check that omits the identifier tests the wrong branch.
- **It is the EPISODE scrobble that counts on its season and show**; scrobbling the season
  or the show leaves its own `viewCount` alone, and unscrobbling it removes it.
- **On Deck is one row per show**: its in-progress episode, else the one after the last
  watched; hiding any of its episodes hides the show until its next timeline. (Cycle 14
  corrected WHICH in-progress episode -- the most recently viewed, not the
  lowest-numbered. See the bullet below.)
- **A client's new play queue retires its older ones** (a 404 afterwards); another client's
  queue does not. A check that creates a second queue cannot read the first.
- **Deleting a play queue's SELECTED item answers the queue as it stood** (old version,
  selection and total, no rows); the next read has the selection on the following item.
- **A collection's children follow its `collectionSort`**: by release date when unset or 0,
  custom at 2 -- and a move starts from the order SHOWN, then switches to custom.
- **Renaming a collection leaves its members' tag alone**: the collection keeps the tag it
  was created under and finds, removes and untags members by it.
- **A library edit applies the whole form and scans**: `language` and `prefs[...]` apply,
  and a scan is ONE `library.update.section` activity stepping through the items, not an
  activity per row.
- **Unmatching an episode or a season gives `local://<id>` and the generated `Episode N` /
  `Season N`**, keeping the external Guids; the filename title and `none://` guid are a
  movie's.
- **A Media, Part or Stream attribute is measured against a real file, never read off
  ffprobe's names.** `tests/test_mediainfo.py` converts 46 recorded probes and compares them
  with what fun and the reference wrote for the same file; add a probe to
  `tests/fixtures/media/` before changing a rule. DTS is `dca` (`dca-ma` on the Media for
  DTS-HD MA), a Blu-ray subtitle `pgs`, a language is named in itself with its 639-2/T code and
  639-1 tag (`languages.json`), the Media's audio fields name the WIDEST track, the resolution
  label buckets width AND height, and the aspect ratio snaps to one of six values.
- **A track title never enters `displayTitle`**, and the extended title leaves out the label
  parts the title already carries: a part is carried when its first word is one of the
  title's words, split on whitespace and `- / ( ) [ ] + , | : ;` `&` but not on dots, folding
  ASCII case only, with a language's two-letter tag, `2.0` for Stereo and `AVC` for H.264
  counting too.
- **ffprobe lists a Matroska cover image as a video stream**, and a real PMS does not; a
  dual-layer Dolby Vision file's enhancement layer IS a second video stream.
- **Catalog media is corrected at load, and the titles are composed LAST.** Composed before
  `normalize_media`, a stored `dts` track is titled from the codec it no longer has.
- **An artwork write stores a VERSIONED path, never the url it was given.** `upload://` and
  remote candidates live beside the row (`_poster_url`, `sim.uploads`); `thumb` and `art`
  stay `/library/metadata/{id}/{thumb,art}/{ts}`, because that string is what a client puts
  in an `img src`.
- **A settings write, a butler run, a match, an artwork upload and a show's prefs write all
  push frames**, and a real PMS answers every one of them with an EMPTY body -- the task list
  or a container there reads to the client as a listing, not an acceptance. `POST /butler`
  and `DELETE /butler` are empty too, while `DELETE /butler/{Task}` is a 404.
- **A match is keyed on its `name`** (a guid with none is a 400, an empty guid with one
  renames and refreshes), and matching to a guid another row in the library already holds
  MERGES this row into that one. Every match runs the refresh sequence, so its activity ends:
  an activity that never ends leaves the Dashboard's spinner turning for the life of the
  process.
- **A smart list's Save is `PUT {list}/items?uri=<listing>`**, which replaces the stored
  query -- `limit` included -- and answers the list's row. Only the uri is stored; `content`,
  `leafCount` and the items are derived from it on every read.
- **A suite check that MERGES or deletes a fixture row starves the checks after it.** The
  match check absorbs the spare movie for that reason; cycle 9's Delete Version check was
  broken the same way, twice.
- **A play is watched only when a beat CROSSES 90% inside its own session.** The session must
  have reported a position under 90% of the duration THAT BEAT carries -- a single beat at the
  end, five climbing beats from 95%, another client's earlier position and a stored
  `/:/progress` offset all complete nothing. Past 90% the answer is `complete` or `ignore`,
  never `progress`. A suite check that wants a completion has to earn it with a beat below the
  threshold first.
- **A timeline beat's own `duration` is the only one that counts**: with none, or with
  `duration=0` (a 200, not a 400), nothing is stored and nothing completes, and the answer
  carries the item's stored offset. `/:/progress` is the exception -- it never sends one and
  still stores any position past a minute.
- **A beat at or under 60 000 ms DELETES the stored resume point** rather than being ignored,
  and a progress beat stamps `lastViewedAt` on the season and the show.
- **Restart a test server by waiting for the PORT to be free**, then check the new pid holds
  it: a kill plus a `/identity` poll can be answered by the process you just killed, and the
  suite then runs against a server it has already written to. The suite's own gate checks bind
  **32599** -- leave it free, or they measure whatever else is listening there.

- **zen runs Plex 1.42.2 — the exact build plexsim reports — and kids 1.43.4.** With both, a
  field only kids sends is a version difference and a rule both answer is not; fun is a third
  opinion, not the only second one. zen is production: GET and HEAD only, like fun.
- **A listing runs a QUERY, and four parts of it were never run.** `?limit=N` caps the rows
  (0 answers ONE row, a negative one every row, `abc` a 500) and `/newest`, `/recentlyAdded`
  and `/children` ignore it; a `type=` that does not parse is a **500** on a section listing
  and a **400** on `/library/all`, an empty one a 500 on both, and a type number the library
  holds no rows of is an empty 200; `id` filters every listing although no Meta advertises it;
  and a section's `allLeaves` takes all of it -- filters, sort, limit and type -- where it used
  to answer its whole leaf list to everything.
- **`id` compares like the column it is.** `=` exact, a comma list an OR, `>>`/`<<`/`>=`/`<=`
  numeric, and `==`/`!==` never match a number. A value that is not a number sorts ABOVE every
  id: `id=abc` matches nothing and `id!=abc` everything. `/query/parse` echoes it as a Rule
  with an EMPTY `fieldTitle`. It is what `guid=<x>&id!=<self>` -- the client's and
  python-plexapi's "another copy?" probe -- rests on.
- **`excludeElements=` reaches the section `allLeaves`, `/library/people/{id}/media` and
  `/hubs/items`** as well as the grid, and **`includeStorage=1`** adds `durationTotal` and
  `storageTotal` to a section grid row at the library's OWN top-level type only -- a show's are
  the sums over its episodes.
- **`/library/people/{id}/media` is typed by the TAG, not by its rows**: `viewGroup` and
  `viewMode` belong to an ACTOR's list (tagType 6), even when its rows are shows, and a
  Director, Writer or Producer list carries neither. The library filters narrow it too.
- **A statistics container pages its SERIES only.** `Device` and `Account` are a lookup: the
  page carries exactly the ones its own rows name, `totalSize` counts the series and `size`
  counts every element. And `timespan` has to parse as a whole number on all three endpoints
  (`0`, `-1` and `06` are fine; `abc`, `1.5`, empty and a leading space are 400s).
- **A real PMS gzips every compressible body the client offers to take compressed**, even when
  that makes it larger (83 bytes in, 90 out) -- `Accept-Encoding: identity` is the only thing
  that stops it. The old 860-byte floor and the "only if it shrinks" test were inferred, not
  measured.
- **A plain queue add lands in Up Next**, after the block of items ALREADY ADDED behind the
  current one and in front of the queue's original members; `next=1` goes ahead of that block.
  Testing "id greater than the selected id" reads every original member as already-added and
  appends to the very end.
- **The Upload Subtitle modal really uploads.** `POST /library/metadata/{id}/subtitles?title=&
  format=` with the file as the body is a 200 with an EMPTY body and leaves a new sidecar
  `Stream` that becomes the SELECTED subtitle; no `title` and a title with no extension are
  **500**s, an unaccepted format a 400 (`srt`/`ass`/`ssa`/`smi`/`txt` pass; `vtt`, `sub`,
  `idx`, `sup`, `pgs` do not), an empty body a **406**. A sidecar subtitle labels its codec
  `<CODEC> External` in both titles.
- **A stream PUT is accepted and stores nothing** (200, empty body, the title unchanged), a
  DELETE of an embedded track is **403**, and a DELETE of an uploaded sidecar really removes
  it. The GET of a real stream stays the 501.
- **A playlist's items and a collection's children are the LIGHT grid row**, like every other
  list -- no `Guid`, `Rating`, `Field` or `Stream`, tags text-only -- while keeping their
  librarySection fields and the `playlistItemID`.
- **A single history read is a projection**: nine attributes with `viewType: 1`, an episode
  adding `grandparentTitle`/`index`/`parentIndex`, and no `key`, `ratingKey` or artwork. It is
  not the row `/status/sessions/history/all` lists.

- **A five-clip library cannot show a row-shape rule that needs real content.** zen's 3,887
  movies are the instrument for those: a LIGHT row caps its tag lists (2 `Genre`, 2 `Writer`,
  2 `Country`, 3 `Role`, always the first of the detail row's order) where the DETAIL row carries
  all 155 of a movie's roles; the search hubs cap at 3 rows with no `limit` and
  `/library/search` at 10; an actor tag row is `tagType: 6` and a director row 4. None of that is
  visible on a library where nothing is long enough to truncate.
- **An episode's `parentGuid` is the SEASON's guid**, not the show's — the season's own
  `parentGuid` is the show, which is right on the season row and wrong copied onto the leaf. A
  catalog built before this was measured carries the wrong value, so `hierarchy.normalize_rows`
  corrects it at LOAD.
- **The section grid says `allowSync: true`**, like every other listing container; only
  `/library/sections`, `/library/all`, the secondary axes and a few subsets say false.
- **`group=` takes `type`, `guid` and `id`** as well as the scalar columns; only tag joins, media
  and per-user state are 500. **`/library/sections/{id}/actor` is the one secondary axis that
  carries no `content`.**
- **`/common` compares an EPISODE's title and not a SEASON's**, and `titleSort` is `""` only when
  genuinely mixed — a single row answers its own sort title, derived where it stores none.
- **A write's frames are their own measurement, and guessing them is a real bug.** A subtitle
  upload pushes NOTHING (cycle 11 added an edit pair without measuring, and told every client on
  the socket that an unchanged item had changed). A refresh's `updated 49` comes SECOND, before
  the processing timeline. A scan ends inside a NESTED pair of `provider.subscriptions.process`
  activities, and `?force=1` runs that whole scan before it refreshes anything. Optimize Database
  runs a `general.db.optimize` activity at 0/10/40/60.
- **A library edit needs an `agent`, not a `name`** — the client's upgrade form sends `agent` and
  `scanner` and nothing else — and a create answers a `Location` header naming the new library.
- **A queue add that adds nothing changes nothing**: a `server://` uri naming another MACHINE
  adds no rows, and no no-op add steps `playQueueVersion`. The client discards a queue response
  whose version it did not expect, so stepping it on a no-op throws away the player's queue.
- **The subtitle upload's format check is CASE-SENSITIVE and takes `psb`**: `SRT` is a 400 where
  `srt` is a 200, and the client's modal offers extensions the server refuses.
- **A stream path tells a malformed id from a missing one**: `DELETE .../abc` is a 400 and
  `DELETE .../999999` a 404; a POST to a stream that exists is a 404, not the 501 its GET answers.

- **A row with NO value sorts BELOW every value** -- first ascending, last descending, on
  every sort key and every row type. Twelve of the fifteen sorts a section advertises
  default to `desc`, so keying on `(value is None, value)` opens Rating, Plays, Date
  Viewed and Critic Rating on the rows that have no value at all.
- **A light row caps `Director` at THREE and carries no `Producer`** however many the
  detail row holds (17 directors and 22 producers on one zen movie). Only a real library
  can show it: no lab item has more than one director.
- **`/hubs/search` returns the ITEMS behind a matched tag**, each naming it in
  `reason`/`reasonID`/`reasonTitle` -- 53 of 81 item rows across ten zen queries -- while
  `/library/search` carries the tag rows and the title hits only. And the seventeen hubs'
  base ORDER has `movie` fifth and `collection` fourteenth; it is visible only on a query
  that matches nothing, because hits PROMOTE the hubs that scored.
- **The "All episodes" row follows the PAGE's row count, not the offset.** It is there
  whenever the page returns more than one season row, wherever the page starts, and gone
  on a one-row page even at `Start=0`. Cycle 8's "the servers disagree past Start=0" was a
  two-season library, not a version difference.
- **`duplicate=1` is "more than one Media"**, and on a SHOW or SEASON "a leaf has more
  than one" (214 movies, 504 episodes, 44 shows and 74 seasons on zen).
- **A bare `<field>=` means three different things.** A tag-TABLE field matches NOTHING
  (37 of 600 zen movies have no Genre and `genre=` still answers none); `studio` and
  `editionTitle` match EVERY row; everything else of that kind matches the rows with no
  value. `contentRating=None`, in that spelling only, is the empty synonym and the key of
  the axis's own trailing bucket -- the one axis that has one.
- **A tag or field axis REFUSES an unparseable `type=`** -- 500, or 400 on `year` alone --
  where a MEDIA axis ignores `type=` completely and answers the same list to `abc`.
  `firstCharacter` is scoped by the type and its counts follow.
- **An EPISODE with an `audienceRating` carries `audienceRatingImage` and a `Rating`
  row**, a SEASON never does, and an episode carries its SHOW's `contentRating`. A show's
  `/allLeaves` -- and no other list -- stamps the show's `studio` on every episode row,
  and a season row of `/library/recentlyAdded` alone carries `parentSummary`.
- **`viewedLeafCount`, `viewedOrInProgressLeafCount` and `unviewedLeafCount` filter a show
  listing** although no Meta advertises them; `leafCount` and `childCount` do not.
- **A shelf with no `count=` carries SIX rows and says `more`**, not twelve -- and 1.42.2
  builds three shelves 1.43.4 does not (`movie.topunwatched`, `tv.startwatching`,
  `tv.toprated`), so zen is the witness. `pinnedContentDirectoryID` reshapes the promoted
  Recently Added shelf into `home.movies.recent.N` / "Recently Added Movies" on 1.42.2 and
  changes nothing on 1.43.4.
- **A write to a PARENT cascades.** Unmatching a season unmatches its episodes; unmatching
  a show unmatches its seasons AND episodes, and every descendant's `parentGuid` /
  `grandparentGuid` follows; an episode unmatch drops the people lists, `audienceRating`
  and `year` and empties `summary` whichever path reaches it; a season's `index` lands on
  its episodes' `parentIndex`. **Read the children back after every parent write** -- the
  rule is never on the parent row alone.
- **An unmatch leaves no activity running.** A real PMS runs the refresh sequence and ends
  it; an activity left open turns the Dashboard spinner for the life of the process.

- **`titleSort` is the title TRIMMED and FOLDED and then article-stripped.** Leading
  whitespace goes, Unicode combining marks are dropped (`Amélie` sorts as `Amelie`,
  `Señor's` as `Senor's`), and only then the/a/an. Measured on zen over all 5,294 rows:
  the model reproduces 5,289, all 21 accented rows carry the folded form and none lacks
  a `titleSort`, and the 51 rows whose non-ASCII survives NFD (CJK, `’`, `–`) carry none
  at all. The five misses are Spanish `La` and German `Das` -- per-item language, not
  mechanical, and deliberately not implemented.
- **The word matcher folds accents and the substring matchers do not.** `/hubs/search`
  and `/library/search` fold unconditionally -- and it IS a fold, not the fuzzy score:
  `Amelie` and `Amélie` both answer 0.93095 where `Amlie` and `Amelke` answer 0.33095
  and `Amelia` answers nothing. `title=` on a listing folds too, but only while the
  query holds NO SPACE: `title=Bogota` finds the accented row and `title=Bogota City`
  and `title=Bogota ` find nothing, while every accented spelling does. The section's
  substring box, `/autocomplete` and `title!=` do not fold at all.
- **`/filters` and the `Meta` descriptor are PER VERSION.** 1.43.4 advertises 28 movie
  filters, 17 at `type=4` and 6 at `type=10`; 1.42.2 -- the build plexsim reports --
  advertises 22, 9 and 5. The difference is the media-codec axes (`hdr10plus`, `atmos`,
  `videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`) everywhere plus `hdr` and
  `dovi` on the EPISODE list, which the movie list keeps. Types 2, 3, 8, 9, 13 and 20
  and the Duplicates row are identical on both. `Meta.Type[].Filter` is a separate
  fixture carrying the same split.
- **A `group=`'s surviving row is the group's LOWEST `addedAt`, picked BEFORE the sort**
  -- so the same SET comes back under every `sort=` and only the order changes. Keying
  on the sort's first row makes changing the sort change which rows the grid shows. And
  a type-prefixed group key is SCOPED like a filter: `type=4&group=show.title` collapses
  to the show count, not to the episode-title count.
- **The `actor` axis is the union of each item's FIRST THREE roles**, not every Role tag
  in the library -- kids' five movies contribute 0+2+2+2+3 and its axis totalSize is
  exactly 9; zen's is 3413 against 59,030 actor tags. Every other tag axis carries every
  tag. The axis's own `title2` says so: "By Starring Actor".
- **Nine columns filter although no Meta advertises them**: `viewOffset`, `skipCount`,
  `ratingCount`, `absoluteIndex`, `lastRatedAt`, `updatedAt`, `createdAt`, `mediaCount`
  and `originalTitle`. `/query/parse` echoes each as a Rule with an EMPTY `fieldTitle`,
  the shape `id` already had. An unknown field answers EVERY row, which is how a known
  one is told apart. `parentIndex` is a **500** on a listing instead, and a MOVIE row's
  `index` column is **1** on every row although the row never emits the attribute.
- **A string field's six operators are the ADVERTISED FieldType's restriction, not the
  python type's.** `titleSort`, `summary`, `tagline` and `originalTitle` appear in no
  Meta and take `>>`/`<<` (`titleSort>>=ZZZZ` is 1 row of zen's 3887), while the
  advertised `title` and `studio` answer every row to the same key -- an ignored field.
- **Thirteen more `sort=` keys work than the fifteen `includeAdvanced=1` advertises**:
  `guid`, `originalTitle`, `contentRatingAge`, `studio`, `skipCount`, `absoluteIndex`,
  `mediaSize`, `createdAt`, `type`, `viewedLeafCount`, `tagline`, `summary` and
  `editionTitle`, each a real ordering. `leafCount` is the other direction -- both
  references 500 it, along with `childCount`, `slug` and `key`.
- **`/status/sessions/history/all` VALIDATES its ids.** A non-integer `accountID`,
  `librarySectionID` or `metadataItemID` is a 400 (`abc`, empty and `1.5` alike, while
  `1`, `-1` and `01` pass); a `metadataItemID` naming no row is a **404**, `0` included;
  an unknown `librarySectionID` is an empty 200; `deviceID` is not validated at all; and
  `viewedAt>=` refuses a non-integer. Answering 200/`size 0` to a bad id reads to the
  client as "no history" rather than as an error.
- **`group=` reaches the SECTION `allLeaves`** -- the last part of the listing grammar
  that surface was missing -- while an ITEM's `/allLeaves?group=` is ignored.
- **`#` is the DIGITS bucket of `firstCharacter`, not the everything-else one**: any
  other non-letter gets a bucket of its OWN, listed after Z, and every bucket `key` is
  percent-encoded (`%23`, `%E3%80%8E`). Raw, `#` is a URL fragment and the client's
  request loses everything after it.
- **`/library/sections/{id}/userRating` is a real axis with a FIXED six-bucket scale** --
  `10` "5 stars" down to `2` "1 star" plus `-1` "Not rated", identical on both servers and
  on both library types whatever the library holds -- and it IGNORES `type=` the way a
  media axis does. The nine axes of OTHER library types (`mood`, `style`, `format`,
  `subformat`, `source`, `make`, `model`, `place`, `tag`) answer an EMPTY secondary
  container with their own `title2`, not a 404, and they VALIDATE `type=` the way `/genre`
  does. The two families split on that, so neither inherits the other's rule.
- **`/library/metadata/{id}/banners` and `/thumbs` are candidate lists too** (200 on both
  versions, `banners` empty on both, `thumbs` from the media bundle). `/squareArts` is
  **1.43.4 only** -- 404 on zen -- so plexsim, which reports 1.42.2, keeps 404ing it.
- **`/library/sections/{id}/recentlyReleased` does not exist.** A 404 on both references,
  every library type, with and without `type=`. plexsim answered a listing there for its
  whole life; the name appears in no findings document and in no suite, so it was assumed
  rather than measured. Sweeping a surface path by path against BOTH references is what
  found it -- 59 top-level paths and 48 item leaves agreed, and this one did not.
- **`/library/metadata/{id}/background` and `/composite` are 404s on an ITEM**, on every
  row type, bare and versioned, while `thumb`, `art` and `poster` are 200s. A `composite`
  is real on a playlist and a collection only; `background` is not a path at all, it is an
  `Image` element's `type`.
- **A sweep must run against a FRESHLY RESTARTED fixture server.** Pointed at one the UI
  suite has finished with -- it deletes a row and a section at the end -- the same probe
  reports `thumb` and `poster` as 404s that both references answer 200.

- **A merge whose target has no `Media` of its own absorbs NOTHING.** Merging one season
  into another is a 200, no frames, and a tree that is completely unchanged -- both seasons
  and every episode still there -- and the `split` that would undo it is a no-op too. An
  EPISODE merge beside it is real (two Media on the survivor, a 404 on the other, the leaf
  counts dropping). plexsim's `catalog.remove` cascades, so a season merge deleted the
  season AND its episodes: the programme's one data-loss bug.
- **On Deck is the MOST RECENTLY VIEWED in-progress episode**, not the lowest-numbered --
  measured on `long` media, where an episode can actually be left in progress. A beat on
  episode 8 puts 8 on deck, a progress write on episode 10 moves it to 10, a fresh beat on
  8 moves it back. Cycle 9 recorded the opposite from ten-second clips.
- **The prefs cascade belongs to `showOrdering` ALONE.** `showOrdering=dvd` pushes 45
  frames -- it renumbers the episodes, so the server re-reads the tree -- while
  `flattenSeasons`, `episodeSort`, `useOriginalTitle` and `subtitleMode` each push ZERO.
  What those four do instead is change the rows: `flattenSeasons=1` sets `skipChildren` on
  the show and `skipParent` on every episode while REMOVING their `parentRatingKey` and
  `parentKey`; `episodeSort=1` puts a season's `/children` newest first while `/allLeaves`
  keeps broadcast order. plexsim cascaded on every prefs write and changed no row.
- **An artwork PICK re-versions `thumb`, `art` and `theme` onto ONE stamp**, moves
  `updatedAt` with them, LOCKS the picked field, pushes one frame (state 5, no
  `metadataState`) and CASCADES the stamp to the children that borrow it -- a season's
  `parentThumb`/`art`, an episode's `grandparent*` -- never the season's own `thumb`.
- **A hub container says `allowSync: true`** on every hub path except
  `/hubs/metadata/{id}/postplay`, which says false; `/hubs/search` and `/hubs/items` carry
  none at all. (`postplay` DOES carry `allowSync` -- an older note here said it carried
  neither that nor the librarySection triple; only the triple half was right.)
- **On a SEASON listing the leaf-count filters read the SHOW's counts.** zen's
  `type=3&viewedLeafCount>>0` answers 48 seasons -- every season of the 8 shows with a
  watched leaf -- and all 48 carry 0 on their own row.
- **A collection answers `/children` on the METADATA path** (a full secondary container with
  `key`, `parentIndex` = its TAG id, `parentTitle`, `title2`, `nocache` and the
  librarySection triple) while `/library/collections/{id}/children` answers the BARE
  `{size: N}`. A `type=18` listing is a real QUERY (`title=`, `sort=`, default titleSort),
  a collection row's `index` is its own tag id, and `/library/all?type=18` lists every
  collection the server holds across libraries in title order.

- **`/status/sessions/history/all` lists NOTHING without a `type=`** -- and the `type` is the
  row's `viewType`, not its metadata type. kids' history is five MOVIES and zen's is 73
  EPISODES and both answer to `type=1`, while `type=0/2/3/4/5` answer none on either;
  `type=abc` and an empty one are 400s and every listing row carries `viewType: 1`. That is
  the whole of the `size 0` cycle 11 could not explain.
- **`titleSort` trims the LEFT only.** A trailing space survives into the sort title -- which
  is why a title written with one carries no `titleSort` at all, the derived string being
  equal to the title. `'  Zeta Both  '` sorts as `'Zeta Both  '`.
- **`flattenSeasons` and `episodeSort` are a TRI-STATE on an item**: `-1` INHERITS the
  library's value and `0`/`1` override it in both directions. The library-level write --
  where the Edit Library dialog puts them -- produces exactly the row effects the item path
  does, and an unwritten show reports `-1`, not the `0` a stale capture recorded.
- **`title=` folds across a SPACE.** Cycle 14's "the fold stops once the query has a space"
  was a colon: a row matches if EITHER the raw word-prefix matcher matches, OR a folded pass
  in which every term but the LAST must equal a whole whitespace word of the title. So
  `Adoravel Trapalhao` finds its row, `Bogota City` does not (the word is `Bogota:`), and
  `Bogot City` does. A query with an empty term -- a trailing or doubled space -- fails the
  folded pass. Punctuation rides inside a term: `tournee` misses `(tournee`.
- **The section search box answers the LIGHT grid row**, like every other listing -- no
  `Guid`, `Rating`, tag ids or `Stream`. It was the one listing plexsim answered in full.
- **`excludeElements=` is a property of the RESPONSE, as paging is.** Every container that
  carries rows honours it, `/library/all`, `/library/recentlyAdded`, a section's
  `recentlyAdded`/`newest`/`search`/`common`, `/grandchildren` and every hub path included.
- **Four more columns filter and eight more PARSE.** `parent=` (the immediate
  `parentRatingKey`), `rating=`, `audienceRating=` and `file=` (a case-insensitive substring
  of ANY Part's path) all constrain a listing; `/query/parse` additionally echoes
  `parentIndex`, `guid`, `slug` and `extraType` as Rules with an empty `fieldTitle`. The
  parse surface is WIDER than the filter surface.
- **`contentDirectoryID` scopes and VALIDATES `/hubs/search` alone** -- a value naming no
  section is a 404 there and ignored on every other hub path.
- **`/playlists?includeMeta=1` and `/hubs/home/recentlyAdded?type=N&includeMeta=1` carry a
  `Meta`.** The playlist list's six sorts end in `mediaCount` "Item Count", NOT the
  `updatedAt` of `/playlists/sorts`; the hub's one row is `movie`/true at `type=1`,
  `mixed`/false at `type=2` and absent for every other type.
- **`includeChildren=1` inlines a `Children` block on a SHOW and on nothing else.**
- **The inline `Related` block caps at FIVE and obeys `includeRelatedCount`**, where the
  standalone `/hubs/metadata/{id}/related` answers six.
- **`limit=` and `parent=` are PARSED wherever a listing consumes them**: an unparseable
  value is a 500 there and a 400 on `/status/sessions/history/all`. A good `limit` still does
  nothing on `/children` -- ignoring the VALUE is not the same as not parsing the string.
  `/hubs/search?limit=` has its own grammar: unparseable 400, non-positive 500.
- **A FRACTIONAL value refuses on seven columns only** -- `viewCount`, `viewOffset`,
  `lastViewedAt`, `lastRatedAt`, `addedAt`, `updatedAt`, `createdAt` -- and not on `year`,
  `duration` or the rest. `lastRatedAt`, `createdAt` and `lastViewedAt` refuse a word too:
  the VALUE is parsed before the row's own is looked at, so a null column does not excuse it.
- **A READ-ONLY route answers 404 to every verb but GET and HEAD**, while the legacy
  single-segment plug-in dispatcher answers EVERY method. `PATCH` and a bare `OPTIONS` are
  ROUTED like any other verb, so a bare OPTIONS 404s wherever a GET-only route does; a
  method a real PMS does not recognise at all is a **500**, never a 501.
- **POSTing an EMPTY body to any artwork or subtitle candidate list is a 406**, `/extras`
  being the 400. On the subtitle upload the empty-body check runs BEFORE the title check.
- **`DELETE /library/metadata/{id}/{poster,art,thumb}` CLEARS the artwork**: 200 with an
  empty body, the field gone, the artwork left re-stamped, one `state 5` frame, nothing
  `selected` afterwards and a 404 on the bare GET. An EPISODE owns no `/art` path at all.
- **A collection's `ratingCount` is its member TYPE number, +100 when smart** (movie 1, show
  2, season 3, episode 4; 101/102/104), its `librarySectionID` is a NUMBER, its
  `includePreferences` block is two settings -- `collectionMode` plus `collectionSort`, or
  `collectionFilterBasedOnUser` when smart, which also 400s a `collectionSort` write -- and
  `GET /library/collections/{id}/prefs` is a 404. `collectionSort` is alphabetical for any
  plain non-zero integer it can parse and by release date otherwise.
- **A `type=18` SECTION save renames the members' `Collection` TAG and the item path does
  not**; the tag keeps its id. A member's tag carries the collection's `guid` and no `count`,
  a label on a collection reaches the `/label` axis, and a collection is found by
  `/hubs/search` as a `tagType: 2` row -- but never by `/library/search`.
- **A refresh CASCADES**: 7 frames on an episode, 18 on a season (its episodes inside its own
  activity, with no `activity updated`), 49 on a two-season show. An **analyze** frames every
  LEAF three times -- `mediaState` analyzing, thumbnailing, none -- and ends in one
  `media.generate.chapter.thumbs` activity; a season and a show row are never framed
  themselves. A library **create** ends in a `backgroundProcessingQueue` `queueRegenerated`
  frame, which follows any write that leaves work in the queue.

- **A real PMS LOCALISES its own text.** `X-Plex-Language` (header or query) beats
  `Accept-Language`, a regional form works like its primary subtag, an unknown language
  falls back to English, and 28 languages answer differently. The filter and sort menus,
  the `Meta` titles, the hub titles, a GENERATED `Season N`, a stream's language name and
  `displayTitle`, and all 200+ `/:/prefs` labels move; the LIBRARY's own rows do not, and
  neither do `All Movies`, an axis's `title2` or `Unplayed` -- the same words ARE
  translated as a section pivot's `title`, so the table is keyed by ATTRIBUTE and then by
  the English string (`translations.json`, captured by diffing each endpoint against
  itself in each language).
- **A real PMS ROUTES before it authenticates.** With no token an unknown path is a
  **404** and only a path that exists is a 401. Of the single-segment paths exactly eleven
  answer 401 (`library hubs playlists devices butler servers clients accounts activities
  playQueues log`), `/identity` and `/security` answer 200, and every other one is a
  **403** -- whether or not it answers when a token IS given. `GET /` is a 401 whose
  193-byte body redirects a browser into the web client.
- **The `firstCharacter` AXIS and its BUCKETS disagree, on a real PMS.** The axis counts a
  season or an episode by its SHOW's sort title (28 of 28 buckets on zen, both types); the
  listing at `/firstCharacter/{key}` filters on the row's OWN stored sort title. So a
  season list's `A` bucket says 64 and answers 0 rows.
- **A section leaf's extra path segment is a bucket or a 404, never the leaf again.**
  `/all/zzz`, `/sorts/zzz`, `/studio/<real key>`, `/audioLanguage/en` and fourteen more
  are 404s; the tag axes, `year`, `decade`, `contentRating`, `resolution` and `userRating`
  answer that bucket's rows. `extras/all`, `query/parse`, `indexes/thumbnails` and the
  VERSIONED `composite/{v}` are the real two-segment leaves; the bare section
  `/composite` is a 404.
- **A show library's `?type=3`/`?type=4` grid defaults to BROADCAST order** -- the show's
  sort title, then `parentIndex`, then `index` -- not to a flat `titleSort`, and its
  `/unwatched` carries `nocache` like every other container it answers.
- **An artwork path is a 200 only on the row that OWNS the image.** A row that borrows
  carries the owner's path verbatim (kids' season 7 and episode 8 both carry
  `/library/metadata/6/art/<v>`), so comparing the stored path's ratingKey with the
  requested one IS the rule; cycle 15's episode-only `/art` 404 was its special case.
- **The envelope is measured too.** Every response names its connection state
  (`Keep-Alive: timeout=20`, and an idle socket really is closed at 20 s); a paged
  container answers `X-Plex-Container-Start`/`-Total-Size` as HEADERS and the paging
  header BEATS the query; `Range` applies to every body (206/416, never gzipped); a
  malformed header (no colon, a TAB in a value, a name over 1024 or a value over 4096) is
  a 400; `Accept-Encoding` tokens match exactly (`GZIP` and `x-gzip` get plain); no `100
  Continue`; a preflight with no `Origin` carries no CORS headers; and **no `Server:`
  banner at all**.
- **The Accept `q` grammar ranks a zero rather than dropping it.** The FIRST parameter must
  begin exactly `q=` or the range is disqualified (`; q=0.5` and `;Q=0.5` are out,
  `application/json ;q=0.5` is in); no parameter weighs 1.0, a single parseable
  non-negative `q` weighs that, and everything else -- `q=0`, `q=abc`, `q=`, `q=-1`,
  `q="0.5"`, `q=0.5x`, a second parameter -- weighs 0 and still QUALIFIES.
- **`/web` is a `302 Moved Temporarily`** with an ABSOLUTE Location from the request's own
  `Host` and `Cache-Control: public`, the one place on the surface that is not
  `no-cache`; a `/web/*` asset is `no-cache`, `Accept-Ranges: bytes` and never gzipped,
  `.js` is `text/javascript`, `.html` carries no charset, `.ico` is
  `application/octet-stream`.
- **`X-Plex-Platform: Roku`** -- that exact spelling -- strips `id` and `Pivot` from every
  library `Directory` of `/media/providers` and changes nothing else.
- **Four XML shapes**: a `Meta/Type` orders its children `Filter, Sort, Field`;
  `includeDetails=1` carries no `active`/`activeDirection` on a Sort nor `advanced` on a
  Filter where `includeMeta=1` carries all three; the rows `includeChildren=1` inlines
  carry no librarySection triple; and a container's attributes are `size`, then
  `totalSize` and `offset` when paged, then alphabetical.
- **`/playlists/{id}/items` is a typed QUERY** -- `type=2` answers the SHOW that owns the
  member episodes and `type=3` their seasons -- and `includeMeta=1` carries the page's own
  tab bar. A playlist is born `viewCount: 1` with `lastViewedAt == addedAt`, and that pair
  is on the LISTING row only. `/playlists` filters by `title`, `smart`, `sectionID` and
  `type` (a playlist is 15), its default order is SMART first then `titleSort`, and
  `sort=mediaCount` is a real ordering where `leafCount` is a 500.
- **`POST /playlists/{id}/items` is a 404**: only PUT adds. An empty `title=` is stored
  verbatim; `smart=1` over a comma-joined KEY LIST is a 400 (the test is the uri's SHAPE,
  not whether it resolves -- refusing anything that resolves breaks every smart create);
  a uri the server cannot resolve adds NOTHING. `PUT /playlists/{id}` takes the whole ITEM
  edit grammar, `playlistItemID` is a SERVER-WIDE sequence, a smart list's items carry
  none and every item write on one is a 400, and a playlist's artwork lives under
  `/library/metadata/{ratingKey}/` while `/playlists/{id}/{posters,arts,thumb,art}` and
  the bare `composite` are 404s.
- **A shuffled play queue is really shuffled** (plexsim reversed it, identically every
  time). `includeBefore`/`includeAfter` window the queue with NO `window=` -- the radius is
  unlimited then -- and each is a boolean whose every other value is a 400. EVERY item
  delete answers the container as it stood BEFORE it, and only the SELECTED one drops the
  rows. A move refuses `after=<itself>` (404) and an empty `after=` (400). A queue built
  from a PLAYLIST uri is a playlist queue.
- **The SECTION save path stores an unlocked `titleSort.value=`** until the next title
  write re-derives it, where the ITEM path discards it; `type=abc` there is a 400. A
  show's `contentRating` reaches only the episodes carrying none of their own, a show's
  `index` cascades onto its seasons' `parentIndex`, a season that LOSES its year gains
  `parentYear` in the same read, and the `state 3` frame carries the row's title from
  BEFORE the write.
- **An UNMATCHED item drops its agent-driven prefs**: a show loses `languageOverride` and
  `useOriginalTitle`, a MOVIE loses the whole `Preferences` block, a season keeps its
  three.

- **Three fixtures were captured off 1.43.4 and plexsim reports 1.42.2.** Cycle 14 split
  `Meta.Type[].Filter` per version and left `Field` at 1.43.4's: 1.42.2 advertises **34**
  movie Fields and **20** episode ones against 40 and 26, differing in the six media-codec
  axes -- and on 1.42.2 those six are UNKNOWN fields, so a listing filtered on one answers
  EVERY row and `/query/parse` reports an `Error`. `tv.plex.agents.nfo.movie`/`.series` are
  1.43.4-only too (four modern agents there, not six). When a fixture disagrees with zen,
  suspect the version before the shape.
- **A real PMS escapes XML attribute values into pure ASCII**, `encoding="UTF-8"`
  notwithstanding: `&amp;`/`&lt;`/`&gt;` named, `"` and `'` NUMERIC (`&#34;`, `&#39;`), TAB
  decimal `&#9;` but NEWLINE and CR HEX (`&#xA;`, `&#xD;`), every codepoint from U+0080 up
  its DECIMAL reference (an emoji is one `&#128512;`, not a surrogate pair), and every
  control XML 1.0 forbids replaced with a literal `?`. The surface carries no element TEXT,
  so that is the whole rule.
- **A ROW's XML attributes come out in a measured order too** -- not alphabetical and not
  the order the row was built in. 25 element buckets in `xmlout._ATTR_ORDER`, derived as a
  topological sort of every PAIR both references were seen to emit and verified as a
  superset of every observed sequence. A tag is `id, filter, tag`; a sort row is `default,
  defaultDirection, descKey, firstCharacterKey, key, title`; a `firstCharacter` bucket is
  `size, key, title`.
- **`titleSort` strips a leading run of PUNCTUATION as well as whitespace, before the
  article.** The set is not a Unicode category: in ASCII only `" ' ( - . [ { ~`, and outside
  it every character from U+0080 to U+00BF and from U+2000 to U+2FFF -- while U+0100..U+1FFF
  and U+3000 upwards stay, so the CJK and fullwidth brackets are kept. The run may mix with
  whitespace and repeat, and a title made of nothing else keeps its own text.
- **A show's `/allLeaves` -- and no other container -- stamps a leaf's BORROWED artwork with
  the LEAF's own `addedAt`** (`grandparentThumb`, `grandparentArt`, `grandparentTheme`,
  `art`). Everywhere else they carry the show's CURRENT version. Same family as the `studio`
  that list already stamps: a property of the LIST, not of the row.
- **The localiser reaches INSIDE a row for exactly two blocks**: the `Preferences` block's
  `label`/`summary`/`enumValues`, and a `Stream`'s language fields -- where the two display
  titles are COMPOSED from the localised name and the omission is decided on the ENGLISH one,
  so a track titled "English" keeps `English (SRT)` in German while its plain title moves.
  The row's own `title`, `summary` and tags stay English.
- **A save a real PMS REFUSES writes NOTHING.** Validate the whole form before applying any
  of it: plexsim applied each parameter as it validated it, so a 500 left half the edit on
  disk while telling the client it had failed.
- **The ITEM edit path's 400 means "this request edits nothing"**, not "an unknown field":
  with a real edit beside it every unrecognised name is IGNORED, and each alone is a 400.
  The SECTION path answers 200 to a form that writes nothing (a bare field is a filter
  there) and still 400s an unknown `.value`.
- **A library preference write moves the SECTION's `updatedAt`**; an INT pref that does not
  parse is stored as **0** (not ignored); and a TEXT pref is stored percent-escaped for `:`
  and `|` -- the `enumValues` delimiters -- with a value carrying `%` stored EMPTY.
- **The collection ITEM paths take one verb each** (`/items` PUT plus its own GET/HEAD,
  `/items/{rk}` DELETE, `/move` PUT) and 404 the rest; a non-numeric ratingKey on the DELETE
  is a 404 where an unknown number is a 200. The MOVE refuses `after=<itself>`, an empty
  `after=` and an unparseable one with a 400, sends an `after=` naming no member to the
  FRONT, and works through the `/children/{rk}/move` alias. A comma-joined add TOLERATES the
  members it cannot use; only a single-key uri of the wrong type is a 400.
- **A collection's `/children` on the METADATA path is the LIGHT row**, with the
  librarySection triple on the CONTAINER, `allowSync: true` and no `thumb`.
- **A local artwork candidate names a server path**: `ratingKey` the bare uri, `key` and
  `thumb` `/library/metadata/{id}/file?url=<encoded, `.` as `%2E`>`, which really serves the
  image. A `media://` or `upload://` candidate carries no `provider`. `DELETE .../thumb`
  clears the poster like `/poster`, and an artwork UPLOAD moves `updatedAt` onto the new
  stamp exactly as a PICK does.
- **Twelve butler tasks report a different sentence than plexsim did**, three report
  SEVERAL and five report one where it reported none; `CleanOldBundles` steps its activity
  SUBTITLE through four named phases and `OptimizeDatabase` is titled `Optimizing database`
  with the subtitle `Optimizing Database`. And **an activity's subtitle is the row's FRAME
  title** -- a season's `<show> S01`, an episode's `<show> S01 E01` -- not `title (year)`.
- **`POST` on an item ACTION leaf is a 404** (`/refresh`, `/analyze`, `/unmatch` take PUT),
  while `POST /library/sections/{id}/refresh` is a 200; `/match` answers EVERY verb with its
  no-parameters 400; `?force=` on a section refresh is PARSED; a KNOWN single-segment path
  (`/butler`, `/devices`) falls through to the plug-in dispatcher for the verbs it does not
  implement; and `GET /livetv` is that dispatcher's three-row container, not an empty one.

- **A title the SERVER generated is stored NOWHERE.** A season's `Season N` and an
  unmatched episode's `Episode N` have an EMPTY stored sort title -- `titleSort=` (a bare
  empty value, an equality here) answers 740 of zen's 741 seasons and 4,630 of its 9,287
  episodes, `titleSort<<=0` the same set, and every LETTER bucket of a season
  `firstCharacter` listing answers none while `#` answers them all -- and it is in no
  search index either: the copy's `Season 2` answers nothing to `search?type=3&
  query=season` while the same season renamed answers at once, and zen's
  `type=3&title=Season` is 0 of 741. An agent-supplied `Season 1`/`Episode 1` (kids'
  season 7, 176 of zen's episodes) IS stored, and nothing on the row tells the two apart.
- **Eleven Latin-1 characters fold in `titleSort` that NFD cannot reach**, swept
  character by character over U+0080..U+02FF: `Æ→E Ð→D ×→x Ø→0 Þ→P ß→s æ→e ð→O ÷→/ ø→0
  þ→p`. `Ø`/`ø` become the DIGIT ZERO and `ð` a capital `O`, so it is a table and not a
  transliteration; zen witnesses two of them (`Død Kalm` → `D0d Kalm`). Nothing above
  U+02FF folds.
- **The bare `title=` matcher also reads the row's ORIGINAL title** -- and only it does:
  `title=the` is 1,484 of zen's 3,887 where the title alone explains 1,481. `title!=`,
  `==`, `!==`, `<=` and `>=` read the title alone.
- **On an UNLISTED string field (`titleSort`, `summary`, `tagline`, `originalTitle`) the
  four ordering keys really ORDER**: `<=` ≡ `<<=` and `>=` ≡ `>>=`, case-insensitive
  lexicographic, both STRICT -- a row whose value IS the query is in neither set. They are
  begins-with and ends-with only on an ADVERTISED string field.
- **`/library/sections/{id}/search?type=N` types its container from the ASKED type**, not
  from the library: `type=3` is `viewGroup=season` and `viewMode` **65593**, `type=4` is
  `viewGroup=episode` with `mixedParents`, and a MOVIE library answers both. The section
  GRID is the other way round and keeps the library's own type.
- **`All <library title>` on a movie library, the constant `All Shows` on a show one** --
  in both places the string appears, the `/all` container's `title2` and the `all` pivot
  row. Not localised.
- **Three endpoints, three meanings for `limit=`.** `/library/recentlyAdded?limit=N` caps
  AND rewrites `totalSize` (0 answers none, a negative value lifts the 50-row cap, an
  unparseable one is ignored); `/status/sessions/history/all` caps too and `-1` there
  answers NONE; a section listing's `limit=0` answers ONE row and an unparseable one is a
  500. `group=` on the history page is a 400.
- **`parentIndex=` and an out-of-enum `resolution=` refuse on `/library/all` and
  `/playlists`** as well as on a section listing.
- **`/hubs`, and a SHOW library's `/hubs/sections/{id}`, 500 fifteen filter keys**
  (`duration lastViewedAt lastRatedAt viewCount skipCount viewOffset userRating unwatched
  resolution hdr mediaSize file parentIndex slug group`) for any non-empty value and
  whatever operator; an EMPTY value and an unknown field are 200s, and a MOVIE library's
  hub page takes all fifteen.
- **`/system` PAGES** -- `totalSize` FIRST, no `offset`, and the response headers -- where
  `/system/scanners/{N}` ignores paging; the plug-in framework's container is ordered
  `[totalSize,] noHistory, replaceParent, size, identifier`. **`/system/search` is a real
  empty container** carrying `prefsKey`. And the whole `/system` subtree has its own
  envelope: **PATCH is a 405** (the only one on the surface), a bare OPTIONS an empty 200,
  every other verb an EMPTY-bodied 404.
- **Four more single-segment paths are the plug-in dispatcher's POPULATED listings** --
  `/media` (grabbers, providers, subscriptions), `/services` (ten), `/status` (sessions),
  `/statistics` (empty) -- and `/security` is a real empty container with NO `content` key.
  **A KNOWN single-segment path answers the dispatcher for every verb it does not
  implement**, `PUT /identity` and `PUT /hubs` included; the real writers are `POST
  /playlists`, `POST /playQueues` and `POST`/`DELETE /butler`, and `/log` refuses on its
  parameters first.
- **The playlist item paths take one verb each**, like the collection's: `/playlists/{id}`
  and `/items` GET/HEAD/PUT/DELETE, `/items/{iid}` DELETE (PUT is a 400, a non-numeric id
  a 404), `/items/{iid}/move` PUT. **`DELETE /library/metadata/{playlist}` is an empty 200
  that deletes NOTHING** where `DELETE /playlists/{id}` is a 204 that does.
- **A repeated `id=` on the section save path edits nothing** (the comma list edits both);
  **`POST` on `/split` and `/merge` is a 404** like the other action leaves; **a write to
  `/library/parts/{id}` resolves the part FIRST** (an unknown or malformed id is a 404
  whatever the parameters); and **`/subtitles` answers 404 to DELETE, PATCH and OPTIONS**.
- **A library DELETE pushes one `state 9 deleted` frame per row** -- `type: 12`, an EMPTY
  title, `sectionID` the NEGATIVE section id -- between the 10% and 40% activity updates.
- **`section_prefs_new.json` is per VERSION too** (the fourth such fixture): on 1.42.2 the
  AGENT's settings are appended whatever the `type=`, and 1.43.4 adds five generic rows to
  `tv.plex.agents.none` at types 1 and 2. Every setting's own attributes are identical.
- **An item's `audioLanguage`/`subtitleLanguage` enum is led by the ITEM's own stream
  languages** -- bare 639-1 tags, audio first then subtitles, each named in its own
  language -- before the generic regional list.
- **A check that asserts an ERROR status cannot tell a refusal from a crash.** Cycle 18's
  hub refusal passed its own fail-then-pass proof while raising a `NameError`, because a
  crash is also a 500. Read something back afterwards.

- **A BOOLEAN filter answers the true set to exactly two requests** -- `field=1` and
  `field!=0`. Every other operator and every other value answers the rows where the
  column is FALSE: zen's `unwatched=1` is 3,866 of 3,887 and its `!=1`, `==1`, `<=1`,
  `>=1`, `<<=1`, `>>=1`, `=0`, `==0`, `!==0`, `=2`, `!=2`, `=`, `=01` and `=true` are all
  the same 21. `!=2` is what rules out "parse the value, then negate".
- **A row with NO value for a numeric column matches NOTHING**, `!=` and `!==` included --
  zen's 742 rated movies split 368/374 across `rating<=7` and `rating>>=7`, so the 3,145
  unrated ones are in neither -- and **`==`/`!==` there are an exact STRING comparison of
  the stored number**: `rating==7` is 0 where `rating==7.0` is the 6 rows `rating=7`
  finds, while `year==2000` is the same 39 as `year=2000`.
- **A `tag`-typed COLUMN takes all eight operators** where a tag-TABLE field takes two.
  `contentRating`'s `<=`/`<<=`/`>=`/`>>=` are a BYTE-ordered comparison over the stored
  string (`>=PG` 802 + `<<=PG` 3,085 = 3,887), an absent value compares as `""`, `=` is
  case-SENSITIVE and `==` is not. **`resolution` is the opposite**: every operator, `!=`
  included, answers the `=` set -- and `resolution=` matches ANY of a row's Media.
- **The section `Meta`'s `active` follows the REQUEST**, not the library: `?type=3` marks
  `season` and `?type=4` marks `episode`. **`?includeDetails=1` honours
  `includeAdvanced=1`** (9 sorts bare, 15 with it), carries no `folder` Type row and marks
  none of them active.
- **A row that HAS children names its child list** -- `key="/library/metadata/{id}/
  children"` on every show and season of both references, where an episode keeps the bare
  item path. The client follows `key` verbatim, so a season carrying the bare path opens
  the season's metadata row instead of its episodes and the page draws nothing.
- **`excludeFields=` strips the field from every CHILD ELEMENT** (`id` off Media, Part,
  Stream, Genre and Guid; `file` off Part; `url` off Image; `value` off Rating), while
  **`excludeElements=Image` and `=UltraBlurColors` are NO-OPS** on both references like an
  unknown name. **`checkFiles=` is validated**: `0`, `1` and `01` pass, everything else --
  an empty value, `2`, `true`, `-1`, `1.5`, a leading or trailing space -- is a 400.
- **`/library/metadata/{id}/extras/all` is a real leaf**, the same container `/extras`
  answers; only `/extras/{n}` is the 404.
- **Four more ordering rules.** A `Meta`'s `Type` rows come before its `FieldType` rows;
  `Common`, `Feature` (`scrobbleKey` first), `Type` and the `/media/providers` library
  `Directory` are element buckets of their own (the last shared `/library/sections`'s and
  is a different order); the rows of `/library/recentlyAdded` and `/library/onDeck` LEAD
  with `allowSync` and the librarySection triple, which `allowSync` on the ROW identifies;
  and a LIGHT row puts `Guid` directly after `UltraBlurColors`, before its tags, where a
  detail row puts it after `Genre`/`Country`.
- **A real PMS's JSON key order IS its XML order** -- the container's, the row's,
  `Media`'s, `Part`'s and the row's child-element keys. `xmlout._split` is the one answer
  both writers ask; ordering only the XML left the two formats disagreeing on every
  response.
- **`sort=duration` and `duration=` read the MEDIA's duration**, not the row's column, and
  on a SHOW or SEASON listing a media column is read from the LEAVES and matches when ANY
  leaf does (zen's `duration<=1200000` and `>=1200000` answer 39 and 483 of 505).
- **The default listing order IS `sort=titleSort` and every tie falls back to `titleSort`
  ASCENDING, in both directions** -- re-measured against zen's own ordering of 3,887 real
  rows, so collation cancels out of the comparison.
- **A library EDIT scans only when the form carries `language` or `scanner`** -- `agent`
  alone, with `name`, or with a `prefs[...]` is a 200 with ZERO frames -- and it REFUSES a
  `type` naming a different type from the library's own. **`?path=` SCOPES a scan**: the
  section's own Location, an empty value and no path scan everything; a parent of it, a
  child of it and a path it does not hold scan nothing (no `progress` rows, the activity
  0 to 100 with an empty subtitle).
- **Three butler tasks push the wrong thing.** `RefreshLibraries` runs a section SCAN per
  library, not a metadata refresh per row; `OptimizeDatabase` starts at 0 with an EMPTY
  subtitle and steps 0/10/40/60 to an `ended` at 100; `CleanOldBundles` has SEVEN updates,
  ending on two `Deleting N metadata bundles` steps at 25 and 50.
- **A show library's scan names the SHOW before its seasons**, in a `progress` row with no
  activity update of its own; **`provider.subscriptions.process` is the one `cancellable:
  true` activity**; and **an analyze pushes NO `backgroundProcessingQueue` frame** --
  plexsim invented one. **`PUT /updater/check` pushes one `update.statechange` frame**
  carrying an `AutoUpdateNotification` row.
- **`DELETE /devices/{id}` and `DELETE /accounts/{id}` are 404s**, for an id the server
  lists and one it does not; **`PUT /activities/{x}` is a 400** where its DELETE is a 404;
  and **`HEAD /:/prefs` is a 200**, the one disagreement in a HEAD sweep over sixty paths.
- **Four settings answer 403 to a PUT** (`PlexOnlineUrl`, `TranscoderH264Options`,
  `TranscoderH264OptionsOverride`, `allowMediaDeletionLanOnly`) even when the value written
  is the one already stored, while **`ButlerStartHour` and `ButlerEndHour` take a WORD**
  with a 200 that stores nothing -- alone among the 38 integer settings.
- **`server_prefs.json` is the FIFTH per-version fixture**: `AllowLegacyAgents` and
  `CLDriver` are 1.43.4-only, and a write to one must be refused as an unknown preference.
- **The Add Library form refuses a missing or unlisted `language`** (the agent's own list,
  case-insensitive), **accepts an EMPTY `location`** and **accepts a NUMERIC `type`**,
  creating a library of that type.
- **`filters=` on a notification channel is a WHITELIST and `all` is not an event type**, so
  a socket opened with `filters=all` receives NOTHING and looks exactly like a server that
  pushes none. Always open with a control write.

- **A season's `titleSort` follows the title it STORES, not its `year`.** 740 of zen's 741
  seasons carry none, and the one that does -- the only one with a year -- carries
  `Egghead`, its agent's arc name, not the `Season 22` it displays. Cycle 7 fitted the year
  rule to kids' season 7, whose `Season 1` came from an agent. And **`Specials` and
  `[Unknown Season]` are generated titles too** (season 0 and a season with no index): no
  stored sort title, absent from `title=` and from the section search box, in the `#`
  bucket.
- **An episode carries `parentThumb` only when the season's poster is the season's OWN.**
  654 of zen's seasons hand it down and its 27 that borrow the show's hand down nothing,
  0 exceptions over 9,293 episodes on both references. `art` is the opposite -- handed down
  borrowed or not -- so it is a property of the FIELD.
- **`viewCount` and `viewOffset` read a row with NO stored value as BELOW every value and
  equal to 0** (`=0` and every `<=`/`<<=` match it, nothing else does); `skipCount` is not
  in the family. **A NULL date matches NOTHING**, on every operator -- the old "a null date
  is before everything" came from fun -- and a date's `!=` really compares.
- **`audioLanguage`/`subtitleLanguage`: `=v` is "SOME stream is v" and every other operator
  is "NO stream satisfies the comparison"** (`!=`/`==` compare equality, `!==` inequality),
  so a row with no stream of that type matches all six vacuously. The 3-letter code
  normalises under every operator. **They do NOT read the leaves**: a show or season row
  answers nothing, where `duration`/`resolution`/`mediaSize` do read them.
- **A show library's media AXES are built from the leaves** (`/resolution`,
  `/audioLanguage`), and **`file=` is a 500 on a SHOW listing** while a season's reads the
  leaves like every other media column.
- **`unmatched` IGNORES the operator**: only the literal `1` selects the unmatched rows,
  under all eight. **On a PARENT row `duplicate`'s two halves OVERLAP** -- each is evaluated
  per leaf, so a show with one duplicated episode and one plain one is in both.
- **`id==<n>` and `id!==<n>` DO compare a number** (overturning cycle 11), and **only
  `titleSort` folds case** among the four unlisted string fields -- `summary`, `tagline`
  and `originalTitle` compare BYTES, so every capital sorts below every lower-case letter.
- **`mediaCount` and `createdAt` are real columns**: `mediaCount` is the number of Media,
  read from the LEAVES on a show or season, and `createdAt` tracks `addedAt`.
  `sort=mediaCount` is a real ordering. Both were recorded in cycle 15 and read off an
  attribute no row carries, so they selected nothing.
- **The parse surface is wider than the filter surface, in three more ways.** `theme`,
  `banner`, `art`, `thumb` and `slug` each parse under every operator with an empty
  `fieldTitle` and are a **500** on a listing; `type<op>` parses (the one Rule with NO
  `fieldTitle` key) and filters on the TYPE NUMBER; `sourceType` parses an integer,
  reports NOTHING, constrains nothing, and any value but `1` adds the default
  `Order: id ascending` block. **`guid` takes only `=` and `<=`, both reported as `<=`**,
  and every other operator is an unknown FIELD.
- **`group=` takes five more keys** -- `originalTitle`, `createdAt`, `mediaCount`,
  `ratingCount`, `absoluteIndex` -- so the group surface follows the FILTER surface rather
  than the Meta.
- **`/unwatched` answers only the library's own top-level type** (and flips its `viewGroup`
  to `episode` for any other), **`/recentlyViewedShows?type=N` is a typed query**, and
  **`/sorts`, `/filters`, `/common` and `/search` 400 every unparseable `type=`**, the
  empty value included. **A section search row carries no librarySection fields.**
- **An EDIT does not move `updatedAt`** -- an artwork write does -- and **an artwork URL's
  version IS the row's `updatedAt`**, on 738 of 738 owned paths across both references.
  Every borrowed reference to it follows.
- **Eleven more columns take an item edit and a lock** (`absoluteIndex`, `updatedAt`,
  `createdAt`, `skipCount`, `viewOffset`, `ratingCount`, `type`, `viewedLeafCount`,
  `contentRatingAge`, `mediaCount`, `extraType`), only `absoluteIndex`'s value being read
  there. **The SECTION path parses every numeric column**: seven are 200/500 and eight are
  a 500 even for a valid number, and four really store.
- **`year` is bounded to signed 32-bit**, and that refusal was the last hole in "a save a
  real PMS refuses writes nothing". **A negative `rating` or `audienceRating` REMOVES the
  field.** **`theme` and `banner` are ARTWORK columns**: the url is discarded and the
  column becomes the row's own versioned path.
- **An episode's inherited `contentRating` is a READ-TIME fallback**: clearing the
  episode's own makes it read the show's at once, and a later show write moves it again.
  **A collection and a playlist take the ITEM path's whole edit validation.**


- **The two forms of `/system/agents` carry different children.** The bare form has a
  `MediaType` row per media type an agent serves and NO `Language`; `?mediaType=N` has
  the Language list and no MediaType. And **that endpoint writes its `Language` and
  `MediaType` as REPEATED JSON KEYS** rather than arrays -- 150 occurrences on zen, 195
  on kids -- so a JSON parser keeps exactly ONE per agent while the XML carries all of
  them. Every other repeated child on every other path is a proper array.
- **A `Feature` element is never self-closed**: all fifteen on `/media/providers` are
  paired empty elements on both references, where `Directory`, `Action` and `Pivot` in
  the same document self-close.
- **A SEASON's `audioLanguage`/`subtitleLanguage` inherit option is `:Series default`**
  -- a season inherits from its SHOW, where a show says `:Account default` and a movie
  has no such setting. plexsim rebuilds that enum at read time and hardcoded the account
  label.
- **An EPISODE's `/tree` is the ancestor CHAIN** -- three `MetadataItem` nodes, the show,
  THAT episode's season and the episode, with no siblings. A season's is the season plus
  its own episodes and a show's is the whole tree.
- **`/hubs/home/recentlyAdded` is populated at `type=1` and `type=2` ONLY.** Every other
  type answers `size 0` on both references, with and without `sectionID`,
  `pinnedContentDirectoryID` and `includeMeta` -- and both hold episodes, so it is not a
  content difference.
- **A `/library/onDeck` row LEADS with `allowSync` and the librarySection triple**, like
  a `/library/recentlyAdded` row; **a section's `/hubs/sections/{id}/continueWatching/
  items` container is `{size, allowSync: true, identifier}` and nothing else**; and **an
  artwork path with an EXTRA SEGMENT after the version is a 404** (a single trailing
  slash and a bad version stay 200s).
- **The per-user-state block sits MID-ROW, before the year, on every row type** --
  `viewCount`, `skipCount`, `viewOffset`, `lastViewedAt`, `userRating`, `lastRatedAt`
  after the ratings and before `year`/`parentYear` on a show, a season and an episode,
  as it already did on a movie -- and **`parentSummary` comes directly after
  `parentTitle`**. Both formats move with it: a real PMS's JSON key order is its XML
  order.
- **A collection and a playlist take EVERY `/:/` state write**, and `rate`, `progress` and
  `unscrobble` store on them as on an item -- the rating survives the unscrobble. They part
  on the SCROBBLE: a playlist's `viewCount` counts up from the 1 it is born with and its
  unscrobble clears it, while a collection's scrobble changes NOTHING (it never carries
  `viewCount` and does not even clear a `viewOffset`), so only the watched half is the
  no-op. Neither reaches a member. The block comes back MID-ROW -- `userRating, viewOffset,
  viewCount, lastViewedAt, lastRatedAt`, after a collection's `ratingCount` and after a
  playlist's `composite` -- on every read but `/playlists/{id}` and the create response.
- **A SHOW library's `/analyze` frames every episode LEAF three times** (analyzing,
  thumbnailing, none), with no activity: the section analyze reads the LEAVES, which is why
  a movie library's fifteen for five movies is the same rule.
- **A collection and a playlist answer the ITEM action leaves**, splitting by ROW TYPE:
  `/analyze` 200 on a collection and 400 on a playlist, `/allLeaves` and `/grandchildren`
  real containers on a collection and 404s on a playlist, `/related` empty on both but with
  the librarySection triple and `allowSync` only on a collection, `/extras` and `/match`
  their 400s, and the artwork writes landing like an item's.
- **`PUT /library/collections/{id}` is the ITEM edit path** -- a form that writes nothing,
  an unknown `.value` and a PREF name are each a 400, and `titleSort.value` with its lock
  stores -- and **`POST`, `PATCH` and a bare `OPTIONS` are 404s** there, on its `/children`
  and on `/library/metadata/{id}` for both list types.
- **Twenty-nine more columns take an item edit and a real LOCK** (and no value), while
  eleven names -- `album artist chapterSource childCount hasChapterVideoStream leafCount
  logo poster role sourceType track` -- are a 400 on both and `hdr10plus` is a 200 that
  stores nothing. Five of the twenty-nine are the 1.43.4-only codec family and are HELD
  OUT: plexscratch is 1.43.4 and zen cannot be written, so 1.42.2 is unmeasurable there.
- **A state write's `key=` is a BARE ratingKey**, not a path: `/library/metadata/17` is a
  404 on scrobble/unscrobble and a 500 on rate/progress. The four then diverge -- rate and
  progress int-parse the key (500), scrobble and unscrobble look it up (404); `/:/progress`
  accepts ANY integer key with a 200 that stores nothing; rate validates its `rating`
  before the identifier and progress validates the identifier before its `time`.
- **`/:/progress?time=` takes a negative value and a leading `+`** -- a negative one stores
  nothing -- while `abc`, empty, `1.5`, `0x10`, `1e6` and a leading space stay 500s.
- **`DELETE /library/sections/{id}/indexes` moves the SECTION's `updatedAt`** and nothing
  else's, and **a `/refresh` moves NO `updatedAt` at all** -- so it re-versions no artwork,
  because cycle 20 established the artwork version IS that stamp.
- **A TEXT library preference is percent-decoded a SECOND time**, then escaped for storage
  with `%`, `:` and `|` in lowercase hex; a second decode that fails stores EMPTY, which is
  all cycle 17's "a value carrying `%` is stored EMPTY" ever was.
- **`PUT /library/metadata/{playlist}/prefs` is a 400**, the item writer's own refusal.
- **An EMPTY response under `/system` carries NO `Content-Type`** -- every 404 on every
  verb and the bare OPTIONS 200 -- where a 404 anywhere else carries `text/html` and a body.

- **`excludeElements=` honours an ALLOW-LIST** -- `Media Part Genre Country Guid Rating
  Director Writer Role Producer` -- and every other name is a NO-OP, `Extras`, `Related`,
  `Preferences`, `Field`, `Chapter` and `Review` included. Cycle 19's "every other element
  really goes" was a two-name sample generalised the wrong way. **`Part` is on the list**
  (it takes the Streams with it and adds `partCount` to the Media), and **the option reaches
  the rows INSIDE the inline `Related` and `Extras` blocks**, not just the top-level row.
- **Five more options carry `checkFiles`'s value grammar** (`skipRefresh`,
  `asyncCheckFiles`, `asyncAugmentMetadata`, `asyncRefreshLocalMediaAgent`,
  `asyncRefreshAnalysis`): `0`/`1`/`00`/`01` are 200s and everything else a 400. **And four
  `include*` options parse their value as an INTEGER** -- `includeExtras`,
  `includeRelated`, `includeChildren`, `includeOnDeck` take `01` -- while
  `includePreferences` compares the string. **`includeRelatedCount=0` EMPTIES the hub**
  (`size 0`, `more true`), where reading `0` as absent falls through to the default of five.
- **A real PMS writes ONE element per line and ends the document with a newline** (236 of
  them in one zen movie, 34 on kids). **Six more names are written PAIRED when empty** --
  `MediaContainer`, `Stream`, `Hub`, `Extras`, `Media`, `Activity` -- while `Part`
  self-closes, so it is an element-NAME table and not "can hold children".
- **Three more attribute orders**: a `Part` leads `accessible, exists`, a section's pivot
  `Directory` leads `secondary` (and `prompt, search`), a `Feature` leads `flavor`.
  `_ATTR_ORDER` is a topological sort of observed PAIRS, so a pair no reference emitted
  falls to the END -- that is its documented failure mode, and these were all of it.
- **A LIGHT row's JSON child order was never the light order**: `json_body` stripped the
  `_light` marker before the orderer read it, so cycle 19's "one answer both writers ask"
  held everywhere but there.
- **A text `sort=` compares RAW CODEPOINTS** -- case-sensitive, no accent folding, so
  lowercase sorts ABOVE uppercase (`iHostage` before `Zero Tolerance` descending). It is
  the SORT side of the split cycle 20 measured on the FILTER side; `titleSort` stays folded
  because no zen row carries a lowercase-initial stored sort title to measure it with.
- **A filter key whose NAME keeps a comparison character is an UNKNOWN FIELD** --
  `collection==`, `year>=`, `title!!` are a 200 listing every row and a 200 `Error` at
  `/query/parse`. A pair with NO `=` at all is still the 400 cycle 11 measured.
  **When you probe an operator, send the client's encoding**: `year>>=2000` is the
  parameter `year>>`, so `collection%3D%3D=zzzz` is the parameter `collection==` and not
  the `==` operator -- a whole finding was wrong on that before it was re-sent.
- **A bare `type=<v>` on the ITEM edit path is an accepted no-op** (`type` is one of cycle
  20's lockable names); it is only on the SECTION path that it is the scoping parameter.
- **An artwork frame carries the LIST row's own type** -- a collection's 18, a playlist's 15
  with `sectionID "-1"`. A stored list row carries no `type`, so a lookup keyed on it
  answers the movie's 1, and the client builds the URL it updates from that number.
- **`/library/collections/{id}/{refresh,unmatch,analyze}` answer an EMPTY body**;
  **`/resources/{anything}` is a real empty 200 on every verb**; **`/search/voice` with no
  parameters is a 400** (a 404 once `query=` is given); **`/search` is a real ROUTED
  endpoint** whose writers are a 400, not the plug-in dispatcher's listing.
- **`PATCH` is a 404 on `/library/sections`, `/status/sessions`, `/media/providers` and
  `/playQueues/{id}`**, and `/accounts/{id}` refuses POST too. **`/system/proxy` is the one
  path in that subtree that answers its own 400 to every verb**, GET included, rather than
  cycle 18's empty-bodied 404.

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- Stdlib only on the server path. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a Real-Debrid `/d/` link, a Plex token, or a tunnel
  credential. The repo is private; that is not a reason to relax this.
