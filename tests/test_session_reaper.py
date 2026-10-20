#!/usr/bin/env python3
"""Now Playing forgets a silent player on the reference's schedule, and only then.

Cycles 26 and 41 measured it (`rust/src/runtime/sessions.rs`): a process-wide reaper ticks every 30
seconds and drops each session whose last beat is older than the tick minus 180 s, so a
player that stops beating stays listed for 180-210 s. A server with no reaper lists it
until the process ends; one with a per-row timer drops it at exactly 180 s.

The ticks are counted from the server's start. A player that falls silent halfway
between two ticks is therefore still listed past 180 s, until the next tick, where a
per-row timer drops it at 180 s exactly.

Drives a real server process (`tests/server_command.py`). It takes about 4 minutes:
the rule is about wall-clock minutes and is measured on them.

    python3 tests/test_session_reaper.py
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, path, client):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={
            "Accept": "application/json", "X-Plex-Client-Identifier": client,
            "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
            "X-Plex-Device-Name": client})
        response = conn.getresponse()
        body = response.read()
    finally:
        conn.close()
    return response.status, json.loads(body) if body else None


def beat(port, client, key):
    return request(port, "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                   "&state=playing&time=60000&duration=600000" % (key, key), client)[0]


def listed(port):
    status, body = request(port, "/status/sessions", "reaper-observer")
    rows = ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
    return {((row.get("Player") or {}).get("machineIdentifier")) for row in rows}


def session_key(port, client):
    status, body = request(port, "/status/sessions", "reaper-observer")
    for row in ((body or {}).get("MediaContainer") or {}).get("Metadata") or []:
        if (row.get("Player") or {}).get("machineIdentifier") == client:
            return row.get("sessionKey")
    return None


def main():
    port = int(os.environ.get("REX_TEST_PORT") or free_port())
    quiet_port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name in ("REX_STATE", "REX_TOKEN"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    launched = time.time()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    # A second server nobody reads Now Playing on while its player is silent: the reaper ran
    # only when `/status/sessions` was read, so the player's next play, minutes later, was the
    # old session going on (its sessionKey, no media.play) where the tick had ended it.
    quiet = subprocess.Popen(
        server_command("--port", quiet_port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", quiet_port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        beat(quiet_port, "reaper-quiet", "1007")
        quiet_key = session_key(quiet_port, "reaper-quiet")
        quiet_beat = time.time()
        silent, steady = "reaper-silent", "reaper-steady"
        check("both players' playing beats are accepted",
              beat(port, silent, "1007") == 200 and beat(port, steady, "1008") == 200)
        last_beat = time.time()
        check("both players are in Now Playing", listed(port) >= {silent, steady}, listed(port))

        # Half a tick after the server's start: its 180 s end mid-interval, at about
        # start + 195 s, and the tick that removes it is start + 210 s.
        midway = "reaper-midway"
        time.sleep(max(0.0, launched + 15 - time.time()))
        check("a player beating half a tick after the start is accepted",
              beat(port, midway, "1009") == 200)
        midway_beat = time.time()

        gone_at = midway_gone_at = None
        next_steady = time.time() + 20
        while time.time() - midway_beat < 216 and (gone_at is None or midway_gone_at is None):
            if time.time() >= next_steady:
                beat(port, steady, "1008")
                next_steady += 20
            rows = listed(port)
            age = time.time() - last_beat
            if gone_at is None and silent not in rows:
                gone_at = age
            if midway_gone_at is None and midway not in rows:
                midway_gone_at = time.time() - midway_beat
            time.sleep(1.0 if age > 170 else 5.0)
        check("a silent player is still listed for 180 s after its last beat",
              gone_at is None or gone_at >= 180, "gone at +%.1f s" % (gone_at or 0))
        check("and is gone by the next 30-second tick, within 210 s",
              gone_at is not None and gone_at <= 212, "gone at %s" % (
                  "+%.1f s" % gone_at if gone_at is not None else "never"))
        check("a player silent from mid-tick outlives its 180 s until the tick, not a per-row timer",
              midway_gone_at is not None and 190 <= midway_gone_at <= 212, "gone at %s" % (
                  "+%.1f s" % midway_gone_at if midway_gone_at is not None else "never"))
        check("a player that keeps beating is never reaped", steady in listed(port), listed(port))
        time.sleep(max(0.0, quiet_beat + 215 - time.time()))
        beat(quiet_port, "reaper-quiet", "1008")
        again = session_key(quiet_port, "reaper-quiet")
        check("a player back after the tick ended it plays a new session, Now Playing read or not",
              quiet_key and again and again != quiet_key, (quiet_key, again))
    finally:
        quiet.send_signal(signal.SIGTERM)
        quiet.wait(timeout=10)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
