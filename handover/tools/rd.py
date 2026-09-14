"""Driver's guarded GET/HEAD client for kids and zen. No writes, ever."""
import json
import os
import re
import urllib.parse
import urllib.request

WRITE_GET = re.compile(
    r"/:/(scrobble|unscrobble|rate|progress|timeline)|/refresh|/analyze|/emptyTrash|/optimize"
    r"|/clean|/match(?![a-z])|/unmatch(?![a-z])|/split|/merge|/actions/|/updater|/myplex/claim"
    r"|/downloadQueue"
    r"|/playQueues|/security/token|/sync|/diagnostics|/servers/|/transcode/universal/"
    r"|/video/:/transcode|[?&]url=")

KIDS = "http://kids:32400"
ZEN = "http://zen:32400"
TOK = {KIDS: os.environ["PLEXLAB_TOKEN"], ZEN: os.environ["ZEN_TOKEN"]}


def get(base, path, headers=None, method="GET", raw=False, timeout=60):
    assert base in TOK, base
    assert method in ("GET", "HEAD"), method
    assert not WRITE_GET.search(path), path
    h = {"X-Plex-Token": TOK[base], "User-Agent": "curl/8"}
    if not raw:
        h["Accept"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(base + path, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def mc(base, path, headers=None, timeout=60):
    st, _, body = get(base, path, headers=headers, timeout=timeout)
    if st != 200:
        return st, None
    return st, json.loads(body)["MediaContainer"]


def page(base, path, size=200, cap=None, timeout=90):
    """Every row of a listing, paged. Returns a list of Metadata rows."""
    out, start = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        st, c = mc(base, path, headers={"X-Plex-Container-Start": str(start),
                                        "X-Plex-Container-Size": str(size)}, timeout=timeout)
        if st != 200 or c is None:
            break
        rows = c.get("Metadata") or c.get("Directory") or []
        out.extend(rows)
        total = c.get("totalSize", c.get("size", 0))
        start += size
        if start >= total or not rows or (cap and len(out) >= cap):
            break
    return out
