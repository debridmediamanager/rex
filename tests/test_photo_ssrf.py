#!/usr/bin/env python3
"""`/photo/:/transcode?url=` fetches no address on rex's own networks, black-box.

An absolute `url` is fetched and its body answered (remote artwork, a poster picked by url).
Any token holder -- a share's friend, or a stolen token -- could name `http://127.0.0.1:...`
or a LAN host and read back what rex can reach and they cannot: zurg, a router, another
service. Plex2Shell (2026-10-19) opened with that SSRF in a real PMS. A stub on loopback
stands in for such a service; it must never be dialled.

    python3 tests/test_photo_ssrf.py
"""
import http.client
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from server_command import server_command
from test_ports import free_port
from test_unclaimed_gate import own_address

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
TOKEN = "photo-ssrf-token"
SECRET = b"lan-only-secret"
results = []
hits = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- %r" % (detail,)))


class Lan(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(SECRET)))
        self.end_headers()
        self.wfile.write(SECRET)

    def log_message(self, *_):
        pass


def photo(port, url):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", "/photo/:/transcode?width=300&height=450&url=%s&X-Plex-Token=%s"
                     % (urllib.parse.quote(url, safe=""), TOKEN))
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def main():
    lan = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Lan)
    lan_port = lan.server_address[1]
    threading.Thread(target=lan.serve_forever, daemon=True).start()
    port = int(os.environ.get("REX_TEST_PORT") or free_port())
    log = tempfile.TemporaryFile()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
        env.pop(name, None)
    proc = subprocess.Popen(server_command("--port", port, "--catalog", CATALOG, "--gdm", "off",
                                           "--token", TOKEN, root=ROOT),
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            raise SystemExit("rex did not start")
        hosts = ["127.0.0.1", "localhost", "127.8.9.10", "0.0.0.0", "[::ffff:127.0.0.1]"]
        address = own_address()
        if address is None:
            print("SKIP the LAN address: no non-loopback IPv4 address")
        else:
            hosts.append(address)
        for host in hosts:
            url = "http://%s:%d/library/secret.jpg" % (host, lan_port)
            got, body = photo(port, url)
            check("a url on %s is a 404 with nothing of it" % host,
                  got == 404 and SECRET not in body, (got, body[:60]))
        check("the LAN stub was never dialled", not hits, hits)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        lan.shutdown()
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    if passed != len(results):
        log.seek(0)
        print(log.read().decode(errors="replace")[-3000:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
