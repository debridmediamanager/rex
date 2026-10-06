#!/usr/bin/env python3
"""manager.py: no server runs until one is started, and the manager keeps them running.

Drives a real manager process on a temporary root, managing real plexsim processes on
`tests/catalog_ui.json`: the native binary by default, the Python oracle with
`PLEXSIM_SERVER=python` (through a two-line wrapper, since the manager takes one
executable), or any executable `PLEXSIM_SERVER` names.

    python3 tests/test_manager.py
"""
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]),
          flush=True)


def free_port():
    # Below 32768: zen's ephemeral range starts there, and an outgoing connection can
    # take a port above it between this probe and the bind.
    for _ in range(200):
        port = random.randint(20000, 32000)
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise SystemExit("no free port")


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def wait_for(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
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
    configured = os.environ.get("PLEXSIM_SERVER")
    if configured and configured.strip() != "python":
        return configured
    if configured is None:
        native = os.path.join(ROOT, "rust", "target", "release", "plexsim")
        if os.path.exists(native):
            return native
    wrapper = os.path.join(work, "plexsim-python")
    with open(wrapper, "w") as handle:
        handle.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(ROOT, "server.py")))
    os.chmod(wrapper, 0o755)
    return wrapper


class Manager:
    def __init__(self, root, binary):
        self.root, self.binary = root, binary
        self.port = free_port()
        self.proc = None

    def start(self):
        self.log = open(os.path.join(self.root + ".log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "manager.py"), "--root", self.root, "serve",
             "--listen", "127.0.0.1:%d" % self.port, "--binary", self.binary],
            cwd=ROOT, stdout=self.log, stderr=self.log, start_new_session=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
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
    return dict({"name": name, "port": port, "catalog": CATALOG, "rd_token": "dummy-rd",
                 "gdm": "off", "assert_claimed": True}, **extra)


def main():
    work = tempfile.mkdtemp(prefix="plexsim-manager-")
    root = os.path.join(work, "root")
    binary = server_binary(work)
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
        check("the page loads without a token", page[0] == 200 and b"plexsim manager" in page[1])

        a, b, c = free_port(), free_port(), free_port()
        status, made = manager.api("POST", "/servers", config("alpha", a, token="gate-secret",
                                                               tmdb_key="tmdb-secret",
                                                               friendly_name="Managed Alpha"))
        check("create answers 201 and leaves the server stopped",
              status == 201 and made.get("status") == "stopped" and made.get("desired") == "stopped",
              made)
        check("a created server does not listen", not answers(a))
        cfg = (made or {}).get("config") or {}
        check("keys are redacted", cfg.get("rd_token") == "***" and cfg.get("token") == "***"
              and cfg.get("tmdb_key") == "***", cfg)
        listed_text = json.dumps(manager.api("GET", "/servers")[1])
        check("no key appears in the listing", "dummy-rd" not in listed_text
              and "gate-secret" not in listed_text and "tmdb-secret" not in listed_text)
        check("the catalog is copied into the server's database",
              os.path.exists(os.path.join(root, "servers", "alpha", "plexsim.db")))
        for label, body, want in (
                ("a duplicate name is a 409", config("alpha", c), 409),
                ("a duplicate port is a 409", config("other", a), 409),
                ("a bad name is a 400", config("Bad Name", c), 400),
                ("a missing catalog is a 400", config("gamma", c, catalog="/nonexistent.json"), 400),
                ("a missing key is a 400", {"name": "gamma", "port": c, "catalog": CATALOG}, 400),
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

        manager.api("POST", "/servers", config("idle", c))
        status, job = manager.api("POST", "/servers/idle/build")
        done = wait_for(lambda: (lambda v: v["status"] in ("succeeded", "failed") and v)(
            manager.api("GET", "/servers/idle/build")[1]), 120)
        # build_catalog.py --library with a dummy key cannot succeed; what is held here
        # is the job: it runs, ends, and its log is kept.
        check("a build runs as a job with its own status and log",
              status == 202 and done and done["log"] and done["exit"] is not None, done)
        check("a PATCH while stopped changes the configuration",
              manager.api("PATCH", "/servers/idle", {"part_mode": "redirect"})[1]["config"]["part_mode"]
              == "redirect")

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
            run = subprocess.run([sys.executable, os.path.join(ROOT, "manager.py"), "--root", root,
                                  args[0], "--url", url, *args[1:]], capture_output=True, text=True,
                                 env=dict(os.environ, **(env or {})), timeout=120)
            return run.returncode, run.stdout + run.stderr
        code, text = cli("create", "cli", "--port", str(d), "--catalog", CATALOG, "--gdm", "off",
                         "--assert-claimed", env={"RD_TOKEN": "cli-secret"})
        check("cli create takes RD_TOKEN from the environment",
              code == 0 and '"rd_token": "***"' in text and "cli-secret" not in text, text)
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
