#!/usr/bin/env python3
"""Fetch one path from the reference (kids), plexsim, and optionally fun; print
status/content-type and a structural diff of JSON bodies.

usage: cmp.py [-X METHOD] [-H 'k: v']... [--xml] [--fun] [--raw N] [--sim-path P] [--ref-path P] [--full] PATH
"""
import os
import sys, json, argparse, urllib.request, urllib.error, gzip

REF = ("http://kids:32400", os.environ["PLEXLAB_TOKEN"])
FUN = ("http://fun:32400", os.environ["FUN_TOKEN"])
SIM = (os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32512"), None)

HDR = {
    "X-Plex-Client-Identifier": "plexsim-cycle5-probe",
    "X-Plex-Product": "Plex Web",
    "X-Plex-Version": "4.147.1",
    "X-Plex-Platform": "Chrome",
    "X-Plex-Device": "OSX",
    "X-Plex-Device-Name": "Chrome",
    "User-Agent": "Mozilla/5.0 (Macintosh) Chrome/128 plexsim-probe",
}


def fetch(base, token, path, method="GET", headers=None, body=None, xml=False):
    h = dict(HDR)
    if not xml:
        h["Accept"] = "application/json"
    if token:
        h["X-Plex-Token"] = token
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, method=method, headers=h, data=body)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        st = r.status
        hd = dict(r.headers)
        b = r.read()
    except urllib.error.HTTPError as e:
        st = e.code
        hd = dict(e.headers)
        b = e.read()
    except Exception as e:
        return None, {}, repr(e).encode()
    if hd.get("Content-Encoding") == "gzip":
        b = gzip.decompress(b)
    return st, hd, b


def flat(o, p="", out=None, maxlist=3):
    if out is None:
        out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            flat(v, f"{p}.{k}" if p else k, out, maxlist)
    elif isinstance(o, list):
        out[p + "[]len"] = len(o)
        for i, v in enumerate(o[:maxlist]):
            flat(v, f"{p}[{i}]", out, maxlist)
    else:
        out[p] = o
    return out


def diff(a, b, la="ref", lb="sim", maxlist=3, ignore=()):
    fa, fb = flat(a, maxlist=maxlist), flat(b, maxlist=maxlist)
    only_a = [k for k in fa if k not in fb]
    only_b = [k for k in fb if k not in fa]
    vd = [(k, fa[k], fb[k]) for k in fa if k in fb and fa[k] != fb[k]]
    def skip(k):
        return any(i in k for i in ignore)
    only_a = [k for k in only_a if not skip(k)]
    only_b = [k for k in only_b if not skip(k)]
    vd = [x for x in vd if not skip(x[0])]
    if only_a:
        print(f"  only in {la} ({len(only_a)}):")
        for k in only_a:
            print(f"    {k} = {json.dumps(fa[k])[:120]}")
    if only_b:
        print(f"  only in {lb} ({len(only_b)}):")
        for k in only_b:
            print(f"    {k} = {json.dumps(fb[k])[:120]}")
    if vd:
        print(f"  value diffs ({len(vd)}):")
        for k, x, y in vd:
            print(f"    {k}: {la}={json.dumps(x)[:100]}  {lb}={json.dumps(y)[:100]}")
    if not (only_a or only_b or vd):
        print("  IDENTICAL (structure + values)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("-X", default="GET")
    ap.add_argument("-H", action="append", default=[])
    ap.add_argument("--xml", action="store_true")
    ap.add_argument("--fun", action="store_true")
    ap.add_argument("--no-ref", action="store_true")
    ap.add_argument("--no-sim", action="store_true")
    ap.add_argument("--raw", type=int, default=0, help="print N chars of each body")
    ap.add_argument("--sim-path")
    ap.add_argument("--ref-path")
    ap.add_argument("--fun-path")
    ap.add_argument("--maxlist", type=int, default=3)
    ap.add_argument("--ignore", action="append", default=[])
    ap.add_argument("--body", default=None)
    a = ap.parse_args()
    headers = {}
    for h in a.H:
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()
    body = a.body.encode() if a.body is not None else None
    res = {}
    targets = []
    if not a.no_ref:
        targets.append(("ref", REF, a.ref_path or a.path))
    if not a.no_sim:
        targets.append(("sim", SIM, a.sim_path or a.path))
    if a.fun:
        targets.append(("fun", FUN, a.fun_path or a.path))
    for name, (base, tok), path in targets:
        st, hd, b = fetch(base, tok, path, a.X, headers, body, a.xml)
        ct = hd.get("Content-Type", "")
        interesting = {k: v for k, v in hd.items() if k.lower() in ("content-type", "content-disposition", "content-range", "cache-control", "content-length", "x-plex-content-original-length", "location", "etag", "last-modified", "accept-ranges", "connection")}
        print(f"[{name}] {a.X} {path} -> {st} {ct} len={len(b)}")
        print(f"       {interesting}")
        j = None
        if b and "json" in ct:
            try:
                j = json.loads(b)
            except Exception as e:
                print("       (json parse failed)", e)
        res[name] = (st, j, b)
        if a.raw:
            print("       BODY:", b[: a.raw].decode("utf-8", "replace"))
    if "ref" in res and "sim" in res:
        rj, sj = res["ref"][1], res["sim"][1]
        if rj is not None and sj is not None:
            print("diff ref vs sim:")
            diff(rj, sj, maxlist=a.maxlist, ignore=a.ignore)
    if "fun" in res and "sim" in res:
        fj, sj = res["fun"][1], res["sim"][1]
        if fj is not None and sj is not None:
            print("diff fun vs sim:")
            diff(fj, sj, "fun", "sim", maxlist=a.maxlist, ignore=a.ignore)


if __name__ == "__main__":
    main()
