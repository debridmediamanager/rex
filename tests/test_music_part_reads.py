#!/usr/bin/env python3
"""A track's Part read, per audio format, answered as plexav's PMS answers it.

`fixtures/music/part-reads.json` (`probes/record_music_part_reads.py`) is plexav's PMS 1.43.4 reading
one Part of each audio format its music section holds -- MP3, FLAC, Opus `.opus`, Vorbis `.ogg`, ALAC
`.m4a` and PCM `.wav` -- whole, `Range: bytes=0-99`, an open `bytes=100-`, as a HEAD and with
Plexamp's `download=1` whole and ranged: the status and every header in order. Plexamp played an AAC
`.m4a` from rex as `application/octet-stream`, where plexav answers `audio/mp4` for its `.m4a`.

This makes a short tone in each format under plexav's own file names, pushes them as tracks of
one album nothing names, and reads each Part the same ways. An AAC `.m4a` is read too: a Part's
type is keyed on its file's extension, never its codec or container (findings, cycle 44's), so it
answers as plexav's ALAC `.m4a` does. rex closes every media read (`Connection: close`, findings
"A media read must NOT keep the connection alive"), so plexav's `Connection`/`Keep-Alive` pair is
left out of the order compared.

`Last-Modified` is the row's `updatedAt` on plexav, its tracks' and its one episode's (none of them
its `addedAt`), and absent for a row with none: the Codec Test tracks Plex's agent never matched.
So it is compared by that rule, checked first on the recording itself: every row rex makes has an
`updatedAt`, a track nothing names included, which plexav's unmatched tracks do not. Catalog rows
with plexav's own stamps for its MP3 and FLAC read back plexav's own `Last-Modified`.

    python3 tests/test_music_part_reads.py
"""
import email.utils
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile

import music_rig as M
from music_rig import check, results

RECORDED = json.load(open(os.path.join(M.FIXTURES, "part-reads.json")))
CODECS = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "flac": ["-c:a", "flac"], "opus": ["-c:a", "libopus"],
          "ogg": ["-c:a", "libvorbis"], "m4a": ["-c:a", "alac"], "wav": ["-c:a", "pcm_s16le"]}
CONNECTION = {"Connection", "Keep-Alive"}


def make(folder, name, codec):
    path = os.path.join(folder, name)
    subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-ac", "2"] + codec + [path], check=True)
    probe = json.loads(subprocess.run([shutil.which("ffprobe"), "-v", "quiet", "-print_format", "json", "-show_format",
                                       "-show_streams", "-show_packets", "-read_intervals", "%+#1", path],
                                      check=True, capture_output=True).stdout)
    return os.path.getsize(path), probe


def scaled(theirs, ours):
    """plexav's headers with its file's size swapped for ours: the same window of a shorter file."""
    out = dict(theirs)
    if "Content-Range" in theirs:
        span, total = theirs["Content-Range"].split(" ")[1].split("/")
        first, last = map(int, span.split("-"))
        last = ours - 1 if last == int(total) - 1 else last
        out["Content-Range"] = "bytes %d-%d/%d" % (first, last, ours)
        out["Content-Length"] = str(last - first + 1)
    else:
        out["Content-Length"] = str(ours)
    return out


def modified(row):
    """The `Last-Modified` a row's read answers: its `updatedAt`, none without one."""
    return email.utils.formatdate(row["updatedAt"], usegmt=True) if row.get("updatedAt") else None


def read(port, method, path, spec):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    connection.request(method, path, headers=dict({"X-Plex-Token": M.TOKEN}, **({"Range": spec} if spec else {})))
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.getheaders(), body


def main():
    work = tempfile.mkdtemp(prefix="rex-part-reads-")
    folder = os.path.join(work, "media", "music", "Rex Test", "Codec Test (2026)")
    os.makedirs(folder)
    parts = [dict(part, extension=part["file"].rsplit(".", 1)[1]) for part in RECORDED["parts"]]
    # The AAC `.m4a` Plexamp was given octet-stream for, answered as plexav's ALAC `.m4a`.
    alac = next(part for part in parts if part["extension"] == "m4a")
    reads = json.loads(json.dumps(alac["reads"]).replace("03 - ALAC Tone", "07 - AAC Tone").replace("03%20-%20ALAC%20Tone", "07%20-%20AAC%20Tone"))
    parts.append(dict(alac, file="07 - AAC Tone.m4a", audioCodec="aac", codec=["-c:a", "aac", "-b:a", "128k"], reads=reads))
    items = []
    for number, part in enumerate(parts, 1):
        part["ours"], probe = make(folder, part["file"], part.get("codec") or CODECS[part["extension"]])
        relative = "music/Rex Test/Codec Test (2026)/" + part["file"]
        items.append({"id": relative, "path": relative, "kind": "track", "track": number, "media_info": probe,
                      "display": {"title": part["file"].rsplit(".", 1)[0], "artist": {"title": "Rex Test"},
                                  "album": {"title": "Codec Test"}}})
    for part in RECORDED["parts"] + [RECORDED["video"]]:
        check("plexav's %s answers Last-Modified %s, its row's updatedAt" % (part["file"], modified(part)),
              {dict(read["headers"]).get("Last-Modified") for read in part.get("reads") or [part]} == {modified(part)})
    # plexav's stamped tracks as catalog rows of their own, reading the same files.
    stamped = [part for part in parts if part.get("updatedAt")]
    rows = [{"ratingKey": str(990 + number), "key": "/library/metadata/%d" % (990 + number), "type": "track",
             "title": part["file"], "librarySectionID": 1, "addedAt": part["addedAt"], "updatedAt": part["updatedAt"],
             "Media": [{"id": 9900 + number, "Part": [{
                 "id": 99000 + number, "key": "/library/parts/%d/%d/file.%s" % (99000 + number, part["updatedAt"], part["extension"]),
                 "file": "/data/music/Rex Test/Codec Test (2026)/" + part["file"], "size": part["ours"]}]}]}
            for number, part in enumerate(stamped)]
    bytes_port, _ = M.range_server(os.path.join(work, "media"))
    agent_url, _ = M.agent_server([])
    rex = M.Rex(work, bytes_port, agent_url, items=rows)
    try:
        for row, part in zip(rows, stamped):
            status, headers, _ = read(rex.port, "GET", row["Media"][0]["Part"][0]["key"], "bytes=0-99")
            theirs = dict(part["reads"][1]["headers"])["Last-Modified"]
            check("a row stamped as plexav's %s reads Last-Modified %s, as plexav (not its addedAt)" % (part["file"], theirs),
                  status == 206 and dict(headers).get("Last-Modified") == theirs, (status, dict(headers).get("Last-Modified")))
        status, body = rex.push(items)
        check("the %d tracks are created" % len(items), status == 200 and body.get("created") == len(items), body)
        keys = {row["id"]: row["ratingKey"] for row in rex.request("/rex/feed/plexav/items")[1]["items"]}
        for item, part in zip(items, parts):
            row = rex.rows("/library/metadata/%s" % keys[item["id"]])[0]
            key = row["Media"][0]["Part"][0]["key"]
            label = "%s (%s)" % (part["file"], part["audioCodec"])
            for recorded in part["reads"]:
                path = key + ("?" + recorded["query"] if recorded["query"] else "")
                status, headers, body = read(rex.port, recorded["method"], path, recorded["range"])
                got = dict(headers)
                theirs = scaled(dict(recorded["headers"]), part["ours"])
                how = "%s%s%s" % (recorded["method"], " ?" + recorded["query"] if recorded["query"] else "",
                                  " " + recorded["range"] if recorded["range"] else "")
                values = {name: theirs[name] for name in
                          ("Content-Type", "Content-Disposition", "Accept-Ranges", "Content-Range", "Content-Length")
                          if name in theirs}
                differ = {name: (got.get(name), value) for name, value in values.items() if got.get(name) != value}
                differ.update({name: (got[name], None) for name in ("Content-Disposition", "Content-Range") if name in got and name not in theirs})
                order = [name for name, _ in recorded["headers"] if name not in CONNECTION | {"Last-Modified"}]
                if modified(row):
                    order.insert(order.index("ETag") + 1, "Last-Modified")
                if got.get("Last-Modified") != modified(row):
                    differ["Last-Modified"] = (got.get("Last-Modified"), modified(row))
                ours = [name for name, _ in headers if name not in CONNECTION]
                length = recorded["length"] and int(values["Content-Length"])
                check("%s %s: %d %s, as plexav" % (label, how, recorded["status"], theirs["Content-Type"]),
                      status == recorded["status"] and not differ and ours == order and len(body) == length,
                      (status, differ, ours if ours != order else "", len(body)))
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe to make the tracks")
        sys.exit(0)
    sys.exit(main())
