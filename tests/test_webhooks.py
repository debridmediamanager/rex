#!/usr/bin/env python3
"""Cloud and local webhooks: recipient isolation, owner controls, persistence and JPEGs.

The cloud list follows the claimed 1.42.2 PMS's recorded request to
plex.tv/api/v2/server/webhooks (fixtures/plextv_registration_s1422.json), served by
a local stub. The multipart payload/thumb parts follow Plex's documented Webhooks
contract, not a recording. FFmpeg generates the independent JPEG source and decodes
the received multipart bytes. Local configuration is rex's owner-only JSON API;
the genuine shared-account access-token fixture exercises its authorization boundary.

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
from test_accounts import SHARED, SHARED_ID

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
        self.thumbs = []
        self.image = b""
        self.image_headers = []


def webhooks_list(owner_hook, shared_hook):
    """rex-fun's live list (`fixtures/plextv_server_webhooks_shared_s1022.xml`): one `<user>` per
    account with access, the owner `owner="1"`. None had a webhook, so the owner's and the first
    shared user's are filled in here, as plex.tv lists them for a shared user who set one (rex #10)."""
    with open(os.path.join(HERE, "fixtures", "plextv_server_webhooks_shared_s1022.xml")) as handle:
        users = handle.read().split("<user ")
    fill = lambda user, hook: user.replace("<webhooks>\n", '<webhooks>\n    <webhook url="%s"/>\n' % hook, 1)
    users = [fill(user, owner_hook) if owner_hook and 'owner="1"' in user else user for user in users]
    # Align the separately anonymized webhook and access-token recordings.
    users[1] = fill(users[1], shared_hook).replace('id="900000001"', 'id="%s"' % SHARED_ID, 1)
    return "<user ".join(users)


class PlexTv(BaseHTTPRequestHandler):
    """plex.tv as REX_PLEXTV_URL reaches it (`/<host><path>`): one webhook, empty elsewhere."""

    def do_ANY(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        url = urlsplit(self.path)
        self.server.records.append((self.command, url.path, parse_qsl(url.query)))
        if url.path == "/plex.tv/api/v2/server/access_tokens":
            with open(os.path.join(HERE, "fixtures", "plextv_access_tokens_shared_s1434.json")) as source:
                body = json.load(source)["body"].encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/plex.tv/api/v2/server/webhooks":
            body = webhooks_list(self.server.hook, self.server.shared_hook).encode()
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

    def do_GET(self):
        self.server.image_headers.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(self.server.image)))
        self.end_headers()
        self.wfile.write(self.server.image)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        kind = self.headers.get("Content-Type", "")
        message = email.message_from_bytes(b"Content-Type: " + kind.encode() + b"\r\n\r\n" + body,
                                           policy=email.policy.HTTP)
        parts = [part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "payload"] \
            if message.is_multipart() else []
        self.server.records.append((kind, json.loads(parts[0].get_content()) if parts else None))
        thumbs = [part for part in message.iter_parts()
                  if part.get_param("name", header="content-disposition") == "thumb"] if message.is_multipart() else []
        self.server.thumbs.append([(part.get_content_type(), part.get_payload(decode=True)) for part in thumbs])
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


def beat(port, state, offset, token=SERVER_TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&state=%s"
                     "&time=%d&duration=600000" % (KEY, KEY, state, offset), headers={
                         "Accept": "application/json", "X-Plex-Client-Identifier": "webhook-player",
                         "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
                         "X-Plex-Device-Name": "Webhook Player", "X-Plex-Token": token})
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


def configure(port, method="GET", value=None, token=SERVER_TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, "/rex/webhooks", body=json.dumps(value) if value is not None else None,
                     headers={"X-Plex-Token": token, "Content-Type": "application/json",
                              "Accept": "application/json"})
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body) if response.status == 200 else body
    finally:
        conn.close()


def main():
    plextv, hook, local, shared = Recorder(PlexTv), Recorder(Hook), Recorder(Hook), Recorder(Hook)
    plextv.hook = "http://127.0.0.1:%d/hook" % hook.server_address[1]
    plextv.shared_hook = "http://127.0.0.1:%d/shared" % shared.server_address[1]
    local_url = "http://127.0.0.1:%d/local" % local.server_address[1]
    ffmpeg = os.environ.get("REX_TRANSCODER") or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("test_webhooks requires ffmpeg to generate and decode the real JPEG fixture")
    hook.image = subprocess.check_output([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32",
                                         "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "-"])
    for server in (plextv, hook, local, shared):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    directory = tempfile.mkdtemp(prefix="rex-webhooks-")
    with open(os.path.join(HERE, "catalog_ui.json")) as source:
        catalog = json.load(source)
    # Feed/source-provided artwork URL, not a payload serialized by rex.
    def set_art(value):
        if isinstance(value, dict):
            if str(value.get("ratingKey")) == KEY:
                value["_poster_url"] = "http://127.0.0.1:%d/poster.jpg" % hook.server_address[1]
                value["contentRating"] = "PG"  # visible to the recorded shared grant
            for child in value.values():
                set_art(child)
        elif isinstance(value, list):
            for child in value:
                set_art(child)
    set_art(catalog)
    with open(os.path.join(directory, "catalog.json"), "w") as target:
        json.dump(catalog, target)
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
                       "--state", os.path.join(directory, "state.db"),
                       "--gdm", "off", "--account-state-interval", 3600, root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        check("local hooks start empty", configure(port) == (200, {"urls": []}))
        urls = [local_url, plextv.hook, local_url]
        check("local configuration deduplicates preserving order",
              configure(port, "PUT", {"urls": urls}) == (200, {"urls": urls[:2]}))
        for invalid in ("file:///etc/passwd", "http://", "http://host:bad/path", "http://u:p@host/",
                        "http://host/#fragment", "http://host/\r\nInjected: yes"):
            check("invalid URL is refused: %r" % invalid,
                  configure(port, "PUT", {"urls": [invalid]})[0] == 400)
        check("invalid configuration leaves the list unchanged",
              configure(port) == (200, {"urls": urls[:2]}))
        check("a real shared account cannot read local configuration",
              configure(port, token="SHARED-TOKEN")[0] == 403)
        for method in ("PUT", "DELETE"):
            check("a real shared account cannot %s local configuration" % method,
                  configure(port, method, {"urls": []}, token="SHARED-TOKEN")[0] == 403)
        for state, offset in (("playing", 60000), ("paused", 61000), ("playing", 62000),
                              ("playing", 560000), ("stopped", 561000)):
            check("timeline %s at %d answers 200" % (state, offset), beat(port, state, offset) == 200)
        want = ["media.play", "media.pause", "media.resume", "media.scrobble", "media.stop"]
        got = wait_for(lambda: len(hook.records) >= len(want) and hook.records, 15) or hook.records
        events = [payload and payload.get("event") for _, payload in got]
        check("the webhook receives play, pause, resume, scrobble and stop, in order", events == want, events)
        check("a shared user's webhook gets none of the owner's playback (rex #10)", not shared.records,
              [payload and payload.get("event") for _, payload in shared.records])
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
        wait_for(lambda: len(local.records) >= len(want), 15)
        check("local and cloud lists deliver each event once", len(local.records) == len(want)
              and len(hook.records) == len(want), (len(local.records), len(hook.records)))
        thumbs = hook.thumbs[0] if hook.thumbs else []
        decoded = subprocess.run([ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
                                 input=thumbs[0][1] if thumbs else b"", capture_output=True)
        check("multipart thumb is a decodable JPEG, beside JSON payload",
              len(thumbs) == 1 and thumbs[0][0] == "image/jpeg" and decoded.returncode == 0,
              decoded.stderr.decode(errors="replace"))
        check("artwork requests never contain owner credentials",
              hook.image_headers and all(not any(k.lower() in ("x-plex-token", "authorization") for k in headers)
                                         for headers in hook.image_headers), hook.image_headers)
        before_owner, before_local = len(hook.records), len(local.records)
        for state, offset in (("playing", 60000), ("stopped", 61000)):
            check("shared timeline %s answers 200" % state, beat(port, state, offset, SHARED) == 200)
        wait_for(lambda: len(shared.records) >= 2, 15)
        own_shared = tuple(shared.records)
        check("a shared callback receives exactly its own play and stop",
              [payload.get("event") for _, payload in own_shared] == ["media.play", "media.stop"]
              and all(payload.get("Account", {}).get("id") == int(SHARED_ID) for _, payload in own_shared),
              own_shared)
        wait_for(lambda: len(hook.records) >= before_owner + 2 and len(local.records) >= before_local + 2, 15)
        check("owner and local callbacks also receive shared playback",
              len(hook.records) == before_owner + 2 and len(local.records) == before_local + 2)
        # Orderly restart saves ordinary preferences; no webhook-specific store.
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = subprocess.Popen(
            server_command("--port", port, "--catalog", os.path.join(directory, "catalog.json"),
                           "--state", os.path.join(directory, "state.db"),
                           "--gdm", "off", "--account-state-interval", 3600, root=ROOT),
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        def restored():
            try:
                return configure(port) == (200, {"urls": urls[:2]})
            except OSError:
                return False
        check("local hooks restore after restart", wait_for(restored, 15))
        before = len(local.records)
        beat(port, "playing", 10000)
        check("restored local configuration actually delivers", wait_for(lambda: len(local.records) > before, 15))
        check("PUT replaces configuration immediately",
              configure(port, "PUT", {"urls": [plextv.hook]}) == (200, {"urls": [plextv.hook]}))
        before_cloud, before_local = len(hook.records), len(local.records)
        beat(port, "paused", 11000)
        wait_for(lambda: len(hook.records) > before_cloud, 15)
        check("replaced local URL receives no further event", len(local.records) == before_local)
        check("DELETE clears the local list", configure(port, "DELETE") == (200, {"urls": []}))
        before_cloud = len(hook.records)
        hook.image = b"not an image"
        beat(port, "playing", 12000)
        check("cloud hook survives local deletion and missing thumbnail",
              wait_for(lambda: len(hook.records) == before_cloud + 1, 15) and not hook.thumbs[-1])
        check("local-only list can be installed without a cloud overlap",
              configure(port, "PUT", {"urls": [local_url]}) == (200, {"urls": [local_url]}))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        with open(os.path.join(directory, "claim_state.json")) as saved:
            claimed_state = saved.read()
        os.remove(os.path.join(directory, "claim_state.json"))
        proc = subprocess.Popen(
            server_command("--port", port, "--catalog", os.path.join(directory, "catalog.json"),
                           "--state", os.path.join(directory, "state.db"), "--token", SERVER_TOKEN,
                           "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        def unclaimed_ready():
            try:
                return configure(port) == (200, {"urls": [local_url]})
            except OSError:
                return False
        check("control-token local hooks need no claim or cloud entitlement", wait_for(unclaimed_ready, 15))
        check("an unclaimed server still refuses unauthenticated configuration",
              configure(port, token="")[0] == 401)
        before_local = len(local.records)
        beat(port, "playing", 20000)
        check("control-token local hook actually receives an event",
              wait_for(lambda: len(local.records) > before_local, 15))
        check("shared callback remains private across restart and local configuration changes",
              tuple(shared.records) == own_shared, shared.records)
        assert configure(port, "DELETE") == (200, {"urls": []})
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        with open(os.path.join(directory, "claim_state.json"), "w") as saved:
            saved.write(claimed_state)
        plextv.hook = None  # only the shared account has a cloud callback
        proc = subprocess.Popen(
            server_command("--port", port, "--catalog", os.path.join(directory, "catalog.json"),
                           "--state", os.path.join(directory, "state.db"),
                           "--gdm", "off", "--account-state-interval", 3600, root=ROOT),
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        def no_local_ready():
            try:
                return configure(port) == (200, {"urls": []})
            except OSError:
                return False
        assert wait_for(no_local_ready, 15)
        before_images, before_shared = len(hook.image_headers), len(shared.records)
        before_owner, before_local = len(hook.records), len(local.records)
        assert beat(port, "playing", 30000) == 200
        assert beat(port, "playing", 40000, SHARED) == 200
        # Delivery of the following authorized event is the worker-ordering barrier.
        check("a shared-only cloud callback receives its account's next event",
              wait_for(lambda: len(shared.records) == before_shared + 1, 15))
        check("an event with no authorized recipient performs no artwork fetch",
              len(hook.image_headers) == before_images + 1, len(hook.image_headers) - before_images)
        check("removed owner and local callbacks receive neither new event",
              len(hook.records) == before_owner and len(local.records) == before_local)
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
