#!/usr/bin/env python3
"""No token reaches rex's log, at INFO or under --verbose, black-box (Fizzy card 850).

The log is what people hand over for support: stderr (a service's journal, the manager's
log file, the Android app's Logs page) and Download Logs (`/diagnostics/logs`). The request
line was masked, but every other line that formats a url was not: the artwork fetch
(`ARTWORK fetch ...?X-Plex-Token=`, 110 characters of the url), a poster picked by url
(`ARTWORK 1007 posters -> ...`, 80), the failed fetch under --verbose (80, so a long token
leaked in part) and the upstream errors that quote the whole url. Each trigger below is a
real request a client sends; the token rides where clients put it: `X-Plex-Token=` and
`token=`, plain, once and twice percent-encoded inside `url=`, and the `X-Plex-Token` header.
The lines stay useful: their paths are kept, only the token is masked.

    python3 tests/test_log_redaction.py
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
# Long enough that the 80-character cuts land inside it: a part of it is a leak too.
TOKEN = "REPLICATE850TOKEN" + "abcdefghijklmnopqrstuvwxyz0123456789"
LEAK = "REPLICATE850"
MASK = "xxxxxxxxxxxxxxxxxxxx"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- %s" % (str(detail)[:900],)))


def quote(text):
    return urllib.parse.quote(text, safe="")


def request(port, method, path, body=None, headers=None, token_in_query=True):
    if token_in_query:
        path += ("&" if "?" in path else "?") + "X-Plex-Token=" + TOKEN
    wanted = {"X-Plex-Client-Identifier": "redaction-check", "X-Plex-Product": "Plex Web",
              "Accept": "application/json"}
    if not token_in_query:
        wanted["X-Plex-Token"] = TOKEN
    wanted.update(headers or {})
    call = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method, data=body,
                                  headers=wanted)
    try:
        with urllib.request.urlopen(call, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def triggers(port):
    """The requests that wrote a token into the log on 588d736d."""
    thumb = "http://127.0.0.1:9/library/metadata/1007/thumb/1?X-Plex-Token=" + TOKEN
    # Remote artwork through the photo transcoder, as Plex Web asks for its own server's art.
    request(port, "GET", "/photo/:/transcode?width=240&height=360&minSize=1&upscale=1&url=" + quote(thumb))
    request(port, "GET", "/photo/:/transcode?width=240&height=360&url="
            + quote("http://127.0.0.1:9/poster.jpg?token=" + TOKEN), token_in_query=False)
    # A url naming another transcode, its own url= encoded inside: the token is %3D-joined there.
    nested = "http://127.0.0.1:9/photo/:/transcode?width=10&url=" + quote(thumb)
    request(port, "GET", "/photo/:/transcode?width=240&height=360&url=" + quote(nested))
    # An upstream error that quotes the whole url (`unsupported url "..."`).
    request(port, "GET", "/photo/:/transcode?width=240&height=360&url="
            + quote("ftp://example.invalid/poster.jpg?X-Plex-Token=" + TOKEN))
    # A poster POSTed by url, and one picked by url, then read back through the artwork path.
    request(port, "POST", "/library/metadata/1007/posters?url="
            + quote("http://127.0.0.1:9/posters/new.jpg?X-Plex-Token=" + TOKEN))
    request(port, "PUT", "/library/metadata/1007/poster?url="
            + quote("https://127.0.0.1:9/poster.jpg?X-Plex-Token=" + TOKEN))
    item = json.loads(request(port, "GET", "/library/metadata/1007")[1] or b"{}")
    thumb_path = ((item.get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("thumb")
    if thumb_path:
        request(port, "GET", thumb_path)
    request(port, "GET", "/photo/:/transcode?width=240&height=360&url=" + quote(thumb_path or "/"))
    # A play queue whose uri carries the token.
    machine = (json.loads(request(port, "GET", "/identity")[1] or b"{}").get("MediaContainer") or {}) \
        .get("machineIdentifier", "")
    request(port, "POST", "/playQueues?type=video&shuffle=0&uri=" + quote(
        "server://%s/com.plexapp.plugins.library/library/metadata/1007?X-Plex-Token=%s" % (machine, TOKEN)))
    # A client's own log message quoting a url it fetched, and a header it sent.
    request(port, "POST", "/log?level=3&source=web&message="
            + quote("GET http://10.0.0.5:32400/library?X-Plex-Token=%s failed" % TOKEN))
    request(port, "POST", "/log?level=3&source=web&message="
            + quote("headers X-Plex-Token: %s Authorization: Bearer %s" % (TOKEN, TOKEN)))
    # An unrouted request (unrouted.log) whose path carries the token.
    request(port, "GET", "/nothing/" + quote("x?X-Plex-Token=" + TOKEN) + "/here")


def run(verbose):
    label = "--verbose" if verbose else "INFO"
    port = int(os.environ.get("REX_TEST_PORT") or free_port())
    stderr = tempfile.TemporaryFile()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
    for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
        env.pop(name, None)
    args = ["--port", port, "--catalog", CATALOG, "--gdm", "off", "--token", TOKEN]
    if verbose:
        args.append("--verbose")
    proc = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=env,
                            stdout=stderr, stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            raise SystemExit("rex did not start")
        triggers(port)
        status, archive = request(port, "GET", "/diagnostics/logs")
        files = {}
        if status == 200:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                files = {name: zipped.read(name).decode("utf-8", "replace") for name in zipped.namelist()}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    stderr.seek(0)
    console = stderr.read().decode("utf-8", "replace")
    check("%s: Download Logs answers its three files" % label,
          sorted(files) == ["Plex Media Server.log", "requests.log", "unrouted.log"], (status, sorted(files)))
    for name, text in sorted(files.items()) + [("stderr", console)]:
        leaks = [line for line in text.splitlines() if LEAK in line]
        check("%s: no token in %s" % (label, name), not leaks, leaks[:6])
    log = files.get("Plex Media Server.log", "")
    kept = [
        "ARTWORK fetch http://127.0.0.1:9/library/metadata/1007/thumb/1?X-Plex-Token=" + MASK,
        "ARTWORK fetch http://127.0.0.1:9/poster.jpg?token=" + MASK,
        "ARTWORK 1007 posters -> https://127.0.0.1:9/poster.jpg?X-Plex-Token=" + MASK,
        "CLIENT LOG [3] web: GET http://10.0.0.5:32400/library?X-Plex-Token=%s failed" % MASK,
        "UNKNOWN GET /nothing/x?X-Plex-Token=%s/here" % MASK,
    ]
    if verbose:
        kept.append("artwork fetch failed for http://127.0.0.1:9/library/metadata/1007/thumb/1?X-Plex-Token=" + MASK)
    for line in kept:
        check("%s: the line keeps its path, the token masked: %s" % (label, line[:60]), line in log,
              [row for row in log.splitlines() if any(word in row for word in ("ARTWORK", "artwork", "CLIENT", "UNKNOWN"))][:12])
    check("%s: the request line keeps its path, the token masked" % label,
          "/photo/:/transcode" in files.get("requests.log", "") and MASK in files.get("requests.log", ""),
          files.get("requests.log", "")[:400])


def main():
    run(verbose=False)
    run(verbose=True)
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
