"""Hold plexsim's own SORT engine out against a real library's ordering.

Cycle 18's `fsweep.py` did this for the filter engine and found the
`title=`/`originalTitle` rule in its first run. The same shape for `sort=`:
fetch every row of a real listing once, ask the reference for the ORDER it
answers under each sort key, then run `PlexSim._sorted` locally over the same
rows and diff the SEQUENCES. Read-only on kids and zen.

    ZEN_TOKEN=... PLEXLAB_TOKEN=... python3 ssweep.py [section] [type]
"""
import json
import os
import sys
import urllib.parse

TREE = os.environ.get("PLEXSIM_TREE",
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
sys.path.insert(0, TREE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rd
import server as S

SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 1
TYPE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BASE = rd.ZEN
# The local side must hold EVERY row or the comparison is meaningless: zen's order
# is over the whole library, so a row it ranks that we do not hold leaves a hole in
# the expected sequence and every later index reads as a disagreement. Fetch all of
# them once, then compare only the reference's first HEAD rows.
HEAD = int(os.environ.get("HEAD", "600"))

KEYS = list(S.PlexSim.TEXT_SORTS) + list(S.PlexSim.NUMERIC_SORTS)
KEYS = sorted(set(KEYS))


class Shim(object):
    """Just enough of PlexSim for the sort path: the two key tables and a
    `scope_rows` that resolves a prefix through the rows we actually hold."""

    TEXT_SORTS = S.PlexSim.TEXT_SORTS
    NUMERIC_SORTS = S.PlexSim.NUMERIC_SORTS
    SCOPE_TYPES = S.PlexSim.SCOPE_TYPES

    def __init__(self, by_key):
        self.by_key = by_key

    def scope_rows(self, scope, row):
        kind = row.get("type")
        if scope not in self.SCOPE_TYPES or scope == kind:
            return [row]
        def lookup(key):
            found = self.by_key.get(str(key))
            return [found] if found is not None else []
        if kind == "episode" and scope == "season":
            return lookup(row.get("parentRatingKey"))
        if kind == "episode" and scope == "show":
            return lookup(row.get("grandparentRatingKey"))
        if kind == "season" and scope == "show":
            return lookup(row.get("parentRatingKey"))
        return [row]

    _sort_key = S.PlexSim._sort_key
    _scoped_sort_key = S.PlexSim._scoped_sort_key
    _natural_order = S.PlexSim._natural_order
    _sorted = S.PlexSim._sorted


def zen_order(key, desc):
    """The ratingKeys zen answers, in order, for one sort spec."""
    spec = key + (":desc" if desc else "")
    path = "/library/sections/%d/all?type=%d&sort=%s" % (SEC, TYPE, urllib.parse.quote(spec))
    out, start = [], 0
    while True:
        st, c = rd.mc(BASE, path, headers={"X-Plex-Container-Start": str(start),
                                           "X-Plex-Container-Size": "300"})
        if st != 200 or c is None:
            return st, None
        got = c.get("Metadata") or []
        out.extend(str(r["ratingKey"]) for r in got)
        total = c.get("totalSize", c.get("size", 0))
        start += 300
        if start >= total or not got or len(out) >= HEAD:
            break
    return 200, out[:HEAD]


def main():
    rows = rd.page(BASE, "/library/sections/%d/all?type=%d" % (SEC, TYPE), size=300)
    by_key = {str(r["ratingKey"]): r for r in rows}
    print("rows: %d  section %d type %d" % (len(rows), SEC, TYPE))
    sim = Shim(by_key)
    bad = []
    for key in KEYS:
        for desc in (False, True):
            st, want = zen_order(key, desc)
            if st != 200:
                print("  %-22s %-4s reference HTTP %s" % (key, "desc" if desc else "asc", st))
                continue
            missing = [k for k in want if k not in by_key]
            assert not missing, ("rows we do not hold", key, missing[:3])
            try:
                got, unsupported = sim._sorted(rows, key + (":desc" if desc else ""))
            except Exception as exc:
                print("  %-22s %-4s plexsim RAISED %r" % (key, "desc" if desc else "asc", exc))
                bad.append((key, desc, "raised"))
                continue
            if unsupported:
                print("  %-22s %-4s plexsim UNSUPPORTED" % (key, "desc" if desc else "asc"))
                bad.append((key, desc, "unsupported"))
                continue
            got = [str(r["ratingKey"]) for r in got][:len(want)]
            if got != want:
                # first index that differs, and the two rows there
                i = next(n for n in range(min(len(got), len(want))) if got[n] != want[n])
                a, b = by_key.get(want[i], {}), by_key.get(got[i], {})
                bare = key
                print("  %-22s %-4s DIFFER at %d: zen %s %r (%s=%r)  plexsim %s %r (%s=%r)"
                      % (key, "desc" if desc else "asc", i,
                         want[i], (a.get("title") or "")[:24], bare, a.get(bare),
                         got[i], (b.get("title") or "")[:24], bare, b.get(bare)))
                bad.append((key, desc, i, want[i], got[i]))
    print("\ndisagreements: %d of %d" % (len(bad), 2 * len(KEYS)))
    json.dump(bad, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "ssweep_%d_%d.json" % (SEC, TYPE)), "w"))


if __name__ == "__main__":
    main()
