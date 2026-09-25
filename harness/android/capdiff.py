"""Diff two Android-client captures by request TEMPLATE.

capdiff.py <ref.jsonl> <ref_port> <sim.jsonl> <sim_port> [--step-ref steps.json --step-sim steps.json]

A template is method + path with every numeric segment replaced by {n} + the
ordered query KEYS (values of id-like keys normalised). For each template on
either side: counts, statuses, content types, and the JSON shape -- the set of
key paths (lists collapsed) with scalar types. Differences are printed per
template; templates only one side issued are listed first, because the client
builds its next request from the previous answer.
"""
import json, re, sys, collections
from urllib.parse import unquote

ID_KEYS = {"key", "ratingKey", "playQueueID", "playQueueItemID", "parentRatingKey",
           "grandparentRatingKey", "url", "uri", "X-Plex-Token", "session", "time",
           "offset", "viewOffset", "t", "machineIdentifier", "X-Plex-Session-Identifier",
           "X-Plex-Client-Identifier", "X-Plex-Playback-Id", "X-Plex-Playback-Session-Id",
           "playbackTime", "duration", "identifier", "sectionId", "mediaIndex", "partIndex",
           "X-Plex-Session-Id", "X-Plex-Language"}


def norm_path(p):
    p = re.sub(r"/\d+(?=/|$)", "/{n}", p)
    p = re.sub(r"/[0-9a-f]{20,}(?=/|$)", "/{h}", p)
    return p


def template(r):
    q = []
    for k, v in r.get("query") or []:
        if k in ID_KEYS or re.fullmatch(r"-?\d+", v or ""):
            q.append(f"{k}=*")
        else:
            q.append(f"{k}={v}")
    return f"{r['method']} {norm_path(r['path'])}?" + "&".join(q)


def shape(obj, pre="", out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            shape(v, f"{pre}.{k}", out)
    elif isinstance(obj, list):
        for v in obj:
            shape(v, f"{pre}[]", out)
    else:
        out.add(f"{pre}:{type(obj).__name__}")
    return out


def load(fn, port):
    by = collections.defaultdict(list)
    for line in open(fn):
        r = json.loads(line)
        if r.get("port") != port:
            continue
        by[template(r)].append(r)
    return by


def summ(rs):
    st = collections.Counter(r.get("status") for r in rs)
    ct = collections.Counter((dict((k.lower(), v) for k, v in r.get("res_headers") or [])).get("content-type", "") for r in rs)
    shp = set()
    for r in rs:
        b = r.get("res_body")
        if isinstance(b, str) and b[:1] in "{[":
            try:
                shp |= shape(json.loads(b))
            except Exception:
                pass
    return st, ct, shp


def main():
    ref, rport, sim, sport = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    A, B = load(ref, rport), load(sim, sport)
    only_a = sorted(set(A) - set(B))
    only_b = sorted(set(B) - set(A))
    print(f"# templates: ref {len(A)}, sim {len(B)}, common {len(set(A) & set(B))}\n")
    print("## only the REFERENCE run issued")
    for t in only_a:
        print(f"- `{t[:220]}` x{len(A[t])} {dict(summ(A[t])[0])}")
    print("\n## only the PLEXSIM run issued")
    for t in only_b:
        print(f"- `{t[:220]}` x{len(B[t])} {dict(summ(B[t])[0])}")
    print("\n## common templates that differ")
    for t in sorted(set(A) & set(B)):
        sa, ca, ha = summ(A[t])
        sb, cb, hb = summ(B[t])
        diffs = []
        if set(sa) != set(sb):
            diffs.append(f"status ref {dict(sa)} sim {dict(sb)}")
        if set(ca) != set(cb):
            diffs.append(f"ctype ref {dict(ca)} sim {dict(cb)}")
        if ha and hb and ha != hb:
            ma, mb = sorted(ha - hb), sorted(hb - ha)
            if ma:
                diffs.append("ref-only keys: " + ", ".join(ma[:40]) + (" ..." if len(ma) > 40 else ""))
            if mb:
                diffs.append("sim-only keys: " + ", ".join(mb[:40]) + (" ..." if len(mb) > 40 else ""))
        if diffs:
            print(f"\n### `{t[:220]}`")
            for d in diffs:
                print("-", d)


main()
