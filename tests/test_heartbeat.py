#!/usr/bin/env python3
"""`heartbeat.py`'s announce against a real PMS's own announce. No network, no server.

The corpus is `tests/fixtures/outbound-announce.json`: the three `PUT /devices/...`
exchanges a real 1.43.4 PMS made to plex.tv, lifted out of the harness's
`capture/outbound.jsonl` on kids with every token replaced. All three are identical,
which is what makes them a specification rather than a sample.

The announce is the only traffic rex sends to plex.tv, and it is what makes a
server DETECTED rather than merely reachable -- so it is emulated like any other
response. This suite drives `announce()` with a stubbed opener and compares the request
it builds with the recorded one; it never opens a socket.

    python3 tests/test_heartbeat.py
"""
import inspect
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "oracle"))
import heartbeat  # noqa: E402
import claim  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "outbound-announce.json")

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-58s %s" % ("PASS" if ok else "FAIL", label, detail))


class _Captured(Exception):
    """Raised by the stub so nothing ever reaches the network."""

    def __init__(self, request):
        self.request = request


def capture(machine, url, **kw):
    """Run announce() with the opener stubbed out and hand back the Request.

    Keyword arguments an older signature does not take are DROPPED rather than
    allowed to raise: a suite that dies on a TypeError cannot tell a wrong request
    from no request, and the fail-then-pass proof needs the old code to build its
    own (wrong) request and be compared with the recording.
    """
    held = {}

    def stub(request, timeout=None):
        raise _Captured(request)

    accepted = dict(kw)
    try:
        names = set(inspect.signature(heartbeat.announce).parameters)
        accepted = {k: v for k, v in kw.items() if k in names}
    except (TypeError, ValueError):
        pass

    real = urllib.request.urlopen
    urllib.request.urlopen = stub
    try:
        heartbeat.announce("PLEX-TOKEN", machine, url, **accepted)
    except _Captured as captured:
        held["request"] = captured.request
    except Exception as error:
        print("      announce() raised before it built a request: %s" % error)
    finally:
        urllib.request.urlopen = real
    return held.get("request")


def main():
    recorded = json.load(open(FIXTURE))
    announces = recorded["announces"]
    check("the recorded announces agree with each other",
          all(a["url"] == announces[0]["url"] and a["query"] == announces[0]["query"]
              and a["headers"] == announces[0]["headers"] for a in announces),
          "%d recorded" % len(announces))
    want = announces[0]
    machine = want["machineIdentifier"]

    built = capture(machine, want["connectionUri"],
                    name=want["deviceName"], version=want["version"],
                    platform=want["platform"], platform_version=want["platformVersion"],
                    device=want["device"], model=want["model"], vendor=want["vendor"])
    check("announce() builds a request at all", built is not None)
    # The parameter's NAME is the contract: rex's took plex.tv's numeric device id,
    # which resolves to whatever device row already holds it and silently overwrites
    # that device. That is the half of this the safety notes have warned about since
    # cycle 7 while the code went on doing it.
    try:
        second = list(inspect.signature(heartbeat.announce).parameters)[1]
    except (TypeError, ValueError, IndexError):
        second = None
    check("announce() takes a machine identifier, not a numeric device id",
          second == "machine_identifier", repr(second))
    if built is None:
        return summary()

    parsed = urllib.parse.urlsplit(built.full_url)
    check("the announce goes to servers.plex.tv, not plex.tv",
          "%s://%s" % (parsed.scheme, parsed.netloc) == want["origin"],
          "%s://%s" % (parsed.scheme, parsed.netloc))
    check("the path segment is the machineIdentifier",
          parsed.path == "/devices/%s" % machine, parsed.path)
    got_query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    want_query = [(k, want["connectionUri"] if k == "Connection[][uri]"
                   else ("PLEX-TOKEN" if k == "X-Plex-Token" else v))
                  for k, v in want["query"]]
    check("the query is the recorded five, in the recorded order",
          got_query == want_query, str(got_query))
    check("the method is PUT with an empty body",
          built.get_method() == "PUT" and (built.data or b"") == b"",
          "%s %r" % (built.get_method(), built.data))

    # urllib title-cases header names, so compare on the lower-cased ones a real PMS
    # sent -- the VALUES and the SET are the measurement, and `content-length` is
    # urllib's own once a body is attached.
    got_headers = [(k.lower(), v) for k, v in built.header_items()]
    want_headers = [(k, v) for k, v in want["headers"] if k != "content-length"]
    got_headers = [(k, v) for k, v in got_headers if k != "content-length"]
    check("every header a real PMS sends is sent, with its value",
          got_headers == want_headers,
          "missing=%s extra=%s" % ([k for k, _ in want_headers if k not in dict(got_headers)],
                                   [k for k, _ in got_headers if k not in dict(want_headers)]))
    check("no Accept: application/xml and no natLoopbackSupported",
          dict(got_headers).get("accept") == "*/*"
          and "natLoopbackSupported" not in parsed.query)

    # httpsEnabled is the server's own capability: the reference sent `1` beside an
    # `http://` uri, so it cannot be read off the scheme.
    plain = capture(machine, "http://10.0.0.1:32400")
    check("httpsEnabled does not follow the uri's scheme",
          dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(plain.full_url).query))
          .get("httpsEnabled") == "1")

    # rex's `/identity` says 1.42.2.10156-rex, so announcing 1.43.3 tells
    # plex.tv a different version from the one every client reads off the server.
    default = getattr(heartbeat, "DEFAULT_VERSION", None)
    check("the default version is the one rex reports",
          isinstance(default, str) and default.startswith("1.42.2"), repr(default))

    # Cycle 46: the reference's public ownerFeatures is exactly the union of its
    # named defaults and the UUID attributes returned by this post-claim read.
    class Response:
        headers = {}  # a real response's; claim.read_body reads Content-Encoding off it

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    calls = []
    bodies = [b'<MyPlex authToken="SERVER-TOKEN" username="owner" email="o@example.test"/>']

    def claim_stub(request, timeout=None):
        calls.append(request)
        return Response(bodies.pop(0))

    real = urllib.request.urlopen
    urllib.request.urlopen = claim_stub
    try:
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "claim.json")
            claimed = claim.Claim("machine", state)
            claimed.exchange("claim-token")
            combined = claimed.owner_features("zeta,alpha")
            reloaded = claim.Claim("machine", state)
            persisted = reloaded.owner_features("zeta,alpha")
    finally:
        urllib.request.urlopen = real
    check("claim exchange persists the owner without blocking on account refresh",
          len(calls) == 1 and claimed.owner == reloaded.owner == "owner")
    check("named defaults survive before the asynchronous account refresh",
          combined == persisted == "alpha,zeta", combined)
    return summary()


def summary():
    passed = sum(1 for _, ok in results if ok)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
