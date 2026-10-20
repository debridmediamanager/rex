#!/usr/bin/env python3
"""Unchanged uploaded artwork must not dominate a playback progress write.

The 2026-10-20 zen measurement of 441dbe22: an 8 MiB upload raised median timeline
writes from 2.74 ms to 72.23 ms. This interleaves warm writes on otherwise equal
servers with and without retained artwork; the generous 6x bound distinguishes
serialization of every byte from filesystem/scheduler noise. Persisted bytes,
selection and progress must survive a hard kill, then a second upload and kill.
The PNG prefix is the actual plexscratch42 upload used by the collection corpus;
trailing bytes scale the retained payload without introducing image decoding.
"""
import hashlib
import http.client
import json
import os
import shutil
import signal
import statistics
import sys
import tempfile
import time
from pathlib import Path

from test_parity_cycle88_b import POSTER_PNG
from test_persist import HEADERS, Instance
ROOT = Path(__file__).resolve().parent.parent
KEY = "1007"
SAMPLES = 40
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def request(server, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=60)
    try:
        start = time.perf_counter()
        conn.request(method, path, body=body, headers=dict(HEADERS, Connection="close"))
        response = conn.getresponse()
        data = response.read()
        return response.status, data, time.perf_counter() - start
    finally:
        conn.close()


def beat(server, offset):
    path = "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&state=playing&time=%d&duration=2400000" % (KEY, KEY, offset)
    status, _, elapsed = request(server, "GET", path)
    if status != 200:
        raise AssertionError("timeline answered %d" % status)
    return elapsed


def row(server):
    status, data, _ = request(server, "GET", "/library/metadata/" + KEY)
    if status != 200:
        raise AssertionError("metadata answered %d" % status)
    return json.loads(data)["MediaContainer"]["Metadata"][0]


def upload(server, payload):
    status, _, _ = request(server, "POST", "/library/metadata/%s/posters" % KEY, payload)
    if status != 200:
        raise AssertionError("poster upload answered %d" % status)
    status, raw, _ = request(server, "GET", "/library/metadata/%s/posters" % KEY)
    photos = json.loads(raw)["MediaContainer"].get("Metadata", []) if status == 200 else []
    digest = hashlib.sha1(payload).hexdigest()
    candidate = next(photo for photo in photos if str(photo.get("ratingKey", "")).endswith("/" + digest))
    check("the new poster is selected", candidate.get("selected") is True, candidate)
    return candidate["key"]


def restored(server, candidates, offset):
    check("progress answered before the hard kill survives", row(server).get("viewOffset") == offset)
    for path, payload in candidates:
        status, data, _ = request(server, "GET", path)
        check("uploaded poster bytes survive ordinary saves and a hard kill",
              status == 200 and hashlib.sha256(data).digest() == hashlib.sha256(payload).digest(), len(payload))


def main():
    os.environ.setdefault("REX_PLEXTV_URL", "http://127.0.0.1:9")
    directory = Path(tempfile.mkdtemp(prefix="rex-artwork-save-"))
    server = None
    try:
        catalog = directory / "catalog.json"
        shutil.copy(ROOT / "tests/catalog_ui.json", catalog)
        state = directory / "rex.db"
        server = Instance(str(catalog), state=str(state), wait=60)
        # Interleave empty and loaded-server samples to avoid treating a changing host
        # load as serialization cost. Each instance uses its own catalog and state file.
        other = directory / "empty"
        other.mkdir()
        empty_catalog = other / "catalog.json"
        shutil.copy(catalog, empty_catalog)
        empty = Instance(str(empty_catalog), state=str(other / "rex.db"), wait=60)
        try:
            payload = POSTER_PNG + b"artwork-save-size-probe" * ((8 * 1024 * 1024 - len(POSTER_PNG)) // 23 + 1)
            payload = payload[:8 * 1024 * 1024]
            candidate = upload(server, payload)
            for n in range(6):
                beat(empty, 100000 + n * 1000)
                beat(server, 100000 + n * 1000)
            baseline, loaded = [], []
            for n in range(SAMPLES):
                offset = 200000 + n * 1000
                if n % 2:
                    loaded.append(beat(server, offset))
                    baseline.append(beat(empty, offset))
                else:
                    baseline.append(beat(empty, offset))
                    loaded.append(beat(server, offset))
            a, b = statistics.median(baseline), statistics.median(loaded)
            check("an unchanged 8 MiB poster does not dominate a progress write", b < a * 6,
                  "empty=%.3fms uploaded=%.3fms ratio=%.2f" % (a * 1000, b * 1000, b / a))
        finally:
            empty.stop()
        server.stop(signal.SIGKILL)
        server = Instance(str(catalog), state=str(state), wait=60)
        restored(server, [(candidate, payload)], offset)
        replacement = POSTER_PNG + b"replacement-upload"
        second = upload(server, replacement)
        beat(server, 765000)
        server.stop(signal.SIGKILL)
        server = Instance(str(catalog), state=str(state), wait=60)
        restored(server, [(candidate, payload), (second, replacement)], 765000)
        status, raw, _ = request(server, "GET", "/library/metadata/%s/posters" % KEY)
        photos = json.loads(raw)["MediaContainer"].get("Metadata", []) if status == 200 else []
        chosen = [photo.get("key") for photo in photos if photo.get("selected")]
        check("the later upload stays selected after restart", chosen == [second], chosen)
    finally:
        if server is not None and server.proc.poll() is None:
            server.stop()
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d artwork-save checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
