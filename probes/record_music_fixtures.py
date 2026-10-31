#!/usr/bin/env python3
"""Turn plexav's music captures into rex's music fixtures (`tests/fixtures/music/`).

plexav is a real PMS 1.43.4 with one music library -- Radiohead's In Rainbows (FLAC) and OK
Computer, Nine Inch Nails' The Slip and Daft Punk's Discovery (MP3), synthetic 45 s tones tagged
as those albums (`make-music.sh` there), so Plex's agent matched real `plex://` guids -- driven on
2026-10-31 by Plex Web 4.163.0, Plex for Android 2026.21.0 and Plexamp 4.12.4. Its capture
directory holds what each client asked and the PMS answered (`capture/sessions/web-music-*.jsonl`,
`capture/android/{droid,plexamp}-music-*.jsonl`) and what the PMS asked Plex's agent
(`capture/outbound.jsonl`).

Writes, from a copy of that directory:

* `agent.json`: the agent's answers for the music library -- each album's `mbid://` match
  (`type=9`), each album with its children and each artist -- path and query as the PMS sent them,
  for a stub agent;
* `<client>.jsonl`: the client exchanges the music suites replay, one per distinct request,
  redacted as `harness/scripts/redact.py` redacts (tokens in queries, headers, bodies and nested
  `url=`), media bodies (part reads) dropped to their length, and the PMS's own server-level
  answers (`/:/prefs`, `/media/providers`, updates, devices) left out.

    python3 probes/record_music_fixtures.py --capture <copy of ~/plexav/capture> [--out tests/fixtures/music]
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness", "scripts"))
from redact import TOKEN_KEYS, scrub_body, scrub_headers  # noqa: E402

CLIENTS = {
    "web-music-browse": "sessions", "web-music-views": "sessions", "web-music-play": "sessions",
    "web-music-actions": "sessions", "web-music-search": "sessions",
    "droid-music-browse": "android", "droid-music-play": "android",
    "plexamp-setup": "android", "plexamp-browse": "android", "plexamp-play": "android",
}
LEFT_OUT = re.compile(r"^/(photo/:/transcode|:/websockets|media/providers|:/prefs|updater|activities|media/grabbers"
                      r"|livetv|tv\.plex\.providers|service/ultrablur|status/sessions/background|identity|$)")
MEDIA = re.compile(r"^/library/(parts/|metadata/\d+/(thumb|art)/)")


def ours(record):
    host = record.get("host") or ""
    return "plex.direct" in host or record.get("port") in (32400, 32420, 32421)


def normalised(record):
    """The capture files differ in shape: one wrote Python reprs of its lists."""
    import ast
    for field in ("query", "req_headers", "res_headers"):
        if isinstance(record.get(field), str):
            try:
                record[field] = ast.literal_eval(record[field])
            except (ValueError, SyntaxError):
                record[field] = []
    for field in ("port", "status", "req_len", "res_len"):
        try:
            record[field] = int(record[field])
        except (KeyError, TypeError, ValueError):
            pass
    return record


def agent(capture):
    answers = []
    for line in open(os.path.join(capture, "outbound.jsonl")):
        record = normalised(json.loads(line))
        if "metadata.provider" not in (record.get("host") or "") or record.get("method") != "GET":
            continue
        query = [(k, v) for k, v in record.get("query") or [] if k.lower() not in TOKEN_KEYS]
        body = record.get("res_body") or ""
        try:
            response = json.loads(body)
        except ValueError:
            continue
        rows = (response.get("MediaContainer") or {}).get("Metadata") or []
        guid = (rows[0].get("guid") if rows else "") or ""
        typed = dict(query).get("type")
        # The music library's: an album's match by its release mbid, an album, an artist.
        if not (guid.startswith(("plex://album/", "plex://artist/")) and typed in (None, "9")):
            continue
        answers.append({"method": "GET", "path": record["path"], "query": query, "response": response})
    return answers


def client(path):
    seen, kept = set(), []
    for line in open(path):
        record = normalised(json.loads(line))
        if not ours(record) or record.get("status") in (None, 101) or LEFT_OUT.match(record["path"]):
            continue
        # Any token rides out: Plex for Android also sends its push `X-Plex-Device-Notification-Token`.
        query = [[k, ("REDACTED" if "token" in k.lower() else scrub_body(v))] for k, v in record.get("query") or []]
        signature = (record["method"], record["path"], tuple(tuple(pair) for pair in query if not pair[0].startswith("X-Plex")))
        if signature in seen:
            continue
        seen.add(signature)
        record["query"] = query
        record["req_headers"] = [[k, "REDACTED" if "token" in k.lower() else v] for k, v in scrub_headers(record.get("req_headers") or [])]
        record["res_headers"] = scrub_headers(record.get("res_headers") or [])
        record["req_body"] = scrub_body(record.get("req_body"))
        record["res_body"] = None if MEDIA.match(record["path"]) else scrub_body(record.get("res_body"))
        record["host"] = "reference.plex.direct"
        kept.append(record)
    return kept


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", required=True)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "tests", "fixtures", "music"))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    answers = agent(args.capture)
    json.dump({"recorded": "plexav 1.43.4 outbound, 2026-10-31", "answers": answers},
              open(os.path.join(args.out, "agent.json"), "w"), ensure_ascii=False)
    print("%d agent answers" % len(answers))
    for name, folder in CLIENTS.items():
        found = glob.glob(os.path.join(args.capture, folder, name + ".jsonl")) or glob.glob(os.path.join(args.capture, name + ".jsonl"))
        if not found:
            print("missing %s" % name)
            continue
        kept = client(found[0])
        with open(os.path.join(args.out, name + ".jsonl"), "w") as out:
            for record in kept:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
        print("%s: %d exchanges" % (name, len(kept)))


if __name__ == "__main__":
    sys.exit(main())
