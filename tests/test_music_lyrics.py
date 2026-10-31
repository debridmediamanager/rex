#!/usr/bin/env python3
"""A track's sidecar lyrics a source pushes (`lyrics`), shown and served as plexav's PMS shows them.

`fixtures/music/lyrics.json` (`probes/record_music_lyrics.py`) is plexav's own answers for an `.lrc`
(timed, `[ti:]`/`[ar:]` tags) and a `.txt` placed beside two of its tracks: the Part's lyrics Stream
(`streamType` 4, `LRC (External)`), and `/library/streams/{id}` under every Accept and `format=` a
client sends -- the `Lyrics` document as JSON or XML, or the file itself. The same synthetic texts
sit beside two of `music_rig.py`'s tracks and are pushed with them; rex reads nothing off their names.

    python3 tests/test_music_lyrics.py
"""
import json
import os
import shutil
import sys
import urllib.parse

import music_rig as M
from music_rig import check, results

RECORDED = json.load(open(os.path.join(M.FIXTURES, "lyrics.json")))


def main():
    work, items, rex, handler, asked = M.start()
    try:
        status, body = rex.push(items)
        assert status == 200 and body.get("created") == 46, body
        pairs = [(items[0], "01 - Opus Tone.lrc"), (items[1], "02 - Vorbis Tone.txt")]
        pushed = []
        for item, name in pairs:
            folder = os.path.dirname(item["path"])
            path = "%s/%s" % (folder, name)
            text = RECORDED["texts"][name]
            open(os.path.join(work, "media", path), "w").write(text)
            pushed.append(dict(item, lyrics=[{"path": path, "size": len(text.encode())}]))
        status, body = rex.push(pushed)
        check("a push naming each track's lyrics updates both", status == 200 and body.get("updated") == 2, body)
        keys = {row["id"]: row["ratingKey"] for row in rex.request("/rex/feed/plexav/items")[1]["items"]}
        for item, recorded in zip(pushed, RECORDED["tracks"]):
            row = rex.rows("/library/metadata/%s" % keys[item["id"]])[0]
            streams = row["Media"][0]["Part"][0]["Stream"]
            theirs = [s for s in recorded["streams"] if s.get("streamType") == 4][0]
            ours = [s for s in streams if s.get("streamType") == 4]
            differences = M.compare(json.dumps(theirs), json.dumps(ours[0]) if ours else "{}")
            check("%s's lyrics are an external stream of its Part, as plexav's %s" % (item["path"].rsplit("/", 1)[1], theirs["codec"]),
                  len(ours) == 1 and not differences and set(ours[0]) == set(theirs)
                  and ours[0]["key"] == "/library/streams/%s" % ours[0]["id"], (ours, differences))
            if not ours:
                continue
            # Plex Web's own read: plexav titled the same stream `LRC (External)` for it.
            status, web = rex.request("/library/metadata/%s?X-Plex-Product=Plex%%20Web" % keys[item["id"]])
            web_streams = web["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]["Stream"]
            check("  to Plex Web it is %s (External), as plexav" % theirs["displayTitle"],
                  [s["displayTitle"] for s in web_streams if s.get("streamType") == 4] == [theirs["displayTitle"] + " (External)"],
                  [s.get("displayTitle") for s in web_streams])
            for answer in recorded["answers"]:
                headers = {"Accept": answer["accept"]} if answer["accept"] else {"Accept": ""}
                url = ours[0]["key"] + ("?" + urllib.parse.urlencode([tuple(pair) for pair in answer["query"]]) if answer["query"] else "")
                status, got_headers, got = rex.request(url, headers=headers, raw=True)
                content = got_headers.get("Content-Type", "")
                if answer["body"].startswith(("{", "<")):
                    same = not M.compare(answer["body"], got)
                else:
                    same = got.decode() == answer["body"]
                check("  Accept %s, %s: %s, as plexav" % (answer["accept"], answer["query"] or "no format", answer["contentType"]),
                      status == answer["status"] and content.split(";")[0] == answer["contentType"].split(";")[0] and same,
                      (status, content, got[:300]))
        # Absent keeps them, [] takes them away.
        status, body = rex.push([dict(pushed[0]).copy()])
        status, body = rex.push([dict(pushed[0], lyrics=[])])
        row = rex.rows("/library/metadata/%s" % keys[pushed[0]["id"]])[0]
        check("an empty list takes a track's lyrics away", body.get("updated") == 1
              and not [s for s in row["Media"][0]["Part"][0]["Stream"] if s.get("streamType") == 4], body)
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe to make plexav's files")
        sys.exit(0)
    sys.exit(main())
