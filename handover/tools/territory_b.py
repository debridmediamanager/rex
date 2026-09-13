"""Territory B: malformed and edge identifiers across the route families,
GET and HEAD, on the reference and plexsim. Prints disagreements."""
import json
import re
import sys
import threading

import lib

VALID = {
    "ref": {"metadata": "1", "section": "1", "collection": None, "playlist": None, "queue": None,
            "part": None, "stream": None, "history": None, "activity": None, "transcode": None, "device": None},
    "sim": {"metadata": "1007", "section": "1", "collection": None, "playlist": None, "queue": None,
            "part": None, "stream": None, "history": None, "activity": None, "transcode": None, "device": None},
}

# Fill in part/stream ids from the valid movie on each server.
for name, server in (("ref", lib.REF), ("sim", lib.SIM)):
    st, c, _ = lib.jreq(server, "GET", "/library/metadata/%s" % VALID[name]["metadata"])
    try:
        media = c["Metadata"][0]["Media"][0]
        VALID[name]["part"] = str(media["Part"][0]["id"])
        VALID[name]["stream"] = str(media["Part"][0]["Stream"][0]["id"])
    except Exception as e:
        print(name, "no part/stream", e)
    st, c, _ = lib.jreq(server, "GET", "/devices")
    try:
        VALID[name]["device"] = str(c["Device"][0]["id"])
    except Exception:
        pass

FAMILIES = [
    ("metadata", "/library/metadata/{id}", ["", "/children", "/tree", "/posters", "/related", "/prefs"]),
    ("section", "/library/sections/{id}", ["", "/all"]),
    ("collection", "/library/collections/{id}", ["", "/children", "/items"]),
    ("playlist", "/playlists/{id}", ["", "/items"]),
    ("queue", "/playQueues/{id}", [""]),
    ("part", "/library/parts/{id}", [""]),
    ("stream", "/library/streams/{id}", [""]),
    ("history", "/status/sessions/history/{id}", [""]),
    ("activity", "/activities/{id}", [""]),
    ("transcode", "/transcode/sessions/{id}", [""]),
    ("device", "/devices/{id}", [""]),
]
BAD = ["abc", "0", "-1", "99999999999999999999", "1.5", "%20", "", "1,abc"]


def shape(status, body, headers):
    ct = (headers.get("Content-Type") or "").split(";")[0]
    b = body[:400].decode("utf-8", "replace")
    if b.startswith("{"):
        try:
            j = json.loads(body)
            mc = j.get("MediaContainer", j)
            kids = [k for k, v in mc.items() if isinstance(v, list)]
            return "%s json size=%s %s" % (status, mc.get("size"), ",".join("%s[%d]" % (k, len(mc[k])) for k in kids))
        except Exception:
            return "%s json-unparsable %r" % (status, b[:80])
    if b.startswith("<?xml") or b.startswith("<"):
        m = re.search(r"<(\w+)[^>]*?(?: size=\"(\d*)\")?", b)
        root = re.search(r"<(\w+)", b.split("?>")[-1] if "?>" in b else b)
        sz = re.search(r' size="([^"]*)"', b)
        children = re.findall(r"<(\w+)", b.split("?>")[-1] if "?>" in b else b)[1:3]
        return "%s %s <%s size=%s> %s" % (status, ct, root.group(1) if root else "?", sz.group(1) if sz else "-", ",".join(children))
    return "%s %s %r" % (status, ct, b[:60])


results = {}


def sweep(name, server):
    v = VALID[name]
    rows = {}
    for fam, tmpl, leaves in FAMILIES:
        ids = list(BAD)
        if v[fam]:
            ids += [v[fam] + "/", "/" + v[fam], v[fam] + ",abc", v[fam]]
        for ident in ids:
            for leaf in leaves:
                path = tmpl.replace("{id}", ident) + leaf
                if ident.endswith("/") and leaf:
                    path = tmpl.replace("{id}", ident.rstrip("/")) + leaf + "/"
                if ident.startswith("/"):
                    path = tmpl.replace("/{id}", "/" + ident) + leaf   # doubled slash
                for method in ("GET", "HEAD"):
                    st, body, hd = lib.req(server, method, path)
                    rows[(method, path)] = shape(st, body, hd) if method == "GET" else "%s len=%s ct=%s" % (
                        st, hd.get("Content-Length"), (hd.get("Content-Type") or "").split(";")[0])
    # query-string edge cases
    m = v["metadata"]
    extras = [
        ("GET", "/library/sections/1/all?type=1&type=2"),
        ("GET", "/library/sections/1/all?type=2&type=1"),
        ("GET", "/library/metadata/%s?includeGuids" % m),
        ("GET", "/library/metadata/%s?includeGuids=" % m),
        ("GET", "/library/sections/1/all?includeGuids&type=1"),
        ("GET", "/library/sections/1/all?sort=titleSort&sort=year:desc"),
        ("GET", "/library/sections/1/all?X-Plex-Container-Start=0"),
        ("GET", "/library/metadata/%s?" % m),
        ("GET", "/library/metadata/%s?&" % m),
        ("GET", "/library/metadata/%s?=1" % m),
        ("GET", "/library/metadata/%s?includeGuids=abc" % m),
    ]
    for method, path in extras:
        st, body, hd = lib.req(server, method, path)
        rows[(method, path)] = shape(st, body, hd)
        if "includeGuids" in path or "type=" in path:
            try:
                mc = json.loads(body)["MediaContainer"]
                rows[(method, path)] += " guids=%s types=%s" % (
                    "Guid" in (mc.get("Metadata") or [{}])[0],
                    sorted(set(r.get("type") for r in mc.get("Metadata") or [])))
            except Exception:
                pass
    for hdrs, label in (({"X-Plex-Container-Size": ""}, "size-empty"),
                        ({"X-Plex-Container-Size": "", "X-Plex-Container-Start": "0"}, "size-empty+start"),
                        ({"X-Plex-Container-Size": "abc", "X-Plex-Container-Start": "0"}, "size-abc+start"),
                        ({"X-Plex-Container-Size": "2", "X-Plex-Container-Start": ""}, "size2+start-empty"),
                        ({"X-Plex-Container-Size": "2", "X-Plex-Container-Start": "abc"}, "size2+start-abc"),
                        ({"X-Plex-Container-Size": "-1", "X-Plex-Container-Start": "0"}, "size-neg+start"),
                        ({"X-Plex-Container-Size": "2", "X-Plex-Container-Start": "-1"}, "size2+start-neg")):
        st, body, hd = lib.req(server, "GET", "/library/sections/1/all", headers=hdrs)
        s = shape(st, body, hd)
        try:
            mc = json.loads(body)["MediaContainer"]
            s += " total=%s off=%s" % (mc.get("totalSize"), mc.get("offset"))
        except Exception:
            pass
        rows[("GET", "/library/sections/1/all [%s]" % label)] = s
    results[name] = rows


t1 = threading.Thread(target=sweep, args=("ref", lib.REF))
t2 = threading.Thread(target=sweep, args=("sim", lib.SIM))
t1.start(); t2.start(); t1.join(); t2.join()

ref, sim = results["ref"], results["sim"]


def norm(s):
    # compare status + rough shape, ignoring server-specific ids
    return re.sub(r"\d{3,}", "N", s)


def key_norm(k):
    m, p = k
    for name in ("ref", "sim"):
        for fam, val in VALID[name].items():
            if val:
                p = p.replace("/" + val, "/{%s}" % fam).replace(val + ",abc", "{%s},abc" % fam)
    return (m, p)


refn = {key_norm(k): (k, v) for k, v in ref.items()}
simn = {key_norm(k): (k, v) for k, v in sim.items()}
json.dump({"ref": {"%s %s" % k: v for k, v in ref.items()}, "sim": {"%s %s" % k: v for k, v in sim.items()}, "valid": VALID},
          open("b_results.json", "w"), indent=1)
print("VALID", json.dumps(VALID))
print("\n== DIFFERENCES (status differs) ==")
same = 0
for k in sorted(set(refn) | set(simn)):
    r = refn.get(k, (None, "-"))[1]
    s = simn.get(k, (None, "-"))[1]
    if r.split()[0] != s.split()[0]:
        print("%-5s %-55s ref: %-40s sim: %s" % (k[0], k[1], r, s))
    else:
        same += 1
print("\nsame status:", same)
print("\n== SHAPE differences with the same status ==")
for k in sorted(set(refn) | set(simn)):
    r = refn.get(k, (None, "-"))[1]
    s = simn.get(k, (None, "-"))[1]
    if r.split()[0] == s.split()[0] and norm(r) != norm(s):
        print("%-5s %-55s ref: %-40s sim: %s" % (k[0], k[1], r, s))
