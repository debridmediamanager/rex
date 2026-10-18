#!/usr/bin/env python3
"""rex-direct, black-box, against a LOCAL stub of DMM's register endpoint and of plex.tv.

With `DMM_API_KEY` set, rex POSTs its machineIdentifier to DMM's
`/api/rex-direct/register` (`REX_DIRECT_URL` points it at the stub below), which answers
the server's `<hash>.rex-direct...` name, the IP that name points at, and the wildcard
certificate for it. rex serves that certificate to a TLS client asking for that name (SNI),
keeps `--tls-cert`'s for every other name, announces `https://<name>:<port>` beside
`--announce-url`'s list once claimed, and keeps the answer in `rex-direct.json` beside the
catalog for a start DMM does not answer. Nothing here reaches DMM or plex.tv: the stub
mints its own throwaway certificates with `openssl` and takes plex.tv's device PUT through
`REX_PLEXTV_URL`. The stub's hostname is under `rex-direct.test`, not DMM's domain: rex
takes the wildcard from the name DMM answers, not from a domain of its own.

    python3 tests/test_rex_direct.py
"""
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
KEY = "DMM-KEY-STUB-5f3a91"
SERVER_TOKEN = "SERVER-TOKEN-STUB"
HASH = "a1b2c3d4e5f6"
HOSTNAME = "%s.rex-direct.test" % HASH
IP = "203.0.113.7"
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
            # DMM's own answers (dmm src/pages/api/rex-direct/register.ts).
            if self.server.status == 200:
                status, body = 200, self.server.answer
            else:
                status, body = self.server.status, json.dumps({"error": "Invalid API key"}).encode()
            kind = "application/json"
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
        env = dict(os.environ, DMM_API_KEY=KEY, REX_DIRECT_URL=register_url,
                   REX_PLEXTV_URL="http://127.0.0.1:%d" % stub_port, **(env_extra or {}))
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN", "REX_DIRECT_PORT",
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
    # DMM's wildcard, with an EC key in SEC1 form (`BEGIN EC PRIVATE KEY`), which is what
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

    stub = Stub(free_port(), answer)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    register_url = "http://127.0.0.1:%d/api/rex-direct/register" % stub.server_address[1]
    closed_url = "http://127.0.0.1:%d/api/rex-direct/register" % free_port()  # nothing listens
    live = []
    try:
        # 1. DMM answers: its certificate for its name, --tls-cert's for any other.
        first = os.path.join(work, "first")
        setup(first)
        instance = Instance(first, free_port(), stub.server_address[1], register_url,
                            extra=("--tls-cert", base_crt, "--tls-key", base_key, "--announce-url", LAN),
                            env_extra={"REX_DIRECT_PORT": "32400"})
        live.append(instance)
        posts = stub.taken("/api/rex-direct/register")
        check("one register request at start", len(posts) == 1, posts)
        post = posts[0] if posts else {"method": None, "headers": {}, "body": b"{}"}
        check("...a POST with the key in x-api-key, as JSON",
              (post["method"], post["headers"].get("x-api-key"), post["headers"].get("content-type"))
              == ("POST", KEY, "application/json"), (post["method"], post["headers"]))
        check("...naming the server's machineIdentifier",
              json.loads(post["body"] or b"{}") == {"machineIdentifier": MACHINE}, post["body"])
        check("the rex-direct name gets DMM's certificate", served(instance.port, HOSTNAME) == der(direct_crt))
        check("...and so does another name under its wildcard",
              served(instance.port, "other.rex-direct.test") == der(direct_crt))
        check("another name gets --tls-cert's", served(instance.port, "rex.example") == der(base_crt))
        check("...and so does no SNI", served(instance.port, None) == der(base_crt))
        check("the same port still answers plain HTTP", plain(instance.port) == b"HTTP/1.1 200 OK")
        want = [LAN, "https://%s:32400" % HOSTNAME]
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("claimed, it announces the rex-direct URL on REX_DIRECT_PORT after --announce-url's",
              got, announced(stub))
        kept = os.path.join(first, "rex-direct.json")
        check("the answer is kept in rex-direct.json beside the catalog",
              os.path.exists(kept) and json.load(open(kept)) == json.loads(answer))
        if os.name != "nt":
            check("...owner-only (0600): it holds the private key",
                  os.path.exists(kept) and stat.S_IMODE(os.stat(kept).st_mode) == 0o600,
                  os.path.exists(kept) and oct(os.stat(kept).st_mode))
        instance.stop()
        log = instance.output()
        check("one INFO line names the URL and the IP",
              logged(log, "INFO", re.escape("rex-direct: https://%s:32400 -> %s" % (HOSTNAME, IP))), log[-3000:])
        check("the log carries neither the key nor the private key",
              KEY not in log and "PRIVATE KEY" not in log and open(direct_key).read().splitlines()[1] not in log)

        # 2. DMM down at the next start: the kept answer serves, now without --tls-cert, so
        #    every name gets the rex-direct certificate, on rex's own port by default.
        before = open(kept, "rb").read()
        with stub.lock:
            stub.records = []
        instance = Instance(first, free_port(), stub.server_address[1], closed_url)
        live.append(instance)
        for label, sni in (("its name", HOSTNAME), ("another name", "rex.example"), ("no SNI", None)):
            check("DMM down, kept answer, no --tls-cert: %s gets the rex-direct certificate" % label,
                  served(instance.port, sni) == der(direct_crt))
        check("...and the same port still answers plain HTTP", plain(instance.port) == b"HTTP/1.1 200 OK")
        want = ["https://%s:%d" % (HOSTNAME, instance.port)]
        got = wait_for(lambda: [uris for uris in announced(stub) if uris == want], 10)
        check("...announcing the kept name on rex's own port", got, announced(stub))
        instance.stop()
        log = instance.output()
        check("...the failure is a WARN", logged(log, "WARNING", r"rex-direct: registration failed: .*"), log[-3000:])
        check("...the kept answer is said", logged(log, "INFO", re.escape(
            "rex-direct: https://%s:%d -> %s" % (HOSTNAME, instance.port, IP)) + r".*"), log[-3000:])
        check("...and the file is left as it was", open(kept, "rb").read() == before)

        # 3. DMM refuses the key and nothing is kept: rex runs on without rex-direct.
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
        check("...announcing nothing", announced(stub) == [], announced(stub))
        check("...and keeping nothing", not os.path.exists(os.path.join(refused, "rex-direct.json")))
        instance.stop()
        log = instance.output()
        check("...the refusal is a WARN with DMM's status and reason",
              logged(log, "WARNING", r"rex-direct: registration failed: HTTP 401 \(Invalid API key\).*"),
              log[-3000:])
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
