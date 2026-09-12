#!/usr/bin/env python3
"""Does a plexsim registration actually decay? Cache-proof version.

`presence.py` announced once and then polled `/api/v2/resources` every 60 s with
ONE client identifier, and saw `resource=YES` for 28 minutes -- from which I
concluded there was no decay.  Then a single check a few minutes after a fresh
announce came back False.  Both cannot be true, and the likely reconciliation is
that plex.tv caches the resources response per client identifier, so the polling
run was re-reading a stale positive while the real state had already dropped.

This distinguishes the two: announce once, then check at a widening schedule
using a BRAND NEW `X-Plex-Client-Identifier` on every single read, so no read can
be served from a previous read's cache.  Also records the device row's own
`presence` attribute next to it.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import heartbeat  # noqa: E402


def resources_uncached(token):
    """Read the client-visible server list with a never-before-used identity."""
    request = urllib.request.Request(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
        headers={
            "X-Plex-Token": token,
            # A fresh identifier per call is the whole point.
            "X-Plex-Client-Identifier": "plexsim-decay-" + uuid.uuid4().hex[:12],
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def device_row(token, name):
    request = urllib.request.Request(
        "https://plex.tv/devices.xml",
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        root = ET.fromstring(response.read())
    for device in root:
        if device.attrib.get("name") == name:
            return device.attrib
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="plexsim-spike")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--schedule",
        default="5,30,60,120,180,240,300,420,540,720,900",
        help="comma-separated seconds after the single announce to probe at",
    )
    args = parser.parse_args()

    token = os.environ["PLEX_TOKEN"]

    print("announce -> %s" % args.url, flush=True)
    heartbeat.announce(token, args.device_id, args.url, name=args.name)
    started = time.time()

    schedule = [int(s) for s in args.schedule.split(",") if s.strip()]
    previous = 0
    first_absent = None

    for mark in schedule:
        time.sleep(max(0, mark - previous))
        previous = mark
        try:
            rows = resources_uncached(token)
            names = [r.get("name") for r in rows if "server" in (r.get("provides") or "")]
            visible = args.name in names
        except Exception as error:
            print("  t+%-4ds read failed: %s" % (mark, error), flush=True)
            continue

        attrs = device_row(token, args.name)
        print(
            "  t+%-4ds visible=%-3s presence=%-4s servers=%s"
            % (
                int(time.time() - started),
                "YES" if visible else "no",
                attrs.get("presence"),
                ",".join(n for n in names if n),
            ),
            flush=True,
        )
        if not visible and first_absent is None:
            first_absent = mark

    if first_absent is None:
        print("\nstill visible at t+%ds -- no decay observed" % schedule[-1])
    else:
        print("\nFIRST ABSENT at t+%ds -- heartbeat must be shorter than that" % first_absent)


if __name__ == "__main__":
    main()
