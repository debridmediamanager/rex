"""The HTTP ENVELOPE prober: what a server does with a request the API never sees.

    PLEXLAB_TOKEN=... ZEN_TOKEN=... PLEXSIM_BASE=http://127.0.0.1:32546 \
        python3 envelope.py [kids|zen|sim ...]

Cycle 27 built this after twenty-six cycles had taken the whole envelope from Python's
`BaseHTTPRequestHandler` without asking what a real PMS answers. It speaks raw sockets
rather than `urllib`, because every case here is a request `urllib` will not send: a
header block of 200 KB, a URL of 32769 bytes, `GET  /identity` with two spaces, an
HTTP/0.9 request line, `Content-Length: abc`.

It is READ-ONLY by construction -- every request is a GET or a HEAD of `/identity`,
which is the one path a real PMS answers without a token -- so it is safe to point at
kids and at zen. Nothing here writes, and the one POST case targets `/hubs`, the
plug-in dispatcher, which has no side effect on any server.

Three servers at a time is the point (§10): report only where the two references AGREE
and plexsim differs, which filters out every version difference automatically.
"""
import os
import socket
import sys
import time

TARGETS = {
    "kids": ("kids", 32400, os.environ.get("PLEXLAB_TOKEN", "")),
    "zen": ("zen", 32400, os.environ.get("ZEN_TOKEN", "")),
    "sim": None,  # filled from PLEXSIM_BASE
}


def _sim():
    base = os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32546")
    hostport = base.split("//", 1)[-1]
    host, _, port = hostport.partition(":")
    return host, int(port or 32400), ""


def send(host, port, payload, timeout=8, tries=3, want=300):
    """One raw exchange. Returns the status line, or NO-RESPONSE, or ERR:<reason>."""
    for _ in range(tries):
        try:
            sock = socket.create_connection((host, port), 10)
            sock.settimeout(timeout)
            sock.sendall(payload)
            got = b""
            try:
                while len(got) < want:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            except socket.timeout:
                pass
            sock.close()
            return got.split(b"\r\n", 1)[0].decode("latin-1") if got else "NO-RESPONSE"
        except Exception as error:                      # a reset is a real answer here
            last = "%s" % error
            time.sleep(0.4)
    return "ERR:" + last


def cases(host, port, token):
    auth = ("X-Plex-Token: %s\r\n" % token).encode() if token else b""
    line = lambda verb, url, version=b"HTTP/1.1": (                       # noqa: E731
        b"%s %s %s" % (verb, url, version))

    def request(first, extra=b"", body=b"", ending=b"\r\n"):
        return (first + ending + b"Host: h" + ending + auth.replace(b"\r\n", ending)
                + extra + b"Connection: close" + ending + ending + body)

    def headers(count, value_len=1):
        return b"".join(b"X-P%05d: %s\r\n" % (i, b"v" * value_len) for i in range(count))

    url = lambda n: b"/identity?p=" + b"x" * (n - len("/identity?p="))    # noqa: E731
    fixed = 2 + (1 if token else 0)                       # Host, Connection [, token]

    return {
        # -- the caps, each bisected in cycle 27 to the byte on kids and zen
        "name 1024": request(line(b"GET", b"/identity"),
                             b"X" + b"a" * 1023 + b": v\r\n"),
        "name 1025": request(line(b"GET", b"/identity"),
                             b"X" + b"a" * 1024 + b": v\r\n"),
        "value 4096": request(line(b"GET", b"/identity"),
                              b"X-Probe: " + b"v" * 4096 + b"\r\n"),
        "value 4097": request(line(b"GET", b"/identity"),
                              b"X-Probe: " + b"v" * 4097 + b"\r\n"),
        # The cap is on the TOTAL line count, so the fixed lines this request already
        # carries -- Host, Connection and the token when there is one -- come off first,
        # or a tokened reference is compared with an untokened plexsim one line apart.
        "100 headers": request(line(b"GET", b"/identity"), headers(100 - fixed)),
        "101 headers": request(line(b"GET", b"/identity"), headers(101 - fixed)),
        "200 KB of headers": request(line(b"GET", b"/identity"), headers(50, 4000)),
        "one 70 KB line": request(line(b"GET", b"/identity"),
                                  b"X-Big: " + b"v" * 70000 + b"\r\n"),
        "url 32768": request(line(b"GET", url(32768))),
        "url 32769": request(line(b"GET", url(32769))),
        # -- the request line's grammar
        "two spaces": request(b"GET  /identity HTTP/1.1"),
        "trailing space": request(b"GET /identity HTTP/1.1 "),
        "tab separators": request(b"GET\t/identity\tHTTP/1.1"),
        "asterisk form": request(b"OPTIONS * HTTP/1.1"),
        "LF only": request(line(b"GET", b"/identity"), ending=b"\n"),
        "HTTP/2.0": request(line(b"GET", b"/identity", b"HTTP/2.0")),
        "HTTP/9.9": request(line(b"GET", b"/identity", b"HTTP/9.9")),
        "HTTP/1": request(line(b"GET", b"/identity", b"HTTP/1")),
        "HTTP/0.9": b"GET /identity\r\n\r\n",
        "lower-case verb": request(b"get /identity HTTP/1.1"),
        "unknown verb": request(b"FROB /identity HTTP/1.1"),
        # -- the body
        "CL abc": request(line(b"GET", b"/identity"), b"Content-Length: abc\r\n"),
        "CL -1": request(line(b"GET", b"/identity"), b"Content-Length: -1\r\n"),
        "CL 0": request(line(b"GET", b"/identity"), b"Content-Length: 0\r\n"),
        "CL abc on a POST": request(line(b"POST", b"/hubs"),
                                    b"Content-Length: abc\r\n"),
        "GET with a body": request(line(b"GET", b"/identity"),
                                   b"Content-Length: 5\r\n", b"hello"),
        "chunked": request(line(b"POST", b"/hubs"),
                           b"Transfer-Encoding: chunked\r\n", b"0\r\n\r\n"),
        # -- the connection
        "pipelined x2": (b"GET /identity HTTP/1.1\r\nHost: h\r\n" + auth + b"\r\n"
                         + b"GET /identity HTTP/1.1\r\nHost: h\r\n" + auth
                         + b"Connection: close\r\n\r\n"),
        "HTTP/1.0 keep-alive": (b"GET /identity HTTP/1.0\r\nHost: h\r\n" + auth
                                + b"Connection: keep-alive\r\n\r\n"),
        "Upgrade h2c": request(line(b"GET", b"/identity"), b"Upgrade: h2c\r\n"),
    }


def main():
    wanted = sys.argv[1:] or ["kids", "zen", "sim"]
    targets = {}
    for name in wanted:
        targets[name] = _sim() if name == "sim" else TARGETS[name]
    rows = {}
    for name, (host, port, token) in targets.items():
        for label, payload in cases(host, port, token).items():
            rows.setdefault(label, {})[name] = send(host, port, payload)
    width = max(len(label) for label in rows)
    for label, answers in rows.items():
        cells = "  ".join("%s=%s" % (name, answers.get(name, "-")[:28])
                          for name in wanted)
        both = [answers.get(n) for n in ("kids", "zen") if n in answers]
        flag = ""
        if len(both) == 2 and both[0] == both[1] and answers.get("sim") not in (None, both[0]):
            flag = "   <<< references agree, plexsim differs"
        print("%-*s  %s%s" % (width, label, cells, flag))


if __name__ == "__main__":
    main()
