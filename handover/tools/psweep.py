"""Hold plexsim's filter PARSER out against both references' /query/parse.

Cycle 20. The listing side of the grammar has been swept repeatedly; the parser's own
report -- which rule shape each expression produces, which key each rule carries, what
an error looks like -- has only ever been sampled. Read-only on kids and zen.
"""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, "/Users/bensarmiento/debridmediamanager/plexsim/handover/tools")
import rd

PS = os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32546")

# (query, section on kids/zen, section on plexsim) -- plexsim's fixture has movie 1, show 2
QUERIES = [
    "type=1&genre=%(g)s",
    "type=1&genre=%(g)s&year%%3E%%3E=2000",
    "type=1&genre=999999",
    "type=1&genre=abc",
    "type=1&genre=",
    "type=1&genre!=%(g)s",
    "type=1&genre=%(g)s,%(g2)s",
    "type=1&contentRating=PG",
    "type=1&studio=Pixar",
    "type=1&unwatched=1",
    "type=1&resolution=1080",
    "type=1&unknownfield=3",
    "type=1&titleSort%%3E%%3E=A",
    "type=1&id=5",
    "type=1&duration%%3E%%3E=1000",
    "type=1&year=2000&year=2001",
    "type=1&title=a&title=b",
    "type=1&year%%3C%%3C=2000",
    "type=1&year%%3E=2000",
    "type=1&year!==2000",
    "type=1&addedAt%%3E%%3E=-1d",
    "type=1&unwatchedLeaves=1",
    "type=1&label=%(g)s",
    "type=1&collection=%(g)s",
    "type=1&actor=%(g)s",
    "type=1&sort=titleSort",
    "type=1&push=1&genre=%(g)s&pop=1",
    "type=1&or=1&genre=%(g)s",
    "type=1",
    "",
    "type=abc",
    "type=1&viewCount%%3E%%3E=0",
    "type=1&originalTitle=x",
    "type=1&parentIndex=1",
    "type=1&guid=plex://movie/1",
]
SHOW_QUERIES = [
    "type=2&show.title=a",
    "type=2&episode.title=a",
    "type=4&show.title=a",
    "type=4&season.index=1",
    "type=3&show.genre=%(g)s",
    "type=2&genre=%(g)s",
]


def fetch(base, path, tok=True):
    if base == PS:
        req = urllib.request.Request(base + path, headers={"Accept": "application/json",
                                                           "User-Agent": "curl/8"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    st, _, b = rd.get(base, path)
    return st, b.decode()


def tags(base, section, field="genre"):
    """Two real tag ids for the library."""
    if base == PS:
        st, b = fetch(base, "/library/sections/%s/%s" % (section, field))
    else:
        st, b = fetch(base, "/library/sections/%s/%s" % (section, field))
    try:
        rows = json.loads(b)["MediaContainer"].get("Directory") or []
    except Exception:
        return "1", "2"
    keys = [r.get("key") for r in rows if str(r.get("key", "")).isdigit()]
    return (keys + ["1", "2"])[0], (keys + ["1", "2"])[1]


def run(qs, msec, ssec, psec):
    out = {}
    for base, section, label in ((rd.ZEN, ssec, "zen"), (rd.KIDS, msec, "kids"), (PS, psec, "sim")):
        g, g2 = tags(base, section)
        for q in qs:
            path = "/library/sections/%s/query/parse?%s" % (section, q % {"g": g, "g2": g2})
            st, b = fetch(base, path)
            try:
                b = json.dumps(json.loads(b), sort_keys=False)
            except Exception:
                pass
            # the tag NAME behind an id is content; its PRESENCE is the rule
            b = re.sub(r'"valueTitle": "[^"]*"', '"valueTitle": "<TAG>"', b)
            b = re.sub(r'"value": "\d{2,}"', '"value": "<ID>"', b)
            out.setdefault(q, {})[label] = (st, b[:400])
    for q, r in out.items():
        z, k, s = r.get("zen"), r.get("kids"), r.get("sim")
        agree = z == k
        same = z == s
        if agree and not same:
            print("!! %s" % q)
            print("   refs: %s %s" % z)
            print("   sim : %s %s" % s)
        elif not agree:
            print(".. %s  (references disagree: zen %s / kids %s)" % (q, z[1][:110], k[1][:110]))


print("=== movie library (kids 1 / zen 1 / sim 1) ===")
run(QUERIES, 1, 1, 1)
print()
print("=== show library (2 everywhere) ===")
run(SHOW_QUERIES, 2, 2, 2)
