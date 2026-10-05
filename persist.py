#!/usr/bin/env python3
"""Keeping what the UI changes across a restart.

A real PMS keeps its library, watch state, lists, history, preferences and device
table in its database, so a restart changes nothing a client can see. plexsim kept
all of it in memory and reloaded `catalog.json`, which made a restart the undo
button: a watched episode came back unwatched, a playlist vanished, a renamed server
took its old name back.

This is the default: `state.pickle` beside the catalog (the native server's is
`state.json`), or `--state FILE` / `$PLEXSIM_STATE`. `--no-state`, or an empty
`$PLEXSIM_STATE`, turns it off, and the test suites run that way: `test_ui_surface.py`
writes rows, lists and sections, and its checks assume every instance it starts
begins from the fixture. The default file is the server's data directory, held by an
advisory lock for the life of the process; a second server finding it held refuses to
start, as a second PMS on a served data directory does (`resolve_path`).

What is kept is the state the server itself mutates, and nothing it derives or
only holds for a live connection:

    catalog    every row as edited (watch state, ratings, edits, locks, artwork,
               merges, splits, deletes), the sections and their indexes, the
               library-wide show settings, and the retired-key set a split reads
    library    playlists, collections, the tag table, play history, view counts
               and the id counters behind all of them
    server     server, section and agent preferences, custom metadata-provider
               groups, uploaded artwork and subtitles, minted theme rows, play
               and download queues
    sessions   the device table and the bandwidth and play statistics; never a
               live session, which a real PMS drops with its process too

Everything goes through ONE pickle so object identity survives: several paths
test that a row IS the catalog's own (`items.get(key) is row`), and a stored list
that shared a row with the catalog must still share it after a load. Only plain
containers and scalars are admitted in either direction -- `_PlainPickler` refuses
anything else when saving, and `_PlainUnpickler` resolves no class at all when
loading, so a state file cannot name code to run.

The snapshot is tied to the catalog it was taken over, by digest. A rebuilt
catalog has different rows and keys, so its structure cannot be overlaid; then
only the per-item view state (matched by guid, then by part file) and the
server-level state carry over, and the old file is kept beside the new one as
`<file>.stale`.

A restored row keeps the shape the code gave it when the catalog was first loaded.
`Catalog.__init__` re-applies the measured row and media rules to every row it reads
(`hierarchy.normalize_rows`, `mediainfo.normalize_media`), and they are not run over
restored rows: they would strip the very fields a user wrote (a locked `Field`, an
edited sort title). So a rule added to those after the file was written reaches its
rows only once the file is removed, or the catalog is rebuilt and the view state is
carried over.
"""

import hashlib
import io
import logging
import os
import pickle
import socket
import threading

import spellfix as spellfix_module

logger = logging.getLogger("plexsim.persist")

FORMAT = 1

# Not plain data: the spelling index is saved as its vocabularies' rows instead.
CATALOG_SKIP = {"spelling"}
# Wiring (the catalog object itself and two callbacks into the server) and the
# activity list, whose entries describe work in flight in THIS process.
LIBRARY_SKIP = {"catalog", "_resolve", "_resolve_collection", "_notify", "_activities"}
# PlexSim attributes that are state a client wrote. Everything else on it is a
# connection, a socket, a lock, a cache or a per-process counter.
SIM_FIELDS = ("pref_overrides", "section_pref_overrides", "agent_orders",
              "metadata_provider_groups", "next_metadata_provider_group",
              "builtin_group_titles", "deleted_builtin_groups", "uploads", "uploaded_subtitles", "theme_rows", "_theme_next",
              "play_queues", "next_play_queue", "next_play_queue_item",
              "download_queues")
# The part of the server state that is not about the catalog's rows, and so still
# means the same thing over a rebuilt catalog.
SIM_PORTABLE = ("pref_overrides", "agent_orders", "metadata_provider_groups",
                "next_metadata_provider_group", "builtin_group_titles",
                "deleted_builtin_groups")
SESSION_FIELDS = ("_devices", "_device_seen", "_device_names", "_device_platforms",
                  "_next_device", "_bytes", "_plays")
# The per-user view state of one row, which is what a rebuilt catalog keeps.
VIEW_FIELDS = ("viewCount", "lastViewedAt", "viewOffset", "userRating", "lastRatedAt",
               "_viewUpdatedAt")


class _PlainPickler(pickle.Pickler):
    # Called for every object that is not None, a bool or an EXACT int, float, str,
    # bytes, dict, set, frozenset, list or tuple -- so anything reaching it is not
    # plain data, and a state file that needed it could not be loaded back.
    def reducer_override(self, obj):
        raise pickle.PicklingError("not plain data: %s" % type(obj).__qualname__)


class _PlainUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError("state files hold no classes: %s.%s" % (module, name))


def dumps(value):
    buffer = io.BytesIO()
    _PlainPickler(buffer, protocol=4).dump(value)
    return buffer.getvalue()


def loads(blob):
    return _PlainUnpickler(io.BytesIO(blob)).load()


def resolve_path(named, catalog_path):
    """The state file `main()` keeps, or None. `--state` and `--no-state` share one
    destination (the last given wins; `--no-state` stores an empty path), then
    `$PLEXSIM_STATE`, where empty also means none, then `state.pickle` beside the
    catalog, locked for this process: a default another server holds ends this one."""
    if named is None:
        named = os.environ.get("PLEXSIM_STATE")
    if named is not None:
        return named or None
    path = os.path.join(os.path.dirname(os.path.abspath(catalog_path)), "state.pickle")
    import fcntl
    try:
        handle = open(path + ".lock", "a")
    except OSError as error:
        logger.warning("cannot open %s.lock (%s): running without a state file", path, error)
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        # Cycle 92, measured on plexscratch: a second PMS on a data directory another
        # one is serving prints this line and exits 1 at once. Running on without the
        # file instead served clients whose every write vanished at the next restart,
        # or, when this server came back first, the other server's writes in their place.
        logger.error("state file %s is in use by another plexsim", path)
        print(ALREADY_RUNNING, flush=True)
        raise SystemExit(1)
    _LOCKS.append(handle)
    return path


# The default state file's lock, held open until the process exits.
_LOCKS = []

# What a second Plex Media Server on a served data directory prints before it exits 1
# (`tests/fixtures/pms_second_instance.json`).
ALREADY_RUNNING = "Plex Media Server is already running. Will not start..."


def catalog_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _row_file(row):
    for media in row.get("Media") or []:
        for part in media.get("Part") or []:
            if part.get("file"):
                return part["file"]
    return None


class Store:
    """One state file: loaded once at start, saved after writes and at exit."""

    def __init__(self, path, catalog_path, interval=2.0, fallback=60.0):
        self.path = os.path.abspath(path)
        self.digest = catalog_digest(catalog_path)
        # A client write is saved before it is answered (`Handler.send_response`);
        # everything else is coalesced: a request marks the state dirty and the saver
        # writes at most once per `interval`. `fallback` catches the writes no request
        # makes, such as the claimed-account import and the session reaper.
        self.interval = interval
        self.fallback = fallback
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._last = None
        self._save_lock = threading.Lock()

    # -- snapshot --------------------------------------------------------

    def snapshot(self, sim):
        catalog = {k: v for k, v in sim.catalog.__dict__.items() if k not in CATALOG_SKIP}
        library = {k: v for k, v in sim.library.__dict__.items() if k not in LIBRARY_SKIP}
        server = {k: getattr(sim, k) for k in SIM_FIELDS if hasattr(sim, k)}
        sessions = {k: getattr(sim.sessions, k) for k in SESSION_FIELDS
                    if hasattr(sim.sessions, k)}
        # Cycle 73, measured on plexscratch: a title edited before a restart is still not
        # found by a misspelling after it -- PMS rebuilds its spellfix tables at database
        # optimization only -- so the vocabulary is kept as it stands, not rebuilt.
        spelling = getattr(sim.catalog, "spelling", None)
        if spelling is not None:
            catalog["_spelling_rows"] = (spelling.metadata.rows, spelling.tags.rows)
        document = {"format": FORMAT, "catalog_digest": self.digest,
                    "catalog": catalog, "library": library, "server": server,
                    "sessions": sessions}
        # Request threads write without a lock, so a dict can change size under the
        # pickler. That tears nothing: the attempt is thrown away and retried, and
        # the next save picks up whatever landed in between.
        for _attempt in range(20):
            try:
                return dumps(document)
            except RuntimeError:
                continue
        raise RuntimeError("state kept changing during the snapshot")

    def save(self, sim):
        with self._save_lock:
            try:
                blob = self.snapshot(sim)
            except (pickle.PicklingError, RuntimeError) as error:
                logger.error("state not saved: %s", error)
                return False
            if blob == self._last:
                return False
            temporary = self.path + ".tmp"
            with open(temporary, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._last = blob
            return True

    # -- restore ---------------------------------------------------------

    def load(self):
        """The saved document, or None when there is none or it cannot be read."""
        try:
            with open(self.path, "rb") as handle:
                blob = handle.read()
        except FileNotFoundError:
            return None
        try:
            document = loads(blob)
            if not isinstance(document, dict) or document.get("format") != FORMAT:
                raise ValueError("unknown state format %r" % (
                    document.get("format") if isinstance(document, dict) else None))
        except Exception as error:
            # Kept rather than overwritten: a file that no longer loads is still the
            # only copy of what it held.
            aside = self.path + ".corrupt"
            os.replace(self.path, aside)
            logger.error("state file unreadable (%s); moved to %s and starting fresh",
                         error, aside)
            return None
        self._last = blob
        return document

    def restore(self, sim):
        """Apply the saved state to a freshly built server. Returns what was done."""
        document = self.load()
        if document is None:
            return "no saved state"
        if document.get("catalog_digest") == self.digest:
            self._restore_all(sim, document)
            return "restored"
        carried = self._restore_portable(sim, document)
        aside = self.path + ".stale"
        os.replace(self.path, aside)
        self._last = None
        logger.warning(
            "catalog changed since the state was saved: carried view state for %d "
            "rows and the server settings; edits, lists, history and library changes "
            "stay in %s", carried, aside)
        return "carried over %d rows" % carried

    def _restore_all(self, sim, document):
        restored = dict(document["catalog"])
        saved = restored.pop("_spelling_rows", None)
        sim.catalog.__dict__.update(restored)
        if saved is not None:
            sim.catalog.spelling = spellfix_module.SearchIndex.from_rows(*saved)
        else:
            sim.catalog.rebuild_spelling()
        sim.library.__dict__.update(document["library"])
        for key, value in document["server"].items():
            setattr(sim, key, value)
        for key, value in document["sessions"].items():
            setattr(sim.sessions, key, value)
        self._apply_friendly_name(sim)

    def _restore_portable(self, sim, document):
        for key in SIM_PORTABLE:
            if key in document["server"]:
                setattr(sim, key, document["server"][key])
        for key, value in document["sessions"].items():
            setattr(sim.sessions, key, value)
        self._apply_friendly_name(sim)

        by_guid, by_file = {}, {}
        for row in sim.catalog.items.values():
            if row.get("guid"):
                by_guid.setdefault((row.get("type"), row["guid"]), row)
            if _row_file(row):
                by_file.setdefault((row.get("type"), _row_file(row)), row)
        carried = []
        for old in document["catalog"].get("items", {}).values():
            if not any(field in old for field in VIEW_FIELDS):
                continue
            row = by_guid.get((old.get("type"), old.get("guid"))) if old.get("guid") \
                else None
            if row is None and _row_file(old):
                row = by_file.get((old.get("type"), _row_file(old)))
            if row is None:
                continue
            for field in VIEW_FIELDS:
                if field in old:
                    row[field] = old[field]
                else:
                    row.pop(field, None)
            carried.append(row)
        for row in carried:
            sim.library.recount(row)
        return len(carried)

    @staticmethod
    def _apply_friendly_name(sim):
        # The one preference whose write changes state outside `pref_overrides`: see
        # `PlexSim.set_prefs`. An empty name falls back to the host name there too.
        if "FriendlyName" in sim.pref_overrides:
            sim.catalog.friendly = sim.pref_overrides["FriendlyName"] or socket.gethostname()

    # -- saver -----------------------------------------------------------

    def touch(self):
        self._dirty.set()

    def start(self, sim):
        def run():
            while not self._stop.is_set():
                self._dirty.wait(self.fallback)
                if self._stop.wait(self.interval):
                    break
                self._dirty.clear()
                try:
                    self.save(sim)
                except OSError as error:
                    logger.error("state not saved: %s", error)
        self._thread = threading.Thread(target=run, name="state-saver", daemon=True)
        self._thread.start()

    def stop(self, sim):
        self._stop.set()
        self._dirty.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.save(sim)
