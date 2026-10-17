#!/usr/bin/env python3
"""Record what a real claimed PMS answers a shared user next to its owner
(`tests/fixtures/shared_user_s1434.json`).

    OWNER_TOKEN=... SHARED_TOKEN=... python3 probes/record_shared_user.py \
        --base http://LAB_IP:32400 --machine MACHINE_ID --shared-id ID --out FILE.json

`OWNER_TOKEN` is one of the owner's device tokens, `SHARED_TOKEN` the `accessToken` plex.tv's
`/api/v2/resources` gives the shared user for this server (its own account token is refused).
Every write is a shared user's scrobble, progress, rating, playlist or play, undone at the
end: the scrobble and progress unscrobbled, the rating cleared, the playlist deleted, the
history rows it left deleted by the owner. Tokens are written as `<OWNER>` and `<SHARED>`.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

LIB = "com.plexapp.plugins.library"
CLIENT = {
    "X-Plex-Client-Identifier": "rex-shared-user-probe",
    "X-Plex-Product": "Plex Web",
    "X-Plex-Version": "4.160.0",
    "X-Plex-Platform": "Chrome",
    "X-Plex-Device": "Linux",
    "X-Plex-Device-Name": "Chrome",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--movie", default="1", help="a movie the share shows")
    parser.add_argument("--other", default="3", help="another movie the share shows")
    parser.add_argument("--shared-id", required=True, help="the shared user's plex.tv id")
    args = parser.parse_args()
    tokens = {"owner": os.environ["OWNER_TOKEN"], "shared": os.environ["SHARED_TOKEN"]}
    names = {tokens["owner"]: "<OWNER>", tokens["shared"]: "<SHARED>"}
    exchanges = []

    def redact(text):
        for token, name in names.items():
            text = text.replace(token, name)
        return text

    def ask(who, method, path, query=(), note=""):
        pairs = list(query) + [("X-Plex-Token", tokens[who])]
        url = args.base + path + "?" + urllib.parse.urlencode(pairs)
        request = urllib.request.Request(url, method=method, headers=dict(CLIENT, Accept="application/xml"))
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        text = body.decode("utf-8", "replace")
        exchanges.append({
            "who": who, "method": method, "path": path, "query": [list(p) for p in query],
            "note": note, "status": status, "body": redact(text),
        })
        print(who, method, path, dict(query), status, len(body))
        return status, text

    def both(method, path, query=(), note=""):
        for who in ("owner", "shared"):
            ask(who, method, path, query, note)

    movie, other = args.movie, args.other
    for path, query in [
        ("/", ()),
        ("/library/sections", ()),
        ("/library/sections/1/all", ()),
        ("/library/sections/2/all", ()),
        ("/library/sections/1/all", (("unwatched", "1"),)),
        ("/library/metadata/1", ()),
        ("/library/metadata/3", ()),
        ("/library/metadata/17", ()),
        ("/library/metadata/18", ()),
        ("/library/metadata/6", ()),
        ("/library/metadata/6/children", ()),
        ("/library/metadata/8", ()),
        ("/hubs", ()),
        ("/hubs/home/continueWatching", ()),
        ("/hubs/home/onDeck", ()),
        ("/library/onDeck", ()),
        ("/hubs/search", (("query", "Firefly"),)),
        ("/hubs/search", (("query", "Elephants"),)),
        ("/hubs/search", (("query", "Sintel"),)),
        ("/accounts", ()),
        ("/accounts/1", ()),
        ("/accounts/" + args.shared_id, ()),
        ("/home/users", ()),
        ("/myplex/account", ()),
        ("/status/sessions", ()),
        ("/status/sessions/history/all", ()),
        ("/playlists", ()),
        ("/media/providers", ()),
        ("/downloadQueue", ()),
    ]:
        both("GET", path, query, "read before any write")

    # Which routes a shared user is refused, by status alone. The writes change nothing
    # when admitted: each sets what is already there, or names a row that does not exist.
    for method, path, query in [
        ("GET", "/:/prefs", ()), ("GET", "/butler", ()), ("GET", "/devices", ()),
        ("GET", "/system", ()), ("GET", "/diagnostics/logs", ()), ("GET", "/updater/status", ()),
        ("GET", "/status/sessions/background", ()), ("GET", "/media/grabbers", ()),
        ("GET", "/livetv/dvrs", ()), ("GET", "/channels/all", ()), ("GET", "/statistics/bandwidth", ()),
        ("GET", "/statistics/media", ()), ("GET", "/activities", ()), ("GET", "/clients", ()),
        ("GET", "/transcode/sessions", ()), ("GET", "/library/sections/1/prefs", ()),
        ("GET", "/library/metadata/1/matches", ()), ("GET", "/library/sections/2", ()),
        ("GET", "/hubs/sections/2", ()), ("GET", "/library/sections/2/filters", ()),
        ("GET", "/library/all", ()), ("GET", "/library/recentlyAdded", ()),
        ("PUT", "/:/prefs", (("FriendlyName", "plexlab"),)),
        ("PUT", "/library/metadata/" + movie, (("type", "1"), ("id", movie), ("summary.locked", "0"))),
        ("DELETE", "/library/metadata/999999", ()),
        ("PUT", "/library/sections/1/refresh", ()),
    ]:
        status, _ = ask("shared", method, path, query, "refusal survey")
        exchanges[-1]["body"] = exchanges[-1]["body"][:200]

    key = ("key", movie), ("identifier", LIB)
    ask("shared", "GET", "/:/scrobble", key, "shared user marks the movie watched")
    both("GET", "/library/metadata/" + movie, (), "after the shared scrobble")
    both("GET", "/library/sections/1/all", (("unwatched", "1"),), "after the shared scrobble")
    both("GET", "/status/sessions/history/all", (), "after the shared scrobble")
    both("GET", "/status/sessions/history/all", (("accountID", "1"),), "filtered to the owner")

    ask("shared", "GET", "/:/progress",
        (("key", other), ("identifier", LIB), ("time", "600"), ("state", "stopped")),
        "shared user leaves the other movie part-watched")
    both("GET", "/library/metadata/" + other, (), "after the shared progress")
    both("GET", "/hubs/home/continueWatching", (), "after the shared progress")

    ask("shared", "PUT", "/:/rate", key + (("rating", "8"),), "shared user rates the movie")
    both("GET", "/library/metadata/" + movie, (), "after the shared rating")

    uri = "server://%s/%s/library/metadata/%s" % (args.machine, LIB, movie)
    status, text = ask("shared", "POST", "/playlists",
                       (("type", "video"), ("title", "rex-probe"), ("smart", "0"), ("uri", uri)),
                       "shared user makes a playlist")
    both("GET", "/playlists", (), "after the shared playlist")
    import re
    made = re.search(r'ratingKey="(\d+)"', text)

    session = (("X-Plex-Session-Identifier", "rex-probe-session"),)
    timeline = key[:1] + (("ratingKey", movie), ("state", "playing"), ("time", "1000"),
                           ("duration", "10000"), ("hasMDE", "1")) + session
    ask("shared", "GET", "/:/timeline", timeline, "shared user plays")
    both("GET", "/status/sessions", (), "while the shared user plays")
    def at(state, time_ms, rating=movie):
        return (("key", rating), ("ratingKey", rating), ("state", state), ("time", str(time_ms)),
                ("duration", "10031"), ("hasMDE", "1")) + session
    ask("shared", "GET", "/:/timeline", at("stopped", 1000), "shared user stops")
    # The lab's clips are ten seconds long: half-way is part-watched, the last 100 ms watched.
    ask("shared", "GET", "/:/timeline", at("playing", 5000, other), "shared user plays the other movie")
    ask("shared", "GET", "/:/timeline", at("stopped", 5000, other), "and stops half-way")
    both("GET", "/hubs/home/continueWatching", (), "after the shared user stopped half-way")
    both("GET", "/library/onDeck", (), "after the shared user stopped half-way")
    both("GET", "/library/metadata/" + other, (), "after the shared user stopped half-way")
    ask("shared", "GET", "/:/timeline", at("playing", 9000, other), "shared user plays on")
    ask("shared", "GET", "/:/timeline", at("stopped", 9950, other), "to the end")
    both("GET", "/status/sessions/history/all", (), "after the shared user played to the end")
    both("GET", "/status/sessions/history/all", (("accountID", "1"),), "filtered to the owner")
    both("GET", "/status/sessions/history/all", (("accountID", args.shared_id),), "filtered to the shared user")
    both("GET", "/library/metadata/" + other, (), "after the shared user played to the end")

    status, text = ask("shared", "POST", "/downloadQueue", (), "shared user with allowSync=0 asks for a queue")

    # Undo, then read back what is left.
    if made:
        ask("shared", "DELETE", "/playlists/" + made.group(1), (), "undo the playlist")
    ask("shared", "PUT", "/:/rate", key + (("rating", "-1"),), "undo the rating")
    ask("shared", "GET", "/:/unscrobble", key, "undo the scrobble")
    ask("shared", "GET", "/:/unscrobble", (("key", other), ("identifier", LIB)), "undo the progress")
    status, text = ask("owner", "GET", "/status/sessions/history/all", (), "history before cleanup")
    for row in re.findall(r'historyKey="/status/sessions/history/(\d+)"[^>]*accountID="(\d+)"', text):
        if row[1] != "1":
            ask("owner", "DELETE", "/status/sessions/history/" + row[0], (), "undo the shared history row")
    both("GET", "/library/metadata/" + movie, (), "after the undo")

    json.dump({
        "source": "plexlab, a claimed Plex Media Server 1.43.4.10903, shared with a managed Home user "
                  "(Movies only, allowSync=0, filterMovies=contentRating=G,PG); recorded by "
                  "probes/record_shared_user.py",
        "captured": time.strftime("%Y-%m-%d"),
        "exchanges": exchanges,
    }, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
