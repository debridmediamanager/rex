#!/usr/bin/env python3
"""Replay every server request the Plex Web UI can make, against any server.

The request list is not invented. It is the set of server endpoints reachable
from the official web client's own code -- `webclient/js` (Plex Web 4.147.1, the
bundle rex serves at /web) -- with the parameter sets that code builds:

  * the item context menu, `main-*.js` module 92273: one flag list per item
    type (movie/show/season/episode/collection/playlist) plus the predicate that
    gates each entry, and module 33211: the action -> endpoint map.
  * the server settings tree, `chunk-1439-*.js`: Status (Dashboard, Alerts,
    Conversions), Settings (General, Remote Access, Agents, Library, Network,
    Transcoder, Languages, DLNA, Scheduled Tasks, Extras) and Manage
    (Libraries, Optimized Versions, Live TV & DVR, Troubleshooting, Console,
    Plugins).

Run it against a real PMS first -- that is the measurement -- then against
rex and diff the two tables:

    python3 probes/ui_surface.py --base http://kids:32400 --token $TOK > ref.txt
    python3 probes/ui_surface.py --base http://127.0.0.1:32400 > sim.txt
    diff -u ref.txt sim.txt

`--mutate` adds the write half (refresh, analyze, edit, playlist and collection
CRUD, scrobble, rate, delete). Run that only against a server whose library you
are willing to change: PUT .../merge?ids= on a real library MERGES TWO ITEMS and
the split that undoes it does not restore the original ratingKey.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# A real client always sends these. A bare urllib request measures something
# else: Cloudflare 403s `Python-urllib/*` on the media path, and a PMS refuses
# POST /playQueues without X-Plex-Client-Identifier.
HEADERS = {
    "User-Agent": "Plex Web/4.147.1 (rex ui_surface probe)",
    "X-Plex-Client-Identifier": "rex-ui-surface",
    "X-Plex-Product": "Plex Web",
    "X-Plex-Version": "4.147.1",
    "X-Plex-Platform": "Chrome",
    "X-Plex-Device": "OSX",
    "Accept": "application/json",
}


def request(base, token, method, path, timeout=30):
    url = base.rstrip("/") + path
    if token:
        url += ("&" if "?" in path else "?") + "X-Plex-Token=" + token
    req = urllib.request.Request(url, method=method, headers=dict(HEADERS))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()
    except Exception as error:  # a refused connection is a result too
        return None, "", ("%s: %s" % (type(error).__name__, error)).encode()


def shape(value, depth=0):
    """Key names and child counts -- not values, which differ per server."""
    if isinstance(value, dict):
        return "{" + ",".join(
            "%s:%s" % (k, shape(v, depth + 1) if depth < 1 else _kind(v))
            for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[%d%s]" % (len(value), (" " + shape(value[0], depth + 1)) if value and depth < 1 else "")
    return _kind(value)


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, list):
        return "[%d]" % len(value)
    if isinstance(value, dict):
        return "{%d}" % len(value)
    return "str"


def summarise(status, content_type, body):
    if body[:1] in (b"{", b"["):
        try:
            document = json.loads(body)
        except ValueError:
            return "unparseable json"
        if isinstance(document, dict) and len(document) == 1:
            root = next(iter(document))
            return root + " " + shape(document[root])
        return shape(document)
    if not body:
        return "empty body  (%s)" % (content_type.split(";")[0] or "no content-type")
    if body[:5] == b"<?xml" or body[:1] == b"<":
        return "%s %d bytes" % (content_type.split(";")[0], len(body))
    return "%s %d bytes" % (content_type.split(";")[0] or "binary", len(body))


def discover(base, token):
    """The target's own ratingKeys. The corpus has to be built per server."""
    found = {"movie": None, "show": None, "season": None, "episode": None,
             "section_movie": None, "section_show": None, "part": None}
    status, _, body = request(base, token, "GET", "/library/sections")
    sections = json.loads(body)["MediaContainer"].get("Directory", []) if status == 200 else []
    for section in sections:
        key = "section_%s" % section.get("type")
        if key in found and found[key] is None:
            found[key] = section["key"]
    for section in sections:
        status, _, body = request(
            base, token, "GET", "/library/sections/%s/all" % section["key"])
        if status != 200:
            continue
        for row in json.loads(body)["MediaContainer"].get("Metadata", []):
            kind = row.get("type")
            if kind in found and found[kind] is None:
                found[kind] = row["ratingKey"]
            if found["part"] is None:
                for media in row.get("Media", []):
                    for part in media.get("Part", []):
                        found["part"] = part.get("id")
                        break
            if kind == "show" and found["season"] is None:
                status, _, body2 = request(
                    base, token, "GET", "/library/metadata/%s/children" % row["ratingKey"])
                if status != 200:
                    continue
                for season in json.loads(body2)["MediaContainer"].get("Metadata", []):
                    found["season"] = season["ratingKey"]
                    status, _, body3 = request(
                        base, token, "GET",
                        "/library/metadata/%s/children" % season["ratingKey"])
                    if status == 200:
                        for episode in json.loads(body3)["MediaContainer"].get("Metadata", []):
                            found["episode"] = episode["ratingKey"]
                            for media in episode.get("Media", []):
                                for part in media.get("Part", []):
                                    found["part"] = part.get("id")
                                    break
                            break
                    break
    return found


def machine_identifier(base, token):
    status, _, body = request(base, token, "GET", "/identity")
    if status == 200 and body[:1] == b"{":
        return json.loads(body)["MediaContainer"].get("machineIdentifier", "")
    return ""


def uri_for(machine, rating_key):
    """The source uri shape the client builds for a universal play queue."""
    return "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, rating_key)


def steps(ids, machine, mutate):
    """(group, method, path) in the order a UI walk produces them."""
    movie, show, season = ids["movie"], ids["show"], ids["season"]
    episode, part = ids["episode"], ids["part"]
    movie_section, show_section = ids["section_movie"], ids["section_show"]
    quote = urllib.parse.quote

    plan = [
        # -- what the client polls the moment it connects ------------------
        ("connect", "GET", "/identity"),
        ("connect", "GET", "/media/providers"),
        ("connect", "GET", "/myplex/account"),
        ("connect", "GET", "/accounts"),
        ("connect", "GET", "/accounts/1"),
        ("connect", "GET", "/:/prefs"),
        ("connect", "GET", "/activities"),
        ("connect", "GET", "/status/sessions"),
        ("connect", "GET", "/status/sessions/background"),
        ("connect", "GET", "/updater/status"),
        ("connect", "GET", "/hubs"),
        ("connect", "GET", "/hubs/promoted"),
        ("connect", "GET", "/hubs/continueWatching"),
        ("connect", "GET", "/hubs/home/recentlyAdded"),
        ("connect", "GET", "/library/sections"),
        ("connect", "GET", "/library/shared/all"),
        ("connect", "GET", "/playlists"),
        # -- the Status pages ---------------------------------------------
        ("status", "GET", "/statistics/bandwidth"),
        ("status", "GET", "/statistics/media"),
        ("status", "GET", "/statistics/resources"),
        ("status", "GET", "/status/sessions/history/all"),
        ("status", "GET", "/transcode/sessions"),
        ("status", "GET", "/clients"),
        ("status", "GET", "/devices"),
        # -- the Settings pages ------------------------------------------
        ("settings", "GET", "/system/agents"),
        ("settings", "GET", "/system/agents?mediaType=1&language=en-US"),
        ("settings", "GET", "/security/token?type=delegation&scope=all"),
        ("settings", "GET", "/myplex/refreshReachability"),
        ("settings", "GET", "/diagnostics/logs"),
        # -- Manage > Libraries ------------------------------------------
        ("manage", "GET", "/library/sections/%s/prefs" % movie_section),
        ("manage", "GET", "/library/sections/prefs?type=movie"),
        # The Add Library form: shaped by the library TYPE and the agent together.
        ("manage", "GET", "/library/sections/prefs?type=1"),
        ("manage", "GET", "/library/sections/prefs?type=2&agent=tv.plex.agents.series"),
        ("manage", "GET", "/library/sections/prefs?type=99"),
        ("manage", "GET", "/library/sections/%s/prefs?agent=tv.plex.agents.none" % movie_section),
        ("manage", "GET", "/system/scanners/%s" % movie_section),
        # Scanners are keyed by TYPE: 13 is photos on any server, 99 is a 500.
        ("manage", "GET", "/system/scanners/13"),
        ("manage", "GET", "/system/scanners/99"),
        ("settings", "GET", "/system/agents?mediaType=1"),
        ("settings", "GET", "/system/agents?mediaType=abc"),
        ("settings", "GET", "/system/agents/tv.plex.agents.movie/config/1"),
        ("settings", "GET", "/system/agents/tv.plex.agents.movie/config/99"),
        ("settings", "GET", "/system/agents/contributors?mediaType=1"
                            "&primaryAgent=tv.plex.agents.movie"),
        ("settings", "GET", "/system/agents/attribution?identifier=tv.plex.agents.movie"),
        ("settings", "GET", "/:/plugins/tv.plex.agents.movie/prefs"),
        ("settings", "GET", "/services/browse"),
        ("settings", "GET", "/services/browse/Lw==?includeFiles=1"),
        ("status", "GET", "/devices/1"),
        ("status", "GET", "/devices/abc"),
        ("connect", "GET", "/channels/all"),
        ("connect", "GET", "/log"),
        ("browse", "GET", "/library/tags?type=1"),
        ("browse", "GET", "/library/tags"),
        ("browse", "GET", "/library/sections/%s/common?type=1" % movie_section),
        ("browse", "GET", "/library/sections/%s/sorts?includeAdvanced=1" % movie_section),
        ("manage", "GET", "/library/optimize"),
        ("manage", "GET", "/playlists/generators?type=42"),
        ("manage", "GET", "/media/subscriptions"),
        ("manage", "GET", "/media/grabbers/devices"),
        ("manage", "GET", "/sync/transcodeQueue"),
        # -- the library grid, with the client's own paging --------------
        ("browse", "GET", "/library/sections/%s/all?includeMeta=1"
                          "&X-Plex-Container-Start=0&X-Plex-Container-Size=2" % movie_section),
        ("browse", "GET", "/library/sections/%s/firstCharacter" % movie_section),
        ("browse", "GET", "/library/sections/%s/sorts" % movie_section),
        ("browse", "GET", "/library/sections/%s/filters" % movie_section),
        # The Edit Filters dialog reading a saved filter back. Every shape the
        # grammar has is exercised here, because the root is `Query` rather than a
        # MediaContainer and the error blocks are reported, not raised.
        ("browse", "GET", "/library/sections/%s/query/parse?type=1&genre=6"
                          "&sort=titleSort" % movie_section),
        ("browse", "GET", "/library/sections/%s/query/parse?type=1&push=1&genre=6"
                          "&or=1&genre=27&pop=1&and=1&year%%3E%%3E=2000"
                          "&sort=year:desc,titleSort&group=decade&limit=7"
                          "&bogus=1" % movie_section),
        ("browse", "GET", "/library/sections/%s/query/parse" % movie_section),
        # A filtered "Play all": the URI carries the grid's own query, which a
        # server that resolves only the section path silently widens to everything.
        ("browse", "GET", "/library/sections/%s/all?type=1&unwatched=1&sort=titleSort"
                          % movie_section),
        # `group=` collapses rather than filters, and refuses anything that is not
        # a scalar column of the metadata row.
        ("browse", "GET", "/library/sections/%s/all?type=1&group=studio" % movie_section),
        ("browse", "GET", "/library/sections/%s/all?type=1&group=genre" % movie_section),
        ("browse", "GET", "/library/sections/%s/genre" % movie_section),
        ("browse", "GET", "/library/sections/%s/year" % movie_section),
        ("browse", "GET", "/library/sections/%s/decade" % movie_section),
        ("browse", "GET", "/library/sections/%s/contentRating" % movie_section),
        ("browse", "GET", "/library/sections/%s/director" % movie_section),
        ("browse", "GET", "/library/sections/%s/folder" % movie_section),
        # The media axes, read off the Media rather than a tag, plus the two that
        # are not secondary containers at all: `location` carries only size and
        # title2, and `unwatched` is a listing titled "Unplayed".
        ("browse", "GET", "/library/sections/%s/resolution" % movie_section),
        ("browse", "GET", "/library/sections/%s/videoCodec" % movie_section),
        ("browse", "GET", "/library/sections/%s/audioCodec" % movie_section),
        ("browse", "GET", "/library/sections/%s/subtitleCodec" % movie_section),
        ("browse", "GET", "/library/sections/%s/audioLayout" % movie_section),
        ("browse", "GET", "/library/sections/%s/audioLanguage" % movie_section),
        ("browse", "GET", "/library/sections/%s/subtitleLanguage" % movie_section),
        ("browse", "GET", "/library/sections/%s/editionTitle" % movie_section),
        ("browse", "GET", "/library/sections/%s/location" % movie_section),
        ("browse", "GET", "/library/sections/%s/unwatched" % movie_section),
        # Advertised in /filters and 404 on the reference itself -- the probe
        # records that, so a rex that starts answering them shows up.
        ("browse", "GET", "/library/sections/%s/hdr" % movie_section),
        ("browse", "GET", "/library/sections/%s/inProgress" % movie_section),
        ("browse", "GET", "/library/sections/%s/unmatched" % movie_section),
        # An axis is scoped by the requested type: a movie library asked for
        # shows lists nothing.
        ("browse", "GET", "/library/sections/%s/genre?type=2" % movie_section),
        ("browse", "GET", "/library/sections/%s/newest" % movie_section),
        ("browse", "GET", "/library/sections/%s/recentlyAdded" % movie_section),
        ("browse", "GET", "/library/sections/%s/onDeck" % movie_section),
        ("browse", "GET", "/library/sections/%s/collections" % movie_section),
        ("browse", "GET", "/library/tags?type=2&sectionId=%s" % movie_section),
        ("browse", "GET", "/hubs/sections/%s" % movie_section),
        # `count=` caps each shelf and sets `more` on the ones it cut, which is
        # what draws the "see all" chevron. The client asks for count+1.
        ("browse", "GET", "/hubs/sections/%s?count=1" % movie_section),
        ("browse", "GET", "/hubs?count=13"),
        # The Top shelf counts PLAYS, and needs a type: a real PMS 400s without one.
        ("browse", "GET", "/library/all/top?type=1&limit=5"),
        ("browse", "GET", "/library/all/top"),
        ("browse", "GET", "/hubs/search?query=a"),
        # The Home screen is one call per pinned library, and each shelf carries
        # key/context/style/promoted plus a hubKey naming its own rows.
        ("browse", "GET", "/hubs/promoted?contentDirectoryID=%s"
                          "&pinnedContentDirectoryID=%s" % (movie_section, movie_section)),
        ("browse", "GET", "/hubs/promoted"),
        ("browse", "GET", "/hubs/continueWatching?contentDirectoryID=%s" % movie_section),
        ("browse", "GET", "/hubs/continueWatching/items"),
        ("browse", "GET", "/hubs/home/continueWatching"),
        ("browse", "GET", "/hubs/home/onDeck"),
        ("browse", "GET", "/hubs/home/recentlyAdded?type=1"),
        ("browse", "GET", "/hubs/home/nosuchhub"),
        # The other three search surfaces, and the in-library search box.
        ("browse", "GET", "/library/search?query=a&limit=30"),
        ("browse", "GET", "/search?query=a"),
        ("browse", "GET", "/hubs/search/voice?query=a"),
        ("browse", "GET", "/library/sections/%s/search?type=1&query=a" % movie_section),
        ("browse", "GET", "/library/search?query=a"),
    ]

    if show_section:
        plan += [
            ("browse", "GET", "/library/sections/%s/all?type=2&includeMeta=1" % show_section),
            ("browse", "GET", "/library/sections/%s/prefs" % show_section),
            # A show library's /filters is a DIFFERENT list, and its axis keys
            # carry ?type=2 -- except country, unmatched and editionTitle.
            ("browse", "GET", "/library/sections/%s/filters" % show_section),
            ("browse", "GET", "/library/sections/%s/genre?type=2" % show_section),
            ("browse", "GET", "/library/sections/%s/network?type=2" % show_section),
            ("browse", "GET", "/library/sections/%s/country" % show_section),
            ("browse", "GET", "/library/sections/%s/editionTitle" % show_section),
            ("browse", "GET", "/library/sections/%s/unwatchedLeaves?type=2"
                              % show_section),
            # A show library advertises its filter fields PREFIXED, so this is
            # the only place the scope echo and the per-type titles show up.
            ("browse", "GET", "/library/sections/%s/query/parse?type=4"
                              "&episode.unwatched=1&unwatched=1" % show_section),
            ("browse", "GET", "/library/sections/%s/all?type=2&show.title=x"
                              % show_section),
        ]

    # -- the item pages, each with the params the client actually sends ---
    # Each include/exclude option on its own, because their effects are separable
    # and each one rex ignored showed up as a valid document with a block
    # missing -- not as an error.
    options = ("includeChildren=1", "includeRelated=1", "includePreferences=1",
               "excludeFields=summary", "excludeElements=Media", "checkFiles=1",
               "asyncCheckFiles=1", "asyncRefreshLocalMediaAgent=1")
    detail = ("?includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1"
              "&includePreferences=1&includeReviews=1&includeChapters=1&includeStations=1"
              "&includeExternalMedia=1")
    for label, key in (("movie", movie), ("show", show), ("season", season), ("episode", episode)):
        if not key:
            continue
        plan += [("item:" + label, "GET", "/library/metadata/%s?%s" % (key, option))
                 for option in options]
        plan += [
            ("item:" + label, "GET", "/library/metadata/%s%s" % (key, detail)),
            ("item:" + label, "GET", "/library/metadata/%s/related?includeAugmentations=1" % key),
            ("item:" + label, "GET", "/library/metadata/%s/similar" % key),
            # The preplay page's Related shelf: a DIFFERENT path from
            # /library/metadata/{id}/related, and only this one has the hubs.
            ("item:" + label, "GET", "/hubs/metadata/%s/related?includeExternalMedia=1"
                                     "&includeExternalMetadata=1"
                                     "&asyncAugmentMetadata=1" % key),
            ("item:" + label, "GET", "/hubs/metadata/%s/postplay" % key),
            ("item:" + label, "GET", "/library/metadata/%s/extras" % key),
            ("item:" + label, "GET", "/library/metadata/%s/userState" % key),
            ("item:" + label, "GET", "/library/metadata/%s/tree" % key),
            ("item:" + label, "GET", "/library/metadata/%s/posters" % key),
            ("item:" + label, "GET", "/library/metadata/%s/arts" % key),
        ]
    if show:
        plan += [
            ("item:show", "GET", "/library/metadata/%s/children?excludeAllLeaves=1"
                                 "&X-Plex-Container-Start=0&X-Plex-Container-Size=20" % show),
            ("item:show", "GET", "/library/metadata/%s/allLeaves" % show),
        ]

    # -- context menu: the read half ------------------------------------
    if movie:
        plan += [
            ("menu:fix match", "GET",
             "/library/metadata/%s/matches?manual=0&agent=tv.plex.agents.movie" % movie),
            ("menu:fix match", "GET",
             "/library/metadata/%s/matches?manual=1&title=%s&year=2008"
             "&agent=tv.plex.agents.movie&language=en-US" % (movie, quote("Big Buck Bunny"))),
        ]
    if part:
        plan += [("menu:download", "HEAD", "/library/parts/%s/file.mkv?download=1" % part)]

    if not mutate:
        return plan

    # -- context menu: the write half -----------------------------------
    #
    # Deliberately no `merge`: PUT /library/metadata/{id}/merge?ids= on a real
    # library merges the two items for good, and the `split` that undoes it
    # leaves the second one on a NEW ratingKey with the first one's metadata.
    plan += [
        ("menu:mark played", "GET",
         "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % movie),
        ("menu:mark unplayed", "GET",
         "/:/unscrobble?key=%s&identifier=com.plexapp.plugins.library" % movie),
        ("menu:rate", "GET",
         "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=8" % movie),
        ("menu:rate", "GET",
         "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=-1" % movie),
        ("menu:refresh", "PUT", "/library/metadata/%s/refresh" % movie),
        ("menu:analyze", "PUT", "/library/metadata/%s/analyze" % movie),
        ("menu:edit", "PUT",
         "/library/metadata/%s?titleSort.value=%s&titleSort.locked=1"
         % (movie, quote("rex probe"))),
        ("menu:edit", "PUT", "/library/metadata/%s?titleSort.value=&titleSort.locked=0" % movie),
        ("menu:split apart", "PUT", "/library/metadata/%s/split" % movie),
        ("menu:remove from cw", "PUT", "/actions/removeFromContinueWatching?ratingKey=%s" % movie),
        ("timeline", "GET",
         "/:/timeline?ratingKey=%s&key=%s&state=playing&time=1000&duration=10000"
         "&identifier=com.plexapp.plugins.library"
         % (movie, quote("/library/metadata/%s" % movie))),
    ]
    if part:
        plan += [("menu:select stream", "PUT",
                  "/library/parts/%s?subtitleStreamID=0&allParts=1" % part)]
    plan += [
        # A write, so it lives behind --mutate: it sat in the read-only plan and sent
        # a PUT to whatever server a "read-only" run was pointed at.
        ("connect", "PUT", "/updater/check"),
        ("playqueue", "POST", "/playQueues?type=video&shuffle=0&repeat=0&own=1&uri=%s"
                              % quote(uri_for(machine, movie), safe="")),
        ("playlist", "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                             % (quote("rex probe"), quote(uri_for(machine, movie), safe=""))),
        ("collection", "POST", "/library/collections?type=1&title=%s&smart=0&sectionId=%s&uri=%s"
                               % (quote("rex probe"), movie_section,
                                  quote(uri_for(machine, movie), safe=""))),
        ("library", "PUT", "/library/sections/%s/emptyTrash" % movie_section),
        ("library", "PUT", "/library/sections/%s/analyze" % movie_section),
        ("library", "GET", "/library/sections/%s/refresh" % movie_section),
        ("library", "DELETE", "/library/sections/%s/refresh" % movie_section),
        ("library", "DELETE", "/library/sections/%s/indexes" % movie_section),
        ("library", "PUT", "/library/clean/bundles"),
        ("library", "PUT", "/library/optimize"),
        ("prefs", "PUT", "/:/prefs?FriendlyName="),
    ]
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:32400")
    parser.add_argument("--token", default=os.environ.get("PLEX_TOKEN", ""))
    parser.add_argument("--mutate", action="store_true",
                        help="also run the write half; changes the target's library")
    parser.add_argument("--dump", default=None, help="directory to write every response body to")
    args = parser.parse_args()

    machine = machine_identifier(args.base, args.token)
    ids = discover(args.base, args.token)
    print("# %s  machine=%s" % (args.base, machine))
    print("# ids: " + "  ".join("%s=%s" % (k, v) for k, v in sorted(ids.items())))
    if not ids["movie"]:
        print("no items found -- cannot build the corpus", file=sys.stderr)
        return 1
    if args.dump:
        os.makedirs(args.dump, exist_ok=True)

    group = None
    followups = []
    for kind, method, path in steps(ids, machine, args.mutate):
        if kind != group:
            group = kind
            print("\n## %s" % group)
        status, content_type, body = request(args.base, args.token, method, path)
        print("%-6s %-72s %-4s %s"
              % (method, path[:72], status, summarise(status, content_type, body)))
        if args.dump:
            name = ("%s_%s" % (method, path)).replace("/", "_").replace("?", "~")[:120]
            with open(os.path.join(args.dump, name), "wb") as handle:
                handle.write(body)
        # A created playlist/collection is only measurable through its own id.
        if method == "POST" and status in (200, 201) and body[:1] == b"{":
            rows = json.loads(body)["MediaContainer"].get("Metadata") or []
            if rows and path.startswith("/playlists"):
                followups.append(("playlist", rows[0]["ratingKey"]))
            elif rows and path.startswith("/library/collections"):
                followups.append(("collection", rows[0]["ratingKey"]))

    for kind, key in followups:
        print("\n## %s:%s" % (kind, key))
        if kind == "playlist":
            tail = [("GET", "/playlists/%s" % key),
                    ("GET", "/playlists/%s/items" % key),
                    ("PUT", "/playlists/%s/items?uri=%s"
                            % (key, urllib.parse.quote(uri_for(machine, ids["movie"]), safe=""))),
                    ("PUT", "/playlists/%s?title=rex+probe+2" % key),
                    ("DELETE", "/playlists/%s/items/1" % key),
                    ("DELETE", "/playlists/%s" % key)]
        else:
            tail = [("GET", "/library/collections/%s" % key),
                    ("GET", "/library/collections/%s/children" % key),
                    ("PUT", "/library/collections/%s?title=rex+probe+2" % key),
                    ("DELETE", "/library/collections/%s" % key)]
        for method, path in tail:
            status, content_type, body = request(args.base, args.token, method, path)
            print("%-6s %-72s %-4s %s"
                  % (method, path[:72], status, summarise(status, content_type, body)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
