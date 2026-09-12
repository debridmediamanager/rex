#!/usr/bin/env python3
"""Every UI surface a Plex client can reach on the server, checked end to end.

`test_navigation.py` covers browsing: the shapes a client reads while it walks
a library. This suite covers the rest of what the UI can do to a server -- the
whole item context menu, and every Settings, Status and Manage page -- because
the web client decides which of those entries to DRAW from what the server
advertises, not from whether the write behind them works. An unimplemented
endpoint therefore shows the user a menu entry that does nothing, which is worse
than an absent one.

The per-entry gates are measured out of the client's own bundle (`webclient/js`,
Plex Web 4.147.1, `main-*.js` module 92273 -- one flag list per item type plus
the predicate for each entry) and the response shapes out of a real PMS
(plexlab, 1.43.4, plus production fun for the rows the lab had none of). Both
are recorded in docs/findings.md.

**This suite WRITES.** It edits rows, marks items played, builds and deletes
playlists and collections, and finally deletes a library row and a section. All
of that is in plexsim's memory -- catalog.json is never rewritten -- so a
restart is the undo button. Run it against a test instance and restart that
instance afterwards:

    python3 server.py --port 32500 --catalog tests/catalog_ui.json --assert-claimed &
    python3 tests/test_ui_surface.py --base http://127.0.0.1:32500

`tests/catalog_ui.json` is the fixture the checks are written against: rows
cloned off the redacted real catalog onto fresh keys, so the section has more
than one movie and the show has two seasons and three episodes. Against a
one-item catalog the Shuffle, Play Next, Merge and paging checks have nothing to
compare and report SKIP instead of passing.

Checks that need something this deployment does not have -- a resolvable debrid
link for the Download entry, a TMDB key for Fix Match -- report SKIP with the
reason rather than passing quietly.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
OFF = "\033[0m"

UA = "Plex Web/4.147.1 (plexsim-test)"

results = []
skipped = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-52s %s" % (GREEN + "PASS" + OFF if ok else RED + "FAIL" + OFF,
                              label, detail))


def skip(label, why):
    skipped.append((label, why))
    print("  %s  %-52s %s" % (YELLOW + "SKIP" + OFF, label, why))


def notifications(base, trigger, seconds=8):
    """Open the notification socket, run `trigger`, and collect what arrives.

    A hand-rolled client rather than a dependency: the handshake plus text-frame
    parsing is a few lines, and the point is to prove the socket carries real
    frames. A client that receives none of these falls back to polling and never
    sees a change made anywhere else.
    """
    import base64
    import os
    import socket
    import struct
    import threading

    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 32400
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=seconds + 5)
    sock.sendall((
        "GET /:/websockets/notifications HTTP/1.1\r\n"
        "Host: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        % (host, port, key)).encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            return None, []
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        sock.close()
        return head.split(b"\r\n")[0].decode(), []

    threading.Timer(0.5, trigger).start()
    frames = []
    sock.settimeout(seconds)
    data = rest
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            while len(data) >= 2:
                first, second = data[0], data[1]
                length = second & 0x7F
                offset = 2
                if length == 126:
                    if len(data) < 4:
                        break
                    length = struct.unpack("!H", data[2:4])[0]
                    offset = 4
                elif length == 127:
                    if len(data) < 10:
                        break
                    length = struct.unpack("!Q", data[2:10])[0]
                    offset = 10
                if len(data) < offset + length:
                    break
                payload = data[offset:offset + length]
                data = data[offset + length:]
                if first & 0x0F == 0x1 and payload:
                    try:
                        frames.append(json.loads(payload))
                    except ValueError:
                        pass
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return "101", frames


def eventsource(base, trigger, seconds=8):
    """Read the SSE channel while `trigger` runs. Returns (status, [(event, data)])."""
    import threading

    request_ = urllib.request.Request(base.rstrip("/") + "/:/eventsource/notifications",
                                      headers={"Accept": "text/event-stream",
                                               "User-Agent": UA})
    try:
        response = urllib.request.urlopen(request_, timeout=seconds + 5)
    except Exception as error:
        return None, [("error", str(error))]
    threading.Timer(0.5, trigger).start()
    events, name = [], None
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                line = response.readline()
            except Exception:
                # A channel that sends nothing at all blocks until the socket
                # times out; that is a result, not a reason to stop the run.
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("event:"):
                name = text.split(":", 1)[1].strip()
            elif text.startswith("data:") and name:
                events.append((name, text.split(":", 1)[1].strip()))
                name = None
            if len(events) >= 4:
                break
    finally:
        response.close()
    return 200, events


def request(base, method, path, want_json=True):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": UA,
        "Accept": "application/json" if want_json else "*/*",
        "X-Plex-Client-Identifier": "plexsim-ui-test",
        "X-Plex-Product": "Plex Web",
        "X-Plex-Version": "4.147.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)
    except Exception as error:
        return None, ("%s" % error).encode(), {}


def container(base, method, path):
    status, body, headers = request(base, method, path)
    if body[:1] == b"{":
        try:
            return status, json.loads(body).get("MediaContainer", {}), headers
        except ValueError:
            return status, {}, headers
    return status, {}, headers


def get(base, path):
    return container(base, "GET", path)


def is_empty_200(status, body):
    """A real PMS answers every mutating endpoint with 200 and no body."""
    return status == 200 and body == b""


def quote(value):
    return urllib.parse.quote(str(value), safe="")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:32500")
    args = parser.parse_args()
    base = args.base

    status, identity, _ = get(base, "/identity")
    if status != 200:
        print("no server at %s" % base)
        return 1
    machine = identity.get("machineIdentifier", "")

    # ------------------------------------------------------------------
    # 0. what the server advertises -- every menu gate reads one of these
    # ------------------------------------------------------------------
    print("\n=== capability contract (what makes the UI draw each entry) ===")
    _, providers, _ = get(base, "/media/providers")
    provider = (providers.get("MediaProvider") or [{}])[0]
    features = {f.get("type"): f for f in provider.get("Feature") or []}
    protocols = str(provider.get("protocols") or "")

    # client module 92273 `showDownload`: provider.protocols.download
    check("Download entry: protocols advertise download", "download" in protocols,
          "protocols=%s" % protocols)
    # `showDelete`: server.allowMediaDeletion
    check("Delete entry: allowMediaDeletion true",
          providers.get("allowMediaDeletion") is True,
          str(providers.get("allowMediaDeletion")))
    # `showSyncDownload` reads server.allowSync; absent is not the same as false
    check("allowSync present and false", providers.get("allowSync") is False,
          repr(providers.get("allowSync")))
    # `showOptimize`: transcoderVideo AND backgroundProcessing. plexsim has no
    # encoder, so the entry must stay hidden rather than fail when pressed.
    check("Optimize entry stays hidden (no encoder)",
          providers.get("transcoderVideo") is False,
          "transcoderVideo=%s" % providers.get("transcoderVideo"))
    # `showEdit`/`showRefresh`/`showAnalyze`/`showFixMatch`: features.manage
    for kind in ("manage", "playlist", "collection", "rate", "actions", "timeline",
                 "playqueue", "metadata", "match", "search", "content",
                 "imagetranscoder", "promoted", "continuewatching", "queryParser",
                 "subscribe", "universalsearch"):
        check("provider advertises %s" % kind, kind in features)
    # The Categories tab is a pivot on every content directory of a real PMS.
    pivots = [p.get("id") for d in (features.get("content") or {}).get("Directory") or []
              for p in d.get("Pivot") or []]
    check("each library offers the recommended, library and categories pivots",
          pivots.count("recommended") and pivots.count("library")
          and pivots.count("categories"), ",".join(sorted(set(pivots))))
    timeline = features.get("timeline") or {}
    check("Mark as Played gate: timeline carries scrobbleKey",
          timeline.get("scrobbleKey") == "/:/scrobble"
          and timeline.get("unscrobbleKey") == "/:/unscrobble",
          "%s / %s" % (timeline.get("scrobbleKey"), timeline.get("unscrobbleKey")))
    actions = {a.get("id") for a in (features.get("actions") or {}).get("Action") or []}
    check("Remove from Continue Watching is advertised",
          "removeFromContinueWatching" in actions, ",".join(sorted(actions)))
    diagnostics = str(providers.get("diagnostics") or "")
    check("Troubleshooting: logs offered, database not",
          "logs" in diagnostics and "databases" not in diagnostics, diagnostics)

    # ------------------------------------------------------------------
    # 1. pick the rows every later check works on
    # ------------------------------------------------------------------
    _, sections, _ = get(base, "/library/sections")
    movie_section = show_section = None
    for directory in sections.get("Directory") or []:
        if directory.get("type") == "movie" and movie_section is None:
            movie_section = directory["key"]
        if directory.get("type") == "show" and show_section is None:
            show_section = directory["key"]
    if movie_section is None:
        print("no movie section in the catalog")
        return 1

    _, listing, _ = get(base, "/library/sections/%s/all" % movie_section)
    movies = listing.get("Metadata") or []
    _, show_listing, _ = get(base, "/library/sections/%s/all?type=2" % show_section) \
        if show_section else (None, {}, {})
    shows = show_listing.get("Metadata") or []
    if not movies:
        print("no movies in the catalog")
        return 1
    movie = movies[0]["ratingKey"]
    spare = movies[-1]["ratingKey"] if len(movies) > 1 else None
    show = shows[0]["ratingKey"] if shows else None

    part_id = None
    for media in movies[0].get("Media") or []:
        for part in media.get("Part") or []:
            part_id = part.get("id")
            break

    # Stream selection needs a row that actually HAS a subtitle track. Picking
    # the first movie and skipping when it has none tests nothing on a catalog
    # where a different row does.
    subtitle_row = subtitle_part = None
    _, leaves, _ = get(base, "/library/sections/%s/all?type=4" % show_section) \
        if show_section else (None, {}, {})
    for candidate in (leaves.get("Metadata") or []) + movies:
        for media in candidate.get("Media") or []:
            for part in media.get("Part") or []:
                if any(s.get("streamType") == 3 for s in part.get("Stream") or []):
                    subtitle_row, subtitle_part = candidate["ratingKey"], part["id"]
                    break
            if subtitle_part:
                break
        if subtitle_part:
            break

    print("\n=== item rows ===")
    check("every row carries a guid (Match/Fix Match gate)",
          all(m.get("guid") for m in movies + shows),
          "%d row(s)" % len(movies + shows))
    if show:
        _, detail, _ = get(base, "/library/metadata/%s" % show)
        row = (detail.get("Metadata") or [{}])[0]
        # client module 92273 `showMarkPlayed` for a show reads
        # unviewedLeafCount, which the client derives from these two.
        check("show carries leafCount and viewedLeafCount",
              row.get("leafCount") is not None and row.get("viewedLeafCount") is not None,
              "leafCount=%s viewedLeafCount=%s"
              % (row.get("leafCount"), row.get("viewedLeafCount")))
        check("item detail container carries identifier + mediaTagPrefix",
              detail.get("identifier") == "com.plexapp.plugins.library"
              and bool(detail.get("mediaTagPrefix")),
              "re-measured on fun and the 1.43.4 reference")
    else:
        skip("show checks", "no show section in the catalog")

    # A missing key is a 404 everywhere on a real PMS -- measured for metadata,
    # its children, a playlist and a collection. An empty container instead
    # tells the client the item exists and has nothing in it.
    for path, label in (("/library/metadata/999999", "item"),
                        ("/library/metadata/999999/children", "item children"),
                        ("/playlists/999999", "playlist"),
                        ("/library/collections/999999", "collection")):
        status, _, _ = get(base, path)
        check("a missing %s is a 404" % label, status == 404, "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 2. Mark as Played / Mark as Unplayed / rating
    # ------------------------------------------------------------------
    print("\n=== context menu: watch state ===")
    status, body, _ = request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % movie)
    check("scrobble answers 200 with an empty body", is_empty_200(status, body),
          "HTTP %s, %d byte(s)" % (status, len(body)))
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("scrobble sets viewCount", (row.get("viewCount") or 0) >= 1,
          "viewCount=%s" % row.get("viewCount"))
    status, body, _ = request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("unscrobble clears viewCount",
          is_empty_200(status, body) and not row.get("viewCount"),
          "viewCount=%s" % row.get("viewCount"))

    if show:
        _, before, _ = get(base, "/library/metadata/%s" % show)
        leaf_count = (before.get("Metadata") or [{}])[0].get("leafCount") or 0
        request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % show)
        _, after, _ = get(base, "/library/metadata/%s" % show)
        row = (after.get("Metadata") or [{}])[0]
        _, leaves, _ = get(base, "/library/metadata/%s/allLeaves" % show)
        watched = [l for l in leaves.get("Metadata") or [] if l.get("viewCount")]
        check("marking a show played marks every episode",
              row.get("viewedLeafCount") == leaf_count and len(watched) == leaf_count,
              "viewedLeafCount=%s of %s, %d episode(s) watched"
              % (row.get("viewedLeafCount"), leaf_count, len(watched)))
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % show)
        _, after, _ = get(base, "/library/metadata/%s" % show)
        check("marking a show unplayed clears every episode",
              (after.get("Metadata") or [{}])[0].get("viewedLeafCount") == 0,
              "viewedLeafCount=%s"
              % (after.get("Metadata") or [{}])[0].get("viewedLeafCount"))

    status, body, _ = request(
        base, "GET", "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=8" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("rate stores userRating", is_empty_200(status, body) and row.get("userRating") == 8,
          "userRating=%s" % row.get("userRating"))
    request(base, "GET",
            "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=-1" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("rating -1 clears it",
          "userRating" not in (detail.get("Metadata") or [{}])[0])

    # ------------------------------------------------------------------
    # 3. Continue Watching, and its own menu entry
    # ------------------------------------------------------------------
    print("\n=== context menu: continue watching ===")
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=playing&time=60000"
                         "&duration=600000&identifier=com.plexapp.plugins.library"
                         % (movie, quote("/library/metadata/%s" % movie)))
    status, timeline, _ = get(base, "/:/timeline?ratingKey=%s&state=playing&time=60000" % movie)
    check("timeline answers with playbackState",
          status == 200 and timeline.get("playbackState") is not None,
          "playbackState=%s" % timeline.get("playbackState"))
    _, hubs, _ = get(base, "/hubs/continueWatching")
    rows = [r for hub in hubs.get("Hub") or [] for r in hub.get("Metadata") or []]
    check("a resumed item appears in Continue Watching",
          any(str(r.get("ratingKey")) == str(movie) for r in rows),
          "%d row(s) in the hub" % len(rows))
    status, body, _ = request(base, "PUT",
                              "/actions/removeFromContinueWatching?ratingKey=%s" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("Remove from Continue Watching clears the resume point",
          is_empty_200(status, body)
          and "viewOffset" not in (detail.get("Metadata") or [{}])[0],
          "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 4. Play History
    # ------------------------------------------------------------------
    print("\n=== View Play History ===")
    status, history, _ = get(base, "/status/sessions/history/all")
    rows = history.get("Metadata") or []
    check("history has a row for the item just played", bool(rows), "%d row(s)" % len(rows))
    if rows:
        entry = rows[-1]
        # Field set measured on production fun.
        check("history row carries the measured field set",
              all(k in entry for k in ("historyKey", "key", "ratingKey",
                                       "librarySectionID", "viewedAt", "accountID",
                                       "deviceID")),
              ",".join(sorted(entry)))
        check("history librarySectionID is a string",
              isinstance(entry.get("librarySectionID"), str),
              repr(entry.get("librarySectionID")))
    status, paged, _ = get(base, "/status/sessions/history/all"
                                 "?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
    check("paged history carries totalSize and offset",
          paged.get("totalSize") is not None and paged.get("offset") == 0,
          "totalSize=%s offset=%s" % (paged.get("totalSize"), paged.get("offset")))

    # ------------------------------------------------------------------
    # 5. Edit, and Add to Collection (which is a tag edit)
    # ------------------------------------------------------------------
    print("\n=== context menu: Edit ===")
    status, body, _ = request(
        base, "PUT", "/library/metadata/%s?titleSort.value=%s&titleSort.locked=1"
                     % (movie, quote("zzz plexsim")))
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    locked = {f.get("name") for f in row.get("Field") or [] if f.get("locked")}
    check("edit writes the field and answers an empty 200",
          is_empty_200(status, body) and row.get("titleSort") == "zzz plexsim",
          "titleSort=%r" % row.get("titleSort"))
    check("edit records the field lock", "titleSort" in locked, ",".join(sorted(locked)))
    request(base, "PUT", "/library/metadata/%s?titleSort.value=&titleSort.locked=0" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("clearing an edited field removes it and its lock",
          "titleSort" not in row
          and "titleSort" not in {f.get("name") for f in row.get("Field") or []})

    status, body, _ = request(
        base, "PUT", "/library/metadata/%s?collection%%5B0%%5D.tag.tag=%s&collection.locked=1"
                     % (movie, quote("plexsim probe tag")))
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    tags = [t.get("tag") for t in row.get("Collection") or []]
    check("Add to Collection tags the row", is_empty_200(status, body)
          and "plexsim probe tag" in tags, ",".join(tags))
    _, collections, _ = get(base, "/library/sections/%s/collections" % movie_section)
    names = [c.get("title") for c in collections.get("Metadata") or []]
    check("the tagged collection appears in the section",
          "plexsim probe tag" in names, ",".join(names))
    request(base, "PUT", "/library/metadata/%s?collection%%5B%%5D.tag.tag-=%s"
                         % (movie, quote("plexsim probe tag")))
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("removing the tag clears it from the row",
          "plexsim probe tag" not in
          [t.get("tag") for t in (detail.get("Metadata") or [{}])[0].get("Collection") or []])

    # ------------------------------------------------------------------
    # 6. Refresh Metadata, Analyze
    # ------------------------------------------------------------------
    print("\n=== context menu: Refresh Metadata / Analyze ===")
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    before_thumb = (detail.get("Metadata") or [{}])[0].get("thumb")
    status, body, _ = request(base, "PUT", "/library/metadata/%s/refresh" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    after_thumb = (detail.get("Metadata") or [{}])[0].get("thumb")
    check("refresh answers an empty 200", is_empty_200(status, body), "HTTP %s" % status)
    if before_thumb:
        check("refresh re-versions the artwork key", after_thumb != before_thumb,
              "%s -> %s" % (before_thumb, after_thumb))
    else:
        skip("refresh re-versions the artwork key", "row has no artwork to version")
    status, activities, _ = get(base, "/activities")
    check("refresh reports an activity", bool(activities.get("Activity")),
          "%d activity(ies)" % len(activities.get("Activity") or []))
    if activities.get("Activity"):
        activity = activities["Activity"][0]
        check("activity carries the measured field set",
              all(k in activity for k in ("uuid", "type", "cancellable", "userID",
                                          "title", "subtitle", "progress")),
              activity.get("type"))
        status, body, _ = request(base, "DELETE", "/activities/%s" % activity["uuid"])
        check("an activity can be cancelled", is_empty_200(status, body), "HTTP %s" % status)
    status, body, _ = request(base, "PUT", "/library/metadata/%s/analyze" % movie)
    check("analyze answers an empty 200", is_empty_200(status, body), "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 7. Fix Match / Match / Unmatch
    # ------------------------------------------------------------------
    print("\n=== context menu: Fix Match / Unmatch ===")
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    original = (detail.get("Metadata") or [{}])[0]
    status, matches, _ = get(base, "/library/metadata/%s/matches?manual=1&title=%s"
                                   "&agent=tv.plex.agents.movie&language=en-US"
                                   % (movie, quote(original.get("title") or "")))
    candidates = matches.get("SearchResult") or []
    check("matches answers 200", status == 200, "HTTP %s" % status)
    if candidates:
        check("match candidates carry guid and name",
              all(c.get("guid") and c.get("name") for c in candidates),
              "%d candidate(s)" % len(candidates))
    else:
        skip("match candidates carry guid and name",
             "no TMDB key configured, so there is nothing to match against")

    status, body, _ = request(base, "PUT", "/library/metadata/%s/unmatch" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("unmatch answers an empty 200", is_empty_200(status, body), "HTTP %s" % status)
    # The client shows "Match..." instead of "Fix Match..." precisely when the
    # guid is a local:// one, and neither entry when there is no guid at all.
    # Measured: the guid becomes `tv.plex.agents.none://<ratingKey>`. Either
    # prefix flips the menu from "Fix Match..." to "Match...", because both
    # predicates test the guid for `local://` or `none://` -- but an ABSENT guid
    # offers neither entry, which is the failure this guards.
    check("unmatch leaves a none:// guid, not an absent one",
          "none://" in str(row.get("guid") or "")
          or str(row.get("guid") or "").startswith("local://"), row.get("guid"))
    check("unmatch keeps the artwork the tile needs",
          bool(row.get("thumb")) and bool(row.get("Image")),
          "thumb=%s" % row.get("thumb"))
    check("unmatch keeps the external Guid array and empties the summary",
          row.get("summary") == "" and (row.get("Guid") is not None
                                        or not original.get("Guid")),
          "summary=%r Guid=%s" % (row.get("summary"), bool(row.get("Guid"))))
    check("unmatch drops what the agent supplied",
          not any(row.get(k) for k in ("Genre", "Role", "Director", "studio",
                                       "contentRating", "audienceRating")),
          ",".join(k for k in ("Genre", "Role", "Director", "studio",
                               "contentRating", "audienceRating") if row.get(k))
          or "all gone")
    _, still, _ = get(base, "/library/sections/%s/all?unmatched=1" % movie_section)
    check("an unmatched row is found by the unmatched filter",
          any(str(r.get("ratingKey")) == str(movie)
              for r in still.get("Metadata") or []),
          "%d unmatched row(s)" % len(still.get("Metadata") or []))
    filename = None
    for media in original.get("Media") or []:
        for part in media.get("Part") or []:
            filename = str(part.get("file") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            break
    if filename:
        # An unmatched row is named by its file: keeping the agent's title would
        # leave it claiming an identity it no longer has.
        check("unmatch retitles the row from its file name",
              row.get("title") == filename, "%r" % row.get("title"))
    else:
        skip("unmatch retitles the row from its file name", "row has no Part file")
    if candidates and candidates[0].get("guid"):
        chosen = candidates[0]
        status, body, _ = request(
            base, "PUT", "/library/metadata/%s/match?guid=%s&name=%s&year=%s"
                         % (movie, quote(chosen["guid"]),
                            quote(chosen.get("name") or ""),
                            chosen.get("year") or ""))
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        row = (detail.get("Metadata") or [{}])[0]
        check("match applies the chosen guid",
              is_empty_200(status, body) and row.get("guid") == chosen["guid"],
              row.get("guid"))
    else:
        skip("match applies the chosen guid", "no candidates to choose from")

    # ------------------------------------------------------------------
    # 8. Change Artwork
    # ------------------------------------------------------------------
    print("\n=== context menu: artwork ===")
    for kind in ("posters", "arts"):
        status, listing, _ = get(base, "/library/metadata/%s/%s" % (movie, kind))
        entries = listing.get("Metadata") or []
        check("%s lists candidates with a selected flag" % kind,
              status == 200 and entries
              and all("selected" in e and e.get("key") for e in entries),
              "%d candidate(s)" % len(entries))
    status, listing, _ = get(base, "/library/metadata/%s/posters" % movie)
    entries = [e for e in (listing.get("Metadata") or []) if e.get("key")]
    if entries:
        status, body, _ = request(base, "PUT", "/library/metadata/%s/posters?url=%s"
                                               % (movie, quote(entries[-1]["key"])))
        check("selecting a poster answers an empty 200", is_empty_200(status, body),
              "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 9. Get Info
    # ------------------------------------------------------------------
    print("\n=== artwork: composites and requested sizes ===")
    # A real PMS builds a 400x600 mosaic of its members' posters. plexsim has no
    # imaging library and will not fabricate one, so it serves one member's own
    # poster -- honestly one of the images that mosaic is made of. The failure
    # this guards is the flat generated grey tile it showed before.
    def image_at(path_):
        status_, body_, headers_ = request(base, "GET", path_, want_json=False)
        return status_, headers_.get("Content-Type", ""), len(body_)

    status, kind, size = image_at("/library/sections/%s/composite/1?width=400" % movie_section)
    if kind.startswith("image/") and "png" not in kind:
        check("a section composite serves a member's own poster", size > 5000,
              "%s %d bytes" % (kind, size))
        narrow = image_at("/library/sections/%s/composite/1?width=185" % movie_section)
        wide = image_at("/library/sections/%s/composite/1?width=1280" % movie_section)
        # plexsim cannot resize; it asks TMDB for the variant nearest the width
        # the client wanted, so the two answers are different images.
        check("the requested width picks a different upstream variant",
              narrow[2] != wide[2], "%d vs %d bytes" % (narrow[2], wide[2]))
    else:
        skip("a section composite serves a member's own poster",
             "no row in this catalog has artwork (%s)" % (kind or status))

    print("\n=== context menu: Get Info ===")
    status, tree, _ = get(base, "/library/metadata/%s/tree" % movie)
    items = tree.get("MetadataItem") or []
    check("tree returns the item, its media and its parts",
          status == 200 and items and items[0].get("MediaItem")
          and items[0]["MediaItem"][0].get("MediaPart"),
          "%d item(s)" % len(items))

    # ------------------------------------------------------------------
    # 10. Download
    # ------------------------------------------------------------------
    print("\n=== context menu: Download ===")
    part_file = None
    for media in movies[0].get("Media") or []:
        for part in media.get("Part") or []:
            part_file = str(part.get("file") or "").rsplit("/", 1)[-1]
            break
    if part_id is None:
        skip("download serves an attachment named after the media file",
             "the row has no Part")
    else:
        status, _, headers = request(
            base, "HEAD", "/library/parts/%s/file.mkv?download=1" % part_id,
            want_json=False)
        disposition = headers.get("Content-Disposition", "")
        if status == 502:
            # The bytes come from the provider, so this needs a catalog whose
            # links the configured RD account owns. Verified by hand against a
            # live link: 206, video/x-matroska, and
            # `attachment; filename="For the Birds (2000).mkv"` plus the
            # RFC 5987 form -- the shape measured on a real PMS.
            skip("download serves an attachment named after the media file",
                 "part unresolvable: catalog links are account-scoped "
                 "(HTTP 502 = the provider said unavailable_file)")
        else:
            check("download serves an attachment named after the media file",
                  status in (200, 206) and "attachment" in disposition
                  and (not part_file or part_file in disposition),
                  "HTTP %s %s" % (status, disposition))

    # ------------------------------------------------------------------
    # 11. Select audio / subtitle stream
    # ------------------------------------------------------------------
    print("\n=== player: stream selection ===")
    if subtitle_part is None:
        skip("selecting a subtitle stream marks it selected",
             "no row in this catalog carries a subtitle stream")
    else:
        _, detail, _ = get(base, "/library/metadata/%s" % subtitle_row)
        row = (detail.get("Metadata") or [{}])[0]
        subtitles = [s for media in row.get("Media") or []
                     for part in media.get("Part") or []
                     for s in part.get("Stream") or []
                     if s.get("streamType") == 3]
        wanted = subtitles[-1]["id"]
        status, body, _ = request(base, "PUT", "/library/parts/%s?subtitleStreamID=%s&allParts=1"
                                               % (subtitle_part, wanted))
        _, detail, _ = get(base, "/library/metadata/%s" % subtitle_row)
        row = (detail.get("Metadata") or [{}])[0]
        chosen = [s.get("id") for media in row.get("Media") or []
                  for part in media.get("Part") or []
                  for s in part.get("Stream") or []
                  if s.get("streamType") == 3 and s.get("selected")]
        check("selecting a subtitle stream marks it selected",
              is_empty_200(status, body) and chosen == [wanted],
              "selected=%s wanted=%s" % (chosen, wanted))

    # ------------------------------------------------------------------
    # 12. Play, Play Next, Add to Queue, Shuffle
    # ------------------------------------------------------------------
    print("\n=== context menu: play queue ===")
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie)
    status, queue, _ = container(base, "POST", "/playQueues?type=video&shuffle=0&repeat=0"
                                              "&own=1&uri=%s" % quote(uri))
    check("play queue created", status == 200 and queue.get("playQueueID"),
          "playQueueID=%s" % queue.get("playQueueID"))
    check("play queue reports its source uri",
          queue.get("playQueueSourceURI") == uri, queue.get("playQueueSourceURI"))
    queue_id = queue.get("playQueueID")
    section_uri = "server://%s/com.plexapp.plugins.library/library/sections/%s/all" \
                  % (machine, movie_section)
    status, full, _ = container(base, "POST", "/playQueues?type=video&uri=%s"
                                             % quote(section_uri))
    check("Play All enqueues the whole section",
          status == 200 and full.get("playQueueTotalCount") == len(movies),
          "%s of %d" % (full.get("playQueueTotalCount"), len(movies)))
    if len(movies) > 1:
        status, shuffled, _ = container(base, "POST", "/playQueues?type=video&shuffle=1&uri=%s"
                                                     % quote(section_uri))
        order = [r.get("ratingKey") for r in shuffled.get("Metadata") or []]
        plain = [r.get("ratingKey") for r in full.get("Metadata") or []]
        check("Shuffle changes the queue order",
              shuffled.get("playQueueShuffled") is True and order != plain,
              "shuffled=%s" % shuffled.get("playQueueShuffled"))
    else:
        skip("Shuffle changes the queue order", "only one item in the section")

    if queue_id and spare:
        next_uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" \
                   % (machine, spare)
        status, after, _ = container(base, "PUT", "/playQueues/%s?uri=%s&next=1&type=video"
                                                 % (queue_id, quote(next_uri)))
        order = [str(r.get("ratingKey")) for r in after.get("Metadata") or []]
        check("Play Next inserts after the current item",
              status == 200 and len(order) == 2 and order[1] == str(spare),
              "order=%s" % order)
        status, after, _ = container(base, "PUT", "/playQueues/%s?uri=%s&type=video"
                                                 % (queue_id, quote(next_uri)))
        order = [str(r.get("ratingKey")) for r in after.get("Metadata") or []]
        check("Add to Queue appends", status == 200 and order[-1] == str(spare),
              "order=%s" % order)
    else:
        skip("Play Next inserts after the current item", "need a second row")

    # ------------------------------------------------------------------
    # 13. Add to Playlist
    # ------------------------------------------------------------------
    print("\n=== context menu: Add to Playlist ===")
    status, created, _ = container(
        base, "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                      % (quote("plexsim probe"), quote(uri)))
    playlist = (created.get("Metadata") or [{}])[0]
    check("playlist created", status == 200 and playlist.get("ratingKey"),
          "ratingKey=%s" % playlist.get("ratingKey"))
    check("playlist row carries the measured field set",
          all(k in playlist for k in ("ratingKey", "key", "guid", "type", "title",
                                      "smart", "playlistType", "composite",
                                      "duration", "leafCount", "addedAt", "updatedAt")),
          ",".join(sorted(playlist)))
    playlist_key = playlist.get("ratingKey")
    if playlist_key:
        _, listing, _ = get(base, "/playlists")
        check("the playlist is listed",
              any(p.get("ratingKey") == playlist_key
                  for p in listing.get("Metadata") or []))
        status, items, _ = get(base, "/playlists/%s/items" % playlist_key)
        rows = items.get("Metadata") or []
        check("playlist items carry a playlistItemID",
              status == 200 and rows and all(r.get("playlistItemID") for r in rows),
              "%d item(s)" % len(rows))
        if spare:
            add_uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" \
                      % (machine, spare)
            status, added, _ = container(base, "PUT", "/playlists/%s/items?uri=%s"
                                                     % (playlist_key, quote(add_uri)))
            check("adding to a playlist reports leafCountAdded",
                  status == 200 and added.get("leafCountAdded") == 1
                  and added.get("leafCountRequested") == 1,
                  "added=%s requested=%s" % (added.get("leafCountAdded"),
                                             added.get("leafCountRequested")))
        _, items, _ = get(base, "/playlists/%s/items" % playlist_key)
        rows = items.get("Metadata") or []
        if rows and rows[0].get("playlistItemID"):
            item_id = rows[0]["playlistItemID"]
            status, removed, _ = container(base, "DELETE", "/playlists/%s/items/%s"
                                                          % (playlist_key, item_id))
            _, items, _ = get(base, "/playlists/%s/items" % playlist_key)
            check("removing a playlist item returns the playlist",
                  status == 200 and (removed.get("Metadata") or [{}])[0].get("ratingKey")
                  == playlist_key
                  and all(r.get("playlistItemID") != item_id
                          for r in items.get("Metadata") or []),
                  "%d item(s) left" % len(items.get("Metadata") or []))
        # The removal above left one item; put another back so the reorder has
        # something to reorder.
        if spare:
            container(base, "PUT", "/playlists/%s/items?uri=%s"
                                   % (playlist_key,
                                      quote("server://%s/com.plexapp.plugins.library"
                                            "/library/metadata/%s" % (machine, spare))))
        _, items, _ = get(base, "/playlists/%s/items" % playlist_key)
        order = [r.get("playlistItemID") for r in items.get("Metadata") or []]
        if len(order) < 2:
            skip("a playlist item can be dragged to the front",
                 "need two items in the playlist")
        else:
            status, moved, _ = container(base, "PUT", "/playlists/%s/items/%s/move"
                                                     % (playlist_key, order[-1]))
            _, items, _ = get(base, "/playlists/%s/items" % playlist_key)
            now = [r.get("playlistItemID") for r in items.get("Metadata") or []]
            # Measured: no `after` means the front, and the response is the
            # playlist row. Drag-to-reorder is the only way the order changes.
            check("a playlist item can be dragged to the front",
                  status == 200 and now and now[0] == order[-1],
                  "%s -> %s" % (order, now))
            status, _, _ = container(base, "PUT", "/playlists/%s/items/%s/move?after=%s"
                                                 % (playlist_key, now[0], now[-1]))
            _, items, _ = get(base, "/playlists/%s/items" % playlist_key)
            after = [r.get("playlistItemID") for r in items.get("Metadata") or []]
            check("a playlist item can be dragged after another",
                  status == 200 and after and after[-1] == now[0],
                  "%s -> %s" % (now, after))
        status, kind, size = image_at("/playlists/%s/composite/1?width=400" % playlist_key)
        check("a playlist composite answers an image",
              status == 200 and kind.startswith("image/"), "%s %d bytes" % (kind, size))
        status, body, _ = request(base, "PUT", "/playlists/%s?title=%s"
                                              % (playlist_key, quote("plexsim renamed")))
        _, one, _ = get(base, "/playlists/%s" % playlist_key)
        check("renaming a playlist works",
              is_empty_200(status, body)
              and (one.get("Metadata") or [{}])[0].get("title") == "plexsim renamed",
              (one.get("Metadata") or [{}])[0].get("title"))
        status, body, _ = request(base, "DELETE", "/playlists/%s" % playlist_key)
        # Measured: a playlist delete is 204, while a collection delete is 200.
        check("deleting a playlist answers 204", status == 204 and body == b"",
              "HTTP %s" % status)
        status, _, _ = get(base, "/playlists/%s" % playlist_key)
        check("the deleted playlist is gone", status == 404, "HTTP %s" % status)
    _, listing, _ = get(base, "/playlists?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
    check("a paged playlist list carries totalSize and offset",
          listing.get("offset") == 0 and listing.get("totalSize") is not None,
          "totalSize=%s offset=%s" % (listing.get("totalSize"), listing.get("offset")))
    _, unpaged_list, _ = get(base, "/playlists")
    check("an unpaged playlist list carries neither",
          "totalSize" not in unpaged_list and "offset" not in unpaged_list,
          ",".join(sorted(k for k in unpaged_list if not isinstance(unpaged_list[k], list))))
    # Two of them, because a direction can only be observed across two rows --
    # and the playlist the checks above built has been deleted by now.
    sort_keys = []
    for name in ("aaa plexsim sort probe", "zzz plexsim sort probe"):
        _, made_sort, _ = container(base, "POST",
                                    "/playlists?type=video&title=%s&smart=0&uri=%s"
                                    % (quote(name), quote(uri)))
        key = ((made_sort.get("Metadata") or [{}])[0]).get("ratingKey")
        if key:
            sort_keys.append(key)
    _, up, _ = get(base, "/playlists/all?type=15&sort=titleSort:asc")
    _, down, _ = get(base, "/playlists/all?type=15&sort=titleSort:desc")
    up_titles = [r.get("title") for r in up.get("Metadata") or []]
    down_titles = [r.get("title") for r in down.get("Metadata") or []]
    if len(up_titles) > 1:
        # A playlist row has no titleSort of its own, so keying the sort on that
        # alone made every row compare equal and the direction do nothing.
        check("a playlist sort direction is honoured",
              up_titles == list(reversed(down_titles)),
              "%s vs %s" % (up_titles, down_titles))
    else:
        skip("a playlist sort direction is honoured", "need two playlists")
    for key in sort_keys:
        request(base, "DELETE", "/playlists/%s" % key)
    status, sorts, _ = get(base, "/playlists/sorts")
    check("playlist sorts are offered",
          status == 200 and len(sorts.get("Directory") or []) >= 6,
          "%d sort(s)" % len(sorts.get("Directory") or []))

    # ------------------------------------------------------------------
    # 14. Collections as items of their own
    # ------------------------------------------------------------------
    print("\n=== collections ===")
    status, created, _ = container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
                      % (quote("plexsim collection"), movie_section, quote(uri)))
    collection = (created.get("Metadata") or [{}])[0]
    check("collection created", status == 200 and collection.get("ratingKey"),
          "ratingKey=%s" % collection.get("ratingKey"))
    check("collection row carries the measured field set",
          all(k in collection for k in ("ratingKey", "key", "guid", "type", "title",
                                        "subtype", "index", "thumb", "childCount",
                                        "addedAt", "updatedAt")),
          ",".join(sorted(collection)))
    collection_key = collection.get("ratingKey")
    if collection_key:
        status, kind, size = image_at("/library/collections/%s/composite/1?width=400"
                                      % collection_key)
        check("a collection composite answers an image",
              status == 200 and kind.startswith("image/"), "%s %d bytes" % (kind, size))
        status, children, _ = get(base, "/library/collections/%s/children" % collection_key)
        check("collection children list its members",
              status == 200 and any(str(r.get("ratingKey")) == str(movie)
                                    for r in children.get("Metadata") or []),
              "%d member(s)" % len(children.get("Metadata") or []))
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        tags = [t.get("tag") for t in (detail.get("Metadata") or [{}])[0].get("Collection") or []]
        check("a member row carries the collection as a tag",
              "plexsim collection" in tags, ",".join(tags))
        if spare:
            add_uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" \
                      % (machine, spare)
            container(base, "PUT", "/library/collections/%s/items?uri=%s"
                                   % (collection_key, quote(add_uri)))
            _, children, _ = get(base, "/library/collections/%s/children" % collection_key)
            order = [r.get("ratingKey") for r in children.get("Metadata") or []]
            if len(order) > 1:
                status, body, _ = request(base, "PUT",
                                          "/library/collections/%s/items/%s/move"
                                          % (collection_key, order[-1]))
                _, children, _ = get(base,
                                     "/library/collections/%s/children" % collection_key)
                now = [r.get("ratingKey") for r in children.get("Metadata") or []]
                # Keyed by the member's own ratingKey here, unlike a playlist's
                # move, which uses the playlistItemID.
                check("a collection member can be reordered",
                      is_empty_200(status, body) and now and now[0] == order[-1],
                      "%s -> %s" % (order, now))
            else:
                skip("a collection member can be reordered", "need two members")
        else:
            skip("a collection member can be reordered", "need a second row")
        status, body, _ = request(base, "PUT", "/library/collections/%s?title=%s"
                                              % (collection_key, quote("plexsim renamed")))
        _, one, _ = get(base, "/library/collections/%s" % collection_key)
        check("renaming a collection retags its members",
              is_empty_200(status, body)
              and (one.get("Metadata") or [{}])[0].get("title") == "plexsim renamed",
              (one.get("Metadata") or [{}])[0].get("title"))
        status, body, _ = request(base, "DELETE", "/library/collections/%s" % collection_key)
        # Measured: 200 here, unlike the playlist delete's 204.
        check("deleting a collection answers an empty 200", is_empty_200(status, body),
              "HTTP %s" % status)
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        check("deleting a collection untags its members",
              not (detail.get("Metadata") or [{}])[0].get("Collection"))
    status, _, _ = get(base, "/library/collections")
    check("the bare collections endpoint 404s, as a real PMS does", status == 404,
          "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 14b. what a real PMS refuses -- answering 200 here tells the client the
    #      endpoint exists and is empty, which is the same trap as answering a
    #      needed endpoint with a clean refusal, from the other side.
    # ------------------------------------------------------------------
    print("\n=== refusals, measured against a real PMS ===")
    for method, path, want, why in (
            ("GET", "/plexsim/nonsense/path", 404, "an unrouted path"),
            ("POST", "/player/playback/play?commandID=1", 400, "player commands"),
            ("GET", "/player/timeline/poll?wait=0", 400, "player timeline"),
            ("GET", "/system/proxy?url=https%3A%2F%2Fplex.tv", 400, "the plex.tv proxy"),
            ("GET", "/library/platforms/plex", 404, "platforms"),
            ("GET", "/hubs/sections/home", 404, "the home hub path"),
            ("GET", "/hubs/metadata", 404, "hub metadata"),
            ("GET", "/library/metadata/%s/markers" % movie, 404, "markers"),
            ("GET", "/library/metadata/%s/banner/1" % movie, 404, "banner artwork"),
            ("GET", "/library/metadata/%s/chapterImages/1" % movie, 404, "chapter images"),
            ("GET", "/library/metadata/%s/indexes/sd" % movie, 404, "preview indexes"),
            ("GET", "/library/metadata/%s/prefs" % movie, 404, "reading item prefs"),
            ("PUT", "/library/metadata/%s/move?after=1" % movie, 404, "moving an item"),
            ("GET", "/library/sections/%s/search?query=a" % movie_section, 400,
             "section search"),
            ("GET", "/library/sections/%s/albums" % movie_section, 404, "a music leaf"),
            ("GET", "/library/streams/1", 501, "sidecar stream extraction"),
            ("GET", "/video/:/transcode/universal/subtitles?session=x", 404,
             "a transcode session"),
            ("GET", "/library/optimize", 404, "optimize via GET"),
            ("GET", "/system/appstore/updates", 404, "the channel appstore"),
            ("GET", "/library/collections", 404, "the bare collections path"),
            ("GET", "/playlists/generators?type=42", 404, "version generators")):
        status, _, _ = container(base, method, path)
        check("%s answers %s" % (why, want), status == want, "HTTP %s" % status)

    print("\n=== concurrency ===")
    # plexsim is a ThreadingHTTPServer and everything the UI writes -- playlists,
    # collections, catalog rows -- is mutable state with no lock around it. This
    # was measured rather than assumed: 33,300 requests with three threads
    # churning the items dict through merge/split while four iterated it produced
    # zero tracebacks, and 320 concurrent playlist creates produced 320 unique
    # keys. No lock was added, because none was needed. The check is here so that
    # if a future change breaks it, it breaks loudly instead of handing two
    # clients the same ratingKey.
    import threading as _threading

    created, failures = [], []
    guard = _threading.Lock()

    def hammer(worker):
        local, bad = [], []
        for index in range(6):
            status, body, _ = request(
                base, "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                              % (quote("concurrent %d-%d" % (worker, index)), quote(uri)))
            if status != 200:
                bad.append("create -> %s" % status)
                continue
            try:
                local.append(json.loads(body)["MediaContainer"]["Metadata"][0]["ratingKey"])
            except (ValueError, KeyError, IndexError):
                bad.append("create -> unparseable")
            for path_ in ("/playlists", "/library/all", "/hubs",
                          "/library/sections/%s/collections" % movie_section):
                code, _, _ = request(base, "GET", path_)
                if code != 200:
                    bad.append("%s -> %s" % (path_, code))
        with guard:
            created.extend(local)
            failures.extend(bad)

    workers = [_threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    check("concurrent writes and reads never fail", not failures,
          "; ".join(failures[:3]) or "%d write(s), %d read(s)"
          % (len(created), len(created) * 4))
    check("concurrent creates never reuse a ratingKey",
          len(created) == len(set(created)), "%d created, %d unique"
          % (len(created), len(set(created))))
    for key in created:
        request(base, "DELETE", "/playlists/%s" % key)

    print("\n=== the notification socket ===")

    def stir():
        request(base, "PUT", "/library/metadata/%s/refresh" % movie)
        get(base, "/library/sections/%s/refresh" % movie_section)
        request(base, "POST", "/butler/RefreshLibraries")

    status, frames = notifications(base, stir, seconds=8)
    kinds = {(f.get("NotificationContainer") or {}).get("type") for f in frames}
    check("the notification socket completes a real upgrade", status == "101",
          str(status))
    # Measured on the reference: these four are what a real PMS pushes. Without
    # them a client polls, and never sees a change made from another device.
    for kind, element in (("activity", "ActivityNotification"),
                          ("timeline", "TimelineEntry"),
                          ("status", "StatusNotification"),
                          ("progress", "ProgressNotification")):
        rows = [r for f in frames
                for r in ((f.get("NotificationContainer") or {}).get(element) or [])
                if (f.get("NotificationContainer") or {}).get("type") == kind]
        check("the socket pushes %s notifications" % kind, bool(rows),
              "%d frame(s)" % len(rows))
    activity_rows = [r for f in frames
                     for r in ((f.get("NotificationContainer") or {})
                               .get("ActivityNotification") or [])]
    if activity_rows:
        check("an activity notification carries the event and the Activity",
              all(r.get("event") and r.get("uuid")
                  and (r.get("Activity") or {}).get("type") for r in activity_rows),
              activity_rows[0].get("event"))
    timeline_rows = [r for f in frames
                     for r in ((f.get("NotificationContainer") or {})
                               .get("TimelineEntry") or [])]
    if timeline_rows:
        entry = timeline_rows[0]
        # sectionID and itemID are STRINGS on the reference, and `type` is Plex's
        # numeric code rather than the word.
        check("a timeline entry carries the measured field set",
              isinstance(entry.get("sectionID"), str)
              and isinstance(entry.get("itemID"), str)
              and isinstance(entry.get("type"), int)
              and entry.get("metadataState") and entry.get("updatedAt"),
              json.dumps(entry)[:110])

    print("\n=== endpoints that are not the UI's but are a server's ===")
    # The one root element on the whole surface that is NOT a MediaContainer, so
    # it has to be read without the container helper.
    status, body, _ = request(base, "GET", "/butler")
    try:
        tasks = json.loads(body).get("ButlerTasks", {}).get("ButlerTask") or []
    except ValueError:
        tasks = []
    check("scheduled tasks are listed under a ButlerTasks root",
          status == 200 and b'"ButlerTasks"' in body and tasks,
          "%d task(s)" % len(tasks))
    if tasks:
        name = tasks[0].get("name")
        status, body, _ = request(base, "POST", "/butler/%s" % name)
        check("a scheduled task can be run now", is_empty_200(status, body),
              "%s -> HTTP %s" % (name, status))
        status, _, _ = request(base, "POST", "/butler/NoSuchTask")
        check("an unknown scheduled task is a 404", status == 404, "HTTP %s" % status)
    _, sections, _ = get(base, "/library/sections")
    directory = (sections.get("Directory") or [{}])[0]
    # `filters` is what tells a client the section HAS a filter bar, and the four
    # timestamps were all absent.
    check("a section row carries filters and its timestamps",
          all(k in directory for k in ("filters", "createdAt", "updatedAt",
                                       "scannedAt", "contentChangedAt",
                                       "metadataAgentProviderGroupId")),
          ",".join(sorted(directory)))
    _, tree, _ = get(base, "/library/metadata/%s/tree" % movie)
    part = (((tree.get("MetadataItem") or [{}])[0].get("MediaItem")
             or [{}])[0].get("MediaPart") or [{}])[0]
    check("the tree carries a part's streams as MediaStream",
          part.get("MediaStream") and part["MediaStream"][0].get("type") is not None,
          ",".join(sorted(part)))
    status, library, _ = get(base, "/library")
    # python-plexapi asks for this before anything else; the old catch-all's
    # empty container is what hid the fact that it was never routed.
    check("the library root lists its three directories",
          status == 200
          and [d.get("key") for d in library.get("Directory") or []]
          == ["sections", "recentlyAdded", "onDeck"],
          ",".join(d.get("key") for d in library.get("Directory") or []))
    status, system, _ = get(base, "/system")
    check("the system provider lists its directories",
          status == 200 and len(system.get("Directory") or []) == 2
          and system.get("identifier") == "com.plexapp.system",
          ",".join(d.get("key") for d in system.get("Directory") or []))
    status, servers, _ = get(base, "/servers")
    check("the legacy server list names this server",
          status == 200
          and (servers.get("Server") or [{}])[0].get("machineIdentifier") == machine,
          str((servers.get("Server") or [{}])[0].get("name")))
    status, logs, _ = get(base, "/logs")
    check("the log endpoint answers with content, not a zip",
          status == 200 and "content" in logs, ",".join(sorted(logs)))
    status, body, _ = request(base, "POST",
                              "/log?level=4&message=plexsim+test&source=Plex+Web")
    check("the client's own log lines are accepted", is_empty_200(status, body),
          "HTTP %s" % status)
    status, top, _ = get(base, "/library/all/top?type=1")
    check("the most-played list answers", status == 200, "HTTP %s" % status)
    status, filtered, _ = get(base, "/hubs?identifier=home.continue")
    identifiers = [h.get("hubIdentifier") for h in filtered.get("Hub") or []]
    # Ignoring `identifier` makes Home draw rows it did not ask for.
    check("the home hubs honour the identifier filter",
          status == 200 and identifiers == ["home.continue"],
          ",".join(str(i) for i in identifiers))
    status, subtitles, _ = get(base, "/library/metadata/%s/subtitles?language=en" % movie)
    check("the subtitle search dialog gets its shape back",
          status == 200 and subtitles.get("identifier") == "com.plexapp.plugins.library",
          ",".join(sorted(subtitles)))
    status, body, _ = request(base, "PUT", "/library/metadata/%s/prefs?episodeSort=0" % movie)
    check("an item preference write is accepted", is_empty_200(status, body),
          "HTTP %s" % status)
    status, unwatched, _ = get(base, "/library/sections/%s/unwatched" % movie_section)
    check("the unwatched listing answers", status == 200,
          "%d row(s)" % len(unwatched.get("Metadata") or []))
    status, resolution, _ = get(base, "/library/sections/%s/resolution" % movie_section)
    check("the resolution axis is built from the Media",
          status == 200 and resolution.get("Directory"),
          ",".join(d.get("title") for d in resolution.get("Directory") or []))
    if resolution.get("Directory"):
        _, rows, _ = get(base, resolution["Directory"][0]["fastKey"])
        check("a resolution entry filters the listing",
              bool(rows.get("Metadata")),
              "%d row(s)" % len(rows.get("Metadata") or []))

    print("\n=== CORS, which a browser enforces before the server sees anything ===")
    # Measured either side of an Origin header: a real PMS ECHOES the origin and
    # echoes the requested header list on a preflight, and sends no CORS headers
    # at all without an Origin. plexsim answered `*` unconditionally and
    # published a FIXED header list that omitted `X-Plex-Session-Id`,
    # `X-Plex-Language` and others the client sends -- a preflight that omits one
    # makes the browser refuse the request with no server-side trace.
    wanted = "x-plex-token,x-plex-session-id,x-plex-language,accept"
    status, _, headers = request(base, "OPTIONS",
                                 "/library/metadata/%s/refresh" % movie, want_json=False)
    check("a preflight is answered", status == 200, "HTTP %s" % status)
    request_headers = {"Origin": "https://app.plex.tv",
                       "Access-Control-Request-Method": "PUT",
                       "Access-Control-Request-Headers": wanted}
    preflight = urllib.request.Request(base.rstrip("/") + "/library/metadata/%s/refresh" % movie,
                                       method="OPTIONS", headers=request_headers)
    try:
        with urllib.request.urlopen(preflight, timeout=20) as response:
            cors = dict(response.headers)
    except Exception as error:
        cors = {"error": str(error)}
    check("a preflight echoes the requested headers",
          cors.get("Access-Control-Allow-Headers") == wanted,
          cors.get("Access-Control-Allow-Headers"))
    check("a preflight echoes the origin rather than a wildcard",
          cors.get("Access-Control-Allow-Origin") == "https://app.plex.tv",
          cors.get("Access-Control-Allow-Origin"))
    check("a preflight allows every verb the UI uses",
          all(verb in (cors.get("Access-Control-Allow-Methods") or "")
              for verb in ("GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS")),
          cors.get("Access-Control-Allow-Methods"))
    with_origin = urllib.request.Request(base.rstrip("/") + "/identity",
                                         headers={"Origin": "https://app.plex.tv"})
    with urllib.request.urlopen(with_origin, timeout=20) as response:
        plain = dict(response.headers)
    check("a normal response echoes the origin and varies on it",
          plain.get("Access-Control-Allow-Origin") == "https://app.plex.tv"
          and "Origin" in (plain.get("Vary") or ""),
          "%s | %s" % (plain.get("Access-Control-Allow-Origin"), plain.get("Vary")))
    status, _, bare = request(base, "GET", "/identity")
    check("no Origin means no CORS headers at all",
          "Access-Control-Allow-Origin" not in bare,
          ",".join(k for k in bare if k.lower().startswith("access-control")) or "none")

    print("\n=== the XML surface, which is plexsim's DEFAULT ===")
    # The playlist checks above deleted theirs; the XML element name for a
    # playlist row can only be checked against a list that has one.
    _, made, _ = container(base, "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                                         % (quote("plexsim xml probe"), quote(uri)))
    xml_playlist = ((made.get("Metadata") or [{}])[0]).get("ratingKey")
    # Every check above asks for JSON. A TV or mobile client sends no Accept
    # header at all and gets XML, and the mapping is not a generic json2xml:
    # the element name for a row comes from the row's `type`, and the endpoints
    # whose rows have no type need the endpoint to say. Three of them were
    # rendering <Video>.
    def xml_root(path):
        status, body, headers = request(base, "GET", path, want_json=False)
        if status != 200 or "xml" not in headers.get("Content-Type", ""):
            return status, None
        try:
            return status, ET.fromstring(body)
        except ET.ParseError:
            return status, None

    for path, wanted, why in (
            ("/library/metadata/%s/posters" % movie, "Photo", "an artwork list"),
            ("/library/metadata/%s/arts" % movie, "Photo", "a background list"),
            ("/library/sections/%s/folder" % movie_section, "Directory", "a folder list"),
            ("/library/shared/all", "Directory", "the shared library list"),
            ("/library/sections/%s/all" % movie_section, "Video", "a movie listing"),
            ("/playlists", "Playlist", "a playlist list")):
        status, root = xml_root(path)
        kids = {child.tag for child in (root or [])}
        check("%s renders as <%s>" % (why, wanted),
              root is not None and wanted in kids,
              "HTTP %s, got %s" % (status, ",".join(sorted(kids)) or "nothing"))
    if show:
        status, root = xml_root("/library/metadata/%s/children" % show)
        kids = {child.tag for child in (root or [])}
        check("a season list renders as <Directory>", "Directory" in kids,
              ",".join(sorted(kids)))
    status, root = xml_root("/butler")
    # The one root element on the surface that is not a MediaContainer.
    check("the scheduled task list keeps its ButlerTasks root in XML",
          root is not None and root.tag == "ButlerTasks",
          root.tag if root is not None else "unparseable")
    broken = []
    for path in ("/", "/identity", "/media/providers", "/:/prefs", "/library",
                 "/library/sections", "/system", "/servers", "/activities",
                 "/statistics/bandwidth", "/devices", "/accounts", "/updater/status",
                 "/hubs", "/hubs/sections/%s" % movie_section,
                 "/library/sections/%s/all?includeMeta=1" % movie_section,
                 "/library/sections/%s/filters" % movie_section,
                 "/library/sections/%s/categories" % movie_section,
                 "/library/metadata/%s" % movie, "/library/metadata/%s/tree" % movie,
                 "/library/metadata/%s/subtitles?language=en" % movie,
                 "/playlists/sorts", "/status/sessions/history/all"):
        status, root = xml_root(path)
        if root is None:
            broken.append("%s -> %s" % (path, status))
    check("every container endpoint renders parseable XML", not broken,
          "; ".join(broken) or "checked 23")
    status, root = xml_root("/library/metadata/%s" % movie)
    streams = root.findall(".//Stream") if root is not None else []
    video = [s for s in streams if s.get("streamType") == "1"]
    if video:
        # Measured on two real servers: the codec belongs in the EXTENDED title.
        # plexsim had `1080p (H.264)` in displayTitle, which is the string the
        # quality picker prints.
        check("a video stream's displayTitle is the resolution alone",
              "(" not in (video[0].get("displayTitle") or ""),
              video[0].get("displayTitle"))
        check("a video stream carries an extendedDisplayTitle with the codec",
              "(" in (video[0].get("extendedDisplayTitle") or ""),
              video[0].get("extendedDisplayTitle"))
    if subtitle_row:
        _, detail, _ = get(base, "/library/metadata/%s" % subtitle_row)
        subs = [s for media in (detail.get("Metadata") or [{}])[0].get("Media") or []
                for part in media.get("Part") or []
                for s in part.get("Stream") or [] if s.get("streamType") == 3]
        if subs:
            # A subtitle with a track title shows its LANGUAGE plainly and the
            # track name only in the extended title.
            check("a subtitle stream has both titles",
                  all(s.get("displayTitle") and s.get("extendedDisplayTitle")
                      for s in subs),
                  "%s | %s" % (subs[0].get("displayTitle"),
                               subs[0].get("extendedDisplayTitle")))

    if xml_playlist:
        request(base, "DELETE", "/playlists/%s" % xml_playlist)

    print("\n=== the eventsource channel, which carries the same events ===")
    sse_status, sse_events = eventsource(base, stir, seconds=8)
    check("the eventsource channel opens", sse_status == 200, str(sse_status))
    check("it carries the same events as the socket",
          {"activity", "timeline"} <= {name for name, _ in sse_events},
          ",".join(sorted({name for name, _ in sse_events})) or "nothing")
    # Measured: SSE sends ONE row under its element name, with no
    # NotificationContainer and no size around it -- a different wire format
    # from the websocket's, carrying the same events.
    payloads = [payload for name, payload in sse_events if name == "timeline"]
    if payloads:
        check("an eventsource frame is one bare row, not a container",
              "NotificationContainer" not in payloads[0]
              and "TimelineEntry" in payloads[0]
              and isinstance(json.loads(payloads[0]).get("TimelineEntry"), dict),
              payloads[0][:90])

    # ------------------------------------------------------------------
    # 15. Server settings pages
    # ------------------------------------------------------------------
    print("\n=== Settings pages ===")
    status, prefs, _ = get(base, "/:/prefs")
    settings = prefs.get("Setting") or []
    check("server prefs are served", status == 200 and len(settings) > 100,
          "%d setting(s)" % len(settings))
    groups = {s.get("group") for s in settings}
    for group in ("general", "library", "network", "transcoder", "dlna", "butler",
                  "extras"):
        check("Settings page has its group: %s" % group, group in groups)
    writable = next((s for s in settings
                     if s.get("type") == "bool" and s.get("group") == "library"), None)
    if writable is None:
        skip("a preference write round-trips", "no boolean library setting to flip")
    else:
        flipped = "0" if str(writable.get("value")).lower() in ("1", "true") else "1"
        status, body, _ = request(base, "PUT", "/:/prefs?%s=%s" % (writable["id"], flipped))
        _, prefs, _ = get(base, "/:/prefs")
        now = next((s for s in prefs.get("Setting") or []
                    if s.get("id") == writable["id"]), {})
        check("a preference write round-trips",
              is_empty_200(status, body) and str(now.get("value")) == flipped,
              "%s=%s" % (writable["id"], now.get("value")))
    status, section_prefs, _ = get(base, "/library/sections/%s/prefs" % movie_section)
    section_settings = section_prefs.get("Setting") or []
    check("the Edit Library dialog gets its library-type settings",
          status == 200 and section_settings,
          "%d setting(s)" % len(section_settings))
    flippable = next((s for s in section_settings if s.get("type") == "bool"), None)
    if flippable is None:
        skip("a section preference write round-trips", "no boolean section setting")
    else:
        flipped = "0" if str(flippable.get("value")).lower() in ("1", "true") else "1"
        status, body, _ = request(base, "PUT", "/library/sections/%s/prefs?%s=%s"
                                               % (movie_section, flippable["id"], flipped))
        _, section_prefs, _ = get(base, "/library/sections/%s/prefs" % movie_section)
        now = next((s for s in section_prefs.get("Setting") or []
                    if s.get("id") == flippable["id"]), {})
        # Section settings have their own id space, so the server-prefs writer
        # would drop every one of them.
        check("a section preference write round-trips",
              is_empty_200(status, body) and str(now.get("value")) == flipped,
              "%s=%s" % (flippable["id"], now.get("value")))

    status, agents, _ = get(base, "/system/agents")
    check("Agents page lists the agents plexsim identifies with",
          status == 200 and agents.get("Agent"),
          "%d agent(s)" % len(agents.get("Agent") or []))
    status, scanners, _ = get(base, "/system/scanners/%s" % movie_section)
    check("Edit Library dialog can list scanners",
          status == 200 and scanners.get("Directory"),
          "%d scanner(s)" % len(scanners.get("Directory") or []))
    status, token, _ = get(base, "/security/token?type=delegation&scope=all")
    check("a delegation token is issued", status == 200 and token.get("token"),
          token.get("token"))
    status, _, headers = request(base, "GET", "/diagnostics/logs", want_json=False)
    check("Download Logs returns a zip",
          status == 200 and "zip" in headers.get("Content-Type", ""),
          "%s %s" % (status, headers.get("Content-Type")))
    status, _, _ = get(base, "/diagnostics/databases")
    check("Download Database is refused, matching the advertised diagnostics",
          status == 404, "HTTP %s" % status)

    print("\n=== Status pages ===")
    for path, label in (("/status/sessions", "Now Playing"),
                        ("/status/sessions/background", "Conversions"),
                        ("/transcode/sessions", "Transcode sessions"),
                        ("/clients", "Clients"),
                        ("/statistics/bandwidth", "Dashboard bandwidth"),
                        ("/statistics/media", "Dashboard media"),
                        ("/statistics/resources", "Dashboard resources")):
        status, body_, _ = get(base, path)
        check("%s answers 200" % label, status == 200, "HTTP %s" % status)
    status, sessions, _ = get(base, "/status/sessions")
    check("an idle server omits Metadata rather than sending an empty list",
          "Metadata" not in sessions, ",".join(sorted(sessions)))
    status, devices, _ = get(base, "/devices")
    check("Devices page lists this server",
          status == 200 and devices.get("Device")
          and devices.get("identifier") == "com.plexapp.system.devices",
          "%d device(s)" % len(devices.get("Device") or []))
    status, accounts, _ = get(base, "/accounts")
    check("Accounts are served with the measured field set",
          status == 200
          and accounts.get("identifier") == "com.plexapp.system.accounts"
          and all(k in (accounts.get("Account") or [{}])[0]
                  for k in ("id", "key", "name", "defaultAudioLanguage",
                            "autoSelectAudio", "defaultSubtitleLanguage",
                            "subtitleMode", "thumb")),
          ",".join(sorted((accounts.get("Account") or [{}])[0])))
    status, updater, _ = get(base, "/updater/status")
    check("Updates page reports nothing to install",
          status == 200 and updater.get("canInstall") is False
          and updater.get("checkedAt"),
          "canInstall=%s" % updater.get("canInstall"))
    status, body, _ = request(base, "PUT", "/updater/check")
    check("Check for Updates answers an empty 200", is_empty_200(status, body),
          "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 16. Manage > Libraries
    # ------------------------------------------------------------------
    print("\n=== Manage > Libraries ===")
    for verb, path, label in (
            ("PUT", "/library/sections/%s/emptyTrash" % movie_section, "Empty Trash"),
            ("PUT", "/library/sections/%s/analyze" % movie_section, "Analyze Library"),
            ("GET", "/library/sections/%s/refresh" % movie_section, "Scan Library Files"),
            ("GET", "/library/sections/%s/refresh?force=1" % movie_section, "Refresh All"),
            ("DELETE", "/library/sections/%s/refresh" % movie_section, "Cancel Scan"),
            ("DELETE", "/library/sections/%s/indexes" % movie_section,
             "Delete Preview Thumbnails"),
            ("PUT", "/library/clean/bundles", "Clean Bundles"),
            ("PUT", "/library/optimize", "Optimize Database")):
        status, body, _ = request(base, verb, path)
        check("%s answers an empty 200" % label, is_empty_200(status, body),
              "HTTP %s" % status)
    status, _, _ = get(base, "/library/optimize")
    check("Optimize Database refuses a GET, as a real PMS does", status == 404,
          "HTTP %s" % status)
    status, sorts, _ = get(base, "/library/sections/%s/sorts" % movie_section)
    check("section sorts are a Directory list, not a Sort list",
          status == 200 and len(sorts.get("Directory") or []) == 9
          and not sorts.get("Sort"),
          "%d sort(s)" % len(sorts.get("Directory") or []))
    status, filters, _ = get(base, "/library/sections/%s/filters" % movie_section)
    check("section filters are a Directory list, not a Filter list",
          status == 200 and filters.get("Directory") and not filters.get("Filter"),
          "%d filter(s)" % len(filters.get("Directory") or []))
    check("filters say secondary in content and viewGroup",
          filters.get("content") == "secondary" and filters.get("viewGroup") == "secondary",
          "%s/%s" % (filters.get("content"), filters.get("viewGroup")))
    status, chars, _ = get(base, "/library/sections/%s/firstCharacter" % movie_section)
    check("first-character index carries per-letter counts",
          status == 200 and chars.get("Directory")
          and all(d.get("size") for d in chars["Directory"]),
          "%d letter(s)" % len(chars.get("Directory") or []))

    print("\n=== secondary filter axes ===")
    # A tag axis keys its entries by tag id and a field axis by the value, and
    # getting that wrong is invisible unless the filtered list is checked against
    # the rows themselves: a key of `None` matched every row that had any tag at
    # all, which looks like a working filter from the outside.
    TAG_AXES = {"genre": "Genre", "director": "Director", "country": "Country",
                "collection": "Collection", "actor": "Role", "writer": "Writer",
                "label": "Label"}
    FIELD_AXES = {"year": "year", "contentRating": "contentRating", "studio": "studio"}
    # Re-read the section rather than reusing the listing from the top of the
    # run: the Unmatch check above deliberately strips a row's tags, so the
    # expectation has to come from the library as it is now.
    _, current, _ = get(base, "/library/sections/%s/all" % movie_section)
    present = current.get("Metadata") or []
    for axis in ("genre", "year", "decade", "contentRating", "director", "country",
                 "studio", "collection", "actor", "writer", "label"):
        status, listing, _ = get(base, "/library/sections/%s/%s" % (movie_section, axis))
        entries = listing.get("Directory") or []
        ok = status == 200 and listing.get("viewGroup") == "secondary"
        detail = "%d entry(ies)" % len(entries)
        if entries:
            entry = entries[0]
            if not entry.get("fastKey"):
                check("By %s lists and filters" % axis, False, "entry has no fastKey")
                continue
            _, filtered, _ = get(base, entry["fastKey"])
            rows = filtered.get("Metadata") or []
            detail += ", first opens %d row(s)" % len(rows)
            ok = ok and bool(rows)
            if axis in TAG_AXES:
                field = TAG_AXES[axis]
                ok = ok and all(any(t.get("tag") == entry["title"]
                                    for t in row.get(field) or []) for row in rows)
                expected = [m for m in present
                            if any(t.get("tag") == entry["title"]
                                   for t in m.get(field) or [])]
                ok = ok and len(rows) == len(expected)
                detail += " of %d that carry it" % len(expected)
            elif axis in FIELD_AXES:
                field = FIELD_AXES[axis]
                ok = ok and all(str(row.get(field)) == entry["title"] for row in rows)
            elif axis == "decade":
                ok = ok and all(int(row.get("year") or 0) // 10 * 10
                                == int(entry["key"]) for row in rows)
        check("By %s lists and filters" % axis, ok, detail)
    status, categories, _ = get(base, "/library/sections/%s/categories" % movie_section)
    tiles = categories.get("Directory") or []
    check("the Categories pivot lists genre tiles",
          status == 200 and tiles
          and all(t.get("type") == "directory" and t.get("key") for t in tiles)
          and (categories.get("Meta") or {}).get("Type"),
          "%d tile(s)" % len(tiles))
    if tiles and tiles[0].get("key"):
        _, opened, _ = get(base, tiles[0]["key"])
        check("a Categories tile opens its filtered list",
              bool(opened.get("Metadata")),
              "%d row(s)" % len(opened.get("Metadata") or []))
    status, folders, _ = get(base, "/library/sections/%s/folder" % movie_section)
    check("By Folder lists folders with the librarySection triple",
          status == 200 and folders.get("Metadata")
          and folders.get("librarySectionUUID"),
          "%d folder(s)" % len(folders.get("Metadata") or []))

    print("\n=== the library's Recommended tab ===")
    for section_id, prefix in ((movie_section, "movie"), (show_section, "tv")):
        if not section_id:
            continue
        status, hubs, _ = get(base, "/hubs/sections/%s" % section_id)
        shelves = hubs.get("Hub") or []
        identifiers = [h.get("hubIdentifier") for h in shelves]
        check("section %s offers the five measured shelves" % section_id,
              status == 200 and len(shelves) == 5
              and identifiers[0] == "%s.inprogress.%s" % (prefix, section_id),
              ", ".join(str(i) for i in identifiers))
        check("every shelf is a shelf with a context and a key",
              all(h.get("style") == "shelf" and h.get("context") and h.get("key")
                  for h in shelves),
              ",".join(str(h.get("context")) for h in shelves))
        promoted = [h.get("hubIdentifier") for h in shelves if h.get("promoted")]
        check("Recently Added is the promoted shelf",
              promoted == ["%s.recentlyadded.%s" % (prefix, section_id)],
              ",".join(promoted))
        broken = []
        for shelf in shelves:
            # A shelf with no key is itself the failure: its "see all" link has
            # nowhere to go.
            if not shelf.get("key"):
                broken.append("%s -> no key" % shelf.get("hubIdentifier"))
                continue
            shelf_status, _, _ = get(base, shelf["key"])
            if shelf_status != 200:
                broken.append("%s -> %s" % (shelf["key"], shelf_status))
        check("every shelf's own list answers 200", not broken, "; ".join(broken))

    print("\n=== the filter bar's descriptors ===")
    _, movie_meta, _ = get(base, "/library/sections/%s/all?includeMeta=1" % movie_section)
    types = (movie_meta.get("Meta") or {}).get("Type") or []
    movie_type = next((x for x in types if x.get("type") == "movie"), {})
    check("a movie section's Meta carries filters and fields",
          len(movie_type.get("Filter") or []) > 10
          and len(movie_type.get("Field") or []) > 10
          and len(movie_type.get("Sort") or []) > 5,
          "%d filter(s), %d field(s), %d sort(s)"
          % (len(movie_type.get("Filter") or []), len(movie_type.get("Field") or []),
             len(movie_type.get("Sort") or [])))
    if show_section:
        _, show_meta, _ = get(base,
                              "/library/sections/%s/all?type=2&includeMeta=1" % show_section)
        show_types = [x.get("type") for x in (show_meta.get("Meta") or {}).get("Type") or []]
        # Retargeting the movie template onto a show library gave the client a
        # show grid whose filter bar could only talk about movies.
        check("a show section's Meta carries its own four types",
              show_types == ["show", "season", "episode", "folder"],
              ",".join(str(x) for x in show_types))
        keys = [x.get("key") for x in (show_meta.get("Meta") or {}).get("Type") or []]
        check("the show Meta's keys are retargeted onto this section",
              all(("/library/sections/%s/" % show_section) in str(k) for k in keys),
              ",".join(str(k) for k in keys))

    print("\n=== sorting ===")
    _, ascending, _ = get(base, "/library/sections/%s/all?sort=titleSort:asc" % movie_section)
    _, descending, _ = get(base, "/library/sections/%s/all?sort=titleSort:desc" % movie_section)
    up = [r.get("ratingKey") for r in ascending.get("Metadata") or []]
    down = [r.get("ratingKey") for r in descending.get("Metadata") or []]
    check("a sort direction is honoured", len(up) > 1 and up == list(reversed(down)),
          "%s vs %s" % (up, down))
    _, first, _ = get(base, "/library/sections/%s/all?sort=random" % movie_section)
    _, second, _ = get(base, "/library/sections/%s/all?sort=random" % movie_section)
    # A random sort has to be stable per request, or paging through it repeats
    # and skips rows.
    check("the random sort is stable across requests",
          [r.get("ratingKey") for r in first.get("Metadata") or []]
          == [r.get("ratingKey") for r in second.get("Metadata") or []])
    # A `sort=` spec is a COMMA-SEPARATED list, and plexsim used to split the
    # whole thing on the first colon: the secondary key was dropped and
    # `titleSort:asc,year:desc` even came out descending, because the spec as a
    # whole ended in `:desc`. These two differ only in the SECOND key.
    _, secondary_up, _ = get(base, "/library/sections/%s/all?sort=year:desc,titleSort:asc"
                                   % movie_section)
    _, secondary_down, _ = get(base, "/library/sections/%s/all?sort=year:desc,titleSort:desc"
                                     % movie_section)
    up_keys = [r.get("ratingKey") for r in secondary_up.get("Metadata") or []]
    down_keys = [r.get("ratingKey") for r in secondary_down.get("Metadata") or []]
    if len(up_keys) > 1:
        check("a secondary sort key is honoured", up_keys == list(reversed(down_keys)),
              "%s vs %s" % (up_keys, down_keys))
    else:
        skip("a secondary sort key is honoured", "need two rows")
    # Measured: `random:<seed>` differs per seed and is stable for one seed --
    # which is what lets a client page through a shuffled library without
    # repeating or skipping rows.
    _, seed_a, _ = get(base, "/library/sections/%s/all?sort=random:12345" % movie_section)
    _, seed_a2, _ = get(base, "/library/sections/%s/all?sort=random:12345" % movie_section)
    _, seed_b, _ = get(base, "/library/sections/%s/all?sort=random:99" % movie_section)
    order_a = [r.get("ratingKey") for r in seed_a.get("Metadata") or []]
    order_b = [r.get("ratingKey") for r in seed_b.get("Metadata") or []]
    check("a seeded random sort is stable for one seed",
          order_a == [r.get("ratingKey") for r in seed_a2.get("Metadata") or []],
          str(order_a))
    if len(order_a) > 1:
        check("two seeds give two orders", order_a != order_b,
              "%s vs %s" % (order_a, order_b))
    else:
        skip("two seeds give two orders", "need two rows")
    status, _, _ = container(base, "GET", "/library/sections/%s/all?sort=bogus" % movie_section)
    # Measured on the reference: an unknown sort key is a 500 there, not a
    # silently unsorted 200. A caller cannot tell the difference otherwise.
    check("an unknown sort key is refused, not ignored", status == 500,
          "HTTP %s" % status)
    for spec in ("addedAt:desc", "mediaHeight:desc", "originallyAvailableAt:desc",
                 "lastViewedAt:desc", "audienceRating:desc", "duration:desc"):
        status, sorted_rows, _ = get(base, "/library/sections/%s/all?sort=%s"
                                           % (movie_section, spec))
        check("sort %s is answered" % spec,
              status == 200 and len(sorted_rows.get("Metadata") or []) == len(movies),
              "%d row(s)" % len(sorted_rows.get("Metadata") or []))

    print("\n=== listing container rules ===")
    _, paged, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=0"
                            "&X-Plex-Container-Size=1" % movie_section)
    _, unpaged, _ = get(base, "/library/sections/%s/all" % movie_section)
    check("a paged listing carries offset and totalSize",
          paged.get("offset") == 0 and paged.get("totalSize") == len(movies),
          "offset=%s totalSize=%s" % (paged.get("offset"), paged.get("totalSize")))
    check("an unpaged listing carries nocache and neither",
          unpaged.get("nocache") is True
          and "offset" not in unpaged and "totalSize" not in unpaged,
          ",".join(sorted(k for k in unpaged if not isinstance(unpaged[k], list))))
    for leaf in ("newest", "recentlyAdded", "onDeck", "collections"):
        _, listing, _ = get(base, "/library/sections/%s/%s" % (movie_section, leaf))
        check("%s carries neither nocache nor totalSize" % leaf,
              "nocache" not in listing and "totalSize" not in listing,
              ",".join(sorted(k for k in listing if not isinstance(listing[k], list))))
    if show:
        _, leaves, _ = get(base, "/library/metadata/%s/allLeaves" % show)
        check("allLeaves says mixedParents and omits thumb and summary",
              leaves.get("mixedParents") is True
              and "thumb" not in leaves and "summary" not in leaves,
              ",".join(sorted(k for k in leaves if not isinstance(leaves[k], list))))
    status, _, _ = get(base, "/library/sections/prefs?type=movie")
    check("a non-numeric section id is a 400, not a section", status == 400,
          "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 17. Add Library / Edit Library / Delete Library
    # ------------------------------------------------------------------
    print("\n=== Manage > Libraries: add, rename, delete ===")
    status, created, _ = container(
        base, "POST", "/library/sections?name=%s&type=movie&agent=tv.plex.agents.movie"
                      "&scanner=Plex%%20Movie&language=en-US&location=%s"
                      % (quote("plexsim probe library"), quote("/plexsim")))
    directory = (created.get("Directory") or [{}])[0]
    check("Add Library answers 201 with the created library",
          status == 201 and directory.get("key"), "HTTP %s key=%s"
          % (status, directory.get("key")))
    new_section = directory.get("key")
    if new_section:
        status, body, _ = request(base, "PUT", "/library/sections/%s?name=%s"
                                               % (new_section, quote("plexsim renamed")))
        _, sections, _ = get(base, "/library/sections")
        titles = [d.get("title") for d in sections.get("Directory") or []]
        check("Edit Library renames it",
              is_empty_200(status, body) and "plexsim renamed" in titles,
              ",".join(titles))
        status, _, _ = request(base, "PUT", "/library/sections/%s" % new_section)
        check("a rename with no name is a 400, as a real PMS answers", status == 400,
              "HTTP %s" % status)
        status, body, _ = request(base, "DELETE", "/library/sections/%s" % new_section)
        _, sections, _ = get(base, "/library/sections")
        check("Delete Library removes it",
              is_empty_200(status, body)
              and all(d.get("key") != new_section
                      for d in sections.get("Directory") or []),
              "%d section(s) left" % len(sections.get("Directory") or []))

    # ------------------------------------------------------------------
    # 18. Merge / Split Apart, then Delete -- destructive, so last
    # ------------------------------------------------------------------
    print("\n=== context menu: Merge / Split Apart / Delete ===")
    if not spare:
        skip("merge absorbs the other row's Media", "need a second row")
    else:
        status, body, _ = request(base, "PUT", "/library/metadata/%s/merge?ids=%s"
                                               % (movie, spare))
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        row = (detail.get("Metadata") or [{}])[0]
        status_spare, _, _ = get(base, "/library/metadata/%s" % spare)
        check("merge absorbs the other row's Media",
              is_empty_200(status, body) and len(row.get("Media") or []) >= 2
              and status_spare == 404,
              "%d Media, merged row now HTTP %s"
              % (len(row.get("Media") or []), status_spare))
        status, body, _ = request(base, "PUT", "/library/metadata/%s/split" % movie)
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        row = (detail.get("Metadata") or [{}])[0]
        _, listing, _ = get(base, "/library/sections/%s/all" % movie_section)
        check("split returns the extra Media to a row of its own",
              is_empty_200(status, body) and len(row.get("Media") or []) == 1
              and len(listing.get("Metadata") or []) == len(movies),
              "%d row(s) in the section" % len(listing.get("Metadata") or []))

    status, _, _ = request(base, "DELETE", "/library/metadata/%s?proxy=1" % movie)
    check("Delete Version with no optimized version is a 400", status == 400,
          "HTTP %s" % status)

    _, listing, _ = get(base, "/library/sections/%s/all" % movie_section)
    victim = (listing.get("Metadata") or [{}])[-1].get("ratingKey")
    status, body, _ = request(base, "DELETE", "/library/metadata/%s" % victim)
    status_after, _, _ = get(base, "/library/metadata/%s" % victim)
    _, listing, _ = get(base, "/library/sections/%s/all" % movie_section)
    check("Delete removes the row from the library",
          is_empty_200(status, body) and status_after == 404
          and all(str(r.get("ratingKey")) != str(victim)
                  for r in listing.get("Metadata") or []),
          "HTTP %s then %s" % (status, status_after))

    if show:
        _, children, _ = get(base, "/library/metadata/%s/children" % show)
        season = (children.get("Metadata") or [{}])[0].get("ratingKey")
        if season:
            _, leaves, _ = get(base, "/library/metadata/%s/children" % season)
            episode = (leaves.get("Metadata") or [{}])[0].get("ratingKey")
            request(base, "DELETE", "/library/metadata/%s" % season)
            status_season, _, _ = get(base, "/library/metadata/%s" % season)
            status_episode, _, _ = get(base, "/library/metadata/%s" % episode) \
                if episode else (404, {}, {})
            _, detail, _ = get(base, "/library/metadata/%s" % show)
            row = (detail.get("Metadata") or [{}])[0]
            check("deleting a season takes its episodes and fixes the counts",
                  status_season == 404 and status_episode == 404
                  and row.get("childCount") == len(
                      get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []),
                  "childCount=%s leafCount=%s"
                  % (row.get("childCount"), row.get("leafCount")))

    # ------------------------------------------------------------------
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n%s  %d/%d checks passed, %d skipped"
          % (GREEN + "PASS" + OFF if passed == total else RED + "FAIL" + OFF,
             passed, total, len(skipped)))
    for label, ok in results:
        if not ok:
            print("   failed: %s" % label)
    for label, why in skipped:
        print("   skipped: %s -- %s" % (label, why))
    print("\nThis suite wrote to the server. Restart it to restore the catalog.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
