"""zurg: where a Part's bytes come from.

rex reads no media itself. A Part's `file` is the path zurg's own mount gives the
file (`<mount>/<directory>/<release>/<file>`), and the same file is
`/dav/<directory>/<release>/<file>` on zurg's WebDAV, which answers a Range GET with the
bytes -- the path rclone's mount reads through, so archives, failover and every
provider are zurg's code rather than a copy of it.

`ZURG_URL` (with `ZURG_USER`/`ZURG_PASSWORD` when zurg asks for basic auth) and
`--zurg-mount` (zurg's `mount_path`) configure it. The native server reads the same
bytes through its sources (`rust/src/runtime/source.rs`) and takes its library from
what they push (`rust/src/runtime/feed.rs`); the oracle has no feed.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

UPSTREAM_UA = "rex/0.1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Zurg:
    def __init__(self, base, user="", password="", mount="/mnt/zurg"):
        self.base = base.rstrip("/")
        self._auth = None
        if user or password:
            self._auth = "Basic " + base64.b64encode(
                ("%s:%s" % (user, password)).encode()).decode()
        self.mount = mount.rstrip("/") or "/"

    @classmethod
    def from_env(cls, mount):
        base = os.environ.get("ZURG_URL")
        if not base:
            return None
        return cls(base, os.environ.get("ZURG_USER", ""), os.environ.get("ZURG_PASSWORD", ""),
                   mount)

    def dav_path(self, file):
        """`<mount>/a/b` -> `/dav/a/b`; None for a path outside the mount."""
        if not file:
            return None
        if self.mount == "/":
            rest = file[1:] if file.startswith("/") else None
        else:
            rest = file[len(self.mount) + 1:] if file.startswith(self.mount + "/") else None
        rest = (rest or "").rstrip("/")
        return "/dav/" + rest if rest else None

    def url(self, dav):
        # A `+` in a release name is a literal plus, never a space.
        return self.base + "/".join(urllib.parse.quote(segment, safe="") for segment in
                                    dav.split("/"))

    def headers(self):
        headers = {"User-Agent": UPSTREAM_UA}
        if self._auth:
            headers["Authorization"] = self._auth
        return headers

    def open(self, dav, headers):
        merged = self.headers()
        merged.update(headers)
        request = urllib.request.Request(self.url(dav), headers=merged, method="GET")
        return urllib.request.urlopen(request, timeout=30)

    def exists(self, dav):
        """`checkFiles`: only zurg's own 404 is a missing file; a transport failure or
        any other answer is not collapsed into one."""
        request = urllib.request.Request(self.url(dav), headers=self.headers(), method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=15):
                return True
        except urllib.error.HTTPError as error:
            return error.code != 404
        except Exception:
            return True

    def redirect_target(self, dav):
        """Where `--part-mode redirect` sends a client: zurg's signed `.strm` URL needs no
        credential and answers with the provider's download URL. None when zurg streams
        the file itself (an archive member, a Usenet release): then rex proxies."""
        escaped = "/".join(urllib.parse.quote(segment, safe="") for segment in dav.split("/"))
        request = urllib.request.Request(
            self.base + "/api/library/entry?path=" + urllib.parse.quote(escaped, safe=""),
            headers=self.headers())
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                signed = (json.load(response) or {}).get("signed_path")
        except Exception:
            return None
        if not signed:
            return None
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(urllib.request.Request(self.base + signed,
                                               headers={"User-Agent": UPSTREAM_UA}), timeout=30)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location") or ""
            if error.code in (301, 302, 303, 307, 308) and location.startswith("http"):
                return location
        except Exception:
            return None
        return None
