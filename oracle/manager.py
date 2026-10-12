#!/usr/bin/env python3
"""manager.py: start and stop rex servers from stored configurations.

One long-lived process that, by default, runs no rex server at all. A server is
created from a configuration (port, zurg, catalog, options), kept stopped
until it is started, and supervised once it is: an unexpected exit is restarted with a
backoff, and a server that keeps dying is marked failed instead of looping. What was
running when the manager stopped is running again when it starts, and nothing else.

    oracle/manager.py serve [--root managed] [--listen 127.0.0.1:32300] [--binary PATH]
    oracle/manager.py create living-room --port 32400 --catalog catalog.json   # ZURG_* from env
    oracle/manager.py start living-room
    oracle/manager.py list | status NAME | logs NAME | stop NAME | restart NAME | delete NAME
    oracle/manager.py create den --port 32401           # no catalog: seeded, then scanned from zurg
    oracle/manager.py build den --start # build_catalog.py --seed into its database, then start

Everything lives under the root (default `managed/` at the repository root, mode 700):

    manager.db              the servers and their configurations (mode 600: it holds keys)
    admin.token             the API's bearer token, generated on first start (mode 600)
    servers/<name>/         rex.db (the catalog and, for the native server, its state
                            in the same file), server.log, build.log, tmdb_cache.json ...

The API (JSON, `Authorization: Bearer <admin token>` on every route but the page at /):

    GET    /servers                     every server and its status
    POST   /servers                     create (stopped)
    GET    /servers/<name>              one server, keys redacted
    PATCH  /servers/<name>              change its configuration (stopped servers only)
    POST   /servers/<name>/start|stop|restart
    DELETE /servers/<name>[?purge=1]    stop and forget it; purge also removes its files
    GET    /servers/<name>/logs?lines=N
    POST   /servers/<name>/build        run build_catalog.py --seed for it
    GET    /servers/<name>/build        that job's status and log tail

Secrets reach a server through its environment (ZURG_URL, ZURG_USER, ZURG_PASSWORD,
TMDB_KEY, PLEX_TOKEN, REX_TOKEN), never its command line, which `ps` shows.
Stdlib only, like the server path.
"""

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import catalogdb  # noqa: E402

DEFAULT_ROOT = os.path.join(ROOT, "managed")
DEFAULT_LISTEN = "127.0.0.1:32300"
DEFAULT_BINARY = os.path.join(ROOT, "rust", "target", "release", "rex.exe" if os.name == "nt" else "rex")

NAME = re.compile(r"^[a-z0-9-]{1,40}$")
SECRETS = ("zurg_password", "tmdb_key", "plex_token", "token")
# What each environment field is called in the server's environment; the zurg URL and
# user are not secret, but travel the same way.
SECRET_ENV = {"zurg_url": "ZURG_URL", "zurg_user": "ZURG_USER", "zurg_password": "ZURG_PASSWORD",
              "tmdb_key": "TMDB_KEY", "plex_token": "PLEX_TOKEN", "token": "REX_TOKEN"}
STOP_GRACE = 15          # seconds between SIGTERM and SIGKILL; the server saves on SIGTERM
FAILURE_WINDOW = 600     # a server that fails this often within ten minutes ...
FAILURE_LIMIT = 5        # ... is marked failed rather than restarted again
LOG_LIMIT = 10 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers(
    name TEXT PRIMARY KEY, config TEXT NOT NULL, desired TEXT NOT NULL,
    pid INTEGER, status TEXT NOT NULL, last_exit TEXT, last_error TEXT,
    started_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS builds(
    name TEXT PRIMARY KEY, status TEXT NOT NULL, exit INTEGER,
    started_at REAL, ended_at REAL);
"""


class Refused(Exception):
    """A request the manager answers with a 4xx: (status, message)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# -- configuration ------------------------------------------------------------------

def validate(config, partial=False):
    """The configuration as stored, or Refused(400). `partial` checks a PATCH's fields."""
    if not isinstance(config, dict):
        raise Refused(400, "the configuration must be a JSON object")
    known = {"name", "port", "catalog", "zurg_url", "zurg_user", "zurg_password", "zurg_mount",
             "tmdb_key", "plex_token", "token", "friendly_name", "part_mode", "host", "gdm", "announce_url", "public_url",
             "web_root", "assert_claimed", "verbose", "extra_args", "restart"}
    unknown = sorted(set(config) - known)
    if unknown:
        raise Refused(400, "unknown field(s): %s" % ", ".join(unknown))
    out = dict(config)
    # No catalog is needed to create a server: one created from its keys alone gets its
    # catalog from `build` (an empty library at zurg's mount) before it can start.
    required = () if partial else ("name", "port")
    for field in required:
        if out.get(field) in (None, ""):
            raise Refused(400, "%s is required" % field)
    if "name" in out and not NAME.match(str(out["name"])):
        raise Refused(400, "name must be 1-40 characters of a-z, 0-9 and -")
    if "port" in out:
        port = out["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise Refused(400, "port must be an integer from 1024 to 65535")
    if out.get("catalog") is not None:
        catalog = out["catalog"]
        if isinstance(catalog, dict):
            catalog = catalog.get("path")
        if not isinstance(catalog, str) or not os.path.isabs(catalog):
            raise Refused(400, "catalog must be an absolute path to a .json or .db catalog")
        if not os.path.isfile(catalog):
            raise Refused(400, "catalog %s does not exist" % catalog)
        out["catalog"] = catalog
    for field in ("zurg_url", "zurg_user", "zurg_password", "zurg_mount", "tmdb_key",
                  "plex_token", "token", "friendly_name", "announce_url", "public_url",
                  "web_root", "host"):
        if field in out and out[field] is not None and not isinstance(out[field], str):
            raise Refused(400, "%s must be a string" % field)
    choices = {"part_mode": ("proxy",), "gdm": ("auto", "on", "off"),
               "restart": ("no", "on-failure")}
    for field, allowed in choices.items():
        if field in out and out[field] not in allowed:
            raise Refused(400, "%s must be one of %s" % (field, ", ".join(allowed)))
    for field in ("assert_claimed", "verbose"):
        if field in out and not isinstance(out[field], bool):
            raise Refused(400, "%s must be true or false" % field)
    if "extra_args" in out and (not isinstance(out["extra_args"], list)
                                or not all(isinstance(a, str) for a in out["extra_args"])):
        raise Refused(400, "extra_args must be a list of strings")
    if out.get("announce_url") and not (out.get("plex_token") or partial):
        # The server refuses to start with --announce-url and no PLEX_TOKEN.
        raise Refused(400, "announce_url needs plex_token")
    if not partial:
        out.setdefault("host", "0.0.0.0")
        out.setdefault("part_mode", "proxy")
        out.setdefault("gdm", "auto")
        out.setdefault("restart", "on-failure")
        out.setdefault("assert_claimed", False)
        out.setdefault("verbose", False)
        out.setdefault("extra_args", [])
    return out


def redact(config):
    return {key: ("***" if key in SECRETS and value else value) for key, value in config.items()}


def import_catalog(source, target, friendly_name=None):
    """The server's own copy of its catalog, as a database at `target`.

    Written the way `build_catalog.py --convert` writes one (the same `catalogdb.write`):
    a JSON catalog keeps its own digests, so a state saved over that file is still the
    same catalog's, and a database catalog keeps the digests it recorded. The server has
    no friendly-name flag -- the name is the catalog's `friendlyName` -- so a name given
    here is written into the copy, which then is a different catalog (new digests).
    Only the catalog half of an existing `target` is replaced: the state the server
    wrote there stays, and the server sets it aside by digest if the catalog changed.
    """
    os.makedirs(os.path.dirname(target), exist_ok=True)
    digests = None
    raw = None
    if catalogdb.is_sqlite(source):
        try:
            document = catalogdb.read(source)
            digests = [(key, catalogdb.digest(source, key))
                       for key in ("catalogDigest", "catalogDigestSha256")]
        except (sqlite3.Error, ValueError) as error:
            raise Refused(400, "catalog %s is not a catalog database: %s" % (source, error))
    else:
        with open(source, "rb") as handle:
            raw = handle.read()
        try:
            document = json.loads(raw)
        except ValueError as error:
            raise Refused(400, "catalog %s is not JSON: %s" % (source, error))
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise Refused(400, "catalog %s holds no items" % source)
    if friendly_name:
        document["friendlyName"] = friendly_name
        raw = digests = None
    catalogdb.write(target, document, source_bytes=raw)
    if digests:
        db = sqlite3.connect(target)
        try:
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", digests)
            db.commit()
        finally:
            db.close()
    os.chmod(target, 0o600)


def port_free(host, port):
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("" if host in ("0.0.0.0", "") else host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def port_answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def tail(path, lines):
    if not os.path.exists(path):
        return []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 512 * 1024))
        text = handle.read().decode(errors="replace")
    return text.splitlines()[-lines:]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def describe_exit(code):
    if code is None:
        return None
    if code < 0:
        try:
            return "killed by %s" % signal.Signals(-code).name
        except ValueError:
            return "killed by signal %d" % -code
    return "exit %d" % code


# -- the supervisor ---------------------------------------------------------------

class Manager:
    def __init__(self, root, binary):
        self.root = os.path.abspath(root)
        self.binary = binary
        os.makedirs(os.path.join(self.root, "servers"), exist_ok=True)
        os.chmod(self.root, 0o700)
        path = os.path.join(self.root, "manager.db")
        fresh = not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if fresh:
            os.chmod(path, 0o600)
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.procs = {}       # name -> Popen
        self.logs = {}        # name -> open log handle
        self.failures = {}    # name -> [exit times]
        self.due = {}         # name -> when a backoff restart is due
        self.builds = {}      # name -> Popen of a running build
        self.start_after = set()  # builds asked to (re)start their server on success
        self.stopping = False

    # storage
    def row(self, name):
        found = self.db.execute("SELECT name, config, desired, pid, status, last_exit, last_error,"
                                " started_at, created_at, updated_at FROM servers WHERE name=?",
                                (name,)).fetchone()
        if not found:
            raise Refused(404, "no server named %s" % name)
        keys = ("name", "config", "desired", "pid", "status", "last_exit", "last_error",
                "started_at", "created_at", "updated_at")
        row = dict(zip(keys, found))
        row["config"] = json.loads(row["config"])
        return row

    def names(self):
        return [r[0] for r in self.db.execute("SELECT name FROM servers ORDER BY name")]

    def update(self, name, **fields):
        fields["updated_at"] = time.time()
        if "config" in fields:
            fields["config"] = json.dumps(fields["config"])
        sets = ", ".join("%s=?" % key for key in fields)
        self.db.execute("UPDATE servers SET %s WHERE name=?" % sets, (*fields.values(), name))

    def directory(self, name):
        return os.path.join(self.root, "servers", name)

    def catalog_path(self, name):
        return os.path.join(self.directory(name), "rex.db")

    # views
    def view(self, name):
        with self.lock:
            row = self.row(name)
            proc = self.procs.get(name)
            uptime = (time.time() - row["started_at"]) if proc and row["started_at"] else None
            return {
                "name": name, "port": row["config"]["port"], "desired": row["desired"],
                "status": row["status"], "pid": row["pid"] if proc else None,
                "uptime": round(uptime, 1) if uptime is not None else None,
                "last_exit": row["last_exit"], "last_error": row["last_error"],
                "recent_failures": len(self.failures.get(name, [])),
                "directory": self.directory(name), "config": redact(row["config"]),
            }

    def list(self):
        with self.lock:
            return [self.view(name) for name in self.names()]

    # configuration
    def check_port(self, name, port):
        for other in self.names():
            if other != name and self.row(other)["config"]["port"] == port:
                raise Refused(409, "port %d is already %s's" % (port, other))

    def create(self, config):
        config = validate(config)
        name = config["name"]
        with self.lock:
            if name in self.names():
                raise Refused(409, "a server named %s exists" % name)
            self.check_port(name, config["port"])
            if config.get("catalog"):
                import_catalog(config["catalog"], self.catalog_path(name), config.get("friendly_name"))
            else:
                os.makedirs(self.directory(name), exist_ok=True)
            now = time.time()
            self.db.execute("INSERT INTO servers(name, config, desired, status, created_at, updated_at)"
                            " VALUES (?, ?, 'stopped', 'stopped', ?, ?)",
                            (name, json.dumps(config), now, now))
        return self.view(name)

    def patch(self, name, changes):
        changes = validate(changes, partial=True)
        with self.lock:
            row = self.row(name)
            if name in self.procs or row["desired"] == "running":
                raise Refused(409, "stop %s before changing its configuration" % name)
            if "name" in changes and changes["name"] != name:
                raise Refused(400, "a server cannot be renamed")
            config = dict(row["config"], **changes)
            config = validate(config)
            if "port" in changes:
                self.check_port(name, config["port"])
            if config.get("catalog") and ("catalog" in changes or "friendly_name" in changes):
                import_catalog(config["catalog"], self.catalog_path(name), config.get("friendly_name"))
            self.update(name, config=config)
        return self.view(name)

    # processes
    def command(self, config, name):
        argv = [self.binary, "--port", str(config["port"]), "--host", config["host"],
                "--catalog", self.catalog_path(name), "--part-mode", config["part_mode"],
                "--gdm", config["gdm"]]
        for field, flag in (("announce_url", "--announce-url"), ("public_url", "--public-url"),
                            ("web_root", "--web-root"), ("zurg_mount", "--zurg-mount")):
            if config.get(field):
                argv += [flag, config[field]]
        if config.get("assert_claimed"):
            argv.append("--assert-claimed")
        if config.get("verbose"):
            argv.append("--verbose")
        return argv + list(config.get("extra_args") or [])

    def environment(self, config):
        env = {key: value for key, value in os.environ.items()
               if key not in SECRET_ENV.values() and key != "REX_STATE"}
        for field, variable in SECRET_ENV.items():
            if config.get(field):
                env[variable] = config[field]
        return env

    def spawn(self, name):
        """Start the process; the caller holds the lock and has set desired=running."""
        row = self.row(name)
        config = row["config"]
        if not catalogdb.is_sqlite(self.catalog_path(name)):
            raise Refused(409, "%s has no catalog yet: build it first (POST /servers/%s/build)"
                          % (name, name))
        if not port_free(config["host"], config["port"]):
            raise Refused(409, "port %d is in use" % config["port"])
        log_path = os.path.join(self.directory(name), "server.log")
        self.rotate(log_path)
        log = open(log_path, "ab")
        log.write(("--- manager: starting %s at %s\n" % (name, time.strftime("%Y-%m-%d %H:%M:%S")))
                  .encode())
        log.flush()
        try:
            proc = subprocess.Popen(self.command(config, name), cwd=ROOT,
                                    env=self.environment(config), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            log.close()
            self.update(name, status="failed", last_error="cannot start: %s" % error)
            raise Refused(500, "cannot start %s: %s" % (name, error))
        self.procs[name] = proc
        self.logs[name] = log
        self.due.pop(name, None)
        self.update(name, pid=proc.pid, status="starting", started_at=time.time(), last_error=None)

    def start(self, name):
        with self.lock:
            row = self.row(name)
            if name in self.procs:
                return self.view(name)
            self.failures.pop(name, None)
            self.update(name, desired="running")
            try:
                self.spawn(name)
            except Refused:
                self.update(name, desired="stopped",
                            status="failed" if row["status"] == "failed" else "stopped")
                raise
        return self.view(name)

    def terminate(self, name):
        """SIGTERM the server's process group, SIGKILL it after the grace period."""
        proc = self.procs.get(name)
        if proc is None:
            return None
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            code = proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = proc.wait()
        return code

    def stop(self, name, keep_desired=False):
        with self.lock:
            self.row(name)
            if not keep_desired:
                self.update(name, desired="stopped")
            self.due.pop(name, None)
            proc = self.procs.get(name)
        # Waited outside the lock: a server saving on SIGTERM must not freeze the API.
        code = self.terminate(name) if proc else None
        with self.lock:
            if self.procs.get(name) is proc and proc is not None:
                self.procs.pop(name, None)
                log = self.logs.pop(name, None)
                if log:
                    log.close()
                self.update(name, pid=None, status="stopped", last_exit=describe_exit(code))
            elif proc is None:
                row = self.row(name)
                if row["status"] not in ("failed",):
                    self.update(name, status="stopped")
        return self.view(name)

    def restart(self, name):
        self.stop(name)
        return self.start(name)

    def delete(self, name, purge=False):
        self.stop(name)
        with self.lock:
            self.db.execute("DELETE FROM servers WHERE name=?", (name,))
            self.db.execute("DELETE FROM builds WHERE name=?", (name,))
            self.failures.pop(name, None)
        if purge:
            shutil.rmtree(self.directory(name), ignore_errors=True)
        return {"deleted": name, "purged": bool(purge)}

    def logs_of(self, name, lines):
        with self.lock:
            self.row(name)
        return {"name": name, "lines": tail(os.path.join(self.directory(name), "server.log"), lines)}

    @staticmethod
    def rotate(path):
        """Copy-truncate: the server holds the log open in append mode, so truncating
        in place is safe and the previous megabytes stay in `<log>.1`."""
        try:
            if os.path.getsize(path) > LOG_LIMIT:
                shutil.copyfile(path, path + ".1")
                os.truncate(path, 0)
        except OSError:
            pass

    # the monitor
    def reap(self):
        """One pass: exits, restarts that are due, readiness, log size."""
        with self.lock:
            if self.stopping:
                return
            now = time.time()
            for name, proc in list(self.procs.items()):
                code = proc.poll()
                if code is None:
                    row = self.row(name)
                    if row["status"] == "starting" and port_answers(row["config"]["port"]):
                        self.update(name, status="running")
                    self.rotate(os.path.join(self.directory(name), "server.log"))
                    continue
                self.procs.pop(name, None)
                log = self.logs.pop(name, None)
                if log:
                    log.close()
                self.exited(name, code, now)
            for name, when in list(self.due.items()):
                if when <= now:
                    self.due.pop(name, None)
                    try:
                        self.spawn(name)
                    except Refused as error:
                        self.exited(name, None, now, reason=str(error))
            for name, proc in list(self.builds.items()):
                code = proc.poll()
                if code is not None:
                    self.builds.pop(name, None)
                    self.db.execute("UPDATE builds SET status=?, exit=?, ended_at=? WHERE name=?",
                                    ("succeeded" if code == 0 else "failed", code, now, name))
                    if name in self.start_after:
                        self.start_after.discard(name)
                        if code == 0:
                            # A running server reads its catalog at start: restart it.
                            # On its own thread, since stopping waits for the server.
                            action = self.restart if name in self.procs else self.start
                            threading.Thread(target=self.quietly, args=(action, name),
                                             daemon=True).start()

    def exited(self, name, code, now, reason=None):
        """A supervised server stopped without being asked to."""
        try:
            row = self.row(name)
        except Refused:
            return
        last_exit = describe_exit(code) if code is not None else reason
        policy = row["config"].get("restart", "on-failure")
        if row["desired"] != "running":
            self.update(name, pid=None, status="stopped", last_exit=last_exit)
            return
        if code == 0 or policy == "no":
            # A clean exit was the server's own decision; it is not restarted.
            self.update(name, pid=None, desired="stopped",
                        status="exited" if code == 0 else "failed", last_exit=last_exit)
            return
        history = [t for t in self.failures.get(name, []) if now - t < FAILURE_WINDOW] + [now]
        self.failures[name] = history
        if len(history) >= FAILURE_LIMIT:
            self.update(name, pid=None, desired="stopped", status="failed", last_exit=last_exit,
                        last_error="%d failures within %d s; not restarted again" % (
                            len(history), FAILURE_WINDOW))
            return
        delay = min(60, 2 ** (len(history) - 1))
        self.due[name] = now + delay
        self.update(name, pid=None, status="restarting", last_exit=last_exit,
                    last_error="restarting in %d s" % delay)

    @staticmethod
    def quietly(action, name):
        try:
            action(name)
        except Refused as error:
            print("manager: %s after its build: %s" % (name, error), file=sys.stderr, flush=True)

    def monitor(self):
        while not self.stopping:
            try:
                self.reap()
            except Exception as error:  # noqa: BLE001 -- the monitor must outlive one bad pass
                print("manager: monitor pass failed: %s" % error, file=sys.stderr, flush=True)
            time.sleep(0.5)

    # start and stop of the manager itself
    def adopt_orphans(self):
        """A manager killed hard leaves its servers running in their own sessions. They
        cannot be supervised without being our children, so they are stopped -- and
        started again below if they should be running."""
        for name in self.names():
            row = self.row(name)
            pid = row["pid"]
            if not pid:
                continue
            try:
                command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                         capture_output=True, text=True).stdout
            except OSError:
                command = ""
            if self.catalog_path(name) in command:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                deadline = time.time() + STOP_GRACE
                while time.time() < deadline and alive(pid):
                    time.sleep(0.2)
                if alive(pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            self.update(name, pid=None)

    def restore(self):
        self.adopt_orphans()
        restored = []
        with self.lock:
            for name in self.names():
                row = self.row(name)
                if row["desired"] == "running":
                    try:
                        self.spawn(name)
                        restored.append(name)
                    except Refused as error:
                        self.exited(name, None, time.time(), reason=str(error))
                elif row["status"] not in ("failed", "exited"):
                    self.update(name, status="stopped", pid=None)
        return restored

    def shutdown(self):
        """Stop every server but keep what should run, for the next start."""
        with self.lock:
            self.stopping = True
            running = list(self.procs)
        threads = [threading.Thread(target=self.stop, args=(name, True)) for name in running]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for proc in list(self.builds.values()):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # catalog builds
    def build(self, name, start=False):
        """`build_catalog.py --seed` into the server's database, as a job: an empty library
        whose locations are zurg's folders, which the server fills by scanning zurg.
        `start`: once it succeeds, start the server (or restart it)."""
        with self.lock:
            row = self.row(name)
            if name in self.builds:
                raise Refused(409, "a build for %s is already running" % name)
            os.makedirs(self.directory(name), exist_ok=True)
            log = open(os.path.join(self.directory(name), "build.log"), "wb")
            # REX_MANAGER_BUILDER replaces the builder (tests/test_manager.py).
            builder = os.environ.get("REX_MANAGER_BUILDER") or os.path.join(HERE, "build_catalog.py")
            argv = [sys.executable, builder, "--seed", "--out", self.catalog_path(name)]
            for field, flag in (("friendly_name", "--name"), ("zurg_mount", "--zurg-mount")):
                if row["config"].get(field):
                    argv += [flag, row["config"][field]]
            try:
                proc = subprocess.Popen(argv, cwd=ROOT, env=self.environment(row["config"]),
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                log.close()
            self.builds[name] = proc
            if start:
                self.start_after.add(name)
            self.db.execute("INSERT OR REPLACE INTO builds VALUES (?, 'running', NULL, ?, NULL)",
                            (name, time.time()))
        return self.build_status(name)

    def build_status(self, name, lines=40):
        with self.lock:
            self.row(name)
            found = self.db.execute("SELECT status, exit, started_at, ended_at FROM builds"
                                    " WHERE name=?", (name,)).fetchone()
        if not found:
            return {"name": name, "status": "never"}
        status, code, started, ended = found
        return {"name": name, "status": status, "exit": code, "started_at": started,
                "ended_at": ended,
                "log": tail(os.path.join(self.directory(name), "build.log"), lines)}


# -- the API ----------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>rex manager</title>
<style>body{font:14px system-ui,sans-serif;margin:16px;max-width:960px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ccc;padding:4px 6px;text-align:left}input,select{margin:2px}
pre{background:#f4f4f4;padding:8px;max-height:300px;overflow:auto}</style></head><body>
<h1>rex manager</h1>
<p>Admin token: <input id=tok type=password size=40> <button onclick="save()">Use</button></p>
<table><thead><tr><th>name<th>port<th>status<th>desired<th>last exit<th></tr></thead><tbody id=rows></tbody></table>
<h2>New server</h2>
<form id=f onsubmit="create(event)">
<input name=name placeholder="name" required> <input name=port type=number placeholder="port" required>
<input name=catalog placeholder="catalog .json/.db (optional: else seed an empty library)" size=48><br>
<input name=zurg_url placeholder="zurg URL, e.g. http://127.0.0.1:9999">
<input name=zurg_user placeholder="zurg user (optional)">
<input name=zurg_password type=password placeholder="zurg password (optional)">
<input name=zurg_mount placeholder="zurg mount_path (default /mnt/zurg)"><br>
<input name=tmdb_key type=password placeholder="TMDB key (optional)">
<input name=plex_token type=password placeholder="Plex token (optional)">
<input name=token type=password placeholder="server token (optional)"><br>
<input name=friendly_name placeholder="friendly name (optional)">
<select name=part_mode><option>proxy</select>
<select name=gdm><option>auto<option>on<option>off</select>
<label><input name=assert_claimed type=checkbox> assert claimed</label>
<button>Create</button></form>
<pre id=out></pre>
<script>
const out=document.getElementById('out');
function tok(){try{return sessionStorage.getItem('t')||''}catch(e){return ''}}
function save(){try{sessionStorage.setItem('t',document.getElementById('tok').value)}catch(e){};load()}
async function api(method,path,body){const r=await fetch(path,{method,headers:{'Authorization':'Bearer '+tok(),
'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();
if(!r.ok)throw new Error(j.error||r.status);return j}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){try{const list=await api('GET','/servers');document.getElementById('rows').innerHTML=list.map(s=>
`<tr><td>${esc(s.name)}<td>${s.port}<td>${esc(s.status)}<td>${esc(s.desired)}<td>${esc(s.last_exit)}<td>
${['start','stop','restart'].map(a=>`<button onclick="act('${esc(s.name)}','${a}')">${a}</button>`).join(' ')}
<button onclick="build('${esc(s.name)}')">build &amp; start</button> <button onclick="logs('${esc(s.name)}')">logs</button> <button onclick="del('${esc(s.name)}')">delete</button>`).join('')}
catch(e){out.textContent=e.message}}
async function act(n,a){try{await api('POST','/servers/'+n+'/'+a)}catch(e){out.textContent=e.message};load()}
async function build(n){try{await api('POST','/servers/'+n+'/build',{start:true});out.textContent='seeding '+n+'...'}catch(e){out.textContent=e.message}}
async function logs(n){try{out.textContent=(await api('GET','/servers/'+n+'/logs?lines=200')).lines.join('\\n')}catch(e){out.textContent=e.message}}
async function del(n){if(!confirm('Delete '+n+'? Its files are kept.'))return;try{await api('DELETE','/servers/'+n)}catch(e){out.textContent=e.message};load()}
async function create(ev){ev.preventDefault();const d=Object.fromEntries(new FormData(ev.target));
const c={name:d.name,port:+d.port,part_mode:d.part_mode,gdm:d.gdm,assert_claimed:!!d.assert_claimed};
for(const k of ['catalog','zurg_url','zurg_user','zurg_password','zurg_mount','tmdb_key','plex_token','token','friendly_name'])if(d[k])c[k]=d[k];
try{await api('POST','/servers',c);ev.target.reset()}catch(e){out.textContent=e.message};load()}
load();setInterval(load,3000);
</script></body></html>"""


def make_handler(manager, admin_token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rex-manager"

        def log_message(self, fmt, *args):  # the request log goes to stderr, tokenless
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def reply(self, status, payload, content_type="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authorised(self):
            header = self.headers.get("Authorization") or ""
            given = header[7:] if header.startswith("Bearer ") else ""
            return bool(given) and hmac.compare_digest(given.encode(), admin_token.encode())

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                raise Refused(400, "the body is not JSON")

        def route(self, method):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            parts = [p for p in url.path.split("/") if p]
            if method == "GET" and not parts:
                return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
            if not self.authorised():
                return self.reply(401, {"error": "an Authorization: Bearer <admin token> is required"})
            if parts[:1] != ["servers"]:
                raise Refused(404, "no such route")
            if len(parts) == 1:
                if method == "GET":
                    return self.reply(200, manager.list())
                if method == "POST":
                    return self.reply(201, manager.create(self.body()))
                raise Refused(405, "GET or POST")
            name = parts[1]
            if len(parts) == 2:
                if method == "GET":
                    return self.reply(200, manager.view(name))
                if method == "PATCH":
                    return self.reply(200, manager.patch(name, self.body()))
                if method == "DELETE":
                    purge = query.get("purge", ["0"])[0] in ("1", "true")
                    return self.reply(200, manager.delete(name, purge))
                raise Refused(405, "GET, PATCH or DELETE")
            action = parts[2]
            if len(parts) == 3 and method == "POST" and action in ("start", "stop", "restart"):
                return self.reply(200, getattr(manager, action)(name))
            if len(parts) == 3 and action == "logs" and method == "GET":
                lines = int(query.get("lines", ["200"])[0])
                return self.reply(200, manager.logs_of(name, max(1, min(lines, 10000))))
            if len(parts) == 3 and action == "build":
                if method == "POST":
                    return self.reply(202, manager.build(name, bool(self.body().get("start"))))
                if method == "GET":
                    return self.reply(200, manager.build_status(name))
            raise Refused(404, "no such route")

        def handle_method(self, method):
            try:
                self.route(method)
            except Refused as refused:
                self.reply(refused.status, {"error": str(refused)})
            except Exception as error:  # noqa: BLE001
                self.reply(500, {"error": "%s: %s" % (type(error).__name__, error)})

        def do_GET(self):
            self.handle_method("GET")

        def do_POST(self):
            self.handle_method("POST")

        def do_PATCH(self):
            self.handle_method("PATCH")

        def do_DELETE(self):
            self.handle_method("DELETE")

    return Handler


def admin_token_for(root, path=None):
    """The API's token: an explicit file, else `<root>/admin.token`, created on first
    start (mode 600) and printed once so the operator can copy it."""
    path = path or os.path.join(root, "admin.token")
    if os.path.exists(path):
        with open(path) as handle:
            return handle.read().strip(), False
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token, True


def serve(args):
    manager = Manager(args.root, args.binary)
    token, created = admin_token_for(manager.root, args.admin_token_file)
    if created:
        print("manager: admin token (stored in %s): %s" % (
            args.admin_token_file or os.path.join(manager.root, "admin.token"), token), flush=True)
    host, _, port = args.listen.rpartition(":")
    httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(manager, token))
    httpd.daemon_threads = True

    def stop(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    restored = manager.restore()
    print("manager: listening on http://%s, root %s, %d server(s) defined, restored %s" % (
        args.listen, manager.root, len(manager.names()), restored or "none"), flush=True)
    monitor = threading.Thread(target=manager.monitor, name="monitor", daemon=True)
    monitor.start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        manager.shutdown()
        httpd.server_close()
        print("manager: stopped", flush=True)


# -- the CLI client ---------------------------------------------------------------

def client_token(args):
    if os.environ.get("REX_MANAGER_TOKEN"):
        return os.environ["REX_MANAGER_TOKEN"]
    path = args.token_file or os.path.join(args.root, "admin.token")
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        raise SystemExit("no admin token: set REX_MANAGER_TOKEN or --token-file (looked in %s)" % path)


def call(args, method, path, body=None):
    request = urllib.request.Request(
        args.url.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + client_token(args), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error")
        except ValueError:
            message = error.reason
        raise SystemExit("manager: %s %s -> %d: %s" % (method, path, error.code, message))
    except urllib.error.URLError as error:
        raise SystemExit("manager: cannot reach %s: %s" % (args.url, error.reason))


def secret(args, field, default_env):
    """A key from the variable --<field>-env names (default `default_env`), never argv."""
    variable = getattr(args, field + "_env") or default_env
    return os.environ.get(variable) or None


def client(args):
    if args.command == "list":
        for row in call(args, "GET", "/servers"):
            print("%-20s port %-5d %-10s desired %-8s %s" % (
                row["name"], row["port"], row["status"], row["desired"], row["last_exit"] or ""))
        return
    if args.command == "create":
        config = {"name": args.name, "port": args.port,
                  "catalog": os.path.abspath(args.catalog) if args.catalog else None,
                  "part_mode": args.part_mode, "gdm": args.gdm, "host": args.host,
                  "restart": args.restart, "assert_claimed": args.assert_claimed,
                  "verbose": args.verbose, "extra_args": args.extra_arg or []}
        for field, default_env in SECRET_ENV.items():
            value = secret(args, field, default_env)
            if value:
                config[field] = value
        for field in ("friendly_name", "announce_url", "public_url", "web_root", "zurg_mount"):
            if getattr(args, field):
                config[field] = getattr(args, field)
        print(json.dumps(call(args, "POST", "/servers", config), indent=1))
        return
    name = urllib.parse.quote(args.name, safe="")
    if args.command in ("start", "stop", "restart"):
        row = call(args, "POST", "/servers/%s/%s" % (name, args.command))
        print("%s: %s (desired %s)" % (row["name"], row["status"], row["desired"]))
    elif args.command == "status":
        print(json.dumps(call(args, "GET", "/servers/" + name), indent=1))
    elif args.command == "logs":
        print("\n".join(call(args, "GET", "/servers/%s/logs?lines=%d" % (name, args.lines))["lines"]))
    elif args.command == "delete":
        print(json.dumps(call(args, "DELETE", "/servers/%s%s" % (name, "?purge=1" if args.purge else ""))))
    elif args.command == "build":
        if args.status:
            print(json.dumps(call(args, "GET", "/servers/%s/build" % name), indent=1))
        else:
            print(json.dumps(call(args, "POST", "/servers/%s/build" % name,
                                  {"start": bool(args.start)}), indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help="the manager's directory (default %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("serve", help="run the manager")
    run.add_argument("--listen", default=DEFAULT_LISTEN, help="host:port of the API (default %(default)s)")
    run.add_argument("--binary", default=DEFAULT_BINARY, help="the rex executable (default %(default)s)")
    run.add_argument("--admin-token-file", help="read the API token here instead of <root>/admin.token")

    def clientish(p):
        p.add_argument("--url", default="http://" + DEFAULT_LISTEN)
        p.add_argument("--token-file", help="the admin token (default <root>/admin.token; or REX_MANAGER_TOKEN)")

    create = sub.add_parser("create", help="define a server (stopped)")
    clientish(create)
    create.add_argument("name")
    create.add_argument("--port", type=int, required=True)
    create.add_argument("--catalog", help="a .json or .db catalog, copied into the server's directory;"
                                          " without one, `build` seeds an empty library at zurg's mount")
    for field, default_env in SECRET_ENV.items():
        create.add_argument("--%s-env" % field.replace("_", "-"), dest=field + "_env", metavar="VAR",
                            help="environment variable holding it (default %s)" % default_env)
    create.add_argument("--friendly-name")
    create.add_argument("--zurg-mount", help="zurg's mount_path (the server's --zurg-mount)")
    create.add_argument("--part-mode", choices=("proxy",), default="proxy")
    create.add_argument("--gdm", choices=("auto", "on", "off"), default="auto")
    create.add_argument("--host", default="0.0.0.0")
    create.add_argument("--announce-url")
    create.add_argument("--public-url")
    create.add_argument("--web-root")
    create.add_argument("--assert-claimed", action="store_true")
    create.add_argument("--verbose", action="store_true")
    create.add_argument("--restart", choices=("no", "on-failure"), default="on-failure")
    create.add_argument("--extra-arg", action="append", help="appended to the server's command line, verbatim")

    listing = sub.add_parser("list", help="every server")
    clientish(listing)
    for command in ("start", "stop", "restart", "status", "logs", "delete", "build"):
        p = sub.add_parser(command)
        clientish(p)
        p.add_argument("name")
        if command == "logs":
            p.add_argument("--lines", type=int, default=200)
        if command == "delete":
            p.add_argument("--purge", action="store_true", help="also remove its directory")
        if command == "build":
            p.add_argument("--status", action="store_true", help="show the last build instead of starting one")
            p.add_argument("--start", action="store_true",
                           help="start the server (or restart it) once the build succeeds")

    args = parser.parse_args(argv)
    args.root = os.path.abspath(args.root)
    if args.command == "serve":
        serve(args)
    else:
        client(args)


if __name__ == "__main__":
    main()
