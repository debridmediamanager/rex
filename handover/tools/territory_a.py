"""Territory A: what every allowed WRITE pushes on both notification channels.
Usage: territory_a.py ref|sim
Every write on the reference is undone right after, under capture too."""
import json
import sys
import time
import urllib.parse

import lib

which = sys.argv[1]
if which == "ref":
    S = lib.REF
    MID = "78a364ad887787f74e834a5c48c987d3e6fb165a"
    MOVIE = "1"        # Big Buck Bunny: unrated, unwatched
    MOVIE2 = "17"      # Elephants Dream: has a summary to restore
    SECTION = "1"
    DURATION = 10000
else:
    S = lib.SIM
    MID = "f1x7u4e0000000000000000000000000000000f1"
    MOVIE = "1007"
    MOVIE2 = "1008"
    SECTION = "1"
    DURATION = None

WINDOW = 7
transcript = []
log = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    log.append(line)


def row(key):
    st, c, _ = lib.jreq(S, "GET", "/library/metadata/%s" % key)
    m = (c.get("Metadata") or [{}])[0] if isinstance(c, dict) else {}
    return {k: m.get(k) for k in ("ratingKey", "title", "summary", "viewCount", "viewOffset",
                                  "userRating", "lastViewedAt", "lastRatedAt", "updatedAt", "Collection")}


def write(label, method, path, headers=None, window=WINDOW):
    """Perform a write with both channels open, record frames and the response."""
    say("\n### %s  %s %s" % (label, method, path))
    result = {}

    def trigger():
        result["resp"] = lib.req(S, method, path, headers=headers)
        result["t"] = time.time()

    out = lib.measure(S, trigger, window)
    st, body, hd = result["resp"]
    say("  -> %s %s" % (st, body[:300]))
    ws = [(t, f) for t, f in out["ws"][1] if "_ping" not in f]
    sse = [(t, n, d) for t, n, d in out["sse"][1] if n != "ping"]
    say("  ws frames (%d, +%d pings):" % (len(ws), len(out["ws"][1]) - len(ws)))
    say(lib.fmt_ws(ws))
    say("  sse events (%d, +%d pings):" % (len(sse), len(out["sse"][1]) - len(sse)))
    say(lib.fmt_sse(sse))
    transcript.append({"label": label, "method": method, "path": path, "status": st,
                       "body": body[:2000].decode("utf-8", "replace"),
                       "headers": hd, "ws": ws, "sse": sse})
    return st, body, hd


ID = "com.plexapp.plugins.library"
URI = "server://%s/com.plexapp.plugins.library/library/metadata/" % MID

say("== before:", json.dumps(row(MOVIE)), json.dumps(row(MOVIE2)))

# 1. rate, and clear it again
write("rate 8", "GET", "/:/rate?key=%s&identifier=%s&rating=8" % (MOVIE, ID))
say("  after rate:", json.dumps(row(MOVIE)))
write("rate clear (-1)", "GET", "/:/rate?key=%s&identifier=%s&rating=-1" % (MOVIE, ID))
say("  after clear:", json.dumps(row(MOVIE)))

# 2. scrobble / unscrobble
write("scrobble", "GET", "/:/scrobble?key=%s&identifier=%s" % (MOVIE, ID))
say("  after scrobble:", json.dumps(row(MOVIE)))
write("unscrobble", "GET", "/:/unscrobble?key=%s&identifier=%s" % (MOVIE, ID))
say("  after unscrobble:", json.dumps(row(MOVIE)))

# 3. a timeline play that completes, then stops
if DURATION is None:
    st, c, _ = lib.jreq(S, "GET", "/library/metadata/%s" % MOVIE)
    DURATION = int((c["Metadata"][0].get("duration") or 600000))
th = {"X-Plex-Session-Identifier": "cycle6-session-1"}
base_tl = "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&identifier=%s&duration=%d" % (MOVIE, MOVIE, ID, DURATION)
write("timeline playing t=0", "GET", base_tl + "&state=playing&time=0&hasMDE=1", headers=th)
say("  sessions:", lib.jreq(S, "GET", "/status/sessions")[1])
late = int(DURATION * 0.95)
write("timeline playing t=95%%", "GET", base_tl + "&state=playing&time=%d&hasMDE=1" % late, headers=th)
say("  after 95%%:", json.dumps(row(MOVIE)))
write("timeline stopped", "GET", base_tl + "&state=stopped&time=%d&hasMDE=1" % (late + 100), headers=th)
say("  after stop:", json.dumps(row(MOVIE)))
st, c, _ = lib.jreq(S, "GET", "/status/sessions/history/all")
hist = [m for m in (c.get("Metadata") or [])] if isinstance(c, dict) else []
say("  history rows:", json.dumps([{k: m.get(k) for k in ("historyKey", "ratingKey", "viewedAt")} for m in hist]))

# 4. history delete of the row this play created
mine = [m for m in hist if str(m.get("ratingKey")) == MOVIE]
if mine:
    hk = mine[-1]["historyKey"]
    write("history delete", "DELETE", hk)
    say("  after history delete:", json.dumps(row(MOVIE)))
    st, c, _ = lib.jreq(S, "GET", "/status/sessions/history/all")
    say("  history now:", c.get("size") if isinstance(c, dict) else c)
else:
    say("  NO history row created by the play")
r = row(MOVIE)
if r.get("viewCount"):
    write("unscrobble (restore after play)", "GET", "/:/unscrobble?key=%s&identifier=%s" % (MOVIE, ID))
    say("  after restore unscrobble:", json.dumps(row(MOVIE)))

# 5. a summary edit, restored to the exact prior text
prior = row(MOVIE2)["summary"] or ""
write("summary edit", "PUT", "/library/sections/%s/all?type=1&id=%s&summary.value=%s"
      % (SECTION, MOVIE2, urllib.parse.quote("cycle6 probe summary")))
say("  after edit:", json.dumps(row(MOVIE2)))
write("summary restore", "PUT", "/library/sections/%s/all?type=1&id=%s&summary.value=%s"
      % (SECTION, MOVIE2, urllib.parse.quote(prior)))
say("  after restore:", json.dumps(row(MOVIE2)), "same as prior:", row(MOVIE2)["summary"] == prior)

# 6. playlist create / add / remove / delete
st, body, hd = write("playlist create", "POST", "/playlists?type=video&title=cycle6-pl&smart=0&uri=%s"
                     % urllib.parse.quote(URI + MOVIE, safe=""))
try:
    pl = json.loads(body)["MediaContainer"]["Metadata"][0]
    plk = pl["ratingKey"]
except Exception:
    pl, plk = None, None
say("  playlist:", plk)
if plk:
    write("playlist add", "PUT", "/playlists/%s/items?uri=%s" % (plk, urllib.parse.quote(URI + MOVIE2, safe="")))
    st, c, _ = lib.jreq(S, "GET", "/playlists/%s/items" % plk)
    items = c.get("Metadata") or []
    say("  items:", [(m.get("ratingKey"), m.get("playlistItemID")) for m in items])
    target = [m for m in items if str(m.get("ratingKey")) == MOVIE2]
    if target:
        write("playlist remove item", "DELETE", "/playlists/%s/items/%s" % (plk, target[0]["playlistItemID"]))
    write("playlist delete", "DELETE", "/playlists/%s" % plk)
    say("  playlists now:", lib.jreq(S, "GET", "/playlists")[1].get("size"))

# 7. collection create / add / remove / delete
st, body, hd = write("collection create", "POST",
                     "/library/collections?type=1&title=cycle6-col&smart=0&sectionId=%s&uri=%s"
                     % (SECTION, urllib.parse.quote(URI + MOVIE, safe="")))
try:
    col = json.loads(body)["MediaContainer"]["Metadata"][0]
    colk = col["ratingKey"]
except Exception:
    col, colk = None, None
say("  collection:", colk)
if colk:
    write("collection add", "PUT", "/library/collections/%s/items?uri=%s" % (colk, urllib.parse.quote(URI + MOVIE2, safe="")))
    st, c, _ = lib.jreq(S, "GET", "/library/collections/%s/children" % colk)
    say("  children:", [(m.get("ratingKey")) for m in (c.get("Metadata") or [])])
    write("collection remove item", "DELETE", "/library/collections/%s/children/%s" % (colk, MOVIE2))
    say("  movie2 collections now:", json.dumps(row(MOVIE2).get("Collection")))
    write("collection delete", "DELETE", "/library/collections/%s" % colk)
    say("  collections now:", lib.jreq(S, "GET", "/library/sections/%s/collections" % SECTION)[1].get("size"))
    say("  movie collections now:", json.dumps(row(MOVIE).get("Collection")))

say("\n== after:", json.dumps(row(MOVIE)), json.dumps(row(MOVIE2)))
json.dump({"transcript": transcript, "log": log}, open("a_%s.json" % which, "w"), indent=1, default=str)
