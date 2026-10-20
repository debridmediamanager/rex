#!/usr/bin/env python3
"""The bandwidth table stays a day of seconds, and the Dashboard's graph answers at once.

Every response counted its bytes into a per-second entry for its device, kept for ever and
saved with the state: a client served for three days left 60,000 entries, and
`/statistics/bandwidth?timespan=6` summed them with a list search per entry (quadratic: about
two billion comparisons for those three days, under the runtime lock). Seconds older than a day
are now kept per local hour, which every rollup but the per-second one reads the same.

    python3 tests/test_perf_bandwidth.py
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_command import server_command  # noqa: E402
from startup_wait import poll_until  # noqa: E402
from test_ports import free_port  # noqa: E402

SECONDS = 60000
STEP = 4
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:400]))


def get(port, path):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers={"Accept": "application/json"})
    began = time.time()
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read())
    return body.get("MediaContainer", {}), time.time() - began


def start(port, catalog, state):
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--state", state, "--assert-claimed", "--gdm", "off",
                       root=ROOT),
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9"))
    if not poll_until(lambda: _alive(port), 120):
        process.kill()
        raise SystemExit("server did not start")
    return process


def _alive(port):
    try:
        return get(port, "/identity")[0] is not None
    except Exception:
        return False


def main():
    work = tempfile.mkdtemp(prefix="rex-perf-bandwidth-")
    catalog = os.path.join(work, "catalog.json")
    shutil.copy(os.path.join(ROOT, "tests", "catalog_ui.json"), catalog)
    state = os.path.join(work, "rex.db")
    port = free_port()
    process = start(port, catalog, state)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=60)
    now = int(time.time())
    seconds = [[now - SECONDS * STEP + n * STEP, 1000] for n in range(SECONDS)]
    db = sqlite3.connect(state)
    db.execute("INSERT OR REPLACE INTO doc(name, kind, body) VALUES (?, ?, NULL)", ("state:byteBuckets", "array"))
    db.execute("DELETE FROM row WHERE coll = ?", ("state:byteBuckets",))
    db.execute("INSERT INTO row(coll, key, pos, body) VALUES (?, ?, ?, ?)",
               ("state:byteBuckets", "seed.0", 0.0, json.dumps([2, True, seconds])))
    db.commit()
    db.close()
    process = start(port, catalog, state)
    try:
        per_second, took = get(port, "/statistics/bandwidth?timespan=6")
        rows = [row for row in per_second.get("StatisticsBandwidth", []) if row.get("deviceID") == 2]
        check("three days of seconds answer the per-second graph in under a second (%.2fs)" % took, took < 1.0, took)
        check("which lists the last day's seconds alone", 0 < len(rows) <= 86400 // STEP + 1, len(rows))
        daily, took = get(port, "/statistics/bandwidth?timespan=3")
        total = sum(row.get("bytes", 0) for row in daily.get("StatisticsBandwidth", []) if row.get("deviceID") == 2)
        check("and the daily rollup still counts every byte", total == SECONDS * 1000, (total, SECONDS * 1000))
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)
    db = sqlite3.connect(state)
    kept = sum(len(entry[2]) for entry in (json.loads(body) for (body,) in
                                           db.execute("SELECT body FROM row WHERE coll = 'state:byteBuckets'"))
               if entry[0] == 2)
    db.close()
    check("and the saved table holds a day of seconds and an entry per older hour", kept <= 86400 // STEP + 80, kept)
    shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
