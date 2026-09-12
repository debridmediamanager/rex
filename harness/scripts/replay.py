#!/usr/bin/env python3
"""Replay a captured client corpus against a server and diff the answers.

This is the point of the harness. A corpus recorded from the official client
against a real PMS is the reference; plexsim is correct when it answers the
*same* requests with the same shapes. Requests are reissued byte-for-byte --
same path, same query params in the same order, same X-Plex-* headers -- because
a replay that drops the client's paging params hides exactly the fields that
turn out to be missing.

Diffs are bucketed rather than counted. A key-level comparison alone is not
enough: a bug where both sides carry a field and only the values are swapped
(viewMode 65592 vs 65593) is invisible to it, so values are compared too, with
the genuinely volatile fields listed explicitly rather than guessed at.
"""
import argparse, json, re, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

# Differ per server by construction: ids, tokens, wall-clock, host-specific paths.
VOLATILE = {
    "machineIdentifier", "ratingKey", "parentRatingKey", "grandparentRatingKey",
    "key", "parentKey", "grandparentKey", "guid", "parentGuid", "grandparentGuid",
    "addedAt", "updatedAt", "createdAt", "scannedAt", "lastViewedAt", "updatedAt",
    "thumb", "art", "parentThumb", "grandparentThumb", "grandparentArt", "banner",
    "theme", "token", "accessToken", "sessionKey", "uuid", "librarySectionUUID",
    "contentChangedAt", "refreshing", "hubKey", "chapterSource", "id",
    "filePath", "file", "duration", "bitrate", "size", "totalFileSize",
    "titleSort", "originallyAvailableAt", "audienceRating", "rating",
}
# Fields whose *value* has been load-bearing in a past rendering bug.
LOADBEARING = {"viewMode", "totalSize", "size", "childCount", "leafCount",
               "viewedLeafCount", "type", "allowSync", "librarySectionID",
               "librarySectionTitle", "index", "parentIndex", "smart", "content",
               "directory", "hidden", "agent", "scanner", "viewGroup", "nocache",
               "mediaTagPrefix", "mediaTagVersion", "identifier", "sortAsc"}

SKIP_PATH = re.compile(r"^/(photo/:/transcode|:/websockets|video/:/transcode|library/parts)")


def load(corpus):
    """Server-directed flows only, in order, deduplicated on method+path+query."""
    seen, out = set(), []
    for line in open(corpus):
        r = json.loads(line)
        if r.get("error") or not r.get("host"):
            continue
        if not (r["host"].endswith(".plex.direct") or r["host"].startswith("172.")):
            continue
        if any(k.lower() == "x-plexlab-blocked" for k, _ in r.get("res_headers") or []):
            continue
        if SKIP_PATH.match(r["path"]) or r.get("status") == 101:
            continue
        q = tuple((k, v) for k, v in r.get("query") or [] if not k.startswith("X-Plex-Token"))
        sig = (r["method"], r["path"], q)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def fetch(base, token, rec, timeout=30):
    q = [(k, v) for k, v in rec.get("query") or [] if k != "X-Plex-Token"]
    url = base + rec["path"] + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, method=rec["method"])
    for k, v in rec.get("req_headers") or []:
        kl = k.lower()
        if kl in ("host", "connection", "content-length", "accept-encoding",
                  "x-plex-token", "origin", "referer", "cookie"):
            continue
        req.add_header(k, v)
    req.add_header("X-Plex-Token", token)
    req.add_header("Accept-Encoding", "identity")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "", f"<transport error: {e}>"


def tree(body, ctype):
    """(tag-path -> list of attribute dicts). Works for Plex XML and JSON alike."""
    nodes = defaultdict(list)

    def walk_xml(el, path):
        p = f"{path}/{el.tag}"
        nodes[p].append(dict(el.attrib))
        for c in el:
            walk_xml(c, p)

    def walk_json(obj, path):
        if isinstance(obj, dict):
            attrs = {k: v for k, v in obj.items() if not isinstance(v, (dict, list))}
            nodes[path].append({k: str(v) for k, v in attrs.items()})
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk_json(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for it in obj:
                walk_json(it, path)

    body = (body or "").strip()
    if not body:
        return nodes
    if "json" in ctype or body.startswith("{"):
        try:
            walk_json(json.loads(body), "")
        except Exception:
            pass
    else:
        try:
            walk_xml(ET.fromstring(body), "")
        except Exception:
            pass
    return nodes


def compare(ref_body, ref_ct, got_body, got_ct):
    """Return (missing_elements, missing_attrs, value_diffs)."""
    a, b = tree(ref_body, ref_ct), tree(got_body, got_ct)
    missing_el, missing_at, valdiff = [], [], []
    for path, ref_nodes in a.items():
        if path not in b:
            missing_el.append((path, len(ref_nodes)))
            continue
        got_nodes = b[path]
        if len(ref_nodes) != len(got_nodes):
            missing_el.append((path + " (count)", f"{len(ref_nodes)} vs {len(got_nodes)}"))
        for i, rn in enumerate(ref_nodes):
            if i >= len(got_nodes):
                break
            gn = got_nodes[i]
            for k, v in rn.items():
                if k not in gn:
                    missing_at.append((path, k))
                elif gn[k] != v and k not in VOLATILE:
                    valdiff.append((path, k, v, gn[k]))
    return missing_el, missing_at, valdiff


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--target", required=True, help="base URL of the server under test")
    ap.add_argument("--target-token", default="")
    ap.add_argument("--reference", default="", help="optional live reference; else use recorded bodies")
    ap.add_argument("--reference-token", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    recs = load(a.corpus)
    print(f"{len(recs)} unique server requests in corpus\n")
    lines, bad = [], 0
    tally = Counter()

    for rec in recs:
        if a.reference:
            rst, rct, rbody = fetch(a.reference, a.reference_token, rec)
        else:
            rst, rct = rec["status"], dict((k.lower(), v) for k, v in rec["res_headers"])\
                .get("content-type", "")
            rbody = rec.get("res_body") or ""
        gst, gct, gbody = fetch(a.target, a.target_token, rec)

        q = [(k, v) for k, v in rec.get("query") or [] if k != "X-Plex-Token"]
        label = rec["method"] + " " + rec["path"] + (("?" + urllib.parse.urlencode(q)) if q else "")
        if gst != rst:
            tally["status"] += 1
            bad += 1
            lines.append(f"### {label}\n- **status** reference `{rst}` vs target `{gst}`")
            if gst is None or gst >= 400:
                lines.append(f"  - target said: `{gbody[:200]}`")
            continue
        me, ma, vd = compare(rbody, rct, gbody, gct)
        if not (me or ma or vd):
            tally["ok"] += 1
            continue
        bad += 1
        lines.append(f"### {label}\n- status {rst} (match)")
        if me:
            tally["missing_elements"] += 1
            lines.append("- **missing / mismatched elements**")
            for p, n in me[:8]:
                lines.append(f"  - `{p}` ({n})")
        if ma:
            tally["missing_attrs"] += 1
            agg = Counter((p, k) for p, k in ma)
            lines.append("- **attributes present in reference, absent in target**")
            for (p, k), n in agg.most_common(12):
                mark = " **(load-bearing)**" if k in LOADBEARING else ""
                lines.append(f"  - `{p}` → `{k}`{mark} ×{n}")
        if vd:
            tally["value_diffs"] += 1
            lines.append("- **value differences**")
            shown = [d for d in vd if d[1] in LOADBEARING] + [d for d in vd if d[1] not in LOADBEARING]
            for p, k, rv, gv in shown[:12]:
                mark = " **(load-bearing)**" if k in LOADBEARING else ""
                lines.append(f"  - `{p}` → `{k}`{mark}: reference `{rv}` vs target `{gv}`")

    head = [f"# replay diff", "",
            f"- corpus: `{a.corpus}` ({len(recs)} unique requests)",
            f"- target: `{a.target}`",
            f"- reference: `{a.reference or 'recorded bodies in corpus'}`", "",
            f"**{tally['ok']} clean, {bad} with differences** "
            f"(status {tally['status']}, missing elements {tally['missing_elements']}, "
            f"missing attrs {tally['missing_attrs']}, value diffs {tally['value_diffs']})", ""]
    report = "\n".join(head + lines)
    if a.out:
        open(a.out, "w").write(report)
        print(report[:3000])
        print(f"\nfull report: {a.out}")
    else:
        print(report)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
