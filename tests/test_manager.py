#!/usr/bin/env python3
"""rex manager: no server runs until one is started, and the manager keeps them running.

Drives a real manager process (`rust/target/release/rex manager`, so build it first) on a
temporary root, managing real rex processes on `tests/catalog_ui.json`: the native binary
by default, or any executable `REX_SERVER` names.

    python3 tests/test_manager.py
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from test_ports import free_port

# The servers the manager starts inherit this: no FFmpeg found on PATH (server_command.py).
os.environ.setdefault("REX_TRANSCODER", "off")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
REX = os.path.join(ROOT, "rust", "target", "release", "rex")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]),
          flush=True)


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def raw_status(port, request, timeout=3, half_close=True):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(request)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        raw = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
        first = bytes(raw).split(b"\r\n", 1)[0].split()
        return int(first[1]) if len(first) >= 2 else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def wait_for(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def get_json(port, path, headers=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     headers=dict({"Accept": "application/json",
                                                   "X-Plex-Client-Identifier": "manager-suite"},
                                                  **(headers or {})))
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw.strip() else {}


def server_binary(work):
    return os.environ.get("REX_SERVER") or os.path.join(ROOT, "rust", "target", "release", "rex")


class Manager:
    def __init__(self, root, binary):
        self.root, self.binary = root, binary
        self.port = free_port()
        self.proc = None

    def start(self):
        self.log = open(os.path.join(self.root + ".log"), "ab")
        self.proc = subprocess.Popen(
            [REX, "manager", "--root", self.root, "serve",
             "--listen", "127.0.0.1:%d" % self.port, "--binary", self.binary],
            cwd=ROOT, stdout=self.log, stderr=self.log, start_new_session=True)
        if not wait_for(lambda: answers(self.port), 30):
            raise SystemExit("the manager did not start:\n" + self.output())
        with open(os.path.join(self.root, "admin.token")) as handle:
            self.token = handle.read().strip()

    def output(self):
        with open(self.root + ".log", "rb") as handle:
            return handle.read().decode(errors="replace")

    def stop(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def api(self, method, path, body=None, token=None):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + (self.token if token is None else token),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw.startswith((b"{", b"[")) else raw
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")


def config(name, port, **extra):
    return dict({"name": name, "port": port, "catalog": CATALOG,
                 "gdm": "off", "assert_claimed": True}, **extra)


# Stands in for `rex catalog --seed`: writes catalog_ui.json into --out the way the
# builder writes a database, so the started server has rows to answer with.
FAKE_BUILDER = """#!%s
import subprocess, sys
out = sys.argv[sys.argv.index("--out") + 1]
subprocess.run([%r, "catalog", "--convert", %r, "--out", out], check=True)
print("built", out)
"""


def main():
    work = tempfile.mkdtemp(prefix="rex-manager-")
    root = os.path.join(work, "root")
    binary = server_binary(work)
    builder = os.path.join(work, "fake_build_catalog.py")
    with open(builder, "w") as handle:
        handle.write(FAKE_BUILDER % (sys.executable, REX, CATALOG))
    os.chmod(builder, 0o755)
    print("server: %s" % binary)
    manager = Manager(root, binary)
    try:
        manager.start()
        status, listed = manager.api("GET", "/servers")
        check("a fresh manager runs no server", status == 200 and listed == [], listed)
        check("the token file is private", oct(os.stat(os.path.join(root, "admin.token")).st_mode & 0o777) == "0o600")
        check("the database is private", oct(os.stat(os.path.join(root, "manager.db")).st_mode & 0o777) == "0o600")
        check("no token is a 401", manager.api("GET", "/servers", token="")[0] == 401)
        check("a wrong token is a 401", manager.api("GET", "/servers", token="nope")[0] == 401)
        page = manager.api("GET", "/", token="")
        check("the page loads without a token", page[0] == 200 and b"rex manager" in page[1])

        oversized = ("POST /servers HTTP/1.1\r\n"
                     "Host: manager\r\n"
                     "Authorization: Bearer %s\r\n"
                     "Content-Length: 16777217\r\n\r\n" % manager.token).encode()
        check("an oversized body declaration is rejected before allocation",
              raw_status(manager.port, oversized) == 413)
        long_target = ("GET /" + ("x" * 32768) + " HTTP/1.1\r\n"
                       "Host: manager\r\nAuthorization: Bearer %s\r\n\r\n" % manager.token).encode()
        check("an oversized request target is a 400", raw_status(manager.port, long_target) == 400)
        long_line = ("GET /servers HTTP/1.1 " + ("x" * 65536) + "\r\n"
                     "Host: manager\r\nAuthorization: Bearer %s\r\n\r\n" % manager.token).encode()
        check("an oversized request line is a 400", raw_status(manager.port, long_line) == 400)
        long_header = ("GET /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n"
                       "X-Long: %s\r\n\r\n" % (manager.token, "x" * 65536)).encode()
        check("an oversized header line is a 400", raw_status(manager.port, long_header) == 400)
        aggregate_headers = ("GET /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n" % manager.token)
        aggregate_headers += "".join("X-Aggregate-%d: %s\r\n" % (index, "x" * 60000) for index in range(5))
        check("an oversized header block is a 400", raw_status(manager.port, (aggregate_headers + "\r\n").encode()) == 400)
        many_headers = ("GET /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n" % manager.token)
        many_headers += "".join("X-Test-%d: x\r\n" % index for index in range(101))
        check("too many request headers are a 400", raw_status(manager.port, (many_headers + "\r\n").encode()) == 400)
        malformed_length = ("GET /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n"
                            "Content-Length: not-a-number\r\n\r\n" % manager.token).encode()
        check("a malformed Content-Length is a 400", raw_status(manager.port, malformed_length) == 400)
        conflicting = ("POST /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n"
                       "Content-Length: 1\r\nContent-Length: 2\r\n\r\n" % manager.token).encode()
        check("conflicting Content-Length values are a 400", raw_status(manager.port, conflicting) == 400)
        transfer = ("GET /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer %s\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n" % manager.token).encode()
        check("unsupported transfer encoding is a 400", raw_status(manager.port, transfer) == 400)
        unauthorized_body = ("POST /servers HTTP/1.1\r\nHost: manager\r\nAuthorization: Bearer wrong\r\n"
                             "Content-Length: 1\r\n\r\n").encode()
        check("unauthorized requests do not await their body",
              raw_status(manager.port, unauthorized_body, half_close=False) == 401)
        check("the API remains usable after rejected envelopes", manager.api("GET", "/servers")[0] == 200)

        a, b, c = free_port(), free_port(), free_port()
        status, made = manager.api("POST", "/servers", config("alpha", a, token="gate-secret",
                                                               plex_token="plex-secret",
                                                               friendly_name="Managed Alpha"))
        check("create answers 201 and leaves the server stopped",
              status == 201 and made.get("status") == "stopped" and made.get("desired") == "stopped",
              made)
        check("a created server does not listen", not answers(a))
        cfg = (made or {}).get("config") or {}
        check("keys are redacted", cfg.get("token") == "***" and cfg.get("plex_token") == "***", cfg)
        listed_text = json.dumps(manager.api("GET", "/servers")[1])
        check("no key appears in the listing",
              "gate-secret" not in listed_text and "plex-secret" not in listed_text)
        check("the catalog is copied into the server's database",
              os.path.exists(os.path.join(root, "servers", "alpha", "rex.db")))
        for label, body, want in (
                ("a duplicate name is a 409", config("alpha", c), 409),
                ("a duplicate port is a 409", config("other", a), 409),
                ("a bad name is a 400", config("Bad Name", c), 400),
                ("a missing catalog is a 400", config("gamma", c, catalog="/nonexistent.json"), 400),
                ("an unknown field is a 400", config("gamma", c, colour="red"), 400)):
            got = manager.api("POST", "/servers", body)
            check(label, got[0] == want, got)

        status, started = manager.api("POST", "/servers/alpha/start")
        check("start answers 200", status == 200, started)
        identity = wait_for(lambda: answers(a) and get_json(a, "/identity"), 90)
        machine = json.load(open(CATALOG))["machineIdentifier"]
        check("the started server answers /identity on its port",
              identity and identity["MediaContainer"]["machineIdentifier"] == machine, identity)
        check("its status becomes running",
              wait_for(lambda: manager.api("GET", "/servers/alpha")[1]["status"] == "running", 10))
        root_doc = get_json(a, "/", {"X-Plex-Token": "gate-secret"})
        check("the friendly name reaches the server", root_doc["MediaContainer"].get("friendlyName")
              == "Managed Alpha", root_doc["MediaContainer"].get("friendlyName"))
        try:
            get_json(a, "/library/sections")
            gated = False
        except urllib.error.HTTPError as error:
            gated = error.code == 401
        check("the server's own token gate is on", gated)
        check("a PATCH while running is a 409",
              manager.api("PATCH", "/servers/alpha", {"verbose": True})[0] == 409)

        get_json(a, "/:/scrobble?key=1007&identifier=com.plexapp.plugins.library",
                 {"X-Plex-Token": "gate-secret"})
        status, stopped = manager.api("POST", "/servers/alpha/stop")
        check("stop frees the port and records the exit",
              status == 200 and stopped["status"] == "stopped" and not answers(a)
              and stopped["last_exit"], stopped)
        manager.api("POST", "/servers/alpha/start")
        wait_for(lambda: answers(a), 90)
        watched = get_json(a, "/library/metadata/1007", {"X-Plex-Token": "gate-secret"})
        check("a write survives the server's restart (its state file is its own)",
              watched["MediaContainer"]["Metadata"][0].get("viewCount") == 1,
              watched["MediaContainer"]["Metadata"][0].get("viewCount"))

        pid = manager.api("GET", "/servers/alpha")[1]["pid"]
        os.killpg(pid, signal.SIGKILL)
        again = wait_for(lambda: (lambda v: v["pid"] and v["pid"] != pid and v)(
            manager.api("GET", "/servers/alpha")[1]), 30)
        check("a killed server is restarted", again and again["recent_failures"] == 1, again)
        check("and answers again", wait_for(lambda: answers(a), 90))

        # A server bound to one address is "running" once it answers there: the status probe
        # dialled 127.0.0.1 alone, and a server on another address stayed "starting".
        v6 = free_port()
        manager.api("POST", "/servers", config("sixer", v6, host="::1"))
        manager.api("POST", "/servers/sixer/start")
        check("a server on one address becomes running",
              wait_for(lambda: manager.api("GET", "/servers/sixer")[1]["status"] == "running", 60),
              manager.api("GET", "/servers/sixer")[1])
        # Keep the child alive until the API exposes the stop's desired state.
        # Thread.start() plus a sleep did not establish request ordering: the manager
        # accepts connections on a 100 ms poll and may handle start before stop.
        import threading
        pid = manager.api("GET", "/servers/sixer")[1]["pid"]
        os.killpg(pid, signal.SIGSTOP)
        stop_result = []
        stopping = threading.Thread(
            target=lambda: stop_result.append(manager.api("POST", "/servers/sixer/stop")))
        stopping.start()
        ordered = False
        started = None
        try:
            ordered = wait_for(lambda: (lambda row:
                row["desired"] == "stopped" and row["pid"] == pid)(
                    manager.api("GET", "/servers/sixer")[1]), 10)
            if ordered:
                started = manager.api("POST", "/servers/sixer/start")
        finally:
            try:
                os.killpg(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            stopping.join()
        def race_snapshot(response):
            if response is None:
                return None
            status, row = response
            return {"http": status, **{key: row.get(key) for key in
                                      ("desired", "status", "pid", "last_exit", "last_error")}}
        check("a start sent during a stop leaves the server running",
              ordered and started[0] == 200 and stop_result and stop_result[0][0] == 200
              and wait_for(lambda: (lambda row:
                  row["desired"] == "running" and row["status"] == "running")(
                      manager.api("GET", "/servers/sixer")[1]), 60),
              {"ordered": ordered, "start": race_snapshot(started),
               "stop": race_snapshot(stop_result[0]) if stop_result else None,
               "final": race_snapshot(manager.api("GET", "/servers/sixer"))})
        manager.api("POST", "/servers/sixer/stop")

        logs = manager.api("GET", "/servers/alpha/logs?lines=500")[1]["lines"]
        check("the logs carry the starts", sum("manager: starting alpha" in line for line in logs) >= 3,
              logs[-5:])
        check("the logs carry no key", not any("dummy-rd" in line or "gate-secret" in line
                                                for line in logs))

        # A server that dies at once: an unknown flag is argparse's exit 2.
        manager.api("POST", "/servers", config("broken", b, extra_args=["--no-such-flag"]))
        manager.api("POST", "/servers/broken/start")
        failed = wait_for(lambda: (lambda v: v["status"] == "failed" and v)(
            manager.api("GET", "/servers/broken")[1]), 60)
        check("a server that keeps dying is marked failed, not restarted forever",
              failed and failed["desired"] == "stopped" and "exit 2" in (failed["last_exit"] or ""),
              manager.api("GET", "/servers/broken")[1])

        # -- a server from its keys alone: no catalog until it is built ----------
        e = free_port()
        status, keyed = manager.api("POST", "/servers", {
            "name": "keyed", "port": e, "plex_token": "keyed-secret", "zurg_mount": "/mnt/zurg",
            "gdm": "off", "assert_claimed": True})
        check("a server can be created from its keys alone",
              status == 201 and keyed["status"] == "stopped"
              and keyed["config"]["plex_token"] == "***"
              and keyed["config"]["zurg_mount"] == "/mnt/zurg", keyed)
        status, refused = manager.api("POST", "/servers", {"name": "zurgkeys", "port": free_port(),
                                                           "zurg_url": "http://127.0.0.1:9"})
        # Sources register with the server themselves (docs/feed-api.md); there is no zurg to configure.
        check("a zurg url is no configuration field", status == 400 and "zurg_url" in refused.get("error", ""),
              (status, refused))
        status, refused = manager.api("POST", "/servers/keyed/start")
        check("it cannot start before its catalog is built",
              status == 409 and "build" in refused.get("error", "") and not answers(e), refused)
        os.environ["REX_MANAGER_BUILDER"] = builder
        manager.stop()
        manager.start()
        status, job = manager.api("POST", "/servers/keyed/build", {"start": True})
        built = wait_for(lambda: (lambda v: v["status"] in ("succeeded", "failed") and v)(
            manager.api("GET", "/servers/keyed/build")[1]), 60)
        check("build then start: the build succeeds", status == 202 and built
              and built["status"] == "succeeded", built)
        identity = wait_for(lambda: answers(e) and get_json(e, "/identity"), 90)
        check("and the server starts on the catalog it built",
              identity and identity["MediaContainer"]["machineIdentifier"] == machine, identity)
        manager.api("DELETE", "/servers/keyed?purge=1")
        os.environ.pop("REX_MANAGER_BUILDER")
        manager.stop()
        manager.start()

        manager.api("POST", "/servers", config("idle", c))
        status, job = manager.api("POST", "/servers/idle/build")
        done = wait_for(lambda: (lambda v: v["status"] in ("succeeded", "failed") and v)(
            manager.api("GET", "/servers/idle/build")[1]), 120)
        # The real `rex catalog --seed`: what is held here is the job -- it runs, ends,
        # and its log is kept.
        check("a build runs as a job with its own status and log",
              status == 202 and done and done["log"] and done["exit"] is not None, done)
        check("a PATCH while stopped changes the configuration",
              manager.api("PATCH", "/servers/idle", {"gdm": "off"})[1]["config"]["gdm"] == "off")

        # -- the manager's own restart --------------------------------------------
        manager.stop()
        check("stopping the manager stops its servers", wait_for(lambda: not answers(a), 30))
        manager.start()
        check("a restarted manager restores the server that was running",
              wait_for(lambda: answers(a), 90))
        views = {v["name"]: v for v in manager.api("GET", "/servers")[1]}
        check("and not the ones that were stopped or failed",
              views["idle"]["status"] == "stopped" and views["broken"]["status"] == "failed"
              and not answers(c), {k: v["status"] for k, v in views.items()})

        # A manager killed outright leaves its server in its own session.
        orphan = views["alpha"]["pid"]
        manager.stop(signal.SIGKILL)
        check("a hard-killed manager leaves its server running", answers(a))
        manager.start()
        fresh = wait_for(lambda: (lambda v: v["pid"] and v["pid"] != orphan and v["status"] == "running" and v)(
            manager.api("GET", "/servers/alpha")[1]), 90)
        check("the next manager replaces the orphan with a supervised server",
              fresh and answers(a), manager.api("GET", "/servers/alpha")[1])
        try:
            os.kill(orphan, 0)
            gone = False
        except ProcessLookupError:
            gone = True
        check("and the orphan is gone", gone)

        check("delete keeps the files", manager.api("DELETE", "/servers/idle")[0] == 200
              and os.path.isdir(os.path.join(root, "servers", "idle")))
        check("delete ?purge=1 removes them", manager.api("DELETE", "/servers/broken?purge=1")[0] == 200
              and not os.path.exists(os.path.join(root, "servers", "broken")))
        check("a deleted server is a 404", manager.api("GET", "/servers/idle")[0] == 404)
        manager.api("DELETE", "/servers/alpha")
        check("deleting a running server stops it", wait_for(lambda: not answers(a), 30))

        # -- the CLI client: the key comes from the environment, never argv ----------
        d = free_port()
        url = "http://127.0.0.1:%d" % manager.port

        def cli(*args, env=None):
            run = subprocess.run([REX, "manager", "--root", root,
                                  args[0], "--url", url, *args[1:]], capture_output=True, text=True,
                                 env=dict(os.environ, **(env or {})), timeout=120)
            return run.returncode, run.stdout + run.stderr
        code, text = cli("create", "cli", "--port", str(d), "--catalog", CATALOG, "--gdm", "off",
                         "--assert-claimed", env={"REX_DIRECT_KEY": "cli-secret", "PLEX_TOKEN": "cli-plex"})
        check("cli create takes REX_DIRECT_KEY from the environment",
              code == 0 and '"rex_direct_key": "***"' in text and "cli-secret" not in text, text)
        check("cli create no longer takes PLEX_TOKEN (rex reads none since 2026-10-19)",
              code == 0 and "plex_token" not in text and "cli-plex" not in text, text)
        code, text = cli("start", "cli")
        check("cli start", code == 0 and wait_for(lambda: answers(d), 90), text)
        code, text = cli("list")
        check("cli list shows it", code == 0 and "cli" in text and str(d) in text, text)
        code, text = cli("status", "cli")
        check("cli status is the redacted view", code == 0 and "cli-secret" not in text
              and '"status"' in text, text)
        code, text = cli("logs", "cli", "--lines", "20")
        check("cli logs", code == 0 and "manager: starting cli" in text, text[-300:])
        code, text = cli("stop", "cli")
        check("cli stop", code == 0 and wait_for(lambda: not answers(d), 30), text)
        code, text = cli("delete", "cli", "--purge")
        check("cli delete --purge", code == 0 and not os.path.exists(os.path.join(root, "servers", "cli")), text)
        code, text = cli("status", "cli")
        check("cli reports a missing server", code != 0 and "404" in text, text)
        manager.stop()
    finally:
        if manager.proc and manager.proc.poll() is None:
            manager.stop(signal.SIGKILL)
        failed = results.count(False)
        if failed:
            print(manager.output()[-3000:])
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (len(results) - results.count(False), len(results)))
    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())
