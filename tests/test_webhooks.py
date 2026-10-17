#!/usr/bin/env python3
"""A claimed server posts its owner's webhooks: play, pause, resume, scrobble and stop.

The list is read as a claimed 1.42.2 PMS read it at its start, `GET
plex.tv/api/v2/server/webhooks?X-Plex-Token=<server token>`
(`tests/fixtures/plextv_registration_s1422.json`), from a local stub plex.tv
(`REX_PLEXTV_URL`) that lists one webhook pointed at a receiver here. The events and the
multipart `payload` part follow Plex's documented webhooks (support.plex.tv, "Webhooks"),
not a recording. Before this rex posted nothing.

    python3 tests/test_webhooks.py
"""
import email
import email.policy
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MACHINE = "f1x7u4e0000000000000000000000000000000f1"  # catalog_ui.json's own; a claim for another is ignored
SERVER_TOKEN = "webhook-suite-server-token"
KEY = "1007"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "   %s" % (detail,)))


class Recorder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler):
        super().__init__(("127.0.0.1", free_port()), handler)
        self.records = []


class PlexTv(BaseHTTPRequestHandler):
    """plex.tv as REX_PLEXTV_URL reaches it (`/<host><path>`): one webhook, empty elsewhere."""

    def do_ANY(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        url = urlsplit(self.path)
        self.server.records.append((self.command, url.path, parse_qsl(url.query)))
        if url.path == "/plex.tv/api/v2/server/webhooks":
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n<users>\n<user id="1" owner="1">\n'
                    '  <webhooks>\n    <webhook url="%s"/>\n  </webhooks>\n</user>\n</users>\n'
                    % self.server.hook).encode()
        else:
            body = b'<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="0"/>\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_ANY

    def log_message(self, *_):
        pass


class Hook(BaseHTTPRequestHandler):
    """The owner's webhook: keeps each multipart `payload` part."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        kind = self.headers.get("Content-Type", "")
        message = email.message_from_bytes(b"Content-Type: " + kind.encode() + b"\r\n\r\n" + body,
                                           policy=email.policy.HTTP)
        parts = [part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "payload"] \
            if message.is_multipart() else []
        self.server.records.append((kind, json.loads(parts[0].get_content()) if parts else None))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


def beat(port, state, offset):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&state=%s"
                     "&time=%d&duration=600000" % (KEY, KEY, state, offset), headers={
                         "Accept": "application/json", "X-Plex-Client-Identifier": "webhook-player",
                         "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
                         "X-Plex-Device-Name": "Webhook Player", "X-Plex-Token": SERVER_TOKEN})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def wait_for(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.1)
    return predicate()


def main():
    plextv, hook = Recorder(PlexTv), Recorder(Hook)
    plextv.hook = "http://127.0.0.1:%d/hook" % hook.server_address[1]
    for server in (plextv, hook):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    directory = tempfile.mkdtemp(prefix="rex-webhooks-")
    shutil.copy(os.path.join(HERE, "catalog_ui.json"), os.path.join(directory, "catalog.json"))
    # What a claim leaves beside the catalog, the stub's server token in it.
    with open(os.path.join(directory, "claim_state.json"), "w") as handle:
        json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner", "email": "owner@example.com",
                   "feature_uuids": [], "account_id": 1, "view_state_cursor": None}, handle)
    port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               REX_PLEXTV_URL="http://127.0.0.1:%d" % plextv.server_address[1])
    for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN", "TMDB_KEY", "REX_METADATA_URL",
                 "REX_SCROBBLING_DECRYPT_SECRET_B64"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(directory, "catalog.json"),
                       "--gdm", "off", "--account-state-interval", 3600, root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        for state, offset in (("playing", 60000), ("paused", 61000), ("playing", 62000),
                              ("playing", 560000), ("stopped", 561000)):
            check("timeline %s at %d answers 200" % (state, offset), beat(port, state, offset) == 200)
        want = ["media.play", "media.pause", "media.resume", "media.scrobble", "media.stop"]
        got = wait_for(lambda: len(hook.records) >= len(want) and hook.records, 15) or hook.records
        events = [payload and payload.get("event") for _, payload in got]
        check("the webhook receives play, pause, resume, scrobble and stop, in order", events == want, events)
        reads = [query for method, path, query in plextv.records
                 if (method, path) == ("GET", "/plex.tv/api/v2/server/webhooks")]
        check("the list is read from plex.tv with the server token, as a claimed PMS reads it",
              reads and reads[0] == [("X-Plex-Token", SERVER_TOKEN)], plextv.records)
        if got:
            kind, payload = got[0]
            check("each event is a multipart POST with a payload part",
                  kind.startswith("multipart/form-data; boundary=") and payload, kind)
            payload = payload or {}
            check("the payload names the server, the owner and the player",
                  payload.get("Server") == {"title": payload.get("Server", {}).get("title"), "uuid": MACHINE}
                  and payload.get("Account", {}).get("id") == 1 and payload.get("owner") is True
                  and payload.get("Player", {}).get("uuid") == "webhook-player"
                  and payload.get("Player", {}).get("title") == "Webhook Player", payload)
            metadata = payload.get("Metadata", {})
            check("Metadata is the item, with its library's type and none of rex's own fields",
                  str(metadata.get("ratingKey")) == KEY and metadata.get("librarySectionType") in ("movie", "show")
                  and not [name for name in metadata if name.startswith("_")], metadata)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(directory, ignore_errors=True)
    if not all(results):
        log.seek(0)
        sys.stdout.write(log.read().decode(errors="replace")[-3000:])
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
