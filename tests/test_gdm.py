#!/usr/bin/env python3
"""gdm.py against what four real Plex Media Servers answer on udp/32410 and udp/32414.

No server and no network: the fixture in tests/fixtures/gdm/ is the exact bytes each
reference answered on 2026-09-18, and the trigger table is the byte-by-byte bisection
run against zen (1.42.2). Run it with `python3 tests/test_gdm.py`.
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gdm  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gdm")

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        print("FAIL %s\n  got  %r\n  want %r" % (name, got, want))


def main():
    recorded = json.load(open(os.path.join(FIXTURES, "responses.json")))

    # 1. Every recorded reference block is reproduced byte for byte from its own
    #    advertised values. The claimed pair carries Host, the unclaimed pair does not
    #    -- the line is ABSENT, not empty.
    for entry in recorded["servers"]:
        want = entry["response"].encode("utf-8")
        got = gdm.build_response(
            name=entry["name"], port=entry["port"], machine=entry["machine"],
            version=entry["version"], host=entry.get("host"),
            updated_at=entry["updated_at"])
        check("response bytes: %s (%s, %s)" % (
            entry["label"], entry["version"],
            "claimed" if entry.get("host") else "unclaimed"), got, want)

    # 2. The trigger is a prefix compare over min(len(datagram), 11), bisected on zen.
    for case in recorded["trigger"]:
        payload = bytes.fromhex(case["hex"])
        check("trigger %s" % case["label"], gdm.matches(payload), case["answers"])

    # 3. The ports and the group are the measured ones. 32412/32413 are the PLAYER
    #    side and a server neither binds nor answers them.
    check("ports", gdm.PORTS, (32410, 32414))
    check("group", gdm.GROUP, "239.0.0.250")
    check("query constant", gdm.QUERY, b"M-SEARCH * ")

    # 4. Drive the real responder over a real socket on an ephemeral port pair, so
    #    the trigger, the reply and the source port are exercised end to end.
    free = []
    for _ in range(2):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        free.append(probe.getsockname()[1])
        probe.close()
    disco = gdm.Discovery(name="plexsim-test", port=32400, machine="m" * 40,
                          version="1.42.2.10156-plexsim", ports=tuple(free))
    disco.updated_at = 1789000000
    assert disco.start(), "GDM responder did not bind"
    try:
        time.sleep(0.2)
        want = gdm.build_response(name="plexsim-test", port=32400, machine="m" * 40,
                                  version="1.42.2.10156-plexsim", updated_at=1789000000)
        for port in free:
            for label, payload, expect_reply in (
                    ("client query", b"M-SEARCH * HTTP/1.1\r\n", True),
                    ("empty datagram", b"", True),
                    ("bare M", b"M", True),
                    ("eleventh byte wrong", b"M-SEARCH *X", False),
                    ("lower case", b"m-search * HTTP/1.0\r\n\r\n", False),
            ):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(1.0)
                sock.sendto(payload, ("127.0.0.1", port))
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    data, addr = None, None
                sock.close()
                if expect_reply:
                    check("udp/%d %s -> block" % (port, label), data, want)
                    # The reply comes back from the port it was sent to.
                    check("udp/%d %s -> source port" % (port, label),
                          addr[1] if addr else None, port)
                else:
                    check("udp/%d %s -> silence" % (port, label), data, None)

        # 5. The peer table: a real PMS's /servers is its own row plus what its own
        #    5-second search found. Injected here, so the check does not depend on the
        #    test host's multicast delivery; the live path is checked below.
        peer = gdm.build_response(name="other", port=32400, machine="p" * 40,
                                  version="1.43.4.10903-e5521bd8c",
                                  host="abc.plex.direct", updated_at=1789000001)
        disco._record(peer, ("10.9.8.7", 32414))
        rows = disco.peers()
        check("a peer's answer becomes a /servers row",
              [(r["machine"], r["name"], r["address"], r["port"], r["version"]) for r in rows],
              [("p" * 40, "other", "10.9.8.7", 32400, "1.43.4.10903-e5521bd8c")])
        # Its own reply is dropped: zen lists itself ONCE.
        disco._record(disco.response(), ("127.0.0.1", 32414))
        check("the server's own answer is not a peer", len(disco.peers()), 1)
        # A player answers `plex/media-player` on the player ports; it is not a server.
        player = peer.replace(b"plex/media-server", b"plex/media-player")
        disco._record(player, ("10.9.8.6", 32414))
        check("a media-player answer is not a server", len(disco.peers()), 1)
        check("a non-GDM datagram is not a server",
              gdm.parse_response(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"), None)
        # SERVERS_TTL ages a row out.
        check("a peer ages out", disco.peers(now=time.time() + gdm.SERVERS_TTL + 1), [])

        # 6. What a real PMS sends, byte for byte, and how often.
        check("the search datagram", gdm.SEARCH, b"M-SEARCH * HTTP/1.1\r\n")
        check("the search interval", gdm.SEARCH_INTERVAL, 5.0)
        # Its own search answers its own responder, which is how the live path is proved
        # without a second machine. A host with no multicast loopback cannot show it.
        seen = []
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        probe.settimeout(2.0)
        try:
            probe.sendto(gdm.SEARCH, (gdm.GROUP, free[-1]))
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    data, addr = probe.recvfrom(4096)
                except socket.timeout:
                    break
                seen.append(data)
        except OSError:
            pass
        finally:
            probe.close()
        if seen:
            check("a multicast search reaches the responder", seen[0], want)
        else:
            print("SKIP a multicast search reaches the responder "
                  "(this host does not deliver multicast to itself)")

        # 7. A rename re-stamps the advertisement, because Name is one of its fields.
        before = disco.updated_at
        disco.name = "renamed"
        disco.touch()
        check("touch moves Updated-At", disco.updated_at > before, True)
        check("touch keeps the new name",
              b"Name: renamed\r\n" in disco.response(), True)
    finally:
        disco.stop()

    print("\n%d/%d checks passed" % (PASS, PASS + FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
