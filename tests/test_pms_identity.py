#!/usr/bin/env python3
"""Latest-PMS identity, methods and static-resource corpus, against a real process.

Recordings are from a fresh official PMS 1.43.5 on zen, not rex. Every recorded
resource is compared, including both favicon routes and the build-file resource.
"""
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from server_command import server_command
from test_ports import free_port
from test_framework_identity import verify as verify_framework_identity

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
CAPTURE = json.loads((FIXTURES / "pms_identity_2026_10_22.json").read_text())
RESOURCES = json.loads((FIXTURES / "pms_resources_2026_10_22.json").read_text())
WIRE = json.loads((FIXTURES / "pms_wire_2026_10_22.json").read_text())
TOKEN = "pms-identity-regression"


def request(port, method, path, token=TOKEN, extra=None, ordered=False, framing=False):
    headers = dict(CAPTURE["request_headers"])
    if token:
        headers["X-Plex-Token"] = token
    headers.update(extra or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        pairs = response.getheaders()
        read_error = None
        try:
            raw = response.read()
        except http.client.IncompleteRead as error:
            if not framing:
                raise
            raw = error.partial
            read_error = {"kind": "IncompleteRead", "received": len(raw), "missing": error.expected}
        result = (response.status, pairs if ordered else dict((k.lower(), v) for k, v in pairs), raw)
        return (*result, read_error) if framing else result
    finally:
        connection.close()


def main():
    failed = []
    checked = 0

    def check(label, condition, detail=""):
        nonlocal checked
        checked += 1
        print(("PASS " if condition else "FAIL ") + label + ("" if condition else " -- " + str(detail)[:240]))
        if not condition:
            failed.append(label)

    with tempfile.TemporaryDirectory(prefix="rex-pms-identity-") as work:
        catalog = Path(work) / "catalog.json"
        shutil.copyfile(ROOT / "tests" / "catalog_ui.json", catalog)
        web_root = Path(work) / "webclient"
        web_root.mkdir()
        for row in WIRE["rows"]:
            if row.get("web_asset"):
                destination = web_root / row["path"][len("/web/"):]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(base64.b64decode(row["pms"]["body_base64"]))
        port = free_port()
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9", REX_DIRECT_URL="off")
        for name in ("REX_TOKEN", "REX_STATE"):
            env.pop(name, None)
        env["REX_TRANSCODER"] = "off"
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(server_command("--catalog", str(catalog), "--port", port,
                                                       "--token", TOKEN, "--gdm", "off",
                                                       "--web-root", str(web_root), root=str(ROOT)),
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError("server exited during startup")
                    try:
                        if request(port, "GET", "/identity")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("server did not listen")

                for row in CAPTURE["rows"]:
                    method, path = row["method"], row["path"]
                    if method == "GET" and path in ("/identity", "/", "/media/providers"):
                        status, headers, raw = request(port, method, path)
                        got = json.loads(raw)["MediaContainer"]
                        expected = json.loads(row["body"])["MediaContainer"]
                        check(path + " advertises the current PMS and API version",
                              status == 200 and all(got.get(k) == expected[k] for k in ("version", "apiVersion")),
                              {k: got.get(k) for k in ("version", "apiVersion")})
                        if path != "/identity":
                            check(path + " exposes the current stream-display capability vocabulary",
                                  got.get("streamDisplayTitleIncludesFormat") == expected["streamDisplayTitleIncludesFormat"]
                                  and ("streamingBrainABRVersion" in got) == ("streamingBrainABRVersion" in expected),
                                  {k: got.get(k) for k in ("streamDisplayTitleIncludesFormat", "streamingBrainABRVersion")})
                    elif (path in ("/identity", "/system", "/does-not-exist/leaf")
                          and method in ("PATCH", "FROB", "OPTIONS")):
                        status, headers, raw = request(port, method, path)
                        expected_headers = {k.lower(): v for k, v in row["headers"]}
                        check(method + " " + path + " matches PMS routing and refusal bytes",
                              status == row["status"] and raw == row["body"].encode()
                              and all(headers.get(k) == expected_headers.get(k)
                                      for k in ("content-type", "x-plex-protocol", "connection")),
                              (status, raw[:120]))
                    elif path == "/favicon.ico":
                        status, headers, raw = request(port, method, path, token=None)
                        check("the public favicon is the PMS recording, not a branded replacement",
                              status == row["status"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
                              (status, len(raw), hashlib.sha256(raw).hexdigest()))
                    elif path == "/diagnostics/logs":
                        status, headers, raw = request(port, method, path)
                        names = zipfile.ZipFile(io.BytesIO(raw)).namelist() if status == 200 else []
                        check("Download Logs uses the PMS archive and main-log names",
                              status == 200 and "Plex Media Server.log" in names
                              and re.fullmatch(r'attachment; filename="Plex Media Server Logs_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.zip"',
                                               headers.get("content-disposition", "")) is not None
                              and not any("rex" in name.lower() for name in names),
                              (headers.get("content-disposition"), names))
                    elif path == "/updater/status":
                        status, _, raw = request(port, method, path)
                        check("the disabled updater does not invent a check on every read",
                              status == row["status"] and json.loads(raw) == json.loads(row["body"]),
                              raw)

                for row in WIRE["rows"]:
                    status, headers, raw, read_error = request(port, row["method"], row["path"],
                        extra=dict(WIRE["request_defaults"], **row["request_headers"]), ordered=True, framing=True)
                    expected = row["pms"]
                    got_headers = [(k, v) for k, v in headers if k.lower() != "date"]
                    expected_headers = [(k, v) for k, v in expected["headers"] if k.lower() != "date"]
                    if row["path"] in ("/web", "/web/", "/manage"):
                        def instance_location(pairs):
                            return [(k, v.rsplit("/", 1)[0] + "/INSTANCE"
                                     if k.lower() == "location" and "#!/setup/" in v else v)
                                    for k, v in pairs]
                        got_headers = instance_location(got_headers)
                        expected_headers = instance_location(expected_headers)
                    check(row["method"] + " " + row["path"] + " matches the measured PMS wire envelope",
                          status == expected["status"] and got_headers == expected_headers
                          and raw == base64.b64decode(expected["body_base64"])
                          and read_error == expected.get("read_error"),
                          (status, got_headers, read_error))

                for row in RESOURCES["rows"]:
                    status, headers, raw = request(port, row["method"], row["path"])
                    check(row["path"] + " matches the complete PMS resource recording",
                          status == row["status"] and headers.get("content-type") == row["content_type"]
                          and hashlib.sha256(raw).hexdigest() == row["sha256"],
                          (status, len(raw), hashlib.sha256(raw).hexdigest()))

                # A byte range must select the reference bytes, not merely mimic its status.
                icon = next(row for row in CAPTURE["rows"] if row["path"] == "/favicon.ico")
                body = base64.b64decode(icon["body_base64"])
                status, headers, raw = request(port, "GET", "/favicon.ico", token=None,
                                               extra={"Range": "bytes=1000-1999", "Accept-Encoding": "gzip"})
                check("the public icon's range is byte-exact and never compressed",
                      status == 206 and raw == body[1000:2000] and "content-encoding" not in headers,
                      (status, headers, raw))
                check("framework exception identities and agent descriptors match current PMS",
                      not verify_framework_identity("http://127.0.0.1:%d" % port, TOKEN))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if failed:
                    log.seek(0)
                    print(log.read().decode(errors="replace")[-2000:])
    print("%d/%d checks passed" % (checked - len(failed), checked))
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
