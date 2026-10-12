"""The server side of Plex's claim flow.

When a client sees an unclaimed server it offers "Claim this server". That button
does NOT talk to plex.tv on the server's behalf -- it fetches a short-lived
`claim-…` token and hands it to the SERVER, which performs the exchange itself:

    POST http://<server>/myplex/claim?token=claim-XXXX

The server then POSTs that token to plex.tv, receives its own server token, and
becomes a claimed server.  This matters beyond cosmetics: doing the exchange
from the server's own request context is the only candidate left for creating
the `/api/servers/{machineIdentifier}` record, which is the gate for
`PUT /api/servers/{id}/connectivity` -> `presence=true` -> plex.tv publishing an
`accessToken` to clients.  Writing the device registry directly (the pd_inject
technique) never creates it, and neither does exchanging a claim token
out-of-band the way this spike did first.

The resulting token is persisted so a restart does not un-claim the server.
"""

import collections
import gzip
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

logger = logging.getLogger("rex.claim")

EXCHANGE_URL = "https://plex.tv/api/claim/exchange"
CONNECTIVITY_URL = "https://plex.tv/api/servers/%s/connectivity"
FEATURES_URL = "https://servers.plex.tv/api/v2/features"
ACCOUNT_URL = "https://servers.plex.tv/api/v2/user"
SERVERS_URL = "https://plex.tv/servers.xml"
PUBSUB_URL = "https://pubsub.plex.tv/sub/eventsource/%s/%s"
ACCESS_TOKENS_URL = "https://plex.tv/api/v2/server/access_tokens"

# Measured in zen's own 1.42.2 PMS log (tests/fixtures/plextv_access_tokens_s1422.json): a
# request carrying a token the server does not hold makes it re-read the list plex.tv keeps of
# every token allowed in -- `?auth_token=<server token>&unknownToken=<that token>` -- unless the
# last read finished about 6 s ago or less ("Auth: tokens were refreshed recently enough"). All
# 586 reads in four hours were prompted that way; none ran on a timer.
ACCESS_REFRESH_WINDOW = 6

# Measured on a real 1.42.2 PMS claimed on zen (tests/fixtures/plextv_registration_s1422.json):
# its EventSourceClient gives up on a pubsub stream that says nothing for 20 s -- plex.tv
# sends a `: -1` comment every 10 s -- and retries a dropped one after 15 s.
PUBSUB_TIMEOUT = 20
PUBSUB_RETRY = 15

# Where the plex.tv family of hosts is reached. Unset -- the default -- is the real
# service. Set, every plex.tv, servers.plex.tv and scrobbles.plex.tv URL is sent to
# `<base>/<host><path>` instead, which is how tests/test_plextv.py drives a claim, an
# announce and an account import against a local stub: exercising these flows against
# the real account signs real servers out (the 2026-09-11 token reset), and a
# second server announcing an identity rewrites that identity's registration. The host
# stays in the path so the stub still sees which service was addressed.
PLEXTV_URL_ENV = "REX_PLEXTV_URL"


def plextv_url(url):
    base = os.environ.get(PLEXTV_URL_ENV)
    if not base:
        return url
    return base.rstrip("/") + "/" + url.split("://", 1)[1]


def read_body(response):
    """The body, inflated: plex.tv gzips its answer whenever a PMS's `Accept-Encoding:
    gzip` asks for it (the device PUT and the claim exchange both, in the fixture), and
    urllib does not undo that."""
    body = response.read()
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
        body = gzip.decompress(body)
    return body


class Claim:
    """Holds rex's claimed identity and performs the exchange."""

    def __init__(self, machine_identifier, state_path, *, name="rex", version="1.43.3"):
        self.machine = machine_identifier
        self.name = name
        self.version = version
        self._path = state_path
        self._lock = threading.Lock()
        self.token = None
        self.owner = None
        # A real PMS reports the owner's email in /myplex/account.
        self.email = None
        self.feature_uuids = []
        # Numeric account id used by the measured watch-state sync endpoints.
        self.account_id = None
        # Opaque scrobble cursor. Its contents are owned by plex.tv; only exact
        # round-tripping matters.
        self.view_state_cursor = None
        # The tokens plex.tv lets reach this server, as last read, and when that read
        # finished. In memory only: a claimed start reads the list again.
        self.access_tokens = frozenset()
        self._access_read = None
        self._access_lock = threading.Lock()
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as handle:
                state = json.load(handle)
            if state.get("machine") == self.machine:
                self.token = state.get("token")
                self.owner = state.get("owner")
                self.email = state.get("email")
                self.feature_uuids = sorted(set(state.get("feature_uuids") or []))
                self.account_id = state.get("account_id")
                self.view_state_cursor = state.get("view_state_cursor")
                logger.info("loaded claim for %s (owner %s)", self.machine, self.owner)
            else:
                # A different machineIdentifier is a different server; its claim
                # does not transfer.
                logger.warning("stored claim is for another machineIdentifier; ignoring")
        except Exception as error:
            logger.error("could not read claim state: %s", error)

    def _save(self):
        try:
            with open(self._path, "w") as handle:
                json.dump(
                    {"machine": self.machine, "token": self.token,
                     "owner": self.owner, "email": self.email,
                     "feature_uuids": self.feature_uuids,
                     "account_id": self.account_id,
                     "view_state_cursor": self.view_state_cursor},
                    handle,
                )
            os.chmod(self._path, 0o600)
        except Exception as error:
            logger.error("could not persist claim state: %s", error)

    # -- the flow --------------------------------------------------------

    @property
    def claimed(self):
        return bool(self.token)

    def _headers(self):
        return {
            "X-Plex-Client-Identifier": self.machine,
            "X-Plex-Product": "Plex Media Server",
            "X-Plex-Version": self.version,
            "X-Plex-Platform": "Linux",
            "X-Plex-Platform-Version": "6.1",
            "X-Plex-Device": "PC",
            "X-Plex-Device-Name": self.name,
            "X-Plex-Provides": "server",
            "Accept": "application/xml",
        }

    def pms_headers(self, agent="PlexMediaServer", accept_encoding=True):
        """What a real PMS sends on every claim and registration call, in its order
        (tests/fixtures/plextv_registration_s1422.json): its user agent, `*/*`, gzip, then
        the identity heartbeat.announce carries. The pubsub stream is the one request
        that names `PlexMediaServer-EventSourceClient` and asks for no encoding."""
        headers = collections.OrderedDict([("User-Agent", "%s/%s" % (agent, self.version)),
                                           ("Accept", "*/*")])
        if accept_encoding:
            headers["Accept-Encoding"] = "gzip"
        headers.update([
            ("X-Plex-Client-Identifier", self.machine),
            ("X-Plex-Version", self.version),
            ("X-Plex-Provides", "server"),
            ("X-Plex-Platform", "Linux"),
            ("X-Plex-Product", "Plex Media Server"),
            ("X-Plex-Platform-Version", "6.1"),
            ("X-Plex-Device-Name", self.name),
            ("X-Plex-Device", "PC"),
            ("X-Plex-Model", "x86_64"),
            ("X-Plex-Device-Vendor", "rex"),
        ])
        return headers

    def exchange(self, claim_token):
        """Redeem a `claim-…` token for this server's own plex.tv token."""
        url = EXCHANGE_URL + "?" + urllib.parse.urlencode({"token": claim_token})
        request = urllib.request.Request(plextv_url(url), method="POST", headers=self.pms_headers(), data=b"")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = read_body(response)

        attrs = ET.fromstring(body).attrib
        token = attrs.get("authToken") or attrs.get("authenticationToken")
        if not token:
            raise RuntimeError("claim exchange returned no authToken")

        with self._lock:
            self.token = token
            self.owner = attrs.get("username")
            self.email = attrs.get("email")
            # The pubsub path names the account; the exchange answer already carries it.
            if str(attrs.get("id") or "").isdigit():
                self.account_id = int(attrs["id"])
            self._save()

        logger.info("CLAIMED by %s; server token stored", self.owner)
        return attrs

    def fetch_features(self, token=None, opener=urllib.request.urlopen):
        """Read the account-scoped feature UUID universe from plex.tv.

        Cycle 46's complete outbound-read census found the exact source a real PMS
        uses: GET servers.plex.tv/api/v2/features?X-Plex-Token=... answers a flat
        XML features document whose feature rows carry id and uuid. The UUID
        attributes, unioned with the server's named defaults, equal the reference's
        public ownerFeatures set exactly.
        """
        token = token or self.token
        if not token:
            return []
        query = urllib.parse.urlencode({"X-Plex-Token": token})
        request = urllib.request.Request(
            plextv_url(FEATURES_URL + "?" + query),
            headers=dict(self._headers(), Accept="application/xml"),
        )
        with opener(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        return sorted({node.attrib["uuid"] for node in root.iter("feature")
                       if node.attrib.get("uuid")})

    def owner_features(self, named_defaults):
        """Return the sorted public feature string for this claimed account."""
        values = set(filter(None, str(named_defaults or "").split(",")))
        values.update(self.feature_uuids)
        return ",".join(sorted(values))

    def fetch_account_id(self, token=None, opener=urllib.request.urlopen):
        """Read the numeric owner id used by both cloud state endpoints."""
        token = token or self.token
        if not token:
            return None
        query = urllib.parse.urlencode([
            ("includeSubscriptions", 1),
            ("includeProviders", 1),
        ])
        request = urllib.request.Request(
            plextv_url(ACCOUNT_URL + "?" + query),
            headers=dict(self._headers(), **{"X-Plex-Token": token}),
        )
        with opener(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        value = root.attrib.get("id")
        if value is None or not str(value).isdigit():
            raise RuntimeError("account response returned no numeric id")
        return int(value)

    def ensure_account_id(self):
        """Acquire and persist the owner id when loading an older claim file."""
        if self.account_id is None and self.token:
            with self._lock:
                if self.account_id is None:
                    self.account_id = self.fetch_account_id()
                    self._save()
        return self.account_id

    def save_view_state_cursor(self, cursor):
        """Atomically advance the opaque cloud cursor after rows were imported."""
        with self._lock:
            self.view_state_cursor = cursor
            self._save()

    def fetch_access_tokens(self, unknown=None):
        """`GET /api/v2/server/access_tokens`: every token plex.tv lets reach this server --
        its own, the owner's devices' and shared users' -- one `<token>` row each. A row with
        `tokenDisabled="1"` is not let in."""
        pairs = [("auth_token", self.token)]
        if unknown:
            pairs.append(("unknownToken", unknown))
        headers = self.pms_headers()
        headers["X-Plex-Token"] = self.token
        url = ACCESS_TOKENS_URL + "?" + urllib.parse.urlencode(pairs)
        request = urllib.request.Request(plextv_url(url), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(read_body(response))
        return frozenset(node.attrib["token"] for node in root.iter("token")
                         if node.attrib.get("token") and node.attrib.get("tokenDisabled") != "1")

    def refresh_access_tokens(self, unknown=None):
        """Re-read the list; a failed read keeps the last one."""
        try:
            self.access_tokens = self.fetch_access_tokens(unknown)
            logger.info("plex.tv lists %d access tokens", len(self.access_tokens))
        except Exception as error:
            logger.error("access tokens read failed: %s", error)
        self._access_read = time.monotonic()

    def admits(self, token):
        """Whether `token` opens this claimed server: its own token, or one plex.tv lists for
        it. An unknown one re-reads the list first unless that was done within
        ACCESS_REFRESH_WINDOW; a request that arrives while a read is running waits for it
        ("Auth: tokens were just refreshed, caller should check again")."""
        if not self.token or not token:
            return False
        if token == self.token or token in self.access_tokens:
            return True
        with self._access_lock:
            if token in self.access_tokens:
                return True
            if (self._access_read is not None
                    and time.monotonic() - self._access_read < ACCESS_REFRESH_WINDOW):
                return False
            self.refresh_access_tokens(unknown=token)
            return token in self.access_tokens

    def announce_connectivity(self, async_identifier):
        """`PUT /api/servers/{id}/connectivity` -- a real PMS asking plex.tv to test its
        reachability, under an asyncIdentifier of its own; plex.tv answers over pubsub
        (`notifyConnectivity`, same id). 404 here means plex.tv has no SERVER record for
        this machineIdentifier (see `publish`)."""
        if not self.token:
            return None, "not claimed"

        query = urllib.parse.urlencode(
            [("X-Plex-Token", self.token), ("asyncIdentifier", async_identifier)]
        )
        url = (CONNECTIVITY_URL % self.machine) + "?" + query
        request = urllib.request.Request(plextv_url(url), method="PUT", headers=self.pms_headers(), data=b"")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, read_body(response)[:200]
        except urllib.error.HTTPError as error:
            return error.code, error.read()[:200]

    def publish(self, local_address):
        """`POST /servers.xml` -- what creates and then updates plex.tv's SERVER record
        (`/api/servers/{machineIdentifier}`), which `connectivity` and the resource's
        `accessToken` hang off. plex.tv answers it 422 every time, for a real PMS too, and
        writes the record anyway: the probe's record was created at the second of its
        first POST and updated at the second of each later one. Returns the status."""
        attr = lambda value: '"%s"' % escape(value or "", {'"': "&quot;"})
        body = ('<MediaContainer size="1">\n<Server relay="0" name=%s host="" '
                'localAddresses=%s machineIdentifier=%s version=%s date="%d" />\n'
                '</MediaContainer>\n' % (attr(self.name), attr(local_address), attr(self.machine),
                                          attr(self.version), int(time.time() * 1000000)))
        url = SERVERS_URL + "?" + urllib.parse.urlencode({"auth_token": self.token})
        headers = self.pms_headers()
        headers["Content-Type"] = "application/xml"
        request = urllib.request.Request(plextv_url(url), method="POST", headers=headers,
                                         data=body.encode("utf-8"))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def subscribe(self, stop=None):
        """One pubsub subscription, read until plex.tv or the network ends it.

        `presence` is this stream: the probe went present while its subscription was
        open and absent the moment it closed, with no connectivity PUT in between. Once
        the stream answers, PMS logs "regained Internet connectivity" and 2 ms later
        requests a reachability check (`announce_connectivity`); plex.tv's verdict comes
        back on the stream 3 s later as `notifyConnectivity`."""
        if self.account_id is None:
            self.ensure_account_id()
        query = urllib.parse.urlencode({"X-Plex-Token": self.token})
        url = (PUBSUB_URL % (self.account_id, self.machine)) + "?" + query
        request = urllib.request.Request(
            plextv_url(url),
            headers=self.pms_headers("PlexMediaServer-EventSourceClient", accept_encoding=False))
        with urllib.request.urlopen(request, timeout=PUBSUB_TIMEOUT) as response:
            logger.info("pubsub subscribed for %s", self.machine)
            status, body = self.announce_connectivity(str(uuid.uuid4()))
            logger.info("connectivity PUT -> %s %s", status, body)
            for raw in response:
                if stop is not None and stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    self._command(line[5:].strip())

    def _command(self, payload):
        try:
            message = ET.fromstring(payload).attrib
        except ET.ParseError:
            logger.warning("pubsub: unparsable message %r", payload[:200])
            return
        command = message.get("command")
        if command == "notifyConnectivity":
            logger.info("pubsub: reachability %s for asyncIdentifier %s",
                        message.get("connectivity"), message.get("asyncIdentifier"))
        else:
            # ponytail: only notifyConnectivity is measured; other commands are logged, not acted on.
            logger.info("pubsub: unhandled command %s", command)

    def start_registration(self, local_address, announce=None, stop=None, access=False):
        """What a claimed PMS keeps up with plex.tv, on a daemon thread: the SERVER record
        (`publish`), the device PUT when there is a URL to announce (after a claim; at
        start the heartbeat's first beat is that PUT), and the pubsub stream, resubscribed
        `PUBSUB_RETRY` seconds after it drops. Nothing else is periodic: in the hour after
        its restart the probe sent no further publish, device PUT or connectivity check,
        and held one stream. `access`: a claimed START reads the access-token list first,
        which is the recording's first call there; the claim itself did not."""
        stop = stop or threading.Event()

        def run():
            if access:
                with self._access_lock:
                    self.refresh_access_tokens()
            try:
                logger.info("servers.xml POST -> %s", self.publish(local_address))
            except Exception as error:
                logger.error("registration failed: %s", error)
            if announce is not None:
                try:
                    announce()
                except Exception as error:
                    logger.error("registration failed: %s", error)
            while not stop.is_set():
                try:
                    self.subscribe(stop)
                    logger.warning("pubsub stream ended; resubscribing in %ds", PUBSUB_RETRY)
                except Exception as error:
                    logger.error("pubsub failed: %s; resubscribing in %ds", error, PUBSUB_RETRY)
                stop.wait(PUBSUB_RETRY)

        threading.Thread(target=run, name="rex-registration", daemon=True).start()
        return stop
