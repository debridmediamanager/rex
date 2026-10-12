"""The catalog in a SQLite database: the half of `rust/src/store.rs`'s schema that
`build_catalog.py` writes and both servers read.

A database catalog is the same document `catalog.json` holds, with each top-level
array (the items, the sections) stored a row per element so that a large catalog is
written and read by the row. The native server keeps its state in the same file, under
`state:` names; this module only ever touches `catalog:` names and the two digests.

The digests are those of the JSON the catalog came from -- SHA-1 for the native server,
SHA-256 for the Python oracle's `persist.py` -- so a state saved over `catalog.json` is
still the same catalog's after `build_catalog.py --convert catalog.json --out x.db`.
"""

import hashlib
import json
import sqlite3

MAGIC = b"SQLite format 3\x00"
PREFIX = "catalog:"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS doc(name TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT);
    CREATE TABLE IF NOT EXISTS row(coll TEXT NOT NULL, key TEXT NOT NULL, pos REAL NOT NULL,
                                   body TEXT NOT NULL, PRIMARY KEY(coll, key));
    CREATE INDEX IF NOT EXISTS row_pos ON row(coll, pos);
"""


def is_sqlite(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == MAGIC
    except OSError:
        return False


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write(path, document, source_bytes=None):
    """Replace the catalog in the database at `path` (created if absent), leaving any
    state in it alone. `source_bytes` is the JSON the document came from; without it the
    digests are taken over the JSON `build_catalog.py` would have written."""
    if source_bytes is None:
        source_bytes = json.dumps(document, indent=2).encode()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM row WHERE coll LIKE ?", (PREFIX + "%",))
        db.execute("DELETE FROM doc WHERE name LIKE ?", (PREFIX + "%",))
        for field, value in document.items():
            name = PREFIX + field
            if isinstance(value, list):
                db.execute("INSERT INTO doc VALUES (?, 'array', NULL)", (name,))
                db.executemany("INSERT INTO row VALUES (?, ?, ?, ?)",
                               ((name, str(index), index, _dumps(element))
                                for index, element in enumerate(value)))
            elif isinstance(value, dict):
                db.execute("INSERT INTO doc VALUES (?, 'object', NULL)", (name,))
                db.executemany("INSERT INTO row VALUES (?, ?, ?, ?)",
                               ((name, key, index, _dumps(member))
                                for index, (key, member) in enumerate(value.items())))
            else:
                db.execute("INSERT INTO doc VALUES (?, 'value', ?)", (name, _dumps(value)))
        for key, value in (("catalogDigest", hashlib.sha1(source_bytes).hexdigest()),
                           ("catalogDigestSha256", hashlib.sha256(source_bytes).hexdigest())):
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def read(path):
    """The catalog document in the database at `path`."""
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        db.execute("PRAGMA busy_timeout=10000")
        document = {}
        docs = db.execute("SELECT name, kind, body FROM doc WHERE name LIKE ? ORDER BY rowid",
                          (PREFIX + "%",)).fetchall()
        for name, kind, body in docs:
            field = name[len(PREFIX):]
            if kind == "value":
                document[field] = json.loads(body if body is not None else "null")
                continue
            rows = db.execute("SELECT key, body FROM row WHERE coll = ? ORDER BY pos, key",
                              (name,)).fetchall()
            if kind == "array":
                document[field] = [json.loads(body) for _key, body in rows]
            else:
                document[field] = {key: json.loads(body) for key, body in rows}
        if not document:
            raise ValueError("%s holds no catalog" % path)
        return document
    finally:
        db.close()


def digest(path, kind="catalogDigestSha256"):
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (kind,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError("%s records no %s" % (path, kind))
    return row[0]
