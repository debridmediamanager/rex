#!/usr/bin/env python3
"""Concurrency: what many clients at once may and may not change about the answers.

Every parity comparison before this sent one request at a time. This suite drives a
real server process (`tests/server_command.py`: the native binary by default, the
oracle with `PLEXSIM_SERVER=python`) with many at once, black-box, and proves:

1. races  -- two requests released by one barrier are serialised: two play-queue adds
   answer two different containers (cycle 32: a real PMS 0 of 110, plexsim 11 of 67
   before the fix), two creates both answer 200, two first beats of one player make
   one session, two writers of one row leave one of their values and lose neither
   field, and two list-membership writes answer two different lists and keep both;
2. load   -- 32+ client threads mixing the UI's reads with its writes: no connection
   error, no 5xx the same request does not also answer alone, `/identity` inside 1 s
   throughout, and afterwards exactly what was created minus deleted, every
   `viewCount` equal to the scrobbles and unscrobbles applied, no duplicate id, every
   listed playlist and collection readable;
3. backlog -- 30, then 128 connections opened at once each get their answer (cycle
   75: a real PMS answers 30 of 30; a listen backlog of 5 reset most of them);
4. state  -- the load again with `--state`, killed mid-load: every write answered
   before a SIGKILL is there after the restart (cycle 73: five writes answered
   0.17-0.5 s before `docker kill -s KILL` all survived on plexscratch, so the rule is
   ANY answered write, not one older than the two-second saver), the state file parses
   and the server starts; and every answered write survives a SIGTERM;
5. reaper -- sessions whose player went silent during the load leave Now Playing on
   the measured schedule (180-210 s, `tests/test_session_reaper.py`) and `/identity`
   stays inside 1 s while the saver writes;
6. slow   -- half-sent requests, a client that does not read its answers and 50 idle
   keep-alive sockets delay nobody else.

    python3 tests/test_concurrency.py [--long] [--only races,load,...]

The default run takes about four minutes, most of it the reaper's three. `--long` soaks
the load for five minutes, so the reaper removes its sessions under full load.
`PLEXSIM_TEST_PORTS=LO-HI` confines the servers it starts to that port range.
"""
import argparse
import collections
import http.client
import json
import os
import pickle
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = "com.plexapp.plugins.library"
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for _name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(_name, None)

# catalog_ui.json: six movies in section 1, one show of eight episodes in section 2.
MOVIES = ["1007", "1008", "1009", "1012", "1013", "1014"]
EPISODES = ["1001", "1002", "1003", "1004", "1005", "1006", "1010", "1011"]
SHOW = "30001"
TIMELINE_ITEMS = ["1007", "1008"]       # beaten at 10 %: never scrobbled by a beat
SCROBBLE_ONLY = ["1013", "1014"]        # every thread scrobbles, nobody unscrobbles
RATED = "1007"                          # every thread rates it
EDITED = "1009"                         # every thread writes its summary
OWNED_RATING = "1008"                   # thread 0 alone rates it, in order
OWNED_SUMMARY = "1012"                  # thread 1 alone writes its summary, in order
# Each of the first eight threads alone scrobbles and unscrobbles one episode.

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "" if ok else "  -- " + str(detail)[:900]), flush=True)


def note(text):
    print("      " + text, flush=True)


_ports = None
_ports_lock = threading.Lock()


def free_port():
    """A free port, from `PLEXSIM_TEST_PORTS=LO-HI` when it is set."""
    global _ports
    configured = os.environ.get("PLEXSIM_TEST_PORTS")
    if not configured:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port
    with _ports_lock:
        if _ports is None:
            low, high = (int(part) for part in configured.split("-"))
            _ports = collections.deque(range(low, high + 1))
        for _ in range(len(_ports)):
            port = _ports.popleft()
            _ports.append(port)
            # A listener -- this run's own load server among them -- makes it busy;
            # old connections in TIME_WAIT do not, since the servers bind with
            # SO_REUSEADDR. A loopback bind with SO_REUSEADDR would succeed beside a
            # wildcard listener, so ask by connecting, then bind the wildcard.
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                continue
            except OSError:
                pass
            probe = socket.socket()
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("", port))
                return port
            except OSError:
                continue
            finally:
                probe.close()
    raise SystemExit("no free port in PLEXSIM_TEST_PORTS=%s" % configured)


class Server:
    def __init__(self, catalog, state=None):
        self.catalog, self.state = catalog, state
        self.start()

    def start(self):
        self.port = free_port()
        command = server_command("--port", self.port, "--catalog", self.catalog,
                                 "--gdm", "off", "--assert-claimed", root=ROOT)
        if self.state:
            command += ["--state", self.state]
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=ENV, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            self.proc.kill()
        # A server that lost the bind exits at once; a listener answering in its place
        # would be someone else's.
        time.sleep(0.3)
        if self.proc.poll() is not None:
            raise SystemExit("plexsim did not start:\n" + self.output()[-2000:])
        self.machine = call(self.port, "GET", "/identity")[1]["MediaContainer"][
            "machineIdentifier"]

    def stop(self, sig=signal.SIGTERM, wait=15):
        """Deliver `sig`; the seconds until the process was gone, or None."""
        started = time.time()
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=wait)
            return time.time() - started
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            return None

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")

    def uri(self, key):
        return urllib.parse.quote("server://%s/%s/library/metadata/%s" % (
            self.machine, LIBRARY, key), safe="")


def failure_excerpt(output):
    """The server log around its first failed handler, which is what a report needs."""
    marks = [output.find(mark) for mark in ("handler failed", "Traceback")]
    marks = [mark for mark in marks if mark >= 0]
    return output[max(0, min(marks) - 200):min(marks) + 2500] if marks else output[-1500:]


def headers_for(client):
    return {"Accept": "application/json", "X-Plex-Client-Identifier": client,
            "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
            "X-Plex-Device-Name": client, "X-Plex-Version": "4.147.1"}


def parse(raw):
    if raw[:1] == b"{":
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def call(port, method, path, client="cc-suite", timeout=15):
    """One request on its own connection: (status, parsed body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, headers=dict(headers_for(client), Connection="close"))
        response = connection.getresponse()
        return response.status, parse(response.read())
    finally:
        connection.close()


def mc(body):
    return body.get("MediaContainer", {}) if isinstance(body, dict) else {}


def rows(body, name="Metadata"):
    return mc(body).get(name) or []


def row(port, key):
    return (rows(call(port, "GET", "/library/metadata/%s" % key)[1]) or [{}])[0]


class Client:
    """A keep-alive client, as a browser tab is. Any exception is a connection error."""

    def __init__(self, port, name, timeout=15):
        self.port, self.name, self.timeout = port, name, timeout
        self.headers = headers_for(name)
        self.connection = None

    def request(self, method, path):
        if self.connection is None:
            self.connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                         timeout=self.timeout)
        sent = time.time()
        try:
            self.connection.request(method, path, headers=self.headers)
            response = self.connection.getresponse()
            raw = response.read()
            if response.will_close:
                self.close()
            return response.status, parse(raw), sent, time.time()
        except Exception as error:  # noqa: BLE001 -- every failure is the finding
            self.close()
            return None, "%s: %s" % (type(error).__name__, error), sent, time.time()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def barrier_pair(port, first, second, names=("cc-race-a", "cc-race-b")):
    """Two requests released by one barrier, each on its own fresh connection."""
    out = [None, None]
    clients = [Client(port, names[0]), Client(port, names[1])]
    # Connect first, so the barrier releases two requests rather than two handshakes.
    for client in clients:
        client.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        client.connection.connect()
    barrier = threading.Barrier(2)

    def run(index, method, path):
        barrier.wait()
        out[index] = clients[index].request(method, path)[:2]

    threads = [threading.Thread(target=run, args=(0,) + first),
               threading.Thread(target=run, args=(1,) + second)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    [client.close() for client in clients]
    return out


# -- 1. barrier races ---------------------------------------------------------------

def prop_races(catalog, pairs):
    server = Server(catalog)
    port = server.port
    try:
        # Cycle 32 (`handover/tools/race.py`): two adds to one queue each answer the queue
        # AS THAT ADD LEFT IT; a write that builds its answer after the lock answers both
        # with the later container.
        same, statuses = 0, collections.Counter()
        for _ in range(pairs):
            status, body = call(port, "POST", "/playQueues?type=video&own=1&uri=%s"
                                % server.uri(MOVIES[0]), client="cc-race-q")
            queue = mc(body).get("playQueueID")
            a, b = barrier_pair(port, ("PUT", "/playQueues/%s?uri=%s" % (queue, server.uri(MOVIES[1]))),
                                ("PUT", "/playQueues/%s?uri=%s" % (queue, server.uri(MOVIES[2]))),
                                names=("cc-race-q", "cc-race-q"))
            statuses[(a[0], b[0])] += 1
            if a[0] == b[0] == 200:
                key = lambda answer: (mc(answer[1]).get("playQueueVersion"),
                                      mc(answer[1]).get("playQueueLastAddedItemID"))
                same += key(a) == key(b)
        check("races: two concurrent play-queue adds never answer the same container",
              same == 0 and statuses == {(200, 200): pairs},
              "%d identical of %d, statuses %s" % (same, pairs, dict(statuses)))

        # Cycle 32: two creates from one client were a 404 for the queue just made.
        bad = collections.Counter()
        for index in range(pairs):
            a, b = barrier_pair(port, ("POST", "/playQueues?type=video&own=1&uri=%s" % server.uri(MOVIES[3])),
                                ("POST", "/playQueues?type=video&own=1&uri=%s" % server.uri(MOVIES[4])),
                                names=("cc-race-c%d" % index,) * 2)
            ids = {mc(answer[1]).get("playQueueID") for answer in (a, b)}
            if (a[0], b[0]) != (200, 200) or None in ids or len(ids) != 2:
                bad[(a[0], b[0], len(ids))] += 1
        check("races: two concurrent play-queue creates are two 200s with two queues",
              not bad, "bad pairs %s of %d" % (dict(bad), pairs))

        # One player's first two beats at once: one session, holding one of the offsets.
        sessions_bad = []
        for index in range(pairs):
            player = "cc-race-beat-%d" % index
            beat = lambda offset: ("GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                                   "&state=playing&time=%d&duration=600000"
                                   % (TIMELINE_ITEMS[0], TIMELINE_ITEMS[0], offset))
            a, b = barrier_pair(port, beat(61000), beat(62000), names=(player, player))
            listed = [entry for entry in rows(call(port, "GET", "/status/sessions")[1])
                      if (entry.get("Player") or {}).get("machineIdentifier") == player]
            offsets = [entry.get("viewOffset") for entry in listed]
            if (a[0], b[0]) != (200, 200) or len(listed) != 1 or offsets[0] not in (61000, 62000):
                sessions_bad.append((a[0], b[0], offsets))
            # And two beats of the now-running session at once.
            a, b = barrier_pair(port, beat(63000), beat(64000), names=(player, player))
            listed = [entry for entry in rows(call(port, "GET", "/status/sessions")[1])
                      if (entry.get("Player") or {}).get("machineIdentifier") == player]
            offsets = [entry.get("viewOffset") for entry in listed]
            if (a[0], b[0]) != (200, 200) or len(listed) != 1 or offsets[0] not in (63000, 64000):
                sessions_bad.append(("running", a[0], b[0], offsets))
            call(port, "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                 "&state=stopped&time=64000&duration=600000"
                 % (TIMELINE_ITEMS[0], TIMELINE_ITEMS[0]), client=player)
        check("races: concurrent beats of one player make ONE session holding one of the offsets",
              not sessions_bad, sessions_bad[:5])

        # Two ratings of one row: both 200 and the row keeps one of the two values.
        bad = []
        for index in range(pairs):
            one, two = 2 + (index % 4) * 2, 3 + (index % 4) * 2
            a, b = barrier_pair(port, ("GET", "/:/rate?key=%s&identifier=%s&rating=%d" % (RATED, LIBRARY, one)),
                                ("GET", "/:/rate?key=%s&identifier=%s&rating=%d" % (RATED, LIBRARY, two)))
            final = row(port, RATED).get("userRating")
            if (a[0], b[0]) != (200, 200) or final not in (float(one), float(two)):
                bad.append((a[0], b[0], one, two, final))
        check("races: two concurrent ratings of one row leave one of the two values",
              not bad, bad[:5])

        # Two writers of DIFFERENT fields of one row -- a title edit, a summary edit and a
        # rating at once -- lose none of them (a read-modify-write of the row would).
        bad = []
        for index in range(pairs):
            title, summary, rating = "Race Title %d" % index, "Race summary %d" % index, 1 + index % 9
            a, b = barrier_pair(port, ("PUT", "/library/metadata/%s?title.value=%s&title.locked=1"
                                       % (EDITED, urllib.parse.quote(title))),
                                ("PUT", "/library/sections/1/all?type=1&id=%s&summary.value=%s"
                                        "&summary.locked=1" % (EDITED, urllib.parse.quote(summary))))
            c, d = barrier_pair(port, ("PUT", "/library/metadata/%s?title.value=%s%%21&title.locked=1"
                                       % (EDITED, urllib.parse.quote(title))),
                                ("GET", "/:/rate?key=%s&identifier=%s&rating=%d" % (EDITED, LIBRARY, rating)))
            final = row(port, EDITED)
            got = (final.get("title"), final.get("summary"), final.get("userRating"))
            want = (title + "!", summary, float(rating))
            if (a[0], b[0], c[0], d[0]) != (200,) * 4 or got != want:
                bad.append(((a[0], b[0], c[0], d[0]), got, want))
        check("races: concurrent writes of different fields of one row lose none of them",
              not bad, bad[:5])

        # Playlist membership: two adds answer two different leafCounts and keep both;
        # two removes answer two different leafCounts and remove both.
        bad = []
        for index in range(pairs):
            status, body = call(port, "POST", "/playlists?type=video&title=Race%%20%d&smart=0&uri=%s"
                                % (index, server.uri(MOVIES[0])))
            playlist = rows(body)[0]["ratingKey"]
            a, b = barrier_pair(port, ("PUT", "/playlists/%s/items?uri=%s" % (playlist, server.uri(MOVIES[1]))),
                                ("PUT", "/playlists/%s/items?uri=%s" % (playlist, server.uri(MOVIES[2]))))
            counts = sorted((rows(answer[1]) or [{}])[0].get("leafCount") or 0 for answer in (a, b))
            listed = rows(call(port, "GET", "/playlists/%s/items" % playlist)[1])
            keys = sorted(entry.get("ratingKey") for entry in listed)
            if (a[0], b[0]) != (200, 200) or counts != [2, 3] or keys != sorted(MOVIES[:3]):
                bad.append(("add", a[0], b[0], counts, keys))
            ids = {entry.get("ratingKey"): entry.get("playlistItemID") for entry in listed}
            a, b = barrier_pair(port, ("DELETE", "/playlists/%s/items/%s" % (playlist, ids.get(MOVIES[1]))),
                                ("DELETE", "/playlists/%s/items/%s" % (playlist, ids.get(MOVIES[2]))))
            counts = sorted((rows(answer[1]) or [{}])[0].get("leafCount") or 0 for answer in (a, b))
            keys = [entry.get("ratingKey") for entry in rows(call(port, "GET", "/playlists/%s/items" % playlist)[1])]
            if (a[0], b[0]) != (200, 200) or counts != [1, 2] or keys != [MOVIES[0]]:
                bad.append(("remove", a[0], b[0], counts, keys))
            call(port, "DELETE", "/playlists/%s" % playlist)
        check("races: concurrent playlist adds and removes answer two lists and keep both writes",
              not bad, "%d of %d pairs, e.g. %s" % (len(bad), pairs, bad[:4]))

        # Collection membership, the same way.
        bad = []
        for index in range(pairs):
            status, body = call(port, "POST", "/library/collections?type=1&title=Race%%20%d&smart=0"
                                "&sectionId=1&uri=%s" % (index, server.uri(MOVIES[0])))
            collection = rows(body)[0]["ratingKey"]
            a, b = barrier_pair(port, ("PUT", "/library/collections/%s/items?uri=%s" % (collection, server.uri(MOVIES[1]))),
                                ("PUT", "/library/collections/%s/items?uri=%s" % (collection, server.uri(MOVIES[2]))))
            counts = sorted((rows(answer[1]) or [{}])[0].get("childCount") or 0 for answer in (a, b))
            keys = sorted(entry.get("ratingKey") for entry in
                          rows(call(port, "GET", "/library/collections/%s/children" % collection)[1]))
            if (a[0], b[0]) != (200, 200) or counts != [2, 3] or keys != sorted(MOVIES[:3]):
                bad.append(("add", a[0], b[0], counts, keys))
            a, b = barrier_pair(port, ("DELETE", "/library/collections/%s/items/%s" % (collection, MOVIES[1])),
                                ("DELETE", "/library/collections/%s/items/%s" % (collection, MOVIES[2])))
            counts = sorted((rows(answer[1]) or [{}])[0].get("childCount") or 0 for answer in (a, b))
            keys = [entry.get("ratingKey") for entry in
                    rows(call(port, "GET", "/library/collections/%s/children" % collection)[1])]
            if (a[0], b[0]) != (200, 200) or counts != [1, 2] or keys != [MOVIES[0]]:
                bad.append(("remove", a[0], b[0], counts, keys))
            call(port, "DELETE", "/library/collections/%s" % collection)
        check("races: concurrent collection adds and removes answer two lists and keep both writes",
              not bad, "%d of %d pairs, e.g. %s" % (len(bad), pairs, bad[:4]))
        # A listing read while other clients create and delete lists. The oracle built
        # both listings by iterating the live dict, so a create or delete landing inside
        # the iteration was a 500 (`dictionary changed size during iteration`): 2 of
        # 330,000 load requests in a five-minute soak, 4 in 1,366 reads here.
        for kind, create, listing, delete in (
                ("collection", "/library/collections?type=1&title=Churn%%20%d&smart=0&sectionId=1&uri=%s",
                 "/library/sections/1/collections", "/library/collections/%s"),
                ("playlist", "/playlists?type=video&title=Churn%%20%d&smart=0&uri=%s",
                 "/playlists", "/playlists/%s")):
            seeder = Client(port, "cc-churn-seed")
            seeded = [rows(seeder.request("POST", create % (index, server.uri(MOVIES[0])))[1])[0]["ratingKey"]
                      for index in range(300)]
            stop_at = time.time() + 5
            statuses = collections.Counter()

            def churn(index):
                client = Client(port, "cc-churn-w%d" % index)
                while time.time() < stop_at:
                    status, body = client.request("POST", create % (index, server.uri(MOVIES[1])))[:2]
                    if status == 200:
                        client.request("DELETE", delete % rows(body)[0]["ratingKey"])
                client.close()

            def read(index):
                client = Client(port, "cc-churn-r%d" % index)
                while time.time() < stop_at:
                    statuses[client.request("GET", listing)[0]] += 1
                client.close()

            workers = [threading.Thread(target=churn, args=(index,)) for index in range(4)]
            workers += [threading.Thread(target=read, args=(index,)) for index in range(4)]
            [worker.start() for worker in workers]
            [worker.join() for worker in workers]
            for key in seeded:
                seeder.request("DELETE", delete % key)
            seeder.close()
            check("races: the %s listing answers 200 while other clients create and delete" % kind,
                  set(statuses) == {200}, dict(statuses))
        check("races: the server logged no failed handler", "handler failed" not in server.output()
              and "Traceback" not in server.output(), failure_excerpt(server.output()))
    finally:
        server.stop()


# -- 2. mixed load ------------------------------------------------------------------

READS = ["/identity", "/", "/library/sections", "/hubs?count=12",
         "/hubs/sections/1?count=12", "/hubs/sections/2?count=12",
         "/library/sections/1/all?type=1&includeCollections=1&X-Plex-Container-Start=0&X-Plex-Container-Size=50",
         "/library/sections/2/all?type=2", "/library/metadata/%s/children" % SHOW,
         "/library/metadata/%s?includeChildren=1" % SHOW, "/library/metadata/1007",
         "/library/metadata/1001", "/hubs/search?query=tears&limit=10",
         "/library/search?query=tru&limit=10", "/playlists", "/library/sections/1/collections",
         "/status/sessions", "/library/onDeck", "/hubs/continueWatching", "/:/prefs",
         "/library/sections/1/genre", "/playQueues?type=video"]


class Load:
    """Client threads mixing the UI's reads and writes, each keeping its own ledger.

    A ledger entry is (op, target, value, status, sent, answered). A write that got no
    answer is IN FLIGHT: it may or may not have landed, and the checks allow both.
    """

    def __init__(self, server, threads, prefix="cc-load"):
        self.server, self.threads, self.prefix = server, threads, prefix
        self.stop_event = threading.Event()
        self.ledgers = [[] for _ in range(threads)]
        self.errors = []
        self.failures = []          # (method, path, status) of every non-2xx answer
        self.counts = collections.Counter()
        self.identity = []          # (sent, latency) of the monitor's /identity reads
        self.monitor_errors = []
        self.workers = []

    def client_name(self, index):
        return "%s-%02d" % (self.prefix, index)

    def start(self):
        for index in range(self.threads):
            thread = threading.Thread(target=self.worker, args=(index,), daemon=True)
            thread.start()
            self.workers.append(thread)
        self.monitor = threading.Thread(target=self.watch_identity, daemon=True)
        self.monitor.start()

    def finish(self):
        self.stop_event.set()
        for thread in self.workers:
            thread.join(timeout=60)
        self.monitor.join(timeout=15)

    def watch_identity(self):
        client = Client(self.server.port, "%s-identity" % self.prefix, timeout=10)
        while not self.stop_event.is_set():
            status, body, sent, answered = client.request("GET", "/identity")
            if status != 200:
                self.monitor_errors.append((round(sent, 3), status, body if status is None else ""))
            self.identity.append((sent, answered - sent))
            time.sleep(0.05)
        client.close()

    def worker(self, index):
        rng = random.Random(index * 7919 + int(time.time()))
        client = Client(self.server.port, self.client_name(index))
        ledger = self.ledgers[index]
        server = self.server
        playing = None
        mine = {"playlists": [], "collections": []}
        episode = EPISODES[index] if index < len(EPISODES) else None
        serial = 0

        def send(op, method, path, target=None, value=None):
            status, body, sent, answered = client.request(method, path)
            self.counts[op] += 1
            if status is None:
                entry = (op, target, value, None, sent, None)
                if not getattr(self, "killed", False):
                    self.errors.append((op, method, path, body))
            else:
                entry = (op, target, value, status, sent, answered)
                if status >= 300 and not (op == "playlist-delete" and status == 204):
                    self.failures.append((method, path, status))
            ledger.append(entry)
            return status, body

        while not self.stop_event.is_set():
            serial += 1
            roll = rng.random()
            if roll < 0.55:
                send("read", "GET", rng.choice(READS))
            elif roll < 0.60:
                status, body = send("pq-create", "POST", "/playQueues?type=video&own=1&uri=%s"
                                    % server.uri(rng.choice(MOVIES)))
                queue = mc(body).get("playQueueID") if status == 200 else None
                if status == 200:
                    ledger[-1] = ledger[-1][:2] + (queue,) + ledger[-1][3:]
                if queue:
                    send("pq-add", "PUT", "/playQueues/%s?uri=%s" % (queue, server.uri(rng.choice(MOVIES))),
                         target=queue)
            elif roll < 0.68:
                item = TIMELINE_ITEMS[index % 2]
                state = "stopped" if playing else "playing"
                send("timeline", "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                     "&state=%s&time=%d&duration=600000" % (item, item, state, 60000 + serial),
                     target=item, value=state)
                playing = None if playing else item
            elif roll < 0.73:
                item = rng.choice(SCROBBLE_ONLY)
                send("scrobble", "GET", "/:/scrobble?key=%s&identifier=%s" % (item, LIBRARY), target=item)
            elif roll < 0.78 and episode:
                verb = rng.choice(["scrobble", "unscrobble"])
                send("toggle-" + verb, "GET", "/:/%s?key=%s&identifier=%s" % (verb, episode, LIBRARY),
                     target=episode, value=verb)
            elif roll < 0.82:
                rating = rng.randint(1, 10)
                if index == 0:
                    send("owned-rate", "GET", "/:/rate?key=%s&identifier=%s&rating=%d"
                         % (OWNED_RATING, LIBRARY, rating), target=OWNED_RATING, value=float(rating))
                else:
                    send("rate", "GET", "/:/rate?key=%s&identifier=%s&rating=%d"
                         % (RATED, LIBRARY, rating), target=RATED, value=float(rating))
            elif roll < 0.86:
                text = "%s %d %d" % (self.client_name(index), serial, rng.randint(0, 10 ** 6))
                if index == 1:
                    send("owned-edit", "PUT", "/library/sections/1/all?type=1&id=%s&summary.value=%s"
                         "&summary.locked=1" % (OWNED_SUMMARY, urllib.parse.quote(text)),
                         target=OWNED_SUMMARY, value=text)
                else:
                    send("edit", "PUT", "/library/metadata/%s?summary.value=%s&summary.locked=1"
                         % (EDITED, urllib.parse.quote(text)), target=EDITED, value=text)
            elif roll < 0.93:
                if mine["playlists"] and rng.random() < 0.5:
                    playlist = mine["playlists"].pop(rng.randrange(len(mine["playlists"])))
                    send("playlist-delete", "DELETE", "/playlists/%s" % playlist, target=playlist)
                else:
                    title = "%s list %d" % (self.client_name(index), serial)
                    status, body = send("playlist-create", "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                                        % (urllib.parse.quote(title), server.uri(rng.choice(MOVIES))), value=title)
                    created = (rows(body) or [{}])[0].get("ratingKey") if status == 200 else None
                    ledger[-1] = ledger[-1][:1] + (created,) + ledger[-1][2:]
                    if created:
                        mine["playlists"].append(created)
                        if rng.random() < 0.5:
                            send("playlist-add", "PUT", "/playlists/%s/items?uri=%s"
                                 % (created, server.uri(rng.choice(MOVIES))), target=created)
            else:
                if mine["collections"] and rng.random() < 0.5:
                    collection = mine["collections"].pop(rng.randrange(len(mine["collections"])))
                    send("collection-delete", "DELETE", "/library/collections/%s" % collection,
                         target=collection)
                else:
                    title = "%s coll %d" % (self.client_name(index), serial)
                    status, body = send("collection-create", "POST", "/library/collections?type=1&title=%s"
                                        "&smart=0&sectionId=1&uri=%s"
                                        % (urllib.parse.quote(title), server.uri(rng.choice(MOVIES))),
                                        value=title)
                    created = (rows(body) or [{}])[0].get("ratingKey") if status == 200 else None
                    ledger[-1] = ledger[-1][:1] + (created,) + ledger[-1][2:]
                    if created:
                        mine["collections"].append(created)
                        if rng.random() < 0.5:
                            send("collection-add", "PUT", "/library/collections/%s/items?uri=%s"
                                 % (created, server.uri(rng.choice(MOVIES))), target=created)
        # A tab that is closed stops its player: the load leaves no session behind.
        if playing and not getattr(self, "killed", False):
            send("timeline", "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                 "&state=stopped&time=60000&duration=600000" % (playing, playing),
                 target=playing, value="stopped")
        client.close()

    # -- what the ledgers say the server must now hold ----------------------------

    def entries(self, *ops):
        return [(index, entry) for index, ledger in enumerate(self.ledgers)
                for entry in ledger if entry[0] in ops]

    def expected(self, cutoff=None):
        """What must be there, and what MAY be there, from the answers received.

        `cutoff`: only answers received before it count as answered (a kill); every
        later answer and every request without one is in flight.
        """
        def answered(entry):
            return entry[3] is not None and (cutoff is None or entry[5] < cutoff)

        def ok(entry):
            return answered(entry) and 200 <= entry[3] < 300

        def unknown(entry):
            return not answered(entry)

        want = {}
        for kind in ("playlist", "collection"):
            created, maybe, deleted, maybe_deleted = set(), set(), set(), set()
            for _, entry in self.entries(kind + "-create"):
                if entry[1] and ok(entry):
                    created.add(entry[1])
            for _, entry in self.entries(kind + "-delete"):
                if ok(entry):
                    deleted.add(entry[1])
                elif unknown(entry):
                    maybe_deleted.add(entry[1])
            # A create whose answer was lost after the kill has no id to look for; a
            # listed row the ledger cannot name is allowed only while such a create is.
            lost = sum(1 for _, entry in self.entries(kind + "-create") if unknown(entry))
            want[kind] = (created - deleted - maybe_deleted, deleted, maybe_deleted, lost)
        scrobbles = {}
        for item in SCROBBLE_ONLY:
            done = sum(1 for _, entry in self.entries("scrobble") if entry[1] == item and ok(entry))
            flying = sum(1 for _, entry in self.entries("scrobble") if entry[1] == item and unknown(entry))
            scrobbles[item] = (done, done + flying)
        # One owner writes these in order, so what was kept is the state after the last
        # answered write or after any later write in flight: a server that keeps serving
        # open connections after a SIGTERM can land several, and a write whose answer
        # was lost to the shutdown may still have landed.
        toggles = {}
        for index, ledger in enumerate(self.ledgers):
            if index >= len(EPISODES):
                continue
            count, possible = 0, None
            for entry in ledger:
                if not entry[0].startswith("toggle-"):
                    continue
                if possible is None and not unknown(entry):
                    if ok(entry):
                        count = count + 1 if entry[2] == "scrobble" else 0
                    continue
                possible = possible or {count}
                count = count + 1 if entry[2] == "scrobble" else 0
                possible.add(count)
            toggles[EPISODES[index]] = possible or {count}
        owned = {}
        for op, item in (("owned-rate", OWNED_RATING), ("owned-edit", OWNED_SUMMARY)):
            last, possible = None, None
            for _, entry in self.entries(op):
                if possible is None and not unknown(entry):
                    if ok(entry):
                        last = entry[2]
                    continue
                possible = possible or {last}
                possible.add(entry[2])
            owned[item] = possible or {last}
        shared = {}
        for op, item in (("rate", RATED), ("edit", EDITED)):
            shared[item] = {entry[2] for _, entry in self.entries(op)
                            if ok(entry) or unknown(entry)}
        # A client's newer queue retires its older one, so a create in flight leaves the
        # last answered queue's fate open.
        queues = {}
        for index, ledger in enumerate(self.ledgers):
            creates = [entry for entry in ledger if entry[0] == "pq-create"]
            if creates and ok(creates[-1]) and creates[-1][2]:
                queues[self.client_name(index)] = creates[-1][2]
        return {"lists": want, "scrobbles": scrobbles, "toggles": toggles,
                "owned": owned, "shared": shared, "queues": queues}


def verify_state(port, load, expected, baseline, label, record=True):
    """Compare the server's state with what the ledgers say must be there."""
    problems = []
    listings = {"playlist": ("/playlists", "/playlists/%s/items", "leafCount"),
                "collection": ("/library/sections/1/collections",
                               "/library/collections/%s/children", "childCount")}
    for kind, (listing, children, count_field) in listings.items():
        status, body = call(port, "GET", listing)
        listed = rows(body)
        keys = [entry.get("ratingKey") for entry in listed]
        if status != 200:
            problems.append("%s listing answered %s" % (kind, status))
        if len(keys) != len(set(keys)):
            problems.append("%s listing has duplicate ids %s" % (
                kind, [key for key, n in collections.Counter(keys).items() if n > 1]))
        present, deleted, maybe_deleted, lost = expected["lists"][kind]
        keys = set(keys) - baseline[kind]
        missing = present - keys
        resurrected = keys & deleted
        unexplained = keys - present - maybe_deleted
        if missing:
            problems.append("%s: %d answered creates missing, e.g. %s" % (kind, len(missing), sorted(missing)[:5]))
        if resurrected:
            problems.append("%s: %d answered deletes still listed %s" % (kind, len(resurrected), sorted(resurrected)[:5]))
        if len(unexplained) > lost:
            problems.append("%s: %d listed that no answered create made (%d in flight) %s" % (
                kind, len(unexplained), lost, sorted(unexplained)[:5]))
        for entry in listed:
            status, body = call(port, "GET", children % entry.get("ratingKey"))
            if status != 200:
                problems.append("%s %s is listed but its items answer %s" % (kind, entry.get("ratingKey"), status))
            elif len(rows(body)) != (entry.get(count_field) or 0):
                problems.append("%s %s lists %s=%s but holds %d" % (
                    kind, entry.get("ratingKey"), count_field, entry.get(count_field), len(rows(body))))
    for item, (low, high) in expected["scrobbles"].items():
        count = row(port, item).get("viewCount") or 0
        if not low <= count <= high:
            problems.append("viewCount of %s is %s, %d scrobbles answered, %d in flight" % (
                item, count, low, high - low))
    for item, possible in expected["toggles"].items():
        count = row(port, item).get("viewCount") or 0
        if count not in possible:
            problems.append("viewCount of episode %s is %s, the owner's answers say %s" % (
                item, count, sorted(possible)))
    for item, possible in expected["owned"].items():
        got = row(port, item)
        value = got.get("userRating") if item == OWNED_RATING else got.get("summary")
        if None in possible and len(possible) == 1:
            continue
        if value not in possible:
            problems.append("%s of %s is %r, the owner's last answered write says %r" % (
                "userRating" if item == OWNED_RATING else "summary", item, value, possible))
    for item, possible in expected["shared"].items():
        got = row(port, item)
        value = got.get("userRating") if item == RATED else got.get("summary")
        if possible and value not in possible:
            problems.append("%s of %s is %r, which no writer sent" % (
                "userRating" if item == RATED else "summary", item, value))
    unread = []
    for client, queue in expected["queues"].items():
        status = call(port, "GET", "/playQueues/%s" % queue, client=client)[0]
        if status != 200:
            unread.append((client, queue, status))
    if unread:
        problems.append("%d clients' last answered play queue unreadable %s" % (len(unread), unread[:4]))
    if record:
        check("%s: the state holds exactly what the answers said" % label, not problems,
              "; ".join(problems))
    elif problems:
        note("%s: %s" % (label, "; ".join(problems)[:600]))
    return problems


def baseline_lists(port):
    return {"playlist": {entry.get("ratingKey") for entry in rows(call(port, "GET", "/playlists")[1])},
            "collection": {entry.get("ratingKey") for entry in
                           rows(call(port, "GET", "/library/sections/1/collections")[1])}}


def load_report(load, label, seconds, identity_limit=1.0):
    total = sum(load.counts.values())
    latencies = sorted(latency for _, latency in load.identity)
    worst = latencies[-1] if latencies else None
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else None
    note("%s: %d requests in %.0f s (%.0f/s) by %d threads; /identity p99 %.3f s, max %.3f s over %d reads" % (
        label, total, seconds, total / max(seconds, 1), load.threads, p99 or 0, worst or 0, len(latencies)))
    note("%s: ops %s" % (label, dict(load.counts)))
    return worst


def replay_alone(port, failures):
    """Every non-2xx answer under load, re-sent alone: the ones that now differ."""
    differing = []
    for method, path, status in sorted(set(failures))[:40]:
        alone = call(port, method, path, client="cc-alone")[0]
        if alone != status:
            differing.append((method, path, status, alone))
    return differing


def prop_load(catalog, seconds, threads, reaper_seconds=None, during=None):
    """The mixed load on one server; `during` runs while the load is going."""
    server = Server(catalog)
    port = server.port
    try:
        baseline = baseline_lists(port)
        # What one request costs where requests serialise. The native server routed
        # every JSON answer through a re-read and re-parse of the 1.4 MB
        # `translations.json` under its runtime lock: 8-13 ms a request, /identity
        # included, which held 32 clients to about 120 requests a second between them.
        lone = Client(port, "cc-lone")
        lone.request("GET", "/identity")
        began = time.time()
        for _ in range(50):
            lone.request("GET", "/identity")
        lone_seconds = time.time() - began
        lone.close()
        check("load: 50 sequential /identity reads from one client take under 0.25 s",
              lone_seconds < 0.25, "%.3f s (%.1f ms each)" % (lone_seconds, lone_seconds * 20))
        load = Load(server, threads)
        abandoned = {}
        started = time.time()
        load.start()
        if reaper_seconds:
            # Players that beat once, early in the load, and go silent.
            time.sleep(1.0)
            for index in range(6):
                player = "cc-silent-%d" % index
                item = TIMELINE_ITEMS[index % 2]
                status = call(port, "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                              "&state=playing&time=60000&duration=600000" % (item, item), client=player)[0]
                abandoned[player] = (time.time(), status)
            abandoned = watch_reaper(port, abandoned)
        if during:
            during(server)
        time.sleep(max(0, seconds - (time.time() - started)))
        load.finish()
        elapsed = time.time() - started
        worst = load_report(load, "load", elapsed)
        check("load: no connection error from %d threads over %.0f s" % (threads, elapsed),
              not load.errors and not load.monitor_errors,
              "%d errors, e.g. %s; monitor %s" % (len(load.errors), load.errors[:3], load.monitor_errors[:3]))
        differing = replay_alone(port, load.failures)
        check("load: every non-2xx answer under load is the same request's answer alone",
              not differing, "%d distinct failing requests differ alone, e.g. %s (all failures: %s)" % (
                  len(differing), differing[:4], collections.Counter(s for _, _, s in load.failures)))
        check("load: /identity answered within 1 s throughout", worst is not None and worst < 1.0,
              "worst %.3f s" % (worst or -1))
        verify_state(port, load, load.expected(), baseline, "load")
        leftover = [entry for entry in rows(call(port, "GET", "/status/sessions")[1])
                    if ((entry.get("Player") or {}).get("machineIdentifier") or "").startswith("cc-load")]
        check("load: a player that stopped leaves no session behind", not leftover,
              [(entry.get("Player") or {}).get("machineIdentifier") for entry in leftover])
        ids = [entry[1] for _, entry in load.entries("playlist-create", "collection-create") if entry[1]]
        check("load: no id was handed out twice", len(ids) == len(set(ids)),
              [key for key, n in collections.Counter(ids).items() if n > 1][:5])
        check("load: the server logged no failed handler", "handler failed" not in server.output()
              and "Traceback" not in server.output(), failure_excerpt(server.output()))
        return server, abandoned
    except BaseException:
        server.stop()
        raise


def watch_reaper(port, abandoned, deadline_margin=212):
    """Poll Now Playing, from now on and alongside the load, until every abandoned
    player is gone or overdue; the thread's `gone` maps each to its age when missed."""
    gone = {}

    def poll():
        last = max(at for at, _ in abandoned.values())
        while time.time() - last < deadline_margin + 2 and len(gone) < len(abandoned):
            try:
                listed = {(entry.get("Player") or {}).get("machineIdentifier")
                          for entry in rows(call(port, "GET", "/status/sessions")[1])}
            except Exception:  # noqa: BLE001 -- a missed poll only widens the window
                time.sleep(1.0)
                continue
            now = time.time()
            for player, (at, _) in abandoned.items():
                if player not in listed and player not in gone:
                    gone[player] = now - at
            oldest = min(at for at, _ in abandoned.values())
            time.sleep(1.0 if now - oldest > 170 else 5.0)

    thread = threading.Thread(target=poll, daemon=True)
    thread.gone, thread.abandoned, thread.margin = gone, abandoned, deadline_margin
    thread.start()
    return thread


def prop_reaper(watch):
    """The watcher's verdict: listed 180 s, gone by the tick after, under the load."""
    abandoned, gone, deadline_margin = watch.abandoned, watch.gone, watch.margin
    check("reaper: the silent players' beats were answered",
          all(status == 200 for _, status in abandoned.values()), abandoned)
    watch.join()
    early = {player: round(age, 1) for player, age in gone.items() if age < 180}
    late = {player: (round(gone[player], 1) if player in gone else "never")
            for player in abandoned if player not in gone or gone[player] > deadline_margin}
    note("reaper: gone after %s s" % sorted(round(age, 1) for age in gone.values()))
    check("reaper: a player that went silent during the load stays listed 180 s",
          not early, early)
    check("reaper: and is gone by the next 30-second tick, within 210 s", not late, late)


# -- 3. listen backlog -------------------------------------------------------------

def prop_backlog(catalog):
    server = Server(catalog)
    port = server.port
    try:
        for count in (30, 128):
            sockets, statuses = [None] * count, [None] * count
            barrier = threading.Barrier(count)
            go = threading.Barrier(count)

            def run(index):
                barrier.wait()
                try:
                    sockets[index] = socket.create_connection(("127.0.0.1", port), timeout=15)
                except OSError as error:
                    statuses[index] = "connect: %s" % error
                    go.wait()
                    return
                go.wait()
                try:
                    sockets[index].sendall(b"GET /identity HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                           b"Accept: application/json\r\nConnection: close\r\n\r\n")
                    data = b""
                    while True:
                        chunk = sockets[index].recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    statuses[index] = int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/1.1 ") \
                        else "no answer %r" % data[:40]
                except (OSError, ValueError, IndexError) as error:
                    statuses[index] = "%s: %s" % (type(error).__name__, error)
                finally:
                    sockets[index].close()

            threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
            [thread.start() for thread in threads]
            [thread.join(timeout=60) for thread in threads]
            answered = sum(1 for status in statuses if status == 200)
            check("backlog: %d connections opened at once are all answered" % count,
                  answered == count, "%d/%d, e.g. %s" % (
                      answered, count, collections.Counter(map(str, statuses)).most_common(3)))
    finally:
        server.stop()


# -- 4. --state under load ---------------------------------------------------------

def restricted_pickle(blob):
    class Refuse(pickle.Unpickler):
        def find_class(self, module, name):
            raise pickle.UnpicklingError("class %s.%s" % (module, name))
    return Refuse(__import__("io").BytesIO(blob)).load()


def state_parses(path):
    try:
        blob = open(path, "rb").read()
    except OSError as error:
        return False, str(error)
    try:
        document = json.loads(blob) if blob[:1] == b"{" else restricted_pickle(blob)
        return isinstance(document, dict), type(document).__name__
    except Exception as error:  # noqa: BLE001
        return False, "%s: %s" % (type(error).__name__, error)


def prop_state(catalog, seconds, threads, sig, label):
    work = tempfile.mkdtemp(prefix="plexsim-cc-state-")
    server = None
    try:
        state = os.path.join(work, "state")
        server = Server(catalog, state)
        port = server.port
        baseline = baseline_lists(port)
        load = Load(server, threads, prefix="cc-%s" % label)
        started = time.time()
        load.start()
        time.sleep(seconds * random.uniform(0.6, 1.0))
        load.killed = True
        killed_at = time.time()
        took = server.stop(sig)
        load.finish()
        elapsed = killed_at - started
        worst = load_report(load, label, elapsed)
        check("%s: /identity answered within 1 s while the saver ran" % label,
              worst is not None and worst < 1.0, "worst %.3f s" % (worst or -1))
        if sig == signal.SIGTERM:
            check("%s: the server exits within 10 s of the signal" % label,
                  took is not None and took < 10, "took %s" % took)
        parses, detail = state_parses(state)
        check("%s: the state file parses" % label, parses, detail)
        before = server.output()
        unsaved = [line for line in before.splitlines() if "state not saved" in line]
        if unsaved:
            note("%s: the server logged %d unsaved states, e.g. %s" % (label, len(unsaved), unsaved[0][-200:]))
        server.start()
        port = server.port
        restored = "unreadable" not in server.output() and not os.path.exists(state + ".corrupt")
        check("%s: the server starts on it and restores it" % label, restored, server.output()[-800:])
        answers = sum(1 for ledger in load.ledgers for entry in ledger
                      if entry[0] != "read" and entry[3] is not None and entry[5] < killed_at)
        older = sum(1 for ledger in load.ledgers for entry in ledger
                    if entry[0] != "read" and entry[3] is not None and entry[5] < killed_at - 2)
        note("%s: %d writes answered before the signal, %d of them more than 2 s before" % (
            label, answers, older))
        # The target is the measured rule, every answered write; the two-second slack
        # the saver's interval would allow is reported separately so a miss says which.
        problems = verify_state(port, load, load.expected(cutoff=killed_at), baseline,
                                "%s: every answered write survives" % label)
        if problems:
            verify_state(port, load, load.expected(cutoff=killed_at - 2), baseline,
                         "%s: of the writes answered 2 s before the signal, lost" % label,
                         record=False)
        devices = {device.get("clientIdentifier") for device in
                   rows(call(port, "GET", "/devices")[1], "Device")}
        seen = {load.client_name(index) for index, ledger in enumerate(load.ledgers)
                if any(entry[3] is not None and entry[5] < killed_at for entry in ledger)}
        check("%s: every client answered before the signal is in the device table" % label,
              seen <= devices, sorted(seen - devices)[:5])
        # Cycle 73, seven restarts of plexscratch: ids keep counting UP across a restart.
        # A counter re-derived from the surviving rows hands out a deleted list's id
        # again, and one not kept restarts `playlistItemID` beside restored items.
        handed = collections.defaultdict(set)
        for _, entry in load.entries("playlist-create", "collection-create", "pq-create"):
            key = entry[2] if entry[0] == "pq-create" else entry[1]
            if key:
                handed[entry[0]].add(int(key))
        status, body = call(port, "POST", "/playlists?type=video&title=After&smart=0&uri=%s"
                            % server.uri(MOVIES[0]))
        new_playlist = int((rows(body) or [{}])[0].get("ratingKey") or 0)
        status, body = call(port, "POST", "/library/collections?type=1&title=After&smart=0"
                            "&sectionId=1&uri=%s" % server.uri(MOVIES[0]))
        new_collection = int((rows(body) or [{}])[0].get("ratingKey") or 0)
        status, body = call(port, "POST", "/playQueues?type=video&own=1&uri=%s"
                            % server.uri(MOVIES[0]), client="cc-after")
        new_queue = int(mc(body).get("playQueueID") or 0)
        reused = [(kind, new, max(handed[kind])) for kind, new in (
            ("playlist-create", new_playlist), ("collection-create", new_collection),
            ("pq-create", new_queue)) if handed[kind] and new <= max(handed[kind])]
        check("%s: ids count on from the highest handed out before the signal" % label,
              not reused, "new id <= highest before: %s" % reused)
        # And every playlist's item ids stay distinct when one gains an item after it.
        listed = rows(call(port, "GET", "/playlists")[1])
        item_ids = []
        for entry in listed:
            if entry.get("ratingKey") != str(new_playlist) and not entry.get("smart"):
                call(port, "PUT", "/playlists/%s/items?uri=%s" % (entry["ratingKey"], server.uri(MOVIES[5])))
                break
        for entry in rows(call(port, "GET", "/playlists")[1]):
            item_ids += [item.get("playlistItemID") for item in
                         rows(call(port, "GET", "/playlists/%s/items" % entry["ratingKey"])[1])]
        doubled = [key for key, n in collections.Counter(item_ids).items() if n > 1]
        check("%s: no playlistItemID is held twice after the restart" % label, not doubled,
              "%d doubled, e.g. %s" % (len(doubled), doubled[:5]))
        server.stop()
    finally:
        if server is not None and server.proc.poll() is None:
            server.stop()
        shutil.rmtree(work, ignore_errors=True)


def prop_state_quiet(catalog):
    """One write on a quiet server, answered, then SIGKILL: is it on disk?

    Under load some other write has always left the state dirty, so a write that does
    not mark it can still ride that save -- the native server lost a play queue that
    way in 1 of 5 loaded kills. On a quiet server nothing else saves it: the saver has
    flushed, the device is known, and the kill follows the answer at once.
    """
    work = tempfile.mkdtemp(prefix="plexsim-cc-quiet-")
    server = None
    try:
        state = os.path.join(work, "state")
        server = Server(catalog, state)
        client = "cc-quiet"
        call(server.port, "GET", "/identity", client=client)
        status, body = call(server.port, "POST", "/playQueues?type=video&own=1&uri=%s"
                            % server.uri(MOVIES[0]), client=client)
        queue = mc(body).get("playQueueID")
        writes = [
            ("a play-queue create", "POST", "/playQueues?type=video&own=1&uri=%s" % server.uri(MOVIES[1]),
             lambda port, answer: call(port, "GET", "/playQueues/%s" % mc(answer).get("playQueueID"),
                                       client=client)[0] == 200),
            ("a play-queue add", "PUT", "/playQueues/%s?uri=%s" % (queue, server.uri(MOVIES[2])),
             lambda port, answer: MOVIES[2] in [entry.get("ratingKey") for entry in rows(
                 call(port, "GET", "/playQueues/%s" % queue, client=client)[1])]),
            ("a playlist create", "POST", "/playlists?type=video&title=Quiet&smart=0&uri=%s" % server.uri(MOVIES[0]),
             lambda port, answer: call(port, "GET", "/playlists/%s" % rows(answer)[0]["ratingKey"])[0] == 200),
            ("a rating", "GET", "/:/rate?key=%s&identifier=%s&rating=6" % (MOVIES[3], LIBRARY),
             lambda port, answer: row(port, MOVIES[3]).get("userRating") == 6.0),
        ]
        for label, method, path, survived in writes:
            time.sleep(2.5)                 # the saver's interval: nothing is left dirty
            status, answer = call(server.port, method, path, client=client)
            server.stop(signal.SIGKILL)
            server.start()
            kept = status == 200 and survived(server.port, answer)
            if label == "a play-queue create":
                # The kept queue is now the client's; the next checks add to the first.
                status, body = call(server.port, "POST", "/playQueues?type=video&own=1&uri=%s"
                                    % server.uri(MOVIES[0]), client=client)
                queue = mc(body).get("playQueueID")
                writes[1] = ("a play-queue add", "PUT", "/playQueues/%s?uri=%s" % (queue, server.uri(MOVIES[2])),
                             writes[1][3])
            check("state: %s answered on a quiet server survives an immediate SIGKILL" % label,
                  kept, "status %s" % status)
    finally:
        if server is not None and server.proc.poll() is None:
            server.stop()
        shutil.rmtree(work, ignore_errors=True)


def prop_save_stall(catalog):
    """A client write's save must not hold up the clients that are only reading.

    Eight clients rate one movie back to back with `--state`, each rating saved before
    it is answered, while a ninth reads `/identity`. The native server wrote and
    fsynced the whole state file under its runtime lock, so the reader queued behind
    every save: a median of 22.8 ms against the oracle's 0.6 ms, which saves outside
    its request lock (and 0.9 ms once the native write moved out of it).
    """
    work = tempfile.mkdtemp(prefix="plexsim-cc-stall-")
    server = None
    try:
        server = Server(catalog, os.path.join(work, "state"))
        stop_at = time.time() + 6
        latencies, writes = [], [0]

        def write(index):
            client = Client(server.port, "cc-stall-w%d" % index)
            while time.time() < stop_at:
                client.request("GET", "/:/rate?key=%s&identifier=%s&rating=%d"
                               % (RATED, LIBRARY, 1 + index % 9))
                writes[0] += 1
            client.close()

        def read():
            client = Client(server.port, "cc-stall-r")
            time.sleep(0.5)
            while time.time() < stop_at:
                status, _, sent, answered = client.request("GET", "/identity")
                latencies.append(answered - sent)
                time.sleep(0.01)
            client.close()

        workers = [threading.Thread(target=write, args=(index,)) for index in range(8)]
        workers.append(threading.Thread(target=read))
        [worker.start() for worker in workers]
        [worker.join() for worker in workers]
        latencies.sort()
        median = latencies[len(latencies) // 2] if latencies else None
        note("saver: %d saved writes; /identity median %.1f ms, p90 %.1f ms, max %.1f ms" % (
            writes[0], (median or 0) * 1e3, latencies[int(len(latencies) * 0.9)] * 1e3,
            latencies[-1] * 1e3))
        check("saver: /identity's median stays under 5 ms while 8 clients' writes are saved",
              median is not None and median < 0.005, "median %.1f ms" % ((median or 0) * 1e3))
    finally:
        if server is not None and server.proc.poll() is None:
            server.stop()
        shutil.rmtree(work, ignore_errors=True)


# -- 6. slow and stalled clients -----------------------------------------------------

def timed_reads(port, count=20, path="/identity"):
    worst = 0.0
    failures = []
    for _ in range(count):
        started = time.time()
        try:
            status = call(port, "GET", path, timeout=10)[0]
        except Exception as error:  # noqa: BLE001
            status = "%s" % type(error).__name__
        worst = max(worst, time.time() - started)
        if status != 200:
            failures.append(status)
        time.sleep(0.05)
    return worst, failures


def prop_slow(catalog):
    server = Server(catalog)
    port = server.port
    try:
        quiet, _ = timed_reads(port, 10)

        # Half a request line and headers, then nothing (the 20 s window has not run out).
        stalled = []
        for index in range(20):
            sock = socket.create_connection(("127.0.0.1", port), timeout=30)
            sock.sendall(b"GET /library/sections HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Plex-Clie")
            stalled.append(sock)
        body = socket.create_connection(("127.0.0.1", port), timeout=30)
        body.sendall(b"PUT /:/prefs?FriendlyName=x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Length: 100000\r\n\r\npartial")
        stalled.append(body)
        worst, failures = timed_reads(port, 20)
        check("slow: 21 half-sent requests delay nobody (worst %.3f s, quiet %.3f s)" % (worst, quiet),
              worst < 1.0 and not failures, "worst %.3f s, failures %s" % (worst, failures))

        # A client that asks for large answers and does not read them.
        greedy = socket.socket()
        greedy.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        greedy.settimeout(30)
        greedy.connect(("127.0.0.1", port))
        one = (b"GET /:/prefs HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: application/json\r\n"
               b"X-Plex-Client-Identifier: cc-greedy\r\n\r\n")
        greedy.sendall(one * 400)
        time.sleep(1.0)
        worst, failures = timed_reads(port, 20)
        worst_list, failures_list = timed_reads(port, 5, "/library/sections/1/all")
        check("slow: a client that stops reading its answers delays nobody (worst %.3f s)" % max(worst, worst_list),
              max(worst, worst_list) < 1.0 and not failures and not failures_list,
              "worst %.3f/%.3f s, failures %s %s" % (worst, worst_list, failures, failures_list))
        # And it still gets every byte once it reads, slowly.
        received = 0
        greedy.settimeout(5)
        deadline = time.time() + 20
        answers = 0
        pending = b""
        while time.time() < deadline and answers < 400:
            try:
                chunk = greedy.recv(16384)
            except socket.timeout:
                break
            if not chunk:
                break
            received += len(chunk)
            pending += chunk
            answers += pending.count(b"HTTP/1.1 200")
            pending = pending[-20:]
        greedy.close()
        check("slow: the slow reader receives all 400 answers in the end", answers >= 400,
              "%d answers, %d bytes" % (answers, received))

        # Fifty idle keep-alive sockets, each having had one answer.
        idle = []
        for index in range(50):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request("GET", "/identity", headers=headers_for("cc-idle-%d" % index))
            connection.getresponse().read()
            idle.append(connection)
        worst, failures = timed_reads(port, 20)
        check("slow: 50 idle keep-alive sockets delay nobody (worst %.3f s)" % worst,
              worst < 1.0 and not failures, "worst %.3f s, failures %s" % (worst, failures))
        reused = 0
        for index, connection in enumerate(idle):
            try:
                connection.request("GET", "/identity", headers=headers_for("cc-idle-%d" % index))
                reused += connection.getresponse().status == 200
            except Exception:  # noqa: BLE001
                pass
            connection.close()
        check("slow: each idle keep-alive socket still serves its next request", reused == 50,
              "%d/50" % reused)
        for sock in stalled:
            sock.close()
        check("slow: the server logged no failed handler", "handler failed" not in server.output()
              and "Traceback" not in server.output(), failure_excerpt(server.output()))
    finally:
        server.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--long", action="store_true", help="soak the load for five minutes")
    parser.add_argument("--only", default="races,load,backlog,state,reaper,slow",
                        help="comma-separated properties to run")
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--seconds", type=float, help="override the load's duration")
    parser.add_argument("--pairs", type=int, default=30, help="barrier pairs per race")
    args = parser.parse_args()
    only = set(args.only.split(","))
    started = time.time()
    work = tempfile.mkdtemp(prefix="plexsim-cc-")
    try:
        catalog = os.path.join(work, "catalog.json")
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
        if "races" in only:
            prop_races(catalog, args.pairs)
        if "backlog" in only:
            prop_backlog(catalog)
        if "slow" in only:
            prop_slow(catalog)
        if only & {"load", "reaper", "state"}:
            seconds = args.seconds or (300 if args.long else 60)
            reaper = "reaper" in only
            if "load" in only or reaper:
                server, abandoned = prop_load(catalog, seconds, args.threads,
                                              reaper_seconds=reaper or None)
            else:
                server, abandoned = None, {}
            try:
                # The state runs use their own servers, so they fill the reaper's wait.
                if "state" in only:
                    state_seconds = args.seconds or (60 if args.long else 20)
                    prop_state(catalog, state_seconds, args.threads, signal.SIGKILL, "sigkill")
                    prop_state(catalog, state_seconds, args.threads, signal.SIGTERM, "sigterm")
                    prop_state_quiet(catalog)
                    prop_save_stall(catalog)
                if reaper and abandoned:
                    prop_reaper(abandoned)
            finally:
                if server:
                    server.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n%.0f s" % (time.time() - started))
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
