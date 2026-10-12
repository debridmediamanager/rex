#!/usr/bin/env python3
"""Drive the official Plex web client through a real browse sequence.

Everything the client does lands in capture/sessions/<label>.jsonl via the
proxy. That file is the reference corpus: rex is correct when it answers the
*same* requests with the same shapes. Requests are never synthesised here -- the
point is to record what the real client actually sends, paging params and all,
because a recording taken without the client's own params hides exactly the
fields that turn out to be missing.

The walk deliberately ends at show -> season -> episode. That is the path that
hid the longest bug in rex (no Seasons block), and it is the one whose
/children requests carry the paging params that matter.
"""
import argparse, collections, json, os, shutil, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Browser

LAB = os.environ.get("PLEXLAB_DIR", os.path.expanduser("~/plexlab"))
CAP = f"{LAB}/capture"
LIVE = f"{CAP}/client.jsonl"


def api(base, token, path):
    """Host-side discovery. Deliberately NOT through the proxy: the corpus must
    contain only what the client itself generated."""
    req = urllib.request.Request(base + path,
                                 headers={"X-Plex-Token": token, "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))["MediaContainer"]


def plan(base, token):
    """Build the click path from what the server actually holds."""
    steps = []
    sections = api(base, token, "/library/sections").get("Directory", [])
    for s in sections:
        steps.append((f"section-{s['key']}-{s['type']}",
                      f"?source={s['key']}"))
        steps.append((f"grid-{s['key']}",
                      "?key=" + urllib.parse.quote(f"/library/sections/{s['key']}/all", safe="")
                      + "&pageType=source"))
    # One movie, then the show -> season -> episode chain.
    for s in sections:
        items = api(base, token, f"/library/sections/{s['key']}/all").get("Metadata", [])
        if not items:
            continue
        if s["type"] == "movie":
            steps.append(("movie-details", "details:" + items[0]["ratingKey"]))
        elif s["type"] == "show":
            show = items[0]
            steps.append(("show-details", "details:" + show["ratingKey"]))
            seasons = api(base, token, f"/library/metadata/{show['ratingKey']}/children").get("Metadata", [])
            if seasons:
                steps.append(("season-details", "details:" + seasons[0]["ratingKey"]))
                eps = api(base, token, f"/library/metadata/{seasons[0]['ratingKey']}/children").get("Metadata", [])
                if eps:
                    steps.append(("episode-details", "details:" + eps[0]["ratingKey"]))
    return steps


def summarise(path):
    hosts, endpoints = collections.Counter(), collections.Counter()
    n = err = blocked = 0
    for line in open(path):
        r = json.loads(line)
        n += 1
        if r.get("error"):
            err += 1
            continue
        if any(k.lower() == "x-plexlab-blocked" for k, _ in r.get("res_headers") or []):
            blocked += 1
            continue
        hosts[r["host"]] += 1
        if r["host"].endswith(".plex.direct"):
            q = dict(r.get("query") or [])
            extra = " ".join(f"{k}={q[k]}" for k in ("X-Plex-Container-Start",
                                                     "X-Plex-Container-Size",
                                                     "includeChildren", "excludeAllLeaves")
                             if k in q)
            endpoints[f'{r["method"]} {r["path"]} -> {r["status"]}' + (f"  [{extra}]" if extra else "")] += 1
    print(f"\n{n} flows  ({err} errored, {blocked} blocked as non-lab)")
    print("hosts:")
    for h, c in hosts.most_common(8):
        print(f"  {c:>4}  {h}")
    print("server endpoints (the corpus rex must match):")
    for p, c in sorted(endpoints.items()):
        print(f"  {c:>3}  {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="session")
    ap.add_argument("--token", required=True, help="plex.tv account token")
    ap.add_argument("--machine", required=True)
    ap.add_argument("--server", default="http://127.0.0.1:32400",
                    help="direct server URL for host-side discovery")
    ap.add_argument("--server-token", required=True, help="server token for discovery")
    ap.add_argument("--settle", type=int, default=9)
    a = ap.parse_args()

    os.makedirs(f"{CAP}/sessions", exist_ok=True)
    os.makedirs(f"{CAP}/shots", exist_ok=True)
    steps = plan(a.server, a.server_token)
    print(f"planned {len(steps)} steps")

    open(LIVE, "w").close()
    b = Browser()
    tid, sid = b.new_page("about:blank")
    try:
        # app.plex.tv sets myPlexAccessToken for signed-OUT sessions too, so its
        # presence proves nothing. /api/v2/user answering a non-empty username is
        # the check that counts.
        b.goto(sid, "https://app.plex.tv/desktop/", settle=a.settle)
        b.js(sid, f'localStorage.setItem("myPlexAccessToken", {json.dumps(a.token)});')
        b.goto(sid, "https://app.plex.tv/desktop/", settle=a.settle + 4)
        who = b.js(sid, """(async()=>{const r=await fetch("https://plex.tv/api/v2/user",
          {headers:{Accept:"application/json","X-Plex-Token":localStorage.getItem("myPlexAccessToken"),
          "X-Plex-Client-Identifier":"plexlab-web"}});const j=await r.json();
          return JSON.stringify({status:r.status,user:j.username,sub:(j.subscription||{}).status});})()""")
        print("signed in as:", who)
        b.shot(sid, f"{CAP}/shots/{a.label}-00-home.png")

        for i, (name, frag) in enumerate(steps, 1):
            if frag.startswith("details:"):
                rk = frag.split(":", 1)[1]
                url = (f"https://app.plex.tv/desktop/#!/server/{a.machine}/details?key="
                       + urllib.parse.quote(f"/library/metadata/{rk}", safe=""))
            else:
                url = f"https://app.plex.tv/desktop/#!/media/{a.machine}/com.plexapp.plugins.library{frag}"
            b.goto(sid, url, settle=a.settle)
            b.shot(sid, f"{CAP}/shots/{a.label}-{i:02d}-{name}.png")
            print(f"  {i:02d} {name}")
    finally:
        try:
            b.js(sid, 'document.querySelectorAll("video").forEach(v=>{v.pause();v.src="";});')
            b.goto(sid, "https://app.plex.tv/desktop/#!/", settle=4)
        except Exception:
            pass
        b.close(tid)

    dest = f"{CAP}/sessions/{a.label}.jsonl"
    shutil.copy(LIVE, dest)
    print("corpus:", dest)
    summarise(dest)


if __name__ == "__main__":
    main()
