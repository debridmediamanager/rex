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
| `decision.py` | A plain-form decision: the client's own profile file (`client_profiles.json`) against the item |
| `debrid.py` | Real-Debrid unrestrict |
| `gdm.py` | Plex's LAN discovery protocol: the UDP half of being FOUND |
| `gate.py` | Applies a client's direct-play gate to the catalog |
| `xmlout.py` | MediaContainer → Plex XML (clients that refuse JSON) |
| `build_catalog.py` | Builds `catalog.json` from real RD content |
| `claim.py`, `heartbeat.py` | plex.tv claim flow and registration upkeep |
| `tests/` | Conformance and end-to-end suites, plus `catalog_ui.json` and `fixtures/decisions/`, the measured decision corpus |
| `tests/fixtures/` | Redacted corpora recorded from a real PMS through a real client; `media/` pairs real files' probes with a real PMS's analysis of them |
| `harness/` | plexlab — the reference PMS and the recorder. See below. |
| `probes/` | One-off measurement scripts that produced findings |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |

Sidecar fixtures are captured from a real PMS -- the later ones read straight out of its
image -- and loaded relative to `server.py`. Keep the layout flat — the code resolves them
with `dirname(__file__)`. The descriptors (`server_prefs.json`, `regional_server_prefs.json`,
`section_prefs.json`, `section_meta.json`, `section_meta_show.json`, `item_prefs.json`,
`section_prefs_new.json`, `section_filters.json`, `section_sorts.json`, `system_agents.json`,
`legacy_agents.json`, `agent_languages.json`, `languages.json`, `translations.json`,
`client_profiles.json`, `spellfix_static_tags.json`, `legacy_agent_config.json`); the image's own tables
(`plugin_bundles.json`, `plugin_prefs.json`, `attributions.json`, `resource_assets.json`,
`media_flags.json`); and three files served verbatim at the top of the surface with no token
(`crossdomain.xml`, `clientaccesspolicy.xml`, `favicon.ico`). Two runtime files are the
exception to the flat rule: `tmdb_cache.json` and `claim_state.json` are resolved beside the
**catalog**, not beside `server.py`, because they are state rather than measurement.

A movie library and a show library have **different**
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
python3 tests/test_decision.py              # decision.py against 1,872 measured decisions; no server
python3 tests/test_gdm.py                   # gdm.py against four real servers' GDM; no server
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
- **`contentDirectoryID` scopes and VALIDATES `/hubs/search`** -- a value naming no section is
  a 404 there. "Ignored on every other hub path" was REFUTED in cycle 28: it scopes `/hubs` and
  all three `/hubs/home/*` leaves as well, and `/hubs/continueWatching` validates it. See the
  cycle-28 bullet below.
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
  `/hubs/search` as a `tagType: 2` row -- and by `/library/search` too, which cycle 28
  re-measured once the copy held a collection at all (the old "never" was the fixture's).
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

- **A `/search` row is the GRID row with a six-attribute lead** -- `allowSync,
  librarySectionID, librarySectionTitle, librarySectionUUID, personal, sourceTitle` in
  front and nothing else different. plexsim answered the DETAIL row there, which on a real
  library is several times the bytes per hit.
- **A `Meta/Type`'s active SORT follows the asked `type=` AND the `sort=` parameter**: it is
  marked only inside the Type row matching the asked type (a movie library asked `type=2`
  marks NOTHING), and which one is marked is the requested sort -- none given lights the
  default, `sort=addedAt:desc` lights `addedAt` with `activeDirection="desc"`, and a spec
  naming no advertised key lights nothing. Different attribute from cycle 19's `Type@active`.
- **`?includeDetails=1&includeAdvanced=1` carries NO Duplicates Filter row at all.** Cycle
  16 measured that path without `includeAdvanced=1`, where the row does not exist to carry
  a flag, so "carries no `advanced`" was the absence of the FLAG, not of the ROW.
- **A `/library/tags` row ends with `thumb`**; **the `/hubs/sections/{id}/manage` Hub leads
  with `identifier` and SELF-CLOSES** -- so `_ALWAYS_PAIRED` is not a flat name list for
  `Hub`: it is paired when it carries a child collection and self-closed when it carries
  none, the rule cycle 22 left open for `Directory`. **A `Common` row leads with the
  GRANDPARENT pair** and puts the parent pair before `ratingKey`.
- **The `_xml=` override has to reach the JSON writer too.** `_send_container` POPS it
  before either writer runs, so `/library/sections/{id}/common` bucketed as `Video[common]`
  and its JSON keys stayed in build order -- the last path where plexsim's two writers
  contradicted each other, which a real PMS never does (re-confirmed over 1,241 pairs).
- **A MOVIE's unmatch pushes TWELVE frames**, opening on `2/matching` then `3/matching` --
  a pair plexsim emitted nowhere -- with an activity that steps through a second TITLE,
  `Analyzing Media`, before returning to `Updating Metadata`.
- **A collection and a playlist answer `/refresh` and `/unmatch`** with their own sequences,
  and a collection's refresh is GATED ON ITS GUID: three frames on an agent guid, eight once
  an unmatch has given it a `local://` one. A collection's `/analyze` is the activity PAIR
  alone -- its members are never framed.
- **Three more read-only routes refuse every other verb**: `/hubs/home/recentlyAdded`,
  `/hubs/sections/{id}` and `/library/metadata/{id}/children`.
- **`xmlout._CHILD_ORDER` holds against a real library** -- 203 ordered pairs witnessed
  across 2,994 paths, zero contradictions, on rows carrying `Chapter`, `Marker`, `Review`,
  `CommonSenseMedia`, 155 roles and 5 Media. Do not re-sweep it without a new row shape.

- **The extended title's LANGUAGE part has its own rule**, settled at the third attempt: the
  name is carried only when it is a SINGLE token that is one of the title's words, with any
  TRAILING PARENTHESISED GROUP stripped from the name first. `Русский` in `Русский (Full) -
  ...` and `English (Weeb)` in `English (Some-Stuffs)` are carried; `norsk bokmål` and
  `Tiếng Việt` in their own titles are not. Cycle 22's whole-name rule broke the recorded
  corpus and the first-word rule alone missed 179 of zen's rows -- **a model fitted on one
  corpus must be scored on the other before it is written.**
- **Five more stand-ins**: `Signs`/`Songs` for Forced, `CC`/`HI` for SDH, `1.0` for Mono,
  `HDR`/`Dolby` for the dynamic range, and the RAW ffprobe codec name with punctuation
  folded (`aac` carries `HE-AAC`, `H264` carries `H.264`). **`eia_608` is labelled
  `Closed Captions`**, and **a sidecar's codec suffix is its `providerTitle`** with
  `External` the fallback -- cycle 17 measured the fallback alone, on one server.
- **`AC-3` in a title carries `AC3` and is NOT modelled** (132 rows): `VC-1` does not carry
  `VC1` and `E-AC3` does not carry `EAC3`, so no punctuation rule fits and a general one
  breaks 58 other streams. Recorded, not fitted.
- **`languages.json` was missing thirteen codes** zen's streams use (`cha guj hye kat mar
  mdr mon mya nno pan sin yor zul`). **A VIDEO stream carries `dub`** and never `comment`
  or `hearingImpaired`. **A layout ffprobe cannot name** (`8 channels (FL+FR+...)`) must be
  stripped of its trailing space.
- **zen's stored `displayTitle` carries the codec and plexsim's catalog form does not** --
  that is the recorded CLIENT-TITLE rule (those rows are read as Plex Web), not a defect.
  Score `mediainfo` on the extended title, which is the catalog form.

- **A SEASON's unmatch pushes ONE `matching` frame and a SHOW's the movie's PAIR.** The
  season then runs the SEASON cascade (28 frames, 9 of them the state-dependent media
  pass) where plexsim sent the seven-frame EPISODE form; the show runs 46 frames of its
  own where plexsim sent NONE. The show's activity is the MOVIE unmatch's -- `Analyzing
  Media` at -1 and 25, then `Updating Metadata` at 25 and 50 -- and only then does the
  subtitle follow each season. **A season cascade's activity has no `updated` frame**;
  the docstring said so since cycle 15 and the code called `finish_activity`, which adds
  one.
- **A show UNMATCH's activity opens with the show's BARE TITLE**, where its REFRESH opens
  with the frame title (`Firefly (2002)`) -- the one place on the surface where an
  activity's subtitle is not the frame title. From the season onwards both use it again.
- **`pixelAspectRatio` and `anamorphic` are a PAIR** -- 358 of zen's video Streams carry
  both, 1,330 neither, none only one. The value is ffprobe's SAR verbatim, present exactly
  when the sample aspect is not square, and `anamorphic` is TRUE when the ratio rounded to
  ONE decimal place is not 1.0. Sixteen distinct ratios witness it; the boundary is
  bracketed to (0.0406, 0.0667] and 0.05 is the only round number in it. A pixel-
  difference model is refuted: a 3840-wide `333:320` displaces 156 pixels and is false
  where a 716-wide `16:15` displaces 48 and is true.
- **The resolution label had no 480 WIDTH bucket**, so a wide short SD release fell to
  `sd`: zen's three 856x358 Media are `480`. The entry is the smallest width zen has in
  the bucket, as the other five are; 720 is the largest below it and the boundary is not
  bisected.
- **A search hit LEADS with `librarySectionTitle`**, before `ratingKey`, on `/hubs/search`
  (where its own `score` follows it) and on `/library/search`. It needs a marker, not a
  signal read off the row: `/library/all` and `/hubs/home/recentlyAdded` carry the same
  attribute in the middle.
- **`/match` answers its no-parameters 400 to EVERY verb**, HEAD, PATCH and a bare OPTIONS
  included -- cycle 17 recorded the sentence and implemented two of the seven.

- **Only THREE identity headers move anything at all**: `X-Plex-Platform`,
  `X-Plex-Product` and `X-Plex-Language`. `X-Plex-Features`, `X-Plex-Client-Profile-Extra`,
  `X-Plex-Drm`, `X-Plex-Model`, `X-Plex-Provides`, `X-Plex-Session-Identifier`,
  `X-Plex-Client-Identifier`, `X-Plex-Device`, `X-Plex-Device-Name`, `X-Plex-Text-Format`,
  `X-Plex-Version`, `X-Plex-Platform-Version`, `X-Plex-Sync-Version` and `X-Plex-Protocol`
  each sent alone move NOTHING on ~80 paths -- not a Media, Part or Stream row, not the
  XML/JSON default, the gzip decision, the paging defaults, a response header or a hub.
- **`X-Plex-Platform: Samsung` appends a hidden `includeInGlobal` to a library's `/prefs`**
  -- that exact spelling, header or query (the HEADER wins when both are sent), both
  library types, and the EXISTING-library leaf alone. The second platform-keyed reshape
  after `Roku` on `/media/providers`.
- **A modern agent's `Language` rows are TITLED and ORDERED per language** (45 rows, all
  six modern agents sharing one list). The ORDER moves with the titles, so the list is
  stored per language (`agent_languages.json`, read from the XML -- the JSON writes the
  rows as repeated KEYS) rather than translated string by string. The NO-HEADER default is
  NOT English: each language is named in ITSELF, and an UNRECOGNISED language falls back
  there rather than to English, alone on the surface.
- **`/media/providers?includePreferences=1` carries each library's whole `Preferences`
  block**, byte-identical to `/library/sections?includePreferences=1`'s -- the per-SHOW
  `episodeSort` variant included -- as the FIRST child of the library `Directory`. The
  `Home` row, which names no library, gets none.
- **`/library`'s three `Directory` titles are localised and `/system`'s identical string is
  not.** The plug-in framework's container (`noHistory` is its signature) is not localised
  at all, which is why the translation table's attribute key is not enough on its own.
- **A `/tree` node is a ROW and its `Setting` rows stay English**, where the same settings
  under `?includePreferences=1` move. It is keyed by `id` rather than `ratingKey`, so the
  row guard did not see it and plexsim translated what a real PMS leaves alone.

- **Continue Watching is NOT On Deck.** `/hubs/home/continueWatching` holds only the rows with
  a stored `viewOffset` -- movies and episodes alike -- where On Deck holds one row per show,
  its in-progress episode or the one after the last watched, which has no offset at all.
- **Home DROPS the `home.ondeck` shelf when its rows are Continue Watching's** -- the element,
  not an empty one, on `/hubs` and `/hubs/promoted` alike -- and only when that set is
  NON-EMPTY. Cleared: both present. Next-up only: both. One episode in progress: On Deck gone.
  A movie in progress beside it: both again.
- **A SCROBBLE writes one history row per LEAF**, overturning cycle 9: a movie scrobbled twice
  adds two, a season adds one per episode, a show one per leaf, and an UNSCROBBLE removes
  none. The timeline half writes no row on the copy, which is its recorded session-bound limit
  and not a rule.
- **An episode history row carries `title` and `originallyAvailableAt`**, in an order of its
  own: `historyKey, key, ratingKey, librarySectionID, parentKey, grandparentKey, title,
  grandparentTitle, type, thumb, ..., index, parentIndex, originallyAvailableAt, viewedAt,
  accountID, deviceID, viewType`. A movie's row is a subsequence of it.
- **A PLAYLIST is on Home's Recent Playlists shelf and in both search endpoints** -- the
  `/playlists` listing row, `lastViewedAt:desc`, `hubKey` naming its own rows. A COLLECTION is
  found as a `tagType: 2` tag row instead, which is a different shape.
- **The UNTYPED `/library/all` lists collections**, byte for byte the row `?type=18` answers,
  sorted in by ratingKey. The section form lists them on neither server.
- **`removeFromContinueWatching` does NOT reach a SECTION's own Continue Watching shelf.** It
  empties Home's On Deck, Home's Continue Watching and the section's `/onDeck`, and the row is
  still in `/hubs/sections/{id}/continueWatching/items` afterwards -- stable to +20 s.
- **A `Common`'s `userRating` is a STRING** (`"8"`, not `8.0`) and sits directly after
  `mixedFields`, ahead of `guid`, while the ITEM row keeps the number.


- **A decision WITHOUT `hasMDE=1` runs the client's profile and can be a DIRECT PLAY**:
  `directPlayDecisionCode 1000` / `generalDecisionCode 1000`, both "Direct play OK.", with the
  Metadata row and `resourceSession` and NO transcode codes. The row is byte-identical to the
  MDE form's; only the container's codes differ, so "a direct play is only
  `mdeDecisionCode: 1000`" is the MDE form's rule. Measured: `Roku` and `Xbox One` direct play,
  `Chrome`/`Safari`/`Chromecast`/`Plex Desktop`/`Konvergo`/`HTML TV App`/`Opera` get `1001`
  (which plexsim cannot answer), and `Firefox`/`Microsoft Edge`/`Generic`/`Plexamp` get the
  `3000/2000/4005` "neither".
- **Only an OUT-OF-RANGE `mediaIndex` refuses a decision.** A missing, non-numeric, negative or
  empty one falls back to media 0; `partIndex` is never refused at any value; a `path` carrying
  a query string is a 200. This CORRECTS the older bullet that claimed all three -- and note
  neither reference holds a multi-part Media, so the `partIndex` half is measured on
  single-part rows only.
- **A decision answers exactly ONE Media** (zen's rk=401 has fifteen), and **`excludeElements=`
  is a NO-OP on a decision** -- the one exception to "every container that carries rows honours
  it".
- **A statistics `timespan` the server does not KEEP is an EMPTY 200**, not the nearest bucket:
  bandwidth keeps `0 1 2 3 4 6`, media `0 1 2 3 4`, resources `6`, and 5, 7, 99, -1 and 007
  answer `size 0`. `06` parses as 6 on all three.
- **`limit=` caps a statistics series and is PARSED** (`1`, `01`, `0`, `-1` are 200s and a
  non-positive one answers no rows; `1.5`, `abc`, empty and `+1` are 400s) **even where the
  value does nothing**, and **`sort=` is a real ordering validated per endpoint** --
  `metadataType` is NOT a media key.
- **The statistics `at` family is four operators**: `at>` means `>=`, `at<` means `<=`, `at=`
  is exact, and `at>=`, `at<=` and `at!=` are unknown keys that answer every row.
- **Neither `/status/sessions` nor `/statistics/{which}` is a PREFIX.** An extra segment after
  either is a 404 -- `/status/sessionsfoo`, `/status/sessions/1`,
  `/status/sessions/history/all/zzz` and `/statistics/bandwidth/zzz` included.
- **A decision arms one SESSION, not the item.** The timeline's `Bandwidths`, the session row's
  `Media@selected`/`Part@selected`/`Part@decision`/`Stream@location` and the transcode start's
  gate all key on `(session, ratingKey)`: a second session beating the same row is unarmed,
  the same session beating another row is unarmed, and only its own `stopped` beat disarms it.
  The identity is `X-Plex-Session-Identifier` (query or header) then the client identifier --
  **NOT** the decision's `session=` parameter, which names the transcode session.
- **A transcode START from a session with no decision is a 400**, on every protocol and every
  leaf of the family; a transcode decision for that session turns the same `start.m3u8` into a
  200. plexsim has no encoder, so its answer once a decision exists is its own 404 -- recorded,
  not modelled, because the reference's own `start.mpd` is a 404 there while its `start.m3u8`
  is a 200.
- **A decision row keeps its `summary`**, whatever the row type. The rule that dropped it for
  movies was fitted to the one copy row whose summary is the EMPTY STRING.
- **A list's `/unmatch` really writes**: a collection's `guid` becomes `local://{rk}`, its
  `ratingCount` is REMOVED and its `subtype` becomes `unknown`; a playlist's `guid` becomes
  `local://{rk}` and its `duration` is removed. The per-user state block keeps the slot
  `ratingCount` vacated. **A collection's refresh/unmatch activity ENDS at 100**, after an
  `updated` at 49. Beware: `_collection_row`/`_playlist_row` re-mint the guid on every read, so
  a write into the row the handler was handed reaches the FRAME GATE and never reaches a client.
- **`removeFromContinueWatching` is per LIBRARY TYPE**: a show library KEEPS the row in
  `tv.inprogress.{id}` and in its see-all list, a movie library REMOVES it from both. The shelf
  and the See All behind it always agree. Home's On Deck drops it either way.
- **The butler's wrapper steps once per task** -- `updated 100` for the task just finished then
  `updated round(100*n/(n+1))` for the queue, no step after the last, so N tasks push N
  hundreds and N-1 steps before `ended 100`. **`CleanOldBundles` INTERLEAVES its two messages
  with its own phases** and **`RefreshLibraries` reports and closes the wrapper BEFORE it
  scans**. `POST /butler` runs only what is DUE, which no row of `GET /butler` exposes, so
  plexsim runs every enabled task; a second POST is a full run, not one `queueRegenerated`.
- **A session with no beat lives THREE MINUTES**, not seventy seconds: measured ten times on
  the copy, gone between +184.0 and +210.4, which is a cutoff at or below 184 s plus a reaper
  tick of at least 26 s. `SESSION_TTL` is the cutoff; the tick is recorded, not emulated.
  Cycle 9's 70 came from ten-second clips before `long` mode existed.
- **`/status/sessions` is written by a DIFFERENT serialiser.** Its attributes come out
  ALPHABETICALLY -- the row, its `Media` and its `Part` -- with the decision flags after, where
  `xmlout._ATTR_ORDER` governs every other container; in JSON every child element's `id`, a
  `Media`'s `aspectRatio`, a `Rating`'s `value` and the row's `librarySectionID` are STRINGS
  where the detail row sends numbers, and `UltraBlurColors` is a LIST of one. Nothing else
  changes type. Cycle 22's 2,994-path order sweep could not see it: reading this path needs a
  live session, which a read-only reference cannot be given.


- **The URL is bounded at exactly 32768 bytes** and 32769 is a 400 -- the URL, not the request
  LINE, since the boundary does not move with the method. **The header caps are 1024 and 4096
  bytes and 100 LINES**, all three bisected: the third is a COUNT, not a size, so 200 KB of
  headers in fifty lines is a 200 and the old "~41 KB total" was that cap measured with
  410-byte headers. Every one of those refusals is a plain **400**; a real PMS sends `414` and
  `431` nowhere.
- **The request LINE is `METHOD SP target SP HTTP/<digit>.<digit>` with CRLF, and nothing
  else.** Two spaces, a trailing space, tab separators, asterisk-form and LF-only endings are
  each a 400; the two version digits are unconstrained, so `HTTP/2.0` and `HTTP/9.9` are 200s
  where `HTTP/1` is a 400; an HTTP/0.9 line is answered with a FULL HTTP/1.1 response; and a
  LOWER-CASE method gets no response at all.
- **An unparseable `Content-Length` is IGNORED and a negative one is a 500.** `abc` is a 200
  with no body read. Reading it with a bare `int()` kills the handler thread and the client
  sees a server that hung up.
- **`/photo/:/transcode` validates five more parameters.** `minSize` and `upscale` are BOOLEANS
  (`0`, `1`, `01`, `+1` only); `opacity` and `quality` take any integer; `blur` takes any
  integer and 500s a negative one; anything that does not parse as a whole number is a 400. The
  `width`/`height` 500s for a scale that degenerates are the image pipeline's, not a rule.
- **Every tag element inside a `/status/sessions` row is plainly ALPHABETICAL**, `Role`
  (`filter, id, role, tag, tagKey, thumb`) included -- there is no `actor` attribute on one.
  **`Player` is the one element there with an order of its own**: its string-valued attributes
  alphabetically, then `local, relayed, secure, userID`.
- **`Player.profile` is the profile the server RESOLVED, not the constant `Web`.** 23 of the 78
  profile names report another profile's NAME (`Roku` -> `Roku-7.x`, every browser but Firefox
  and Edge -> `Web`, `Plex Home Theater-RaspberryPi` -> `Raspberry PI`, a name in no file
  list). The resolution is `X-Plex-Client-Profile-Name`, `X-Plex-Platform`, `X-Plex-Device`,
  first match wins, an unmatched value falls THROUGH, header before query per name, and the
  attribute is omitted when nothing matches.
- **A borrowed artwork field is DERIVED from its owner, so every write to the owner re-derives
  it.** Clearing a row's own poster leaves it carrying the PARENT's path (a season falls back
  to the show, an episode to the season); a pick on a season CREATES `parentThumb` on episodes
  that had none; a pick on a show moves a BORROWING season's own `thumb` and every episode's
  own `art`; and when the owner's field goes, the borrower's goes with it.
- **A pick re-versions only the artwork the row OWNS.** A borrowed field keeps the owner's path,
  stamp and all -- zen's episode 11115 carries `art` naming its SEASON beside a `grandparentArt`
  naming the show at a different stamp. Only the field being picked becomes the row's own.
- **`durationTotal` and `storageTotal` are JSON STRINGS**, where every other number on that row
  is a number. XML cannot tell the two apart, which is why it took a JSON read on both
  references to see it.
- **The bare `/system/agents` carries a MODERN agent's 45 `Language` rows and no `MediaType`.**
  The `MediaType`-in-the-bare-form rule is the LEGACY plug-in agents' alone. A modern `Agent`
  row leads `identifier, name, hasPrefs, id`.
- **`?includeDetails=1` pages its `Directory`, `Type` and `FieldType` rows as ONE sequence** --
  20 + 1 + 8 = 29, so a 20-row window is `size=20 totalSize=29` and `Start=20` answers the meta
  rows. **A `Sort` row leads with `active, activeDirection`** and a **`Hub` puts `random`
  before `style`**.
- **The SINGLE history read is a third projection with an order of its own** (`historyKey,
  librarySectionID, title, ...`), not the listing row's and not an item row's.
- **Three more empty elements are written PAIRED**: a `Meta/Type` with no children, an empty
  `Related`, and the `/media/providers` `Home` `Directory` -- the one childless `Directory` on
  the surface that is paired, which `hubKey` identifies.
- **A real PMS ignores every conditional request header.** A part read carries an `ETag` and a
  `Last-Modified` and answers 206 to `If-None-Match`, `If-Modified-Since`, `If-Range` and
  `If-Match` alike -- never 304, never 412.
- **`/library/all/top` answers any type NUMBER** -- 0, 3, 8, 9, 10, 12, 13, 15, 18, 20 and -1
  included -- and 400s only a value that does not parse. The Dashboard asks for movie, show and
  ARTIST in one `all([...])` under one try/catch, so refusing the music type fails the whole
  Top Played panel.
- **`/library/all/top` breaks a tie on the MOST RECENT view, descending**, then on the
  ratingKey. The time is the history's own; the row does not carry it.
- **`/media/providers/metadata` and `/media/providers/metadata/group` are real endpoints** -- the
  MODERN agent-provider tables the Settings Agents pane reads, a different surface from
  `/system/agents`. A provider carries one `MetadataType` child per media type it serves and
  `tv.plex.agents.none` an EMPTY `uri`.
- **Every PERSON tag carries a 24-hex `tagKey`** -- `Director`, `Writer`, `Role` and `Producer`
  on a detail row, and `/library/tags?type=4|5|6|7` -- where `Genre` and `Country` carry none.
  The client builds every Cast & Crew link as `/library/people/${tag.tagKey}`, so a tag without
  one links to `/library/people/undefined`. plexsim MINTS the value (sha1 of the name) and
  resolves the minted key on `/library/people/{key}` as a real PMS resolves both forms.

- **`contentDirectoryID` scopes `/hubs` and all three `/hubs/home/*` leaves** -- and on
  `/hubs` it also REMOVES the `home.playlists` shelf. A comma LIST selects several (the form
  Plex Web sends, joining its pinned array), an EMPTY value scopes nothing, and the match is
  an exact STRING, so `01` selects no section. Cycle 15's "`/hubs/search` alone" is true of
  `/hubs/sections/{id}` only. **`/hubs/continueWatching` and its `/items` leaf VALIDATE it**:
  one comma member must be a POSITIVE INTEGER (`01`, `999`, `abc,1`, `1,` pass; `abc`, `0`,
  `-1`, `1.5` and a space are 404s) -- the SHAPE, not whether the section exists.
- **`excludeContinueWatching` is true for the integer 1 alone** (`1` and `01`) and drops BOTH
  `home.continue` and `home.ondeck`, on `/hubs` and `/hubs/promoted` alike. No other
  `exclude<Name>=1` spelling does anything.
- **A shelf's rows are the HEAD of its own `key` listing** -- 29 of 32 non-empty shelves byte
  for byte at `count=40` -- and the three exceptions are the `random="1"` ones, which are a
  random SAMPLE of the whole key set in random order. plexsim's Recently Watched shelf
  published `unwatched=0&viewOffset=0` and honoured neither. A NEGATIVE `count` is
  `len(rows) + count` clamped at 0 and says `more: false`, where `count=0` says `more: TRUE`.
- **`/hubs/home/recentlyAdded?type=2` is a MIXED, ROLLED-UP list**, capped at 50: two or more
  recently-added siblings collapse into their parent, recursively (one new episode stays an
  EPISODE, two become the SEASON, two new seasons become the SHOW), and a parent row is dated
  by its NEWEST leaf -- the opposite aggregation from `/library/recentlyAdded`, which dates a
  season by its earliest.
- **On Deck answers the DETAIL row on 1.42.2**, on all three paths, `librarySectionKey`
  included, plus `allowSync`, `librarySectionUUID` and `lastViewedAt` on `/library/onDeck`
  (the home shelf carries the key and no UUID). 1.43.4 drops the key, `Guid`, `Rating` and
  `Producer` and stamps `studio`: the SIXTH per-version shape. Cycle 21's "light, named by
  UUID" was read off a server with nothing in progress.
- **`/clients/profiles` is the client PROFILE catalogue**, not the hosted-client list: 78 rows
  bare and at `type=0`/`00`, SEVEN at `type=42` (`LOCAL` in the client's own enum) -- Android,
  iOS, Universal Mobile, Universal TV, Windows Phone, Windows, Xbox One -- every other value a
  400, an extra segment a 404, a trailing slash a 200.
- **The `/collections` leaf is the `/all` listing with a default `type=18`** and runs the same
  query: a `type=` the library holds answers those rows instead, an unknown `sort`, an
  unparseable `type` and an unparseable `limit` are each a 500, and `subtype`, `smart` and an
  empty `sort` are accepted and ignored.
- **A search tag hit is per LIBRARY, not per NAME** (kids' `drama` is two `Drama` rows against
  one tag id; zen's `comedy` carries 590 and 179), and its `count` is the size of the listing
  its own `key` names -- the library's top-level type. **A PERSON row carries its 24-hex
  `tagKey`**, after `tagType` and before the `thumb`, with `count` last; a `Genre` row carries
  none.
- **A `/library/search` tag row is the `/hubs/search` row minus `reason`, `reasonID`,
  `reasonTitle` and its own `score`** -- there the score is the `SearchResult` wrapper's alone,
  a NUMBER in JSON and FIVE decimals in XML (`score="0.52000"`).
- **A COLLECTION is found by `/library/search`** too, as a `tagType: 2` row: cycle 15's note
  was a copy that held no collection. And **`contentDirectoryID` scopes every row family** of
  `/hubs/search` -- the tag rows per library, the collections by library, a playlist by its
  MEMBERS' library.
- **`searchTypes=` is `/library/search`'s own filter and `/hubs/search` IGNORES it.** Six
  tokens -- `movies tv otherVideos music photos people` -- EXACT, case-sensitive and never
  trimmed (`Movies`, ` movies`, `zzz` select nothing), a comma list a UNION with unrecognised
  tokens dropped (`zzz,movies` is `movies`), an empty value everything. `collections`,
  `playlists` and `tags` are NOT tokens. `otherVideos` takes the movie library's TAG rows
  WITHOUT its movies, because an Other Videos library is a type-1 library too.
- **`/library/search?limit=` caps the WHOLE list**, not each hub, and refuses twice over like
  `/hubs/search`: a value it cannot parse is a 400, a non-positive one a 500, `01` fine.

- **A row's own artwork field is its NEAREST ancestor's that OWNS one**, and
  `parentX`/`grandparentX` always name that specific ancestor. Clearing a show's `art` removes it
  from every season AND episode (with the episodes' `grandparentArt`) and picking one back puts all
  of them back -- a borrowed field is DERIVED and is RE-CREATED, not moved. Giving ONE season its
  own `art` switches that season's episodes to it while their `grandparentArt` goes on naming the
  show; a later show pick leaves the owning season's own path and stamp alone. Clearing a row's own
  `art` drops it back to the ancestor's, as `thumb` already did, and the bare `/art` becomes a 404.
- **Every artwork write cascades, and both write paths re-version the OTHER field only when the row
  OWNS it.** An artwork UPLOAD cascades exactly as a pick does (plexsim's never called the cascade,
  so children kept the old stamp and a client that caches on it never refetched); a poster upload on
  a row that borrows its `art` leaves that `art` naming the owner, and so does clearing that row's
  thumb.
- **`/photo/:/transcode` validates its DIMENSIONS first and then its `url`**: a missing `url=` is a
  400, an EMPTY one a 404, an item artwork path of a row it holds the image, of a row it does not
  hold a 404, one the row does not OWN a 404 (the transcoder answers what the direct path answers),
  a server path that is not artwork a **500**, and a url with a SCHEME or no leading slash a 404. A
  generated tile there carries `max-age=259200`, so a grey square caches for three days.
- **`/:/resources/` is SEVEN names** -- `movie show artist photo .png` at 300x300 and
  `movie-fanart show-fanart artist-fanart .jpg` at 1280x720, byte-identical on both references, so
  they are PMS's own bundled assets -- and `library-art.png`, `playlist.png` and `album.png` are
  **404s** even though a real PMS's `/library` container advertises the first as its `art`.
  `?width=`/`?height=` are IGNORED there.
- **A 0/1 option's value is `+?0*[01]`**: `0`, `1`, `00`, `01`, `000`, `0000001`, `+1`, `+0` and
  `+01` pass, and `2`, `+2`, `-1`, `+`, `++1`, `1+`, `1.5`, `true`, empty and either surrounding
  space are 400s -- on `checkFiles` and its five siblings, on `minSize`/`upscale`, and on
  **`download=`**, which is one of the family. The four integer `include*` options take the leading
  `+` too. Cycle 22's and 23's "`+1` does not count" was a RAW `+` in a query string, which decodes
  to a SPACE.
- **A 416 is an error response, not the resource with no bytes**: `Content-Type: text/html`, a
  129-byte body, and neither `Accept-Ranges` nor `Content-Range`. plexsim sends its own text/plain
  sentence (cycle 1's deviation) with the header dropped.
- **Under `/web/*` a DIRECTORY is an empty `application/octet-stream` 200**, a trailing slash after
  a file is a 404, a path escaping the root is a **400**, and `.woff`/`.woff2` are
  `application/octet-stream` like `.ico`.
- **`X-Plex-Local-Asset: 1` marks an ITEM's own artwork and nothing else** -- not a section
  composite, not `/:/resources/*`, not `/web/*`, not a part read.
- **The `Content-Disposition` `filename*` escaper is "unreserved minus the dot"** (`.` -> `%2E`,
  `+` -> `%2B`, `-` kept): the same escaper as a local artwork candidate's `/file?url=`.
- **`/library/sections/{id}/extras/all` is a real listing QUERY** -- an unparseable `limit`, an
  unparseable or empty `type`, an unknown `sort`, ANY `group=` and an unparseable `parent` are each
  a 500, and so are `duration`, `resolution`, `mediaSize` and `file`, which the section GRID
  accepts. `extraType=`, `title=`, `audioLanguage=` and a good `limit` are 200s and really
  constrain.
- **`group=` is honoured on `/library/all`** (kids' 14 rows collapse to 4 by type) and an unknown or
  empty field is a 500 there too. **`sort=type` orders by the type NUMBER**, not the name.
- **`/library/metadata/{id}/extras/all` and the section's answer 404 to every verb but GET and
  HEAD** -- the bare `/extras` keeps its POST 400. A five-segment read-only route needs the gate as
  much as a four-segment one.
- **A clip is a row type plexsim does not hold, and its shape is recorded** (attribute order with
  `extraType` LAST, no `Guid`/`Rating`/tags/`Field`, a container with `allowSync="0"` and no
  librarySection triple, `art` the parent's verbatim while `/art` is a 404, `primaryGuid` on trailer
  rows only, six `extraType` values, three projections, uncapped, and in NO listing or axis
  anywhere). See `docs/findings.md`; the build waits on a catalog with extras in it.


- **`plexscratch42` is the writable 1.42.2 reference** (`harness/scratch42/`, kids `:32411`) -- a
  fresh install of the build plexsim reports, isolated from plex.tv, so every item is UNMATCHED and
  every SETTINGS count is short (151 prefs against zen's 173). Use it for grammar, never for a
  count, and never for anything needing real metadata or a claimed server.
- **An On Deck row carries no `librarySectionKey` and is the detail row MINUS `Field`** -- on a
  movie and an episode alike, on both versions. `/hubs/home/onDeck` and
  `/hubs/home/continueWatching` DO carry the key; `/library/onDeck` and the section leaf carry
  `allowSync` and the librarySection triple without it. No resume list carries `Field`.
- **`hdr10plus` takes an item LOCK on both versions**, where the five media-codec names beside it
  (`atmos`, `audioCodec`, `audioLayout`, `subtitleCodec`, `videoCodec`) are a **400 on 1.42.2** and a
  200 on 1.43.4 -- so plexsim, which reports 1.42.2, refuses those five and accepts `hdr10plus`.
- **Two settings answer 403 to a PUT on 1.42.2, not four**: `PlexOnlineUrl` and
  `allowMediaDeletionLanOnly`. `TranscoderH264Options` and `TranscoderH264OptionsOverride` take a
  write there and really store it, where 1.43.4 refuses both.
- **A `random="1"` shelf's sample is STABLE** -- 21 reads over seven minutes on zen answered the
  identical rows, a different client identifier gets the same sample, and the same shelf at another
  `count=` holds its own. What makes a real PMS redraw one is NOT measured, so plexsim mints a seed
  per (shelf, count) once and holds it.
- **An unauthorised refusal KEEPS the connection alive** -- the 401 with no token, the 401 with a
  wrong one, the 403 for an unknown single segment, the 404 for a path that does not exist, all
  `Connection: Keep-Alive` and really reusable. **The one refusal that CLOSES is the 400 for a query
  pair with no `=`**, and seventeen other 400s keep it alive.
- **A `collection[0].tag.tag=` write makes a TAG, not a collection.** The tag appears on the row, in
  the `/collection` axis and in both search endpoints (a `tagType: 2` row with a `count`), and
  `/library/all?type=18` gains NOTHING -- only `POST /library/collections` makes an item. The tell is
  the `guid`: a tag-only collection's search row carries none.
- **The ping timer is FREE-RUNNING: a pong must not trigger another ping.** The reference pings
  every 10 s whether the client pongs or not; plexsim read "a frame arrived" as "the interval
  passed" and answered a pong with a ping -- **903,480 pings in 35 seconds**. Every real client
  pongs automatically, and a probe that never pongs sees the correct cadence, which is how twelve
  cycles missed it.
- **One notification consumer that stops reading must not reach the request path.** A dead socket
  plus repeated writes took plexsim's write 105 to 20 s and timed out a second client's edit at 30 s,
  where the reference bounds the per-socket backlog at ~415 KB and never slows a request. plexsim now
  gives a sink a two-second budget and DROPS the channel -- it drops where the reference buffers.
- **A library DELETE pushes NO per-row timeline frames** -- only its eight `library.delete` activity
  frames, on a 3-row and a 200-row library alike. Cycle 18's fourteen `state 9` frames named another
  section and none of the library's rows: they were a one-off orphan cleanup. A single item's delete
  frame is real, with the row's own type and the POSITIVE sectionID.
- **`state 1` belongs in the timeline alphabet** (0 created, **1 created/processing**, 3
  queued/processing, 4 loading, 5 done, 9 deleted): a row's CREATION pushes eight frames -- `state 0`
  with an empty title, then five `state 1`s, `state 4 loading`, `state 5` -- plus a
  `library.update.item.metadata` activity per new row, so cycle 19's "no scan does a metadata
  activity per row" holds only for a scan that finds nothing new. plexsim cannot create a row during
  a scan, so the sequence is recorded and not built.
- **The eventsource channel has no five-minute deadline** on a real PMS (still open at 430 s), and
  neither transport sends `id:` or `retry:`, so nothing replays across a reconnect. plexsim's 300 s
  constant is gone; it still closes at ~256 s for an unfound reason (HANDOVER 3.3). **And the 101
  upgrade carries a `Date`** -- it was the one response on the surface without one.

- **`plexscratch42` answers the WRITE surface too, and the split is the VERB TABLE.** Held out write
  by write against plexscratch (1.43.4): the edit-field tables, the section save path, the three
  settings writers, the list writers, the state writes and the upload ladder are **identical on both
  versions** -- that is the negative result -- while four VERBS differ. On **1.42.2**: `PATCH` is not
  a method at all (a **500** on every path, `/identity` included, the same body `FROB` gets), all five
  `DELETE /library/metadata/{id}/{poster,thumb,art,banner,theme}` leaves are **404s**, a smart
  playlist accepts a comma-joined KEY LIST as its uri, and `PUT /playlists/{smart}/items?uri=` takes
  an ITEM uri and REPLACES the stored query with it. plexsim reports 1.42.2 and carried 1.43.4's
  answers to all four.
- **An EMPTY `thumb.value=` or `art.value=` clears the column** (200, the field gone, the bare path a
  404) on both versions -- and on 1.42.2 it is the **only** way to clear artwork, since that DELETE
  does not exist there. **A non-empty one is an artwork COLUMN write**: the url is discarded and the
  column becomes the row's own versioned path, exactly as `theme` and `banner` already did. The column
  has to be EMPTY for that to be visible, which is why cycle 20 read `thumb`/`art` as discarded.
- **`PUT /library/collections/{id}/prefs` really writes** (`collectionSort`, `collectionMode`; an
  empty 200, GET/POST/DELETE 404, an unknown setting 400) on both versions. Cycle 15 measured the GET
  alone and recorded the 404, so plexsim advertised both settings and 404'd the save behind them.
- **`absoluteIndex.value=` stores NOTHING** on either path or version (its `.locked=1` does record the
  lock), and **`lastRatedAt.value=` on the SECTION path is a 500** on both -- the ninth member of that
  set. **`hdr10plus` stores neither a value nor a lock**: cycle 30 confirmed the 200 and put the name
  in `LOCK_ONLY_FIELDS`, which records one, so the Edit dialog drew a lock on a field it cannot show.
- **Ten read paths refuse every verb but GET and HEAD** (`/statistics`, its two series,
  `/status/sessions`, `/clients/profiles`, `/playlists/sorts`, `/library/sections/prefs`, and
  `/library/sections`, `/media/providers`, `/media/providers/metadata` -- whose POST is their own 400).
  plexsim answered **200 with the whole GET body** to a bare OPTIONS on each, which is fix 4's trap
  from the outside.
- **A stored sort title EQUAL to a generated title is still stored.** Read off kids' own DATABASE:
  its agent-titled season 7 stores `title = ''` and `title_sort = 'Season 1'`, so the displayed
  `Season N` is generated in every case and what an agent supplies is the SORT title. plexsim treated
  such a value as absent, so `titleSort=Season 1` found nothing, `titleSort=` wrongly kept the row and
  the ordering keys ignored it -- one clause, three symptoms.
- **`title=`: a COMMA is an alternation and a DOT is literal.** `title=Steel,,` and `title=Love,`
  answer the WHOLE library, `title=Tears, of` answers the one row, and `title=Steel.` answers NONE.
  plexsim stripped both characters out of the query. The comma half lives in the filter PARSER, which
  already ORs a comma list and drops empty members -- on `title` the empty member is KEPT, and an
  empty query matches every row.
- **`title=` matches the ORIGINAL title as a consecutive run of whole words, unfolded** -- `Salaire
  de` and `Le Sal` hit, `Salaire peur` and `e Salaire` do not -- where the TITLE's own matcher allows
  gaps and folds. And **the in-library search box does not read `originalTitle` at all**; the hub
  search is the endpoint that does.
- **The hub matcher takes a multi-word query WORD BY WORD, in order** (`Tears Steel` finds "Tears of
  Steel" at 0.73; reversed it drops to the 0.33 fuzzy band), **tokenises the TITLE on WHITESPACE** so
  punctuation stays inside the word (`don't` finds "Don't Move"; `Delete` does not find
  'Control-Alt-Delete'), and **matches a ONE-character query against a title equal to it**. plexsim
  required the joined phrase, split the title on `[^0-9a-z]+` and had a two-character floor -- so two
  non-adjacent words of a title, or any title with an apostrophe, found nothing at all.
- **The reference's own files are readable, and they explain two tables.** `ssh ben@kids docker exec
  plexlab-plex` reads the image: `Resources/Profiles/` holds 78 `.xml` plus a stray `sugar.cmake`
  (hence 78 rows), a row's title is its `<Client name="...">` and not the file name, and the 23
  `Player.profile` aliases are **22 `redirect="..."` attributes plus one name-versus-filename
  difference** -- plexsim's table reproduces all 23 with zero errors. The `type=42` seven carry no
  marker, so that list is PMS's own. The decision's inputs are visible too: `redirect=`,
  `<DirectPlayProfiles>`, `<TranscodeTargets>`, `<CodecProfiles><Limitations>`.

- **A write's RESPONSE pages and excludes like a read's.** `POST /playQueues`, the playlist and
  collection item writes and their moves and deletes all honour `X-Plex-Container-Start/Size` and
  `excludeElements=` on both versions; plexsim gated both post-processors on GET and HEAD.
- **A write must build its answer inside the lock it wrote under.** Two concurrent
  `POST /playQueues` from one client are two 200s on a real PMS and were a 404 for the queue plexsim
  had just made in 9 of 60 pairs; two concurrent adds each answer the queue AS THAT ADD LEFT IT and
  plexsim answered both with the second's container. The window is under a millisecond, so no
  sequential check can see it: fire barrier-released pairs.
- **The title FOLD is a 100-character TABLE, not NFD, and the two sides differ.** Swept on both
  copies over 2,076 codepoints for the sort title and on kids over 2,098 for the query: `ę ą ş ć ł`
  and the capital macrons survive, lower-case macrons and Latin-1 fold, a combining mark is dropped.
  The `title=` QUERY is the table plus a simple case fold (so `ſ` and the Kelvin sign match `s` and
  `k`); the STORED sort title is case-folded only, so a locked `Éggęhead` is not found by `Egghead`.
  The folded pass reads the stored SORT title, not the title (`Thé Train` finds nothing).
- **The hub search folds by the same table**, but its score CASE handling is two distinct passes:
  the raw-query partial pass and a broader whole-title pass. Cycle 39 completed and built the table,
  including the long-s and two-letter macron-`o` boundaries.
- **A plain-form decision runs the client's own PROFILE FILE** (`decision.py`,
  `client_profiles.json`, read out of the lab image): selected audio not first, container and video
  codec, the stream it would play, a sidecar subtitle, the audio codec, an embedded subtitle, then
  the container's limitations before a named codec's before `*`. Exact on 1,864 of 1,872 measured
  cells; 44 profiles direct play a real row where plexsim hard-coded two, and those two do not list
  webm or eac3. A refused direct play still answers "neither", because plexsim cannot convert.
- **JSON is COMPACT and raw UTF-8**: no space after `:` or `,`, every non-ASCII character as itself,
  `/ < > &` unescaped, and a C0 control other than `\b \t \n \f \r` as `\u00XX` in UPPER-CASE hex. The
  notification frames too. Python's defaults differ on all three.
- **`/livetv`'s 403 on the copies is the UNCLAIMED server's**, not the surface's: both claimed
  references answer the three-row container, and the copies answer it at `/livetv/`.

- **The response HEADER BLOCK is ORDERED, per response family.** Measured off a raw socket on both
  references, identical: `X-Plex-Protocol` FIRST and `Date` LAST on every response, `Keep-Alive`
  always straight after `Connection`, and then per family -- a 200 container is type-then-length, an
  ERROR length-then-type, a gzipped body puts `Connection` ahead of the encoding pair, artwork leads
  with the type and `X-Plex-Local-Asset`, `/photo/:/transcode` and a `/web`/`/:/resources` asset send
  the same names in two different orders, and a part read leads `ETag, Last-Modified`. plexsim wrote
  `Date` first, which is Python's. See `HEADER_ORDER` and `end_headers`.
- **A 416 names no connection state at all** -- the only response class that does not -- and **a part
  read carries no `Cache-Control`**, which is the exception to "no-cache goes on EVERY response".
- **The plug-in framework's `/system` paths carry `Set-Cookie: com.plexapp.system=<constant>`** on
  every status, and order their length before their type; `/system/agents`, `/system/scanners/{N}`
  and `/system/proxy` -- the leaves PMS implements itself -- carry no cookie and order like a
  container. Nothing else on the surface sends a cookie.
- **The CORS preflight and the `/web` 302 name their connection state too**, and the preflight
  carries `Cache-Control: no-cache`. A handler that writes its own header set has to say so itself.
- **A ranged CONTAINER answers `Content-Length` TWICE** -- the whole body's length, then
  `Content-Range`, then the SLICE's after the connection state, with the slice as the body. A ranged
  artwork, asset or part answers the slice's length alone. Read one with a RAW SOCKET: a client that
  takes the first length waits for bytes that never come, `http.client` included.
- **A refusal written before the request LINE parses has no `self.path`** -- every malformed-line 400
  answered NOTHING when a header-ordering pass read the attribute directly.
- **A NEVER-MATCHED row (`local://`) opens its refresh with `state 2 matching` and a bare `state 5`**
  before the queued frame, on both versions; an explicitly unmatched (`tv.plex.agents.none://`) row
  opens with the queued frame alone. Whether the cascade then RUNS is the server's own state -- an
  isolated fresh install never gets past the queue -- and not a version rule.

- **A response with an EMPTY body orders its LENGTH before its type**, the way an error does, where
  a MediaContainer body is type-first -- so cycle 33's families split on "does the body carry a
  container", not on the status. A HEAD keeps the container order, its length being the body's.
- **The preflight sends `Access-Control-Allow-Headers` only when the request ASKED for headers**, and
  echoes exactly what it asked for; Plex Web's own preflight asks for none. A fallback list answers a
  question the browser did not put.
- **The 101 handshake LEADS with the CORS pair**: `X-Plex-Protocol, Access-Control-Expose-Headers,
  Vary, Upgrade, Connection, Sec-WebSocket-Accept, Cache-Control, Access-Control-Allow-Origin, Date`.
  It cannot be probed -- a socket may never be opened on kids or zen -- so it was read out of a
  client-session capture, which records both sides of the client's own handshake.
- **A decision reads EVERY audio stream, not only the selected one**: the first in INDEX order that no
  matching `<VideoProfile>` names is the codec the refusal names (Roku plays the selected mp3 and still
  refuses the row, naming its ac3). **Unless the profile declares
  `StreamUnselectedIncompatibleAudioStreams`** -- Windows, Windows 10 Desktop, Windows 10 Mobile,
  Windows Phone, iOS, tvOS -- where only the selected stream counts.
- **The decision reads a per-user audio selection the ITEM ROW DOES NOT CARRY.** Before any client has
  chosen a track, 41 of the 78 profiles refuse a row whose tracks name no language as "not the first
  audio stream" although `selected` names the first one; `PUT /library/parts/{id}?audioStreamID=<that
  same track>` makes all 41 answer from the visible row and `audioStreamID=-1` puts them back, with
  `Media`, `Part` and `Stream` byte-identical throughout. plexsim holds a chosen track and models that
  state; `tests/test_decision.py` scores both against the recorded corpus.
- **A sidecar fixture read out of the reference's image has to be the WHOLE thing.**
  `client_profiles.json` kept 10 of the 30 `<Setting>` rows -- the one the model of the day read --
  and the decision turned on one of the twenty it dropped.

- **A real PMS zeroes its gzip header's MTIME**, so a compressed body is a pure function of the body
  -- `gzip.compress(payload, 6, mtime=0)` reproduces the reference's member byte for byte. Python
  stamps the clock, which makes the same request answer different bytes every second.
- **The `Range` grammar is lenient in a measured shape, and 416 is part of it.** The unit token is
  never read (`items=0-10` ranges) but a value with no `=` is not a Range; an empty spec is a 200; a
  suffix of zero or one that does not parse answers `bytes 0-0/N` with the WHOLE body; an
  unparseable FIRST -- and any second `-`, and a comma list -- answers the whole body as a 206; an
  unparseable LAST becomes ZERO; and a reversed range or a first at or past the end is a **416**.
- **A library CREATE pushes the scan's skeleton even when there is nothing to scan** -- eleven
  frames, the `library.update.section` activity through a Context-carrying 99 and 100, the two
  LIBRARY_UPDATE statuses, and the nested `provider.subscriptions.process` pair. The Add Library
  dialog's spinner and its "Library scan complete" toast are drawn off exactly these.
- **`ButlerTaskGenerateAdMarkers` ships DISABLED**, alone among the three marker tasks, on both
  versions -- the one disagreement in the 23-row enabled table, and `POST /butler` runs what is
  enabled.
- **A search hit's SCORE tier is the MATCHER's rule**: an exact title is 0.93, a query whose terms
  match the title's words in order starting at the title's FIRST word is 0.73, everything else is
  the 0.33 fuzzy band. `Tears Steel` is 0.73 and `Steel` alone is 0.33 -- a title PREFIX test gets
  both backwards.
- **A `list=` limitation's refusal names the list and appends ` [list]`**, where a `value=` one
  carries no suffix.

- **A refusal's BODY is one fixed HTML document per status** -- `<html><head><title>{reason}
  </title></head><body><h1>{code} {reason}</h1></body></html>` with `Content-Type: text/html`, from
  HTTP's own reason phrase: 89 bytes for a 400, 91 for a 401, 85 for a 403 and a 404, 95 for a 406,
  129 for a 416, 109 for a 500. It IGNORES `Accept`, it is GZIPPED when offered, and a HEAD carries
  its length. The exceptions are `GET /`'s 193-byte redirect and `/system`'s empty bodies with no
  `Content-Type` at all. Every refusal below the API answers it too -- a malformed request line, an
  over-long URL, a 101st header -- where `BaseHTTPRequestHandler.send_error` writes Python's own
  460-byte DOCTYPE page. Cycle 1's `text/plain` sentence was never measured; it is now the LOG's.
- **A path-segment id is parsed as a NUMBER**: `01`, `001`, `+1` and `+01` name the same row as `1`,
  byte for byte, while `++1`, `1+`, `-1`, `0x1`, `1.0` and every non-ASCII digit name nothing. It is
  the general case of cycle 29's `+?0*[01]` option grammar. **Four paths bound it to a SIGNED 32-BIT
  integer** -- `/devices`, `/accounts`, `/playQueues`, `/system/scanners`: `2147483648` is a 400 and a
  negative value inside the range a 404, on all four. `/library/metadata` and `/library/sections` have
  no bound.
- **The request TARGET is printable ASCII (0x21-0x7E) and nothing else** -- every raw control byte and
  every byte from 0x7F up is a 400, in a path segment and in a query value alike, while a
  percent-escape of the same byte is fine. **In the PATH a `%` must begin an escape the parser can
  read**: two characters must follow, and they parse like `strtol` in base 16 (an optional `+`/`-`
  then at least one hex digit, not necessarily consuming both), so `%4y`, `%AB`, `%+1` and `%-1` pass
  and `%zA`, `%y4`, `%++`, `%+y`, `%`, `%4` and `%+` are 400s; `%%` is a literal pair consumed before
  that rule. The QUERY is not validated this way at all.
- **`/hubs/sections/{id}` and its `continueWatching/items` leaf 404 a section the server does not
  hold**, like every other section leaf since cycle 15, while **`/hubs/metadata/{id}/related` never
  resolves the row**: an unknown id there is the empty hub container (`{size, allowSync, identifier}`,
  no librarySection triple) where `/library/metadata/{id}/related` and `/hubs/metadata/{id}/postplay`
  are 404s.
- **A repeated query parameter takes the LAST occurrence in the TOKEN GATE too** -- a bad token then a
  good one is a 200 and the reverse a 401 -- and in the client identifier and the notification
  channels' `filters=`. Three readers used `parse_qs(...)[0]` where the server's own `query` dict
  takes the last.
- **A search hit's five decimals are a TIER plus the row's own `audienceRating`/10000**, and a
  row with none scores the bare tier -- held out over 11,076 scored (query, row) pairs. `rating`
  is not it. Without the tail every row inside a tier ties and the page falls back to insertion
  order.
- **The ladder has FOUR steps and a base per ROW TYPE.** exact (the query's tokens ARE the
  title, its `titleSort` or its `originalTitle`) / anchored (the query's FIRST token EQUALS the
  title's first, and nothing else matters) / prefix (the query is a token-wise prefix of the
  title -- no more tokens, each a prefix at the SAME index, the first a PROPER prefix) / fuzzy.
  A movie and a show are 0.93/0.73/0.53/0.33, an EPISODE is 0.02 lower throughout, and a TAG has
  0.92/0.52/0.32 with NO anchored step. Cycle 35's `word_tier` -- the in-order skip-tolerant walk
  -- is REFUTED as the tier rule: `Riders X` and `Riders of Justices` are anchored on both
  references.
- **ORDER decides the SCORE, not membership**: a query whose words are all in the title but out
  of order comes back in the 0.33 band. The rest of the membership rule is SQLite `spellfix1` --
  a phonetic-hash candidate then a top-N prune by a GLOBAL token frequency -- which is why
  `Tears zzz` finds the row on kids' five titles and `Riders zzz` finds nothing on zen's 3,887.
  The candidate half alone would answer most of a catalog to a nonsense query, so it is recorded
  and not built.
- **A Dolby Vision PROFILE 5 source is refused by the SERVER**: `transcodeDecisionCode 2003`,
  "File is unplayable. DoVi (Profile 5) color space is not supported." -- where profile 7 and 8
  rows answer the ordinary 4002. Not a blanket veto: the one profile whose transcode target names
  the source's own codec still converts it. **`DOVIBLPresent` is the dual-layer discriminator**,
  not `DOVIELPresent`: a separate-track enhancement layer says false on its own stream, and that
  settles the one-witness-each-side disagreement in favour of what `mediainfo.py` already does.
- **An UNMATCHED show refuses the agent prefs**: its `Preferences` block carries eight settings, not
  ten, and `useOriginalTitle`/`languageOverride` are 400s there; **`showOrdering` is a 500** for
  every value, with NO frames, while the value is STORED -- the write lands and the cascade behind
  it fails. A matched show answers 200 to all of them.
- **A playlist and a collection take an artwork UPLOAD** on `POST /library/metadata/{list}/posters`
  -- an empty 200, the candidate list going from one `default://` row to two, the upload selected.
  And **the `/library/collections/{id}` action leaves really RUN**: `refresh` pushes the metadata
  path's three frames and `unmatch` gives the row `local://{rk}`, `subtype "unknown"` and no
  `ratingCount`. Cycle 22 measured their empty body and stopped there.
- **A `?path=`-scoped scan scans the section's own folder AND any child of it** -- cycle 19's "a
  child scans nothing" was measured on a movie library, whose children are files. Only a parent, or
  a path the library does not hold, scans nothing.
- **A type=18 SECTION save frames `state 3 queued` with the title from BEFORE the write**, where
  the item and metadata spellings of the same edit send `loading` with the new title.
- **An MBAFF interlaced row sets `scanType: interlaced` and NOT `video.separateFields`**: made on
  the copy and driven against all 78 profiles, only `RokuDLNA-5.1` refuses it (on `video.scanType`)
  and iOS and tvOS -- the two profiles naming `separateFields` -- direct-play it. `decision.py`'s
  constant 0 is right; a row that trips it needs field-coded PAFF pictures no encoder here writes.
- **The request target is RAW data, not a browser URL.** A literal `;` or `#` remains in the path or
  query; `urlparse`/`urlsplit` silently strips it as params/fragment syntax and routes a different
  request. Split only at the first `?`, and pass that same raw query to the token gate, localisation,
  paging, excludes and notification filters.
- **A non-terminal all-dot path segment is a 400 after ONE `%2e` decode**, not a traversal: `..`,
  `.%2e` and `...` inside the path refuse, a terminal `..` is 404, and a double escape stays escaped.
- **GET and HEAD never consume a declared request body.** Both references answer immediately with
  `Content-Length: 999` and no bytes, with or without `Expect: 100-continue`; reading before routing
  turns a read-only request into a trivial server stall.
- **A measured multipart filename is `Title - cdN.ext`.** One Media owns ordered Parts, its duration
  is their sum, and every Part owns its debrid link. `partIndex=0/1` projects that Part,
  out-of-range is 400 and `-1` is the 4005 all-parts conversion refusal.
- **A clip is not an ordinary library row.** It publishes no librarySection triple; the parent owns
  `primaryExtraKey` and inline `Extras`; item and section `/extras/all` use different projections;
  the clip borrows the parent's `art` attribute but its own `/art` route is 404.
- **Fuzzy search is the reference's `spellfix1` pipeline, not a similarity shortcut.** The two live
  vocabularies use ICU-token ranks, candidates are `top=10 AND score<=200`, cost is
  `editdist1 + 32 - bit_length(rank)`, and normal movie/show/list/tag hits suppress fallback
  globally. Per-word candidate groups are ANDed, tokens shorter than three UTF-8 bytes are dropped,
  and the FTS comparison uses ICU primary/shifted collation. Spellfix cost chooses membership; it is
  not the API relevance score. The external table may be stale between database optimizations;
  rebuild it only at the measured optimize/Butler/section-delete completion points, not on edits.
- **Two query-only writes ignore a declared body:** item metadata `PUT` and playlist-create `POST`
  dispatch from their query string even when a large `Content-Length` body never arrives. This is
  path-specific; subtitle upload and other body-dependent writes still wait for their bytes.
- **Search order is not a stable score/title sort.** A hub sorts the full-precision score with the
  reference's unstable comparison sort and formats to five decimals afterwards; its title
  candidates entered in raw-title/id descending order with exact hits prepended. Flat
  `/library/search` preserves the movie/show/episode bucket sequence because its final comparator
  accidentally reads an empty generic score attribute.
- **Hub-search membership folding and score-tier folding are separate passes.** Partial-title
  tiering applies the measured `_TITLE_FOLD` table to the RAW query before ASCII-only case folding;
  lowercasing first promotes eleven unsupported capitals and loses `Ð`. Whole-title exactness is a
  separate, broader comparison, with measured long-s and two-letter macron-`o` boundaries. Do not
  replace either pass with one global Unicode normalizer.
- **The spellfix tag vocabulary is larger than `Catalog.TAG_FIELDS`.** Rebuilds also index the row's
  Chapter/Review/Marker/CommonSenseMedia/Network tags, Guid ids, Rating images, studio and the
  Thumb/Art/Theme/ClearLogo presence strings. The only server-static remainder on 1.42.2 is the
  measured 28-string optimize/show-ordering sidecar. Technical GUID tokens can change the top ten
  and therefore which people reach the wire; never substitute a library-specific token dump.
- **`/system/services/url/lookup` is not an empty collection.** The bare request is the legacy
  plug-in framework's fixed 500 `application/xml` failure, independent of Accept. plexsim reports
  the exact 464-byte 1.42.2 body: GET may gzip it, HEAD carries the uncompressed length without a
  body, and the response closes the connection. A valid `url=` request is external-fetch shaped and
  was not probed.
- **The metadata-provider tables expose Add Agent.** Once both tables load, Plex Web offers group
  create with no further capability gate, then consumes the returned group id. Create and rename
  return the full JSON group; deleting the group is an empty 200. The offered Personal Media
  secondary membership is refused 400 and absent membership deletion is 404. Provider creation is
  a separate external-object capability and remains absent.
- **A historical “measured but not acted on” note is not a disposition.** Cycle 41 normalized 513
  raw notes into 332 atomic behaviors and found 24 reachable mismatches hiding among entries marked
  thin, deliberate or already built. Keep one atomic claim per independently testable behavior,
  record the current code and a branch-reaching regression, and distinguish a bounded external cell
  from an unresolved deduction.
- **Title folding removes one finite block, not every combining character.** Both PMS versions drop
  exactly U+0300..U+036F at the front and between ASCII sentinels. A generic Unicode combining-class
  predicate over-removes 699 Mn/Mc characters from other scripts and misses U+034F. The regression
  sweeps all 7,531 assigned punctuation/symbol/separator/mark/format scalars in the measured gaps.
- **Playback sessions expire on a global reaper phase.** A process-wide tick advances every 30
  seconds and removes rows with `seen < tick - 180`; adjacent rows at age 180.9 and 179.0 split at
  the same tick. Thus a session's visible lifetime is 180-210 seconds, not a lazy per-read 180-second
  timer.
- **Recently Added TV is a 50-row listing, not a twelve-row shelf.** Build the full roll-up, cap at
  50, then apply the ordinary container window. A start of 50 returns zero rows with
  `totalSize=50, offset=50`; legacy `limit=` and `count=` do not truncate it. One new episode stays
  an episode, while two siblings roll up to their season.
- **A show merge is a hierarchy merge.** It deletes the absorbed show, season and episode, appends
  the absorbed leaf Media before the target leaf Media at matching season/episode indexes, retains
  target then absorbed Locations, and pushes three delete frames followed by the survivor's state
  3/5 pair. An unmatched hierarchy shape remains a no-op because no reference measurement defines
  a pairing for it.
- **Collection leaf views are empty even when the collection has members.** `/children` exposes the
  selected member and its collection tag, while `/allLeaves` and `/grandchildren` return empty 200
  containers on both supported versions. Do not apply the generic parent-leaf projection there.
- **Multi-Media selection is path-specific.** A decision over more than one Media requires one
  explicit, in-range `mediaIndex`; absent, negative, nonnumeric and repeated selectors are refused.
  A play queue ignores that selector and keeps every version. Playlist metadata containers carry
  `allowSync=false`, `identifier`, `mediaTagPrefix`, `mediaTagVersion` in the measured order.
- **Moving a queue item can re-anchor `playQueueLastAddedItemID`.** The marker names the item that
  remains the tail of the plain-add block after the move; leaving it on the moved id changes the
  client's Up Next shuffle state. Search hubs likewise have their own recovered ordering: strongest
  match class plus the type bias, sorted with the same unstable comparison routine as result rows.
- **Legacy quirks remain real side effects.** A type-mismatched playlist create returns 400 after
  creating the empty playlist and emitting its frame. `/playlists/upload` is 400 when either `path`
  or `sectionID` is absent and 500 when both are present, irrespective of whether the path exists.
  A section `/search` ignores every legacy limit spelling. The plain download `filename=` maps `+`
  to `_`, while `filename*=` keeps the percent-escaped plus.
- **Projection flags and derived media values are exact gates.** Part `accessible`/`exists` appear
  only under detail `checkFiles=1`; duration sort reads `Media.duration`; visually-impaired subtitle
  disposition is projected only when true; mixed folder XML emits a `Directory` for a folder and a
  `Video` for a file. Generated probes also pin the short-wide 480-resolution transition and the
  strict anamorphic boundary—do not restore the earlier fitted constants.
- **The `#` first-character bucket aliases every ASCII non-letter.** The axis aggregates punctuation
  and digits there, while explicit `/firstCharacter/!` and `/firstCharacter/0` requests remain
  literal buckets. Non-ASCII punctuation such as `。`, `『` and Greek letters retain their own
  buckets; only the measured leading-strip characters disappear before bucketing.
- **Localisation coverage is an emitted-source-pair census.** Cycle 41 walked all 124 safe-read
  families of the local 4.147.1 client plan and found four missing 1.42.2 source pairs: one server
  preference summary and three hub titles. Each was captured in all 28 languages. A table entry is
  complete only when every route context that emits that English source has been joined and checked.
- **A clean cycle rebuilds the denominator; it does not inherit “no findings”.** Cycle 42 recreated
  all 332 historical atoms and the 49/1,221/47/339/45/45 advertised-control graph from current
  source, current tests and fresh live responses, then replayed all 24 preceding fixes. Both reports
  ended `no findings`; this is the stopping gate. Retained evidence may be reused only when the
  exact request/state is unchanged and the relevant source/test blobs are byte-identical and hashed.
- **A Plex Web version/build label does not identify one webpack graph.** The historical gateway
  and current public app both say 4.160.0 / `75ddd7b`, but corresponding chunk hashes differ and
  each has one mapped id the other lacks. Complete one manifest-rooted graph and keep its provenance;
  never fill an absent historical body with a same-named current asset. The complete current app is
  205 JS plus 16 CSS, and the 177 JS assets beyond Cycle 40's recovered subset contain no PMS
  request builders or call sites.
- **Inventory server processes at both ends of every cycle.** Cycle 43 startup found Cycle 41's
  frozen listeners still on 32587/32588 despite a later clean-close record. List every
  `server.py --port` process, resolve cwd and command line, and kill only proved cycle-owned
  processes; checking the current cycle's expected ports cannot catch an older stray listener.
- **A live catalog build needs both playable bytes and identity.** RD torrents whose display name
  ends in a video extension can still unrestrict to a RAR; let `build_catalog.py` reject those.
  Without a valid TMDB key, an SxxEyy filename remains a movie row because no measured external
  identity exists from which to mint its show/season/episode guids. A byte test alone can therefore
  pass while navigation correctly reports no shows.
- **Container magic belongs to the selected Part, not to the test fixture's history.** The first
  live MP4 made `conformance.py` select the right Plex Web direct-play row and then fail its old
  unconditional Matroska assertion. MP4 carries `ftyp` at offset four; Matroska/WebM carries the
  EBML marker at zero. Keep the conformance target and magic assertion container-aware.

- **plexsim's MEDIA BYTE PATH had never been run against a real byte until 2026-09-17**, because
  every link in the fixture was dead and every part read a 502. It applied NO `Range` grammar: the
  client's header went to the debrid CDN and whatever came back was echoed, so 13 of the 20 cells
  both references agree on were wrong — `bytes=-1` read the FIRST byte where a real PMS reads the
  LAST (which is how every player finds an MP4 `moov` or a Matroska Cues block), a reversed or
  past-the-end range answered 206 instead of **416**, a one-byte probe pulled the whole file, and
  `bytes=<N+10>-` answered `Content-Length: -10`. `_range_window` is now the ONE grammar both
  `_ranged` and the part path ask. A bare **`bytes=-`** is not a Range at all (200 on every body);
  `bytes=- `, `-x`, `-abc`, `-0` are the 206.
- **A part read carries `X-Plex-Protocol` first like every other response**, its `Content-Type` is
  keyed on the stored FILE's extension (a `mp4` Media whose file is `.mkv` answers
  `video/x-matroska`; the URL's extension is ignored; an unknown extension is
  `application/octet-stream`, NOT `video/mp4`), and the bandwidth statistic counts what was
  WRITTEN — every seek and stop is an abort, so counting the advertised length moves the Dashboard
  by a whole file per seek. **`POST`/`PUT` on the THREE-segment media path is a 404**; only
  `/library/parts/{id}` takes the track write.
- **A 416 of a MediaContainer names its connection state; a 416 of a BINARY body does not.**
  Re-measured with a KEEP-ALIVE request, which is the only way to see the server's own choice
  rather than an echo of the probe. That corrects "a 416 names no connection state at all" to its
  binary half. **Every 416 is `Cache-Control: no-cache`**, the photo transcoder's included, whose
  200 caches for three days — and no 416 carries `X-Plex-Local-Asset`.
- **With an `Origin` on the wire, `Access-Control-Expose-Headers`/`Vary` sit directly after
  `X-Plex-Protocol` and `Access-Control-Allow-Origin` directly before `Date`, in EVERY response
  family** — the `/web` 302 included, which carried no CORS headers at all. Cycle 33 measured the
  families without an `Origin`, where the trio is absent, so it could not see this.
- **`/library/sections/{id}/rating` is a real axis that a MOVIE library ADVERTISES in its own pivot
  list** (plexsim advertised it and 404'd it, so the client's Ratings entry opened nothing). Its
  container is byte-identical to `/userRating`'s and its BUCKETS read the CRITIC rating.
  **A star bucket rounds its key DOWN to an even star** and takes the values within one of it, so
  8 and 9 answer the same rows; `key=0` is the rows with NO value. Truncation gets every other
  bucket wrong.
- **`videoCodec`, `audioCodec`, `subtitleCodec` and `audioLayout` are 1.43.4-only AXES** — 404 on
  zen and on plexscratch42, 200 on kids. The same six media-codec names already split `/filters`
  and the `Meta`; the axes split with them.
- **A `.webm` file's `container` is `mkv`.** Measured on the identical 571,346,576-byte file: zen
  answers `mkv` on the Media and the Part while the Part key keeps `file.webm`, and no row of zen's
  4,508 movie Media carries `webm` at all. `tests/fixtures/media/fun-1052407-0.json` has recorded
  that since cycle 10 and nothing read it, because `test_mediainfo.py` compared every stream field
  and **never the container**. The override cost a direct play (`Sony Bravia 2013`) rather than
  buying one.
- **A direct play is refused when the SELECTED embedded subtitle is not the FIRST embedded one** —
  `Samsung`, `RokuDLNA-5000X-7.x` and `RokuDLNA-6000X-7.x` only, gated on the same
  `DirectPlayStreamSelection` as the audio rule, and it runs after the audio-codec pass AND after
  the subtitle-codec pass. The 2,106-cell corpus could not show it: in every row it held whose
  subtitle is not first, the AUDIO is not first either, so the audio refusal masked it.
- **A SEASON carries `parentYear`, not a `year` of its own** — 689 of zen's 742 do, exactly one
  does not — and a season and an episode carry `Guid`. `metadata.py` fetched those ids and threw
  them away on a note claiming a real PMS leaves the arrays empty; kids' season 7 answers
  `tmdb://3846` + `tvdb://15791` and its episode 8 answers three.
- **A real PMS announces with `PUT https://servers.plex.tv/devices/{machineIdentifier}`** — the
  identifier form, to `servers.plex.tv`, token in the query, empty body, thirteen headers led by
  `User-Agent: PlexMediaServer/<version>` — and `httpsEnabled` is its own capability, not the uri's
  scheme. The numeric `/devices/{id}` form resolves to whatever device row holds that id and
  overwrites it. The harness's `capture/outbound.jsonl` is the instrument for this axis and no
  cycle had swept it.
- **`/myplex/claim` is routed for POST alone**; every other verb is a 404. **The two stream-select
  parameters have different grammars**: an unparseable `subtitleStreamID` CLEARS the selection and
  a negative other than `-1` is a 400, while `audioStreamID` 400s `0` and every negative but `-1`
  and accepts an unparseable value as a no-op. **An EMPTY `session=` on transcode `ping`/`stop` is
  a 404**, not the 400 a missing one gets.
- **`/media/providers/metadata/{id}` and `/group/{id}` are real** (400 for a non-numeric id), and
  in JSON `builtIn` is the STRING `"1"` on 1.42.2 and `order` is the FLOAT `1000.0` on both — the
  XML cannot tell either apart.
- **A part whose FILE is gone is a plain 404**, not a 5xx: zen part 23595 (`accessible=false`,
  `exists=false`) answers 404 to the byte read. plexsim answered 502 to a dead link and to a
  network failure alike. Real-Debrid's `unavailable_file`/error_code 24 is now the 404 and
  everything else keeps the 502, so a transport failure is still diagnosable.
- **`tests/test_part_bytes.py` stands up its own upstream** (a local CDN and a one-route
  Real-Debrid stub behind `RD_API`) and drives the real handler, so the byte path is testable with
  no credential at all. Use it rather than waiting for a live `/d/` link.


- **`/library/sections/{id}/{name}/top` is a real leaf** -- PMS's own route table spells it
  `/:tagType/top`, so ANY single word matches it -- and it answers the section's TAG TABLE for
  the type the name resolves to, **ascending by tag id** rather than by usage. The `key` carries
  the REQUESTED name (`?studio=28`) and the `filter` the tag's OWN type (`director=28`). Eleven
  names resolve, in six pairs of a tag name and a metadata COLUMN name sharing one number:
  `genre`/`title` 1, `director`/`studio` 4, `writer`/`rating` 5, `actor`/`tagline` 6,
  `producer`/`summary` 7, `country` 8. Every other name the server knows as a filter field or an
  axis is an EMPTY 200 and everything else a 404 -- including the eleven edit-path-400 names,
  the section's own leaves, `thumb`/`art`/`theme` and `platform`. `resolution` answers the whole
  library like any media-axis bucket. It is NOT the axis: kids' actor axis is the
  first-three-roles union and `/actor/top` is every Role tag.
- **`includeAlternates=1` inlines the AGENT's remote artwork on a FULL row and on nothing else** --
  `Thumb`, `Art`, `Theme` in that order, each `{key, source}` with a `previewKey` where the
  provider gives one. A light row never carries it, so the `_light` marker is the whole gate, and
  a DECISION ignores the option exactly as it ignores `excludeElements=`. `Thumb`/`Art` sit
  between the tags and `Guid`, `Theme` directly after `Rating`. **It is not 1.43.4-only**: the
  hunt measured 1.42.2 on plexscratch42, whose unmatched rows hold no agent artwork at all --
  "use the copy for grammar, never for a count", from the other side.
- **`Accept-Language` ranks the WHOLE list and `X-Plex-Accept-Language` is a third input.** The
  precedence is `X-Plex-Language` > `X-Plex-Accept-Language` > `Accept-Language`, header or
  query. Weights rank the list, the default is 1.0, `q=0` disqualifies that member, an unknown
  tag is SKIPPED rather than ending the search, `*` is ignored, an empty member is dropped, a tie
  goes to the first listed -- and **any malformed member disqualifies the entire header**. That
  is the OPPOSITE of the `Accept:` parser, where a bad `q` weighs 0 and the range still
  qualifies: two different parsers on the same surface.
- **`/library/metadata/{id}/theme/{v}/metadata` is a GET that MINTS A ROW.** It is how a cycle-45
  hunt left four orphan rows on the reference lab; measure it on a disposable copy only. The row
  is a `track` whose `guid` is `library://` + the **SHA-1 of the request path itself**, idempotent
  per (item, version), carrying no `librarySectionID` -- so it is invisible in every listing,
  `/library/all` included -- and readable at its own key. `theme` is the ONLY artwork leaf with
  this sub-leaf. plexsim mints it BESIDE the catalog so a `track` row cannot leak into the tag
  tables or the spelling index.
- **`/library/shared` is a whole subtree**: a constant seven-row `Type` tab bar in its own
  identifier, under which EVERY second segment answers the same list of the server's own
  libraries and a third segment is a 404. That row projection leads with an empty `art` and
  `thumb` and drops `allowSync`, `filters` and `refreshing`. `?type=` selects NOTHING for any
  integer (the rows are libraries), `?sort=` really orders by title or `createdAt`, `?limit=` is
  ignored -- and **an empty `?sort=` CRASHES a real PMS** (connection dropped on kids, SEGV on
  zen). Never send that request to zen.
- **`ownerFeatures` on `/` and `subscriptionFeatures` on `/myplex/account` are the SAME string**,
  byte for byte: a sorted comma join of ~180 opaque plex.tv UUIDs and 32 NAMED features. The 32
  names are identical on both servers where the UUID count is not. The UUIDs are NOT invented:
  fetch them from authenticated `/api/v2/features`, persist them with the owner id, and union them
  with the named defaults. A claimed server refreshes user then features at startup/after claim
  and every 86,400 seconds, with the feature read about 11 seconds after the user read; commit the
  pair atomically and retain the whole last-good pair on failure. Plex Web reads `ownerFeatures`
  in 28 places in its bundle.
- **An axis BUCKET resolves against the server's global tag TABLE, not the leaf's own column.** A
  genre tag id answers the identical container under `genre`, `director`, `actor`, `mood`,
  `style`, `similar`, `tag` and eleven more; only `studio` and `platform` are not bucket names at
  all. And **`narrator` is the one axis that does NOT validate `type=`** -- 200 for `abc` where
  every other tag axis is a 500 -- while `platform`'s `title2` is the EMPTY STRING where every
  other axis carries a "By ..." heading.
- **An item leaf with an EXTRA path segment is a 404**, with two exceptions only: the artwork
  leaves read the second segment as a version, and `/extras/all` and `/users/top` are real
  two-segment leaves. A **validated version segment refuses a leading `+`** although
  `+?0*<digits>` normalises in every other path segment -- read the RAW segment before the
  normalisation.
- **A refusal keyed on the QUERY's declared type must not be decided per ROW.** `file=` is a 500
  at `type=2` and `/library/all?file=` untyped is a 200 that really filters; plexsim raised it
  from inside the row walk -- "this row is a show" -- so the untyped whole-library query was a
  500. And **`extraType` is a 500 on every listing EXCEPT the section extras leaf**, which is the
  one path that really reads it (zen's 3,580 clips are 1,264 at `extraType=1`); putting the name
  in the global refusal table broke that leaf, and only `test_catalog_features.py` could see it.
- **A play completes on a beat that CROSSES 90% however short it is.** The gate is
  `position >= (total * 9) // 10`, an integer comparison; the sub-minute exemption the old rule
  carried was never measured.
- **The bare `/sync` is the plug-in dispatcher's 200** like every other single-segment path,
  while `/sync/items` and its siblings really are 404s. A "we do not implement this"
  short-circuit on a whole prefix makes one single-segment path answer differently from all the
  others.
- **File-check options report what the PROVIDER says, not a catalog constant.** `checkFiles`,
  `asyncCheckFiles` and `checkFileAvailability` share the same `+?0*[01]` grammar and resolver
  result: a live part projects `accessible=true, exists=true`, `unavailable_file` projects both
  false, and an unchecked row omits them. The byte path answers the same unavailable file 404;
  a resolver/network failure remains 502.
- **The broad option vocabulary was exhaustively swept: 116 names × 76 safe container families ×
  three servers.** The load-bearing propagation rules are that global `/hubs` passes
  `includeChildren`, `includeExtras`, `includeOnDeck`, `includeRelated` and
  `includePreferences` into FULL rows; `includeGuids` reaches hubs, Home Recently Added, children
  and section subsets; `includeMeta` reaches Home resume leaves, collections, folders and Shared;
  `includeCollections` moves the search collection shelf after Tracks; `includeSummary` reaches
  Shared; and `skipCount` filters the source rows before children/search/axis aggregation.
  `includeStorage` on `/media/providers` is NUMERIC, unlike a section grid's string totals.
- **Two request headers have special `/web` redirect semantics.** Exact
  `X-Plex-Http-Pipeline: disable` keeps the 302 but sends `Connection: close` without
  `Keep-Alive`; exact `X-Plex-Neuter-Redirect: 1` sends an empty 200 while retaining the absolute
  `Location`. Case-mismatched `Disable`, `infinite`, `0` and `true` are controls, not truthy
  synonyms.
- **Cloud watch/rating sync is a consent read plus an opaque-cursor state read.** Consent uses the
  server token; state uses the latest short-lived RS256 Bearer from the owner's `scrobbling`
  service. The encrypted service token is AES-256-CBC under PMS's measured MD5 expansion and
  permissive final-byte strip. The static 32-byte transport secret is NEVER checked in: inject
  base64 through `PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64`, or sync stays off. `complete` stores
  view time, `unplayed` clears it, an absent rating pair PRESERVES stars, `-1` clears them and a
  nonnegative rating sets them. Persist an `endCursor` only after applying the page; import writes
  no local history row.

- **A client FINDS a server over UDP, and plexsim answered nothing.** A real PMS answers
  Plex's own GDM discovery on udp/**32410** and udp/**32414** only (32412/32413 are the
  PLAYER side), on unicast, on broadcast and on the multicast group **239.0.0.250**. The
  trigger is a PREFIX COMPARE against `b"M-SEARCH * "` over `min(len, 11)` bytes -- an
  EMPTY datagram and a bare `M` answer, one wrong byte anywhere in the eleven is silent,
  anything may follow, and it is case-sensitive. The reply is `HTTP/1.0 200 OK` plus seven
  ALPHABETICAL headers, and `Host: <cert hash>.plex.direct` is present only on a CLAIMED
  server -- absent, not empty. A real PMS also SENDS `M-SEARCH * HTTP/1.1\r\n` (one CRLF)
  to the group every **5.00 s**, and `/servers` is exactly what that search found, each row
  ageing out between 177 s and 180 s after its last answer. `Updated-At` is NOT a clock: it
  stands still under polling, is days stale on an idle server, and the two ports carry
  independent stamps.
- **`/servers`' own row names the server's own interface address and its REAL port**, never
  loopback and never a constant 32400; `/myplex/account`'s `privateAddress` and
  `privatePort` move TOGETHER (both set, or `""` and **0**). `livetv` is a constant **7** on
  every server, tuner or no tuner, and `offlineTranscode` is 1 claimed and ABSENT unclaimed
  -- never 0. `/` also carries the transcoder's quality LADDER as three parallel comma
  lists (`transcoderVideoBitrates`/`Qualities`/`Resolutions`), identical on all four
  servers, which is what the client's Quality menu is built from.
- **A real PMS CONSUMES a declared request body after answering, and closes if it never
  comes.** Cycle 37's "GET and HEAD never consume a declared body" is half the rule: they
  answer without waiting AND then swallow exactly `Content-Length` bytes, so two requests on
  one socket get two answers. A duplicate `Content-Length` takes the **FIRST** occurrence --
  the opposite of the repeated-query-parameter rule -- and a leading `+` parses. It also
  really DECHUNKS `Transfer-Encoding: chunked`, including on an upload; the size token is
  `strtol` base 16 and **nothing may follow it**, so a chunk EXTENSION is a 400, and the
  header must be exactly `chunked` with nothing else in the value, beating `Content-Length`.
- **The incomplete-request cap is ABSOLUTE from the first byte, not idle.** Three pieces
  twelve seconds apart is closed at 20.0 s with no answer. A socket timeout is per-read by
  construction, which is why fifty dribbling clients held fifty threads. And a header block
  ended by **EOF** rather than by a blank line is answered with NOTHING -- plexsim ran it,
  and a query-only write really committed.
- **A refusal of the ENVELOPE closes the connection; a refusal of the MEANING keeps it.**
  That is the rule behind cycle 30's lone "exception". A missing `/:/resources/*` or
  `/web/*` asset is a 404 that names **no connection state at all** and closes; an
  `obs-fold` continuation line is a 400 both references refuse where Python's own reader
  unfolds it into an ordinary header; a leading CRLF or space before the request line draws
  **silence**. The METHOD token is bounded at **ten bytes** and the eleventh is refused the
  moment it arrives, before any delimiter. The ABSOLUTE-FORM target (`http://host/path`) is
  ROUTED with the authority discarded, and every other scheme spelling is a 400.
  `X-Plex-Http-Pipeline: disable` is a header-or-QUERY option, last occurrence winning,
  header beating query.
- **Every write rule in this project was measured on an UNCLAIMED copy, and the claim moves
  things.** 116 read families across the 2x2 of claim and version: 15 families and 40
  container attributes are claim-dependent. `ButlerTaskGenerateAdMarkers` ships **ENABLED**
  on a claimed server (with `GenerateAdMarkerBehavior: scheduled`, a setting the copies do
  not have at all) -- cycle 35 read the copies and recorded the opposite, which is class E.
  Before trusting any rule whose evidence is "measured on plexscratch", ask whether the
  claim could be computing it.
- **`/playlists/sorts` ends with `mediaCount` "Item Count"**, the same row
  `/playlists?includeMeta=1`'s `Meta` carries: cycle 15's "the two lists really are
  different" is REFUTED on four servers. **`?includeDetails=1&includeAdvanced=1` DOES carry
  the Duplicates Filter row**, and it is the one Filter there that keeps `advanced="1"`:
  cycle 23 read the absence of the FLAG as the absence of the ROW. **`/dlna` and `/manage`
  are real** -- the DLNA library list and the same 302 `/web` answers.
- **A LIGHT row carries none of `Label`, `Style`, `Similar` or `Mood`**, `Style` sits AFTER
  `Country` on a detail row, and `/status/sessions` writes EVERY tag element alphabetically
  with a string `id`. None of it is visible on a real library: it needed a movie MADE to
  carry the whole tag family at once, which is why cycle 23's 2,994-path order sweep could
  not have found it. A `mood` write on a movie is a 200 that stores nothing.
- **Emptying a collection leaves the collection ALIVE** and takes the tag off the item --
  both, so the state plexsim's deliberate deletion called unrepresentable is not. And an
  identifier-less request's `X-Plex-Device-Name`/`X-Plex-Platform` land on the ANONYMOUS
  device row.
- **XML cannot see a JSON scalar's TYPE**, and that is where the last two-writer bug was
  hiding: a `/system/agents` `Language`'s `default` is the boolean `true` and plexsim sent
  `"1"`, because the fixture is read out of the XML, which writes `default="1"` either way.
  `to_xml` also hard-coded `container=True` for the ROOT, so `/myplex/account` sorted its
  attributes alphabetically in XML and kept the right order in JSON.
- **Inline OnDeck is playback state, not a required detail block.** A show exposes one current
  episode only when its watch state selects one; a season exposes the block only when it owns
  that same episode. A pristine unwatched show and every other season omit it. The timeline's
  `playbackState=progress` is separately latched by `X-Plex-Session-Identifier`: once that live
  session crosses one minute, rewinding it below a minute remains `progress`, while a fresh
  session is `ignore` even though the stored item offset is still above a minute.
- **A searchable tag row has a top-level membership gate but an all-row count.** The tag must be
  present on at least one current top-level movie/show in the requested library before search
  emits a row. Once admitted, `count` is every current metadata row in that library carrying the
  tag, including episodes: show+episode is count 2 while the row's key lists one show. An
  episode alone emits no row; removing and re-adding proves the count is current, not cumulative,
  while the tag id itself persists.
- **Regional language equivalence is surface-specific.** `/:/prefs` has complete measured
  regional catalogues: `pt-BR` and `pt-PT` are identical, `hu-HU` is distinct, and the reported
  1.42 build serves 172 of each captured 174 rows. Do not collapse them to their primary subtag,
  and do not project this exception onto unmeasured endpoints. `zh-HK` and `zh-TW` likewise route
  to their measured English catalogue rather than generic `zh`.
- **The legacy single-segment prefix dispatcher is a finite registry, not a prefix algorithm.**
  The durable executed denominator is 58 unique paths (old prose said 57), identical over 174
  reference GETs. Reproduce the captured ordered shapes only; deriving rows from plexsim's own
  router would model plexsim rather than PMS. Double and trailing slashes are another finite
  per-route table: there is no global path normalisation rule.
- **GDM `Updated-At` moves only with library lifecycle.** Section create, edit and delete touch
  the advertisement. FriendlyName changes `Name` without moving the stamp; refresh, item edit,
  collection creation and index deletion do not touch it. A manual `disco.touch()` in a test is
  not evidence for a server lifecycle hook.
- **`/connections` is deliberately absent after a complete disposable-only matrix.** Both PMS
  versions show Pending partial requests, Routing observers, Pipelined pauses, Finished rows and
  a cleanup around the 20-second incomplete-request sweep, but snapshot ordinals/order are
  unstable, the hexadecimal allocator has invisible gaps, Finished cleanup is coupled to an
  opaque scheduler and the byte-count form requires excluded media traffic. Implementing any
  visible row now would fabricate fields or lifecycle.
- **A normal keep-alive idle close must be quiet.** The first timeout can occur in the method
  peek and poison `BufferedReader`, whose following `readline()` raises `OSError: cannot read
  from timed out object`. Close the connection there; never let the handler traceback while the
  wire correctly reaches EOF.


- **The server's own BINARY is a route table, and it names endpoints no client sends.** It
  is 22 MB and readable on both references (`docker exec plexlab-plex cat "/usr/lib/
  plexmediaserver/Plex Media Server"`; `cat` over ssh on zen), and its strings carry PMS's
  route templates with their captures -- `/parts/:partID(\d+)/:changestamp(\d+)?/:filename?`,
  `/:assetType(thumb|art|clearLogo|squareArt|banner|poster|theme)`,
  `/:type(video|music|audio|subtitles)/:/transcode`. Diffing the two binaries splits the
  vocabulary per version for free: 1.42.2 omits `squareArt` and keeps `clearLogo`.
- **The legacy dispatcher's listing is a RULE**: the LAST SEGMENT of every registered legacy
  prefix that begins with the requested path and is STRICTLY LONGER than it. `/statistic`
  names `statistics` and `/statistics` is EMPTY; `/co` is empty because `/connections` is a
  native route, not a registered prefix. Cycle 50's 58-path table was that rule sampled at
  58 points and 119 other prefixes answered rows it had none for. 148 of the 154 listings
  are in byte order of the row key, five put the built-in `library` row LAST, and `/l` is
  recorded as measured.
- **Three static files answer at the top level with NO token** -- `/crossdomain.xml` (347
  bytes), `/clientaccesspolicy.xml` (325) and `/favicon.ico` (5,430), byte-identical on both
  versions, `Accept-Ranges: bytes`, never gzipped, and the dispatcher's empty listing for
  every verb but GET and HEAD. They are the third, fourth and fifth exception to "a real PMS
  401s everything but `/identity` and `/web/*`", and the unauthenticated partition is wider
  than cycle 16's eleven besides: `/connections` and `/dlna` are 401s, `/manage` is the
  `/web` 302, and `/search`, `/downloadQueue` and `/player` answer their own refusal (400,
  404 and, on 1.42.2, 400) before the gate.
- **`/:/plugins` is SEVENTEEN constant rows** -- the bundles in the image, identical on four
  servers, `state` 4 on all but `com.plexapp.system` -- and everything under it is a SECOND
  plug-in framework subtree: thirteen of the seventeen answer a preference document at
  `/{id}/prefs` (the other four the framework's 500), a bare identifier and any other path
  under a hosted one are an EMPTY-bodied 404, the `/system` envelope applies (OPTIONS an
  empty 200, the other verbs empty 404s, PATCH the version's own refusal), and the cookie is
  NAMED AFTER THE PLUG-IN. So cycle 18's "PATCH is a 405, the only one on the surface" names
  two subtrees. **The framework's cookie rides its own responses only**: neither the bare
  OPTIONS 200 nor the PATCH refusal carries it, under either subtree.
- **`/:/resources/*` is SEVENTY names**, PMS's whole `Resources/Graphics` tree, identical on
  both versions -- 59 png, 9 jpeg, 2 octet-stream. A DIRECTORY is an empty
  `application/octet-stream` 200 in both spellings, the bare `/:/resources` is a 404,
  matching is case-SENSITIVE, and `?width=`/`?height=` are ignored. Cycle 29's "seven names"
  was a ten-name sample. An ACTOR with no portrait carries `/:/resources/actor-icon.png?t=`
  -- 479 of zen's 3,416 actor rows, and not one lacks a `thumb` -- where a director, writer
  or producer with none carries no `thumb` at all.
- **The photo transcoder FETCHES the `url=` path**: anything the server does not answer 200
  is a **404** there (an unknown leaf, a missing composite, a borrowed field, and
  `/children`, whose own answer is a 400), and only a 200 whose bytes are not an image is
  the 500. So a section COMPOSITE transcodes to its own jpeg -- which is the path Plex Web
  builds every library card from.
- **An artwork READ leaf takes GET, HEAD, PUT and DELETE only**: POST and a bare OPTIONS are
  the ordinary 404, and the VERSIONED path 404s the PUT. **A collection's and a playlist's
  artwork leaves follow the item ownership rule** -- a list with nothing uploaded 404s
  `/thumb`, `/art` and `/poster` and answers the empty candidate container at `/clearLogos`
  and `/themes` -- and an EMPTY playlist's `default://` candidate still names
  `/playlists/{id}/composite/{updatedAt}`.
- **`/connections` is `text/plain`**, one line per connection slot
  (`3 <peer> - Routing - GET /connections #<hex> - Token (<user>) (<agent>)`), where the
  trailing parenthesis is `X-Plex-Device-Name, X-Plex-Product` or the `User-Agent`. PMS's
  acceptor pool, its ordinals and its request allocator stay unmodelled.
- **`/hubs/sections/{id}/popular` is real**: the library's SEASONS with a `viewCount` above
  zero, most-watched first, in the full row projection. A movie library answers the empty
  container on 1.42.2 (kids 500s it, unexplained).
- **A Categories tile's `blendColor` is a TABLE** -- thirteen fixed genre names plus a
  seven-colour cycle walked in TAG-ID order over the rest (22 of 22 across both references)
  -- and the tile's `thumb` is the FIRST attribute of the row. The art is the highest-rated
  item carrying that genre and an `art` no earlier genre has taken.
- **Four refusal shapes, both versions**: an extra segment after a STREAM id, an ACCOUNT id
  or a DEVICE id is a 404 (not the 501 or the row); `/library/metadata/augmentations/{x}`
  parses its id first, so a non-number is a 400; all four `/:type(video|music|audio|
  subtitles)/:/transcode` prefixes are routed; and the PART leaf's one extra segment is a
  FILENAME whatever it says, the digit rule deciding the changestamp only when a filename
  follows.
- **Exactly four actions live under `/system/agents`** -- `search`, `update`, `contributors`
  and `media/get` -- each the plug-in framework's own TypeError traceback with a 500, and
  every other name an EMPTY-bodied 404. The framework passes the query through to its own
  Python function, so the sentence moves with the parameters; the recognised names are
  measured and the general rule is the bundle's signatures.
- **On the SECTION save path an indexed tag parameter is a tag write whatever follows
  `.tag.`** (`.role`, `.thumb`, `.id`, `.filter`, `.zzz` are all 200s and the role stores
  empty), not a type-scoped filter.


- **The plug-in framework is PYTHON, shipped uncompiled in the image, and
  `System.bundle/Contents/Code/*.py` IS the `/system` surface.** Every `@route` in those
  files is an endpoint (`ssh ben@kids docker exec plexlab-plex sh -c 'ls
  /usr/lib/plexmediaserver/Resources/Plug-ins-e5521bd8c/'`; zen's is `Plug-ins-f737b826c`
  and the two differ). `runtime.py`'s `handle_request` is the `f(**d)` in every framework
  traceback, so the TypeError sentence is the function's own signature: `given` counts
  `self` plus every supplied name the function DECLARES, an undeclared one is "got an
  unexpected keyword argument", and a `**kwargs` tail is why `search` alone swallows one.
- **`/system/bundle/media/flags/{type}[/{country}]/{value}` is a real image family, and
  every container's `mediaTagPrefix` points at it.** TEN types (`aspectRatio audioChannels
  audioCodec contentRating media recordLabel studio videoCodec videoFrameRate
  videoResolution`), 742 PNGs, and a 624-regex `substitutions.xml` for studio, audio codec
  and video codec. An unknown VALUE is the bundle's 82-byte `blank.png` with a **200**; an
  unknown TYPE, an empty value and a THIRD segment are the `/system` empty-bodied 404;
  `media` and `recordLabel` have no directory so every value under them is blank; a direct
  file name is case-SENSITIVE and the substituted types are not (`studio/Disney` and
  `studio/Walt Disney Pictures` are one logo, `audioCodec/dca` the DTS one).
- **Eighteen more framework routes are real**: `/system/channels/{all,music,video,photos,
  applications}` and `/system/plugins/{same five}` (the framework's 130-byte EMPTY
  container), `/system/notify/serverUpdated` (an empty `text/plain` 200),
  `/system/debug/agentservice/status` (a 229-byte report), `/system/messaging/*`, and
  `/system/services/{search,searches,relatedcontent/lookup}` plus both
  `/system/agents/attribution` leaves (the framework's TypeError traceback). **A framework
  STRING result spells its header `Content-type`**, with a lower-case `t`.
- **Five sub-paths under a hosted plug-in are real** beyond cycle 52's `prefs`:
  `prefs/list` (byte-identical to `prefs`), `plist` (the bundle's `Info.plist`, text/plain),
  `prefs/set` (empty 200), `messaging/wait` (a 16-byte constant) and `resources/{name}`.
  `root` is a **302 to `/system` for `com.plexapp.system`** -- the only bundle with a
  prefix -- and the framework's `IndexError` traceback for every other running one. The
  four bundles the server hosts but does not run answer the 109-byte 500 to all of them.
- **The ELEVEN legacy agents are listed beside the modern four** -- 15 rows on 1.42.2,
  identical on four servers, so it is a fixed table in the image and not hosting, the same
  argument that retired the `/:/plugins` listing. A legacy row carries `MediaType` children
  and no `Language` in the BARE form; in the typed form the primaries for that type come
  FIRST with their own `Language` CODES (no titles: 1 for Personal Media, 33 for the legacy
  IMDb agent, 26 for The Movie Database). 1.43.4 adds an `id` to every legacy row; the
  order of `lastfm` and `plexthememusic` is the server's own load order. With them listed,
  `/system/agents/attribution[/image]?identifier=` is reachable: nine texts, eight images,
  `org.musicbrainz.agents.music` an EMPTY text and no image, everything else a 404.
- **A framework error KEEPS the connection alive.** All five `plugin-xml` paths answer
  `Connection: Keep-Alive` and really serve the next request on the same socket -- cycle
  45's rule, a refusal of the MEANING rather than of the ENVELOPE. **Five routes put
  `Cache-Control` SECOND** (`/system/search`, `/system/library/sections`,
  `/system/channels/*`, `/system/plugins/*`, `/system/notify/serverUpdated`) where every
  other `/system` response puts it last. **A framework container pages without an `offset`
  and still names its window in the response headers.** **`/system//` is the framework's
  empty-bodied 404**, not the 85-byte document.
- **Only the BARE `/system/agents` is PMS's own handler**: everything under it is the
  framework's, so `/system/agents/{id}/config/{N}` is written by the framework's serialiser
  (single-quoted declaration, SELF-CLOSED empty container) and is the one framework
  document carrying NO cookie. **`/system/library/sections` carries thirteen attributes**,
  its `key` the URL-safe base64 of `http://{host}:{port}/library/sections/{id}`, `unique`
  set when the title occurs once, rows sorted by the lower-cased title -- and an extra
  segment is a 404.
- **A state cookie the server cannot unpack empties the framework**: a 200 with an EMPTY
  body, `text/html; charset=UTF-8` and **`Server: TornadoServer/1.0`** -- the one `Server:`
  banner on the surface, against cycle 33's "no banner at all". It swallows the route's own
  status, the cookie NAME must be the identifier serving the route, and PMS's own handlers
  are untouched.

- **PMS's own DATABASE TEMPLATE is a column oracle.**
  `Resources/com.plexapp.plugins.library.db` is an empty 311 KB database whose schema
  names every column PMS can store (71 tables; 1.42.2's and 1.43.4's templates are
  identical and the live schemas differ only in `metadata_items.user_square_art_url`).
  **Ten more of its columns are real query fields** -- `remote isAdult
  resourcesChangedAt userFields lastSkippedAt mediaAnalysisVersion beginsAt endsAt agent
  source` -- filtering, parsing, sorting, grouping and taking an item edit on both
  references, where an UNKNOWN field answers every row. Five refuse an unparseable value
  with a 500 and `userFields` refuses every value; seven SORT and three GROUP, so here
  the sort surface is wider than the group surface; six are a 500 on the SECTION path.
- **`isAdult` is a real row attribute**: exactly the literal `1` stores it, every other
  value clears it, and the row then carries the JSON boolean `true` between
  `librarySectionKey`/`originalTitle` and `summary` on every projection.
- **A continue-watching row carries `includedAt`**, an integer directly after
  `lastViewedAt`, on `/hubs/continueWatching` and its `/items` leaf and on NOTHING else.
  Plex Web's reducer reads `includedAt ?? lastViewedAt`.
- **`includeAlternates=1` inlines only the artwork the row OWNS**: a season's and an
  episode's `/arts` are empty, so neither carries an inline `Art` -- the option must apply
  the same gate the candidate leaf does, or the server contradicts its own `/arts`.

- **The part `Content-Type` table is wider than zen's six extensions.** Measured on
  plexscratch42 one container at a time: `.ts` is `video/MP2T`, `.m4v` `video/mp4`, `.mpg`/
  `.mpeg` `video/mpeg`, `.flv` `video/x-flv`, `.wmv` `video/x-ms-wmv`, `.asf`
  `video/x-ms-asf`, `.divx` `video/x-msvideo`; `.mts`, `.m2ts`, `.ogv`, `.3gp` stay
  octet-stream. **A `.mov` is container `mov`** and the rest of the QuickTime family `mp4` --
  the extension decides, not the brand -- and **every MPEG-TS Part carries
  `packetLength: 188`**, a 192-byte `.m2ts` included. The copy's analysis is SHALLOW, so its
  fixtures are marked `shallow` and only container-level checks read them.
- **`viewUpdatedAt` is the view-state WRITE time** -- a date filter and a sort key (Plex Web
  sorts a hub on it), stamped by every rating, scrobble, unscrobble, stored progress and
  Remove from Continue Watching on exactly the rows the write touches, and never on a row.
  **`lastViewedAt`, `lastRatedAt` and `lastSkippedAt` read an unset value as 0**, like
  `viewCount`; every other date column's null matches nothing. **`lastSkippedAt`, `beginsAt`
  and `endsAt` are dates.** A negative rating other than `-1` STAMPS `lastRatedAt`.
- **The binary names filter fields no Meta advertises.** Six media columns
  (`mediaHeight`, `mediaVideoCodec`, ... -- ANY-Media semantics, case-sensitive names), eighteen
  more tag-table fields (`mood`, `style`, `similar`, ...), the photo-cluster columns (parse,
  match nothing) and `trash` as an INTEGER. The tagging join (`tagId`, `tagType`,
  `taggingIndex`) is real but matches hidden `tagType=318` taggings plexsim does not hold.
- **An episode listing's leaf-count filters read the SHOW**, as a season's do, and so does a
  season's `unwatchedLeaves`.
- **The listing query reaches further than the grid**: a show's `/children` and
  `/grandchildren` (filters, `sort=`, 500s -- "a show's season list ignores `skipCount`" is
  refuted), `/unwatched`, `/recentlyViewedShows`, the media axes (still `type=`-blind),
  `/autocomplete` and `/common?id=`. "All episodes" appears only when MORE THAN ONE season
  row is answered, paged or not. An empty Common is `{mixedFields, guid, type, titleSort}`
  and a `Common` is always paired.
- **Every TV Recently Added shelf filters its EPISODES before the roll-up**, on all four hub
  paths; a hub PAGE drops an empty-valued pair first, the `type=2` list parses it and refuses
  the per-user keys, and `contentDirectoryID` empties a section page's TV shelf and scopes
  `/hubs/items`. `unwatched=1` is a flag on `/library/recentlyAdded` and `/allLeaves` (0/1
  grammar) and on a section's `recentlyAdded` (the literal `1` only).
- **The Dashboard's view-time keys are `viewedAt>` and `viewedAt<` (both INCLUSIVE) and a bare
  `viewedAt` (exact)**; `viewedAt>=`/`viewedAt<=` are unknown keys. Top Played filters its
  history by them, `accountID` and `librarySectionID` and counts what is left. Probe with the
  client's encoding: a raw `>=` IS the `viewedAt>` key.
- **Thirteen more parameters refuse what they cannot parse** -- int32 (`augmentCount`,
  `count`, `offset`, `excludeParentID`), any-size (`monthsAgo`), int64 (`bytes`), a float
  (`maxDistance`), the 0/1 option (`personal`), `metadataTypes` (an int32 list that also
  filters) and `sectionId` (`0` and 2147483647 only) -- and an unknown item id still 404s
  first. **`sectionID` on Recently Added is a number** (`01` is section 1).

- **Related is a TAG SCORE computed by the server**: the item's first three genres and first
  four roles, a candidate needs two in common, shared-count then release date descending, cap
  200 -- zen's 59 rows and kids' four lists exactly. It is not agent data.
- **On Deck's next-up anchors on the MOST RECENTLY VIEWED watched episode** (and the row carries
  that view's `lastViewedAt`); **`recentlyViewed` and a section's `/onDeck` leaf order by the
  view-state WRITE time** (`viewUpdatedAt`); **`popular` counts HISTORY rows**, so an unscrobbled
  season stays; a playlist's `duration` sums whole seconds; the genre shelf is the head of its
  own key; a season's generated title follows an index edit (`0` is `Specials`).
- **A library's TV Recently Added shelf IS the section's mixed roll-up** (zen 12 of 12) -- a
  one-episode show is its episode there. Cycle 50's "top-level shows" was kids' one show.
- **Structural parameters are parsed wherever they are read**: `sort`, `group`, `sourceType`,
  `limit`, `index`, `type` refuse on every axis, subset, children list, people list,
  autocomplete, `/common` and extras listing; `sourceType` other than 1 ORDERS by id. **An
  integer is `[+-]?digits`** (`+1` and `01` are 1) and every include/async option takes
  `+?0*[01]`; the include blocks reach every hub's full rows.
- **Filter values**: `=-1` is "no value" on `rating`, `audienceRating`, `userRating`,
  `absoluteIndex`, `parent`; a date epoch is a strict int64 with no spaces; a bare `=` on the
  per-user dates takes an int32 only; `clusterStartsAt` is a date; `decade`/`viewCount`/
  `viewOffset` are int32. An empty `title=` matches everything, generated titles included.
- **`/hubs/search?sectionId=` refuses any id naming no section**; `personal=1` empties only the
  MOVIE Recently Added list; `/hubs/promoted` refuses the fifteen hub keys whenever a show
  library's shelf is in scope; the `X-Plex-Product` HEADER beats the query for client titles.
- **`/library/search?limit=4294967296` crashed kids' PMS.** Treat search limits past int32 as
  crash-shaped on a reference, like `/library/shared/all?sort=`.

- **The loop's stopping rule is client impact** (HANDOVER 1): tag every finding CLIENT-VISIBLE
  (with the client call site) or MALFORMED-ONLY; build the first, record the second.
- **A show library's provider storage totals are its episodes'**; the playlist Meta's Type key
  carries `playlistType`/`sectionID`/`type` and nothing is active on `/playlists/all`; a legacy
  agent's `MediaType` carries its own `Language` rows; `decade` ignores its operator; any
  per-user write makes `viewCount` and `skipCount` read 0; `/hubs/sections/{id}` honours
  `includeGuids=1` (python-plexapi always sends it).

- **Lists were never on kids or zen, so they are measured on the copies.** A collection search
  row is on the tag ladder; a list's history is an empty 200; a playlist scores one step below a
  movie; `X-Plex-Sync-Version` puts `allowSync="1"` on playlist rows and a playlist's items.
- **Home's merged Continue Watching hub holds the On Deck next-up rows too** (stamped
  `includedAt`); a library's own Continue Watching shelf lists a next-up only after a PLAYBACK
  completion, never after a scrobble; a never-watched show carries its FIRST episode as `OnDeck`;
  a collection of two or more adds a `collection.related` shelf to its members' Related, whose key
  filters on `tagId=` (exact `=` only); `playQueueLastAddedItemID` is the tail of the Up Next
  block after the selection.
- **A LEGACY agent has a combination chain, and the Agents page saves it.**
  `/system/agents/{id}/config/{N}` lists every agent serving the type -- the saved (or
  default) order first with `enabled="1"`, the primary appended if the order omits it, then
  the rest by identifier -- and `PUT ?order=` answers the `<combine>` document (`Content-type`,
  lower-case `t`, with the cookie). An empty order is the framework's `split` AttributeError, a
  missing one its TypeError; mediaType 8/9 is a `KeyError` for a non-legacy agent and 20 an
  AttributeError for every one; a non-integer type is a ValueError. Both answer XML whatever
  `Accept` asks. See `legacy_agent_config.json`, zen's 192 cells byte for byte.
- **Top Played counts an episode's play on its SHOW at `type=2` and its SEASON at `type=3`** --
  zen's one show answers `globalViewCount: 73`, its episodes' history rows. The Dashboard's TV
  column asks `type=2`, which never matched a history row's own type.
- **Statistics buckets are CALENDAR buckets in local time**: a day at local midnight, a week on
  a Monday, a month on the 1st, a year on January 1st (zen UTC, kids Europe/Berlin). Flooring
  the epoch to fixed widths put every week on a Thursday. See `sessions.bucket_start`.
- **Edit Library > Visibility (`hidden`) is a real filter.** At 1 the library leaves every Home
  shelf, `/hubs/continueWatching`, `/library/recentlyAdded` and `/library/onDeck`; `/hubs`
  keeps its shelf EMPTY and `/hubs/promoted` drops that library's shelf outright; its own page,
  section hubs and On Deck are unchanged. At 2 it also leaves `/hubs/search` and
  `/library/search`, and a search scoped to it is a 404. The `/library/sections` row reports it.

- **A sub-minute beat deletes the resume point only inside a session that has passed the
  minute.** A NEW playback session's beats under a minute -- "Play from beginning", stopped
  within a minute -- answer `ignore` with the STORED offset and keep it, on both versions.
- **A beat at EXACTLY the stored position writes nothing**: `lastViewedAt` stands still and a
  row removed from Continue Watching stays removed, while 1 ms either side stamps and restores it.
- **A section's Continue Watching gate is the SHOW's play history**: once any episode was
  completed in a playback session, the shelf lists the On Deck episode however it became next
  (Mark Played on a later episode or the season, and after the played one is unscrobbled).
- **A collection rename to a name a row of the library carries as a `Collection` tag is a
  409** on the section path, ignoring case, and changes nothing; its own title in any case is a 200.

- **A library's Continue Watching shelf gates on the SHOW's play history** (cycle 58): once one
  episode has been completed in a playback session the next-up is listed however it became
  next. A new session's sub-minute beats keep the resume point, and a beat at EXACTLY the
  stored position writes nothing -- a removed row stays removed.
- **`hidden=1|2` keeps a library off Home and `/library/recentlyAdded`** (2 also out of both
  searches), with the Home shelf kept and emptied; statistics `at` buckets align to LOCAL
  calendar boundaries; `/library/all/top?type=2|3` rolls an episode play up to its show and
  season.

- **`X-Plex-Language: en` is NOT "no translation", and a language enum is not every enum
  whose first row says `Library default`.** Cycle 50's injection caught Show/Hide and every
  other library-default enum, so under python-plexapi's default header a show's Seasons
  dropdown offered 46 languages. An item's own language enum is built at READ time and is
  localised piece by piece (`item_language_names.json`): the head by tag, the tail from the
  table in its own order, the inherit option on its own.
- **`translations.json` is per VERSION** -- the seventh such fixture. It was captured off kids
  (1.43.4); re-capture it with `capture_i18n.py` against zen (`I18N_BASE`, `I18N_IDS` remapping
  kids' ratingKeys) and overlay, never replace: zen lacks what kids' paths alone reach.
- **An upload belongs to its row, and a theme or logo upload is stored.** A poster upload keeps
  the agent's candidate unselected with exactly one selected; a theme becomes the row's own
  versioned `theme` (served `audio/mpeg3`), a logo a `clearLogo` Image (served `image/jpeg`).
  The candidate lists were keyed server-wide by the bytes' hash.
- **A playlist is a timeline row** (`Playlist.updateTimeline()`): the item machine runs on it,
  `viewCount` steps from its born 1, and Now Playing shows a `Playlist` with `User` and `Player`.
- **What a library OFFERS follows what it holds**: a Collections pivot while it holds a
  collection, a Playlists source (one `playlists.video` pivot) once a video playlist exists, and
  a library's Recent Playlists shelf is its key's head -- an EMPTY playlist answering every
  library's `sectionID=`.
- **On 1.42.2 the PINNED list shapes Home's Recently Added**: only the first pinned library of
  each type carries a shelf, merging every pinned library of that type; an empty pinned list
  gets none, and the See All leaf takes the pinned list over `contentDirectoryID`.
- **A proxied `/player/*` command (with `X-Plex-Target-Client-Identifier`) is a 404**, and
  `DELETE /library/sections/all/refresh` a 200 with a "Library scan canceled" status frame.
- **Drive the RENDERED client, not only its requests.** Cycle 59 pointed the real Plex Web app
  at plexscratch42, kids and plexsim through kids' 9223 Chromium with `page.route` answering
  plex.tv, and found four surfaces no request replay had: a response is only half of what the
  client shows, and whether a tab EXISTS is decided by another endpoint entirely.

- **Localisation is an AXIS, not a table lookup.** A real PMS localises a stream's language by
  its TAG (under `en` too), the WORDS a title is composed from, a hub title AROUND the name it
  carries, and the lead of an enum built from the host -- none of which a table of whole English
  strings can hold. And the table itself is per version: capture it with `I18N_KEEP_SAME=1` so
  the cells a language leaves in ENGLISH remove the other version's translation.
- **Joining a collection LOCKS the member's `collection` field**, and nothing unlocks it but an
  explicit `collection.locked=0`. A check that adds to a collection must unlock afterwards.
- **An artwork leaf's upload is served to GET and HEAD only**; every DELETE on 1.42.2 is a 404.
- **A symlinked media file is re-homed by the next scan**: PMS resolves the link and the rows
  follow the TARGET. Make a test show from re-muxed copies with their own bytes (cycle 60 lost
  cycle 59's `Twofold` that way).

- **The collection tag TABLE outlives every collection**: a rename onto any name in it is a 409,
  and a NEW tag's first ASCII letter is upper-cased. Each rename or add leaves a name behind on
  a copy for good -- use a fresh name per run.
- **A session is listed from its first `playing` beat, and carries `Session` only after a
  decision** -- the key is allocated on a `paused` beat all the same.
- **`/:/prefs` pages like a listing.** A capture sending `X-Plex-Container-Size: 20` reads twenty
  settings and nothing says so; cycle 60 took that for the token's reach.

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- Stdlib only on the server path. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a Real-Debrid `/d/` link, a Plex token, or a tunnel
  credential. The repo is private; that is not a reason to relax this.
