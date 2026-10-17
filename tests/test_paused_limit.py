#!/usr/bin/env python3
"""A session paused past `MinutesAllowedPaused` is ended; one that plays is not.

The setting is minutes, so this waits a little over one: it sets the limit to 1, keeps
one player paused and one playing, beating every 10 s as Plex Web does, and expects the
paused one's timeline to carry the terminate (`terminationCode` 2006, as a kick from
`/status/sessions/terminate` is delivered). Before the fix rex never read the setting
and a paused client held its session indefinitely (one sat 22 h, 2026-10-14).

    python3 tests/test_paused_limit.py
"""
import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command
from test_ports import free_port
from test_session_reaper import check, request, results

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def beat(port, client, key, state):
    status, body = request(port, "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                           "&state=%s&time=60000&duration=600000" % (key, key, state), client)
    return status, (body or {}).get("MediaContainer") or {}


def main():
    port = int(os.environ.get("REX_TEST_PORT") or free_port())
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name in ("REX_STATE", "REX_TOKEN"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        status = request_put(port, "/:/prefs?MinutesAllowedPaused=1")
        check("MinutesAllowedPaused=1 is stored", status == 200, status)
        paused, playing = "pause-limit-paused", "pause-limit-playing"
        beat(port, paused, "1007", "playing")
        beat(port, playing, "1008", "playing")
        started = time.time()
        early = None
        paused_answer = playing_answer = {}
        while time.time() - started < 75:
            _, paused_answer = beat(port, paused, "1007", "paused")
            _, playing_answer = beat(port, playing, "1008", "playing")
            if early is None and "terminationCode" in paused_answer and time.time() - started < 55:
                early = time.time() - started
            if "terminationCode" in paused_answer:
                break
            time.sleep(10)
        check("a paused session is not ended before its minute", early is None, "ended at +%s s" % early)
        check("a session paused past MinutesAllowedPaused is ended through its timeline",
              paused_answer.get("terminationCode") == 2006 and paused_answer.get("terminationText"),
              paused_answer)
        check("a playing session beside it is not", "terminationCode" not in playing_answer, playing_answer)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


def request_put(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("PUT", path, headers={"X-Plex-Client-Identifier": "pause-limit-admin"})
        response = conn.getresponse()
        response.read()
    finally:
        conn.close()
    return response.status


if __name__ == "__main__":
    sys.exit(main())
