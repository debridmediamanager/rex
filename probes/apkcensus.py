#!/usr/bin/env python3
"""Plex for Android request census and version diff (the Android analogue of webcensus.py).

usage:
  apkcensus.py decompile APK_OR_SPLIT_DIR OUTDIR     # -> OUTDIR/bundle.hbc, OUTDIR/full.js
  apkcensus.py census FULL.js --out CENSUS.json [--version 2026.19.1]
  apkcensus.py diff OLD.json NEW.json [--out DELTA.json] [--md DELTA.md]
  apkcensus.py show CENSUS.json [--base pms] [--grep /statistics]

Plex for Android is React Native compiled to Hermes bytecode. `decompile` pulls
`assets/index.android.bundle` out of the base APK and runs `hbc-decompiler` (hermes-dec) over it.
The output is register-level JS (`r1 = '/library/sections/'; r0 = r39[r13](r38, r37, ...)`), so
`census` is not a tokenizer like webcensus.py's: it splits the bundle into its metro modules
(`__d(factory, id, deps)`), walks every function with a per-function register tracker, and
rebuilds each request template from the string concatenations Hermes emits
(`HermesInternal.concat`, `+`). Unknown operands print as `{}`. Per call site it records the
template, the object keys the same function writes under `searchParams`/`headers`/`method` (with
literal values where the value is a literal), any `X-Plex-*` key, and `js:N` (the line in the
decompiled file).

Base-URL classification -- how the call site picks its host, read off the client helpers:
  * an absolute literal (`https://discover.provider.plex.tv/...`) names its host;
  * the PMS helpers resolve a server's connection and throw "No connection available for server"
    when there is none (module `useMediaServerAPI` and the `usePms*Mutation` modules); the store
    module exporting `getMediaServer` and the `serverPath`/`ratingKeyToKey` utility module are PMS
    too. A call site whose function reaches one of them (module slot or inline dependency index) is
    `pms`;
  * a module that holds an absolute plex.tv-family base URL and is reached the same way gives that
    host (`plex.tv`, `clients.plex.tv`, `discover.provider.plex.tv`, `community.plex.tv`,
    `luma.plex.tv`, ...);
  * `pms?` / `<host>?` mark a call site classified only by its module's imports or by a `pms`/`Pms`
    function or module name (action builders hand a `url` to a PMS action runner);
  * `player-direct` is the remote-player family (`/player/playback/*`, `/player/timeline/*`): the
    PlexCompanion controller sends them to `http://{player address}:{port}` straight (POST with
    `X-Plex-Target-Client-Identifier`, timeline poll GET with `wait=1&commandID=`), and the app's own
    companion receiver answers the same paths; neither is PMS-bound;
  * `pms-root` is a path under a PMS root (`/library`, `/playQueues`, `/:/`, `/video/:/transcode`
    ...) whose function reached no helper this classifier knows -- the request is built here and
    sent by a caller (playback decision, play queues, timeline). Counted as PMS-bound;
  * `route` is an in-app navigation path (no fetch helper reached); `unresolved` is the rest with
    a query string.
Metro module ids are sequential, not hashes, so `diff` matches call sites by (base, template)
and by template alone when only the classification moved. Bundles are the vendor's: keep them
and census JSON outside the repository; commit only request shapes.
"""
import argparse, collections, json, os, re, shutil, subprocess, sys, tempfile, zipfile

HBC = os.path.expanduser("~/.local/bin/hbc-decompiler")
FAC = re.compile(r"^    r\d+ = function\(a0, a1, a2, a3, a4, a5, a6\) \{")
MODID = re.compile(r"^    r\d+ = (\d+);$")
HDR = re.compile(r"^(\s*)(?:r\d+ = |var \w+ = )?function\*? ?\(([^)]*)\) \{ // (?:Original name: ([^,]+), )?[Ee]nvironment")
LIT = re.compile(r"^\s*r(\d+) = '((?:[^'\\]|\\.)*)';$")
NUM = re.compile(r"^\s*r(\d+) = (-?\d+);$")
COPY = re.compile(r"^\s*r(\d+) = r(\d+);$")
ARG = re.compile(r"^\s*r(\d+) = a(\d+);$")
CONCATP = re.compile(r"^\s*r(\d+) = r\d+\.concat;$")
CALLIDX = re.compile(r"^\s*r(\d+) = r(\d+)\[r(\d+)\]\(([^)]*)\);$")
CALLBIND = re.compile(r"^\s*r(\d+) = r(\d+)\.bind\(r(\d+)\)\(([^)]*)\);$")
PLUS = re.compile(r"^\s*r(\d+) = r(\d+) \+ r(\d+);$")
IDX = re.compile(r"^\s*r(\d+) = r(\d+)\[r(\d+)\];$")
OBJ = re.compile(r"^\s*r(\d+) = \{(.*)\};$")
SETK = re.compile(r"^\s*r(\d+)\['([^']+)'\] = r(\d+);$")
# `rO[rK] = rV`; hermes-dec also prints a string-keyed PutByVal as `rO[14] = rV` where 14 is the
# NUMBER of the register holding the key (`r14 = 'X-Plex-Token'`), not a numeric index.
SETR = re.compile(r"^\s*r(\d+)\[r?(\d+)\] = r(\d+);$")
SLOTSET = re.compile(r"^\s*var _closure(\d+)_slot(\d+) = r(\d+);$")
SLOTGET = re.compile(r"^\s*r(\d+) = _closure(\d+)_slot(\d+);$")
ASSIGN = re.compile(r"^\s*r(\d+) = ")
EXPORT = re.compile(r"^        r\d+\['([A-Za-z_$][\w$]*)'\] = r\d+;$")
ABS = re.compile(r"^https?://([^/'?]+)")
REQ = re.compile(r"^(?:/[A-Za-z:{]|https?://)")
PLAYER = re.compile(r"^/player/(?:playback|timeline|mirror|application|navigation)")
PROPR = re.compile(r"^\s*r(\d+) = r\d+\.([A-Za-z_$][\w$]*);$")
# First path segments a PMS answers (webcensus.py's set, without `servers`, which this app only
# uses as an in-app route: /servers/{id}/dashboard). A template outside them that only reached a
# PMS helper by import or name is an in-app route, not a request.
PMS_ROOTS = {
    "library", "hubs", ":", "playQueues", "playlists", "status", "transcode", "video", "music",
    "photo", "media", "myplex", "system", "butler", "security", "accounts", "devices",
    "clients", "player", "activities", "sync", "updater", "statistics", "livetv", "channels",
    "identity", "services", "downloadQueue", "actions", "prefs", "search", "diagnostics", "log",
    "resources", "web", "manage", "neighborhood", "tv.plex.providers.epg.cloud", "mediaproviders",
    "playback", "agents", "applications", "certificates", "logs", "metadata",
    "transcoders", "cloud", "subtitles", "{}",
}
# In-app navigation roots (the app's linking table), which no PMS answers.
APP_ROUTES = {"servers", "libraries", "live-tv", "downloads", "watch", "account", "view-state-sync",
              "home", "plex-pass", "media-unavailable", "watch-together", "lists", "person",
              "on-demand", "discover", "api"}
PMS_HINT = "No connection available for server"
# Accessors a call site reads to pick its base, as the client exports them (environment module:
# services.{account,community,luma,myplex,notifications,pubsub,watchtogether,epg}.baseUrl).
PROP_BASE = [
    (re.compile(r"^(?:get|use)MediaServer(?:ConnectionUri|AccessToken|sAPI|PaginatedAPI|RandomPaginatedAPI|APIQueries)?$"
                r"|^(?:serverPath|getMediaServerAPIQuery|getMediaServerAPIInfiniteQuery)$"), "pms"),
    (re.compile(r"^(?:get|use)MyPlexUrl$|^myPlexUrl$"), "clients.plex.tv"),
    (re.compile(r"^(?:get|use)Luma(?:Url|FetchOptions)$|^lumaBaseUrl$"), "luma.plex.tv"),
    (re.compile(r"^(?:get|use)CommunityUrl$"), "community.plex.tv"),
    (re.compile(r"^(?:get|use)NotificationsUrl$"), "notifications.plex.tv"),
    (re.compile(r"^(?:get|use)PubSub\w*Url$"), "pubsub.plex.tv"),
    (re.compile(r"^(?:get|use)EpgUrl$"), "epg.provider.plex.tv"),
    (re.compile(r"^(?:get|use)DiscoveryUrl$|^discoverBaseUrl$"), "discover.provider.plex.tv"),
]
PMS_EXPORTS = {"getMediaServer", "serverPath", "ratingKeyToKey", "useMediaServerAPIQueries"}


def unq(s):
    return s.replace("\\'", "'").replace("\\\\", "\\")


# ---------------------------------------------------------------- decompile
def cmd_decompile(a):
    src = a.apk
    apk = src
    if os.path.isdir(src):
        cands = [os.path.join(src, f) for f in sorted(os.listdir(src)) if f.endswith(".apk")]
        apk = next((c for c in cands if _has_bundle(c)), None)
        if not apk:
            sys.exit("no APK in %s carries assets/index.android.bundle" % src)
    os.makedirs(a.outdir, exist_ok=True)
    hbc = os.path.join(a.outdir, "bundle.hbc")
    with zipfile.ZipFile(apk) as z, open(hbc, "wb") as f:
        f.write(z.read("assets/index.android.bundle"))
    with zipfile.ZipFile(apk) as z:
        man = z.read("AndroidManifest.xml") if "AndroidManifest.xml" in z.namelist() else b""
    out = os.path.join(a.outdir, "full.js")
    subprocess.run([HBC, hbc, out], check=True)
    print("bundle", os.path.getsize(hbc), "->", out, os.path.getsize(out))


def _has_bundle(p):
    try:
        with zipfile.ZipFile(p) as z:
            return "assets/index.android.bundle" in z.namelist()
    except zipfile.BadZipFile:
        return False


# ---------------------------------------------------------------- census
def split_modules(L):
    mods, cur = [], None
    for i, l in enumerate(L):
        if FAC.match(l):
            cur = {"start": i}
        elif cur is not None and l.startswith("    r") and MODID.match(l) and L[i - 1].startswith("    };"):
            cur["id"] = int(MODID.match(l).group(1))
            m = re.match(r"^    r\d+ = \[(.*)\];$", L[i + 1])
            cur["deps"] = [None if x == "null" else int(x) for x in m.group(1).split(", ")] if m else []
            cur["end"] = i
            mods.append(cur)
            cur = None
    return mods


def module_kinds(L, mods):
    """Content classification of each module: 'pms' and/or the plex.tv-family hosts it holds."""
    kinds, names = {}, {}
    for m in mods:
        k, exps = set(), set()
        for l in L[m["start"]:m["end"]]:
            if PMS_HINT in l:
                k.add("pms")
            e = EXPORT.match(l)
            if e:
                exps.add(e.group(1))
        if exps & PMS_EXPORTS:
            k.add("pms")
        kinds[m["id"]] = k
        names[m["id"]] = exps
    return kinds, names


class Fn:
    def __init__(self, name, line, indent):
        self.name, self.line, self.indent = name or "?", line, indent
        self.val = {}          # register -> ('s', template) | ('n', int) | ('c',) concat | ('o', objid) | ('d', depidx) | ('m',) depmap
        self.tmpl = {}         # template id -> [line, text, consumed]
        self.reg_t = {}        # register -> template id it currently holds
        self.objs = {}
        self.deps = set()
        self.nt = 0
        self.props = set()


def census(path, version=None):
    L = open(path, encoding="utf-8", errors="replace").read().split("\n")
    mods = split_modules(L)
    kinds, exps = module_kinds(L, mods)
    out = []
    for m in mods:
        deps = m["deps"]
        slots = {}          # (closure depth, slot) -> ('d', idx) | ('m',)
        stack = []
        mod_dep_kinds = set()
        for d in deps:
            if d is not None:
                mod_dep_kinds |= kinds.get(d, set())
        mod_name_pms = any(re.search(r"(?:^p|P)ms|MediaServer", e) for e in exps.get(m["id"], ()))
        for i in range(m["start"], m["end"] + 1):
            l = L[i]
            h = HDR.match(l)
            if h:
                f = Fn(h.group(3), i, len(h.group(1)))
                if not stack:   # the module factory: a6 is the dependency map
                    f.name = "<module %d>" % m["id"]
                stack.append(f)
                continue
            if not stack:
                continue
            f = stack[-1]
            if l.strip() in ("};", "}", "},", "});") and len(l) - len(l.lstrip()) == f.indent:
                stack.pop()
                _emit(f, m, out, kinds, deps, mod_dep_kinds, mod_name_pms)
                continue
            _step(f, l, i, slots, deps)
        while stack:
            _emit(stack.pop(), m, out, kinds, deps, mod_dep_kinds, mod_name_pms)
    return {"version": version, "file": os.path.basename(path), "modules": len(mods), "sites": out}


def _tv(f, r):
    v = f.val.get(r)
    if v and v[0] == "s":
        return v[1]
    return "{}"


def _new_t(f, i, text, parts):
    for p in parts:
        t = f.reg_t.get(p)
        if t is not None:
            f.tmpl[t][2] = True
    f.nt += 1
    f.tmpl[f.nt] = [i, text, False]
    return f.nt


def _step(f, l, i, slots, deps):
    mm = LIT.match(l)
    if mm:
        r = mm.group(1)
        f.val[r] = ("s", unq(mm.group(2)))
        f.reg_t[r] = _new_t(f, i, unq(mm.group(2)), [])
        return
    mm = NUM.match(l)
    if mm:
        f.val[mm.group(1)] = ("n", int(mm.group(2)))
        f.reg_t.pop(mm.group(1), None)
        return
    mm = ARG.match(l)
    if mm:
        f.val[mm.group(1)] = ("m",) if mm.group(2) == "6" and f.name.startswith("<module") else None
        f.reg_t.pop(mm.group(1), None)
        return
    mm = COPY.match(l)
    if mm:
        a, b = mm.groups()
        f.val[a] = f.val.get(b)
        if b in f.reg_t:
            f.reg_t[a] = f.reg_t[b]
        else:
            f.reg_t.pop(a, None)
        return
    mm = CONCATP.match(l)
    if mm:
        f.val[mm.group(1)] = ("c",)
        f.reg_t.pop(mm.group(1), None)
        return
    mm = SLOTSET.match(l)
    if mm:
        v = f.val.get(mm.group(3))
        if v and v[0] in ("d", "m"):
            slots[(mm.group(1), mm.group(2))] = v
        return
    mm = SLOTGET.match(l)
    if mm:
        v = slots.get((mm.group(2), mm.group(3)))
        f.val[mm.group(1)] = v
        f.reg_t.pop(mm.group(1), None)
        if v and v[0] == "d" and v[1] < len(deps):
            f.deps.add(deps[v[1]])
        return
    mm = IDX.match(l)
    if mm:
        a, b, c = mm.groups()
        vb, vc = f.val.get(b), f.val.get(c)
        f.reg_t.pop(a, None)
        if vb and vb[0] == "m" and vc and vc[0] == "n":
            f.val[a] = ("d", vc[1])
            if vc[1] < len(deps):
                f.deps.add(deps[vc[1]])
        else:
            f.val[a] = None
        return
    mm = CALLIDX.match(l) or CALLBIND.match(l)
    if mm:
        a, recv, fn, args = mm.groups()
        if CALLBIND.match(l):
            recv, fn = fn, recv   # rFN.bind(rRECV)(args)
        args = [x.strip()[1:] for x in args.split(",") if x.strip().startswith("r")]
        if f.val.get(fn) == ("c",):
            parts = [recv] + args
            text = "".join(_tv(f, p) for p in parts)
            f.val[a] = ("s", text)
            f.reg_t[a] = _new_t(f, i, text, parts)
            return
        # require(dep): keep the dependency a register holds across interop wrappers
        carried = next((f.val[x] for x in args if f.val.get(x) and f.val[x][0] == "d"), None)
        f.val[a] = carried
        f.reg_t.pop(a, None)
        return
    mm = PLUS.match(l)
    if mm:
        a, b, c = mm.groups()
        if (f.val.get(b) or (None,))[0] == "s" or (f.val.get(c) or (None,))[0] == "s":
            text = _tv(f, b) + _tv(f, c)
            f.val[a] = ("s", text)
            f.reg_t[a] = _new_t(f, i, text, [b, c])
        else:
            f.val[a] = None
            f.reg_t.pop(a, None)
        return
    mm = OBJ.match(l)
    if mm:
        a = mm.group(1)
        oid = len(f.objs)
        f.objs[oid] = {}
        for k, v in re.findall(r"'([^']+)': ('(?:[^'\\]|\\.)*'|[^,}]+)", mm.group(2)):
            f.objs[oid][k] = unq(v[1:-1]) if v.startswith("'") else "{}"
        f.val[a] = ("o", oid)
        f.reg_t.pop(a, None)
        return
    mm = SETK.match(l)
    if not mm:
        mr = SETR.match(l)
        if mr and (f.val.get(mr.group(2)) or (None,))[0] == "s":
            mm = (mr.group(1), f.val[mr.group(2)][1], mr.group(3))
    else:
        mm = mm.groups()
    if mm:
        o, k, v = mm
        vo = f.val.get(o)
        if vo and vo[0] == "o":
            vv = f.val.get(v)
            f.objs[vo[1]][k] = vv[1] if vv and vv[0] == "s" else (("<obj%d>" % vv[1]) if vv and vv[0] == "o" else "{}")
            t = f.reg_t.get(v)
            if t is not None:
                f.tmpl[t][2] = "key:" + k
        return
    mm = PROPR.match(l)
    if mm:
        f.props.add(mm.group(2))
    mm = ASSIGN.match(l)
    if mm:
        f.val[mm.group(1)] = None
        f.reg_t.pop(mm.group(1), None)


def _obj_sel(f, key):
    out = {}
    for o in f.objs.values():
        v = o.get(key)
        if v and v.startswith("<obj"):
            out.update(f.objs.get(int(v[4:-1]), {}))
    return out


def _emit(f, m, out, kinds, deps, mod_dep_kinds, mod_name_pms):
    reqs = []
    for tid, (line, text, consumed) in f.tmpl.items():
        if consumed is True:
            continue
        # `{connection uri}` + path: the PMS mutations concatenate the server's bestConnection.uri
        # in front of the path at the call site itself.
        text = re.sub(r"^(?:\{\})+(?=/[A-Za-z:])", "", text)
        if not REQ.match(text):
            continue
        if isinstance(consumed, str) and consumed not in ("key:url", "key:path", "key:key", "key:uri", "key:endpoint"):
            continue
        reqs.append((line, text))
    if not reqs:
        return
    fk = set()
    for d in f.deps:
        fk |= kinds.get(d, set())
    pk = sorted({b for p in f.props for rx, b in PROP_BASE if rx.match(p)})
    params = _obj_sel(f, "searchParams")
    params.update(_obj_sel(f, "params") if not params else {})
    headers = _obj_sel(f, "headers")
    for o in f.objs.values():
        for k, v in o.items():
            if k.startswith("X-Plex-"):
                headers.setdefault(k, v)
    methods = sorted({o["method"] for o in f.objs.values() if o.get("method") not in (None, "{}")})
    for line, text in reqs:
        # A DSN-style `https://<key>@host` keeps only the host: the census is committed.
        text = re.sub(r"^(https?://)[^/@'?]+@", r"\1", text)
        a = ABS.match(text)
        if a:
            base = a.group(1)
        elif PLAYER.match(text):
            base = "player-direct"
        elif pk:
            base = "+".join(pk)
        elif "pms" in fk:
            base = "pms"
        elif re.search(r"(?:^p|P)ms|MediaServer", f.name) or mod_name_pms:
            base = "pms?"
        elif "pms" in mod_dep_kinds:
            base = "pms?"
        elif (re.match(r"^/([^/?]*)", text) or [None, None])[1] in PMS_ROOTS - {"{}"}:
            base = "pms-root"
        elif "?" in text:
            base = "unresolved"
        else:
            base = "route"
        root = re.match(r"^/([^/?]*)", text)
        root = root.group(1) if root else None
        if root is not None and (base == "pms?" and root not in PMS_ROOTS
                                 or base == "pms" and root in APP_ROUTES):
            base = "plex.tv-family?" if root == "api" else "route"
        q = dict(re.findall(r"[?&]([^=&{}]+)=([^&]*)", text))
        out.append({"js": line + 1, "module": m["id"], "fn": f.name, "template": text, "base": base,
                    "query": q, "params": params, "headers": headers, "methods": methods,
                    "helpers": sorted(fk | set(pk))})


def cmd_census(a):
    c = census(a.full, a.version)
    json.dump(c, open(a.out, "w"), indent=1)
    n = collections.Counter(s["base"] for s in c["sites"])
    print("modules", c["modules"], "sites", len(c["sites"]), dict(n.most_common()))


# ---------------------------------------------------------------- diff
def norm(t):
    return re.sub(r"\{\}(\{\})+", "{}", t)


def keyed(c):
    k = collections.defaultdict(list)
    for s in c["sites"]:
        k[norm(s["template"])].append(s)
    return k


def shape(sites):
    base = sorted({s["base"] for s in sites})
    params = sorted({"%s=%s" % kv for s in sites for kv in list(s["params"].items()) + list(s["query"].items())})
    headers = sorted({h for s in sites for h in s["headers"]})
    methods = sorted({m for s in sites for m in s["methods"]})
    return {"base": base, "params": params, "headers": headers, "methods": methods}


def is_pms(bases):
    return any(b in ("pms", "pms?", "pms-root") for b in bases)


def cmd_diff(a):
    o, n = json.load(open(a.old)), json.load(open(a.new))
    ko, kn = keyed(o), keyed(n)
    new = sorted(set(kn) - set(ko))
    gone = sorted(set(ko) - set(kn))
    changed = []
    for t in sorted(set(ko) & set(kn)):
        so, sn = shape(ko[t]), shape(kn[t])
        if so != sn:
            changed.append((t, so, sn))
    res = {
        "old": o.get("version"), "new": n.get("version"),
        "counts": {
            "old": len(ko), "new": len(kn), "added": len(new), "removed": len(gone), "changed": len(changed),
            "old_pms": sum(is_pms(shape(v)["base"]) for v in ko.values()),
            "new_pms": sum(is_pms(shape(v)["base"]) for v in kn.values()),
            "added_pms": sum(is_pms(shape(kn[t])["base"]) for t in new),
            "removed_pms": sum(is_pms(shape(ko[t])["base"]) for t in gone),
            "changed_pms": sum(is_pms(sn["base"]) or is_pms(so["base"]) for t, so, sn in changed),
        },
        "added": [{"template": t, **shape(kn[t]), "sites": ["js:%d %s" % (s["js"], s["fn"]) for s in kn[t]]} for t in new],
        "removed": [{"template": t, **shape(ko[t]), "sites": ["js:%d %s" % (s["js"], s["fn"]) for s in ko[t]]} for t in gone],
        "changed": [{"template": t, "old": so, "new": sn, "sites": ["js:%d %s" % (s["js"], s["fn"]) for s in kn[t]]}
                    for t, so, sn in changed],
    }
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)
    if a.md:
        open(a.md, "w").write(render_md(res))
    print(json.dumps(res["counts"]))


def _row(d):
    p = ", ".join(d["params"]) or "-"
    return "| `%s` | %s | %s | %s | %s | %s |" % (
        d["template"].replace("|", "\\|"), "/".join(d["base"]), p.replace("|", "\\|"),
        ", ".join(d["headers"]) or "-", ", ".join(d["methods"]) or "-", "<br>".join(d["sites"][:4]))


def render_md(r):
    c = r["counts"]
    out = ["# Plex for Android request census: %s -> %s" % (r["old"], r["new"]), "",
           "Generated by `probes/apkcensus.py diff`. Templates are rebuilt from the decompiled",
           "Hermes bundle (`{}` = a runtime value); `js:N` is the line in `hbc-decompiler`'s output of that",
           "version's `assets/index.android.bundle`. Base: `pms` reached a PMS helper, `pms?` by module or",
           "name only, `pms-root` a PMS path whose helper is not resolved, `player-direct` the companion",
           "family sent to a player's own address, a host name for plex.tv-family clients, `route` an",
           "in-app navigation path, `unresolved` the rest.", "",
           "| | %s | %s |" % (r["old"], r["new"]), "|---|---|---|",
           "| request templates | %d | %d |" % (c["old"], c["new"]),
           "| of which PMS-bound (pms, pms?, pms-root) | %d | %d |" % (c["old_pms"], c["new_pms"]), "",
           "Added %d (PMS-bound %d), removed %d (PMS-bound %d), changed %d (PMS-bound %d)." % (
               c["added"], c["added_pms"], c["removed"], c["removed_pms"], c["changed"], c["changed_pms"]), ""]
    hdr = ["| template | base | params / query | headers | methods | call sites |", "|---|---|---|---|---|---|"]
    for sec in ("added", "removed"):
        items = r[sec]
        pms = [d for d in items if is_pms(d["base"])]
        rest = [d for d in items if not is_pms(d["base"])]
        out += ["## %s, PMS-bound (%d)" % (sec.capitalize(), len(pms)), ""] + hdr + [_row(d) for d in pms] + [""]
        out += ["## %s, other bases (%d)" % (sec.capitalize(), len(rest)), ""] + hdr + [_row(d) for d in rest] + [""]
    out += ["## Changed (%d)" % len(r["changed"]), "", "| template | was | now | call sites |", "|---|---|---|---|"]
    for d in r["changed"]:
        def fmt(s):
            return "base %s; params %s; headers %s; methods %s" % (
                "/".join(s["base"]), ", ".join(s["params"]) or "-", ", ".join(s["headers"]) or "-",
                ", ".join(s["methods"]) or "-")
        out.append("| `%s` | %s | %s | %s |" % (d["template"].replace("|", "\\|"), fmt(d["old"]).replace("|", "\\|"),
                                                fmt(d["new"]).replace("|", "\\|"), "<br>".join(d["sites"][:4])))
    return "\n".join(out) + "\n"


def cmd_show(a):
    c = json.load(open(a.census))
    for s in c["sites"]:
        if a.base and not s["base"].startswith(a.base):
            continue
        if a.grep and a.grep not in s["template"]:
            continue
        print("js:%-8d %-10s %-45s %s %s %s %s" % (s["js"], s["base"], s["fn"][:45], s["template"],
              s["params"] or "", s["headers"] or "", s["methods"] or ""))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("decompile"); d.add_argument("apk"); d.add_argument("outdir")
    c = sp.add_parser("census"); c.add_argument("full"); c.add_argument("--out", required=True); c.add_argument("--version")
    f = sp.add_parser("diff"); f.add_argument("old"); f.add_argument("new"); f.add_argument("--out"); f.add_argument("--md")
    s = sp.add_parser("show"); s.add_argument("census"); s.add_argument("--base"); s.add_argument("--grep")
    a = ap.parse_args()
    {"decompile": cmd_decompile, "census": cmd_census, "diff": cmd_diff, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
