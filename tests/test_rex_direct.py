#!/usr/bin/env python3
"""rex-direct, black-box, against a LOCAL stub of its provider (DMM's register endpoint) and
of plex.tv (docs/rex-direct-api.md).

Once claimed, rex POSTs its machineIdentifier (and its LAN address as `lanIp` when that is
RFC 1918, none when bound to loopback) with the claim's server token to the provider
(`REX_DIRECT_URL` points it at the stub below; no key), which answers the server's
`<hash>.rex-direct...` name, the IP that name points at, and the certificate for it. rex
serves that certificate to a TLS client asking for that name (SNI), keeps `--tls-cert`'s for
every other name, announces `https://<name>:<port>` beside `--announce-url`'s list, and keeps
the answer in `rex-direct.json` beside the catalog for a start the provider does not answer.
A provider may add `lanHostname`, a name on rex's LAN one label under `<name>`: rex serves the
certificate for it too and announces `https://<lanHostname>:<--port>` after the public name; one
that is no DNS name refuses the whole answer.
Unclaimed it registers nothing until a claim lands, then at once, without a restart; a 202
is a provider still issuing the certificate, asked again after its `retryAfter` (15 s at
least, so that leg takes ~15 s). Nothing here reaches DMM or plex.tv: the stub mints its own
throwaway certificates with `openssl` and takes plex.tv's claim exchange and device PUT
through `REX_PLEXTV_URL`. The stub's hostname is under `rex-direct.test`, not DMM's domain:
rex takes the wildcard from the name the provider answers, not from a domain of its own.

    python3 tests/test_rex_direct.py
"""
import http.client
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
MACHINE = json.load(open(CATALOG))["machineIdentifier"]
KEY = "PROVIDER-KEY-STUB-5f3a91"
SERVER_TOKEN = "SERVER-TOKEN-STUB-77d1"
CLAIM_TOKEN = "claim-STUB-0c4e"
REFUSAL = "the token does not own this server"
HASH = "a1b2c3d4e5f6"
HOSTNAME = "%s.rex-direct.test" % HASH
IP = "203.0.113.7"
# The name the provider gives rex on its LAN, one label under HOSTNAME, and its echo of `lanIp`.
LAN_HOSTNAME = "lan.%s" % HOSTNAME
LAN_IP = "192.168.1.20"
LAN = "http://10.0.0.1:32400"
STDERR_LINE = r"^\d\d:\d\d:\d\d %-7s %s$"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def logged(text, level, message):
    return re.search(STDERR_LINE % (re.escape(level), message), text, re.M) is not None


class Stub(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, answer):
        super().__init__(("127.0.0.1", port), Handler)
        self.lock = threading.Lock()
        self.records = []
        self.answer = answer
        self.status = 200
        # (status, body) answered before `status`, one per register request.
        self.queue = []

    def taken(self, path):
        with self.lock:
            return [r for r in self.records if re.fullmatch(path, r["path"])]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self):
        target = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        record = {"method": self.command, "path": target.path,
                  "query": urllib.parse.parse_qsl(target.query, keep_blank_values=True),
                  "headers": {k.lower(): v for k, v in self.headers.items()},
                  "body": self.rfile.read(length) if length else b""}
        with self.server.lock:
            self.server.records.append(record)
        if target.path == "/api/rex-direct/register":
            # The provider contract's answers (docs/rex-direct-api.md).
            with self.server.lock:
                status, body = self.server.queue.pop(0) if self.server.queue else (self.server.status, None)
            if body is None:
                body = self.server.answer if status == 200 else json.dumps({"error": REFUSAL}).encode()
            kind = "application/json"
        elif target.path == "/plex.tv/api/claim/exchange" and self.command == "POST":
            status, kind = 200, "application/xml"
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n<user id="1" username="stubowner" '
                    'email="owner@stub.invalid" authToken="%s"/>\n' % SERVER_TOKEN).encode()
        elif target.path == "/servers.plex.tv/devices/%s" % MACHINE and self.command == "PUT":
            status, kind = 200, "application/xml"
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n<device clientIdentifier="%s"/>\n'
                    % MACHINE).encode()
        elif target.path == "/plex.tv/api/v2/resources":
            status, kind = 200, "application/json"
            body = json.dumps([{"name": "rex-ui-fixture", "provides": "server"}]).encode()
        else:
            status, kind, body = 404, "text/plain", b"stub: no such route"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_POST = _handle


class Instance:
    def __init__(self, directory, port, stub_port, register_url, extra=(), env_extra=None):
        self.port = port
        command = server_command("--port", port, "--catalog", os.path.join(directory, "catalog.json"),
                                 "--gdm", "off", "--heartbeat-interval", 2, *extra, root=ROOT)
        env = dict(os.environ, REX_DIRECT_URL=register_url,
                   REX_PLEXTV_URL="http://127.0.0.1:%d" % stub_port, **(env_extra or {}))
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN", "REX_DIRECT_PORT", "REX_DIRECT_KEY",
                     "REX_SCROBBLING_DECRYPT_SECRET_B64"):
            if name not in (env_extra or {}):
                env.pop(name, None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.proc.kill()
        raise SystemExit("rex did not start:\n" + self.output())

    def claim(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/myplex/claim?token=%s" % CLAIM_TOKEN,
                         headers={"X-Plex-Client-Identifier": "rex-direct-suite"})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def served(port, sni):
    """The DER certificate the port serves for `sni` (None: no SNI), or None."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=6)
    try:
        with context.wrap_socket(raw, server_hostname=sni) as wrapped:
            return wrapped.getpeercert(binary_form=True)
    except OSError:
        return None
    finally:
        raw.close()


def plain(port):
    with socket.create_connection(("127.0.0.1", port), timeout=6) as sock:
        sock.sendall(b"GET /identity HTTP/1.1\r\nHost: h\r\n\r\n")
        return sock.recv(64).partition(b"\r\n")[0]


def der(pem_path):
    return ssl.PEM_cert_to_DER_cert(open(pem_path).read())


def announced(stub):
    """The connection URIs of each device PUT the stub took, in order."""
    return [[value for name, value in r["query"] if name == "Connection[][uri]"]
            for r in stub.taken("/servers.plex.tv/devices/%s" % MACHINE)]


def on_container_network():
    """Whether the default route leaves through a veth (Docker, Podman or LXC without host
    networking), as rex reads it off /proc/net/route and sysfs: then it announces no LAN address
    and sends no lanIp, since nothing outside that network reaches one. Linux only."""
    try:
        with open("/proc/net/route") as table:
            iface = next(line.split()[0] for line in list(table)[1:] if line.split()[1:2] == ["00000000"])
        with open("/sys/class/net/%s/ifindex" % iface) as f1, open("/sys/class/net/%s/iflink" % iface) as f2:
            return f1.read().strip() != f2.read().strip()
    except (OSError, StopIteration, IndexError):
        return False


def local_address():
    """The address rex reaches the world from (`local_interface_address`); None without a route,
    and None on a container network (`lan_interface_address`), as CI's bridged container is."""
    if on_container_network():
        return None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def lan(port):
    """The LAN connection rex announces of itself unasked, as a real PMS does: the address it
    reaches the world from, on its port; none without a route."""
    address = local_address()
    return [] if not address or address.startswith("127.") or address == "0.0.0.0" else ["http://%s:%d" % (address, port)]


def lan_ip():
    """What rex sends its provider as `lanIp`: that address when it is RFC 1918, else None
    (never a public one, a tunnel's 100.64/10 or IPv6)."""
    address = local_address()
    private = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    return address if address and any(ipaddress.ip_address(address) in ipaddress.ip_network(net) for net in private) else None


def wait_for(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    return predicate()


def setup(directory, claimed=True):
    os.makedirs(directory)
    shutil.copy(CATALOG, os.path.join(directory, "catalog.json"))
    if claimed:
        # A claim kept from an earlier start: the announce needs one (`plextv::Beat`).
        with open(os.path.join(directory, "claim_state.json"), "w") as handle:
            json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "stubowner",
                       "email": "owner@stub.invalid", "feature_uuids": [], "account_id": None,
                       "view_state_cursor": None, "access_tokens": []}, handle)


def main():
    if not shutil.which("openssl"):
        print("SKIP the whole rex-direct suite (openssl is not on PATH)")
        return 0
    work = tempfile.mkdtemp(prefix="rex-direct-")
    # The provider's wildcard, with an EC key in SEC1 form (`BEGIN EC PRIVATE KEY`), which is what
    # many ACME clients write; the operator's own certificate is RSA in PKCS#8.
    direct_key, direct_crt = os.path.join(work, "direct.key"), os.path.join(work, "direct.crt")
    subprocess.check_call(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                           "-out", direct_key], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["openssl", "req", "-x509", "-key", direct_key, "-out", direct_crt,
                           "-days", "2", "-subj", "/CN=*.rex-direct.test"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base_key, base_crt = os.path.join(work, "base.key"), os.path.join(work, "base.crt")
    subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", base_key,
                           "-out", base_crt, "-days", "2", "-nodes", "-subj", "/CN=rex.example"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("the stub's key is a SEC1 EC key", open(direct_key).read().startswith("-----BEGIN EC PRIVATE KEY-----"),
          open(direct_key).read()[:40])
    answer = json.dumps({"hostname": HOSTNAME, "hash": HASH, "ip": IP,
                         "certificate": open(direct_crt).read(),
                         "privateKey": open(direct_key).read()}).encode()
    # Legs 1 to 3c are answered with a LAN name as well; 4 and 5 without, as before LAN names.
    answer_lan = json.dumps(dict(json.loads(answer), lanHostname=LAN_HOSTNAME, lanIp=LAN_IP)).encode()

    stub = Stub(free_port(), answer_lan)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    register_url = "http://127.0.0.1:%d/api/rex-direct/register" % stub.server_address[1]
    closed_url = "http://127.0.0.1:%d/api/rex-direct/register" % free_port()  # nothing listens
    live = []
    try:
        # 1. Claimed: rex registers before its port opens, and the provider's certificate
        #    answers for its name, --tls-cert's for any other.
        first = os.path.join(work, "first")
        setup(first)
        instance = Instance(first, free_port(), stub.server_address[1], register_url,
                            extra=("--tls-cert", base_crt, "--tls-key", base_key, "--announce-url", LAN),
                            env_extra={"REX_DIRECT_PORT": "32400"})
        live.append(instance)
        posts = stub.taken("/api/rex-direct/register")
        check("claimed: one register request, before the port opened", len(posts) == 1, posts)
        post = posts[0] if posts else {"method": None, "headers": {}, "body": b"{}"}
        check("...a POST as JSON",
              (post["method"], post["headers"].get("content-type")) == ("POST", "application/json"),
              (post["method"], post["headers"]))
        check("...carrying the claim's server token and the machineIdentifier as plex.tv headers",
              (post["headers"].get("x-plex-token"), post["headers"].get("x-plex-client-identifier"))
              == (SERVER_TOKEN, MACHINE), post["headers"])
        check("...and no key: no Authorization without REX_DIRECT_KEY, no x-api-key",
              "authorization" not in post["headers"] and "x-api-key" not in post["headers"], post["headers"])
        # --announce-url names a plain http RFC 1918 address (LAN), so that is the lanIp, whatever
        # this machine's own interface address is.
        body = {"machineIdentifier": MACHINE, "lanIp": "10.0.0.1"}
        check("...naming the server's machineIdentifier and --announce-url's LAN address as lanIp (10.0.0.1)",
              json.loads(post["body"] or b"{}") == body, post["body"])
        check("the rex-direct name gets the provider's certificate", served(instance.port, HOSTNAME) == der(direct_crt))
        check("...and so does another name under its wildcard",
              served(instance.port, "other.rex-direct.test") == der(direct_crt))
        check("...and so does its LAN name, one label under it",
              served(instance.port, LAN_HOSTNAME) == der(direct_crt))
        check("another name gets --tls-cert's", served(instance.port, "rex.example") == der(base_crt))
        check("...and so do a deeper name under the LAN name and no SNI",
              served(instance.port, "x." + LAN_HOSTNAME) == der(base_crt)
              and served(instance.port, None) == der(base_crt))
        check("the same port still answers plain HTTP", plain(instance.port) == b"HTTP/1.1 200 OK")
        want = ([LAN, "https://%s:32400" % HOSTNAME, "https://%s:%d" % (LAN_HOSTNAME, instance.port)]
                + [uri for uri in lan(instance.port) if uri != LAN])
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("claimed, it announces the rex-direct URL on REX_DIRECT_PORT after --announce-url's, then the "
              "LAN name on rex's own port (LAN clients do not pass the router's forward)", got, announced(stub))
        kept = os.path.join(first, "rex-direct.json")
        check("the answer is kept in rex-direct.json beside the catalog",
              os.path.exists(kept) and json.load(open(kept)) == json.loads(answer_lan))
        if os.name != "nt":
            check("...owner-only (0600): it holds the private key",
                  os.path.exists(kept) and stat.S_IMODE(os.stat(kept).st_mode) == 0o600,
                  os.path.exists(kept) and oct(os.stat(kept).st_mode))
        instance.stop()
        log = instance.output()
        check("one INFO line names the URL and the IP",
              logged(log, "INFO", re.escape("rex-direct: https://%s:32400 -> %s" % (HOSTNAME, IP))), log[-3000:])
        check("...and one the LAN name and its IP",
              logged(log, "INFO", re.escape("rex-direct: https://%s:%d -> %s (LAN)"
                                            % (LAN_HOSTNAME, instance.port, LAN_IP))), log[-3000:])
        check("the log carries neither the token nor the private key",
              SERVER_TOKEN not in log and "PRIVATE KEY" not in log and open(direct_key).read().splitlines()[1] not in log)

        # 2. The provider down at the next start: the kept answer serves, now without
        #    --tls-cert, so every name gets the rex-direct certificate, on rex's own port.
        before = open(kept, "rb").read()
        with stub.lock:
            stub.records = []
        instance = Instance(first, free_port(), stub.server_address[1], closed_url)
        live.append(instance)
        for label, sni in (("its name", HOSTNAME), ("its LAN name", LAN_HOSTNAME), ("another name", "rex.example"),
                           ("no SNI", None)):
            check("provider down, kept answer, no --tls-cert: %s gets the rex-direct certificate" % label,
                  served(instance.port, sni) == der(direct_crt))
        check("...and the same port still answers plain HTTP", plain(instance.port) == b"HTTP/1.1 200 OK")
        want = ["https://%s:%d" % (name, instance.port) for name in (HOSTNAME, LAN_HOSTNAME)] + lan(instance.port)
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("...announcing its LAN address and the kept names on rex's own port", got, announced(stub))
        instance.stop()
        log = instance.output()
        check("...the failure is a WARN", logged(log, "WARNING", r"rex-direct: registration failed: .*"), log[-3000:])
        check("...the kept answer is said", logged(log, "INFO", re.escape(
            "rex-direct: https://%s:%d -> %s" % (HOSTNAME, instance.port, IP)) + r".*"), log[-3000:])
        check("...and its LAN name", logged(log, "INFO", re.escape(
            "rex-direct: https://%s:%d -> %s (LAN)" % (LAN_HOSTNAME, instance.port, LAN_IP))), log[-3000:])
        check("...and the file is left as it was", open(kept, "rb").read() == before)

        # 3. The provider refuses and nothing is kept: rex runs on without rex-direct.
        refused = os.path.join(work, "refused")
        setup(refused)
        stub.status = 401
        with stub.lock:
            stub.records = []
        instance = Instance(refused, free_port(), stub.server_address[1], register_url)
        live.append(instance)
        check("a 401 leaves rex running", instance.proc.poll() is None)
        check("...answering plain HTTP", plain(instance.port) == b"HTTP/1.1 200 OK")
        check("...with no certificate to serve", served(instance.port, HOSTNAME) is None)
        time.sleep(3)
        check("...announcing its LAN address alone", all(uris == lan(instance.port) for uris in announced(stub)),
              announced(stub))
        check("...and keeping nothing", not os.path.exists(os.path.join(refused, "rex-direct.json")))
        instance.stop()
        log = instance.output()
        check("...the refusal is a WARN with the provider's status and reason",
              logged(log, "WARNING", r"rex-direct: registration failed: HTTP 401 \(%s\); retrying in 10 min"
                     % re.escape(REFUSAL)), log[-3000:])

        # 3b. Bound to loopback rex has no LAN address, so the request names none.
        loopback = os.path.join(work, "loopback")
        setup(loopback)
        stub.status = 200
        with stub.lock:
            stub.records = []
        instance = Instance(loopback, free_port(), stub.server_address[1], register_url, extra=("--host", "127.0.0.1"))
        live.append(instance)
        posts = stub.taken("/api/rex-direct/register")
        check("bound to loopback, the request carries no lanIp",
              len(posts) == 1 and json.loads(posts[0]["body"] or b"{}") == {"machineIdentifier": MACHINE},
              posts and posts[0]["body"])
        instance.stop()

        # 3c. A lanHostname that is no DNS name refuses the whole answer, as a bad hostname does.
        bad = os.path.join(work, "bad-lan")
        setup(bad)
        with stub.lock:
            stub.records = []
            stub.queue = [(200, json.dumps(dict(json.loads(answer_lan), lanHostname="lan.evil.example/x")).encode())]
        instance = Instance(bad, free_port(), stub.server_address[1], register_url)
        live.append(instance)
        check("an answer whose lanHostname is no DNS name serves no certificate",
              served(instance.port, HOSTNAME) is None)
        time.sleep(3)
        check("...announces no rex-direct URL", all(uris == lan(instance.port) for uris in announced(stub)),
              announced(stub))
        check("...and keeps nothing", not os.path.exists(os.path.join(bad, "rex-direct.json")))
        instance.stop()
        log = instance.output()
        check("...a WARN that does not quote it",
              logged(log, "WARNING", re.escape("rex-direct: registration failed: the answer's lanHostname is not "
                                               "a DNS name; retrying in 10 min")) and "evil" not in log, log[-3000:])
        stub.answer = answer

        # 4. Unclaimed it registers nothing, since the provider knows rex by its claim; a claim
        #    registers at once and announces the rex-direct URL, without a restart. With
        #    REX_DIRECT_KEY the request carries it as a bearer key.
        unclaimed = os.path.join(work, "unclaimed")
        setup(unclaimed, claimed=False)
        stub.status = 200
        with stub.lock:
            stub.records = []
        instance = Instance(unclaimed, free_port(), stub.server_address[1], register_url,
                            env_extra={"REX_DIRECT_KEY": KEY})
        live.append(instance)
        time.sleep(3)
        check("unclaimed, it registers nothing", stub.taken("/api/rex-direct/register") == [],
              stub.taken("/api/rex-direct/register"))
        check("...announces nothing", announced(stub) == [], announced(stub))
        check("...and serves no certificate", served(instance.port, HOSTNAME) is None)
        with stub.lock:
            stub.records = []  # what follows is the claim's
        status = instance.claim()
        check("a claim at runtime is answered", status == 200, status)
        posts = wait_for(lambda: stub.taken("/api/rex-direct/register"), 10)
        check("...and registers at once, without a restart",
              len(posts) == 1 and instance.proc.poll() is None, posts)
        post = posts[0] if posts else {"headers": {}}
        check("...with the token the claim was given",
              (post["headers"].get("x-plex-token"), post["headers"].get("x-plex-client-identifier"))
              == (SERVER_TOKEN, MACHINE), post["headers"])
        check("...and REX_DIRECT_KEY as a bearer key",
              post["headers"].get("authorization") == "Bearer %s" % KEY, post["headers"])
        want = ["https://%s:%d" % (HOSTNAME, instance.port)] + lan(instance.port)
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("...announcing its LAN address and the rex-direct URL", got, announced(stub))
        check("...and serving its certificate", served(instance.port, HOSTNAME) == der(direct_crt))
        instance.stop()
        log = instance.output()
        check("...having said at start that it registers once claimed",
              logged(log, "INFO", re.escape("rex-direct: registers with %s once claimed (POST /myplex/claim)"
                                            % register_url)), log[-3000:])
        check("...the log carries neither the token nor the key", SERVER_TOKEN not in log and KEY not in log)

        # 5. A 202 is a provider still issuing the certificate: an INFO, the port opens at
        #    once, and rex asks again after retryAfter, kept to 15 s at least.
        pending = os.path.join(work, "pending")
        setup(pending)
        with stub.lock:
            stub.records = []
            stub.queue = [(202, json.dumps({"retryAfter": 1, "error": "issuing the certificate"}).encode())]
        started = time.time()
        instance = Instance(pending, free_port(), stub.server_address[1], register_url)
        live.append(instance)
        check("a 202 at start does not hold the port", time.time() - started < 10, time.time() - started)
        check("...which serves no certificate yet", served(instance.port, HOSTNAME) is None)
        posts = wait_for(lambda: len(stub.taken("/api/rex-direct/register")) >= 2 and stub.taken("/api/rex-direct/register"), 25)
        asked = [r for r in stub.records if r["path"] == "/api/rex-direct/register"]
        check("...asked again after 15 s, not the 1 s retryAfter said", posts and len(posts) == 2
              and time.time() - started >= 14, (len(asked), time.time() - started))
        check("...then serves the certificate", wait_for(lambda: served(instance.port, HOSTNAME) == der(direct_crt), 5))
        want = ["https://%s:%d" % (HOSTNAME, instance.port)] + lan(instance.port)
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("...and announces its URL, then its LAN address", got, announced(stub))
        instance.stop()
        log = instance.output()
        check("...the 202 is an INFO with its reason, not a failure",
              logged(log, "INFO", re.escape("rex-direct: the provider is still issuing the certificate "
                                            "(issuing the certificate); asking again in 15 s"))
              and "registration failed" not in log, log[-3000:])

        # 6. A provider over plain http to another machine is refused: the answer carries a
        #    private key and the request the claim's token.
        insecure = os.path.join(work, "insecure")
        setup(insecure)
        with stub.lock:
            stub.records = []
        instance = Instance(insecure, free_port(), stub.server_address[1], "http://203.0.113.9/api/rex-direct/register")
        live.append(instance)
        time.sleep(3)
        check("a non-loopback http:// provider gets no request", stub.taken("/api/rex-direct/register") == [])
        check("...and no announce names a rex-direct URL", all(uris == lan(instance.port) for uris in announced(stub)),
              announced(stub))
        instance.stop()
        log = instance.output()
        check("...it is a WARN, and rex-direct is off",
              logged(log, "WARNING", re.escape("rex-direct: off -- REX_DIRECT_URL must be https://") + r".*")
              and "rex-direct: https://" not in log, log[-3000:])

        # 7. REX_DIRECT_URL=off: nothing at all.
        off = os.path.join(work, "off")
        setup(off)
        with stub.lock:
            stub.records = []
        instance = Instance(off, free_port(), stub.server_address[1], "off")
        live.append(instance)
        lan_only = lan(instance.port)
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == lan_only], 10)
        check("REX_DIRECT_URL=off registers nothing", stub.taken("/api/rex-direct/register") == [])
        check("...and, claimed with no --announce-url, announces its LAN address as a real PMS does",
              lan_only and got and all(uris == lan_only for uris in announced(stub)), announced(stub))
        instance.stop()
        check("...and says nothing of rex-direct", "rex-direct:" not in instance.output(), instance.output()[-3000:])
    finally:
        for instance in live:
            if instance.proc.poll() is None:
                instance.proc.kill()
                instance.proc.wait()
        stub.shutdown()
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
