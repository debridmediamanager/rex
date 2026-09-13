import os
import json, re, sys, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict, OrderedDict

REF = ("http://kids:32400", os.environ["PLEXLAB_TOKEN"])
SIM = (os.environ.get("PLEXSIM_BASE", "http://127.0.0.1:32511"), None)
MAP = {"MOVIE": ("4", "1007"), "SHOW": ("6", "20001"), "SEASON": ("7", "30001"), "EPISODE": ("8", "1001")}

def fetch(side, path, accept=None, method="GET"):
    base, tok = side
    url = base + path
    if tok:
        url += ("&" if "?" in url else "?") + "X-Plex-Token=" + tok
    h = {"Accept-Encoding": "identity"}
    if accept: h["Accept"] = accept
    req = urllib.request.Request(url, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()
    except Exception as e:
        return None, "", str(e).encode()

def vclass(v):
    if v == "": return "empty"
    if v in ("true", "false"): return "truefalse"
    if re.fullmatch(r"-?\d+", v): return "int"
    if re.fullmatch(r"-?\d+\.\d+", v): return "float"
    return "str"

def xshape(root):
    """path -> {attrs: {name: set(vclass)}, children: [ordered distinct child tags], n}"""
    shape = OrderedDict()
    def walk(el, path):
        p = path + "/" + el.tag
        s = shape.setdefault(p, {"attrs": OrderedDict(), "children": [], "n": 0, "text": False})
        s["n"] += 1
        if (el.text or "").strip(): s["text"] = True
        for k, v in el.attrib.items():
            s["attrs"].setdefault(k, set()).add(vclass(v))
        seq = []
        for c in el:
            if not seq or seq[-1] != c.tag:
                seq.append(c.tag)
        if seq and (len(seq) > len(s["children"])):
            s["children"] = seq
        for c in el:
            walk(c, p)
    walk(root, "")
    return shape

def jtype(v):
    if isinstance(v, bool): return "bool"
    if isinstance(v, int): return "int"
    if isinstance(v, float): return "float"
    if isinstance(v, str): return "str"
    return type(v).__name__

def jshape(obj):
    shape = OrderedDict()
    def walk(o, path):
        if isinstance(o, dict):
            s = shape.setdefault(path, OrderedDict())
            for k, v in o.items():
                if isinstance(v, dict):
                    walk(v, path + "/" + k)
                elif isinstance(v, list):
                    if v and all(isinstance(x, dict) for x in v):
                        for x in v: walk(x, path + "/" + k)
                    else:
                        s.setdefault(k, set()).add("list[" + ",".join(sorted({jtype(x) for x in v})) + "]")
                else:
                    s.setdefault(k, set()).add(jtype(v))
    walk(obj, "")
    return shape

def diff_xml(label, rpath, spath, ignore_extra=False):
    rs, rct, rb = fetch(REF, rpath)
    ss, sct, sb = fetch(SIM, spath)
    out = []
    if rs != ss: out.append(f"  STATUS ref {rs} sim {ss}")
    if rs != 200 or ss != 200:
        return out, None, None
    if rct != sct: out.append(f"  CTYPE ref {rct} sim {sct}")
    try:
        rt = ET.fromstring(rb); st = ET.fromstring(sb)
    except Exception as e:
        out.append(f"  PARSE {e} ref[:120]={rb[:120]!r} sim[:120]={sb[:120]!r}")
        return out, None, None
    if rt.tag != st.tag: out.append(f"  ROOT ref <{rt.tag}> sim <{st.tag}>")
    a, b = xshape(rt), xshape(st)
    a2 = {re.sub(r"^/[^/]+", "/ROOT", k): v for k, v in a.items()}
    b2 = {re.sub(r"^/[^/]+", "/ROOT", k): v for k, v in b.items()}
    for p, s in a2.items():
        if p not in b2:
            out.append(f"  ELEMENT only-ref {p} (n={s['n']}) attrs={list(s['attrs'])[:12]}")
            continue
        t = b2[p]
        if s["children"] != t["children"] and s["children"] and t["children"]:
            out.append(f"  CHILD-ORDER {p}: ref {s['children']} sim {t['children']}")
        ra, sa = s["attrs"], t["attrs"]
        miss = [k for k in ra if k not in sa]
        extra = [k for k in sa if k not in ra]
        if miss: out.append(f"  ATTR only-ref {p}: {miss}")
        if extra and not ignore_extra: out.append(f"  ATTR only-sim {p}: {extra}")
        for k in ra:
            if k in sa and ra[k] != sa[k]:
                # int vs empty etc
                out.append(f"  VCLASS {p}@{k}: ref {sorted(ra[k])} sim {sorted(sa[k])}")
    for p, t in b2.items():
        if p not in a2:
            out.append(f"  ELEMENT only-sim {p} (n={t['n']})")
    return out, rt, st

def diff_json(label, rpath, spath):
    rs, rct, rb = fetch(REF, rpath, "application/json")
    ss, sct, sb = fetch(SIM, spath, "application/json")
    out = []
    if rs != 200 or ss != 200: return out
    try:
        a = jshape(json.loads(rb)); b = jshape(json.loads(sb))
    except Exception as e:
        out.append(f"  JSON PARSE {e} ref={rb[:100]!r} sim={sb[:100]!r}")
        return out
    for p, ra in a.items():
        if p not in b: continue
        sa = b[p]
        for k in ra:
            if k in sa and ra[k] != sa[k]:
                out.append(f"  JTYPE {p}@{k}: ref {sorted(ra[k])} sim {sorted(sa[k])}")
    return out

def sub(path, which):
    for k, (r, s) in MAP.items():
        path = path.replace("{" + k + "}", r if which == 0 else s)
    return path

if __name__ == "__main__":
    paths = [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")]
    for line in paths:
        if " => " in line:
            rp, sp = line.split(" => ")
        else:
            rp, sp = sub(line, 0), sub(line, 1)
        xo, _, _ = diff_xml(line, rp, sp)
        jo = diff_json(line, rp, sp)
        print(("OK  " if not (xo or jo) else "DIFF") + " " + line)
        for o in xo + jo: print(o[:400])
