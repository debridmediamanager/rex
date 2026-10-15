#!/usr/bin/env python3
"""rex's plex.tv integration, black-box, against a LOCAL stub of plex.tv.

Drives a real server process (`tests/server_command.py`) through the four things a
deployment does with plex.tv (`rust/src/runtime/plextv.rs`):

* announcing its connection URI on `--heartbeat-interval`,
* redeeming a claim token handed to `POST /myplex/claim`,
* the claimed owner's profile/feature refresh and cloud watch-state import, and
* a restart that keeps the claim (`claim_state.json` beside the catalog).

Nothing here reaches plex.tv. `REX_PLEXTV_URL` sends every plex.tv,
servers.plex.tv and scrobbles.plex.tv request to the stub below as
`<stub>/<host><path>`. The claim token, the account token and the server token are
literal placeholders; a real claim or announce from a test identity is what reset the
account's tokens on 2026-09-11, and a second server announcing a real identity rewrites
that identity's registration.

The stub's request grammar is the one rex sends and `tests/fixtures/
outbound-announce.json` recorded from a real PMS; its answers carry exactly the
attributes rex reads. The claim and registration half -- the exchange, `POST /servers.xml`,
the pubsub stream and the connectivity PUT it prompts -- is checked against
`tests/fixtures/plextv_registration_s1422.json`, a real 1.42.2 PMS claimed on the 1.42.2 reference
with its plex.tv traffic recorded: the stub replays that recording's pubsub events and its 422, and
gzips an answer whenever the request asks for gzip, as plex.tv did there.

    python3 tests/test_plextv.py

`REX_TEST_PORT` picks the first of the three ports it binds (server, restarted
server, stub); otherwise the bounded test-port allocator is used.
"""
import base64
import gzip
import hashlib
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
from test_ports import free_port

# The stub stamps each request it takes. Python 3.12's time.time() moves in 15.6 ms steps on
# Windows, so two requests rex sends a few ms apart stamped equal and every "after" check
# failed there; this is the wall clock with perf_counter's resolution.
_WALL_AT_START = time.time() - time.perf_counter()


def precise_time():
    return _WALL_AT_START + time.perf_counter()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

CATALOG = os.path.join(HERE, "catalog_ui.json")
with open(os.path.join(HERE, "fixtures", "plextv_registration_s1422.json")) as _handle:
    RECORDED = json.load(_handle)
# plex.tv's list of the tokens a claimed server lets in, as it answered for rex-e2e, and
# the 1.42.2 reference itself re-reading it for an unknown token (the 6 s window is measured there).
with open(os.path.join(HERE, "fixtures", "plextv_access_tokens_s1422.json")) as _handle:
    ACCESS = json.load(_handle)
# the 1.42.2 reference's own PMS announcing its seventeen connections: one Connection[][uri] pair
# each, in a row.
with open(os.path.join(HERE, "fixtures", "pms_announce_connections_s1422.json")) as _handle:
    CONNECTIONS = json.load(_handle)
# plexlab's own PMS (1.43.4) renewing its registration, three times, tokens replaced.
with open(os.path.join(HERE, "fixtures", "outbound-announce.json")) as _handle:
    ANNOUNCES = json.load(_handle)["announces"]
MACHINE = "f1x7u4e0000000000000000000000000000000f1"
FRIENDLY = "rex-ui-fixture"
VERSION = "1.42.2.10156-rex"
CLAIM_TOKEN = "claim-STUB-CLAIM-TOKEN"
REUSED_CLAIM_TOKEN = "claim-STUB-REUSED"    # plex.tv answers it with the ACCOUNT token
ACCOUNT_TOKEN = "ACCOUNT-TOKEN-STUB"
SERVER_TOKEN = "SERVER-TOKEN-STUB"
DEVICE_TOKEN = "DEVICE-TOKEN-STUB-3"        # another of the owner's devices, as plex.tv lists it
DISABLED_TOKEN = "DISABLED-TOKEN-STUB"      # listed with tokenDisabled="1"
UNLISTED_TOKEN = "UNLISTED-TOKEN-STUB"
LATE_TOKEN = "LATE-TOKEN-STUB"              # listed only once the test adds it
ACCESS_WINDOW = 6
OWNER = "stubowner"
EMAIL = "owner@stub.invalid"
ACCOUNT_ID = 42
ANNOUNCE_URL = "https://rex.stub.invalid"
# A second connection (a public one, as a Funnel hostname is), announced beside the first.
ANNOUNCE_URLS = ANNOUNCE_URL + ",https://rex-public.stub.invalid:8443"
SECRET = bytes(range(32))
WATCHED_KEY, WATCHED_GUID = "1007", "plex://movie/520a1a3c2c634f2594808ee9"
UNPLAYED_KEY, UNPLAYED_GUID = "1008", "plex://movie/520a1a3c2c634f2594808ee9-fixture1008"
CLEARED_KEY, CLEARED_GUID = "1009", "plex://movie/520a1a3c2c634f2594808ee9-fixture1009"
NAMED_FEATURES = ("collections,federated-auth,home,item_clusters,kevin-bacon,pass,server-manager,"
                  "session_kick,type-first")
FEATURE_UUIDS = ["0a1b2c3d-stub-feature-uuid-a", "9f8e7d6c-stub-feature-uuid-b"]

# The identity every request to plex.tv carries, in its order.
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

# What a real PMS sends on every claim and registration call, in its order, with
# rex's own identity values.
PMS_HEADERS = [
    ("user-agent", "PlexMediaServer/%s" % VERSION), ("accept", "*/*"),
    ("accept-encoding", "gzip"), ("x-plex-client-identifier", MACHINE),
    ("x-plex-version", VERSION), ("x-plex-provides", "server"), ("x-plex-platform", "Linux"),
    ("x-plex-product", "Plex Media Server"), ("x-plex-platform-version", "6.1"),
    ("x-plex-device-name", FRIENDLY), ("x-plex-device", "PC"), ("x-plex-model", "x86_64"),
    ("x-plex-device-vendor", "rex"),
]


def recorded(section, method, path):
    """The recording's first exchange for a call, its machineIdentifier generalised."""
    for row in RECORDED[section]:
        if row["method"] == method and re.fullmatch(path, row["path"]):
            return row
    raise KeyError((section, method, path))


def names(headers, drop=("host", "content-length", "content-type", "connection")):
    """Header names in order, without the transport ones (as `sent` drops them)."""
    return [k.lower() for k, v in headers if k.lower() not in drop
            and (k.lower(), v) != ("accept-encoding", "identity")
            and not (k.lower() == "user-agent" and v.startswith("Python-urllib/"))]


# plex.tv's reachability verdict, as recorded; the stub sends it under the asyncIdentifier
# of the connectivity PUT that asked for it, which is the order the recording shows.
VERDICT = next(e["data"] for e in RECORDED["pubsub"]["events"] if e["data"].startswith("data:"))
UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"

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


# -- the scrobbling credential, encrypted as plex.tv hands it out ---------------
# AES-256-CBC under PMS's MD5 key expansion (OpenSSL's EVP_BytesToKey, NID 4): the
# encrypting half of what rex decrypts, in the standard library only. It is checked
# against the public NIST AES-256 block vector, so the credential rex decrypts below
# cannot merely agree with a wrong AES of rex's own.

SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")


def _gf_mul(left, right):
    value = 0
    for _ in range(8):
        if right & 1:
            value ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return value


def aes256_round_keys(key):
    words = [list(key[index:index + 4]) for index in range(0, 32, 4)]
    rcon = 1
    while len(words) < 60:
        index = len(words)
        word = words[-1][:]
        if index % 8 == 0:
            word = [SBOX[value] for value in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif index % 8 == 4:
            word = [SBOX[value] for value in word]
        words.append([words[index - 8][offset] ^ word[offset] for offset in range(4)])
    return [sum(words[index:index + 4], []) for index in range(0, 60, 4)]


def aes_encrypt_block(block, round_keys):
    def shift_rows(state):
        result = state[:]
        for row in range(1, 4):
            values = [state[row + 4 * column] for column in range(4)]
            values = values[row:] + values[:row]
            for column, value in enumerate(values):
                result[row + 4 * column] = value
        return result

    def mix_columns(state):
        matrix = ((2, 3, 1, 1), (1, 2, 3, 1), (1, 1, 2, 3), (3, 1, 1, 2))
        result = state[:]
        for column in range(4):
            source = state[4 * column:4 * column + 4]
            for row in range(4):
                result[4 * column + row] = 0
                for offset in range(4):
                    result[4 * column + row] ^= _gf_mul(matrix[row][offset], source[offset])
        return result

    state = [value ^ round_keys[0][index] for index, value in enumerate(block)]
    for round_number in range(1, 14):
        state = mix_columns(shift_rows([SBOX[value] for value in state]))
        state = [value ^ round_keys[round_number][index] for index, value in enumerate(state)]
    state = shift_rows([SBOX[value] for value in state])
    return bytes(value ^ round_keys[14][index] for index, value in enumerate(state))


def synthetic_service_token(bearer, secret):
    expanded, previous = b"", b""
    while len(expanded) < 48:
        previous = hashlib.md5(previous + secret).digest()
        expanded += previous
    round_keys, iv = aes256_round_keys(expanded[:32]), expanded[32:48]
    plaintext = bearer.encode("utf-8")
    plaintext += bytes([16 - len(plaintext) % 16]) * (16 - len(plaintext) % 16)
    ciphertext, previous = bytearray(), iv
    for index in range(0, len(plaintext), 16):
        previous = aes_encrypt_block(bytes(left ^ right for left, right in
                                           zip(plaintext[index:index + 16], previous)), round_keys)
        ciphertext.extend(previous)
    return base64.b64encode(bytes(ciphertext)).decode("ascii")


SERVICE_TOKEN = synthetic_service_token(BEARER, SECRET)


# -- the stub ----------------------------------------------------------------

class Stub(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), StubHandler)
        self.lock = threading.Lock()
        self.records = []
        self.pages = 0
        self.late_tokens = []
        self.refuse_access = False
        # The first claimed startup deliberately holds the pubsub handler after its 200
        # headers are visible.  This makes the response/recording race deterministic: rex's
        # connectivity PUT must be stamped after `opened`.
        self.pubsub_gap = False
        self.pubsub_connectivity_seen = threading.Event()
        # A later owner read names another account id beside a broken feature table.
        self.user_id = ACCOUNT_ID
        self.broken_features = False

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
            "at": precise_time(), "method": self.command, "host": host, "path": path,
            "query": urllib.parse.parse_qsl(target.query, keep_blank_values=True),
            "headers": [(k.lower(), v) for k, v in self.headers.items()],
            "body": body,
        }
        with self.server.lock:
            self.server.records.append(record)
        if (record["method"], record["host"], record["path"]) == \
                ("PUT", "plex.tv", "/api/servers/%s/connectivity" % MACHINE):
            self.server.pubsub_connectivity_seen.set()
        if (self.command, host) == ("GET", "pubsub.plex.tv"):
            return self.pubsub(path)
        status, kind, payload = self.answer(record)
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header("Content-Type", kind)
        # plex.tv gzips whenever a PMS's `Accept-Encoding: gzip` asks (the recording's
        # exchange and device PUT answers both carry `content-encoding: gzip`).
        if data and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def pubsub(self, path):
        """The recorded event stream: plex.tv's comment, then -- once the server has asked
        for a reachability check -- its notifyConnectivity verdict under that request's
        asyncIdentifier, then a keepalive, until the stub ends it (the server resubscribes)."""
        if path != "/sub/eventsource/%d/%s" % (ACCOUNT_ID, MACHINE):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.close_connection = True
        self.send_response(200)
        for name, value in RECORDED["pubsub"]["response"]["headers"]:
            if name.lower() in ("content-type", "cache-control", "expires"):
                self.send_header(name, value)
        self.send_header("Connection", "close")
        opened = precise_time()  # compared with the requests' own `at`
        self.end_headers()
        if self.server.pubsub_gap:
            self.server.pubsub_connectivity_seen.wait(5)
        self.wfile.write(RECORDED["pubsub"]["events"][0]["data"].encode("utf-8"))
        self.wfile.flush()
        asked = None
        while asked is None and precise_time() - opened < 5:
            time.sleep(0.1)
            asked = next((r for r in self.server.taken("plex.tv", "/api/servers/.*/connectivity", "PUT")
                          if r["at"] >= opened), None)
        if asked is not None:
            verdict = re.sub(r'asyncIdentifier="[^"]*"', 'asyncIdentifier="%s"'
                             % dict(asked["query"]).get("asyncIdentifier"), VERDICT)
            self.wfile.write(verdict.encode("utf-8") + b": -1\n")
            self.wfile.flush()
        time.sleep(3)

    do_GET = do_PUT = do_POST = do_DELETE = _handle

    def answer(self, record):
        method, host, path = record["method"], record["host"], record["path"]
        query = dict(record["query"])
        xml = "application/xml"
        if (method, host, path) == ("POST", "plex.tv", "/api/claim/exchange"):
            if query.get("token") == REUSED_CLAIM_TOKEN:
                return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="%d" '
                                  'username="%s" email="%s" authToken="%s"/>\n'
                                  % (ACCOUNT_ID, OWNER, EMAIL, ACCOUNT_TOKEN))
            if query.get("token") != CLAIM_TOKEN:
                return 401, xml, "<errors><error code=\"1001\" message=\"stub\"/></errors>"
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="%d" '
                              'username="%s" email="%s" authToken="%s"/>\n'
                              % (ACCOUNT_ID, OWNER, EMAIL, SERVER_TOKEN))
        if method == "PUT" and host == "plex.tv" and path == "/api/servers/%s/connectivity" % MACHINE:
            return 200, xml, ""
        if (method, host, path) == ("GET", "plex.tv", "/api/v2/server/access_tokens"):
            # The recorded answer with the stub's tokens in it, plus a disabled row and any
            # token the test has started listing; anything but the server token is a 401.
            if self.server.refuse_access:
                return 503, "text/plain", "stub: plex.tv is down"
            if query.get("auth_token") != SERVER_TOKEN:
                return ACCESS["bad_auth_answer"]["status"], xml, ACCESS["bad_auth_answer"]["body"]
            body = ACCESS["response"]["body"].replace("SERVER-TOKEN", SERVER_TOKEN) \
                .replace("ACCOUNT-TOKEN", ACCOUNT_TOKEN).replace("DEVICE-TOKEN-", "DEVICE-TOKEN-STUB-")
            row = ('<token type="device" device="stub" token="%s" tokenDisabled="%s" owned="1" '
                   'createdAt="2026-10-10 00:00:00 UTC"/>\n')
            with self.server.lock:
                extra = [row % (DISABLED_TOKEN, 1)] + [row % (t, 0) for t in self.server.late_tokens]
            return ACCESS["response"]["status"], xml, body.replace("</tokens>", "".join(extra) + "</tokens>")
        if (method, host, path) == ("POST", "plex.tv", "/servers.xml"):
            # 422 every time, to a real PMS too -- and the server record is written anyway.
            return recorded("claim", "POST", "/servers.xml")["status"], xml, ""
        if (method, host, path) == ("GET", "servers.plex.tv", "/api/v2/user"):
            return 200, xml, ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="%d" '
                              'username="%s" email="%s"/>\n' % (self.server.user_id, OWNER, EMAIL))
        if (method, host, path) == ("GET", "servers.plex.tv", "/api/v2/features"):
            if self.server.broken_features:
                return 200, xml, "<broken"
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
                {"guid": CLEARED_GUID, "viewState": "unplayed", "viewedAt": 1789591000999,
                 "rating": -1, "ratedAt": 1789591000999},
                {"guid": "plex://movie/not-held-here", "viewState": "complete",
                 "viewedAt": 1789591000999},
            ] if first else []
            return 200, "application/json", json.dumps(
                {"items": items, "pageInfo": {"hasNextPage": False,
                                              "endCursor": "opaque-cursor-%d" % (1 if first else 2)}})
        return 404, "text/plain", "stub: no such route"


# -- the server under test -----------------------------------------------------

LIVE = []


class Instance:
    def __init__(self, directory, port, stub_port, extra=(), announce=ANNOUNCE_URL):
        LIVE.append(self)
        self.port = port
        catalog = os.path.join(directory, "catalog.json")
        command = server_command("--port", port, "--catalog", catalog, "--gdm", "off",
                                 "--announce-url", announce, "--heartbeat-interval", 2,
                                 "--account-state-interval", 3600, *extra, root=ROOT)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PLEX_TOKEN=ACCOUNT_TOKEN,
                   REX_PLEXTV_URL="http://127.0.0.1:%d" % stub_port,
                   REX_SCROBBLING_DECRYPT_SECRET_B64=base64.b64encode(SECRET).decode())
        for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
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
        raise SystemExit("rex did not start:\n" + self.output())

    def request(self, method, path, raw=False, token=None):
        headers = {"Accept": "application/json", "X-Plex-Client-Identifier": "plextv-suite",
                   "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
        if token:
            headers["X-Plex-Token"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, headers=headers)
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


def first_row(response):
    """The first Metadata row of a (status, body) answer, or {}."""
    return (container(response[1]).get("Metadata") or [{}])[0]


# The native client adds transport headers of its own; what
# rex SETS is what is compared, in the order it sets it.
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


def log_archive(instance, token=None):
    status, body = instance.request("GET", "/diagnostics/logs", raw=True, token=token)
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


def surfaces(instance, token=None):
    """A claimed server gates every one of these but /identity, so a client of it sends a
    token plex.tv lists for it (the account's own, here)."""
    return {
        "identity": instance.request("GET", "/identity", token=token),
        "root": instance.request("GET", "/", token=token),
        "account": instance.request("GET", "/myplex/account", token=token),
        "providers": instance.request("GET", "/media/providers", token=token),
        "prefs": instance.request("GET", "/:/prefs", token=token),
        "watched": instance.request("GET", "/library/metadata/%s" % WATCHED_KEY, token=token),
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


def placeholder_identity():
    """The example catalog's all-zero machineIdentifier is every copy's: an announce or a claim
    on it would write the same plex.tv device row as every other copy's, so neither starts."""
    stub = Stub(free_port())
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    directory = tempfile.mkdtemp(prefix="rex-plextv-placeholder-")
    try:
        document = json.load(open(CATALOG))
        document["machineIdentifier"] = "0" * 40
        catalog = os.path.join(directory, "catalog.json")
        json.dump(document, open(catalog, "w"))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PLEX_TOKEN=ACCOUNT_TOKEN,
                   REX_PLEXTV_URL="http://127.0.0.1:%d" % stub.server_address[1])
        for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
            env.pop(name, None)
        port = free_port()
        announced = subprocess.Popen(server_command("--port", port, "--catalog", catalog, "--gdm", "off",
                                                    "--announce-url", ANNOUNCE_URL, "--heartbeat-interval", 2,
                                                    root=ROOT),
                                     cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            output, _ = announced.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            announced.kill()
            output, _ = announced.communicate()
        check("--announce-url on a placeholder machineIdentifier refuses to start, announcing nothing",
              announced.returncode not in (0, None, -9) and "placeholder" in output
              and not stub.taken("servers.plex.tv"),
              (announced.returncode, output[-400:], [r["path"] for r in stub.taken("servers.plex.tv")]))
        log = tempfile.TemporaryFile()
        proc = subprocess.Popen(server_command("--port", port, "--catalog", catalog, "--gdm", "off", root=ROOT),
                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(150):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("POST", "/myplex/claim?token=%s" % CLAIM_TOKEN)
            status = conn.getresponse().status
            conn.close()
            check("a claim on a placeholder machineIdentifier is refused before plex.tv is asked",
                  status == 400 and not stub.taken("plex.tv", "/api/claim/exchange"), status)
        finally:
            proc.kill()
            proc.wait()
    finally:
        stub.shutdown()
        shutil.rmtree(directory, ignore_errors=True)


def main():
    base = os.environ.get("REX_TEST_PORT")
    ports = [int(base) + offset for offset in range(3)] if base else \
        [free_port(), free_port(), free_port()]
    stub = Stub(ports[2])
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    directory = tempfile.mkdtemp(prefix="rex-plextv-")
    shutil.copy(CATALOG, os.path.join(directory, "catalog.json"))
    claim_file = os.path.join(directory, "claim_state.json")
    features = ",".join(sorted(set(NAMED_FEATURES.split(",")) | set(FEATURE_UUIDS)))
    try:
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        check("the suite's AES-256 encrypts the public NIST block vector",
              aes_encrypt_block(bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"),
                                aes256_round_keys(key)).hex() == "f3eed1bdb5d2a03c064b5a7e3db181f8")
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
                      ("x-plex-device-vendor", "rex")], sent(put))
            # The three PUTs a real 1.43.4 PMS made (plexlab's capture/outbound.jsonl).
            real = ANNOUNCES[0]
            check("the recorded announces agree with each other",
                  all(a["url"] == real["url"] and a["query"] == real["query"]
                      and a["headers"] == real["headers"] for a in ANNOUNCES), len(ANNOUNCES))
            check("announce goes to the recorded origin with the recorded query keys and headers",
                  "https://%s" % put["host"] == real["origin"]
                  and [k for k, _ in put["query"]] == [k for k, _ in real["query"]]
                  and [k for k, _ in sent(put)] == names(real["headers"])
                  and dict(sent(put))["accept"] == dict(real["headers"])["accept"]
                  and "natLoopbackSupported" not in dict(put["query"]),
                  (put["host"], put["query"], sent(put)))
            gap = announces[1]["at"] - announces[0]["at"]
            check("renewal follows the interval, not a busy loop", 1.0 <= gap <= 6.0, gap)
        visible = stub.taken("plex.tv", "/api/v2/resources", "GET")
        check("each announce is verified against /api/v2/resources",
              visible and visible[0]["query"] == [("includeHttps", "1"), ("includeRelay", "1"),
                                                 ("includeIPv6", "1")]
              and sent(visible[0]) == [("x-plex-token", ACCOUNT_TOKEN),
                                       ("x-plex-client-identifier", MACHINE),
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
        status, body = first.request("POST", "/myplex/claim?token=%s" % REUSED_CLAIM_TOKEN)
        check("an exchange that answers the account token is refused, and nothing is stored",
              status == 500 and not os.path.exists(claim_file)
              and not truthy(container(first.request("GET", "/identity")[1]).get("claimed")),
              (status, body))

        # Watch state the cloud import is to clear: a watched, part-played, rated row the
        # cloud calls unplayed with no rating, and a rated row it sends the -1 clear for.
        library = "&identifier=com.plexapp.plugins.library"
        for path in ("/:/scrobble?key=%s" % UNPLAYED_KEY,
                     "/:/progress?key=%s&time=61000&state=stopped" % UNPLAYED_KEY,
                     "/:/rate?key=%s&rating=7" % UNPLAYED_KEY, "/:/rate?key=%s&rating=6" % CLEARED_KEY):
            first.request("GET", path + library)
        local_state = {key: first_row(first.request("GET", "/library/metadata/%s" % key))
                       for key in (UNPLAYED_KEY, CLEARED_KEY)}
        history_before = [row.get("ratingKey") for row in container(first.request(
            "GET", "/status/sessions/history/all?type=0,1")[1]).get("Metadata") or []]
        check("before the claim the rows hold local view state and stars",
              all(local_state[UNPLAYED_KEY].get(k) for k in ("viewCount", "viewOffset", "lastViewedAt"))
              and local_state[UNPLAYED_KEY].get("userRating") == 7.0
              and local_state[CLEARED_KEY].get("userRating") == 6.0
              and local_state[CLEARED_KEY].get("lastRatedAt"),
              {key: {k: row.get(k) for k in ("viewCount", "viewOffset", "lastViewedAt", "userRating",
                                             "lastRatedAt")} for key, row in local_state.items()})

        # -- the claim --------------------------------------------------------------
        claimed_at = time.time()
        stub.pubsub_gap = True
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
              and sent(exchange) == PMS_HEADERS
              and names(exchange["headers"])
              == names(recorded("claim", "POST", "/api/claim/exchange")["req_headers"]),
              exchange and (exchange["query"], sent(exchange)))
        # The registration a real PMS ran next: POST /servers.xml, the device PUT, the
        # pubsub stream, and -- once that is open -- a reachability check.
        connectivity = wait_for(lambda: stub.taken("plex.tv", "/api/servers/.*/connectivity", "PUT"), 10)
        publishes = stub.taken("plex.tv", "/servers.xml", "POST")
        publish = publishes[0] if publishes else None
        real = recorded("claim", "POST", "/servers.xml")
        body = publish and publish["body"].decode()
        shape = lambda text: re.sub(r'="[^"]*"', '=""', text)
        check("the claim POSTs /servers.xml with the server token, shaped as the recording",
              publish and publish["query"] == [("auth_token", SERVER_TOKEN)]
              and [k for k, _ in real["query"]] == ["auth_token"]
              and sent(publish) == PMS_HEADERS + [("content-type", "application/xml")]
              and names(publish["headers"], ("host", "content-length", "connection"))
              == names(real["req_headers"], ("host", "content-length", "connection"))
              and shape(body) == shape(real["req_body"])
              and 'name="%s"' % FRIENDLY in body and 'machineIdentifier="%s"' % MACHINE in body
              and 'version="%s"' % VERSION in body
              and abs(int(re.search(r'date="(\d+)"', body).group(1)) / 1e6 - claimed_at) < 10,
              publish and (publish["query"], sent(publish), body))
        puts = [r for r in stub.taken("servers.plex.tv", "/devices/.*", "PUT")
                if publish and 0 <= r["at"] - publish["at"] < 1.0
                and dict(r["query"])["X-Plex-Token"] == SERVER_TOKEN]
        check("the claim announces at once after it, with the server's own token", puts,
              [(r["at"] - claimed_at, dict(r["query"])["X-Plex-Token"])
               for r in stub.taken("servers.plex.tv", "/devices/.*", "PUT")])
        subscriptions = stub.taken("pubsub.plex.tv", "/sub/eventsource/.*", "GET")
        real = RECORDED["pubsub"]["request"]
        check("then subscribes to pubsub as the recording did",
              subscriptions and publish and subscriptions[0]["at"] > publish["at"]
              and subscriptions[0]["path"] == "/sub/eventsource/%d/%s" % (ACCOUNT_ID, MACHINE)
              and re.fullmatch(r"/sub/eventsource/\d+/[0-9a-f]{40}", real["path"])
              and subscriptions[0]["query"] == [("X-Plex-Token", SERVER_TOKEN)]
              and [k for k, _ in real["query"]] == ["X-Plex-Token"]
              and names(subscriptions[0]["headers"]) == names(real["headers"])
              and sent(subscriptions[0])[0] == ("user-agent",
                                                "PlexMediaServer-EventSourceClient/%s" % VERSION),
              subscriptions and (subscriptions[0]["path"], subscriptions[0]["query"],
                                 sent(subscriptions[0])))
        real = recorded("claimed_start", "PUT", r"/api/servers/[0-9a-f]+/connectivity")
        check("once subscribed it asks plex.tv for a reachability check, as the recording did",
              connectivity and subscriptions and connectivity[0]["at"] > subscriptions[0]["at"]
              and connectivity[0]["at"] - subscriptions[0]["at"] < 2
              and connectivity[0]["path"] == "/api/servers/%s/connectivity" % MACHINE
              and [k for k, _ in connectivity[0]["query"]] == ["X-Plex-Token", "asyncIdentifier"]
              and dict(connectivity[0]["query"])["X-Plex-Token"] == SERVER_TOKEN
              and re.fullmatch(UUID4, dict(connectivity[0]["query"])["asyncIdentifier"])
              and [k for k, _ in real["query"]] == ["X-Plex-Token", "asyncIdentifier"]
              # the recorded PUT went out before plex.tv's verdict named its id
              and RECORDED["pubsub"]["connectivity_put_after_s"]
              < next(e["after_s"] for e in RECORDED["pubsub"]["events"] if e["data"].startswith("data:"))
              and sent(connectivity[0]) == PMS_HEADERS
              and names(connectivity[0]["headers"]) == names(real["req_headers"]),
              connectivity and (connectivity[0]["query"], sent(connectivity[0])))
        asked = connectivity and dict(connectivity[0]["query"])["asyncIdentifier"]

        claim_now = surfaces(first, ACCOUNT_TOKEN)
        exchanged = len(stub.taken("plex.tv", "/api/claim/exchange", "POST"))
        status, body = first.request("POST", "/myplex/claim?token=%s" % CLAIM_TOKEN, token=ACCOUNT_TOKEN)
        check("a claimed server refuses another claim without asking plex.tv",
              status == 409 and len(stub.taken("plex.tv", "/api/claim/exchange", "POST")) == exchanged,
              (status, body))
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
        check("until the account refresh lands, ownerFeatures are the named defaults",
              not stub.taken("servers.plex.tv", "/api/v2/features")
              and root.get("ownerFeatures") == NAMED_FEATURES, root.get("ownerFeatures"))

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
        after = surfaces(first, ACCOUNT_TOKEN)
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
        unplayed = first_row(first.request("GET", "/library/metadata/%s" % UNPLAYED_KEY, token=ACCOUNT_TOKEN))
        check("an unplayed cloud row clears view state, and no rating leaves the stars alone",
              not any(key in unplayed for key in ("viewCount", "viewOffset", "lastViewedAt"))
              and unplayed.get("userRating") == 7.0
              and unplayed.get("lastRatedAt") == local_state[UNPLAYED_KEY].get("lastRatedAt"),
              {k: unplayed.get(k) for k in ("viewCount", "viewOffset", "lastViewedAt", "userRating",
                                            "lastRatedAt")})
        cleared = first_row(first.request("GET", "/library/metadata/%s" % CLEARED_KEY, token=ACCOUNT_TOKEN))
        check("an explicit cloud rating of -1 clears the stars",
              "userRating" not in cleared and "lastRatedAt" not in cleared,
              {k: cleared.get(k) for k in ("userRating", "lastRatedAt")})
        history = container(first.request("GET", "/status/sessions/history/all?type=0,1",
                                          token=ACCOUNT_TOKEN)[1]).get("Metadata") or []
        check("an import is not a play: no history row beyond the local scrobble",
              [row.get("ratingKey") for row in history] == history_before,
              [row.get("ratingKey") for row in history])

        saved = read_json(claim_file)
        saved.pop("access_tokens", None)
        check("the claim file keeps machine, credentials, owner, features, id and cursor",
              saved == {"machine": MACHINE, "token": SERVER_TOKEN, "owner": OWNER, "email": EMAIL,
                        "feature_uuids": FEATURE_UUIDS, "account_id": ACCOUNT_ID,
                        "view_state_cursor": "opaque-cursor-1"}, saved)
        # Windows has no mode bits: a profile's folders are private to their user instead.
        check("the claim file is private (0600)",
              os.path.exists(claim_file) and (os.name == "nt" or stat.S_IMODE(os.stat(claim_file).st_mode) == 0o600),
              os.path.exists(claim_file) and oct(os.stat(claim_file).st_mode))
        claim_lines = [("INFO", r"CLAIMED by %s; server token stored" % OWNER),
                       ("INFO", r"servers\.xml POST -> 422"),
                       ("INFO", r"pubsub subscribed for %s" % MACHINE),
                       ("INFO", r"connectivity PUT -> 200 b''"),
                       ("INFO", r"pubsub: reachability 0 for asyncIdentifier %s" % re.escape(str(asked))),
                       ("ERROR", r"claim exchange failed: .*")]
        # plex.tv's verdict comes down the pubsub stream whenever it is sent, so a slow runner
        # stopped rex before logging it: wait for the lines, then stop.
        deadline = time.time() + 30
        while time.time() < deadline and not all(logged(first.output(), *line) for line in claim_lines):
            time.sleep(0.2)
        stub.clear()
        first.stop()
        first_log = first.output()
        check("startup says the claim state, the token gate and where it listens",
              logged(first_log, "INFO", r"claim state: unclaimed")
              and logged(first_log, "INFO", r"token gate: OFF -- unclaimed: this machine and its "
                                            r"private networks \(allowedNetworks\) can read the library")
              and logged(first_log, "INFO", r"rex on http://\S+:%d  machine=%s  items=\d+  "
                                            r"parts=proxy" % (ports[0], MACHINE)),
              first_log[-3000:])
        check("the heartbeat and the proxy-behind-a-public-URL warning are logged",
              logged(first_log, "INFO", r"heartbeat every 2s for machine %s -> %s"
                     % (MACHINE, re.escape(ANNOUNCE_URL)))
              and logged(first_log, "WARNING", r"part-mode=proxy behind a public URL: .*"),
              first_log[-3000:])
        missing = [message for level, message in claim_lines if not logged(first_log, level, message)]
        check("the claim is logged, and its connectivity PUT", not missing,
              "missing %s in %s" % (missing, first_log[-2000:]))
        stub.pubsub_gap = False

        # -- restart: the claim survives -------------------------------------------
        second = Instance(directory, ports[1], ports[2], extra=("--verbose",), announce=ANNOUNCE_URLS)
        restarted = surfaces(second, ACCOUNT_TOKEN)
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
        answered = wait_for(lambda: stub.taken("plex.tv", "/api/servers/.*/connectivity", "PUT"), 10)
        check("a claimed startup publishes, resubscribes and asks for a reachability check again",
              stub.taken("plex.tv", "/servers.xml", "POST")
              and stub.taken("pubsub.plex.tv", "/sub/eventsource/%d/%s" % (ACCOUNT_ID, MACHINE))
              and answered and re.fullmatch(UUID4, dict(answered[0]["query"])["asyncIdentifier"]),
              [(r["host"], r["path"]) for r in stub.records])
        beats = stub.taken("servers.plex.tv", "/devices/.*", "PUT")
        check("a claimed startup announces with the server's own token",
              beats and dict(beats[0]["query"])["X-Plex-Token"] == SERVER_TOKEN,
              beats and beats[0]["query"])
        real = [k for k, _ in CONNECTIONS["query"]]
        check("two --announce-url connections are two Connection[][uri] pairs, as a real PMS sends",
              beats and beats[0]["query"] == [("Connection[][uri]", uri) for uri in ANNOUNCE_URLS.split(",")]
              + [("httpsEnabled", "1"), ("httpsRequired", "0"), ("dnsRebindingProtection", "0"),
                 ("X-Plex-Token", SERVER_TOKEN)]
              and real[:17] == ["Connection[][uri]"] * 17 and real[17] == "httpsEnabled",
              beats and beats[0]["query"])

        # -- claimed: the token gate is up, as on a real PMS -------------------------
        access_path = "/api/v2/server/access_tokens"
        reads = lambda: stub.taken("plex.tv", access_path, "GET")
        startup = [r for r in reads() if r["query"] == [("auth_token", SERVER_TOKEN)]]
        publishes = stub.taken("plex.tv", "/servers.xml", "POST")
        real = recorded("claimed_start", "GET", access_path)
        check("a claimed startup reads plex.tv's access-token list first, as the recording did",
              startup and publishes and startup[0]["at"] < publishes[0]["at"]
              and [k for k, _ in real["query"]] == ["auth_token"]
              and sent(startup[0]) == PMS_HEADERS + [("x-plex-token", SERVER_TOKEN)]
              and names(startup[0]["headers"]) == names(real["req_headers"]),
              [(r["query"], sent(r)) for r in reads()])
        check("claimed: /identity still answers without a token",
              second.request("GET", "/identity")[0] == 200)
        for label, path in (("the library", "/library/sections"),
                            ("an item", "/library/metadata/%s" % WATCHED_KEY),
                            ("the account", "/myplex/account")):
            status, body = second.request("GET", path, raw=True)
            check("claimed: no token is the measured 401 on %s" % label,
                  status == 401 and b"401 Unauthorized" in body, (status, body[:120]))
        check("claimed: the server's own token opens it",
              second.request("GET", "/library/sections", token=SERVER_TOKEN)[0] == 200)
        check("claimed: a token plex.tv lists opens it, as a header or in the query",
              second.request("GET", "/library/sections", token=ACCOUNT_TOKEN)[0] == 200
              and second.request("GET", "/library/sections?X-Plex-Token=%s" % DEVICE_TOKEN)[0] == 200)
        time.sleep(ACCESS_WINDOW + 0.5)
        before = len(reads())
        status = second.request("GET", "/library/sections", token=UNLISTED_TOKEN)[0]
        asked = reads()[before:]
        check("claimed: a token plex.tv does not list is a 401, after one re-read naming it",
              status == 401 and len(asked) == 1
              and asked[0]["query"] == [("auth_token", SERVER_TOKEN), ("unknownToken", UNLISTED_TOKEN)]
              and [k for k, _ in ACCESS["request"]["query_on_unknown_token"]]
              == ["auth_token", "unknownToken"]
              and sent(asked[0]) == PMS_HEADERS + [("x-plex-token", SERVER_TOKEN)],
              (status, [(r["query"], sent(r)) for r in asked]))
        with stub.lock:
            stub.late_tokens.append(LATE_TOKEN)
        before = len(reads())
        status = second.request("GET", "/library/sections", token=LATE_TOKEN)[0]
        check("claimed: within the measured window an unknown token is refused without asking again",
              status == 401 and len(reads()) == before, (status, len(reads()) - before))
        time.sleep(ACCESS_WINDOW + 0.5)
        status = second.request("GET", "/library/sections", token=LATE_TOKEN)[0]
        check("claimed: a token plex.tv starts listing opens it at the next re-read",
              status == 200 and len(reads()) == before + 1
              and dict(reads()[-1]["query"]).get("unknownToken") == LATE_TOKEN,
              (status, [r["query"] for r in reads()[before:]]))
        check("claimed: a listed row with tokenDisabled=1 is refused",
              second.request("GET", "/library/sections", token=DISABLED_TOKEN)[0] == 401)
        delegation = "/security/token?type=delegation&scope=all"
        check("claimed: /security/token is 403 to an unlisted token and answers a listed one",
              second.request("GET", delegation, token=UNLISTED_TOKEN)[0] == 403
              and second.request("GET", delegation, token=ACCOUNT_TOKEN)[0] == 200)
        # Measured on a claimed 1.42.2 PMS (2026-10-13): the minted token opens the library as
        # a header or in the query, and is refused only a token of its own.
        status, body = second.request("GET", delegation, token=ACCOUNT_TOKEN)
        transient = (container(body) or {}).get("token") if status == 200 else None
        check("claimed: a /security/token token opens the server, but mints no other",
              transient and transient.startswith("transient-")
              and second.request("GET", "/library/sections", token=transient)[0] == 200
              and second.request("GET", "/library/sections?X-Plex-Token=%s" % transient)[0] == 200
              and second.request("GET", delegation, token=transient)[0] == 403,
              (status, transient))

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
        status, files = log_archive(second, ACCOUNT_TOKEN)
        rex_log = files.get("rex.log", "")
        check("Download Logs hands back the startup lines, claimed",
              status == 200 and logged(rex_log, "INFO", r"claim state: CLAIMED by %s" % OWNER,
                                       ARCHIVE_LINE)
              and logged(rex_log, "INFO", r"token gate: ENFORCED -- claimed: the server's own "
                                              r"token and the ones plex\.tv lists for it", ARCHIVE_LINE),
              (status, sorted(files), rex_log[-1500:]))
        check("Download Logs' request log masks a query-string token",
              DEVICE_TOKEN not in files.get("requests.log", "")
              and "X-Plex-Token=xxxxxxxxxxxxxxxxxxxx" in files.get("requests.log", ""),
              [line for line in files.get("requests.log", "").splitlines() if "oken" in line][:5])
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
        check("--verbose request lines mask a query-string token",
              DEVICE_TOKEN not in second_log
              and logged(second_log, "DEBUG", r'"GET /library/sections\?X-Plex-Token=x{20} HTTP/1\.1" 200 -'),
              [line for line in second_log.splitlines() if "oken=" in line][:5])
        check("without --verbose there are no request lines",
              not logged(first_log, "DEBUG", r'".*'), "first run had DEBUG lines")

        # -- restart while plex.tv refuses the list: the last one read still holds -----
        kept = read_json(claim_file).get("access_tokens") or []
        check("the claim file keeps the enabled access tokens plex.tv listed",
              ACCOUNT_TOKEN in kept and DEVICE_TOKEN in kept and DISABLED_TOKEN not in kept, kept)
        before_refresh = read_json(claim_file)
        with stub.lock:
            stub.refuse_access = True
            stub.user_id = ACCOUNT_ID + 1
            stub.broken_features = True
        stub.clear()
        # A plain-http connection URI: the reference announced httpsEnabled=1 beside one.
        third = Instance(directory, ports[0], ports[2], announce="http://10.0.0.1:32400")
        check("a restart whose list read fails still lets listed tokens in",
              wait_for(lambda: stub.taken("plex.tv", access_path, "GET"), 10)
              and third.request("GET", "/library/sections", token=ACCOUNT_TOKEN)[0] == 200
              and third.request("GET", "/library/sections?X-Plex-Token=%s" % DEVICE_TOKEN)[0] == 200
              and third.request("GET", "/library/sections", token=DISABLED_TOKEN)[0] == 401,
              third.output()[-1500:])
        beats = stub.taken("servers.plex.tv", "/devices/.*", "PUT")
        check("httpsEnabled does not follow the connection URI's scheme",
              beats and beats[0]["query"][:2] == [("Connection[][uri]", "http://10.0.0.1:32400"),
                                                  ("httpsEnabled", "1")],
              beats and beats[0]["query"])
        broken = wait_for(lambda: stub.taken("servers.plex.tv", "/api/v2/features"), 20)
        time.sleep(0.5)
        after_refresh = read_json(claim_file)
        check("an owner read and a broken feature table change nothing: one last-good update",
              broken and stub.taken("servers.plex.tv", "/api/v2/user")
              and after_refresh.get("account_id") == before_refresh.get("account_id") == ACCOUNT_ID
              and after_refresh.get("feature_uuids") == before_refresh.get("feature_uuids") == FEATURE_UUIDS,
              {k: (before_refresh.get(k), after_refresh.get(k)) for k in ("account_id", "feature_uuids")})
        third.stop()
    finally:
        for instance in LIVE:
            if instance.proc.poll() is None:
                instance.proc.kill()
                instance.proc.wait()
        stub.shutdown()
        shutil.rmtree(directory, ignore_errors=True)

    placeholder_identity()
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
