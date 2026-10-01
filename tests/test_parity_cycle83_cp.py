#!/usr/bin/env python3
"""Cycle 83, territories C and P on the native server: the differences that were the native's.

Each check replays a reference exchange recorded in `tests/fixtures/cycle83_cp.json` (zen and
plexscratch42, both PMS 1.42.2) and asserts the measured value, not agreement between the two
servers.

Starts its own server (native by default, `PLEXSIM_SERVER=python` the oracle,
`PLEXSIM_SERVER=/path/to/binary` any build) on `tests/catalog_ui.json`, without `--state`.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "cycle83_cp.json")))
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self):
        self.port = free_port()
        env = dict(os.environ, RD_TOKEN="dummy", PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("TMDB_KEY", "PLEXSIM_STATE"):
            env.pop(name, None)
        self.proc = subprocess.Popen(
            server_command("--port", str(self.port), "--catalog", "tests/catalog_ui.json",
                           "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.3).close()
                return
            except OSError:
                time.sleep(0.2)
        raise SystemExit("server never listened")

    def req(self, method, path, headers=(), client="c83cp-check"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.putrequest(method, path, skip_accept_encoding=True)
        sent = dict(headers)
        sent.setdefault("X-Plex-Client-Identifier", client)
        for k, v in sent.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheaders(), body

    def json(self, method, path, client="c83cp-check"):
        status, _, body = self.req(method, path, {"Accept": "application/json"}, client)
        return status, (json.loads(body) if body.startswith(b"{") else None)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def agents_paged(srv):
    """zen, 1.42.2: `GET /system/agents?mediaType=1` with the X-Plex-Container pair as request
    headers and no Accept (python-plexapi's `PlexServer.agents()`) is the XML page -- `size 2
    totalSize 5 offset 1`, the second and third agents, and the two paging headers. The native
    server rendered this XML by hand past the pager, so it answered all five agents with no
    `totalSize`, and plexapi's `agents().totalSize` and `.offset` read None."""
    fx = FIXTURE["agents_paged"]
    status, headers, body = srv.req("GET", fx["request"]["path"], fx["request"]["headers"])
    text = body.decode("utf-8", "replace")
    top = re.search(r"<MediaContainer([^>]*)>", text)
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', top.group(1))) if top else {}
    ids = re.findall(r'<Agent [^>]*identifier="([^"]*)"', text)
    check("agents XML page: status", status == fx["status"], status)
    check("agents XML page: container size/totalSize/offset",
          {k: attrs.get(k) for k in fx["container"]} == fx["container"], attrs)
    check("agents XML page: the paged rows", ids == fx["identifiers"], ids)
    want = {k: v for k, v in fx["headers"] if k.startswith("X-Plex-Container")}
    got = {k: v for k, v in headers if k in want}
    check("agents XML page: the X-Plex-Container headers", got == want, got)
    status, headers, body = srv.req("GET", fx["request"]["path"],
                                    [("X-Plex-Container-Start", "0"), ("X-Plex-Container-Size", "100")])
    ids = re.findall(r'<Agent [^>]*identifier="([^"]*)"', body.decode("utf-8", "replace"))
    check("agents XML, one page of 100: every agent", ids == fx["unpaged_identifiers"], ids)


def main():
    srv = Server()
    try:
        agents_paged(srv)
    finally:
        srv.stop()
    print("PASS %d/%d" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
