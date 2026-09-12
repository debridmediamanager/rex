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

    print("\n=== what a show, a season and an episode each carry ===")
    detail_params = ("includeExtras=1&includeOnDeck=1&includePreferences=1"
                     "&includeChapters=1&includeReviews=1")
    if show:
        _, show_row, _ = get(base, "/library/metadata/%s?%s" % (show, detail_params))
        row = (show_row.get("Metadata") or [{}])[0]
        show_prefs = [s.get("id") for s in (row.get("Preferences") or {}).get("Setting") or []]
        # Measured: eleven settings on a show and three on a season, and they are
        # not the same three. plexsim used to invent a single `episodeSort` row.
        check("a show's own settings tab gets the measured list",
              "episodeSort" in show_prefs and "flattenSeasons" in show_prefs
              and len(show_prefs) > 5, "%d setting(s)" % len(show_prefs))
        _, children, _ = get(base, "/library/metadata/%s/children" % show)
        season_key = ((children.get("Metadata") or [{}])[0]).get("ratingKey")
        if season_key:
            _, season_row, _ = get(base, "/library/metadata/%s?%s"
                                         % (season_key, detail_params))
            srow = (season_row.get("Metadata") or [{}])[0]
            season_prefs = [s.get("id")
                            for s in (srow.get("Preferences") or {}).get("Setting") or []]
            # A season page had no episode to offer and an empty settings tab,
            # because plexsim answered these for shows only.
            check("a season carries OnDeck, Extras and its own settings",
                  bool(srow.get("OnDeck")) and srow.get("Extras") is not None
                  and season_prefs == ["audioLanguage", "subtitleLanguage",
                                       "subtitleMode"],
                  "OnDeck=%s prefs=%s" % (bool(srow.get("OnDeck")), season_prefs))
            _, leaves, _ = get(base, "/library/metadata/%s/children" % season_key)
            episode_key = ((leaves.get("Metadata") or [{}])[0]).get("ratingKey")
            if episode_key:
                _, ep, _ = get(base, "/library/metadata/%s?%s"
                                     % (episode_key, detail_params))
                erow = (ep.get("Metadata") or [{}])[0]
                check("an episode carries Extras but no settings of its own",
                      erow.get("Extras") is not None and not erow.get("Preferences"))
                # Measured on the reference AND on production: a real episode row
                # carries no Genre, studio, tagline or Country. plexsim's
                # inherited all of them from the show.
                strays = [k for k in ("Genre", "studio", "tagline", "Country")
                          if erow.get(k)]
                check("an episode does not carry its show's tags", not strays,
                      ",".join(strays) or "none")
        status, body, _ = request(base, "PUT",
                                  "/library/metadata/%s/prefs?episodeSort=1" % show)
        _, show_row, _ = get(base, "/library/metadata/%s?%s" % (show, detail_params))
        row = (show_row.get("Metadata") or [{}])[0]
        written = next((s for s in (row.get("Preferences") or {}).get("Setting") or []
                        if s.get("id") == "episodeSort"), {})
        check("a written item setting comes back on the row and in its settings",
              is_empty_200(status, body) and str(written.get("value")) == "1"
              and str(row.get("episodeSort")) == "1",
              "value=%s row=%s" % (written.get("value"), row.get("episodeSort")))
        # flattenSeasons does NOT change what /children returns: measured, a real
        # PMS still answers with the seasons and sets `skipChildren` on the SHOW,
        # and the client skips the level on the strength of that one field.
        # plexsim served the preference and then ignored it.
        request(base, "PUT", "/library/sections/%s/prefs?flattenSeasons=1" % show_section)
        _, flat, _ = get(base, "/library/metadata/%s" % show)
        _, kids, _ = get(base, "/library/metadata/%s/children" % show)
        check("flattenSeasons sets skipChildren on the show",
              (flat.get("Metadata") or [{}])[0].get("skipChildren") is True,
              str((flat.get("Metadata") or [{}])[0].get("skipChildren")))
        check("flattenSeasons leaves /children answering seasons",
              kids.get("viewGroup") == "season",
              str(kids.get("viewGroup")))
        request(base, "PUT", "/library/sections/%s/prefs?flattenSeasons=0" % show_section)
        _, unflat, _ = get(base, "/library/metadata/%s" % show)
        check("turning flattenSeasons off clears it again",
              "skipChildren" not in (unflat.get("Metadata") or [{}])[0])
    else:
        skip("a season carries OnDeck, Extras and its own settings", "no show section")

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
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=stopped&time=60000"
                         "&duration=600000&identifier=com.plexapp.plugins.library"
                         % (movie, quote("/library/metadata/%s" % movie)))
    status, timeline, _ = get(base, "/:/timeline?ratingKey=%s&state=stopped&time=60000" % movie)
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
    # An artwork version is a unix SECOND, so a refresh inside the same second as
    # the row's last version produces the same key and the check fails for a
    # reason that is not a bug. Wait for the clock to tick first.
    edge = time.time()
    while int(time.time()) == int(edge):
        time.sleep(0.05)
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

    print("\n=== smart playlists and smart collections ===")
    # A smart list is a stored QUERY, re-run on every read. plexsim stored the
    # rows the query matched at creation time, so the list was a snapshot: it
    # looked right the moment it was made and then went quietly stale.
    smart_query = "type=1&unwatched=1&sort=titleSort"
    smart_uri = quote("server://%s/com.plexapp.plugins.library"
                      "/library/sections/%s/all?%s" % (machine, movie_section, smart_query))
    _, expected, _ = get(base, "/library/sections/%s/all?%s" % (movie_section, smart_query))
    wanted = [r.get("title") for r in expected.get("Metadata") or []]
    status, created, _ = container(base, "POST",
                                  "/playlists?type=video&title=%s&smart=1&uri=%s"
                                  % (quote("plexsim smart list"), smart_uri))
    smart_playlist = (created.get("Metadata") or [{}])[0]
    check("a smart playlist reports leafCount from its query and a smart icon",
          status == 200 and smart_playlist.get("smart") is True
          and smart_playlist.get("icon") == "playlist://image.smart"
          and smart_playlist.get("leafCount") == len(wanted),
          "leafCount=%s icon=%s" % (smart_playlist.get("leafCount"),
                                    smart_playlist.get("icon")))
    smart_key = smart_playlist.get("ratingKey")
    if smart_key:
        # Measured: the LIST omits `content` and the single read carries it, and
        # a playlist REWRITES the server:// URI into a library:// one.
        _, listed, _ = get(base, "/playlists")
        row = next((r for r in listed.get("Metadata") or []
                    if str(r.get("ratingKey")) == str(smart_key)), {})
        _, single, _ = get(base, "/playlists/%s" % smart_key)
        stored = (single.get("Metadata") or [{}])[0].get("content") or ""
        check("a playlist's content is rewritten, and only the single read has it",
              "content" not in row
              and stored.startswith("library://x/directory/")
              and "genre" not in stored and "unwatched" in urllib.parse.unquote(stored),
              stored[:70])
        _, items, _ = get(base, "/playlists/%s/items" % smart_key)
        check("a smart playlist's items are its query, not a stored list",
              [r.get("title") for r in items.get("Metadata") or []] == wanted,
              ", ".join(str(t) for t in wanted))
        # The decisive one: change a row the query tests and the membership moves.
        moving = (expected.get("Metadata") or [{}])[0].get("ratingKey")
        if moving:
            request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library"
                                 % moving)
            _, after, _ = get(base, "/playlists/%s/items" % smart_key)
            request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library"
                                 % moving)
            _, back, _ = get(base, "/playlists/%s/items" % smart_key)
            check("a smart playlist re-evaluates live",
                  len(after.get("Metadata") or []) == len(wanted) - 1
                  and len(back.get("Metadata") or []) == len(wanted),
                  "%d -> %d -> %d" % (len(wanted), len(after.get("Metadata") or []),
                                      len(back.get("Metadata") or [])))
        else:
            skip("a smart playlist re-evaluates live", "the query matches nothing")
        request(base, "DELETE", "/playlists/%s" % smart_key)
    status, created, _ = container(
        base, "POST", "/library/collections?type=1&smart=1&sectionId=%s&title=%s&uri=%s"
                      % (movie_section, quote("plexsim smart collection"), smart_uri))
    smart_collection = (created.get("Metadata") or [{}])[0]
    # Measured: a collection's `smart` is the STRING "1", it keeps the server://
    # URI verbatim, and its `content` is in the LIST as well as the single read.
    check("a smart collection says smart=\"1\" and keeps the uri verbatim",
          status == 200 and smart_collection.get("smart") == "1"
          and (smart_collection.get("content") or "").startswith("server://")
          and smart_collection.get("childCount") == len(wanted),
          "smart=%r childCount=%s" % (smart_collection.get("smart"),
                                      smart_collection.get("childCount")))
    smart_collection_key = smart_collection.get("ratingKey")
    if smart_collection_key:
        _, listed, _ = get(base, "/library/sections/%s/collections" % movie_section)
        row = next((r for r in listed.get("Metadata") or []
                    if str(r.get("ratingKey")) == str(smart_collection_key)), {})
        _, children, _ = get(base, "/library/collections/%s/children"
                                   % smart_collection_key)
        # And a smart collection does NOT tag its members -- measured on the
        # reference, where a member's Collection stayed absent.
        _, member, _ = get(base, "/library/metadata/%s"
                                 % (expected.get("Metadata") or [{}])[0].get("ratingKey"))
        tags = (member.get("Metadata") or [{}])[0].get("Collection") or []
        check("a smart collection lists content and leaves its members untagged",
              (row.get("content") or "").startswith("server://")
              and [r.get("title") for r in children.get("Metadata") or []] == wanted
              and not any(t.get("tag") == "plexsim smart collection" for t in tags),
              "%d child(ren), %d tag(s) on the member" % (
                  len(children.get("Metadata") or []), len(tags)))
        request(base, "DELETE", "/library/collections/%s" % smart_collection_key)
    # A PLAIN collection omits `smart` altogether rather than sending a false.
    plain_uri = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                      % (machine, movie))
    status, created, _ = container(
        base, "POST", "/library/collections?type=1&sectionId=%s&title=%s&uri=%s"
                      % (movie_section, quote("plexsim plain collection"), plain_uri))
    plain = (created.get("Metadata") or [{}])[0]
    check("a plain collection omits smart, where a plain playlist sends false",
          "smart" not in plain and "content" not in plain,
          ",".join(sorted(k for k in plain if k in ("smart", "content"))) or "neither")
    if plain.get("ratingKey"):
        request(base, "DELETE", "/library/collections/%s" % plain["ratingKey"])

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
            ("GET", "/video/:/transcode/universal/subtitles?session=x", 400,
             "a subtitle request with no transcode session"),
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

    print("\n=== playback sessions: Now Playing and the Dashboard ===")
    # Measured: a session is created by /:/timeline, not by anyone reading bytes
    # -- a bare timeline with no play queue, no decision and no part read puts a
    # full row in /status/sessions, and `state=stopped` removes it. plexsim
    # answered `{size:0}` for its whole life, so Now Playing was always empty and
    # the Terminate button had nothing to act on.
    session_item = spare or movie
    identity = ("X-Plex-Client-Identifier=suite-probe&X-Plex-Session-Identifier=suite-sess"
                "&X-Plex-Product=Plex+Web&X-Plex-Device=OSX&X-Plex-Device-Name=Harness"
                "&X-Plex-Platform=Chrome&X-Plex-Version=4.147.1")

    def beat(state, offset, item=None):
        return container(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=%s&time=%d"
                                     "&duration=600000&%s"
                                     % (item or session_item,
                                        quote("/library/metadata/%s" % (item or session_item)),
                                        state, offset, identity))

    # Anything an earlier check left playing is stopped first: a session is
    # server state, and the point here is the transition, not the starting point.
    _, existing, _ = get(base, "/status/sessions")
    for open_row in existing.get("Metadata") or []:
        container(base, "GET", "/:/timeline?ratingKey=%s&state=stopped&time=1"
                               "&X-Plex-Session-Identifier=%s"
                               % (open_row.get("ratingKey"),
                                  quote((open_row.get("Session") or {}).get("id") or "")))
    status, idle, _ = get(base, "/status/sessions")
    check("an idle server lists no session",
          status == 200 and "Metadata" not in idle, ",".join(sorted(idle)))
    beat("playing", 3000)
    _, sessions, _ = get(base, "/status/sessions")
    row = (sessions.get("Metadata") or [{}])[0]
    check("a timeline alone creates a session",
          sessions.get("size") == 1 and row.get("sessionKey"),
          "sessionKey=%s" % row.get("sessionKey"))
    check("the session row carries viewOffset, User, Player and Session",
          row.get("viewOffset") == 3000 and row.get("User") and row.get("Player")
          and row.get("Session"),
          "viewOffset=%s" % row.get("viewOffset"))
    player = row.get("Player") or {}
    # Player is an echo of the client's own X-Plex values plus the peer address.
    check("Player echoes the client and names the peer",
          player.get("product") == "Plex Web" and player.get("title") == "Harness"
          and player.get("machineIdentifier") == "suite-probe"
          and player.get("address") and player.get("state") == "playing",
          "%s / %s / %s" % (player.get("product"), player.get("address"),
                            player.get("state")))
    check("Session.id is the client's session identifier",
          (row.get("Session") or {}).get("id") == "suite-sess",
          str((row.get("Session") or {}).get("id")))
    # The client renders "Direct Play" precisely when Session is present and
    # TranscodeSession is not, which is the truth for a server with no encoder.
    check("a direct-play session has no TranscodeSession",
          "TranscodeSession" not in row and any(
              part.get("decision") == "directplay"
              for media in row.get("Media") or [] for part in media.get("Part") or []),
          "decision=%s" % [p.get("decision") for m in row.get("Media") or []
                           for p in m.get("Part") or []])
    # The timeline RESPONSE is not an echo of the state sent: measured as
    # `ignore` until a minute in, then `progress`, then `complete` at the end.
    _, early, _ = beat("playing", 5000)
    _, late, _ = beat("playing", 90000)
    check("the timeline answers playbackState=ignore under a minute",
          early.get("playbackState") == "ignore" and early.get("viewOffset") == -1
          and early.get("skipCount") == 0,
          "%s viewOffset=%s" % (early.get("playbackState"), early.get("viewOffset")))
    check("and playbackState=progress past a minute",
          late.get("playbackState") == "progress" and late.get("viewCount") == 1,
          str(late.get("playbackState")))
    check("a live session's timeline carries Bandwidths",
          bool(late.get("Bandwidths")), str(bool(late.get("Bandwidths"))))
    # Terminate: keyed on Session.id, and the kill lands on the NEXT timeline.
    status, _, _ = container(base, "GET", "/status/sessions/terminate")
    check("terminate without a sessionId is a 400", status == 400, "HTTP %s" % status)
    status, _, _ = container(base, "GET", "/status/sessions/terminate?sessionId=nope&reason=x")
    check("terminate on an unknown session is a 404", status == 404, "HTTP %s" % status)
    status, body, _ = request(base, "GET", "/status/sessions/terminate"
                                          "?sessionId=suite-sess&reason=harness")
    check("terminate on a live session answers an empty 200",
          is_empty_200(status, body), "HTTP %s" % status)
    _, killed, _ = beat("playing", 95000)
    check("the kill is delivered through the next timeline",
          killed.get("terminationCode") == 2006 and "harness" in
          str(killed.get("terminationText") or ""),
          "code=%s" % killed.get("terminationCode"))
    if part_id:
        status, _, _ = request(base, "GET", "/library/parts/%s/1/file.mkv"
                                           "?X-Plex-Session-Identifier=suite-sess" % part_id,
                               want_json=False)
        check("a terminated session's next part read is a 503", status == 503,
              "HTTP %s" % status)
    beat("stopped", 600000)
    _, gone, _ = get(base, "/status/sessions")
    check("a stopped timeline removes the session",
          "Metadata" not in gone, ",".join(sorted(gone)))
    # A fifth notification type, and the one Now Playing depends on.
    playing = []

    def play_once():
        beat("playing", 4000)

    _, frames = notifications(base, play_once, seconds=6)
    playing = [r for f in frames
               for r in ((f.get("NotificationContainer") or {})
                         .get("PlaySessionStateNotification") or [])]
    check("the socket pushes a playing notification", bool(playing),
          "%d frame(s)" % len(playing))
    if playing:
        entry = playing[0]
        check("the playing frame carries the measured field set",
              all(k in entry for k in ("sessionKey", "clientIdentifier", "ratingKey",
                                       "key", "viewOffset", "state")),
              ",".join(sorted(entry)))
    beat("stopped", 600000)

    print("\n=== the Dashboard's own numbers ===")
    # Measured: a real PMS counts EVERY response's bytes, not just media, into
    # per-second rows and rolls them into hour/day/week/month/year buckets.
    # plexsim serves every byte it reports, so these are real.
    _, live, _ = get(base, "/statistics/bandwidth?timespan=6")
    rows = live.get("StatisticsBandwidth") or []
    check("bandwidth statistics carry real per-second rows",
          bool(rows) and all(r.get("bytes") for r in rows)
          and live.get("Device") and live.get("Account"),
          "%d row(s)" % len(rows))
    _, hourly, _ = get(base, "/statistics/bandwidth?timespan=4")
    hour_rows = hourly.get("StatisticsBandwidth") or []
    check("they roll up into a coarser bucket",
          bool(hour_rows) and all(r.get("timespan") == 4 for r in hour_rows)
          and len(hour_rows) <= len(rows),
          "%d row(s) at timespan=4" % len(hour_rows))
    _, media_stats, _ = get(base, "/statistics/media")
    check("media statistics count a completed play",
          bool(media_stats.get("StatisticsMedia")),
          "%d row(s)" % len(media_stats.get("StatisticsMedia") or []))
    # Re-measured against BOTH reference servers: /statistics/resources answers
    # 200 with a StatisticsResources series, and only timespan=6 has samples --
    # every coarser bucket is an empty 200, and so is a request with no timespan.
    # plexsim used to 404 it on the theory that the client hides an unsupported
    # graph; the measurement says a real PMS answers.
    status, resources, _ = get(base, "/statistics/resources?timespan=6")
    sample = (resources.get("StatisticsResources") or [{}])[0]
    check("resource statistics answer a series at the finest bucket",
          status == 200 and sample.get("timespan") == 6 and sample.get("at")
          and set(sample) == {"timespan", "at", "hostCpuUtilization",
                              "processCpuUtilization", "hostMemoryUtilization",
                              "processMemoryUtilization"},
          "%d sample(s): %s" % (len(resources.get("StatisticsResources") or []),
                                sorted(sample)))
    for spec in ("", "?timespan=5", "?timespan=4"):
        status, coarse, _ = get(base, "/statistics/resources%s" % spec)
        check("resource statistics are empty for %s" % (spec or "no timespan"),
              status == 200 and coarse.get("size") == 0
              and not coarse.get("StatisticsResources"),
              "HTTP %s size=%s" % (status, coarse.get("size")))
    for path_, want in (("/transcode/sessions/nope", 404),
                        ("/video/:/transcode/universal/subtitles?session=nope", 400)):
        status, _, _ = container(base, "GET", path_)
        check("an unknown transcode session: %s" % path_.split("/")[-1][:24],
              status == want, "HTTP %s" % status)

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
        entry = resolution["Directory"][0]
        # Measured: a media axis entry carries `key` and `title` and NO fastKey --
        # the client builds `?resolution=<key>` itself -- and the title is the
        # LABEL (`1080p`), not the value.
        _, rows, _ = get(base, "/library/sections/%s/all?resolution=%s"
                               % (movie_section, quote(entry["key"])))
        check("a resolution entry filters the listing, with no fastKey to follow",
              bool(rows.get("Metadata")) and "fastKey" not in entry
              and entry.get("title") != entry.get("key"),
              "%s -> %d row(s)" % (entry.get("title"), len(rows.get("Metadata") or [])))

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
    # Measured: the element is `Scanner` with nothing but a `name`, and the
    # container's identifier is com.plexapp.system rather than the library's.
    # plexsim answered `Directory` rows with a key and a title, which the Edit
    # Library dialog's scanner dropdown does not read at all.
    status, scanners, _ = get(base, "/system/scanners/%s" % movie_section)
    names = [s.get("name") for s in scanners.get("Scanner") or []]
    check("Edit Library dialog can list scanners",
          status == 200 and names == ["Plex Movie", "Plex Movie Scanner",
                                      "Plex Video Files Scanner", "Plex Video Files"]
          and not scanners.get("Directory")
          and scanners.get("identifier") == "com.plexapp.system",
          ", ".join(str(n) for n in names))
    if show_section:
        _, show_scanners, _ = get(base, "/system/scanners/%s" % show_section)
        check("a show library lists its own two scanners",
              [s.get("name") for s in show_scanners.get("Scanner") or []]
              == ["Plex TV Series", "Plex Series Scanner"],
              ", ".join(str(s.get("name")) for s in show_scanners.get("Scanner") or []))
    else:
        skip("a show library lists its own two scanners", "no show library")
    # A real PMS does NOT put its account token in /myplex/account, and plexsim
    # did -- a page that reads this endpoint is a page that could log the token.
    _, body, _ = request(base, "GET", "/myplex/account")
    account = json.loads(body).get("MyPlex", {}) if body[:1] == b"{" else {}
    check("/myplex/account carries no authToken",
          "authToken" not in account and account.get("signInState"),
          ",".join(sorted(account)))
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
                        ("/statistics/media", "Dashboard media")):
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

    print("\n=== the filter query language ===")
    # The operator is part of the KEY, not the value: the client holds `>>=` and
    # sends `year%3E%3E=2000`, so the server reads a parameter NAMED `year>>`.
    # plexsim iterated its parameters looking for names it knew, saw none of
    # these, and applied no constraint -- every filter returned the whole library
    # while looking like it worked.
    def filtered(spec):
        _, body, _ = container(base, "GET", "/library/sections/%s/all%s" % (movie_section, spec))
        return len(body.get("Metadata") or [])

    total = len(movies)
    _, sample, _ = get(base, "/library/sections/%s/all" % movie_section)
    titles = [r.get("title") or "" for r in sample.get("Metadata") or []]
    first = titles[0] if titles else ""
    word = first.split(" ")[0] if first else "zzz"
    cases = [
        ("?title=%s" % quote(word), total, "a bare = on a string CONTAINS"),
        ("?title=zzzznope", 0, "a contains miss"),
        ("?title%%3D=%s" % quote(word), 0, "== is exact, so a word does not match"),
        ("?title%%3D=%s" % quote(first), 1, "== matches the whole title"),
        ("?title%%3C=%s" % quote(word), total, "<= begins with"),
        ("?bogusfield=1", total, "an unknown field is ignored, not an error"),
    ]
    years = sorted({r.get("year") for r in sample.get("Metadata") or [] if r.get("year")})
    if years:
        low, high = years[0] - 1, years[-1] + 1
        cases += [
            ("?year%%3E%%3E=%d" % low, total, ">>= is strictly greater"),
            ("?year%%3C%%3C=%d" % low, 0, "<<= is strictly less"),
            ("?year%%3E=%d" % years[0], total, ">= is inclusive"),
            ("?year%%3C%%3C=%d" % high, total, "<<= the other way"),
            ("?push=1&year=%d&or=1&year=%d&pop=1" % (low, years[0]), total,
             "push/pop with or is a union"),
            ("?year=%d&and=1&year=%d" % (low, years[0]), 0, "and of two years is empty"),
        ]
    tags = [t.get("tag") for r in (sample.get("Metadata") or []) for t in r.get("Genre") or []]
    if tags:
        tag = tags[0]
        cases += [
            ("?genre=%s" % quote(tag), None, "a tag matches by name"),
            ("?genre!=%s" % quote(tag), None, "a negated tag excludes"),
            ("?genre=%s,zzznope" % quote(tag), None, "a comma is an OR"),
            ("?genre!=%s,zzznope" % quote(tag), None, "a negated comma is NEITHER"),
            ("?genre=%s&genre=zzznope" % quote(tag), 0, "a repeated key is an AND"),
        ]
    for spec, want, why in cases:
        got = filtered(spec)
        if want is None:
            check("filter: %s" % why, True, "%s -> %d row(s)" % (spec[:46], got))
        else:
            check("filter: %s" % why, got == want,
                  "%s -> %d row(s), wanted %d" % (spec[:46], got, want))
    check("filter: a relative date in the past matches",
          filtered("?addedAt%3E%3E=-10y") == total, "-10y")
    check("filter: a relative date in the future matches nothing",
          filtered("?addedAt%3E%3E=%2B30d") == 0, "+30d")
    # Measured refusals: a real PMS is explicit rather than silently unfiltered.
    for spec, want, why in (
            ("?year%3E2011", 400, "a comparison with no separator is a 400"),
            ("?year%3E%3E=notanumber", 500, "an unparseable number is a 500"),
            ("?year=", 500, "an empty value is a 500"),
            ("?addedAt%3E%3E=-1mo", 500, "an unknown date suffix is a 500")):
        status, _, _ = container(base, "GET",
                                 "/library/sections/%s/all%s" % (movie_section, spec))
        check("filter: %s" % why, status == want, "HTTP %s" % status)

    print("\n=== the filter query language: the field's TYPE decides ===")
    # The operator table alone is not the whole language: what a bare `=` MEANS
    # depends on the field's advertised type, and plexsim treated everything that
    # was not a number or a date as a substring match.
    _, sample, _ = get(base, "/library/sections/%s/all" % movie_section)
    rows = sample.get("Metadata") or []
    total = len(rows)
    rating = next((r.get("contentRating") for r in rows if r.get("contentRating")), "")
    if rating and len(rating) > 1:
        check("filter: a tag field's = is EXACT, not contains",
              filtered("?contentRating=%s" % quote(rating)) > 0
              and filtered("?contentRating=%s" % quote(rating[:1])) == 0,
              "%s matches, %s does not" % (rating, rating[:1]))
    else:
        skip("filter: a tag field's = is EXACT, not contains",
             "no multi-character contentRating in the catalog")
    # Measured: an EMPTY tag value matches the rows that have NO value -- on the
    # reference `?contentRating=` returned exactly the two rows without one, and
    # `?genre=` returned none because every row has a genre.
    # The counts come off the rows rather than being assumed: earlier checks in
    # this suite edit tags, so a hard-coded expectation here reads as a filter bug.
    unlabelled = len([r for r in rows if not (r.get("Label") or [])])
    ungenred = len([r for r in rows if not (r.get("Genre") or [])])
    check("filter: an empty tag value matches the rows with no tag",
          filtered("?label=") == unlabelled and filtered("?genre=") == ungenred,
          "label= -> %d of %d, genre= -> %d of %d"
          % (filtered("?label="), unlabelled, filtered("?genre="), ungenred))
    # Measured: `?unwatched=2` returns the same rows as `?unwatched=0`, so a
    # boolean value is true only when it is exactly `1`.
    def watch_set(spec):
        _, body, _ = get(base, "/library/sections/%s/all?%s" % (movie_section, spec))
        return sorted(str(r.get("ratingKey")) for r in body.get("Metadata") or [])
    false_rows, other_rows = watch_set("unwatched=0"), watch_set("unwatched=2")
    true_rows = watch_set("unwatched=1")
    check("filter: a boolean is true only for the value 1",
          false_rows == other_rows
          and len(true_rows) + len(false_rows) == total
          and not set(true_rows) & set(false_rows),
          "=1 -> %d, =0 -> %d, =2 -> %d"
          % (len(true_rows), len(false_rows), len(other_rows)))
    # Measured: `resolution` is validated against a fixed set, so a value outside
    # it is a 500 rather than an empty list -- and a valid one that matches
    # nothing is a plain empty 200.
    have = {str(m.get("videoResolution")) for r in rows for m in r.get("Media") or []}
    mine = next(iter(have - {"None"}), "")
    if mine:
        others = sorted({"4k", "1080", "720", "576", "480", "sd"} - {mine})
        check("filter: a known resolution matches and another is empty",
              filtered("?resolution=%s" % mine) == total
              and filtered("?resolution=%s" % others[0]) == 0,
              "%s -> %d, %s -> 0" % (mine, filtered("?resolution=%s" % mine), others[0]))
    else:
        skip("filter: a known resolution matches and another is empty",
             "no videoResolution on the catalog's Media")
    for spec, want, why in (("?resolution=2160", 500, "a resolution outside the set is a 500"),
                            ("?resolution=", 500, "an empty resolution is a 500"),
                            ("?location=%2Fplexsim%2F1", 500,
                             "a folder location is an ID, so a path is a 500")):
        status, _, _ = container(base, "GET",
                                 "/library/sections/%s/all%s" % (movie_section, spec))
        check("filter: %s" % why, status == want, "HTTP %s" % status)
    check("filter: a folder location matches by its section id",
          filtered("?location=%s" % movie_section) == total
          and filtered("?location=99") == 0,
          "location=%s -> %d" % (movie_section, filtered("?location=%s" % movie_section)))
    # Measured: an operator the field's type does not take is an unknown KEY, so
    # the listing ignores it -- `?genre>>=6` returns the whole library.
    check("filter: an operator the type does not take is ignored",
          filtered("?genre%3E%3E=1") == total and filtered("?title%3C%3C=x") == total,
          "genre>> -> %d, title<< -> %d"
          % (filtered("?genre%3E%3E=1"), filtered("?title%3C%3C=x")))

    # A SHOW library advertises its fields PREFIXED (`show.genre`), and the client
    # sends the advertised key back verbatim. plexsim looked the name up after
    # stripping the prefix, in a map keyed by the prefixed names, so nothing
    # matched and every filter on a show grid returned the whole library.
    if show_section:
        _, shows, _ = get(base, "/library/sections/%s/all?type=2" % show_section)
        show_rows = shows.get("Metadata") or []
        title = (show_rows[0].get("title") or "") if show_rows else ""

        def show_filtered(spec):
            _, body, _ = container(base, "GET", "/library/sections/%s/all?%s"
                                                % (show_section, spec))
            return len(body.get("Metadata") or [])

        if title:
            check("filter: a show library's PREFIXED field filters",
                  show_filtered("type=2&show.title=%s" % quote(title)) == 1
                  and show_filtered("type=2&show.title=zzznope") == 0
                  and show_filtered("type=2&title=%s" % quote(title)) == 1,
                  "%d show(s) in the section" % len(show_rows))
        else:
            skip("filter: a show library's PREFIXED field filters", "no show rows")
    else:
        skip("filter: a show library's PREFIXED field filters", "no show library")

    print("\n=== the query language read back: /query/parse ===")
    # A tag id from the catalog's own numbering, so `valueTitle` has something to
    # resolve: a real PMS titles `?genre=6` "Animation" and leaves a name or an
    # unknown id untitled.
    genre_id = genre_name = None
    for row in rows:
        for tag in row.get("Genre") or []:
            if tag.get("id") and tag.get("tag"):
                genre_id, genre_name = tag["id"], tag["tag"]
                break
        if genre_id:
            break
    # The Edit Filters dialog reopens a saved filter by handing the query string
    # back here and reading the Rule tree out of the answer. An unrouted path is
    # a 404 the dialog reads as "no rules", so the dialog silently draws empty.
    def parsed(spec, section=None):
        status, body, _ = request(base, "GET", "/library/sections/%s/query/parse%s"
                                              % (section or movie_section, spec))
        if body[:1] != b"{":
            return status, {}
        try:
            return status, json.loads(body).get("Query", {})
        except ValueError:
            return status, {}

    status, doc = parsed("?type=1&genre=%s&sort=titleSort" % quote(str(genre_id or 1)))
    rule = doc.get("Rule") or {}
    check("query/parse answers a Query, not a MediaContainer",
          status == 200 and doc.get("type") == 1
          and rule.get("field") == "genre" and rule.get("operator") == "="
          and rule.get("fieldTitle") == "Genre" and rule.get("value") == str(genre_id or 1)
          and doc.get("Order") == [{"field": "titleSort", "ascending": True}],
          json.dumps(doc)[:90])
    if genre_id:
        check("query/parse titles a tag value from the tag table",
              rule.get("valueTitle") == genre_name,
              "%s -> %s" % (genre_id, rule.get("valueTitle")))
    else:
        skip("query/parse titles a tag value from the tag table", "no tag ids in the catalog")
    # Measured: the default type is the SECTION's own, and it is an integer.
    _, doc = parsed("")
    _, show_doc = parsed("", show_section) if show_section else (200, {"type": 2})
    check("query/parse defaults the type to the section's own",
          doc.get("type") == 1 and show_doc.get("type") == 2,
          "movie %s, show %s" % (doc.get("type"), show_doc.get("type")))
    _, doc = parsed("?type=1&push=1&genre=1&or=1&genre=2&pop=1&and=1&year%3E%3E=2000")
    outer = doc.get("Rule") or {}
    inner = (outer.get("Rule") or [{}])[0]
    check("query/parse nests a push/pop group as a composite",
          outer.get("type") == "composite" and outer.get("operator") == "and"
          and inner.get("type") == "composite" and inner.get("operator") == "or"
          and len(inner.get("Rule") or []) == 2,
          json.dumps(outer)[:90])
    _, doc = parsed("?type=1&push=1&genre=1&pop=1")
    check("query/parse collapses a one-rule group to the rule",
          (doc.get("Rule") or {}).get("field") == "genre",
          json.dumps(doc.get("Rule"))[:70])
    _, doc = parsed("?type=1&group=year&limit=7&sort=year:desc&genre=1&bogus=1")
    check("query/parse carries Order, Limit, Group and Error",
          doc.get("Order") == [{"field": "year", "ascending": False}]
          and doc.get("Limit") == [{"limit": 7}]
          and doc.get("Group") == [{"field": "year"}]
          and doc.get("Error") == [{"type": "field", "value": "bogus"}],
          json.dumps(doc)[:110])
    # The seed is DROPPED from the Order block, and `nocollate` is a modifier
    # that does not change the direction.
    _, seeded = parsed("?type=1&sort=random:12345")
    _, collated = parsed("?type=1&sort=titleSort:desc:nocollate")
    check("query/parse drops a random seed and reads desc past a modifier",
          seeded.get("Order") == [{"field": "random", "ascending": True}]
          and collated.get("Order") == [{"field": "titleSort", "ascending": False}],
          "%s / %s" % (seeded.get("Order"), collated.get("Order")))
    # An unknown FIELD is reported beside whatever did parse, where the same
    # field on a listing is ignored; an operator the type does not take is
    # reported as an unknown field too, key and all.
    _, doc = parsed("?type=1&genre=1&bogus%3E%3E=3&checkFiles=1&includeGuids=1")
    check("query/parse reports unknown fields and keeps the good rule",
          (doc.get("Rule") or {}).get("field") == "genre"
          and doc.get("Error") == [{"type": "field", "value": "bogus>>"},
                                   {"type": "field", "value": "checkFiles"}],
          json.dumps(doc.get("Error")))
    _, doc = parsed("?type=1&genre%3E%3E=1")
    check("query/parse reports a tag field's comparison operator as a bad field",
          doc.get("Error") == [{"type": "field", "value": "genre>>"}]
          and "Rule" not in doc, json.dumps(doc))
    _, doc = parsed("?type=1&push=1&genre=1")
    check("query/parse reports an unclosed push as a mismatch",
          doc.get("Error") == [{"type": "mismatch", "value": "eof",
                                "expected": "pop"}], json.dumps(doc.get("Error")))
    # The field titles are scoped: an explicit prefix wins, otherwise the type
    # implies one, and a type with no such field answers an EMPTY title.
    _, doc = parsed("?type=1&show.title=x")
    scoped = doc.get("Rule") or {}
    check("query/parse echoes a field's scope and titles it from there",
          scoped.get("type") == "show" and scoped.get("field") == "title"
          and scoped.get("fieldTitle") == "Show Title", json.dumps(scoped))
    _, doc = parsed("?type=3&title=x")
    check("query/parse answers an empty title for a type with no such field",
          (doc.get("Rule") or {}).get("fieldTitle") == "",
          json.dumps(doc.get("Rule")))
    if show_section:
        _, doc = parsed("?type=4&unwatched=1", show_section)
        check("query/parse titles an unprefixed field from the requested type",
              (doc.get("Rule") or {}).get("fieldTitle") == "Episode Unwatched",
              json.dumps(doc.get("Rule")))
    for spec, want, why in (("?type=abc", 400, "a non-numeric type is a 400"),
                            ("?type=1&year%3E2010", 400, "a malformed key is a 400"),
                            ("?type=1&limit=abc", 500, "an unparseable limit is a 500"),
                            ("?type=1&sort=bogusKey", 500, "an unknown sort key is a 500")):
        status, _ = parsed(spec)
        check("query/parse: %s" % why, status == want, "HTTP %s" % status)
    # Measured: an unknown type NUMBER is echoed untouched, and only `parse`
    # exists under `/query` -- the parent and a POST are both 404.
    _, doc = parsed("?type=99")
    check("query/parse echoes an unknown type number", doc.get("type") == 99,
          json.dumps(doc))
    status, _, _ = request(base, "GET", "/library/sections/%s/query?type=1" % movie_section)
    status_post, _, _ = request(base, "POST",
                                "/library/sections/%s/query/parse?type=1" % movie_section)
    check("query/parse: /query alone and a POST are both 404",
          status == 404 and status_post == 404, "%s / %s" % (status, status_post))
    # XML is plexsim's default, and this response's root is not a MediaContainer.
    status, raw, _ = request(base, "GET", "/library/sections/%s/query/parse"
                                         "?type=1&genre=1&sort=titleSort" % movie_section,
                             want_json=False)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = None
    check("query/parse renders <Query> in XML with ascending as 1/0",
          root is not None and root.tag == "Query"
          and root.findtext(".") is not None
          and root.find("Order") is not None
          and root.find("Order").get("ascending") == "1"
          and root.find("Rule") is not None,
          (raw[:80].decode("utf-8", "replace")) if raw else "no body")

    print("\n=== the include/exclude options the client actually sends ===")
    # Each of these is a parameter the official client sends and plexsim ignored.
    # An ignored option is the quiet kind of wrong: the response is a valid detail
    # document that simply lacks the block asked for, so the page draws with a
    # section missing rather than with an error.
    def detail_of(spec, key=None):
        status, body, _ = request(base, "GET", "/library/metadata/%s?%s"
                                              % (key or movie, spec))
        if body[:1] != b"{":
            return status, {}, {}
        parsed = json.loads(body).get("MediaContainer", {})
        return status, parsed, (parsed.get("Metadata") or [{}])[0]
    _, plain_container, plain_row = detail_of("x=1")
    check("excludeFields drops the named field",
          "summary" in plain_row
          and "summary" not in detail_of("excludeFields=summary")[2],
          "summary present plainly, absent excluded")
    _, _, no_media = detail_of("excludeElements=Media")
    check("excludeElements=Media substitutes two counts",
          "Media" not in no_media
          and no_media.get("mediaCount") == len(plain_row.get("Media") or [])
          and no_media.get("mediaCountOptimized") == 0,
          "mediaCount=%s mediaCountOptimized=%s" % (no_media.get("mediaCount"),
                                                    no_media.get("mediaCountOptimized")))
    check("includeRelated adds a Related block",
          "Related" not in plain_row and "Related" in detail_of("includeRelated=1")[2],
          "absent plainly, present when asked")
    # Measured: either async flag makes the CONTAINER drop identifier and the
    # media-tag pair, which is how a client loses the ability to resolve a tag.
    for flag in ("asyncCheckFiles", "asyncRefreshLocalMediaAgent"):
        _, async_container, _ = detail_of("%s=1" % flag)
        check("%s strips identifier and the media-tag pair" % flag,
              all(k in plain_container for k in ("identifier", "mediaTagPrefix",
                                                "mediaTagVersion"))
              and not any(k in async_container for k in ("identifier", "mediaTagPrefix",
                                                        "mediaTagVersion")),
              ",".join(sorted(k for k in async_container
                              if not isinstance(async_container[k], list))))
    # Per-item settings are per TYPE: a movie has three, a show eleven, a season
    # three, and an episode NONE -- where an empty block draws an empty tab.
    _, _, movie_prefs = detail_of("includePreferences=1")
    names = [s.get("id") for s in (movie_prefs.get("Preferences") or {}).get("Setting") or []]
    check("a movie's own settings are the three measured ones",
          names == ["languageOverride", "useOriginalTitle",
                    "enableCreditsMarkerGeneration"], ",".join(str(n) for n in names))
    if show:
        _, _, show_row = detail_of("includeChildren=1", show)
        children = show_row.get("Children") or {}
        _, seasons, _ = get(base, "/library/metadata/%s/children" % show)
        check("includeChildren inlines the seasons with their own size",
              children.get("size") == len(seasons.get("Metadata") or [])
              and len(children.get("Metadata") or []) == children.get("size"),
              "Children size=%s" % children.get("size"))
    else:
        skip("includeChildren inlines the seasons with their own size", "no show row")

    print("\n=== group=, which collapses rather than filters ===")
    # Measured field by field: `group=` takes the scalar columns of the metadata
    # row and answers 500 for everything else -- a tag, the media, or per-user
    # view state. A 200 with the list ungrouped is the trap.
    _, ungrouped, _ = get(base, "/library/sections/%s/all" % movie_section)
    total = len(ungrouped.get("Metadata") or [])
    studios = {r.get("studio") for r in ungrouped.get("Metadata") or []}
    _, by_studio, _ = get(base, "/library/sections/%s/all?group=studio" % movie_section)
    check("group collapses to one row per distinct value",
          len(by_studio.get("Metadata") or []) == len(studios) <= total,
          "%d studio(s) out of %d row(s) -> %d"
          % (len(studios), total, len(by_studio.get("Metadata") or [])))
    for field in ("genre", "viewCount", "resolution", "duration", "bogus", ""):
        status, _, _ = container(base, "GET", "/library/sections/%s/all?group=%s"
                                             % (movie_section, field))
        check("group=%s is a 500, not an ungrouped 200" % (field or "<empty>"),
              status == 500, "HTTP %s" % status)

    print("\n=== the decision container's codes ===")
    # Measured: on a DIRECT PLAY a real PMS sends only the `mde*` pair. The
    # general/directPlay/transcode triples appear only when direct play is
    # refused. plexsim sent every 1000 at once, which tells a client a transcode
    # decision was made when none was.
    def decided(spec):
        status, body, _ = request(
            base, "GET", "/video/:/transcode/universal/decision?path=%s"
                         "&mediaIndex=0&partIndex=0&protocol=dash&session=uitest&%s"
                         % (quote("/library/metadata/%s" % movie), spec))
        if body[:1] != b"{":
            return status, {}
        return status, json.loads(body).get("MediaContainer", {})
    _, ok_decision = decided("directPlay=1&directStream=1&hasMDE=1")
    check("a direct play answers the mde pair and nothing else",
          ok_decision.get("mdeDecisionCode") == 1000
          and ok_decision.get("mdeDecisionText") == "Direct play OK."
          and not any(k.startswith(("general", "directPlay", "transcode"))
                      for k in ok_decision),
          ",".join(sorted(k for k in ok_decision if k.endswith("Code"))) or "no codes")
    check("the decision container names the session and says allowSync as a string",
          ok_decision.get("resourceSession") == "plexsim-ui-test"
          and ok_decision.get("allowSync") == "1"
          and isinstance(ok_decision.get("librarySectionID"), str)
          and isinstance(ok_decision.get("mediaTagVersion"), str),
          "resourceSession=%s allowSync=%r librarySectionID=%r"
          % (ok_decision.get("resourceSession"), ok_decision.get("allowSync"),
             ok_decision.get("librarySectionID")))
    _, refused = decided("directPlay=0&directStream=1&hasMDE=1")
    check("a refused direct play answers 3000 with 1001 either side",
          refused.get("directPlayDecisionCode") == 3000
          and refused.get("generalDecisionCode") == 1001
          and refused.get("transcodeDecisionCode") == 1001
          and "mdeDecisionCode" not in refused
          and refused.get("generalDecisionText")
              == "Direct play not available; Conversion OK.",
          "%s / %s / %s" % (refused.get("directPlayDecisionCode"),
                            refused.get("generalDecisionCode"),
                            refused.get("transcodeDecisionCode")))

    print("\n=== container paging: both parameters, either transport ===")
    # Measured: a real PMS pages only when BOTH parameters are present, and it
    # accepts them as HTTP HEADERS as well as query parameters -- the React paths
    # send headers, which plexsim never read.
    _, both, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=0"
                           "&X-Plex-Container-Size=1" % movie_section)
    _, size_only, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Size=1"
                                % movie_section)
    _, start_only, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=1"
                                 % movie_section)
    _, negative, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=0"
                               "&X-Plex-Container-Size=-1" % movie_section)
    check("both parameters page", both.get("size") == 1 and both.get("totalSize") == total,
          "size=%s total=%s" % (both.get("size"), both.get("totalSize")))
    check("a size alone is ignored entirely",
          size_only.get("size") == total and "totalSize" not in size_only,
          "size=%s total=%s" % (size_only.get("size"), size_only.get("totalSize")))
    check("a start alone is ignored entirely",
          start_only.get("size") == total and "offset" not in start_only,
          "size=%s offset=%s" % (start_only.get("size"), start_only.get("offset")))
    check("a negative size drops paging rather than slicing",
          negative.get("size") == total and "totalSize" not in negative,
          "size=%s" % negative.get("size"))
    _, past, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=500"
                           "&X-Plex-Container-Size=10" % movie_section)
    check("paging past the end is an empty page, not an error",
          past.get("size") == 0 and past.get("totalSize") == total,
          "size=%s total=%s" % (past.get("size"), past.get("totalSize")))
    header_request = urllib.request.Request(
        base.rstrip("/") + "/library/sections/%s/all" % movie_section,
        headers={"Accept": "application/json", "User-Agent": UA,
                 "X-Plex-Container-Start": "0", "X-Plex-Container-Size": "1"})
    with urllib.request.urlopen(header_request, timeout=20) as response:
        by_header = json.load(response)["MediaContainer"]
    check("paging arrives as HTTP headers too",
          by_header.get("size") == 1 and by_header.get("totalSize") == total,
          "size=%s total=%s" % (by_header.get("size"), by_header.get("totalSize")))

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
            # The reference's own `studio` fastKey carries a RAW space
            # (`all?studio=Blender Foundation`), which cannot go on an HTTP
            # request line at all -- the client encodes it before use. So the
            # fastKey is re-encoded here rather than followed literally.
            path, _, raw = entry["fastKey"].partition("?")
            follow = path + ("?" + urllib.parse.urlencode(
                urllib.parse.parse_qsl(raw, keep_blank_values=True)) if raw else "")
            _, filtered, _ = get(base, follow)
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
    # Measured: only a `genre` entry carries a `type`, and a person entry carries
    # the tag's own `thumb`. plexsim stamped `type` on every tag axis.
    _, genres, _ = get(base, "/library/sections/%s/genre" % movie_section)
    _, actors, _ = get(base, "/library/sections/%s/actor" % movie_section)
    genre_entries = genres.get("Directory") or []
    actor_entries = actors.get("Directory") or []
    check("only a genre entry carries a type",
          all(e.get("type") == "genre" for e in genre_entries)
          and all("type" not in e for e in actor_entries),
          "%d genre(s), %d actor(s)" % (len(genre_entries), len(actor_entries)))
    if actor_entries:
        with_thumb = [e for e in actor_entries if e.get("thumb")]
        check("a person entry carries the tag's thumb",
              bool(with_thumb) or not any(
                  t.get("thumb") for m in present for t in m.get("Role") or []),
              "%d of %d" % (len(with_thumb), len(actor_entries)))
    else:
        skip("a person entry carries the tag's thumb", "no actor tags in the catalog")
    # An axis is scoped by `?type=`: a movie library asked for shows lists nothing.
    _, wrong_type, _ = get(base, "/library/sections/%s/genre?type=2" % movie_section)
    check("an axis is scoped by the requested type",
          wrong_type.get("size") == 0 and not wrong_type.get("Directory"),
          "size=%s" % wrong_type.get("size"))

    print("\n=== the media axes, and the two that are not secondary at all ===")
    # These read off the Media and its Streams. Measured: their entries carry
    # `key` and `title` and no `fastKey`, and the title is a LABEL -- `1080`
    # prints as `1080p`, a language code as the language's own name.
    for axis in ("resolution", "videoCodec", "audioCodec", "subtitleCodec",
                 "audioLayout", "audioLanguage", "subtitleLanguage"):
        status, listing, _ = get(base, "/library/sections/%s/%s" % (movie_section, axis))
        entries = listing.get("Directory") or []
        check("the %s axis answers with key/title entries" % axis,
              status == 200 and listing.get("viewGroup") == "secondary"
              and listing.get("title2", "").startswith("By ")
              and all(set(e) == {"key", "title"} for e in entries),
              "%s: %d entry(ies) %s" % (listing.get("title2"), len(entries),
                                        entries[:2]))
    # By Folder Location is the one axis whose container is NOT the secondary one.
    status, location, _ = get(base, "/library/sections/%s/location" % movie_section)
    entries = location.get("Directory") or []
    check("By Folder Location carries only size and title2",
          status == 200
          and sorted(k for k in location if not isinstance(location[k], list))
              == ["size", "title2"]
          and location.get("title2") == "By Folder Location",
          ",".join(sorted(location)))
    if entries:
        _, by_location, _ = get(base, entries[0]["fastKey"])
        check("a folder location entry filters the listing",
              bool(by_location.get("Metadata")),
              "%s -> %d row(s)" % (entries[0].get("title"),
                                   len(by_location.get("Metadata") or [])))
    else:
        skip("a folder location entry filters the listing", "the section has no Location")
    # `/unwatched` is a LISTING titled "Unplayed", not an axis.
    _, unplayed, _ = get(base, "/library/sections/%s/unwatched" % movie_section)
    check("the unwatched listing is titled Unplayed",
          unplayed.get("title2") == "Unplayed" and unplayed.get("viewGroup") != "secondary",
          "%s / %s" % (unplayed.get("title2"), unplayed.get("viewGroup")))
    # The seven axes the REFERENCE itself 404s, advertised in /filters all the
    # same. plexsim advertises them too, so a 200 here would be the invention.
    for axis in ("hdr", "dovi", "hdr10plus", "atmos", "inProgress", "unmatched"):
        status, _, _ = get(base, "/library/sections/%s/%s" % (movie_section, axis))
        check("the %s axis 404s, as the reference's does" % axis, status == 404,
              "HTTP %s" % status)

    print("\n=== /filters is per library TYPE ===")
    # A movie library advertises 28 filters and a show library a DIFFERENT 15 --
    # the same lesson as the two Meta sidecars. plexsim served one trimmed list
    # for both, so a show grid's filter bar could only talk about movies.
    _, movie_filters, _ = get(base, "/library/sections/%s/filters" % movie_section)
    movie_names = [e.get("filter") for e in movie_filters.get("Directory") or []]
    check("a movie library advertises the reference's 28 filters",
          len(movie_names) == 28 and movie_names[0] == "genre"
          and "resolution" in movie_names and "location" in movie_names
          and "network" not in movie_names,
          "%d: %s" % (len(movie_names), ",".join(movie_names[:6])))
    if show_section:
        _, show_filters, _ = get(base, "/library/sections/%s/filters" % show_section)
        show_entries = show_filters.get("Directory") or []
        show_names = [e.get("filter") for e in show_entries]
        check("a show library advertises a different 15",
              len(show_names) == 15 and "network" in show_names
              and "unwatchedLeaves" in show_names and "resolution" not in show_names,
              "%d: %s" % (len(show_names), ",".join(show_names[:6])))
        # The `?type=2` suffix is measured per entry and is not uniform: three
        # entries do not carry it.
        bare = sorted(e.get("filter") for e in show_entries
                      if "?" not in (e.get("key") or ""))
        check("a show library's filter keys carry ?type=2, with three exceptions",
              bare == ["editionTitle", "unmatched"] or bare == ["country",
                                                                "editionTitle",
                                                                "unmatched"],
              ",".join(bare))
        # Every advertised key either answers or 404s the way the reference's
        # does; an advertised key that answers a 200 the reference does not is
        # the invention this suite exists to catch.
        broken = []
        for entry in show_entries:
            status, _, _ = get(base, entry["key"])
            if status not in (200, 404):
                broken.append("%s=%s" % (entry.get("filter"), status))
        check("every advertised show filter key answers 200 or 404",
              not broken, ",".join(broken) or "all clean")
    else:
        skip("a show library advertises a different 15", "no show library")

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

    print("\n=== the Home screen's own shelves ===")
    # /hubs/promoted is called ONCE PER PINNED LIBRARY, and plexsim ignored
    # contentDirectoryID and answered every library's shelf every time -- so Home
    # drew each library's rows under every library's heading. Its hubs were also
    # missing the five fields the client lays the page out from.
    _, promoted, _ = get(base, "/hubs/promoted?contentDirectoryID=%s"
                               "&pinnedContentDirectoryID=%s" % (movie_section, movie_section))
    hubs = promoted.get("Hub") or []
    identifiers = [h.get("hubIdentifier") for h in hubs]
    check("promoted answers Continue Watching, On Deck and ONE library's shelf",
          identifiers == ["home.continue", "home.ondeck",
                          "movie.recentlyadded.%s" % movie_section],
          ", ".join(str(i) for i in identifiers))
    check("every promoted shelf carries key, context, style and promoted",
          all(h.get("key") and h.get("context") and h.get("style")
              and h.get("promoted") is True for h in hubs),
          ", ".join("%s/%s" % (h.get("context"), h.get("style")) for h in hubs))
    # Measured: the context drops a trailing section id -- it names the KIND of
    # shelf, which is why it is shared across libraries.
    recent = next((h for h in hubs if "recentlyadded" in (h.get("hubIdentifier") or "")), {})
    check("a shelf's context drops the section id and hubKey names its rows",
          recent.get("context") == "hub.movie.recentlyadded"
          and (recent.get("hubKey") or "").startswith("/library/metadata/")
          and "," in (recent.get("hubKey") or ",")
          and recent.get("key") == "/library/sections/%s/all?sort=addedAt:desc" % movie_section,
          "%s | %s" % (recent.get("context"), recent.get("hubKey")))
    if show_section:
        _, tv_promoted, _ = get(base, "/hubs/promoted?contentDirectoryID=%s" % show_section)
        tv = next((h for h in tv_promoted.get("Hub") or []
                   if "recentlyadded" in (h.get("hubIdentifier") or "")), {})
        # Measured: a SHOW library's shelf is typed `mixed` and keyed to
        # /hubs/home/recentlyAdded, where a movie library's is typed `movie`.
        check("a show library's Recently Added shelf is mixed and keyed to hubs/home",
              tv.get("hubIdentifier") == "tv.recentlyadded.%s" % show_section
              and tv.get("type") == "mixed"
              and tv.get("key") == "/hubs/home/recentlyAdded?type=2&sectionID=%s" % show_section,
              "%s %s %s" % (tv.get("hubIdentifier"), tv.get("type"), tv.get("key")))
    else:
        skip("a show library's Recently Added shelf is mixed and keyed to hubs/home",
             "no show library")
    _, bare, _ = get(base, "/hubs/promoted")
    check("the bare call adds Recent Playlists and every library",
          "home.playlists" in [h.get("hubIdentifier") for h in bare.get("Hub") or []]
          and len(bare.get("Hub") or []) > len(hubs),
          ", ".join(str(h.get("hubIdentifier")) for h in bare.get("Hub") or []))
    _, excluded, _ = get(base, "/hubs/promoted?contentDirectoryID=%s"
                               "&excludeContinueWatching=1" % movie_section)
    check("excludeContinueWatching drops that shelf",
          "home.continue" not in [h.get("hubIdentifier")
                                  for h in excluded.get("Hub") or []],
          ", ".join(str(h.get("hubIdentifier")) for h in excluded.get("Hub") or []))
    # The Continue Watching hub is identified `continueWatching`, styled hero, and
    # its key is its OWN items endpoint -- which plexsim did not route at all.
    _, watching, _ = get(base, "/hubs/continueWatching?contentDirectoryID=%s" % movie_section)
    hub = (watching.get("Hub") or [{}])[0]
    status, items, _ = get(base, hub.get("key") or "/nope")
    check("the Continue Watching hub is hero-styled and its key resolves",
          hub.get("hubIdentifier") == "continueWatching"
          and hub.get("style") == "hero" and hub.get("type") == "mixed"
          and hub.get("key") == "/hubs/continueWatching/items" and status == 200,
          "%s %s -> HTTP %s" % (hub.get("hubIdentifier"), hub.get("key"), status))
    # Measured: /hubs/home/recentlyAdded keys on `type`, not on `sectionID`.
    _, by_type, _ = get(base, "/hubs/home/recentlyAdded?type=1")
    _, by_section, _ = get(base, "/hubs/home/recentlyAdded?sectionID=%s" % movie_section)
    check("hubs/home/recentlyAdded keys on type, not sectionID",
          (by_type.get("size") or 0) > 0 and by_section.get("size") == 0,
          "type=1 -> %s, sectionID -> %s" % (by_type.get("size"), by_section.get("size")))
    for leaf in ("continueWatching", "onDeck"):
        status, body, _ = get(base, "/hubs/home/%s" % leaf)
        check("hubs/home/%s answers a plain row list" % leaf,
              status == 200 and "Hub" not in body
              and sorted(k for k in body if not isinstance(body[k], list))
                  == ["allowSync", "identifier", "size"],
              ",".join(sorted(k for k in body if not isinstance(body[k], list))))
    status, _, _ = get(base, "/hubs/home/nosuchhub")
    check("an unknown home hub is a 404, not an empty shelf", status == 404,
          "HTTP %s" % status)

    print("\n=== the preplay page's Related shelf ===")
    # The client rewrites /library/metadata/{id} to /hubs/metadata/{id} and appends
    # the leaf. plexsim routed neither, and a 404 is exactly what the preplay page
    # reads as "no related items" -- silently.
    # Pick a row that still HAS genres: the Unmatch check above deliberately strips
    # them off the movie every other check uses, and a row with no genre has no
    # related rows to offer -- which is itself what the reference answers for one
    # of its own five movies.
    _, current, _ = get(base, "/library/sections/%s/all" % movie_section)
    genred = next((r.get("ratingKey") for r in current.get("Metadata") or []
                   if r.get("Genre")), None)
    subject = genred or movie
    status, related, _ = get(base, "/hubs/metadata/%s/related?includeExternalMedia=1"
                                   "&includeExternalMetadata=1&asyncAugmentMetadata=1"
                                   % subject)
    hub = (related.get("Hub") or [{}])[0]
    if genred:
        check("hubs/metadata/{id}/related answers a movie.similar shelf",
              status == 200 and hub.get("hubIdentifier") == "movie.similar"
              and hub.get("title") == "Related Movies"
              and hub.get("key") == "/library/metadata/%s/similar" % subject
              and hub.get("context") == "hub.movie.similar",
              json.dumps({k: v for k, v in hub.items() if not isinstance(v, list)})[:110])
    else:
        check("hubs/metadata/{id}/related answers an empty shelf list",
              status == 200 and not related.get("Hub"),
              "no row in the section carries a genre")
    # Measured: related carries the librarySection triple and allowSync, postplay
    # carries NEITHER.
    status, postplay, _ = get(base, "/hubs/metadata/%s/postplay" % subject)
    check("related carries the librarySection triple and postplay does not",
          related.get("librarySectionID") is not None
          and related.get("allowSync") is True
          and status == 200 and "librarySectionID" not in postplay
          and postplay.get("allowSync") is False,
          "related=%s postplay=%s" % (sorted(k for k in related
                                             if not isinstance(related[k], list)),
                                      sorted(k for k in postplay
                                             if not isinstance(postplay[k], list))))
    _, similar, _ = get(base, "/library/metadata/%s/similar" % subject)
    check("the shelf's key resolves to the same rows",
          (similar.get("size") or 0) == (hub.get("size") or 0),
          "%s vs %s" % (similar.get("size"), hub.get("size")))
    status, _, _ = get(base, "/hubs/metadata/%s/nosuchleaf" % subject)
    check("an unknown item hub leaf is a 404", status == 404, "HTTP %s" % status)

    print("\n=== search, which is seventeen hubs every time ===")
    # plexsim answered only the hubs that had rows, so the results page was laid
    # out from a list that changed per query; it matched by SUBSTRING, where a real
    # PMS matches a word PREFIX; and it sent no `score`, which the client sorts by.
    _, sample, _ = get(base, "/library/sections/%s/all" % movie_section)
    first = ((sample.get("Metadata") or [{}])[0].get("title") or "").split(" ")[0]
    _, found, _ = get(base, "/hubs/search?query=%s&limit=30&includeCollections=1"
                            % quote(first.lower()))
    hubs = found.get("Hub") or []
    # Indexed defensively: a server that answers fewer hubs must FAIL this check,
    # not crash the run before the rest of the suite gets to speak.
    second = hubs[1] if len(hubs) > 1 else {}
    check("search answers all seventeen typed hubs, empty ones included",
          len(hubs) == 17 and [h.get("hubIdentifier") for h in hubs][:5]
          == ["movie", "show", "artist", "album", "episode"]
          and second.get("title") == "Shows",
          "%d hub(s), second is %r" % (len(hubs), second.get("title")))
    check("every search hub has an empty context, a shelf style and no hubKey",
          all(h.get("context") == "" and h.get("style") == "shelf"
              and "hubKey" not in h for h in hubs),
          "contexts=%s" % {h.get("context") for h in hubs})
    movie_hub = next((h for h in hubs if h.get("hubIdentifier") == "movie"), {})
    rows = movie_hub.get("Metadata") or []
    check("a search row carries a score as a five-decimal STRING",
          rows and all(isinstance(r.get("score"), str)
                       and len(r["score"].split(".")[-1]) == 5 for r in rows),
          ", ".join(str(r.get("score")) for r in rows[:3]))
    # Measured word-prefix rule: one character matches nothing, and a non-prefix
    # fragment of a word matches nothing either.
    for spec, want, why in ((first[:1].lower(), 0, "a single character matches nothing"),
                            (first[:2].lower(), 1, "two characters match the prefix"),
                            (first[1:].lower(), 0, "a mid-word fragment matches nothing")):
        _, probe, _ = get(base, "/hubs/search?query=%s" % quote(spec))
        hit = sum(h.get("size") or 0 for h in probe.get("Hub") or [])
        check("search: %s" % why, (hit > 0) == bool(want), "%r -> %d hit(s)" % (spec, hit))
    status, _, _ = container(base, "GET", "/hubs/search")
    check("search with no query is a 400, not an empty page", status == 400,
          "HTTP %s" % status)
    # /library/search is advertised as `universalsearch` and answered nothing.
    _, universal, _ = get(base, "/library/search?query=%s&limit=30" % quote(first.lower()))
    # NOT named `results`: that is the module-level list check() appends to, and
    # shadowing it here made the whole run report three checks.
    hits = universal.get("SearchResult") or []
    check("universalsearch answers SearchResult rows with a FLOAT score",
          hits and all(isinstance(r.get("score"), float) and r.get("Metadata")
                       for r in hits),
          "%d result(s), score %r" % (len(hits),
                                      hits[0].get("score") if hits else None))
    status, _, _ = container(base, "GET", "/library/search")
    check("universalsearch with no query is a 400", status == 400, "HTTP %s" % status)
    # The legacy /search also lists the providers, and its size counts them.
    _, legacy, _ = get(base, "/search?query=%s" % quote(first.lower()))
    providers = legacy.get("Provider") or []
    check("legacy search lists four providers and counts them in size",
          len(providers) == 4
          and legacy.get("size") == len(legacy.get("Metadata") or []) + 4
          and all(r.get("personal") is True and r.get("sourceTitle")
                  for r in legacy.get("Metadata") or []),
          "size=%s rows=%d providers=%d" % (legacy.get("size"),
                                            len(legacy.get("Metadata") or []),
                                            len(providers)))
    # Measured: no `query` parameter is a 400 and an EMPTY one is a 404 -- two
    # different refusals on the same endpoint.
    status_missing, _, _ = container(base, "GET", "/search")
    status_empty, _, _ = container(base, "GET", "/search?query=")
    check("legacy search: a missing query is 400 and an empty one 404",
          status_missing == 400 and status_empty == 404,
          "%s / %s" % (status_missing, status_empty))
    _, voice, _ = get(base, "/hubs/search/voice?query=%s" % quote(first.lower()))
    vhub = (voice.get("Hub") or [{}])[0]
    check("voice search hubs are titled by type and carry a distance",
          vhub.get("hubIdentifier") == "results.search.1"
          and vhub.get("context") == "hub.results.search"
          and vhub.get("distance") is not None and "key" not in vhub
          and (vhub.get("hubKey") or "").startswith("/library/metadata/"),
          json.dumps({k: v for k, v in vhub.items() if not isinstance(v, list)})[:110])
    # The in-library search box: only the form with NO type is a 400.
    status, in_library, _ = get(base, "/library/sections/%s/search?type=1&query=%s"
                                      % (movie_section, quote(first.lower())))
    check("a section search with a type is a listing titled Search for '...'",
          status == 200 and in_library.get("title2") == "Search for '%s'" % first.lower()
          and "content" not in in_library
          and in_library.get("viewGroup") == "movie",
          "%r, %d row(s)" % (in_library.get("title2"),
                             len(in_library.get("Metadata") or [])))

    print("\n=== the library's Recommended tab ===")
    # Measured: `?count=N` caps every shelf at N rows AND sets `more` on the ones
    # that were cut, which is how the client knows to draw the "see all" chevron.
    # plexsim hard-coded `more: false` and ignored count, so a truncated shelf
    # looked complete. The client asks for count+1 for exactly this reason.
    for section_id in (movie_section, show_section):
        if not section_id:
            continue
        _, full, _ = get(base, "/hubs/sections/%s" % section_id)
        _, capped, _ = get(base, "/hubs/sections/%s?count=1" % section_id)
        by_id = {h.get("hubIdentifier"): h for h in capped.get("Hub") or []}
        cut = [h for h in full.get("Hub") or [] if (h.get("size") or 0) > 1]
        if not cut:
            skip("count caps a shelf and sets more on section %s" % section_id,
                 "no shelf has more than one row")
            continue
        ok = all(len(by_id.get(h["hubIdentifier"], {}).get("Metadata") or []) == 1
                 and by_id.get(h["hubIdentifier"], {}).get("size") == 1
                 and by_id.get(h["hubIdentifier"], {}).get("more") is True
                 for h in cut)
        untouched = [h for h in full.get("Hub") or [] if (h.get("size") or 0) <= 1]
        ok = ok and all(by_id.get(h["hubIdentifier"], {}).get("more") is False
                        for h in untouched)
        check("count caps a shelf and sets more on section %s" % section_id, ok,
              "%d shelf(s) cut, %d untouched" % (len(cut), len(untouched)))
    _, home, _ = get(base, "/hubs?count=1")
    check("count caps the Home shelves too",
          all(len(h.get("Metadata") or []) <= 1 for h in home.get("Hub") or []),
          ",".join("%s=%s" % (h.get("hubIdentifier"), h.get("size"))
                   for h in home.get("Hub") or []))

    # The Top shelf counts PLAYS, not the row's current viewCount: measured on the
    # reference, a movie that had been played and then marked unplayed still
    # reported globalViewCount 1 with no viewCount at all.
    status, _, _ = container(base, "GET", "/library/all/top")
    check("the top shelf needs a type, as a real PMS does", status == 400,
          "HTTP %s" % status)
    _, top, _ = get(base, "/library/all/top?type=1&limit=5")
    rows = top.get("Metadata") or []
    _, played, _ = get(base, "/status/sessions/history/all")
    history = [h for h in played.get("Metadata") or [] if h.get("type") == "movie"]
    if history:
        check("the top shelf carries globalViewCount, userCount and User",
              rows and all(r.get("globalViewCount") and r.get("userCount")
                           and r.get("User") for r in rows),
              "%d row(s): %s" % (len(rows), [(r.get("title"), r.get("globalViewCount"),
                                              r.get("User")) for r in rows[:2]]))
    else:
        skip("the top shelf carries globalViewCount, userCount and User",
             "nothing has been played yet in this run")

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
    # A term's TAIL is a list too: direction and modifiers come in either order,
    # and reading the direction off the first colon made `titleSort:desc:nocollate`
    # ascending -- the reference orders it descending.
    def titles(spec):
        _, body, _ = get(base, "/library/sections/%s/all?sort=%s" % (movie_section, spec))
        return [r.get("title") for r in body.get("Metadata") or []]

    check("a sort modifier does not eat the direction",
          titles("titleSort:desc:nocollate") == titles("titleSort:desc")
          and titles("titleSort:nocollate:desc") == titles("titleSort:desc")
          and titles("titleSort:desc") == list(reversed(titles("titleSort:asc"))),
          ", ".join(str(t) for t in titles("titleSort:desc:nocollate")))
    check("an unknown modifier is ignored, where an unknown KEY is a 500",
          titles("titleSort:bogus") == titles("titleSort"),
          ", ".join(str(t) for t in titles("titleSort:bogus")))
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
