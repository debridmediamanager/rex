#!/usr/bin/env python3
"""Record plexav's artwork analysis: `thumbBlurHash`, `artBlurHash` and `UltraBlurColors`.

plexav (PMS 1.43.4 on kids, http://127.0.0.1:32420 there) holds rex's own synthetic covers
(`make_blur_images.py`, copies in tests/fixtures/artwork/) as the `Rex Blur` albums of its music
library. This asks it, over ssh and with its server token read on kids (never printed or kept):

* which rows carry which field for Plexamp's exact requests (`plexamp-*.jsonl`) and for the same
  requests without `includeFields`, per row type, with whether the row owns its thumb and art;
* each cover's hash and colours as its album or artist row answers them, and what
  `/services/ultrablur/colors?url=` answers for the row's own image;
* images served to plexav over http from kids (`--images KEY:DIR`, repeated, optional): the colours
  alone, under KEY (`solids`, 300x300 single colours named `s_RRGGBB.png`; `tiny`, 1x1 to 3x1 PNGs);
* with `--fresh`, a new album placed under `Rex Blur` and the library refreshed, polled until its
  analysis appears: what the row answers before that (a run without it keeps the last one).

Plex's own artwork is recorded by sha256 and length only. GETs only, plus the one refresh.

    python3 probes/record_blur.py tests/fixtures/artwork/plexav_blur.json [--fresh] [--images KEY:DIR ...]
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COVERS = os.path.join(HERE, "..", "tests", "fixtures", "artwork")

REMOTE = r'''
import json, subprocess, sys, time, urllib.parse, urllib.request, hashlib
tok = subprocess.check_output(["sh", "-c", "docker exec plexav-plex sh -c \"grep -o 'PlexOnlineToken=\\\"[^\\\"]*' "
    "'/config/Library/Application Support/Plex Media Server/Preferences.xml'\" | cut -d'\"' -f2"]).decode().strip()
def get(path, raw=False):
    request = urllib.request.Request("http://127.0.0.1:32420" + path, headers={"Accept": "application/json", "X-Plex-Token": tok})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, body if raw else json.loads(body)
    except urllib.error.HTTPError as error:
        return error.code, None
job = json.loads(JOB)
out = {"requests": [], "rows": {}, "fresh": None}
def rows_of(body):
    mc = (body or {}).get("MediaContainer") or {}
    return list(mc.get("Metadata") or []) + [x for h in mc.get("Hub") or [] for x in h.get("Metadata") or []]
def owns(row, field):
    return str(row.get(field, "")).startswith("/library/metadata/%s/" % row.get("ratingKey"))
for path in job["requests"]:
    status, body = get(path)
    seen = sorted({(r.get("type"), tuple(k for k in r if "Blur" in k), owns(r, "thumb"), owns(r, "art")) for r in rows_of(body)})
    out["requests"].append({"path": path, "status": status, "rows": [{"type": t, "fields": list(f), "ownThumb": a, "ownArt": b} for t, f, a, b in seen]})
for kind in ("8", "9"):
    for row in rows_of(get("/library/sections/1/all?type=%s&includeFields=thumbBlurHash,artBlurHash" % kind)[1]):
        thumb = row.get("thumb") if owns(row, "thumb") else None
        entry = {"type": row["type"], "title": row["title"], "thumbBlurHash": row.get("thumbBlurHash"),
                 "artBlurHash": row.get("artBlurHash"), "UltraBlurColors": row.get("UltraBlurColors")}
        if thumb:
            status, image = get(thumb, raw=True)
            entry["thumbSha256"], entry["thumbLength"] = hashlib.sha256(image).hexdigest(), len(image)
            entry["service"] = get("/services/ultrablur/colors?url=" + urllib.parse.quote(thumb, safe=""))[1]
        out["rows"][row["ratingKey"]] = entry
for key, names in job["images"].items():
    for name in names:
        url = "%s%s/%s" % (job["image_base"], key, name)
        status, body = get("/services/ultrablur/colors?url=" + urllib.parse.quote(url, safe=""))
        out.setdefault(key, {})[name] = {"status": status, "colors": ((body or {}).get("MediaContainer") or {}).get("UltraBlurColors")}
if job["fresh"]:
    # Placed here, with the poll already running, or PMS's file watcher analyses it first.
    import base64, os
    folder = os.path.expanduser("~/plexav/media/music/Rex Blur/%s (2026)" % job["fresh"])
    os.makedirs(folder, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=5",
                    "-metadata", "title=%s Tone" % job["fresh"], "-metadata", "artist=Rex Blur",
                    "-metadata", "album_artist=Rex Blur", "-metadata", "album=" + job["fresh"], "-metadata", "date=2026",
                    "-metadata", "track=1/1", os.path.join(folder, "01 - %s Tone.flac" % job["fresh"])], check=True)
    open(os.path.join(folder, "cover.jpg"), "wb").write(base64.b64decode(job["cover"]))
    get("/library/sections/1/refresh", raw=True)
    seen, start = [], time.time()
    while time.time() - start < 120:
        found = [r for r in rows_of(get("/library/sections/1/all?type=9&includeFields=thumbBlurHash")[1]) if r.get("title") == job["fresh"]]
        state = [{k: r.get(k) for k in ("thumb", "thumbBlurHash", "UltraBlurColors")} for r in found]
        for r, s in zip(found, state):
            s["ownThumb"] = owns(r, "thumb")
        if state and (not seen or seen[-1]["rows"] != state):
            seen.append({"after": round(time.time() - start, 2), "rows": state})
        if state and state[0].get("thumbBlurHash"):
            break
        time.sleep(0.05)
    out["fresh"] = seen
json.dump(out, sys.stdout)
'''


def plexamp_requests():
    """Plexamp's own requests that answered a hash, and each without `includeFields`."""
    paths = []
    for name in ("plexamp-browse", "plexamp-setup"):
        for line in open(os.path.join(HERE, "..", "tests", "fixtures", "music", name + ".jsonl")):
            record = json.loads(line)
            if record["method"] != "GET" or "BlurHash" not in str(record.get("res_body")):
                continue
            query = [(k, v) for k, v in record["query"] if not k.startswith("X-Plex-")]
            import urllib.parse
            paths.append(record["path"] + "?" + urllib.parse.urlencode(query))
            plain = [(k, v) for k, v in query if k != "includeFields"]
            paths.append(record["path"] + ("?" + urllib.parse.urlencode(plain) if plain else ""))
    extra = ["/library/sections/1/all?type=10&includeFields=thumbBlurHash",
             "/library/sections/1/all?type=9&includeFields=artBlurHash",
             "/library/sections/1/all?type=8&includeFields=artBlurHash",
             "/library/sections/2/all?includeFields=thumbBlurHash,artBlurHash",
             "/library/sections/2/all?type=3&includeFields=thumbBlurHash,artBlurHash",
             "/library/sections/2/all?type=4&includeFields=thumbBlurHash,artBlurHash",
             "/library/sections/2/all?type=4"]
    return list(dict.fromkeys(paths + extra))


def main():
    out_path = sys.argv[1]
    fresh = "--fresh" in sys.argv
    images = {}
    for at, arg in enumerate(sys.argv):
        if arg == "--images":
            key, folder = sys.argv[at + 1].split(":", 1)
            images[key] = sorted(os.listdir(folder))
            # Served by `python3 -m http.server 18765` in ~/rexblur-probe on kids.
            subprocess.run(["rsync", "-qr", folder + "/", "ben@kids:rexblur-probe/%s/" % key], check=True)
    title = time.strftime("Blur Fresh %H%M%S") if fresh else None
    cover = base64.b64encode(open(os.path.join(COVERS, "fresh.jpg"), "rb").read()).decode()
    job = {"requests": plexamp_requests(), "images": images,
           "image_base": "http://192.168.88.242:18765/", "fresh": title, "cover": cover}
    # The script and its job go over stdin: ssh would re-split a `-c` argument.
    run = subprocess.run(["ssh", "ben@kids", "python3", "-"], input=("JOB = %r\n" % json.dumps(job) + REMOTE).encode(),
                         capture_output=True)
    if run.returncode:
        sys.exit("kids: " + run.stderr.decode()[-600:])
    answer = run.stdout
    try:
        recorded = json.loads(answer)
    except ValueError:
        sys.exit("kids answered: %r ... %r" % (answer[:300], run.stderr.decode()[-300:]))
    covers = {}
    for name in sorted(os.listdir(COVERS)):
        if name.endswith((".jpg", ".png")):
            data = open(os.path.join(COVERS, name), "rb").read()
            covers[hashlib.sha256(data).hexdigest()] = name
    for entry in recorded["rows"].values():
        # rex's own synthetic covers by their file; Plex's artwork by its hash and length only.
        entry["cover"] = covers.get(entry.get("thumbSha256"))
    if not fresh and os.path.exists(out_path):
        # The unanalysed row can be caught only once per album placed: a re-run keeps it.
        recorded["fresh"] = json.load(open(out_path)).get("fresh")
    recorded["recorded"] = "plexav, PMS 1.43.4, 2026-10-31"
    json.dump(recorded, open(out_path, "w"), indent=1, sort_keys=True)
    print("%d requests, %d rows, images %s, fresh %s" % (len(recorded["requests"]), len(recorded["rows"]),
                                                          {key: len(recorded.get(key, {})) for key in images}, bool(recorded["fresh"])))


if __name__ == "__main__":
    main()
