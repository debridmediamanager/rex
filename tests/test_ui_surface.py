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

import base64
import argparse
import json
import os
import re
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


def eventsource(base, trigger, seconds=8, spec=""):
    """Read the SSE channel while `trigger` runs. Returns (status, [(event, data)]).

    `spec` appends a query string -- `?filters=log` is the one the client's own log
    connection passes, and the filter is what decides which frames arrive at all.
    """
    import threading

    request_ = urllib.request.Request(
        base.rstrip("/") + "/:/eventsource/notifications" + spec,
        headers={"Accept": "text/event-stream", "User-Agent": UA})
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
            if len(events) >= 12:
                # A refresh alone pushes seven frames plus the opening ping, so a cap
                # of four cut the sequence off before the one that matters (state 5).
                break
    finally:
        response.close()
    return 200, events


def request(base, method, path, want_json=True, body=None):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method, data=body, headers={
        "User-Agent": UA,
        "Accept": "application/json" if want_json else "*/*",
        "X-Plex-Client-Identifier": "plexsim-ui-test",
        "X-Plex-Product": "Plex Web",
        "X-Plex-Version": "4.147.1",
        # The web client sends its platform on every request, and a real PMS picks
        # the playback profile by it: a decision without one is a 400.
        "X-Plex-Platform": "Chrome",
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
        # A grid row carries no Stream list (measured), so the track is looked for
        # on the DETAIL row.
        _, candidate_detail, _ = get(base, "/library/metadata/%s" % candidate["ratingKey"])
        candidate = (candidate_detail.get("Metadata") or [candidate])[0]
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
    # Past the minute: the reference does not store EXACTLY 60 000 (measured), so a
    # resume point at 60 000 is not one.
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=stopped&time=90000"
                         "&duration=600000&identifier=com.plexapp.plugins.library"
                         % (movie, quote("/library/metadata/%s" % movie)))
    status, timeline, _ = get(base, "/:/timeline?ratingKey=%s&state=stopped&time=90000" % movie)
    check("timeline answers with playbackState",
          status == 200 and timeline.get("playbackState") is not None,
          "playbackState=%s" % timeline.get("playbackState"))
    _, hubs, _ = get(base, "/hubs/continueWatching")
    rows = [r for hub in hubs.get("Hub") or [] for r in hub.get("Metadata") or []]
    check("a resumed item appears in Continue Watching",
          any(str(r.get("ratingKey")) == str(movie) for r in rows),
          "%d row(s) in the hub" % len(rows))
    # Re-measured: this HIDES the row and KEEPS the resume point. The reference's
    # viewOffset was still 91000 afterwards and the row simply stopped appearing in
    # /library/onDeck and /hubs/continueWatching -- until the next timeline brought it
    # back. plexsim deleted the offset, so the entry also threw away the resume
    # position and the next Play started the item over.
    status, hidden, _ = container(base, "PUT",
                                  "/actions/removeFromContinueWatching?ratingKey=%s" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    _, hubs, _ = get(base, "/hubs/continueWatching")
    rows = [r for hub in hubs.get("Hub") or [] for r in hub.get("Metadata") or []]
    _, on_deck, _ = get(base, "/library/onDeck")
    check("Remove from Continue Watching hides the row and keeps the offset",
          status == 200
          and (detail.get("Metadata") or [{}])[0].get("viewOffset")
          and not any(str(r.get("ratingKey")) == str(movie) for r in rows)
          and not any(str(r.get("ratingKey")) == str(movie)
                      for r in on_deck.get("Metadata") or []),
          "viewOffset=%s, %d hub row(s)"
          % ((detail.get("Metadata") or [{}])[0].get("viewOffset"), len(rows)))
    status, _, _ = container(base, "PUT",
                             "/actions/removeFromContinueWatching?ratingKey=999999")
    check("removing an unknown item from Continue Watching is a 404", status == 404,
          "HTTP %s" % status)
    # And the next timeline puts it back.
    request(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=90000"
                         "&duration=600000" % movie)
    _, hubs, _ = get(base, "/hubs/continueWatching")
    rows = [r for hub in hubs.get("Hub") or [] for r in hub.get("Metadata") or []]
    check("the next timeline brings it back",
          any(str(r.get("ratingKey")) == str(movie) for r in rows),
          "%d row(s)" % len(rows))
    # A resume point under a MINUTE is not stored at all -- the reference leaves the
    # row untouched at 59 s and starts recording at 61.
    _, spare_detail, _ = get(base, "/library/metadata/%s" % (spare or movie))
    if spare and "viewOffset" not in (spare_detail.get("Metadata") or [{}])[0]:
        request(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=5000"
                             "&duration=600000" % spare)
        _, glanced, _ = get(base, "/library/metadata/%s" % spare)
        check("a resume point under a minute is not stored",
              "viewOffset" not in (glanced.get("Metadata") or [{}])[0],
              str((glanced.get("Metadata") or [{}])[0].get("viewOffset")))
    else:
        skip("a resume point under a minute is not stored", "no clean second row")

    # ------------------------------------------------------------------
    # 4. Play History
    # ------------------------------------------------------------------
    print("\n=== View Play History ===")
    # Re-measured: a SCROBBLE writes no history row -- the reference's history stayed
    # at one row through three of them, because rows come only from a timeline that
    # reaches the end. plexsim recorded one per mark-as-played, so History and the Top
    # shelf counted things nobody had watched. So the row has to be earned here.
    # Re-measured in cycle 10 on half-hour media: a play is completed by a beat that
    # CROSSES 90% inside its session, so the row is earned by a beat below the threshold
    # first and one past it after -- a single beat at the end completes nothing, on a real
    # PMS or here. This check used to send only the second beat.
    request(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=61000"
                         "&duration=100000" % movie)
    request(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=100000"
                         "&duration=100000" % movie)
    request(base, "GET", "/:/timeline?ratingKey=%s&state=stopped&time=100000"
                         "&duration=100000" % movie)
    status, history, _ = get(base, "/status/sessions/history/all?type=1")
    rows = history.get("Metadata") or []
    check("a completed play writes a history row", bool(rows), "%d row(s)" % len(rows))
    _, before_scrobble, _ = get(base, "/status/sessions/history/all?type=1")
    request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library"
                         % movie)
    _, after_scrobble, _ = get(base, "/status/sessions/history/all?type=1")
    check("a scrobble writes none",
          len(after_scrobble.get("Metadata") or [])
          == len(before_scrobble.get("Metadata") or []),
          "%d -> %d" % (len(before_scrobble.get("Metadata") or []),
                        len(after_scrobble.get("Metadata") or [])))
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
    status, paged, _ = get(base, "/status/sessions/history/all?type=1"
                                 "&X-Plex-Container-Start=0&X-Plex-Container-Size=1")
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
    # Measured field by field: only `title` and `summary` are STORED as the empty
    # string -- everything else is removed so the agent can supply it again, and an
    # empty INTEGER is a 500. plexsim removed every field and accepted every value.
    request(base, "PUT", "/library/metadata/%s?summary.value=" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("an empty summary is stored as the empty string, not removed",
          row.get("summary") == "", repr(row.get("summary")))
    status, _, _ = container(base, "PUT", "/library/metadata/%s?year.value=" % movie)
    check("an empty year is a 500, as an empty integer filter is", status == 500,
          "HTTP %s" % status)

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

    print("\n=== the Edit dialog's own paths ===")
    # THE save path: MultiMetadataModel.saveUrl is `PUT /library/sections/{id}/all`,
    # and plexsim answered the LISTING to it with a 200 -- so every Edit dialog Save
    # applied nothing and the dialog reported success. The quietest failure on the
    # whole surface.
    status, body, _ = request(base, "PUT", "/library/sections/%s/all?type=1&id=%s"
                                          "&includeExternalMedia=1&tagline.value=%s"
                                          "&tagline.locked=1"
                                          % (movie_section, movie, quote("edited here")))
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    check("the section-wide save path applies the edit and answers an empty 200",
          is_empty_200(status, body) and row.get("tagline") == "edited here"
          and "tagline" in {f.get("name") for f in row.get("Field") or []},
          "tagline=%r" % row.get("tagline"))
    status, _, _ = container(base, "PUT", "/library/sections/%s/all?tagline.value=x"
                                         % movie_section)
    check("that path needs a type, as a real PMS does", status == 400, "HTTP %s" % status)
    # With no `id` it edits the whole FILTERED set -- which is how "Edit all" works.
    _, before, _ = get(base, "/library/sections/%s/all" % movie_section)
    everything = [r.get("ratingKey") for r in before.get("Metadata") or []]
    request(base, "PUT", "/library/sections/%s/all?type=1&studio.value=%s"
                         % (movie_section, quote("Edited Studio")))
    _, after, _ = get(base, "/library/sections/%s/all" % movie_section)
    check("with no id it edits every row the filter selects",
          all(r.get("studio") == "Edited Studio" for r in after.get("Metadata") or [])
          and len(everything) > 1,
          "%d row(s)" % len(after.get("Metadata") or []))
    # A bare `field=value` on the SECTION path is a FILTER that scopes the edit, not
    # a field to write -- where on the ITEM path the same shape IS an edit.
    target = (after.get("Metadata") or [{}])[0]
    # `title%3D=` is the EXACT operator: a bare `title=` CONTAINS, and the fixture's
    # rows share a title prefix, so a contains-filter would scope to all of them.
    request(base, "PUT", "/library/sections/%s/all?type=1&title%%3D=%s"
                         "&contentRating.value=%s"
                         % (movie_section, quote(str(target.get("title"))),
                            quote("SCOPED")))
    _, scoped, _ = get(base, "/library/sections/%s/all" % movie_section)
    hits = [r for r in scoped.get("Metadata") or [] if r.get("contentRating") == "SCOPED"]
    check("a bare parameter scopes the edit rather than being written",
          len(hits) == 1 and hits[0].get("ratingKey") == target.get("ratingKey"),
          "%d of %d row(s) edited" % (len(hits), len(scoped.get("Metadata") or [])))
    # The dialog's PRE-read. plexsim 404'd it, and populate() rejects on that, so
    # Edit said "There was an error editing this item" before the user could type.
    status, common, _ = get(base, "/library/sections/%s/common?type=1&id=%s"
                                 "&includeExternalMedia=1" % (movie_section, movie))
    one = (common.get("Metadata") or [{}])[0]
    check("the Edit dialog's /common pre-read answers a Common row",
          status == 200 and one.get("type") == "common"
          and one.get("mixedFields") == "" and common.get("title2") == "Common",
          "type=%r mixedFields=%r" % (one.get("type"), one.get("mixedFields")))
    if spare:
        _, mixed, _ = get(base, "/library/sections/%s/common?type=1&id=%s,%s"
                                % (movie_section, movie, spare))
        row_mixed = (mixed.get("Metadata") or [{}])[0]
        # Measured: the fields that DIFFER are dropped and named in `mixedFields`,
        # which is how the dialog knows to show "(mixed)" rather than overwrite.
        check("several ids name their differing fields in mixedFields",
              "title" in (row_mixed.get("mixedFields") or "")
              and "title" not in row_mixed,
              row_mixed.get("mixedFields"))
    else:
        skip("several ids name their differing fields in mixedFields", "need a second row")
    status, _, _ = container(base, "GET", "/library/sections/%s/common" % movie_section)
    check("/common needs a type too", status == 400, "HTTP %s" % status)
    # The dialog's source of EDITABLE TAG FIELDS. plexsim answered two Directory
    # rows and no Type at all, so the Tags pane had no genre, collection or label.
    _, plain_pivots, _ = get(base, "/library/sections/%s" % movie_section)
    _, detailed, _ = get(base, "/library/sections/%s?includeDetails=1&includeAdvanced=1"
                               % movie_section)
    tag_fields = [f.get("key") for t in detailed.get("Type") or []
                  for f in t.get("Field") or [] if f.get("type") == "tag"]
    check("a section lists its pivots and, with includeDetails, its editable fields",
          len(plain_pivots.get("Directory") or []) == 20
          and not plain_pivots.get("Type")
          and detailed.get("Type") and detailed.get("FieldType")
          and len(tag_fields) >= 10,
          "%d pivot(s), %d tag field(s)" % (len(plain_pivots.get("Directory") or []),
                                            len(tag_fields)))
    check("the pivot rows carry the titles the sidebar prints",
          (plain_pivots.get("Directory") or [{}])[0].get("title") == "All Movies"
          and any(p.get("secondary") for p in plain_pivots.get("Directory") or [])
          and any(p.get("search") for p in plain_pivots.get("Directory") or []),
          ", ".join(str(p.get("title")) for p in (plain_pivots.get("Directory") or [])[:4]))
    # A batch edit through a comma-joined key list, which also answered the listing.
    if spare:
        status, body, _ = request(base, "PUT", "/library/metadata/%s,%s?summary.value=%s"
                                              % (movie, spare, quote("batched")))
        summaries = []
        for key in (movie, spare):
            _, one_row, _ = get(base, "/library/metadata/%s" % key)
            summaries.append((one_row.get("Metadata") or [{}])[0].get("summary"))
        check("a comma-joined key list is a batch EDIT, not a listing",
              is_empty_200(status, body) and summaries == ["batched", "batched"],
              str(summaries))
    else:
        skip("a comma-joined key list is a batch EDIT, not a listing", "need a second row")
    # Parameter-level refusals and coercions, measured one at a time.
    for spec, want, why in (
            ("bogus.value=1", 400, "an unknown field"),
            ("sortTitle.value=abc", 400, "a misspelled field"),
            ("bogustag[0].tag.tag=x", 400, "an unknown tag field"),
            ("year.value=abc", 500, "an unparseable year"),
            ("", 400, "no parameters at all")):
        status, _, _ = container(base, "PUT", "/library/metadata/%s%s"
                                             % (movie, ("?" + spec) if spec else ""))
        check("edit refuses %s" % why, status == want, "HTTP %s" % status)
    # These two REMOVE fields the filter and sort checks further down count on, so
    # whatever was there is put back afterwards.
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    was = (detail.get("Metadata") or [{}])[0]
    kept_year, kept_date = was.get("year"), was.get("originallyAvailableAt")
    request(base, "PUT", "/library/metadata/%s?year.value=-5" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("a negative year removes the field",
          "year" not in (detail.get("Metadata") or [{}])[0],
          str((detail.get("Metadata") or [{}])[0].get("year")))
    request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=notadate" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("an unparseable date removes the field rather than storing the text",
          "originallyAvailableAt" not in (detail.get("Metadata") or [{}])[0],
          str((detail.get("Metadata") or [{}])[0].get("originallyAvailableAt")))
    if kept_year is not None:
        request(base, "PUT", "/library/metadata/%s?year.value=%s" % (movie, kept_year))
    if kept_date is not None:
        request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s"
                             % (movie, quote(str(kept_date))))
    # Only `.locked=1` locks: `true`, `false` and an empty value all UNLOCK.
    request(base, "PUT", "/library/metadata/%s?studio.locked=1" % movie)
    request(base, "PUT", "/library/metadata/%s?studio.locked=true" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    locks = {f.get("name") for f in (detail.get("Metadata") or [{}])[0].get("Field") or []}
    check("only .locked=1 locks a field", "studio" not in locks, ",".join(sorted(locks)))
    # An indexed tag write REPLACES the list, and any `tag-` removal clears it.
    request(base, "PUT", "/library/metadata/%s?genre[0].tag.tag=Alpha"
                         "&genre[1].tag.tag=Beta" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    genres = [g.get("tag") for g in (detail.get("Metadata") or [{}])[0].get("Genre") or []]
    check("an indexed tag write REPLACES the whole list",
          genres == ["Alpha", "Beta"], ", ".join(genres))
    first_ids = {g.get("tag"): g.get("id")
                 for g in (detail.get("Metadata") or [{}])[0].get("Genre") or []}
    request(base, "PUT", "/library/metadata/%s?genre[].tag.tag-=NotThere" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    check("any tag- removal clears the whole list",
          not (detail.get("Metadata") or [{}])[0].get("Genre"),
          str((detail.get("Metadata") or [{}])[0].get("Genre")))
    request(base, "PUT", "/library/metadata/%s?genre[0].tag.tag=alpha" % movie)
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    back = ((detail.get("Metadata") or [{}])[0].get("Genre") or [{}])[0]
    # A tag matches case-insensitively and keeps its canonical spelling AND its id,
    # even after being removed from every row -- a real PMS keeps a tag TABLE.
    check("a re-added tag keeps its canonical spelling and its id",
          back.get("tag") == "Alpha" and back.get("id") == first_ids.get("Alpha"),
          "%s/%s vs %s" % (back.get("tag"), back.get("id"), first_ids.get("Alpha")))

    print("\n=== collections and playlists as metadata items ===")
    # Both are metadata items in their own right, and the client edits them through
    # /library/metadata/{key} -- its saveUrl falls back to that path for a playlist.
    # plexsim kept them outside the catalog, so both answered 404 and neither could
    # be renamed from its own page.
    _, made, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s"
                                        "&title=%s&uri=%s"
                                        % (movie_section, quote("plexsim item probe"),
                                           quote("server://%s/com.plexapp.plugins.library"
                                                 "/library/metadata/%s" % (machine, movie))))
    ckey = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if ckey:
        status, body, _ = request(base, "PUT", "/library/metadata/%s?title.value=%s"
                                              "&titleSort.value=zz&contentRating.value=PG"
                                              % (ckey, quote("The renamed here")))
        _, one, _ = get(base, "/library/metadata/%s" % ckey)
        crow = (one.get("Metadata") or [{}])[0]
        check("a collection is editable through /library/metadata/{key}",
              is_empty_200(status, body) and crow.get("title") == "The renamed here"
              # Measured: an UNLOCKED Sort Title does not stick; the title re-derives it.
              and crow.get("titleSort") == "renamed here" and crow.get("contentRating") == "PG",
              json.dumps({k: crow.get(k) for k in ("title", "titleSort",
                                                   "contentRating")}))
        status, body, _ = request(base, "PUT", "/library/metadata/%s/prefs"
                                              "?collectionSort=1&collectionMode=2" % ckey)
        _, one, _ = get(base, "/library/metadata/%s" % ckey)
        crow = (one.get("Metadata") or [{}])[0]
        # Measured: they come back as STRING attributes, which is what the dialog
        # reads to show the current selection.
        check("collection prefs land on the row as strings",
              is_empty_200(status, body) and crow.get("collectionSort") == "1"
              and crow.get("collectionMode") == "2",
              "%r/%r" % (crow.get("collectionSort"), crow.get("collectionMode")))
        for spec, want, why in (("/library/metadata/%s/prefs" % ckey, 404,
                                 "reading the prefs"),
                                ("/library/metadata/%s/prefs?bogus=1" % ckey, 400,
                                 "an unknown pref")):
            status, _, _ = container(base, "GET" if want == 404 else "PUT", spec)
            check("collection prefs: %s is a %d" % (why, want), status == want,
                  "HTTP %s" % status)
        request(base, "DELETE", "/library/collections/%s" % ckey)
    else:
        skip("a collection is editable through /library/metadata/{key}",
             "could not create a collection")
    _, made, _ = container(base, "POST", "/playlists?type=video&title=%s&uri=%s"
                                        % (quote("plexsim item playlist"),
                                           quote("server://%s/com.plexapp.plugins.library"
                                                 "/library/metadata/%s" % (machine, movie))))
    pkey = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if pkey:
        status, body, _ = request(base, "PUT", "/library/metadata/%s?title.value=%s"
                                              % (pkey, quote("playlist renamed")))
        _, one, _ = get(base, "/playlists/%s" % pkey)
        check("a playlist is editable through /library/metadata/{key} too",
              is_empty_200(status, body)
              and (one.get("Metadata") or [{}])[0].get("title") == "playlist renamed",
              (one.get("Metadata") or [{}])[0].get("title"))
        for spec, want, why in ((("?bogus=1"), 400, "an unknown parameter"),
                                ("", 400, "no parameters"),
                                ("?smart=1", 400, "a smart flag it cannot honour"),
                                ("?title=fine", 200, "a title")):
            status, _, _ = container(base, "PUT", "/playlists/%s%s" % (pkey, spec))
            check("playlist PUT: %s is a %d" % (why, want), status == want,
                  "HTTP %s" % status)
        request(base, "DELETE", "/playlists/%s" % pkey)
    else:
        skip("a playlist is editable through /library/metadata/{key} too",
             "could not create a playlist")

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
    # Re-measured: a refresh's activity ENDS. plexsim only ever fired `started`, so
    # an activity that nothing cancelled stayed in /activities for the life of the
    # process and the header's spinner never stopped -- this check used to assert
    # that stuck row was there.
    status, activities, _ = get(base, "/activities")
    check("a finished refresh leaves no activity behind",
          status == 200 and not activities.get("Activity"),
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
        # Change Poster writes to the SINGULAR path, and the response body is the new
        # artwork path -- the client does `model.set("thumb", response)` with it.
        # plexsim had the plural path as the write and served image bytes from the
        # singular one, so Change Poster did nothing AND set the tile's img src to a
        # blob of PNG. The plural path is a 404 for a write.
        status, body, _ = request(base, "PUT", "/library/metadata/%s/poster?url=%s"
                                               % (movie, quote(entries[-1]["key"])))
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        row = (detail.get("Metadata") or [{}])[0]
        # Decoded defensively: a server that answers image BYTES here (what plexsim
        # did before this was measured) must FAIL this check, not raise a
        # UnicodeDecodeError that kills the run before the rest of the suite speaks.
        check("selecting a poster answers the new artwork path as its body",
              status == 200
              and body.decode("utf-8", "replace") == str(row.get("thumb")),
              "HTTP %s body=%r thumb=%r" % (status, body[:48], row.get("thumb")))
        status_plural, _, _ = request(base, "PUT", "/library/metadata/%s/posters?url=%s"
                                                  % (movie, quote(entries[-1]["key"])))
        check("the plural artwork path is a 404 for a write", status_plural == 404,
              "HTTP %s" % status_plural)
        _, after, _ = get(base, "/library/metadata/%s/posters" % movie)
        check("the chosen poster comes back selected",
              any(p.get("selected") for p in after.get("Metadata") or []),
              str([p.get("selected") for p in after.get("Metadata") or []]))
        status_img, _, headers_img = request(base, "GET", "/library/metadata/%s/poster"
                                                         % movie, want_json=False)
        check("the singular path on a GET is the image itself",
              status_img == 200
              and str(headers_img.get("Content-Type", "")).startswith("image/"),
              "HTTP %s %s" % (status_img, headers_img.get("Content-Type")))

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
    # Re-measured: the source URI comes back REWRITTEN, not echoed -- a single
    # item as `library://x/item/<encoded path>` and anything naming a list as
    # `library://x/directory/<the same>`. plexsim echoed the raw server:// uri, so
    # a client that re-read the queue got a source it had not sent.
    check("play queue rewrites its source uri to a library:// one",
          queue.get("playQueueSourceURI")
          == "library://x/item/%s" % quote("/library/metadata/%s" % movie)
          and "allowSync" not in queue,
          queue.get("playQueueSourceURI"))
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
        # Measured on a writable copy of the reference: a client's new queue RETIRES its
        # older ones, so Play Next works on a queue built after the Play All and Shuffle
        # queues above rather than on the first one.
        queue_id = container(base, "POST", "/playQueues?type=video&shuffle=0&repeat=0&own=1&uri=%s"
                                           % quote(uri))[1].get("playQueueID")
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

    print("\n=== the play queue as a queue, not a key list ===")
    # A queue over a SHOW or a SEASON holds its EPISODES. plexsim enqueued the
    # parent row, which has no Media at all, so Play, Play All and Shuffle on every
    # show page, season page and TV library handed the player something it could
    # not play -- "An error occurred loading items to play" for the whole TV half.
    if show:
        show_uri = ("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                    % (machine, show))
        _, leaves, _ = get(base, "/library/metadata/%s/allLeaves" % show)
        wanted = len(leaves.get("Metadata") or [])
        status, show_queue, _ = container(base, "POST", "/playQueues?type=video&uri=%s"
                                                       "&continuous=1" % quote(show_uri))
        kinds = {m.get("type") for m in show_queue.get("Metadata") or []}
        check("a queue over a show holds its episodes, not the show row",
              status == 200 and len(show_queue.get("Metadata") or []) == wanted
              and kinds == {"episode"},
              "%d item(s) of type %s, wanted %d"
              % (len(show_queue.get("Metadata") or []), kinds or "-", wanted))
        _, children, _ = get(base, "/library/metadata/%s/children" % show)
        season = (children.get("Metadata") or [{}])[0].get("ratingKey")
        if season:
            season_uri = ("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                          % (machine, season))
            _, season_children, _ = get(base, "/library/metadata/%s/children" % season)
            _, season_queue, _ = container(base, "POST", "/playQueues?type=video&uri=%s"
                                                        % quote(season_uri))
            check("a queue over a season holds that season's episodes",
                  len(season_queue.get("Metadata") or [])
                  == len(season_children.get("Metadata") or [])
                  and {m.get("type") for m in season_queue.get("Metadata") or []}
                      == {"episode"},
                  "%d item(s)" % len(season_queue.get("Metadata") or []))
        else:
            skip("a queue over a season holds that season's episodes", "no season")
    else:
        skip("a queue over a show holds its episodes, not the show row", "no show row")
    # A multi-select Play sends a comma-joined key list; a resolver that stops at
    # the first key silently plays one of them.
    if spare:
        multi = ("server://%s/com.plexapp.plugins.library/library/metadata/%s,%s"
                 % (machine, movie, spare))
        _, multi_queue, _ = container(base, "POST", "/playQueues?type=video&uri=%s"
                                                   % quote(multi))
        check("a queue over a comma-joined key list holds every key",
              len(multi_queue.get("Metadata") or []) == 2,
              "%d item(s)" % len(multi_queue.get("Metadata") or []))
    else:
        skip("a queue over a comma-joined key list holds every key", "need a second row")
    # Item ids are GLOBAL and stable, the version steps on every mutation, and the
    # selection follows the timeline. The client discards any queue response whose
    # selected id is not the one it holds, so all three decide whether the answer
    # is kept at all.
    play_uri = "server://%s/com.plexapp.plugins.library/library/sections/%s/all" \
               % (machine, movie_section)
    _, built, _ = container(base, "POST", "/playQueues?type=video&uri=%s" % quote(play_uri))
    qid = built.get("playQueueID")
    ids_before = [m.get("playQueueItemID") for m in built.get("Metadata") or []]
    add_uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie)
    _, added, _ = container(base, "PUT", "/playQueues/%s?uri=%s" % (qid, quote(add_uri)))
    ids_after = [m.get("playQueueItemID") for m in added.get("Metadata") or []]
    check("queue item ids are stable across an insert",
          all(i in ids_after for i in ids_before)
          and len(ids_after) == len(ids_before) + 1
          and added.get("playQueueVersion") == 2,
          "%s -> %s, version %s" % (ids_before, ids_after, added.get("playQueueVersion")))
    check("a plain add sets playQueueLastAddedItemID",
          added.get("playQueueLastAddedItemID") in ids_after,
          str(added.get("playQueueLastAddedItemID")))
    _, nexted, _ = container(base, "PUT", "/playQueues/%s?uri=%s&next=1"
                                         % (qid, quote(add_uri)))
    order = [m.get("playQueueItemID") for m in nexted.get("Metadata") or []]
    current = nexted.get("playQueueSelectedItemID")
    # The id that appeared is the one Play Next inserted, and it must sit directly
    # after the item the player is on -- ahead of the block a plain add built up.
    inserted = [i for i in order if i not in ids_after]
    check("Play Next inserts immediately after the current item",
          len(inserted) == 1 and current in order
          and order[order.index(current) + 1] == inserted[0],
          "order=%s current=%s inserted=%s" % (order, current, inserted))
    check("a next=1 add leaves playQueueLastAddedItemID where it was",
          nexted.get("playQueueLastAddedItemID") == added.get("playQueueLastAddedItemID"),
          "%s vs %s" % (nexted.get("playQueueLastAddedItemID"),
                        added.get("playQueueLastAddedItemID")))
    # The timeline moves the selection.
    second = order[1] if len(order) > 1 else None
    if second:
        row_key = (nexted.get("Metadata") or [{}])[1].get("ratingKey")
        container(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=1000"
                               "&duration=600000&playQueueItemID=%s" % (row_key, second))
        _, refetched, _ = get(base, "/playQueues/%s" % qid)
        check("the timeline moves the queue's selected item",
              refetched.get("playQueueSelectedItemID") == second
              and refetched.get("playQueueSelectedItemOffset") == 1,
              "selected=%s offset=%s" % (refetched.get("playQueueSelectedItemID"),
                                         refetched.get("playQueueSelectedItemOffset")))
        container(base, "GET", "/:/timeline?ratingKey=%s&state=stopped&time=1000"
                               "&duration=600000" % row_key)
    else:
        skip("the timeline moves the queue's selected item", "queue too short")
    # A move really moves, and steps the version.
    _, current_queue, _ = get(base, "/playQueues/%s" % qid)
    order = [m.get("playQueueItemID") for m in current_queue.get("Metadata") or []]
    if len(order) > 1:
        status, moved, _ = container(base, "PUT", "/playQueues/%s/items/%s/move?after=-1"
                                                 % (qid, order[-1]))
        new_order = [m.get("playQueueItemID") for m in moved.get("Metadata") or []]
        check("a move puts the item at the front and steps the version",
              status == 200 and new_order and new_order[0] == order[-1]
              and moved.get("playQueueVersion") > current_queue.get("playQueueVersion"),
              "%s -> %s" % (order, new_order))
        status, deleted, _ = container(base, "DELETE", "/playQueues/%s/items/%s"
                                                      % (qid, order[-1]))
        check("deleting a queue item removes that item",
              status == 200
              and order[-1] not in [m.get("playQueueItemID")
                                    for m in deleted.get("Metadata") or []],
              "%d item(s) left" % len(deleted.get("Metadata") or []))
    else:
        skip("a move puts the item at the front and steps the version", "queue too short")
    # `window` slices AROUND the current item and totalCount still reports the whole.
    _, windowed, _ = get(base, "/playQueues/%s?window=1" % qid)
    _, whole, _ = get(base, "/playQueues/%s" % qid)
    check("window slices around the current item and keeps totalCount",
          len(windowed.get("Metadata") or []) <= len(whole.get("Metadata") or [])
          and windowed.get("playQueueTotalCount") == whole.get("playQueueTotalCount"),
          "%d of %d, total %s" % (len(windowed.get("Metadata") or []),
                                  len(whole.get("Metadata") or []),
                                  windowed.get("playQueueTotalCount")))
    # A queue built from a playlist names the playlist and carries NO source uri.
    _, made, _ = container(base, "POST", "/playlists?type=video&title=%s&uri=%s"
                                        % (quote("plexsim queue probe"), quote(add_uri)))
    probe_playlist = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if probe_playlist:
        _, from_playlist, _ = container(base, "POST", "/playQueues?type=video&playlistID=%s"
                                                     % probe_playlist)
        check("a playlist queue names the playlist and omits the source uri",
              from_playlist.get("playQueuePlaylistID") == int(probe_playlist)
              and from_playlist.get("playQueuePlaylistTitle") == "plexsim queue probe"
              and "playQueueSourceURI" not in from_playlist
              and len(from_playlist.get("Metadata") or []) == 1,
              json.dumps({k: v for k, v in from_playlist.items()
                          if not isinstance(v, list)})[:120])
        request(base, "DELETE", "/playlists/%s" % probe_playlist)
    else:
        skip("a playlist queue names the playlist and omits the source uri",
             "could not create a playlist")
    # A queue of our own for the refusals below: the checks above built newer ones, and a
    # client's new queue retires its older ones.
    qid = container(base, "POST", "/playQueues?type=video&uri=%s" % quote(add_uri))[1].get("playQueueID")
    # Every refusal measured on the reference, each of which plexsim answered 200 to.
    for method, path_, want, why in (
            ("POST", "/playQueues?type=video", 400, "a create with no uri"),
            ("POST", "/playQueues?uri=%s" % quote(add_uri), 400, "a create with no type"),
            ("GET", "/playQueues?type=1", 400, "a GET of the collection"),
            ("GET", "/playQueues/999999", 404, "an unknown queue"),
            ("PUT", "/playQueues/%s" % qid, 400, "a refresh PUT with no uri"),
            ("PUT", "/playQueues/%s/shuffle" % qid, 404, "the shuffle toggle"),
            ("PUT", "/playQueues/%s/unshuffle" % qid, 404, "the unshuffle toggle"),
            ("DELETE", "/playQueues/%s/items/999999" % qid, 404, "an unknown queue item"),
            ("POST", "/playQueues/%s" % qid, 404, "a POST to one queue"),
            ("DELETE", "/playQueues/%s" % qid, 404, "a DELETE of one queue")):
        status, _, _ = container(base, method, path_)
        check("play queue: %s is a %d" % (why, want), status == want, "HTTP %s" % status)

    print("\n=== playback state: progress, refusals, history filters ===")
    # /:/progress is NOT an alias of /:/timeline: it keys on `key`, not `ratingKey`,
    # and answers an EMPTY body. Routed at the timeline it read no key at all, so
    # every client that reports progress this way lost its resume point silently.
    status, body, _ = request(base, "GET", "/:/progress?key=%s"
                                          "&identifier=com.plexapp.plugins.library"
                                          "&time=90000&state=stopped" % movie)
    _, after, _ = get(base, "/library/metadata/%s" % movie)
    stored = (after.get("Metadata") or [{}])[0].get("viewOffset")
    check("progress records the offset and answers an empty body",
          is_empty_200(status, body) and stored == 90000,
          "HTTP %s body=%r viewOffset=%s" % (status, body[:20], stored))
    for path_, want, why in (
            ("/:/progress?time=1", 400, "progress with no key"),
            ("/:/progress?key=999999&time=1&identifier=com.plexapp.plugins.library", 404, "progress for an unknown key"),
            ("/:/scrobble", 400, "scrobble with no key"),
            ("/:/scrobble?key=999999&identifier=com.plexapp.plugins.library", 404, "scrobble for an unknown key"),
            ("/:/unscrobble?key=999999&identifier=com.plexapp.plugins.library", 404, "unscrobble for an unknown key"),
            ("/:/rate?rating=8", 400, "rate with no key"),
            ("/:/rate?key=999999&rating=8&identifier=com.plexapp.plugins.library", 404, "rate for an unknown key"),
            ("/:/rate?key=%s&rating=abc" % movie, 400, "rate with an unparseable rating"),
            ("/:/timeline?ratingKey=%s&state=wibble" % movie, 400, "a timeline state"
             " outside the closed set"),
            ("/:/timeline?ratingKey=999999&state=playing", 404,
             "a timeline for an unknown key"),
            ("/library/metadata/%s,%s/userState" % (movie, movie), 404,
             "a leaf on a comma-joined key list"),
            ("/library/metadata/notanumber", 404, "a non-numeric ratingKey"),
            ("/subtitles/:/transcode/universal/start?path=x", 400,
             "the subtitle sidecar endpoint")):
        status, _, _ = container(base, "GET", path_)
        check("refusal: %s is a %d" % (why, want), status == want, "HTTP %s" % status)
    # The History page filters and sorts, and plexsim ignored all of it.
    # Cycle 15: every read here carries `type=1`. A real PMS lists NOTHING
    # without one, so these checks were reading a page the client never sees.
    _, whole_history, _ = get(base, "/status/sessions/history/all?type=1")
    rows = whole_history.get("Metadata") or []
    if rows:
        one = rows[0]
        _, by_item, _ = get(base, "/status/sessions/history/all?type=1&metadataItemID=%s"
                                  % one.get("ratingKey"))
        _, by_account, _ = get(base, "/status/sessions/history/all?type=1&accountID=999999")
        _, since, _ = get(base, "/status/sessions/history/all?type=1&viewedAt%%3E=%d"
                                % (int(one.get("viewedAt") or 0) + 1))
        check("history filters by item, account and viewedAt",
              all(str(r.get("ratingKey")) == str(one.get("ratingKey"))
                  for r in by_item.get("Metadata") or [])
              and by_account.get("size") == 0
              and (since.get("size") or 0) < len(rows) + 1,
              "item=%s account=%s since=%s" % (by_item.get("size"),
                                               by_account.get("size"),
                                               since.get("size")))
        _, ascending, _ = get(base, "/status/sessions/history/all?type=1&sort=viewedAt")
        _, descending, _ = get(base, "/status/sessions/history/all?type=1&sort=viewedAt:desc")
        up = [r.get("viewedAt") for r in ascending.get("Metadata") or []]
        down = [r.get("viewedAt") for r in descending.get("Metadata") or []]
        check("history sorts by viewedAt either way",
              up == sorted(up) and down == sorted(down, reverse=True),
              "%s / %s" % (up, down))
        # Measured: an unknown sort key here is a 400, where a library listing
        # answers 500 for the same mistake.
        status, _, _ = container(base, "GET", "/status/sessions/history/all?sort=wibble")
        check("an unknown history sort key is a 400, not a 500", status == 400,
              "HTTP %s" % status)
        # And a row can be deleted, which plexsim answered 200 to without deleting.
        key = str(one.get("historyKey") or "").rsplit("/", 1)[-1]
        status, deleted_row, _ = container(base, "DELETE",
                                           "/status/sessions/history/%s" % key)
        _, left, _ = get(base, "/status/sessions/history/all?type=1")
        check("deleting a history row removes it",
              status == 200 and deleted_row.get("size") == 0
              and len(left.get("Metadata") or []) == len(rows) - 1,
              "%d row(s) left of %d" % (len(left.get("Metadata") or []), len(rows)))
        status, _, _ = container(base, "DELETE", "/status/sessions/history/999999")
        check("deleting an unknown history row is a 404", status == 404,
              "HTTP %s" % status)
    else:
        skip("history filters by item, account and viewedAt", "no history rows yet")
    # Stream selection validates the id, and a refusal leaves the selection alone.
    _, detail, _ = get(base, "/library/metadata/%s" % movie)
    row = (detail.get("Metadata") or [{}])[0]
    part = ((row.get("Media") or [{}])[0].get("Part") or [{}])[0]
    audio = [s for media in row.get("Media") or []
             for p in media.get("Part") or []
             for s in p.get("Stream") or [] if s.get("streamType") == 2]
    if part.get("id") and audio:
        before = {s.get("id"): s.get("selected") for s in audio}
        status, _, _ = request(base, "PUT", "/library/parts/%s?audioStreamID=999999"
                                           % part["id"])
        _, again, _ = get(base, "/library/metadata/%s" % movie)
        after_row = (again.get("Metadata") or [{}])[0]
        now = {s.get("id"): s.get("selected") for media in after_row.get("Media") or []
               for p in media.get("Part") or [] for s in p.get("Stream") or []
               if s.get("streamType") == 2}
        check("an unknown stream id is a 400 and leaves the selection alone",
              status == 400 and now == before, "HTTP %s, %s -> %s" % (status, before, now))
        status_zero, _, _ = request(base, "PUT", "/library/parts/%s?audioStreamID=0"
                                                % part["id"])
        status_none, _, _ = request(base, "PUT", "/library/parts/%s?subtitleStreamID=0"
                                                % part["id"])
        check("audioStreamID=0 is a 400 where subtitleStreamID=0 is the None track",
              status_zero == 400 and status_none == 200,
              "%s / %s" % (status_zero, status_none))
    else:
        skip("an unknown stream id is a 400 and leaves the selection alone",
             "the row has no audio stream")

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
        # The removal above left one item; put a DIFFERENT one back so the reorder
        # has something to reorder. A real PMS does not add a row that is already in
        # the playlist -- measured, `leafCountAdded: 0` and the count unchanged -- so
        # re-adding the same key would leave one item here.
        _, section_rows, _ = get(base, "/library/sections/%s/all" % movie_section)
        present = {str(r.get("ratingKey")) for r in items.get("Metadata") or []}
        other = next((r.get("ratingKey") for r in section_rows.get("Metadata") or []
                      if str(r.get("ratingKey")) not in present), None)
        if other:
            container(base, "PUT", "/playlists/%s/items?uri=%s"
                                   % (playlist_key,
                                      quote("server://%s/com.plexapp.plugins.library"
                                            "/library/metadata/%s" % (machine, other))))
            # And prove the dedupe while we are here.
            _, twice, _ = container(base, "PUT", "/playlists/%s/items?uri=%s"
                                                % (playlist_key,
                                                   quote("server://%s/com.plexapp"
                                                         ".plugins.library/library"
                                                         "/metadata/%s"
                                                         % (machine, other))))
            check("re-adding a playlist item adds nothing",
                  twice.get("leafCountAdded") == 0
                  and twice.get("leafCountRequested") == 1,
                  "added=%s requested=%s" % (twice.get("leafCountAdded"),
                                             twice.get("leafCountRequested")))
        else:
            skip("re-adding a playlist item adds nothing", "no second row to add")
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
                # Measured: the move answers the collection row and switches the
                # collection to its custom order.
                check("a collection member can be reordered",
                      status == 200 and json.loads(body or b"{}").get("MediaContainer", {}).get("size") == 1
                      and now and now[0] == order[-1],
                      "%s -> %s" % (order, now))
            else:
                skip("a collection member can be reordered", "need two members")
        else:
            skip("a collection member can be reordered", "need a second row")
        status, body, _ = request(base, "PUT", "/library/collections/%s?title=%s"
                                              % (collection_key, quote("plexsim renamed")))
        _, one, _ = get(base, "/library/collections/%s" % collection_key)
        # Measured on a writable copy of the reference: the rename changes the title and
        # leaves the members' tag as it was.
        check("renaming a collection renames it",
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
    # Measured: the single-collection container carries allowSync TRUE, the
    # identifier and the media-tag pair, and its librarySectionID is a NUMBER --
    # plexsim sent a string and omitted the other three, so a client could not
    # resolve a tag into an image URL from a collection page. And `/items`
    # answers the members, where plexsim answered an empty container.
    _, made, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s"
                                        "&title=%s&uri=%s"
                                        % (movie_section, quote("plexsim shape probe"),
                                           quote("server://%s/com.plexapp.plugins.library"
                                                 "/library/metadata/%s" % (machine, movie))))
    probe_key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if probe_key:
        _, one_collection, _ = get(base, "/library/collections/%s" % probe_key)
        _, by_items, _ = get(base, "/library/collections/%s/items" % probe_key)
        _, by_children, _ = get(base, "/library/collections/%s/children" % probe_key)
        check("a collection container carries the identifier triple and a numeric id",
              one_collection.get("allowSync") is True
              and one_collection.get("identifier") == "com.plexapp.plugins.library"
              and one_collection.get("mediaTagPrefix")
              and isinstance(one_collection.get("librarySectionID"), int),
              ",".join(sorted(k for k in one_collection
                              if not isinstance(one_collection[k], list))))
        check("/items answers the same members as /children",
              (by_items.get("size") or 0) == (by_children.get("size") or 0) > 0,
              "%s vs %s" % (by_items.get("size"), by_children.get("size")))
        request(base, "DELETE", "/library/collections/%s" % probe_key)
    else:
        skip("a collection container carries the identifier triple and a numeric id",
             "could not create a collection")

    # Measured: the playlist ITEMS container reports its duration in SECONDS where
    # the playlist ROW reports the same number in milliseconds -- one endpoint apart.
    _, built_playlist, _ = container(
        base, "POST", "/playlists?type=video&title=%s&uri=%s"
                      % (quote("plexsim duration probe"),
                         quote("server://%s/com.plexapp.plugins.library"
                               "/library/metadata/%s" % (machine, movie))))
    row = (built_playlist.get("Metadata") or [{}])[0]
    if row.get("ratingKey") and (row.get("duration") or 0) > 0:
        _, its_items, _ = get(base, "/playlists/%s/items" % row["ratingKey"])
        check("the playlist items container's duration is in seconds",
              its_items.get("duration") == int(round(row["duration"] / 1000.0))
              and its_items.get("duration") != row["duration"],
              "row=%sms container=%ss" % (row.get("duration"), its_items.get("duration")))
        request(base, "DELETE", "/playlists/%s" % row["ratingKey"])
    else:
        skip("the playlist items container's duration is in seconds",
             "the probe row has no duration")

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
            # Re-measured in cycle 9 on both servers: `/albums` is NOT refused -- it is
            # an empty "By Album" list, on a movie and a show library alike. This entry
            # had wanted a 404.
            ("GET", "/library/sections/%s/albums" % movie_section, 200, "a music leaf"),
            ("GET", "/library/streams/99999999", 404, "an unknown sidecar stream"),
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
          early.get("playbackState") == "ignore" and early.get("skipCount") == 0,
          "%s viewOffset=%s" % (early.get("playbackState"), early.get("viewOffset")))
    # `viewCount` on the response is the item's OWN count -- 0 on a first play in
    # progress, measured -- not a 1 for "being watched".
    stored_count = (get(base, "/library/metadata/%s" % session_item)[1]
                    .get("Metadata") or [{}])[0].get("viewCount", 0)
    check("and playbackState=progress past a minute",
          late.get("playbackState") == "progress" and late.get("viewCount") == stored_count,
          "%s viewCount=%s stored=%s" % (late.get("playbackState"), late.get("viewCount"),
                                         stored_count))
    # Re-measured, correcting the earlier note: `viewOffset` is the item's STORED
    # offset and is -1 only when it has none. plexsim always sent -1, so a client
    # reading its resume point off the timeline never found one.
    check("the timeline echoes the item's stored offset, not -1",
          late.get("viewOffset") == 90000,
          "viewOffset=%s" % late.get("viewOffset"))
    # And `Bandwidths` is armed by a DECISION, not by the state: it appears on
    # every timeline once one has been made for the item, `stopped` included, and
    # disappears after the stopping one. plexsim keyed it on the state alone.
    _, no_decision, _ = beat("playing", 5000)
    container(base, "GET", "/video/:/transcode/universal/decision?path=%s"
                           "&mediaIndex=0&partIndex=0&protocol=dash&session=uitest"
                           "&directPlay=1&directStream=1&hasMDE=1"
                           % quote("/library/metadata/%s" % session_item))
    _, after_decision, _ = beat("playing", 90000)
    _, on_stop, _ = beat("stopped", 90000)
    # The stopping beat closed the session and disarmed Bandwidths; the terminate
    # checks below need a live one, so it is re-opened here.
    container(base, "GET", "/video/:/transcode/universal/decision?path=%s"
                           "&mediaIndex=0&partIndex=0&protocol=dash&session=uitest"
                           "&directPlay=1&directStream=1&hasMDE=1"
                           % quote("/library/metadata/%s" % session_item))
    beat("playing", 90000)
    check("Bandwidths is armed by a decision, not by the state",
          not no_decision.get("Bandwidths") and after_decision.get("Bandwidths")
          and on_stop.get("Bandwidths"),
          "before=%s after=%s stopped=%s" % (bool(no_decision.get("Bandwidths")),
                                             bool(after_decision.get("Bandwidths")),
                                             bool(on_stop.get("Bandwidths"))))
    # And its resolution is the LABEL, not the raw videoResolution.
    band = ((after_decision.get("Bandwidths") or [{}])[0].get("Bandwidth")
            or [{}])[0]
    check("a Bandwidth's resolution is the label, not the raw value",
          str(band.get("resolution", "")).endswith(("p", "K", "D")),
          str(band.get("resolution")))
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

    print("\n=== the notification LIFECYCLE, which is what makes a client refetch ===")
    # Measured verbatim off the reference's eventsource: a refresh pushes SEVEN
    # frames, and the last one is `state: 5` with NO metadataState. That frame is
    # what dispatches the client's `metadataProcessed` -- which is what makes it
    # refetch the row it just refreshed. plexsim pushed one activity `started` and
    # one `timeline(3, queued)` and stopped: Refresh Metadata did nothing on screen
    # until a reload, the spinner never stopped (its gate is `!!metadataState`, never
    # cleared), and the activity stayed in /activities for the life of the process.
    def sequence(trigger, seconds=5, spec=""):
        status, events = eventsource(base, trigger, seconds=seconds, spec=spec)
        return [(name, json.loads(data)) for name, data in events
                if name != "ping"] if status == 200 else []

    frames = sequence(lambda: request(base, "PUT", "/library/metadata/%s/refresh" % movie))
    states = [(row.get("TimelineEntry") or {}).get("state") for name, row in frames
              if name == "timeline"]
    metadata_states = [(row.get("TimelineEntry") or {}).get("metadataState")
                       for name, row in frames if name == "timeline"]
    activity_events = [(row.get("ActivityNotification") or {}).get("event")
                       for name, row in frames if name == "activity"]
    check("a refresh pushes the reference's timeline states, ending at 5",
          states == [3, 3, 4, 5] and metadata_states[:3] == ["queued", "processing",
                                                             "loading"],
          "states=%s metadataStates=%s" % (states, metadata_states))
    check("the last timeline frame carries NO metadataState",
          metadata_states and metadata_states[-1] is None, str(metadata_states[-1:]))
    check("the activity starts, updates and ENDS",
          activity_events == ["started", "updated", "ended"], str(activity_events))
    progresses = [(row.get("ActivityNotification") or {}).get("Activity", {}).get("progress")
                  for name, row in frames if name == "activity"]
    # Measured: the started frame's progress is -1, not 0, and it reaches 100.
    check("the activity's progress runs -1 to 100",
          progresses and progresses[0] == -1 and progresses[-1] == 100, str(progresses))
    # An EDIT pushes two frames. plexsim pushed none, so an edit made here was
    # invisible to every other open client.
    frames = sequence(lambda: request(base, "PUT", "/library/metadata/%s?tagline.value=%s"
                                                  % (movie, quote("notified"))))
    edit_states = [(row.get("TimelineEntry") or {}).get("state") for name, row in frames
                   if name == "timeline"]
    check("an edit pushes loading then done", edit_states == [3, 5], str(edit_states))
    # And a DELETE pushes state 9, which is what dispatches the client's `deleted`
    # action -- without it a deleted row stays on screen in every other client.
    _, made, _ = container(base, "POST", "/playlists?type=video&title=%s&uri=%s"
                                        % (quote("plexsim notify probe"),
                                           quote("server://%s/com.plexapp.plugins.library"
                                                 "/library/metadata/%s" % (machine, movie))))
    probe_key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if probe_key:
        frames = sequence(lambda: request(base, "DELETE", "/playlists/%s" % probe_key))
        gone = [(row.get("TimelineEntry") or {}).get("state") for name, row in frames
                if name == "timeline"]
        check("a delete pushes state 9", gone == [9], str(gone))
    else:
        skip("a delete pushes state 9", "could not create a playlist")
    # `filters=` is an exact whitelist, and `log` is OPT-IN ONLY. An earlier finding
    # said this channel carries no log frames on a real server -- that measurement
    # was taken WITHOUT the `filters=log` the client's own log connection passes.
    frames = sequence(lambda: request(base, "PUT", "/library/metadata/%s?tagline.value=%s"
                                                  % (movie, quote("logged"))),
                      spec="?filters=log")
    kinds = {name for name, _row in frames}
    check("filters=log answers log frames and nothing else",
          kinds == {"log"} and frames
          and set((frames[0][1].get("LogSentNotification") or {}))
              >= {"level", "thread", "time", "message"},
          "kinds=%s" % (kinds or "none"))
    frames = sequence(lambda: request(base, "PUT", "/library/metadata/%s?tagline.value=%s"
                                                  % (movie, quote("unfiltered"))))
    check("an unfiltered channel gets everything EXCEPT log",
          frames and "log" not in {name for name, _row in frames},
          "kinds=%s" % {name for name, _row in frames})

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
    # A MOVIE's own setting. `episodeSort` belongs to a show, and writing a setting
    # the item's type does not have is a 400 (measured).
    status, body, _ = request(base, "PUT", "/library/metadata/%s/prefs?useOriginalTitle=1" % movie)
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

    print("\n=== the token gate, and the headers every response carries ===")
    # A real PMS 401s EVERYTHING but /identity and /web/* without its token -- for a
    # missing one and a wrong one alike, with no WWW-Authenticate and no difference
    # between them. plexsim had no token check at all, so any peer that could reach
    # the port read the whole library, the preferences, the sessions and the bytes.
    # The gate is opt-in (`--token`) because the suites and probes talk to plexsim
    # without one; this check starts its OWN gated instance to prove it works.
    import subprocess
    import sys as _sys
    gated_port = 32599
    gated = subprocess.Popen(
        [_sys.executable, "server.py", "--port", str(gated_port),
         "--catalog", "tests/catalog_ui.json", "--assert-claimed",
         "--token", "suite-gate-token"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=dict(os.environ, RD_TOKEN="dummy"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gated_base = "http://127.0.0.1:%d" % gated_port
    try:
        for _attempt in range(40):
            try:
                urllib.request.urlopen(gated_base + "/identity", timeout=2).read()
                break
            except Exception:
                time.sleep(0.25)

        def gated_status(path, token=None):
            req = urllib.request.Request(gated_base + path, headers={"User-Agent": UA})
            if token:
                req.add_header("X-Plex-Token", token)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.headers, error.read()

        open_status, _, _ = gated_status("/identity")
        web_status, _, _ = gated_status("/web/index.html")
        check("a gated server leaves /identity and /web open",
              open_status == 200 and web_status in (200, 404),
              "identity=%s web=%s" % (open_status, web_status))
        for path_ in ("/", "/library/sections", "/:/prefs", "/status/sessions",
                      "/media/providers", "/hubs"):
            missing, headers_, body_ = gated_status(path_)
            wrong, _, _ = gated_status(path_, "definitelynotatoken")
            good, _, _ = gated_status(path_, "suite-gate-token")
            check("gate: %s is 401 without a token and 200 with one" % path_,
                  missing == 401 and wrong == 401 and good == 200,
                  "none=%s bad=%s good=%s" % (missing, wrong, good))
        missing, headers_, body_ = gated_status("/library/sections")
        check("the 401 is the reference's HTML shell, no-cache, no challenge",
              b"401 Unauthorized" in body_
              and headers_.get("Content-Type") == "text/html"
              and headers_.get("Cache-Control") == "no-cache"
              and headers_.get("WWW-Authenticate") is None,
              "%s %s" % (headers_.get("Content-Type"), headers_.get("Cache-Control")))
        query_status, _, _ = gated_status("/library/sections?X-Plex-Token=suite-gate-token")
        check("the token is accepted in the query as well as the header",
              query_status == 200, "HTTP %s" % query_status)
        # /security/token is the one exception: its own 400 and 403, not a 401.
        bare, _, _ = gated_status("/security/token")
        forbidden, _, _ = gated_status("/security/token?type=delegation&scope=all", "nope")
        allowed, _, _ = gated_status("/security/token?type=delegation&scope=all",
                                     "suite-gate-token")
        check("a delegation token request answers 400, 403 and 200, not 401",
              bare == 400 and forbidden == 403 and allowed == 200,
              "%s / %s / %s" % (bare, forbidden, allowed))
    finally:
        gated.terminate()
        gated.wait(timeout=10)

    # Measured across eight endpoints on both reference servers: `Cache-Control:
    # no-cache` on EVERYTHING. plexsim sent none -- except on artwork, where it sent
    # a day of caching the reference does not, so a Change Artwork or a re-match
    # stayed invisible to the browser even though the version in the URL changed.
    for path_ in ("/library/sections", "/library/sections/%s/all" % movie_section,
                  "/identity", "/media/providers", "/:/prefs", "/status/sessions"):
        _, headers_, _ = request(base, "GET", path_)[:2] + ({},)
        with urllib.request.urlopen(urllib.request.Request(
                base.rstrip("/") + path_, headers={"User-Agent": UA}), timeout=10) as r:
            cache = r.headers.get("Cache-Control")
        check("%s says no-cache" % path_, cache == "no-cache", str(cache))
    # And a real PMS gzips its containers, naming both sizes in two Plex headers.
    squeezed = urllib.request.Request(
        base.rstrip("/") + "/library/sections/%s/all" % movie_section,
        headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(squeezed, timeout=10) as r:
        encoding = r.headers.get("Content-Encoding")
        original = r.headers.get("X-Plex-Content-Original-Length")
        compressed = r.headers.get("X-Plex-Content-Compressed-Length")
        payload = r.read()
    import gzip as _gzip
    check("a container is gzipped, with both sizes named",
          encoding == "gzip" and original and compressed
          and int(compressed) < int(original)
          and len(_gzip.decompress(payload)) == int(original),
          "%s -> %s bytes" % (original, compressed))

    print("\n=== artwork refusals, resources, and the paths a real PMS answers ===")
    # Measured: both dimensions are MANDATORY on /photo/:/transcode, a non-numeric one
    # is a 400, and a `url` naming a row the server does not have is a 404. plexsim
    # answered a 200 grey tile to all four, so a client that would have drawn its own
    # title-card placeholder drew -- and could cache -- plexsim's instead.
    art_url = quote("/library/metadata/%s/thumb/1" % movie)
    for spec, want, why in (
            ("?width=100&height=100", 400, "no url"),
            ("?url=%s" % art_url, 400, "neither dimension"),
            ("?url=%s&width=100" % art_url, 400, "width alone"),
            ("?url=%s&height=100" % art_url, 400, "height alone"),
            ("?url=%s&width=abc&height=100" % art_url, 400, "a non-numeric width"),
            ("?url=%s&width=200&height=300"
             % quote("/library/metadata/9999999/thumb/1"), 404, "an unknown row")):
        status, _, _ = container(base, "GET", "/photo/:/transcode%s" % spec)
        check("photo transcode refuses %s" % why, status == want, "HTTP %s" % status)
    status, _, headers_ = request(base, "GET", "/photo/:/transcode?url=%s"
                                              "&width=200&height=300" % art_url,
                                 want_json=False)
    check("a real transcode still answers",
          status == 200 and str(headers_.get("Content-Type", "")).startswith("image/"),
          "HTTP %s %s" % (status, headers_.get("Content-Type")))
    # A resource name a real PMS does not have is a 404; plexsim drew a tile for any.
    status_known, _, _ = request(base, "GET", "/:/resources/movie.png", want_json=False)
    status_unknown, _, _ = request(base, "GET", "/:/resources/nope.png", want_json=False)
    check("an unknown /:/resources name is a 404",
          status_known == 200 and status_unknown == 404,
          "%s / %s" % (status_known, status_unknown))
    # Measured on both reference servers: an unknown SINGLE-segment path falls into
    # the legacy plug-in dispatcher and answers 200 with `content="plugins"`, where a
    # multi-segment one 404s. `/prefs`, `/settings`, `/login` and `/logout` are all
    # named in the client bundle and were all 404 here.
    for path_ in ("/zzzznope", "/prefs", "/settings", "/login", "/logout"):
        status, body_, _ = get(base, path_)
        check("%s is the plug-in dispatcher's 200, not a 404" % path_,
              status == 200 and body_.get("content") == "plugins",
              "HTTP %s content=%r" % (status, body_.get("content")))
    for path_ in ("/foo/bar", "/a/b/c"):
        status, _, _ = container(base, "GET", path_)
        check("%s is still a 404" % path_, status == 404, "HTTP %s" % status)
    status, channels, _ = get(base, "/channels")
    check("/channels answers its two rows rather than 404ing",
          status == 200
          and [d.get("key") for d in channels.get("Directory") or []]
              == ["all", "recentlyViewed"],
          "HTTP %s %s" % (status, [d.get("key") for d in channels.get("Directory") or []]))
    # A real PMS routes per (method, path) and NOT uniformly: /identity and /hubs
    # answer any verb, these three refuse with their own statuses. plexsim answered
    # the GET body to every verb.
    for method, path_, want in (("POST", "/library/sections", 400),
                                ("PUT", "/library/sections", 404),
                                ("DELETE", "/library/sections", 404),
                                ("POST", "/status/sessions", 404),
                                ("PUT", "/status/sessions", 404),
                                ("DELETE", "/status/sessions", 404),
                                ("POST", "/media/providers", 400),
                                ("PUT", "/media/providers", 404),
                                ("DELETE", "/media/providers", 404),
                                ("POST", "/identity", 200),
                                ("PUT", "/hubs", 200)):
        status, _, _ = container(base, method, path_)
        check("%s %s is a %d" % (method, path_, want), status == want, "HTTP %s" % status)

    print("\n=== CORS, which a browser enforces before the server sees anything ===")
    # Measured either side of an Origin header: a real PMS ECHOES the origin and
    # echoes the requested header list on a preflight, and sends no CORS headers
    # at all without an Origin. plexsim answered `*` unconditionally and
    # published a FIXED header list that omitted `X-Plex-Session-Id`,
    # `X-Plex-Language` and others the client sends -- a preflight that omits one
    # makes the browser refuse the request with no server-side trace.
    wanted = "x-plex-token,x-plex-session-id,x-plex-language,accept"
    # Cycle 15 re-measured this on the copy: a BARE OPTIONS -- no
    # `Access-Control-Request-*` headers -- is ROUTED like any other verb and 404s
    # here; only a genuine preflight is answered. The old form sent no preflight
    # headers and asserted 200, so it was testing plexsim's own habit of answering
    # every OPTIONS as a preflight rather than anything a real PMS does.
    status, _, headers = request(base, "OPTIONS",
                                 "/library/metadata/%s/refresh" % movie, want_json=False)
    check("a bare OPTIONS is not a preflight and is routed", status == 404,
          "HTTP %s" % status)
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
            ("/library/shared/all", "Directory", "the shared library list"),
            ("/library/sections/%s/all" % movie_section, "Video", "a movie listing"),
            ("/playlists", "Playlist", "a playlist list")):
        status, root = xml_root(path)
        kids = {child.tag for child in (root or [])}
        check("%s renders as <%s>" % (why, wanted),
              root is not None and wanted in kids,
              "HTTP %s, got %s" % (status, ",".join(sorted(kids)) or "nothing"))
    # Re-measured in cycle 8: a folder's SUBDIRECTORIES render as <Directory> and its
    # FILES as the items they hold -- <Video> for Firefly's episodes under `?parent=`.
    # The cycle-4 check wanted <Directory> for every row, which holds only for a level
    # made of folders; plexsim's catalogs keep their files flat, so its top level is
    # the items themselves.
    status, root = xml_root("/library/sections/%s/folder" % movie_section)
    folder_rows = get(base, "/library/sections/%s/folder" % movie_section)[1].get("Metadata") or []
    wanted_tags = sorted("Video" if r.get("type") in ("movie", "episode") else "Directory"
                         for r in folder_rows)
    got_tags = sorted(child.tag for child in (root or []))
    check("a folder list renders folders as <Directory>, files as their items",
          root is not None and got_tags and got_tags == wanted_tags,
          "HTTP %s, got %s" % (status, ",".join(got_tags) or "nothing"))
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
        # Measured on two real servers: the codec belongs in the EXTENDED title for most
        # clients. Re-measured in cycle 10: this request says `X-Plex-Product: Plex Web`,
        # one of the thirteen products that get the codec in displayTitle too, so an
        # untitled video stream's two titles are the same string.
        check("a video stream's displayTitle carries the codec for Plex Web",
              "(" in (video[0].get("displayTitle") or "")
              and video[0].get("displayTitle") == video[0].get("extendedDisplayTitle"),
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
        # The stored value keeps its TYPE: a bool reads back as a boolean, not as
        # the "0"/"1" that was written.
        check("a preference write round-trips",
              is_empty_200(status, body) and now.get("value") is (flipped == "1"),
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
        # A library's bool is stored as the STRING "true" or "false" (measured).
        check("a section preference write round-trips",
              is_empty_200(status, body)
              and now.get("value") == ("true" if flipped == "1" else "false"),
              "%s=%s" % (flippable["id"], now.get("value")))

    status, agents, _ = get(base, "/system/agents")
    check("Agents page lists the agents plexsim identifies with",
          status == 200 and agents.get("Agent"),
          "%d agent(s)" % len(agents.get("Agent") or []))
    # The agent CHAIN the page edits lives one path deeper, and plexsim answered
    # the plain agent list to every /system/agents* path.
    status, chain, _ = get(base, "/system/agents/tv.plex.agents.movie/config/1")
    rows = chain.get("Agent") or []
    # Re-measured on both real servers: for a modern agent the chain is EMPTY.
    check("the agent chain for a media type is its own container",
          status == 200 and chain.get("identifier") == "com.plexapp.system"
          and chain.get("size") == 0 and not rows,
          "%d agent(s): %s" % (len(rows), [a.get("identifier") for a in rows]))
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

    print("\n=== the server-info document, and the refusals a real PMS makes ===")
    # `GET /` is NOT `/identity`: plexsim answered the five-field identity document
    # to both. A real PMS answers the whole capability blob plus 25 Directory rows
    # naming its top-level endpoints, and python-plexapi reads every attribute it
    # has from here.
    _, root, _ = get(base, "/")
    _, ident, _ = get(base, "/identity")
    dirs = [d.get("key") for d in root.get("Directory") or []]
    check("/ answers the server-info document, not /identity",
          len(dirs) == 25 and "library" in dirs and "butler" in dirs
          and root.get("machineIdentifier") and root.get("ownerFeatures")
          and len(ident) == 5 and "ownerFeatures" not in ident,
          "%d directory(ies), %d scalar(s) on / vs %d on /identity"
          % (len(dirs), len([k for k in root if not isinstance(root[k], list)]),
             len(ident)))
    check("every / Directory row is {count, key, title}",
          all(set(d) == {"count", "key", "title"} for d in root.get("Directory") or []),
          str((root.get("Directory") or [{}])[0]))
    # 23 scheduled tasks, and only BackupDatabase is titled -- plexsim listed two
    # with invented titles, so the page showed tasks that do not exist.
    status, body, _ = request(base, "GET", "/butler")
    butler = json.loads(body).get("ButlerTasks", {}) if body[:1] == b"{" else {}
    tasks = butler.get("ButlerTask") or []
    titled = [t.get("name") for t in tasks if t.get("title")]
    check("the butler lists the reference's 23 tasks",
          len(tasks) == 23 and titled == ["BackupDatabase"] and "size" not in butler,
          "%d task(s), titled: %s" % (len(tasks), titled))
    # The tree NESTS, and never sends an empty MediaItem list.
    if show:
        status, body, _ = request(base, "GET", "/library/metadata/%s/tree" % show)
        tree = json.loads(body).get("MediaContainer", {}) if body[:1] == b"{" else {}
        top = (tree.get("MetadataItem") or [{}])[0]
        seasons = top.get("MetadataItem") or []
        episodes = (seasons[0].get("MetadataItem") or []) if seasons else []
        check("a show's tree nests seasons and episodes and omits MediaItem",
              status == 200 and "MediaItem" not in top and seasons
              and episodes and (episodes[0].get("MediaItem") or [{}])[0].get("MediaPart"),
              "%d season(s), %d episode(s) under the first" % (len(seasons), len(episodes)))
        # A MediaItem carries only its id here.
        item = (episodes[0].get("MediaItem") or [{}])[0] if episodes else {}
        check("a tree MediaItem carries only its id",
              set(item) <= {"id", "MediaPart"}, ",".join(sorted(item)))
    else:
        skip("a show's tree nests seasons and episodes and omits MediaItem", "no show row")
    # Every refusal below was an empty 200 or an empty container on plexsim, each of
    # which a client reads as "that worked" or "that exists and is empty".
    for method, path_, want, why in (
            ("GET", "/updater/check?download=0", 404, "a GET of updater/check"),
            ("GET", "/updater/apply", 404, "a GET of updater/apply"),
            ("PUT", "/updater/apply", 400, "applying an update with nothing downloaded"),
            ("DELETE", "/activities/not-a-real-uuid", 404, "cancelling an unknown activity"),
            ("PUT", "/:/prefs?NoSuchPrefXyz=1", 400, "writing an unknown preference"),
            ("POST", "/:/prefs", 404, "a POST to the preferences"),
            ("GET", "/accounts/999", 404, "an unknown account"),
            ("GET", "/media/subscriptions/1", 404, "an unknown subscription"),
            ("GET", "/media/grabbers/foo", 404, "an unknown grabber"),
            ("GET", "/system/scanners", 404, "the bare scanners collection"),
            ("GET", "/security/token", 400, "a token request with no type or scope"),
            ("PUT", "/butler/CleanOldBundles", 404, "a PUT to a butler task")):
        status, _, _ = container(base, method, path_)
        check("refusal: %s is a %d" % (why, want), status == want, "HTTP %s" % status)
    # And the endpoints plexsim refused that a real PMS answers.
    for path_, why, test in (
            ("/log/networked", "networked logging", lambda s, b: s == 200),
            ("/diagnostics", "the diagnostics root",
             lambda s, b: s == 200 and b.get("content") == "plugins"),
            ("/resources", "the resource list", lambda s, b: s == 200 and b.get("size") == 0),
            ("/media/grabbers", "the grabber list",
             lambda s, b: s == 200 and len(b.get("MediaGrabber") or []) == 4),
            ("/system/library/sections", "the system library view",
             lambda s, b: s == 200 and b.get("identifier") == "com.plexapp.system")):
        status, body_, _ = get(base, path_)
        check("%s answers rather than 404s" % why, test(status, body_),
              "HTTP %s %s" % (status, sorted(k for k in body_
                                             if not isinstance(body_[k], list))))
    # A delegation token is per call, not a constant two devices would share.
    _, first_token, _ = get(base, "/security/token?type=delegation&scope=all")
    _, second_token, _ = get(base, "/security/token?type=delegation&scope=all")
    check("each delegation token is different",
          first_token.get("token") and first_token.get("token") != second_token.get("token"),
          "%s vs %s" % (first_token.get("token"), second_token.get("token")))

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
        ("?title=%s" % quote(word), total, "a bare = on a title matches a word's beginning"),
        ("?title=zzzznope", 0, "a title miss"),
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
    # RE-MEASURED in cycle 13 on zen, and this check had it wrong: an empty value on a
    # tag-TABLE field matches NOTHING, not the rows that have no value. 37 of 600 zen
    # movies carry no Genre and all 600 carry no Label, yet `genre=` and `label=` both
    # answer 0 rows there and on kids. The older reading -- "`genre=` returned none
    # because every row has a genre" -- was an inference from a five-movie library that
    # this data disproves. The rows-with-no-value rule is real, but it belongs to
    # `contentRating` and the language fields; `studio` and `editionTitle` are a third
    # case again and match every row. See the cycle-13 check further down.
    unlabelled = len([r for r in rows if not (r.get("Label") or [])])
    ungenred = len([r for r in rows if not (r.get("Genre") or [])])
    check("filter: an empty tag-table value matches nothing, however many rows have none",
          filtered("?label=") == 0 and filtered("?genre=") == 0
          and (unlabelled > 0 or ungenred > 0),
          "label= -> %d (%d rows have none), genre= -> %d (%d have none)"
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
        # A grid row's tags are text only (measured); the ids are on the detail row.
        row = (get(base, "/library/metadata/%s" % row.get("ratingKey"))[1]
               .get("Metadata") or [row])[0]
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
    # plexsim cannot convert, so a refused direct play is the measured "neither"
    # answer a real PMS gives a client with no conversion profile -- not the
    # 1001 "Conversion OK." of a server that has an encoder.
    check("a refused direct play answers 3000 with 2000 and 4005",
          refused.get("directPlayDecisionCode") == 3000
          and refused.get("generalDecisionCode") == 2000
          and refused.get("transcodeDecisionCode") == 4005
          and "mdeDecisionCode" not in refused
          and refused.get("generalDecisionText")
              == "Neither direct play nor conversion is available.",
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
                # Measured in cycle 12 against a real library: a LISTING row CAPS its tag
                # lists (2 Genre, 2 Writer, 2 Country, 3 Role), so membership has to be
                # read off the DETAIL row. This check used to compare listing rows with
                # listing rows, which agreed only because plexsim sent the whole list.
                def carries(key):
                    _, _, full = detail_of("x=1", key)
                    return any(t.get("tag") == entry["title"] for t in full.get(field) or [])
                ok = ok and all(carries(row.get("ratingKey")) for row in rows)
                expected = [m for m in present if carries(m.get("ratingKey"))]
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
    # A movie library advertises a different list from a show library -- the same
    # lesson as the two Meta sidecars. plexsim served one trimmed list for both, so
    # a show grid's filter bar could only talk about movies.
    #
    # Cycle 14 re-measured the COUNT: this check said 28, which is kids' answer
    # (1.43.4). zen -- 1.42.2, the build plexsim reports -- answers 22, and the six
    # it drops are the media-codec axes. plexsim was advertising a newer server's
    # filter bar than the version it claims to be. See docs/findings.md.
    _, movie_filters, _ = get(base, "/library/sections/%s/filters" % movie_section)
    movie_names = [e.get("filter") for e in movie_filters.get("Directory") or []]
    check("a movie library advertises its own version's 22 filters",
          len(movie_names) == 22 and movie_names[0] == "genre"
          and not {"hdr10plus", "atmos", "videoCodec", "audioCodec", "subtitleCodec",
                   "audioLayout"} & set(movie_names)
          and "hdr" in movie_names and "dovi" in movie_names
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
    # RE-MEASURED in cycle 13 on zen, the 1.42.2 build plexsim reports: this call sends
    # `pinnedContentDirectoryID`, and with it the third shelf is `home.movies.recent.N`,
    # not `movie.recentlyadded.N`. The bare call still answers the latter, and the check
    # below covers it. kids (1.43.4) ignores the parameter, which is how the pinned
    # shape went unnoticed for twelve cycles.
    _, bare_promoted, _ = get(base, "/hubs/promoted?contentDirectoryID=%s" % movie_section)
    bare_ids = [h.get("hubIdentifier") for h in bare_promoted.get("Hub") or []]
    check("promoted answers Continue Watching, On Deck and ONE library's shelf",
          identifiers == ["home.continue", "home.ondeck",
                          "home.movies.recent.%s" % movie_section]
          and bare_ids == ["home.continue", "home.ondeck",
                           "movie.recentlyadded.%s" % movie_section],
          "pinned %s | bare %s" % (", ".join(str(i) for i in identifiers),
                                   ", ".join(str(i) for i in bare_ids)))
    check("every promoted shelf carries key, context, style and promoted",
          all(h.get("key") and h.get("context") and h.get("style")
              and h.get("promoted") is True for h in hubs),
          ", ".join("%s/%s" % (h.get("context"), h.get("style")) for h in hubs))
    # Measured: the context drops a trailing section id -- it names the KIND of
    # shelf, which is why it is shared across libraries.
    # The pinned form of the shelf, for the reason given above.
    recent = next((h for h in hubs if "recent" in (h.get("hubIdentifier") or "")), {})
    check("a shelf's context drops the section id and hubKey names its rows",
          recent.get("context") == "hub.home.movies.recent"
          and (recent.get("hubKey") or "").startswith("/library/metadata/")
          and "," in (recent.get("hubKey") or ",")
          and recent.get("key") == ("/hubs/home/recentlyAdded?type=1"
                                    "&pinnedContentDirectoryID=%s" % movie_section),
          "%s | %s | %s" % (recent.get("context"), recent.get("hubKey"), recent.get("key")))
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
    # A row is only useful here if it SHARES a genre with another row -- an earlier
    # check in this suite rewrites one row's genres, so "has a genre" is not enough.
    genred = None
    for candidate in current.get("Metadata") or []:
        tags = {t.get("id") for t in candidate.get("Genre") or []}
        if not tags:
            continue
        if any(tags & {t.get("id") for t in other.get("Genre") or []}
               for other in current.get("Metadata") or []
               if other.get("ratingKey") != candidate.get("ratingKey")):
            genred = candidate.get("ratingKey")
            break
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
    # RE-MEASURED in cycle 13 on four queries that match NOTHING, on both servers:
    # `movie` is FIFTH and `collection` FOURTEENTH. The order plexsim had -- movie
    # first, collection seventh -- was never the base order; a query with fuzzy hits
    # promotes the hubs that scored, which is what an earlier reading picked up.
    check("search answers all seventeen typed hubs, empty ones included",
          len(hubs) == 17 and [h.get("hubIdentifier") for h in hubs][:5]
          == ["show", "artist", "album", "episode", "movie"]
          and second.get("title") == "Artists",
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
    _, played, _ = get(base, "/status/sessions/history/all?type=1")
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
        # RE-MEASURED in cycle 13 on zen, the 1.42.2 build plexsim reports: a movie
        # library carries SIX shelves and a show library SEVEN. kids (1.43.4) builds
        # neither `movie.topunwatched` nor `tv.startwatching`/`tv.toprated` although
        # its own rows qualify for all three, so the five were a version difference
        # read as a complete list.
        wanted_shelves = 7 if prefix == "tv" else 6
        check("section %s offers the shelves a 1.42.2 server builds" % section_id,
              status == 200 and len(shelves) == wanted_shelves
              and identifiers[0] == "%s.inprogress.%s" % (prefix, section_id)
              and identifiers[-1] == "%s.recentlyviewed.%s" % (prefix, section_id),
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

    print("\n=== order, and the state the sequence leaves behind ===")
    # Measured on both servers: the DEFAULT order of a listing is titleSort ascending,
    # not the catalog's own order, and the secondary key is ALWAYS titleSort whatever
    # the primary is. plexsim answered catalog order, so an edited Sort Title never
    # moved a row and the grid's default disagreed with its own sort control.
    _, defaulted, _ = get(base, "/library/sections/%s/all" % movie_section)
    _, by_title, _ = get(base, "/library/sections/%s/all?sort=titleSort" % movie_section)
    check("the default listing order is titleSort",
          [r.get("ratingKey") for r in defaulted.get("Metadata") or []]
          == [r.get("ratingKey") for r in by_title.get("Metadata") or []],
          ", ".join(str(r.get("titleSort") or r.get("title"))
                    for r in (defaulted.get("Metadata") or [])[:3]))
    # A key every row ties on must fall back to titleSort, not to arrival order.
    _, tied_titles, _ = get(base, "/library/sections/%s/all?sort=titleSort" % movie_section)
    # Whichever advertised key the rows all share -- the suite's own scrobbles and
    # edits move some of these, so the tying key is found rather than assumed.
    tie_key = next((key for key in ("audienceRating", "userRating", "duration",
                                    "viewCount", "mediaBitrate", "year")
                    if len({str(r.get(key)) for r in defaulted.get("Metadata") or []}) == 1
                    and len(defaulted.get("Metadata") or []) > 1), None)
    if tie_key:
        _, tied, _ = get(base, "/library/sections/%s/all?sort=%s" % (movie_section, tie_key))
        check("a tie falls back to titleSort",
              [r.get("ratingKey") for r in tied.get("Metadata") or []]
              == [r.get("ratingKey") for r in tied_titles.get("Metadata") or []],
              "every row ties on %s" % tie_key)
    else:
        skip("a tie falls back to titleSort", "no key the rows all share")
    # The advanced Meta: the client sends includeAdvanced=1 and reads the Sort list
    # straight into its sort menu. plexsim's Meta was identical with and without it.
    _, plain_meta, _ = get(base, "/library/sections/%s/all?includeMeta=1"
                                "&X-Plex-Container-Start=0&X-Plex-Container-Size=0"
                                % movie_section)
    _, adv_meta, _ = get(base, "/library/sections/%s/all?includeMeta=1&includeAdvanced=1"
                               "&X-Plex-Container-Start=0&X-Plex-Container-Size=0"
                               % movie_section)
    plain_type = ((plain_meta.get("Meta") or {}).get("Type") or [{}])[0]
    adv_type = ((adv_meta.get("Meta") or {}).get("Type") or [{}])[0]
    added_sorts = [s.get("key") for s in adv_type.get("Sort") or []
                   if s.get("key") not in {x.get("key") for x in plain_type.get("Sort") or []}]
    check("includeAdvanced adds the reference's six movie sorts and Duplicates",
          added_sorts == ["year", "userRating", "contentRating", "viewOffset",
                          "viewCount", "mediaBitrate"]
          and any(f.get("advanced") and f.get("filter") == "duplicate"
                  for f in adv_type.get("Filter") or []),
          ", ".join(added_sorts))
    # And every key it advertises has to WORK -- three of them were 500s.
    for key in added_sorts:
        status, _, _ = container(base, "GET", "/library/sections/%s/all?sort=%s"
                                             % (movie_section, key))
        check("the advertised sort %s is answered" % key, status == 200,
              "HTTP %s" % status)
    # The in-library search box is a SUBSTRING matcher, not the word-prefix one
    # /hubs/search uses -- and one character is enough.
    _, sample_rows, _ = get(base, "/library/sections/%s/all" % movie_section)
    a_title = str((sample_rows.get("Metadata") or [{}])[0].get("title") or "")
    if len(a_title) > 3:
        fragment = a_title[1:4]
        _, in_library, _ = get(base, "/library/sections/%s/search?type=1&query=%s"
                                    % (movie_section, quote(fragment)))
        _, hub_search, _ = get(base, "/hubs/search?query=%s" % quote(fragment))
        hub_hits = sum(h.get("size") or 0 for h in hub_search.get("Hub") or [])
        check("the in-library search box matches a mid-word fragment",
              (in_library.get("size") or 0) > 0 and hub_hits == 0,
              "%r -> %s in-library, %s in the hub search"
              % (fragment, in_library.get("size"), hub_hits))
    else:
        skip("the in-library search box matches a mid-word fragment", "title too short")
    # Removing a collection member: the client builds this exact DELETE and assigns
    # the response onto its model. plexsim had no DELETE branch, so the tile
    # disappeared optimistically and came back on the next fetch.
    _, made, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s"
                                        "&title=%s&uri=%s"
                                        % (movie_section, quote("plexsim member probe"),
                                           quote("server://%s/com.plexapp.plugins.library"
                                                 "/library/metadata/%s" % (machine, movie))))
    ckey = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if ckey:
        status, answer, _ = container(base, "DELETE", "/library/collections/%s/items/%s"
                                                     % (ckey, movie))
        _, left, _ = get(base, "/library/collections/%s/children" % ckey)
        row = (answer.get("Metadata") or [{}])[0]
        check("removing a collection member answers the collection row",
              status == 200 and str(row.get("ratingKey")) == str(ckey)
              and (left.get("size") or 0) == 0,
              "childCount=%s, %s child(ren) left" % (row.get("childCount"),
                                                     left.get("size")))
        status_again, _, _ = container(base, "DELETE", "/library/collections/%s/items/%s"
                                                      % (ckey, movie))
        check("removing it twice is idempotent", status_again == 200,
              "HTTP %s" % status_again)
        request(base, "DELETE", "/library/collections/%s" % ckey)
    else:
        skip("removing a collection member answers the collection row",
             "could not create a collection")
    # /library/onDeck is the server-wide Continue Watching list, not the first twelve
    # catalog rows -- which is what every non-web client's On Deck reads.
    _, deck, _ = get(base, "/library/onDeck")
    _, hub_deck, _ = get(base, "/hubs/continueWatching/items")
    check("/library/onDeck is the resume list, and every row has an offset",
          all(r.get("viewOffset") for r in deck.get("Metadata") or [])
          and (deck.get("size") or 0) == (hub_deck.get("size") or 0),
          "%s row(s), hub says %s" % (deck.get("size"), hub_deck.get("size")))
    # A monotonic historyKey: a delete must not make the next play reuse the key.
    _, current_history, _ = get(base, "/status/sessions/history/all?type=1")
    keys = [str(r.get("historyKey") or "") for r in current_history.get("Metadata") or []]
    if keys:
        last = keys[-1].rsplit("/", 1)[-1]
        container(base, "DELETE", "/status/sessions/history/%s" % last)
        request(base, "GET", "/:/timeline?ratingKey=%s&state=playing&time=100000"
                             "&duration=100000" % movie)
        request(base, "GET", "/:/timeline?ratingKey=%s&state=stopped&time=100000"
                             "&duration=100000" % movie)
        _, after_history, _ = get(base, "/status/sessions/history/all?type=1")
        fresh = [str(r.get("historyKey") or "").rsplit("/", 1)[-1]
                 for r in after_history.get("Metadata") or []]
        check("a historyKey is never reused after a delete",
              last not in fresh, "deleted %s, now %s" % (last, fresh))
    else:
        skip("a historyKey is never reused after a delete", "no history rows")

    print("\n=== listing container rules ===")
    _, paged, _ = get(base, "/library/sections/%s/all?X-Plex-Container-Start=0"
                            "&X-Plex-Container-Size=1" % movie_section)
    _, unpaged, _ = get(base, "/library/sections/%s/all" % movie_section)
    check("a paged listing carries offset and totalSize",
          paged.get("offset") == 0 and paged.get("totalSize") == len(movies),
          "offset=%s totalSize=%s" % (paged.get("offset"), paged.get("totalSize")))
    check("an unpaged listing carries neither offset nor totalSize",
          "offset" not in unpaged and "totalSize" not in unpaged,
          ",".join(sorted(k for k in unpaged if not isinstance(unpaged[k], list))))
    # Re-measured on both servers: `nocache` on a section listing tracks the library
    # TYPE, not paging -- a show library carries it paged AND unpaged, a movie library
    # never does. The earlier reading had it the other way round, so the show grid the
    # client always pages never carried it.
    if show_section:
        _, show_paged, _ = get(base, "/library/sections/%s/all?type=2"
                                    "&X-Plex-Container-Start=0&X-Plex-Container-Size=1"
                                    % show_section)
        _, show_unpaged, _ = get(base, "/library/sections/%s/all?type=2" % show_section)
        check("nocache tracks the library type, not the paging",
              show_paged.get("nocache") is True and show_unpaged.get("nocache") is True
              and "nocache" not in unpaged and "nocache" not in paged,
              "show paged=%s unpaged=%s movie unpaged=%s"
              % (show_paged.get("nocache"), show_unpaged.get("nocache"),
                 unpaged.get("nocache")))
    else:
        skip("nocache tracks the library type, not the paging", "no show library")
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
    # Measured: a real PMS validates the create form and answers a PLAIN-TEXT 400
    # naming what is wrong. plexsim answered 201 to every malformed form and really
    # created the library -- a POST with NO parameters at all made one called
    # "New Library", which then sat in the sidebar.
    for spec, why in (
            ("", "no parameters at all"),
            ("?name=x&type=movie&agent=tv.plex.agents.movie&scanner=Plex%20Movie",
             "no location"),
            ("?name=x&type=bogus&location=%2Fx&agent=tv.plex.agents.movie"
             "&scanner=Plex%20Movie", "an unknown type"),
            ("?type=movie&location=%2Fx&agent=tv.plex.agents.movie"
             "&scanner=Plex%20Movie", "no name"),
            ("?name=x&type=movie&location=%2Fx&scanner=Plex%20Movie", "no agent"),
            ("?name=x&type=movie&location=%2Fx&agent=tv.plex.agents.movie",
             "no scanner")):
        status_bad, _, _ = container(base, "POST", "/library/sections%s" % spec)
        check("Add Library refuses %s" % why, status_bad == 400, "HTTP %s" % status_bad)
    new_section = directory.get("key")
    if new_section:
        # The dialog sends the WHOLE form, and a real PMS validates it: a `name`
        # on its own is a 400, and so are a missing or unknown agent, scanner or
        # language. plexsim accepted every one with a 200, so a typo looked saved.
        form = ("&agent=tv.plex.agents.movie&scanner=Plex%%20Movie&language=en-US"
                "&location=%s" % quote("/plexsim"))
        status, body, _ = request(base, "PUT", "/library/sections/%s?name=%s%s"
                                               % (new_section, quote("plexsim renamed"),
                                                  form))
        _, sections, _ = get(base, "/library/sections")
        titles = [d.get("title") for d in sections.get("Directory") or []]
        check("Edit Library renames it",
              is_empty_200(status, body) and "plexsim renamed" in titles,
              ",".join(titles))
        for spec, why in (
                ("", "no name at all"),
                ("?name=x", "a name with no agent or scanner"),
                ("?name=x&agent=bogus&scanner=Plex%20Movie", "an unknown agent"),
                ("?name=x&agent=tv.plex.agents.movie&scanner=Nope", "an unknown scanner"),
                ("?name=x&agent=tv.plex.agents.movie&scanner=Plex%20Movie"
                 "&language=xx-XX", "an unknown language")):
            status, _, _ = container(base, "PUT",
                                     "/library/sections/%s%s" % (new_section, spec))
            check("Edit Library refuses %s" % why, status == 400, "HTTP %s" % status)
        # And the scan is a GET or a POST -- a PUT to it is a 404.
        status, _, _ = container(base, "PUT", "/library/sections/%s/refresh" % new_section)
        check("a PUT to a section refresh is a 404", status == 404, "HTTP %s" % status)
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
        # Measured on a writable copy of the reference: the split hands the second item back
        # on a NEW ratingKey, so every later check meaning "the second movie" uses that key
        # rather than the one the merge absorbed.
        spare = next((r.get("ratingKey") for r in listing.get("Metadata") or []
                      if str(r.get("ratingKey")) not in {str(m.get("ratingKey")) for m in movies}), spare)

    # ------------------------------------------------------------------
    # Cycle 5: the settings forms, agents, devices, the wire format, the hubs
    # and the row shapes -- every check here was measured against the reference
    # and fails against the server as it stood before.
    # ------------------------------------------------------------------
    print("\n=== Settings forms, agents, wire format and row shapes ===")

    def raw(path, method="GET", headers=None):
        wanted = {"User-Agent": UA}
        wanted.update(headers or {})
        req = urllib.request.Request(base.rstrip("/") + path, method=method, headers=wanted)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def empties(node, where=""):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if value is None or value == []:
                    found.append(where + "." + key)
                else:
                    found += empties(value, where + "." + key)
        elif isinstance(node, list):
            for value in node:
                found += empties(value, where)
        return found

    libraries = get(base, "/library/sections")[1].get("Directory") or []
    movie_lib = next((d["key"] for d in libraries if d.get("type") == "movie"), None)
    show_lib = next((d["key"] for d in libraries if d.get("type") == "show"), None)
    grid = get(base, "/library/sections/%s/all" % movie_lib)[1].get("Metadata") or []
    c5_movie = grid[0]["ratingKey"] if grid else None
    c5_show = c5_season = c5_episode = None
    if show_lib:
        shows = get(base, "/library/sections/%s/all" % show_lib)[1].get("Metadata") or []
        c5_show = shows[0]["ratingKey"] if shows else None
    if c5_show:
        seasons = get(base, "/library/metadata/%s/children" % c5_show)[1].get("Metadata") or []
        c5_season = seasons[0]["ratingKey"] if seasons else None
    if c5_season:
        episodes = get(base, "/library/metadata/%s/children" % c5_season)[1].get("Metadata") or []
        c5_episode = episodes[0]["ratingKey"] if episodes else None
    # Read afresh: `identity` is reused for another value further up `main`.
    machine_id = get(base, "/identity")[1].get("machineIdentifier")

    # -- the Add Library and Edit Library forms --------------------------
    status, form, _ = get(base, "/library/sections/prefs?type=1")
    ids = [s.get("id") for s in form.get("Setting") or []]
    check("the Add Library form answers for a movie library",
          status == 200 and len(ids) == 7 and "enableCinemaTrailers" in ids,
          "HTTP %s, %d setting(s)" % (status, len(ids)))
    status, form, _ = get(base, "/library/sections/prefs?type=2&agent=tv.plex.agents.series")
    check("the Add Library form is shaped by the agent",
          status == 200 and len(form.get("Setting") or []) == 21
          and "showOrdering" in [s.get("id") for s in form.get("Setting") or []],
          "HTTP %s, %d setting(s)" % (status, len(form.get("Setting") or [])))
    status, _, _ = get(base, "/library/sections/prefs")
    check("the Add Library form needs a type", status == 400, "HTTP %s" % status)
    status, form, _ = get(base, "/library/sections/%s/prefs?agent=tv.plex.agents.none"
                          % movie_lib)
    check("an existing library's form is reshaped by ?agent=",
          status == 200 and len(form.get("Setting") or []) == 12,
          "%d setting(s)" % len(form.get("Setting") or []))

    # -- writes to the three kinds of settings ---------------------------
    prefs_now = {s.get("id"): s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []}
    original_deck = prefs_now.get("OnDeckLimit")
    request(base, "PUT", "/:/prefs?OnDeckLimit=41")
    after = {s.get("id"): s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []}
    check("an int server setting keeps its type after a write",
          after.get("OnDeckLimit") == 41 and not isinstance(after.get("OnDeckLimit"), bool),
          repr(after.get("OnDeckLimit")))
    status, _, _ = request(base, "PUT", "/:/prefs?OnDeckLimit=abc")
    after = {s.get("id"): s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []}
    check("a non-numeric int setting is a 500 and changes nothing",
          status == 500 and after.get("OnDeckLimit") == 41,
          "HTTP %s, now %r" % (status, after.get("OnDeckLimit")))
    status, _, _ = request(base, "PUT", "/:/prefs?OnDeckLimit=42&NoSuchPref=1")
    after = {s.get("id"): s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []}
    check("an unknown server setting refuses the whole write",
          status == 400 and after.get("OnDeckLimit") == 41,
          "HTTP %s, now %r" % (status, after.get("OnDeckLimit")))
    request(base, "PUT", "/:/prefs?OnDeckLimit=%s" % original_deck)
    original_low = prefs_now.get("ScannerLowPriority")
    request(base, "PUT", "/:/prefs?ScannerLowPriority=abc")
    after = {s.get("id"): s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []}
    check("a bool server setting is false for anything but 1 or true",
          after.get("ScannerLowPriority") is False, repr(after.get("ScannerLowPriority")))
    request(base, "PUT", "/:/prefs?ScannerLowPriority=%s"
            % ("1" if original_low in (True, "1", "true") else "0"))
    check("the server settings carry no GPU the server does not have",
          "TranscoderHEVCOptimize" not in prefs_now and "TranscoderHEVCEncodingMode" not in prefs_now,
          "%d setting(s)" % len(prefs_now))

    library_prefs = {s.get("id"): s.get("value") for s in
                     get(base, "/library/sections/%s/prefs" % movie_lib)[1].get("Setting") or []}
    status, _, _ = request(base, "PUT", "/library/sections/%s/prefs?hidden=1&NoSuchPref=1"
                           % movie_lib)
    now = {s.get("id"): s.get("value") for s in
           get(base, "/library/sections/%s/prefs" % movie_lib)[1].get("Setting") or []}
    check("an unknown library setting refuses the whole write",
          status == 400 and now.get("hidden") == library_prefs.get("hidden"),
          "HTTP %s, hidden=%r" % (status, now.get("hidden")))
    status, _, _ = request(base, "PUT", "/library/sections/%s/prefs?hidden=abc" % movie_lib)
    now = {s.get("id"): s.get("value") for s in
           get(base, "/library/sections/%s/prefs" % movie_lib)[1].get("Setting") or []}
    check("a non-integer library setting is ignored",
          status == 200 and now.get("hidden") == library_prefs.get("hidden"),
          "HTTP %s, hidden=%r" % (status, now.get("hidden")))
    request(base, "PUT", "/library/sections/%s/prefs?enableCinemaTrailers=yes" % movie_lib)
    now = {s.get("id"): s.get("value") for s in
           get(base, "/library/sections/%s/prefs" % movie_lib)[1].get("Setting") or []}
    check("a bool library setting stores \"false\" for anything but 1 or true",
          now.get("enableCinemaTrailers") == "false", repr(now.get("enableCinemaTrailers")))
    request(base, "PUT", "/library/sections/%s/prefs?enableCinemaTrailers=%s"
            % (movie_lib, library_prefs.get("enableCinemaTrailers")))

    if c5_show:
        status, _, _ = request(base, "PUT", "/library/metadata/%s/prefs?NoSuchPref=1" % c5_show)
        row = (get(base, "/library/metadata/%s" % c5_show)[1].get("Metadata") or [{}])[0]
        check("an item setting its type does not have is a 400 and stores nothing",
              status == 400 and "NoSuchPref" not in row and "_pairs" not in row,
              "HTTP %s" % status)
        request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=abc" % c5_show)
        row = (get(base, "/library/metadata/%s?includePreferences=1" % c5_show)[1]
               .get("Metadata") or [{}])[0]
        setting = next((s for s in (row.get("Preferences") or {}).get("Setting") or []
                        if s.get("id") == "flattenSeasons"), {})
        check("an item setting that does not parse shows on the row, not in the setting",
              row.get("flattenSeasons") == "abc" and setting.get("value") == "-1",
              "row=%r setting=%r" % (row.get("flattenSeasons"), setting.get("value")))
        request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=" % c5_show)
        row = (get(base, "/library/metadata/%s" % c5_show)[1].get("Metadata") or [{}])[0]
        check("an empty item setting clears it", "flattenSeasons" not in row,
              repr(row.get("flattenSeasons")))
    if c5_episode:
        status, _, _ = request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=1"
                               % c5_episode)
        check("an episode has no settings to write", status == 400, "HTTP %s" % status)

    # -- refusals and small endpoints ------------------------------------
    status, _, _ = get(base, "/library/metadata/%s/refresh" % c5_movie)
    check("a wrong verb on an item action is a 404, never a 405", status == 404,
          "HTTP %s" % status)
    status, agents, _ = get(base, "/system/agents?mediaType=1")
    agent_rows = agents.get("Agent") or []
    movie_agent = next((a for a in agent_rows if a.get("identifier") == "tv.plex.agents.movie"), {})
    check("a movie library is offered its three modern agents",
          [a.get("identifier") for a in agent_rows]
          == ["tv.plex.agents.movie", "tv.plex.agents.none", "tv.plex.agents.nfo.movie"],
          ",".join(str(a.get("identifier")) for a in agent_rows))
    check("an agent's languages carry the default the dialog preselects",
          len(movie_agent.get("Language") or []) > 20
          and [l.get("code") for l in movie_agent.get("Language") or [] if l.get("default")]
          == ["en-US"], "%d language(s)" % len(movie_agent.get("Language") or []))
    status, _, _ = get(base, "/system/agents?mediaType=abc")
    check("a non-numeric agent media type is a 400", status == 400, "HTTP %s" % status)
    statuses = [get(base, path)[0] for path in (
        "/system/agents/tv.plex.agents.movie/config/99",
        "/system/agents/attribution?identifier=tv.plex.agents.movie",
        "/system/agents/attribution/image?identifier=tv.plex.agents.movie",
        "/system/agents/contributors",
        "/:/plugins/tv.plex.agents.movie/prefs")]
    check("agent config, attribution and plug-in prefs refuse like a real PMS",
          statuses == [404, 404, 404, 500, 404], str(statuses))
    status, chain, _ = get(base, "/system/agents/tv.plex.agents.movie/config/1")
    check("a modern agent's contributor chain is an empty container",
          status == 200 and chain.get("size") == 0 and not chain.get("Agent"),
          "HTTP %s size=%s" % (status, chain.get("size")))
    status, scanners, _ = get(base, "/system/scanners/13")
    status_bad, _, _ = get(base, "/system/scanners/99")
    check("scanners are listed per library TYPE",
          status == 200 and [s.get("name") for s in scanners.get("Scanner") or []]
          == ["Plex Photo Scanner"] and status_bad == 500,
          "HTTP %s / %s" % (status, status_bad))
    status, near, _ = get(base, "/library/metadata/%s/nearest" % c5_movie)
    check("nearest answers an empty library container", status == 200 and near.get("size") == 0,
          "HTTP %s" % status)
    status, channels, _ = get(base, "/channels/all")
    check("/channels/all answers the plug-in container",
          status == 200 and channels.get("content") == "plugins", "HTTP %s" % status)
    status, _, _ = request(base, "GET", "/log")
    check("a log line with no level or message is a 400", status == 400, "HTTP %s" % status)
    status, paths, _ = get(base, "/services/browse")
    status_plain, _, _ = get(base, "/services/browse/%s" % quote("L3BsZXhzaW0="))
    status_files, children, _ = get(base, "/services/browse/L3BsZXhzaW0=?includeFiles=1")
    check("the folder picker walks a filesystem",
          status == 200 and (paths.get("Path") or [{}])[0].get("path") == "/"
          and status_plain == 400 and status_files == 200 and children.get("Path"),
          "HTTP %s / %s / %s" % (status, status_plain, status_files))
    status, tags, _ = get(base, "/library/tags?type=1")
    status_bare, _, _ = get(base, "/library/tags")
    check("the tag table lists a type's tags with their filters",
          status == 200 and tags.get("Directory")
          and all(str(t.get("filter", "")).startswith("genre=") and t.get("tagType") == 1
                  for t in tags.get("Directory") or [])
          and status_bare == 400,
          "HTTP %s, %d tag(s), bare %s" % (status, len(tags.get("Directory") or []), status_bare))

    # -- devices, play attribution and view counts -----------------------
    player = {"X-Plex-Client-Identifier": "plexsim-c5-player", "X-Plex-Device-Name": "C5 Player",
              "X-Plex-Platform": "c5platform", "Accept": "application/json"}
    duration = int((get(base, "/library/metadata/%s" % c5_movie)[1].get("Metadata") or [{}])[0]
                   .get("duration") or 600000)
    # Re-measured in cycle 10: a play completes only when a beat CROSSES 90% inside its own
    # session, so this play starts half way -- two beats in the last tenth complete nothing,
    # on a real PMS or here, and the row this check reads would be an older play's by
    # another device.
    for state, offset in (("playing", duration // 2), ("playing", duration - 5000),
                          ("stopped", duration)):
        raw("/:/timeline?ratingKey=%s&key=%s&state=%s&time=%d&duration=%d"
            % (c5_movie, quote("/library/metadata/%s" % c5_movie), state, offset, duration),
            headers=player)
    devices = get(base, "/devices")[1].get("Device") or []
    seen = next((d for d in devices if d.get("clientIdentifier") == "plexsim-c5-player"), {})
    check("/devices lists every client with its name and platform",
          seen.get("name") == "C5 Player" and seen.get("platform") == "c5platform",
          "%d device(s)" % len(devices))
    status_one, one, _ = get(base, "/devices/%s" % seen.get("id"))
    status_bad, _, _ = get(base, "/devices/abc")
    check("one device reads by id, and an unknown id is a 404",
          status_one == 200 and (one.get("Device") or [{}])[0].get("id") == seen.get("id")
          and status_bad == 404, "HTTP %s / %s" % (status_one, status_bad))
    history = get(base, "/status/sessions/history/all?type=1&sort=viewedAt:desc")[1].get("Metadata") or []
    # The NEWEST row by historyKey: the suite played this movie earlier, and two
    # plays in the same second tie on viewedAt.
    played = sorted((h for h in history if str(h.get("ratingKey")) == str(c5_movie)),
                    key=lambda h: int(str(h.get("historyKey") or "0").rsplit("/", 1)[-1]))
    check("a completed play is attributed to the device that played it",
          played and played[-1].get("deviceID") == seen.get("id"),
          "deviceID=%s device=%s" % (played[-1].get("deviceID") if played else None,
                                     seen.get("id")))
    status, top, _ = get(base, "/library/metadata/%s/users/top" % c5_movie)
    check("an item's top users count its completed views",
          status == 200 and (top.get("Account") or [{}])[0].get("globalViewCount", 0) >= 1,
          "HTTP %s %s" % (status, top.get("Account")))
    if c5_season:
        status, _, _ = get(base, "/library/metadata/%s/users/top" % c5_season)
        check("a season has no top users: a 500", status == 500, "HTTP %s" % status)
    stats = get(base, "/statistics/bandwidth")[1].get("StatisticsBandwidth") or []
    check("statistics with no timespan answer every rollup",
          {row.get("timespan") for row in stats} == {0, 1, 2, 3, 4},
          str(sorted({row.get("timespan") for row in stats})))

    # -- the wire format -------------------------------------------------
    formats = []
    for accept in ("application/xml, application/json", "application/json; charset=utf-8",
                   "Application/JSON", "application/json, text/plain, */*"):
        _, body, _ = raw("/library/sections", headers={"Accept": accept})
        formats.append("json" if body[:1] == b"{" else "xml")
    check("Accept is read the way a real PMS reads it",
          formats == ["xml", "xml", "xml", "json"], ",".join(formats))
    _, detail, _ = get(base, "/library/metadata/%s" % c5_movie)
    _, hubs_doc, _ = get(base, "/hubs")
    check("JSON carries no null and no empty list",
          not empties(detail) and not empties(hubs_doc),
          ",".join((empties(detail) + empties(hubs_doc))[:4]))
    _, xml_body, _ = request(base, "GET", "/library/metadata/%s" % c5_movie, want_json=False)
    rates = re.findall(r'frameRate="([^"]*)"', xml_body.decode("utf-8", "replace"))
    check("a stream's frameRate is written with three decimals",
          rates and all(re.match(r"^\d+\.\d{3}$", r) for r in rates), str(rates[:2]))
    media = ((detail.get("Metadata") or [{}])[0].get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]
    video = next((s for s in part.get("Stream") or [] if s.get("streamType") == 1), {})
    check("a non-mp4 Media carries no streaming flags but does carry hasVoiceActivity",
          (media.get("container") in ("mp4", "mov", "m4v")
           or ("optimizedForStreaming" not in media and "has64bitOffsets" not in media))
          and media.get("hasVoiceActivity") is False and "hasThumbnail" not in part
          and isinstance(video.get("bitDepth", 8), int),
          "container=%s" % media.get("container"))

    # -- row shapes ------------------------------------------------------
    row = (detail.get("Metadata") or [{}])[0]
    check("a movie row has no filename originalTitle, no default lock and no Location",
          not str(row.get("originalTitle", "")).lower().endswith((".mkv", ".mp4", ".webm", ".avi"))
          and row.get("Field") != [{"locked": True, "name": "thumb"}]
          and "Location" not in row and "rating" not in row,
          ",".join(k for k in ("originalTitle", "Field", "Location", "rating") if k in row))
    everything = []
    for library in (movie_lib, show_lib):
        if library:
            everything += get(base, "/library/sections/%s/all" % library)[1].get("Metadata") or []
    wrong = [r.get("title") for r in everything
             if ("titleSort" in r and r["titleSort"] == r.get("title"))
             or (str(r.get("title", "")).startswith("The ") and r.get("titleSort") != r["title"][4:])]
    check("titleSort is the article-stripped title, sent only when it differs",
          not wrong, ",".join(str(w) for w in wrong[:3]))
    if c5_season:
        season_row = (get(base, "/library/metadata/%s" % c5_season)[1].get("Metadata") or [{}])[0]
        # Re-measured in cycle 7: a season with a year of its OWN carries it, with its
        # title as titleSort and no parentYear; only a season without one falls back.
        check("a season row carries no skipCount or theme, and a year only with its titleSort",
              not any(k in season_row for k in ("skipCount", "theme"))
              and ("year" in season_row) == (season_row.get("titleSort") == season_row.get("title"))
              and not ("year" in season_row and "parentYear" in season_row),
              ",".join(k for k in ("year", "skipCount", "theme", "titleSort", "parentYear")
                       if k in season_row))
        _, episodes_doc, _ = get(base, "/library/metadata/%s/children" % c5_season)
        show_title = (get(base, "/library/metadata/%s" % c5_show)[1].get("Metadata") or [{}])[0].get("title")
        # Re-measured in cycle 8 on both servers: the container DOES carry `parentYear`
        # when the season has a year of its own, and it is that year. This check wanted
        # it absent, which had been measured on seasons without a year.
        check("an episode list's container is presented as the show's",
              episodes_doc.get("title1") == show_title and episodes_doc.get("parentTitle") == ""
              and isinstance(episodes_doc.get("grandparentRatingKey"), int)
              and not any(k in episodes_doc for k in ("summary", "grandparentYear"))
              and episodes_doc.get("parentYear") == season_row.get("year"),
              "title1=%r parentTitle=%r" % (episodes_doc.get("title1"), episodes_doc.get("parentTitle")))
    if c5_episode:
        episode_row = (get(base, "/library/metadata/%s" % c5_episode)[1].get("Metadata") or [{}])[0]
        # And no grandparentTheme either: plexsim has no theme audio, and a real show
        # without a theme names none on itself or on its children (measured, cycle 7).
        check("an episode row carries no theme of its own", "theme" not in episode_row
              and "grandparentTheme" not in episode_row, str(episode_row.get("theme")))
    grid = get(base, "/library/sections/%s/all" % movie_lib)[1].get("Metadata") or []
    heavy = [r.get("title") for r in grid
             if any(set(t) != {"tag"} for key in ("Genre", "Country", "Director", "Writer", "Role")
                    for t in r.get(key) or [])
             or any("Stream" in p for m in r.get("Media") or [] for p in m.get("Part") or [])
             or any(k in r for k in ("Location", "Rating", "Field", "librarySectionID"))]
    check("a grid row is the light projection: text tags, no streams, no section fields",
          grid and not heavy, ",".join(str(h) for h in heavy[:3]))
    _, tree, _ = get(base, "/library/metadata/%s/tree" % c5_movie)
    tree_part = (((tree.get("MetadataItem") or [{}])[0].get("MediaItem") or [{}])[0]
                 .get("MediaPart") or [{}])[0]
    tree_video = next((s for s in tree_part.get("MediaStream") or [] if s.get("type") == 1), {})
    check("the tree's MediaStream numbers are strings and its part has no container",
          isinstance(tree_video.get("bitDepth"), str) and "container" not in tree_part
          and "bitrate" not in tree_video, repr(tree_video.get("bitDepth")))
    check("a library row carries no composite",
          libraries and not any("composite" in d for d in libraries),
          ",".join(str(d.get("key")) for d in libraries if "composite" in d))
    _, deck, _ = get(base, "/library/onDeck")
    _, recent, _ = get(base, "/library/recentlyAdded")
    check("the server-wide onDeck and recentlyAdded lists are mixedParents",
          deck.get("mixedParents") is True and recent.get("mixedParents") is True,
          "%s/%s" % (deck.get("mixedParents"), recent.get("mixedParents")))
    _, root, _ = get(base, "/")
    check("GET / carries the five capability flags /media/providers does not",
          all(root.get(flag) is True for flag in
              ("hubSearch", "itemClusters", "mediaProviders", "multiuser", "transcoderPhoto")),
          ",".join(f for f in ("hubSearch", "itemClusters", "mediaProviders", "multiuser",
                               "transcoderPhoto") if root.get(f) is not True))

    # -- hubs, related, common, sorts ------------------------------------
    global_hubs = hubs_doc.get("Hub") or []
    check("the global hubs are the eight measured shelves, in order",
          [h.get("hubIdentifier") for h in global_hubs]
          == ["home.continue", "home.ondeck", "home.movies.recent", "home.television.recent",
              "home.music.recent", "home.photos.recent", "home.videos.recent", "home.playlists"]
          and all(h.get("key") and h.get("context") and h.get("style") and h.get("promoted")
                  for h in global_hubs),
          ",".join(str(h.get("hubIdentifier")) for h in global_hubs))
    _, library_hubs, _ = get(base, "/hubs/sections/%s" % movie_lib)
    shelves = library_hubs.get("Hub") or []
    released = next((h for h in shelves if str(h.get("hubIdentifier", "")).startswith(
        "movie.recentlyreleased")), {})
    check("a library shelf names its own rows in hubKey and carries no section fields",
          shelves and all(("hubKey" in h) == bool(h.get("size")) for h in shelves)
          and all(str(h.get("hubKey", "/library/metadata/")).startswith("/library/metadata/")
                  for h in shelves)
          and not any("librarySectionID" in h for h in shelves),
          ",".join("%s:%s" % (h.get("hubIdentifier"), h.get("hubKey")) for h in shelves[:3]))
    stale = [r for r in released.get("Metadata") or []
             if str(r.get("originallyAvailableAt") or "") <
             time.strftime("%Y-%m-%d", time.gmtime(time.time() - 366 * 86400))]
    check("Recently Released honours its own one-year window", not stale,
          "%d row(s) older than a year" % len(stale))
    # An item that HAS related titles: earlier edits in this run replace genres, so
    # the first movie may have lost every neighbour it shared one with. Two movies
    # are given one shared genre first, so this cannot pass by comparing nothing.
    for candidate in grid[:2]:
        request(base, "PUT", "/library/metadata/%s?%s=%s"
                % (candidate["ratingKey"], quote("genre[0].tag.tag"), quote("Cycle5 Related")))
    related_item = wanted_hubs = None
    for candidate in grid:
        _, hub_related, _ = get(base, "/hubs/metadata/%s/related" % candidate["ratingKey"])
        if hub_related.get("Hub"):
            related_item = candidate["ratingKey"]
            wanted_hubs = [h.get("hubIdentifier") for h in hub_related["Hub"]]
            break
    if related_item is None:
        skip("the library form of Related carries the hubs form's shelves",
             "no movie shares a genre with another at this point in the run")
    else:
        _, library_related, _ = get(base, "/library/metadata/%s/related" % related_item)
        _, with_related, _ = get(base, "/library/metadata/%s?includeRelated=1" % related_item)
        check("the library form of Related carries the hubs form's shelves",
              [h.get("hubIdentifier") for h in library_related.get("Hub") or []]
              == wanted_hubs, "%s vs %s" % (wanted_hubs, library_related.get("size")))
        related_block = (with_related.get("Metadata") or [{}])[0].get("Related") or {}
        check("includeRelated=1 carries the same shelves",
              [h.get("hubIdentifier") for h in related_block.get("Hub") or []] == wanted_hubs,
              str(related_block)[:80])
    _, common_all, _ = get(base, "/library/sections/%s/common?type=1" % movie_lib)
    shared = (common_all.get("Metadata") or [{}])[0]
    check("the select-all Common is computed over the whole filtered set",
          "ratingKey" in str(shared.get("mixedFields", "")).split(",")
          and shared.get("guid") == "" and "key" not in shared,
          "mixedFields=%r" % shared.get("mixedFields"))
    _, common_one, _ = get(base, "/library/sections/%s/common?type=1&id=%s" % (movie_lib, c5_movie))
    single = (common_one.get("Metadata") or [{}])[0]
    check("a single item's Common has its key and an empty guid",
          single.get("key") == "/library/metadata/%s/children" % c5_movie
          and single.get("guid") == "" and single.get("mixedFields") == "",
          "key=%r guid=%r" % (single.get("key"), single.get("guid")))
    _, sorts, _ = get(base, "/library/sections/%s/sorts?includeAdvanced=1" % movie_lib)
    sort_keys = [s.get("key") for s in sorts.get("Directory") or []]
    check("the advanced sorts are interleaved, Year second",
          sort_keys[:3] == ["titleSort", "year", "originallyAvailableAt"] and len(sort_keys) == 15,
          ",".join(str(k) for k in sort_keys[:4]))
    if show_lib:
        _, season_sorts, _ = get(base, "/library/sections/%s/sorts?type=3" % show_lib)
        check("a library's sorts are per type",
              [s.get("key") for s in season_sorts.get("Directory") or []]
              == ["show.titleSort,index", "random"],
              ",".join(str(s.get("key")) for s in season_sorts.get("Directory") or []))

    # -- an emptied playlist, and a collection read as an item ----------
    uri = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine_id, c5_movie))
    _, made, _ = container(base, "POST", "/playlists?type=video&title=Cycle5&smart=0&uri=%s" % uri)
    playlist_key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if playlist_key:
        members = get(base, "/playlists/%s/items" % playlist_key)[1].get("Metadata") or []
        if members:
            request(base, "DELETE", "/playlists/%s/items/%s"
                    % (playlist_key, members[0].get("playlistItemID")))
        emptied = (get(base, "/playlists/%s" % playlist_key)[1].get("Metadata") or [{}])[0]
        _, emptied_items, _ = get(base, "/playlists/%s/items" % playlist_key)
        check("an emptied playlist carries no composite and no duration",
              "composite" not in emptied and "duration" not in emptied
              and "duration" not in emptied_items,
              ",".join(k for k in ("composite", "duration") if k in emptied))
        request(base, "DELETE", "/playlists/%s" % playlist_key)
    _, made, _ = container(base, "POST", "/library/collections?type=1&title=Cycle5&smart=0"
                           "&sectionId=%s&uri=%s" % (movie_lib, uri))
    collection_key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    if collection_key:
        _, as_item, _ = get(base, "/library/metadata/%s" % collection_key)
        collection_row = (as_item.get("Metadata") or [{}])[0]
        check("a collection read as an item carries the library container",
              as_item.get("identifier") == "com.plexapp.plugins.library"
              and as_item.get("librarySectionID") is not None
              and collection_row.get("librarySectionTitle"),
              ",".join(sorted(k for k in as_item if not isinstance(as_item[k], list))))
        request(base, "DELETE", "/library/collections/%s" % collection_key)

    # ------------------------------------------------------------------
    # Cycle 6: what the PLAYER asks for -- the decision, the transcode family,
    # streams, the part URL and the download queue. Measured against the
    # reference; every check fails against the server as it stood before.
    # ------------------------------------------------------------------
    print("\n=== the player's requests: decision, streams, parts, downloads ===")

    def decide(params=None, headers=None):
        spec = {"path": "/library/metadata/%s" % c5_movie, "mediaIndex": "0", "partIndex": "0",
                "protocol": "dash", "directPlay": "1", "directStream": "1", "hasMDE": "1"}
        for key, value in (params or {}).items():
            if value is None:
                spec.pop(key, None)
            else:
                spec[key] = value
        sent = {"Accept": "application/json", "X-Plex-Client-Identifier": "plexsim-ui-test",
                "X-Plex-Platform": "Chrome"}
        for key, value in (headers or {}).items():
            if value is None:
                sent.pop(key, None)
            else:
                sent[key] = value
        status, body, _ = raw("/video/:/transcode/universal/decision?"
                              + urllib.parse.urlencode(spec), headers=sent)
        return status, (json.loads(body).get("MediaContainer", {}) if body[:1] == b"{" else {})

    statuses = [decide(params)[0] for params in (
        {"path": "/library/metadata/99999999"},
        {"path": "/library/metadata/%s" % (c5_show or 99999999)},
        {"path": "/library/sections/%s" % movie_lib},
        {"mediaIndex": None}, {"mediaIndex": "1"}, {"mediaIndex": "abc"}, {"partIndex": "5"})]
    status_no_part, _ = decide({"partIndex": None})
    check("a decision for nothing playable, or a bad index, is a 400",
          statuses == [400] * 7 and status_no_part == 200,
          "%s, missing partIndex %s" % (statuses, status_no_part))
    profiles = [decide(headers={"X-Plex-Platform": platform})[0]
                for platform in ("Darwin", None, "chrome", "Microsoft Edge")]
    status_named, _ = decide(headers={"X-Plex-Platform": "Darwin",
                                      "X-Plex-Client-Profile-Name": "Generic"})
    check("a decision must name a client profile the server has",
          profiles == [400, 400, 400, 200] and status_named == 200,
          "%s, with a profile name %s" % (profiles, status_named))
    status, refused = decide({"directPlay": "0"})
    check("a refused direct play says neither play nor conversion is available",
          status == 200 and refused.get("generalDecisionCode") == 2000
          and refused.get("transcodeDecisionCode") == 4005
          and refused.get("directPlayDecisionCode") == 3000
          and refused.get("size") == 0 and not refused.get("Metadata")
          and "resourceSession" not in refused,
          "%s / %s / %s" % (refused.get("directPlayDecisionCode"),
                            refused.get("generalDecisionCode"),
                            refused.get("transcodeDecisionCode")))
    _, no_mde = decide({"hasMDE": None, "protocol": "dash"})
    check("without hasMDE the refusal always names protocol http",
          "for protocol http," in str(no_mde.get("directPlayDecisionText")),
          str(no_mde.get("directPlayDecisionText"))[:80])
    _, with_session = decide(headers={"X-Plex-Session-Identifier": "c6-session"})
    _, anonymous = decide(headers={"X-Plex-Client-Identifier": None})
    check("resourceSession is the session identifier, else the client, else empty",
          with_session.get("resourceSession") == "c6-session"
          and anonymous.get("resourceSession") == "",
          "%r / %r" % (with_session.get("resourceSession"), anonymous.get("resourceSession")))
    _, played = decide()
    decided_row = (played.get("Metadata") or [{}])[0]
    decided_media = (decided_row.get("Media") or [{}])[0]
    decided_part = (decided_media.get("Part") or [{}])[0]
    decided_streams = decided_part.get("Stream") or []
    check("a decision's row is its own projection, ids as strings, streams located",
          "Guid" not in decided_row and "Image" not in decided_row
          and isinstance(decided_part.get("id"), str) and isinstance(decided_media.get("id"), str)
          and decided_media.get("hasVoiceActivity") in ("0", "1")
          and decided_streams and all(s.get("location") == "direct" for s in decided_streams)
          and "exists" not in decided_part,
          "part id %r, locations %s" % (decided_part.get("id"),
                                         [s.get("location") for s in decided_streams]))

    status_start, _, _ = request(base, "GET", "/video/:/transcode/universal/start.mpd")
    status_ping, _, _ = request(base, "GET", "/video/:/transcode/universal/ping")
    status_ping_unknown, _, _ = request(base, "GET", "/video/:/transcode/universal/ping?session=nope")
    check("the transcode start family validates before it refuses",
          status_start == 400 and status_ping == 400 and status_ping_unknown == 404,
          "start %s, ping %s, unknown session %s"
          % (status_start, status_ping, status_ping_unknown))
    writes = [request(base, method, "/transcode/sessions")[0] for method in ("DELETE", "POST")]
    check("the bare transcode sessions collection takes no writes",
          writes == [404, 404], str(writes))

    detail_row = (get(base, "/library/metadata/%s" % c5_movie)[1].get("Metadata") or [{}])[0]
    part_row = ((detail_row.get("Media") or [{}])[0].get("Part") or [{}])[0]
    stream_ids = [s.get("id") for s in part_row.get("Stream") or []]
    stream_statuses = [request(base, "GET", "/library/streams/%s" % sid)[0]
                       for sid in ("99999999", "abc")]
    status_put, _, _ = request(base, "PUT", "/library/streams/99999999")
    status_known = request(base, "GET", "/library/streams/%s" % stream_ids[0])[0] \
        if stream_ids else None
    check("an unknown stream is a 404, a PUT to one a 400, a known one 501",
          stream_statuses == [404, 404] and status_put == 400 and status_known == 501,
          "%s / %s / %s" % (stream_statuses, status_put, status_known))
    subtitle_statuses = [request(base, "GET", "/library/metadata/%s/subtitles%s"
                                 % (c5_movie, spec))[0]
                         for spec in ("", "?language=xx", "?language=eng", "?language=en-US")]
    check("a subtitle search needs a real two-letter language",
          subtitle_statuses == [400, 500, 500, 200], str(subtitle_statuses))
    if part_row.get("id"):
        status_empty, body_empty, _ = request(base, "PUT", "/library/parts/%s?audioStreamID="
                                              % part_row["id"])
        status_text, body_text, _ = request(base, "PUT", "/library/parts/%s?subtitleStreamID=abc"
                                            % part_row["id"])
        check("a malformed stream id is ignored, not refused",
              is_empty_200(status_empty, body_empty) and is_empty_200(status_text, body_text),
              "%s / %s" % (status_empty, status_text))
        stamp = str(part_row.get("key", "")).split("/")
        version = stamp[4] if len(stamp) > 4 else "1"
        deeper = [request(base, "HEAD", path, want_json=False)[0] for path in (
            "/library/parts/%s/indexes/sd" % part_row["id"],
            "/library/parts/%s/indexes/sd/1000" % part_row["id"],
            "/library/parts/%s/x/file.mkv" % part_row["id"],
            "/library/parts/%s/%s/file.mkv/extra" % (part_row["id"], version))]
        check("only /library/parts/{id}/{version}/{file} is the media",
              deeper == [404, 404, 404, 404], str(deeper))

    subtitle_tracks = [s for row in get(base, "/library/sections/%s/all?type=4" % show_lib)[1]
                       .get("Metadata") or [] if show_lib
                       for s in ((get(base, "/library/metadata/%s" % row["ratingKey"])[1]
                                  .get("Metadata") or [{}])[0].get("Media") or [{}])[0]
                       .get("Part", [{}])[0].get("Stream") or []
                       if s.get("streamType") == 3]
    if subtitle_tracks:
        check("a subtitle track carries only true flags and canAutoSync",
              all("embeddedInVideo" not in s and s.get("canAutoSync") is False
                  and s.get("default", True) is True and s.get("forced", True) is True
                  for s in subtitle_tracks),
              "%d track(s)" % len(subtitle_tracks))
    else:
        skip("a subtitle track carries only true flags and canAutoSync",
             "no episode in this catalog carries a subtitle track")

    status_q, queue_doc, _ = container(base, "POST", "/downloadQueue")
    queue = (queue_doc.get("DownloadQueue") or [{}])[0]
    _, again, _ = container(base, "POST", "/downloadQueue")
    status_read, read_back, _ = get(base, "/downloadQueue/%s" % queue.get("id"))
    status_items, items, _ = get(base, "/downloadQueue/%s/items" % queue.get("id"))
    status_delete, _, _ = request(base, "DELETE", "/downloadQueue/%s" % queue.get("id"))
    _, bare_queue, _ = get(base, "/downloadQueue")
    check("the download queue is created per client and read back",
          status_q == 200 and queue.get("clientIdentifier") == "plexsim-ui-test"
          and queue.get("status") == "done"
          and (again.get("DownloadQueue") or [{}])[0].get("id") == queue.get("id")
          and status_read == 200 and (read_back.get("DownloadQueue") or [{}])[0].get("id")
          == queue.get("id") and status_items == 200 and items.get("size") == 0
          and status_delete == 404 and bare_queue.get("content") == "plugins",
          "POST %s id=%s, read %s, items %s, delete %s"
          % (status_q, queue.get("id"), status_read, status_items, status_delete))

    print("\n=== what every write pushes, and malformed identifiers ===")

    def timeline_entries(frames):
        return [entry for frame in frames
                if (frame.get("NotificationContainer") or {}).get("type") == "timeline"
                for entry in frame["NotificationContainer"].get("TimelineEntry") or []]

    def detail_of(key):
        return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]

    machine_now = get(base, "/identity")[1].get("machineIdentifier")
    member_uri = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                       % (machine_now, c5_movie))
    made = {}

    def list_lifecycle():
        _, doc, _ = container(base, "POST", "/playlists?type=video&title=Cycle6List&smart=0"
                                            "&uri=%s" % member_uri)
        made["playlist"] = (doc.get("Metadata") or [{}])[0].get("ratingKey")
        time.sleep(0.3)
        request(base, "DELETE", "/playlists/%s" % made["playlist"])

    _, frames = notifications(base, list_lifecycle, seconds=4)
    mine = [(e.get("state"), e.get("type")) for e in timeline_entries(frames)
            if str(e.get("itemID")) == str(made.get("playlist"))]
    check("a playlist's create and delete frames are typed 15", mine == [(0, 15), (9, 15)],
          str(mine))

    def collection_lifecycle():
        _, doc, _ = container(base, "POST", "/library/collections?type=1&title=Cycle6Set"
                                            "&smart=0&sectionId=%s&uri=%s" % (movie_lib, member_uri))
        made["collection"] = (doc.get("Metadata") or [{}])[0]
        time.sleep(0.3)
        request(base, "DELETE", "/library/collections/%s" % made["collection"].get("ratingKey"))

    _, frames = notifications(base, collection_lifecycle, seconds=4)
    collection_key = str((made.get("collection") or {}).get("ratingKey"))
    entries = timeline_entries(frames)
    sequence = [(str(e.get("itemID")) == collection_key, e.get("state"), e.get("type"),
                 e.get("metadataState")) for e in entries]
    check("a collection's lifecycle: created, its members refetched, then deleted",
          sequence == [(True, 0, 18, "created"), (False, 3, 1, "queued"), (False, 5, 1, None),
                       (True, 9, 18, "deleted")], str(sequence))
    gone = [e for e in entries if str(e.get("itemID")) == collection_key and e.get("state") == 9]
    check("a collection create answers no section fields and its delete frame no title",
          "librarySectionID" not in (made.get("collection") or {})
          and gone and gone[0].get("title") == "",
          "create keys %s" % sorted(k for k in (made.get("collection") or {})
                                    if k.startswith("librarySection")))
    movie_detail = detail_of(c5_movie)
    member_titles = {e.get("title") for e in entries if str(e.get("itemID")) == str(c5_movie)}
    wanted_title = ("%s (%s)" % (movie_detail.get("title"), movie_detail.get("year"))
                    if movie_detail.get("year") else movie_detail.get("title"))
    check("a movie's frames carry its year in the title", member_titles == {wanted_title},
          str(member_titles))

    _, doc, _ = container(base, "POST", "/library/collections?type=1&title=Cycle6Member&smart=0"
                                        "&sectionId=%s&uri=%s" % (movie_lib, member_uri))
    member_collection = (doc.get("Metadata") or [{}])[0].get("ratingKey")
    if member_collection:
        status, _, _ = request(base, "DELETE", "/library/collections/%s/children/%s"
                               % (member_collection, c5_movie))
        members = get(base, "/library/collections/%s/children" % member_collection)[1] \
            .get("Metadata") or []
        check("removing a member through /children removes it",
              status == 200 and not any(str(m.get("ratingKey")) == str(c5_movie) for m in members),
              "HTTP %s, %d member(s) left" % (status, len(members)))
        request(base, "DELETE", "/library/collections/%s" % member_collection)

    def edits():
        request(base, "PUT", "/library/metadata/%s?summary.value=%s"
                % (c5_movie, quote("cycle six item edit")))
        time.sleep(0.3)
        request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&summary.value=%s"
                % (movie_lib, c5_movie, quote("cycle six section edit")))

    _, frames = notifications(base, edits, seconds=4)
    first_states = [e.get("metadataState") for e in timeline_entries(frames)
                    if str(e.get("itemID")) == str(c5_movie) and e.get("state") == 3]
    check("an item-path edit is `loading` and a section edit `queued`",
          first_states == ["loading", "queued"], str(first_states))

    subject = c5_episode or c5_movie
    player6 = {"X-Plex-Client-Identifier": "plexsim-c6-timeline", "Accept": "application/json",
               "X-Plex-Platform": "Chrome"}

    def beat6(state, offset, duration):
        _, body, _ = raw("/:/timeline?ratingKey=%s&key=%s&state=%s&time=%d&duration=%d"
                         % (subject, quote("/library/metadata/%s" % subject), state, offset,
                            duration), headers=player6)
        return json.loads(body).get("MediaContainer", {}) if body[:1] == b"{" else {}

    before = detail_of(subject)
    short = beat6("stopped", 59500, 60000)
    after_short = detail_of(subject)
    check("a stopped beat under a minute is ignored and stores nothing",
          short.get("playbackState") == "ignore"
          and after_short.get("viewCount", 0) == before.get("viewCount", 0)
          and after_short.get("viewOffset") == before.get("viewOffset"),
          "%s viewCount %s->%s" % (short.get("playbackState"), before.get("viewCount", 0),
                                   after_short.get("viewCount", 0)))
    exact = beat6("playing", 60000, 734000)
    check("exactly one minute is still ignored",
          exact.get("playbackState") == "ignore"
          and detail_of(subject).get("viewOffset") == before.get("viewOffset"),
          str(exact.get("playbackState")))
    if short.get("Bandwidths"):
        skip("the timeline response's size counts only Bandwidths", "a decision is armed here")
    else:
        check("the timeline response's size counts only Bandwidths", short.get("size") == 0,
              "size=%s" % short.get("size"))
    done = beat6("playing", 700000, 734000)
    after_done = detail_of(subject)
    stop = beat6("stopped", 700100, 734000)
    after_stop = detail_of(subject)
    check("a playing beat past 90% completes the play, and stopping after counts nothing",
          done.get("playbackState") == "complete"
          and after_done.get("viewCount", 0) == before.get("viewCount", 0) + 1
          and after_stop.get("viewCount", 0) == after_done.get("viewCount", 0)
          and "viewOffset" not in after_stop and stop.get("playbackState") == "complete",
          "viewCount %s -> %s -> %s, offset %s" % (before.get("viewCount", 0),
                                                  after_done.get("viewCount", 0),
                                                  after_stop.get("viewCount", 0),
                                                  after_stop.get("viewOffset")))

    refused = [request(base, "GET", path)[0] for path in (
        "/identity?foo", "/library/sections?&", "/library/metadata/%s?includeGuids" % c5_movie)]
    accepted = [request(base, "GET", path)[0] for path in ("/identity?", "/library/sections?foo=")]
    check("a query parameter with no = is a 400", refused == [400, 400, 400]
          and accepted == [200, 200], "%s / %s" % (refused, accepted))
    slashes = [request(base, "GET", path)[0] for path in (
        "//identity", "/library//sections", "/library/metadata/%s//children" % c5_movie)]
    status_trailing, trailing, _ = get(base, "/library/sections/")
    status_history, _, _ = get(base, "/status/sessions/history/")
    check("an empty path segment is a 404, one trailing slash is not",
          slashes == [404, 404, 404] and status_trailing == 200 and trailing.get("Directory")
          and status_history == 404,
          "%s, trailing %s, history %s" % (slashes, status_trailing, status_history))
    refusals = (get(base, "/library/sections/abc")[0],
                get(base, "/library/metadata/%s/children" % c5_movie)[0],
                get(base, "/library/metadata/%s/allLeaves" % c5_movie)[0],
                request(base, "GET", "/activities/abc")[0])
    check("a word for a section id, a movie's children and an activity read refuse",
          refusals == (404, 400, 200, 404), str(refusals))
    one = get(base, "/library/metadata/%s,abc/tree" % c5_movie)[0]
    many = get(base, "/library/metadata/%s,%s/tree" % (c5_movie, c5_movie))[0]
    check("a leaf on a list with one numeric member is that item's leaf",
          one == 200 and many == 404, "%s / %s" % (one, many))
    sized = [request(base, "GET", path)[0] for path in (
        "/playlists/2147483647", "/playlists/2147483648", "/playQueues/2147483648",
        "/library/metadata/2147483648", "/playQueues/abc")]
    check("an id past int32 is a 400 where it is parsed, and a play queue id must be a number",
          sized == [404, 400, 400, 404, 400], str(sized))
    _, both_types, _ = get(base, "/library/sections/%s/all?type=2&type=1" % movie_lib)
    status_blank, _, _ = raw("/library/sections/%s/all" % movie_lib,
                             headers={"Accept": "application/json", "X-Plex-Container-Size": ""})
    check("a repeated type selects nothing, and an empty container size is a 500",
          not both_types.get("Metadata") and status_blank == 500,
          "%d row(s), blank size %s" % (len(both_types.get("Metadata") or []), status_blank))
    status_quiet, quiet = eventsource(base, lambda: None, seconds=3)
    check("the eventsource channel's first ping comes ten seconds in, not on connect",
          status_quiet == 200 and not quiet, str(quiet[:2]))
    _, logged = eventsource(base, lambda: request(base, "GET", "/log?level=3&message=cycle6"
                                                               "&source=suite"),
                            seconds=4, spec="?filters=")
    check("an empty filters= delivers the log stream as well",
          any(name == "log" for name, _ in logged), ",".join(sorted({n for n, _ in logged})))

    # ------------------------------------------------------------------
    # Cycle 7: themes, hub row projections, excludeFields, library playlists,
    # season and episode artwork. Measured against the reference; every check
    # fails against the server as it stood before.
    # ------------------------------------------------------------------
    print("\n=== themes, hub rows, excludeFields and episode artwork ===")
    if c5_show and c5_season and c5_episode:
        show_row = detail_of(c5_show)
        _, season_list, _ = get(base, "/library/metadata/%s/children" % c5_season)
        episode_row = detail_of(c5_episode)
        themed = ([k for k in show_row if "heme" in k] + [k for k in season_list if "heme" in k]
                  + [k for k in episode_row if "heme" in k]
                  + [k for r in season_list.get("Metadata") or [] for k in r if "heme" in k])
        check("nothing advertises a theme plexsim cannot serve", not themed, ",".join(themed))
        images = [(i.get("type"), i.get("url")) for i in episode_row.get("Image") or []]
        check("an episode's coverPoster is its show's poster and its still a snapshot",
              images[:2] == [("coverPoster", episode_row.get("grandparentThumb")),
                             ("snapshot", episode_row.get("thumb"))],
              str([t for t, _ in images]))
        arts = [get(base, "/library/metadata/%s/arts" % key)[1].get("size")
                for key in (c5_season, c5_episode)]
        check("a season and an episode offer no background of the show's", arts == [0, 0],
              str(arts))
    else:
        skip("nothing advertises a theme plexsim cannot serve", "no show in this catalog")

    _, with_playlists, _ = get(base, "/hubs/sections/%s?includeLibraryPlaylists=1" % movie_lib)
    _, without, _ = get(base, "/hubs/sections/%s" % movie_lib)
    ids = [h.get("hubIdentifier") for h in with_playlists.get("Hub") or []]
    wanted_shelf = "recent.library.playlists.%s" % movie_lib
    added_at = ids.index("movie.recentlyadded.%s" % movie_lib) if "movie.recentlyadded.%s" % movie_lib in ids else -2
    check("includeLibraryPlaylists adds Recent Playlists right after Recently Added",
          wanted_shelf in ids and ids.index(wanted_shelf) == added_at + 1
          and wanted_shelf not in [h.get("hubIdentifier") for h in without.get("Hub") or []],
          ",".join(str(i) for i in ids))

    _, recent_movies, _ = get(base, "/hubs/home/recentlyAdded?type=1")
    word = str((detail_of(c5_movie).get("title") or "x").split()[0])
    _, searched, _ = get(base, "/hubs/search?query=%s" % quote(word))
    light = [r for r in (recent_movies.get("Metadata") or [])
             + [m for h in searched.get("Hub") or [] for m in h.get("Metadata") or []
                if m.get("type") in ("movie", "episode")]]
    heavy = [r.get("title") for r in light
             if "Guid" in r or any(set(t) != {"tag"} for key in ("Genre", "Country", "Director", "Writer", "Role")
                                   for t in r.get(key) or [])]
    check("a movie on a hub or in search has no Guid and text-only tags",
          light and not heavy, ",".join(str(h) for h in heavy[:3]))
    if show_lib:
        _, show_hubs, _ = get(base, "/hubs/sections/%s" % show_lib)
        tv_added = next((h for h in show_hubs.get("Hub") or []
                         if str(h.get("hubIdentifier", "")).startswith("tv.recentlyadded")), {})
        shows_there = [r for r in tv_added.get("Metadata") or [] if r.get("type") == "show"]
        check("a show on Recently Added TV is the full row",
              shows_there and all("Location" in r and r.get("Guid") for r in shows_there),
              ",".join(sorted(shows_there[0]) if shows_there else []))

    _, trimmed_grid, _ = get(base, "/library/sections/%s/all?excludeFields=summary" % movie_lib)
    _, trimmed_hubs, _ = get(base, "/hubs/sections/%s?excludeFields=summary" % movie_lib)
    trimmed_rows = (trimmed_grid.get("Metadata") or []) + [
        m for h in trimmed_hubs.get("Hub") or [] for m in h.get("Metadata") or []]
    summary_left = [r.get("title") for r in trimmed_rows if "summary" in r]
    children_trim = {}
    if c5_show:
        _, children_trim, _ = get(base, "/library/metadata/%s/children?excludeFields=summary"
                                  % c5_show)
    check("excludeFields drops the field from every row and from the container",
          trimmed_rows and not summary_left and "summary" not in children_trim
          and not any("summary" in r for r in children_trim.get("Metadata") or []),
          "%d row(s) kept summary" % len(summary_left))

    # ------------------------------------------------------------------
    # Cycle 7, second batch: python-plexapi as a third-party client. Scoped sort
    # keys and filters, the title word matcher, /library/all as a query, and the
    # reads plexapi makes that plexsim 404'd. Measured against the reference and
    # fun; every check fails against the server as it stood before.
    # ------------------------------------------------------------------
    print("\n=== scoped sorts and filters, /library/all, and python-plexapi's reads ===")
    if show_lib and c5_show and c5_season and c5_episode:
        leaves = get(base, "/library/metadata/%s/allLeaves" % c5_show)[1].get("Metadata") or []
        broadcast = [str(r.get("ratingKey")) for r in leaves]

        def episode_keys(spec):
            status, body, _ = get(base, "/library/sections/%s/all?type=4&%s" % (show_lib, spec))
            return status, [str(r.get("ratingKey")) for r in body.get("Metadata") or []]

        status_show, by_show = episode_keys("sort=show.titleSort:desc")
        status_season, by_season = episode_keys("sort=season.index:desc")
        status_id, by_id = episode_keys("sort=episode.id:desc")
        status_bad, _ = episode_keys("sort=show.nosuch")
        season_desc = [str(r.get("ratingKey")) for r in
                       sorted(leaves, key=lambda r: -int(r.get("parentIndex") or 0))]
        movies_scoped = get(base, "/library/sections/%s/all?type=1&sort=movie.titleSort:desc"
                            % movie_lib)[1].get("Metadata") or []
        movies_plain = get(base, "/library/sections/%s/all?type=1&sort=titleSort:desc"
                           % movie_lib)[1].get("Metadata") or []
        check("a type-prefixed sort key sorts by that type's field, ties in broadcast order",
              status_show == 200 and by_show == broadcast
              and status_season == 200 and by_season == season_desc
              and status_id == 200 and by_id == sorted(by_id, key=int, reverse=True)
              and status_bad == 500 and movies_scoped
              and [r.get("ratingKey") for r in movies_scoped]
              == [r.get("ratingKey") for r in movies_plain],
              "HTTP %s/%s/%s/%s, %s vs %s" % (status_show, status_season, status_id, status_bad,
                                              by_season, season_desc))

        def keys_in(section, spec):
            body = get(base, "/library/sections/%s/all?%s" % (section, spec))[1]
            return sorted(str(r.get("ratingKey")) for r in body.get("Metadata") or [])

        show_title = str(detail_of(c5_show).get("title") or "x")
        season_row7 = detail_of(c5_season)
        episode_row7 = detail_of(c5_episode)
        season_leaves = get(base, "/library/metadata/%s/children" % c5_season)[1].get("Metadata") or []
        same_index = [r for r in leaves if str(r.get("index")) == str(episode_row7.get("index"))]
        scoped = {
            "show": keys_in(show_lib, "type=4&show.title=%s" % quote(show_title.split()[-1][:4])),
            "season": keys_in(show_lib, "type=4&season.index=%s" % season_row7.get("index")),
            "up": keys_in(show_lib, "type=2&episode.title=%s"
                          % quote(str(episode_row7.get("title") or "x").split()[0])),
            "seasons": keys_in(show_lib, "type=3&episode.index=%s" % episode_row7.get("index")),
            "index": keys_in(show_lib, "type=4&index=%s" % episode_row7.get("index")),
        }
        wanted = {
            "show": sorted(broadcast),
            "season": sorted(str(r.get("ratingKey")) for r in season_leaves),
            "up": [str(c5_show)],
            "seasons": sorted({str(r.get("parentRatingKey")) for r in same_index}),
            "index": sorted(str(r.get("ratingKey")) for r in same_index),
        }
        check("a scoped filter reads the field of the row its prefix names",
              scoped == wanted,
              ", ".join("%s %s/%s" % (k, scoped[k], wanted[k]) for k in scoped
                        if scoped[k] != wanted[k]))

        listed_seasons = get(base, "/library/sections/%s/all?type=3" % show_lib)[1].get("Metadata") or []
        child_seasons = get(base, "/library/metadata/%s/children" % c5_show)[1].get("Metadata") or []
        child_episodes = get(base, "/library/metadata/%s/children" % c5_season)[1].get("Metadata") or []
        sectioned = [r.get("title") for r in child_seasons + child_episodes
                     if any(k in r for k in ("librarySectionID", "librarySectionKey",
                                             "librarySectionTitle"))]
        check("a season LISTING row has no leaf counts and a child row no section fields",
              listed_seasons and not any("leafCount" in r or "viewedLeafCount" in r
                                         for r in listed_seasons)
              and any("leafCount" in r for r in child_seasons) and not sectioned,
              "listing %s, sectioned %s" % (sorted(listed_seasons[0]) if listed_seasons else [],
                                            sectioned[:2]))
        check("a season with a year of its own carries it, titled for sorting, no parentYear",
              season_row7.get("year") and season_row7.get("titleSort") == season_row7.get("title")
              and "parentYear" not in season_row7,
              "year=%r titleSort=%r parentYear=%r" % (season_row7.get("year"),
                                                      season_row7.get("titleSort"),
                                                      season_row7.get("parentYear")))
        show_listed = next((r for r in get(base, "/library/sections/%s/all" % show_lib)[1]
                            .get("Metadata") or [] if str(r.get("ratingKey")) == str(c5_show)), {})
        show_locations = detail_of(c5_show).get("Location") or []
        check("a show's listing row carries its duration and first air date, its Location no id",
              show_listed.get("duration") and show_listed.get("originallyAvailableAt")
              and show_locations and not any("id" in location for location in show_locations),
              "duration=%r aired=%r Location=%s" % (show_listed.get("duration"),
                                                    show_listed.get("originallyAvailableAt"),
                                                    show_locations))
    else:
        skip("a type-prefixed sort key sorts by that type's field, ties in broadcast order",
             "no show in this catalog")

    movie_rows7 = get(base, "/library/sections/%s/all" % movie_lib)[1].get("Metadata") or []
    subject7 = next((r for r in movie_rows7 if len(str(r.get("title", "")).split()) >= 3), None)
    if subject7:
        words = str(subject7["title"]).split()
        subject_key = str(subject7["ratingKey"])

        def movie_hits(spec):
            body = get(base, "/library/sections/%s/all?%s" % (movie_lib, spec))[1]
            return [str(r.get("ratingKey")) for r in body.get("Metadata") or []]

        inner = words[0][1:] if len(words[0]) > 2 else words[0]
        inner_begins = any(w.lower().startswith(inner.lower()) for w in words)
        all_keys = sorted(str(r.get("ratingKey")) for r in movie_rows7)
        two_words = sorted(str(r.get("ratingKey")) for r in movie_rows7
                           if len(str(r.get("title", "")).split()) >= 2)
        skipping = movie_hits("title=%s" % quote(words[0] + " " + words[-1]))
        backwards = movie_hits("title=%s" % quote(words[-1] + " " + words[0]))
        middle = movie_hits("title=%s" % quote(inner))
        not_middle = movie_hits("title%%21=%s" % quote(inner))
        check("a bare title= is each word's beginning, in order; != still reads a substring",
              subject_key in skipping and subject_key not in backwards
              and (subject_key in middle) == inner_begins and subject_key not in not_middle
              and sorted(movie_hits("title=")) == all_keys
              and sorted(movie_hits("title=%20")) == two_words,
              "%r: skipping %s backwards %s middle %s" % (subject7["title"], subject_key in skipping,
                                                          subject_key in backwards,
                                                          subject_key in middle))
        sort_value = str(subject7.get("titleSort") or subject7["title"])
        exact = movie_hits("titleSort=%s" % quote(sort_value.lower()))
        partial = movie_hits("titleSort=%s" % quote(sort_value[:-1]))
        not_partial = movie_hits("titleSort%%21=%s" % quote(sort_value[:-1]))
        equal = movie_hits("title%%3D=%s" % quote(str(subject7["title"]).lower()))
        check("titleSort= is a case-insensitive equality, and so is title==",
              subject_key in exact and subject_key not in partial
              and subject_key in not_partial and subject_key in equal,
              "exact %s partial %s not-partial %s equal %s" % (
                  subject_key in exact, subject_key in partial, subject_key in not_partial,
                  subject_key in equal))
    else:
        skip("a bare title= is each word's beginning, in order; != still reads a substring",
             "no title of three words")

    status_typed, typed_all, _ = get(base, "/library/all?type=4" if show_lib else "/library/all?type=1")
    _, bare_all, _ = get(base, "/library/all")
    _, missed_all, _ = get(base, "/library/all?type=1&title=zzzznope")
    typed_rows = typed_all.get("Metadata") or []
    bare_rows = bare_all.get("Metadata") or []
    bare_keys = [int(r.get("ratingKey")) for r in bare_rows]
    check("/library/all selects by type, filters, and carries Media only when typed",
          status_typed == 200 and typed_rows
          and all(r.get("type") == ("episode" if show_lib else "movie") and r.get("Media")
                  for r in typed_rows)
          and bare_rows and not any("Media" in r for r in bare_rows)
          and bare_keys == sorted(bare_keys) and not missed_all.get("Metadata"),
          "%d typed, %d bare, %d missed" % (len(typed_rows), len(bare_rows),
                                            len(missed_all.get("Metadata") or [])))

    status_managed, managed, _ = get(base, "/hubs/sections/%s/manage" % movie_lib)
    managed_rows = managed.get("Hub") or []
    managed_show = (get(base, "/hubs/sections/%s/manage" % show_lib)[1].get("Hub") or []) if show_lib else []
    check("Manage Recommendations lists the library type's hubs and where each is promoted",
          status_managed == 200 and len(managed_rows) == 8
          and [h.get("identifier") for h in managed_rows][:2]
          == ["movie.recentlyreleased", "movie.recentlyadded"]
          and [h.get("identifier") for h in managed_rows if h.get("promotedToOwnHome")]
          == ["movie.recentlyadded", "movie.curated"]
          and (not show_lib or len(managed_show) == 9)
          and get(base, "/hubs/sections/99999/manage")[0] == 404,
          "HTTP %s, %d/%d hub(s)" % (status_managed, len(managed_rows), len(managed_show)))

    request(base, "PUT", "/library/metadata/%s/refresh" % c5_movie)
    status_timeline, section_timeline, finished = None, {}, {}
    for _ in range(20):
        status_timeline, section_timeline, _ = get(base, "/library/sections/%s/timeline" % movie_lib)
        finished = next((r for r in section_timeline.get("Metadata") or []
                         if str(r.get("ratingKey")) == str(c5_movie)), {})
        if finished.get("state") == 5:
            break
        time.sleep(0.25)
    check("a library's timeline lists what the server just finished, without its tags",
          status_timeline == 200 and finished.get("state") == 5 and "Genre" not in finished
          and section_timeline.get("updateQueueSize") == 0
          and section_timeline.get("latestEntryTime"),
          "HTTP %s, state %s" % (status_timeline, finished.get("state")))

    candidate_lists = [get(base, "/library/metadata/%s/%s" % (key, leaf))[:2]
                       for key in (c5_movie, c5_episode) if key
                       for leaf in ("clearLogos", "themes")]
    check("clear logos and theme music answer an empty candidate list",
          candidate_lists and all(status == 200 and body.get("size") == 0
                                  for status, body in candidate_lists),
          str([status for status, _ in candidate_lists]))

    folder = base64.b64encode(("/plexsim/%s" % movie_lib).encode()).decode()
    status_off, folders_only, _ = get(base, "/services/browse/%s?includeFiles=0" % folder)
    status_on, with_files, _ = get(base, "/services/browse/%s?includeFiles=1" % folder)
    check("the folder picker needs includeFiles present, and =1 lists the files too",
          status_off == 200 and not folders_only.get("File")
          and status_on == 200 and with_files.get("File"),
          "HTTP %s / %s, %d file(s)" % (status_off, status_on, len(with_files.get("File") or [])))

    recent_rows = get(base, "/library/recentlyAdded")[1].get("Metadata") or []
    wrong_recent = [r.get("title") for r in recent_rows
                    if "librarySectionKey" in r or "Guid" in r or "allowSync" not in r
                    or not r.get("librarySectionUUID") or not r.get("librarySectionTitle")]
    check("a recentlyAdded row names its section by id, title and UUID, and is light",
          recent_rows and not wrong_recent, ",".join(str(w) for w in wrong_recent[:3]))

    # -- cycle 8: the paths the web bundle builds that no recorded walk reached ------
    def paged(path, start, size):
        """A JSON read with the container paging the client sends as HEADERS."""
        req = urllib.request.Request(base.rstrip("/") + path, headers={
            "User-Agent": UA, "Accept": "application/json",
            "X-Plex-Client-Identifier": "plexsim-ui-test", "X-Plex-Product": "Plex Web",
            "X-Plex-Platform": "Chrome",
            "X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read()).get("MediaContainer") or {}
        except urllib.error.HTTPError as error:
            return error.code, {}
        except ValueError:
            return None, {}

    subsets = {}
    for leaf in ("onDeck", "recentlyAdded", "newest", "recentlyViewed", "recentlyViewedShows"):
        status_subset, body_subset, _ = get(base, "/library/sections/%s/%s" % (movie_lib, leaf))
        subsets[leaf] = (status_subset, body_subset.get("title2"), body_subset.get("allowSync"))
    check("a library's subsets are titled for themselves; only Viewed Shows can't sync",
          subsets == {"onDeck": (200, "On Deck", True),
                      "recentlyAdded": (200, "Recently Added", True),
                      "newest": (200, "Recently Released", True),
                      "recentlyViewed": (200, "Recently Viewed", True),
                      "recentlyViewedShows": (200, "Recently Viewed Shows", False)},
          str(subsets))
    if show_lib:
        _, show_added, _ = get(base, "/library/sections/%s/recentlyAdded?type=2" % show_lib)
        _, show_viewed, _ = get(base, "/library/sections/%s/recentlyViewed" % show_lib)
        _, show_shows, _ = get(base, "/library/sections/%s/recentlyViewedShows?type=2" % show_lib)
        added_types = sorted({str(r.get("type")) for r in show_added.get("Metadata") or []})
        shows_listed = show_shows.get("Metadata") or []
        check("a show library's recent subsets are EPISODE lists, Viewed Shows its shows",
              added_types == ["episode"] and show_added.get("viewGroup") == "episode"
              and show_added.get("mixedParents") and show_added.get("nocache")
              and show_viewed.get("title2") == "Recently Viewed Episodes"
              and shows_listed and {r.get("type") for r in shows_listed} == {"show"}
              and show_shows.get("totalSize") == len(shows_listed) and show_shows.get("offset") == 0,
              "types %s viewGroup %s, %d show(s) totalSize %s" % (
                  added_types, show_added.get("viewGroup"), len(shows_listed),
                  show_shows.get("totalSize")))
    else:
        skip("a show library's recent subsets are EPISODE lists, Viewed Shows its shows",
             "no show library in this catalog")

    movie_rows8 = get(base, "/library/sections/%s/all" % movie_lib)[1].get("Metadata") or []
    subject8 = next((r for r in movie_rows8 if len(str(r.get("title", "")).split()) >= 3), None)
    if subject8:
        title8 = str(subject8["title"])
        words8 = title8.split()
        prefix8 = words8[-1][:3]
        at8 = title8.lower().find(prefix8.lower())

        def completes(spec):
            return get(base, "/library/sections/%s/autocomplete?type=1&limit=10&%s"
                       % (movie_lib, spec))

        status_ac, found_ac, _ = completes("title.query=%s" % quote(prefix8))
        hit8 = next((r for r in found_ac.get("Metadata") or []
                     if str(r.get("ratingKey")) == str(subject8["ratingKey"])), {})
        skipping8 = completes("title.query=%s" % quote(words8[0] + " " + words8[-1]))[1]
        genres8 = detail_of(subject8["ratingKey"]).get("Genre") or []
        tag_rows = (completes("genre.query=%s" % quote(genres8[0]["tag"][:2]))[1]
                    .get("Directory") or []) if genres8 else []
        tag_hit = next((t for t in tag_rows if t.get("tag") == (genres8[0]["tag"] if genres8 else None)), {})
        refusals = (get(base, "/library/sections/%s/autocomplete?title.query=a" % movie_lib)[0],
                    completes("title.query=a&genre.query=a")[0], completes("decade.query=1")[0],
                    completes("bogus.query=1")[0], completes("title.query=a&limit=abc")[0])
        check("autocomplete finds a phrase at a word's start and says where it matched",
              status_ac == 200 and hit8.get("title.queryRange") == "%d,%d" % (at8, at8 + len(prefix8) - 1)
              and not any(str(r.get("ratingKey")) == str(subject8["ratingKey"])
                          for r in skipping8.get("Metadata") or [])
              and (not genres8 or (tag_hit.get("tagType") == 1
                                   and tag_hit.get("filter") == "genre=%s" % genres8[0].get("id")))
              and refusals == (400, 400, 500, 400, 400),
              "%r: range %s, tag %s, refusals %s" % (title8, hit8.get("title.queryRange"),
                                                     tag_hit.get("filter"), refusals))
    else:
        skip("autocomplete finds a phrase at a word's start and says where it matched",
             "no title of three words")

    episode_filters = get(base, "/library/sections/%s/filters?type=4" % movie_lib)[1].get("Directory") or []
    advanced_filters = (get(base, "/library/sections/%s/filters?includeAdvanced=1" % movie_lib)[1]
                        .get("Directory") or [])
    season_filters = get(base, "/library/sections/%s/filters?type=3" % movie_lib)[1].get("Directory")
    # Cycle 14: the episode list is 17 on kids (1.43.4) and 9 on zen (1.42.2, the
    # build plexsim reports) -- it drops the six codec axes AND `hdr`/`dovi`, which
    # the movie list keeps. The type-keying and the Duplicates rule are unchanged.
    check("/filters is the asked TYPE's list, and includeAdvanced adds Duplicates",
          len(episode_filters) == 9
          and episode_filters[0].get("key") == "/library/sections/%s/year?type=4" % movie_lib
          and not season_filters
          and get(base, "/library/sections/%s/filters?type=movie" % movie_lib)[0] == 400
          and advanced_filters and advanced_filters[-1].get("filter") == "duplicate"
          and advanced_filters[-1].get("advanced") is True,
          "%d episode filter(s), last advanced %s" % (
              len(episode_filters), advanced_filters[-1].get("filter") if advanced_filters else None))

    status_top, top_folder, _ = get(base, "/library/sections/%s/folder" % movie_lib)
    top_rows = top_folder.get("Metadata") or []
    folders8 = [r for r in top_rows if not r.get("ratingKey")]
    opened8 = (get(base, folders8[0]["key"])[1].get("Metadata") or []) if folders8 else top_rows
    check("By Folder opens a folder into what it holds; an unknown parent is empty",
          status_top == 200 and top_folder.get("allowSync") is True
          and any(r.get("ratingKey") for r in opened8)
          and get(base, "/library/sections/%s/folder?parent=987654" % movie_lib)[1].get("size") == 0,
          "%d top row(s), %d folder(s), opened %d" % (len(top_rows), len(folders8), len(opened8)))

    libraries8 = get(base, "/library/sections?includePreferences=1")[1].get("Directory") or []
    inline8 = {d.get("key"): [s.get("id") for s in (d.get("Preferences") or {}).get("Setting") or []]
               for d in libraries8}
    own8 = {d.get("key"): [s.get("id") for s in get(base, "/library/sections/%s/prefs" % d.get("key"))[1]
                           .get("Setting") or []] for d in libraries8}
    episode_sort = next((s for d in libraries8 if d.get("type") == "show"
                         for s in (d.get("Preferences") or {}).get("Setting") or []
                         if s.get("id") == "episodeSort"), {})
    check("includePreferences puts each library's settings on its row",
          libraries8 and inline8 == own8 and all(inline8.values())
          and (not show_lib or "-1:Library default" in str(episode_sort.get("enumValues"))),
          "%s" % {k: len(v) for k, v in inline8.items()})

    if c5_show and c5_season and c5_episode:
        leaves8 = [r.get("ratingKey") for r in get(base, "/library/metadata/%s/allLeaves" % c5_show)[1]
                   .get("Metadata") or []]
        status_gc, grand8, _ = get(base, "/library/metadata/%s/grandchildren" % c5_show)
        season_grand = [r.get("ratingKey") for r in get(base, "/library/metadata/%s/grandchildren"
                                                        % c5_season)[1].get("Metadata") or []]
        season_kids = [r.get("ratingKey") for r in get(base, "/library/metadata/%s/children"
                                                       % c5_season)[1].get("Metadata") or []]
        episode_grand = [r.get("ratingKey") for r in get(base, "/library/metadata/%s/grandchildren"
                                                         % c5_episode)[1].get("Metadata") or []]
        status_movie_gc, movie_gc, _ = get(base, "/library/metadata/%s/grandchildren" % c5_movie)
        check("grandchildren: a show's episodes, a season's, an episode itself, a movie none",
              status_gc == 200 and leaves8
              and [r.get("ratingKey") for r in grand8.get("Metadata") or []] == leaves8
              and grand8.get("viewGroup") == "show" and "librarySectionID" not in grand8
              and season_grand == season_kids and episode_grand == [str(c5_episode)]
              and status_movie_gc == 200 and not movie_gc.get("Metadata"),
              "HTTP %s, %d of %d leaves" % (status_gc, len(grand8.get("Metadata") or []), len(leaves8)))

        _, page8 = paged("/library/metadata/%s/children" % c5_show, 0, 20)
        _, single8 = paged("/library/metadata/%s/children" % c5_show, 0, 1)
        all_row8 = (page8.get("Directory") or [{}])[0]
        _, raw8, _ = request(base, "GET", "/library/metadata/%s/children" % c5_show, want_json=False)
        try:
            first8 = list(ET.fromstring(raw8))[0].get("title")
        except (ET.ParseError, IndexError):
            first8 = None
        excluded8 = (get(base, "/library/metadata/%s/children?excludeElements=Media" % c5_show)[1]
                     .get("Metadata") or [])
        check("the client's paged season list keeps All episodes, counted, first in XML",
              all_row8.get("title") == "All episodes" and all_row8.get("leafCount") == len(leaves8)
              and all_row8.get("thumb") == detail_of(c5_show).get("thumb")
              and page8.get("size") == len(page8.get("Metadata") or []) + 1
              and not any("librarySectionID" in r for r in page8.get("Metadata") or [])
              and not single8.get("Directory") and first8 == "All episodes"
              and excluded8 and all(r.get("mediaCount") == 0 and "Media" not in r for r in excluded8),
              "All episodes %r leafCount %s, XML first %r" % (all_row8.get("title"),
                                                           all_row8.get("leafCount"), first8))

        _, pair8, _ = get(base, "/library/metadata/%s,%s" % (c5_show, c5_movie))
        check("a comma-joined read names the FIRST row's library and can sync",
              pair8.get("allowSync") is True and str(pair8.get("librarySectionID")) == str(show_lib)
              and pair8.get("librarySectionUUID"),
              "allowSync %s section %s" % (pair8.get("allowSync"), pair8.get("librarySectionID")))
    else:
        skip("grandchildren: a show's episodes, a season's, an episode itself, a movie none",
             "no show, season and episode in this catalog")

    guid8 = detail_of(c5_movie).get("guid")
    same_guid = sorted(str(r.get("ratingKey")) for r in movie_rows8 if r.get("guid") == guid8)
    by_guid = sorted(str(r.get("ratingKey")) for r in get(base, "/library/all?type=1&guid=%s"
                                                          % quote(guid8))[1].get("Metadata") or [])
    no_guid = get(base, "/library/all?type=1&guid=%s"
                  % quote("plex://movie/000000000000000000000000"))[1].get("Metadata")
    wrong_type = get(base, "/library/all?type=2&guid=%s" % quote(guid8))[1].get("Metadata")
    check("guid= finds the rows whose guid it begins, of the asked type, and nothing else",
          guid8 and by_guid and by_guid == same_guid and not no_guid and not wrong_type,
          "%s: %d found, %d expected" % (guid8, len(by_guid), len(same_guid)))

    status_pref, one_pref, _ = get(base, "/:/prefs/get?id=FriendlyName")
    pref8 = (one_pref.get("Setting") or [{}])[0]
    check("/:/prefs/get reads one setting, unlabelled, and refuses what it cannot find",
          status_pref == 200 and one_pref.get("size") == 1 and pref8.get("id") == "FriendlyName"
          and pref8.get("label") == "" and pref8.get("summary") == "" and "enumValues" not in pref8
          and get(base, "/:/prefs/get")[0] == 400
          and get(base, "/:/prefs/get?id=NoSuchSetting")[0] == 404,
          "HTTP %s, %s" % (status_pref, sorted(pref8)))

    small8 = (get(base, "/hubs/sections/%s/manage?metadataItemId=%s" % (movie_lib, c5_movie))[0],
              get(base, "/neighborhood/devices")[:2],
              get(base, "/channels/recentlyViewed")[1].get("content"),
              get(base, "/media/subscriptions/template?guid=x")[0],
              get(base, "/services/defaultLibraries")[0],
              get(base, "/devices.xml")[1].get("content"))
    check("the small reads the bundle builds answer as a real PMS answers them",
          small8[0] == 400 and small8[1][0] == 200 and small8[1][1].get("size") == 0
          and small8[2] == "plugins" and small8[3] == 400 and small8[4] == 400
          and small8[5] == "plugins",
          str([s if not isinstance(s, tuple) else s[0] for s in small8]))

    # -- cycle 8, second batch: the XML path and the clients that are not Plex Web -----
    def fetch_as(path, headers, want_json=True):
        """A read with extra request headers, in JSON or (no Accept) in XML."""
        sent = {"User-Agent": UA, "X-Plex-Client-Identifier": "plexsim-ui-test",
                "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
        if want_json:
            sent["Accept"] = "application/json"
        sent.update(headers)
        req = urllib.request.Request(base.rstrip("/") + path, headers=sent)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, ({} if want_json else b"")
        if not want_json:
            return status, body
        try:
            return status, json.loads(body).get("MediaContainer") or {}
        except ValueError:
            return status, {}

    movie_path8 = quote("/library/metadata/%s" % c5_movie)
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=playing&time=5000&duration=600000"
            % (c5_movie, movie_path8))
    sessions8 = get(base, "/status/sessions")[1].get("Metadata") or []
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=stopped&time=5000&duration=600000"
            % (c5_movie, movie_path8))
    leaked8 = set()
    for media in detail_of(c5_movie).get("Media") or []:
        leaked8.update(k for k in ("selected",) if k in media)
        for part in media.get("Part") or []:
            leaked8.update(k for k in ("selected", "decision") if k in part)
            for stream in part.get("Stream") or []:
                leaked8.update(k for k in ("location",) if k in stream)
    check("a session's playback flags stay on the session row, not in the catalog",
          sessions8 and not leaked8, "%d session(s), leaked %s" % (len(sessions8), sorted(leaked8)))

    related8 = [r for hub in get(base, "/hubs/metadata/%s/related" % c5_movie)[1].get("Hub") or []
                for r in hub.get("Metadata") or []]
    first_related = related8[0] if related8 else {}
    check("a Related shelf carries the full row: tag ids, streams and its library",
          first_related and "librarySectionID" in first_related
          and any("id" in tag for tag in first_related.get("Genre") or [])
          and any(part.get("Stream") for media in first_related.get("Media") or []
                  for part in media.get("Part") or []),
          "%d related row(s), keys %s" % (len(related8), sorted(k for k in first_related
                                                             if k.startswith("librarySection"))))

    word8 = str(detail_of(c5_movie).get("title") or "").split()[0]
    found8 = [hit.get("Metadata") for hit in get(base, "/library/search?query=%s" % quote(word8))[1]
              .get("SearchResult") or [] if isinstance(hit.get("Metadata"), dict)]
    movies8 = [r for r in found8 if r.get("type") == "movie"]
    check("a /library/search hit is light and names its library by title only",
          movies8 and all(r.get("librarySectionTitle") and "librarySectionID" not in r
                          and "librarySectionKey" not in r and "Guid" not in r
                          and not any("id" in tag for tag in r.get("Genre") or [])
                          for r in movies8),
          "%d movie hit(s) for %r" % (len(movies8), word8))

    recent8 = get(base, "/library/recentlyAdded")[1].get("Metadata") or []
    _, recent_page8 = fetch_as("/library/recentlyAdded",
                               {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "1"})
    recent_seasons = [r for r in recent8 if r.get("type") == "season"]
    earliest8 = None
    if recent_seasons:
        earliest8 = min([e.get("addedAt") or 0 for e in get(
            base, "/library/metadata/%s/children" % recent_seasons[0]["ratingKey"])[1]
            .get("Metadata") or []] or [0])
    check("Recently Added lists new episodes as their season, dated by the first, and pages",
          recent8 and not any(r.get("type") == "episode" for r in recent8)
          and recent_page8.get("size") == 1 and recent_page8.get("totalSize") == len(recent8)
          and recent_page8.get("offset") == 0
          and (not show_lib or (recent_seasons and recent_seasons[0].get("addedAt") == earliest8)),
          "types %s, page %s/%s" % (sorted({str(r.get("type")) for r in recent8}),
                                    recent_page8.get("size"), recent_page8.get("totalSize")))

    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=stopped&time=120000&duration=600000"
            % (c5_movie, movie_path8))
    deck8 = get(base, "/library/onDeck")[1].get("Metadata") or []
    home_deck8 = get(base, "/hubs/home/continueWatching")[1].get("Metadata") or []
    heavy8 = [r.get("title") for r in deck8 + home_deck8
              if "Guid" in r or any("id" in tag for tag in r.get("Genre") or [])
              or any(part.get("Stream") for media in r.get("Media") or []
                     for part in media.get("Part") or [])]
    check("On Deck and Continue Watching rows are light, On Deck's named by UUID",
          deck8 and home_deck8 and not heavy8
          and all("librarySectionKey" not in r and r.get("librarySectionUUID") for r in deck8),
          "%d on deck, %d continue, heavy %s" % (len(deck8), len(home_deck8), heavy8[:2]))

    if c5_show and c5_season:
        season8 = detail_of(c5_season)
        _, season_page8 = fetch_as("/library/metadata/%s/children" % c5_season,
                                   {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "20"})
        if season8.get("year") is not None:
            check("a season's episode list carries the season's own year as parentYear",
                  season_page8.get("parentYear") == season8.get("year"),
                  "parentYear %s, season year %s" % (season_page8.get("parentYear"),
                                                     season8.get("year")))
        else:
            skip("a season's episode list carries the season's own year as parentYear",
                 "the first season has no year of its own")
        check("a show library's collection list carries nocache",
              get(base, "/library/sections/%s/collections" % show_lib)[1].get("nocache") is True,
              "")

        _, tree8, _ = get(base, "/library/metadata/%s/tree" % c5_show)
        show_node = (tree8.get("MetadataItem") or [{}])[0]
        season_node = (show_node.get("MetadataItem") or [{}])[0]
        episode_node = (season_node.get("MetadataItem") or [{}])[0]
        part_node = ((episode_node.get("MediaItem") or [{}])[0].get("MediaPart") or [{}])[0]
        season_root = (get(base, "/library/metadata/%s/tree" % c5_season)[1]
                       .get("MetadataItem") or [{}])[0]
        show_title8 = detail_of(c5_show).get("title")
        check("the tree blanks season titles, types episode parts, lists the show's settings",
              season_node.get("title") == "" and season_node.get("parentTitle") == ""
              and part_node.get("metadataType") == 4
              and [s.get("id") for s in show_node.get("Setting") or []]
              == ["episodeSort", "autoDeletionItemPolicyUnwatchedLibrary",
                  "autoDeletionItemPolicyWatchedLibrary", "flattenSeasons", "showOrdering",
                  "audioLanguage", "subtitleLanguage", "subtitleMode"]
              and all(isinstance(s.get("value"), str) for s in show_node.get("Setting") or [])
              and season_root.get("title") == "" and season_root.get("parentTitle") == show_title8
              and all(e.get("parentTitle") == show_title8
                      for e in season_root.get("MetadataItem") or []),
              "season %r/%r, part type %s, %d setting(s)" % (
                  season_node.get("title"), season_node.get("parentTitle"),
                  part_node.get("metadataType"), len(show_node.get("Setting") or [])))

        _, ios8 = fetch_as("/library/metadata/%s/children" % c5_show, {"X-Plex-Platform": "iOS"})
        _, android8 = fetch_as("/library/metadata/%s/children" % c5_show,
                               {"X-Plex-Platform": "Android"})
        check("the iOS platform types All episodes as a season, and no other does",
              (ios8.get("Directory") or [{}])[0].get("type") == "season"
              and (android8.get("Directory") or [{}])[0].get("title") == "All episodes"
              and "type" not in (android8.get("Directory") or [{}])[0],
              "iOS %s, Android %s" % ((ios8.get("Directory") or [{}])[0].get("type"),
                                      (android8.get("Directory") or [{}])[0].get("type")))
    else:
        skip("the tree blanks season titles, types episode parts, lists the show's settings",
             "no show and season in this catalog")

    random8 = [get(base, "/library/sections/%s/all?type=1&sort=%s" % (movie_lib, spec))[0]
               for spec in ("movie.random", "movie.random:desc", "movie.random:42")]
    check("a type-prefixed random sort sorts, as python-plexapi sends it",
          random8 == [200, 200, 200], str(random8))

    # The fixture's rows carry no studio, so give one a studio through the Edit
    # dialog's own write first; a check that skips on an empty table proves nothing.
    studio8 = "Cycle8 Studio"
    request(base, "PUT", "/library/metadata/%s?type=1&studio.value=%s&studio.locked=1"
            % (c5_movie, quote(studio8)))
    studios8 = get(base, "/library/tags?type=318")[1].get("Directory") or []
    studio_row = next((t for t in studios8 if t.get("tag") == studio8), {})
    check("studios are a tag table: tagType 318 with a bare =id filter",
          detail_of(c5_movie).get("studio") == studio8
          and studio_row.get("tagType") == 318
          and studio_row.get("filter") == "=%s" % studio_row.get("id"),
          "%d studio(s), %s" % (len(studios8), studio_row))

    stream8 = next((s.get("id") for media in detail_of(c5_movie).get("Media") or []
                    for part in media.get("Part") or [] for s in part.get("Stream") or []), None)
    check("a stream's levels are a 404 where the bare stream is a 501",
          stream8 and get(base, "/library/streams/%s/levels" % stream8)[0] == 404
          and get(base, "/library/streams/%s" % stream8)[0] == 501,
          "stream %s" % stream8)

    order8 = ["Media", "Image", "UltraBlurColors", "Genre", "Country", "Guid", "Rating",
              "Collection", "Director", "Writer", "Role", "Producer", "Label",
              "CommonSenseMedia", "Field", "Location", "Preferences", "OnDeck", "Extras",
              "Related", "Children"]
    _, detail_xml8 = fetch_as("/library/metadata/%s?includeExtras=1&includePreferences=1"
                              % c5_movie, {}, want_json=False)
    _, listing_xml8 = fetch_as("/library/sections/%s/all?includeMeta=1" % movie_lib, {},
                               want_json=False)
    try:
        tags8 = []
        for child in list(ET.fromstring(detail_xml8))[0]:
            if not tags8 or tags8[-1] != child.tag:
                tags8.append(child.tag)
        first_child8 = list(ET.fromstring(listing_xml8))[0].tag
    except (ET.ParseError, IndexError):
        tags8, first_child8 = [], None
    ranked8 = [t for t in tags8 if t in order8]
    check("XML children come in a real PMS's order, and a listing's Meta first",
          ranked8 and ranked8 == sorted(ranked8, key=order8.index)
          and len(set(ranked8)) == len(ranked8) and first_child8 == "Meta",
          "%s; listing starts %s" % (",".join(tags8), first_child8))

    # -- cycle 9: the community OpenAPI description, every GET in it ----------------
    status_all_sections, all_sections, _ = get(base, "/library/sections/all")
    check("/library/sections/all is /library/sections",
          status_all_sections == 200 and all_sections
          and all_sections == get(base, "/library/sections")[1],
          "HTTP %s" % status_all_sections)

    prefs_all9 = get(base, "/:/prefs")[1].get("Setting") or []
    _, prefs_page9 = paged("/:/prefs", 2, 2)
    _, identity_page9 = paged("/identity", 0, 1)
    _, scanners_page9 = paged("/system/scanners/1", 0, 1)
    check("every container pages, /:/prefs and /identity included, a plug-in's not",
          prefs_page9.get("size") == 2 and prefs_page9.get("totalSize") == len(prefs_all9)
          and prefs_page9.get("offset") == 2
          and [s.get("id") for s in prefs_page9.get("Setting") or []]
          == [s.get("id") for s in prefs_all9[2:4]]
          and identity_page9.get("totalSize") == 0 and identity_page9.get("offset") == 0
          and scanners_page9.get("Scanner") and "totalSize" not in scanners_page9,
          "prefs %s/%s, identity %s, scanners %s" % (
              prefs_page9.get("size"), prefs_page9.get("totalSize"),
              identity_page9.get("totalSize"), scanners_page9.get("totalSize")))

    agent_statuses9 = [get(base, "/system/agents/%s" % name)[0]
                       for name in ("tv.plex.agents.movie", "nosuch.agent")]
    check("one agent by identifier is a 404, the agent list a 200",
          agent_statuses9 == [404, 404] and get(base, "/system/agents")[0] == 200,
          str(agent_statuses9))

    stream9 = next((s.get("id") for media in detail_of(c5_movie).get("Media") or []
                    for part in media.get("Part") or [] for s in part.get("Stream") or []), None)
    refusals9 = (get(base, "/library/streams/%s/loudness" % stream9)[0],
                 get(base, "/library/streams/%s.srt" % stream9)[0],
                 get(base, "/library/streams/%s.vtt" % stream9)[0],
                 get(base, "/library/streams/999999999.srt")[0],
                 get(base, "/library/metadata/%s/file" % c5_movie)[0])
    check("loudness 404s, a sidecar extension 501s like its stream, a bare file read 400s",
          stream9 and refusals9 == (404, 501, 501, 404, 400), str(refusals9))

    livetv9 = [get(base, path)[0] for path in (
        "/livetv/epg", "/livetv/epg/guide", "/livetv/epg/search", "/livetv/recordings",
        "/livetv/dvrs/1", "/livetv/dvrs/1/guide", "/livetv/sessions/1",
        "/livetv/sessions/1/index.m3u8", "/livetv/epg/channels", "/livetv/dvrs", "/livetv/sessions")]
    check("livetv refuses the guide, recordings and named DVRs; its two roots are empty",
          livetv9 == [404] * 8 + [400, 200, 200], str(livetv9))

    if c5_show and c5_season and c5_episode:
        _, season_leaves9, _ = get(base, "/library/metadata/%s/allLeaves" % c5_season)
        _, episode_leaves9, _ = get(base, "/library/metadata/%s/allLeaves" % c5_episode)
        _, show_children9, _ = get(base, "/library/metadata/%s/children" % c5_show)
        season_title9 = detail_of(c5_season).get("title")
        check("a season's allLeaves is empty, an episode's is its season's; child lists sync",
              season_leaves9.get("size") == 0 and not season_leaves9.get("Metadata")
              and season_leaves9.get("viewGroup") == "episode"
              and season_leaves9.get("allowSync") is True
              and "grandparentRatingKey" not in season_leaves9
              and episode_leaves9.get("viewGroup") == "episode"
              and episode_leaves9.get("viewMode") == 65592
              and episode_leaves9.get("title1") == season_title9
              and episode_leaves9.get("grandparentTitle") == ""
              and show_children9.get("allowSync") is True,
              "season %s/%s, episode %s/%r" % (
                  season_leaves9.get("size"), season_leaves9.get("viewGroup"),
                  episode_leaves9.get("viewGroup"), episode_leaves9.get("title1")))

    if c5_show:
        word9 = str(detail_of(c5_show).get("title") or "").split()[-1][:4]
        voice_all9 = get(base, "/hubs/search/voice?query=%s" % quote(word9))[1].get("Hub") or []
        voice_shows9 = get(base, "/hubs/search/voice?type=2&query=%s" % quote(word9))[1].get("Hub") or []
        voice_music9 = get(base, "/hubs/search/voice?type=8&query=%s" % quote(word9))[1].get("Hub") or []
        check("voice search keeps the asked type's hub, identified by its type number",
              any(h.get("type") == "show" and h.get("hubIdentifier") == "results.search.2"
                  for h in voice_all9)
              and [h.get("hubIdentifier") for h in voice_shows9] == ["results.search.2"]
              and not voice_music9,
              "%s / %s / %d" % ([h.get("hubIdentifier") for h in voice_all9],
                                [h.get("hubIdentifier") for h in voice_shows9], len(voice_music9)))

    trimmed9 = (get(base, "/library/sections/%s/all?type=1&excludeElements=Media,Genre" % movie_lib)[1]
                .get("Metadata") or [])
    shows_trimmed9 = (get(base, "/library/sections/%s/all?type=2&excludeElements=Media" % show_lib)[1]
                      .get("Metadata") or []) if show_lib else [{"mediaCount": 0}]
    check("a section listing drops excluded elements, counting Media instead",
          trimmed9 and all("Media" not in r and "Genre" not in r and r.get("mediaCount", 0) >= 1
                           and r.get("mediaCountOptimized") == 0 for r in trimmed9)
          and shows_trimmed9 and all(r.get("mediaCount") == 0 for r in shows_trimmed9),
          "%d movie row(s), %d show row(s)" % (len(trimmed9), len(shows_trimmed9)))

    band9 = {q: get(base, "/statistics/bandwidth?timespan=6%s" % q)
             for q in ("", "&accountID=999", "&deviceID=999", "&lan=0", "&lan=1", "&lan=true", "&deviceID=")}
    band_rows9 = {q: len(body.get("StatisticsBandwidth") or []) for q, (_, body, _) in band9.items()}
    band_lan9 = {bool(r.get("lan")) for r in band9[""][1].get("StatisticsBandwidth") or []}
    check("bandwidth statistics filter by account, device and lan, and refuse bad values",
          band_rows9[""] > 0 and band_rows9["&accountID=999"] == 0 and band_rows9["&deviceID=999"] == 0
          and band_rows9["&lan=1"] == sum(1 for r in band9[""][1].get("StatisticsBandwidth") or [] if r.get("lan"))
          and band_rows9["&lan=0"] == sum(1 for r in band9[""][1].get("StatisticsBandwidth") or [] if not r.get("lan"))
          and band9["&lan=true"][0] == 400 and band9["&deviceID="][0] == 400,
          "%s lan=%s" % (band_rows9, sorted(band_lan9)))

    history9 = get(base, "/status/sessions/history/all?type=1")[1].get("Metadata") or []
    if not history9:
        request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=stopped&time=590000&duration=600000"
                % (c5_movie, quote("/library/metadata/%s" % c5_movie)))
        history9 = get(base, "/status/sessions/history/all?type=1")[1].get("Metadata") or []
    if history9:
        status_item9, item9, _ = get(base, history9[0]["historyKey"])
        one9 = (item9.get("Metadata") or [{}])[0]
        # Re-measured in cycle 11 on kids (1.43.4) and zen (1.42.2), against every
        # readable row on each: the single read is NOT the row history/all lists. It is a
        # nine-attribute projection carrying `viewType: 1` and no `key`, `ratingKey` or
        # artwork. This check asserted the two were equal.
        check("one history row reads back by its historyKey, as a projection",
              status_item9 == 200 and item9.get("size") == 1
              and one9.get("historyKey") == history9[0]["historyKey"]
              and one9.get("viewType") == 1
              and one9.get("title") == history9[0].get("title")
              and not {"key", "ratingKey", "thumb"} & set(one9)
              and get(base, "/status/sessions/history/999999999")[0] == 404,
              "HTTP %s for %s -> %s" % (status_item9, history9[0]["historyKey"], sorted(one9)))
    else:
        skip("one history row reads back by its historyKey", "no history row could be made")

    status_item_hubs9, item_hubs9, _ = get(base, "/hubs/metadata/%s" % c5_movie)
    hubs_rows9 = item_hubs9.get("Hub") or []
    check("an item's own hub list is the four empty artist shelves keyed to it",
          status_item_hubs9 == 200 and item_hubs9.get("allowSync") is True
          and [h.get("hubIdentifier") for h in hubs_rows9]
          == ["artist.mostplayedtracks", "artist.mostpopulartracks", "artist.albums",
              "music.videos.popular.new"]
          and all(h.get("size") == 0 and "artist.id=%s" % c5_movie in h.get("key", "") for h in hubs_rows9)
          and get(base, "/hubs/metadata/999999999")[0] == 404,
          "HTTP %s, %s" % (status_item_hubs9, [h.get("hubIdentifier") for h in hubs_rows9]))

    movie_leaves9 = get(base, "/library/sections/%s/allLeaves" % movie_lib)[1]
    movie_all9 = get(base, "/library/sections/%s/all?type=1" % movie_lib)[1].get("Metadata") or []
    leaf_statuses9 = (get(base, "/library/sections/%s/computePath" % movie_lib)[0],
                      get(base, "/library/sections/%s/computePath?startID=%s&endID=%s"
                          % (movie_lib, c5_movie, c5_movie))[0],
                      get(base, "/library/sections/%s/computePath?startID=%s&endID=999999999"
                          % (movie_lib, c5_movie))[0],
                      get(base, "/library/sections/%s/nearest" % movie_lib)[0])
    albums9 = get(base, "/library/sections/%s/albums" % movie_lib)[1]
    moment9 = get(base, "/library/sections/%s/moment" % movie_lib)[1]
    show_leaves9 = get(base, "/library/sections/%s/allLeaves" % show_lib)[1] if show_lib else {}
    check("a section's allLeaves, albums, moment and computePath answer as a real PMS's",
          [r.get("ratingKey") for r in movie_leaves9.get("Metadata") or []]
          == [r.get("ratingKey") for r in movie_all9]
          and movie_leaves9.get("viewGroup") == "movie" and "librarySectionID" not in movie_leaves9
          and (not show_lib or ({r.get("type") for r in show_leaves9.get("Metadata") or []} == {"episode"}
                                and show_leaves9.get("viewGroup") == "show"))
          and albums9.get("viewGroup") == "album" and albums9.get("size") == 0
          and moment9.get("clusterZoomLevel") == 1 and moment9.get("content") == "secondary"
          and leaf_statuses9 == (400, 200, 404, 400),
          "leaves %d/%d, statuses %s" % (len(movie_leaves9.get("Metadata") or []), len(movie_all9),
                                         leaf_statuses9))

    role9 = next((t for t in detail_of(c5_movie).get("Role") or [] if t.get("id")), {})
    if role9:
        _, person9, _ = get(base, "/library/people/%s" % role9["id"])
        _, person_media9, _ = get(base, "/library/people/%s/media" % role9["id"])
        person_row9 = (person9.get("Directory") or [{}])[0]
        check("a person reads back as its tag row, and /media lists what they are on",
              person_row9.get("tag") == role9.get("tag") and person_row9.get("tagType") == 6
              and person_row9.get("filter") == "actor=%s" % role9["id"]
              and str(c5_movie) in [str(r.get("ratingKey")) for r in person_media9.get("Metadata") or []]
              and person_media9.get("viewGroup") == "movie"
              and all(r.get("librarySectionID") for r in person_media9.get("Metadata") or [])
              and get(base, "/library/people/999999999")[0] == 404,
              "%s: %r, %d item(s)" % (role9["id"], person_row9.get("filter"),
                                      len(person_media9.get("Metadata") or [])))
    else:
        skip("a person reads back as its tag row, and /media lists what they are on", "no role on the movie")

    movie9 = detail_of(c5_movie)
    title9, year9, guid9 = movie9.get("title"), movie9.get("year"), movie9.get("guid")
    def matched9(spec):
        status, body, _ = get(base, "/library/matches?%s" % spec)
        return status, [(str(r.get("ratingKey")), r.get("score")) for r in body.get("Metadata") or []]
    exact9 = matched9("type=1&title=%s&year=%s" % (quote(str(title9).upper()), year9))
    partial9 = matched9("type=1&title=%s" % quote(str(title9).split()[0]))
    wrong_year9 = matched9("type=1&title=%s&year=1900" % quote(title9))
    by_guid9 = matched9("type=1&guid=%s" % quote(guid9))
    check("/library/matches finds a title exactly (scored) or a guid, and refuses no type",
          exact9[0] == 200 and (str(c5_movie), 100) in exact9[1]
          and partial9 == (200, []) and wrong_year9 == (200, [])
          and (str(c5_movie), None) in by_guid9[1]
          and matched9("type=1") == (200, []) and get(base, "/library/matches?title=%s" % quote(title9))[0] == 400,
          "exact %s partial %s guid %s" % (exact9, partial9[1][:2], by_guid9[1][:2]))

    section_hubs9 = get(base, "/hubs/sections/%s?count=100" % movie_lib)[1].get("Hub") or []
    added_hub9 = next((h for h in section_hubs9 if str(h.get("hubIdentifier", "")).startswith("movie.recentlyadded")), {})
    if added_hub9:
        status_items9, items9, _ = get(base, "/hubs/items?identifier=%s" % added_hub9["hubIdentifier"])
        capped9 = get(base, "/hubs/items?identifier=%s&count=1" % added_hub9["hubIdentifier"])[1]
        check("/hubs/items answers one shelf's rows by identifier, and 404s the rest",
              status_items9 == 200 and items9.get("allowSync") is True
              and [r.get("ratingKey") for r in items9.get("Metadata") or []]
              == [r.get("ratingKey") for r in added_hub9.get("Metadata") or []]
              and capped9.get("size") == 1
              and get(base, "/hubs/items?identifier=nosuch.hub")[0] == 404
              and get(base, "/hubs/items")[0] == 404
              and (not show_lib or get(base, "/hubs/items?identifier=tv.inprogress.%s" % show_lib)[0] == 404),
              "HTTP %s, %d row(s)" % (status_items9, len(items9.get("Metadata") or [])))
    else:
        skip("/hubs/items answers one shelf's rows by identifier, and 404s the rest", "no recently added shelf")

    def sort_keys9(library, spec):
        status, body, _ = get(base, "/library/sections/%s/sorts?%s" % (library, spec))
        return status, [d.get("key") for d in body.get("Directory") or []]
    episode_sorts9 = sort_keys9(movie_lib, "type=4")
    artist_sorts9 = sort_keys9(movie_lib, "type=8")
    movie_sorts_on_show9 = sort_keys9(show_lib, "type=1") if show_lib else (200, ["titleSort"] * 9)
    check("/sorts is the asked TYPE's list on any library, and a non-number is a 400",
          len(episode_sorts9[1]) == 7 and str(episode_sorts9[1][0]).startswith("show.titleSort,season.index")
          and artist_sorts9[1][:2] == ["titleSort", "userRating"]
          and len(movie_sorts_on_show9[1]) == 9
          and sort_keys9(movie_lib, "type=5") == (200, [])
          and sort_keys9(movie_lib, "type=movie")[0] == 400,
          "episode %d, artist %s, movie-on-show %d" % (len(episode_sorts9[1]), artist_sorts9[1][:2],
                                                       len(movie_sorts_on_show9[1])))

    with_art9 = [r for r in get(base, "/library/sections/%s/all?type=1" % movie_lib)[1].get("Metadata") or []
                 if r.get("art")]
    random9 = get(base, "/library/randomArtwork?sections=%s" % movie_lib)[1].get("Metadata") or []
    arts9 = get(base, "/library/sections/%s/arts" % movie_lib)[1]
    _, arts_xml9 = fetch_as("/library/sections/%s/arts" % movie_lib, {}, want_json=False)
    try:
        arts_tags9 = {child.tag for child in ET.fromstring(arts_xml9)}
    except ET.ParseError:
        arts_tags9 = set()
    # The order is the ROWS' ratingKey order. The id inside an art URL is not the row's:
    # after a Change Art write a row can point at another item's image.
    art_keys9 = [r.get("key") for r in arts9.get("Metadata") or []]
    expected_art9 = [r.get("art") for r in sorted(with_art9, key=lambda r: int(r.get("ratingKey")))]
    check("randomArtwork and a library's arts list one Photo per backdrop",
          len(random9) == len(with_art9) and with_art9
          and all(r.get("type") == "image" and r.get("rating") == -1 for r in random9)
          and get(base, "/library/randomArtwork")[1].get("size") == 0
          and art_keys9 == expected_art9
          and arts_tags9 == {"Photo"},
          "random %d, arts %d of %d, xml %s" % (len(random9), len(art_keys9), len(with_art9), sorted(arts_tags9)))

    # Measured on plexscratch, a writable copy of the reference: the Edit dialog's own
    # save path MERGES tags -- the indexed list first, in INDEX order and deduplicated
    # case-insensitively, then the row's other tags -- and a `tag-` removal there
    # removes only the (double-encoded) names it lists. The item path replaces.
    # plexsim replaced on both paths, in parameter order.
    def genres_w1(key):
        return [t.get("tag") for t in (get(base, "/library/metadata/%s" % key)[1]
                                       .get("Metadata") or [{}])[0].get("Genre") or []]
    request(base, "PUT", "/library/metadata/%s?genre[0].tag.tag=%s&genre[1].tag.tag=%s"
                         % (movie, quote("Keep One"), quote("Keep Two")))
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&genre[1].tag.tag=Second"
                         "&genre[0].tag.tag=First&genre[2].tag.tag=first" % (movie_section, movie))
    merged_w1 = genres_w1(movie)
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&genre[].tag.tag-=%s"
                         % (movie_section, movie, quote(quote("keep one") + "," + quote("Not There"))))
    removed_w1 = genres_w1(movie)
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&genre[0].tag.tag="
                         % (movie_section, movie))
    empty_w1 = genres_w1(movie)
    request(base, "PUT", "/library/metadata/%s?genre[1].tag.tag=Zeta&genre[0].tag.tag=Eta" % movie)
    replaced_w1 = genres_w1(movie)
    check("the section save path merges tags and removes only what it names; the item path replaces",
          merged_w1 == ["First", "Second", "Keep One", "Keep Two"]
          and removed_w1 == ["First", "Second", "Keep Two"] and empty_w1 == removed_w1
          and replaced_w1 == ["Eta", "Zeta"],
          "%s / %s / %s / %s" % (merged_w1, removed_w1, empty_w1, replaced_w1))

    # Measured on plexscratch: a Sort Title that is not LOCKED does not stick -- a title
    # write, a sort-title write and an unlock all re-derive it from the title, article
    # stripped -- and only a lock keeps what was typed. plexsim stored whatever came.
    def row_w2(key):
        return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
    title_w2 = row_w2(movie).get("title")
    request(base, "PUT", "/library/metadata/%s?title.value=%s&titleSort.value=zz"
                         % (movie, quote("The Sorted One")))
    derived_w2 = row_w2(movie).get("titleSort")
    request(base, "PUT", "/library/metadata/%s?titleSort.value=kept&titleSort.locked=1" % movie)
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&title.value=%s"
                         % (movie_section, movie, quote("An Sorted Two")))
    kept_w2 = row_w2(movie).get("titleSort")
    request(base, "PUT", "/library/metadata/%s?titleSort.locked=0" % movie)
    unlocked_w2 = row_w2(movie).get("titleSort")
    request(base, "PUT", "/library/metadata/%s?title.value=%s" % (movie, quote(str(title_w2))))
    check("an unlocked Sort Title is re-derived from the title; only a lock keeps it",
          derived_w2 == "Sorted One" and kept_w2 == "kept" and unlocked_w2 == "Sorted Two",
          "%r / %r / %r" % (derived_w2, kept_w2, unlocked_w2))

    # Measured on plexscratch: the save path edits only rows of the asked TYPE in ITS
    # library -- a movie id under `type=2`, or on another library's path, is a 200 that
    # edits nothing -- and a type-scoped filter there is a 400. plexsim edited every id
    # it had and applied the filter.
    if show and show_section:
        status_type_w3, _, _ = request(base, "PUT", "/library/sections/%s/all?type=2&id=%s"
                                                   "&tagline.value=w3a" % (movie_section, movie))
        status_lib_w3, _, _ = request(base, "PUT", "/library/sections/%s/all?type=1&id=%s"
                                                  "&tagline.value=w3b" % (show_section, movie))
        request(base, "PUT", "/library/sections/%s/all?type=1&id=%s,%s&summary.value=w3c"
                             % (movie_section, movie, show))
        status_scope_w3, _, _ = container(base, "PUT", "/library/sections/%s/all?type=1"
                                                      "&movie.year%%3E%%3E=1900&id=%s&tagline.value=w3d"
                                                      % (movie_section, movie))
        movie_w3 = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
        show_w3 = (get(base, "/library/metadata/%s" % show)[1].get("Metadata") or [{}])[0]
        check("the save path edits only its own type and library, and refuses a type-scoped filter",
              (status_type_w3, status_lib_w3, status_scope_w3) == (200, 200, 400)
              and movie_w3.get("tagline") not in ("w3a", "w3b", "w3d")
              and movie_w3.get("summary") == "w3c" and show_w3.get("summary") != "w3c",
              "HTTP %s/%s/%s, tagline %r, show summary %r"
              % (status_type_w3, status_lib_w3, status_scope_w3, movie_w3.get("tagline"),
                 show_w3.get("summary")))
    else:
        skip("the save path edits only its own type and library, and refuses a type-scoped filter",
             "need a show library")

    # Measured on plexscratch: a release date is PARSED (a day past the month's end
    # rolls over, a bare integer is epoch seconds, a trailing time is dropped), and on
    # the section path it sets the year, clearing it removes both, and a date it cannot
    # read is a 500 -- the item path leaves the year alone. A year of 0 is removed and
    # `+2000` is 2000. plexsim stored dates verbatim and never touched the year.
    def dated_w4():
        row = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
        return row.get("originallyAvailableAt"), row.get("year")
    kept_w4 = dated_w4()
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&originallyAvailableAt.value=%s"
                         % (movie_section, movie, quote("2003-04-05T23:30:00Z")))
    section_w4 = dated_w4()
    request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=2007-02-30" % movie)
    rolled_w4 = dated_w4()
    request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=20070203" % movie)
    epoch_w4 = dated_w4()
    bad_w4, _, _ = container(base, "PUT", "/library/sections/%s/all?type=1&id=%s"
                                          "&originallyAvailableAt.value=2001-13-45"
                                          % (movie_section, movie))
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&originallyAvailableAt.value="
                         % (movie_section, movie))
    cleared_w4 = dated_w4()
    request(base, "PUT", "/library/metadata/%s?year.value=%%2B2000" % movie)
    plus_w4 = dated_w4()
    request(base, "PUT", "/library/metadata/%s?year.value=0" % movie)
    zero_w4 = dated_w4()
    if kept_w4[1] is not None:
        request(base, "PUT", "/library/metadata/%s?year.value=%s" % (movie, kept_w4[1]))
    if kept_w4[0] is not None:
        request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s"
                             % (movie, quote(str(kept_w4[0]))))
    check("a release date is parsed, sets the year on the section path, and a bad one is a 500 there",
          section_w4 == ("2003-04-05", 2003) and rolled_w4 == ("2007-03-02", 2003)
          and epoch_w4 == ("1970-08-21", 2003) and bad_w4 == 500
          and cleared_w4 == (None, None) and plus_w4 == (None, 2000) and zero_w4 == (None, None),
          "%s %s %s HTTP %s %s %s %s" % (section_w4, rolled_w4, epoch_w4, bad_w4, cleared_w4,
                                         plus_w4, zero_w4))

    # Measured on plexscratch: the section save path STORES a userRating, stamping
    # lastRatedAt (-1 clears both), where the item path discards it; audienceRating and
    # rating are numbers, and an empty one is a 500 on the item path and removed on the
    # section path. plexsim kept the strings and discarded userRating on both paths.
    def rated_w5():
        return (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    audience_w5 = rated_w5().get("audienceRating")
    request(base, "PUT", "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=-1" % movie)
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&userRating.value=8.5"
                         % (movie_section, movie))
    stored_w5 = rated_w5()
    request(base, "PUT", "/library/metadata/%s?userRating.value=3" % movie)
    ignored_w5 = rated_w5().get("userRating")
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&userRating.value=-1"
                         % (movie_section, movie))
    cleared_w5 = rated_w5()
    request(base, "PUT", "/library/metadata/%s?audienceRating.value=5.5" % movie)
    number_w5 = rated_w5().get("audienceRating")
    empty_item_w5, _, _ = container(base, "PUT", "/library/metadata/%s?audienceRating.value=" % movie)
    bad_w5, _, _ = container(base, "PUT", "/library/sections/%s/all?type=1&id=%s&userRating.value=abc"
                                          % (movie_section, movie))
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&audienceRating.value="
                         % (movie_section, movie))
    removed_w5 = "audienceRating" not in rated_w5()
    if audience_w5 is not None:
        request(base, "PUT", "/library/metadata/%s?audienceRating.value=%s" % (movie, audience_w5))
    check("the section save path stores a userRating; a rating is a number, empty a 500 on the item path",
          stored_w5.get("userRating") == 8.5 and stored_w5.get("lastRatedAt") is not None
          and ignored_w5 == 8.5 and "userRating" not in cleared_w5
          and "lastRatedAt" not in cleared_w5 and isinstance(number_w5, float) and number_w5 == 5.5
          and empty_item_w5 == 500 and bad_w5 == 500 and removed_w5,
          "stored %r/%r, item write %r, cleared %r, number %r, empty %s, abc %s, removed %s"
          % (stored_w5.get("userRating"), stored_w5.get("lastRatedAt"), ignored_w5,
             cleared_w5.get("userRating"), number_w5, empty_item_w5, bad_w5, removed_w5))

    # Measured on plexscratch: the section path parses a lock as an integer 0 or 1 (`01`
    # locks; `true` and `2` are a 400), `thumb` and the other artwork and identity
    # columns take a lock (`key` does not), and `Field` lists in the metadata table's
    # column order. plexsim unlocked on anything and 400'd `thumb`.
    statuses_w6 = [container(base, "PUT", "/library/sections/%s/all?type=1&id=%s&studio.locked=%s"
                                          % (movie_section, movie, value))[0]
                   for value in ("true", "2")]
    request(base, "PUT", "/library/sections/%s/all?type=1&id=%s&studio.locked=01"
                         % (movie_section, movie))
    thumb_w6, _, _ = request(base, "PUT", "/library/metadata/%s?thumb.locked=1" % movie)
    key_w6, _, _ = container(base, "PUT", "/library/metadata/%s?key.locked=1" % movie)
    request(base, "PUT", "/library/metadata/%s?userRating.locked=1&genre.locked=1"
                         "&rating.locked=1&title.locked=1" % movie)
    locked_w6 = ("title", "studio", "rating", "thumb", "genre", "userRating")
    order_w6 = [f.get("name") for f in (get(base, "/library/metadata/%s" % movie)[1]
                                        .get("Metadata") or [{}])[0].get("Field") or []
                if f.get("locked") and f.get("name") in locked_w6]
    request(base, "PUT", "/library/metadata/%s?%s"
                         % (movie, "&".join("%s.locked=0" % name for name in locked_w6)))
    check("a section-path lock is an integer 0 or 1, thumb takes a lock, and locks list in column order",
          statuses_w6 == [400, 400] and thumb_w6 == 200 and key_w6 == 400
          and order_w6 == list(locked_w6),
          "HTTP %s, thumb %s, key %s, order %s" % (statuses_w6, thumb_w6, key_w6, order_w6))

    # Measured on plexscratch: a frame titles a show with its year, a season `<show> S19`
    # and an episode `<show> S19 E09`; a section save pushes every row's state 3 before any
    # state 5, in sort-title order; and renaming a playlist or a collection pushes the
    # list's own type-15 or type-18 pair. plexsim titled rows by their own title,
    # interleaved the batch and pushed nothing for a list.
    def entries_w8(trigger):
        _, frames = notifications(base, trigger, seconds=3)
        return [(e.get("type"), e.get("state"), str(e.get("itemID")), e.get("title"))
                for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    def row_w8(key):
        return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
    if show and spare:
        show_row_w8 = row_w8(show)
        season_key_w8 = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                              if r.get("type") == "season"), {}).get("ratingKey")
        episode_row_w8 = row_w8((get(base, "/library/metadata/%s/children" % season_key_w8)[1]
                                 .get("Metadata") or [{}])[0].get("ratingKey"))
        episode_w8 = entries_w8(lambda: request(base, "PUT", "/library/metadata/%s?summary.value=w8"
                                                          % episode_row_w8.get("ratingKey")))
        show_w8 = entries_w8(lambda: request(base, "PUT", "/library/metadata/%s?summary.value=w8" % show))
        # Two rows whose sort titles DIFFER, named in reverse: by here earlier checks can
        # leave two movies with one title, and a tie is ordered by something this check
        # does not pin.
        listing_w8 = get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or [{}]
        sort_w8 = lambda r: str(r.get("titleSort") or r.get("title") or "").lower()
        pair_w8 = sorted([listing_w8[0]] + [r for r in listing_w8 if sort_w8(r) != sort_w8(listing_w8[0])][:1],
                         key=sort_w8)
        batch_w8 = entries_w8(lambda: request(base, "PUT", "/library/sections/%s/all?type=1&id=%s"
                                                          "&summary.value=w8"
                                                          % (movie_section, ",".join(
                                                              str(r["ratingKey"]) for r in reversed(pair_w8)))))
        uri_w8 = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))
        _, made_w8, _ = container(base, "POST", "/playlists?type=video&smart=0&title=w8list&uri=%s" % uri_w8)
        list_w8 = str((made_w8.get("Metadata") or [{}])[0].get("ratingKey"))
        playlist_w8 = entries_w8(lambda: request(base, "PUT", "/playlists/%s?title=w8renamed" % list_w8))
        request(base, "DELETE", "/playlists/%s" % list_w8)
        _, made_w8, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s&title=w8coll&uri=%s"
                                                % (movie_section, uri_w8))
        coll_w8 = str((made_w8.get("Metadata") or [{}])[0].get("ratingKey"))
        collection_w8 = entries_w8(lambda: request(base, "PUT", "/library/collections/%s?title=w8collrenamed"
                                                               % coll_w8))
        request(base, "DELETE", "/library/collections/%s" % coll_w8)
        episode_title_w8 = "%s S%02d E%02d" % (show_row_w8.get("title"), int(episode_row_w8.get("parentIndex") or 0),
                                               int(episode_row_w8.get("index") or 0))
        show_title_w8 = "%s (%s)" % (show_row_w8.get("title"), show_row_w8.get("year"))
        keys_w8 = [str(r["ratingKey"]) for r in pair_w8]
        # Only the frames for the rows each write touched: another item's earlier work
        # can land a frame in the same window.
        episode_w8 = [e for e in episode_w8 if e[2] == str(episode_row_w8.get("ratingKey"))]
        show_w8 = [e for e in show_w8 if e[2] == str(show)]
        batch_w8 = [e for e in batch_w8 if e[2] in keys_w8]
        check("a frame names a show with its year and an episode by S and E; a batch and a list frame as measured",
              [e[3] for e in episode_w8] == [episode_title_w8] * 2 and [e[3] for e in show_w8] == [show_title_w8] * 2
              and [(e[1], e[2]) for e in batch_w8] == [(3, k) for k in keys_w8] + [(5, k) for k in keys_w8]
              and playlist_w8 == [(15, 3, list_w8, "w8renamed"), (15, 5, list_w8, "w8renamed")]
              and collection_w8 == [(18, 3, coll_w8, "w8collrenamed"), (18, 5, coll_w8, "w8collrenamed")],
              "%s want %r / %s want %r / %s want %s by %s / %s / %s"
              % ([e[3] for e in episode_w8], episode_title_w8, [e[3] for e in show_w8], show_title_w8,
                 [(e[1], e[2]) for e in batch_w8], keys_w8,
                 [(r.get("titleSort"), r.get("title")) for r in pair_w8], playlist_w8, collection_w8))
    else:
        skip("a frame names a show with its year and an episode by S and E; a batch and a list frame as measured",
             "need a show and a second movie")

    # Measured on plexscratch: a parent's title and studio are COPIED onto its children --
    # a season's onto its episodes' parentTitle/parentStudio, a show's onto its seasons'
    # parentTitle/parentStudio and every episode's grandparentTitle, an emptied show title
    # dropping them -- and a season's episode list names a custom season title and carries
    # the season's summary. plexsim copied them once, when the catalog loaded.
    if show:
        def row_w7(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
        season_w7 = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                          if r.get("type") == "season"), {}).get("ratingKey")
        episode_w7 = (get(base, "/library/metadata/%s/children" % season_w7)[1]
                      .get("Metadata") or [{}])[0].get("ratingKey")
        show_was_w7, season_was_w7 = row_w7(show), row_w7(season_w7)
        request(base, "PUT", "/library/metadata/%s?title.value=%s&studio.value=%s&summary.value=%s"
                             % (season_w7, quote("Named Season"), quote("Season Studio"), quote("Season words")))
        request(base, "PUT", "/library/metadata/%s?title.value=%s&studio.value=%s"
                             % (show, quote("Renamed Show"), quote("Show Studio")))
        episode_now_w7, season_now_w7 = row_w7(episode_w7), row_w7(season_w7)
        children_w7 = get(base, "/library/metadata/%s/children" % season_w7)[1]
        request(base, "PUT", "/library/metadata/%s?title.value=" % show)
        episode_bare_w7, season_bare_w7 = row_w7(episode_w7), row_w7(season_w7)
        request(base, "PUT", "/library/metadata/%s?title.value=%s&studio.value=%s"
                             % (show, quote(str(show_was_w7.get("title"))), quote(str(show_was_w7.get("studio") or ""))))
        request(base, "PUT", "/library/metadata/%s?title.value=%s&studio.value=%s&summary.value=%s"
                             % (season_w7, quote(str(season_was_w7.get("title"))),
                                quote(str(season_was_w7.get("studio") or "")),
                                quote(str(season_was_w7.get("summary") or ""))))
        check("a parent's title and studio reach its children, and a season list names a custom title",
              episode_now_w7.get("parentTitle") == "Named Season"
              and episode_now_w7.get("parentStudio") == "Season Studio"
              and episode_now_w7.get("grandparentTitle") == "Renamed Show"
              and season_now_w7.get("parentTitle") == "Renamed Show"
              and season_now_w7.get("parentStudio") == "Show Studio"
              and children_w7.get("parentTitle") == "Named Season" and children_w7.get("summary") == "Season words"
              and "grandparentTitle" not in episode_bare_w7 and "parentTitle" not in season_bare_w7,
              "episode %r/%r/%r, season %r/%r, list %r/%r, emptied %r/%r"
              % (episode_now_w7.get("parentTitle"), episode_now_w7.get("parentStudio"),
                 episode_now_w7.get("grandparentTitle"), season_now_w7.get("parentTitle"),
                 season_now_w7.get("parentStudio"), children_w7.get("parentTitle"), children_w7.get("summary"),
                 episode_bare_w7.get("grandparentTitle"), season_bare_w7.get("parentTitle")))
    else:
        skip("a parent's title and studio reach its children, and a season list names a custom title",
             "need a show")

    # Measured on plexscratch: an emptied season title is GENERATED -- `Season N` -- and an
    # episode's is `Episode N`, following a later index edit; every emptied title keeps
    # its old title's sort form; and a playlist sorts without its article from creation,
    # emptied or not. plexsim stored "" and dropped the sort title.
    if show:
        season_w9 = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                          if r.get("type") == "season"), {}).get("ratingKey")
        episode_w9 = (get(base, "/library/metadata/%s/children" % season_w9)[1]
                      .get("Metadata") or [{}])[0].get("ratingKey")
        def row_w9(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
        season_was_w9, episode_was_w9 = row_w9(season_w9), row_w9(episode_w9)
        request(base, "PUT", "/library/metadata/%s?title.value=%s" % (season_w9, quote("The Named Season")))
        request(base, "PUT", "/library/metadata/%s?title.value=" % season_w9)
        season_now_w9 = row_w9(season_w9)
        request(base, "PUT", "/library/metadata/%s?title.value=" % episode_w9)
        episode_now_w9 = row_w9(episode_w9)
        request(base, "PUT", "/library/metadata/%s?index.value=42" % episode_w9)
        reindexed_w9 = row_w9(episode_w9)
        request(base, "PUT", "/library/metadata/%s?index.value=%s" % (episode_w9, episode_was_w9.get("index")))
        for key_w9, was_w9 in ((season_w9, season_was_w9), (episode_w9, episode_was_w9)):
            request(base, "PUT", "/library/metadata/%s?title.value=%s" % (key_w9, quote(str(was_w9.get("title")))))
        _, made_w9, _ = container(base, "POST", "/playlists?type=video&smart=0&title=%s&uri=%s"
                                                % (quote("The Sorted List"),
                                                   quote("server://%s/com.plexapp.plugins.library"
                                                         "/library/metadata/%s" % (machine, movie))))
        list_w9 = (made_w9.get("Metadata") or [{}])[0].get("ratingKey")
        created_w9 = (get(base, "/playlists/%s" % list_w9)[1].get("Metadata") or [{}])[0].get("titleSort")
        request(base, "PUT", "/playlists/%s?title=" % list_w9)
        emptied_w9 = (get(base, "/playlists/%s" % list_w9)[1].get("Metadata") or [{}])[0]
        request(base, "DELETE", "/playlists/%s" % list_w9)
        sort_form_w9 = re.sub(r"^(The|An|A) ", "", str(episode_was_w9.get("title")))
        check("an emptied season or episode title is generated, and an emptied title keeps its sort form",
              season_now_w9.get("title") == "Season %s" % season_now_w9.get("index")
              and season_now_w9.get("titleSort") == "Named Season"
              and episode_now_w9.get("title") == "Episode %s" % episode_was_w9.get("index")
              and episode_now_w9.get("titleSort") == sort_form_w9
              and reindexed_w9.get("title") == "Episode 42"
              and created_w9 == "Sorted List" and emptied_w9.get("title") == ""
              and emptied_w9.get("titleSort") == "Sorted List",
              "season %r/%r, episode %r/%r, reindexed %r, playlist %r then %r/%r"
              % (season_now_w9.get("title"), season_now_w9.get("titleSort"), episode_now_w9.get("title"),
                 episode_now_w9.get("titleSort"), reindexed_w9.get("title"), created_w9,
                 emptied_w9.get("title"), emptied_w9.get("titleSort")))
    else:
        skip("an emptied season or episode title is generated, and an emptied title keeps its sort form",
             "need a show")

    # Measured on plexscratch: /:/progress stores ANY position past a minute -- even one past
    # the end -- without completing the play or writing history, and a missing or
    # unparseable `time` is a 500. plexsim marked the item watched and answered 200.
    if spare:
        progress_w10d = "/:/progress?key=%s&identifier=com.plexapp.plugins.library" % spare
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % spare)
        history_w10d = len(get(base, "/status/sessions/history/all?type=1")[1].get("Metadata") or [])
        status_w10d, body_w10d, _ = request(base, "GET", progress_w10d + "&time=999999999")
        far_w10d = (get(base, "/library/metadata/%s" % spare)[1].get("Metadata") or [{}])[0]
        history_after_w10d = len(get(base, "/status/sessions/history/all?type=1")[1].get("Metadata") or [])
        missing_w10d, _, _ = container(base, "GET", progress_w10d)
        bad_w10d, _, _ = container(base, "GET", progress_w10d + "&time=abc")
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % spare)
        check("progress stores a position past the end without completing, and needs a numeric time",
              is_empty_200(status_w10d, body_w10d) and far_w10d.get("viewOffset") == 999999999
              and not far_w10d.get("viewCount") and history_after_w10d == history_w10d
              and (missing_w10d, bad_w10d) == (500, 500),
              "HTTP %s viewOffset=%r viewCount=%r history %s->%s, missing %s, abc %s"
              % (status_w10d, far_w10d.get("viewOffset"), far_w10d.get("viewCount"), history_w10d,
                 history_after_w10d, missing_w10d, bad_w10d))
    else:
        skip("progress stores a position past the end without completing, and needs a numeric time",
             "need a second movie")

    # Measured on plexscratch: a negative rating other than -1 clears the stars and KEEPS
    # lastRatedAt; only -1 clears both. plexsim cleared both for any negative rating.
    rate_w10e = "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=%%s" % movie
    request(base, "GET", rate_w10e % "6")
    request(base, "GET", rate_w10e % "-5")
    minus5_w10e = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    request(base, "GET", rate_w10e % "-1")
    minus1_w10e = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    check("a negative rating other than -1 clears the stars and keeps lastRatedAt",
          "userRating" not in minus5_w10e and minus5_w10e.get("lastRatedAt")
          and "userRating" not in minus1_w10e and "lastRatedAt" not in minus1_w10e,
          "-5: %r/%r, -1: %r/%r" % (minus5_w10e.get("userRating"), minus5_w10e.get("lastRatedAt"),
                                    minus1_w10e.get("userRating"), minus1_w10e.get("lastRatedAt")))

    # Measured on plexscratch: a timeline `time` past the `duration` sent with it, a negative
    # or unparseable `time` and an unparseable `duration` are each a 400. plexsim answered
    # 200, and a time past the duration completed the play.
    if spare:
        def timeline_w11a(time_w11a, duration_w11a):
            return ("/:/timeline?ratingKey=%s&key=%s&state=stopped&time=%s&duration=%s"
                    % (spare, quote("/library/metadata/%s" % spare), time_w11a, duration_w11a))
        views_w11a = (get(base, "/library/metadata/%s" % spare)[1].get("Metadata") or [{}])[0].get("viewCount")
        statuses_w11a = [container(base, "GET", timeline_w11a(*pair))[0]
                         for pair in (("700000", "600000"), ("-5", "600000"), ("abc", "600000"),
                                      ("120000", "abc"))]
        after_w11a = (get(base, "/library/metadata/%s" % spare)[1].get("Metadata") or [{}])[0].get("viewCount")
        check("a timeline refuses a time past its duration and an unparseable time or duration",
              statuses_w11a == [400, 400, 400, 400] and after_w11a == views_w11a,
              "HTTP %s, viewCount %r -> %r" % (statuses_w11a, views_w11a, after_w11a))
    else:
        skip("a timeline refuses a time past its duration and an unparseable time or duration",
             "need a second movie")

    # Measured on plexscratch: a second completion of the same play answers `ignore` -- it
    # counts nothing -- where plexsim answered `complete` again.
    if spare:
        def timeline_w11b(time_w11b):
            return ("/:/timeline?ratingKey=%s&key=%s&state=stopped&time=%s&duration=600000"
                    % (spare, quote("/library/metadata/%s" % spare), time_w11b))
        container(base, "GET", timeline_w11b("120000"))
        first_w11b = container(base, "GET", timeline_w11b("590000"))[1].get("playbackState")
        second_w11b = container(base, "GET", timeline_w11b("590000"))[1].get("playbackState")
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % spare)
        check("a second completion of the same play answers ignore",
              (first_w11b, second_w11b) == ("complete", "ignore"), "%s then %s" % (first_w11b, second_w11b))
    else:
        skip("a second completion of the same play answers ignore", "need a second movie")

    # Measured on plexscratch: Remove from Continue Watching is a PUT that needs its
    # ratingKey -- a GET is a 404 and a bare PUT a 400. plexsim hid the row on a GET.
    get_w11c, _, _ = container(base, "GET", "/actions/removeFromContinueWatching?ratingKey=%s" % movie)
    bare_w11c, _, _ = container(base, "PUT", "/actions/removeFromContinueWatching")
    check("Remove from Continue Watching is a PUT that needs a ratingKey",
          (get_w11c, bare_w11c) == (404, 400), "GET %s, bare PUT %s" % (get_w11c, bare_w11c))

    # Measured on plexscratch: a playlist create needs a title, a type and a uri (each a 400
    # when missing), a type outside video/audio/photo is a 500, and audio over a movie is a
    # 400. plexsim created an "Untitled" list of whatever it was sent.
    uri_w12a = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))
    results_w12a = [container(base, "POST", "/playlists?" + spec)
                    for spec in ("type=video&smart=0&uri=%s" % uri_w12a, "title=w12a&smart=0&uri=%s" % uri_w12a,
                                 "type=video&title=w12a&smart=0", "type=bogus&title=w12a&smart=0&uri=%s" % uri_w12a,
                                 "type=audio&title=w12a&smart=0&uri=%s" % uri_w12a)]
    for _, made_w12a, _ in results_w12a:
        for row_w12a in made_w12a.get("Metadata") or []:
            request(base, "DELETE", "/playlists/%s" % row_w12a.get("ratingKey"))
    check("a playlist create needs a title, a type and a uri, and a type that fits it",
          [r[0] for r in results_w12a] == [400, 400, 400, 500, 400], str([r[0] for r in results_w12a]))

    # Measured on plexscratch: deleting an item id a playlist does not hold is a 404 (a
    # ratingKey is not an item id), DELETE on the items clears the list, and a smart
    # playlist refuses an add or a removal with 400. plexsim answered 200 to all of them.
    uri_w12b = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))
    _, made_w12b, _ = container(base, "POST", "/playlists?type=video&title=w12b&smart=0&uri=%s" % uri_w12b)
    list_w12b = (made_w12b.get("Metadata") or [{}])[0].get("ratingKey")
    unknown_w12b, _, _ = container(base, "DELETE", "/playlists/%s/items/999999" % list_w12b)
    by_key_w12b, _, _ = container(base, "DELETE", "/playlists/%s/items/%s" % (list_w12b, movie))
    clear_w12b, _, _ = container(base, "DELETE", "/playlists/%s/items" % list_w12b)
    left_w12b = get(base, "/playlists/%s/items" % list_w12b)[1].get("Metadata") or []
    _, smart_w12b, _ = container(base, "POST", "/playlists?type=video&title=w12bs&smart=1&uri=%s"
                                               % quote("server://%s/com.plexapp.plugins.library/library/sections/%s"
                                                       "/all?type=1" % (machine, movie_section)))
    smart_key_w12b = (smart_w12b.get("Metadata") or [{}])[0].get("ratingKey")
    smart_add_w12b, _, _ = container(base, "PUT", "/playlists/%s/items?uri=%s" % (smart_key_w12b, uri_w12b))
    smart_drop_w12b, _, _ = container(base, "DELETE", "/playlists/%s/items/1" % smart_key_w12b)
    request(base, "DELETE", "/playlists/%s" % list_w12b)
    request(base, "DELETE", "/playlists/%s" % smart_key_w12b)
    check("a playlist 404s an item it does not hold, clears on DELETE, and a smart one refuses item writes",
          (unknown_w12b, by_key_w12b, clear_w12b) == (404, 404, 200) and not left_w12b
          and (smart_add_w12b, smart_drop_w12b) == (400, 400),
          "unknown %s, ratingKey %s, clear %s leaving %d, smart add %s drop %s"
          % (unknown_w12b, by_key_w12b, clear_w12b, len(left_w12b), smart_add_w12b, smart_drop_w12b))

    # Measured on plexscratch: a playlist move with an `after` the list does not hold -- an
    # unknown id, `abc` -- sends the item to the FRONT, and `after` naming the item itself
    # changes nothing. plexsim appended to the end.
    _, made_w12c, _ = container(base, "POST", "/playlists?type=video&title=w12c&smart=0&uri=%s"
                                              % quote("server://%s/com.plexapp.plugins.library/library/sections/%s"
                                                      "/all?type=1" % (machine, movie_section)))
    list_w12c = (made_w12c.get("Metadata") or [{}])[0].get("ratingKey")
    def order_w12c():
        return [r.get("playlistItemID") for r in get(base, "/playlists/%s/items" % list_w12c)[1].get("Metadata") or []]
    ids_w12c = order_w12c()
    if len(ids_w12c) >= 3:
        request(base, "PUT", "/playlists/%s/items/%s/move?after=999999" % (list_w12c, ids_w12c[-1]))
        unknown_w12c = order_w12c()
        request(base, "PUT", "/playlists/%s/items/%s/move?after=%s" % (list_w12c, ids_w12c[1], ids_w12c[1]))
        itself_w12c = order_w12c()
        request(base, "PUT", "/playlists/%s/items/%s/move?after=abc" % (list_w12c, ids_w12c[0]))
        abc_w12c = order_w12c()
        check("a playlist move after nothing it holds goes to the front, and after itself stays",
              unknown_w12c[0] == ids_w12c[-1] and itself_w12c == unknown_w12c and abc_w12c[0] == ids_w12c[0],
              "%s -> %s -> %s -> %s" % (ids_w12c, unknown_w12c, itself_w12c, abc_w12c))
    else:
        skip("a playlist move after nothing it holds goes to the front, and after itself stays",
             "need three movies")
    request(base, "DELETE", "/playlists/%s" % list_w12c)

    # Measured on plexscratch: every state write needs the `identifier` the client always
    # sends -- without it a scrobble, unscrobble, rate or progress is a 400, even for a key
    # the server does not have -- and a foreign identifier is a 404. plexsim accepted both.
    statuses_w10a = [container(base, "GET", path_w10a % movie)[0] for path_w10a in (
        "/:/scrobble?key=%s", "/:/unscrobble?key=%s", "/:/rate?key=%s&rating=6", "/:/progress?key=%s&time=120000",
        "/:/scrobble?key=%s&identifier=bogus", "/:/rate?key=%s&rating=6&identifier=bogus")]
    unknown_w10a, _, _ = container(base, "GET", "/:/scrobble?key=999999")
    bad_rating_w10a, _, _ = container(base, "GET", "/:/rate?key=%s&rating=abc&identifier=bogus" % movie)
    request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % movie)
    request(base, "GET", "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=-1" % movie)
    check("a state write needs the library identifier: 400 without it, 404 with a foreign one",
          statuses_w10a == [400, 400, 400, 400, 404, 404] and unknown_w10a == 400 and bad_rating_w10a == 400,
          "%s, unknown key %s, bad rating with a foreign identifier %s" % (statuses_w10a, unknown_w10a,
                                                                          bad_rating_w10a))

    # Measured on plexscratch: an EPISODE scrobble adds one to its season's and its show's
    # own viewCount -- every time -- while scrobbling the show itself leaves the show's own
    # count alone, and unscrobbling the show removes it. plexsim did the reverse.
    if show:
        def count_w10b(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0].get("viewCount")
        season_w10b = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                            if r.get("type") == "season"), {}).get("ratingKey")
        episode_w10b = (get(base, "/library/metadata/%s/children" % season_w10b)[1]
                        .get("Metadata") or [{}])[0].get("ratingKey")
        scrobble_w10b = "/:/%sscrobble?key=%s&identifier=com.plexapp.plugins.library"
        request(base, "GET", scrobble_w10b % ("un", show))
        season_before_w10b = count_w10b(season_w10b) or 0
        request(base, "GET", scrobble_w10b % ("", episode_w10b))
        request(base, "GET", scrobble_w10b % ("", episode_w10b))
        season_after_w10b, show_after_w10b = count_w10b(season_w10b) or 0, count_w10b(show)
        request(base, "GET", scrobble_w10b % ("", show))
        show_marked_w10b = count_w10b(show)
        request(base, "GET", scrobble_w10b % ("un", show))
        show_cleared_w10b = count_w10b(show)
        check("an episode scrobble counts on its season and show; a show scrobble does not count on the show",
              season_after_w10b - season_before_w10b == 2 and show_after_w10b == 2
              and show_marked_w10b == 2 and show_cleared_w10b is None,
              "season %s -> %s, show %r, after the show's own scrobble %r, after unscrobble %r"
              % (season_before_w10b, season_after_w10b, show_after_w10b, show_marked_w10b, show_cleared_w10b))
    else:
        skip("an episode scrobble counts on its season and show; a show scrobble does not count on the show",
             "need a show")

    # Measured on plexscratch: a show puts ONE row on deck -- its in-progress episode with
    # the lowest number, not the one played last -- removing any of its episodes from
    # Continue Watching hides that row, a timeline on one of them brings it back, and with
    # the first one watched the next one in progress is the row. plexsim listed every
    # episode with a resume point.
    leaves_w10c = sorted(get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or [],
                         key=lambda r: (r.get("parentIndex") or 0, r.get("index") or 0)) if show else []
    if len(leaves_w10c) >= 2:
        first_w10c, second_w10c = str(leaves_w10c[0]["ratingKey"]), str(leaves_w10c[1]["ratingKey"])
        keys_w10c = {str(r["ratingKey"]) for r in leaves_w10c}
        def deck_w10c():
            return [str(r.get("ratingKey")) for r in get(base, "/library/onDeck")[1].get("Metadata") or []
                    if str(r.get("ratingKey")) in keys_w10c]
        def play_w10c(key, ms):
            request(base, "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&state=stopped"
                                 "&time=%s&duration=600000" % (key, key, ms))
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % show)
        play_w10c(second_w10c, 120000)
        play_w10c(first_w10c, 180000)
        both_w10c = deck_w10c()
        request(base, "PUT", "/actions/removeFromContinueWatching?ratingKey=%s" % second_w10c)
        hidden_w10c = deck_w10c()
        play_w10c(second_w10c, 150000)
        back_w10c = deck_w10c()
        request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % first_w10c)
        next_w10c = deck_w10c()
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % show)
        check("On Deck holds one row per show: the lowest episode in progress, hidden and restored as one",
              both_w10c == [first_w10c] and hidden_w10c == [] and back_w10c == [first_w10c]
              and next_w10c == [second_w10c],
              "%s / %s / %s / %s" % (both_w10c, hidden_w10c, back_w10c, next_w10c))
    else:
        skip("On Deck holds one row per show: the lowest episode in progress, hidden and restored as one",
             "need a show with two episodes")

    # Measured on plexscratch without a session identifier: a completed play answers
    # `complete` to every beat past 90% until a `stopped` beat closes it -- playing twice,
    # then stopped, all `complete` -- and `ignore` to every beat after that.
    if spare:
        def beat_w11d(state_w11d, time_w11d):
            return container(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=%s&time=%s&duration=600000"
                                          % (spare, quote("/library/metadata/%s" % spare), state_w11d,
                                             time_w11d))[1].get("playbackState")
        beat_w11d("stopped", "120000")
        sequence_w11d = [beat_w11d(s, t) for s, t in (("playing", "580000"), ("playing", "585000"),
                                                      ("stopped", "585100"), ("stopped", "585200"),
                                                      ("playing", "590000"))]
        request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % spare)
        check("a completed play answers complete until a stopped beat closes it, then ignore",
              sequence_w11d == ["complete", "complete", "complete", "ignore", "ignore"], str(sequence_w11d))
    else:
        skip("a completed play answers complete until a stopped beat closes it, then ignore", "need a second movie")

    # Measured on plexscratch: an item takes no POST (edits are PUTs) and /library/all takes
    # no write -- both are 404s, where plexsim answered the detail row and the listing.
    post_w17a, _, _ = container(base, "POST", "/library/metadata/%s?summary.value=w17a" % movie)
    all_w17a, _, _ = container(base, "PUT", "/library/all?type=1&tagline.value=w17a")
    check("an item takes no POST and /library/all takes no write",
          (post_w17a, all_w17a) == (404, 404), "POST %s, PUT /library/all %s" % (post_w17a, all_w17a))

    # Measured on plexscratch: these edits are each a 200 that changes nothing -- thumb,
    # art, addedAt, guid, lastViewedAt and viewCount values, a movie's duration and index,
    # an episode's parentIndex, a show's network tag and a bare `genre=` -- and an actor
    # written with a tagRole is stored with `role: ""`. plexsim 400'd most and stored two.
    def row_w17b(key):
        return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
    before_w17b = row_w17b(movie)
    statuses_w17b = [container(base, "PUT", "/library/metadata/%s?%s.value=17" % (movie, name))[0]
                     for name in ("thumb", "art", "addedAt", "guid", "lastViewedAt", "viewCount",
                                  "duration", "index")]
    statuses_w17b.append(container(base, "PUT", "/library/metadata/%s?genre=W17b" % movie)[0])
    after_w17b = row_w17b(movie)
    extra_w17b = []
    if show:
        season_w17b = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                            if r.get("type") == "season"), {}).get("ratingKey")
        episode_w17b = (get(base, "/library/metadata/%s/children" % season_w17b)[1]
                        .get("Metadata") or [{}])[0].get("ratingKey")
        episode_before_w17b = row_w17b(episode_w17b).get("parentIndex")
        extra_w17b = [container(base, "PUT", "/library/metadata/%s?parentIndex.value=3" % episode_w17b)[0],
                      container(base, "PUT", "/library/metadata/%s?network[0].tag.tag=W17b" % show)[0],
                      row_w17b(episode_w17b).get("parentIndex") == episode_before_w17b]
    role_w17b, _, _ = container(base, "PUT", "/library/metadata/%s?actor[0].tag.tag=%s&actor[0].tagRole=Hero"
                                             % (movie, quote("Actor W17b")))
    roles_w17b = [{k: r.get(k) for k in ("tag", "role")} for r in row_w17b(movie).get("Role") or []]
    check("value-less fields, a movie's duration and index, network tags and bare tags are accepted and discarded",
          statuses_w17b == [200] * 9 and all(after_w17b.get(k) == before_w17b.get(k)
                                             for k in ("thumb", "art", "addedAt", "guid", "duration", "index",
                                                       "viewCount", "Genre"))
          and extra_w17b in ([], [200, 200, True]) and role_w17b == 200
          and roles_w17b == [{"tag": "Actor W17b", "role": ""}],
          "%s %s role %s %s" % (statuses_w17b, extra_w17b, role_w17b, roles_w17b))

    # Measured on plexscratch: `key=` names the item a play queue starts on -- a queue over
    # the whole library selects that movie -- where plexsim selected the first row.
    listing_w14a = get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or []
    if len(listing_w14a) >= 2:
        last_w14a = str(listing_w14a[-1]["ratingKey"])
        made_w14a = container(base, "POST", "/playQueues?type=video&own=1&uri=%s&key=%s"
                                            % (quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)), quote("/library/metadata/%s" % last_w14a)))[1]
        rows_w14a = made_w14a.get("Metadata") or []
        chosen_w14a = [n for n, r in enumerate(rows_w14a)
                       if r.get("playQueueItemID") == made_w14a.get("playQueueSelectedItemID")]
        check("a play queue starts on the item its key names",
              chosen_w14a and str(rows_w14a[chosen_w14a[0]].get("ratingKey")) == last_w14a
              and made_w14a.get("playQueueSelectedItemOffset") == chosen_w14a[0],
              "selected %s at offset %s, wanted %s" % ([rows_w14a[n].get("ratingKey") for n in chosen_w14a],
                                                       made_w14a.get("playQueueSelectedItemOffset"), last_w14a))
    else:
        skip("a play queue starts on the item its key names", "need two movies")

    # Measured on plexscratch: a client's new play queue retires its older one -- reading it
    # is a 404 -- while another client's queue leaves it alone. plexsim kept every queue.
    uri_w14c = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))
    first_w14c = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % uri_w14c)[1].get("playQueueID")
    other_w14c = urllib.request.Request(base.rstrip("/") + "/playQueues?type=video&own=1&uri=%s" % uri_w14c,
                                        method="POST", headers={"User-Agent": UA, "Accept": "application/json",
                                                                "X-Plex-Client-Identifier": "plexsim-w14c-other",
                                                                "X-Plex-Platform": "Chrome"})
    with urllib.request.urlopen(other_w14c, timeout=30) as response_w14c:
        other_status_w14c = response_w14c.status
    survives_w14c = container(base, "GET", "/playQueues/%s?own=1" % first_w14c)[0]
    second_w14c = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % uri_w14c)[1].get("playQueueID")
    retired_w14c = container(base, "GET", "/playQueues/%s?own=1" % first_w14c)[0]
    current_w14c = container(base, "GET", "/playQueues/%s?own=1" % second_w14c)[0]
    check("a client's new play queue retires its older one, and another client's does not",
          (other_status_w14c, survives_w14c, retired_w14c, current_w14c) == (200, 200, 404, 200),
          "other %s, first after it %s, first after our second %s, second %s"
          % (other_status_w14c, survives_w14c, retired_w14c, current_w14c))

    # Measured on plexscratch: `PUT /playQueues/{id}?playlistID=` adds that playlist's items;
    # plexsim stepped the version and added nothing.
    _, list_w14d, _ = container(base, "POST", "/playlists?type=video&title=w14d&smart=0&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)))
    list_key_w14d = (list_w14d.get("Metadata") or [{}])[0].get("ratingKey")
    list_size_w14d = len(get(base, "/playlists/%s/items" % list_key_w14d)[1].get("Metadata") or [])
    queue_w14d = container(base, "POST", "/playQueues?type=video&own=1&uri=%s"
                                         % quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                                 % (machine, movie)))[1].get("playQueueID")
    added_w14d = container(base, "PUT", "/playQueues/%s?playlistID=%s" % (queue_w14d, list_key_w14d))[1]
    request(base, "DELETE", "/playlists/%s" % list_key_w14d)
    check("a play queue PUT with playlistID adds the playlist's items",
          list_size_w14d > 0 and len(added_w14d.get("Metadata") or []) == 1 + list_size_w14d,
          "%d row(s) after adding a %d-item playlist" % (len(added_w14d.get("Metadata") or []), list_size_w14d))

    # Measured on plexscratch: DELETE on a play queue's items clears it down to the selected
    # item; plexsim answered 404.
    made_w14e = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)))[1]
    cleared_status_w14e, cleared_w14e, _ = container(base, "DELETE", "/playQueues/%s/items" % made_w14e.get("playQueueID"))
    check("DELETE on a play queue's items keeps only the selected item",
          len(made_w14e.get("Metadata") or []) > 1 and cleared_status_w14e == 200
          and [r.get("playQueueItemID") for r in cleared_w14e.get("Metadata") or []]
          == [made_w14e.get("playQueueSelectedItemID")],
          "HTTP %s, %s" % (cleared_status_w14e, [r.get("playQueueItemID") for r in cleared_w14e.get("Metadata") or []]))

    # Measured on plexscratch: deleting a play queue's selected item answers the queue as it
    # stood -- the old selection and version, no rows -- and the next read has the selection
    # on the following item. plexsim left the selection on the deleted id.
    made_w14f = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)))[1]
    ids_w14f = [r.get("playQueueItemID") for r in made_w14f.get("Metadata") or []]
    if len(ids_w14f) >= 2:
        queue_w14f = made_w14f.get("playQueueID")
        _, gone_w14f, _ = container(base, "DELETE", "/playQueues/%s/items/%s" % (queue_w14f, ids_w14f[0]))
        _, after_w14f, _ = get(base, "/playQueues/%s" % queue_w14f)
        check("deleting the selected queue item answers the old header and moves the selection on",
              gone_w14f.get("size") == 0 and not gone_w14f.get("Metadata")
              and gone_w14f.get("playQueueSelectedItemID") == ids_w14f[0]
              and gone_w14f.get("playQueueVersion") == made_w14f.get("playQueueVersion")
              and after_w14f.get("playQueueSelectedItemID") == ids_w14f[1],
              "answer size %s selected %s version %s; then selected %s"
              % (gone_w14f.get("size"), gone_w14f.get("playQueueSelectedItemID"), gone_w14f.get("playQueueVersion"),
                 after_w14f.get("playQueueSelectedItemID")))
    else:
        skip("deleting the selected queue item answers the old header and moves the selection on", "need two movies")

    # Measured on plexscratch: a play queue move with an `after` the queue does not hold --
    # `0`, an unknown id -- goes to the front; plexsim answered 404.
    made_w14g = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)))[1]
    ids_w14g = [r.get("playQueueItemID") for r in made_w14g.get("Metadata") or []]
    if len(ids_w14g) >= 3:
        queue_w14g = made_w14g.get("playQueueID")
        zero_status_w14g, zero_w14g, _ = container(base, "PUT", "/playQueues/%s/items/%s/move?after=0"
                                                                % (queue_w14g, ids_w14g[-1]))
        unknown_status_w14g, unknown_w14g, _ = container(base, "PUT", "/playQueues/%s/items/%s/move?after=999999"
                                                                      % (queue_w14g, ids_w14g[1]))
        check("a play queue move after nothing it holds goes to the front",
              (zero_status_w14g, unknown_status_w14g) == (200, 200)
              and [r.get("playQueueItemID") for r in zero_w14g.get("Metadata") or []][:1] == [ids_w14g[-1]]
              and [r.get("playQueueItemID") for r in unknown_w14g.get("Metadata") or []][:1] == [ids_w14g[1]],
              "HTTP %s/%s" % (zero_status_w14g, unknown_status_w14g))
    else:
        skip("a play queue move after nothing it holds goes to the front", "need three movies")

    # Measured on plexscratch: a windowed queue read's `includeBefore`/`includeAfter` choose
    # the sides, and the selected item comes back only when both are on. plexsim ignored both.
    made_w14h = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/sections/%s/all?type=1" % (machine, movie_section)))[1]
    ids_w14h = [r.get("playQueueItemID") for r in made_w14h.get("Metadata") or []]
    if len(ids_w14h) >= 3:
        def window_w14h(spec):
            return [r.get("playQueueItemID") for r in get(base, "/playQueues/%s?%s" % (made_w14h.get("playQueueID"),
                                                                                        spec))[1].get("Metadata") or []]
        after_only_w14h = window_w14h("window=1&includeBefore=0&includeAfter=1")
        before_only_w14h = window_w14h("window=1&includeBefore=1&includeAfter=0")
        both_w14h = window_w14h("window=2")
        check("a windowed queue read honours includeBefore and includeAfter",
              after_only_w14h == ids_w14h[1:2] and before_only_w14h == [] and both_w14h == ids_w14h[:3],
              "%s / %s / %s" % (after_only_w14h, before_only_w14h, both_w14h))
    else:
        skip("a windowed queue read honours includeBefore and includeAfter", "need three movies")

    # Measured on plexscratch: a play queue uri naming ANOTHER server builds an empty queue
    # that echoes the uri verbatim; plexsim resolved it against its own library.
    foreign_w14i = ("server://0123456789abcdef0123456789abcdef01234567/com.plexapp.plugins.library"
                    "/library/metadata/%s" % movie)
    status_w14i, made_w14i, _ = container(base, "POST", "/playQueues?type=video&own=1&uri=%s" % quote(foreign_w14i))
    check("a play queue uri naming another server builds an empty queue echoing it",
          status_w14i == 200 and not made_w14i.get("Metadata") and made_w14i.get("size") == 0
          and made_w14i.get("playQueueSourceURI") == foreign_w14i,
          "HTTP %s size %s source %r" % (status_w14i, made_w14i.get("size"), made_w14i.get("playQueueSourceURI")))

    # Measured on plexscratch: a collection create needs a title and a sectionId -- each
    # missing one is a 400 -- where plexsim made an "Untitled" collection anyway.
    results_w13a = [container(base, "POST", "/library/collections?" + spec)
                    for spec in ("type=1&sectionId=%s&uri=%s" % (movie_section, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))),
                                 "type=1&title=w13a&uri=%s" % quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie)))]
    for _, made_w13a, _ in results_w13a:
        for row_w13a in made_w13a.get("Metadata") or []:
            request(base, "DELETE", "/library/collections/%s" % row_w13a.get("ratingKey"))
    check("a collection create needs a title and a sectionId",
          [r[0] for r in results_w13a] == [400, 400], str([r[0] for r in results_w13a]))

    # Measured on plexscratch: a collection over a show holds the SHOW (subtype show, one
    # child) and one over a season holds the season; plexsim expanded both into episodes.
    if show and show_section:
        season_w13f = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                            if r.get("type") == "season"), {}).get("ratingKey")
        made_w13f = []
        for type_w13f, key_w13f in (("2", show), ("3", season_w13f)):
            _, doc_w13f, _ = container(base, "POST", "/library/collections?type=%s&sectionId=%s&title=w13f%s&uri=%s"
                                                     % (type_w13f, show_section, type_w13f,
                                                        quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                                              % (machine, key_w13f))))
            row_w13f = (doc_w13f.get("Metadata") or [{}])[0]
            children_w13f = [str(r.get("ratingKey")) for r in get(base, "/library/collections/%s/children"
                                                                       % row_w13f.get("ratingKey"))[1].get("Metadata") or []]
            made_w13f.append((row_w13f.get("subtype"), row_w13f.get("childCount"), children_w13f))
            request(base, "DELETE", "/library/collections/%s" % row_w13f.get("ratingKey"))
        check("a collection over a show or a season holds that row, typed by it",
              made_w13f == [("show", 1, [str(show)]), ("season", 1, [str(season_w13f)])], str(made_w13f))
    else:
        skip("a collection over a show or a season holds that row, typed by it", "need a show")

    # Measured on plexscratch: a collection add with no uri, an unknown key, or a row of
    # another type (an episode into a movie collection) is a 400, and so is any add to a
    # smart collection. plexsim added whatever the uri named.
    _, doc_w13b, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s&title=w13b&uri=%s"
                                             % (movie_section, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))))
    key_w13b = (doc_w13b.get("Metadata") or [{}])[0].get("ratingKey")
    uri_w13b = lambda k: quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, k))
    leaves_w13b = get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or [{}] if show else [{}]
    statuses_w13b = [container(base, "PUT", "/library/collections/%s/items" % key_w13b)[0],
                     container(base, "PUT", "/library/collections/%s/items?uri=%s" % (key_w13b, uri_w13b(999999)))[0]]
    if show:
        statuses_w13b.append(container(base, "PUT", "/library/collections/%s/items?uri=%s"
                                                    % (key_w13b, uri_w13b(leaves_w13b[0].get("ratingKey"))))[0])
    members_w13b = [str(r.get("ratingKey")) for r in get(base, "/library/collections/%s/children" % key_w13b)[1]
                    .get("Metadata") or []]
    _, smart_w13b, _ = container(base, "POST", "/library/collections?type=1&smart=1&sectionId=%s&title=w13bs&uri=%s"
                                               % (movie_section, quote("server://%s/com.plexapp.plugins.library/library"
                                                                       "/sections/%s/all?type=1" % (machine, movie_section))))
    smart_key_w13b = (smart_w13b.get("Metadata") or [{}])[0].get("ratingKey")
    statuses_w13b.append(container(base, "PUT", "/library/collections/%s/items?uri=%s"
                                                % (smart_key_w13b, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))))[0])
    request(base, "DELETE", "/library/collections/%s" % key_w13b)
    request(base, "DELETE", "/library/collections/%s" % smart_key_w13b)
    check("a collection add refuses no uri, an unknown key, another type and a smart collection",
          statuses_w13b == [400] * len(statuses_w13b) and members_w13b == [str(movie)],
          "%s, members %s" % (statuses_w13b, members_w13b))

    # Measured on plexscratch: a collection's children come back by RELEASE DATE unless a
    # custom order is set; a move starts from that order, answers the collection row and
    # switches the collection to custom (collectionSort 2), and collectionSort 0 goes back
    # to release date. plexsim listed insertion order and answered the move with nothing.
    listing_w13c = (get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or [])[:3]
    if len(listing_w13c) == 3:
        keys_w13c = [str(r["ratingKey"]) for r in listing_w13c]
        def date_w13c(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0].get("originallyAvailableAt")
        kept_w13c = [date_w13c(k) for k in keys_w13c]
        for key_w13c, day_w13c in zip(keys_w13c, ("2012-01-03", "2012-01-01", "2012-01-02")):
            request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s" % (key_w13c, day_w13c))
        _, doc_w13c, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s&title=w13c&uri=%s"
                                                 % (movie_section, quote("server://%s/com.plexapp.plugins.library"
                                                                         "/library/metadata/%s" % (machine, keys_w13c[0]))))
        coll_w13c = (doc_w13c.get("Metadata") or [{}])[0].get("ratingKey")
        for key_w13c in keys_w13c[1:]:
            request(base, "PUT", "/library/collections/%s/items?uri=%s"
                                 % (coll_w13c, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                                     % (machine, key_w13c))))
        def children_w13c():
            return [str(r.get("ratingKey")) for r in get(base, "/library/collections/%s/children"
                                                              % coll_w13c)[1].get("Metadata") or []]
        dated_w13c = children_w13c()
        moved_status_w13c, moved_w13c, _ = container(base, "PUT", "/library/collections/%s/items/%s/move"
                                                                  % (coll_w13c, keys_w13c[0]))
        custom_w13c = children_w13c()
        sort_w13c = (get(base, "/library/collections/%s" % coll_w13c)[1].get("Metadata") or [{}])[0].get("collectionSort")
        request(base, "PUT", "/library/metadata/%s/prefs?collectionSort=0" % coll_w13c)
        back_w13c = children_w13c()
        request(base, "DELETE", "/library/collections/%s" % coll_w13c)
        for key_w13c, day_w13c in zip(keys_w13c, kept_w13c):
            if day_w13c:
                request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s" % (key_w13c, day_w13c))
        by_date_w13c = [keys_w13c[1], keys_w13c[2], keys_w13c[0]]
        check("a collection lists by release date, and a move starts from that order and switches to custom",
              dated_w13c == by_date_w13c and moved_status_w13c == 200 and moved_w13c.get("size") == 1
              and custom_w13c == [keys_w13c[0], keys_w13c[1], keys_w13c[2]] and sort_w13c == "2"
              and back_w13c == by_date_w13c,
              "%s -> move %s %s -> %s sort %r -> %s" % (dated_w13c, moved_status_w13c, moved_w13c.get("size"),
                                                         custom_w13c, sort_w13c, back_w13c))
    else:
        skip("a collection lists by release date, and a move starts from that order and switches to custom",
             "need three movies")

    # Measured on plexscratch: a collection takes a Label like any item, and the section save
    # with type=18 edits the collection its id names. plexsim ignored both.
    _, doc_w13d, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s&title=w13d&uri=%s"
                                             % (movie_section, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))))
    key_w13d = (doc_w13d.get("Metadata") or [{}])[0].get("ratingKey")
    request(base, "PUT", "/library/metadata/%s?label[0].tag.tag=W13d" % key_w13d)
    request(base, "PUT", "/library/sections/%s/all?type=18&id=%s&summary.value=w13d" % (movie_section, key_w13d))
    row_w13d = (get(base, "/library/metadata/%s" % key_w13d)[1].get("Metadata") or [{}])[0]
    request(base, "DELETE", "/library/collections/%s" % key_w13d)
    check("a collection takes a label, and a type=18 section save edits it",
          [t.get("tag") for t in row_w13d.get("Label") or []] == ["W13d"] and row_w13d.get("summary") == "w13d",
          "labels %s summary %r" % ([t.get("tag") for t in row_w13d.get("Label") or []], row_w13d.get("summary")))

    # Measured on plexscratch: renaming a collection leaves its members' Collection tag as
    # it was, and deleting it still untags them. plexsim renamed the tag with the collection.
    _, doc_w13e, _ = container(base, "POST", "/library/collections?type=1&sectionId=%s&title=w13e&uri=%s"
                                             % (movie_section, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, movie))))
    key_w13e = (doc_w13e.get("Metadata") or [{}])[0].get("ratingKey")
    def tags_w13e():
        return [t.get("tag") for t in (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                .get("Collection") or [] if str(t.get("tag")).startswith("w13e")]
    request(base, "PUT", "/library/collections/%s?title=w13erenamed" % key_w13e)
    renamed_w13e = tags_w13e()
    request(base, "DELETE", "/library/collections/%s" % key_w13e)
    deleted_w13e = tags_w13e()
    check("renaming a collection leaves its members' tag, and deleting it still untags them",
          renamed_w13e == ["w13e"] and deleted_w13e == [], "%s then %s" % (renamed_w13e, deleted_w13e))


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: a full library edit APPLIES its language and prefs[...] -- they
    # read back on the section and in its settings -- and runs a scan: one
    # library.update.section activity and a status naming the section. plexsim renamed the
    # library and dropped the rest.
    section_w15a = next((d for d in get(base, "/library/sections")[1].get("Directory") or []
                         if str(d.get("key")) == str(movie_section)), {})
    form_w15a = ("/library/sections/%s?name=%s&type=movie&agent=%s&scanner=%s&location=%s"
                 % (movie_section, quote(section_w15a.get("title") or ""), quote(section_w15a.get("agent") or ""),
                    quote(section_w15a.get("scanner") or ""),
                    quote(((section_w15a.get("Location") or [{}])[0]).get("path") or "/")))
    holder_w15a = []
    _, frames_w15a = notifications(base, lambda: holder_w15a.append(
        request(base, "PUT", form_w15a + "&language=de-DE&prefs%5BenableBIFGeneration%5D=0")[0]), seconds=3)
    language_w15a = next((d.get("language") for d in get(base, "/library/sections")[1].get("Directory") or []
                          if str(d.get("key")) == str(movie_section)), None)
    bif_w15a = next((s.get("value") for s in get(base, "/library/sections/%s/prefs" % movie_section)[1]
                     .get("Setting") or [] if s.get("id") == "enableBIFGeneration"), None)
    request(base, "PUT", form_w15a + "&language=en-US&prefs%5BenableBIFGeneration%5D=1")
    runs_w15a = activities_w15(frames_w15a)
    check("a library edit applies its language and prefs and runs a scan",
          holder_w15a == [200] and language_w15a == "de-DE" and str(bif_w15a) in ("false", "0")
          and ("started", "library.update.section", 0) in runs_w15a
          and ("ended", "library.update.section", 100) in runs_w15a
          and 'Scanning the "%s" section' % section_w15a.get("title") in statuses_w15(frames_w15a),
          "HTTP %s language %r bif %r, %d activity frame(s), statuses %s"
          % (holder_w15a, language_w15a, bif_w15a, len(runs_w15a), statuses_w15(frames_w15a)[:2]))


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: /library/sections/all/refresh scans EVERY library -- a status
    # naming each -- and a scan is one library.update.section activity. plexsim 404'd the
    # path, and scanned by starting a metadata activity per row.
    titles_w15b = [d.get("title") for d in get(base, "/library/sections")[1].get("Directory") or []]
    holder_w15b = []
    _, frames_w15b = notifications(base, lambda: holder_w15b.append(
        request(base, "GET", "/library/sections/all/refresh")[0]), seconds=3)
    kinds_w15b = {run[1] for run in activities_w15(frames_w15b)}
    check("refreshing all libraries scans each one as a library.update.section activity",
          holder_w15b == [200] and titles_w15b
          and all('Scanning the "%s" section' % t in statuses_w15(frames_w15b) for t in titles_w15b)
          and "library.update.section" in kinds_w15b and "library.update.item.metadata" not in kinds_w15b,
          "HTTP %s, statuses %s, kinds %s" % (holder_w15b, statuses_w15(frames_w15b)[:4], sorted(kinds_w15b)))

    # Measured on plexscratch: a movie's written setting shows on its row, as a show's does;
    # plexsim stored it and never showed it.
    request(base, "PUT", "/library/metadata/%s/prefs?useOriginalTitle=1" % movie)
    shown_w15c = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0].get("useOriginalTitle")
    request(base, "PUT", "/library/metadata/%s/prefs?useOriginalTitle=" % movie)
    cleared_w15c = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0].get("useOriginalTitle")
    check("a movie's written setting shows on its row", shown_w15c == "1" and cleared_w15c is None,
          "%r then %r" % (shown_w15c, cleared_w15c))

    # Measured on plexscratch: a library create applies its prefs[...] and answers a bare
    # container; plexsim dropped the prefs and sent the library container's extras.
    status_w15d, body_w15d, _ = request(base, "POST", "/library/sections?name=W15d&type=show"
                                                      "&agent=tv.plex.agents.series&scanner=%s&language=en-US"
                                                      "&location=%s&prefs%%5BflattenSeasons%%5D=1"
                                                      "&prefs%%5BepisodeSort%%5D=1"
                                                      % (quote("Plex TV Series"), quote("/data/w15d")))
    made_w15d = json.loads(body_w15d).get("MediaContainer", {}) if body_w15d[:1] == b"{" else {}
    key_w15d = ((made_w15d.get("Directory") or [{}])[0]).get("key")
    prefs_w15d = {s.get("id"): s.get("value") for s in get(base, "/library/sections/%s/prefs" % key_w15d)[1]
                  .get("Setting") or []}
    if key_w15d:
        request(base, "DELETE", "/library/sections/%s" % key_w15d)
    check("a library create applies its prefs and answers a bare container",
          status_w15d == 201 and "identifier" not in made_w15d and "allowSync" not in made_w15d
          and str(prefs_w15d.get("flattenSeasons")) == "1" and str(prefs_w15d.get("episodeSort")) == "1",
          "HTTP %s, container %s, prefs %r/%r" % (status_w15d, sorted(k for k in made_w15d if k != "Directory"),
                                                 prefs_w15d.get("flattenSeasons"), prefs_w15d.get("episodeSort")))


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: analyzing a movie library pushes, for every movie, a state-5
    # frame with mediaState analyzing, one with thumbnailing, and a plain state 5. plexsim
    # pushed nothing.
    movies_w15f = get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or []
    holder_w15f = []
    _, frames_w15f = notifications(base, lambda: holder_w15f.append(
        request(base, "PUT", "/library/sections/%s/analyze" % movie_section)[0]), seconds=3)
    states_w15f = [e.get("mediaState") for e in timelines_w15(frames_w15f) if e.get("state") == 5]
    check("analyzing a library pushes analyzing and thumbnailing frames for every movie",
          holder_w15f == [200] and movies_w15f
          and states_w15f.count("analyzing") == len(movies_w15f)
          and states_w15f.count("thumbnailing") == len(movies_w15f),
          "HTTP %s, %d movie(s), states %s" % (holder_w15f, len(movies_w15f), states_w15f[:6]))


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: cleaning bundles runs a general.bundle.clean activity and
    # deleting a library a library.delete one, each started, six updates, ended. plexsim
    # pushed nothing for either.
    def run_w15g(trigger, kind):
        _, frames = notifications(base, trigger, seconds=3)
        return [event for event, found, _ in activities_w15(frames) if found == kind]
    bundles_w15g = run_w15g(lambda: request(base, "PUT", "/library/clean/bundles"), "general.bundle.clean")
    _, made_w15g, _ = container(base, "POST", "/library/sections?name=W15g&type=movie&agent=tv.plex.agents.movie"
                                              "&scanner=%s&language=en-US&location=%s"
                                              % (quote("Plex Movie"), quote("/data/w15g")))
    key_w15g = ((made_w15g.get("Directory") or [{}])[0]).get("key")
    deleted_w15g = run_w15g(lambda: request(base, "DELETE", "/library/sections/%s" % key_w15g), "library.delete")
    check("cleaning bundles and deleting a library each run their activity",
          bundles_w15g == ["started"] + ["updated"] * 6 + ["ended"]
          and deleted_w15g == ["started"] + ["updated"] * 6 + ["ended"],
          "%s / %s" % (bundles_w15g, deleted_w15g))

    # Measured on plexscratch: a merge with no ids is a 400, an unknown id a 404, and a movie
    # into a show a 400. plexsim answered 200 to all three and moved the movie into the show.
    unknown_w16a, _, _ = container(base, "PUT", "/library/metadata/%s/merge?ids=999999" % movie)
    bare_w16a, _, _ = container(base, "PUT", "/library/metadata/%s/merge" % movie)
    cross_w16a = container(base, "PUT", "/library/metadata/%s/merge?ids=%s" % (show, movie))[0] if show else 400
    kept_w16a = len((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0].get("Media") or [])
    check("a merge refuses no ids, an unknown id and a row of another type",
          (unknown_w16a, bare_w16a, cross_w16a) == (404, 400, 400) and kept_w16a >= 1,
          "unknown %s, bare %s, cross-type %s, movie media %s" % (unknown_w16a, bare_w16a, cross_w16a, kept_w16a))


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: a merge frames the absorbed row deleted and the survivor as
    # type -1 (state 3 then 5); a split frames the new row created (0, 0, 1) and the source
    # refreshed (3, 5). plexsim pushed nothing for either.
    listing_w16b = get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or []
    if len(listing_w16b) >= 3:
        keep_w16b, absorb_w16b = str(listing_w16b[1]["ratingKey"]), str(listing_w16b[2]["ratingKey"])
        def frames_w16b(trigger):
            _, frames = notifications(base, trigger, seconds=3)
            return [(e.get("type"), e.get("state"), str(e.get("itemID"))) for e in timelines_w15(frames)]
        merge_w16b = frames_w16b(lambda: request(base, "PUT", "/library/metadata/%s/merge?ids=%s" % (keep_w16b, absorb_w16b)))
        split_w16b = frames_w16b(lambda: request(base, "PUT", "/library/metadata/%s/split" % keep_w16b))
        new_w16b = sorted({e[2] for e in split_w16b if e[1] in (0, 1)})
        check("a merge frames the absorbed row deleted and the survivor as type -1; a split frames the new row",
              merge_w16b == [(1, 9, absorb_w16b), (-1, 3, keep_w16b), (-1, 5, keep_w16b)]
              and len(new_w16b) == 1 and new_w16b[0] not in (keep_w16b, absorb_w16b)
              and split_w16b == [(1, 0, new_w16b[0]), (1, 0, new_w16b[0]), (1, 1, new_w16b[0]),
                                 (1, 3, keep_w16b), (1, 5, keep_w16b)],
              "merge %s / split %s" % (merge_w16b, split_w16b))
    else:
        skip("a merge frames the absorbed row deleted and the survivor as type -1; a split frames the new row",
             "need three movies")


    def activities_w15(frames):
        return [(a.get("event"), (a.get("Activity") or {}).get("type"), (a.get("Activity") or {}).get("progress"))
                for f in frames or [] for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    def statuses_w15(frames):
        return [s.get("title") for f in frames or []
                for s in (f.get("NotificationContainer") or {}).get("StatusNotification") or []]
    def timelines_w15(frames):
        return [e for f in frames or [] for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    # Measured on plexscratch: unmatching an episode gives it `local://<id>` and the generated
    # `Episode N`, keeps its Guids and pushes matching then done; a season becomes `Season
    # N` on `local://<id>`. plexsim used the filename, a none:// guid and dropped the Guids.
    if show:
        season_w16c = next((r for r in get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                            if r.get("type") == "season"), {}).get("ratingKey")
        episode_w16c = (get(base, "/library/metadata/%s/children" % season_w16c)[1]
                        .get("Metadata") or [{}])[0].get("ratingKey")
        def row_w16c(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
        guids_w16c = [g.get("id") for g in row_w16c(episode_w16c).get("Guid") or []]
        _, frames_w16c = notifications(base, lambda: request(base, "PUT", "/library/metadata/%s/unmatch" % episode_w16c),
                                       seconds=3)
        states_w16c = [(e.get("state"), e.get("metadataState")) for e in timelines_w15(frames_w16c)
                       if str(e.get("itemID")) == str(episode_w16c)]
        episode_after_w16c = row_w16c(episode_w16c)
        request(base, "PUT", "/library/metadata/%s/unmatch" % season_w16c)
        season_after_w16c = row_w16c(season_w16c)
        check("unmatching an episode or a season gives a local guid and a generated title, keeping the Guids",
              states_w16c == [(2, "matching"), (5, None)]
              and episode_after_w16c.get("title") == "Episode %s" % episode_after_w16c.get("index")
              and episode_after_w16c.get("guid") == "local://%s" % episode_w16c
              and [g.get("id") for g in episode_after_w16c.get("Guid") or []] == guids_w16c
              and season_after_w16c.get("title") == "Season %s" % season_after_w16c.get("index")
              and season_after_w16c.get("guid") == "local://%s" % season_w16c,
              "frames %s, episode %r %r, season %r %r" % (states_w16c, episode_after_w16c.get("title"),
                                                         episode_after_w16c.get("guid"), season_after_w16c.get("title"),
                                                         season_after_w16c.get("guid")))
    else:
        skip("unmatching an episode or a season gives a local guid and a generated title, keeping the Guids",
             "need a show")

    # Measured on both servers: a video's and a subtitle's displayTitle carries the codec only
    # for Plex Web and the twelve other products named in mediainfo.CODEC_TITLE_PRODUCTS, in
    # the X-Plex-Product header or the query alike; any other product gets the plain title,
    # and the extended title is the same for everyone. plexsim sent every client the plain one.
    episodes_c10a = (get(base, "/library/sections/%s/all?type=4" % show_section)[1].get("Metadata") or []
                     if show_section else [])
    if episodes_c10a:
        def titles_c10a(product, query=""):
            headers_c10a = {"User-Agent": UA, "Accept": "application/json", "X-Plex-Platform": "Chrome",
                            "X-Plex-Client-Identifier": "plexsim-ui-test"}
            if product:
                headers_c10a["X-Plex-Product"] = product
            url_c10a = base.rstrip("/") + "/library/metadata/%s%s" % (episodes_c10a[0]["ratingKey"], query)
            with urllib.request.urlopen(urllib.request.Request(url_c10a, headers=headers_c10a), timeout=30) as reply:
                row_c10a = json.loads(reply.read())["MediaContainer"]["Metadata"][0]
            return [(s.get("streamType"), s.get("displayTitle"), s.get("extendedDisplayTitle"))
                    for m in row_c10a.get("Media") or [] for p in m.get("Part") or [] for s in p.get("Stream") or []]
        web_c10a = titles_c10a("Plex Web")
        roku_c10a = titles_c10a("Plex for Roku")
        query_c10a = titles_c10a(None, "?X-Plex-Product=Plex%20Web")
        coded_c10a = [t for t in web_c10a if t[0] in (1, 3)]
        plain_c10a = [t for t in roku_c10a if t[0] in (1, 3)]
        check("Plex Web gets the codec in a video's and a subtitle's displayTitle, Roku the plain title",
              coded_c10a and all(t[1] == t[2] and t[1].endswith(")") for t in coded_c10a)
              and all("(" not in t[1] for t in plain_c10a)
              and [t[2] for t in web_c10a] == [t[2] for t in roku_c10a]
              and [t for t in web_c10a if t[0] == 2] == [t for t in roku_c10a if t[0] == 2]
              and query_c10a == web_c10a,
              "Plex Web %s / Roku %s / query %s" % (web_c10a, roku_c10a, query_c10a))
    else:
        skip("Plex Web gets the codec in a video's and a subtitle's displayTitle, Roku the plain title",
             "need an episode")

    # A catalog built before cycle 10's media rules still loads right: the resolution label and
    # aspect ratio are recomputed from the Media's own size, a stored `profile: 0` / `level: -99`
    # is dropped, and the titles are composed after those fields are corrected (the rules are
    # in tests/test_mediainfo.py). plexsim served a stored 1920x800 row as 720 / 2.4 / 0 / -99.
    media_c10b = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                  .get("Media") or [{}])[0]
    video_c10b = next((s for p in media_c10b.get("Part") or [] for s in p.get("Stream") or []
                       if s.get("streamType") == 1), {})
    if (media_c10b.get("width"), media_c10b.get("height")) == (1920, 800):
        check("a stored catalog row's resolution, aspect ratio and unknown markers are corrected at load",
              media_c10b.get("videoResolution") == "1080" and media_c10b.get("aspectRatio") == 2.35
              and video_c10b.get("profile") not in ("0",) and video_c10b.get("level") != -99
              and media_c10b.get("videoProfile") != "0",
              "videoResolution %r, aspectRatio %r, profile %r, level %r, videoProfile %r"
              % (media_c10b.get("videoResolution"), media_c10b.get("aspectRatio"), video_c10b.get("profile"),
                 video_c10b.get("level"), media_c10b.get("videoProfile")))
    else:
        skip("a stored catalog row's resolution, aspect ratio and unknown markers are corrected at load",
             "the fixture movie is not the 1920x800 one")

    # Measured on both servers: a library's extras are a section leaf -- `/extras` names its one
    # `all` view, `/extras/all` lists the library's clips (none here) in the library's container,
    # anything deeper is a 404 -- and `/hubs/sections/{id}/extras` is an empty hub list; an
    # item's `/extras/{n}` is a 404. plexsim 404'd the first three and answered the last.
    titles_c10c = {str(d.get("key")): d.get("title") for d in get(base, "/library/sections")[1].get("Directory") or []}
    status_c10c, extras_c10c, _ = get(base, "/library/sections/%s/extras" % movie_section)
    status_all_c10c, all_c10c, _ = get(base, "/library/sections/%s/extras/all" % movie_section)
    status_deep_c10c, _, _ = get(base, "/library/sections/%s/extras/bogus" % movie_section)
    status_hub_c10c, hub_c10c, _ = get(base, "/hubs/sections/%s/extras" % movie_section)
    status_item_c10c, _, _ = get(base, "/library/metadata/%s/extras/1" % movie)
    show_extras_c10c = get(base, "/library/sections/%s/extras/all" % show_section)[1] if show_section else {}
    check("a library's extras are a section leaf and a hub, and an item's extras/{n} is a 404",
          (status_c10c, status_all_c10c, status_deep_c10c, status_hub_c10c, status_item_c10c)
          == (200, 200, 404, 200, 404)
          and extras_c10c.get("Directory") == [{"key": "all", "title": "All %s" % titles_c10c.get(str(movie_section))}]
          and extras_c10c.get("title1") == titles_c10c.get(str(movie_section)) and "nocache" not in extras_c10c
          and all_c10c.get("size") == 0 and not all_c10c.get("Metadata")
          and all_c10c.get("thumb") == extras_c10c.get("thumb")
          and hub_c10c == {"size": 0, "allowSync": True, "identifier": "com.plexapp.plugins.library"}
          and (not show_section or show_extras_c10c.get("nocache") is True),
          "HTTP %s/%s/%s/%s/%s, extras %s, all %s, hub %s"
          % (status_c10c, status_all_c10c, status_deep_c10c, status_hub_c10c, status_item_c10c,
             extras_c10c, all_c10c, hub_c10c))

    # Measured on plexscratch: a settings save pushes one `preference` frame per written id,
    # in ASCII order of the id, whether or not the value changed -- and FriendlyName is the
    # server's name everywhere, with an empty one falling back to the host name. plexsim
    # stored the values, pushed nothing, and kept its old name on /, /media/providers and
    # /servers.
    def prefs_c10d(query):
        return request(base, "PUT", "/:/prefs?" + query)[0]
    def name_c10d():
        return (get(base, "/")[1].get("friendlyName"), get(base, "/media/providers")[1].get("friendlyName"),
                [s.get("name") for s in get(base, "/servers")[1].get("Server") or []])
    before_c10d = name_c10d()
    _, frames_c10d = notifications(base, lambda: prefs_c10d(
        "FriendlyName=c10%20name&ButlerStartHour=4&ButlerEndHour=6"), seconds=3)
    settings_c10d = [s for f in frames_c10d or []
                     for s in (f.get("NotificationContainer") or {}).get("Setting") or []]
    kinds_c10d = [(f.get("NotificationContainer") or {}).get("type") for f in frames_c10d or []]
    named_c10d = name_c10d()
    prefs_c10d("FriendlyName=")
    empty_c10d = get(base, "/")[1].get("friendlyName")
    value_c10d = next((s.get("value") for s in get(base, "/:/prefs")[1].get("Setting") or []
                       if s.get("id") == "FriendlyName"), None)
    check("a settings save pushes a preference frame per id and FriendlyName names the server",
          [s.get("id") for s in settings_c10d] == ["ButlerEndHour", "ButlerStartHour", "FriendlyName"]
          and set(kinds_c10d) == {"preference"}
          and all(s.get("label") == "" and s.get("summary") == "" and "enumValues" not in s
                  for s in settings_c10d)
          and [s.get("value") for s in settings_c10d] == [6, 4, "c10 name"]
          and named_c10d == ("c10 name", "c10 name", ["c10 name"])
          and empty_c10d not in ("", None, before_c10d[0], "c10 name") and value_c10d == "",
          "frames %s, names %s -> %s, empty -> %r (pref %r)"
          % ([(s.get("id"), s.get("value")) for s in settings_c10d], before_c10d, named_c10d,
             empty_c10d, value_c10d))

    # Measured on plexscratch: track selection is a POST as well as a PUT -- the same
    # endpoint, the same 200 -- where plexsim 404'd the POST.
    row_c10e = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    part_c10e = ((row_c10e.get("Media") or [{}])[0].get("Part") or [{}])[0]
    audio_c10e = next((s.get("id") for s in part_c10e.get("Stream") or []
                       if s.get("streamType") == 2), None)
    if audio_c10e:
        status_post_c10e, _, _ = request(base, "POST", "/library/parts/%s?audioStreamID=%s"
                                                       % (part_c10e.get("id"), audio_c10e))
        status_put_c10e, _, _ = request(base, "PUT", "/library/parts/%s?audioStreamID=%s"
                                                     % (part_c10e.get("id"), audio_c10e))
        status_get_c10e, _, _ = request(base, "GET", "/library/parts/%s?audioStreamID=%s"
                                                     % (part_c10e.get("id"), audio_c10e))
        check("track selection takes a POST as well as a PUT",
              (status_post_c10e, status_put_c10e) == (200, 200) and status_get_c10e != 200,
              "POST %s, PUT %s, GET %s" % (status_post_c10e, status_put_c10e, status_get_c10e))
    else:
        skip("track selection takes a POST as well as a PUT", "need an audio stream")

    # Measured on plexscratch: a butler task run answers an EMPTY body and pushes one
    # `butler` activity around the task's own frames, ending with its message; `POST
    # /butler` and `DELETE /butler` are empty too, and `DELETE /butler/{Task}` -- which
    # plexsim answered 200 -- is a 404. plexsim answered the task LIST to the first three
    # and pushed no frames at all, so Settings > Scheduled Tasks showed nothing running.
    _, frames_c10f = notifications(base, lambda: request(base, "POST", "/butler/BackupDatabase"),
                                   seconds=4)
    events_c10f = [(a.get("event"), (a.get("Activity") or {}).get("type"),
                    (a.get("Activity") or {}).get("title"))
                   for f in frames_c10f or []
                   for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
    messages_c10f = [p.get("message") for f in frames_c10f or []
                     for p in (f.get("NotificationContainer") or {}).get("ProgressNotification") or []]
    after_c10f = [a.get("type") for a in get(base, "/activities")[1].get("Activity") or []]
    status_run_c10f, body_run_c10f, _ = request(base, "POST", "/butler/BackupDatabase")
    status_all_c10f, body_all_c10f, _ = request(base, "POST", "/butler")
    status_del_c10f, body_del_c10f, _ = request(base, "DELETE", "/butler")
    status_task_c10f, _, _ = request(base, "DELETE", "/butler/BackupDatabase")
    check("a butler run is an empty 200 that pushes a butler activity, and a task DELETE is a 404",
          events_c10f[:1] == [("started", "butler", "Butler tasks")]
          and events_c10f[-1:] == [("ended", "butler", "Butler tasks")]
          and ("started", "general.db.backup", "Database") in events_c10f
          and ("ended", "general.db.backup", "Database") in events_c10f
          and "The butler backed up your database." in messages_c10f
          and "butler" not in after_c10f
          and is_empty_200(status_run_c10f, body_run_c10f)
          and is_empty_200(status_all_c10f, body_all_c10f)
          and is_empty_200(status_del_c10f, body_del_c10f)
          and status_task_c10f == 404,
          "events %s, messages %s, after %s, HTTP %s/%s/%s/%s"
          % (events_c10f, messages_c10f, after_c10f, status_run_c10f, status_all_c10f,
             status_del_c10f, status_task_c10f))

    # Measured on plexscratch: the Edit-smart-list Save is `PUT {list}/items?uri=<listing>`,
    # which REPLACES the stored query and answers the list ROW -- for a playlist and a
    # collection alike -- while an ITEM uri and no uri stay 400s and `DELETE` on a smart
    # playlist's items is a 400. plexsim refused every uri on a smart list, so Save showed
    # "There was a problem updating this item.", and cleared a smart playlist on a DELETE.
    machine_c10g = get(base, "/identity")[1].get("machineIdentifier")
    def uri_c10g(query):
        return "server://%s/com.plexapp.plugins.library/library/sections/%s/%s" % (
            machine_c10g, movie_section, query)
    status_create_c10g, created_c10g, _ = container(
        base, "POST", "/playlists?type=video&title=c10+smart&smart=1&uri="
        + urllib.parse.quote(uri_c10g("all?type=1"), safe=""))
    playlist_c10g = (created_c10g.get("Metadata") or [{}])[0].get("ratingKey")
    listing_c10g = uri_c10g("all?type=1&sort=titleSort%3Adesc&limit=1")
    status_save_c10g, saved_c10g, _ = container(
        base, "PUT", "/playlists/%s/items?uri=%s"
        % (playlist_c10g, urllib.parse.quote(listing_c10g, safe="")))
    row_c10g = (saved_c10g.get("Metadata") or [{}])[0]
    items_c10g = get(base, "/playlists/%s/items" % playlist_c10g)[1].get("Metadata") or []
    detail_c10g = (get(base, "/playlists/%s" % playlist_c10g)[1].get("Metadata") or [{}])[0]
    status_item_c10g, _, _ = request(base, "PUT", "/playlists/%s/items?uri=%s" % (
        playlist_c10g, urllib.parse.quote(
            "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine_c10g, movie),
            safe="")))
    status_none_c10g, _, _ = request(base, "PUT", "/playlists/%s/items" % playlist_c10g)
    status_clear_c10g, _, _ = request(base, "DELETE", "/playlists/%s/items" % playlist_c10g)
    status_coll_c10g, collection_c10g, _ = container(
        base, "POST", "/library/collections?type=1&title=c10+smart+collection&smart=1"
        "&sectionId=%s&uri=%s" % (movie_section, urllib.parse.quote(uri_c10g("all?type=1"), safe="")))
    key_c10g = (collection_c10g.get("Metadata") or [{}])[0].get("ratingKey")
    status_csave_c10g, csaved_c10g, _ = container(
        base, "PUT", "/library/collections/%s/items?uri=%s"
        % (key_c10g, urllib.parse.quote(listing_c10g, safe="")))
    children_c10g = get(base, "/library/collections/%s/children" % key_c10g)[1].get("Metadata") or []
    check("saving an edited smart list replaces its query and answers the list row",
          status_create_c10g == 200 and status_save_c10g == 200
          and row_c10g.get("ratingKey") == playlist_c10g and row_c10g.get("smart") is True
          and len(items_c10g) == 1 and detail_c10g.get("leafCount") == 1
          and "limit%3D1" in (detail_c10g.get("content") or "")
          and (status_item_c10g, status_none_c10g, status_clear_c10g) == (400, 400, 400)
          and status_coll_c10g == 200 and status_csave_c10g == 200
          and (csaved_c10g.get("Metadata") or [{}])[0].get("ratingKey") == key_c10g
          and len(children_c10g) == 1,
          "HTTP %s/%s, row %r, items %s, content %r, refusals %s/%s/%s, collection %s/%s children %s"
          % (status_create_c10g, status_save_c10g, row_c10g.get("ratingKey"), len(items_c10g),
             detail_c10g.get("content"), status_item_c10g, status_none_c10g, status_clear_c10g,
             status_coll_c10g, status_csave_c10g, len(children_c10g)))

    # Measured on plexscratch: an artwork UPLOAD is `POST /library/metadata/{id}/{posters,arts}`
    # with the image as the body, and it answers an EMPTY 200 -- the uploaded image becomes an
    # `upload://` candidate, selected, the row's thumb and art both re-version, and one state-5
    # frame is pushed. Picking a candidate (`PUT .../poster?url=`) answers a NEW versioned path
    # and stores THAT, never the url itself. plexsim answered the candidate list to the upload
    # and stored nothing, and wrote the raw url into `thumb` -- so a client set the tile's src
    # to `upload://posters/...`. A playlist's and a collection's artwork list live under
    # /library/metadata/{ratingKey} too, which plexsim 404'd.
    def row_c10h():
        return (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    def posters_c10h():
        return get(base, "/library/metadata/%s/posters" % movie)[1].get("Metadata") or []
    before_c10h = row_c10h()
    png_c10h = (b"\x89PNG\r\n\x1a\n" + b"c10-upload-fixture" * 4)
    _, frames_c10h = notifications(
        base, lambda: request(base, "POST", "/library/metadata/%s/posters" % movie, body=png_c10h),
        seconds=3)
    status_up_c10h, body_up_c10h, _ = request(base, "POST", "/library/metadata/%s/arts" % movie,
                                              body=png_c10h + b"art")
    after_c10h = row_c10h()
    uploaded_c10h = [p for p in posters_c10h() if str(p.get("ratingKey")).startswith("upload://")]
    states_c10h = [(e.get("type"), e.get("state"), e.get("metadataState"))
                   for f in frames_c10h or []
                   for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
    spare_c10h = next((p.get("ratingKey") for p in posters_c10h()
                       if p.get("ratingKey") != (uploaded_c10h or [{}])[0].get("ratingKey")), None)
    status_pick_c10h, picked_c10h, _ = request(base, "PUT", "/library/metadata/%s/poster?url=%s"
                                               % (movie, urllib.parse.quote(spare_c10h or "", safe="")))
    thumb_c10h = row_c10h().get("thumb")
    selected_c10h = [p.get("ratingKey") for p in posters_c10h() if p.get("selected")]
    status_bogus_c10h, bogus_c10h, _ = request(
        base, "PUT", "/library/metadata/%s/poster?url=%s"
        % (movie, urllib.parse.quote("upload://posters/nope", safe="")))
    after_bogus_c10h = row_c10h().get("thumb")
    lists_c10h = []
    for path_c10h in ("/playlists?type=video&title=c10+art&uri="
                      + urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                           % (get(base, "/identity")[1].get("machineIdentifier"), movie),
                                           safe=""),):
        created_c10h = (container(base, "POST", path_c10h)[1].get("Metadata") or [{}])[0]
        if created_c10h.get("ratingKey"):
            status_list_c10h, list_c10h, _ = get(
                base, "/library/metadata/%s/posters" % created_c10h["ratingKey"])
            lists_c10h.append((status_list_c10h, (list_c10h.get("Metadata") or [{}])[0]))
    check("an artwork upload is an empty 200 that becomes the selected candidate, and a pick is versioned",
          is_empty_200(status_up_c10h, body_up_c10h)
          and len(uploaded_c10h) == 1 and uploaded_c10h[0].get("selected") is True
          and after_c10h.get("thumb") != before_c10h.get("thumb")
          and after_c10h.get("art") != before_c10h.get("art")
          and states_c10h == [(1, 5, None)]
          and status_pick_c10h == 200 and picked_c10h.decode().startswith("/library/metadata/%s/thumb/" % movie)
          and thumb_c10h == picked_c10h.decode() and selected_c10h == [spare_c10h]
          and status_bogus_c10h == 200
          and after_bogus_c10h.startswith("/library/metadata/%s/thumb/" % movie)
          and lists_c10h and lists_c10h[0][0] == 200
          and str(lists_c10h[0][1].get("ratingKey")).startswith("default://"),
          "upload %s %r, candidates %s, thumb %r -> %r, frames %s, pick %s %r -> %r, selected %s, bogus %r, list %s"
          % (status_up_c10h, body_up_c10h[:40], len(uploaded_c10h), before_c10h.get("thumb"),
             after_c10h.get("thumb"), states_c10h, status_pick_c10h, picked_c10h[:60], thumb_c10h,
             selected_c10h, after_bogus_c10h, lists_c10h))

    # Measured on plexscratch: a match needs a `name` (a guid alone is a 400), an EMPTY guid
    # with a name is a 200 that just renames and refreshes, and matching to a guid ANOTHER
    # row in the library already carries MERGES this row into that one -- this row is deleted,
    # the survivor keeps both Media, and the frames are the merge's followed by the survivor's
    # refresh. Every match ends its activity. plexsim accepted a nameless match, refused an
    # empty guid, left two rows with one guid, and started a `Matching` activity that never
    # ended -- one per match, for the life of the process.
    spare_c10i = spare if spare and spare != movie else None
    if spare_c10i:
        # The row that gets ABSORBED is the spare, not `movie`: a merge deletes it, and the
        # checks after this block still need `movie`.
        def titles_c10i(key):
            return (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
        target_c10i = titles_c10i(movie)
        status_noname_c10i, _, _ = request(base, "PUT", "/library/metadata/%s/match?guid=%s"
                                           % (spare_c10i, urllib.parse.quote(target_c10i.get("guid") or "", safe="")))
        status_empty_c10i, body_empty_c10i, _ = request(
            base, "PUT", "/library/metadata/%s/match?guid=&name=c10+renamed" % spare_c10i)
        renamed_c10i = titles_c10i(spare_c10i)
        _, frames_c10i = notifications(
            base, lambda: request(base, "PUT", "/library/metadata/%s/match?guid=%s&name=%s&year=%s"
                                  % (spare_c10i, urllib.parse.quote(target_c10i.get("guid") or "", safe=""),
                                     "c10+matched", target_c10i.get("year") or 2000)), seconds=3)
        states_c10i = [(e.get("type"), e.get("state"), e.get("metadataState"), str(e.get("itemID")))
                       for f in frames_c10i or []
                       for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
        events_c10i = [a.get("event") for f in frames_c10i or []
                       for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
        gone_c10i, _, _ = request(base, "GET", "/library/metadata/%s" % spare_c10i)
        survivor_c10i = titles_c10i(movie)
        activities_c10i = [a.get("title") for a in get(base, "/activities")[1].get("Activity") or []]
        check("a match needs a name, an empty guid renames, and a guid another row holds merges",
              status_noname_c10i == 400
              and is_empty_200(status_empty_c10i, body_empty_c10i)
              and renamed_c10i.get("title") == "c10 renamed"
              and states_c10i[:2] == [(1, 3, None, str(spare_c10i)), (1, 9, "deleted", str(spare_c10i))]
              and (-1, 3, None, str(movie)) in states_c10i
              and states_c10i[-1] == (1, 5, None, str(movie))
              and events_c10i[:1] == ["started"] and events_c10i[-1:] == ["ended"]
              and gone_c10i == 404
              and len(survivor_c10i.get("Media") or []) == 2
              and "Matching" not in activities_c10i,
              "HTTP %s/%s, renamed %r, frames %s, events %s, gone %s, survivor media %s, activities %s"
              % (status_noname_c10i, status_empty_c10i, renamed_c10i.get("title"), states_c10i,
                 events_c10i, gone_c10i, len(survivor_c10i.get("Media") or []), activities_c10i))
    else:
        skip("a match needs a name, an empty guid renames, and a guid another row holds merges",
             "need a second movie")

    # Measured on plexscratch: a scan's `updated` frames carry a Context naming the library,
    # and a SHOW library steps per SEASON ("Scanning <show>/Season 02", subtitle
    # "<show> - Season 02") where a movie library steps per movie. plexsim sent no Context
    # and stepped a show library by show.
    if show_section:
        _, frames_c10j = notifications(
            base, lambda: request(base, "GET", "/library/sections/%s/refresh" % show_section),
            seconds=4)
        updates_c10j = [(a.get("Activity") or {}) for f in frames_c10j or []
                        for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []
                        if a.get("event") == "updated"]
        messages_c10j = [p.get("message") for f in frames_c10j or []
                         for p in (f.get("NotificationContainer") or {}).get("ProgressNotification") or []]
        stepped_c10j = [a for a in updates_c10j if a.get("subtitle")]
        check("a scan names its library in a Context and steps a show library by season",
              stepped_c10j
              and all((a.get("Context") or {}).get("librarySectionID") == str(show_section)
                      for a in stepped_c10j)
              and all(" - Season " in (a.get("subtitle") or "") for a in stepped_c10j)
              and any("/Season " in (m or "") for m in messages_c10j),
              "subtitles %s, contexts %s, messages %s"
              % ([a.get("subtitle") for a in stepped_c10j],
                 [a.get("Context") for a in stepped_c10j], messages_c10j))
    else:
        skip("a scan names its library in a Context and steps a show library by season",
             "need a show library")

    # Measured on plexscratch: writing a show's `showOrdering` re-reads the whole show --
    # the show's own refresh sequence, then each SEASON (state 5 `processing`, 4 `loading`)
    # and each EPISODE inside it (5 `processing`, 4 `loading` twice, 5), the episodes again
    # with `mediaState` analyzing then thumbnailing, and the activity stepping per season --
    # and ends with the show at state 5. plexsim stored the value and pushed nothing, so a
    # re-ordering made in one client was invisible in every other until a reload.
    if show:
        _, frames_c10k = notifications(
            base, lambda: request(base, "PUT", "/library/metadata/%s/prefs?showOrdering=dvd" % show),
            seconds=4)
        entries_c10k = [e for f in frames_c10k or []
                        for e in (f.get("NotificationContainer") or {}).get("TimelineEntry") or []]
        states_c10k = [(e.get("type"), e.get("state"), e.get("metadataState"), e.get("mediaState"))
                       for e in entries_c10k]
        seasons_c10k = {str(r.get("ratingKey")) for r in
                        get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []}
        episodes_c10k = {str(r.get("ratingKey")) for r in
                         get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []}
        touched_c10k = {str(e.get("itemID")) for e in entries_c10k}
        subtitles_c10k = [(a.get("Activity") or {}).get("subtitle")
                          for f in frames_c10k or []
                          for a in (f.get("NotificationContainer") or {}).get("ActivityNotification") or []]
        check("a show's prefs write pushes the refresh cascade of its seasons and episodes",
              states_c10k[:2] == [(2, 3, "queued", None), (2, 3, "processing", None)]
              and states_c10k[-1] == (2, 5, None, None)
              and seasons_c10k and seasons_c10k <= touched_c10k
              and episodes_c10k and episodes_c10k <= touched_c10k
              and (4, 5, None, "analyzing") in states_c10k
              and (4, 5, None, "thumbnailing") in states_c10k
              and any(s and " S" in s for s in subtitles_c10k),
              "%d frames %s, seasons %s, episodes %s, touched %s, subtitles %s"
              % (len(states_c10k), states_c10k[:6], seasons_c10k, episodes_c10k, touched_c10k,
                 subtitles_c10k))
    else:
        skip("a show's prefs write pushes the refresh cascade of its seasons and episodes",
             "need a show")

    # Measured on plexscratch, whose clips are half an hour long: a play is marked watched
    # only when a beat CROSSES 90% inside its own session -- the session must have reported a
    # position under 90% of the duration THAT BEAT carries. A cold item beaten once at 95%
    # records nothing (five climbing beats from 95% to 99% record nothing either), another
    # client's earlier position does not enable it, and neither does a stored `/:/progress`
    # offset. plexsim completed on the position alone, so dragging the scrubber to the end,
    # or a second client joining near the end, silently marked the item watched.
    def beat_c10l(key, position, duration, session, state="playing"):
        path = ("/:/timeline?ratingKey=%s&key=%s&state=%s&hasMDE=1&time=%s"
                % (key, urllib.parse.quote("/library/metadata/%s" % key, safe=""), state, position))
        if duration is not None:
            path += "&duration=%s" % duration
        status, body, _ = request(base, "GET", path + "&X-Plex-Session-Identifier=" + session)
        try:
            return json.loads(body).get("MediaContainer", {})
        except ValueError:
            return {"status": status}
    def viewed_c10l(key):
        row = (get(base, "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]
        return (row.get("viewCount"), row.get("viewOffset"))
    # One row is enough, and the suite has watched and merged rows by the time this runs:
    # every case is a distinct SESSION against the same item, and what is asserted is
    # whether the beat ADDS a view.
    span_c10l = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    high_c10l, low_c10l = int(span_c10l * 0.95), int(span_c10l * 0.3)
    def views_c10l():
        row = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
        return int(row.get("viewCount") or 0)
    was_cold_c10l = views_c10l()
    answer_cold_c10l = beat_c10l(movie, high_c10l, span_c10l, "c10l-cold")
    climb_c10l = [beat_c10l(movie, int(span_c10l * step), span_c10l, "c10l-cold").get("playbackState")
                  for step in (0.96, 0.97, 0.98)]
    now_cold_c10l = views_c10l()
    beat_c10l(movie, low_c10l, span_c10l, "c10l-cross")
    answer_cross_c10l = beat_c10l(movie, high_c10l, span_c10l, "c10l-cross")
    now_cross_c10l = views_c10l()
    beat_c10l(movie, low_c10l, span_c10l, "c10l-clientA")
    answer_other_c10l = beat_c10l(movie, high_c10l, span_c10l, "c10l-clientB")
    now_other_c10l = views_c10l()
    check("a play completes only when a beat crosses 90% inside its own session",
          bool(span_c10l) and answer_cold_c10l.get("playbackState") == "ignore"
          and now_cold_c10l == was_cold_c10l
          and climb_c10l == ["ignore", "ignore", "ignore"]
          and answer_cross_c10l.get("playbackState") == "complete"
          and now_cross_c10l == was_cold_c10l + 1
          and answer_other_c10l.get("playbackState") == "ignore"
          and now_other_c10l == now_cross_c10l,
          "cold %r %s->%s climb %s, crossed %r ->%s, other client %r ->%s"
          % (answer_cold_c10l.get("playbackState"), was_cold_c10l, now_cold_c10l, climb_c10l,
             answer_cross_c10l.get("playbackState"), now_cross_c10l,
             answer_other_c10l.get("playbackState"), now_other_c10l))

    # Measured on plexscratch: a beat with NO `duration` stores nothing and completes nothing,
    # and answers the offset the item already has; `duration=0` is a 200 that stores nothing,
    # where an empty one and a `time` past the duration stay 400s. plexsim fell back to the
    # item's own duration -- so a player that omits it drove the whole watched-state machine --
    # and 400'd `duration=0`.
    bare_c10m = [r["ratingKey"] for r in
                 (get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or [])
                 if not r.get("viewCount") and not r.get("viewOffset")]
    if bare_c10m:
        key_c10m = bare_c10m[-1]
        span_c10m = int((get(base, "/library/metadata/%s" % key_c10m)[1]
                         .get("Metadata") or [{}])[0].get("duration") or 0)
        def timeline_c10m(position, duration, session="c10m"):
            path = ("/:/timeline?ratingKey=%s&key=%s&state=playing&hasMDE=1&time=%s"
                    % (key_c10m, urllib.parse.quote("/library/metadata/%s" % key_c10m, safe=""), position))
            if duration is not None:
                path += "&duration=%s" % duration
            status, body, _ = request(base, "GET", path + "&X-Plex-Session-Identifier=" + session)
            try:
                return status, json.loads(body).get("MediaContainer", {})
            except ValueError:
                return status, {}
        def stored_c10m():
            row = (get(base, "/library/metadata/%s" % key_c10m)[1].get("Metadata") or [{}])[0]
            return (row.get("viewCount"), row.get("viewOffset"))
        status_none_c10m, answer_none_c10m = timeline_c10m(130000, None)
        cold_c10m = stored_c10m()
        status_high_c10m, answer_high_c10m = timeline_c10m(int(span_c10m * 0.95), None)
        high_c10m = stored_c10m()
        timeline_c10m(200000, span_c10m)
        status_after_c10m, answer_after_c10m = timeline_c10m(210000, None)
        status_zero_c10m, answer_zero_c10m = timeline_c10m(230000, 0)
        status_empty_c10m, _ = timeline_c10m(230000, "")
        status_past_c10m, _ = timeline_c10m(span_c10m + 10000, span_c10m)
        check("a beat with no duration stores nothing, and duration=0 is a 200",
              status_none_c10m == 200 and answer_none_c10m.get("playbackState") == "ignore"
              and cold_c10m == (None, None)
              and answer_high_c10m.get("playbackState") == "ignore" and high_c10m == (None, None)
              and status_after_c10m == 200 and answer_after_c10m.get("viewOffset") == 200000
              and stored_c10m()[1] == 200000
              and status_zero_c10m == 200 and answer_zero_c10m.get("viewOffset") == 200000
              and status_empty_c10m == 400 and status_past_c10m == 400,
              "none %s %r %s, 95%% %r %s, after %s %r, zero %s %r, empty %s, past %s"
              % (status_none_c10m, answer_none_c10m.get("playbackState"), cold_c10m,
                 answer_high_c10m.get("playbackState"), high_c10m, status_after_c10m,
                 answer_after_c10m.get("viewOffset"), status_zero_c10m,
                 answer_zero_c10m.get("viewOffset"), status_empty_c10m, status_past_c10m))
    else:
        skip("a beat with no duration stores nothing, and duration=0 is a 200",
             "need an unwatched movie")

    # Measured on plexscratch: a session driven from a play queue carries `playQueueItemID`
    # and `playQueueID` on every `playing` frame -- the Now Playing panel and companion
    # clients tie the frame to the queue item they hold. plexsim sent neither.
    machine_c10n = get(base, "/identity")[1].get("machineIdentifier")
    queue_c10n = container(base, "POST", "/playQueues?type=video&uri=%s&X-Plex-Client-Identifier=c10n"
                           % urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                                % (machine_c10n, movie), safe=""))[1]
    item_c10n = (queue_c10n.get("Metadata") or [{}])[0].get("playQueueItemID")
    id_c10n = queue_c10n.get("playQueueID")
    span_c10n = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    _, frames_c10n = notifications(base, lambda: request(
        base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=playing&hasMDE=1&time=300000&duration=%s"
                     "&playQueueItemID=%s&X-Plex-Client-Identifier=c10n&X-Plex-Session-Identifier=c10n1"
                     % (movie, urllib.parse.quote("/library/metadata/%s" % movie, safe=""),
                        span_c10n, item_c10n)), seconds=3)
    playing_c10n = [p for f in frames_c10n or []
                    for p in (f.get("NotificationContainer") or {}).get("PlaySessionStateNotification") or []]
    check("a play queue's session names the queue and the item on every playing frame",
          playing_c10n and item_c10n
          and playing_c10n[0].get("playQueueItemID") == item_c10n
          and playing_c10n[0].get("playQueueID") == id_c10n,
          "queue %s item %s, frame %s" % (id_c10n, item_c10n, playing_c10n[0] if playing_c10n else None))

    # Measured on plexscratch: a /status/sessions row does NOT carry `Guid` or `Image`, which
    # the item's own detail row does, and its `Player` carries a `profile`. plexsim copied the
    # detail row wholesale and sent no profile.
    span_c10o = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=playing&hasMDE=1&time=300000&duration=%s"
                         "&X-Plex-Session-Identifier=c10o" % (
                             movie, urllib.parse.quote("/library/metadata/%s" % movie, safe=""), span_c10o))
    live_c10o = next((r for r in get(base, "/status/sessions")[1].get("Metadata") or []
                      if str(r.get("ratingKey")) == str(movie)), {})
    detail_c10o = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    check("a session row drops Guid and Image and its Player names a profile",
          live_c10o and "Guid" not in live_c10o and "Image" not in live_c10o
          and ("Guid" in detail_c10o or "Image" in detail_c10o)
          and (live_c10o.get("Player") or {}).get("profile"),
          "session-only %s, detail-only %s, profile %r"
          % (sorted(set(live_c10o) - set(detail_c10o)), sorted(set(detail_c10o) - set(live_c10o)),
             (live_c10o.get("Player") or {}).get("profile")))

    # Measured on both reference servers: /statistics/media takes `accountID` and `deviceID`
    # like /statistics/bandwidth does -- an id neither has answers `size 0` with no Device and
    # no Account row, the account that played keeps its rows, and an empty or non-numeric
    # `accountID` is a 400. plexsim answered the whole server to every one of them.
    span_c10p = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    for state_c10p, position_c10p in (("playing", 300000), ("stopped", 400000)):
        request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=%s&hasMDE=1&time=%s&duration=%s"
                             "&X-Plex-Session-Identifier=c10p&X-Plex-Client-Identifier=c10p"
                             % (movie, urllib.parse.quote("/library/metadata/%s" % movie, safe=""),
                                state_c10p, position_c10p, span_c10p))
    def media_stats_c10p(query):
        status, body, _ = request(base, "GET", "/statistics/media" + query)
        try:
            return status, json.loads(body).get("MediaContainer", {})
        except ValueError:
            return status, {}
    status_all_c10p, all_c10p = media_stats_c10p("")
    status_dev_c10p, dev_c10p = media_stats_c10p("?deviceID=99999")
    status_acct_c10p, acct_c10p = media_stats_c10p("?accountID=99999")
    status_mine_c10p, mine_c10p = media_stats_c10p("?accountID=1")
    status_empty_c10p, _ = media_stats_c10p("?accountID=")
    status_word_c10p, _ = media_stats_c10p("?accountID=abc")
    check("media statistics filter by account and device, and refuse an unparseable account",
          status_all_c10p == 200 and (all_c10p.get("size") or 0) > 0
          and (status_dev_c10p, dev_c10p.get("size")) == (200, 0)
          and not dev_c10p.get("Device") and not dev_c10p.get("Account")
          and (status_acct_c10p, acct_c10p.get("size")) == (200, 0)
          and status_mine_c10p == 200 and mine_c10p.get("size") == all_c10p.get("size")
          and status_empty_c10p == 400 and status_word_c10p == 400,
          "all %s/%s, device %s/%s, account %s/%s, mine %s/%s, empty %s, word %s"
          % (status_all_c10p, all_c10p.get("size"), status_dev_c10p, dev_c10p.get("size"),
             status_acct_c10p, acct_c10p.get("size"), status_mine_c10p, mine_c10p.get("size"),
             status_empty_c10p, status_word_c10p))


    # Measured on kids (1.43.4) and zen (1.42.2): `?limit=N` caps a listing's rows.
    # `limit=0` answers ONE row, a negative one every row, and `limit=abc` is a 500 --
    # and it is a property of the QUERY grammar, so /newest and /children ignore it.
    # plexsim read `limit` only in /query/parse, so every capped list was the whole
    # section: a smart list saved with a limit previewed as if it had none.
    def rows_c11a(path):
        status, body, _ = get(base, path)
        return status, len(body.get("Metadata") or [])
    section_c11a = "/library/sections/%s/all" % movie_section
    # Counted live rather than assumed: by this point in the suite the match check
    # has merged the spare movie away, so the section holds two rows, not three.
    total_c11a = rows_c11a(section_c11a)[1]
    one_c11a = rows_c11a(section_c11a + "?limit=1")
    zero_c11a = rows_c11a(section_c11a + "?limit=0")
    neg_c11a = rows_c11a(section_c11a + "?limit=-1")
    word_c11a = request(base, "GET", section_c11a + "?limit=abc")[0]
    lib_c11a = rows_c11a("/library/all?type=1&limit=1")
    newest_c11a = rows_c11a("/library/sections/%s/newest?limit=1" % movie_section)
    newest_plain_c11a = rows_c11a("/library/sections/%s/newest" % movie_section)
    kids_c11a = rows_c11a("/library/metadata/%s/children?limit=1" % show)
    kids_plain_c11a = rows_c11a("/library/metadata/%s/children" % show)
    check("a listing caps at its limit, 0 answers one row and a negative one all",
          total_c11a >= 2 and one_c11a == (200, 1) and zero_c11a == (200, 1)
          and neg_c11a == (200, total_c11a) and word_c11a == 500
          and lib_c11a == (200, 1)
          and newest_c11a == newest_plain_c11a and kids_c11a == kids_plain_c11a,
          "total %s, limit1 %s, limit0 %s, limit-1 %s, abc %s, library %s, "
          "newest %s vs %s, children %s vs %s"
          % (total_c11a, one_c11a, zero_c11a, neg_c11a, word_c11a, lib_c11a,
             newest_c11a, newest_plain_c11a, kids_c11a, kids_plain_c11a))


    # Measured on kids (1.43.4) and zen (1.42.2), identically on both: a section
    # listing answers 500 to a `type=` it cannot parse and 200-with-no-rows to a
    # number it does not know, and /library/all answers 400 to the same values while
    # keeping 500 for an empty one. plexsim answered the whole grid to all of them,
    # so a client that sent a malformed type read the library as if it had matched.
    def status_c11b(path):
        return request(base, "GET", path)[0]
    def rows_c11b(path):
        status, body, _ = get(base, path)
        return status, len(body.get("Metadata") or [])
    section_c11b = "/library/sections/%s/all" % movie_section
    check("a listing refuses a type it cannot parse, and empties one it cannot place",
          status_c11b(section_c11b + "?type=abc") == 500
          and status_c11b(section_c11b + "?type=") == 500
          and status_c11b(section_c11b + "?type=1.5") == 500
          and rows_c11b(section_c11b + "?type=-1") == (200, 0)
          and rows_c11b(section_c11b + "?type=99") == (200, 0)
          and rows_c11b(section_c11b + "?type=1")[1] == rows_c11b(section_c11b)[1]
          and status_c11b("/library/all?type=abc") == 400
          and status_c11b("/library/all?type=1.5") == 400
          and status_c11b("/library/all?type=") == 500,
          "section abc/empty/1.5 %s/%s/%s, -1 %s, 99 %s, all %s/%s/%s"
          % (status_c11b(section_c11b + "?type=abc"), status_c11b(section_c11b + "?type="),
             status_c11b(section_c11b + "?type=1.5"), rows_c11b(section_c11b + "?type=-1"),
             rows_c11b(section_c11b + "?type=99"), status_c11b("/library/all?type=abc"),
             status_c11b("/library/all?type=1.5"), status_c11b("/library/all?type=")))


    # Measured on kids (1.43.4) and zen (1.42.2): `excludeElements=Media` drops the
    # Media children and substitutes `mediaCount` on EVERY listing, including the
    # three cycles 9 and 10 added -- the section's allLeaves, /library/people/{id}/media
    # and /hubs/items. plexsim applied it to the grid and to child lists only, so a
    # client asking for the cheap list to lay a page out got the expensive one and no
    # count to draw the badge from.
    def excl_c11c(path):
        status, body, _ = get(base, path)
        rows_c11c = body.get("Metadata") or []
        return (status, sum(len(r.get("Media") or []) for r in rows_c11c),
                sum(1 for r in rows_c11c if "mediaCount" in r), len(rows_c11c))
    def row_c11c():
        status, body, _ = get(base, "/library/metadata/%s" % movie)
        return (body.get("Metadata") or [{}])[0]
    person_c11c = None
    for tag_c11c in (row_c11c().get("Role") or []) + (row_c11c().get("Director") or []):
        if tag_c11c.get("id"):
            person_c11c = tag_c11c["id"]
            break
    leaves_plain_c11c = excl_c11c("/library/sections/%s/allLeaves" % movie_section)
    leaves_c11c = excl_c11c("/library/sections/%s/allLeaves?excludeElements=Media" % movie_section)
    hub_c11c = excl_c11c("/hubs/items?identifier=movie.recentlyadded.%s&excludeElements=Media"
                         % movie_section)
    people_c11c = excl_c11c("/library/people/%s/media?excludeElements=Media" % person_c11c) \
        if person_c11c else (200, 0, 1, 1)
    check("excludeElements applies to the section leaves, a person's media and a hub",
          leaves_plain_c11c[1] > 0
          and leaves_c11c[0] == 200 and leaves_c11c[1] == 0 and leaves_c11c[2] == leaves_c11c[3]
          and hub_c11c[0] == 200 and hub_c11c[1] == 0 and hub_c11c[2] == hub_c11c[3]
          and people_c11c[0] == 200 and people_c11c[1] == 0 and people_c11c[2] == people_c11c[3],
          "allLeaves plain %s excluded %s, hub %s, person %s %s"
          % (leaves_plain_c11c, leaves_c11c, hub_c11c, person_c11c, people_c11c))


    # Measured on kids (1.43.4) and zen (1.42.2): a section's `allLeaves` is a full
    # query surface like `/all` -- `type=`, the filters, `sort=` and `limit=` all
    # apply, an unknown sort key and an unparseable type are 500s. plexsim answered
    # its whole leaf list to every one of them.
    # Stated as "allLeaves answers the same query the grid answers" rather than against a
    # hand-computed order: on a movie library the two are over the same rows, and by this
    # point in the suite the fixture's titles have been edited into ties, which an
    # asserted `desc == reversed(plain)` cannot survive.
    def leaves_c11d(query_c11d="", leaf_c11d="allLeaves"):
        status, body, _ = get(base, "/library/sections/%s/%s%s" % (movie_section, leaf_c11d, query_c11d))
        return status, [r.get("ratingKey") for r in (body.get("Metadata") or [])]
    year_c11d = ((get(base, "/library/metadata/%s" % movie)[1]
                  .get("Metadata") or [{}])[0].get("year") or 0)
    queries_c11d = ("", "?year=%s" % year_c11d, "?year=1899", "?type=1", "?type=4",
                    "?limit=1", "?sort=titleSort:desc", "?sort=year:desc,titleSort:asc")
    same_c11d = {q_c11d: (leaves_c11d(q_c11d), leaves_c11d(q_c11d, "all")) for q_c11d in queries_c11d}
    plain_c11d = same_c11d[""][0]
    bad_sort_c11d = request(base, "GET", "/library/sections/%s/allLeaves?sort=nope" % movie_section)[0]
    bad_type_c11d = request(base, "GET", "/library/sections/%s/allLeaves?type=abc" % movie_section)[0]
    check("a section's allLeaves filters, sorts, caps and types like a listing",
          plain_c11d[0] == 200 and len(plain_c11d[1]) >= 2
          and all(leaf_c11d == grid_c11d for leaf_c11d, grid_c11d in same_c11d.values())
          and same_c11d["?year=1899"][0] == (200, [])
          and same_c11d["?type=4"][0] == (200, [])
          and same_c11d["?limit=1"][0] == (200, plain_c11d[1][:1])
          and bad_sort_c11d == 500 and bad_type_c11d == 500,
          "%s, bad sort %s, bad type %s"
          % ({q_c11d: (leaf_c11d[1], grid_c11d[1])
              for q_c11d, (leaf_c11d, grid_c11d) in same_c11d.items()},
             bad_sort_c11d, bad_type_c11d))


    # Measured on kids (1.43.4) and zen (1.42.2): `id` filters every listing although
    # no Meta advertises it -- /query/parse echoes it as a Rule with an EMPTY
    # fieldTitle. `=` is exact, `!=` excludes, a comma list is an OR, `>>`/`<<`/`>=`/`<=`
    # compare numerically, and `==`/`!==` -- the exact-string pair -- never match a
    # number. A value that is not a number sorts above every id: `id=abc` matches
    # nothing and `id!=abc` everything. plexsim reported it as an unknown field, so
    # python-plexapi's editions() (`guid=<x>&id!=<self>`) called every item its own
    # duplicate.
    def ids_c11e(query_c11e):
        status, body, _ = get(base, "/library/sections/%s/all?%s" % (movie_section, query_c11e))
        return status, [r.get("ratingKey") for r in (body.get("Metadata") or [])]
    every_c11e = ids_c11e("x=1")[1]
    first_c11e = every_c11e[0]
    check("a listing filters by id, and an unparseable id sorts above every row",
          ids_c11e("id=%s" % first_c11e) == (200, [first_c11e])
          and ids_c11e("id%%21=%s" % first_c11e) == (200, [k for k in every_c11e if k != first_c11e])
          and ids_c11e("id=%s" % ",".join(every_c11e[:2])) == (200, every_c11e[:2])
          and ids_c11e("id%%3D=%s" % first_c11e) == (200, [])
          and ids_c11e("id%%21%%3D=%s" % first_c11e) == (200, every_c11e)
          and ids_c11e("id%%3E%%3E=%s" % first_c11e)[1] == [k for k in every_c11e if int(k) > int(first_c11e)]
          and ids_c11e("id%%3C%%3C=%s" % first_c11e) == (200, [])
          and ids_c11e("id=abc") == (200, []) and ids_c11e("id%%21=abc") == (200, every_c11e)
          and ids_c11e("id=") == (200, []) and ids_c11e("id=999999999") == (200, []),
          "all %s, =%s -> %s, != -> %s, == -> %s, abc -> %s"
          % (every_c11e, first_c11e, ids_c11e("id=%s" % first_c11e)[1],
             ids_c11e("id%%21=%s" % first_c11e)[1], ids_c11e("id%%3D=%s" % first_c11e)[1],
             ids_c11e("id=abc")[1]))
    status_c11e, parsed_c11e, _ = request(base, "GET",
                                          "/library/sections/%s/query/parse?id%%21=1" % movie_section)
    try:
        query_c11e = json.loads(parsed_c11e).get("Query") or {}
    except ValueError:
        query_c11e = {}
    rules_c11e = query_c11e.get("Rule")
    rules_c11e = rules_c11e if isinstance(rules_c11e, list) else [rules_c11e or {}]
    check("query/parse echoes an id rule with an empty fieldTitle",
          status_c11e == 200 and not query_c11e.get("Error")
          and [(r.get("field"), r.get("operator"), r.get("fieldTitle"), r.get("value"))
               for r in rules_c11e] == [("id", "!=", "", "1")],
          "%s %s" % (status_c11e, parsed_c11e[-120:]))


    # Re-measured on kids (1.43.4) and zen (1.42.2), correcting cycle 9's reading:
    # `viewGroup`/`viewMode` on /library/people/{id}/media belong to an ACTOR's list
    # (tagType 6) only -- a Director, Writer or Producer list carries neither -- and
    # the list is a QUERY, so the library filters narrow it. plexsim sent the actor
    # container for every tag type and answered every row to every filter.
    # Scanned across the whole section rather than off one row: by this point the suite
    # has replaced that row's tag lists, and a check that skips is a check that never runs.
    actor_c11f = director_c11f = year_c11f = None
    for listed_c11f in (get(base, "/library/sections/%s/all" % movie_section)[1].get("Metadata") or []):
        row_c11f = (get(base, "/library/metadata/%s" % listed_c11f.get("ratingKey"))[1]
                    .get("Metadata") or [{}])[0]
        # Both ids have to come from the SAME row, or the year below narrows a list the
        # other person is not on.
        mine_c11f = next((t.get("id") for t in (row_c11f.get("Role") or []) if t.get("id")), None)
        theirs_c11f = next((t.get("id") for t in (row_c11f.get("Director") or []) if t.get("id")), None)
        if mine_c11f and theirs_c11f:
            actor_c11f, director_c11f, year_c11f = mine_c11f, theirs_c11f, row_c11f.get("year")
            break
    def media_c11f(person_c11f, query_c11f=""):
        status, body, _ = get(base, "/library/people/%s/media%s" % (person_c11f, query_c11f))
        return (status, body.get("viewGroup"), body.get("viewMode"),
                len(body.get("Metadata") or []))
    if not actor_c11f or not director_c11f:
        skip("a person's media list is typed by its tag and filters like a listing",
             "the fixture has no person tags")
    else:
        plain_c11f = media_c11f(actor_c11f)
        dir_c11f = media_c11f(director_c11f)
        narrow_c11f = media_c11f(actor_c11f, "?year=%s" % year_c11f)
        none_c11f = media_c11f(actor_c11f, "?year=1899")
        check("a person's media list is typed by its tag and filters like a listing",
              plain_c11f[0] == 200 and plain_c11f[1] == "movie" and plain_c11f[2] == 65592
              and dir_c11f[0] == 200 and dir_c11f[1] is None and dir_c11f[2] is None
              and dir_c11f[3] > 0
              and narrow_c11f[3] > 0 and narrow_c11f[3] <= plain_c11f[3]
              and none_c11f[3] == 0,
              "actor %s, director %s, year %s, none %s"
              % (plain_c11f, dir_c11f, narrow_c11f, none_c11f))


    # Measured on kids (1.43.4) and zen (1.42.2): a statistics container pages its
    # SERIES only. Device and Account are a lookup for the rows on the page -- the
    # answer carries exactly the ones its rows name -- so a window past the end
    # carries neither. plexsim sliced all three lists with the same offset, so the
    # second page of a Dashboard graph named a device it had not sent.
    span_c11g = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    for state_c11g, at_c11g, client_c11g in (("playing", 300000, "c11g1"), ("stopped", 400000, "c11g1"),
                                             ("playing", 300000, "c11g2"), ("stopped", 400000, "c11g2")):
        request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=%s&hasMDE=1&time=%s&duration=%s"
                             "&X-Plex-Session-Identifier=%s&X-Plex-Client-Identifier=%s"
                             % (movie, quote("/library/metadata/%s" % movie), state_c11g,
                                at_c11g, span_c11g, client_c11g, client_c11g))
    def stats_c11g(headers_c11g=None):
        url_c11g = base.rstrip("/") + "/statistics/media"
        req_c11g = urllib.request.Request(url_c11g, headers=dict({
            "User-Agent": UA, "Accept": "application/json",
            "X-Plex-Client-Identifier": "plexsim-ui-test", "X-Plex-Product": "Plex Web",
            "X-Plex-Platform": "Chrome"}, **(headers_c11g or {})))
        with urllib.request.urlopen(req_c11g, timeout=30) as response_c11g:
            body_c11g = json.loads(response_c11g.read()).get("MediaContainer", {})
        rows_c11g = body_c11g.get("StatisticsMedia") or []
        return (body_c11g.get("size"), body_c11g.get("totalSize"),
                sorted({str(r.get("deviceID")) for r in rows_c11g}),
                sorted(str(d.get("id")) for d in (body_c11g.get("Device") or [])),
                len(body_c11g.get("Account") or []), len(rows_c11g))
    whole_c11g = stats_c11g()
    first_c11g = stats_c11g({"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "1"})
    past_c11g = stats_c11g({"X-Plex-Container-Start": "500", "X-Plex-Container-Size": "5"})
    check("a paged statistics container keeps the Device and Account rows its page names",
          whole_c11g[5] >= 2
          and first_c11g[5] == 1 and first_c11g[2] == first_c11g[3] and first_c11g[4] == 1
          and first_c11g[1] == whole_c11g[5]
          and first_c11g[0] == first_c11g[5] + len(first_c11g[3]) + first_c11g[4]
          and past_c11g[5] == 0 and past_c11g[3] == [] and past_c11g[4] == 0
          and past_c11g[0] == 0,
          "whole %s, first page %s, past the end %s" % (whole_c11g, first_c11g, past_c11g))


    # Measured on kids (1.43.4) and zen (1.42.2): every statistics endpoint refuses a
    # `timespan` that is not a whole number with 400 -- `abc`, `1.5`, an empty value,
    # a leading space -- and answers 200 to `0`, `-1` and `06`. plexsim ran an
    # unguarded int() on it: a 500 on two endpoints, and an ignored parameter on the
    # third.
    spans_c11h = {}
    for leaf_c11h in ("media", "bandwidth", "resources"):
        for value_c11h in ("abc", "1.5", "", "%206", "6", "0", "-1"):
            spans_c11h[(leaf_c11h, value_c11h)] = request(
                base, "GET", "/statistics/%s?timespan=%s" % (leaf_c11h, value_c11h))[0]
    check("every statistics endpoint refuses a timespan that is not a whole number",
          all(spans_c11h[(leaf_c11h, bad_c11h)] == 400
              for leaf_c11h in ("media", "bandwidth", "resources")
              for bad_c11h in ("abc", "1.5", "", "%206"))
          and all(spans_c11h[(leaf_c11h, good_c11h)] == 200
                  for leaf_c11h in ("media", "bandwidth", "resources")
                  for good_c11h in ("6", "0", "-1")),
          "%s" % sorted((k[0], k[1] or "(empty)", v) for k, v in spans_c11h.items()))


    # Measured on kids (1.43.4) and zen (1.42.2): `includeStorage=1` adds
    # `durationTotal` and `storageTotal` to a section grid row, at the library's own
    # top-level type only -- a show's are the sums over its episodes -- and nowhere
    # else (allLeaves, /library/all, /newest, a detail read carry neither). plexsim
    # sent neither, so the library's storage column was empty although the row
    # already carries both numbers.
    def storage_c11i(path):
        status, body, _ = get(base, path)
        rows_c11i = body.get("Metadata") or []
        return status, [(r.get("durationTotal"), r.get("storageTotal")) for r in rows_c11i]
    movies_c11i = storage_c11i("/library/sections/%s/all?includeStorage=1" % movie_section)
    plain_c11i = storage_c11i("/library/sections/%s/all" % movie_section)
    shows_c11i = storage_c11i("/library/sections/%s/all?includeStorage=1" % show_section)
    episodes_c11i = storage_c11i("/library/sections/%s/all?type=4&includeStorage=1" % show_section)
    leaves_c11i = storage_c11i("/library/sections/%s/allLeaves?includeStorage=1" % movie_section)
    detail_c11i = storage_c11i("/library/metadata/%s?includeStorage=1" % movie)
    row_c11i = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    own_c11i = (int(row_c11i.get("duration") or 0),
                sum(int(p.get("size") or 0) for m in (row_c11i.get("Media") or [])
                    for p in (m.get("Part") or [])))
    leaf_total_c11i = (0, 0)
    show_row_c11i = (get(base, "/library/sections/%s/all" % show_section)[1].get("Metadata") or [{}])[0]
    show_leaves_c11i = (get(base, "/library/metadata/%s/allLeaves" % show_row_c11i.get("ratingKey"))[1]
                        .get("Metadata") or [])
    leaf_total_c11i = (sum(int(e.get("duration") or 0) for e in show_leaves_c11i),
                       sum(int(p.get("size") or 0) for e in show_leaves_c11i
                           for m in (e.get("Media") or []) for p in (m.get("Part") or [])))
    check("includeStorage adds the duration and storage totals a library counts",
          movies_c11i[0] == 200 and own_c11i in movies_c11i[1]
          and all(pair_c11i == (None, None) for pair_c11i in plain_c11i[1])
          and shows_c11i[1][:1] == [leaf_total_c11i]
          and all(pair_c11i == (None, None) for pair_c11i in episodes_c11i[1])
          and all(pair_c11i == (None, None) for pair_c11i in leaves_c11i[1])
          and all(pair_c11i == (None, None) for pair_c11i in detail_c11i[1]),
          "movies %s (own %s), plain %s, shows %s (leaves %s), episodes %s, leaves %s, detail %s"
          % (movies_c11i[1], own_c11i, plain_c11i[1], shows_c11i[1], leaf_total_c11i,
             episodes_c11i[1], leaves_c11i[1], detail_c11i[1]))


    # Measured on kids (1.43.4) and zen (1.42.2), identical on both across every
    # readable row: a SINGLE history read is a nine-attribute row -- no `key`, no
    # `ratingKey`, no artwork -- with `viewType` 1, and an episode adds
    # grandparentTitle, index and parentIndex. plexsim answered its stored listing
    # row, which advertises links this endpoint does not have.
    span_c11j = int((get(base, "/library/metadata/%s" % movie)[1]
                     .get("Metadata") or [{}])[0].get("duration") or 0)
    for state_c11j, at_c11j in (("playing", 30000), ("playing", int(span_c11j * 0.95)),
                                ("stopped", int(span_c11j * 0.96))):
        request(base, "GET", "/:/timeline?ratingKey=%s&key=%s&state=%s&hasMDE=1&time=%s&duration=%s"
                             "&X-Plex-Session-Identifier=c11j&X-Plex-Client-Identifier=c11j"
                             % (movie, quote("/library/metadata/%s" % movie), state_c11j,
                                at_c11j, span_c11j))
    status_c11j, all_c11j, _ = get(base, "/status/sessions/history/all?type=1")
    rows_c11j = all_c11j.get("Metadata") or []
    if not rows_c11j:
        skip("a single history read is the projection a real PMS answers", "no history row")
    else:
        key_c11j = (rows_c11j[-1].get("historyKey") or "").rsplit("/", 1)[-1]
        one_status_c11j, one_c11j, _ = get(base, "/status/sessions/history/%s" % key_c11j)
        row_c11j = (one_c11j.get("Metadata") or [{}])[0]
        allowed_c11j = {"historyKey", "librarySectionID", "title", "grandparentTitle", "type",
                        "index", "parentIndex", "originallyAvailableAt", "viewedAt",
                        "accountID", "deviceID", "viewType"}
        check("a single history read is the projection a real PMS answers",
              one_status_c11j == 200 and row_c11j.get("viewType") == 1
              and set(row_c11j) <= allowed_c11j
              and {"historyKey", "title", "type", "viewedAt", "accountID",
                   "deviceID"} <= set(row_c11j)
              and "key" not in row_c11j and "ratingKey" not in row_c11j
              and "thumb" not in row_c11j
              and "ratingKey" in rows_c11j[-1],
              "single %s, extra %s, listing keeps ratingKey %s"
              % (sorted(row_c11j), sorted(set(row_c11j) - allowed_c11j),
                 "ratingKey" in rows_c11j[-1]))


    # Re-measured on kids (1.43.4) and zen (1.42.2): a real PMS gzips every
    # compressible body whenever the client offers gzip, even when compressing makes
    # it LARGER (83 bytes in, 90 out), and sends it plain only for
    # `Accept-Encoding: identity`. plexsim had an 860-byte floor and a "only if it
    # shrinks" test, both inferred rather than measured, so seventeen endpoints
    # answered plain and without the length pair.
    def encoded_c11k(path, encoding_c11k="gzip"):
        req_c11k = urllib.request.Request(base.rstrip("/") + path, headers={
            "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": encoding_c11k,
            "X-Plex-Client-Identifier": "plexsim-ui-test", "X-Plex-Product": "Plex Web",
            "X-Plex-Platform": "Chrome"})
        with urllib.request.urlopen(req_c11k, timeout=30) as response_c11k:
            raw_c11k = response_c11k.read()
            return (response_c11k.headers.get("Content-Encoding"),
                    response_c11k.headers.get("X-Plex-Content-Original-Length"),
                    response_c11k.headers.get("X-Plex-Content-Compressed-Length"),
                    len(raw_c11k))
    tiny_c11k = encoded_c11k("/livetv/dvrs")
    small_c11k = encoded_c11k("/library/sections/%s/albums" % movie_section)
    plain_c11k = encoded_c11k("/livetv/dvrs", "identity")
    check("every compressible body is gzipped when the client offers it",
          tiny_c11k[0] == "gzip" and tiny_c11k[1] and tiny_c11k[2]
          and small_c11k[0] == "gzip" and small_c11k[1] and small_c11k[2]
          and plain_c11k[0] is None,
          "tiny %s, small %s, identity %s" % (tiny_c11k, small_c11k, plain_c11k))


    # Re-measured on a writable copy of the reference: a plain "Add to Queue" lands
    # immediately after the current item, in front of the queue's original members,
    # and a second one stacks behind the first -- [A,B,C] + D = [A,D,B,C], + E =
    # [A,D,E,B,C]. plexsim skipped forward while the item id was above the selected
    # id, which every original member also satisfies, so it appended to the very end
    # and Add to Queue from a grid put the row at the bottom of the queue.
    def queue_rows_c11l(body_c11l):
        return [(m.get("playQueueItemID"), m.get("ratingKey"))
                for m in (body_c11l.get("Metadata") or [])]
    members_c11l = [r.get("ratingKey") for r in
                    (get(base, "/library/sections/%s/all" % movie_section)[1].get("Metadata") or [])]
    extra_c11l = [r.get("ratingKey") for r in
                  (get(base, "/library/sections/%s/all?type=4" % show_section)[1].get("Metadata") or [])]
    if len(members_c11l) < 2 or not extra_c11l:
        skip("a plain queue add lands in Up Next, after the items already added",
             "the fixture has too few rows")
    else:
        uri_c11l = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                         % (machine, ",".join(members_c11l)))
        status_c11l, queue_c11l, _ = container(
            base, "POST", "/playQueues?type=video&uri=%s&key=%s&shuffle=0&repeat=0&continuous=0"
            % (uri_c11l, quote("/library/metadata/%s" % members_c11l[0])))
        id_c11l = queue_c11l.get("playQueueID")
        created_c11l = queue_rows_c11l(queue_c11l)
        first_c11l = queue_rows_c11l(container(
            base, "PUT", "/playQueues/%s?uri=%s"
            % (id_c11l, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                              % (machine, extra_c11l[0]))))[1])
        second_c11l = queue_rows_c11l(container(
            base, "PUT", "/playQueues/%s?uri=%s"
            % (id_c11l, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                              % (machine, extra_c11l[-1]))))[1])
        ahead_c11l = queue_rows_c11l(container(
            base, "PUT", "/playQueues/%s?uri=%s&next=1"
            % (id_c11l, quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                              % (machine, members_c11l[-1]))))[1])
        check("a plain queue add lands in Up Next, after the items already added",
              status_c11l == 200 and len(created_c11l) == len(members_c11l)
              and [key for _, key in first_c11l]
              == [members_c11l[0], extra_c11l[0]] + members_c11l[1:]
              and [key for _, key in second_c11l]
              == [members_c11l[0], extra_c11l[0], extra_c11l[-1]] + members_c11l[1:]
              and [key for _, key in ahead_c11l]
              == [members_c11l[0], members_c11l[-1], extra_c11l[0], extra_c11l[-1]] + members_c11l[1:],
              "created %s, +1 %s, +2 %s, next=1 %s"
              % ([k for _, k in created_c11l], [k for _, k in first_c11l],
                 [k for _, k in second_c11l], [k for _, k in ahead_c11l]))


    # Measured on a writable copy of the reference with the client's own request
    # (bundle module 90487): `POST /library/metadata/{id}/subtitles?title=&format=`
    # with the file as the body answers 200 and an EMPTY body, and leaves a new
    # subtitle Stream that becomes the selected one. No title, and a title with no
    # extension, are 500s; a format it does not take is a 400; an empty body is a 406.
    # plexsim fell through to the subtitle SEARCH branch and answered 400 to all of
    # them, so the modal always said "Unable to upload subtitle file".
    def upload_c11m(query_c11m, body_c11m=b"1\n00:00:00,000 --> 00:00:02,000\nc11\n\n"):
        return request(base, "POST", "/library/metadata/%s/subtitles%s" % (movie, query_c11m),
                       body=body_c11m)[:2]
    ok_c11m = upload_c11m("?title=c11sub.srt&format=srt")
    inferred_c11m = upload_c11m("?title=c11sub2.srt")
    no_title_c11m = upload_c11m("?format=srt")
    no_extension_c11m = upload_c11m("?title=c11sub3&format=srt")
    bad_format_c11m = upload_c11m("?title=c11sub4.bin&format=bin")
    empty_c11m = upload_c11m("?title=c11sub5.srt&format=srt", b"")
    missing_c11m = request(base, "POST", "/library/metadata/99999999/subtitles?title=x.srt&format=srt",
                           body=b"x")[0]
    row_c11m = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    subs_c11m = [s_c11m for m_c11m in (row_c11m.get("Media") or [])
                 for p_c11m in (m_c11m.get("Part") or [])
                 for s_c11m in (p_c11m.get("Stream") or []) if s_c11m.get("streamType") == 3]
    mine_c11m = [s_c11m for s_c11m in subs_c11m if s_c11m.get("title") in ("c11sub", "c11sub2")]
    check("the Upload Subtitle modal's request is accepted, and its refusals match",
          ok_c11m == (200, b"") and inferred_c11m == (200, b"")
          and no_title_c11m[0] == 500 and no_extension_c11m[0] == 500
          and bad_format_c11m[0] == 400 and empty_c11m[0] == 406 and missing_c11m == 404
          and len(mine_c11m) == 2
          and all(s_c11m.get("codec") == "srt" and s_c11m.get("format") == "srt"
                  and str(s_c11m.get("key") or "").startswith("/library/streams/")
                  and s_c11m.get("transient") == "0"
                  and s_c11m.get("displayTitle") == "Unknown (SRT External)"
                  for s_c11m in mine_c11m)
          and [s_c11m.get("title") for s_c11m in subs_c11m if s_c11m.get("selected")] == ["c11sub2"],
          "ok %s, inferred %s, no title %s, no extension %s, bad format %s, empty %s, "
          "missing %s, streams %s"
          % (ok_c11m, inferred_c11m, no_title_c11m[0], no_extension_c11m[0], bad_format_c11m[0],
             empty_c11m[0], missing_c11m,
             [(s_c11m.get("title"), s_c11m.get("displayTitle"), s_c11m.get("selected"))
              for s_c11m in subs_c11m]))


    # Measured on a writable copy of the reference: a PUT to a stream the server HAS is
    # 200 with an empty body (and stores nothing -- the title is unchanged after it),
    # a DELETE of an embedded track is 403 because it is part of the file, and a DELETE
    # of an uploaded sidecar really removes it (200). An unknown id keeps cycle 6's
    # refusals: PUT 400, DELETE 404, GET 404. plexsim answered 501 to every verb.
    row_c11n = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    streams_c11n = [s_c11n for m_c11n in (row_c11n.get("Media") or [])
                    for p_c11n in (m_c11n.get("Part") or []) for s_c11n in (p_c11n.get("Stream") or [])]
    embedded_c11n = next((s_c11n.get("id") for s_c11n in streams_c11n
                          if not str(s_c11n.get("key") or "").startswith("/library/streams/")), None)
    request(base, "POST", "/library/metadata/%s/subtitles?title=c11del.srt&format=srt" % movie,
            body=b"1\n00:00:00,000 --> 00:00:02,000\nc11\n\n")
    row_c11n = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    uploaded_c11n = next((s_c11n.get("id") for m_c11n in (row_c11n.get("Media") or [])
                          for p_c11n in (m_c11n.get("Part") or [])
                          for s_c11n in (p_c11n.get("Stream") or [])
                          if s_c11n.get("title") == "c11del"), None)
    if embedded_c11n is None or uploaded_c11n is None:
        skip("a stream PUT is accepted and only an uploaded sidecar can be deleted",
             "no stream to write to")
    else:
        put_c11n = request(base, "PUT", "/library/streams/%s?title=c11new" % embedded_c11n)[:2]
        delete_embedded_c11n = request(base, "DELETE", "/library/streams/%s" % embedded_c11n)[0]
        delete_upload_c11n = request(base, "DELETE", "/library/streams/%s" % uploaded_c11n)[:2]
        gone_c11n = [s_c11n.get("title")
                     for m_c11n in ((get(base, "/library/metadata/%s" % movie)[1]
                                     .get("Metadata") or [{}])[0].get("Media") or [])
                     for p_c11n in (m_c11n.get("Part") or [])
                     for s_c11n in (p_c11n.get("Stream") or [])]
        check("a stream PUT is accepted and only an uploaded sidecar can be deleted",
              put_c11n == (200, b"") and delete_embedded_c11n == 403
              and delete_upload_c11n == (200, b"") and "c11del" not in gone_c11n
              and request(base, "GET", "/library/streams/%s" % embedded_c11n)[0] == 501
              and request(base, "PUT", "/library/streams/99999999")[0] == 400
              and request(base, "DELETE", "/library/streams/99999999")[0] == 404,
              "put %s, delete embedded %s, delete upload %s, left %s"
              % (put_c11n, delete_embedded_c11n, delete_upload_c11n, gone_c11n))


    # Measured on a writable copy of the reference: a playlist's items and a
    # collection's children are the LIGHT grid row -- no Guid, no Rating, no Field, no
    # Stream under the Part, tags text-only -- while the same item's detail read is
    # full. plexsim sent the detail row on both, the two list endpoints the grid
    # projection was never applied to.
    def weight_c11o(row_c11o):
        parts_c11o = [p_c11o for m_c11o in (row_c11o.get("Media") or [])
                      for p_c11o in (m_c11o.get("Part") or [])]
        return (len(row_c11o.get("Guid") or []), len(row_c11o.get("Rating") or []),
                len(row_c11o.get("Field") or []),
                sum(len(p_c11o.get("Stream") or []) for p_c11o in parts_c11o),
                any("id" in t_c11o for t_c11o in (row_c11o.get("Genre") or [])))
    members_c11o = [r_c11o.get("ratingKey") for r_c11o in
                    (get(base, "/library/sections/%s/all" % movie_section)[1].get("Metadata") or [])]
    uri_c11o = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                     % (machine, ",".join(members_c11o)))
    _, list_c11o, _ = container(base, "POST", "/playlists?type=video&title=c11light&smart=0&uri=%s"
                                % uri_c11o)
    playlist_c11o = (list_c11o.get("Metadata") or [{}])[0].get("ratingKey")
    _, coll_c11o, _ = container(base, "POST",
                                "/library/collections?type=1&title=c11lightc&smart=0&sectionId=%s&uri=%s"
                                % (movie_section, uri_c11o))
    collection_c11o = (coll_c11o.get("Metadata") or [{}])[0].get("ratingKey")
    items_c11o = (get(base, "/playlists/%s/items" % playlist_c11o)[1].get("Metadata") or [{}])[0]
    children_c11o = (get(base, "/library/collections/%s/children" % collection_c11o)[1]
                     .get("Metadata") or [{}])[0]
    detail_c11o = (get(base, "/library/metadata/%s" % members_c11o[0])[1].get("Metadata") or [{}])[0]
    check("a playlist's items and a collection's children are the light row",
          weight_c11o(items_c11o) == (0, 0, 0, 0, False)
          and weight_c11o(children_c11o) == (0, 0, 0, 0, False)
          and weight_c11o(detail_c11o) != (0, 0, 0, 0, False)
          and items_c11o.get("playlistItemID")
          and items_c11o.get("librarySectionID") is not None
          and children_c11o.get("librarySectionID") is not None,
          "items %s, children %s, detail %s"
          % (weight_c11o(items_c11o), weight_c11o(children_c11o), weight_c11o(detail_c11o)))


    # Measured on kids (1.43.4) and zen (1.42.2) against a real library: a LIGHT row
    # carries at most 2 Genre, 2 Writer, 2 Country and 3 Role -- the first of the detail
    # row's own order -- where the detail row carries them all (155 roles on one zen
    # movie, 3 on every listing of it). plexsim sent the whole list on every grid, hub
    # and search row. Invisible on a five-clip library, which is why eleven cycles
    # missed it.
    def tags_c12a(path_c12a, key_c12a=None):
        status_c12a, body_c12a, _ = get(base, path_c12a)
        rows_c12a = body_c12a.get("Metadata") or []
        row_c12a = next((r for r in rows_c12a if str(r.get("ratingKey")) == str(key_c12a or movie)),
                        rows_c12a[0] if rows_c12a else {})
        return status_c12a, {el_c12a: len(row_c12a.get(el_c12a) or [])
                             for el_c12a in ("Genre", "Writer", "Country", "Role")}
    caps_c12a = {"Genre": 2, "Writer": 2, "Country": 2, "Role": 3}
    # The condition is BUILT rather than hoped for: by this point the suite's own edits
    # have emptied the fixture's tag lists, and an indexed tag write replaces the list on
    # the item path, so this leaves exactly three genres to truncate.
    request(base, "PUT", "/library/metadata/%s?genre[0].tag.tag=c12capa"
                         "&genre[1].tag.tag=c12capb&genre[2].tag.tag=c12capc" % movie)
    detail_c12a = tags_c12a("/library/metadata/%s" % movie)
    grid_c12a = tags_c12a("/library/sections/%s/all" % movie_section)
    check("a light row caps its tag lists where the detail row carries them all",
          detail_c12a[0] == 200 and grid_c12a[0] == 200
          and detail_c12a[1]["Genre"] == 3 and grid_c12a[1]["Genre"] == 2
          and all(grid_c12a[1][el_c12a] == min(detail_c12a[1][el_c12a], caps_c12a[el_c12a])
                  for el_c12a in caps_c12a),
          "detail %s, grid %s" % (detail_c12a[1], grid_c12a[1]))


    # Measured on kids (1.43.4) and zen (1.42.2): an episode's `parentGuid` is the
    # SEASON's guid, not the show's. plexsim copied the season's own `parentGuid` (the
    # show), so every episode of a show shared one parent guid and a client keying
    # per-season state off it could not tell the seasons apart.
    episodes_c12b = (get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                     .get("Metadata") or [])
    if not episodes_c12b:
        skip("an episode's parentGuid is the season's own guid", "no episode in the fixture")
    else:
        # Picked from an episode whose season is still MATCHED: the suite unmatches one
        # later on, and an unmatched season's guid is a `local://` one whose effect on a
        # child's parentGuid has never been measured.
        row_c12b, season_c12b = {}, {}
        for candidate_c12b in episodes_c12b:
            row_c12b = (get(base, "/library/metadata/%s" % candidate_c12b["ratingKey"])[1]
                        .get("Metadata") or [{}])[0]
            season_c12b = (get(base, "/library/metadata/%s" % row_c12b.get("parentRatingKey"))[1]
                           .get("Metadata") or [{}])[0]
            if str(season_c12b.get("guid") or "").startswith("plex://season/"):
                break
        check("an episode's parentGuid is the season's own guid",
              row_c12b.get("parentGuid")
              and row_c12b.get("parentGuid") == season_c12b.get("guid")
              and row_c12b.get("parentGuid") != row_c12b.get("grandparentGuid")
              and str(row_c12b.get("parentGuid")).startswith("plex://season/"),
              "episode parent %s, season guid %s, grandparent %s"
              % (row_c12b.get("parentGuid"), season_c12b.get("guid"),
                 row_c12b.get("grandparentGuid")))


    # Measured on kids (1.43.4) and zen (1.42.2): the section grid answers
    # `allowSync: true`. plexsim answered false on the grid while already answering
    # true on /newest, /recentlyAdded, /onDeck, /folder, /children and a detail row --
    # the most-requested container on the surface, and an attribute python-plexapi
    # reads off every section listing.
    sync_c12c = {}
    for path_c12c in ("/library/sections/%s/all" % movie_section,
                      "/library/sections/%s/all?type=1" % movie_section,
                      "/library/sections/%s/newest" % movie_section,
                      "/library/sections/%s/collections" % movie_section):
        sync_c12c[path_c12c] = get(base, path_c12c)[1].get("allowSync")
    bare_c12c = get(base, "/library/sections")[1].get("allowSync")
    check("the section grid says allowSync, and the section list still does not",
          all(value_c12c is True for value_c12c in sync_c12c.values())
          and bare_c12c is False,
          "%s, /library/sections %s" % (sync_c12c, bare_c12c))


    # Measured on kids (1.43.4) and zen (1.42.2): with no `limit`, /hubs/search caps
    # each hub at 3 rows and /library/search answers 10 results in all. plexsim
    # defaulted both to 30, so the type-ahead path and every non-web client got a page
    # many times the size a real PMS sends. The query has to overflow a hub or the
    # fixture cannot tell 3 from 30 -- 'ha' matches four of its actors by word prefix.
    def hubs_c12d(query_c12d, suffix_c12d=""):
        status_c12d, body_c12d, _ = get(base, "/hubs/search?query=%s%s" % (query_c12d, suffix_c12d))
        return status_c12d, {hub_c12d.get("type"): len(hub_c12d.get("Metadata")
                                                       or hub_c12d.get("Directory") or [])
                             for hub_c12d in (body_c12d.get("Hub") or [])
                             if (hub_c12d.get("Metadata") or hub_c12d.get("Directory"))}
    # Four genres sharing a word prefix, written onto one row: the fixture has nothing
    # that overflows a search hub by itself, and without an overflow the check cannot
    # tell a cap of 3 from a cap of 30.
    request(base, "PUT", "/library/metadata/%s?genre[0].tag.tag=c12hita"
                         "&genre[1].tag.tag=c12hitb&genre[2].tag.tag=c12hitc"
                         "&genre[3].tag.tag=c12hitd" % movie)
    query_c12d = "c12hit"
    wide_c12d = hubs_c12d(query_c12d, "&limit=50")
    plain_c12d = hubs_c12d(query_c12d)
    lib_wide_c12d = len(get(base, "/library/search?query=%s&limit=50" % query_c12d)[1]
                        .get("SearchResult") or [])
    lib_plain_c12d = len(get(base, "/library/search?query=%s" % query_c12d)[1]
                         .get("SearchResult") or [])
    check("a search caps at the rows a real PMS sends when no limit is asked for",
          plain_c12d[0] == 200 and wide_c12d[0] == 200
          and wide_c12d[1].get("genre") == 4 and plain_c12d[1].get("genre") == 3
          and all(plain_c12d[1].get(hub_c12d) == min(count_c12d, 3)
                  for hub_c12d, count_c12d in wide_c12d[1].items())
          and lib_plain_c12d == min(lib_wide_c12d, 10),
          "query %r -- hubs: no limit %s, limit=50 %s | /library/search %s vs %s"
          % (query_c12d, plain_c12d[1], wide_c12d[1], lib_plain_c12d, lib_wide_c12d))


    # Measured on kids (1.43.4) and zen (1.42.2): an actor tag row in search is
    # `tagType: 6` and a director row 4. plexsim sent 4 for both, while
    # /library/people/{id} already answered 6 for the same person -- the server
    # contradicted itself.
    def tag_rows_c12e(path_c12e):
        status_c12e, body_c12e, _ = get(base, path_c12e)
        out_c12e = {}
        for hub_c12e in (body_c12e.get("Hub") or []):
            rows_c12e = hub_c12e.get("Directory") or hub_c12e.get("Metadata") or []
            if rows_c12e and hub_c12e.get("type") in ("actor", "director", "genre"):
                out_c12e[hub_c12e["type"]] = (rows_c12e[0].get("tagType"), rows_c12e[0].get("id"))
        return status_c12e, out_c12e
    # 'ha' matches four of the fixture's actors by word prefix; a single letter finds
    # nothing at all, which is the search matcher's own measured rule.
    status_c12e, hubs_c12e = tag_rows_c12e("/hubs/search?query=ha&limit=6")
    person_c12e = None
    if hubs_c12e.get("actor"):
        person_c12e = (get(base, "/library/people/%s" % hubs_c12e["actor"][1])[1]
                       .get("Directory") or [{}])[0].get("tagType")
    check("an actor search row is tagType 6, and agrees with the person row",
          status_c12e == 200 and hubs_c12e.get("actor", (None,))[0] == 6
          and hubs_c12e.get("director", (4,))[0] == 4
          and hubs_c12e.get("genre", (1,))[0] == 1
          and person_c12e == 6,
          "hubs %s, /library/people tagType %s" % (hubs_c12e, person_c12e))


    # Measured on kids (1.43.4) and zen (1.42.2): `group=type`, `group=guid` and
    # `group=id` all answer 200 where plexsim answered 500. Exercised against all 27
    # candidate fields -- the other 24 agree, including the 11 that are 500 everywhere.
    # `id` became a filter field in cycle 11 and was still not a group field.
    groups_c12f = {field_c12f: request(base, "GET", "/library/sections/%s/all?group=%s"
                                       % (movie_section, field_c12f))[0]
                   for field_c12f in ("type", "guid", "id", "year", "decade", "titleSort",
                                      "genre", "duration", "userRating")}
    check("a listing groups by type, guid and id, and still refuses the rest",
          all(groups_c12f[good_c12f] == 200
              for good_c12f in ("type", "guid", "id", "year", "decade", "titleSort"))
          and all(groups_c12f[bad_c12f] == 500
                  for bad_c12f in ("genre", "duration", "userRating")),
          "%s" % groups_c12f)


    # Measured across all 26 secondary axes on kids (1.43.4) and zen (1.42.2): every
    # axis carries `content: "secondary"` except `actor`, which carries `viewGroup`
    # without it. plexsim sent it there too. No rule explains the exception and both
    # servers agree on it, so it is emulated as measured.
    axes_c12h = {axis_c12h: get(base, "/library/sections/%s/%s" % (movie_section, axis_c12h))[1]
                 for axis_c12h in ("actor", "genre", "director", "writer", "year", "collection")}
    check("the actor axis carries no content, where every other axis does",
          axes_c12h["actor"].get("content") is None
          and axes_c12h["actor"].get("viewGroup") == "secondary"
          and all(axes_c12h[axis_c12h].get("content") == "secondary"
                  for axis_c12h in ("genre", "director", "writer", "year", "collection")),
          "%s" % {a_c12h: b_c12h.get("content") for a_c12h, b_c12h in axes_c12h.items()})


    # Re-measured on kids (1.43.4) and zen (1.42.2), correcting a lab-only reading: an
    # EPISODE's title IS compared by /common (several differently titled episodes list
    # `title` in mixedFields, a single one sends its title) where a SEASON's is not, and
    # `titleSort` is "" only when it is genuinely mixed -- a single row answers its own
    # sort title. plexsim dropped `title` for both and hardcoded `titleSort` to "", so
    # the Edit dialog opened with the Title and Sort Title boxes blank on an episode.
    def common_c12g(query_c12g):
        status_c12g, body_c12g, _ = get(base, "/library/sections/%s/common?%s"
                                        % (show_section, query_c12g))
        return status_c12g, (body_c12g.get("Metadata") or [{}])[0]
    episodes_c12g = [r_c12g.get("ratingKey") for r_c12g in
                     (get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                      .get("Metadata") or [])]
    seasons_c12g = [r_c12g.get("ratingKey") for r_c12g in
                    (get(base, "/library/sections/%s/all?type=3" % show_section)[1]
                     .get("Metadata") or [])]
    if len(episodes_c12g) < 2 or not seasons_c12g:
        skip("an episode's title is compared by /common and a season's is not",
             "the fixture has too few rows")
    else:
        many_c12g = common_c12g("type=4")
        one_c12g = common_c12g("type=4&id=%s" % episodes_c12g[0])
        season_c12g = common_c12g("type=3&id=%s" % seasons_c12g[0])
        episode_row_c12g = (get(base, "/library/metadata/%s" % episodes_c12g[0])[1]
                            .get("Metadata") or [{}])[0]
        season_row_c12g = (get(base, "/library/metadata/%s" % seasons_c12g[0])[1]
                           .get("Metadata") or [{}])[0]
        check("an episode's title is compared by /common and a season's is not",
              "title" in (many_c12g[1].get("mixedFields") or "").split(",")
              and many_c12g[1].get("titleSort") == ""
              and one_c12g[1].get("title") == episode_row_c12g.get("title")
              and one_c12g[1].get("titleSort")
              and "title" not in (season_c12g[1].get("mixedFields") or "").split(",")
              and "title" not in season_c12g[1]
              and season_c12g[1].get("titleSort") == (season_row_c12g.get("titleSort")
                                                      or season_row_c12g.get("title")),
              "many %r, one title=%r sort=%r, season sort=%r (row %r)"
              % (many_c12g[1].get("mixedFields"), one_c12g[1].get("title"),
                 one_c12g[1].get("titleSort"), season_c12g[1].get("titleSort"),
                 season_row_c12g.get("titleSort")))


    # Measured on the copy: a subtitle upload pushes NO notification frames -- the
    # client refetches the row itself. Cycle 11 added an edit pair here without
    # measuring it, so every other client was told the item had changed when nothing
    # about it had.
    _, upload_frames_c12i = notifications(
        base,
        lambda: request(base, "POST", "/library/metadata/%s/subtitles?title=c12frames.srt&format=srt"
                        % movie, body=b"1\n00:00:00,000 --> 00:00:02,000\nc12\n\n"),
        seconds=6)
    check("a subtitle upload pushes no frames",
          not [f_c12i for f_c12i in upload_frames_c12i
               if (f_c12i.get("NotificationContainer", f_c12i) or {}).get("type")
               in ("timeline", "activity")],
          "%d frame(s): %s"
          % (len(upload_frames_c12i),
             [(f_c12i.get("NotificationContainer", f_c12i) or {}).get("type")
              for f_c12i in upload_frames_c12i]))


    # Re-measured on the copy, correcting cycle 4's reading: a section edit needs an
    # `agent`, not a `name` and not a `scanner`. The client's library-upgrade handler
    # sends `agent` and `scanner` and nothing else; `agent` alone is a 200 too, while
    # `name` alone, `scanner` alone, `language` alone and an empty form are 400.
    # plexsim demanded `name`, so the upgrade form was refused every time.
    forms_c12j = {
        "agent+scanner": "?agent=tv.plex.agents.movie&scanner=Plex%20Movie",
        "agent only": "?agent=tv.plex.agents.movie",
        "scanner only": "?scanner=Plex%20Movie",
        "language only": "?language=en-US",
        "name only": "?name=c12name",
        "empty": "",
        "bad agent": "?agent=com.bogus.agent",
    }
    edits_c12j = {label_c12j: request(base, "PUT", "/library/sections/%s%s"
                                      % (movie_section, form_c12j))[0]
                  for label_c12j, form_c12j in forms_c12j.items()}
    renamed_c12j = request(base, "PUT", "/library/sections/%s?name=c12renamed"
                                        "&agent=tv.plex.agents.movie&scanner=Plex%%20Movie"
                                        % movie_section)[0]
    title_c12j = next((d_c12j.get("title") for d_c12j in
                       (get(base, "/library/sections")[1].get("Directory") or [])
                       if str(d_c12j.get("key")) == str(movie_section)), None)
    check("a library edit needs an agent, not a name",
          edits_c12j["agent+scanner"] == 200 and edits_c12j["agent only"] == 200
          and edits_c12j["scanner only"] == 400 and edits_c12j["language only"] == 400
          and edits_c12j["name only"] == 400 and edits_c12j["empty"] == 400
          and edits_c12j["bad agent"] == 400
          and renamed_c12j == 200 and title_c12j == "c12renamed",
          "%s, rename %s -> %r" % (edits_c12j, renamed_c12j, title_c12j))


    # Re-measured on the copy: `psb` is accepted alongside srt/ass/ssa/smi/txt, and the
    # format check is CASE-SENSITIVE -- `SRT` is a 400 where `srt` is a 200. plexsim
    # lowercased the value and so accepted uploads a real PMS refuses, and refused psb.
    def upload_c12k(query_c12k):
        return request(base, "POST", "/library/metadata/%s/subtitles%s" % (movie, query_c12k),
                       body=b"1\n00:00:00,000 --> 00:00:02,000\nc12\n\n")[0]
    formats_c12k = {
        "psb": upload_c12k("?title=c12a.psb&format=psb"),
        "srt": upload_c12k("?title=c12b.srt&format=srt"),
        "SRT": upload_c12k("?title=c12c.SRT&format=SRT"),
        "format upper": upload_c12k("?title=c12d.srt&format=SRT"),
        "vtt": upload_c12k("?title=c12e.vtt&format=vtt"),
        "aqt": upload_c12k("?title=c12f.aqt&format=aqt"),
        "txt named srt": upload_c12k("?title=c12g.txt&format=srt"),
    }
    check("the subtitle upload takes psb and is case-sensitive about the format",
          formats_c12k["psb"] == 200 and formats_c12k["srt"] == 200
          and formats_c12k["txt named srt"] == 200
          and formats_c12k["SRT"] == 400 and formats_c12k["format upper"] == 400
          and formats_c12k["vtt"] == 400 and formats_c12k["aqt"] == 400,
          "%s" % formats_c12k)


    # Measured on the copy: `DELETE /library/streams/abc` is a 400 (a malformed id)
    # where `DELETE /library/streams/999999` is a 404 (a missing one), and a POST to a
    # stream the server HAS is a 404 rather than the 501 its GET answers. plexsim made
    # a malformed id indistinguishable from a missing one.
    row_c12l = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    stream_c12l = next((s_c12l.get("id") for m_c12l in (row_c12l.get("Media") or [])
                        for p_c12l in (m_c12l.get("Part") or [])
                        for s_c12l in (p_c12l.get("Stream") or [])), None)
    verbs_c12l = {
        "DELETE abc": request(base, "DELETE", "/library/streams/abc")[0],
        "DELETE unknown": request(base, "DELETE", "/library/streams/999999")[0],
        "PUT abc": request(base, "PUT", "/library/streams/abc")[0],
        "POST known": request(base, "POST", "/library/streams/%s" % stream_c12l)[0],
        "GET known": request(base, "GET", "/library/streams/%s" % stream_c12l)[0],
    }
    check("a stream path tells a malformed id from a missing one, and 404s a POST",
          verbs_c12l == {"DELETE abc": 400, "DELETE unknown": 404, "PUT abc": 400,
                         "POST known": 404, "GET known": 501},
          "%s" % verbs_c12l)


    # Measured on the copy: a `server://` uri naming ANOTHER machine adds nothing, and
    # an add that adds nothing leaves `playQueueVersion` exactly where it was. plexsim
    # read the ratingKey out of any uri and added the item, and stepped the version on
    # every no-op -- and the client DISCARDS a queue response whose version it does not
    # expect, so a no-op add invalidated the queue the player was holding.
    members_c12m = [r_c12m.get("ratingKey") for r_c12m in
                    (get(base, "/library/sections/%s/all" % movie_section)[1].get("Metadata") or [])]
    mine_c12m = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                      % (machine, members_c12m[0]))
    status_c12m, queue_c12m, _ = container(
        base, "POST", "/playQueues?type=video&uri=%s&key=%s&shuffle=0&repeat=0&continuous=0"
        % (mine_c12m, quote("/library/metadata/%s" % members_c12m[0])))
    id_c12m = queue_c12m.get("playQueueID")
    start_c12m = (len(queue_c12m.get("Metadata") or []), queue_c12m.get("playQueueVersion"))
    def add_c12m(uri_c12m):
        _, body_c12m, _ = container(base, "PUT", "/playQueues/%s?uri=%s" % (id_c12m, uri_c12m))
        return len(body_c12m.get("Metadata") or []), body_c12m.get("playQueueVersion")
    foreign_c12m = add_c12m(quote(
        "server://deadbeefdeadbeefdeadbeefdeadbeefdeadbeef/com.plexapp.plugins.library"
        "/library/metadata/%s" % members_c12m[-1]))
    unknown_c12m = add_c12m(quote("server://%s/com.plexapp.plugins.library"
                                  "/library/metadata/99999999" % machine))
    real_c12m = add_c12m(quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                               % (machine, members_c12m[-1])))
    check("another server's uri adds nothing, and a no-op add leaves the version alone",
          status_c12m == 200 and foreign_c12m == start_c12m and unknown_c12m == start_c12m
          and real_c12m[0] == start_c12m[0] + 1 and real_c12m[1] != start_c12m[1],
          "start %s, foreign %s, unknown %s, real %s"
          % (start_c12m, foreign_c12m, unknown_c12m, real_c12m))


    # Measured on the copy, frame for frame: `PUT /library/optimize` runs a
    # `general.db.optimize` activity -- started 0 with an empty subtitle, updated at
    # 0/10/40/60 with `Optimizing Database`, ended 100. plexsim answered 200 and pushed
    # nothing, so the Settings progress bar never moved.
    _, optimize_c12n = notifications(base, lambda: request(base, "PUT", "/library/optimize"),
                                     seconds=6)
    steps_c12n = [(a_c12n.get("event"), (a_c12n.get("Activity") or {}).get("type"),
                   (a_c12n.get("Activity") or {}).get("progress"))
                  for f_c12n in optimize_c12n
                  for a_c12n in ((f_c12n.get("NotificationContainer", f_c12n) or {})
                                 .get("ActivityNotification") or [])]
    check("Optimize Database runs its activity",
          [step_c12n for step_c12n in steps_c12n if step_c12n[1] == "general.db.optimize"]
          == [("started", "general.db.optimize", 0), ("updated", "general.db.optimize", 0),
              ("updated", "general.db.optimize", 10), ("updated", "general.db.optimize", 40),
              ("updated", "general.db.optimize", 60), ("ended", "general.db.optimize", 100)],
          "%s" % steps_c12n)


    # Measured on the copy: a refresh's frames are timeline(3 queued), activity started
    # -1, activity updated 49, timeline(3 processing), timeline(4 loading), activity
    # ended 100, timeline(5) -- the order this file's own docstring already recorded.
    # plexsim emitted the `updated` inside finish_activity, after `loading`. And the
    # activity's subtitle is the row's `title (year)`, not the bare title.
    _, refresh_frames_c12o = notifications(
        base, lambda: request(base, "PUT", "/library/metadata/%s/refresh" % movie), seconds=8)
    order_c12o, subtitle_c12o = [], None
    for frame_c12o in refresh_frames_c12o:
        body_c12o = frame_c12o.get("NotificationContainer", frame_c12o) or {}
        for a_c12o in body_c12o.get("ActivityNotification") or []:
            order_c12o.append("activity:%s:%s" % (a_c12o.get("event"),
                                                  (a_c12o.get("Activity") or {}).get("progress")))
            subtitle_c12o = subtitle_c12o or (a_c12o.get("Activity") or {}).get("subtitle")
        for t_c12o in body_c12o.get("TimelineEntry") or []:
            order_c12o.append("timeline:%s:%s" % (t_c12o.get("state"), t_c12o.get("metadataState")))
    row_c12o = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    want_c12o = "%s (%s)" % (row_c12o.get("title"), row_c12o.get("year")) \
        if row_c12o.get("year") else row_c12o.get("title")
    check("a refresh pushes its progress frame before the processing timeline",
          order_c12o == ["timeline:3:queued", "activity:started:-1", "activity:updated:49",
                         "timeline:3:processing", "timeline:4:loading",
                         "activity:ended:100", "timeline:5:None"]
          and subtitle_c12o == want_c12o,
          "%s | subtitle %r wanted %r" % (order_c12o, subtitle_c12o, want_c12o))


    # Measured on the copy: a scan ends inside a NESTED pair of
    # `provider.subscriptions.process` activities -- outer started, section ended,
    # completion status, inner started, inner ended, outer ended -- and "Refresh All
    # Metadata" (`?force=1`) runs that whole sequence before it refreshes anything.
    # plexsim pushed neither, so the Libraries page showed no scan for the one
    # operation that takes longest.
    def scan_frames_c12p(path_c12p):
        _, frames_c12p = notifications(base, lambda: request(base, "GET", path_c12p), seconds=8)
        out_c12p = []
        for frame_c12p in frames_c12p:
            body_c12p = frame_c12p.get("NotificationContainer", frame_c12p) or {}
            for a_c12p in body_c12p.get("ActivityNotification") or []:
                out_c12p.append("%s:%s" % ((a_c12p.get("Activity") or {}).get("type"),
                                           a_c12p.get("event")))
            for s_c12p in body_c12p.get("StatusNotification") or []:
                out_c12p.append("status:%s" % (s_c12p.get("title") or "")[:14])
        return out_c12p
    plain_c12p = scan_frames_c12p("/library/sections/%s/refresh" % movie_section)
    tail_c12p = ["provider.subscriptions.process:started",
                 "library.update.section:ended",
                 "status:Library scan c",
                 "provider.subscriptions.process:started",
                 "provider.subscriptions.process:ended",
                 "provider.subscriptions.process:ended"]
    forced_c12p = scan_frames_c12p("/library/sections/%s/refresh?force=1" % movie_section)
    check("a scan ends inside the subscriptions pair, and a forced refresh scans first",
          plain_c12p[-6:] == tail_c12p
          and plain_c12p[0] == "library.update.section:started"
          and forced_c12p[0] == "library.update.section:started"
          and forced_c12p[1].startswith("status:Scanning")
          and "provider.subscriptions.process:ended" in forced_c12p,
          "plain %s | forced %s" % (plain_c12p[-7:], forced_c12p[:4]))

        # Re-measured on zen (1.42.2) against its 3887-row library, on the one movie with
    # 17 directors and on a show and an episode with four: a light row's `Director`
    # list caps at 3 -- like `Role`, not like the two-entry Genre/Writer/Country caps
    # -- and `Producer` is absent from a light row however many the detail row carries
    # (22 on one zen movie, none on any listing of it). Cycle 12 recorded the cap as
    # "only known to be above 2" because the richest Director row in its 240-movie
    # sample had two. kids cannot show this: no lab item has more than one director.
    def people_c13a(path_c13a):
        status_c13a, body_c13a, _ = get(base, path_c13a)
        rows_c13a = body_c13a.get("Metadata") or []
        row_c13a = next((r for r in rows_c13a if str(r.get("ratingKey")) == str(movie)),
                        rows_c13a[0] if rows_c13a else {})
        return status_c13a, {el_c13a: len(row_c13a.get(el_c13a) or [])
                             for el_c13a in ("Director", "Producer")}
    # Built rather than hoped for: by this point the suite's own edits have rewritten
    # the fixture's tag lists, and an indexed tag write REPLACES the list on the item
    # path, so this leaves exactly four directors and two producers to truncate.
    request(base, "PUT", "/library/metadata/%s?director[0].tag.tag=c13capa"
                         "&director[1].tag.tag=c13capb&director[2].tag.tag=c13capc"
                         "&director[3].tag.tag=c13capd"
                         "&producer[0].tag.tag=c13cappa&producer[1].tag.tag=c13cappb" % movie)
    detail_c13a = people_c13a("/library/metadata/%s" % movie)
    grid_c13a = people_c13a("/library/sections/%s/all" % movie_section)
    check("a light row caps Director at three and carries no Producer at all",
          detail_c13a[0] == 200 and grid_c13a[0] == 200
          and detail_c13a[1] == {"Director": 4, "Producer": 2}
          and grid_c13a[1] == {"Director": 3, "Producer": 0},
          "detail %s, grid %s" % (detail_c13a[1], grid_c13a[1]))

        # Measured on a writable copy of the reference (1.43.4) by snapshotting the show,
    # the season and all three episodes field by field either side of ONE call:
    # `PUT /library/metadata/{season}/unmatch` does not touch the season alone. Every
    # episode under it is unmatched too -- `local://<id>`, the generated `Episode N`,
    # `summary` emptied, `year`, `audienceRating` and the people tags dropped,
    # `titleSort` removed, `parentThumb` nulled -- and each episode's `parentGuid`
    # follows the season to `local://<season id>`. The season itself loses its `year`,
    # so the show's shows through as `parentYear`, and its `thumb` falls back to the
    # SHOW's. kids corroborates the season half from the other side: its season 11 is
    # unmatched and carries the show's thumb, no year and `parentYear: 2002`, where
    # its matched season 7 carries its own thumb and year. plexsim edited the one row,
    # which left matched episodes under an unmatched parent -- a state a real PMS
    # cannot produce, and one where the client's `local://` menu predicate disagrees
    # with itself between the season page and the episodes listed on it.
    if not show:
        skip("unmatching a season unmatches every episode under it", "need a show")
    else:
        def row_c13b(key_c13b):
            return (get(base, "/library/metadata/%s" % key_c13b)[1].get("Metadata") or [{}])[0]
        # An earlier check unmatches the show's FIRST season, so this needs the one
        # still carrying an agent guid -- the reason the fixture has two.
        season_c13b = next((r.get("ratingKey") for r in
                            get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
                            if str(row_c13b(r.get("ratingKey")).get("guid") or "").startswith("plex://season/")),
                           None)
        eps_c13b = [r["ratingKey"] for r in
                    get(base, "/library/metadata/%s/children" % season_c13b)[1].get("Metadata") or []
                    if r.get("type") == "episode"] if season_c13b else []
        if not eps_c13b:
            skip("unmatching a season unmatches every episode under it",
                 "no matched season with episodes left in the fixture")
        else:
            before_c13b = {k: row_c13b(k) for k in eps_c13b}
            status_c13b, _, _ = request(base, "PUT", "/library/metadata/%s/unmatch" % season_c13b)
            after_s_c13b = row_c13b(season_c13b)
            after_c13b = {k: row_c13b(k) for k in eps_c13b}
            check("unmatching a season unmatches every episode under it",
                  status_c13b == 200
                  and all(str(before_c13b[k].get("guid") or "").startswith("plex://") for k in eps_c13b)
                  and after_s_c13b.get("guid") == "local://%s" % season_c13b
                  and after_s_c13b.get("year") is None
                  and all(after_c13b[k].get("guid") == "local://%s" % k for k in eps_c13b)
                  and all(after_c13b[k].get("parentGuid") == "local://%s" % season_c13b for k in eps_c13b)
                  and all(after_c13b[k].get("title") == "Episode %s" % after_c13b[k].get("index")
                          for k in eps_c13b)
                  and all(after_c13b[k].get("summary") == "" for k in eps_c13b)
                  and all(after_c13b[k].get("year") is None for k in eps_c13b)
                  and all(not after_c13b[k].get("Role") for k in eps_c13b)
                  and all(after_c13b[k].get("parentThumb") is None for k in eps_c13b),
                  "season %s -> %s (year %s), episodes %s"
                  % (season_c13b, after_s_c13b.get("guid"), after_s_c13b.get("year"),
                     [(k, after_c13b[k].get("guid"), after_c13b[k].get("parentGuid"),
                       after_c13b[k].get("title")) for k in eps_c13b]))

        # Measured on kids (1.43.4) and zen (1.42.2), on audienceRating, rating,
    # userRating, contentRating, year, originallyAvailableAt, viewCount, viewOffset,
    # lastViewedAt, mediaHeight and mediaBitrate, and on a show library at type 2, 3
    # and 4: a row with NO value for the sort key sorts BELOW every row that has one
    # -- first ascending, last descending. plexsim keyed on `(value is None, value)`,
    # which puts it above instead. Twelve of the fifteen sorts a section advertises
    # default to `desc`, so Rating, Plays, Date Viewed, Critic Rating and the rest all
    # opened the grid on the rows with no value at all.
    rows_c13c = get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or []
    keys_c13c = [r["ratingKey"] for r in rows_c13c]
    if len(keys_c13c) < 2:
        skip("a row with no value sorts below every row that has one",
             "need two movies left in the fixture")
    else:
        # Built rather than hoped for: a negative year REMOVES the field (measured in
        # cycle 9), so this leaves exactly one row without one.
        request(base, "PUT", "/library/metadata/%s?year.value=2001" % keys_c13c[0])
        request(base, "PUT", "/library/metadata/%s?year.value=-1" % keys_c13c[-1])
        def sorted_c13c(spec_c13c):
            body_c13c = get(base, "/library/sections/%s/all?type=1&sort=%s" % (movie_section, spec_c13c))[1]
            return [(r["ratingKey"], r.get("year")) for r in body_c13c.get("Metadata") or []]
        asc_c13c, desc_c13c = sorted_c13c("year:asc"), sorted_c13c("year:desc")
        check("a row with no value sorts below every row that has one",
              asc_c13c and desc_c13c
              and asc_c13c[0] == (keys_c13c[-1], None)
              and desc_c13c[-1] == (keys_c13c[-1], None)
              and asc_c13c[-1][1] is not None and desc_c13c[0][1] is not None,
              "asc %s | desc %s" % (asc_c13c, desc_c13c))

        # Measured on zen (1.42.2), which holds 214 duplicated movies of 3887 and 504
    # duplicated episodes of 9287: `duplicate=1` is "this row has more than one Media"
    # -- 60 sampled movie rows carried 2 to 15 and 60 `duplicate=0` rows exactly one,
    # with no violation either way, and the two halves add up to the library. For a SHOW
    # or a SEASON, which have no Media of their own, it is "a leaf under it has more
    # than one" -- 44 shows and 74 seasons on zen, and three sampled `duplicate=1` shows
    # each had a multi-Media episode where three `duplicate=0` shows had none. kids holds
    # no multi-Media item at all, so it answers 0 and is consistent rather than
    # contradicting. plexsim returned 0 for every row, so the Duplicates control that
    # `includeAdvanced=1` puts in the filter bar was advertised and permanently empty.
    if not show_section:
        skip("the Duplicates filter selects the rows with more than one version",
             "need a show library")
    else:
        def dupes_c13d(type_c13d, value_c13d):
            body_c13d = get(base, "/library/sections/%s/all?type=%s&duplicate=%s"
                            % (show_section, type_c13d, value_c13d))[1]
            return [r["ratingKey"] for r in body_c13d.get("Metadata") or []]
        leaves_c13d = get(base, "/library/sections/%s/all?type=4" % show_section)[1].get("Metadata") or []
        dupe_c13d = next((r["ratingKey"] for r in leaves_c13d if len(r.get("Media") or []) > 1), None)
        if dupe_c13d is None:
            skip("the Duplicates filter selects the rows with more than one version",
                 "no two-version episode left in the fixture")
        else:
            parent_c13d = str((get(base, "/library/metadata/%s" % dupe_c13d)[1]
                               .get("Metadata") or [{}])[0].get("parentRatingKey"))
            every_c13d = [r["ratingKey"] for r in leaves_c13d]
            check("the Duplicates filter selects the rows with more than one version",
                  dupes_c13d(4, 1) == [dupe_c13d]
                  and dupe_c13d not in dupes_c13d(4, 0)
                  and sorted(dupes_c13d(4, 1) + dupes_c13d(4, 0)) == sorted(every_c13d)
                  # A show and a season carry no Media, so theirs is about their leaves.
                  and dupes_c13d(3, 1) == [parent_c13d]
                  and parent_c13d not in dupes_c13d(3, 0)
                  and len(dupes_c13d(2, 1)) == 1,
                  "episodes %s of %s, season %s of %s, shows %s"
                  % (dupes_c13d(4, 1), len(every_c13d), dupes_c13d(3, 1), parent_c13d,
                     dupes_c13d(2, 1)))

        # Measured on kids (1.43.4) and zen (1.42.2), which agree exactly: a bare `<field>=`
    # on a `tag` field means THREE different things, by field.
    #   a tag-TABLE field (genre, country, director, writer, producer, actor, label,
    #     collection, network, similar) matches NOTHING -- 0 of 5 and 0 of 3887;
    #   `studio` and `editionTitle` match EVERY row -- 5 of 5 and 3887 of 3887, even
    #     though 14 of 200 sampled zen movies carry no studio at all;
    #   everything else of that kind (contentRating, audioLanguage, subtitleLanguage)
    #     matches the rows with NO value -- contentRating= is 3 of 5 and 2864 of 3887.
    # And `contentRating=None`, in that exact spelling, is a synonym for the empty value
    # (2864 on zen, 3 on kids) where `none` and `NONE` match nothing; no other field has
    # such a synonym. The codec axes disagree between the two servers (kids 0, zen every
    # row), so they are a version difference and are left alone. plexsim applied the
    # valueless-row rule to all of them, so a bare `genre=` selected the whole library
    # and `studio=` selected only the rows that had none.
    def sized_c13e(query_c13e):
        path_c13e = "/library/sections/%s/all?type=1" % movie_section
        if query_c13e:
            path_c13e += "&" + query_c13e
        return len(get(base, path_c13e)[1].get("Metadata") or [])
    rows_c13e = [r["ratingKey"] for r in
                 get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("Metadata") or []]
    every_c13e = len(rows_c13e)
    if every_c13e < 2:
        skip("an empty tag value means nothing, everything or 'has no value', by field",
             "need two movies left in the fixture")
    else:
        # Built rather than hoped for: one row with a studio and a rating, one with
        # neither, so each of the three behaviours has something to tell apart. An empty
        # value REMOVES a field that is not title or summary (measured in cycle 9).
        request(base, "PUT", "/library/metadata/%s?studio.value=c13studio"
                             "&contentRating.value=PG&genre[0].tag.tag=c13genre" % rows_c13e[0])
        request(base, "PUT", "/library/metadata/%s?studio.value=&contentRating.value="
                             % rows_c13e[-1])
        empty_c13e = {name_c13e: sized_c13e("%s=" % name_c13e)
                      for name_c13e in ("genre", "director", "actor", "label", "studio",
                                        "editionTitle", "contentRating")}
        upper_c13e = sized_c13e("contentRating=None")
        lower_c13e = sized_c13e("contentRating=none")
        check("an empty tag value means nothing, everything or 'has no value', by field",
              all(empty_c13e[name_c13e] == 0
                  for name_c13e in ("genre", "director", "actor", "label"))
              and empty_c13e["studio"] == every_c13e
              and empty_c13e["editionTitle"] == every_c13e
              and 0 < empty_c13e["contentRating"] < every_c13e
              and upper_c13e == empty_c13e["contentRating"]
              and lower_c13e == 0,
              "%s of %s rows | contentRating=None %s, =none %s"
              % (empty_c13e, every_c13e, upper_c13e, lower_c13e))
        # The axis itself carries the bucket, LAST, after the alphabetical entries.
        axis_rows_c13e = get(base, "/library/sections/%s/contentRating" % movie_section)[1].get("Directory") or []
        axis_c13e = [d_c13e.get("title") for d_c13e in axis_rows_c13e]
        none_c13e = next((d_c13e for d_c13e in axis_rows_c13e if d_c13e.get("key") == "None"), None)
        studio_axis_c13e = [d_c13e.get("title") for d_c13e in
                            get(base, "/library/sections/%s/studio" % movie_section)[1].get("Directory") or []]
        check("the contentRating axis ends with a None bucket, and no other axis has one",
              len(axis_c13e) >= 2 and axis_c13e[-1] == "None" and "None" not in axis_c13e[:-1]
              and none_c13e == {"fastKey": "/library/sections/%s/all?contentRating=None" % movie_section,
                                "key": "None", "title": "None"}
              and "None" not in studio_axis_c13e,
              "contentRating %s | None row %s | studio %s" % (axis_c13e, none_c13e, studio_axis_c13e))

        # Measured on kids (1.43.4) and zen (1.42.2), identically, across seven values:
    # a tag or field axis REFUSES a `type=` it cannot parse -- `abc`, `1.5` and an empty
    # one are all 500 on genre, studio, actor, contentRating, firstCharacter, decade and
    # country, and 400 on `year` alone -- while `-1`, `99`, `2` and `04` are 200. The
    # MEDIA axes (resolution, audioLanguage, subtitleLanguage and the codecs) ignore
    # `type=` completely: the same list comes back for every value including `abc`, on a
    # movie library and a show library alike. And `firstCharacter` IS scoped by the type,
    # its counts following it (zen's show library: 13/40 shows at type=2, 13/64 seasons at
    # type=3, 185/207 episodes at type=4, empty at type=1). plexsim answered 200 to every
    # axis whatever the type said, scoped `resolution` by type, and read firstCharacter off
    # the whole section.
    codes_c13f = {}
    for axis_c13f in ("genre", "studio", "actor", "contentRating", "firstCharacter",
                      "decade", "country", "year", "resolution", "audioLanguage"):
        codes_c13f[axis_c13f] = [request(base, "GET", "/library/sections/%s/%s?type=%s"
                                         % (movie_section, axis_c13f, value_c13f))[0]
                                 for value_c13f in ("abc", "", "1.5", "-1", "99")]
    first_c13f = {value_c13f: get(base, "/library/sections/%s/firstCharacter?type=%s"
                                  % (show_section, value_c13f))[1].get("size")
                  for value_c13f in ("2", "3", "4", "1", "99")} if show_section else {}
    media_c13f = {value_c13f: [d_c13f.get("title") for d_c13f in
                               get(base, "/library/sections/%s/resolution?type=%s"
                                   % (movie_section, value_c13f))[1].get("Directory") or []]
                  for value_c13f in ("", "1", "4", "abc")}
    check("a tag or field axis refuses an unparseable type where a media axis ignores it",
          all(codes_c13f[axis_c13f] == [500, 500, 500, 200, 200]
              for axis_c13f in ("genre", "studio", "actor", "contentRating",
                                "firstCharacter", "decade", "country"))
          and codes_c13f["year"] == [400, 400, 400, 200, 200]
          and codes_c13f["resolution"] == [200] * 5
          and codes_c13f["audioLanguage"] == [200] * 5
          and len({tuple(v_c13f) for v_c13f in media_c13f.values()}) == 1
          and (not first_c13f or (first_c13f["1"] == 0 and first_c13f["99"] == 0
                                  and first_c13f["4"] > 0)),
          "%s | firstCharacter %s | resolution %s" % (codes_c13f, first_c13f, media_c13f))

        # Measured on zen (1.42.2) over its 507-show library and confirmed on kids (1.43.4):
    # `viewedLeafCount`, `viewedOrInProgressLeafCount` and `unviewedLeafCount` are real
    # filter fields on a show listing -- `viewedOrInProgressLeafCount=0` is 499 of 507
    # and `=1` is 4, `viewedLeafCount>>0` is exactly the 8 shows with a watched episode,
    # and `unviewedLeafCount>>0` is 505 -- while `leafCount` and `childCount` are NOT
    # (both answer the whole library, which plexsim already matched). plexsim answered
    # the same rows to `=0` and `=1`, i.e. it read all five as unknown fields. The first
    # of them is the key of a real PMS's own "Start Watching" shelf.
    if not show_section:
        skip("the leaf-count fields filter a show listing", "need a show library")
    else:
        def shows_c13g(query_c13g):
            path_c13g = "/library/sections/%s/all?type=2" % show_section
            if query_c13g:
                path_c13g += "&" + query_c13g
            return [r["ratingKey"] for r in get(base, path_c13g)[1].get("Metadata") or []]
        every_c13g = shows_c13g("")
        # Built rather than hoped for: scrobbling one EPISODE is what moves its show's
        # viewedLeafCount (measured in cycle 9), and by this point the suite has
        # unscrobbled everything it watched.
        leaf_c13g = next((r["ratingKey"] for r in
                          get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                          .get("Metadata") or []), None)
        if leaf_c13g:
            request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % leaf_c13g)
        watched_c13g = next((r for r in
                             get(base, "/library/sections/%s/all?type=2" % show_section)[1]
                             .get("Metadata") or [] if (r.get("viewedLeafCount") or 0) > 0), None)
        if watched_c13g is None:
            skip("the leaf-count fields filter a show listing", "no watched leaf in the fixture")
        else:
            key_c13g = watched_c13g["ratingKey"]
            check("the leaf-count fields filter a show listing where leafCount does not",
                  every_c13g
                  and shows_c13g("viewedLeafCount%3E%3E=0") == [key_c13g]
                  and key_c13g not in shows_c13g("viewedLeafCount=0")
                  and shows_c13g("viewedOrInProgressLeafCount%3E%3E=0") == [key_c13g]
                  and key_c13g not in shows_c13g("viewedOrInProgressLeafCount=0")
                  and shows_c13g("leafCount%3E%3E=0") == every_c13g
                  and shows_c13g("childCount%3E%3E=0") == every_c13g,
                  "all %s, watched %s, viewed>>0 %s, viewedOrInProgress=0 %s, leafCount>>0 %s"
                  % (every_c13g, key_c13g, shows_c13g("viewedLeafCount%3E%3E=0"),
                     shows_c13g("viewedOrInProgressLeafCount=0"), shows_c13g("leafCount%3E%3E=0")))

        # Measured on zen (1.42.2 -- the build plexsim reports), against a 3887-movie and
    # 507-show library: with no `count=` every library shelf carries at most SIX rows and
    # sets `more: true` -- movie.recentlyreleased, movie.recentlyadded, movie.topunwatched,
    # tv.recentlyaired, tv.startwatching and tv.toprated are all size 6, more true -- and
    # `/hubs` does the same (home.television.recent size 6, more true). `?count=3`,
    # `?count=12` and `?count=20` are honoured exactly, so six is the DEFAULT, not a
    # ceiling; the related shelf caps at six too (`movie.similar` size 6, more true).
    # plexsim used 12, a number no reference has ever answered. kids cannot witness it
    # either way: with five movies and one show, no shelf there can reach even six, which
    # is how the recorded "up to 12" survived twelve cycles unmeasured.
    if not show_section:
        skip("a shelf with no count carries at most six rows", "need a show library")
    else:
        shelves_c13h = [(h_c13h.get("hubIdentifier"), h_c13h.get("size"), h_c13h.get("more"))
                        for h_c13h in get(base, "/hubs/sections/%s" % show_section)[1].get("Hub") or []]
        deep_c13h = [row_c13h for row_c13h in shelves_c13h if (row_c13h[1] or 0) > 3]
        counted_c13h = {n_c13h: [h_c13h.get("size") for h_c13h in
                                 get(base, "/hubs/sections/%s?count=%s" % (show_section, n_c13h))[1]
                                 .get("Hub") or [] if h_c13h.get("size")]
                        for n_c13h in (1, 3, 8)}
        if not deep_c13h:
            skip("a shelf with no count carries at most six rows",
                 "no shelf in the fixture holds enough rows to overflow")
        else:
            check("a shelf with no count carries at most six rows and says there are more",
                  all(size_c13h == 6 and more_c13h is True
                      for _name_c13h, size_c13h, more_c13h in deep_c13h)
                  and all(size_c13h <= 1 for size_c13h in counted_c13h[1])
                  and all(size_c13h <= 3 for size_c13h in counted_c13h[3])
                  and max(counted_c13h[8] or [0]) > 6,
                  "%s | count=1 %s | count=3 %s | count=8 %s"
                  % (shelves_c13h, counted_c13h[1], counted_c13h[3], counted_c13h[8]))

        # Measured on zen (1.42.2 -- plexsim's own reported build): a movie library's
    # Recommended tab carries `movie.topunwatched` between the genre shelf and Recently
    # Watched, and a show library carries `tv.startwatching` after Recently Added and
    # `tv.toprated` after Rediscover. Each is `style: shelf`, `random: true`, and each
    # key is the listing its "see all" opens. kids (1.43.4) emits none of the three
    # although it HAS qualifying rows -- all three keys answer `size 1` there -- so this
    # is a version difference and zen is the witness that matters. plexsim built six
    # shelves where 1.42.2 builds eight.
    shelves_c13i = {h_c13i.get("hubIdentifier"): h_c13i for h_c13i in
                    get(base, "/hubs/sections/%s" % movie_section)[1].get("Hub") or []}
    order_c13i = list(shelves_c13i)
    top_c13i = shelves_c13i.get("movie.topunwatched.%s" % movie_section) or {}
    tv_c13i = {h_c13i.get("hubIdentifier"): h_c13i for h_c13i in
               get(base, "/hubs/sections/%s" % show_section)[1].get("Hub") or []} if show_section else {}
    start_c13i = tv_c13i.get("tv.startwatching.%s" % show_section) or {}
    rated_c13i = tv_c13i.get("tv.toprated.%s" % show_section) or {}
    check("the library tab carries the three shelves a 1.42.2 server builds",
          top_c13i.get("title") == "Top Unwatched Movies"
          and top_c13i.get("context") == "hub.movie.topunwatched"
          and top_c13i.get("type") == "movie" and top_c13i.get("random") is True
          and top_c13i.get("key") == ("/library/sections/%s/all?unwatched=1"
                                      "&audienceRating>=7.0&sort=audienceRating:desc" % movie_section)
          and order_c13i.index("movie.topunwatched.%s" % movie_section)
              == len(order_c13i) - 2
          and (not show_section or (
              start_c13i.get("title") == "Start Watching"
              and start_c13i.get("context") == "hub.tv.startwatching"
              and start_c13i.get("type") == "show"
              and start_c13i.get("key") == ("/library/sections/%s/all?type=2"
                                            "&viewedOrInProgressLeafCount=0" % show_section)
              and rated_c13i.get("title") == "Top Rated TV"
              and rated_c13i.get("context") == "hub.tv.toprated"
              and rated_c13i.get("key") == ("/library/sections/%s/all?type=2&unwatchedLeaves=1"
                                            "&audienceRating>=7.0&sort=audienceRating:desc"
                                            % show_section))),
          "movies %s | tv %s" % (order_c13i, list(tv_c13i)))

        # Re-measured on kids (1.43.4) and zen (1.42.2) across four queries that match
    # nothing at all (`nomatchatall`, `xyzzyplugh`, `qwertyuiopzz`, `1234567890zz`):
    # both servers answer the seventeen hubs in ONE order, and it is not the order
    # plexsim used. `movie` is FIFTH, not first, and `collection` is FOURTEENTH,
    # between genre and playlist, not seventh. (A query with fuzzy hits promotes the
    # hubs that scored -- `zzqqxx` moves actor to sixth on kids -- which is why this
    # has to be measured on a query that matches nothing; that promotion is recorded
    # and deliberately not reproduced.) The client lays the results page out from this
    # list, so every search drew its sections in the wrong order.
    order_c13j = [h_c13j.get("type") for h_c13j in
                  get(base, "/hubs/search?query=zzzznomatchzzzz")[1].get("Hub") or []]
    check("the search hubs come back in the order both servers answer",
          order_c13j == ["show", "artist", "album", "episode", "movie", "track",
                         "photoalbum", "autotag", "photo", "tag", "actor", "director",
                         "genre", "collection", "playlist", "shared", "place"],
          "%s" % order_c13j)

        # Measured on kids (1.43.4) and zen (1.42.2): `/hubs/search` returns the ITEMS a
    # matched tag is on, not just the tag row. kids `colin` answers the two tag rows AND
    # Firefly (`reason: actor, reasonID 105, reasonTitle "Colin Patrick Lynch"`) and
    # Sintel (`reason: director, reasonTitle "Colin Levy"`); zen `tom hanks` answers
    # eleven movie rows all reasoned on the actor, and across ten zen queries 53 of 81
    # item rows carried a `reason` -- most search hits on a real library are tag hits.
    # `/library/search` does NOT carry them (kids `colin` is two Directory rows and
    # nothing else, zen `spielberg` five), so this is the hubs endpoint alone. plexsim
    # matched titles only, so searching an actor, a director or a genre drew the tag row
    # with an empty Movies shelf beside it -- and its `reason` code could never fire.
    # One WORD of a director's name, because plexsim's tag matcher is a word-prefix
    # matcher and a two-word needle never begins a single word.
    person_c13k = next((t_c13k.get("tag") for t_c13k in
                        (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                        .get("Director") or [] if t_c13k.get("tag")), None)
    word_c13k = (person_c13k or "").split()[-1] if person_c13k else None
    if not word_c13k:
        skip("a search returns the items behind a matched tag, and only in the hubs form",
             "no director left on the fixture movie")
    else:
        hubs_c13k = {h_c13k.get("type"): h_c13k for h_c13k in
                     get(base, "/hubs/search?query=%s" % quote(word_c13k))[1].get("Hub") or []}
        hit_c13k = next((r_c13k for r_c13k in (hubs_c13k.get("movie") or {}).get("Metadata") or []
                         if str(r_c13k.get("ratingKey")) == str(movie)), None)
        tag_c13k = (hubs_c13k.get("director") or {}).get("Directory") or []
        flat_c13k = get(base, "/library/search?query=%s" % quote(word_c13k))[1]
        items_c13k = [r_c13k for r_c13k in flat_c13k.get("SearchResult") or [] if r_c13k.get("Metadata")]
        check("a search returns the items behind a matched tag, and only in the hubs form",
              hit_c13k is not None
              and hit_c13k.get("reason") == "director"
              and hit_c13k.get("reasonTitle") == person_c13k
              and hit_c13k.get("reasonID") is not None
              and len(tag_c13k) == 1
              and not items_c13k,
              "hub row %s | tag rows %s | /library/search items %s"
              % ({k_c13k: hit_c13k.get(k_c13k) for k_c13k in ("ratingKey", "reason", "reasonTitle")}
                 if hit_c13k else None, len(tag_c13k), len(items_c13k)))

        # Measured on kids (1.43.4) and zen (1.42.2): an EPISODE that carries an
    # `audienceRating` carries `audienceRatingImage` and a `Rating` element too -- 60 of
    # 60 One Piece episodes and 3 of 3 kids episodes, with no exception, and the three
    # kids episodes with no rating carry none of the three. A SEASON never carries any of
    # them (0 of 12 sampled across both servers). plexsim built `Rating` and
    # `audienceRatingImage` for a show and a movie only, so its own episode rows
    # contradicted its own movie rows, and the rating badge was missing from every
    # episode page and every episode row of a listing.
    leaf_c13l = next((r_c13l for r_c13l in
                      get(base, "/library/sections/%s/all?type=4" % show_section)[1].get("Metadata") or []
                      if r_c13l.get("audienceRating") is not None), None) if show_section else None
    if leaf_c13l is None:
        skip("an episode with an audience rating carries its image and a Rating row",
             "no rated episode in the fixture")
    else:
        detail_c13l = (get(base, "/library/metadata/%s" % leaf_c13l["ratingKey"])[1]
                       .get("Metadata") or [{}])[0]
        season_c13l = (get(base, "/library/metadata/%s" % detail_c13l.get("parentRatingKey"))[1]
                       .get("Metadata") or [{}])[0]
        check("an episode with an audience rating carries its image and a Rating row",
              detail_c13l.get("audienceRatingImage") == "themoviedb://image.rating"
              and [(r_c13l.get("type"), r_c13l.get("image"), r_c13l.get("value"))
                   for r_c13l in detail_c13l.get("Rating") or []]
                  == [("audience", "themoviedb://image.rating", detail_c13l.get("audienceRating"))]
              # and a season carries none of it, on either server
              and not season_c13l.get("Rating")
              and not season_c13l.get("audienceRatingImage"),
              "episode %s / %s | season Rating %s"
              % (detail_c13l.get("audienceRatingImage"), detail_c13l.get("Rating"),
                 season_c13l.get("Rating")))

        # Measured on kids (1.43.4) and zen (1.42.2), 20 rows across five shows: a SHOW's
    # `/allLeaves` gives every episode row the SHOW's `studio` -- "Mutant Enemy
    # Productions" on all six of Firefly's, "Toei Animation" on One Piece's, "20th
    # Century Fox Television" on The Simpsons'. Nothing else carries it: the episode's
    # own detail row, the season's `/children` and the SECTION's `allLeaves` all answer
    # no studio on both servers. plexsim sent none anywhere.
    if not show:
        skip("a show's allLeaves gives every episode the show's studio", "need a show")
    else:
        studio_c13m = (get(base, "/library/metadata/%s" % show)[1].get("Metadata") or [{}])[0].get("studio")
        leaves_c13m = get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []
        one_c13m = (get(base, "/library/metadata/%s" % leaves_c13m[0]["ratingKey"])[1]
                    .get("Metadata") or [{}])[0] if leaves_c13m else {}
        season_kids_c13m = get(base, "/library/metadata/%s/children"
                               % one_c13m.get("parentRatingKey"))[1].get("Metadata") or []
        section_c13m = get(base, "/library/sections/%s/allLeaves" % show_section)[1].get("Metadata") or []
        if not studio_c13m or not leaves_c13m:
            skip("a show's allLeaves gives every episode the show's studio",
                 "the show has no studio left")
        else:
            check("a show's allLeaves gives every episode the show's studio, and nothing else does",
                  all(r_c13m.get("studio") == studio_c13m for r_c13m in leaves_c13m)
                  and one_c13m.get("studio") is None
                  and all(r_c13m.get("studio") is None for r_c13m in season_kids_c13m)
                  and all(r_c13m.get("studio") is None for r_c13m in section_c13m),
                  "allLeaves %s | detail %r | season children %s | section allLeaves %s"
                  % ([r_c13m.get("studio") for r_c13m in leaves_c13m], one_c13m.get("studio"),
                     [r_c13m.get("studio") for r_c13m in season_kids_c13m],
                     [r_c13m.get("studio") for r_c13m in section_c13m]))

        # Measured on kids (1.43.4) and zen (1.42.2): a SEASON row of `/library/recentlyAdded`
    # carries `parentSummary`, the SHOW's summary -- 2 of 2 season rows on kids and 3 of 3
    # zen season rows whose show has a summary, while the 4 zen rows whose show has an
    # empty one carry no such field (an empty string is omitted, like every other). A
    # movie row never carries it. And it is on that endpoint ALONE: swept across
    # /library/sections/{id}/recentlyAdded, /newest, /hubs/home/recentlyAdded,
    # /hubs/sections/{id}, /hubs/promoted, a type=3 listing, /children and /library/onDeck,
    # every other season row on both servers carries none. plexsim never sent it anywhere.
    rows_c13n = get(base, "/library/recentlyAdded")[1].get("Metadata") or []
    seasons_c13n = [r_c13n for r_c13n in rows_c13n if r_c13n.get("type") == "season"]
    movies_c13n = [r_c13n for r_c13n in rows_c13n if r_c13n.get("type") == "movie"]
    if not seasons_c13n:
        skip("a season in the global Recently Added carries its show's summary",
             "no season in the global recently-added list")
    else:
        show_c13n = (get(base, "/library/metadata/%s" % seasons_c13n[0].get("parentRatingKey"))[1]
                     .get("Metadata") or [{}])[0]
        elsewhere_c13n = []
        for path_c13n in ("/library/sections/%s/all?type=3" % show_section,
                          "/library/sections/%s/recentlyAdded" % show_section,
                          "/library/metadata/%s/children" % show):
            elsewhere_c13n += [r_c13n for r_c13n in get(base, path_c13n)[1].get("Metadata") or []
                               if r_c13n.get("type") == "season"]
        check("a season in the global Recently Added carries its show's summary, and only there",
              bool(show_c13n.get("summary"))
              and all(r_c13n.get("parentSummary") == show_c13n.get("summary")
                      for r_c13n in seasons_c13n)
              and all("parentSummary" not in r_c13n for r_c13n in movies_c13n)
              and all("parentSummary" not in r_c13n for r_c13n in elsewhere_c13n),
              "seasons %s | movies %s | elsewhere %s"
              % ([r_c13n.get("parentSummary") is not None for r_c13n in seasons_c13n],
                 [("parentSummary" in r_c13n) for r_c13n in movies_c13n],
                 [("parentSummary" in r_c13n) for r_c13n in elsewhere_c13n]))

        # Measured on zen (1.42.2 -- the build plexsim reports): `/hubs/promoted` answers a
    # DIFFERENT third shelf when the client sends `pinnedContentDirectoryID` beside
    # `contentDirectoryID`. Bare, it is `movie.recentlyadded.1` / "Recently Added in Zen
    # Movies", keyed on the section listing; with the parameter it is
    # `home.movies.recent.1` / "Recently Added Movies", context `hub.home.movies.recent`,
    # keyed on `/hubs/home/recentlyAdded?type=1&pinnedContentDirectoryID=1`. A show
    # library gives `home.television.recent.N` / "Recently Added TV". kids (1.43.4)
    # ignores the parameter entirely, so this is a version difference and zen is the
    # witness. The web client does send it -- its Promoted fetch builds
    # `{contentDirectoryID, pinnedContentDirectoryID, includeMeta, excludeFields}` --
    # and plexsim answered the bare shape to both, so a pinned library's Home row was
    # titled and keyed as the unpinned one.
    def promoted_c13o(section_c13o, pinned_c13o):
        path_c13o = "/hubs/promoted?contentDirectoryID=%s" % section_c13o
        if pinned_c13o:
            path_c13o += "&pinnedContentDirectoryID=%s" % section_c13o
        return [(h_c13o.get("hubIdentifier"), h_c13o.get("title"), h_c13o.get("context"),
                 h_c13o.get("key"), h_c13o.get("type"))
                for h_c13o in get(base, path_c13o)[1].get("Hub") or []]
    bare_c13o = promoted_c13o(movie_section, False)
    pinned_c13o = promoted_c13o(movie_section, True)
    tv_c13o = promoted_c13o(show_section, True) if show_section else []
    check("pinnedContentDirectoryID reshapes the promoted Recently Added shelf",
          bare_c13o[:2] == pinned_c13o[:2]
          and bare_c13o[2][0] == "movie.recentlyadded.%s" % movie_section
          and pinned_c13o[2] == ("home.movies.recent.%s" % movie_section,
                                 "Recently Added Movies", "hub.home.movies.recent",
                                 "/hubs/home/recentlyAdded?type=1&pinnedContentDirectoryID=%s"
                                 % movie_section, "movie")
          and (not show_section or tv_c13o[2] == (
              "home.television.recent.%s" % show_section, "Recently Added TV",
              "hub.home.television.recent",
              "/hubs/home/recentlyAdded?type=2&pinnedContentDirectoryID=%s" % show_section,
              "mixed")),
          "bare %s | pinned %s | tv %s" % (bare_c13o[2:], pinned_c13o[2:], tv_c13o[2:]))

        # Re-measured on zen (1.42.2) against One Piece, a 24-season show -- the first time
    # this project has had a show deep enough to tell the two rules apart. A show's
    # `/children` carries the "All episodes" `Directory` row exactly when the PAGE comes
    # back with more than one season row, wherever the page starts: Start=0 Size=10 and
    # Start=10 Size=10 both answer size 11 with the row, Start=5 Size=3 answers 4 with it,
    # and Start=23 Size=2, Start=3 Size=1 and Start=0 Size=1 -- all one-row pages -- answer
    # 1 without it. kids agrees on all six cases it can express. Cycle 8 recorded "at
    # Start>0 the two servers disagree, follow kids and drop it", but kids has TWO seasons,
    # so every Start>0 page there is a one-row page: the disagreement was a two-season
    # library, not a version difference, and fun was right. A virtualised season list
    # therefore mis-measured its own height on every page but the first.
    if not show:
        skip("the All-episodes row follows the page's row count, not its offset", "need a show")
    else:
        def paged_c13p(start_c13p, size_c13p):
            # The paging parameters go in the query here: a real PMS reads them from the
            # headers, and plexsim normalises both spellings into the same place.
            _, body_c13p, _ = get(base, "/library/metadata/%s/children"
                                        "?X-Plex-Container-Start=%s&X-Plex-Container-Size=%s"
                                        % (show, start_c13p, size_c13p))
            return (body_c13p.get("size"), len(body_c13p.get("Metadata") or []),
                    len(body_c13p.get("Directory") or []))
        seasons_c13p = len(get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                           .get("Metadata") or [])
        if seasons_c13p < 3:
            skip("the All-episodes row follows the page's row count, not its offset",
                 "need three seasons to tell the offset rule from the row-count rule")
        else:
            cases_c13p = {(start_c13p, size_c13p): paged_c13p(start_c13p, size_c13p)
                          for start_c13p, size_c13p in ((0, 2), (1, 2), (2, 2), (0, 1), (1, 1),
                                                        (seasons_c13p - 1, 2))}
            check("the All-episodes row follows the page's row count, not its offset",
                  # more than one row on the page -> the row is there, wherever it starts
                  cases_c13p[(0, 2)] == (3, 2, 1) and cases_c13p[(1, 2)] == (3, 2, 1)
                  # a one-row page drops it, at Start=0 as much as past it
                  and cases_c13p[(0, 1)] == (1, 1, 0) and cases_c13p[(1, 1)] == (1, 1, 0)
                  # and the last page, which has one row left
                  and cases_c13p[(seasons_c13p - 1, 2)] == (1, 1, 0),
                  "%s" % cases_c13p)

        # Re-measured on the copy, field by field either side of one call, after cycle 13's
    # season cascade raised the question: `PUT /library/metadata/{episode}/unmatch` on an
    # episode whose SEASON is still matched drops exactly what the cascade drops --
    # `Director`, `Writer`, `Producer`, `Role` and `audienceRating` removed, `summary`
    # emptied, `year` removed (and `parentYear` appearing in its place, from the season
    # that still has one), `titleSort` removed where there was one, the thumb re-versioned
    # and every `Image.alt` following the new title. The two things the cascade does that
    # this does not -- `parentGuid` moving to `local://` and `parentThumb` going null --
    # are consequences of the SEASON changing, not of the episode write. plexsim changed
    # the guid and the title and left the rest, so an unmatched episode kept the agent's
    # summary, rating and cast under a `local://` guid: a state a real PMS cannot produce.
    if not show:
        skip("unmatching an episode alone drops what the cascade drops", "need a show")
    else:
        leaf_c13q = next((r["ratingKey"] for r in
                          get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                          .get("Metadata") or []
                          if not str((get(base, "/library/metadata/%s" % r["ratingKey"])[1]
                                      .get("Metadata") or [{}])[0].get("guid") or "").startswith("local://")),
                         None)
        if not leaf_c13q:
            skip("unmatching an episode alone drops what the cascade drops",
                 "every episode in the fixture is already unmatched")
        else:
            def leaf_row_c13q():
                return (get(base, "/library/metadata/%s" % leaf_c13q)[1].get("Metadata") or [{}])[0]
            before_c13q = leaf_row_c13q()
            season_before_c13q = (get(base, "/library/metadata/%s"
                                      % before_c13q.get("parentRatingKey"))[1].get("Metadata") or [{}])[0]
            status_c13q, _, _ = request(base, "PUT", "/library/metadata/%s/unmatch" % leaf_c13q)
            after_c13q = leaf_row_c13q()
            season_after_c13q = (get(base, "/library/metadata/%s"
                                     % before_c13q.get("parentRatingKey"))[1].get("Metadata") or [{}])[0]
            check("unmatching an episode alone drops what the cascade drops",
                  status_c13q == 200
                  and after_c13q.get("guid") == "local://%s" % leaf_c13q
                  and after_c13q.get("summary") == ""
                  and after_c13q.get("year") is None
                  and after_c13q.get("audienceRating") is None
                  and not after_c13q.get("Role") and not after_c13q.get("Director")
                  and not after_c13q.get("Writer") and not after_c13q.get("Producer")
                  # the SEASON is untouched, so the parent links stay where they were
                  and after_c13q.get("parentGuid") == before_c13q.get("parentGuid")
                  and after_c13q.get("parentThumb") == before_c13q.get("parentThumb")
                  and season_after_c13q.get("guid") == season_before_c13q.get("guid"),
                  "guid %s, summary %r, year %s, rating %s, Role %s | parent %s"
                  % (after_c13q.get("guid"), after_c13q.get("summary"), after_c13q.get("year"),
                     after_c13q.get("audienceRating"), len(after_c13q.get("Role") or []),
                     after_c13q.get("parentGuid") == before_c13q.get("parentGuid")))

        # Re-measured on the copy by snapshotting the show, both seasons and all six episodes
    # either side of one `PUT /library/metadata/{show}/unmatch`: it does NOT edit the show
    # row alone. Every season under it becomes `local://<id>` and every episode too, with
    # the generated `Episode N` and the same drops a direct episode unmatch makes; each
    # season's `parentGuid` and each episode's `grandparentGuid` follow the show to its
    # `tv.plex.agents.none://<id>`, each episode's `parentGuid` follows its season, and the
    # artwork goes with them -- the show's `art`, the seasons' `thumb`/`art`/`parentThumb`,
    # the episodes' `art`/`parentThumb`/`grandparentThumb`/`grandparentArt` all removed.
    # The inherited `contentRating` goes from every descendant as well. plexsim edited the
    # show row and stopped, leaving matched seasons and episodes under an unmatched show,
    # which is the state the client's own Match/Fix Match predicate reads per row.
    if not show:
        skip("unmatching a show unmatches every season and episode under it", "need a show")
    else:
        def row_c13r(key_c13r):
            return (get(base, "/library/metadata/%s" % key_c13r)[1].get("Metadata") or [{}])[0]
        seasons_c13r = [r["ratingKey"] for r in
                        get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                        .get("Metadata") or [] if r.get("type") == "season"]
        leaves_c13r = [r["ratingKey"] for r in
                       get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []]
        matched_c13r = [k for k in seasons_c13r
                        if str(row_c13r(k).get("guid") or "").startswith("plex://")]
        status_c13r, _, _ = request(base, "PUT", "/library/metadata/%s/unmatch" % show)
        after_c13r = {k: row_c13r(k) for k in [show] + seasons_c13r + leaves_c13r}
        check("unmatching a show unmatches every season and episode under it",
              status_c13r == 200 and bool(matched_c13r) and bool(leaves_c13r)
              and after_c13r[show].get("guid") == "tv.plex.agents.none://%s" % show
              and all(after_c13r[k].get("guid") == "local://%s" % k for k in seasons_c13r)
              and all(after_c13r[k].get("parentGuid") == after_c13r[show].get("guid")
                      for k in seasons_c13r)
              and all(after_c13r[k].get("guid") == "local://%s" % k for k in leaves_c13r)
              and all(after_c13r[k].get("grandparentGuid") == after_c13r[show].get("guid")
                      for k in leaves_c13r)
              and all(after_c13r[k].get("parentGuid")
                      == "local://%s" % after_c13r[k].get("parentRatingKey") for k in leaves_c13r)
              and all(after_c13r[k].get("summary") == "" for k in leaves_c13r)
              and all(after_c13r[k].get("contentRating") is None for k in leaves_c13r),
              "show %s | seasons %s | leaves %s"
              % (after_c13r[show].get("guid"),
                 [after_c13r[k].get("guid") for k in seasons_c13r],
                 [(after_c13r[k].get("guid"), after_c13r[k].get("parentGuid")) for k in leaves_c13r]))

        # Re-measured on the copy: after `PUT /library/metadata/{movie}/unmatch` the server's
    # `/activities` is EMPTY -- the whole refresh sequence runs and ends (12 frames for a
    # one-Media row, 18 for a two-Media one). plexsim opened an `Unmatching` activity at
    # progress 0 and never ended it, so it was still listed minutes later: the Dashboard
    # spinner then turns for the life of the process, which is the exact failure
    # docs/findings.md names for an activity that never ends. Cycle 9 fixed this for a
    # season and an episode -- both go through notify_refreshed -- and left the movie path.
    before_c13s = len(get(base, "/activities")[1].get("Activity") or [])
    target_c13s = next((r["ratingKey"] for r in
                        get(base, "/library/sections/%s/all?type=1" % movie_section)[1]
                        .get("Metadata") or []), None)
    if not target_c13s:
        skip("an unmatch leaves no activity running", "no movie left in the fixture")
    else:
        request(base, "PUT", "/library/metadata/%s/unmatch" % target_c13s)
        after_c13s = get(base, "/activities")[1].get("Activity") or []
        check("an unmatch leaves no activity running",
              before_c13s == 0 and after_c13s == [],
              "before %s, after %s" % (before_c13s,
                                       [(a_c13s.get("title"), a_c13s.get("progress"))
                                        for a_c13s in after_c13s]))

        # Re-measured on the copy: `PUT /library/metadata/{season}?index.value=5` moves the
    # season from 1 to 5 AND rewrites every episode's `parentIndex` to 5 -- on the detail
    # row and in the season's `/children` alike. It is the same rule cycle 9 measured for
    # a parent's `title` and `studio`, which plexsim already copies; `index` was missed,
    # so an episode kept pointing at a season number that no longer existed and any client
    # grouping by `parentIndex` drew an empty season.
    if not show:
        skip("a season's index lands on its episodes' parentIndex", "need a show")
    else:
        season_c13t = next((r["ratingKey"] for r in
                            get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                            .get("Metadata") or [] if r.get("type") == "season"), None)
        kids_c13t = [r["ratingKey"] for r in
                     get(base, "/library/metadata/%s/children" % season_c13t)[1].get("Metadata") or []
                     if r.get("type") == "episode"] if season_c13t else []
        if not kids_c13t:
            skip("a season's index lands on its episodes' parentIndex", "no episode under a season")
        else:
            request(base, "PUT", "/library/metadata/%s?index.value=41" % season_c13t)
            detail_c13t = [(get(base, "/library/metadata/%s" % k)[1].get("Metadata") or [{}])[0]
                           .get("parentIndex") for k in kids_c13t]
            listed_c13t = [r.get("parentIndex") for r in
                           get(base, "/library/metadata/%s/children" % season_c13t)[1]
                           .get("Metadata") or [] if r.get("type") == "episode"]
            season_index_c13t = (get(base, "/library/metadata/%s" % season_c13t)[1]
                                 .get("Metadata") or [{}])[0].get("index")
            check("a season's index lands on its episodes' parentIndex",
                  season_index_c13t == 41
                  and detail_c13t == [41] * len(kids_c13t)
                  and listed_c13t == [41] * len(kids_c13t),
                  "season %s, detail %s, children %s"
                  % (season_index_c13t, detail_c13t, listed_c13t))

        # Measured on a writable copy: editing a SHOW's `contentRating` moves it on the
    # episodes beneath it -- the write side of the rule kids and zen show from the read
    # side, where every episode of a show carries the show's certificate and no season
    # carries one. plexsim derived it once, when the catalog loaded, so a show edited
    # afterwards left its episodes showing the old certificate, and a show whose rating was
    # cleared left them showing one it no longer had.
    #
    # The copy distinguishes an episode's OWN agent rating from an inherited one and moves
    # only the inherited ones; plexsim's catalog has one column for both, so it moves all
    # of them. That divergence is named here rather than guessed around.
    if not show or not show_section:
        skip("a show's content rating follows onto its episodes when it is edited",
             "need a show")
    else:
        request(base, "PUT", "/library/metadata/%s?contentRating.value=TV-C13" % show)
        after_c13v = [(get(base, "/library/metadata/%s" % r["ratingKey"])[1].get("Metadata") or [{}])[0]
                      .get("contentRating")
                      for r in get(base, "/library/metadata/%s/allLeaves" % show)[1]
                      .get("Metadata") or []]
        seasons_c13v = [(get(base, "/library/metadata/%s" % r["ratingKey"])[1].get("Metadata") or [{}])[0]
                        .get("contentRating")
                        for r in get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                        .get("Metadata") or [] if r.get("type") == "season"]
        request(base, "PUT", "/library/metadata/%s?contentRating.value=" % show)
        cleared_c13v = [(get(base, "/library/metadata/%s" % r["ratingKey"])[1].get("Metadata") or [{}])[0]
                        .get("contentRating")
                        for r in get(base, "/library/metadata/%s/allLeaves" % show)[1]
                        .get("Metadata") or []]
        check("a show's content rating follows onto its episodes when it is edited",
              bool(after_c13v) and set(after_c13v) == {"TV-C13"}
              and all(r_c13v is None for r_c13v in seasons_c13v)
              and all(r_c13v is None for r_c13v in cleared_c13v),
              "set %s | seasons %s | cleared %s" % (after_c13v, seasons_c13v, cleared_c13v))


    # Cycle 14: a real PMS derives titleSort by trimming whitespace and folding
    # combining marks BEFORE stripping the article. Measured on zen over 5,294 rows
    # -- all 21 accented rows carry the folded form and all 90 leading-space rows
    # carry the trimmed one, with no counterexample, which is what makes it the
    # server's derivation and not agent metadata. plexsim stripped the article only,
    # so an accented row sorted under its raw title and sent no titleSort at all.
    orig_c14a = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("title") or "Movie")
    request(base, "PUT", "/library/metadata/%s?titleSort.locked=0" % movie)
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("Amélie Café")))
    fold_c14a = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("titleSort"))
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("  Zebra Test")))
    ws_c14a = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
               .get("titleSort"))
    # A title whose non-ASCII does not decompose is left alone, so it sends none.
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("Zoro’s Swords")))
    keep_c14a = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("titleSort"))
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote(orig_c14a)))
    check("titleSort trims whitespace and folds combining marks before the article",
          fold_c14a == "Amelie Cafe" and ws_c14a == "Zebra Test" and keep_c14a is None,
          "accented %r | leading-space %r | undecomposable %r"
          % (fold_c14a, ws_c14a, keep_c14a))


    # Cycle 14: the word-prefix matcher folds combining marks, the section's
    # substring box does not -- both measured on zen, which holds real accented
    # titles. Give a fixture row an accented name and ask for the plain spelling.
    orig_c14b = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("title") or "Movie")
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("Amélie Café")))
    hub_c14b = []
    for hub_row_c14b in get(base, "/hubs/search?query=Amelie")[1].get("Hub") or []:
        hub_c14b += [r.get("title") for r in hub_row_c14b.get("Metadata") or []]
    flat_c14b = [(r.get("Metadata") or {}).get("title")
                 for r in get(base, "/library/search?query=Amelie")[1].get("SearchResult") or []]
    plain_c14b = [r.get("title") for r in
                  get(base, "/library/sections/%s/search?type=1&query=Amelie"
                      % movie_section)[1].get("Metadata") or []]
    acc_c14b = [r.get("title") for r in
                get(base, "/library/sections/%s/search?type=1&query=%s"
                    % (movie_section, urllib.parse.quote("Amélie")))[1].get("Metadata") or []]
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote(orig_c14b)))
    check("the word matcher folds accents and the substring box does not",
          "Amélie Café" in hub_c14b and "Amélie Café" in flat_c14b
          and plain_c14b == [] and "Amélie Café" in acc_c14b,
          "hubs %s | flat %s | plain %s | accented %s"
          % (hub_c14b[:2], flat_c14b[:2], plain_c14b, acc_c14b))


    # Cycle 14 read the SPACE as what turns the fold off; cycle 15 re-measured it over
    # 52 queries on zen and the space is not the rule -- the COLON is. The fixture row
    # cycle 14 used dropped the colon from zen's actual title, so the check asserted a
    # shape zen was never asked for. Both of zen's titles are used verbatim here now:
    # `Bogotá: City of the Lost`, where `Bogota City` really does find nothing, and
    # `Adorável Trapalhão`, where the folded TWO-WORD query finds the row.
    orig_c14c = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("title") or "Movie")
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("Bogotá: City of the Lost")))

    def titles_c14c(spec):
        return [r.get("title") for r in
                get(base, "/library/sections/%s/all?type=1&title=%s"
                    % (movie_section, urllib.parse.quote(spec)))[1].get("Metadata") or []]

    want_c14c = ["Bogotá: City of the Lost"]
    colon_c14c = {spec_c14c: titles_c14c(spec_c14c) == want_c14c for spec_c14c in (
        "Bogota", "Bogotá", "Bogota City", "Bogotá City", "Bogot City",
        "Bogota: City", "Bogota: City of the Lost", "Bogota ", "Bogota:  City")}
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("Adorável Trapalhão")))
    want2_c14c = ["Adorável Trapalhão"]
    plain_c14c = {spec_c14c: titles_c14c(spec_c14c) == want2_c14c for spec_c14c in (
        "Adoravel Trapalhao", "Adoravel Trap", "Adoravel", "Adorave Trapalhao",
        "Adoravel Trapalhão")}
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote(orig_c14c)))
    check("title= folds across a space, and a whole word is what a folded term needs",
          colon_c14c == {"Bogota": True, "Bogotá": True, "Bogota City": False,
                         "Bogotá City": True, "Bogot City": True,
                         "Bogota: City": True, "Bogota: City of the Lost": True,
                         "Bogota ": False, "Bogota:  City": False}
          and plain_c14c == {"Adoravel Trapalhao": True, "Adoravel Trap": True,
                             "Adoravel": True, "Adorave Trapalhao": False,
                             "Adoravel Trapalhão": True},
          "colon %s | plain %s" % (colon_c14c, plain_c14c))


    # Cycle 14: `/filters` is the list the server's OWN VERSION advertises. kids
    # (1.43.4) answers 28 for a movie library, 17 at type=4 and 6 at type=10; zen
    # (1.42.2 -- the build plexsim reports) answers 22, 9 and 5, and the difference
    # is exactly the media-codec axes: `hdr10plus`, `atmos`, `videoCodec`,
    # `audioCodec`, `subtitleCodec`, `audioLayout` everywhere, plus `hdr` and `dovi`
    # on the EPISODE list, which keeps them on the movie list. Types 2, 3, 8, 9, 13
    # and 20 are identical on both, and so is the advanced Duplicates row. plexsim
    # shipped kids' lists, so its filter bar offered a 1.42.2 client six axes that
    # build's own libraries do not have.
    codec_c14d = {"hdr10plus", "atmos", "videoCodec", "audioCodec", "subtitleCodec",
                  "audioLayout"}

    def filt_c14d(spec):
        return [d.get("filter") for d in
                get(base, "/library/sections/%s/filters%s" % (movie_section, spec))[1]
                .get("Directory") or []]

    movie_c14d = filt_c14d("")
    ep_c14d = filt_c14d("?type=4")
    show_c14d = filt_c14d("?type=2")
    ten_c14d = filt_c14d("?type=10")
    adv_c14d = filt_c14d("?includeAdvanced=1")
    check("/filters advertises the 1.42.2 lists plexsim's version reports",
          len(movie_c14d) == 22 and len(ep_c14d) == 9 and len(show_c14d) == 15
          and len(ten_c14d) == 5 and len(adv_c14d) == 23
          and not codec_c14d & set(movie_c14d) and not codec_c14d & set(ep_c14d)
          and "hdr" in movie_c14d and "hdr" not in ep_c14d and "dovi" not in ep_c14d
          and "duplicate" in adv_c14d,
          "movie %d | episode %d | show %d | type10 %d | advanced %d"
          % (len(movie_c14d), len(ep_c14d), len(show_c14d), len(ten_c14d), len(adv_c14d)))


    # Cycle 14: the grid's filter bar is built from `Meta.Type[].Filter`, which is a
    # different fixture from /filters and carried the same 1.43.4 axes. On zen
    # (1.42.2) a movie Type advertises 22 and an episode Type 9; on kids (1.43.4)
    # they are 28 and 17. Trimming /filters alone left the two disagreeing, which is
    # worse than either: the bar drew axes the axis list no longer names.
    codec_c14e = {"hdr10plus", "atmos", "videoCodec", "audioCodec", "subtitleCodec",
                  "audioLayout"}

    def metatypes_c14e(section, type_number):
        types_c14e = (get(base, "/library/sections/%s/all?type=%s&includeMeta=1"
                          % (section, type_number))[1].get("Meta") or {}).get("Type") or []
        return {t.get("type"): [f.get("key", "").rsplit("/", 1)[-1].split("?")[0]
                                for f in t.get("Filter") or []] for t in types_c14e}

    mv_c14e = metatypes_c14e(movie_section, 1)
    if not show_section:
        skip("the Meta descriptor advertises its version's filter list", "need a show")
    else:
        sh_c14e = metatypes_c14e(show_section, 2)
        check("the Meta descriptor advertises its version's filter list",
              len(mv_c14e.get("movie") or []) == 22
              and len(sh_c14e.get("episode") or []) == 9
              and len(sh_c14e.get("show") or []) == 15
              and not codec_c14e & set(mv_c14e.get("movie") or [])
              and not codec_c14e & set(sh_c14e.get("episode") or [])
              and "hdr" in (mv_c14e.get("movie") or [])
              and "hdr" not in (sh_c14e.get("episode") or []),
              "movie %d | episode %d | show %d"
              % (len(mv_c14e.get("movie") or []), len(sh_c14e.get("episode") or []),
                 len(sh_c14e.get("show") or [])))


    # Cycle 14: the row that survives a `group=` is the group's MINIMUM addedAt,
    # picked before the sort -- so the same SET comes back under every sort and only
    # the order changes. Measured on kids (group=type answers the lowest-addedAt row,
    # not the lowest ratingKey, under three different sorts) and on zen (the minimum
    # addedAt in 88/88 year groups and 3450/3450 title groups). plexsim kept the first
    # row the sort produced, so changing the sort changed which rows the grid showed.
    def grouped_c14f(spec):
        return [r.get("ratingKey") for r in
                get(base, "/library/sections/%s/all?type=4&%s" % (show_section, spec))[1]
                .get("Metadata") or []]

    if not show_section:
        skip("a group's survivor is its lowest addedAt, chosen before the sort",
             "need a show")
    else:
        asc_c14f = grouped_c14f("group=index")
        desc_c14f = grouped_c14f("group=index&sort=titleSort:desc")
        added_c14f = grouped_c14f("group=index&sort=addedAt:desc")
        leaves_c14f = {r.get("ratingKey"): r.get("addedAt") for r in
                       get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                       .get("Metadata") or []}
        idx_c14f = {}
        for row_c14f in get(base, "/library/sections/%s/all?type=4" % show_section)[1] \
                .get("Metadata") or []:
            idx_c14f.setdefault(row_c14f.get("index"), []).append(row_c14f.get("ratingKey"))
        want_c14f = {min(members_c14f, key=lambda k: (leaves_c14f.get(k) or 0, int(k)))
                     for members_c14f in idx_c14f.values()}
        check("a group's survivor is its lowest addedAt, chosen before the sort",
              set(asc_c14f) == set(desc_c14f) == set(added_c14f) == want_c14f
              and asc_c14f != desc_c14f,
              "asc %s | desc %s | addedAt %s | want %s"
              % (asc_c14f, desc_c14f, added_c14f, sorted(want_c14f)))


    # Cycle 14: a type-prefixed `group=` is SCOPED like a filter and like a sort key.
    # On kids `type=4&group=show.title` is 1 and `group=title` is 4; `group=season.index`
    # is 2 and `group=index` is 3. On zen they are 505 vs 4443 and 58 vs 1144, and
    # `group=season.title` collapses 9287 episodes to 1. plexsim stripped the prefix and
    # read the field off the episode itself, so the two always agreed.
    def gtotal_c14g(spec):
        body_c14g = get(base, "/library/sections/%s/all?type=4&%s" % (show_section, spec))[1]
        return body_c14g.get("totalSize", body_c14g.get("size"))

    if not show_section:
        skip("a prefixed group= is scoped to the row the prefix names", "need a show")
    else:
        bare_c14g = gtotal_c14g("group=title")
        show_c14g = gtotal_c14g("group=show.title")
        idx_c14g = gtotal_c14g("group=index")
        season_c14g = gtotal_c14g("group=season.index")
        shows_c14g = len(get(base, "/library/sections/%s/all?type=2" % show_section)[1]
                         .get("Metadata") or [])
        check("a prefixed group= is scoped to the row the prefix names",
              show_c14g == shows_c14g and show_c14g < bare_c14g
              and season_c14g < idx_c14g,
              "title %s vs show.title %s (shows %s) | index %s vs season.index %s"
              % (bare_c14g, show_c14g, shows_c14g, idx_c14g, season_c14g))


    # Cycle 14: the actor axis is the union of each item's FIRST THREE roles. kids'
    # five movies contribute 0+2+2+2+3 and the axis totalSize is exactly 9; zen's is
    # 3413 against 59030 actor tags. Every other tag axis carries every tag. plexsim
    # listed all of them, so on a real library it would hold 155 names per movie.
    # Five roles have to be WRITTEN here: by this point the suite has replaced every
    # fixture tag list, so a check that waited for a row with more than three would
    # skip itself and quietly measure nothing.
    # The edit parameter is the FILTER field name `actor`, not the row's element name
    # `Role` -- `role[0].tag.tag` is a 400 (an unknown tag type), which is how the
    # first version of this check wrote nothing and then skipped itself.
    request(base, "PUT", "/library/metadata/%s?%s" % (movie, "&".join(
        "actor[%d].tag.tag=%s" % (i_c14h, urllib.parse.quote("C14 Actor %d" % i_c14h))
        for i_c14h in range(5))))
    roles_c14h = []
    for row_c14h in get(base, "/library/sections/%s/all" % movie_section)[1] \
            .get("Metadata") or []:
        detail_c14h = (get(base, "/library/metadata/%s" % row_c14h["ratingKey"])[1]
                       .get("Metadata") or [{}])[0]
        roles_c14h.append([t.get("tag") for t in detail_c14h.get("Role") or []])
    axis_c14h = [d.get("title") for d in
                 get(base, "/library/sections/%s/actor" % movie_section)[1]
                 .get("Directory") or []]
    want_c14h = set()
    for names_c14h in roles_c14h:
        want_c14h |= set(names_c14h[:3])
    beyond_c14h = set()
    for names_c14h in roles_c14h:
        beyond_c14h |= set(names_c14h[3:])
    beyond_c14h -= want_c14h
    dirs_c14h = [d.get("title") for d in
                 get(base, "/library/sections/%s/director" % movie_section)[1]
                 .get("Directory") or []]
    alldirs_c14h = set()
    for row_c14h in get(base, "/library/sections/%s/all" % movie_section)[1] \
            .get("Metadata") or []:
        detail_c14h = (get(base, "/library/metadata/%s" % row_c14h["ratingKey"])[1]
                       .get("Metadata") or [{}])[0]
        alldirs_c14h |= {t.get("tag") for t in detail_c14h.get("Director") or []}
    if not beyond_c14h:
        skip("the actor axis carries each item's first three roles only",
             "no fixture row has more than three roles")
    else:
        check("the actor axis carries each item's first three roles only",
              set(axis_c14h) == want_c14h and not (set(axis_c14h) & beyond_c14h)
              and alldirs_c14h <= set(dirs_c14h),
              "axis %d, first-three %d, beyond-three omitted %s, directors all present %s"
              % (len(axis_c14h), len(want_c14h), not (set(axis_c14h) & beyond_c14h),
                 alldirs_c14h <= set(dirs_c14h)))


    # Cycle 14: nine columns filter on a real PMS without appearing in any Meta, and
    # plexsim answered the whole library to each. Measured on kids and zen, which
    # agree: `viewOffset>>=0` is 0 rows on both (an UNKNOWN field answers every row,
    # which is how the two are told apart), and on zen `updatedAt>>=1780000000` is
    # 3861/3887 and `originalTitle>>=M` is 393. `parentIndex` is a 500 on a listing on
    # both. A movie row's `index` is 1 on every row, so `index=1` is the whole library
    # and `index=2` is none -- plexsim's movie rows carry no index and answered zero.
    def ftotal_c14i(spec):
        path_c14i = "/library/sections/%s/all?type=1" % movie_section
        if spec:
            path_c14i += "&" + spec
        body_c14i = get(base, path_c14i)[1]
        return body_c14i.get("totalSize", body_c14i.get("size"))

    all_c14i = ftotal_c14i("")
    # The expectation is computed from the rows, not hard-coded: by this point in the
    # suite a movie HAS a viewOffset and a rating, and a check that assumed none would
    # be measuring the fixture's history rather than the filter.
    detail_c14i = [(get(base, "/library/metadata/%s" % r_c14i["ratingKey"])[1]
                    .get("Metadata") or [{}])[0]
                   for r_c14i in get(base, "/library/sections/%s/all?type=1" % movie_section)[1]
                   .get("Metadata") or []]
    known_c14i = {}
    for field_c14i in ("viewOffset", "skipCount", "ratingCount", "absoluteIndex"):
        known_c14i["%s>>0" % field_c14i] = (
            ftotal_c14i("%s%%3E%%3E=0" % field_c14i)
            == sum(1 for d_c14i in detail_c14i if (d_c14i.get(field_c14i) or 0) > 0))
    known_c14i["lastRatedAt>>far"] = ftotal_c14i("lastRatedAt%3E%3E=9999999999") == 0
    wide_c14i = ftotal_c14i("updatedAt%3E%3E=1")
    orig_c14i = ftotal_c14i("originalTitle%3E%3E=%7B")
    pstatus_c14i, pbody_c14i, _ = request(
        base, "GET", "/library/sections/%s/query/parse?viewOffset%%3E%%3E=1" % movie_section)
    try:
        rule_c14i = json.loads(pbody_c14i).get("Query", {}).get("Rule") or {}
    except ValueError:
        rule_c14i = {}
    refused_c14i = request(base, "GET", "/library/sections/%s/all?type=1&parentIndex%%3E%%3E=0"
                           % movie_section)[0]
    idx_c14i = (ftotal_c14i("index=1"), ftotal_c14i("index=2"), ftotal_c14i("index%3E%3E=1"))
    check("the nine unadvertised filter columns constrain, and parentIndex refuses",
          all(known_c14i.values()) and all_c14i > 0
          and wide_c14i == all_c14i and orig_c14i == 0
          and rule_c14i.get("field") == "viewOffset" and rule_c14i.get("fieldTitle") == ""
          and refused_c14i == 500 and idx_c14i == (all_c14i, 0, 0),
          "all %s | per-column %s | updatedAt %s | originalTitle %s | parse %r | "
          "parentIndex %s | index %s"
          % (all_c14i, sorted(k_c14i for k_c14i, ok_c14i in known_c14i.items() if not ok_c14i)
             or "all match the rows", wide_c14i, orig_c14i,
             rule_c14i.get("fieldTitle"), refused_c14i, idx_c14i))


    # Cycle 14: thirteen sort keys a real PMS accepts and plexsim answered 500 to, and
    # one it accepted that both references refuse. Measured key by key on kids and zen,
    # which agree exactly; none of the thirteen is in `/sorts?includeAdvanced=1`, so
    # this is the unadvertised-but-real set, the same class as the filter columns above.
    def sortstatus_c14j(key_c14j):
        return request(base, "GET", "/library/sections/%s/all?type=1&sort=%s"
                       % (movie_section, key_c14j))[0]

    accepted_c14j = {k_c14j: sortstatus_c14j(k_c14j) for k_c14j in
                     ("guid", "originalTitle", "contentRatingAge", "studio", "skipCount",
                      "absoluteIndex", "mediaSize", "createdAt", "type", "viewedLeafCount",
                      "tagline", "summary", "editionTitle")}
    refused_c14j = {k_c14j: sortstatus_c14j(k_c14j) for k_c14j in
                    ("leafCount", "childCount", "slug", "key", "zzz")}
    # A real ordering, not a tolerated no-op: mediaSize puts the smallest Part first.
    sizes_c14j = []
    for row_c14j in get(base, "/library/sections/%s/all?type=1&sort=mediaSize"
                        % movie_section)[1].get("Metadata") or []:
        detail_c14j = (get(base, "/library/metadata/%s" % row_c14j["ratingKey"])[1]
                       .get("Metadata") or [{}])[0]
        sizes_c14j.append(max([p_c14j.get("size") or 0
                               for m_c14j in detail_c14j.get("Media") or []
                               for p_c14j in m_c14j.get("Part") or []] or [0]))
    check("the thirteen unadvertised sort keys order, and leafCount is refused",
          all(v_c14j == 200 for v_c14j in accepted_c14j.values())
          and all(v_c14j == 500 for v_c14j in refused_c14j.values())
          and sizes_c14j == sorted(sizes_c14j),
          "accepted %s | refused %s | mediaSize ordered %s"
          % (sorted({v_c14j for v_c14j in accepted_c14j.values()}),
             sorted({v_c14j for v_c14j in refused_c14j.values()}),
             sizes_c14j == sorted(sizes_c14j)))


    # Cycle 14: /status/sessions/history/all VALIDATES its ids. Measured on kids and
    # zen across nineteen cases, which agree exactly: a non-integer accountID,
    # librarySectionID or metadataItemID is 400 (`abc`, empty and `1.5` alike, while
    # `1`, `-1` and `01` pass); a metadataItemID naming no row is 404, `0` included; an
    # unknown librarySectionID is an empty 200, not a 404; deviceID is not validated at
    # all; and `viewedAt>=` refuses a non-integer. plexsim answered 200/size 0 to all
    # of them, so a bad id read as "no history" rather than as an error.
    def hstatus_c14k(spec_c14k):
        return request(base, "GET", "/status/sessions/history/all?" + spec_c14k)[0]

    bad_c14k = {s_c14k: hstatus_c14k(s_c14k) for s_c14k in
                ("accountID=abc", "accountID=", "accountID=1.5", "librarySectionID=abc",
                 "librarySectionID=", "metadataItemID=abc", "metadataItemID=",
                 "viewedAt%3E=abc", "viewedAt%3E=")}
    ok_c14k = {s_c14k: hstatus_c14k(s_c14k) for s_c14k in
               ("accountID=1", "accountID=-1", "accountID=01", "librarySectionID=999",
                "deviceID=abc", "deviceID=", "sort=viewedAt:desc")}
    missing_c14k = {s_c14k: hstatus_c14k(s_c14k) for s_c14k in
                    ("metadataItemID=0", "metadataItemID=99999999")}
    here_c14k = hstatus_c14k("metadataItemID=%s" % movie)
    check("history/all refuses a malformed id and 404s one it does not hold",
          set(bad_c14k.values()) == {400} and set(ok_c14k.values()) == {200}
          and set(missing_c14k.values()) == {404} and here_c14k == 200,
          "bad %s | ok %s | missing %s | present %s"
          % (sorted(set(bad_c14k.values())), sorted(set(ok_c14k.values())),
             sorted(set(missing_c14k.values())), here_c14k))


    # Cycle 14: the section `allLeaves` takes `group=` too -- the one part of the
    # listing grammar cycle 11 left out of it. kids' 6 episodes group to 3 and zen's
    # 9287 to 1144, the same as the grid's `type=4&group=index`. An ITEM's
    # `/allLeaves?group=` is ignored on both references, so only this form takes it.
    if not show_section:
        skip("the section allLeaves takes group= and an item's does not", "need a show")
    else:
        plain_c14l = len(get(base, "/library/sections/%s/allLeaves" % show_section)[1]
                         .get("Metadata") or [])
        grouped_c14l = len(get(base, "/library/sections/%s/allLeaves?group=index"
                               % show_section)[1].get("Metadata") or [])
        grid_c14l = len(get(base, "/library/sections/%s/all?type=4&group=index"
                            % show_section)[1].get("Metadata") or [])
        item_c14l = len(get(base, "/library/metadata/%s/allLeaves?group=index" % show)[1]
                        .get("Metadata") or [])
        item_plain_c14l = len(get(base, "/library/metadata/%s/allLeaves" % show)[1]
                              .get("Metadata") or [])
        check("the section allLeaves takes group= and an item's does not",
              grouped_c14l == grid_c14l and grouped_c14l < plain_c14l
              and item_c14l == item_plain_c14l,
              "section %s -> %s (grid %s) | item %s -> %s"
              % (plain_c14l, grouped_c14l, grid_c14l, item_plain_c14l, item_c14l))


    # Cycle 14: `#` is the DIGITS bucket only -- any other non-letter gets a bucket of
    # its own -- and every bucket key is percent-encoded. Reconstructed from zen's 3887
    # rows against its own axis (29 digit-led rows = the `#` bucket's 29; one
    # bracket-led row in a bucket of its own, key `%E3%80%8E`, listed after Z), with
    # the encoding corroborated on kids' other axes (`contentRating` key `Not%20Rated`).
    # plexsim folded every non-letter into `#` and emitted the key raw, so `#` was a URL
    # fragment the client's request threw away.
    orig_c14m = ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("title") or "Movie")
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("300 Spartans")))
    buckets_c14m = [(d.get("title"), d.get("key")) for d in
                    get(base, "/library/sections/%s/firstCharacter" % movie_section)[1]
                    .get("Directory") or []]
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote("『Bracket』 Movie")))
    odd_c14m = [(d.get("title"), d.get("key")) for d in
                get(base, "/library/sections/%s/firstCharacter" % movie_section)[1]
                .get("Directory") or []]
    request(base, "PUT", "/library/metadata/%s?title.value=%s"
            % (movie, urllib.parse.quote(orig_c14m)))
    check("# is the digits bucket, any other non-letter gets its own, keys encoded",
          ("#", "%23") in buckets_c14m
          and ("『", "%E3%80%8F") not in odd_c14m
          and ("『", urllib.parse.quote("『")) in odd_c14m
          and not any(t_c14m == "#" for t_c14m, _k in odd_c14m)
          and odd_c14m[-1][0] == "『",
          "digits %s | bracket %s" % (buckets_c14m, odd_c14m))


    # Cycle 14: `/userRating` is a real axis with a FIXED six-bucket scale -- kids and
    # zen answer the same rows on a movie library and a show library alike, whatever
    # ratings the library holds -- and the nine axes belonging to OTHER library types
    # answer an EMPTY secondary container with their own title2 rather than a 404.
    # plexsim 404'd all ten, so the Ratings filter entry opened nothing and a client
    # reading the axis list was told nine endpoints do not exist.
    rating_c14n = get(base, "/library/sections/%s/userRating" % movie_section)[1]
    rating_rows_c14n = [(d.get("key"), d.get("title"), "fastKey" in d)
                        for d in rating_c14n.get("Directory") or []]
    cross_c14n = {}
    for axis_c14n, title_c14n in (("mood", "By Mood"), ("style", "By Style"),
                                  ("format", "By Format"), ("subformat", "By Type"),
                                  ("source", "By Source"), ("make", "By Camera Make"),
                                  ("model", "By Camera Model"), ("place", "By Place"),
                                  ("tag", "By Tag")):
        status_c14n, body_c14n, _ = request(
            base, "GET", "/library/sections/%s/%s" % (movie_section, axis_c14n))
        parsed_c14n = json.loads(body_c14n).get("MediaContainer", {}) if body_c14n[:1] == b"{" else {}
        cross_c14n[axis_c14n] = (status_c14n, parsed_c14n.get("size"),
                                 parsed_c14n.get("title2"), parsed_c14n.get("content"))
    check("userRating is a fixed six-bucket axis and the cross-type axes are empty 200s",
          rating_rows_c14n == [("10", "5 stars", False), ("8", "4 stars", False),
                               ("6", "3 stars", False), ("4", "2 stars", False),
                               ("2", "1 star", False), ("-1", "Not rated", False)]
          and rating_c14n.get("title2") == "By Rating"
          and all(v_c14n == (200, 0, t_c14n, "secondary")
                  for (_a, t_c14n), v_c14n in zip(
                      (("mood", "By Mood"), ("style", "By Style"), ("format", "By Format"),
                       ("subformat", "By Type"), ("source", "By Source"),
                       ("make", "By Camera Make"), ("model", "By Camera Model"),
                       ("place", "By Place"), ("tag", "By Tag")), cross_c14n.values())),
          "ratings %s | cross-type %s"
          % (rating_rows_c14n, sorted({v_c14n[:2] for v_c14n in cross_c14n.values()})))


    # Cycle 14: `/library/sections/{id}/recentlyReleased` is a 404 on kids and zen
    # alike -- both library types, every `type=`, five library/type combinations -- and
    # it appears nowhere in findings.md, CLAUDE.md or either suite. plexsim answered a
    # 200 listing for an endpoint nobody ever measured. `/unwatched` beside it is real
    # and stays.
    rr_c14o = request(base, "GET", "/library/sections/%s/recentlyReleased" % movie_section)[0]
    rr_typed_c14o = request(base, "GET", "/library/sections/%s/recentlyReleased?type=1"
                            % movie_section)[0]
    unwatched_c14o = get(base, "/library/sections/%s/unwatched" % movie_section)
    check("recentlyReleased is a 404, and unwatched beside it is still a listing",
          rr_c14o == 404 and rr_typed_c14o == 404
          and unwatched_c14o[0] == 200 and unwatched_c14o[1].get("title2") == "Unplayed",
          "recentlyReleased %s/%s | unwatched %s %r"
          % (rr_c14o, rr_typed_c14o, unwatched_c14o[0], unwatched_c14o[1].get("title2")))


    # Cycle 14: `/banners` and `/thumbs` are artwork candidate lists on BOTH references
    # -- 200 with the same container shape as `/posters` -- and plexsim 404'd them.
    # `banners` is empty on kids and zen alike; `thumbs` lists the media bundle's video
    # thumbnails, which plexsim has none of. `/squareArts` stays a 404: it is 200 on
    # kids (1.43.4) and 404 on zen (1.42.2), the version plexsim reports.
    art_c14p = {}
    for leaf_c14p in ("posters", "arts", "banners", "thumbs", "squareArts"):
        status_c14p, body_c14p, _ = request(
            base, "GET", "/library/metadata/%s/%s" % (movie, leaf_c14p))
        parsed_c14p = (json.loads(body_c14p).get("MediaContainer", {})
                       if body_c14p[:1] == b"{" else {})
        art_c14p[leaf_c14p] = (status_c14p, parsed_c14p.get("size"),
                               "identifier" in parsed_c14p)
    writes_c14p = (request(base, "PUT", "/library/metadata/%s/banners" % movie)[0],
                   request(base, "POST", "/library/metadata/%s/thumbs" % movie)[0])
    check("banners and thumbs are empty candidate lists, and squareArts stays a 404",
          art_c14p["banners"] == (200, 0, True) and art_c14p["thumbs"] == (200, 0, True)
          and art_c14p["squareArts"][0] == 404 and art_c14p["posters"][0] == 200
          # Cycle 15: the POST is a **406**, not a 404 -- every artwork candidate
          # list refuses an empty body that way, measured on the copy across seven
          # of them. The PUT stays a 404.
          and art_c14p["arts"][0] == 200 and writes_c14p == (404, 406),
          "%s | writes %s" % (art_c14p, writes_c14p))


    # Cycle 14: the two families of axis added above disagree about `type=`, and both
    # disagreements are measured on kids and zen. A cross-type axis VALIDATES it --
    # `?type=abc` and an empty `?type=` are 500s, `?type=2` a 200 -- exactly as `/genre`
    # does; `userRating` IGNORES it completely, answering 200 to all three the way
    # `/resolution` does. Landing them in one branch got `userRating` right and the
    # cross-type axes wrong.
    def axstatus_c14q(axis_c14q, spec_c14q):
        return request(base, "GET", "/library/sections/%s/%s%s"
                       % (movie_section, axis_c14q, spec_c14q))[0]

    cross_c14q = {spec_c14q: [axstatus_c14q(a_c14q, spec_c14q)
                              for a_c14q in ("mood", "tag", "style", "make")]
                  for spec_c14q in ("", "?type=2", "?type=abc", "?type=")}
    rating_c14q = [axstatus_c14q("userRating", s_c14q)
                   for s_c14q in ("", "?type=2", "?type=abc", "?type=")]
    genre_c14q = [axstatus_c14q("genre", s_c14q)
                  for s_c14q in ("?type=abc", "?type=")]
    check("a cross-type axis validates type= and userRating ignores it",
          cross_c14q[""] == [200] * 4 and cross_c14q["?type=2"] == [200] * 4
          and cross_c14q["?type=abc"] == [500] * 4 and cross_c14q["?type="] == [500] * 4
          and rating_c14q == [200] * 4 and genre_c14q == [500, 500],
          "cross %s | userRating %s | genre %s"
          % ({k_c14q: sorted(set(v_c14q)) for k_c14q, v_c14q in cross_c14q.items()},
             sorted(set(rating_c14q)), genre_c14q))


    # Cycle 14: `/library/metadata/{id}/background` and `/composite` are 404s on kids
    # and zen alike -- every row type, bare and versioned -- and plexsim served image
    # bytes from both. `thumb`, `art` and `poster` beside them are real and stay 200.
    # A playlist's and a collection's `/composite` is real and is served elsewhere.
    def artstatus_c14r(key_c14r, leaf_c14r):
        return request(base, "GET", "/library/metadata/%s/%s" % (key_c14r, leaf_c14r))[0]

    gone_c14r = [artstatus_c14r(movie, leaf_c14r) for leaf_c14r in
                 ("background", "background/1", "composite", "composite/1")]
    kept_c14r = [artstatus_c14r(movie, leaf_c14r) for leaf_c14r in
                 ("thumb", "art", "poster", "thumb/1")]
    show_gone_c14r = ([artstatus_c14r(show, "composite"),
                       artstatus_c14r(show, "background")] if show else [404, 404])
    # `kept` is "the real leaves still serve", not "this row has all three": by this
    # point in the suite the fixture movie's artwork has been uploaded to, picked and
    # locked several times over, so which of the three it still carries is the suite's
    # history and not a rule. A playlist's own `/composite` is checked with the
    # playlists, not here.
    check("background and composite are 404s on an item, and the real leaves stay",
          gone_c14r == [404] * 4 and 200 in kept_c14r
          and show_gone_c14r == [404, 404],
          "item gone %s | kept %s | show %s"
          % (gone_c14r, kept_c14r, show_gone_c14r))


    # Cycle 14, the cycle's one DATA-LOSS bug: merging one SEASON into another deleted
    # the absorbed season AND every episode under it, because `catalog.remove` cascades.
    # On the copy the same request is a 200 that changes NOTHING -- both seasons still
    # there, every leaf still there, the show's leafCount unmoved -- while an EPISODE
    # merge beside it really does absorb (two Media on the survivor, a 404 on the other,
    # the leaf counts dropping). Re-measured by the driver on a freshly reset copy.
    if not show:
        skip("merging a season absorbs nothing and loses no episodes", "need a show")
    else:
        def seasons_c14s():
            return [r.get("ratingKey") for r in
                    get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                    .get("Metadata") or [] if r.get("type") == "season"]

        def leaves_c14s():
            return sorted(r.get("ratingKey") for r in
                          get(base, "/library/metadata/%s/allLeaves" % show)[1]
                          .get("Metadata") or [])

        before_s_c14s, before_l_c14s = seasons_c14s(), leaves_c14s()
        if len(before_s_c14s) < 2:
            skip("merging a season absorbs nothing and loses no episodes",
                 "need two seasons")
        else:
            status_c14s = request(base, "PUT", "/library/metadata/%s/merge?ids=%s"
                                  % (before_s_c14s[0], before_s_c14s[1]))[0]
            after_s_c14s, after_l_c14s = seasons_c14s(), leaves_c14s()
            gone_c14s = request(base, "GET", "/library/metadata/%s" % before_s_c14s[1])[0]
            check("merging a season absorbs nothing and loses no episodes",
                  status_c14s == 200 and after_s_c14s == before_s_c14s
                  and after_l_c14s == before_l_c14s and gone_c14s == 200,
                  "HTTP %s | seasons %s -> %s | leaves %d -> %d | absorbed row %s"
                  % (status_c14s, before_s_c14s, after_s_c14s,
                     len(before_l_c14s), len(after_l_c14s), gone_c14s))


    # Cycle 14: a hub container says `allowSync: true` on kids and zen alike -- every
    # hub path except `/hubs/metadata/{id}/postplay`, which says false, and
    # `/hubs/search` and `/hubs/items`, which carry none at all. plexsim defaulted to
    # false with three call sites putting it back, so eight of the ten said the opposite.
    def sync_c14t(path_c14t):
        body_c14t = get(base, path_c14t)[1]
        return body_c14t.get("allowSync", "<absent>")

    true_c14t = {p_c14t: sync_c14t(p_c14t) for p_c14t in (
        "/hubs", "/hubs/sections/%s" % movie_section, "/hubs/promoted?contentDirectoryID=%s"
        % movie_section, "/hubs/home/recentlyAdded?type=1", "/hubs/home/onDeck",
        "/hubs/continueWatching", "/hubs/metadata/%s/related" % movie,
        "/library/metadata/%s/related" % movie)}
    post_c14t = sync_c14t("/hubs/metadata/%s/postplay" % movie)
    absent_c14t = (sync_c14t("/hubs/search?query=a"),
                   sync_c14t("/hubs/items?identifier=com.plexapp.plugins.library"))
    check("a hub container says allowSync true, and only postplay says false",
          set(true_c14t.values()) == {True} and post_c14t is False
          and absent_c14t == ("<absent>", "<absent>"),
          "true-paths %s | postplay %r | absent %s"
          % (sorted({str(v_c14t) for v_c14t in true_c14t.values()}), post_c14t, absent_c14t))


    # Cycle 14: on a SEASON listing the three leaf-count filters read the SHOW's
    # counts. Measured on zen -- `type=3&viewedLeafCount>>0` answers 48 seasons, every
    # season of the 8 shows with a watched leaf, and all 48 carry 0 on their own row --
    # and on the copy. plexsim read the season's own counts, so a season grid filtered
    # by "watched" answered only the seasons that happened to hold the watched episode.
    if not show or not show_section:
        skip("a season listing's leaf-count filters read the show's counts", "need a show")
    else:
        leaves_c14u = get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []
        seasons_c14u = [r.get("ratingKey") for r in
                        get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                        .get("Metadata") or [] if r.get("type") == "season"]
        if len(seasons_c14u) < 2 or not leaves_c14u:
            skip("a season listing's leaf-count filters read the show's counts",
                 "need two seasons and a leaf")
        else:
            request(base, "GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library"
                    % leaves_c14u[0]["ratingKey"])
            watched_c14u = [r.get("ratingKey") for r in
                            get(base, "/library/sections/%s/all?type=3&viewedLeafCount%%3E%%3E=0"
                                % show_section)[1].get("Metadata") or []]
            own_c14u = [(get(base, "/library/metadata/%s" % k_c14u)[1].get("Metadata")
                         or [{}])[0].get("viewedLeafCount") or 0 for k_c14u in watched_c14u]
            request(base, "GET", "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library"
                    % leaves_c14u[0]["ratingKey"])
            check("a season listing's leaf-count filters read the show's counts",
                  sorted(watched_c14u) == sorted(seasons_c14u) and 0 in own_c14u,
                  "seasons %s | filtered %s | their own counts %s"
                  % (seasons_c14u, watched_c14u, own_c14u))


    # Cycle 14: a show's On Deck row is its MOST RECENTLY VIEWED in-progress episode,
    # not the lowest-numbered one. Re-measured on the copy in `long` mode, where an
    # episode can actually be left in progress: a beat on the first episode puts it on
    # deck, a progress write on a LATER one moves the row there, and a fresh beat on the
    # first moves it back. Cycle 9 recorded the opposite from ten-second clips.
    if not show or not show_section:
        skip("On Deck is the most recently viewed in-progress episode", "need a show")
    else:
        eps_c14v = [r for r in get(base, "/library/metadata/%s/allLeaves" % show)[1]
                    .get("Metadata") or []]
        if len(eps_c14v) < 2:
            skip("On Deck is the most recently viewed in-progress episode", "need two episodes")
        else:
            first_c14v, later_c14v = eps_c14v[0], eps_c14v[-1]
            dur_c14v = first_c14v.get("duration") or 1200000

            def beat_c14v(row_c14v, fraction_c14v):
                request(base, "GET", "/:/timeline?ratingKey=%s&key=/library/metadata/%s"
                        "&state=playing&time=%d&duration=%d&identifier=com.plexapp.plugins.library"
                        % (row_c14v["ratingKey"], row_c14v["ratingKey"],
                           int((row_c14v.get("duration") or dur_c14v) * fraction_c14v),
                           row_c14v.get("duration") or dur_c14v))

            def deck_c14v():
                return [r.get("ratingKey") for r in
                        get(base, "/library/sections/%s/onDeck" % show_section)[1]
                        .get("Metadata") or []]

            # `lastViewedAt` has one-second resolution, so the beats have to be more
            # than a second apart or they tie and the tie-break (lowest-numbered, which
            # is what cycle 9 recorded as the whole rule) answers every time.
            beat_c14v(first_c14v, 0.40)
            one_c14v = deck_c14v()
            time.sleep(1.1)
            beat_c14v(later_c14v, 0.30)
            two_c14v = deck_c14v()
            time.sleep(1.1)
            beat_c14v(first_c14v, 0.45)
            three_c14v = deck_c14v()
            check("On Deck is the most recently viewed in-progress episode",
                  one_c14v == [first_c14v["ratingKey"]]
                  and two_c14v == [later_c14v["ratingKey"]]
                  and three_c14v == [first_c14v["ratingKey"]],
                  "first %s -> later %s -> first again %s (episodes %s, %s)"
                  % (one_c14v, two_c14v, three_c14v,
                     first_c14v["ratingKey"], later_c14v["ratingKey"]))


    # Cycle 14: a show's prefs write pushes NOTHING (measured with a socket open on the
    # copy -- nine seconds, zero frames, on a listener that answered a title edit with
    # the usual pair) and changes the ROWS instead. `flattenSeasons=1` sets
    # `skipChildren` on the show and `skipParent` on every episode while removing their
    # `parentRatingKey`/`parentKey`; `episodeSort=1` puts a season's `/children` newest
    # first. plexsim pushed a 79-frame cascade and changed nothing.
    if not show:
        skip("a show's prefs write changes the rows and pushes no frames", "need a show")
    else:
        seasons_c14w = [r.get("ratingKey") for r in
                        get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                        .get("Metadata") or [] if r.get("type") == "season"]

        def leafrow_c14w():
            rows_c14w = get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []
            return (get(base, "/library/metadata/%s" % rows_c14w[0]["ratingKey"])[1]
                    .get("Metadata") or [{}])[0] if rows_c14w else {}

        def showrow_c14w():
            return (get(base, "/library/metadata/%s" % show)[1].get("Metadata") or [{}])[0]

        before_c14w = (showrow_c14w().get("skipChildren"), leafrow_c14w().get("skipParent"),
                       "parentRatingKey" in leafrow_c14w())
        request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=1" % show)
        flat_c14w = (showrow_c14w().get("skipChildren"), leafrow_c14w().get("skipParent"),
                     "parentRatingKey" in leafrow_c14w())
        seasons_still_c14w = [r.get("ratingKey") for r in
                              get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                              .get("Metadata") or [] if r.get("type") == "season"]
        request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=0" % show)
        back_c14w = (showrow_c14w().get("skipChildren"), leafrow_c14w().get("skipParent"),
                     "parentRatingKey" in leafrow_c14w())
        order_c14w = []
        if seasons_c14w:
            def kids_c14w():
                return [r.get("ratingKey") for r in
                        get(base, "/library/metadata/%s/children" % seasons_c14w[-1])[1]
                        .get("Metadata") or []]
            plain_c14w = kids_c14w()
            request(base, "PUT", "/library/metadata/%s/prefs?episodeSort=1" % show)
            sorted_c14w = kids_c14w()
            request(base, "PUT", "/library/metadata/%s/prefs?episodeSort=0" % show)
            restored_c14w = kids_c14w()
            order_c14w = [plain_c14w, sorted_c14w, restored_c14w]
    check("a show's prefs write changes the rows and pushes no frames",
          bool(show) and before_c14w == (None, None, True)
          and flat_c14w == (True, True, False) and back_c14w == (None, None, True)
          and seasons_still_c14w == seasons_c14w
          and (len(order_c14w[0]) < 2
               or (order_c14w[1] == list(reversed(order_c14w[0]))
                   and order_c14w[2] == order_c14w[0])),
          "before %s | flattened %s | back %s | seasons kept %s | order %s"
          % (before_c14w, flat_c14w, back_c14w, seasons_still_c14w == seasons_c14w,
             order_c14w))


    # Cycle 14: a poster pick re-versions `thumb`, `art` and `theme` onto ONE stamp,
    # moves `updatedAt` with them, LOCKS the picked field and CASCADES the new stamp
    # onto the children that point at the row -- a season's `parentThumb`/`art` and an
    # episode's `grandparent*`, never the season's own `thumb`. One frame goes out.
    # Measured on the copy with a socket open. plexsim re-versioned one field and left
    # every child tile pointing at the old stamp.
    if not show:
        skip("an artwork pick re-versions all three and cascades to the children",
             "need a show")
    else:
        def detail_c14x(key_c14x):
            return (get(base, "/library/metadata/%s" % key_c14x)[1].get("Metadata") or [{}])[0]

        showrow_c14x = detail_c14x(show)
        seasons_c14x = [r.get("ratingKey") for r in
                        get(base, "/library/metadata/%s/children?excludeAllLeaves=1" % show)[1]
                        .get("Metadata") or [] if r.get("type") == "season"]
        leaves_c14x = [r.get("ratingKey") for r in
                       get(base, "/library/metadata/%s/allLeaves" % show)[1].get("Metadata") or []]
        # Pick children that STILL carry the borrowed field: by this point the suite's
        # unmatch checks have nulled `parentThumb`/`grandparentThumb` on some rows, and
        # the cascade deliberately only moves a field a child already has (whether a
        # real PMS would CREATE one where there was none is unmeasured).
        seasons_c14x = [k_c14x for k_c14x in seasons_c14x
                        if detail_c14x(k_c14x).get("parentThumb")] or seasons_c14x
        leaves_c14x = [k_c14x for k_c14x in leaves_c14x
                       if detail_c14x(k_c14x).get("grandparentThumb")] or leaves_c14x
        before_season_c14x = detail_c14x(seasons_c14x[0]) if seasons_c14x else {}
        candidates_c14x = get(base, "/library/metadata/%s/posters" % show)[1].get("Metadata") or []
        pickable_c14x = next((c_c14x.get("key") for c_c14x in candidates_c14x
                              if not str(c_c14x.get("key") or "").endswith("/thumb/1")), None)
        # The cascade half is asserted only for children that still CARRY the borrowed
        # field -- by this point the suite's unmatch checks have stripped some -- but
        # the re-version and the lock are asserted either way, so the check never
        # becomes a no-op the way its first version did.
        want_season_c14x = bool(before_season_c14x.get("parentThumb"))
        want_leaf_c14x = bool(detail_c14x(leaves_c14x[0]).get("grandparentThumb")) \
            if leaves_c14x else False
        if not pickable_c14x or not seasons_c14x or not leaves_c14x:
            skip("an artwork pick re-versions all three and cascades to the children",
                 "no remote candidate to pick")
        else:
            status_c14x, body_c14x, _ = request(
                base, "PUT", "/library/metadata/%s/poster?url=%s"
                % (show, urllib.parse.quote(pickable_c14x, safe="")))
            after_c14x = detail_c14x(show)
            stamp_c14x = str(after_c14x.get("thumb") or "").rsplit("/", 1)[-1]
            season_c14x = detail_c14x(seasons_c14x[0])
            leaf_c14x = detail_c14x(leaves_c14x[0])
            locked_c14x = {f_c14x.get("name") for f_c14x in after_c14x.get("Field") or []
                           if f_c14x.get("locked")}
            moved_c14x = [name_c14x for name_c14x in ("thumb", "art", "theme")
                          if after_c14x.get(name_c14x)
                          and str(after_c14x[name_c14x]).rsplit("/", 1)[-1] != stamp_c14x]
            check("an artwork pick re-versions all three and cascades to the children",
                  status_c14x == 200 and body_c14x.decode() == after_c14x.get("thumb")
                  and not moved_c14x and "thumb" in locked_c14x
                  and (not want_season_c14x
                       or season_c14x.get("parentThumb") == after_c14x.get("thumb"))
                  and (not want_leaf_c14x
                       or leaf_c14x.get("grandparentThumb") == after_c14x.get("thumb"))
                  and season_c14x.get("thumb") == before_season_c14x.get("thumb"),
                  "stamp %s | unmoved-of-three %s | locked %s | season.parentThumb %s "
                  "(checked %s) | leaf.grandparentThumb %s | season.thumb kept %s"
                  % (stamp_c14x, moved_c14x, sorted(locked_c14x),
                     season_c14x.get("parentThumb") == after_c14x.get("thumb"),
                     want_season_c14x,
                     leaf_c14x.get("grandparentThumb") == after_c14x.get("thumb"),
                     season_c14x.get("thumb") == before_season_c14x.get("thumb")))


    # Cycle 14: the refresh cascade belongs to `showOrdering` alone. Measured on the
    # copy one setting at a time with a socket open: `showOrdering=dvd` pushes 45
    # frames, while `flattenSeasons`, `episodeSort`, `useOriginalTitle` and
    # `subtitleMode` each push zero. plexsim pushed the cascade for every prefs write.
    if not show:
        skip("only showOrdering pushes the prefs cascade", "need a show")
    else:
        def prefframes_c14y(spec_c14y):
            _, frames_c14y = notifications(
                base, lambda: request(base, "PUT", "/library/metadata/%s/prefs?%s"
                                      % (show, spec_c14y)), seconds=3)
            return [e_c14y for f_c14y in frames_c14y or []
                    for e_c14y in (f_c14y.get("NotificationContainer") or {})
                    .get("TimelineEntry") or []]

        ordering_c14y = prefframes_c14y("showOrdering=dvd")
        flatten_c14y = prefframes_c14y("flattenSeasons=1")
        sortpref_c14y = prefframes_c14y("episodeSort=1")
        title_c14y = prefframes_c14y("useOriginalTitle=1")
        request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=-1" % show)
        request(base, "PUT", "/library/metadata/%s/prefs?episodeSort=-1" % show)
        check("only showOrdering pushes the prefs cascade",
              len(ordering_c14y) > 6 and not flatten_c14y and not sortpref_c14y
              and not title_c14y,
              "showOrdering %d | flattenSeasons %d | episodeSort %d | useOriginalTitle %d"
              % (len(ordering_c14y), len(flatten_c14y), len(sortpref_c14y), len(title_c14y)))


    # Cycle 14: a collection answers `/children` on the METADATA path -- the path its
    # own row's `key` points at -- with a full secondary container carrying `key`,
    # `parentIndex` (its TAG id), `parentTitle`, `title2`, `nocache` and the
    # librarySection triple, while `/library/collections/{id}/children` beside it stays
    # the bare `{size: N}`. Measured on the copy. plexsim 404'd the metadata form, so a
    # collection page drew nothing.
    made_c14z = request(base, "POST", "/library/collections?type=1&title=C14Kids&smart=0"
                        "&sectionId=%s&uri=%s"
                        % (movie_section, urllib.parse.quote(
                            "server://%s/com.plexapp.plugins.library/library/metadata/%s"
                            % (machine, movie), safe="")))
    coll_c14z = None
    for row_c14z in get(base, "/library/sections/%s/collections" % movie_section)[1] \
            .get("Metadata") or []:
        if row_c14z.get("title") == "C14Kids":
            coll_c14z = row_c14z
    if coll_c14z is None:
        skip("a collection answers /children on the metadata path", "collection not created")
    else:
        meta_c14z = get(base, "/library/metadata/%s/children" % coll_c14z["ratingKey"])
        bare_c14z = get(base, "/library/collections/%s/children" % coll_c14z["ratingKey"])
        body_c14z = meta_c14z[1] or {}
        detail_c14z = (get(base, "/library/metadata/%s" % coll_c14z["ratingKey"])[1]
                       .get("Metadata") or [{}])[0]
        request(base, "DELETE", "/library/collections/%s" % coll_c14z["ratingKey"])
        check("a collection answers /children on the metadata path",
              meta_c14z[0] == 200 and body_c14z.get("size") == 1
              and body_c14z.get("key") == str(coll_c14z["ratingKey"])
              and body_c14z.get("parentTitle") == "C14Kids"
              and body_c14z.get("title2") == "C14Kids"
              and body_c14z.get("nocache") is True
              and body_c14z.get("parentIndex") == detail_c14z.get("index")
              and bare_c14z[0] == 200
              and sorted(k_c14z for k_c14z in bare_c14z[1]
                         if not isinstance(bare_c14z[1][k_c14z], (list, dict))) == ["size"],
              "metadata %s size=%s key=%r parentIndex=%r nocache=%r | collections form %s"
              % (meta_c14z[0], body_c14z.get("size"), body_c14z.get("key"),
                 body_c14z.get("parentIndex"), body_c14z.get("nocache"),
                 sorted(k_c14z for k_c14z in bare_c14z[1]
                        if not isinstance(bare_c14z[1][k_c14z], (list, dict)))))


    # Cycle 14: a `type=18` listing is a real query -- `title=` filters it, `sort=`
    # orders it, the default order is titleSort -- and a collection row's `index` is its
    # own TAG id, the number its members carry in their `Collection` tag. Measured on
    # the copy with two collections. plexsim answered the whole collection list to every
    # query and sent `index` 1 for all of them.
    for name_c15a, member_c15a in (("C15 Zulu", movie), ("C15 Alpha", movie)):
        request(base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
                % (urllib.parse.quote(name_c15a), movie_section, urllib.parse.quote(
                    "server://%s/com.plexapp.plugins.library/library/metadata/%s"
                    % (machine, member_c15a), safe="")))

    def colls_c15a(spec_c15a):
        return [(r_c15a.get("title"), r_c15a.get("index")) for r_c15a in
                get(base, "/library/sections/%s/all?type=18%s" % (movie_section, spec_c15a))[1]
                .get("Metadata") or []]

    mine_c15a = [c_c15a for c_c15a in colls_c15a("") if c_c15a[0].startswith("C15 ")]
    filtered_c15a = [c_c15a for c_c15a in colls_c15a("&title=alpha")
                     if c_c15a[0].startswith("C15 ")]
    member_tags_c15a = {t_c15a.get("tag"): t_c15a.get("id") for t_c15a in
                        (get(base, "/library/metadata/%s" % movie)[1].get("Metadata")
                         or [{}])[0].get("Collection") or []}
    for row_c15a in get(base, "/library/sections/%s/collections" % movie_section)[1] \
            .get("Metadata") or []:
        if str(row_c15a.get("title") or "").startswith("C15 "):
            request(base, "DELETE", "/library/collections/%s" % row_c15a["ratingKey"])
    check("a type=18 listing filters and sorts, and a collection's index is its tag id",
          [c_c15a[0] for c_c15a in mine_c15a] == ["C15 Alpha", "C15 Zulu"]
          and [c_c15a[0] for c_c15a in filtered_c15a] == ["C15 Alpha"]
          and len({c_c15a[1] for c_c15a in mine_c15a}) == 2
          and all(member_tags_c15a.get(t_c15a) == i_c15a for t_c15a, i_c15a in mine_c15a),
          "listing %s | filtered %s | member tags %s"
          % (mine_c15a, filtered_c15a,
             {k_c15a: v_c15a for k_c15a, v_c15a in member_tags_c15a.items()
              if str(k_c15a).startswith("C15 ")}))


    # Cycle 14: `/library/all?type=18` lists every collection the server holds, across
    # libraries, in title order. Measured on the copy (two collections, `Aardvark`
    # before `C14 Trio`). plexsim answered `size 0` because its collections are not
    # rows in the item catalog and that handler only looked there.
    for name_c15b in ("C15b Zulu", "C15b Alpha"):
        request(base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
                % (urllib.parse.quote(name_c15b), movie_section, urllib.parse.quote(
                    "server://%s/com.plexapp.plugins.library/library/metadata/%s"
                    % (machine, movie), safe="")))
    all18_c15b = [r_c15b.get("title") for r_c15b in
                  get(base, "/library/all?type=18")[1].get("Metadata") or []]
    section18_c15b = [r_c15b.get("title") for r_c15b in
                      get(base, "/library/sections/%s/all?type=18" % movie_section)[1]
                      .get("Metadata") or []]
    mine_c15b = [t_c15b for t_c15b in all18_c15b if str(t_c15b).startswith("C15b ")]
    for row_c15b in get(base, "/library/sections/%s/collections" % movie_section)[1] \
            .get("Metadata") or []:
        if str(row_c15b.get("title") or "").startswith("C15b "):
            request(base, "DELETE", "/library/collections/%s" % row_c15b["ratingKey"])
    check("/library/all?type=18 lists the collections in title order",
          mine_c15b == ["C15b Alpha", "C15b Zulu"]
          and set(section18_c15b) <= set(all18_c15b),
          "library/all %s | section %s" % (all18_c15b, section18_c15b))

    # Cycle 15: `titleSort` trims the LEFT only. Measured on the copy -- zen holds no
    # row with trailing or doubled whitespace, so cycle 14 could witness only the
    # leading half and read `.strip()` into the gap. A trailing space SURVIVES into
    # the sort title, which is why a title written with one carries no `titleSort` at
    # all: the derived string equals the title, and a real PMS omits it then.
    trim_before_t15 = get(base, "/library/metadata/%s" % movie)[1].get("Metadata", [{}])[0].get("title")
    request(base, "PUT", "/library/metadata/%s?title=%s" % (movie, quote("  Zeta Both  ")))
    row_t15 = get(base, "/library/metadata/%s" % movie)[1].get("Metadata", [{}])[0]
    check("titleSort keeps trailing whitespace and drops leading",
          row_t15.get("titleSort") == "Zeta Both  ",
          "title=%r titleSort=%r" % (row_t15.get("title"), row_t15.get("titleSort")))
    request(base, "PUT", "/library/metadata/%s?title=%s" % (movie, quote("Zeta Trail ")))
    row_t15 = get(base, "/library/metadata/%s" % movie)[1].get("Metadata", [{}])[0]
    check("a trailing-space title derives a titleSort equal to it, so none is sent",
          row_t15.get("titleSort") is None and row_t15.get("title") == "Zeta Trail ",
          "title=%r titleSort=%r" % (row_t15.get("title"), row_t15.get("titleSort")))
    request(base, "PUT", "/library/metadata/%s?title=%s" % (movie, quote("The Zeta Art ")))
    row_t15 = get(base, "/library/metadata/%s" % movie)[1].get("Metadata", [{}])[0]
    check("the article still comes off a title with trailing whitespace",
          row_t15.get("titleSort") == "Zeta Art ", repr(row_t15.get("titleSort")))
    request(base, "PUT", "/library/metadata/%s?title=%s" % (movie, quote(trim_before_t15)))

    # Cycle 15: a show whose setting has never been written -- or has just been
    # CLEARED, which is the same state -- reports `episodeSort` as `-1`, the same as
    # its `default`. Re-read live on BOTH references while implementing the
    # inheritance rule: kids (1.43.4) show 6 and zen (1.42.2) show 5189 both answer
    # `-1`. `item_prefs.json` carried `0`, captured on 2026-09-12 from a show that
    # had been written, and plexsim served that stale value for every show it holds.
    # It matters because `-1` is the value that INHERITS the library's setting, so a
    # captured `0` reads as a per-show refusal of it.
    #
    # Clear the settings first: by this point in the suite the show's prefs have been
    # written several times, and "unwritten" is a state the suite has to restore.
    request(base, "PUT", "/library/metadata/%s/prefs?episodeSort=&flattenSeasons=&subtitleMode="
            % show)
    prefs_e15 = {s_e15.get("id"): s_e15 for s_e15 in
                 ((get(base, "/library/metadata/%s?includePreferences=1" % show)[1]
                   .get("Metadata", [{}])[0].get("Preferences") or {}).get("Setting") or [])}
    check("a cleared show setting reads its own default, and episodeSort's is -1",
          prefs_e15.get("episodeSort", {}).get("value") == "-1"
          and prefs_e15.get("episodeSort", {}).get("default") == "-1",
          "episodeSort=%r" % (prefs_e15.get("episodeSort"),))
    check("every tri-state show setting reads its own default once cleared",
          all(prefs_e15[n_e15]["value"] == prefs_e15[n_e15]["default"]
              for n_e15 in ("episodeSort", "flattenSeasons", "subtitleMode")
              if n_e15 in prefs_e15),
          str({n_e15: (v_e15.get("value"), v_e15.get("default"))
               for n_e15, v_e15 in prefs_e15.items()}))

    # Cycle 15, measured on the copy: the LIBRARY-level `flattenSeasons` and
    # `episodeSort` produce exactly the row effects the ITEM path produces, and the
    # show's own setting stays `-1` throughout -- so `-1` on an item means INHERIT,
    # not "off". All four combinations were measured; the item overrides the library
    # in BOTH directions. plexsim read the item's `_prefs` alone, so a library-wide
    # write (where the Edit Library dialog actually puts these two) moved the setting
    # list and left every row untouched -- except `skipChildren` on the show, which it
    # set without the episode half, leaving the client every `parentRatingKey` it
    # needed to draw the season level anyway.
    #
    # Count the seasons LIVE and take the fullest: by this point in the suite rows
    # have been deleted and moved, and a season with one episode reverses to itself.
    seasons_s15 = [r_s15["ratingKey"] for r_s15 in
                   get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []]
    kids_s15 = {k_s15: get(base, "/library/metadata/%s/children" % k_s15)[1].get("Metadata") or []
                for k_s15 in seasons_s15}
    season_s15 = max(kids_s15, key=lambda k_s15: len(kids_s15[k_s15]))
    order_before_s15 = [r_s15.get("index") for r_s15 in kids_s15[season_s15]]
    leaf_s15 = kids_s15[season_s15][0]["ratingKey"]
    request(base, "PUT", "/library/sections/%s/prefs?flattenSeasons=1&episodeSort=1" % show_section)
    show_row_s15 = get(base, "/library/metadata/%s" % show)[1].get("Metadata", [{}])[0]
    leaf_row_s15 = get(base, "/library/metadata/%s" % leaf_s15)[1].get("Metadata", [{}])[0]
    order_after_s15 = [r_s15.get("index") for r_s15 in
                       get(base, "/library/metadata/%s/children" % season_s15)[1]
                       .get("Metadata") or []]
    check("a library-wide flattenSeasons reaches the EPISODES, not just the show row",
          show_row_s15.get("skipChildren") is True and leaf_row_s15.get("skipParent") is True
          and leaf_row_s15.get("parentRatingKey") is None
          and leaf_row_s15.get("parentKey") is None
          and leaf_row_s15.get("parentIndex") is not None,
          "show skipChildren=%r leaf skipParent=%r parentRatingKey=%r parentIndex=%r"
          % (show_row_s15.get("skipChildren"), leaf_row_s15.get("skipParent"),
             leaf_row_s15.get("parentRatingKey"), leaf_row_s15.get("parentIndex")))
    check("a library-wide episodeSort=1 puts a season's children newest first",
          len(order_before_s15) > 1 and order_after_s15 == list(reversed(order_before_s15)),
          "season %s before %s after %s" % (season_s15, order_before_s15, order_after_s15))
    # The item overrides the library in both directions: -1 inherits, 0 refuses.
    request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=0&episodeSort=0" % show)
    show_row_s15 = get(base, "/library/metadata/%s" % show)[1].get("Metadata", [{}])[0]
    leaf_row_s15 = get(base, "/library/metadata/%s" % leaf_s15)[1].get("Metadata", [{}])[0]
    order_over_s15 = [r_s15.get("index") for r_s15 in
                      get(base, "/library/metadata/%s/children" % season_s15)[1]
                      .get("Metadata") or []]
    check("a show's own 0 overrides the library's 1 for both settings",
          show_row_s15.get("skipChildren") is None and leaf_row_s15.get("skipParent") is None
          and leaf_row_s15.get("parentRatingKey") is not None
          and order_over_s15 == order_before_s15,
          "skipChildren=%r skipParent=%r order %s"
          % (show_row_s15.get("skipChildren"), leaf_row_s15.get("skipParent"), order_over_s15))
    request(base, "PUT", "/library/metadata/%s/prefs?flattenSeasons=-1&episodeSort=-1" % show)
    request(base, "PUT", "/library/sections/%s/prefs?flattenSeasons=0&episodeSort=-1" % show_section)

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: the View
    # Play History page lists NOTHING without a `type=` -- `size 0`, `totalSize 0`,
    # on both -- and the `type` it matches is the row's `viewType`, NOT its metadata
    # type. kids' history is five MOVIES and zen's is 73 EPISODES and BOTH come back
    # under `type=1`, while `type=2`, `3`, `4`, `5` and `0` answer nothing on either.
    # `type=abc` and an empty `type=` are 400s, like the ids cycle 14 measured, and
    # every listing row carries `viewType: 1`.
    #
    # This is the answer to the `size 0` that cycle 11 recorded as unexplained and
    # three cycles carried in the revalidation register: the plain call really does
    # list nothing, on a server with 73 rows in it. plexsim ignored `type=`
    # altogether, so it answered its whole history to a request a real PMS answers
    # nothing to, and answered every row to `type=2` as well.
    watched_h15 = get(base, "/library/sections/%s/all?type=1" % movie_section)[1] \
        .get("Metadata") or []
    row_h15 = watched_h15[0]
    dur_h15 = int(row_h15.get("duration") or 0) or 1400000
    for frac_h15, state_h15 in ((0.30, "playing"), (0.95, "playing"), (0.96, "stopped")):
        request(base, "GET", "/:/timeline?ratingKey=%s&key=/library/metadata/%s&state=%s"
                             "&time=%d&duration=%d&identifier=com.plexapp.plugins.library"
                % (row_h15["ratingKey"], row_h15["ratingKey"], state_h15,
                   int(dur_h15 * frac_h15), dur_h15))
    none_h15 = get(base, "/status/sessions/history/all")[1]
    one_h15 = get(base, "/status/sessions/history/all?type=1")[1]
    four_h15 = get(base, "/status/sessions/history/all?type=4")[1]
    list_h15 = get(base, "/status/sessions/history/all?type=1,4")[1]
    check("the history page lists nothing without a type=",
          (none_h15.get("size") or 0) == 0 and (one_h15.get("size") or 0) > 0,
          "no type: size=%s | type=1: size=%s" % (none_h15.get("size"), one_h15.get("size")))
    check("history type= matches the row's viewType, not its metadata type",
          all(r_h15.get("viewType") == 1 for r_h15 in one_h15.get("Metadata") or [])
          and (four_h15.get("size") or 0) == 0
          and (list_h15.get("size") or 0) == (one_h15.get("size") or 0),
          "type=1 viewTypes=%s | type=4 size=%s | type=1,4 size=%s"
          % (sorted({r_h15.get("viewType") for r_h15 in one_h15.get("Metadata") or []}),
             four_h15.get("size"), list_h15.get("size")))
    check("a history type= that is not a number is a 400",
          request(base, "GET", "/status/sessions/history/all?type=abc")[0] == 400
          and request(base, "GET", "/status/sessions/history/all?type=")[0] == 400
          and request(base, "GET", "/status/sessions/history/all?type=1")[0] == 200,
          "abc %s empty %s one %s"
          % (request(base, "GET", "/status/sessions/history/all?type=abc")[0],
             request(base, "GET", "/status/sessions/history/all?type=")[0],
             request(base, "GET", "/status/sessions/history/all?type=1")[0]))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: the in-library
    # search box answers the LIGHT grid row, exactly as every other listing does -- no
    # `Guid`, no `Rating`, no tag ids, no `Stream` inside a Part. Its row is
    # byte-comparable with the grid's row for the same item on both servers. plexsim
    # answered the DETAIL row here and nowhere else, so the one listing a user reaches
    # by typing carried uncapped tag lists, every Stream element and every external
    # guid -- and a client that tests `Rating`/`Guid` presence to decide "this row is
    # fully loaded" skips its detail fetch and then renders a page missing whatever
    # only a detail row carries.
    srow_c15s = (get(base, "/library/sections/%s/search?type=1&query=%s"
                     % (movie_section, quote("Tears")))[1].get("Metadata") or [{}])[0]
    grow_c15s = (get(base, "/library/sections/%s/all?type=1" % movie_section)[1]
                 .get("Metadata") or [{}])[0]
    streams_c15s = any("Stream" in p_c15s for m_c15s in srow_c15s.get("Media") or []
                       for p_c15s in m_c15s.get("Part") or [])
    tagids_c15s = any("id" in t_c15s for t_c15s in
                      (srow_c15s.get("Genre") or []) + (srow_c15s.get("Director") or []))
    check("the section search box answers the light grid row, not the detail row",
          "Guid" not in srow_c15s and "Rating" not in srow_c15s
          and not tagids_c15s and not streams_c15s
          and ("Guid" in grow_c15s) == ("Guid" in srow_c15s),
          "search keys %s" % sorted(srow_c15s))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2): `includeChildren=1` inlines
    # a `Children` block on a SHOW and on nothing else. Neither reference puts one on a
    # season, and neither is silent for want of children -- kids' season 7 holds three
    # episodes and zen's season 17201 holds twenty-two. plexsim inlined a season's
    # episodes too, so a season detail read carried a list the client had not asked for
    # and would fetch again from /children.
    show_row_c15c = (get(base, "/library/metadata/%s?includeChildren=1" % show)[1]
                     .get("Metadata") or [{}])[0]
    seasons_c15c = get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
    season_row_c15c = (get(base, "/library/metadata/%s?includeChildren=1"
                           % seasons_c15c[0]["ratingKey"])[1].get("Metadata") or [{}])[0]
    movie_row_c15c = (get(base, "/library/metadata/%s?includeChildren=1" % movie)[1]
                      .get("Metadata") or [{}])[0]
    check("includeChildren inlines a Children block on a show and not on a season",
          isinstance(show_row_c15c.get("Children"), dict)
          and (show_row_c15c["Children"].get("size") or 0) > 0
          and "Children" not in season_row_c15c and "Children" not in movie_row_c15c,
          "show %s | season %s | movie %s"
          % (show_row_c15c.get("Children", {}).get("size"),
             season_row_c15c.get("Children"), movie_row_c15c.get("Children")))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: four more
    # columns filter a listing although no Meta advertises them.
    #   parent=<ratingKey>  the row's immediate parentRatingKey. kids' type=4 goes
    #                       6 -> 3 for a season and 6 -> 0 for the show above it;
    #                       zen's goes 9287 -> 22 and -> 0. It reaches /library/all too.
    #   rating=, audienceRating=  the two agent ratings. On zen `audienceRating>>=9` is
    #                       107 of 3887 and `rating>>=9` is 142; `<<=1` is 0 for both.
    #   file=               a case-insensitive SUBSTRING of the Part's path -- `unn`
    #                       finds Big Buck Bunny, `.mkv` is 3811 of zen's 3887, an empty
    #                       value is every row and `zzz` is none.
    # plexsim ignored all four, which on a filter is worse than refusing: it answered
    # every row to two mutually exclusive queries.
    seasons_c15p = get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []
    one_c15p = seasons_c15p[0]["ratingKey"]
    kids_c15p = len(get(base, "/library/metadata/%s/children" % one_c15p)[1]
                    .get("Metadata") or [])
    all4_c15p = get(base, "/library/sections/%s/all?type=4" % show_section)[1]
    byparent_c15p = get(base, "/library/sections/%s/all?type=4&parent=%s"
                        % (show_section, one_c15p))[1]
    byshow_c15p = get(base, "/library/sections/%s/all?type=4&parent=%s"
                      % (show_section, show))[1]
    liball_c15p = get(base, "/library/all?type=4&parent=%s" % one_c15p)[1]
    check("parent= filters a listing to one parent's children",
          (byparent_c15p.get("size") or 0) == kids_c15p
          and (all4_c15p.get("size") or 0) > kids_c15p
          and (byshow_c15p.get("size") or 0) == 0
          and (liball_c15p.get("size") or 0) == kids_c15p,
          "all %s | parent=season %s (want %s) | parent=show %s | library/all %s"
          % (all4_c15p.get("size"), byparent_c15p.get("size"), kids_c15p,
             byshow_c15p.get("size"), liball_c15p.get("size")))
    hi_c15p = get(base, "/library/sections/%s/all?type=1&audienceRating%%3E%%3E=9"
                  % movie_section)[1]
    lo_c15p = get(base, "/library/sections/%s/all?type=1&audienceRating%%3C%%3C=1"
                  % movie_section)[1]
    rhi_c15p = get(base, "/library/sections/%s/all?type=1&rating%%3E%%3E=9" % movie_section)[1]
    check("rating= and audienceRating= are real columns, not a tolerated no-op",
          (hi_c15p.get("size") or 0) + (lo_c15p.get("size") or 0)
          <= (get(base, "/library/sections/%s/all?type=1" % movie_section)[1].get("size") or 0)
          and not ((hi_c15p.get("size") or 0) and (lo_c15p.get("size") or 0)),
          "audienceRating>>9 %s <<1 %s rating>>9 %s"
          % (hi_c15p.get("size"), lo_c15p.get("size"), rhi_c15p.get("size")))
    part_c15p = (((get(base, "/library/metadata/%s" % movie)[1].get("Metadata")
                   or [{}])[0].get("Media") or [{}])[0].get("Part") or [{}])[0].get("file") or ""
    frag_c15p = part_c15p.rsplit("/", 1)[-1][2:8]
    hit_c15p = get(base, "/library/sections/%s/all?type=1&file=%s"
                   % (movie_section, quote(frag_c15p)))[1]
    miss_c15p = get(base, "/library/sections/%s/all?type=1&file=zzzzzz" % movie_section)[1]
    empty_c15p = get(base, "/library/sections/%s/all?type=1&file=" % movie_section)[1]
    every_c15p = get(base, "/library/sections/%s/all?type=1" % movie_section)[1]
    check("file= is a case-insensitive substring of the Part path",
          (hit_c15p.get("size") or 0) >= 1 and (miss_c15p.get("size") or 0) == 0
          and (empty_c15p.get("size") or 0) == (every_c15p.get("size") or 0),
          "fragment %r -> %s | zzzzzz %s | empty %s of %s"
          % (frag_c15p, hit_c15p.get("size"), miss_c15p.get("size"),
             empty_c15p.get("size"), every_c15p.get("size")))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: /query/parse
    # echoes eight fields as a Rule with an EMPTY fieldTitle that a LISTING does not
    # filter on -- the parse surface is wider than the filter surface, and the two were
    # the same map in plexsim. `guid` is the sharpest of them: plexsim's listing has
    # filtered on `guid=` since cycle 8 while its own /query/parse called the field
    # unknown, so the server contradicted itself. `parentIndex` is a 500 on a listing
    # (cycle 14) and still a Rule here.
    # An Error is what the smart-list builder draws as "that field does not exist".
    def parsed_c15q(spec_c15q):
        # The root here is `Query`, not a MediaContainer, so the suite's own `get()`
        # unwrapper cannot be used -- read the raw body.
        body_c15q = request(base, "GET", "/library/sections/%s/query/parse?type=1&%s"
                            % (movie_section, spec_c15q))[1]
        doc_c15q = json.loads(body_c15q).get("Query") or {}
        if doc_c15q.get("Rule"):
            rules_c15q = doc_c15q["Rule"]
            rules_c15q = rules_c15q if isinstance(rules_c15q, list) else [rules_c15q]
            return "Rule", rules_c15q[0].get("fieldTitle")
        return ("Error" if doc_c15q.get("Error") else "?%s" % sorted(doc_c15q)), None


    got_c15q = {f_c15q: parsed_c15q("%s=5" % f_c15q) for f_c15q in
                ("parent", "parentIndex", "audienceRating", "rating", "guid", "slug",
                 "file", "extraType")}
    check("/query/parse echoes eight more fields as Rules with an empty fieldTitle",
          all(v_c15q == ("Rule", "") for v_c15q in got_c15q.values())
          and parsed_c15q("wibble=5")[0] == "Error"
          and parsed_c15q("title=x") == ("Rule", "Title"),
          "%s | wibble %s | title %s"
          % (got_c15q, parsed_c15q("wibble=5")[0], parsed_c15q("title=x")))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), whose answers are
    # byte-identical: `/playlists?includeMeta=1` carries a `Meta` descriptor with one
    # `Type` row and SIX sorts, and `/hubs/home/recentlyAdded?type=N&includeMeta=1`
    # carries one `Type` row whose `active` flips with the type -- `movie` true at
    # type=1, `mixed` false at type=2, and no `Meta` block at all for any other type.
    # `Meta.Type[].Sort` is what the playlist page reads straight into its sort menu, so
    # without it the menu is empty. plexsim sent no Meta on either path.
    #
    # The sixth sort is `mediaCount` "Item Count" -- NOT the `updatedAt` "Date Modified"
    # that `/playlists/sorts` ends with. The two lists are different and were measured
    # separately.
    meta_c15m = get(base, "/playlists?includeMeta=1")[1].get("Meta") or {}
    types_c15m = meta_c15m.get("Type") or []
    sorts_c15m = [(s_c15m.get("key"), s_c15m.get("title"))
                  for s_c15m in (types_c15m[0].get("Sort") if types_c15m else []) or []]
    plain_c15m = get(base, "/playlists")[1]
    check("/playlists?includeMeta=1 carries the Meta descriptor and its six sorts",
          len(types_c15m) == 1 and types_c15m[0].get("type") == "playlist"
          and types_c15m[0].get("key") == "/playlists"
          and types_c15m[0].get("title") == "All" and types_c15m[0].get("active") is True
          and sorts_c15m == [("titleSort", "Name"), ("viewCount", "Plays"),
                             ("lastViewedAt", "Last Played"), ("duration", "Duration"),
                             ("addedAt", "Date Added"), ("mediaCount", "Item Count")]
          and "Meta" not in plain_c15m,
          "types %s sorts %s bare-has-Meta %s"
          % (types_c15m, sorts_c15m, "Meta" in plain_c15m))
    home_c15m = {t_c15m: (get(base, "/hubs/home/recentlyAdded?type=%s&includeMeta=1" % t_c15m)[1]
                          .get("Meta") or {}).get("Type")
                 for t_c15m in ("1", "2", "4", "8")}
    check("/hubs/home/recentlyAdded carries a one-row Meta whose active flips by type",
          home_c15m["1"] == [{"key": "/hubs/home/recentlyAdded?type=1", "type": "movie",
                              "title": "Recently Added", "active": True}]
          and home_c15m["2"] == [{"key": "/hubs/home/recentlyAdded?type=2", "type": "mixed",
                                  "title": "Recently Added", "active": False}]
          and home_c15m["4"] is None and home_c15m["8"] is None,
          str(home_c15m))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: on
    # `/hubs/search` -- and on no other hub path -- `contentDirectoryID` SCOPES the
    # search to one library and VALIDATES its value. zen's `?query=the` answers show
    # and movie hubs; with `contentDirectoryID=1` only the movie library's, with `=2`
    # only the show library's. `abc`, `99`, `1.5` and `-1` -- anything that does not
    # name a section -- are 404s on both, while an EMPTY value is a 200 that scopes
    # nothing. plexsim ignored the parameter and 200'd every value, so search-within-
    # this-library returned hits from every library and a bad id read as "no results
    # here" rather than as an error.
    sm_c15h = {cd_c15h: get(base, "/hubs/search?query=%s%s" % (quote("Tears"), cd_c15h))
               for cd_c15h in ("", "&contentDirectoryID=%s" % movie_section,
                               "&contentDirectoryID=%s" % show_section,
                               "&contentDirectoryID=abc", "&contentDirectoryID=99",
                               "&contentDirectoryID=")}


    def hits_c15h(key_c15h):
        return sum(h_c15h.get("size") or 0
                   for h_c15h in (sm_c15h[key_c15h][1].get("Hub") or []))


    check("contentDirectoryID scopes /hubs/search and 404s an id that names no section",
          sm_c15h[""][0] == 200 and sm_c15h["&contentDirectoryID="][0] == 200
          and sm_c15h["&contentDirectoryID=abc"][0] == 404
          and sm_c15h["&contentDirectoryID=99"][0] == 404
          and hits_c15h("&contentDirectoryID=%s" % movie_section) == hits_c15h("")
          and hits_c15h("&contentDirectoryID=%s" % show_section) == 0
          and hits_c15h("&contentDirectoryID=") == hits_c15h(""),
          "statuses %s | hits none=%s movie=%s show=%s empty=%s"
          % ({k_c15h: v_c15h[0] for k_c15h, v_c15h in sm_c15h.items()}, hits_c15h(""),
             hits_c15h("&contentDirectoryID=%s" % movie_section),
             hits_c15h("&contentDirectoryID=%s" % show_section),
             hits_c15h("&contentDirectoryID=")))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree on all of it:
    # `excludeElements=` is a property of the RESPONSE, exactly as paging is -- every
    # container that carries rows drops the named child elements, and dropping `Media`
    # substitutes `mediaCount` and `mediaCountOptimized`. Cycle 11 wired it into the
    # section grid, the section allLeaves, /library/people/{id}/media and /hubs/items;
    # eight more surfaces were never wired, and the Home screen is the one that costs
    # most -- it is how a client asks for a CHEAP shelf, and plexsim sent a full
    # Media/Part/Stream tree for every shelf row on every Home render.
    paths_c15e = ["/library/all?type=1", "/library/recentlyAdded",
                  "/library/sections/%s/recentlyAdded" % movie_section,
                  "/library/sections/%s/newest" % movie_section,
                  "/library/sections/%s/search?type=1&query=%s" % (movie_section, quote("Tears")),
                  "/hubs", "/hubs/promoted?contentDirectoryID=%s" % movie_section,
                  "/hubs/home/recentlyAdded?type=1",
                  "/library/metadata/%s/grandchildren" % show]
    bad_c15e = []
    for p_c15e in paths_c15e:
        sep_c15e = "&" if "?" in p_c15e else "?"
        plain_c15e = get(base, p_c15e)[1]
        excl_c15e = get(base, p_c15e + sep_c15e + "excludeElements=Media")[1]

        def rows_c15e(doc_c15e):
            out_c15e = list(doc_c15e.get("Metadata") or [])
            for hub_c15e in doc_c15e.get("Hub") or []:
                out_c15e += hub_c15e.get("Metadata") or []
            return out_c15e

        before_c15e, after_c15e = rows_c15e(plain_c15e), rows_c15e(excl_c15e)
        if not before_c15e or not any("Media" in r_c15e for r_c15e in before_c15e):
            continue
        if any("Media" in r_c15e for r_c15e in after_c15e) \
                or not any("mediaCount" in r_c15e for r_c15e in after_c15e):
            bad_c15e.append(p_c15e)
    check("excludeElements=Media reaches every response that carries rows",
          not bad_c15e, "still carrying Media: %s" % bad_c15e)

    # Cycle 15, measured on plexscratch: `DELETE /library/metadata/{id}/{poster,art,thumb}`
    # is a real endpoint. It answers 200 with an EMPTY body, removes the field from the
    # row, re-stamps whatever artwork is left onto a new version, moves `updatedAt`, pushes
    # ONE `state 5` frame with no `metadataState` -- the artwork-pick shape -- and leaves
    # the candidate list with nothing `selected`. Afterwards the bare GET of the deleted
    # path is a 404.
    #
    # plexsim answered the GET -- 56 KB of JPEG -- to the DELETE and changed nothing. That
    # is Remove Poster doing nothing, and because Change Poster assigns the response BODY
    # onto the row (the recorded singular-path rule), a client that clears the artwork sets
    # the tile's img src to a blob of JPEG.
    before_c15d = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    st_c15d, body_c15d, _ = request(base, "DELETE", "/library/metadata/%s/poster" % movie)
    after_c15d = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    check("DELETE on an artwork path clears it and answers an empty 200",
          is_empty_200(st_c15d, body_c15d)
          and before_c15d.get("thumb") and after_c15d.get("thumb") is None
          and after_c15d.get("art") != before_c15d.get("art")
          and get(base, "/library/metadata/%s/thumb" % movie)[0] == 404,
          "HTTP %s len=%d thumb %r -> %r | art %r -> %r | bare GET %s"
          % (st_c15d, len(body_c15d), before_c15d.get("thumb"), after_c15d.get("thumb"),
             before_c15d.get("art"), after_c15d.get("art"),
             get(base, "/library/metadata/%s/thumb" % movie)[0]))
    sel_c15d = [r_c15d.get("selected") for r_c15d in
                get(base, "/library/metadata/%s/posters" % movie)[1].get("Metadata") or []]
    check("after an artwork delete nothing in the candidate list is selected",
          not any(sel_c15d), str(sel_c15d))
    st_c15d, body_c15d, _ = request(base, "DELETE", "/library/metadata/%s/art" % movie)
    after_c15d = (get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
    check("deleting the background clears art too",
          is_empty_200(st_c15d, body_c15d) and after_c15d.get("art") is None,
          "HTTP %s art=%r" % (st_c15d, after_c15d.get("art")))

    # Cycle 15, measured on plexscratch across nine paths -- and this CORRECTS the hunt
    # that reported it. `PATCH` and `OPTIONS` are not blanket 404s: they are routed
    # exactly like every other verb, so `/identity`, `/hubs` and `/playlists` answer
    # 200 to both (the recorded "these answer any verb" rule) and every other path
    # 404s them. What a real PMS does not recognise as a method AT ALL -- `FROB`,
    # `TRACE`, a wrong-case `Get` -- is a **500** with its own four-line html body, on
    # every path.
    #
    # plexsim answered Python's BaseHTTPRequestHandler 501 page, which also leaks
    # `Server: PlexSim/0.1 Python/3.10.20`; HANDOVER records that a real PMS never
    # answers 405, and it never answers 501 either. A bare OPTIONS with no
    # Access-Control-Request-* headers was a 200 where a real PMS routes it; a genuine
    # preflight is unchanged except that its response carries `Content-Type: text/plain`.
    routed_c15v = {}
    for path_c15v in ("/library/sections", "/identity", "/hubs", "/playlists",
                      "/status/sessions", "/library/sections/%s/all" % movie_section):
        routed_c15v[path_c15v] = (request(base, "PATCH", path_c15v)[0],
                                  request(base, "OPTIONS", path_c15v)[0])
    # The paths whose VERB TABLE is wrong are a separate finding (they answer the GET
    # body to every verb); what this check pins is that PATCH and a bare OPTIONS go
    # through routing at all rather than being refused or answered as a preflight.
    check("PATCH and OPTIONS are routed like any other verb, not blanket-404ed",
          routed_c15v["/identity"] == (200, 200) and routed_c15v["/hubs"] == (200, 200)
          and routed_c15v["/playlists"] == (200, 200)
          and routed_c15v["/library/sections/%s/all" % movie_section] == (404, 404),
          str(routed_c15v))
    frob_c15v, frob_body_c15v, frob_hd_c15v = request(base, "FROB", "/library/sections")
    check("a method a real PMS does not recognise is a 500, not a 501",
          frob_c15v == 500 and b"501" not in frob_body_c15v
          and b"Internal Server Error" in frob_body_c15v
          and b"DOCTYPE" not in frob_body_c15v,
          "HTTP %s body %r" % (frob_c15v, frob_body_c15v[:70]))

    # Cycle 15, measured on plexscratch: POSTing an EMPTY body to any artwork or
    # subtitle candidate list is a **406** -- `/posters`, `/arts`, `/thumbs`,
    # `/banners`, `/themes`, `/clearLogos` and `/subtitles` alike, all seven identical.
    # `/extras` is the one that is a 400 instead.
    #
    # It also refines a recorded rule. HANDOVER has the subtitle upload's "no `title`
    # is a 500, an empty body a 406"; with NEITHER, the copy answers 406, so the
    # empty-body check runs FIRST. plexsim checked the title first and answered 500,
    # and answered 400/404/200 across the other six -- two of which (`/themes`,
    # `/clearLogos`) returned the candidate LISTING to a POST, which is fix 4's trap:
    # a client reads that as an accepted upload.
    posts_c15n = {leaf_c15n: request(base, "POST", "/library/metadata/%s/%s" % (movie, leaf_c15n))[0]
                  for leaf_c15n in ("posters", "arts", "thumbs", "banners", "themes",
                                    "clearLogos", "subtitles")}
    check("POST with an empty body to any candidate list is a 406",
          set(posts_c15n.values()) == {406}, str(posts_c15n))
    check("an empty body is refused before a missing title on the subtitle upload",
          request(base, "POST", "/library/metadata/%s/subtitles" % movie)[0] == 406
          and request(base, "POST", "/library/metadata/%s/subtitles?title=a.srt&format=srt"
                      % movie)[0] == 406,
          "bare %s | titled but empty %s"
          % (request(base, "POST", "/library/metadata/%s/subtitles" % movie)[0],
             request(base, "POST", "/library/metadata/%s/subtitles?title=a.srt&format=srt"
                     % movie)[0]))

    # Cycle 15, measured on plexscratch over eleven collections of four member types and
    # both smart values: a collection row's `ratingCount` is NOT a count. It is the Plex
    # TYPE NUMBER of its members, plus 100 when the collection is smart:
    #
    #     movie 1   show 2   season 3   episode 4      smart: 101  102  104
    #
    # Cycle 14 saw `1` on a three-member collection and left it unexplained. The rule
    # predicted 102 and 104 before either was measured. plexsim sent the member count,
    # which is already in `childCount` -- and the 100 offset is a second, independent
    # signal of `smart`.
    #
    # `librarySectionID` is a NUMBER on a collection row too. plexsim already sends an
    # int on a movie detail row, so its collection path contradicted its own rows.
    made_c15r = {}
    for label_c15r, typ_c15r, sec_c15r, key_c15r in (("movie", 1, movie_section, movie),
                                                     ("show", 2, show_section, show)):
        u_c15r = quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                       % (machine, key_c15r))
        body_c15r = container(
            base, "POST",
            "/library/collections?type=%d&title=%s&smart=0&sectionId=%s&uri=%s"
            % (typ_c15r, quote("C15r %s" % label_c15r), sec_c15r, u_c15r))[1]
        made_c15r[label_c15r] = (body_c15r.get("Metadata") or [{}])[0]
    smart_body_c15r = container(
        base, "POST", "/library/collections?type=1&title=%s&smart=1&sectionId=%s&uri=%s"
                          % (quote("C15r smart"), movie_section,
                             quote("server://%s/com.plexapp.plugins.library"
                                   "/library/sections/%s/all?type=1"
                                   % (machine, movie_section))))[1]
    made_c15r["smart"] = (smart_body_c15r.get("Metadata") or [{}])[0]
    counts_c15r = {k_c15r: v_c15r.get("ratingCount") for k_c15r, v_c15r in made_c15r.items()}
    # Read one back from the metadata path as well: the create response and the detail
    # row are built by the same function, and the section id is what is being checked.
    detail_c15r = (get(base, "/library/metadata/%s" % made_c15r["movie"].get("ratingKey"))[1]
                   .get("Metadata") or [{}])[0]
    for row_c15r in get(base, "/library/sections/%s/collections" % movie_section)[1] \
            .get("Metadata") or []:
        if str(row_c15r.get("title") or "").startswith("C15r "):
            request(base, "DELETE", "/library/collections/%s" % row_c15r["ratingKey"])
    for row_c15r in get(base, "/library/sections/%s/collections" % show_section)[1] \
            .get("Metadata") or []:
        if str(row_c15r.get("title") or "").startswith("C15r "):
            request(base, "DELETE", "/library/collections/%s" % row_c15r["ratingKey"])
    check("a collection's ratingCount is its member type number, +100 when smart",
          counts_c15r == {"movie": 1, "show": 2, "smart": 101}, str(counts_c15r))
    check("a collection row's librarySectionID is a number",
          isinstance(detail_c15r.get("librarySectionID"), int),
          "detail %r | create %r" % (detail_c15r.get("librarySectionID"),
                                     made_c15r["movie"].get("librarySectionID")))

    # Cycle 15, measured on plexscratch. Three separate defects on the collection leaves:
    #
    #   GET /library/collections/{id}/prefs  is a **404** -- smart and plain alike --
    #       where plexsim answered an empty 200. That is fix 4's trap from the inside:
    #       the client reads a 200 as "this endpoint exists and is empty" and stops
    #       looking for the settings, which live in the includePreferences block.
    #   GET /library/collections/{id}/posters carries the container's identifying
    #       triple (identifier, mediaTagPrefix, mediaTagVersion) and ONE `default://`
    #       candidate -- the collection's own composite -- marked selected, with NO
    #       `provider` key. /arts is an empty container carrying the same triple.
    #       plexsim answered a bare `{size: 0}` to both, so Change Poster on a
    #       collection page offered nothing.
    #   GET /library/metadata/{collection}/arts must be EMPTY as well; plexsim handed
    #       back the POSTER there, so Change Background offered the poster.
    coll_c15l = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("C15 Leaves"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, movie))))[1].get("Metadata") or [{}])[0].get("ratingKey")
    prefs_c15l = request(base, "GET", "/library/collections/%s/prefs" % coll_c15l)[0]
    posters_c15l = get(base, "/library/collections/%s/posters" % coll_c15l)[1]
    arts_c15l = get(base, "/library/collections/%s/arts" % coll_c15l)[1]
    meta_arts_c15l = get(base, "/library/metadata/%s/arts" % coll_c15l)[1]
    request(base, "DELETE", "/library/collections/%s" % coll_c15l)
    triple_c15l = ("identifier", "mediaTagPrefix", "mediaTagVersion")
    check("a collection's /prefs is a 404 and its artwork lists carry the container triple",
          prefs_c15l == 404
          and all(k_c15l in posters_c15l for k_c15l in triple_c15l)
          and all(k_c15l in arts_c15l for k_c15l in triple_c15l)
          and (posters_c15l.get("size") or 0) == 1
          and (arts_c15l.get("size") or 0) == 0
          and (meta_arts_c15l.get("size") or 0) == 0,
          "prefs %s | posters %s size=%s | arts %s size=%s | metadata/arts size=%s"
          % (prefs_c15l, sorted(k_c15l for k_c15l in posters_c15l if k_c15l != "Metadata"),
             posters_c15l.get("size"),
             sorted(k_c15l for k_c15l in arts_c15l if k_c15l != "Metadata"),
             arts_c15l.get("size"), meta_arts_c15l.get("size")))
    cand_c15l = (posters_c15l.get("Metadata") or [{}])[0]
    check("the collection poster candidate is its own composite, selected, with no provider",
          str(cand_c15l.get("ratingKey") or "").startswith("default://")
          and cand_c15l.get("selected") is True and "provider" not in cand_c15l,
          str(cand_c15l))

    # Cycle 15, measured on plexscratch across four collections of three shapes: a
    # collection's `includePreferences=1` block carries exactly TWO settings, and which
    # second one depends on `smart`:
    #
    #     plain  ->  collectionMode  +  collectionSort
    #     smart  ->  collectionMode  +  collectionFilterBasedOnUser
    #
    # A smart collection has no collection ORDER at all -- consistent with the write
    # side, where `PUT .../prefs?collectionSort=` on a smart collection is a 400 (it is
    # a 200 for `collectionMode`). The block rides on `/library/metadata/{id}` and on
    # `/library/collections/{id}` alike and never on a listing row.
    #
    # plexsim sent no `Preferences` on a collection at all, so the Edit Collection
    # dialog had no Collection Mode and no Collection order control.
    def coll_prefs_c15k(smart_c15k):
        uri_c15k = (quote("server://%s/com.plexapp.plugins.library"
                          "/library/sections/%s/all?type=1" % (machine, movie_section))
                    if smart_c15k else
                    quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                          % (machine, movie)))
        key_c15k = (container(
            base, "POST", "/library/collections?type=1&title=%s&smart=%d&sectionId=%s&uri=%s"
            % (quote("C15k %d" % smart_c15k), smart_c15k, movie_section, uri_c15k))[1]
            .get("Metadata") or [{}])[0].get("ratingKey")
        ids_c15k = [s_c15k.get("id") for s_c15k in
                    (((get(base, "/library/metadata/%s?includePreferences=1" % key_c15k)[1]
                       .get("Metadata") or [{}])[0].get("Preferences") or {}).get("Setting") or [])]
        via_c15k = [s_c15k.get("id") for s_c15k in
                    (((get(base, "/library/collections/%s?includePreferences=1" % key_c15k)[1]
                       .get("Metadata") or [{}])[0].get("Preferences") or {}).get("Setting") or [])]
        sort_c15k = request(base, "PUT",
                            "/library/metadata/%s/prefs?collectionSort=1" % key_c15k)[0]
        mode_c15k = request(base, "PUT",
                            "/library/metadata/%s/prefs?collectionMode=1" % key_c15k)[0]
        request(base, "DELETE", "/library/collections/%s" % key_c15k)
        return ids_c15k, via_c15k, sort_c15k, mode_c15k


    plain_c15k = coll_prefs_c15k(0)
    smart_c15k = coll_prefs_c15k(1)
    check("a collection's Preferences are two settings, and smart swaps the second",
          plain_c15k[0] == ["collectionMode", "collectionSort"]
          and smart_c15k[0] == ["collectionMode", "collectionFilterBasedOnUser"]
          and plain_c15k[1] == plain_c15k[0],
          "plain %s | smart %s | via /collections %s"
          % (plain_c15k[0], smart_c15k[0], plain_c15k[1]))
    check("a smart collection refuses collectionSort and takes collectionMode",
          plain_c15k[2] == 200 and smart_c15k[2] == 400 and smart_c15k[3] == 200,
          "plain sort %s | smart sort %s | smart mode %s"
          % (plain_c15k[2], smart_c15k[2], smart_c15k[3]))

    # Cycle 15, measured on plexscratch over twelve values: `collectionSort` sorts a
    # collection's children ALPHABETICALLY for anything that parses as a plain non-zero
    # integer -- `1`, `2`, `3`, `99`, `-1`, `01` and `+1` all do -- and by RELEASE DATE
    # for `0` and for anything that does not parse: `1.5`, a leading space, `abc`, and an
    # empty value (which also removes the field). So a leading `+` is allowed, a decimal
    # point and a leading space are not. plexsim honoured only `1` and `2` and fell back
    # to release date for every other number, so a stored value the free-text pref can
    # easily hold drew the collection in the wrong order.
    # The fixture's three movies share a release date and sort alphabetically in
    # ratingKey order, so release order and alphabetical order coincide and nothing can
    # discriminate. Give them dates that REVERSE the alphabetical order first, and put
    # them back afterwards.
    members_c15t = [r_c15t["ratingKey"] for r_c15t in
                    get(base, "/library/sections/%s/all?type=1" % movie_section)[1]
                    .get("Metadata") or []]
    was_c15t = {k_c15t: (get(base, "/library/metadata/%s" % k_c15t)[1]
                         .get("Metadata") or [{}])[0].get("originallyAvailableAt")
                for k_c15t in members_c15t}
    for n_c15t, k_c15t in enumerate(reversed(members_c15t)):
        request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s"
                % (k_c15t, quote("200%d-01-01" % n_c15t)))
    sortcoll_c15t = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("C15 Sort"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, ",".join(members_c15t)))))[1]
        .get("Metadata") or [{}])[0].get("ratingKey")


    def order_c15t(value_c15t):
        request(base, "PUT", "/library/metadata/%s/prefs?collectionSort=%s"
                % (sortcoll_c15t, quote(value_c15t)))
        return [r_c15t.get("ratingKey") for r_c15t in
                get(base, "/library/metadata/%s/children" % sortcoll_c15t)[1]
                .get("Metadata") or []]


    release_c15t = order_c15t("0")
    alpha_c15t = order_c15t("1")
    # `2` is left out deliberately: it is the CUSTOM order a move writes (cycle 9), and
    # on this fixture the custom, insertion and alphabetical orders coincide, so it
    # cannot discriminate. The other five are unambiguous.
    same_c15t = {v_c15t: order_c15t(v_c15t) == alpha_c15t
                 for v_c15t in ("3", "99", "-1", "01", "+1")}
    fallback_c15t = {v_c15t: order_c15t(v_c15t) == release_c15t
                     for v_c15t in ("1.5", " 1", "abc", "")}
    request(base, "DELETE", "/library/collections/%s" % sortcoll_c15t)
    for k_c15t, when_c15t in was_c15t.items():
        if when_c15t:
            request(base, "PUT", "/library/metadata/%s?originallyAvailableAt.value=%s"
                    % (k_c15t, quote(when_c15t)))
    check("collectionSort sorts alphabetically for any non-zero integer it can parse",
          all(same_c15t.values()) and all(fallback_c15t.values())
          and release_c15t != alpha_c15t,
          "alpha-like %s | release-like %s | release %s alpha %s"
          % (same_c15t, fallback_c15t, release_c15t, alpha_c15t))

    # Cycle 15, measured on plexscratch and reproduced on a freshly reset copy: the two
    # rename paths differ. `PUT /library/collections/{id}?title=` renames the collection
    # and LEAVES the members' `Collection` tag alone -- cycle 9 measured that, and it
    # still holds. `PUT /library/sections/{id}/all?type=18&id=&title.value=` renames the
    # TAG as well, on every member and in the `/collection` axis.
    #
    # It matters because the Edit Collection dialog saves through the SECTION path. After
    # a rename on plexsim every member's page still showed the old collection name and
    # the library's Collections filter still offered it, and the two never converged.
    #
    rn_c15w = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("C15w Before"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, movie))))[1].get("Metadata") or [{}])[0].get("ratingKey")


    def member_tags_c15w():
        return [(t_c15w.get("tag"), "guid" in t_c15w, "count" in t_c15w) for t_c15w in
                ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                 .get("Collection") or []) if str(t_c15w.get("tag") or "").startswith("C15w")]


    start_c15w = member_tags_c15w()
    request(base, "PUT", "/library/collections/%s?title=%s" % (rn_c15w, quote("C15w Item")))
    after_item_c15w = member_tags_c15w()
    request(base, "PUT", "/library/sections/%s/all?type=18&id=%s&title.value=%s"
            % (movie_section, rn_c15w, quote("C15w Section")))
    after_section_c15w = member_tags_c15w()
    axis_c15w = [d_c15w.get("title") for d_c15w in
                 get(base, "/library/sections/%s/collection" % movie_section)[1]
                 .get("Directory") or [] if str(d_c15w.get("title") or "").startswith("C15w")]
    request(base, "DELETE", "/library/collections/%s" % rn_c15w)
    check("the section-path rename renames the members' Collection tag and the item path does not",
          [t_c15w[0] for t_c15w in start_c15w] == ["C15w Before"]
          and [t_c15w[0] for t_c15w in after_item_c15w] == ["C15w Before"]
          and [t_c15w[0] for t_c15w in after_section_c15w] == ["C15w Section"]
          and axis_c15w == ["C15w Section"],
          "start %s | item %s | section %s | axis %s"
          % (start_c15w, after_item_c15w, after_section_c15w, axis_c15w))

    # Cycle 15, measured on plexscratch: a member row's `Collection` tag carries the
    # COLLECTION's own `guid` -- the same `collection://<uuid>` string the collection row
    # has -- and carries NO `count`, which is consistent with cycle 12's refutation of
    # `count` on a detail row's tags. Cycle 14 recorded "guid and count"; only the guid
    # half is real. plexsim emitted neither, so a client could not tie a member's tag to
    # the collection it names by anything but the title.
    tg_c15u = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("C15u Tagged"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, movie))))[1].get("Metadata") or [{}])[0]
    mine_c15u = [t_c15u for t_c15u in
                 ((get(base, "/library/metadata/%s" % movie)[1].get("Metadata") or [{}])[0]
                  .get("Collection") or [])
                 if str(t_c15u.get("tag") or "").startswith("C15u")]
    request(base, "DELETE", "/library/collections/%s" % tg_c15u.get("ratingKey"))
    check("a member's Collection tag carries the collection's guid and no count",
          len(mine_c15u) == 1 and mine_c15u[0].get("guid") == tg_c15u.get("guid")
          and str(mine_c15u[0].get("guid") or "").startswith("collection://")
          and "count" not in mine_c15u[0],
          "tag %s | collection guid %r" % (mine_c15u, tg_c15u.get("guid")))

    # Cycle 15, measured on THREE servers -- kids (1.43.4), plexscratch and zen (1.42.2),
    # the last on episode 11115 of a show that does have artwork: an episode's own
    # `/library/metadata/{id}/art` path is a 404, bare and versioned. Its `/thumb` and
    # `/poster` are 200s, and a SEASON's `/art` is a 200 on zen, so this belongs to the
    # episode alone. The row ATTRIBUTE is not affected: an episode's `art` names its
    # season's, exactly as plexsim's does, and that is the URL a client actually builds.
    leaf_c15x = (get(base, "/library/metadata/%s/children"
                     % (get(base, "/library/metadata/%s/children" % show)[1]
                        .get("Metadata") or [{}])[0]["ratingKey"])[1]
                 .get("Metadata") or [{}])[0]["ratingKey"]
    arts_c15x = {name_c15x: request(base, "GET", "/library/metadata/%s/%s"
                                    % (leaf_c15x, name_c15x))[0]
                 for name_c15x in ("art", "art/1", "thumb", "poster")}
    check("an episode has no art path of its own, and keeps its thumb and poster",
          arts_c15x["art"] == 404 and arts_c15x["art/1"] == 404
          and arts_c15x["thumb"] == 200 and arts_c15x["poster"] == 200,
          str(arts_c15x))

    # Cycle 15, measured on plexscratch: a label written onto a COLLECTION reaches the
    # library's `/label` axis -- under a bare call, under `?type=18` and under `?type=1`
    # alike, consistent with cycle 14's note that the axis is global. plexsim's axis
    # listed a label written onto a MOVIE and never looked at collections, so a label
    # used to organise collections could not be filtered on at all.
    lb_c15y = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("C15y Labelled"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, movie))))[1].get("Metadata") or [{}])[0].get("ratingKey")
    request(base, "PUT", "/library/metadata/%s?label%%5B0%%5D.tag.tag=%s"
            % (lb_c15y, quote("C15yLabel")))
    axis_c15y = {spec_c15y: [d_c15y.get("title") for d_c15y in
                             get(base, "/library/sections/%s/label%s" % (movie_section, spec_c15y))[1]
                             .get("Directory") or []
                             if str(d_c15y.get("title") or "").startswith("C15y")]
                 for spec_c15y in ("", "?type=18", "?type=1")}
    request(base, "DELETE", "/library/collections/%s" % lb_c15y)
    check("a label on a collection reaches the /label axis under every type",
          all(v_c15y == ["C15yLabel"] for v_c15y in axis_c15y.values()), str(axis_c15y))

    # Cycle 15, measured on plexscratch: a collection is found by `/hubs/search`, as a
    # `tag` Directory row in the `collection` hub -- `tagType: 2`, the member count,
    # the collection's own `guid`, a `key` and `filter` of `collection=<tag id>`, and
    # `reason`/`reasonID`/`reasonTitle` naming the LIBRARY (`section`, its id, its
    # title), which is the tag-row shape plexsim already builds for actors and genres.
    #
    # `/library/search` finds NOTHING for a collection on the copy, with or without
    # `includeCollections=1` -- which REFUTES cycle 14's note that it carries the
    # collection as a tag row. Only `/hubs/search` and the library's own
    # `?type=18` search box do.
    #
    # plexsim's `collection` hub was empty for every query, so typing a collection's
    # name into Plex Web's search box never found it.
    sc_c15z = (container(
        base, "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
        % (quote("Zarquon Collection"), movie_section,
           quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                 % (machine, movie))))[1].get("Metadata") or [{}])[0]
    hubs_c15z = {h_c15z.get("type"): h_c15z for h_c15z in
                 get(base, "/hubs/search?query=%s&includeCollections=1" % quote("Zarquon"))[1]
                 .get("Hub") or []}
    dirs_c15z = (hubs_c15z.get("collection") or {}).get("Directory") or []
    flat_c15z = get(base, "/library/search?query=%s&includeCollections=1"
                    % quote("Zarquon"))[1].get("SearchResult") or []
    request(base, "DELETE", "/library/collections/%s" % sc_c15z.get("ratingKey"))
    check("a collection is found by /hubs/search as a tag row and not by /library/search",
          len(dirs_c15z) == 1 and dirs_c15z[0].get("type") == "tag"
          and dirs_c15z[0].get("tagType") == 2
          and dirs_c15z[0].get("tag") == "Zarquon Collection"
          and dirs_c15z[0].get("guid") == sc_c15z.get("guid")
          and dirs_c15z[0].get("count") == 1
          and dirs_c15z[0].get("reason") == "section"
          and not [r_c15z for r_c15z in flat_c15z
                   if "Zarquon" in str((r_c15z.get("Metadata") or {}).get("title"))],
          "hub rows %s | library/search %d" % (dirs_c15z, len(flat_c15z)))

    # Cycle 15. Nobody had ever swept this surface by METHOD -- a write to a shared
    # reference is forbidden, so it needed the disposable copy. Re-measured by the
    # driver across 33 paths, every one of which differed:
    #
    #   * A READ-ONLY route answers **404** to PUT, POST and DELETE. plexsim answered
    #     the GET body with a 200 to all three on twenty of them -- `/`,
    #     `/library/recentlyAdded`, `/library/onDeck`, `/hubs/promoted`,
    #     `/hubs/continueWatching`, `/hubs/search`, `/channels/all`, `/services/browse`,
    #     `/system`, `/system/agents`, `/system/scanners/{n}`, `/updater/status`,
    #     `/:/plugins`, `/playlists/all`, `/status/sessions/history/all`, and the item
    #     leaves `allLeaves`, `tree`, `related`, `similar` and `matches`.
    #   * The legacy single-segment plug-in dispatcher answers **every** method, not
    #     just GET: `PUT /prefs`, `POST /setting`, `DELETE /users` and `/devices.xml`
    #     are all 200 with `content="plugins"` on the copy, where plexsim 404'd them.
    #
    # HANDOVER already records that verb routing is per (method, path) and not uniform.
    # It reads to a client -- and to python-plexapi's capability probes, which are
    # exactly this shape -- as "this endpoint exists and accepts writes".
    readonly_c15v2 = {}
    for p_c15v2 in ("/", "/library/recentlyAdded", "/library/onDeck", "/hubs/promoted",
                    "/hubs/continueWatching", "/channels/all", "/services/browse",
                    "/system", "/system/agents", "/updater/status", "/:/plugins",
                    "/library/metadata/%s/tree" % show,
                    "/library/metadata/%s/allLeaves" % show,
                    "/library/metadata/%s/related" % show):
        readonly_c15v2[p_c15v2] = [request(base, m_c15v2, p_c15v2)[0]
                                   for m_c15v2 in ("PUT", "POST", "DELETE")]
    check("a read-only route answers 404 to every verb but GET and HEAD",
          all(codes_c15v2 == [404, 404, 404] for codes_c15v2 in readonly_c15v2.values()),
          str({k_c15v2: v_c15v2 for k_c15v2, v_c15v2 in readonly_c15v2.items()
               if v_c15v2 != [404, 404, 404]}) or "all 404")
    plugin_c15v2 = {}
    for p_c15v2 in ("/prefs", "/setting", "/users", "/devices.xml"):
        plugin_c15v2[p_c15v2] = [container(base, m_c15v2, p_c15v2)
                                 for m_c15v2 in ("PUT", "POST", "DELETE")]
    check("the single-segment plug-in dispatcher answers every method, not just GET",
          all(st_c15v2 == 200 and doc_c15v2.get("content") == "plugins"
              for codes_c15v2 in plugin_c15v2.values()
              for st_c15v2, doc_c15v2, _ in codes_c15v2),
          str({k_c15v2: [(s_c15v2, d_c15v2.get("content")) for s_c15v2, d_c15v2, _ in v_c15v2]
               for k_c15v2, v_c15v2 in plugin_c15v2.items()}))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree on every cell:
    # `limit=` and `parent=` are PARSED wherever a listing consumes them, and a value
    # that is not a whole number is a **500** -- `abc`, an empty value and `1.5` alike --
    # on the section grid, `allLeaves`, `search`, `common`, `/library/all`, an item's
    # `/children` and `/grandchildren`, `/playlists` and the secondary axes. On
    # `/status/sessions/history/all` it is a **400** instead, the refusal that page gives
    # its ids. A GOOD limit still does nothing on `/children` (cycle 11) -- ignoring the
    # VALUE is not the same as not parsing the string.
    #
    # plexsim validated `limit` on three paths and `parent` nowhere, so a caller reading
    # a 200 with the full listing took it for "that filter applied".
    bad_c15g = {}
    for path_c15g in ("/library/sections/%s/all?type=1" % movie_section,
                      "/library/sections/%s/allLeaves" % show_section,
                      "/library/sections/%s/search?type=1&query=a" % movie_section,
                      "/library/sections/%s/common?type=1" % movie_section,
                      "/library/all?type=1",
                      "/library/metadata/%s/children" % show,
                      "/library/metadata/%s/grandchildren" % show,
                      "/playlists",
                      "/library/sections/%s/genre" % movie_section,
                      "/library/sections/%s/firstCharacter" % movie_section):
        sep_c15g = "&" if "?" in path_c15g else "?"
        bad_c15g[path_c15g] = [request(base, "GET", path_c15g + sep_c15g + spec_c15g)[0]
                               for spec_c15g in ("limit=abc", "limit=", "limit=1.5",
                                                 "parent=abc", "parent=")]
    check("limit= and parent= are a 500 on every listing that consumes them",
          all(codes_c15g == [500] * 5 for codes_c15g in bad_c15g.values()),
          str({k_c15g: v_c15g for k_c15g, v_c15g in bad_c15g.items()
               if v_c15g != [500] * 5}) or "all 500")
    hist_c15g = [request(base, "GET", "/status/sessions/history/all?type=1&" + spec_c15g)[0]
                 for spec_c15g in ("limit=abc", "limit=", "limit=1.5")]
    check("the history page refuses a malformed limit with a 400, not a 500",
          hist_c15g == [400, 400, 400]
          and request(base, "GET", "/status/sessions/history/all?type=1&parent=abc")[0] == 200,
          "%s | parent %s" % (hist_c15g,
                              request(base, "GET",
                                      "/status/sessions/history/all?type=1&parent=abc")[0]))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree exactly: a
    # FRACTIONAL value is a 500 on seven columns and on no others.
    #
    #   500 on `=1.5`:  viewCount  viewOffset  lastViewedAt  lastRatedAt
    #                   addedAt    updatedAt   createdAt
    #   0 rows, no refusal: year duration index skipCount ratingCount mediaCount
    #                       absoluteIndex userRating
    #
    # So it is not the python type -- `year` and `duration` are integers too. It is the
    # timestamp and per-user-state family. And `lastRatedAt`, `createdAt` and
    # `lastViewedAt` refuse `abc` and an empty value as well, which plexsim missed for a
    # reason worth writing down: its date branch returns early when the ROW's value is
    # null, so on a library where nothing carries the column the VALUE was never parsed.
    # A real PMS parses the value first.
    frac_c15f = {col_c15f: request(base, "GET", "/library/sections/%s/all?type=1&%s=1.5"
                                   % (movie_section, col_c15f))[0]
                 for col_c15f in ("viewCount", "viewOffset", "lastViewedAt", "lastRatedAt",
                                  "addedAt", "updatedAt", "createdAt")}
    ok_c15f = {col_c15f: request(base, "GET", "/library/sections/%s/all?type=1&%s=1.5"
                                 % (movie_section, col_c15f))[0]
               for col_c15f in ("year", "duration", "index", "skipCount", "ratingCount",
                                "mediaCount", "absoluteIndex", "userRating")}
    word_c15f = {col_c15f: request(base, "GET", "/library/sections/%s/all?type=1&%s=abc"
                                   % (movie_section, col_c15f))[0]
                 for col_c15f in ("lastRatedAt", "createdAt", "lastViewedAt")}
    check("a fractional value refuses on the seven timestamp and state columns only",
          all(v_c15f == 500 for v_c15f in frac_c15f.values())
          and all(v_c15f == 200 for v_c15f in ok_c15f.values()),
          "refuse %s | allow %s" % (frac_c15f, ok_c15f))
    check("the three date columns refuse a word even when no row carries them",
          all(v_c15f == 500 for v_c15f in word_c15f.values()), str(word_c15f))

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), identical on both:
    # `/hubs/search?limit=` refuses in two different ways, and plexsim had them the
    # wrong way round.
    #
    #     abc  ''  1.5   -> 400   (unparseable)
    #     -1   0         -> 500   (parses, but is not a positive count)
    #     01   1   3     -> 200
    #
    # plexsim answered 500 to the 400 cases, 200 to the 500 cases, and let an empty
    # value through. No real client sends a malformed limit; it is recorded because it
    # is a complete two-witness grammar and cheap to hold.
    sl_c15q2 = {v_c15q2: request(base, "GET", "/hubs/search?query=%s&limit=%s"
                                 % (quote("a"), quote(v_c15q2)))[0]
                for v_c15q2 in ("abc", "", "1.5", "-1", "0", "01", "1", "3")}
    check("/hubs/search refuses an unparseable limit with 400 and a non-positive one with 500",
          sl_c15q2 == {"abc": 400, "": 400, "1.5": 400, "-1": 500, "0": 500,
                       "01": 200, "1": 200, "3": 200},
          str(sl_c15q2))

    # Cycle 15, measured on zen (1.42.2 -- the build plexsim reports; kids holds no
    # related items at all for any row, which is an instrument limit and is stated
    # rather than hidden): the INLINE `Related` block caps at **5** rows per hub and
    # says `more: true`, where the standalone `/hubs/metadata/{id}/related` answers 6
    # for the same row. `includeRelatedCount=N` sets the inline cap -- `=1` gives 1 and
    # `=3` gives 3, and `more` stays true -- and it does nothing without
    # `includeRelated=1`. plexsim honoured `count=` on the standalone endpoint and
    # ignored the inline spelling entirely, so on a real catalog the preplay page's
    # Related shelf would carry every related row the server had.
    rel_c15o = {}
    for spec_c15o in ("?includeRelated=1", "?includeRelated=1&includeRelatedCount=1",
                      "?includeRelatedCount=1"):
        row_c15o = (get(base, "/library/metadata/%s%s" % (movie, spec_c15o))[1]
                    .get("Metadata") or [{}])[0]
        rel_c15o[spec_c15o] = [h_c15o.get("size") for h_c15o in
                               ((row_c15o.get("Related") or {}).get("Hub") or [])]
    # By this point in the suite the fixture's rows have been edited and deleted,
    # so the item may have nothing related left to cap -- count them live.
    if rel_c15o["?includeRelated=1"]:
        check("includeRelatedCount caps the inline Related hubs and needs includeRelated",
              rel_c15o["?includeRelated=1&includeRelatedCount=1"]
              == [1] * len(rel_c15o["?includeRelated=1"])
              and all(n_c15o >= 1 for n_c15o in rel_c15o["?includeRelated=1"])
              and rel_c15o["?includeRelatedCount=1"] == [],
              str(rel_c15o))
    else:
        skip("includeRelatedCount caps the inline Related hubs",
             "nothing related is left in the fixture by this point")

    # Cycle 15: two standing checks asserted shapes this cycle re-measured and found
    # wrong. Updated rather than removed, per the handover's own rule, and re-measured
    # on plexscratch before either was touched:
    #
    #   * A BARE `OPTIONS` -- no `Access-Control-Request-*` headers -- is a 404 on
    #     `/library/metadata/{id}/refresh`, `/library/metadata/{id}`,
    #     `/library/metadata/{id}/posters` and `/library/sections/{id}/all` alike. Only
    #     a genuine preflight is answered 200. The old check sent no preflight headers
    #     and asserted 200, so it was testing plexsim's own habit of answering every
    #     OPTIONS as a preflight.
    #   * `PUT /library/metadata/{id}/banners` is a 404 and `POST .../thumbs` a **406**,
    #     not the 404 the old check wanted -- every artwork candidate list answers 406
    #     to a POST with no body.
    bare_c15j = {p_c15j: request(base, "OPTIONS", p_c15j, want_json=False)[0]
                 for p_c15j in ("/library/metadata/%s/refresh" % movie,
                                "/library/metadata/%s" % movie,
                                "/library/metadata/%s/posters" % movie,
                                "/library/sections/%s/all" % movie_section)}
    check("a bare OPTIONS is routed and 404s where only a preflight is answered",
          set(bare_c15j.values()) == {404}, str(bare_c15j))

    # Cycle 15, re-measured on plexscratch with a control write on the same socket
    # first. Cycle 13 recorded this and did not build it; cycle 14 re-measured it and
    # carried it; HANDOVER 3.3 named it cycle 15's cheapest territory. The frame counts,
    # on the two-season Firefly tree:
    #
    #     PUT /library/metadata/8/refresh   (episode)   7 frames
    #     PUT /library/metadata/7/refresh   (season)   18 frames
    #     PUT /library/metadata/6/refresh   (show)     49 frames
    #
    # The EPISODE form is the seven-frame sequence plexsim already sends. A SEASON adds
    # its episodes inside its own activity -- season queued, activity started, season
    # processing, season loading, then each episode's four (state 5 processing, state 4
    # loading twice, state 5 with no metadataState), activity ended, season done -- and a
    # SHOW nests a season's whole block under each season, with an `activity updated`
    # carrying that SEASON's frame title as its subtitle.
    #
    # plexsim answered SEVEN frames at every level, so refreshing a show told the client
    # one row had been re-read and left every season and episode showing stale metadata
    # until a reload.
    def frames_for_c15r2(key_c15r2):
        # notifications() answers (status, frames).
        return notifications(base, lambda: request(base, "PUT",
                                                   "/library/metadata/%s/refresh" % key_c15r2),
                             seconds=6)[1]


    seasons_c15r2 = [r_c15r2["ratingKey"] for r_c15r2 in
                     get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []]
    leaves_c15r2 = get(base, "/library/metadata/%s/children" % seasons_c15r2[0])[1] \
        .get("Metadata") or []
    ep_frames_c15r2 = frames_for_c15r2(leaves_c15r2[0]["ratingKey"])
    season_frames_c15r2 = frames_for_c15r2(seasons_c15r2[0])
    show_frames_c15r2 = frames_for_c15r2(show)


    def ids_c15r2(frames_c15r2):
        out_c15r2 = set()
        for f_c15r2 in frames_c15r2:
            box_c15r2 = f_c15r2.get("NotificationContainer", f_c15r2)
            for e_c15r2 in (box_c15r2.get("TimelineEntry") or []):
                out_c15r2.add(str(e_c15r2.get("itemID")))
        return out_c15r2


    all_leaves_c15r2 = {str(r_c15r2["ratingKey"]) for r_c15r2 in
                        get(base, "/library/metadata/%s/allLeaves" % show)[1]
                        .get("Metadata") or []}


    # The COUNT depends on how many rows the tree holds, so what is asserted is which
    # ROWS the cascade reaches -- which is the whole of the finding.
    check("a refresh cascades: an episode alone, a season with its episodes, a show with its tree",
          len(ep_frames_c15r2) == 7
          and ids_c15r2(ep_frames_c15r2) == {str(leaves_c15r2[0]["ratingKey"])}
          and ids_c15r2(season_frames_c15r2)
          == {str(seasons_c15r2[0])} | {str(r_c15r2["ratingKey"]) for r_c15r2 in leaves_c15r2}
          and ids_c15r2(show_frames_c15r2)
          >= {str(show)} | {str(k_c15r2) for k_c15r2 in seasons_c15r2} | all_leaves_c15r2,
          "episode %d frames over %s | season %d over %s | show %d over %d ids"
          % (len(ep_frames_c15r2), sorted(ids_c15r2(ep_frames_c15r2)),
             len(season_frames_c15r2), sorted(ids_c15r2(season_frames_c15r2)),
             len(show_frames_c15r2), len(ids_c15r2(show_frames_c15r2))))

    # Cycle 15, re-measured on plexscratch with a control write on the same socket
    # first -- the third of the three cascades HANDOVER 3.3 carried into this cycle.
    # `PUT /library/metadata/{id}/analyze` is a 200 with an empty body and then, per
    # LEAF and in tree order, THREE `state 5` frames carrying `mediaState` `analyzing`,
    # `thumbnailing` and none, followed by ONE `media.generate.chapter.thumbs` activity
    # (started at -1, ended at 100) whose subtitle is the TARGET's frame title:
    #
    #     episode  5 frames    season 11    show 21 (with the queue frame)    movie 5
    #
    # Cycle 13's "a per-LEVEL triple" was wrong -- the season and show rows are never
    # framed themselves, only their leaves. plexsim pushed nothing at all.
    #
    # The 21st frame of the show form is a `backgroundProcessingQueue`
    # `queueRegenerated` at +7.1 s. It is not create-specific and not periodic: cycle 14
    # saw it +60.8 s after a library create, +2.4 s after a season split and +1.2 s
    # after a collection add, and this cycle re-measured it at +63.1 s after a create.
    # It follows a write that leaves work in the queue.
    def analyze_frames_c15a(key_c15a):
        return notifications(base, lambda: request(base, "PUT",
                                                   "/library/metadata/%s/analyze" % key_c15a),
                             seconds=6)[1]


    def states_c15a(frames_c15a):
        out_c15a = []
        for f_c15a in frames_c15a:
            box_c15a = f_c15a.get("NotificationContainer", f_c15a)
            for e_c15a in (box_c15a.get("TimelineEntry") or []):
                out_c15a.append((str(e_c15a.get("itemID")), e_c15a.get("mediaState")))
            for e_c15a in (box_c15a.get("ActivityNotification") or []):
                out_c15a.append((e_c15a.get("event"),
                                 (e_c15a.get("Activity") or {}).get("type")))
        return out_c15a


    seasons_c15a = [r_c15a["ratingKey"] for r_c15a in
                    get(base, "/library/metadata/%s/children" % show)[1].get("Metadata") or []]
    leaf_c15a = (get(base, "/library/metadata/%s/children" % seasons_c15a[0])[1]
                 .get("Metadata") or [{}])[0]["ratingKey"]
    ep_c15a = states_c15a(analyze_frames_c15a(leaf_c15a))
    movie_c15a = states_c15a(analyze_frames_c15a(movie))
    want_c15a = [(str(leaf_c15a), "analyzing"), (str(leaf_c15a), "thumbnailing"),
                 (str(leaf_c15a), None),
                 ("started", "media.generate.chapter.thumbs"),
                 ("ended", "media.generate.chapter.thumbs")]
    check("an analyze frames every leaf three times and ends in a chapter-thumbs activity",
          ep_c15a == want_c15a
          and movie_c15a == [(str(movie), "analyzing"), (str(movie), "thumbnailing"),
                             (str(movie), None),
                             ("started", "media.generate.chapter.thumbs"),
                             ("ended", "media.generate.chapter.thumbs")],
          "episode %s | movie %s" % (ep_c15a, movie_c15a))
    season_c15a = states_c15a(analyze_frames_c15a(seasons_c15a[0]))
    kids_c15a = [r_c15a["ratingKey"] for r_c15a in
                 get(base, "/library/metadata/%s/children" % seasons_c15a[0])[1]
                 .get("Metadata") or []]
    check("a season's analyze frames its EPISODES and never the season row",
          [i_c15a for i_c15a, _s_c15a in season_c15a if i_c15a not in ("started", "ended")]
          == [str(k_c15a) for k_c15a in kids_c15a for _n_c15a in range(3)]
          and str(seasons_c15a[0]) not in [i_c15a for i_c15a, _s_c15a in season_c15a],
          str(season_c15a))

    # Cycle 15, re-measured on plexscratch -- the last of the three cascades HANDOVER
    # 3.3 carried into this cycle. `POST /library/sections` is a 201 with a `Location`
    # header (plexsim already had both) and then 75 frames: a `library.update.section`
    # "Scanning" activity stepping through the files with a `status` LIBRARY_UPDATE
    # frame beside it, a `created` pair per item, each item's refresh sequence, and
    # finally ONE `backgroundProcessingQueue` `queueRegenerated` at **+63.1 s**.
    #
    # The per-item half is over REAL files being scanned. plexsim owns none and a
    # library it creates is empty by construction, so there is nothing for those frames
    # to name -- that is an instrument fact and is recorded rather than faked. The queue
    # frame is not: it follows the write itself, and plexsim never sent it.
    frames_c15n2 = notifications(
        base, lambda: request(base, "POST",
                              "/library/sections?name=C15n&type=movie"
                              "&agent=tv.plex.agents.movie&scanner=Plex%20Movie"
                              "&language=en-US&location=%2Fplexsim%2F1"), seconds=6)[1]
    queue_c15n2 = [f_c15n2 for f_c15n2 in frames_c15n2
                   if (f_c15n2.get("NotificationContainer", f_c15n2).get("type")
                       == "backgroundProcessingQueue")]
    made_c15n2 = [d_c15n2.get("key") for d_c15n2 in
                  get(base, "/library/sections")[1].get("Directory") or []
                  if d_c15n2.get("title") == "C15n"]
    for key_c15n2 in made_c15n2:
        request(base, "DELETE", "/library/sections/%s" % key_c15n2)
    check("a library create pushes the backgroundProcessingQueue regeneration frame",
          len(queue_c15n2) == 1
          and (queue_c15n2[0].get("NotificationContainer", queue_c15n2[0])
               .get("BackgroundProcessingQueueEventNotification") or [{}])[0]
          == {"queueID": 1, "event": "queueRegenerated"},
          "%d queue frame(s): %s" % (len(queue_c15n2), queue_c15n2[:1]))


    # Cycle 16, measured on kids (1.43.4) and zen (1.42.2) over fifteen paths and
    # every status they answer: a real PMS sends NO `Server:` header at all.
    # BaseHTTPRequestHandler adds one from `server_version`, so plexsim announced
    # itself as `PlexSim/0.1 Python/3.10.20` on every single response -- the one
    # header on the wire that says the server is not a Plex Media Server.
    banner_c16a = {}
    for path_c16a in ("/identity", "/library/sections", "/no/such/path",
                      "/library/sections/%s/all" % movie_section):
        _, _, headers_c16a = request(base, "GET", path_c16a)
        lower_c16a = {k.lower(): v for k, v in headers_c16a.items()}
        banner_c16a[path_c16a] = (lower_c16a.get("server"), "date" in lower_c16a)
    check("no response carries a Server banner, and Date survives",
          all(server_c16a is None and dated_c16a
              for server_c16a, dated_c16a in banner_c16a.values()),
          "%s" % banner_c16a)


    # Cycle 16, measured on zen's 507-show / 741-season / 9287-episode library and
    # confirmed on kids: the `firstCharacter` AXIS counts a row BELOW the library's
    # own top-level type by its SHOW's sort title, not by its own. Bucketing zen's
    # seasons and episodes by their show reproduces the axis on 28 of 28 buckets at
    # type=3 AND at type=4; bucketing them by their own titles reproduces 0 of 28.
    # kids -- whose one show is Firefly, whose seasons are "Season 1"/"Season 2" and
    # whose episodes have real titles -- answers `F:2` at type=3 and `F:6` at type=4.
    # plexsim counted each row's own title, so an episode grid's A-Z rail was an index
    # of episode titles where a real PMS indexes the shows.
    def bucket_of_c16c(row_c16c):
        head_c16c = (str(row_c16c.get("titleSort") or row_c16c.get("title") or "")[:1]
                     or "#").upper()
        return "#" if head_c16c.isdigit() else head_c16c

    shows_c16c = (get(base, "/library/sections/%s/all?type=2" % show_section)[1]
                  .get("Metadata") or [])
    want_c16c = {}
    for row_c16c in shows_c16c:
        leaves_c16c = get(base, "/library/metadata/%s/allLeaves" % row_c16c["ratingKey"])[1]
        want_c16c[bucket_of_c16c(row_c16c)] = (
            want_c16c.get(bucket_of_c16c(row_c16c), 0)
            + len(leaves_c16c.get("Metadata") or []))
    axis_c16c = get(base, "/library/sections/%s/firstCharacter?type=4" % show_section)[1]
    letters_c16c = {d_c16c.get("title"): d_c16c.get("size")
                    for d_c16c in axis_c16c.get("Directory") or []}
    # the old server bucketed by the episode's own title; only a fixture where the two
    # predictions DIFFER can tell them apart, so assert that too rather than pass by
    # coincidence
    own_c16c = {}
    for row_c16c in (get(base, "/library/sections/%s/all?type=4" % show_section)[1]
                     .get("Metadata") or []):
        own_c16c[bucket_of_c16c(row_c16c)] = own_c16c.get(bucket_of_c16c(row_c16c), 0) + 1
    check("the firstCharacter axis buckets episodes by their SHOW's sort title",
          letters_c16c == want_c16c and want_c16c != own_c16c,
          "axis %s, by show %s, by own title %s" % (letters_c16c, want_c16c, own_c16c))


    # Cycle 16, measured on kids and zen: `/library/sections/{id}/firstCharacter/{key}`
    # is a real LISTING of that bucket's rows -- the light grid row, `title2` reading
    # "Start with '<key>'", the librarySection triple, and the listing grammar on top
    # (zen's `B` bucket answers 6 of its 161 rows to `year=2020` and reverses under
    # `sort=titleSort:desc`). It filters on the ROW's own sort title, which is what
    # makes it disagree with the axis above it. plexsim routed on the leaf alone, so
    # the extra segment was dropped and every bucket answered the AXIS again: clicking
    # a letter in the A-Z rail redrew the rail instead of opening the rows behind it.
    grid_c16d = (get(base, "/library/sections/%s/all" % movie_section)[1]
                 .get("Metadata") or [])
    letter_c16d = str(grid_c16d[0].get("titleSort")
                      or grid_c16d[0].get("title") or "#")[:1].upper()
    want_c16d = [r_c16d["ratingKey"] for r_c16d in grid_c16d
                 if str(r_c16d.get("titleSort") or r_c16d.get("title") or "")[:1].upper()
                 == letter_c16d]
    bucket_c16d = get(base, "/library/sections/%s/firstCharacter/%s"
                      % (movie_section, letter_c16d))
    lower_c16d = get(base, "/library/sections/%s/firstCharacter/%s"
                     % (movie_section, letter_c16d.lower()))[1]
    check("a firstCharacter bucket lists its own rows, case-sensitively",
          bucket_c16d[0] == 200
          and [r_c16d["ratingKey"] for r_c16d in bucket_c16d[1].get("Metadata") or []]
          == want_c16d
          and bool(want_c16d)
          and bucket_c16d[1].get("title2") == "Start with '%s'" % letter_c16d
          and "Directory" not in bucket_c16d[1]
          and (lower_c16d.get("Metadata") or []) == []
          and lower_c16d.get("title2") == "Start with '%s'" % letter_c16d.lower(),
          "%s letter=%r rows=%s want=%s title2=%r lowercase=%d"
          % (bucket_c16d[0], letter_c16d,
             [r_c16d.get("title") for r_c16d in bucket_c16d[1].get("Metadata") or []],
             want_c16d, bucket_c16d[1].get("title2"),
             len(lower_c16d.get("Metadata") or [])))


    # Cycle 16, measured on kids and zen: a SHOW library's `/unwatched` carries
    # `nocache: true` like every other container it answers, and a movie library's
    # does not. plexsim carried it on /all, /newest, /recentlyAdded, /onDeck,
    # /recentlyViewed, /allLeaves and /folder and dropped it on exactly this one, so
    # the Unplayed view was the single show-library listing a client was free to
    # cache.
    unwatched_c16e = get(base, "/library/sections/%s/unwatched" % show_section)[1]
    movies_c16e = get(base, "/library/sections/%s/unwatched" % movie_section)[1]
    check("a show library's /unwatched carries nocache and a movie library's does not",
          unwatched_c16e.get("nocache") is True and "nocache" not in movies_c16e,
          "show=%r movie=%r" % (unwatched_c16e.get("nocache"), movies_c16e.get("nocache")))


    # Cycle 16, swept across both references and both library types: a section leaf's
    # EXTRA path segment is never the leaf again. Every leaf that has no bucket --
    # /all, /onDeck, /recentlyAdded, /newest, /recentlyViewed, /allLeaves,
    # /collections, /common, /sorts, /filters, /folder, /search, /unwatched, /prefs,
    # /studio, /audioLanguage, /subtitleLanguage, /videoCodec, /audioCodec,
    # /audioLayout, /location -- answers 404 to one, and so does a SECOND extra
    # segment on a leaf that has one (`/genre/24/more`). The leaves that DO have a
    # bucket answer that bucket's rows: a tag axis keyed by TAG ID with the tag's own
    # title as `title2` and a 404 for an id the library does not hold, `year` and
    # `decade` keyed by a non-negative integer (a word or a negative number is a 404),
    # `contentRating` as "Rated <value>" and `resolution` accepting anything at all.
    # plexsim routed on the leaf alone and dropped the rest of the path, so every one
    # of these answered the AXIS -- 200 with a Directory of letters where the client
    # asked for a list of films.
    dead_c16g = {}
    for leaf_c16g in ("all", "onDeck", "recentlyAdded", "newest", "allLeaves",
                      "collections", "sorts", "filters", "folder", "unwatched",
                      "studio", "audioLanguage", "subtitleLanguage", "location"):
        dead_c16g[leaf_c16g] = get(base, "/library/sections/%s/%s/zzz999"
                                   % (movie_section, leaf_c16g))[0]
    axis_c16g = get(base, "/library/sections/%s/genre" % movie_section)[1]
    tag_c16g = (axis_c16g.get("Directory") or [{}])[0]
    bucket_c16g = get(base, "/library/sections/%s/genre/%s"
                      % (movie_section, tag_c16g.get("key")))
    filtered_c16g = get(base, "/library/sections/%s/all?genre=%s"
                        % (movie_section, tag_c16g.get("key")))[1]
    check("a section leaf's extra segment is a bucket listing or a 404, never the leaf",
          all(status_c16g == 404 for status_c16g in dead_c16g.values())
          and get(base, "/library/sections/%s/genre/%s/more"
                  % (movie_section, tag_c16g.get("key")))[0] == 404
          and get(base, "/library/sections/%s/genre/zzz999" % movie_section)[0] == 404
          and get(base, "/library/sections/%s/year/abc" % movie_section)[0] == 404
          and get(base, "/library/sections/%s/year/1999" % movie_section)[0] == 200
          and bucket_c16g[0] == 200
          and bucket_c16g[1].get("title2") == tag_c16g.get("title")
          and "Directory" not in bucket_c16g[1]
          and [r_c16g["ratingKey"] for r_c16g in bucket_c16g[1].get("Metadata") or []]
          == [r_c16g["ratingKey"] for r_c16g in filtered_c16g.get("Metadata") or []],
          "dead=%s tag=%r bucket=%s title2=%r rows=%d vs filter %d"
          % (dead_c16g, tag_c16g.get("title"), bucket_c16g[0],
             bucket_c16g[1].get("title2"),
             len(bucket_c16g[1].get("Metadata") or []),
             len(filtered_c16g.get("Metadata") or [])))


    # Cycle 16, measured on kids (1.43.4) and zen (1.42.2), every status and both
    # formats: a real PMS NAMES its connection state on every response. With
    # `Connection: keep-alive` (or HTTP/1.1 and no Connection header) it answers
    # `Connection: Keep-Alive` and `Keep-Alive: timeout=20`; with `Connection: close`
    # or HTTP/1.0 it answers `Connection: close` and no Keep-Alive. plexsim sent
    # neither header on any response, so a client that reads the timeout to schedule
    # reuse got nothing -- and an abandoned keep-alive socket pinned a thread for the
    # life of the process, because nothing closed an idle connection either.
    # urllib always sends `Connection: close`, so this one speaks HTTP itself.
    import socket as socket_c16h

    def envelope_c16h(want_c16h):
        sock_c16h = socket_c16h.create_connection(
            (base.split("//")[1].split(":")[0], int(base.rsplit(":", 1)[1])), timeout=20)
        sock_c16h.sendall(("GET /identity HTTP/1.1\r\nHost: h\r\nConnection: %s\r\n"
                           "Accept: application/json\r\n\r\n" % want_c16h).encode())
        seen_c16h = b""
        while b"\r\n\r\n" not in seen_c16h:
            chunk_c16h = sock_c16h.recv(4096)
            if not chunk_c16h:
                break
            seen_c16h += chunk_c16h
        sock_c16h.close()
        lines_c16h = seen_c16h.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        found_c16h = {}
        for line_c16h in lines_c16h[1:]:
            if ":" in line_c16h:
                name_c16h, _, value_c16h = line_c16h.partition(":")
                found_c16h[name_c16h.strip().lower()] = value_c16h.strip()
        return found_c16h.get("connection"), found_c16h.get("keep-alive")

    conn_c16h = {want_c16h: envelope_c16h(want_c16h)
                 for want_c16h in ("keep-alive", "close")}
    check("every response names its connection state",
          conn_c16h == {"keep-alive": ("Keep-Alive", "timeout=20"),
                        "close": ("close", None)},
          "%s" % conn_c16h)


    # Cycle 16, measured on kids and zen: a paged container answers
    # `X-Plex-Container-Start` and `X-Plex-Container-Total-Size` as response HEADERS
    # beside the body's own `offset`/`totalSize`, on every container that pages
    # (`/identity` answers Total-Size 0). And the paging HEADER beats the query when
    # both are sent: query `Start=0&Size=2` with header `Start=1&Size=1` answers
    # offset 1, size 1 on both references. plexsim read the header only where the
    # query did not name the parameter, so a client sending both got the query's page.
    _, _, plain_c16i = request(base, "GET", "/library/sections/%s/all" % movie_section)
    lower_c16i = {k_c16i.lower(): v_c16i for k_c16i, v_c16i in plain_c16i.items()}
    paged_c16i = get(base, "/library/sections/%s/all"
                     "?X-Plex-Container-Start=1&X-Plex-Container-Size=1" % movie_section)
    headers_c16i = {k_c16i.lower(): v_c16i for k_c16i, v_c16i in paged_c16i[2].items()}
    check("a paged container answers its window in response headers",
          headers_c16i.get("x-plex-container-start") == "1"
          and headers_c16i.get("x-plex-container-total-size")
          == str(paged_c16i[1].get("totalSize"))
          and "x-plex-container-start" not in lower_c16i,
          "paged=%s/%s body totalSize=%s unpaged carries start=%r"
          % (headers_c16i.get("x-plex-container-start"),
             headers_c16i.get("x-plex-container-total-size"),
             paged_c16i[1].get("totalSize"), lower_c16i.get("x-plex-container-start")))


    # Cycle 16, measured on kids and zen: a section's `/composite` exists only in its
    # VERSIONED form. The bare path is a 404 on both references, with and without
    # `?width&height`, and no `/library/sections` row carries a `composite` attribute
    # for a client to build the bare form from. plexsim answered image bytes there --
    # the same shape of invention as cycle 14's `/recentlyReleased`.
    bare_c16j = request(base, "GET", "/library/sections/%s/composite" % movie_section)
    versioned_c16j = request(base, "GET", "/library/sections/%s/composite/1" % movie_section)
    check("a section's bare /composite is a 404 and only the versioned form answers",
          bare_c16j[0] == 404 and versioned_c16j[0] == 200,
          "bare=%s versioned=%s" % (bare_c16j[0], versioned_c16j[0]))


    # Cycle 16, measured on kids and zen: a real PMS never sends `100 Continue`. With
    # `Expect: 100-continue` on a GET both references answer the 200 directly, while
    # plexsim wrote `HTTP/1.1 100 Continue` first (BaseHTTPRequestHandler's default
    # `handle_expect_100`). A client that counts responses, or a proxy, sees an extra
    # one that a real server never sends.
    import socket as socket_c16k
    sock_c16k = socket_c16k.create_connection(
        (base.split("//")[1].split(":")[0], int(base.rsplit(":", 1)[1])), timeout=20)
    sock_c16k.sendall(b"GET /identity HTTP/1.1\r\nHost: h\r\nExpect: 100-continue\r\n"
                      b"Accept: application/json\r\nConnection: close\r\n\r\n")
    first_c16k = b""
    while b"\r\n" not in first_c16k:
        chunk_c16k = sock_c16k.recv(4096)
        if not chunk_c16k:
            break
        first_c16k += chunk_c16k
    sock_c16k.close()
    check("a real PMS never answers 100 Continue",
          b"100 Continue" not in first_c16k.split(b"\r\n")[0],
          "%r" % first_c16k.split(b"\r\n")[0])


    # Cycle 16, measured on kids and zen: the `Accept-Encoding` token is matched
    # EXACTLY and case-sensitively. `GZIP` and `x-gzip` get a plain body on both
    # references, `*` gets a plain body, and `gzip;q=0` is still gzipped (the q is
    # ignored). plexsim lower-cased the whole header and looked for the substring
    # "gzip", so it compressed for `GZIP` and `x-gzip` where a real PMS does not.
    encodings_c16l = {}
    for offer_c16l in ("gzip", "GZIP", "x-gzip", "*", "gzip;q=0"):
        body_c16l = urllib.request.Request(
            base + "/library/sections/%s/all" % movie_section,
            headers={"Accept-Encoding": offer_c16l, "Accept": "application/json",
                     "User-Agent": UA})
        with urllib.request.urlopen(body_c16l, timeout=20) as answer_c16l:
            encodings_c16l[offer_c16l] = answer_c16l.headers.get("Content-Encoding")
    check("Accept-Encoding tokens are matched exactly and case-sensitively",
          encodings_c16l == {"gzip": "gzip", "GZIP": None, "x-gzip": None,
                             "*": None, "gzip;q=0": "gzip"},
          "%s" % encodings_c16l)


    # Cycle 16, measured on plexscratch: a PREFLIGHT that carries no `Origin` gets no
    # CORS headers at all -- not the method list, not the header list, not Max-Age.
    # plexsim answered all three. A browser always sends Origin, so nothing
    # user-visible turns on it; it is a probe being answered a question it did not ask.
    # A bare OPTIONS with no `Access-Control-Request-Method` is a different thing
    # entirely (cycle 15: it is routed like any other verb), so the preflight header
    # has to be sent for this to reach the branch at all.
    def preflight_c16m(origin_c16m):
        headers_c16m = {"User-Agent": UA, "Access-Control-Request-Method": "GET"}
        if origin_c16m:
            headers_c16m["Origin"] = origin_c16m
        probe_c16m = urllib.request.Request(base + "/library/sections", method="OPTIONS",
                                            headers=headers_c16m)
        try:
            with urllib.request.urlopen(probe_c16m, timeout=20) as answer_c16m:
                seen_c16m = dict(answer_c16m.headers)
        except urllib.error.HTTPError as refused_c16m:
            seen_c16m = dict(refused_c16m.headers)
        return sorted(name_c16m for name_c16m in seen_c16m
                      if name_c16m.lower().startswith("access-control"))

    check("a preflight with no Origin carries no CORS headers",
          preflight_c16m(None) == [] and preflight_c16m("http://x.example") != [],
          "none=%s origin=%s" % (preflight_c16m(None), preflight_c16m("http://x.example")))


    # Cycle 16, measured on plexscratch: `POST /library/metadata/{id}/extras` is a
    # 400 for an empty body and for a non-empty one alike -- cycle 15 recorded exactly
    # that while implementing the 406 its neighbours answer, and the code kept
    # answering the GET listing. A 200 carrying the extras container reads to a client
    # as an accepted upload.
    extras_c16n = request(base, "POST", "/library/metadata/%s/extras" % movie)
    check("POST to an item's /extras is a 400",
          extras_c16n[0] == 400,
          "%s %r" % (extras_c16n[0], extras_c16n[1][:60]))


    # Cycle 16, measured on kids and zen: `X-Plex-Platform: Roku` (header or query,
    # that exact spelling -- `roku`, iOS, tvOS, Chrome, Windows and the rest change
    # nothing) strips `id` AND the whole `Pivot` list from every library `Directory`
    # of `/media/providers`, leaving the other nine attributes untouched. plexsim sent
    # the same rows to every platform, so a Roku client is handed pivots a real server
    # withholds from it.
    def library_rows_c16o(platform_c16o):
        probe_c16o = urllib.request.Request(
            base + "/media/providers",
            headers={"Accept": "application/json", "User-Agent": UA,
                     "X-Plex-Platform": platform_c16o})
        with urllib.request.urlopen(probe_c16o, timeout=20) as answer_c16o:
            body_c16o = json.load(answer_c16o)["MediaContainer"]
        return [row_c16o for provider_c16o in body_c16o.get("MediaProvider") or []
                for feature_c16o in provider_c16o.get("Feature") or []
                for row_c16o in feature_c16o.get("Directory") or []
                if row_c16o.get("key")]

    roku_c16o = library_rows_c16o("Roku")
    chrome_c16o = library_rows_c16o("Chrome")
    check("X-Plex-Platform: Roku strips id and Pivot from the library rows",
          bool(chrome_c16o)
          and all("id" in row_c16o and "Pivot" in row_c16o for row_c16o in chrome_c16o)
          and all("id" not in row_c16o and "Pivot" not in row_c16o for row_c16o in roku_c16o)
          and all(sorted(set(chrome_c16o[n_c16o]) - {"id", "Pivot"}) == sorted(roku_c16o[n_c16o])
                  for n_c16o in range(len(roku_c16o))),
          "roku=%s chrome=%s" % (sorted(roku_c16o[0]) if roku_c16o else None,
                                 sorted(chrome_c16o[0]) if chrome_c16o else None))


    # Cycle 16, re-measured on kids and zen over thirty Accept headers, correcting
    # cycle 5's reading in six places. A range qualifies only if its media type is
    # exactly `application/json` or `application/xml` AND either it has no parameters
    # at all or its FIRST parameter begins exactly `q=` -- `;charset=utf-8`,
    # `;level=1;q=0.5`, `; q=0.5` (a space after the semicolon) and `;Q=0.5` are all
    # disqualified, while `application/json ;q=0.5` (a space BEFORE it) is not. A
    # range with no parameter weighs 1.0; one whose single `q=` value parses as a
    # non-negative float weighs that; anything else -- `q=0`, `q=abc`, `q=`, `q=-1`,
    # `q="0.5"`, `q=0.5x`, and a second parameter of any kind -- weighs 0 and still
    # QUALIFIES. Highest weight wins, a tie goes to the range listed first, and no
    # qualifying range at all is XML. plexsim dropped every zero-or-unparseable q
    # instead of ranking it, so `application/json;q=0` came back as XML where both
    # references answer JSON.
    accepts_c16p = {}
    for header_c16p in ("application/json;q=0", "application/json;q=abc",
                        "application/json;q=", "application/json;q=-1",
                        "application/json;q=0.5;charset=utf-8",
                        "application/json;charset=utf-8", "application/json; q=0.5",
                        "application/json ;q=0.5", "application/json;Q=0.5",
                        "application/json;q=0, application/xml;q=0.5",
                        "application/xml;q=0.5, application/json",
                        "application/json;q=0.5;level=1, application/xml;q=0.4"):
        probe_c16p = urllib.request.Request(base + "/identity",
                                            headers={"Accept": header_c16p, "User-Agent": UA})
        with urllib.request.urlopen(probe_c16p, timeout=20) as answer_c16p:
            accepts_c16p[header_c16p] = answer_c16p.read()[:1] == b"{" and "json" or "xml"
    want_c16p = {"application/json;q=0": "json", "application/json;q=abc": "json",
                 "application/json;q=": "json", "application/json;q=-1": "json",
                 "application/json;q=0.5;charset=utf-8": "json",
                 "application/json;charset=utf-8": "xml",
                 "application/json; q=0.5": "xml", "application/json ;q=0.5": "json",
                 "application/json;Q=0.5": "xml",
                 "application/json;q=0, application/xml;q=0.5": "xml",
                 "application/xml;q=0.5, application/json": "json",
                 "application/json;q=0.5;level=1, application/xml;q=0.4": "xml"}
    check("the Accept q grammar ranks a zero or unparseable q rather than dropping it",
          accepts_c16p == want_c16p,
          "%s" % {k_c16p: v_c16p for k_c16p, v_c16p in accepts_c16p.items()
                  if want_c16p[k_c16p] != v_c16p})


    # Cycle 16, measured on kids and zen: `Range` applies to EVERY response body, not
    # only to media bytes. `Range: bytes=0-9` on `/identity`, a grid, `/:/prefs` or
    # artwork answers `206 Partial Content` with `Content-Range: bytes 0-9/<len>`;
    # a range past the end is a `416`; an unrecognised unit (`cheese=0-9`) is served as
    # bytes; and a 206 is never gzipped even when the client offers gzip. plexsim
    # answered 200 with the whole body to all of them.
    ranged_c16q = urllib.request.Request(
        base + "/identity", headers={"Range": "bytes=0-9", "Accept": "application/json",
                                     "Accept-Encoding": "gzip", "User-Agent": UA})
    with urllib.request.urlopen(ranged_c16q, timeout=20) as answer_c16q:
        status_c16q = answer_c16q.status
        crange_c16q = answer_c16q.headers.get("Content-Range")
        enc_c16q = answer_c16q.headers.get("Content-Encoding")
        size_c16q = len(answer_c16q.read())
    past_c16q = request(base, "GET", "/identity")
    try:
        beyond_c16q = urllib.request.Request(
            base + "/identity", headers={"Range": "bytes=99999-", "User-Agent": UA,
                                         "Accept": "application/json"})
        with urllib.request.urlopen(beyond_c16q, timeout=20) as answer2_c16q:
            beyond_status_c16q = answer2_c16q.status
    except urllib.error.HTTPError as refused_c16q:
        beyond_status_c16q = refused_c16q.code
    check("Range applies to every response body, not only to media bytes",
          status_c16q == 206 and size_c16q == 10 and enc_c16q is None
          and crange_c16q == "bytes 0-9/%d" % len(past_c16q[1])
          and beyond_status_c16q == 416,
          "%s %r len=%d enc=%r beyond=%s"
          % (status_c16q, crange_c16q, size_c16q, enc_c16q, beyond_status_c16q))


    # Cycle 16, measured on kids and zen in XML: a `Meta/Type` element's children come
    # out `Filter, Sort, Field`. plexsim wrote `Field, Filter, Sort` --
    # `xmlout._CHILD_ORDER` knew the item-level `Field` and nothing about these three,
    # so they fell out in the order the dict happened to hold them. XML child order is
    # load-bearing in this project's history, which is why that table exists at all.
    _, meta_c16r, _ = request(base, "GET",
                              "/library/sections/%s/all?includeMeta=1"
                              "&X-Plex-Container-Size=1" % movie_section,
                              want_json=False)
    body_c16r = meta_c16r.decode("utf-8", "replace")
    block_c16r = body_c16r[body_c16r.find("<Type"):body_c16r.find("</Type>")]
    order_c16r = []
    for name_c16r in re.findall(r"<(\w+)", block_c16r)[1:]:
        if name_c16r not in order_c16r:
            order_c16r.append(name_c16r)
    check("a Meta/Type element orders its children Filter, Sort, Field",
          order_c16r == ["Filter", "Sort", "Field"],
          "%s" % order_c16r)


    # Cycle 16, measured on kids and zen: under `includeDetails=1` a `Sort` carries
    # NO `active`/`activeDirection` and a `Filter` no `advanced` -- those three belong
    # to the `includeMeta=1` form, where all of them appear. The `includeDetails` Type
    # blocks sit at the TOP level of the container rather than inside a `Meta`.
    # plexsim stamped `active` and `activeDirection` on every Sort under
    # includeDetails, so the Edit Library dialog's descriptor claimed a sort was
    # selected when the client had asked for none.
    details_c16s = get(base, "/library/sections/%s?includeDetails=1" % movie_section)[1]
    meta_c16s = get(base, "/library/sections/%s/all?includeMeta=1"
                    "&X-Plex-Container-Size=1" % movie_section)[1]
    detail_sorts_c16s = [sort_c16s for type_c16s in details_c16s.get("Type") or []
                         for sort_c16s in type_c16s.get("Sort") or []]
    meta_sorts_c16s = [sort_c16s
                       for type_c16s in (meta_c16s.get("Meta") or {}).get("Type") or []
                       for sort_c16s in type_c16s.get("Sort") or []]
    check("includeDetails carries no active/activeDirection where includeMeta does",
          bool(detail_sorts_c16s) and bool(meta_sorts_c16s)
          and not any("active" in sort_c16s or "activeDirection" in sort_c16s
                      for sort_c16s in detail_sorts_c16s)
          and any("active" in sort_c16s for sort_c16s in meta_sorts_c16s),
          "details=%s meta=%s"
          % (sorted(detail_sorts_c16s[0]) if detail_sorts_c16s else None,
             sorted(meta_sorts_c16s[0]) if meta_sorts_c16s else None))


    # Cycle 16, measured on kids and zen: the rows inlined by `includeChildren=1` carry
    # NO `librarySectionID`, `librarySectionKey` or `librarySectionTitle` -- and
    # neither do the same rows fetched through `/children`, which is where plexsim
    # already had it right. The inline block was built from the FULL row instead of the
    # child projection, so it alone carried the triple, and a client reading the
    # inlined season rows saw a shape no real PMS sends.
    inline_c16t = get(base, "/library/metadata/%s?includeChildren=1" % show)[1]
    rows_c16t = ((inline_c16t.get("Metadata") or [{}])[0].get("Children")
                 or {}).get("Metadata") or []
    standalone_c16t = get(base, "/library/metadata/%s/children?excludeAllLeaves=1"
                          % show)[1].get("Metadata") or []
    check("the rows includeChildren inlines carry no librarySection triple",
          bool(rows_c16t) and bool(standalone_c16t)
          and not any(key_c16t.startswith("librarySection")
                      for row_c16t in rows_c16t for key_c16t in row_c16t)
          and not any(key_c16t.startswith("librarySection")
                      for row_c16t in standalone_c16t for key_c16t in row_c16t),
          "inline=%s standalone=%s"
          % (sorted(k_c16t for k_c16t in (rows_c16t[0] if rows_c16t else {})
                    if k_c16t.startswith("librarySection")),
             sorted(k_c16t for k_c16t in (standalone_c16t[0] if standalone_c16t else {})
                    if k_c16t.startswith("librarySection"))))

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
