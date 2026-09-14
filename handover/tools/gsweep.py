"""`group=` held out against zen: which keys are accepted, and is the survivor really
the group's lowest addedAt on 3,887 real rows? Read-only."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/Users/bensarmiento/debridmediamanager/plexsim/handover/tools")
import rd

PS = os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32546")
SP = os.path.dirname(os.path.abspath(__file__))

FIELDS = """title titleSort originalTitle summary tagline studio contentRating year decade
duration rating audienceRating userRating viewCount skipCount addedAt updatedAt createdAt
lastViewedAt originallyAvailableAt editionTitle index type guid id slug mediaCount
ratingCount contentRatingAge resolution genre actor unwatched file mediaSize
audioLanguage absoluteIndex parentIndex viewOffset leafCount childCount theme art thumb
sourceType key ratingKey librarySectionID chapterSource unmatched duplicate hdr
firstCharacter location subtitleLanguage audioChannels bogus""".split()


def status(base, path):
    if base == PS:
        req = urllib.request.Request(base + path, headers={"Accept": "application/json",
                                                           "User-Agent": "curl/8"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                c = json.loads(r.read())["MediaContainer"]
                return r.status, c.get("totalSize", c.get("size"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return "err", None
    st, c = rd.mc(base, path, headers={"X-Plex-Container-Start": "0",
                                       "X-Plex-Container-Size": "1"})
    return st, (c or {}).get("totalSize", (c or {}).get("size"))


print("field                    zen        kids       plexsim")
diffs = []
for f in FIELDS:
    z = status(rd.ZEN, "/library/sections/1/all?type=1&group=" + f)
    k = status(rd.KIDS, "/library/sections/1/all?type=1&group=" + f)
    p = status(PS, "/library/sections/1/all?type=1&group=" + f)
    zc, kc, pc = z[0], k[0], p[0]
    mark = ""
    if zc == kc and zc != pc:
        mark = "  <<< DIFFERS"
        diffs.append((f, zc, pc))
    elif zc != kc:
        mark = "  (version)"
    print("%-24s %-10s %-10s %-10s%s" % (f, z, k, p, mark))
print()
print("%d fields differ: %s" % (len(diffs), diffs))
