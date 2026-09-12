"""Keep a plexsim registration visible to Plex clients.

A device registered with `PUT /devices/{id}?Connection[][uri]=...` appears in
`/api/v2/resources` -- the list every Plex client reads to find servers -- and
then STOPS appearing.  The device row carries `presence="0"` from the moment it
is written, because presence is something a real PMS earns by maintaining a live
relationship with plex.tv, not something the PUT confers.  Measured: plexsim
vanished from the resource list within minutes of registering, after which
python-plexapi's discovery could not find it at all; re-announcing brought it
straight back.

pd_inject registered once at startup and never again, which is very likely why
it was never released: the library browses fine for a few minutes and then the
server simply is not there any more.

So the registration is a lease, and this renews it.  Deliberately opt-in and
explicit about its target: it WRITES to a plex.tv account, so nothing here runs
unless a device id and token are passed in.

Note on the target id: `/devices/{id}` expects plex.tv's own NUMERIC device id.
Passing a machineIdentifier does not 404 -- it resolves to some other device row
and overwrites it -- so `device_id` here must be the numeric one, and
`resolve_device_id` is the way to look it up rather than guess.
"""

import logging
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger("plexsim.heartbeat")

# MEASURED 2026-09-11: a registration expires ~40 minutes after its last PUT.
# One announce at 11:18:37 was still visible at t+39 min and gone at t+40 min,
# with the transition caught inside a 72-second window; both a fixed and a fresh
# X-Plex-Client-Identifier agreed at every timepoint, so this is a real lease and
# not a caching artifact. Re-announcing restores visibility immediately.
#
# 10 minutes gives a 4x margin on a 40-minute lease at a cost of one small
# request per interval, and also papers over the one unexplained early transient
# seen during the spike.
DEFAULT_INTERVAL = 600


def resolve_device_id(token, *, name=None, client_identifier=None):
    """Find plex.tv's numeric device id for a device, by name or clientIdentifier.

    Needed because the registration endpoint is keyed on the numeric id while
    everything else about a server is keyed on its machineIdentifier.
    """
    if not name and not client_identifier:
        raise ValueError("need a name or a clientIdentifier to resolve")

    request = urllib.request.Request(
        "https://plex.tv/devices.xml",
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        root = ET.fromstring(response.read())

    for device in root:
        attrs = device.attrib
        if client_identifier and attrs.get("clientIdentifier") == client_identifier:
            return attrs.get("id")
        if name and attrs.get("name") == name:
            return attrs.get("id")
    return None


def announce(token, device_id, url, *, name="plexsim", version="1.43.3.6021-plexsim"):
    """One registration PUT. Returns the parsed Device element."""
    query = urllib.parse.urlencode(
        {
            "Connection[][uri]": url,
            "httpsEnabled": 1 if url.startswith("https://") else 0,
            "httpsRequired": 0,
            "dnsRebindingProtection": 0,
            "natLoopbackSupported": 1,
        }
    )
    request = urllib.request.Request(
        "https://plex.tv/devices/%s?%s" % (device_id, query),
        method="PUT",
        headers={
            "X-Plex-Token": token,
            "X-Plex-Product": "Plex Media Server",
            "X-Plex-Version": version,
            "X-Plex-Provides": "server",
            "X-Plex-Device-Name": name,
            "Accept": "application/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return ET.fromstring(response.read())


def is_visible(token, name):
    """Is the device actually in the list clients read? The only test that counts.

    The device row existing is not the same thing: a row with `presence="0"` is
    invisible to every client, which is exactly the failure this guards against.
    """
    request = urllib.request.Request(
        "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
        headers={
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": "plexsim-heartbeat",
            "Accept": "application/json",
        },
    )
    import json

    with urllib.request.urlopen(request, timeout=25) as response:
        rows = json.load(response)
    return any(
        row.get("name") == name and "server" in (row.get("provides") or "") for row in rows
    )


class Heartbeat:
    """Re-announces the registration on an interval, on a daemon thread."""

    def __init__(self, token, device_id, url, *, name="plexsim", interval=DEFAULT_INTERVAL):
        self._token = token
        self._device_id = device_id
        self._url = url
        self._name = name
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_ok = None
        self.failures = 0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="plexsim-heartbeat", daemon=True)
        self._thread.start()
        logger.info(
            "heartbeat every %ds for device %s -> %s", self._interval, self._device_id, self._url
        )

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                announce(self._token, self._device_id, self._url, name=self._name)
                self.last_ok = time.time()
                self.failures = 0
                # Verifying costs a second request but turns a silent
                # disappearance into a log line, which is the whole point.
                if not is_visible(self._token, self._name):
                    logger.warning(
                        "announced but NOT visible in /api/v2/resources -- "
                        "clients cannot see plexsim right now"
                    )
            except Exception as error:
                self.failures += 1
                logger.error("heartbeat failed (%d in a row): %s", self.failures, error)
            self._stop.wait(self._interval)
