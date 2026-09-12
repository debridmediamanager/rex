#!/usr/bin/env python3
"""Measure how long a registered fake server stays client-visible.

Registering a device with a Connection URI (the pd_inject trick) makes it appear
in `/api/v2/resources` -- the list every Plex client reads to find servers --
but only for a while.  A real PMS holds a live relationship with plex.tv, so
plex.tv considers it present; a server that only ever PUTs once goes stale and
silently vanishes from every client's server list.

This re-announces once and then polls, recording the moment the resource
disappears.  The gap is the heartbeat interval any plexsim deployment has to
honour, and it is the reason "register at startup" is not enough.

    PLEX_TOKEN=... ./presence.py --url https://host:port --machine <id> --seconds 600
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def _get(url, token, client_id="plexsim-presence", accept="application/json"):
    request = urllib.request.Request(
        url,
        headers={
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": client_id,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def announce(token, machine, url, name):
    """The pd_inject registration PUT."""
    query = urllib.parse.urlencode(
        {
            "Connection[][uri]": url,
            "httpsEnabled": 1,
            "httpsRequired": 0,
            "dnsRebindingProtection": 0,
            "natLoopbackSupported": 1,
        }
    )
    request = urllib.request.Request(
        "https://plex.tv/devices/%s?%s" % (machine, query),
        method="PUT",
        headers={
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": machine,
            "X-Plex-Product": "Plex Media Server",
            "X-Plex-Version": "1.43.3.6021-plexsim",
            "X-Plex-Provides": "server",
            "X-Plex-Device-Name": name,
            "Accept": "application/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def resource_names(token):
    body = _get(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1", token
    )
    rows = json.loads(body)
    return [r.get("name") for r in rows if "server" in (r.get("provides") or "")]


def device_presence(token, name):
    body = _get("https://plex.tv/devices.xml", token, accept="application/xml")
    root = ET.fromstring(body)
    for device in root:
        if device.attrib.get("name") == name:
            return {
                "presence": device.attrib.get("presence"),
                "lastSeenAt": device.attrib.get("lastSeenAt"),
                "connections": [c.get("uri") for c in device.findall("Connection")],
            }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--name", default="plexsim-spike")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=int, default=20)
    args = parser.parse_args()

    token = os.environ["PLEX_TOKEN"]

    print("announcing %s -> %s" % (args.name, args.url))
    announce(token, args.machine, args.url, args.name)

    started = time.time()
    last_visible = None
    first_absent = None

    while time.time() - started < args.seconds:
        elapsed = int(time.time() - started)
        names = resource_names(token)
        visible = args.name in names
        info = device_presence(token, args.name) or {}
        print(
            "  t+%-4ds resource=%-5s presence=%-4s lastSeen=%s"
            % (elapsed, "YES" if visible else "no", info.get("presence"), info.get("lastSeenAt"))
        )
        if visible:
            last_visible = elapsed
            first_absent = None
        elif first_absent is None:
            first_absent = elapsed
        time.sleep(args.interval)

    print("\nlast seen visible at t+%ss; first absent at t+%ss" % (last_visible, first_absent))


if __name__ == "__main__":
    main()
