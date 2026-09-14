"""Run plexsim's own filter engine over zen's real rows and diff it against zen.

    ZEN_TOKEN=... PLEXLAB_TOKEN=... python3 fsweep.py [field]


The strongest holdout this project can build without a second writable server: fetch
every row of a real library once, ask the reference which rows a (field, operator,
value) probe matches, then evaluate `filters.compare` locally over the same rows and
compare the two SETS. A difference is a rule plexsim has wrong, on real data, with the
disagreeing rows named. Read-only.
"""
import json
import os
import sys
import urllib.parse

# `PLEXSIM_TREE` is the checkout whose engine is being held out (default: this
# repository); `rd.py` is the cycle's own guarded GET/HEAD client for kids and zen.
sys.path.insert(0, os.environ.get("PLEXSIM_TREE",
                                  os.path.dirname(os.path.dirname(os.path.dirname(
                                      os.path.abspath(__file__))))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rd
import filters as F
import hierarchy

OPS = {"=": "", "!=": "%21", "==": "%3D", "!==": "%21%3D",
       "<=": "%3C", ">=": "%3E", "<<=": "%3C%3C", ">>=": "%3E%3E"}

SEC, TYPE = 1, 1


def zen_rows():
    rows = rd.page(rd.ZEN, "/library/sections/%d/all?type=%d" % (SEC, TYPE), size=300)
    return {str(r["ratingKey"]): r for r in rows}


def zen_match(field, operator, value):
    key = "%s%s=%s" % (field, OPS[operator], urllib.parse.quote(str(value)))
    out, start = set(), 0
    while True:
        st, c = rd.mc(rd.ZEN, "/library/sections/%d/all?type=%d&%s" % (SEC, TYPE, key),
                      headers={"X-Plex-Container-Start": str(start),
                               "X-Plex-Container-Size": "300"})
        if st != 200:
            return st, None
        got = c.get("Metadata") or []
        out.update(str(r["ratingKey"]) for r in got)
        total = c.get("totalSize", c.get("size", 0))
        start += 300
        if start >= total or not got:
            break
    return 200, out


# plexsim's own field -> (kind, accessor), mirroring server.filter_value for the
# fields a GRID row carries.
TAGS = {"genre": "Genre", "collection": "Collection", "director": "Director",
        "writer": "Writer", "producer": "Producer", "country": "Country",
        "actor": "Role", "label": "Label"}


def local_value(field, row):
    if field == "titleSort":
        return hierarchy.stored_sort_title(row)
    if field in TAGS:
        return row.get(TAGS[field])
    if field == "id":
        return row.get("ratingKey")
    if field == "resolution":
        return [m.get("videoResolution") for m in row.get("Media") or []]
    if field == "unwatched":
        return not row.get("viewCount")
    if field == "duplicate":
        return len(row.get("Media") or []) > 1
    if field == "unmatched":
        return str(row.get("guid") or "").startswith(("local://", "none://"))
    return row.get(field)


def local_match(field, kind, operator, value, rows):
    out = set()
    for key, row in rows.items():
        try:
            if F.compare(kind, operator, local_value(field, row), value, field):
                out.add(key)
        except F.FilterError:
            return None
    return out


PROBES = [
    # (field, kind, [values]), operators applied to each
    ("title", "string", ["the", "Baasha", "B"]),
    ("titleSort", "string", ["B", "Baasha", "0"]),
    ("summary", "string", ["the", "M"]),
    ("tagline", "string", ["a", "M"]),
    ("originalTitle", "string", ["a", "M"]),
    ("studio", "string", ["W", "Warner"]),
    ("year", "integer", ["2000", "1999"]),
    ("duration", "integer", ["3600000"]),
    ("viewCount", "integer", ["0", "1"]),
    ("rating", "integer", ["7"]),
    ("audienceRating", "integer", ["7"]),
    ("userRating", "integer", ["8"]),
    ("index", "integer", ["1"]),
    ("addedAt", "date", ["2025-01-01"]),
    ("updatedAt", "date", ["2025-01-01"]),
    ("originallyAvailableAt", "date", ["2020-01-01"]),
    ("contentRating", "tag", ["PG", ""]),
    ("genre", "tag", ["Drama", ""]),
    ("collection", "tag", ["", "x"]),
    ("unwatched", "boolean", ["1", "0"]),
    ("duplicate", "boolean", ["1"]),
    ("unmatched", "boolean", ["1"]),
    ("resolution", "resolution", ["1080", "sd"]),
]

if __name__ == "__main__":
    rows = zen_rows()
    print("zen rows:", len(rows))
    only = sys.argv[1] if len(sys.argv) > 1 else None
    bad = []
    for field, kind, values in PROBES:
        if only and field != only:
            continue
        for value in values:
            for operator in OPS:
                st, want = zen_match(field, operator, value)
                if st != 200:
                    print("  %-14s %-4s %-12r zen HTTP %s" % (field, operator, value, st))
                    continue
                got = local_match(field, kind, operator, value, rows)
                if got is None:
                    print("  %-14s %-4s %-12r plexsim RAISED" % (field, operator, value))
                    bad.append((field, operator, value, "raised", len(want)))
                    continue
                if got != want:
                    extra = sorted(got - want)[:3]
                    missing = sorted(want - got)[:3]
                    print("  %-14s %-4s %-12r zen %4d  plexsim %4d  extra %s missing %s"
                          % (field, operator, value, len(want), len(got),
                             [rows[k].get("title", "")[:18] for k in extra],
                             [rows[k].get("title", "")[:18] for k in missing]))
                    bad.append((field, operator, value, len(want), len(got)))
    print("\ndisagreements:", len(bad))
    json.dump(bad, open("fsweep.json", "w"))
