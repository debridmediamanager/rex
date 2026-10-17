#!/usr/bin/env python3
"""Exercise the native Rust GDM responder over its real UDP sockets.

The recorded references' blocks, the bisected trigger table, the ports and the peer
table are unit-tested beside the responder (`gdm_tests` in `rust/src/runtime.rs`,
driven by `tests/fixtures/gdm/responses.json`); this drives the running server.
"""

import json
import os
import socket
import subprocess
import time
import urllib.request

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QUERY = b"M-SEARCH * "


def wait_http(port):
    for _ in range(80):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=1):
                return True
        except Exception:
            time.sleep(0.1)
    return False


def datagram(port, payload):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
        return sock.recvfrom(4096)
    except socket.timeout:
        return None, None
    finally:
        sock.close()


def free_udp_ports():
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return tuple(sock.getsockname()[1] for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()


def main():
    catalog = os.path.join(HERE, "catalog_ui.json")
    catalog_document = json.load(open(catalog, encoding="utf-8"))
    machine = catalog_document["machineIdentifier"]
    friendly = catalog_document.get("friendlyName", "rex")
    gdm_ports = free_udp_ports()
    tcp = 32597
    proc = start(catalog, tcp, gdm_ports)
    checks = []
    try:
        if not wait_http(tcp):
            print("FAIL native server did not start")
            return 1
        expected_prefix = (
            b"HTTP/1.0 200 OK\r\n"
            b"Content-Type: plex/media-server\r\n"
            b"Name: " + friendly.encode() + b"\r\n"
            b"Port: %d\r\n" % tcp
            + b"Resource-Identifier: " + machine.encode() + b"\r\n"
        )
        expected_version = b"Version: 1.42.2.10156-f737b826c\r\n"
        for port in gdm_ports:
            for label, payload, answer in (
                ("proper query", b"M-SEARCH * HTTP/1.1\r\n", True),
                ("empty datagram", b"", True),
                ("proper prefix", QUERY[:1], True),
                ("wrong eleventh byte", b"M-SEARCH *X", False),
                ("lower case", b"m-search * HTTP/1.1\r\n", False),
            ):
                body, address = datagram(port, payload)
                ok = (body is not None) == answer
                if answer:
                    prefix_ok = body is not None and body.startswith(expected_prefix)
                    prefix_ok = prefix_ok and expected_version in body
                    suffix_ok = body is not None and body.endswith(b"\r\n\r\n")
                    source_ok = address is not None and address[1] == port
                    ok = ok and prefix_ok and suffix_ok and source_ok
                checks.append(ok)
                print("%s udp/%d %s" % ("PASS" if ok else "FAIL", port, label))

        # A real PMS's own search, sent to the group, reaches the responder. A host that
        # does not deliver multicast to itself cannot show it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        probe.settimeout(2.0)
        try:
            probe.sendto(b"M-SEARCH * HTTP/1.1\r\n", ("239.0.0.250", gdm_ports[-1]))
            seen = probe.recvfrom(4096)[0]
        except OSError:
            seen = None
        finally:
            probe.close()
        if seen is None:
            print("SKIP a multicast search reaches the responder "
                  "(this host does not deliver multicast to itself)")
        else:
            ok = seen.startswith(expected_prefix) and seen.endswith(b"\r\n\r\n")
            checks.append(ok)
            print("%s a multicast search reaches the responder" % ("PASS" if ok else "FAIL"))

        request = urllib.request.Request(
            "http://127.0.0.1:%d/servers" % tcp,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.load(response)
        rows = (body.get("MediaContainer") or {}).get("Server") or []
        ok = any(row.get("machineIdentifier") == machine for row in rows)
        checks.append(ok)
        print("%s /servers includes native self row" % ("PASS" if ok else "FAIL"))
    finally:
        stop(proc)

    # `main()`'s `Host:` line: `--plex-direct-host`, else the `--public-url` hostname
    # only when it is a real `*.plex.direct` name (lower-cased, as `urlsplit` gives
    # it), else none -- a tunnel hostname is not what the field carries on either
    # reference.
    for label, extra, wanted in (
        ("a tunnel public URL advertises no Host",
         ("--public-url", "https://Tunnel.Example.COM"), None),
        ("a plex.direct public URL is the Host, lower-cased",
         ("--public-url", "https://10-0-0-2.ABCDEF0123.plex.direct:32400"),
         b"Host: 10-0-0-2.abcdef0123.plex.direct\r\n"),
        ("--plex-direct-host wins over the public URL",
         ("--public-url", "https://Tunnel.Example.COM",
          "--plex-direct-host", "10-0-0-3.fedcba.plex.direct"),
         b"Host: 10-0-0-3.fedcba.plex.direct\r\n"),
    ):
        proc = start(catalog, tcp, gdm_ports, extra)
        try:
            body = datagram(gdm_ports[0], b"M-SEARCH * HTTP/1.1\r\n")[0] \
                if wait_http(tcp) else None
            hosts = [line for line in (body or b"").split(b"\r\n") if line.startswith(b"Host:")]
            ok = body is not None and (hosts == [wanted.rstrip(b"\r\n")] if wanted else not hosts)
            checks.append(ok)
            print("%s %s  %s" % ("PASS" if ok else "FAIL", label, hosts))
        finally:
            stop(proc)
    print("\n%d/%d native GDM checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


def start(catalog, tcp, gdm_ports, extra=()):
    return subprocess.Popen(
        server_command("--port", tcp, "--catalog", catalog, "--gdm", "auto",
                       "--assert-claimed", *extra, root=ROOT),
        cwd=ROOT,
        env=dict(os.environ, 
                 REX_GDM_PORTS=",".join(str(port) for port in gdm_ports)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
