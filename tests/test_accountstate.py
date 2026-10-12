#!/usr/bin/env python3
"""Measured claimed-account watch/rating import. No network and no live server."""

import json
import base64
import os
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "oracle"))

import accountstate  # noqa: E402
import claim  # noqa: E402
import library  # noqa: E402
import server  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print("  %s  %-62s %s" % ("PASS" if ok else "FAIL", label, detail))


class Response:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def aes_encrypt_block(block, round_keys):
    state = [value ^ round_keys[0][index] for index, value in enumerate(block)]
    for round_number in range(1, 14):
        state = [accountstate._SBOX[value] for value in state]
        state = accountstate._shift_rows(state)
        state = accountstate._mix_columns(state)
        state = [value ^ round_keys[round_number][index]
                 for index, value in enumerate(state)]
    state = [accountstate._SBOX[value] for value in state]
    state = accountstate._shift_rows(state)
    return bytes(value ^ round_keys[14][index] for index, value in enumerate(state))


def synthetic_service_token(bearer, secret):
    key, iv = accountstate._pms_key_iv(secret)
    round_keys = accountstate._aes256_round_keys(key)
    plaintext = bearer.encode("utf-8")
    count = 16 - (len(plaintext) % 16)
    plaintext += bytes([count]) * count
    ciphertext = bytearray()
    previous = iv
    for index in range(0, len(plaintext), 16):
        block = bytes(left ^ right for left, right in
                      zip(plaintext[index:index + 16], previous))
        encrypted = aes_encrypt_block(block, round_keys)
        ciphertext.extend(encrypted)
        previous = encrypted
    return base64.b64encode(bytes(ciphertext)).decode("ascii")


def b64url(document):
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main():
    # Public NIST AES-256 block vector: this prevents an encrypt/decrypt pair in
    # the synthetic service fixture from merely agreeing on the same wrong AES.
    vector_key = bytes.fromhex(
        "603deb1015ca71be2b73aef0857d7781"
        "1f352c073b6108d72d9810a30914dff4")
    vector_plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    vector_cipher = bytes.fromhex("f3eed1bdb5d2a03c064b5a7e3db181f8")
    check("stdlib AES-256 decrypt matches the NIST block vector",
          accountstate._aes_decrypt_block(
              vector_cipher, accountstate._aes256_round_keys(vector_key)) == vector_plain)

    # Cycle 47: claimed servers refresh the owner first and the feature UUID
    # universe about eleven seconds later, once per day. Both are one atomic
    # last-good update.
    with tempfile.TemporaryDirectory() as refresh_directory:
        refresh_path = os.path.join(refresh_directory, "claim.json")
        refreshed_owner = claim.Claim("refresh-machine", refresh_path)
        refreshed_owner.token = "REFRESH-TOKEN"
        refreshed_owner.account_id = 7
        refreshed_owner.feature_uuids = ["uuid-old"]
        refreshed_owner._save()
        refresh_calls = []
        refresh_bodies = [
            b'<user id="42" username="owner"/>',
            b'<features><feature id="second" uuid="uuid-b"/>'
            b'<feature id="first" uuid="uuid-a"/></features>',
        ]
        now = [1000]
        delays = []

        def refresh_open(request, timeout=None):
            refresh_calls.append(request)
            return Response(refresh_bodies.pop(0))

        refresher = accountstate.AccountRefresh(
            refreshed_owner, opener=refresh_open, clock=lambda: now[0],
            waiter=delays.append)
        first_refresh = refresher.refresh_due()
        early_refresh = refresher.refresh_due()
        now[0] += 86400
        refresh_bodies.extend([
            b'<user id="43" username="owner"/>',
            b'<broken',
        ])
        try:
            refresher.refresh_due()
        except Exception:
            pass
        refresh_reloaded = claim.Claim("refresh-machine", refresh_path)

    refresh_urls = [urllib.parse.urlsplit(call.full_url) for call in refresh_calls]
    check("daily owner refresh reads user then features with measured auth",
          first_refresh and not early_refresh and len(refresh_calls) == 4
          and refresh_urls[0].path == "/api/v2/user"
          and urllib.parse.parse_qsl(refresh_urls[0].query)
          == [("includeSubscriptions", "1"), ("includeProviders", "1")]
          and refresh_calls[0].get_header("X-plex-token") == "REFRESH-TOKEN"
          and refresh_urls[1].path == "/api/v2/features"
          and urllib.parse.parse_qsl(refresh_urls[1].query)
          == [("X-Plex-Token", "REFRESH-TOKEN")]
          and delays == [11, 11])
    check("account id and sorted UUIDs persist as one last-good update",
          refresh_reloaded.account_id == 42
          and refresh_reloaded.feature_uuids == ["uuid-a", "uuid-b"])

    catalog = server.Catalog(os.path.join(ROOT, "tests", "catalog_ui.json"))
    mutable = library.Library(catalog)
    rows = [row for row in catalog.items.values() if row.get("type") == "movie"][:2]
    first, second = rows
    second.update({"viewCount": 4, "viewOffset": 61000, "lastViewedAt": 1,
                   "userRating": 7.0, "lastRatedAt": 2})

    with tempfile.TemporaryDirectory() as directory:
        state_path = os.path.join(directory, "claim.json")
        owner = claim.Claim("machine", state_path, version="1.42.2.10156-rex")
        owner.token = "SERVER-TOKEN"
        owner.account_id = 42
        owner._save()
        calls = []
        clock = 1789591000
        decrypt_secret = bytes(range(32))
        bearer = ".".join([
            b64url({"alg": "RS256"}),
            b64url({"aud": "synthetic-audience", "exp": clock + 3600,
                    "iss": "synthetic-issuer", "userId": 42}),
            base64.urlsafe_b64encode(b"synthetic-signature").decode("ascii").rstrip("="),
        ])
        encrypted = synthetic_service_token(bearer, decrypt_secret)
        services = ("<users><user id=\"42\"><services>"
                    "<service identifier=\"metadata\" status=\"online\" token=\"ignored\"/>"
                    "<service identifier=\"scrobbling\" status=\"online\" secret=\"\" token=\"%s\"/>"
                    "</services></user></users>" % encrypted).encode("utf-8")
        bodies = [
            {"consent": True, "updatedAt": "2026-09-17T00:00:00Z",
             "deletionRequestedAt": None},
            services,
            {"items": [
                {"guid": first["guid"], "viewState": "complete",
                 "viewedAt": 1789591000123, "rating": 8,
                 "ratedAt": 1789591000456},
                {"guid": second["guid"], "viewState": "unplayed",
                 "viewedAt": 1789591000789},
                {"guid": "plex://movie/not-local", "viewState": "complete",
                 "viewedAt": 1789591000999},
            ], "pageInfo": {"hasNextPage": False, "endCursor": "opaque-next"}},
        ]

        def opener(request, timeout=None):
            calls.append(request)
            return Response(bodies.pop(0))

        provider = accountstate.ScrobblingBearerProvider(
            owner, decrypt_secret=decrypt_secret, opener=opener, clock=lambda: clock)
        sync = accountstate.WatchStateSync(owner, mutable, provider, opener=opener)
        applied = sync.refresh()
        cached = provider()
        reloaded = claim.Claim("machine", state_path)

    check("consent, services credential, then state are the only reads", len(calls) == 3)
    consent = urllib.parse.urlsplit(calls[0].full_url)
    service_call = urllib.parse.urlsplit(calls[1].full_url)
    page = urllib.parse.urlsplit(calls[2].full_url)
    check("consent path uses the persisted numeric owner id",
          consent.netloc == "servers.plex.tv"
          and consent.path == "/api/v2/server/users/42/view_state_sync")
    check("consent authenticates with the server token",
          calls[0].get_header("X-plex-token") == "SERVER-TOKEN")
    check("services XML uses measured auth_token query and server token header",
          service_call.netloc == "servers.plex.tv"
          and service_call.path == "/api/v2/server/users/services"
          and urllib.parse.parse_qsl(service_call.query) == [("auth_token", "SERVER-TOKEN")]
          and calls[1].get_header("X-plex-token") == "SERVER-TOKEN")
    check("synthetic encrypted service token becomes an in-memory cached JWT",
          cached == bearer and len(calls) == 3)
    check("state query preserves measured after,count order and values",
          page.netloc == "scrobbles.plex.tv"
          and page.path == "/state/42"
          and urllib.parse.parse_qsl(page.query, keep_blank_values=True)
          == [("after", ""), ("count", "100")])
    check("state authenticates with the short-lived bearer",
          calls[2].get_header("Authorization") == "Bearer " + bearer)
    check("only held catalog GUIDs count as imported", applied == 2, str(applied))
    check("complete imports view time and rating without local-clock drift",
          first.get("viewCount") == 1
          and first.get("lastViewedAt") == 1789591000
          and first.get("userRating") == 8.0
          and first.get("lastRatedAt") == 1789591000
          and "viewOffset" not in first)
    check("unplayed clears view state while absent rating leaves stars unchanged",
          not any(key in second for key in
                  ("viewCount", "viewOffset", "lastViewedAt"))
          and second.get("userRating") == 7.0
          and second.get("lastRatedAt") == 2)
    cleared = mutable.import_account_state({
        "guid": second["guid"], "viewState": "unplayed",
        "viewedAt": 1789591000999, "rating": -1,
        "ratedAt": 1789591000999,
    })
    check("explicit cloud rating -1 clears stars",
          cleared and "userRating" not in second and "lastRatedAt" not in second)
    check("the opaque end cursor persists only after import",
          reloaded.account_id == 42
          and reloaded.view_state_cursor == "opaque-next")

    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
