#!/usr/bin/env python3
"""Compare a running rex's framework identity with current real PMS captures.

Usage: python3 tests/test_framework_identity.py --base-url http://127.0.0.1:32663 --token TOKEN
No reference-server token is stored in the fixture or used by this regression.
"""
import argparse
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

FIXTURE = Path(__file__).parent / "fixtures" / "framework_identity_2026_10_22.json"


def verify(base_url, token):
    base = urlsplit(base_url)
    capture = json.loads(FIXTURE.read_text())
    failures = []
    checked = 0
    seen = set()
    for row in capture["records"]:
        path = row["path"]
        if path in seen:
            continue
        seen.add(path)
        # Capture includes one plugin URL without /root solely as a control.
        if row["status"] != 500 and not path.startswith("/system/agents"):
            continue
        for accept in ("application/xml", "application/json"):
            connection_type = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            conn = connection_type(base.hostname, base.port, timeout=20)
            try:
                conn.request("GET", path, headers={"Accept": accept, "X-Plex-Token": token})
                response = conn.getresponse()
                body = response.read()
                assert response.status == row["status"], (response.status, row["status"])
                if row["status"] == 500:
                    # Framework exceptions remain XML even when JSON is requested.
                    assert response.getheader("Content-Type") == "application/xml"
                    assert body == row["body"].encode(), body.decode(errors="replace")
                else:
                    expected = [(agent.get("identifier"), int(agent.get("id")))
                                for agent in ET.fromstring(row["body"]).findall("Agent")]
                    if accept == "application/json":
                        container = json.loads(body)["MediaContainer"]
                        got = [(agent["identifier"], agent.get("id")) for agent in container.get("Agent", [])]
                        assert container["size"] == len(expected)
                    else:
                        container = ET.fromstring(body)
                        got = [(agent.get("identifier"), int(agent.get("id"))
                                if agent.get("id") is not None else None)
                               for agent in container.findall("Agent")]
                        assert int(container.get("size")) == len(expected)
                    assert got == expected, (got, expected)
                print("PASS", accept, path)
            except (AssertionError, ValueError, KeyError, OSError) as error:
                failures.append((accept, path))
                print("FAIL", accept, path, str(error)[:1200])
            finally:
                conn.close()
            checked += 1
    print(f"{checked} framework identity comparisons; {len(failures)} failures")
    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default="pms-identity-smoke")
    args = parser.parse_args()
    return verify(args.base_url, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
