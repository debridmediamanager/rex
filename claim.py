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

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger("plexsim.claim")

EXCHANGE_URL = "https://plex.tv/api/claim/exchange"
CONNECTIVITY_URL = "https://plex.tv/api/servers/%s/connectivity"
FEATURES_URL = "https://servers.plex.tv/api/v2/features"
ACCOUNT_URL = "https://servers.plex.tv/api/v2/user"

# Where the plex.tv family of hosts is reached. Unset -- the default -- is the real
# service. Set, every plex.tv, servers.plex.tv and scrobbles.plex.tv URL is sent to
# `<base>/<host><path>` instead, which is how tests/test_plextv.py drives a claim, an
# announce and an account import against a local stub: exercising these flows against
# the real account signs real servers out (HANDOVER: the 2026-09-11 token reset), and a
# second server announcing an identity rewrites that identity's registration. The host
# stays in the path so the stub still sees which service was addressed.
PLEXTV_URL_ENV = "PLEXSIM_PLEXTV_URL"


def plextv_url(url):
    base = os.environ.get(PLEXTV_URL_ENV)
    if not base:
        return url
    return base.rstrip("/") + "/" + url.split("://", 1)[1]


class Claim:
    """Holds plexsim's claimed identity and performs the exchange."""

    def __init__(self, machine_identifier, state_path, *, name="plexsim", version="1.43.3"):
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

    def exchange(self, claim_token):
        """Redeem a `claim-…` token for this server's own plex.tv token."""
        url = EXCHANGE_URL + "?" + urllib.parse.urlencode({"token": claim_token})
        request = urllib.request.Request(plextv_url(url), method="POST", headers=self._headers(), data=b"")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()

        attrs = ET.fromstring(body).attrib
        token = attrs.get("authToken") or attrs.get("authenticationToken")
        if not token:
            raise RuntimeError("claim exchange returned no authToken")

        with self._lock:
            self.token = token
            self.owner = attrs.get("username")
            self.email = attrs.get("email")
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

    def announce_connectivity(self):
        """`PUT /api/servers/{id}/connectivity` -- what a real PMS calls to be
        marked present. 404 here means plex.tv has no SERVER record for this
        machineIdentifier, only a device record."""
        if not self.token:
            return None, "not claimed"

        import uuid

        query = urllib.parse.urlencode(
            {"X-Plex-Token": self.token, "asyncIdentifier": str(uuid.uuid4())}
        )
        url = (CONNECTIVITY_URL % self.machine) + "?" + query
        request = urllib.request.Request(plextv_url(url), method="PUT", headers=self._headers(), data=b"")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()[:200]
        except urllib.error.HTTPError as error:
            return error.code, error.read()[:200]
