# Polling just stopped after 30 min of continuous YES. Both previous "absent"
# reads landed a few minutes after polling stopped, which would mean READING the
# resource list is what keeps the entry warm -- an odd causal direction worth
# one clean test rather than another theory. No announce here: this measures the
# same 30-minute-old registration, after a deliberate quiet gap.
import os, sys, time, json, urllib.request, uuid
sys.path.insert(0, '.')

def read_once():
    req = urllib.request.Request(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
        headers={"X-Plex-Token": os.environ["PLEX_TOKEN"],
                 "X-Plex-Client-Identifier": "rex-quiet-" + uuid.uuid4().hex[:12],
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        rows = json.load(r)
    return [x.get("name") for x in rows if "server" in (x.get("provides") or "")]

print("quiet gap of 300s with zero reads and zero announces...", flush=True)
time.sleep(300)
for i in range(4):
    names = read_once()
    print("  read %d after quiet gap: rex visible=%s  (%s)"
          % (i + 1, "YES" if "rex-spike" in names else "no", ",".join(n for n in names if n)), flush=True)
    if i < 3:
        time.sleep(60)
