#!/usr/bin/env python3
"""plexsim's plex.tv integration, black-box, against a LOCAL stub of plex.tv.

Drives a real server process (`tests/server_command.py`: unset `PLEXSIM_SERVER` is the
native binary, `PLEXSIM_SERVER=python` the Python oracle) through the four things a deployment
does with plex.tv:

* announcing its connection URI on `--heartbeat-interval` (heartbeat.py),
* redeeming a claim token handed to `POST /myplex/claim` (claim.py),
* the claimed owner's profile/feature refresh and cloud watch-state import
  (accountstate.py), and
* a restart that keeps the claim (`claim_state.json` beside the catalog).

Nothing here reaches plex.tv. `PLEXSIM_PLEXTV_URL` sends every plex.tv,
servers.plex.tv and scrobbles.plex.tv request to the stub below as
`<stub>/<host><path>`. The claim token, the account token and the server token are
literal placeholders; a real claim or announce from a test identity is what reset the
account's tokens on 2026-09-11, and a second server announcing a real identity rewrites
that identity's registration.

The stub's request grammar is the one the modules send and `tests/fixtures/
outbound-announce.json` recorded from a real PMS; its answers carry exactly the
attributes the oracle's modules read, in the element shapes `tests/test_accountstate.py`
already feeds them. No recorded plex.tv answer BODY is checked in -- the harness
recording keeps request bodies and statuses -- so the answers are the smallest documents
the oracle accepts, not transcriptions of plex.tv.

    python3 tests/test_plextv.py                      # the native server
    PLEXSIM_SERVER=python python3 tests/test_plextv.py   # the Python oracle

`PLEXSIM_TEST_PORT` picks the first of the three ports it binds (server, restarted
server, stub); the default asks the OS for free ones.
"""
import base64
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import test_accountstate  # noqa: E402  (its synthetic encrypted-service helpers)

CATALOG = os.path.join(HERE, "catalog_ui.json")
MACHINE = "f1x7u4e0000000000000000000000000000000f1"
FRIENDLY = "plexsim-ui-fixture"
VERSION = "1.42.2.10156-plexsim"
CLAIM_TOKEN = "claim-STUB-CLAIM-TOKEN"
ACCOUNT_TOKEN = "ACCOUNT-TOKEN-STUB"
SERVER_TOKEN = "SERVER-TOKEN-STUB"
OWNER = "stubowner"
EMAIL = "owner@stub.invalid"
ACCOUNT_ID = 42
ANNOUNCE_URL = "https://plexsim.stub.invalid"
SECRET = bytes(range(32))
WATCHED_KEY, WATCHED_GUID = "1007", "plex://movie/520a1a3c2c634f2594808ee9"
UNPLAYED_GUID = "plex://movie/520a1a3c2c634f2594808ee9-fixture1008"
NAMED_FEATURES = ("adaptive_bitrate,camera_upload,collections,content_filter,"
                  "download_certificates,dvr,federated-auth,hardware_transcoding,home,"
                  "hwtranscode,item_clusters,kevin-bacon,livetv,loudness,lyrics,"
                  "music-analysis,music_videos,pass,photosV6-edit,photosV6-tv-albums,"
                  "premium_music_metadata,radio,server-manager,"
                  "session_bandwidth_restrictions,session_kick,shared-radio,sync,trailers,"
                  "tuner-sharing,type-first,unsupportedtuners,webhooks")
FEATURE_UUIDS = ["0a1b2c3d-stub-feature-uuid-a", "9f8e7d6c-stub-feature-uuid-b"]

# The identity every request to plex.tv carries (claim.Claim._headers), in its order.
IDENTITY = [
    ("x-plex-client-identifier", MACHINE),
    ("x-plex-product", "Plex Media Server"),
    ("x-plex-version", VERSION),
    ("x-plex-platform", "Linux"),
    ("x-plex-platform-version", "6.1"),
    ("x-plex-device", "PC"),
    ("x-plex-device-name", FRIENDLY),
    ("x-plex-provides", "server"),
]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def b64url(document):
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


BEARER = ".".join([
    b64url({"alg": "RS256"}),
    b64url({"aud": "stub-audience", "exp": 4102444800, "iss": "stub-issuer",
            "userId": ACCOUNT_ID}),
    base64.urlsafe_b64encode(b"stub-signature").decode("ascii").rstrip("="),
])
SERVICE_TOKEN = test_accountstate.synthetic_service_token(BEARER, SECRET)


# -- the stub ----------------------------------------------------------------

class Stub(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), StubHandler)
        self.lock = threading.Lock()
        self.records = []
        self.pages = 0

    def taken(self, host=None, path=None, method=None):
        with self.lock:
            return [r for r in self.records
                    if (host is None or r["host"] == host)
                    and (path is None or re.fullmatch(path, r["path"]))
                    and (method is None or r["method"] == method)]

    def clear(self):
        with self.lock:
            self.records = []


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self):
        target = urllib.parse.urlsplit(self.path)
        _, host, path = target.path.split("/", 2) if target.path.count("/") >= 2 \
            else ("", target.path.strip("/"), "")
        path = "/" + path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {
            "at": time.time(), "method": self.command, "host": host, "path": path,
            "query": urllib.parse.parse_qsl(target.query, keep_blank_values=True),
            "headers": [(k.lower(), v) for k, v in self.headers.items()],
            "body": body,
        }
        with self.server.lock:
            self.server.records.append(record)
        status, kind, payload = self.answer(record)
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_PUT = do_POST = do_DELETE = _handle

    def answer(self, record):
        method, host, path = record["method"], record["host"], record["path"]
        query = dict(record["query"])
        xml = "application/xml"
        if (method, host, path) == ("POST", "plex.tv", "/api/claim/exchange"):
            if query.get("token") != CLAIM_TOKEN:
                return 401, xml, "<errors><error code=\"1001\" message=\"stub\"/></errors>"
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="%d" '
                              'username="%s" email="%s" authToken="%s"/>\n'
                              % (ACCOUNT_ID, OWNER, EMAIL, SERVER_TOKEN))
        if method == "PUT" and host == "plex.tv" and path == "/api/servers/%s/connectivity" % MACHINE:
            return 200, xml, ""
        if (method, host, path) == ("GET", "servers.plex.tv", "/api/v2/user"):
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="%d" '
                              'username="%s" email="%s"/>\n' % (ACCOUNT_ID, OWNER, EMAIL))
        if (method, host, path) == ("GET", "servers.plex.tv", "/api/v2/features"):
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<features>'
                              '<feature id="stub-b" uuid="%s"/><feature id="stub-a" uuid="%s"/>'
                              '<feature id="named-only"/></features>\n'
                              % (FEATURE_UUIDS[1], FEATURE_UUIDS[0]))
        if method == "PUT" and host == "servers.plex.tv" and path == "/devices/%s" % MACHINE:
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<device '
                              'clientIdentifier="%s" name="%s"/>\n' % (MACHINE, FRIENDLY))
        if (method, host, path) == ("GET", "plex.tv", "/api/v2/resources"):
            return 200, "application/json", json.dumps(
                [{"name": FRIENDLY, "provides": "server", "clientIdentifier": MACHINE}])
        if (method, host, path) == ("GET", "servers.plex.tv",
                                    "/api/v2/server/users/%d/view_state_sync" % ACCOUNT_ID):
            return 200, "application/json", json.dumps(
                {"consent": True, "updatedAt": "2026-09-17T00:00:00Z",
                 "deletionRequestedAt": None})
        if (method, host, path) == ("GET", "servers.plex.tv", "/api/v2/server/users/services"):
            return 200, xml, ('<users><user id="%d"><services>'
                              '<service identifier="metadata" status="online" token="ignored"/>'
                              '<service identifier="scrobbling" status="online" secret="" '
                              'token="%s"/></services></user></users>'
                              % (ACCOUNT_ID, SERVICE_TOKEN))
        if (method, host, path) == ("GET", "scrobbles.plex.tv", "/state/%d" % ACCOUNT_ID):
            with self.server.lock:
                self.server.pages += 1
                first = self.server.pages == 1
            items = [
                {"guid": WATCHED_GUID, "viewState": "complete", "viewedAt": 1789591000123,
                 "rating": 8, "ratedAt": 1789591000456},
                {"guid": UNPLAYED_GUID, "viewState": "unplayed", "viewedAt": 1789591000789},
                {"guid": "plex://movie/not-held-here", "viewState": "complete",
                 "viewedAt": 1789591000999},
            ] if first else []
            return 200, "application/json", json.dumps(
                {"items": items, "pageInfo": {"hasNextPage": False,
                                              "endCursor": "opaque-cursor-%d" % (1 if first else 2)}})
        return 404, "text/plain", "stub: no such route"


# -- the server under test -----------------------------------------------------

def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


LIVE = []


class Instance:
    def __init__(self, directory, port, stub_port, extra=()):
        LIVE.append(self)
        self.port = port
        catalog = os.path.join(directory, "catalog.json")
        command = server_command("--port", port, "--catalog", catalog, "--gdm", "off",
                                 "--announce-url", ANNOUNCE_URL, "--heartbeat-interval", 2,
                                 "--account-state-interval", 3600, *extra, root=ROOT)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PLEX_TOKEN=ACCOUNT_TOKEN,
                   PLEXSIM_PLEXTV_URL="http://127.0.0.1:%d" % stub_port,
                   PLEXSIM_SCROBBLING_DECRYPT_SECRET_B64=base64.b64encode(SECRET).decode())
        for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
            env.pop(name, None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        raise SystemExit("plexsim did not start:\n" + self.output())

    def request(self, method, path, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, headers={
                "Accept": "application/json", "X-Plex-Client-Identifier": "plextv-suite",
                "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"})
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        if raw:
            return response.status, body
        try:
            return response.status, json.loads(body) if body else None
        except ValueError:
            return response.status, body.decode(errors="replace")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")


def container(body):
    return (body or {}).get("MediaContainer") or {} if isinstance(body, dict) else {}


def truthy(value):
    return value in (True, 1, "1", "true")


# urllib and the native client each add transport headers of their own; what the
# modules SET is what is compared, in the order they set it.
TRANSPORT = {"host", "connection", "content-length"}


def sent(record):
    out = []
    for name, value in record["headers"]:
        if name in TRANSPORT:
            continue
        if name == "user-agent" and value.startswith("Python-urllib/"):
            continue
        if name == "accept-encoding" and value == "identity":
            continue
        if name == "content-type" and value == "application/x-www-form-urlencoded":
            continue
        out.append((name, value))
    return out


def wait_for(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    return predicate()


def log_archive(instance):
    status, body = instance.request("GET", "/diagnostics/logs", raw=True)
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        return status, {name: archive.read(name).decode(errors="replace")
                        for name in archive.namelist()}
    except zipfile.BadZipFile:
        return status, {}


STDERR_LINE = r"^\d\d:\d\d:\d\d %-7s %s$"
ARCHIVE_LINE = r"^[A-Z][a-z]{2} \d\d, \d{4} \d\d:\d\d:\d\d\.\d{3} \[[^\]]+\] %s - %s$"


def logged(text, level, message, pattern=STDERR_LINE):
    return re.search(pattern % (re.escape(level), message), text, re.M) is not None


def surfaces(instance):
    return {
        "identity": instance.request("GET", "/identity"),
        "root": instance.request("GET", "/"),
        "account": instance.request("GET", "/myplex/account"),
        "providers": instance.request("GET", "/media/providers"),
        "prefs": instance.request("GET", "/:/prefs"),
        "watched": instance.request("GET", "/library/metadata/%s" % WATCHED_KEY),
    }


def read_json(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def pref(body, name):
    for row in container(body).get("Setting") or []:
        if row.get("id") == name:
            return row.get("value")
    return None


def main():
    base = os.environ.get("PLEXSIM_TEST_PORT")
    ports = [int(base) + offset for offset in range(3)] if base else \
        [free_port(), free_port(), free_port()]
    stub = Stub(ports[2])
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    directory = tempfile.mkdtemp(prefix="plexsim-plextv-")
    shutil.copy(CATALOG, os.path.join(directory, "catalog.json"))
    claim_file = os.path.join(directory, "claim_state.json")
    features = ",".join(sorted(set(NAMED_FEATURES.split(",")) | set(FEATURE_UUIDS)))
    try:
        # -- unclaimed, announcing ----------------------------------------------
        first = Instance(directory, ports[0], ports[2])
        announces = wait_for(lambda: len(stub.taken("servers.plex.tv", "/devices/.*", "PUT")) >= 2
                             and stub.taken("servers.plex.tv", "/devices/.*", "PUT"), 12) or []
        check("the announce is renewed on --heartbeat-interval", len(announces) >= 2,
              [r["path"] for r in stub.records])
        if announces:
            put = announces[0]
            check("announce is PUT servers.plex.tv/devices/{machineIdentifier}",
                  put["path"] == "/devices/%s" % MACHINE and put["body"] == b"", put["path"])
            check("announce query is the recorded five, in order, with PLEX_TOKEN",
                  put["query"] == [("Connection[][uri]", ANNOUNCE_URL), ("httpsEnabled", "1"),
                                   ("httpsRequired", "0"), ("dnsRebindingProtection", "0"),
                                   ("X-Plex-Token", ACCOUNT_TOKEN)], put["query"])
            check("announce carries the recorded thirteen headers, in order",
                  sent(put) == [
                      ("user-agent", "PlexMediaServer/%s" % VERSION), ("accept", "*/*"),
                      ("accept-encoding", "gzip"),
                      ("x-plex-client-identifier", MACHINE), ("x-plex-version", VERSION),
                      ("x-plex-provides", "server"), ("x-plex-platform", "Linux"),
                      ("x-plex-product", "Plex Media Server"),
                      ("x-plex-platform-version", "6.1"), ("x-plex-device-name", FRIENDLY),
                      ("x-plex-device", "PC"), ("x-plex-model", "x86_64"),
                      ("x-plex-device-vendor", "plexsim")], sent(put))
            gap = announces[1]["at"] - announces[0]["at"]
            check("renewal follows the interval, not a busy loop", 1.0 <= gap <= 6.0, gap)
        visible = stub.taken("plex.tv", "/api/v2/resources", "GET")
        check("each announce is verified against /api/v2/resources",
              visible and visible[0]["query"] == [("includeHttps", "1"), ("includeRelay", "1"),
                                                 ("includeIPv6", "1")]
              and sent(visible[0]) == [("x-plex-token", ACCOUNT_TOKEN),
                                       ("x-plex-client-identifier", "plexsim-heartbeat"),
                                       ("accept", "application/json")],
              visible and (visible[0]["query"], sent(visible[0])))
        check("nothing but the announce reaches plex.tv while unclaimed",
              {(r["host"], r["path"]) for r in stub.records}
              <= {("servers.plex.tv", "/devices/%s" % MACHINE), ("plex.tv", "/api/v2/resources")},
              {(r["host"], r["path"]) for r in stub.records})

        before = surfaces(first)
        check("unclaimed /identity reports claimed false",
              not truthy(container(before["identity"][1]).get("claimed")), before["identity"])
        check("unclaimed /myplex/account signs in as nobody",
              before["account"][1]["MyPlex"].get("signInState") == "none"
              and before["account"][1]["MyPlex"].get("username") is None, before["account"])
        check("unclaimed ownerFeatures are the named defaults",
              container(before["root"][1]).get("ownerFeatures") == NAMED_FEATURES,
              container(before["root"][1]).get("ownerFeatures"))
        check("no claim file before a claim", not os.path.exists(claim_file))

        status, body = first.request("POST", "/myplex/claim")
        check("a claim with no token is a 400 that reaches nothing", status == 400
              and not stub.taken("plex.tv", "/api/claim/exchange"), status)
        status, body = first.request("POST", "/myplex/claim?token=claim-WRONG")
        check("a refused exchange is a 500 and leaves the server unclaimed",
              status == 500 and not truthy(container(first.request("GET", "/identity")[1])
                                           .get("claimed")), (status, body))

        # -- the claim --------------------------------------------------------------
        claimed_at = time.time()
        status, body = first.request("POST", "/myplex/claim?token=%s" % CLAIM_TOKEN)
        myplex = (body or {}).get("MyPlex") if isinstance(body, dict) else None
        check("POST /myplex/claim answers the claimed /myplex/account",
              status == 200 and myplex and myplex.get("signInState") == "ok"
              and myplex.get("username") == EMAIL and myplex.get("subscriptionActive") in (True, 1)
              and myplex.get("subscriptionState") == "Active", (status, body))
        exchanges = stub.taken("plex.tv", "/api/claim/exchange", "POST")
        exchange = exchanges[-1] if exchanges else None
        check("the exchange POSTs the claim token with the server identity",
              exchange and exchange["query"] == [("token", CLAIM_TOKEN)]
              and exchange["body"] == b""
              and sent(exchange) == IDENTITY + [("accept", "application/xml")],
              exchange and (exchange["query"], sent(exchange)))
        connectivity = stub.taken("plex.tv", "/api/servers/.*/connectivity", "PUT")
        check("the claim is followed by the connectivity PUT with the SERVER token",
              connectivity and connectivity[0]["path"] == "/api/servers/%s/connectivity" % MACHINE
              and [k for k, _ in connectivity[0]["query"]] == ["X-Plex-Token", "asyncIdentifier"]
              and dict(connectivity[0]["query"])["X-Plex-Token"] == SERVER_TOKEN
              and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                               dict(connectivity[0]["query"])["asyncIdentifier"])
              and sent(connectivity[0]) == IDENTITY + [("accept", "application/xml")],
              connectivity and (connectivity[0]["query"], sent(connectivity[0])))

        claim_now = surfaces(first)
        check("claimed /identity reports claimed true at once",
              truthy(container(claim_now["identity"][1]).get("claimed")), claim_now["identity"])
        root = container(claim_now["root"][1])
        check("claimed / names the owner's email and signs in",
              root.get("myPlexUsername") == EMAIL and root.get("myPlexSigninState") == "ok"
              and truthy(root.get("myPlexSubscription")),
              {k: root.get(k) for k in ("myPlexUsername", "myPlexSigninState", "myPlexSubscription")})
        providers = container(claim_now["providers"][1])
        check("claimed /media/providers names the owner's email and signs in",
              providers.get("myPlexUsername") == EMAIL and providers.get("myPlexSigninState") == "ok",
              {k: providers.get(k) for k in ("myPlexUsername", "myPlexSigninState")})
        check("claimed PlexOnlineMail is the owner's email",
              pref(claim_now["prefs"][1], "PlexOnlineMail") == EMAIL,
              pref(claim_now["prefs"][1], "PlexOnlineMail"))

        # The owner read, then the feature table eleven seconds later (cycle 47).
        refreshed = wait_for(lambda: stub.taken("servers.plex.tv", "/api/v2/features"), 20)
        users = stub.taken("servers.plex.tv", "/api/v2/user", "GET")
        check("the claim starts the owner refresh: /api/v2/user with the server token",
              users and users[0]["query"] == [("includeSubscriptions", "1"), ("includeProviders", "1")]
              and sent(users[0]) == IDENTITY + [("accept", "application/xml"),
                                                ("x-plex-token", SERVER_TOKEN)],
              users and (users[0]["query"], sent(users[0])))
        check("the feature table follows the owner read by the measured eleven seconds",
              refreshed and users and 10.0 <= refreshed[0]["at"] - users[0]["at"] <= 14.0
              and refreshed[0]["query"] == [("X-Plex-Token", SERVER_TOKEN)]
              and sent(refreshed[0]) == IDENTITY + [("accept", "application/xml")],
              refreshed and users and (refreshed[0]["at"] - users[0]["at"], sent(refreshed[0])))
        time.sleep(0.5)
        after = surfaces(first)
        check("ownerFeatures gain the account's feature UUIDs",
              container(after["root"][1]).get("ownerFeatures") == features,
              container(after["root"][1]).get("ownerFeatures"))
        check("/myplex/account subscriptionFeatures is the same string",
              after["account"][1]["MyPlex"].get("subscriptionFeatures") == features,
              after["account"][1]["MyPlex"].get("subscriptionFeatures"))
        check("/media/providers ownerFeatures is the same string",
              container(after["providers"][1]).get("ownerFeatures") == features,
              container(after["providers"][1]).get("ownerFeatures"))

        consent = stub.taken("servers.plex.tv", "/api/v2/server/users/.*/view_state_sync")
        services = stub.taken("servers.plex.tv", "/api/v2/server/users/services")
        pages = stub.taken("scrobbles.plex.tv", "/state/.*")
        check("the claim starts the cloud import: consent with the server token",
              consent and consent[0]["path"] == "/api/v2/server/users/%d/view_state_sync" % ACCOUNT_ID
              and consent[0]["query"] == []
              and sent(consent[0]) == IDENTITY[:1] + [
                  ("x-plex-product", "Plex Media Server"), ("x-plex-version", VERSION),
                  ("x-plex-platform", "Linux"), ("x-plex-platform-version", "6.1"),
                  ("x-plex-device", "PC"), ("x-plex-device-name", FRIENDLY),
                  ("x-plex-provides", "server"), ("accept", "*/*"),
                  ("user-agent", "PlexMediaServer/%s" % VERSION),
                  ("content-type", "application/json"), ("x-plex-token", SERVER_TOKEN)],
              consent and sent(consent[0]))
        check("the scrobbling credential is read with auth_token and the server token",
              services and services[0]["query"] == [("auth_token", SERVER_TOKEN)]
              and sent(services[0]) == [
                  ("user-agent", "PlexMediaServer/%s" % VERSION), ("accept", "application/xml"),
                  ("x-plex-client-identifier", MACHINE), ("x-plex-version", VERSION),
                  ("x-plex-provides", "server"), ("x-plex-platform", "Linux"),
                  ("x-plex-product", "Plex Media Server"), ("x-plex-platform-version", "6.1"),
                  ("x-plex-device-name", FRIENDLY), ("x-plex-device", "PC"),
                  ("x-plex-token", SERVER_TOKEN)],
              services and (services[0]["query"], sent(services[0])))
        check("the state page is read with the decrypted bearer and an empty cursor",
              pages and pages[0]["path"] == "/state/%d" % ACCOUNT_ID
              and pages[0]["query"] == [("after", ""), ("count", "100")]
              and sent(pages[0]) == [
                  ("user-agent", "PlexMediaServer/%s" % VERSION), ("accept", "*/*"),
                  ("authorization", "Bearer " + BEARER), ("content-type", "application/json"),
                  ("x-plex-version", VERSION), ("x-plex-product", "Plex Media Server")],
              pages and (pages[0]["query"], sent(pages[0])))
        watched = (container(after["watched"][1]).get("Metadata") or [{}])[0]
        check("a complete cloud row is watched and rated here, at the cloud's times",
              watched.get("viewCount") == 1 and watched.get("lastViewedAt") == 1789591000
              and watched.get("userRating") == 8.0 and watched.get("lastRatedAt") == 1789591000,
              {k: watched.get(k) for k in ("viewCount", "lastViewedAt", "userRating", "lastRatedAt")})
        check("an import is not a play: no history row",
              not (container(first.request("GET", "/status/sessions/history/all?type=0,1")[1])
                   .get("Metadata")), "history")

        saved = read_json(claim_file)
        check("the claim file keeps machine, credentials, owner, features, id and cursor",
              saved == {"machine": MACHINE, "token": SERVER_TOKEN, "owner": OWNER, "email": EMAIL,
                        "feature_uuids": FEATURE_UUIDS, "account_id": ACCOUNT_ID,
                        "view_state_cursor": "opaque-cursor-1"}, saved)
        check("the claim file is private (0600)",
              os.path.exists(claim_file) and stat.S_IMODE(os.stat(claim_file).st_mode) == 0o600,
              os.path.exists(claim_file) and oct(os.stat(claim_file).st_mode))
        stub.clear()
        first.stop()
        first_log = first.output()
        check("startup says the claim state, the token gate and where it listens",
              logged(first_log, "INFO", r"claim state: unclaimed")
              and logged(first_log, "INFO", r"token gate: OFF -- every peer that can reach "
                                            r"this port can read the library")
              and logged(first_log, "INFO", r"plexsim on http://\S+:%d  machine=%s  items=\d+  "
                                            r"parts=proxy" % (ports[0], MACHINE)),
              first_log[-3000:])
        check("the heartbeat and the proxy-behind-a-public-URL warning are logged",
              logged(first_log, "INFO", r"heartbeat every 2s for machine %s -> %s"
                     % (MACHINE, re.escape(ANNOUNCE_URL)))
              and logged(first_log, "WARNING", r"part-mode=proxy behind a public URL: .*"),
              first_log[-3000:])
        check("the claim is logged, and its connectivity PUT",
              logged(first_log, "INFO", r"CLAIMED by %s; server token stored" % OWNER)
              and logged(first_log, "INFO", r"post-claim connectivity PUT -> 200 b''")
              and logged(first_log, "ERROR", r"claim exchange failed: .*"),
              first_log[-3000:])

        # -- restart: the claim survives -------------------------------------------
        second = Instance(directory, ports[1], ports[2], extra=("--verbose",))
        restarted = surfaces(second)
        check("after a restart /identity is still claimed",
              truthy(container(restarted["identity"][1]).get("claimed")), restarted["identity"])
        check("after a restart / still names the owner and the account's features",
              container(restarted["root"][1]).get("myPlexUsername") == EMAIL
              and container(restarted["root"][1]).get("ownerFeatures") == features,
              {k: container(restarted["root"][1]).get(k) for k in ("myPlexUsername", "ownerFeatures")})
        check("after a restart /myplex/account still signs in as the owner",
              restarted["account"][1]["MyPlex"].get("signInState") == "ok"
              and restarted["account"][1]["MyPlex"].get("username") == EMAIL, restarted["account"])
        check("no second exchange: the stored server token is reused",
              not stub.taken("plex.tv", "/api/claim/exchange"), [r["path"] for r in stub.records])
        refreshed = wait_for(lambda: stub.taken("servers.plex.tv", "/api/v2/features"), 20)
        users = stub.taken("servers.plex.tv", "/api/v2/user")
        check("a claimed startup refreshes the owner, then the features",
              len(users) == 1 and refreshed and users[0]["at"] < refreshed[0]["at"],
              [(r["host"], r["path"]) for r in stub.records])
        pages = wait_for(lambda: stub.taken("scrobbles.plex.tv", "/state/.*"), 10)
        check("a claimed startup resumes the cloud import from the stored cursor",
              pages and pages[0]["query"] == [("after", "opaque-cursor-1"), ("count", "100")],
              pages and pages[0]["query"])
        time.sleep(0.5)
        status, files = log_archive(second)
        plexsim_log = files.get("plexsim.log", "")
        check("Download Logs hands back the startup lines, claimed",
              status == 200 and logged(plexsim_log, "INFO", r"claim state: CLAIMED by %s" % OWNER,
                                       ARCHIVE_LINE)
              and logged(plexsim_log, "INFO", r"token gate: OFF -- .*", ARCHIVE_LINE),
              (status, sorted(files), plexsim_log[-1500:]))
        check("Download Logs carries the request log",
              re.search(r"^GET /identity$", files.get("requests.log", ""), re.M) is not None,
              files.get("requests.log", "")[-500:])
        check("the owner refresh and the cursor persist across the restart",
              read_json(claim_file).get("view_state_cursor") == "opaque-cursor-2", "cursor")
        second.stop()
        second_log = second.output()
        check("a claimed startup logs the loaded claim",
              logged(second_log, "INFO", r"loaded claim for %s \(owner %s\)" % (MACHINE, OWNER))
              and logged(second_log, "INFO", r"claim state: CLAIMED by %s" % OWNER),
              second_log[-3000:])
        check("--verbose logs each request line at DEBUG",
              logged(second_log, "DEBUG", r'"GET /identity HTTP/1\.1" 200 -'),
              second_log[-3000:])
        check("without --verbose there are no request lines",
              not logged(first_log, "DEBUG", r'".*'), "first run had DEBUG lines")
    finally:
        for instance in LIVE:
            if instance.proc.poll() is None:
                instance.proc.kill()
                instance.proc.wait()
        stub.shutdown()
        shutil.rmtree(directory, ignore_errors=True)

    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
