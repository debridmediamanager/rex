# The decisive question: a real Plex client has a PERSISTENT
# X-Plex-Client-Identifier. Fresh-identifier reads always saw rex; reads
# from persistent identities (python-plexapi, heartbeat.is_visible) repeatedly
# did not. If plex.tv caches the resource list per client identifier, then real
# clients can go blind to rex while my instrument says everything is fine --
# which would make the fresh-identifier measurements worthless for the question
# that matters.
#
# Design: one FIXED identifier read every 60s, and a FRESH identifier read at
# the same instant, side by side. Announce once at the halfway mark and watch
# whether that clears a stale fixed-identifier view.
import os, sys, time, json, urllib.request, uuid
sys.path.insert(0, 'oracle')
import heartbeat

TOKEN = os.environ["PLEX_TOKEN"]
DEVICE_ID = os.environ["REX_DEVICE_ID"]    # the numeric plex.tv device id to re-announce
PUBLIC_URL = os.environ["REX_PUBLIC_URL"]
FIXED = "rex-fixed-client-7f3a91"   # stands in for a real client's identity

def read(cid):
    req = urllib.request.Request(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
        headers={"X-Plex-Token": TOKEN, "X-Plex-Client-Identifier": cid,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        rows = json.load(r)
    names = [x.get("name") for x in rows if "server" in (x.get("provides") or "")]
    return "rex-spike" in names

print("t      fixed-id  fresh-id   note", flush=True)
started = time.time()
for i in range(13):
    note = ""
    if i == 6:
        heartbeat.announce(TOKEN, DEVICE_ID, PUBLIC_URL,
                           name="rex-spike")
        note = "<- re-announced here"
    f = read(FIXED)
    n = read("rex-fresh-" + uuid.uuid4().hex[:12])
    print("t+%-5ds %-9s %-10s %s"
          % (int(time.time() - started), "YES" if f else "no",
             "YES" if n else "no", note), flush=True)
    if i < 12:
        time.sleep(60)
