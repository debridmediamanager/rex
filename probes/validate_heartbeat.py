# Validate the mitigation, not the diagnosis: with the heartbeat running, does
# visibility survive PAST the measured ~40-minute lease? Probed every 5 min for
# 60 min with a fresh identifier each time. A single 'no' past t+40min means the
# interval is wrong; all YES means the lease is being renewed.
import os, sys, time, json, urllib.request, uuid
TOKEN = os.environ["PLEX_TOKEN"]
def visible():
    req = urllib.request.Request(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
        headers={"X-Plex-Token": TOKEN,
                 "X-Plex-Client-Identifier": "simplex-val-" + uuid.uuid4().hex[:12],
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        rows = json.load(r)
    return "simplex-spike" in [x.get("name") for x in rows if "server" in (x.get("provides") or "")]
started = time.time()
bad = 0
for i in range(13):
    v = visible()
    m = (time.time() - started) / 60
    flag = ""
    if m > 40 and not v:
        bad += 1; flag = "  <- PAST THE LEASE AND ABSENT"
    print("  t+%-5.1f min visible=%s%s" % (m, "YES" if v else "no", flag), flush=True)
    if i < 12: time.sleep(300)
print("\nverdict: %s" % ("heartbeat FAILED to hold the lease" if bad else
      "heartbeat held the registration past the 40-minute lease"))
