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
answering at `http://100.101.10.1:32410`:

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
  with `Host: 100.101.10.1:32410`, and any `X-Forwarded-For` makes it WAN. So a socat hop
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
  by decade.
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
  equality. `==` is the exact one, and it ignores case. See `filters.py`.
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
  one's metadata, so measuring it costs a re-match. It cost one here.
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
  `hierarchy.normalize_rows` runs at LOAD as well as at build.
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
- **On Deck is one row per show**: the lowest-numbered episode in progress, else the one
  after the last watched; hiding any of its episodes hides the show until its next
  timeline.
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

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- Stdlib only on the server path. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a Real-Debrid `/d/` link, a Plex token, or a tunnel
  credential. The repo is private; that is not a reason to relax this.
