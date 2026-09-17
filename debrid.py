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
import urllib.error
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


class Unavailable(Exception):
    """The debrid provider says the file itself is not there.

    Distinct from a transport failure: a real PMS answers a byte read of a part whose
    file it cannot open with a plain **404**, not a 5xx (measured cycle 44 on zen part
    23595, whose `checkFiles=1` reports `accessible=false`/`exists=false`).
    """


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
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            # Cycle 44: tell "this file is not there" apart from "the transport
            # failed". A real PMS answers a plain 404 to a byte read of a part whose
            # file it cannot open -- zen part 23595, whose `checkFiles=1` reports
            # `accessible=false`/`exists=false`, answers `404` with the binary refusal
            # header set -- where plexsim answered 502 to everything. Real-Debrid's
            # `unavailable_file` (error_code 24) and a 404 from its API are that case;
            # anything else stays a transport failure and keeps the 502, so a network
            # blip is still distinguishable from a dead link.
            detail = {}
            try:
                detail = json.loads(error.read() or b"{}")
            except Exception:
                detail = {}
            code = detail.get("error_code")
            if error.code == 404 or code == 24 or detail.get("error") == "unavailable_file":
                raise Unavailable(detail.get("error") or "unavailable_file")
            raise
        return payload["download"], int(payload.get("filesize") or 0)

    def invalidate(self, link):
        with self._lock:
            self._cache.pop(link, None)
