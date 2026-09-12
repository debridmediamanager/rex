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
import urllib.error
import urllib.parse
import urllib.request

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
                 "playqueue", "metadata", "match", "search", "content"):
        check("provider advertises %s" % kind, kind in features)
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
    check("unmatch leaves a local:// guid, not an absent one",
          str(row.get("guid") or "").startswith("local://"), row.get("guid"))
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
    if candidates:
        chosen = candidates[0]
        status, body, _ = request(
            base, "PUT", "/library/metadata/%s/match?guid=%s&name=%s&year=%s"
                         % (movie, quote(chosen["guid"]), quote(chosen["name"]),
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
    entries = listing.get("Metadata") or []
    if entries:
        status, body, _ = request(base, "PUT", "/library/metadata/%s/posters?url=%s"
                                               % (movie, quote(entries[-1]["key"])))
        check("selecting a poster answers an empty 200", is_empty_200(status, body),
              "HTTP %s" % status)

    # ------------------------------------------------------------------
    # 9. Get Info
    # ------------------------------------------------------------------
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
    if part_id is None:
        skip("download serves an attachment", "the row has no Part")
    else:
        status, _, headers = request(
            base, "HEAD", "/library/parts/%s/file.mkv?download=1" % part_id,
            want_json=False)
        disposition = headers.get("Content-Disposition", "")
        if status == 502:
            skip("download serves an attachment",
                 "part unresolvable: catalog links are account-scoped "
                 "(HTTP 502 = RD said unavailable_file)")
        else:
            check("download serves an attachment",
                  status in (200, 206) and "attachment" in disposition,
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
        if rows:
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
        status, children, _ = get(base, "/library/collections/%s/children" % collection_key)
        check("collection children list its members",
              status == 200 and any(str(r.get("ratingKey")) == str(movie)
                                    for r in children.get("Metadata") or []),
              "%d member(s)" % len(children.get("Metadata") or []))
        _, detail, _ = get(base, "/library/metadata/%s" % movie)
        tags = [t.get("tag") for t in (detail.get("Metadata") or [{}])[0].get("Collection") or []]
        check("a member row carries the collection as a tag",
              "plexsim collection" in tags, ",".join(tags))
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
    for axis in ("genre", "year", "decade", "contentRating", "director", "country",
                 "studio", "collection", "actor", "writer", "label"):
        status, listing, _ = get(base, "/library/sections/%s/%s" % (movie_section, axis))
        entries = listing.get("Directory") or []
        ok = status == 200 and listing.get("viewGroup") == "secondary"
        detail = "%d entry(ies)" % len(entries)
        if entries:
            # Every entry has to open a list that is actually filtered, which
            # only works if the key matches what the listing reads back.
            _, filtered, _ = get(base, entries[0]["fastKey"])
            rows = filtered.get("Metadata") or []
            ok = ok and bool(rows) and len(rows) <= len(movies)
            detail += ", first opens %d row(s)" % len(rows)
        check("By %s lists and filters" % axis, ok, detail)
    status, folders, _ = get(base, "/library/sections/%s/folder" % movie_section)
    check("By Folder lists folders with the librarySection triple",
          status == 200 and folders.get("Metadata")
          and folders.get("librarySectionUUID"),
          "%d folder(s)" % len(folders.get("Metadata") or []))

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
