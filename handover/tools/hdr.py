"""Cycle 33: the RESPONSE HEADER block, byte for byte, across kids, zen and plexsim.

Raw sockets: `urllib` and `http.client` normalise header case and order, which is what is being
measured. GET/HEAD/OPTIONS only, and every path is read-only (no WRITE_GET path is ever sent).
Prints, per case, the status line and the header NAMES in order, and flags where kids and zen
agree and plexsim differs.
"""
import json
import os
import socket
import sys

KIDS = ("kids", 32400, os.environ["PLEXLAB_TOKEN"])
ZEN = ("zen", 32400, os.environ["ZEN_TOKEN"])
SIM = ("127.0.0.1", int(os.environ.get("SIM_PORT", "32588")), None)

FORBIDDEN = ("/:/scrobble", "/:/unscrobble", "/:/rate", "/:/progress", "/:/timeline", "/:/plugins",
             "/refresh", "/analyze", "/emptyTrash", "/optimize", "/clean", "/match", "/unmatch",
             "/split", "/merge", "/actions/", "/updater", "/myplex/claim", "/downloadQueue",
             "/playQueues", "/security/token", "/sync", "/diagnostics", "/servers/",
             "/transcode/universal/start", "/transcode/universal/ping",
             "/transcode/universal/stop")


def send(host, port, token, method, path, extra=(), body=b"", raw_line=None):
    for entry in FORBIDDEN:
        assert entry not in path, ("forbidden path", path)
    assert method in ("GET", "HEAD", "OPTIONS"), method
    line = raw_line or ("%s %s HTTP/1.1\r\n" % (method, path))
    head = line + "Host: %s:%d\r\n" % (host, port)
    head += "User-Agent: curl/8\r\nConnection: close\r\n"
    if token:
        head += "X-Plex-Token: %s\r\n" % token
    for name, value in extra:
        head += "%s: %s\r\n" % (name, value)
    head += "\r\n"
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.sendall(head.encode() + body)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if len(b"".join(chunks)) > 400000:
                break
    blob = b"".join(chunks)
    head_bytes, _, rest = blob.partition(b"\r\n\r\n")
    lines = head_bytes.decode("latin-1").split("\r\n")
    return lines[0], [l.split(":", 1)[0] for l in lines[1:] if l], \
        {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}, \
        len(rest)


# (label, method, path, extra headers)
CASES = [
    ("identity-json", "GET", "/identity", (("Accept", "application/json"),)),
    ("identity-xml", "GET", "/identity", ()),
    ("container-json", "GET", "/library/sections", (("Accept", "application/json"),)),
    ("container-gzip", "GET", "/library/sections",
     (("Accept", "application/json"), ("Accept-Encoding", "gzip"))),
    ("container-head", "HEAD", "/library/sections", (("Accept", "application/json"),)),
    ("paged", "GET", "/library/sections/1/all",
     (("Accept", "application/json"), ("X-Plex-Container-Start", "0"),
      ("X-Plex-Container-Size", "1"))),
    ("root", "GET", "/", ()),
    ("401", "GET", "/library/sections", (("X-Plex-Token", ""),)),
    ("403-unknown-segment", "GET", "/zzzz", ()),
    ("404-multi", "GET", "/library/zzzz/zzzz", ()),
    ("404-system", "GET", "/system/zzzz", ()),
    ("400-bad-pair", "GET", "/library/sections?zzz", ()),
    ("500-unknown-sort", "GET", "/library/sections/1/all?sort=zzzz",
     (("Accept", "application/json"),)),
    ("web-302", "GET", "/web", ()),
    ("web-asset", "GET", "/web/index.html", ()),
    ("resource-png", "GET", "/:/resources/movie.png", ()),
    ("options-preflight", "OPTIONS", "/library/sections",
     (("Origin", "https://app.plex.tv"), ("Access-Control-Request-Method", "GET"),
      ("Access-Control-Request-Headers", "x-plex-token"))),
    ("options-bare", "OPTIONS", "/library/sections", ()),
    ("cors-get", "GET", "/library/sections",
     (("Accept", "application/json"), ("Origin", "https://app.plex.tv"))),
]


def per_server(server, cases):
    host, port, token = server
    out = {}
    for label, method, path, extra in cases:
        try:
            out[label] = send(host, port, token, method, path, extra)
        except Exception as error:  # a case that cannot be driven is recorded, not guessed
            out[label] = ("EXC %s" % error, [], {}, 0)
    return out


if __name__ == "__main__":
    kids = per_server(KIDS, CASES)
    zen = per_server(ZEN, CASES)
    sim = per_server(SIM, CASES)
    json.dump({"kids": {k: (v[0], v[1], v[2], v[3]) for k, v in kids.items()},
               "zen": {k: (v[0], v[1], v[2], v[3]) for k, v in zen.items()},
               "sim": {k: (v[0], v[1], v[2], v[3]) for k, v in sim.items()}},
              open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hdr.json"), "w"),
              indent=1)
    for label, _m, _p, _e in CASES:
        k, z, s = kids[label], zen[label], sim[label]
        agree = k[0] == z[0] and k[1] == z[1]
        print("%-22s %-28s %s" % (label, k[0], "refs agree" if agree else "REFS DIFFER"))
        print("   kids %s" % k[1])
        if not agree:
            print("   zen  %s %s" % (z[0], z[1]))
        print("   sim  %-24s %s" % (s[0], s[1]))
