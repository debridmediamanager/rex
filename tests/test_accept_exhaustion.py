#!/usr/bin/env python3
"""A server out of file descriptors keeps its accept loop quiet and serves again after.

rex-direct forwards rex's port from the internet, so its connection count is anyone's. Out of
descriptors, `accept` fails at once on every call; the loop retried it with no pause and held a
core at 100 % for as long as the sockets stayed open (audit 2026-10-20). The server here runs
with 64 descriptors and is held at 120 idle connections for three seconds.

    python3 tests/test_accept_exhaustion.py
"""
import os
import resource
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_command import server_command  # noqa: E402
from startup_wait import poll_until  # noqa: E402
from test_ports import free_port  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:400]))


def cpu_seconds(pid):
    text = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    parts = [float(part) for part in text.replace("-", ":").split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def identity(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=5) as response:
            return response.status
    except Exception:
        return 0


def main():
    port = free_port()
    limit = lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join("tests", "catalog_ui.json"), "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, preexec_fn=limit, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9"))
    held = []
    try:
        poll_until(lambda: identity(port) == 200, 60)
        for _ in range(120):
            try:
                held.append(socket.create_connection(("127.0.0.1", port), timeout=2))
            except OSError:
                break
        time.sleep(0.5)
        before = cpu_seconds(process.pid)
        time.sleep(3)
        spent = cpu_seconds(process.pid) - before
        check("out of descriptors, the accept loop spends under a second of CPU in three", spent < 1.0, spent)
        check("and the server is still running", process.poll() is None, process.poll())
        for sock in held:
            sock.close()
        held = []
        check("and answers once the connections close", poll_until(lambda: identity(port) == 200, 30) is not False,
              identity(port))
    finally:
        for sock in held:
            sock.close()
        process.terminate()
        process.wait(timeout=20)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
