"""Each account's requests answered as a real PMS answers them, replayed from plexlab.

plexlab, a claimed PMS 1.43.4, was shared with a managed Home user and asked the same
requests with the owner's token and the user's (`fixtures/shared_user_s1434.json`: Movies
only, allowSync=0, contentRating=G,PG; `fixtures/shared_user_label_s1434.json`: allowSync=1,
label!=Rexhide; recorded by `probes/record_shared_user.py`). This builds rex a library with
plexlab's keys, ratings and labels and the owner's watch state as plexlab held it, claims it
against a stub plex.tv answering the access-token list plexlab was given
(`fixtures/plextv_access_tokens_shared_s1434.json`), replays every recorded exchange in order
and checks what is per account: each answer's status, which rows it lists, every row's
watch state and rating, the server's flags, the session's user and the history's account.

    python3 tests/test_accounts.py
"""
import copy
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
MACHINE = "15fad815c00463b33e2e644245763ee42ef84757"
SERVER_TOKEN = "SERVER-TOKEN-ACCOUNTS"
OWNER = "DEVICE-TOKEN-1"
SHARED = "SHARED-TOKEN"
SHARED_ID = "873552796"
LIST = json.load(open(os.path.join(FIXTURES, "plextv_access_tokens_shared_s1434.json")))["body"]
VARIANTS = [
    ("allow-list", "shared_user_s1434.json", LIST),
    ("label", "shared_user_label_s1434.json",
     LIST.replace('filterMovies="contentRating=G%2CPG"', 'filterMovies="label!=Rexhide"')
         .replace('allowSync="0"', 'allowSync="1"')),
]
# plexlab's rows: the films' ratings and the label the second recording hid Sintel by.
MOVIES = [("1", "Big Buck Bunny", "G"), ("3", "Sintel", "PG"), ("4", "Tears of Steel", None),
          ("17", "Elephants Dream", "Not Rated"), ("18", "Cosmos Laundromat", None)]
EPISODES = [("8", "7", 1, 1), ("9", "7", 1, 2), ("10", "7", 1, 3), ("12", "11", 2, 1), ("13", "11", 2, 2),
            ("14", "11", 2, 3)]
STATE = ("viewCount", "userRating")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


class PlexTv(http.server.BaseHTTPRequestHandler):
    tokens = LIST

    def log_message(self, *args):
        pass

    def answer(self):
        path = self.path.split("?")[0]
        if path == "/plex.tv/api/v2/server/access_tokens":
            body = PlexTv.tokens
        elif path == "/servers.plex.tv/api/v2/user":
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<user id="316687974" username="owner"/>\n'
        else:
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="0"/>\n'
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_PUT = do_POST = answer


def media(key):
    return [{"id": int(key) * 10, "duration": 10031, "bitrate": 4999, "width": 1920, "height": 1080,
             "audioChannels": 1, "audioCodec": "aac", "videoCodec": "h264", "videoResolution": "1080",
             "container": "mkv", "Part": [{
                 "id": int(key) * 10, "key": "/library/parts/%d/1789187071/file.mkv" % (int(key) * 10),
                 "duration": 10031, "file": "/data/%s.mkv" % key, "size": 6273654, "container": "mkv",
                 "Stream": [{"id": int(key) * 100 + 1, "streamType": 1, "codec": "h264", "index": 0},
                            {"id": int(key) * 100 + 2, "streamType": 2, "codec": "aac", "index": 1,
                             "selected": True, "languageCode": "eng"}]}]}]


def owner_state(fixture):
    """The owner's watch state per row, as plexlab answered it before any write."""
    state = {}
    for exchange in fixture:
        if exchange["who"] == "owner" and exchange["note"] == "read before any write" \
                and exchange["path"] in ("/library/sections/1/all", "/library/sections/2/all") \
                and not exchange["query"]:
            for element in re.findall(r"<(?:Video|Directory) [^>]*>", exchange["body"]):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', element))
                state[attrs["ratingKey"]] = {name: attrs[name] for name in STATE + ("lastViewedAt",) if name in attrs}
    return state


def catalog(fixture):
    state = owner_state(fixture)
    items = []
    for key, title, rating in MOVIES:
        row = {"ratingKey": key, "key": "/library/metadata/" + key, "guid": "plex://movie/%024d" % int(key),
               "type": "movie", "title": title, "year": 2010, "duration": 10031, "addedAt": 1789187071 + int(key),
               "librarySectionID": 1, "librarySectionTitle": "Movies", "librarySectionKey": "/library/sections/1",
               "Media": media(key)}
        if rating:
            row["contentRating"] = rating
        if key == "3":
            row["Label"] = [{"tag": "Rexhide"}]
        for name, value in state.get(key, {}).items():
            row[name] = float(value) if name == "userRating" else int(value)
        items.append(row)
    show = {"ratingKey": "6", "key": "/library/metadata/6/children", "guid": "plex://show/%024d" % 6,
            "type": "show", "title": "Firefly", "contentRating": "TV-14", "addedAt": 1789187257,
            "librarySectionID": 2, "librarySectionTitle": "TV Shows", "librarySectionKey": "/library/sections/2"}
    items.append(show)
    for season, index in (("7", 1), ("11", 2)):
        items.append({"ratingKey": season, "key": "/library/metadata/%s/children" % season, "type": "season",
                      "title": "Season %d" % index, "index": index, "parentRatingKey": "6",
                      "parentKey": "/library/metadata/6", "parentTitle": "Firefly", "librarySectionID": 2,
                      "librarySectionTitle": "TV Shows", "librarySectionKey": "/library/sections/2",
                      "guid": "plex://season/%024d" % int(season), "addedAt": 1789187257})
    for key, season, season_index, index in EPISODES:
        items.append({"ratingKey": key, "key": "/library/metadata/" + key, "type": "episode", "title": "Episode %d" % index,
                      "index": index, "parentIndex": season_index, "parentRatingKey": season,
                      "parentKey": "/library/metadata/" + season, "grandparentRatingKey": "6",
                      "grandparentKey": "/library/metadata/6", "grandparentTitle": "Firefly",
                      "contentRating": "TV-14", "duration": 10031, "addedAt": 1789187257,
                      "guid": "plex://episode/%024d" % int(key), "librarySectionID": 2,
                      "librarySectionTitle": "TV Shows", "librarySectionKey": "/library/sections/2",
                      "Media": media(key)})
    return {"machineIdentifier": MACHINE, "friendlyName": "plexlab", "sections": [
        {"key": "1", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie",
         "scanner": "Plex Movie", "language": "en-US", "uuid": "e46d000f-bcc4-4b5b-8a7b-1a9d721346ec",
         "Location": [{"id": 1, "path": "/data/movies"}]},
        {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series",
         "scanner": "Plex TV Series", "language": "en-US", "uuid": "d962847e-2c7c-4d45-8355-e45e16287846",
         "Location": [{"id": 2, "path": "/data/shows"}]}], "items": items}


class Server:
    def __init__(self, work, stub_port):
        self.port = free_port()
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:%d" % stub_port)
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN", "TMDB_KEY", "REX_METADATA_URL", "REX_METADATA_KEY"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, "catalog.json"),
                           "--state", os.path.join(work, "rex.db"), "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        poll_until(lambda: self.ask(OWNER, "GET", "/library/sections")[0] == 200
                   and self.ask(SHARED, "GET", "/library/sections")[0] == 200, 40)

    def ask(self, token, method, path, query=()):
        pairs = list(query) + [("X-Plex-Token", token)]
        url = "http://127.0.0.1:%d%s?%s" % (self.port, path, urllib.parse.urlencode(pairs))
        request = urllib.request.Request(url, method=method, headers={
            "Accept": "application/json", "X-Plex-Client-Identifier": "rex-shared-user-probe",
            "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.160.0", "X-Plex-Platform": "Chrome",
            "X-Plex-Device": "Linux", "X-Plex-Device-Name": "Chrome"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else None
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, ConnectionError):
            return 0, None

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=20)
        self.log.close()


def recorded_rows(body):
    """`ratingKey` -> attributes, for every row element of a recorded XML answer."""
    rows = {}
    for element in re.findall(r"<(?:Video|Directory|Playlist) [^>]*>", body):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', element))
        if "ratingKey" in attrs:
            rows.setdefault(attrs["ratingKey"], attrs)
    return rows


def rex_rows(document):
    rows = {}

    def walk(node):
        if isinstance(node, dict):
            if "ratingKey" in node and ("type" in node or "title" in node):
                rows.setdefault(str(node["ratingKey"]), node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(document or {})
    return rows


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def run(variant, name, tokens):
    fixture = json.load(open(os.path.join(FIXTURES, name)))["exchanges"]
    work = tempfile.mkdtemp(prefix="rex-accounts-")
    PlexTv.tokens = tokens
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    json.dump(catalog(fixture), open(os.path.join(work, "catalog.json"), "w"))
    json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner", "email": "owner@example.com",
               "feature_uuids": [], "account_id": 316687974, "view_state_cursor": None, "access_tokens": []},
              open(os.path.join(work, "claim_state.json"), "w"))
    server = Server(work, stub.server_address[1])
    known = {key for key, *_ in MOVIES} | {"6", "7", "11"} | {key for key, *_ in EPISODES}
    ids = {}
    owner_answers = {}
    try:
        for exchange in fixture:
            who, method, path = exchange["who"], exchange["method"], exchange["path"]
            query = [tuple(pair) for pair in exchange["query"]]
            for recorded_id, ours in ids.items():
                path = path.replace("/" + recorded_id, "/" + ours) if path.endswith("/" + recorded_id) else path
            status, document = server.ask(OWNER if who == "owner" else SHARED, method, path, query)
            label = "%s: %s %s %s%s (%s)" % (variant, who, method, path,
                                             "?" + urllib.parse.urlencode(query) if query else "", exchange["note"])
            if method == "POST" and path == "/playlists" and document:
                made = re.search(r'ratingKey="(\d+)"', exchange["body"])
                ours = rex_rows(document)
                if made and ours:
                    ids[made.group(1)] = next(iter(ours))
            if path == "/status/sessions/history/all" and who == "owner" and exchange["note"] == "history before cleanup":
                theirs = [row for row in re.findall(r'historyKey="/status/sessions/history/(\d+)"[^>]*accountID="(\d+)"',
                                                    exchange["body"]) if row[1] != "1"]
                mine = [str(entry.get("historyKey", "")).rsplit("/", 1)[-1]
                        for entry in (document or {}).get("MediaContainer", {}).get("Metadata", [])
                        if str(entry.get("accountID")) != "1"]
                ids.update(zip([row[0] for row in theirs], mine))
            if exchange["note"] == "refusal survey" or status in (403, 404) or exchange["status"] in (403, 404):
                check(label + ": status %d as plexlab's" % exchange["status"], status == exchange["status"], status)
            if status != 200 or exchange["status"] != 200 or not exchange["body"].lstrip().startswith("<"):
                continue
            theirs = {key: attrs for key, attrs in recorded_rows(exchange["body"]).items() if key in known}
            mine = {key: row for key, row in rex_rows(document).items() if key in known}
            listing = exchange["note"] != "refusal survey" and path not in (
                "/library/sections", "/", "/media/providers", "/accounts") and not path.startswith("/accounts/")
            pair = (method, path, tuple(query), exchange["note"])
            if who == "owner":
                owner_answers[pair] = (set(mine), set(theirs))
            # What plexlab hid from the shared user is what rex must hide from rex's own answer
            # to the owner; the rest (search's episode hits, the lab's older plays) is the two
            # servers' libraries, not the accounts.
            if listing and who == "shared" and pair in owner_answers and not path.startswith("/status/sessions/history"):
                rex_owner, lab_owner = owner_answers[pair]
                wanted = sorted((rex_owner | (set(theirs) - lab_owner)) - (lab_owner - set(theirs)))
                check(label + ": the rows plexlab listed, out of the owner's", sorted(mine) == wanted,
                      (sorted(mine), wanted))
            elif listing and path in ("/playlists", "/hubs/home/continueWatching", "/library/onDeck", "/status/sessions"):
                check(label + ": the rows plexlab listed", sorted(mine) == sorted(theirs), (sorted(mine), sorted(theirs)))
            for key in set(mine) & set(theirs):
                for field in STATE:
                    check(label + ": %s %s as plexlab held it" % (key, field),
                          number(mine[key].get(field)) == number(theirs[key].get(field)),
                          (mine[key].get(field), theirs[key].get(field)))
            body = (document or {}).get("MediaContainer", {})
            if path in ("/", "/media/providers"):
                flags = lambda text: dict(re.findall(r'(\w+)="([^"]*)"', re.search(r"<MediaContainer [^>]*>", text).group(0)))
                recorded = flags(exchange["body"])
                if who == "owner":
                    owner_answers[("flags", path)] = (body, recorded)
                elif ("flags", path) in owner_answers:
                    rex_owner, lab_owner = owner_answers[("flags", path)]
                    for flag in ("allowCameraUpload", "allowChannelAccess", "allowMediaDeletion", "allowSync", "allowTuners"):
                        ours = body.get(flag)
                        ours = None if ours is None else str(int(ours))
                        # A flag the share changed is the share's; one it left is rex's own.
                        if recorded.get(flag) == lab_owner.get(flag):
                            wanted = rex_owner.get(flag)
                            wanted = None if wanted is None else str(int(wanted))
                        else:
                            wanted = recorded.get(flag)
                        check(label + ": %s as plexlab's share made it" % flag, ours == wanted, (ours, wanted))
            if path == "/media/providers":
                recorded = re.search(r'<MediaProvider [^>]*protocols="([^"]*)"', exchange["body"]).group(1)
                provider = body.get("MediaProvider", [{}])[0]
                check(label + ": protocols as plexlab's", provider.get("protocols") == recorded,
                      (provider.get("protocols"), recorded))
                kinds = sorted(re.findall(r'<Feature [^>]*type="([^"]*)"', exchange["body"]))
                ours = sorted(feature.get("type") for feature in provider.get("Feature", []))
                for kind in ("manage", "subscribe"):
                    check(label + ": a %s Feature as plexlab's" % kind, (kind in ours) == (kind in kinds), (ours, kinds))
            if path == "/library/sections" and who == "shared":
                recorded = re.findall(r'<Directory allowSync="(\d)"[^>]*key="(\d+)"', exchange["body"])
                ours = [(str(int(directory.get("allowSync", 0))), str(directory.get("key")))
                        for directory in body.get("Directory", [])]
                check(label + ": the libraries and their allowSync as plexlab's", sorted(ours) == sorted(recorded),
                      (ours, recorded))
            if path == "/accounts":
                recorded = sorted(re.findall(r'<Account id="(\d+)"[^>]*name="([^"]+)"', exchange["body"]))
                ours = sorted((str(row["id"]), row.get("name")) for row in body.get("Account", []) if row.get("name"))
                check(label + ": the named accounts as plexlab's", [r[0] for r in ours] == [r[0] for r in recorded]
                      and dict(ours).get(SHARED_ID) == dict(recorded).get(SHARED_ID), (ours, recorded))
            if path == "/status/sessions" and "while" in exchange["note"]:
                user = re.search(r"<User ([^>]*)>", exchange["body"]).group(1)
                user = dict(re.findall(r'(\w+)="([^"]*)"', user))
                ours = (body.get("Metadata") or [{}])[0]
                check(label + ": the session's User as plexlab's",
                      str(ours.get("User", {}).get("id")) == user["id"] and ours.get("User", {}).get("title") == user["title"]
                      and ours.get("User", {}).get("thumb") == user["thumb"], (ours.get("User"), user))
                player = dict(re.findall(r'(\w+)="([^"]*)"', re.search(r"<Player ([^>]*)>", exchange["body"]).group(1)))
                check(label + ": the session's Player userID as plexlab's",
                      str(ours.get("Player", {}).get("userID")) == player["userID"], (ours.get("Player"), player))
            if path == "/status/sessions/history/all":
                recorded = sorted(re.findall(r'ratingKey="(\d+)"[^>]*accountID="(\d+)"', exchange["body"]))
                recorded = [row for row in recorded if row[1] != "1"]
                ours = sorted((str(entry.get("ratingKey")), str(entry.get("accountID")))
                              for entry in body.get("Metadata", []) if str(entry.get("accountID")) != "1")
                check(label + ": the shared user's plays as plexlab listed them", ours == recorded, (ours, recorded))
        # The owner's watch between two of the shared user's requests stays the owner's, and the
        # shared user's own stays theirs (the overlay follows the rows written in between).
        library = ("identifier", "com.plexapp.plugins.library")
        server.ask(SHARED, "GET", "/library/metadata/1")
        server.ask(OWNER, "GET", "/:/scrobble", [("key", "1"), library])
        owner_row = rex_rows(server.ask(OWNER, "GET", "/library/metadata/1")[1]).get("1", {})
        shared_row = rex_rows(server.ask(SHARED, "GET", "/library/metadata/1")[1]).get("1", {})
        check(variant + ": the owner's watch made between the shared user's requests is the owner's alone",
              owner_row.get("viewCount") == 1 and "viewCount" not in shared_row, (owner_row.get("viewCount"), shared_row.get("viewCount")))
        server.ask(SHARED, "GET", "/:/scrobble", [("key", "1"), library])
        server.ask(OWNER, "GET", "/:/unscrobble", [("key", "1"), library])
        owner_row = rex_rows(server.ask(OWNER, "GET", "/library/metadata/1")[1]).get("1", {})
        shared_row = rex_rows(server.ask(SHARED, "GET", "/library/metadata/1")[1]).get("1", {})
        check(variant + ": and the owner's unwatch leaves the shared user's watch",
              "viewCount" not in owner_row and shared_row.get("viewCount") == 1, (owner_row.get("viewCount"), shared_row.get("viewCount")))
        server.ask(SHARED, "GET", "/:/unscrobble", [("key", "1"), library])
        # The native apps ask for XML: the same pruning reaches it.
        url = "http://127.0.0.1:%d/library/sections/1/all?X-Plex-Token=%s" % (server.port, SHARED)
        xml = urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/xml"}), timeout=60).read().decode()
        _, whole = server.ask(SHARED, "GET", "/library/sections/1/all")
        check(variant + ": the shared user's XML listing holds the rows its JSON does",
              sorted(re.findall(r'<Video [^>]*ratingKey="(\d+)"', xml)) == sorted(rex_rows(whole)),
              (re.findall(r'<Video [^>]*ratingKey="(\d+)"', xml), sorted(rex_rows(whole))))
        # A filtered share's pages are full and its totals its own: the filter runs before the
        # page is cut, as it must for a grid of thousands (fun's 3,970 films).
        _, whole = server.ask(SHARED, "GET", "/library/sections/1/all")
        visible = sorted(rex_rows(whole))
        for start in range(len(visible)):
            status, page = server.ask(SHARED, "GET", "/library/sections/1/all",
                                      [("X-Plex-Container-Start", str(start)), ("X-Plex-Container-Size", "1")])
            body = (page or {}).get("MediaContainer", {})
            check(variant + ": the shared user's page %d of 1 row is full, of %d in all" % (start, len(visible)),
                  status == 200 and body.get("size") == 1 and body.get("totalSize") == len(visible), body)
        # The shared user's state outlives a restart, and the owner's rows never took it.
        server.ask(SHARED, "GET", "/:/scrobble", [("key", "1"), library])
        server.ask(SHARED, "PUT", "/:/rate", [("key", "1"), library, ("rating", "7")])
        server.stop()
        server = Server(work, stub.server_address[1])
        status, document = server.ask(OWNER, "GET", "/library/metadata/1")
        row = rex_rows(document).get("1", {})
        check(variant + ": after a restart the owner's row holds none of the shared user's state",
              status == 200 and not any(field in row for field in ("viewCount", "userRating", "viewOffset")), row)
        status, document = server.ask(SHARED, "GET", "/library/metadata/1")
        row = rex_rows(document).get("1", {})
        check(variant + ": and the shared user's watch and rating came back",
              status == 200 and row.get("viewCount") == 1 and number(row.get("userRating")) == 7, row)
        share_gaps(server, variant)
    finally:
        server.stop()
        stub.shutdown()
        if results.count(False) == 0:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("work kept in", work)


def raw(server, method, path, query=(), headers=None, body=None):
    """One request with no token added: (status, headers, text)."""
    url = "http://127.0.0.1:%d%s%s" % (server.port, path, "?" + urllib.parse.urlencode(list(query)) if query else "")
    request = urllib.request.Request(url, method=method, data=body, headers=dict(
        {"Accept": "application/json", "X-Plex-Product": "Plex Web"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode("utf-8", "replace")


def share_gaps(server, variant):
    """What a share must not reach however it spells the request (audit 2026-10-20): every
    route below answered the share as the owner, or let it change the owner's things."""
    _, whole = server.ask(SHARED, "GET", "/library/sections/1/all")
    visible = sorted(rex_rows(whole))
    hidden = [key for key, *_ in MOVIES if key not in visible]
    # A repeated query token: the gate took the last, the account the first, and an unknown
    # first one fell back to the owner.
    plain = server.ask(SHARED, "GET", "/library/sections/2/all")[0]
    doubled = server.ask(SHARED, "GET", "/library/sections/2/all", [("X-Plex-Token", "zz")])[0]
    check(variant + ": a repeated query token answers the share's 403, not the owner's 200",
          plain == 403 and doubled == 403, (plain, doubled))
    # A zero-padded id names the row the route serves, so it is the share's 404 too.
    if hidden:
        part = int(hidden[0]) * 10
        plain = server.ask(SHARED, "GET", "/library/parts/%d/1789187071/file.mkv" % part)[0]
        padded = server.ask(SHARED, "GET", "/library/parts/0%d/1789187071/file.mkv" % part)[0]
        check(variant + ": a hidden row's part read is a 404 however its id is padded",
              plain == 404 and padded == 404, (plain, padded))
    # A comma list of keys is the owner's edit route as one key is.
    status = server.ask(SHARED, "PUT", "/library/metadata/1,4", [("title.value", "shared edit")])[0]
    titles = [rex_rows(server.ask(OWNER, "GET", "/library/metadata/" + key)[1]).get(key, {}).get("title")
              for key in ("1", "4")]
    check(variant + ": a share's edit of a comma list is the owner's 403 and changes nothing",
          status == 403 and "shared edit" not in titles, (status, titles))
    # A collection is the owner's: its metadata alias is refused as a row's is.
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/1" % MACHINE
    _, made = server.ask(OWNER, "POST", "/library/collections", [("type", "1"), ("title", "Owner set"),
                                                                ("smart", "0"), ("sectionId", "1"), ("uri", uri)])
    collection = str(((made or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0].get("ratingKey"))
    edited = server.ask(SHARED, "PUT", "/library/metadata/" + collection, [("title.value", "shared edit")])[0]
    deleted = server.ask(SHARED, "DELETE", "/library/metadata/" + collection)[0]
    status, kept = server.ask(OWNER, "GET", "/library/collections/" + collection)
    title = ((kept or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0].get("title")
    check(variant + ": a share can neither retitle nor delete the owner's collection",
          edited == 403 and deleted == 403 and status == 200 and title == "Owner set",
          (collection, edited, deleted, status, title))
    # An uploaded subtitle is the owner's library: a share's delete is refused.
    status, _, _ = raw(server, "POST", "/library/metadata/1/subtitles",
                       [("language", "en"), ("title", "owner.srt"), ("X-Plex-Token", OWNER)],
                       body=b"1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    streams = [stream for media in rex_rows(server.ask(OWNER, "GET", "/library/metadata/1")[1]).get("1", {}).get("Media", [])
               for part in media.get("Part", []) for stream in part.get("Stream", []) if stream.get("title") == "owner"]
    if status == 200 and streams:
        stream = str(streams[0]["id"])
        refused = server.ask(SHARED, "DELETE", "/library/streams/" + stream)[0]
        still = server.ask(OWNER, "GET", "/library/streams/%s.srt" % stream)[0]
        check(variant + ": a share cannot delete the owner's uploaded subtitle", refused == 403 and still == 200,
              (refused, still))
    else:
        check(variant + ": the owner's subtitle upload", False, (status, streams))
    # Another account's history row is not the share's to read or delete.
    for state, time in (("playing", 1000), ("playing", 5000), ("stopped", 10000)):
        server.ask(OWNER, "GET", "/:/timeline", [("ratingKey", "4"), ("key", "/library/metadata/4"),
                                                  ("state", state), ("time", str(time)), ("duration", "10031")])
    _, history = server.ask(OWNER, "GET", "/status/sessions/history/all")
    owners = [str(entry.get("historyKey", "")).rsplit("/", 1)[-1]
              for entry in (history or {}).get("MediaContainer", {}).get("Metadata", [])
              if str(entry.get("accountID")) == "1"]
    if owners:
        read = server.ask(SHARED, "GET", "/status/sessions/history/" + owners[0])[0]
        gone = server.ask(SHARED, "DELETE", "/status/sessions/history/" + owners[0])[0]
        kept = server.ask(OWNER, "GET", "/status/sessions/history/" + owners[0])[0]
        check(variant + ": the owner's history row is a 404 to the share and survives its delete",
              read == 404 and gone == 404 and kept == 200, (read, gone, kept))
    else:
        check(variant + ": the owner has a history row to ask for", False, history)
    # The most-played list counts the share's own plays, as its history lists them.
    _, top = server.ask(SHARED, "GET", "/library/all/top", [("type", "1")])
    users = {str(user.get("id")) for row in (top or {}).get("MediaContainer", {}).get("Metadata", [])
             for user in row.get("User", [])}
    check(variant + ": the share's most-played names no other account", users <= {SHARED_ID}, users)
    # A trailing slash is the same route, pruned the same way.
    _, bare = server.ask(SHARED, "GET", "/media/providers")
    _, slashed = server.ask(SHARED, "GET", "/media/providers/")
    check(variant + ": the share's /media/providers/ is its /media/providers", bare == slashed and bare,
          (bare, slashed))
    # A typed axis lists the share's rows alone.
    _, letters = server.ask(SHARED, "GET", "/library/sections/1/firstCharacter", [("type", "1")])
    counted = sum(entry.get("size", 0) for entry in (letters or {}).get("MediaContainer", {}).get("Directory", []))
    check(variant + ": the share's firstCharacter counts its own films", counted == len(visible), (counted, visible))
    # A live connection's line carries no other account's token.
    import socket
    held = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    held.sendall(("GET /:/eventsource/notifications?filters=activity&X-Plex-Token=%s HTTP/1.1\r\n"
                  "Host: 127.0.0.1\r\nX-Plex-Client-Identifier: owner-sse\r\n\r\n" % OWNER).encode())
    held.recv(1024)
    try:
        status, _, text = raw(server, "GET", "/connections", [("X-Plex-Token", SHARED)])
        check(variant + ": /connections shows the share no other account's token",
              status != 200 or OWNER not in text, text[:300])
    finally:
        held.close()
    # A request without a valid token is no device of this server's.
    for number in range(3):
        raw(server, "GET", "/library/sections", headers={"X-Plex-Client-Identifier": "unauthenticated-%d" % number,
                                                          "X-Plex-Token": "not-a-token"})
    _, _, devices = raw(server, "GET", "/devices", [("X-Plex-Token", OWNER)])
    check(variant + ": a refused request registers no device", "unauthenticated-" not in devices, devices[:300])
    # A deleted library takes its playlist entries with it, and its key is never minted again.
    _, made = server.ask(OWNER, "POST", "/playlists", [("type", "video"), ("title", "Episodes"), ("smart", "0"),
                         ("uri", "server://%s/com.plexapp.plugins.library/library/metadata/8" % MACHINE)])
    playlist = str(((made or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0].get("ratingKey"))
    _, items = server.ask(OWNER, "GET", "/playlists/%s/items" % playlist)
    entry = str(((items or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0].get("playlistItemID"))
    status = server.ask(OWNER, "DELETE", "/library/sections/2")[0]
    gone = server.ask(OWNER, "DELETE", "/playlists/%s/items/%s" % (playlist, entry))[0]
    check(variant + ": a deleted library's playlist entry goes with it", status == 200 and gone == 404,
          (status, playlist, entry, gone))
    status, _, _ = raw(server, "POST", "/library/sections", [
        ("name", "Again"), ("type", "show"), ("agent", "tv.plex.agents.series"), ("scanner", "Plex TV Series"),
        ("language", "en-US"), ("location", "/data/again"), ("X-Plex-Token", OWNER)])
    _, sections = server.ask(OWNER, "GET", "/library/sections")
    keys = {directory.get("title"): str(directory.get("key"))
            for directory in (sections or {}).get("MediaContainer", {}).get("Directory", [])}
    check(variant + ": a library added after a delete takes a key never used before",
          status == 201 and keys.get("Again") not in (None, "1", "2"), (status, keys))


def main():
    for variant, name, tokens in VARIANTS:
        run(variant, name, tokens)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
