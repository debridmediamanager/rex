#!/usr/bin/env python3
"""Raw HTTP body boundaries measured on PMS 1.43.4; no credentials required."""
import json
import os
import re
import socket
import subprocess
import tempfile
import unittest

from server_command import register_source, server_command
from startup_wait import poll_until
from test_ports import free_port

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "body_framing_s1434.json")


class BodyFraming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="rex-body-framing-")
        cls.port = free_port()
        catalog = os.path.join(cls.work.name, "catalog.json")
        with open(os.path.join(ROOT, "tests", "catalog_ui.json")) as source:
            document = json.load(source)
        document["machineIdentifier"] = "body-framing"
        with open(catalog, "w") as out:
            json.dump(document, out)
        env = dict(os.environ)
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(name, None)
        env["REX_PLEXTV_URL"] = "http://127.0.0.1:9"
        cls.log = open(os.path.join(cls.work.name, "server.log"), "wb")
        cls.process = subprocess.Popen(server_command(
            "--port", cls.port, "--catalog", catalog, "--state", os.path.join(cls.work.name, "rex.db"),
            "--gdm", "off", "--assert-claimed", root=ROOT), cwd=ROOT, env=env,
            stdout=cls.log, stderr=subprocess.STDOUT)
        def ready():
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=.2):
                    return True
            except OSError:
                return False
        poll_until(ready, 30)
        if register_source(cls.port, "probe", {"url": "http://127.0.0.1:9", "mount": "/body-probe"}) != 200:
            cls.tearDownClass()
            raise AssertionError("could not register the JSON-body consumer")
        with open(FIXTURE) as source:
            cls.recorded = json.load(source)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=20)
        cls.log.close()
        cls.work.cleanup()

    def connect(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def envelope(method, path, framing="Content-Length: 5"):
        return ("%s %s HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n%s\r\n\r\n"
                % (method, path, framing)).encode()

    @staticmethod
    def receive(conn):
        data = bytearray()
        while True:
            try:
                block = conn.recv(65536)
            except ConnectionResetError:
                break
            if not block:
                break
            data.extend(block)
        return bytes(data)

    @staticmethod
    def statuses(data):
        return [int(code) for code in re.findall(rb"HTTP/1\.[01] (\d{3})", data)]

    def test_measured_partial_hubs_answers_without_waiting_for_body(self):
        for measured in self.recorded["hubs"]:
            with self.subTest(body_bytes=measured["body_bytes"]):
                conn = self.connect()
                conn.sendall(self.envelope("POST", "/hubs") + b"hello"[:measured["body_bytes"]])
                # PMS answers and closes while the declared body is still absent/short.
                self.assertEqual(self.statuses(self.receive(conn)), measured["statuses"])

    def test_buffered_body_preserves_the_next_request(self):
        for framing, body in [("Content-Length: 5", b"hello"),
                              ("Transfer-Encoding: chunked", b"5\r\nhello\r\n0\r\n\r\n")]:
            with self.subTest(framing=framing):
                conn = self.connect()
                conn.sendall(self.envelope("POST", "/hubs", framing) + body +
                             self.envelope("GET", "/identity", "Connection: close"))
                self.assertEqual(self.statuses(self.receive(conn)), self.recorded["pipeline_statuses"])

    def test_late_body_cannot_become_another_request(self):
        conn = self.connect()
        conn.sendall(self.envelope("POST", "/hubs") + b"he")
        first = conn.recv(65536)
        self.assertEqual(self.statuses(first), [200])
        try:
            conn.sendall(b"llo" + self.envelope("GET", "/identity", "Connection: close"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.assertEqual(self.statuses(first + self.receive(conn)), [200])

    def test_hubs_keeps_envelope_validation(self):
        for framing in ("Content-Length: 16777217", "Transfer-Encoding: chunked"):
            with self.subTest(framing=framing):
                conn = self.connect()
                body = b"not-hex\r\n" if framing.startswith("Transfer") else b""
                conn.sendall(self.envelope("POST", "/hubs", framing) + body)
                self.assertEqual(self.statuses(self.receive(conn)), [400])

    def test_consuming_routes_do_not_dispatch_a_partial_body(self):
        for method, path in [("POST", "/library/metadata/1007/subtitles?title=probe.srt&format=srt"),
                             ("PUT", "/rex/sources/probe"), ("POST", "/rex/feed/probe/items")]:
            with self.subTest(path=path):
                conn = self.connect()
                conn.sendall(self.envelope(method, path) + b"he")
                conn.settimeout(.25)
                with self.assertRaises(socket.timeout):
                    conn.recv(1)
                # Finish the incomplete connection without imposing a route-specific EOF status.
                conn.shutdown(socket.SHUT_WR)
                conn.settimeout(2)
                self.receive(conn)


if __name__ == "__main__":
    unittest.main()
