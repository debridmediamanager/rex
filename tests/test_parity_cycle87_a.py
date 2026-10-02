#!/usr/bin/env python3
"""Cycle 87, territory A (localisation): the surfaces the cycle's music-library and Add
Library commits added, in the request's language.

`tests/fixtures/c87_a_music_i18n_s42.json` was read off plexscratch42 (1.42.2):

- an EMPTY music library made with Plex Web's own Add Library form, then every read
  Plex Web 4.162.1 and python-plexapi 4.18.2 make for it whose answer moves between
  English and German, in en, de, fr and ja. A real PMS translates the library's Meta
  field, sort and operator titles, its `All Artists`/`By Album`/`Search ...` rows, the
  three search prompts and its shelves' titles; plexsim had none of them in its table.
- the random `music.vault` shelf's title, read in every language Plex Web sends over
  forty minutes: `Haven't played in %s months|years` is localised around the number.
- six refusing Add Library forms that between them raise all eleven refusal lines, in
  every language Plex Web sends and `hu-HU`: three of the lines are translated.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle87_a.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c87_a_music_i18n_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("server exited at start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process, port
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def ask(port, method, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def normalise(document, section):
    """The library's own number written `{S}` wherever it names the library."""
    text = json.dumps(document, ensure_ascii=False)
    s = re.escape(str(section))
    for pattern in (r"(sections/)%s\b" % s,
                    r"((?:contentDirectoryID|sectionID)=)%s\b" % s,
                    r"(%%2C)%s(?=%%2C)" % s,
                    r"(\.)%s(?=\")" % s,
                    r"(\"(?:key|id|librarySectionID)\": \")%s(?=\")" % s):
        text = re.sub(pattern, lambda match: match.group(1) + "{S}", text)
    text = re.sub(r"(\"librarySectionID\": )%s\b" % s,
                  lambda match: match.group(1) + "\"{S}\"", text)
    return json.loads(text)


def strings(node, path, out):
    """Every string attribute, keyed by its element's identifying attributes."""
    if isinstance(node, dict):
        # A shelf is named by its identifier alone: a random shelf's key moves.
        names = ("hubIdentifier",) if "hubIdentifier" in node else ("id", "filter", "key", "type")
        ids = ["%s=%s" % (k, node[k]) for k in names
               if k in node and not isinstance(node[k], (dict, list))]
        tag = path + "{" + ",".join(ids[:2]) + "}"
        for name, value in node.items():
            if isinstance(value, (dict, list)):
                strings(value, tag + "/" + name, out)
            elif isinstance(value, str):
                out[tag + "@" + name] = value
    elif isinstance(node, list):
        for index, value in enumerate(node):
            keyed = isinstance(value, dict) and any(k in value for k in ("id", "hubIdentifier", "filter", "key"))
            strings(value, path if keyed else "%s[%d]" % (path, index), out)
    return out


def main():
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        base = dict(fixture["request_headers"], **{"X-Plex-Client-Identifier": "parity-c87-a"})
        status, body = ask(port, "POST", fixture["created_with"], base)
        check("the music library is created (%d)" % fixture["created_status"],
              status == fixture["created_status"], (status, body[:200]))
        section = json.loads(body)["MediaContainer"]["Directory"][0]["key"]

        # -- the library's own vocabulary, per language --------------------------
        vault = re.compile(r"^Haven't played in ")
        for path, by_language in fixture["documents"].items():
            reference_en = strings(by_language["en"], "", {})
            _s, mine = ask(port, "GET", path.replace("{S}", str(section)),
                           dict(base, **{"X-Plex-Language": "en"}))
            mine_en = strings(normalise(json.loads(mine), section), "", {})
            # Only what both sides say alike in English is compared: the reference's
            # other libraries, its uuids and its clock are its own.
            shared = [k for k, v in reference_en.items()
                      if mine_en.get(k) == v and not vault.match(v)
                      and "music.vault" not in k and not k.endswith("@key")]
            for language in ("de", "fr", "ja"):
                reference = strings(by_language[language], "", {})
                _s, mine = ask(port, "GET", path.replace("{S}", str(section)),
                               dict(base, **{"X-Plex-Language": language}))
                got = strings(normalise(json.loads(mine), section), "", {})
                wrong = [(reference_en[k], got.get(k), reference.get(k)) for k in shared
                         if k in reference and got.get(k) != reference[k]]
                moved = sum(1 for k in shared if k in reference and reference[k] != reference_en[k])
                check("%s %s: the %d strings the reference translates read as measured"
                      % (language, path[:90], moved), not wrong, wrong[:6])

        # -- the random vault shelf's title, around its own number ---------------
        templates = {}
        for language, title, key in fixture["vault_reads"]:
            number, unit = re.search(r"lastViewedAt<=-(\d+)(mon|y)", key).groups()
            templates.setdefault((language, unit), set()).add(title.replace(number, "%s", 1))
        check("(reference) each language words a period one way",
              all(len(v) == 1 for v in templates.values()), templates)
        wrong = []
        for language in sorted({language for language, _unit in templates}):
            _s, mine = ask(port, "GET", "/hubs/sections/%s" % section,
                           dict(base, **{"X-Plex-Language": language}))
            for hub in json.loads(mine)["MediaContainer"].get("Hub", []):
                if hub.get("hubIdentifier", "").startswith("music.vault"):
                    number, unit = re.search(r"lastViewedAt<=-(\d+)(mon|y)", hub["key"]).groups()
                    want = next(iter(templates[(language, unit)])).replace("%s", number)
                    if hub["title"] != want:
                        wrong.append((language, hub["title"], want))
        check("the vault shelf's title is worded as measured in %d languages"
              % len({language for language, _unit in templates}), not wrong, wrong[:6])

        # -- Add Library's refusal lines -----------------------------------------
        wrong = []
        for exchange in fixture["refusals"]:
            headers = dict(base, **exchange["headers"])
            status, body = ask(port, "POST", exchange["path"], headers)
            if (status, body) != (exchange["status"], exchange["body"]):
                wrong.append((exchange["headers"], exchange["label"], body, exchange["body"]))
        check("%d refusals read line for line as measured" % len(fixture["refusals"]),
              not wrong, wrong[:4])
    finally:
        process.terminate()
        process.wait(timeout=10)
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
