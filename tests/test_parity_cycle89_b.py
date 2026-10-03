#!/usr/bin/env python3
"""Cycle 89, B: a collection created under a DELETED collection's name is that collection
again to everything stored against its guid.

`tests/fixtures/collection_recreate_s42.json` (plexscratch42, 1.42.2) holds six sequences,
each created, written, deleted and created again under the same name, with every read the
client makes in between. A collection's guid lives on its TAG (`tags.extra_data` holds
`at:guid`), the tag outlives the collection, and the per-user rating and the collection
prefs are stored against the guid:

- the re-created collection carries the deleted one's guid and index, and its
  `userRating`/`lastRatedAt` (7.0 and the ORIGINAL rating time) on the detail and the
  listing alike, and its member's `Collection` tag carries that guid; a `-1` clears both;
- the name matches ignoring case, and the title takes the deleted one's spelling
  (`c89b fx recase` reads `C89B Fx Recase`);
- the prefs (`collectionMode`, `collectionSort`; `collectionFilterBasedOnUser` on a smart
  one) do NOT show on the re-created row until a prefs write is accepted -- then every old
  one it does not write comes back with it;
- the library does not matter: a name deleted in the movie library and re-created in the
  show library is the same guid and rating;
- a name never used mints a new guid with nothing on it.

Both servers minted a fresh guid per collection and brought nothing back, so re-creating a
collection Plex Web had rated showed it unrated, and python-plexapi's `userRating` and
`guid` disagreed with a real PMS.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle89_b.py
    python3 tests/test_parity_cycle89_b.py --capture BASE TOKEN OUT   # re-measure (writes!)
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "collection_recreate_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle89-b", "Accept": "application/json"}
# The movie library and a movie, the show library and a show: plexscratch42's and
# catalog_ui's.
IDS = {"s42": {"movies": "1", "movie": "3", "shows": "3", "show": "15"},
       "sim": {"movies": "1", "movie": "1007", "shows": "2", "show": "20001"}}
FIELDS = ("title", "guid", "index", "librarySectionID", "userRating", "lastRatedAt",
          "collectionMode", "collectionSort", "collectionFilterBasedOnUser")
# Values that differ per server by construction: compared as the ORDER they first appear in.
SYMBOLS = {"guid": "G", "index": "I", "lastRatedAt": "T", "ratingKey": "R"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


class Client:
    def __init__(self, base, token=None):
        parsed = urllib.parse.urlsplit(base)
        self.host, self.port, self.token = parsed.hostname, parsed.port, token

    def __call__(self, method, path):
        if self.token:
            path += ("&" if "?" in path else "?") + "X-Plex-Token=" + self.token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            connection.request(method, path, headers=dict(HEADERS, Connection="close"))
            response = connection.getresponse()
            data = response.read()
            try:
                return response.status, json.loads(data) if data.strip() else None
            except ValueError:
                return response.status, None
        finally:
            connection.close()


def first(body):
    return ((body or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0]


def sequences(ids):
    """(name, smart, steps): each step is (label, action, argument)."""
    q = urllib.parse.quote
    return [
        ("rated", False, [("create", "movies", "C89B Fx Rated"), ("rate", "7", None),
                          ("delete", None, None), ("create", "movies", "C89B Fx Rated"),
                          ("listing", None, None), ("member", None, None),
                          ("rate", "-1", None), ("delete", None, None)]),
        ("case", False, [("create", "movies", "C89B Fx Recase"), ("rate", "7", None),
                         ("delete", None, None), ("create", "movies", "c89b fx recase"),
                         ("rate", "-1", None), ("delete", None, None)]),
        ("prefs", False, [("create", "movies", "C89B Fx Prefs"),
                          ("prefs", "collectionMode=0&collectionSort=1", None),
                          ("delete", None, None), ("create", "movies", "C89B Fx Prefs"),
                          ("prefs", "collectionSort=0", None), ("delete", None, None)]),
        ("smart", True, [("create", "shows", "C89B Fx Smart"), ("prefs", "collectionMode=0", None),
                         ("delete", None, None), ("create", "shows", "C89B Fx Smart"),
                         ("prefs", "collectionFilterBasedOnUser=1", None),
                         ("delete", None, None)]),
        ("cross", False, [("create", "movies", "C89B Fx Cross"), ("rate", "7", None),
                          ("delete", None, None), ("create", "shows", "C89B Fx Cross"),
                          ("rate", "-1", None), ("delete", None, None)]),
        ("fresh", False, [("create", "movies", "C89B Fx Fresh"), ("delete", None, None)]),
    ]


def run(client, ids):
    """Drive every sequence; return what each step answered, projected."""
    machine = client("GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
    out = []
    for name, smart, steps in sequences(ids):
        key, title, member = None, None, None
        record = {"name": name, "steps": []}
        for label, action, argument in steps:
            entry = {"step": label}
            if label == "create":
                section = ids[action]
                kind = "1" if action == "movies" else "2"
                member = ids["movie" if action == "movies" else "show"]
                target = ("/library/sections/%s/all?type=2" % section if smart
                          else "/library/metadata/%s" % member)
                uri = urllib.parse.quote("server://%s/com.plexapp.plugins.library%s"
                                         % (machine, target), safe="")
                status, body = client("POST", "/library/collections?sectionId=%s&smart=%s"
                                      "&title=%s&type=%s&uri=%s"
                                      % (section, "1" if smart else "0",
                                         urllib.parse.quote(argument), kind, uri))
                key, title = str(first(body).get("ratingKey")), argument
                entry.update(sent=argument, status=status)
            elif label == "rate":
                status, _ = client("PUT", "/:/rate?key=%s&identifier=com.plexapp.plugins.library"
                                   "&rating=%s" % (key, action))
                entry.update(sent=action, status=status)
            elif label == "prefs":
                status, _ = client("PUT", "/library/metadata/%s/prefs?%s" % (key, action))
                entry.update(sent=action, status=status)
            elif label == "delete":
                status, _ = client("DELETE", "/library/collections/%s" % key)
                entry.update(status=status)
                record["steps"].append(entry)
                continue
            if label == "listing":
                status, body = client("GET", "/library/sections/%s/all?type=18" % ids["movies"])
                rows = [row for row in (body or {}).get("MediaContainer", {}).get("Metadata") or []
                        if str(row.get("ratingKey")) == key]
                entry["row"] = {k: rows[0].get(k) for k in FIELDS} if rows else None
            elif label == "member":
                status, body = client("GET", "/library/metadata/%s" % member)
                entry["tags"] = [{"tag": t.get("tag"), "guid": t.get("guid")}
                                 for t in first(body).get("Collection") or []
                                 if str(t.get("tag")).lower() == title.lower()]
            else:
                status, body = client("GET", "/library/metadata/%s" % key)
                row = first(body)
                entry["row"] = {k: row.get(k) for k in FIELDS}
                entry["row"]["ratingKey"] = row.get("ratingKey")
            record["steps"].append(entry)
        out.append(record)
    return out


def normalise(runs, ids):
    """Replace the per-server values by their order of first appearance: guids, indexes
    and keys across the whole run (a fresh name must mint a NEW guid), rating times within
    one sequence (the reference stamps whole seconds, so two sequences may share one)."""
    seen = {kind: {} for kind in SYMBOLS}

    def walk(value, field=None):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, field) for v in value]
        if field == "librarySectionID" and value is not None:
            return "movies" if str(value) == ids["movies"] else "shows"
        if field in SYMBOLS and value is not None:
            table = seen[field]
            table.setdefault(str(value), "%s%d" % (SYMBOLS[field], len(table) + 1))
            return table[str(value)]
        return value
    out = []
    for record in runs:
        seen["lastRatedAt"] = {}
        out.append(walk(record))
    return out


def capture(base, token, out):
    runs = run(Client(base, token), IDS["s42"])
    json.dump({"server": "plexscratch42 (PMS 1.42.2)",
               "measured": time.strftime("%Y-%m-%d"),
               "ids": IDS["s42"],
               "note": "Each sequence creates, writes, deletes and re-creates a collection under "
                       "the same name; every write was undone (rating -1, delete). The tag rows "
                       "and the guid-keyed settings outlive it on the copy by design.",
               "runs": runs}, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("wrote", out)


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start():
    port = free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    process = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = Client("http://127.0.0.1:%s" % port)
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            if client("GET", "/identity")[0] == 200:
                return process, client
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def main():
    if sys.argv[1:2] == ["--capture"]:
        capture(*sys.argv[2:5])
        return 0
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    want = normalise(fixture["runs"], fixture["ids"])
    process, client = start()
    try:
        got = normalise(run(client, IDS["sim"]), IDS["sim"])
    finally:
        stop(process)
    for want_run, got_run in zip(want, got):
        for index, (w, g) in enumerate(zip(want_run["steps"], got_run["steps"])):
            label = "%s step %d %s%s" % (want_run["name"], index + 1, w["step"],
                                         " %s" % w["sent"] if w.get("sent") else "")
            if "status" in w:
                check("%s answers %s" % (label, w["status"]), g.get("status") == w["status"],
                      g.get("status"))
            for part in ("row", "tags"):
                if part in w:
                    check("%s reads %s" % (label, json.dumps(w[part], sort_keys=True)),
                          w[part] == g.get(part), json.dumps(g.get(part), sort_keys=True))
    check("every sequence ran", len(got) == len(want) and all(
        len(a["steps"]) == len(b["steps"]) for a, b in zip(want, got)))
    passed = sum(results)
    print("PASS %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
