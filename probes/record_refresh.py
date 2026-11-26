#!/usr/bin/env python3
"""Record what plexlab's PMS does on a Refresh Metadata, for tests/test_refresh_updates.py.

Run on the lab host against plexlab, which answers on loopback with no token while unclaimed, with
its venv (websocket-client) and its outbound capture. Each run sends ONE `PUT .../refresh`, which
changes plexlab for good (a saved row's `updatedAt` and artwork versions): keep them few.

    record_refresh.py record capture/outbound.jsonl <key> <out.json>   # one refresh, recorded
    record_refresh.py fixture <dir holding film1.json season7.json show6.json> > refresh_s1434.json

`record` reads every row of the two libraries and their listings (GETs) before and after, the
notification frames while it runs (`/:/websockets/notifications`; a frame's time is when its read
began), every artwork path's bytes as length and SHA-256 before and after, and plexlab's requests to
anyone but plex.tv's servers from the PUT on, the agent's JSON bodies kept and every header
dropped (they carry the server's token). `fixture` keeps what the suite reads.
"""
import hashlib
import json
import sys
import threading
import time
import urllib.request

import websocket

BASE = "http://127.0.0.1:32400"
MOVIES = ["1", "3", "4", "17", "18"]
FIREFLY = ["6", "7", "8", "9", "10", "11", "12", "13", "14"]
LISTS = ["/library/sections", "/library/sections/1/all?sort=updatedAt:desc", "/library/sections/2/all?sort=updatedAt:desc",
         "/library/recentlyAdded", "/library/sections/1/recentlyAdded", "/library/sections/2/recentlyAdded",
         "/hubs/home/recentlyAdded?type=1", "/hubs/home/recentlyAdded?type=2", "/library/sections/2/all?type=4&sort=updatedAt:desc",
         "/library/sections/1/all", "/library/sections/2/all", "/library/metadata/6/children", "/library/metadata/6/allLeaves"]


def get(path):
    request = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=30))


def image(path):
    try:
        response = urllib.request.urlopen(BASE + path, timeout=30)
        body = response.read()
        return {"status": response.status, "len": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "type": response.headers.get("Content-Type")}
    except urllib.error.HTTPError as error:
        return {"status": error.code}


def snapshot():
    items = {key: get("/library/metadata/" + key)["MediaContainer"]["Metadata"][0] for key in MOVIES + FIREFLY}
    art = {}
    for row in items.values():
        for field in ("thumb", "art", "theme"):
            if row.get(field):
                art[row[field]] = image(row[field])
    return {"at": time.time(), "items": items, "lists": {path: get(path) for path in LISTS}, "artwork": art}


def record(capture, key, out, dry=False):
    frames = []
    ws = websocket.create_connection("ws://127.0.0.1:32400/:/websockets/notifications", timeout=5)

    def listen():
        while True:
            try:
                frames.append({"t": time.time(), "frame": json.loads(ws.recv())})
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                return

    threading.Thread(target=listen, daemon=True).start()
    before = snapshot()
    json.dump(before, open(out + ".before", "w"))
    time.sleep(2)
    frames.clear()
    t0 = time.time()
    if dry:
        status = None
    else:
        status = urllib.request.urlopen(urllib.request.Request(BASE + "/library/metadata/%s/refresh" % key, method="PUT"), timeout=30).status
    # Until the frames have been quiet 20 s, at least 40 s, at most 180 s.
    while True:
        time.sleep(2)
        last = frames[-1]["t"] if frames else t0
        if time.time() - t0 > 180 or (time.time() - t0 > 40 and time.time() - last > 20):
            break
    after = snapshot()
    # Every old artwork path read again after the refresh: what a client holding it gets.
    stale = {path: image(path) for path in before["artwork"]}
    ws.close()
    json.dump({"key": key, "put_status": status, "t0": t0, "before": before, "after": after,
               "stale_paths_after": stale, "frames": frames}, open(out + ".after", "w"))
    outbound = []
    for line in open(capture, errors="replace"):
        try:
            exchange = json.loads(line)
        except ValueError:
            continue
        if exchange.get("t", 0) < t0 - 1 or exchange.get("t", 0) > time.time() or exchange.get("host") in ("servers.plex.tv", "pubsub.plex.tv"):
            continue
        entry = {k: exchange.get(k) for k in ("t", "iso", "method", "host", "path", "status", "ms")}
        entry["query"] = [[k, "<redacted>" if "token" in k.lower() else v] for k, v in (exchange.get("query") or [])]
        body = exchange.get("res_body")
        if exchange.get("host") == "metadata.provider.plex.tv" and body:
            try:
                entry["response"] = json.loads(body)
            except ValueError:
                entry["res_len"] = exchange.get("res_len")
        else:
            entry["res_len"] = exchange.get("res_len")
        outbound.append(entry)
    json.dump({"key": key, "put_status": status, "t0": t0, "before": before, "after": after,
               "stale_paths_after": stale, "frames": frames, "outbound": outbound}, open(out, "w"), indent=1)
    print("frames", len(frames), "outbound", len(outbound))
    for entry in outbound:
        print(entry["iso"], entry["method"], entry["host"], entry["path"], entry["status"], [k for k, _ in entry["query"]])



FIELDS = ("guid", "title", "type", "updatedAt", "addedAt", "thumb", "art", "theme", "parentThumb", "parentArt", "parentTheme",
          "grandparentThumb", "grandparentArt", "grandparentTheme", "audienceRating", "audienceRatingImage", "rating",
          "ratingImage", "Rating", "Field")
KEPT = ("/library/sections", "/library/sections/1/all?sort=updatedAt:desc", "/library/sections/2/all?sort=updatedAt:desc",
         "/library/recentlyAdded", "/library/sections/1/recentlyAdded", "/library/sections/2/recentlyAdded",
         "/library/sections/2/all?type=4&sort=updatedAt:desc")


def row(m):
    out = {k: m[k] for k in FIELDS if k in m}
    out["Image"] = [i["url"] for i in m.get("Image", [])]
    return out


def listing(answer):
    c = answer["MediaContainer"]
    rows = c.get("Metadata", c.get("Directory", []))
    return [{k: r.get(k) for k in ("key", "ratingKey", "title", "updatedAt", "contentChangedAt", "addedAt") if k in r} for r in rows]


def refresh(path, label):
    d = json.load(open(path))
    t0 = d["t0"]
    changed = {p for p in d["after"]["artwork"] if p not in d["before"]["artwork"]}
    return {
        "label": label, "put": "PUT /library/metadata/%s/refresh" % d["key"], "status": d["put_status"], "at": t0,
        "outbound": [{"after_s": round(o["t"] - t0, 2), "method": o["method"], "host": o["host"], "path": o["path"],
                      "query": o["query"], "status": o["status"], "response": o.get("response")} for o in d["outbound"]],
        "frames": [f["frame"]["NotificationContainer"] for f in d["frames"]],
        "rows": {k: {"before": row(d["before"]["items"][k]), "after": row(d["after"]["items"][k])} for k in d["before"]["items"]},
        "lists": {p: {"before": listing(d["before"]["lists"][p]), "after": listing(d["after"]["lists"][p])} for p in KEPT},
        # Every artwork path the rows named before the refresh, read again after it, and each new one: bytes as length and hash.
        "artwork": {p: {"before": r, "after": d["stale_paths_after"][p]} for p, r in d["before"]["artwork"].items()},
        "new_paths": {p: d["after"]["artwork"][p] for p in sorted(changed)},
    }


def fixture(m):
    json.dump({
        "reference": "plexlab, PMS 1.43.4.10903, unclaimed (claimed=\"0\" at /identity) with Plex's agent reachable: "
                     "it asks metadata.provider.plex.tv and is answered. 2026-11-26 23:19-23:23 UTC, PUTs sent on "
                     "loopback, read with GETs before and after, frames from /:/websockets/notifications, the agent "
                     "requests from the lab's outbound capture (headers dropped).",
        "keys": {"1": "Big Buck Bunny", "6": "Firefly", "7": "Firefly Season 1 (matched)", "8": "The Train Job",
                 "9": "Bushwhacked", "10": "Our Mrs. Reynolds", "11": "Season 2 (no match, no guid)", "12-14": "its episodes"},
        "refreshes": [refresh(m + "/film1.json", "film, its answer the same as the last one (2026-11-26 03:19)"),
                      refresh(m + "/season7.json", "season, last asked 2026-09-12"),
                      refresh(m + "/show6.json", "show, last asked 2026-11-12, its season asked 52 s before")],
        # PMS's own log (Plex Media Server.log, DEBUG): which rows each refresh saved.
        "saved": {
            "film 2026-11-25 23:33:53": ["1"], "film 2026-11-26 03:19:20": ["1"], "film 2026-11-26 23:19:03": [],
            "season 2026-11-26 23:22:14": ["7", "8", "9", "10"], "show 2026-11-26 23:23:06": ["6"],
            "line": "There was a change for metadata item <id> (<title>), saving. / Updating metadata item (save)",
        },
        # Big Buck Bunny's item answer from the agent at each refresh (md5 of the body plexlab was given).
        "film_answers": {"2026-11-12 02:17 (pass)": "differs", "2026-11-25 23:33": "adc1eb5d2d", "2026-11-26 03:19": "adc1eb5d2d",
                         "2026-11-26 23:19": "adc1eb5d2d",
                         "updatedAt": {"before 2026-11-25": 1794449833, "after 2026-11-25 23:33": 1795649634,
                                       "after 2026-11-26 03:19": 1795663160, "after 2026-11-26 23:19": 1795663160}},
        # The agent read directly (an account token, 2026-11-27) with plexlab's refresh queries: is one answer whole?
        "agent_reads": {
            "show": {"guid": "plex://show/5d9c080d3c3f87001f3460e8", "title": "The Simpsons", "query": "episodeOrder=tmdbAiring&includeChildren=1&updating=1",
                     "childCount": 38, "Children.size": 39, "rows": 39},
            "season": {"guid": "plex://season/65dd8dbef97685e8c5506592", "title": "Animaniacs Season 1", "query": "includeChildren=1&updating=1",
                       "leafCount": 160, "Children.size": 160, "rows": 160},
            "long season": {"title": "Detective Conan Season 1", "query": "includeChildren=1&updating=1",
                            "leafCount": 1222, "Children.size": 1222, "rows": 1222, "bytes": 8432397, "seconds": 2.7},
        },
    }, sys.stdout, indent=1, ensure_ascii=False)



def main():
    if sys.argv[1] == "record":
        record(sys.argv[2], sys.argv[3], sys.argv[4], "--dry" in sys.argv)
    else:
        fixture(sys.argv[2])


if __name__ == "__main__":
    main()
