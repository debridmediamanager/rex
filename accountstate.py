"""Import a claimed owner's Plex watch and rating state.

Cycle 46's finite outbound census measured 284 consent reads followed by 283
cursor pages.  This module deliberately implements that observed grammar only:
the owner id comes from ``/api/v2/user``; consent uses the server token; the
state page uses the short-lived scrobbling bearer; and the opaque cursor is
persisted only after every recognized row has been applied.
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from claim import plextv_url

logger = logging.getLogger("simplex.accountstate")

DEFAULT_INTERVAL = 1800
ACCOUNT_REFRESH_INTERVAL = 86400
ACCOUNT_FEATURE_DELAY = 11
CONSENT_URL = "https://servers.plex.tv/api/v2/server/users/%s/view_state_sync"
STATE_URL = "https://scrobbles.plex.tv/state/%s"
SERVICES_URL = "https://servers.plex.tv/api/v2/server/users/services"
DECRYPT_SECRET_ENV = "SIMPLEX_SCROBBLING_DECRYPT_SECRET_B64"


class AccountRefresh:
    """Refresh the claimed owner profile and feature UUIDs once per day.

    Across the complete outbound capture every owner-profile read is followed by
    a feature-table read; the daily pairs are 10.8--11.45 seconds apart. Both
    values are committed together so a failure leaves the last-good identity and
    feature universe intact.
    """

    def __init__(self, claim, *, interval=ACCOUNT_REFRESH_INTERVAL,
                 feature_delay=ACCOUNT_FEATURE_DELAY,
                 opener=urllib.request.urlopen, clock=time.time, waiter=None):
        self.claim = claim
        self._interval = interval
        self._feature_delay = feature_delay
        self._open = opener
        self._clock = clock
        self._stop = threading.Event()
        self._waiter = waiter or self._stop.wait
        self._thread = None
        self._next = 0
        self.last_ok = None
        self.failures = 0

    def refresh(self):
        if not self.claim.claimed:
            return False
        account_id = self.claim.fetch_account_id(opener=self._open)
        if self._feature_delay:
            self._waiter(self._feature_delay)
        feature_uuids = self.claim.fetch_features(opener=self._open)
        with self.claim._lock:
            self.claim.account_id = account_id
            self.claim.feature_uuids = sorted(set(feature_uuids))
            self.claim._save()
        self.last_ok = self._clock()
        self.failures = 0
        return True

    def refresh_due(self):
        """Run at startup and then no more often than the measured daily cadence."""
        now = self._clock()
        if now < self._next:
            return False
        try:
            return self.refresh()
        finally:
            self._next = now + self._interval

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run,
                                        name="simplex-account-refresh", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh_due()
            except Exception as error:
                self.failures += 1
                logger.error("claimed-account refresh failed (%d in a row): %s",
                             self.failures, error)
            self._stop.wait(max(0, self._next - self._clock()))


# AES is deliberately local and small: Python's standard library has no block
# cipher, and account-state sync must not add a runtime package.  The table is
# the public Rijndael substitution box, not credential material.
_SBOX = (
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
)
_INV_SBOX = tuple(_SBOX.index(value) for value in range(256))


def _gf_mul(left, right):
    value = 0
    for _ in range(8):
        if right & 1:
            value ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return value


def _aes256_round_keys(key):
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    words = [list(key[index:index + 4]) for index in range(0, 32, 4)]
    rcon = 1
    while len(words) < 60:
        index = len(words)
        word = words[-1][:]
        if index % 8 == 0:
            word = [_SBOX[value] for value in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif index % 8 == 4:
            word = [_SBOX[value] for value in word]
        words.append([words[index - 8][offset] ^ word[offset] for offset in range(4)])
    return [sum(words[index:index + 4], []) for index in range(0, 60, 4)]


def _shift_rows(state, inverse=False):
    result = state[:]
    for row in range(1, 4):
        values = [state[row + 4 * column] for column in range(4)]
        amount = (-row if inverse else row) % 4
        values = values[amount:] + values[:amount]
        for column, value in enumerate(values):
            result[row + 4 * column] = value
    return result


def _mix_columns(state, inverse=False):
    matrix = ((14,11,13,9),(9,14,11,13),(13,9,14,11),(11,13,9,14)) if inverse \
        else ((2,3,1,1),(1,2,3,1),(1,1,2,3),(3,1,1,2))
    result = state[:]
    for column in range(4):
        source = state[4 * column:4 * column + 4]
        for row in range(4):
            result[4 * column + row] = 0
            for offset in range(4):
                result[4 * column + row] ^= _gf_mul(matrix[row][offset], source[offset])
    return result


def _aes_decrypt_block(block, round_keys):
    state = [value ^ round_keys[14][index] for index, value in enumerate(block)]
    for round_number in range(13, 0, -1):
        state = _shift_rows(state, inverse=True)
        state = [_INV_SBOX[value] for value in state]
        state = [value ^ round_keys[round_number][index]
                 for index, value in enumerate(state)]
        state = _mix_columns(state, inverse=True)
    state = _shift_rows(state, inverse=True)
    state = [_INV_SBOX[value] for value in state]
    return bytes(value ^ round_keys[0][index] for index, value in enumerate(state))


def _pms_key_iv(secret):
    """PMS's measured MD5 expansion (OpenSSL NID 4), not PBKDF2."""
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError("scrobbling decrypt secret must be exactly 32 bytes")
    expanded = b""
    previous = b""
    while len(expanded) < 48:
        previous = hashlib.md5(previous + secret).digest()
        expanded += previous
    return expanded[:32], expanded[32:48]


def _decrypt_scrobbling_token(encoded, secret):
    """Base64 + AES-256-CBC + PMS's permissive final-byte strip."""
    try:
        ciphertext = base64.b64decode(encoded, validate=True)
    except Exception as error:
        raise ValueError("scrobbling service token is not valid base64") from error
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("scrobbling service token is not whole AES blocks")
    key, iv = _pms_key_iv(secret)
    round_keys = _aes256_round_keys(key)
    plaintext = bytearray()
    previous = iv
    for index in range(0, len(ciphertext), 16):
        block = ciphertext[index:index + 16]
        decoded = _aes_decrypt_block(block, round_keys)
        plaintext.extend(left ^ right for left, right in zip(decoded, previous))
        previous = block
    # PMS disables EVP padding while decrypting and removes only the count in
    # the final byte. Reproducing that oddity is required by captured tokens.
    padding_count = plaintext[-1]
    if not 1 <= padding_count <= 16:
        raise ValueError("scrobbling service token has invalid padding count")
    try:
        return bytes(plaintext[:-padding_count]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("scrobbling service token is not UTF-8") from error


class ScrobblingBearerProvider:
    """Fetch and decrypt the measured per-owner scrobbling service JWT.

    Plex ships the 32-byte transport secret inside PMS. It is intentionally not
    copied into this repository; provide its base64 form through
    ``SIMPLEX_SCROBBLING_DECRYPT_SECRET_B64`` or ``decrypt_secret``. Bearers and
    encrypted service tokens remain memory-only and are never logged.
    """

    def __init__(self, claim, *, decrypt_secret=None, opener=urllib.request.urlopen,
                 clock=time.time):
        self.claim = claim
        self._secret = decrypt_secret
        self._open = opener
        self._clock = clock
        self._bearer = None
        self._expires = 0
        self._lock = threading.Lock()

    def _decrypt_secret(self):
        if self._secret is not None:
            secret = self._secret() if callable(self._secret) else self._secret
        else:
            encoded = os.environ.get(DECRYPT_SECRET_ENV, "")
            if not encoded:
                raise RuntimeError("%s is required for account-state sync" % DECRYPT_SECRET_ENV)
            try:
                secret = base64.b64decode(encoded, validate=True)
            except Exception as error:
                raise RuntimeError("%s is not valid base64" % DECRYPT_SECRET_ENV) from error
        if not isinstance(secret, bytes) or len(secret) != 32:
            raise RuntimeError("scrobbling decrypt secret must be exactly 32 bytes")
        return secret

    def _headers(self):
        values = self.claim._headers()
        return {
            "User-Agent": "PlexMediaServer/%s" % self.claim.version,
            "Accept": values.get("Accept", "application/xml"),
            "X-Plex-Client-Identifier": values.get("X-Plex-Client-Identifier", self.claim.machine),
            "X-Plex-Version": values.get("X-Plex-Version", self.claim.version),
            "X-Plex-Provides": values.get("X-Plex-Provides", "server"),
            "X-Plex-Platform": values.get("X-Plex-Platform", "Linux"),
            "X-Plex-Product": values.get("X-Plex-Product", "Plex Media Server"),
            "X-Plex-Platform-Version": values.get("X-Plex-Platform-Version", ""),
            "X-Plex-Device-Name": values.get("X-Plex-Device-Name", ""),
            "X-Plex-Device": values.get("X-Plex-Device", "PC"),
            "X-Plex-Token": self.claim.token,
        }

    def _fetch(self):
        account_id = self.claim.ensure_account_id()
        if account_id is None:
            raise RuntimeError("account id unavailable for scrobbling service")
        query = urllib.parse.urlencode([("auth_token", self.claim.token)])
        request = urllib.request.Request(plextv_url(SERVICES_URL + "?" + query),
                                         headers=self._headers())
        with self._open(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        service = None
        for user in root.iter("user"):
            if str(user.get("id")) != str(account_id):
                continue
            service = next((node for node in user.iter("service")
                            if node.get("identifier") == "scrobbling"), None)
            break
        if service is None or not service.get("token"):
            raise RuntimeError("scrobbling service credential absent")
        bearer = _decrypt_scrobbling_token(service.get("token"), self._decrypt_secret())
        try:
            parts = bearer.split(".")
            header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except Exception as error:
            raise RuntimeError("decrypted scrobbling credential is not a JWT") from error
        if (len(parts) != 3 or header.get("alg") != "RS256"
                or not isinstance(payload.get("aud"), str)
                or not isinstance(payload.get("iss"), str)
                or str(payload.get("userId")) != str(account_id)
                or not isinstance(payload.get("exp"), int)):
            raise RuntimeError("decrypted scrobbling JWT has an unexpected shape")
        if payload["exp"] <= self._clock():
            raise RuntimeError("decrypted scrobbling JWT is expired")
        return bearer, payload["exp"]

    def __call__(self):
        with self._lock:
            if self._bearer and self._expires > self._clock() + 30:
                return self._bearer
            self._bearer, self._expires = self._fetch()
            return self._bearer


class WatchStateSync:
    """One measured cloud refresh plus an optional periodic daemon."""

    def __init__(self, claim, library, bearer_provider, *, interval=DEFAULT_INTERVAL,
                 opener=urllib.request.urlopen):
        self.claim = claim
        self.library = library
        self._bearer_provider = bearer_provider
        self._interval = interval
        self._open = opener
        self._stop = threading.Event()
        self._thread = None
        self.last_ok = None
        self.failures = 0

    def _server_headers(self):
        headers = self.claim._headers()
        headers.update({
            "User-Agent": "PlexMediaServer/%s" % self.claim.version,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "X-Plex-Token": self.claim.token,
        })
        return headers

    def _read_json(self, request):
        with self._open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh(self):
        """Import one consent/page pair and return its applied-row count."""
        if not self.claim.claimed:
            return 0
        account_id = self.claim.ensure_account_id()
        if account_id is None:
            return 0

        consent = self._read_json(urllib.request.Request(
            plextv_url(CONSENT_URL % account_id),
            headers=self._server_headers(),
        ))
        # Every measured response was consent=true and deletionRequestedAt=null.
        # Refusing any other shape avoids importing state the account has revoked.
        if consent.get("consent") is not True or consent.get("deletionRequestedAt") is not None:
            logger.info("cloud watch-state sync is not consented")
            return 0

        bearer = self._bearer_provider()
        if not bearer:
            raise RuntimeError("no scrobbling bearer available")
        query = urllib.parse.urlencode([
            ("after", self.claim.view_state_cursor or ""),
            ("count", 100),
        ])
        page = self._read_json(urllib.request.Request(
            plextv_url((STATE_URL % account_id) + "?" + query),
            headers={
                "User-Agent": "PlexMediaServer/%s" % self.claim.version,
                "Accept": "*/*",
                "Authorization": "Bearer %s" % bearer,
                "Content-Type": "application/json",
                "X-Plex-Version": self.claim.version,
                "X-Plex-Product": "Plex Media Server",
            },
        ))

        applied = 0
        for state in page.get("items") or []:
            if self.library.import_account_state(state):
                applied += 1

        info = page.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if isinstance(cursor, str) and cursor:
            # Last on purpose: a crash while applying a page must replay it, not
            # silently skip state that was never committed to the catalog.
            self.claim.save_view_state_cursor(cursor)
        if info.get("hasNextPage") is True:
            logger.warning("cloud state returned an unmeasured multi-page response")
        self.last_ok = __import__("time").time()
        self.failures = 0
        return applied

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run,
                                        name="simplex-account-state", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as error:
                self.failures += 1
                logger.error("cloud watch-state sync failed (%d in a row): %s",
                             self.failures, error)
            self._stop.wait(self._interval)
