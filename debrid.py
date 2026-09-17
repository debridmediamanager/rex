"""Real-Debrid link resolution for plexsim.

A catalog row carries the durable `https://real-debrid.com/d/XXXX` link.  The
playable CDN URL is derived from it by POSTing /unrestrict/link, and it expires,
so it is resolved lazily and cached with a TTL rather than baked into the
catalog.  Nothing here holds the whole file; the part handler only ever issues
Range reads against the resolved URL.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

# Overridable so the part path can be exercised without a credential. plexsim's byte
# path -- the Range grammar, the envelope, the byte counters -- was unmeasurable for the
# whole programme because every check needed a live account-scoped `/d/` link; cycle 44
# added `tests/test_part_bytes.py`, which stands a local stub here and a local CDN behind
# it and drives the REAL handler. Nothing but the base URL changes.
API = os.environ.get("RD_API") or "https://api.real-debrid.com/rest/1.0"

# Measured: an unrestricted RD CDN URL keeps serving well past an hour, but the
# cost of re-resolving is one small POST, so the cache is deliberately short.
TTL_SECONDS = 1800


class Resolver:
    def __init__(self, token, ttl=TTL_SECONDS):
        self._token = token
        self._ttl = ttl
        self._cache = {}  # restricted link -> (cdn url, resolved_at, size)
        self._lock = threading.Lock()

    def resolve(self, link):
        """Restricted /d/ link -> (cdn_url, size_bytes). Cached with a TTL."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(link)
            if hit and now - hit[1] < self._ttl:
                return hit[0], hit[2]

        url, size = self._unrestrict(link)

        with self._lock:
            self._cache[link] = (url, now, size)
        return url, size

    def _unrestrict(self, link):
        body = urllib.parse.urlencode({"link": link}).encode()
        req = urllib.request.Request(
            API + "/unrestrict/link",
            data=body,
            headers={"Authorization": "Bearer " + self._token},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
        return payload["download"], int(payload.get("filesize") or 0)

    def invalidate(self, link):
        with self._lock:
            self._cache.pop(link, None)
