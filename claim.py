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
                     "feature_uuids": self.feature_uuids},
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
        request = urllib.request.Request(url, method="POST", headers=self._headers(), data=b"")
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
            try:
                self.feature_uuids = self.fetch_features(token)
            except Exception as error:
                # Claiming is the irreversible/user-visible operation. A transient
                # feature-table failure must not turn a successful exchange into an
                # apparent claim failure; the named, measured defaults remain valid.
                self.feature_uuids = []
                logger.warning("could not read plex.tv feature table: %s", error)
            self._save()

        logger.info("CLAIMED by %s; server token stored", self.owner)
        return attrs

    def fetch_features(self, token=None):
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
            FEATURES_URL + "?" + query,
            headers=dict(self._headers(), Accept="application/xml"),
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        return sorted({node.attrib["uuid"] for node in root.iter("feature")
                       if node.attrib.get("uuid")})

    def owner_features(self, named_defaults):
        """Return the sorted public feature string for this claimed account."""
        values = set(filter(None, str(named_defaults or "").split(",")))
        values.update(self.feature_uuids)
        return ",".join(sorted(values))

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
        request = urllib.request.Request(url, method="PUT", headers=self._headers(), data=b"")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()[:200]
        except urllib.error.HTTPError as error:
            return error.code, error.read()[:200]
