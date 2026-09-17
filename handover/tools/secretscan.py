#!/usr/bin/env python3
"""Gate a cycle's commit on a secret scan of every file it changes.

Anchored on the VALUE, not on the pattern's own description (section 10): a naive
`X-Plex-Token=\\S+` matches this file's prose about the pattern and the suite's own
literal test token. The one known literal is excluded by name and by nothing else.
"""
import os
import re
import subprocess
import sys

WT = sys.argv[1]
KNOWN = {
    "X-Plex-Token=suite-gate-token",
    # Cycle 44's local Real-Debrid stub accepts these explicit fixture sentinels
    # without authentication. They are deliberately shaped like durable links so
    # the production handler is exercised, but they are not account-owned links.
    "real-debrid.com/d/PARTBYTESTEST",
    "real-debrid.com/d/PARTBYTESGONE",
}
PATTERNS = ((r"X-Plex-Token=[A-Za-z0-9_-]{15,}", "token in a URL"),
            (r"real-debrid\.com/d/[A-Za-z0-9]+", "real-debrid link"),
            (r"plex_token:\s*[A-Za-z0-9_-]{15,}", "plex token"))
names = subprocess.run(["git", "-C", WT, "diff", "--name-only", "origin/main"],
                       capture_output=True, text=True).stdout.split()
names += subprocess.run(["git", "-C", WT, "ls-files", "--others", "--exclude-standard"],
                        capture_output=True, text=True).stdout.split()
bad, total = [], 0
for name in sorted({n for n in names if n and n != "catalog.json"}):
    path = os.path.join(WT, name)
    if not os.path.isfile(path):
        continue
    text = open(path, encoding="utf-8", errors="replace").read()
    total += len(text)
    for pattern, label in PATTERNS:
        for hit in re.findall(pattern, text):
            if hit not in KNOWN:
                bad.append((name, label, hit[:24]))
    for var in ("PLEXLAB_TOKEN", "ZEN_TOKEN", "SCRATCH_TOKEN"):
        value = os.environ.get(var)
        if value and len(value) > 8 and value in text:
            bad.append((name, var, "PRESENT"))
print("scanned %d files, %d bytes" % (len({n for n in names if n}), total))
if bad:
    print("LEAKS:", bad)
    raise SystemExit(1)
print("no secrets")
