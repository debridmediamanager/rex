"""Dashboard account isolation through recorded grants: completion history, live plays and restart.

Run with python3 tests/test_dashboard_accounts.py after building rex. No cloud credentials.
The access-token fixture is the real PMS recording reused by test_accounts.py.
Timeline-only live rows match shared_user_s1434.json; Session requires a playback decision.
"""
import http.client
import http.server
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse

from test_accounts import (Server, PlexTv, OWNER, SHARED, SHARED_ID, MACHINE, SERVER_TOKEN,
                           FIXTURES, catalog)
from test_ports import free_port
from startup_wait import poll_until


def ask(server, token, path, query=(), client="dashboard-observer"):
    target = path + "?" + urllib.parse.urlencode(list(query) + [("X-Plex-Token", token)])
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    try:
        connection.request("GET", target, headers={"Accept": "application/json",
            "X-Plex-Client-Identifier": client, "X-Plex-Product": "Plex Web"})
        response = connection.getresponse()
        raw = response.read()
        assert response.status == 200, (path, response.status, raw)
        return json.loads(raw) if raw.startswith(b"{") else {}, len(raw)
    finally:
        connection.close()


def series(server, kind, account=None, lan=None):
    query = [("timespan", "4")]
    if account is not None:
        query.append(("accountID", str(account)))
    if lan is not None:
        query.append(("lan", str(int(lan))))
    document, _ = ask(server, OWNER, "/statistics/" + kind, query)
    return document.get("MediaContainer", {}).get("Statistics" + kind.title(), [])


def beat(server, token, client, state, seconds, session="same-raw-session"):
    return ask(server, token, "/:/timeline", [("ratingKey", "1"), ("key", "/library/metadata/1"),
        ("state", state), ("time", str(seconds * 1000)), ("duration", "100000"),
        ("X-Plex-Session-Identifier", session)], client)


def main():
    work = tempfile.mkdtemp(prefix="rex-dashboard-accounts-")
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    server = None
    try:
        fixture = json.load(open(os.path.join(FIXTURES, "shared_user_s1434.json")))["exchanges"]
        json.dump(catalog(fixture), open(os.path.join(work, "catalog.json"), "w"))
        json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner",
                   "email": "owner@example.com", "feature_uuids": [], "account_id": 316687974,
                   "view_state_cursor": None, "access_tokens": []},
                  open(os.path.join(work, "claim_state.json"), "w"))
        server = Server(work, stub.server_address[1])
        for client in ("owner-only", "shared-only", "shared-device"):
            for token, seconds in ((OWNER, 2), (SHARED, 3)):
                if (client == "owner-only" and token != OWNER) or (client == "shared-only" and token != SHARED):
                    continue
                beat(server, token, client, "playing", 1)
                beat(server, token, client, "stopped", seconds)
        owner = series(server, "media", 1)
        shared = series(server, "media", SHARED_ID)
        assert sum(row["count"] for row in owner) == 2, owner
        assert sum(row["duration"] for row in owner) == 4, owner
        assert sum(row["count"] for row in shared) == 2, shared
        assert sum(row["duration"] for row in shared) == 6, shared
        # Short plays count in Dashboard statistics, but PMS history requires a
        # crossing of 90%, not merely stopping (shared_user_s1434.json: "to the end").
        history, _ = ask(server, OWNER, "/status/sessions/history/all")
        assert history["MediaContainer"]["size"] == 0, history
        assert not history["MediaContainer"].get("Metadata"), history

        def history_rows(token=OWNER, query=()):
            document, _ = ask(server, token, "/status/sessions/history/all", query)
            return document["MediaContainer"].get("Metadata", [])

        # Same raw id, interleaved accounts: the owner's low offset cannot make
        # the shared user's two near-end beats cross; nor may those beats erase
        # the owner's real crossing or leak a completed/closed latch.
        history_session = "same-history-session"
        beat(server, SHARED, "shared-device", "playing", 95, history_session)
        beat(server, OWNER, "shared-device", "playing", 1, history_session)
        beat(server, SHARED, "shared-device", "stopped", 95, history_session)
        assert history_rows() == []
        beat(server, OWNER, "shared-device", "stopped", 95, history_session)
        assert [int(row["accountID"]) for row in history_rows()] == [1]
        beat(server, SHARED, "shared-device", "playing", 1, history_session)
        beat(server, SHARED, "shared-device", "stopped", 95, history_session)
        recorded_history = history_rows()
        assert len(recorded_history) == 2, recorded_history
        assert {int(row["accountID"]) for row in recorded_history} == {1, int(SHARED_ID)}
        # Recorded PMS: the owner sees both accounts, shares see only themselves,
        # even when explicitly asking for the owner's accountID.
        shared_history = [row for row in recorded_history if int(row["accountID"]) == int(SHARED_ID)]
        assert history_rows(SHARED) == shared_history
        assert history_rows(SHARED, [("accountID", "1")]) == shared_history
        assert history_rows(OWNER, [("accountID", str(SHARED_ID))]) == shared_history

        # Concurrent beats with the same session and client must remain two live plays.
        beat(server, OWNER, "shared-device", "playing", 1)
        beat(server, SHARED, "shared-device", "playing", 1)
        sessions, _ = ask(server, OWNER, "/status/sessions")
        live = sessions["MediaContainer"]["Metadata"]
        assert len(live) == 2, live
        assert {int(row["User"]["id"]) for row in live} == {1, int(SHARED_ID)}, live
        # Recorded PMS omits Session for timeline-only playback (shared-user
        # recording, "while the shared user plays"). Establish real decision
        # context before checking the untouched client session identifier.
        assert all("Session" not in row for row in live), live
        for token in (OWNER, SHARED):
            decision, _ = ask(server, token, "/video/:/transcode/universal/decision",
                [("path", "/library/metadata/1"), ("mediaIndex", "0"), ("partIndex", "0"),
                 ("protocol", "http"), ("directPlay", "1"), ("directStream", "1"),
                 ("hasMDE", "1"), ("X-Plex-Client-Profile-Name", "Generic"),
                 ("X-Plex-Session-Identifier", "same-raw-session")],
                "shared-device")
            assert decision["MediaContainer"]["resourceSession"] == "same-raw-session", decision
            current, _ = ask(server, OWNER, "/status/sessions")
            by_account = {int(row["User"]["id"]): row
                          for row in current["MediaContainer"]["Metadata"]}
            assert by_account[1]["Session"]["id"] == "same-raw-session", by_account
            if token == OWNER:
                assert "Session" not in by_account[int(SHARED_ID)], by_account
        sessions, _ = ask(server, OWNER, "/status/sessions")
        live = sessions["MediaContainer"]["Metadata"]
        assert len(live) == 2, live
        assert {int(row["User"]["id"]) for row in live} == {1, int(SHARED_ID)}, live
        assert all(row["Session"]["id"] == "same-raw-session" for row in live), live
        beat(server, OWNER, "shared-device", "stopped", 2)
        sessions, _ = ask(server, OWNER, "/status/sessions")
        remaining = sessions["MediaContainer"]["Metadata"]
        assert [int(row["User"]["id"]) for row in remaining] == [int(SHARED_ID)], remaining
        assert remaining[0]["Session"]["id"] == "same-raw-session", remaining

        # Exact response-byte deltas on a shared device: observer traffic cannot mask a bleed.
        def device_total(account):
            rows = series(server, "bandwidth", account, True)
            devices, _ = ask(server, OWNER, "/devices")
            device = next(row["id"] for row in devices["MediaContainer"]["Device"]
                          if row["clientIdentifier"] == "shared-device")
            return sum(row["bytes"] for row in rows if row["deviceID"] == device)
        for token, account, other in ((OWNER, 1, SHARED_ID), (SHARED, SHARED_ID, 1)):
            before, untouched = device_total(account), device_total(other)
            _, size = ask(server, token, "/identity", client="shared-device")
            assert poll_until(lambda: device_total(account) == before + size, 5)
            assert device_total(other) == untouched
        assert not series(server, "bandwidth", SHARED_ID, False)
        before_restart = series(server, "media", SHARED_ID)
        server.stop()
        server = None

        # Seed actual historical serialized tuples, not a fixture made by serializing rex output.
        # Preserve the real per-account rows while adding one legacy owner WAN bucket/play.
        db = sqlite3.connect(os.path.join(work, "rex.db"))
        at = int(time.time())
        for name, body in (("plays", [at, "movie", 37, 1]),
                           ("byteBuckets", [1, False, [[at, 7777]]]),
                           ("byteBuckets", [1, False, [[at, 8888]], int(SHARED_ID)])):
            collection = "state:" + name
            db.execute("INSERT OR REPLACE INTO doc(name,kind,body) VALUES (?, 'array', NULL)", (collection,))
            db.execute("INSERT INTO row(coll,key,pos,body) VALUES (?, ?, ?, ?)",
                       (collection, "dashboard-account-" + str(len(body)), 1000000 + len(body), json.dumps(body)))
        db.commit()
        db.close()
        server = Server(work, stub.server_address[1])
        assert series(server, "media", SHARED_ID) == before_restart
        assert history_rows() == recorded_history
        assert history_rows(SHARED) == shared_history
        assert any(row["duration"] == 37 and row["accountID"] == 1 for row in series(server, "media", 1))
        assert sum(row["bytes"] for row in series(server, "bandwidth", 1, False)) == 7777
        assert sum(row["bytes"] for row in series(server, "bandwidth", SHARED_ID, False)) == 8888
        server.stop()
        server = Server(work, stub.server_address[1])
        assert history_rows() == recorded_history
        assert history_rows(SHARED) == shared_history
        assert sum(row["bytes"] for row in series(server, "bandwidth", 1, False)) == 7777
        assert sum(row["bytes"] for row in series(server, "bandwidth", SHARED_ID, False)) == 8888
        print("PASS dashboard accounts: history, live sessions, byte ownership, LAN/WAN and legacy restart")
    finally:
        if server is not None:
            server.stop()
        stub.shutdown()
        stub.server_close()
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
