"""Cycle 35: the `Range` header's GRAMMAR, swept off a raw socket. GET-only.

    python3 rangesweep.py out.json

Reads `PLEXLAB_TOKEN` and `ZEN_TOKEN`. 43 header values against both references over a
container (`/identity`) and a `/web` asset, because a ranged CONTAINER answers its length
TWICE (cycle 33) and a naive client takes the first. `urllib` will not send most of these,
so every request is written to the socket by hand. The 172 cells it produces are what
`server.py`'s `_ranged` docstring reproduces; re-run it before changing that parser."""
import json, os, socket, sys
CASES=["bytes=0-10","bytes=0-","bytes=-10","bytes=-0","bytes=0-0","bytes=5-4","bytes=10-5",
 "bytes=1-0","bytes=99999999-","bytes=0-99999999","bytes=0-10,20-30","bytes=0-10,","bytes=,0-10",
 "bytes=abc-def","bytes=abc-10","bytes=0-abc","bytes=","bytes","bytes= 0-10","BYTES=0-10",
 "items=0-10","bytes=+0-10","bytes=00-0010","bytes=-abc","0-10","bytes=--5","bytes=5-abc",
 "bytes=5-","bytes=-1","bytes=0-10-20","bytes=-99999","bytes=1-0","bytes=0- ","bytes=0-1 ",
 "bytes=0-10;x=1","bytes=2-2","bytes=0-1","bytes=-2","bytes= -10","bytes=0 -10","bytes=0-+10",
 "bytes=0--10","bytes=0-10 20","bytes=0-10\t","bytes=x0-10"]

def get(host, port, path, token, rng):
    head = ["GET %s HTTP/1.1" % (path + (("?X-Plex-Token=" + token) if token else "")),
            "Host: h", "User-Agent: curl/8", "Accept: application/json",
            "Accept-Encoding: identity", "Range: " + rng, "Connection: close"]
    s = socket.create_connection((host, port), 15); s.settimeout(20)
    s.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
    blob = b""
    try:
        while True:
            c = s.recv(65536)
            if not c: break
            blob += c
    except socket.timeout:
        pass
    s.close()
    line, _, rest = blob.partition(b"\r\n"); block, _, body = rest.partition(b"\r\n\r\n")
    cr = None
    for l in block.decode("latin-1").split("\r\n"):
        if l.lower().startswith("content-range"):
            cr = l.partition(":")[2].strip()
    return {"status": int(line.decode().split()[1]), "content_range": cr, "body": len(body)}

out = {}
for label, host, port, token, path in (
        ("kids", "kids", 32400, os.environ["PLEXLAB_TOKEN"], "/web/index.html"),
        ("zen", "zen", 32400, os.environ["ZEN_TOKEN"], "/web/index.html"),
        ("kids-id", "kids", 32400, os.environ["PLEXLAB_TOKEN"], "/identity"),
        ("zen-id", "zen", 32400, os.environ["ZEN_TOKEN"], "/identity")):
    out[label] = {"path": path, "cases": {}}
    full = get(host, port, path, token, "bytes=0-")
    out[label]["N"] = int((full["content_range"] or "//0").split("/")[-1])
    for rng in CASES:
        out[label]["cases"][rng] = get(host, port, path, token, rng)
json.dump(out, open(sys.argv[1], "w"), indent=1)
print("wrote", sys.argv[1])
