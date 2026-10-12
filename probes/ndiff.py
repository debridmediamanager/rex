#!/usr/bin/env python3
"""Every-exchange diff of two rec.py logs with volatile values normalised.

usage: ndiff.py PY.jsonl NATIVE.jsonl [--detail] [--grep TEXT] [--sig SUBSTR] [--max N]

Aligns checks by (label, occurrence) and requests by position within a check, then
compares status, response headers (minus Date and friends) and bodies. JSON bodies
are compared structurally (values, key order, lengths); text/XML bodies line by line
after the XML is split per element. Timestamps, epoch values and dates are masked.
Prints a grouped summary: one signature per distinct difference, with its count and
first example; --detail prints every occurrence."""
import argparse, collections, json, re, sys, urllib.parse

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^\s*(PASS|FAIL|SKIP)\s+(.*)$")
EPOCH = re.compile(r"(?<![0-9.])1[6-9][0-9]{8}(?:[0-9]{3})?(?![0-9])")
DATES = [re.compile(p) for p in (
    r"[A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d GMT",
    r"[A-Z][a-z]{2} \d\d, \d{4} \d\d:\d\d:\d\d(\.\d{3})?",
    r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?Z?",
    r"\b\d\d:\d\d:\d\d\b",
)]
SKIP_HEADERS = {"date", "x-plex-protocol"}


def mask_text(s):
    s = EPOCH.sub("<T>", s)
    for d in DATES:
        s = d.sub("<D>", s)
    return s


def mask(v):
    if isinstance(v, dict):
        return {k: mask(x) for k, x in v.items()}
    if isinstance(v, list):
        return [mask(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        if 1.6e9 <= v <= 2.1e9 or 1.6e12 <= v <= 2.1e12:
            return "<T>"
        return v
    if isinstance(v, str):
        return mask_text(v)
    return v


def load(path):
    checks, pending, seen = [], [], {}
    for raw in open(path, errors="replace"):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if "out" in e:
            m = LINE.match(ANSI.sub("", e["out"]))
            if not m:
                continue
            verdict, rest = m.groups()
            label = rest[:52].rstrip()
            n = seen.get(label, 0)
            seen[label] = n + 1
            checks.append({"verdict": verdict, "label": label, "n": n,
                           "line": rest.strip(), "reqs": pending})
            pending = []
        else:
            pending.append(e)
    return checks


def path_of(url):
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = [(k, "<tok>" if "token" in k.lower() else v) for k, v in q]
    return p.path + ("?" + urllib.parse.urlencode(q) if q else "")


def template(path):
    return re.sub(r"\d+", "N", path.split("?")[0])


def short(v, n=140):
    s = json.dumps(v) if not isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[:n] + "..."


def jdiff(a, b, path="", out=None, limit=30):
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k not in b:
                out.append(("-", "%s.%s" % (path, k), short(a[k]), ""))
        for k in b:
            if k not in a:
                out.append(("+", "%s.%s" % (path, k), "", short(b[k])))
        for k in a:
            if k in b:
                jdiff(a[k], b[k], path + "." + k, out, limit)
        ka = [k for k in a if k in b]
        kb = [k for k in b if k in a]
        if ka != kb:
            out.append(("order", path or ".", str(ka), str(kb)))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(("len", path, str(len(a)), str(len(b))))
        for i, (x, y) in enumerate(zip(a, b)):
            jdiff(x, y, "%s[%d]" % (path, i), out, limit)
    elif a != b or type(a) != type(b):
        out.append(("val", path, short(a), short(b)))
    return out


def textlines(s):
    s = mask_text(s or "")
    if "><" in s:
        s = s.replace("><", ">\n<")
    return s.splitlines()


def parse(body):
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body


def compare(x, y):
    """Return a list of (kind, where, py, native) differences for one exchange."""
    diffs = []
    if path_of(x["url"]) != path_of(y["url"]) or x["method"] != y["method"]:
        diffs.append(("request", "", "%s %s" % (x["method"], path_of(x["url"])),
                      "%s %s" % (y["method"], path_of(y["url"]))))
    if x.get("status") != y.get("status"):
        diffs.append(("status", "", str(x.get("status")), str(y.get("status"))))
    if x.get("error") or y.get("error"):
        if mask_text(str(x.get("error"))) != mask_text(str(y.get("error"))):
            diffs.append(("error", "", str(x.get("error")), str(y.get("error"))))
    hx = {k.lower(): mask_text(v) for k, v in (x.get("rheaders") or {}).items() if k.lower() not in SKIP_HEADERS}
    hy = {k.lower(): mask_text(v) for k, v in (y.get("rheaders") or {}).items() if k.lower() not in SKIP_HEADERS}
    for k in sorted(set(hx) | set(hy)):
        if hx.get(k) != hy.get(k):
            if k == "content-length" and x.get("rbody") is not None:
                continue  # judged by the body itself
            diffs.append(("header", k, str(hx.get(k)), str(hy.get(k))))
    bx, by = parse(x.get("rbody")), parse(y.get("rbody"))
    if isinstance(bx, (dict, list)) and isinstance(by, (dict, list)):
        diffs += jdiff(mask(bx), mask(by))
    elif mask_text(x.get("rbody") or "") != mask_text(y.get("rbody") or ""):
        la, lb = textlines(x.get("rbody")), textlines(y.get("rbody"))
        import difflib
        d = [l for l in difflib.unified_diff(la, lb, lineterm="", n=0)
             if not l.startswith(("---", "+++", "@@"))]
        diffs.append(("text", "", "\n".join(d[:12])[:600], ""))
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("py")
    ap.add_argument("native")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--grep", default=None)
    ap.add_argument("--sig", default=None)
    ap.add_argument("--max", type=int, default=400)
    a = ap.parse_args()
    py = {(c["label"], c["n"]): c for c in load(a.py)}
    nat = load(a.native)
    groups = collections.OrderedDict()
    unmatched = []
    verdicts = []
    for c in nat:
        p = py.get((c["label"], c["n"]))
        if not p:
            unmatched.append(c["line"][:120])
            continue
        if p["verdict"] != c["verdict"]:
            verdicts.append((c["label"], p["verdict"], c["verdict"]))
        for i in range(max(len(p["reqs"]), len(c["reqs"]))):
            x = p["reqs"][i] if i < len(p["reqs"]) else None
            y = c["reqs"][i] if i < len(c["reqs"]) else None
            if not x or not y:
                z = x or y
                key = ("only-" + ("py" if x else "native"), template(path_of(z["url"])), "", "")
                groups.setdefault(key, []).append((c["label"], z["method"] + " " + path_of(z["url"]), ""))
                continue
            for kind, where, pv, nv in compare(x, y):
                where_t = re.sub(r"\[\d+\]", "[]", where)
                key = (kind, x["method"] + " " + template(path_of(x["url"])), where_t,
                       (pv + " | " + nv) if kind in ("header", "status", "order") else "")
                groups.setdefault(key, []).append((c["label"], path_of(x["url"]), "%s -> %s" % (pv, nv)))
    print("# %d native checks, %d unmatched labels, %d verdict differences, %d difference signatures"
          % (len(nat), len(unmatched), len(verdicts), len(groups)))
    for v in verdicts:
        print("VERDICT", v)
    for u in unmatched[:20]:
        print("UNMATCHED", u)
    shown = 0
    for key, items in groups.items():
        text = " ".join(key)
        if a.grep and a.grep.lower() not in (text + json.dumps(items)).lower():
            continue
        if a.sig and a.sig not in text:
            continue
        shown += 1
        if shown > a.max:
            break
        print("\n## [%d] %s %s %s" % (len(items), key[0], key[1], key[2]))
        if key[3]:
            print("   ", key[3][:300])
        for label, path, ex in (items if a.detail else items[:1]):
            print("   check:", label, "|", path[:160])
            if ex:
                print("   ", ex[:700].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
