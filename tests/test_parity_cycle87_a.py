#!/usr/bin/env python3
"""Cycle 87, territory A (localisation): the surfaces the cycle's music-library and Add
Library commits added, in the request's language.

`tests/fixtures/c87_a_music_i18n_s42.json` was read off plexscratch42 (1.42.2):

- six refusing Add Library forms that between them raise all eleven refusal lines, in
  every language Plex Web sends and `hu-HU`: three of the lines are translated.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle87_a.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c87_a_music_i18n_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("server exited at start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process, port
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def ask(port, method, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def main():
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        base = dict(fixture["request_headers"], **{"X-Plex-Client-Identifier": "parity-c87-a"})
        # -- Add Library's refusal lines -----------------------------------------
        wrong = []
        for exchange in fixture["refusals"]:
            headers = dict(base, **exchange["headers"])
            status, body = ask(port, "POST", exchange["path"], headers)
            if (status, body) != (exchange["status"], exchange["body"]):
                wrong.append((exchange["headers"], exchange["label"], body, exchange["body"]))
        check("%d refusals read line for line as measured" % len(fixture["refusals"]),
              not wrong, wrong[:4])
    finally:
        process.terminate()
        process.wait(timeout=10)
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
