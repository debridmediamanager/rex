"""Which fields does the PARSER know, and which of the three answers does each get:
a Rule, an Error, or silence? Plus the listing's answer for the same key."""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, "/Users/bensarmiento/debridmediamanager/plexsim/handover/tools")
import rd

PS = os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32546")
NAMES = """title titleSort originalTitle summary tagline studio contentRating year decade
duration rating audienceRating userRating ratingCount viewCount skipCount viewOffset
lastViewedAt lastRatedAt addedAt updatedAt createdAt unwatched inProgress duplicate
unmatched resolution hdr dovi hdr10plus atmos mediaSize file audioLanguage subtitleLanguage
audioChannels videoCodec audioCodec subtitleCodec audioLayout genre collection label actor
director writer producer country id guid slug index parentIndex absoluteIndex mediaCount
editionTitle extraType parent type sourceType viewedLeafCount unviewedLeafCount
viewedOrInProgressLeafCount leafCount childCount episodeCount seasonCount chapterSource
originallyAvailableAt availableAt firstCharacter location unwatchedLeaves onDeck
contentRatingAge theme banner art thumb key ratingKey librarySectionID sectionID
playlistItemID smart placeholder hasThumbnail hasChapters hasMarkers markerType
optimizedForStreaming trash deletedAt subtitles audioStream videoStream""".split()


def fetch(base, path):
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


def shape(st, b):
    """rule / error / silent / status."""
    if st != 200:
        return "HTTP%d" % st
    try:
        q = json.loads(b)["Query"]
    except Exception:
        return "<non-json>"
    if "Error" in q:
        return "error"
    if "Rule" not in q:
        return "silent"
    r = q["Rule"]
    keys = [k for k in r if k not in ("value", "valueTitle")]
    return "rule(%s)" % ",".join("%s=%s" % (k, r[k]) if k in ("operator",) else k for k in keys)


SECTION = os.environ.get("SECTION", "1")
TYPE = os.environ.get("TYPE", "1")
PREFIX = os.environ.get("PREFIX", "")
rows = []
for name in NAMES:
    out = {}
    for base, label in ((rd.ZEN, "zen"), (rd.KIDS, "kids"), (PS, "sim")):
        out[label] = shape(*fetch(base, "/library/sections/%s/query/parse?type=%s&%s%s=1"
                                  % (SECTION, TYPE, PREFIX, name)))
    if out["zen"] == out["kids"] and out["zen"] != out["sim"]:
        rows.append((name, out["zen"], out["sim"]))
    elif out["zen"] != out["kids"]:
        rows.append((name, "VERSION zen=%s kids=%s" % (out["zen"], out["kids"]), out["sim"]))
print("field                      references                                   plexsim")
for name, ref, sim in rows:
    print("%-26s %-44s %s" % (name, ref, sim))
print(len(rows), "of", len(NAMES), "differ")
