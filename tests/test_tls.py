#!/usr/bin/env python3
"""plexsim's TLS port against what a real PMS does with the same port.

A real PMS MULTIPLEXES: TLS and plain HTTP live on the same TCP port, and the
discriminator is the first byte. Measured 2026-09-18 on zen (1.42.2) and kids (1.43.4),
identical on both -- see `tests/fixtures/tls/reference.json` for the cells.

This suite starts its own plexsim and makes its own throwaway certificate with `openssl`,
so it needs no credential and no network. Run it with `python3 tests/test_tls.py`.
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "tls", "reference.json")

PASS = FAIL = SKIP = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("PASS %s" % label)
    else:
        FAIL += 1
        print("FAIL %s\n  got  %r\n  want %r" % (label, got, want))


def skip(label, why):
    global SKIP
    SKIP += 1
    print("SKIP %s (%s)" % (label, why))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def first_bytes(port, payload, wait=3.0, limit=200):
    sock = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        sock.sendall(payload)
        sock.settimeout(wait)
        buf = b""
        try:
            while len(buf) < limit:
                piece = sock.recv(65536)
                if not piece:
                    break
                buf += piece
        except socket.timeout:
            pass
        return buf
    finally:
        sock.close()


def handshake(port, maximum=None, minimum=None, sni="plexsim.test", alpn=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if maximum:
        context.maximum_version = maximum
    if minimum:
        context.minimum_version = minimum
    if alpn:
        context.set_alpn_protocols(alpn)
    raw = socket.create_connection(("127.0.0.1", port), timeout=6)
    try:
        # `server_hostname=None` is how this client omits the SNI extension entirely;
        # Python refuses an empty string for it.
        wrapped = context.wrap_socket(raw, server_hostname=sni or None)
    except OSError as error:
        raw.close()
        return ("FAIL", type(error).__name__, None)
    try:
        return (wrapped.version(), wrapped.selected_alpn_protocol(), wrapped)
    except Exception:
        wrapped.close()
        raise


def http_over(sock, path="/identity"):
    sock.sendall(("GET %s HTTP/1.1\r\nHost: h\r\n\r\n" % path).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        piece = sock.recv(65536)
        if not piece:
            break
        buf += piece
    return buf.partition(b"\r\n")[0].decode("latin-1")


def serve(port, extra):
    env = dict(os.environ, RD_TOKEN="dummy")
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                        *extra, root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()
            return proc
        except OSError:
            time.sleep(0.25)
    proc.kill()
    raise SystemExit("plexsim did not start on %d" % port)


def main():
    recorded = json.load(open(FIXTURE))
    alert = bytes.fromhex(recorded["malformed_record_alert"])

    if not shutil_which("openssl"):
        skip("the whole TLS suite", "openssl is not on PATH")
        print("\n%d/%d checks passed, %d skipped" % (PASS, PASS + FAIL, SKIP))
        return 0

    work = tempfile.mkdtemp(prefix="plexsim-tls-")
    pem = os.path.join(work, "test.pem")
    key = os.path.join(work, "test.key")
    crt = os.path.join(work, "test.crt")
    subprocess.check_call(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", crt,
         "-days", "2", "-nodes", "-subj", "/CN=*.%s.plex.direct" % ("0" * 32)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(pem, "wb") as handle:
        handle.write(open(crt, "rb").read() + open(key, "rb").read())

    tls_port = free_port()
    plain_port = free_port()
    tls_proc = serve(tls_port, ["--tls-cert", pem])
    plain_proc = serve(plain_port, [])
    try:
        # The system Python on macOS links LibreSSL 2.8.3 and cannot offer TLS 1.3 at
        # all. Keep the handshake/SNI assertions live at its highest version and record
        # the unobservable preference explicitly; an OpenSSL-3 Python still has to prove
        # the measured TLS-1.3 selection.
        negotiated = "TLSv1.3" if ssl.HAS_TLSv1_3 else "TLSv1.2"
        if not ssl.HAS_TLSv1_3:
            skip("TLS 1.3 is preferred", "%s cannot offer TLS 1.3" % ssl.OPENSSL_VERSION)

        # 1. The same port answers plain HTTP and TLS, and the API is the same on both.
        check("the TLS port still answers plain HTTP",
              first_bytes(tls_port, b"GET /identity HTTP/1.1\r\nHost: h\r\n\r\n"
                          ).partition(b"\r\n")[0], b"HTTP/1.1 200 OK")
        version, protocol, sock = handshake(tls_port)
        try:
            check("the same port completes a TLS handshake", version, negotiated)
            check("...and answers the same API over it", http_over(sock), "HTTP/1.1 200 OK")
            # Measured on both references: the server selects NO ALPN protocol, even when
            # the client offers only `h2` -- it does not negotiate the extension at all.
            check("no ALPN protocol is selected", protocol, recorded["alpn"])
        finally:
            sock.close()

        # 2. The discriminator is the FIRST BYTE. `0x16` is a TLS handshake record and
        #    draws a TLS alert when the record is malformed; everything else is HTTP.
        check("a malformed TLS record draws the reference's own alert",
              first_bytes(tls_port, b"\x16\x03\x01\x00\x05" + b"\x00" * 5)[:7], alert)

        # 3. TLS 1.2 negotiates and 1.3 is preferred, as on both references.
        version12, _, sock12 = handshake(tls_port, ssl.TLSVersion.TLSv1_2,
                                         ssl.TLSVersion.TLSv1_2)
        if sock12:
            sock12.close()
        check("TLS 1.2 negotiates", version12, "TLSv1.2")

        # 4. SNI is ignored: the reference answers the same certificate to no SNI and to a
        #    bogus one.
        for label, sni in (("no SNI", ""), ("a bogus SNI", "nonsense.example")):
            version_sni, _, sock_sni = handshake(tls_port, sni=sni)
            if sock_sni:
                sock_sni.close()
            check("%s still completes the handshake" % label, version_sni, negotiated)

        # 5. Without a certificate the port is plain only -- which is what plexsim was.
        check("a server with no --tls-cert answers no TLS",
              handshake(plain_port)[0], "FAIL")
        check("...and still answers plain HTTP",
              first_bytes(plain_port, b"GET /identity HTTP/1.1\r\nHost: h\r\n\r\n"
                          ).partition(b"\r\n")[0], b"HTTP/1.1 200 OK")
    finally:
        tls_proc.kill()
        plain_proc.kill()

    print("\n%d/%d checks passed, %d skipped" % (PASS, PASS + FAIL, SKIP))
    return 1 if FAIL else 0


def shutil_which(name):
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    sys.exit(main())
