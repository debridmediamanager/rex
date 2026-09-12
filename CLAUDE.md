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
| `server.py` | The PMS surface. Every route, ~1800 lines. Start here. |
| `hierarchy.py` | Flat leaves → show/season/episode with synthesised parents |
| `metadata.py` | TMDB identification, `plex://` guid minting, artwork URLs |
| `mediainfo.py` | ffprobe JSON → `Media`/`Part`/`Stream` elements |
| `debrid.py` | Real-Debrid unrestrict |
| `gate.py` | Applies a client's direct-play gate to the catalog |
| `xmlout.py` | MediaContainer → Plex XML (clients that refuse JSON) |
| `build_catalog.py` | Builds `catalog.json` from real RD content |
| `claim.py`, `heartbeat.py` | plex.tv claim flow and registration upkeep |
| `tests/` | Conformance and end-to-end suites |
| `probes/` | One-off measurement scripts that produced findings |
| `docs/findings.md` | Everything measured. Read before changing behaviour. |

Sidecar fixtures (`server_prefs.json`, `section_prefs.json`,
`section_meta.json`) are captured from a real PMS and loaded relative to
`server.py`. Keep the layout flat — the code resolves them with
`dirname(__file__)`.

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

## Testing

Run both against a live server. Neither needs network beyond RD and TMDB.

```bash
python3 tests/test_navigation.py            # client request replay, shape conformance
python3 tests/test_all.py                   # play queue → decision → byte reads → real decode
python3 tests/conformance.py                # drives plexsim with python-plexapi
```

`test_navigation.py` is the regression suite. Every fixed rendering bug gets a
check here, and the check must **fail against the unfixed server** — prove it,
by running the suite against the old code on another port, before claiming a
fix. A key-level comparison is not enough on its own: the `viewMode` bug was
invisible to one, because both sides had the field and only the values were
swapped.

`test_all.py` decodes real frames rather than trusting a 200, because a 200
that returns an error page still has a status a naive check accepts.

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
- **`viewMode`: a season list is 65593, an episode list 65592.** Inverted, the
  client is handed the episode layout for a season list and draws no seasons.
- **Artwork URLs must be versioned.** Unversioned `/thumb/1` plus ratingKey
  reuse serves a stale poster — one show wore another's art for a day.
- **Never write to plex.tv's device registry to fake a server.**
  `PUT /devices/{id}` resolves a numeric id and will silently overwrite an
  existing dormant device. Repeated claim exchanges with fabricated identities
  reset account-wide tokens and signed real servers out. Read the claim section
  of `docs/findings.md` before touching anything under plex.tv.

## Conventions

- Comments explain **what was measured and why the obvious thing is wrong**,
  not what the line does. Match the existing density.
- Stdlib only on the server path. Tests may use `plexapi`; probes may use
  anything.
- Atomic commits, semantic single-line messages (`fix: emit totalSize on paged
  children`), authored as `Ben Adrian Sarmiento <me@bensarmiento.com>`.
- Never commit a token, a Real-Debrid `/d/` link, a Plex token, or a tunnel
  credential. The repo is private; that is not a reason to relax this.
