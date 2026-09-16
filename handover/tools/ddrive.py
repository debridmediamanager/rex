#!/usr/bin/env python3
"""Drive all 78 client profiles' PLAIN-FORM decision against one row, and record it.

    python3 ddrive.py <base> <token> <ratingKey> <out.json> [profiles.json]

One GET per profile -- `X-Plex-Client-Profile-Name` is the first name the reference resolves a
profile file by (cycle 31) -- with no `hasMDE`, so the answer is the client's own profile run
against the item (cycle 32, `decision.py`). Writes `{profile: [status, {the four decision
codes and their texts}]}`, which is what `tests/fixtures/decisions/cells.jsonl` is built from.

READ-ONLY: a decision endpoint is a GET and arms nothing but a session, so this may be pointed
at kids or zen as well as at a writable copy. What is NOT read-only is the state the answer
depends on: a row with no per-user audio selection answers differently from the same row after
`PUT /library/parts/{id}?audioStreamID=<id>`, and `audioStreamID=-1` clears that again (cycle
34). Record which state a run was taken in; the item's Stream rows do not say.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE, TOKEN, RATING_KEY, OUT = sys.argv[1:5]
PROFILES = sys.argv[5] if len(sys.argv) > 5 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "client_profiles.json")

FIELDS = ("directPlayDecisionCode", "directPlayDecisionText",
          "generalDecisionCode", "generalDecisionText",
          "transcodeDecisionCode", "transcodeDecisionText")

answers = {}
for name in sorted(json.load(open(PROFILES, encoding="utf-8"))):
    query = urllib.parse.urlencode({
        "path": "/library/metadata/%s" % RATING_KEY,
        "mediaIndex": "0", "partIndex": "0", "protocol": "http",
        "directPlay": "1", "directStream": "1",
        "X-Plex-Token": TOKEN,
        "X-Plex-Client-Profile-Name": name,
        "X-Plex-Client-Identifier": "ddrive",
    })
    request = urllib.request.Request(
        BASE.rstrip("/") + "/video/:/transcode/universal/decision?" + query,
        headers={"Accept": "application/json", "User-Agent": "Plex/4.0 ddrive"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            container = json.loads(response.read() or b"{}").get("MediaContainer", {})
            answers[name] = [response.status,
                             {field: container.get(field) for field in FIELDS}]
    except urllib.error.HTTPError as error:
        answers[name] = [error.code, {"body": error.read().decode("utf-8", "replace")[:200]}]
with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(answers, handle, indent=1, sort_keys=True)
print("%s: %d profiles" % (OUT, len(answers)))
