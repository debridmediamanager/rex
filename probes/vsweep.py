#!/usr/bin/env python3
"""Differential vocabulary sweep: the oracle against the native server, cell by cell.

usage: vsweep.py FAMILIES.txt NAMES.json --tree DIR --bin NATIVE [--ports A B]
                 [--values 1,0,abc] [--where query|header] [--method GET] [--jobs 8]
                 [--out PREFIX] [--catalog tests/catalog_ui.json] [--setup REQS.jsonl]

FAMILIES.txt holds one request path per line (its own query included, `#` comments skipped).
Every (family, name, value) cell appends `name=value` to the query -- or sends it as a request
header with --where header -- to a fresh oracle and a fresh native server on the same catalog
and diffs the pair with pair.py's comparison. A family's BASE request (nothing appended) is
diffed first, and a cell reports only what its base did not already show, so one family-level
difference is not repeated once per name.

--setup replays a request list against both servers first (writes that put the fixture in the
state the sweep should read). Only send read-only families: nothing restores state between
cells. Output: PREFIX.cells.jsonl (one line per differing cell) and a grouped summary on stdout.
"""
import argparse, collections, json, os, re, shlex, signal, subprocess, sys, threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndiff, pair

HDRS = [("Accept", "application/json"), ("X-Plex-Token", "sweep"),
        ("X-Plex-Client-Identifier", "vsweep"), ("Accept-Encoding", "gzip")]


def sigs(x, y):
    out = set()
    for kind, where, pv, nv in ndiff.compare(x, y):
        where_t = re.sub(r"\[\d+\]", "[]", where)
        tail = (pv + " | " + nv) if kind in ("header", "status", "order") else ""
        out.add((kind, where_t, tail[:200], ("%s -> %s" % (pv, nv))[:300]))
    if x.get("horder") and y.get("horder"):
        ox, oy = [h.lower() for h in x["horder"]], [h.lower() for h in y["horder"]]
        if sorted(ox) == sorted(oy) and ox != oy:
            out.add(("horder", "", ",".join(ox) + " | " + ",".join(oy), ""))
    # the two length headers only restate a body difference already in the set
    if any(k not in ("header", "horder") for k, _, _, _ in out):
        out = {s for s in out if not (s[0] == "header" and "-length" in s[1])}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("families")
    ap.add_argument("names")
    ap.add_argument("--tree", required=True)
    ap.add_argument("--bin", required=True)
    ap.add_argument("--ports", nargs=2, type=int, default=[25720, 25721])
    ap.add_argument("--values", default="1")
    ap.add_argument("--where", default="query", choices=["query", "header", "path"])
    ap.add_argument("--method", default="GET")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", default="vsweep")
    ap.add_argument("--catalog", default="tests/catalog_ui.json")
    ap.add_argument("--setup", default=None)
    ap.add_argument("--xml", action="store_true", help="ask for XML instead of JSON")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--oracle", default="oracle/server.py",
                    help="the oracle's script in --tree (a cycle head's server.py is the native shim: "
                         "pass server_oracle.py there)")
    a = ap.parse_args()
    fams = [l.strip() for l in open(a.families) if l.strip() and not l.startswith("#")]
    names = json.load(open(a.names))
    # NAMES.json is a list swept on every family, or {family: [names]} from eff2names.py
    per_family = names if isinstance(names, dict) else None
    values = a.values.split(",")
    hdrs = [h for h in HDRS if not (a.xml and h[0] == "Accept")]
    procs = []
    if not a.keep:
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9", TZ="UTC")
        # No ZURG_URL: every part read is the same routed 502 on both servers.
        for k in ("ZURG_URL", "TMDB_KEY", "REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(k, None)
        # A state file is the default since 2026-10-05: without --no-state both servers kept one beside the
        # catalog and every restart loaded what the sweep (and other legs) had written (cycle 92).
        tail = ["--catalog", a.catalog, "--assert-claimed", "--gdm", "off", "--no-state"]
        for cmd, port, tag in (([sys.executable, a.oracle], a.ports[0], "py"), ([a.bin], a.ports[1], "nat")):
            log = open("%s.%s.log" % (a.out, tag), "w")
            procs.append(subprocess.Popen(cmd + ["--port", str(port)] + tail, cwd=a.tree, env=env,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        for p, port in zip(procs, a.ports):
            pair.wait_port(port, p)
    lock = threading.Lock()
    groups = collections.OrderedDict()
    cells = open(a.out + ".cells.jsonl", "w")
    effects = open(a.out + ".eff.jsonl", "w")
    done = [0]

    def both(path, extra=()):
        r = {"method": a.method, "path": path, "headers": hdrs + list(extra), "body": None}
        return pair.send(a.ports[0], r, 60), pair.send(a.ports[1], r, 60)

    def family(fam):
        # --where path: the family carries one `{}` the name fills; its base is a name nobody routes
        bx, by = both(fam.replace("{}", "zzNoSuchLeaf") if a.where == "path" else fam)
        base = sigs(bx, by)
        basebody = (bx.get("status"), ndiff.mask_text(bx.get("rbody") or ""))
        found, eff = [], []
        for kind, where, tail, ex in base:
            found.append(("", "", kind, where, tail, ex))
        for name in (per_family.get(fam, []) if per_family is not None else names):
            for v in values:
                if a.where == "query":
                    sep = "&" if "?" in fam else "?"
                    x, y = both(fam + sep + urllib.parse.quote(name, safe="[].:-") + "=" + urllib.parse.quote(v, safe=""))
                elif a.where == "path":
                    x, y = both(fam.replace("{}", urllib.parse.quote(name, safe="")))
                else:
                    x, y = both(fam, [(name, v)])
                if (x.get("status"), ndiff.mask_text(x.get("rbody") or "")) != basebody:
                    eff.append((name, v))
                # key a base difference by place, not by value: a parameter that moves the
                # value of an already-differing field is still the base's difference
                basekeys = {(k, w) for k, w, _, _ in base}
                for kind, where, tail, ex in sigs(x, y):
                    if (kind, where) in basekeys:
                        continue
                    found.append((name, v, kind, where, tail, ex))
        with lock:
            ft = ndiff.template(fam)
            for name, v, kind, where, tail, ex in found:
                cells.write(json.dumps({"fam": fam, "name": name, "value": v, "kind": kind,
                                        "where": where, "tail": tail, "ex": ex}) + "\n")
                key = ("BASE" if not name else "cell", ft, kind, where, tail)
                groups.setdefault(key, []).append((name, v, fam, ex))
            cells.flush()
            # the cells that MOVED the oracle's answer: the next stage's denominator
            effects.write(json.dumps({"fam": fam, "eff": eff}) + "\n")
            effects.flush()
            done[0] += 1
            print("family %d/%d %s: %d differing cells" % (done[0], len(fams), fam[:80], len(found)),
                  file=sys.stderr, flush=True)

    try:
        if a.setup:
            for r in pair.load([a.setup], False, []):
                pair.send(a.ports[0], r, 60), pair.send(a.ports[1], r, 60)
        with ThreadPoolExecutor(a.jobs) as ex:
            list(ex.map(family, fams))
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
    ncell = (sum(len(per_family.get(f, [])) for f in fams) if per_family is not None
             else len(fams) * len(names)) * len(values)
    print("# %d families x %d names x %d values = %d cells; %d difference signatures"
          % (len(fams), len(names), len(values), ncell, len(groups)))
    for n, (key, items) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][0] != "BASE", kv[0][1]))):
        if n >= a.max:
            break
        nm = sorted({i[0] for i in items})
        print("\n## [%d cells, %d names] %s %s %s %s" % (len(items), len(nm), key[0], key[1], key[2], key[3]))
        if key[4]:
            print("   ", key[4][:300])
        print("    names:", ", ".join(nm[:25]) + (" ..." if len(nm) > 25 else ""))
        print("    e.g. %s=%s on %s" % (items[0][0], items[0][1], items[0][2][:160]))
        print("   ", items[0][3][:500].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
