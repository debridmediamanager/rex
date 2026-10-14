#!/usr/bin/env python3
"""What the identify corpus recorders share: replay a corpus through the native identifier
(`rex identify-replay`, rust/src/main.rs), ask TMDB and Plex's metadata agent live for the
first request each torrent lacked, record the trimmed answer, and replay again until nothing
is missing. So a recording holds exactly what `rust/src/identify.rs` asks for.

The binary is REX_NATIVE_BIN, else rust/target/release/rex. Full provider answers are cached
under RECORD_CACHE (outside the repository); REX_PLEXTV_URL sends both providers to a stub as
`<base>/<host><path>`, as it sends the server's own plex.tv calls.

    python3 probes/identify_replay.py     # self-check against a local stub; no network

The self-check tops up a copy of each checked-in corpus and proves nothing is asked and the
file is untouched, then removes one recorded answer and proves exactly that one is asked again
and the file comes back byte for byte.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.environ.get("REX_NATIVE_BIN", os.path.join(ROOT, "rust", "target", "release", "rex"))
TMDB_API = "https://api.themoviedb.org/3"
PLEX_PROVIDER = "https://metadata.provider.plex.tv"


def native(*argv, document=None):
    """One native adapter call; `document` goes in as `--input`."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(document, handle)
    try:
        args = [BIN, *argv] + (["--input", handle.name] if document is not None else [])
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("%s failed: %s" % (" ".join(argv), result.stderr.strip()))
        return json.loads(result.stdout)
    finally:
        os.unlink(handle.name)


# The years the oracle passed TMDB as ints: its keys hold them as JSON numbers.
INT_PARAMS = ("year", "first_air_date_year")


def tmdb_key(path, pairs):
    """The recording's TMDB key: `json.dumps([path, sorted(params.items())])`."""
    return json.dumps([path, sorted((name, int(value) if name in INT_PARAMS and str(value).isdigit()
                                     else value) for name, value in pairs)])


def plex_key(method, path, body):
    return json.dumps([method, path, body], sort_keys=True)


def _stubbed(url):
    base = os.environ.get("REX_PLEXTV_URL")
    return "%s/%s" % (base.rstrip("/"), url.split("://", 1)[1]) if base else url


class Live:
    """TMDB and Plex's metadata agent as the Python oracle asked them: a failure is None."""

    def __init__(self):
        cache = os.environ.get("RECORD_CACHE") or tempfile.mkdtemp(prefix="identify-record-")
        os.makedirs(cache, exist_ok=True)
        self.paths = {name: os.path.join(cache, name + ".json") for name in ("tmdb", "plex")}
        self.cache = {}
        for name, path in self.paths.items():
            try:
                self.cache[name] = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                self.cache[name] = {}

    def _keep(self, name, key, payload):
        self.cache[name][key] = payload
        with open(self.paths[name], "w", encoding="utf-8") as handle:
            json.dump(self.cache[name], handle)
        return payload

    def tmdb(self, path, params):
        key_value = os.environ["TMDB_KEY"]
        url = "%s/%s?%s" % (TMDB_API, path.lstrip("/"),
                            urllib.parse.urlencode(dict(params, api_key=key_value)))
        cached = url.replace(key_value, "KEY")
        if cached in self.cache["tmdb"]:
            return self.cache["tmdb"][cached]
        for attempt in (0, 1):
            try:
                with urllib.request.urlopen(_stubbed(url), timeout=25) as response:
                    return self._keep("tmdb", cached, json.load(response))
            except urllib.error.HTTPError as error:
                if error.code != 429 or attempt:  # rate limited: back off once, then give up
                    return None
                time.sleep(2)
            except Exception:
                return None

    def plex(self, method, path, body):
        cached = "%s %s %s" % (method, path, json.dumps(body, sort_keys=True) if body else "")
        if cached in self.cache["plex"]:
            return self.cache["plex"][cached]
        headers = {"X-Plex-Token": os.environ["PLEX_TOKEN"], "Accept": "application/json",
                   "Content-Type": "application/json", "X-Plex-Product": "Plex Media Server",
                   "X-Plex-Provider-Version": "6.5.0"}
        if method == "GET":
            # The agent pages at 50 (the lab capture); 500 is refused with a 400.
            headers.update({"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "50"})
        request = urllib.request.Request(
            _stubbed(PLEX_PROVIDER + path), method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return self._keep("plex", cached, json.load(response))
        except Exception:
            return None


def recorded_tmdb(corpus, live, trim, path, params):
    """A TMDB answer from the recording, asked live and recorded when it lacks one."""
    key = tmdb_key(path, params.items())
    if key not in corpus["tmdb"]:
        corpus["tmdb"][key] = trim(path, live.tmdb(path, params))
    return corpus["tmdb"][key]


def replay(mode, corpus, live, trim_tmdb, trim_plex):
    """Replay until the recording holds every answer the identifier asks for.

    Returns (identities, TMDB answers added, Plex answers added). Only the FIRST request
    each torrent lacked is asked per round: a later one was asked because of the `{}` the
    replay put in its place, and may not be asked once the real answer is there."""
    added = {"tmdb": 0, "plex": 0}
    while True:
        identities = native("identify-replay", document={
            "mode": mode, "torrents": corpus["torrents"],
            "tmdb": corpus["tmdb"], "plex": corpus["plex"]})["identities"]
        firsts = {json.dumps(i["missed"][0], sort_keys=True): i["missed"][0]
                  for i in identities if i["missed"]}
        if not firsts:
            return identities, added["tmdb"], added["plex"]
        for miss in firsts.values():
            if "tmdb" in miss:
                path, pairs = miss["tmdb"]
                key = tmdb_key(path, pairs)
                if key in corpus["tmdb"]:
                    raise SystemExit("replay asked for %s although it is recorded" % key)
                corpus["tmdb"][key] = trim_tmdb(path, live.tmdb(path, dict(pairs)))
                added["tmdb"] += 1
            else:
                method, path, body = miss["plex"]
                key = plex_key(method, path, body)
                if key in corpus["plex"]:
                    raise SystemExit("replay asked for %s although it is recorded" % key)
                corpus["plex"][key] = trim_plex(path, live.plex(method, path, body))
                added["plex"] += 1


def write(corpus, path):
    with open(path, "w") as handle:
        json.dump(corpus, handle, indent=1, sort_keys=True)


def top_up(path, mode, trim_tmdb, trim_plex):
    """Ask only what CORPUS lacks; a top-up that adds nothing leaves the file untouched."""
    corpus = json.load(open(path))
    _, tmdb_added, plex_added = replay(mode, corpus, Live(), trim_tmdb, trim_plex)
    if tmdb_added or plex_added:
        write(corpus, path)
    print("%s: %d TMDB and %d Plex answers added" % (path, tmdb_added, plex_added))


# -- the self-check ---------------------------------------------------------------------

def _stub(answers, asked):
    """A provider stub on 127.0.0.1: serves `answers` by recording key, logs every request."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self):
            url = urllib.parse.urlsplit(self.path)
            host, _, path = url.path.lstrip("/").partition("/")
            if host == "api.themoviedb.org":
                pairs = [(k, v) for k, v in urllib.parse.parse_qsl(url.query) if k != "api_key"]
                key = tmdb_key(path[len("3/"):], pairs)
            else:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                key = plex_key(self.command, "/" + path, body)
            asked.append(key)
            body = json.dumps(answers[key]).encode() if key in answers else b""
            self.send_response(200 if key in answers else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("REX_STUB_PORT", "39531"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _top_up(recorder, path, port):
    env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:%d" % port, TMDB_KEY="stub-key",
               PLEX_TOKEN="stub-token", RECORD_CACHE=tempfile.mkdtemp(prefix="identify-stub-"))
    run = subprocess.run([sys.executable, os.path.join(ROOT, "probes", recorder), "--top-up", path],
                         env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    shutil.rmtree(env["RECORD_CACHE"])
    return run.stdout.splitlines()[0]


def self_check():
    fixtures = os.path.join(ROOT, "tests", "fixtures", "identify")
    answers, asked = {}, []
    server = _stub(answers, asked)
    port = server.server_address[1]
    work = tempfile.mkdtemp(prefix="identify-selfcheck-")
    # (corpus, the recorder that wrote it, the mode that recorder identifies in)
    for name, recorder, mode in (("corpus.json", "record_identify_corpus.py", "name"),
                                 ("tom_and_jerry.json", "record_episode_id_corpus.py", "hash"),
                                 ("blank_tile_releases.json", "record_unidentified_corpus.py", "release"),
                                 ("episode_ids.json", "record_episode_id_corpus.py", "hash")):
        original = os.path.join(fixtures, name)
        corpus = json.load(open(original))
        missed = [i["missed"] for i in native("identify-replay", document=dict(corpus, mode=mode))["identities"]
                  if i["missed"]]
        assert not missed, "%s: the native identifier asks what the recording lacks: %s" % (name, missed[:2])
        print("PASS %s: a %s replay asks nothing the recording lacks" % (name, mode))
        copy = os.path.join(work, name)
        shutil.copy(original, copy)
        before = _digest(copy)
        del asked[:]
        line = _top_up(recorder, copy, port)
        assert not asked and _digest(copy) == before, (asked[:3], line)
        print("PASS %s: --top-up asks nothing and leaves it byte-identical (sha256 %s) -- %s"
              % (name, before[:16], line))
        # Remove one answer the replay reads -- one TMDB, one Plex -- and top it up again.
        for table in ("tmdb", "plex"):
            for key in sorted(corpus[table]):
                lacking = dict(corpus, **{table: {k: v for k, v in corpus[table].items() if k != key}})
                if corpus[table][key] and any(i["missed"] for i in native(
                        "identify-replay", document=dict(lacking, mode=mode))["identities"]):
                    break
            write(lacking, copy)
            answers.clear()
            answers[key] = corpus[table][key]
            del asked[:]
            line = _top_up(recorder, copy, port)
            assert asked == [key] and _digest(copy) == before, (asked, line)
            print("PASS %s: without %s %s, --top-up asks exactly it and restores the file -- %s"
                  % (name, table, key[:70], line))
    server.shutdown()
    shutil.rmtree(work)


if __name__ == "__main__":
    self_check()
