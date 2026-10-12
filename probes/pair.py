#!/usr/bin/env python3
"""Lockstep replay of one request list against the Python oracle and the native server.

usage: pair.py REQS.jsonl [REQS2.jsonl ...] --tree DIR --bin NATIVE [--ports A B]
               [--catalog tests/catalog_ui.json] [--out PREFIX] [--map OLD=NEW,...]
               [--detail] [--max N] [--keep] [--only-get]

Starts both servers fresh on the same catalog (cwd = DIR, no --state, plex.tv pointed at a
closed port, TZ=UTC), sends every request to the oracle and then to the native server over a
raw socket-level client -- same path, same query in the same order, same headers in the same
order -- and diffs each pair with ndiff's normaliser, plus the response header ORDER, which
ndiff's dict comparison cannot see. A request line is either a harness capture (`path`,
`query`, `req_headers`, `req_body`) or a plain {"method", "path", "headers", "body"}.

It prints one signature per distinct difference with its count and first example. The two
recordings are written as PREFIX.py.jsonl and PREFIX.nat.jsonl.
"""
import argparse, base64, collections, gzip, hashlib, http.client, json, os, re, signal
import socket, struct, subprocess, sys, time, urllib.parse, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndiff

DROP = {"host", "content-length", "connection"}


def load(paths, only_get, idmap):
    out = []
    for p in paths:
        for raw in open(p, errors="replace"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if "out" in e or "method" not in e:
                continue
            if "host" in e and "plex.direct" not in (e["host"] or ""):
                continue  # a capture also holds the client's plex.tv traffic
            if "query" in e and isinstance(e.get("query"), list):
                q = urllib.parse.urlencode([tuple(x) for x in e["query"]])
                path = e["path"] + ("?" + q if q else "")
                headers = [(k, v) for k, v in (e.get("req_headers") or [])]
                body = e.get("req_body")
            else:
                path = e.get("path") or e.get("url")
                if path.startswith("http"):
                    s = urllib.parse.urlsplit(path)
                    path = s.path + ("?" + s.query if s.query else "")
                h = e.get("headers") or {}
                headers = list(h.items()) if isinstance(h, dict) else [tuple(x) for x in h]
                body = e.get("body")
            if only_get and e["method"] not in ("GET", "HEAD", "OPTIONS"):
                continue
            for old, new in idmap:
                path = re.sub(r"(/metadata/|/sections/|/playlists/|/collections/)%s(?=[/?,]|$)" % re.escape(old),
                              lambda m: m.group(1) + new, path)
            if isinstance(body, dict) and "b64" in body:
                body = base64.b64decode(body["b64"])
            elif isinstance(body, str):
                body = body.encode("utf-8", "surrogateescape")
            out.append({"method": e["method"], "path": path,
                        "headers": [(k, v) for k, v in headers if k.lower() not in DROP],
                        "body": body, "label": e.get("label") or ""})
    return out


def send(port, r, timeout):
    # a sweep opens one connection per request; on macOS the ephemeral ports run out
    # under TIME_WAIT (EADDRNOTAVAIL, errno 49), which is the client's, not the server's
    for attempt in range(8):
        rec = _send(port, r, timeout)
        if "Errno 49" not in str(rec.get("error") or ""):
            return rec
        time.sleep(0.5 * (attempt + 1))
    return rec


def _send(port, r, timeout):
    rec = {"method": r["method"], "url": "http://x" + r["path"]}
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.putrequest(r["method"], r["path"], skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", "127.0.0.1:%d" % port)
        for k, v in r["headers"]:
            c.putheader(k, v)
        body = r["body"]
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        resp = c.getresponse()
        hl = resp.getheaders()
        ct = (resp.getheader("Content-Type") or "").lower()
        if "event-stream" in ct or resp.status == 101:
            data = b""
        else:
            data = resp.read()
        # close with RST (linger 0): a sweep's hundred thousand client sockets would
        # otherwise sit in TIME_WAIT and exhaust the Mac's ephemeral ports
        try:
            c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except (OSError, AttributeError):
            pass
        c.close()
        rec["status"] = resp.status
        rec["reason"] = resp.reason
        rec["horder"] = [k for k, _ in hl]
        rec["rheaders"] = {}
        for k, v in hl:
            rec["rheaders"][k] = (rec["rheaders"][k] + " || " + v) if k in rec["rheaders"] else v
        rec["wirelen"] = len(data)
        enc = (resp.getheader("Content-Encoding") or "").lower()
        if enc == "gzip" and data:
            try:
                data = gzip.decompress(data)
            except Exception as ex:
                rec["error"] = "gzip: %s" % ex
        elif enc == "deflate" and data:
            data = zlib.decompress(data)
        try:
            rec["rbody"] = data.decode("utf-8")
        except UnicodeDecodeError:
            rec["rbody"] = "<binary %d bytes sha1 %s>" % (len(data), hashlib.sha1(data).hexdigest())
    except Exception as ex:
        rec["status"] = None
        rec["error"] = "%s: %s" % (type(ex).__name__, ex)
    return rec


def wait_port(port, proc, secs=40):
    end = time.time() + secs
    while time.time() < end:
        if proc.poll() is not None:
            raise SystemExit("server on %d exited %s" % (port, proc.returncode))
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("server on %d never listened" % port)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reqs", nargs="+")
    ap.add_argument("--tree", required=True)
    ap.add_argument("--bin", required=True)
    ap.add_argument("--ports", nargs=2, type=int, default=[25710, 25711])
    ap.add_argument("--catalog", default="tests/catalog_ui.json")
    ap.add_argument("--out", default="pair")
    ap.add_argument("--map", default="")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--max", type=int, default=100000)
    ap.add_argument("--keep", action="store_true", help="servers are already running on --ports")
    ap.add_argument("--only-get", action="store_true")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--extra", default="", help="extra server arguments, shell-split")
    a = ap.parse_args()
    idmap = [tuple(x.split("=", 1)) for x in a.map.split(",") if x]
    reqs = load(a.reqs, a.only_get, idmap)
    procs = []
    if not a.keep:
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9", TZ="UTC")
        # No ZURG_URL: every part read is the same routed 502 on both servers.
        for k in ("ZURG_URL", "TMDB_KEY", "REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(k, None)
        import shlex
        # `--no-state` first: a state file is the default, and an --extra `--state` wins.
        tail = ["--catalog", a.catalog, "--assert-claimed", "--gdm", "off", "--no-state"] + shlex.split(a.extra)
        for cmd, port, tag in (([sys.executable, "server.py"], a.ports[0], "py"), ([a.bin], a.ports[1], "nat")):
            log = open("%s.%s.log" % (a.out, tag), "w")
            procs.append(subprocess.Popen(cmd + ["--port", str(port)] + tail, cwd=a.tree, env=env,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        for p, port in zip(procs, a.ports):
            wait_port(port, p)
    groups = collections.OrderedDict()
    try:
        with open(a.out + ".py.jsonl", "w") as fp, open(a.out + ".nat.jsonl", "w") as fn:
            for i, r in enumerate(reqs):
                x = send(a.ports[0], r, a.timeout)
                y = send(a.ports[1], r, a.timeout)
                for f, rec in ((fp, x), (fn, y)):
                    f.write(json.dumps(dict(rec, i=i, label=r["label"])) + "\n")
                diffs = ndiff.compare(x, y)
                if x.get("horder") and y.get("horder"):
                    ox = [h.lower() for h in x["horder"]]
                    oy = [h.lower() for h in y["horder"]]
                    if sorted(ox) == sorted(oy) and ox != oy:
                        diffs.append(("horder", "", ",".join(ox), ",".join(oy)))
                    if x["horder"] != y["horder"] and ox == oy:
                        diffs.append(("hcase", "", ",".join(x["horder"]), ",".join(y["horder"])))
                if x.get("reason") != y.get("reason") and x.get("status") == y.get("status"):
                    diffs.append(("reason", "", str(x.get("reason")), str(y.get("reason"))))
                for kind, where, pv, nv in diffs:
                    where_t = re.sub(r"\[\d+\]", "[]", where)
                    key = (kind, r["method"] + " " + ndiff.template(ndiff.path_of(x["url"])), where_t,
                           (pv + " | " + nv) if kind in ("header", "status", "order", "horder", "hcase", "reason") else "")
                    groups.setdefault(key, []).append((i, ndiff.path_of(x["url"]), "%s -> %s" % (pv, nv)))
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(10)
            except Exception:
                p.kill()
    print("# %d requests, %d difference signatures" % (len(reqs), len(groups)))
    for n, (key, items) in enumerate(groups.items()):
        if n >= a.max:
            break
        print("\n## [%d] %s %s %s" % (len(items), key[0], key[1], key[2]))
        if key[3]:
            print("   ", key[3][:400])
        for i, path, ex in (items if a.detail else items[:1]):
            print("   #%d %s" % (i, path[:200]))
            if ex:
                print("   ", ex[:700].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
