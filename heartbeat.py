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

Cycle 44 corrected the target id, against the harness's own recording of a real PMS
announcing (`capture/outbound.jsonl`, three identical `PUT`s): a real server announces
with `PUT https://servers.plex.tv/devices/{machineIdentifier}` -- the IDENTIFIER form --
carrying its whole `X-Plex-*` identity and its token in the query. CLAUDE.md has
recorded that correction since 2026-09-12 and this module did not follow it.

The numeric form this module used is also the dangerous one: `/devices/{numeric}`
resolves to whatever device row already holds that id and silently overwrites it, which
is what the safety notes in HANDOVER.md §9 are about. `resolve_device_id` is kept for
looking a device up, and nothing here needs it any more.
"""

import collections
import logging
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from claim import plextv_url

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

# Where a real PMS announces, and the identity it carries. Read out of the harness's
# `capture/outbound.jsonl` on kids: three `PUT`s, byte-identical, to `servers.plex.tv`.
ANNOUNCE_BASE = "https://servers.plex.tv"
# The version plexsim's own `/identity` reports. Announcing a different one tells
# plex.tv something no client reading the server would ever see.
DEFAULT_VERSION = "1.42.2.10156-plexsim"
DEFAULT_PLATFORM = "Linux"
DEFAULT_PLATFORM_VERSION = "6.1"
DEFAULT_DEVICE = "PC"
DEFAULT_MODEL = "x86_64"
DEFAULT_VENDOR = "plexsim"


def resolve_device_id(token, *, name=None, client_identifier=None):
    """Find plex.tv's numeric device id for a device, by name or clientIdentifier.

    Needed because the registration endpoint is keyed on the numeric id while
    everything else about a server is keyed on its machineIdentifier.
    """
    if not name and not client_identifier:
        raise ValueError("need a name or a clientIdentifier to resolve")

    request = urllib.request.Request(
        plextv_url("https://plex.tv/devices.xml"),
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


def announce(token, machine_identifier, url, *, name="plexsim",
             version=DEFAULT_VERSION, platform=DEFAULT_PLATFORM,
             platform_version=DEFAULT_PLATFORM_VERSION, device=DEFAULT_DEVICE,
             model=DEFAULT_MODEL, vendor=DEFAULT_VENDOR, https_enabled=True):
    """One registration PUT, byte-shaped like a real PMS's.

    Cycle 44, read out of the harness's own `capture/outbound.jsonl` -- the
    server-to-plex.tv axis, which no cycle had ever swept. All three announces a real
    1.43.4 PMS made are identical, and they are:

        PUT https://servers.plex.tv/devices/<machineIdentifier>
            ?Connection[][uri]=<uri>&httpsEnabled=1&httpsRequired=0
            &dnsRebindingProtection=0&X-Plex-Token=<token>

    with an empty body and thirteen headers in a fixed order. Four things plexsim had
    wrong, each of them measured here:

    * the HOST is `servers.plex.tv`, not `plex.tv`;
    * the path segment is the **machineIdentifier**, not plex.tv's numeric device id.
      CLAUDE.md has recorded that correction since 2026-09-12 and the code never
      followed it -- HANDOVER.md §3.1 class C, a rule recorded correctly and
      implemented as a proxy. It is also the dangerous half: a numeric id resolves to
      whatever device row already holds it and silently overwrites that device;
    * the token rides in the QUERY, and `natLoopbackSupported` is not sent at all;
    * `httpsEnabled` is the server's own https capability, not the uri's scheme -- the
      reference sent `httpsEnabled=1` beside an `http://172.31.7.2:32400` uri.

    A real PMS also sends its whole identity -- `User-Agent: PlexMediaServer/<version>`,
    `Accept: */*` (not `application/xml`), `Accept-Encoding: gzip`, and eight `X-Plex-*`
    headers led by an `X-Plex-Client-Identifier` equal to the machineIdentifier. plexsim
    sent six headers and none of the identifying ones.

    Returns the parsed Device element.
    """
    query = urllib.parse.urlencode([
        ("Connection[][uri]", url),
        ("httpsEnabled", 1 if https_enabled else 0),
        ("httpsRequired", 0),
        ("dnsRebindingProtection", 0),
        ("X-Plex-Token", token),
    ])
    request = urllib.request.Request(
        plextv_url("%s/devices/%s?%s" % (ANNOUNCE_BASE, machine_identifier, query)),
        method="PUT",
        data=b"",
        headers=collections.OrderedDict([
            ("User-Agent", "PlexMediaServer/%s" % version),
            ("Accept", "*/*"),
            ("Accept-Encoding", "gzip"),
            ("X-Plex-Client-Identifier", machine_identifier),
            ("X-Plex-Version", version),
            ("X-Plex-Provides", "server"),
            ("X-Plex-Platform", platform),
            ("X-Plex-Product", "Plex Media Server"),
            ("X-Plex-Platform-Version", platform_version),
            ("X-Plex-Device-Name", name),
            ("X-Plex-Device", device),
            ("X-Plex-Model", model),
            ("X-Plex-Device-Vendor", vendor),
        ]),
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return ET.fromstring(response.read())


def is_visible(token, name):
    """Is the device actually in the list clients read? The only test that counts.

    The device row existing is not the same thing: a row with `presence="0"` is
    invisible to every client, which is exactly the failure this guards against.
    """
    request = urllib.request.Request(
        plextv_url("https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1"),
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

    def __init__(self, token, machine_identifier, url, *, name="plexsim",
                 interval=DEFAULT_INTERVAL, version=DEFAULT_VERSION,
                 platform=DEFAULT_PLATFORM, platform_version=DEFAULT_PLATFORM_VERSION):
        self._token = token
        self._machine = machine_identifier
        self._version = version
        self._platform = platform
        self._platform_version = platform_version
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
            "heartbeat every %ds for machine %s -> %s", self._interval, self._machine, self._url
        )

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                announce(self._token, self._machine, self._url, name=self._name,
                         version=self._version, platform=self._platform,
                         platform_version=self._platform_version)
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
