//! The SQLite store: the state file and, optionally, the catalog, written by the row.
//!
//! The state used to be one JSON document rewritten whole on every save, and a client
//! write is saved before it is answered -- so one rating on a 20,000-row library
//! rewrote all 20,000 rows. Here the same document is kept in a database: each
//! top-level array or object of the document is a collection stored one row per
//! element, and a save writes only the elements that changed since the last one, in
//! one transaction (`tests/test_sqlite_store.py` counts them with triggers).
//!
//! Schema, shared with `catalogdb.py` (which writes the catalog half):
//!
//! ```text
//! meta(key PRIMARY KEY, value)               format, catalogDigest, catalogDigestSha256
//! doc(name PRIMARY KEY, kind, body)          kind 'value' (body = JSON), 'array', 'object'
//! row(coll, key, pos, body, PRIMARY KEY(coll, key))   elements of an array/object doc
//! ```
//!
//! Names are prefixed: `catalog:<field>` for the catalog `build_catalog.py` writes,
//! `state:<field>` for what the server writes. The server never writes `catalog:*`.
//! An array element's key is its content hash and occurrence (an element that changes
//! is a delete and an insert); an object element's key is its own key. `pos` orders a
//! collection's rows and is kept where the retained rows' order allows, so an append or
//! a removal moves no other row.

use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs;
use std::hash::{Hash, Hasher};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde_json::{Map, Value};

pub const MAGIC: &[u8; 16] = b"SQLite format 3\0";
pub const FORMAT: &str = "rex-sqlite-1";
const STATE: &str = "state:";
const CATALOG: &str = "catalog:";

const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS doc(name TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT);
    CREATE TABLE IF NOT EXISTS row(coll TEXT NOT NULL, key TEXT NOT NULL, pos REAL NOT NULL,
                                   body TEXT NOT NULL, PRIMARY KEY(coll, key));
    CREATE INDEX IF NOT EXISTS row_pos ON row(coll, pos);
";

/// Whether `path` is a SQLite database (by its header, not its name).
pub fn is_sqlite(path: &Path) -> bool {
    let mut head = [0u8; 16];
    fs::File::open(path)
        .and_then(|mut file| file.read_exact(&mut head))
        .map(|()| &head == MAGIC)
        .unwrap_or(false)
}

fn open(path: &Path) -> Result<Connection, String> {
    let connection = Connection::open(path).map_err(|e| e.to_string())?;
    // WAL: a commit appends the changed pages instead of rewriting the file, and
    // readers (a backup, `sqlite3`) never block the server. FULL: a commit is on disk
    // before it returns, which is what "saved before it is answered" means.
    connection
        .execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=10000;",
        )
        .map_err(|e| e.to_string())?;
    connection
        .execute_batch(SCHEMA)
        .map_err(|e| e.to_string())?;
    Ok(connection)
}

fn open_read(path: &Path) -> Result<Connection, String> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| e.to_string())?;
    connection
        .execute_batch("PRAGMA busy_timeout=10000;")
        .map_err(|e| e.to_string())?;
    Ok(connection)
}

fn has_table(connection: &Connection, table: &str) -> Result<bool, String> {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1",
            params![table],
            |_| Ok(()),
        )
        .optional()
        .map(|found| found.is_some())
        .map_err(|e| e.to_string())
}

fn hash_text(text: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    hasher.finish()
}

/// Reassemble the documents under `prefix` and, for the state, the cache of what is on
/// disk that the next save diffs against.
fn read_prefix(
    connection: &Connection,
    prefix: &str,
) -> Result<(Map<String, Value>, Cache), String> {
    let mut document = Map::new();
    let mut cache = Cache::default();
    let pattern = format!("{prefix}%");
    let mut docs = connection
        .prepare("SELECT name, kind, body FROM doc WHERE name LIKE ?1 ORDER BY rowid")
        .map_err(|e| e.to_string())?;
    let docs = docs
        .query_map(params![pattern], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    let mut rows = connection
        .prepare("SELECT key, pos, body FROM row WHERE coll = ?1 ORDER BY pos, key")
        .map_err(|e| e.to_string())?;
    for (name, kind, body) in docs {
        let field = name[prefix.len()..].to_string();
        let kind = Kind::parse(&kind).ok_or_else(|| format!("unknown kind {kind} for {name}"))?;
        match kind {
            Kind::Value => {
                let body = body.unwrap_or_else(|| "null".to_string());
                let value: Value =
                    serde_json::from_str(&body).map_err(|e| format!("{name}: {e}"))?;
                cache
                    .docs
                    .insert(field.clone(), (Kind::Value, hash_text(&body)));
                document.insert(field, value);
            }
            Kind::Array | Kind::Object => {
                let mut collection = Collection::default();
                let mut array = Vec::new();
                let mut object = Map::new();
                let listed = rows
                    .query_map(params![name], |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, f64>(1)?,
                            row.get::<_, String>(2)?,
                        ))
                    })
                    .map_err(|e| e.to_string())?;
                for listed in listed {
                    let (key, pos, body) = listed.map_err(|e| e.to_string())?;
                    let value: Value =
                        serde_json::from_str(&body).map_err(|e| format!("{name}/{key}: {e}"))?;
                    collection.rows.insert(key.clone(), (pos, hash_text(&body)));
                    collection.order.push(key.clone());
                    if kind == Kind::Array {
                        array.push(value);
                    } else {
                        object.insert(key, value);
                    }
                }
                cache.docs.insert(field.clone(), (kind, 0));
                cache.collections.insert(field.clone(), collection);
                document.insert(
                    field,
                    if kind == Kind::Array {
                        Value::Array(array)
                    } else {
                        Value::Object(object)
                    },
                );
            }
        }
    }
    Ok((document, cache))
}

fn meta(connection: &Connection, key: &str) -> Result<Option<String>, String> {
    if !has_table(connection, "meta")? {
        return Ok(None);
    }
    connection
        .query_row(
            "SELECT value FROM meta WHERE key = ?1",
            params![key],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())
}

/// A catalog `build_catalog.py` wrote into a database, and the digest it recorded --
/// the SHA-1 of the JSON the catalog came from, so a state saved over that JSON file
/// is still the same catalog's.
pub fn read_catalog(path: &Path) -> Result<(Value, String), String> {
    let connection = open_read(path).map_err(|e| format!("cannot read catalog: {e}"))?;
    if !has_table(&connection, "doc")? {
        return Err("cannot read catalog: the database holds no catalog".to_string());
    }
    let (document, _) =
        read_prefix(&connection, CATALOG).map_err(|e| format!("cannot parse catalog: {e}"))?;
    if document.is_empty() {
        return Err("cannot read catalog: the database holds no catalog".to_string());
    }
    let digest = meta(&connection, "catalogDigest")?
        .ok_or_else(|| "cannot read catalog: the database records no catalogDigest".to_string())?;
    Ok((Value::Object(document), digest))
}

/// What a state path holds.
pub enum Saved {
    Missing,
    /// A database with no state in it (a catalog database, before the first save).
    Empty,
    /// The pre-database JSON state file's bytes.
    Legacy(Vec<u8>),
    /// Neither: the bytes, for the caller's `.corrupt` handling.
    Unreadable(Vec<u8>, String),
    Database(Value, Cache),
}

pub fn read_state(path: &Path) -> Result<Saved, String> {
    if !path.exists() {
        return Ok(Saved::Missing);
    }
    if !is_sqlite(path) {
        let bytes = fs::read(path).map_err(|e| format!("cannot read state file: {e}"))?;
        let text = bytes.iter().position(|b| !b.is_ascii_whitespace());
        if text.map(|at| bytes[at]) == Some(b'{') {
            return Ok(Saved::Legacy(bytes));
        }
        return Ok(Saved::Unreadable(
            bytes,
            "not a SQLite database".to_string(),
        ));
    }
    let read = (|| -> Result<Saved, String> {
        let connection = open_read(path)?;
        connection
            .query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0))
            .map_err(|e| e.to_string())
            .and_then(|verdict| {
                if verdict == "ok" {
                    Ok(())
                } else {
                    Err(verdict)
                }
            })?;
        if !has_table(&connection, "doc")? {
            return Ok(Saved::Empty);
        }
        let (document, cache) = read_prefix(&connection, STATE)?;
        if document.is_empty() {
            return Ok(Saved::Empty);
        }
        Ok(Saved::Database(Value::Object(document), cache))
    })();
    match read {
        Ok(saved) => Ok(saved),
        Err(error) => Ok(Saved::Unreadable(fs::read(path).unwrap_or_default(), error)),
    }
}

/// A state saved over another catalog: copy the whole database aside as `<path>.stale`
/// (a consistent copy, WAL included) and drop the state from it, keeping the catalog.
pub fn set_aside(path: &Path, stale: &Path) -> Result<(), String> {
    let _ = fs::remove_file(stale);
    let connection = open(path)?;
    connection
        .execute("VACUUM INTO ?1", params![stale.to_string_lossy()])
        .map_err(|e| e.to_string())?;
    clear_state(&connection)
}

fn clear_state(connection: &Connection) -> Result<(), String> {
    let pattern = format!("{STATE}%");
    connection
        .execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| e.to_string())?;
    let result = connection
        .execute("DELETE FROM row WHERE coll LIKE ?1", params![pattern])
        .and_then(|_| connection.execute("DELETE FROM doc WHERE name LIKE ?1", params![pattern]));
    match result {
        Ok(_) => connection
            .execute_batch("COMMIT")
            .map_err(|e| e.to_string()),
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK");
            Err(error.to_string())
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Value,
    Array,
    Object,
}

impl Kind {
    fn of(value: &Value) -> Self {
        match value {
            Value::Array(_) => Kind::Array,
            Value::Object(_) => Kind::Object,
            _ => Kind::Value,
        }
    }
    fn parse(text: &str) -> Option<Self> {
        match text {
            "value" => Some(Kind::Value),
            "array" => Some(Kind::Array),
            "object" => Some(Kind::Object),
            _ => None,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Kind::Value => "value",
            Kind::Array => "array",
            Kind::Object => "object",
        }
    }
}

#[derive(Debug, Default, Clone)]
struct Collection {
    /// key -> (pos, hash of the stored body)
    rows: HashMap<String, (f64, u64)>,
    order: Vec<String>,
}

/// What the database holds, as of the last snapshot: the next one writes the difference.
#[derive(Debug, Default, Clone)]
pub struct Cache {
    /// field -> (kind, hash of a value doc's body; 0 for a collection)
    docs: HashMap<String, (Kind, u64)>,
    collections: HashMap<String, Collection>,
}

#[derive(Debug)]
enum Op {
    Doc {
        name: String,
        kind: Kind,
        body: Option<String>,
    },
    DropDoc {
        name: String,
    },
    ClearRows {
        coll: String,
    },
    Put {
        coll: String,
        key: String,
        pos: f64,
        body: String,
    },
    Move {
        coll: String,
        key: String,
        pos: f64,
    },
    Delete {
        coll: String,
        key: String,
    },
}

/// One snapshot's changes, against the snapshot before it.
#[derive(Debug, Default)]
pub struct Delta {
    ops: Vec<Op>,
}

impl Delta {
    pub fn extend(&mut self, other: Delta) {
        self.ops.extend(other.ops);
    }
    pub fn len(&self) -> usize {
        self.ops.len()
    }
    pub fn is_empty(&self) -> bool {
        self.ops.is_empty()
    }
}

impl Cache {
    /// Diff `state` against the cache, update the cache to it, and return the writes.
    /// `separate` names fields diffed by `diff_rows` instead, which are therefore not
    /// gone for being absent from `state`.
    pub fn diff(&mut self, state: &Map<String, Value>, separate: &[&str]) -> Delta {
        let mut delta = Delta::default();
        for (field, value) in state {
            let name = format!("{STATE}{field}");
            let kind = Kind::of(value);
            let previous = self.docs.get(field).copied();
            if let Some((was, _)) = previous {
                if was != kind && was != Kind::Value {
                    delta.ops.push(Op::ClearRows { coll: name.clone() });
                    self.collections.remove(field);
                }
            }
            match value {
                Value::Array(_) | Value::Object(_) => {
                    if previous.map(|(was, _)| was) != Some(kind) {
                        delta.ops.push(Op::Doc {
                            name: name.clone(),
                            kind,
                            body: None,
                        });
                        self.docs.insert(field.clone(), (kind, 0));
                    }
                    let entries: Vec<(String, String)> = match value {
                        Value::Array(elements) => {
                            let mut seen: HashMap<u64, usize> = HashMap::new();
                            elements
                                .iter()
                                .map(|element| {
                                    let body = element.to_string();
                                    let hash = hash_text(&body);
                                    let occurrence = seen.entry(hash).or_insert(0);
                                    let key = format!("{hash:016x}.{occurrence}");
                                    *occurrence += 1;
                                    (key, body)
                                })
                                .collect()
                        }
                        Value::Object(members) => members
                            .iter()
                            .map(|(key, member)| (key.clone(), member.to_string()))
                            .collect(),
                        _ => unreachable!(),
                    };
                    let collection = self.collections.entry(field.clone()).or_default();
                    diff_collection(&name, collection, entries, &mut delta);
                }
                _ => {
                    let body = value.to_string();
                    let hash = hash_text(&body);
                    if previous != Some((Kind::Value, hash)) {
                        delta.ops.push(Op::Doc {
                            name,
                            kind: Kind::Value,
                            body: Some(body),
                        });
                        self.docs.insert(field.clone(), (Kind::Value, hash));
                    }
                }
            }
        }
        let gone: Vec<String> = self
            .docs
            .keys()
            .filter(|field| !state.contains_key(*field) && !separate.contains(&field.as_str()))
            .cloned()
            .collect();
        for field in gone {
            let name = format!("{STATE}{field}");
            delta.ops.push(Op::ClearRows { coll: name.clone() });
            delta.ops.push(Op::DropDoc { name });
            self.docs.remove(&field);
            self.collections.remove(&field);
        }
        delta
    }
}

impl Cache {
    /// An object field kept by its members' keys, so a save serialises only the members
    /// `changed` names: the 28,859 held feed items on fun (2026-10-10) were serialised,
    /// hashed and compared whole on every save, two seconds apart while any client polled.
    /// A field the cache does not hold yet takes `diff`'s full pass over `all()`.
    pub fn diff_members<'a>(
        &mut self,
        field: &str,
        changed: &HashSet<String>,
        get: impl Fn(&str) -> Option<&'a Value>,
        all: impl FnOnce() -> Map<String, Value>,
    ) -> Delta {
        let known = self.docs.get(field).map(|(kind, _)| *kind) == Some(Kind::Object)
            && self.collections.contains_key(field);
        if !known {
            let mut state = Map::new();
            state.insert(field.to_string(), Value::Object(all()));
            let others: Vec<String> = self.docs.keys().filter(|name| *name != field).cloned().collect();
            let others: Vec<&str> = others.iter().map(String::as_str).collect();
            return self.diff(&state, &others);
        }
        let name = format!("{STATE}{field}");
        let mut delta = Delta::default();
        let collection = self.collections.get_mut(field).expect("checked above");
        // Every pass leaves `order` in position order, and a new member goes last.
        let mut next = collection
            .order
            .last()
            .and_then(|key| collection.rows.get(key))
            .map_or(0.0, |(pos, _)| pos + 1.0);
        let mut keys: Vec<&String> = changed.iter().collect();
        keys.sort();
        let mut gone = HashSet::new();
        for key in keys {
            match (collection.rows.get(key).copied(), get(key)) {
                (Some((pos, hash)), Some(member)) => {
                    let body = member.to_string();
                    let new_hash = hash_text(&body);
                    if new_hash != hash {
                        collection.rows.insert(key.clone(), (pos, new_hash));
                        delta.ops.push(Op::Put { coll: name.clone(), key: key.clone(), pos, body });
                    }
                }
                // A member's position only orders the rows read back into a map.
                (None, Some(member)) => {
                    let body = member.to_string();
                    collection.rows.insert(key.clone(), (next, hash_text(&body)));
                    collection.order.push(key.clone());
                    delta.ops.push(Op::Put { coll: name.clone(), key: key.clone(), pos: next, body });
                    next += 1.0;
                }
                (Some(_), None) => {
                    collection.rows.remove(key);
                    gone.insert(key.clone());
                    delta.ops.push(Op::Delete { coll: name.clone(), key: key.clone() });
                }
                (None, None) => {}
            }
        }
        if !gone.is_empty() {
            collection.order.retain(|key| !gone.contains(key));
        }
        delta
    }
}

impl Cache {
    /// An array field kept by its rows' own keys (the catalog's `ratingKey`), so a save
    /// can serialise only the rows `changed` names: a row written, appended (`position`
    /// past every stored row) or removed. `changed: None` -- rows reordered or keyless --
    /// serialises them all, as does a first save; an element with no key or a repeated
    /// one is kept by content, like any array element.
    pub fn diff_rows<'a>(
        &mut self,
        field: &str,
        rows: &'a [Value],
        changed: Option<&HashSet<String>>,
        key_of: impl Fn(&Value) -> Option<String>,
        get: impl Fn(&str) -> Option<&'a Value>,
        position: impl Fn(&str) -> Option<usize>,
    ) -> Delta {
        let name = format!("{STATE}{field}");
        let mut delta = Delta::default();
        let known = self.docs.get(field).map(|(kind, _)| *kind) == Some(Kind::Array)
            && self.collections.contains_key(field);
        if let (Some(changed), true) = (changed, known) {
            let collection = self.collections.get_mut(field).expect("checked above");
            let mut complete = true;
            let mut puts = Vec::with_capacity(changed.len());
            let mut appended = Vec::new();
            let mut gone = Vec::new();
            for key in changed {
                match (collection.rows.get(key).copied(), get(key)) {
                    (Some((pos, hash)), Some(row)) => {
                        let body = row.to_string();
                        let new_hash = hash_text(&body);
                        if new_hash != hash {
                            puts.push((key.clone(), pos, new_hash, body));
                        }
                    }
                    (Some(_), None) => gone.push(key.clone()),
                    (None, Some(row)) if !key.starts_with('#') => match position(key) {
                        Some(at) => appended.push((at, key.clone(), row)),
                        None => {
                            complete = false;
                            break;
                        }
                    },
                    // Added and removed again since the last save.
                    (None, None) => {}
                    _ => {
                        complete = false;
                        break;
                    }
                }
            }
            // An appended row goes after every stored one, in the rows' own order --
            // which is only true when each sits past every row already stored, so
            // anything else (a row inserted in the middle) takes the full pass.
            appended.sort_by_key(|(at, _, _)| *at);
            let stored = collection.order.len() - gone.len();
            if appended
                .iter()
                .enumerate()
                .any(|(offset, (at, _, _))| *at != stored + offset)
            {
                complete = false;
            }
            if complete {
                for (key, pos, hash, body) in puts {
                    collection.rows.insert(key.clone(), (pos, hash));
                    delta.ops.push(Op::Put { coll: name.clone(), key, pos, body });
                }
                if !gone.is_empty() {
                    let gone_set: HashSet<&String> = gone.iter().collect();
                    collection.order.retain(|key| !gone_set.contains(key));
                    for key in gone {
                        collection.rows.remove(&key);
                        delta.ops.push(Op::Delete { coll: name.clone(), key });
                    }
                }
                let mut next = collection
                    .order
                    .last()
                    .and_then(|key| collection.rows.get(key))
                    .map_or(0.0, |(pos, _)| pos + 1.0);
                for (_, key, row) in appended {
                    let body = row.to_string();
                    collection.rows.insert(key.clone(), (next, hash_text(&body)));
                    collection.order.push(key.clone());
                    delta.ops.push(Op::Put { coll: name.clone(), key, pos: next, body });
                    next += 1.0;
                }
                return delta;
            }
        }
        if self.docs.get(field).map(|(kind, _)| *kind) != Some(Kind::Array) {
            if self.docs.contains_key(field) {
                delta.ops.push(Op::ClearRows { coll: name.clone() });
            }
            delta.ops.push(Op::Doc { name: name.clone(), kind: Kind::Array, body: None });
            self.docs.insert(field.to_string(), (Kind::Array, 0));
            self.collections.remove(field);
        }
        let mut taken: HashSet<String> = HashSet::with_capacity(rows.len());
        let mut seen: HashMap<u64, usize> = HashMap::new();
        let entries = rows
            .iter()
            .map(|row| {
                let body = row.to_string();
                let key = match key_of(row) {
                    Some(key) if !key.starts_with('#') && taken.insert(key.clone()) => key,
                    _ => {
                        let hash = hash_text(&body);
                        let occurrence = seen.entry(hash).or_insert(0);
                        let key = format!("#{hash:016x}.{occurrence}");
                        *occurrence += 1;
                        key
                    }
                };
                (key, body)
            })
            .collect();
        let collection = self.collections.entry(field.to_string()).or_default();
        diff_collection(&name, collection, entries, &mut delta);
        delta
    }
}

fn diff_collection(
    name: &str,
    collection: &mut Collection,
    entries: Vec<(String, String)>,
    delta: &mut Delta,
) {
    let present: HashSet<&str> = entries.iter().map(|(key, _)| key.as_str()).collect();
    for key in &collection.order {
        if !present.contains(key.as_str()) {
            delta.ops.push(Op::Delete {
                coll: name.to_string(),
                key: key.clone(),
            });
        }
    }
    // Positions: a retained row keeps its pos when the retained rows are still in pos
    // order; a new row takes a pos between its neighbours. Otherwise (a reorder, or a
    // gap worn too thin) every row is renumbered.
    let retained: Vec<Option<f64>> = entries
        .iter()
        .map(|(key, _)| collection.rows.get(key).map(|(pos, _)| *pos))
        .collect();
    let mut positions = vec![0f64; entries.len()];
    let mut ordered = retained
        .iter()
        .flatten()
        .collect::<Vec<_>>()
        .windows(2)
        .all(|pair| pair[0] < pair[1]);
    if ordered {
        let mut next = vec![None; entries.len() + 1];
        for index in (0..entries.len()).rev() {
            next[index] = retained[index].or(next[index + 1]);
        }
        let mut previous: Option<f64> = None;
        for index in 0..entries.len() {
            let pos = match retained[index] {
                Some(pos) => pos,
                None => match (previous, next[index + 1]) {
                    (None, None) => index as f64,
                    (None, Some(after)) => after - 1.0,
                    (Some(before), None) => before + 1.0,
                    (Some(before), Some(after)) => {
                        let middle = before + (after - before) / 2.0;
                        if !(before < middle && middle < after) {
                            ordered = false;
                            break;
                        }
                        middle
                    }
                },
            };
            positions[index] = pos;
            previous = Some(pos);
        }
    }
    if !ordered {
        positions = (0..entries.len()).map(|index| index as f64).collect();
    }
    let mut rows = HashMap::with_capacity(entries.len());
    let mut order = Vec::with_capacity(entries.len());
    for ((key, body), pos) in entries.into_iter().zip(positions) {
        let hash = hash_text(&body);
        match collection.rows.get(&key) {
            Some((was_pos, was_hash)) if *was_hash == hash => {
                if *was_pos != pos {
                    delta.ops.push(Op::Move {
                        coll: name.to_string(),
                        key: key.clone(),
                        pos,
                    });
                }
            }
            _ => delta.ops.push(Op::Put {
                coll: name.to_string(),
                key: key.clone(),
                pos,
                body,
            }),
        }
        rows.insert(key.clone(), (pos, hash));
        order.push(key);
    }
    collection.rows = rows;
    collection.order = order;
}

fn apply(connection: &mut Connection, deltas: &[Delta]) -> Result<(), String> {
    let transaction = connection.transaction().map_err(|e| e.to_string())?;
    {
        let mut put = transaction
            .prepare_cached(
                "INSERT OR REPLACE INTO row(coll, key, pos, body) VALUES (?1, ?2, ?3, ?4)",
            )
            .map_err(|e| e.to_string())?;
        let mut moved = transaction
            .prepare_cached("UPDATE row SET pos = ?3 WHERE coll = ?1 AND key = ?2")
            .map_err(|e| e.to_string())?;
        let mut delete = transaction
            .prepare_cached("DELETE FROM row WHERE coll = ?1 AND key = ?2")
            .map_err(|e| e.to_string())?;
        for op in deltas.iter().flat_map(|delta| delta.ops.iter()) {
            let result = match op {
                Op::Doc { name, kind, body } => transaction.execute(
                    "INSERT OR REPLACE INTO doc(name, kind, body) VALUES (?1, ?2, ?3)",
                    params![name, kind.name(), body],
                ),
                Op::DropDoc { name } => {
                    transaction.execute("DELETE FROM doc WHERE name = ?1", params![name])
                }
                Op::ClearRows { coll } => {
                    transaction.execute("DELETE FROM row WHERE coll = ?1", params![coll])
                }
                Op::Put {
                    coll,
                    key,
                    pos,
                    body,
                } => put.execute(params![coll, key, pos, body]),
                Op::Move { coll, key, pos } => moved.execute(params![coll, key, pos]),
                Op::Delete { coll, key } => delete.execute(params![coll, key]),
            };
            result.map_err(|e| e.to_string())?;
        }
    }
    transaction
        .execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('format', ?1)",
            params![FORMAT],
        )
        .map_err(|e| e.to_string())?;
    transaction.commit().map_err(|e| e.to_string())
}

/// Remove the write-ahead log and shared-memory files a hard kill leaves beside a
/// database. Whatever replaces or moves the main file must take them too: a new
/// database must never open on another one's log.
pub fn remove_sidecars(path: &Path) {
    for suffix in ["-wal", "-shm"] {
        let _ = fs::remove_file(format!("{}{suffix}", path.display()));
    }
}

/// Write one complete state into a NEW database at `path` (the legacy import).
pub fn write_fresh(path: &Path, state: &Map<String, Value>) -> Result<Cache, String> {
    let _ = fs::remove_file(path);
    remove_sidecars(path);
    let mut cache = Cache::default();
    let delta = cache.diff(state, &[]);
    let mut connection = open(path)?;
    apply(&mut connection, &[delta])?;
    // Closing checkpoints the log into the file, which is then renamed into place.
    connection.close().map_err(|(_, e)| e.to_string())?;
    Ok(cache)
}

/// Snapshots are queued in the order they are taken (under the runtime lock) and
/// written in that order by whichever thread flushes first, outside that lock: a
/// later snapshot is a delta on the earlier ones, so none may be skipped or reordered.
/// Each entry names its state file, and a flush takes only its own file's entries: the
/// queue is the process's, and the unit tests run several runtimes at once, whose deltas
/// one flush used to write into another's file while marking them done for the rest.
struct Queue {
    pending: VecDeque<(PathBuf, u64, Delta)>,
}

struct Writer {
    path: Option<PathBuf>,
    connection: Option<Connection>,
    /// The last serial committed, per state file.
    written: Vec<(PathBuf, u64)>,
}

impl Writer {
    fn written(&self, path: &Path) -> u64 {
        self.written.iter().find(|(of, _)| of == path).map_or(0, |(_, serial)| *serial)
    }

    fn mark(&mut self, path: &Path, serial: u64) {
        match self.written.iter_mut().find(|(of, _)| of == path) {
            Some(entry) => entry.1 = serial,
            None => self.written.push((path.to_path_buf(), serial)),
        }
    }
}

static QUEUE: Mutex<Queue> = Mutex::new(Queue {
    pending: VecDeque::new(),
});
static WRITER: Mutex<Writer> = Mutex::new(Writer {
    path: None,
    connection: None,
    written: Vec::new(),
});

/// The serial of the next snapshot, for the whole process. A counter per runtime let a
/// second runtime's first saves look already written and be dropped: a runtime loaded
/// after another had saved lost its writes.
pub fn next_serial() -> u64 {
    static SERIAL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    SERIAL.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1
}

/// Queue a snapshot's delta for `path`; call with the runtime lock held, so serials are
/// in order.
pub fn enqueue(path: &Path, serial: u64, delta: Delta) {
    let mut queue = QUEUE
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    queue.pending.push_back((path.to_path_buf(), serial, delta));
}

/// Make every snapshot queued for `path` up to `serial` durable there. A failed write
/// leaves the queue as it was, so the next flush retries it.
pub fn flush(path: &Path, serial: u64) -> Result<(), String> {
    let mut writer = WRITER
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if writer.written(path) >= serial {
        return Ok(());
    }
    let batch: Vec<(u64, Delta)> = {
        let mut queue = QUEUE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let (mine, others): (VecDeque<_>, VecDeque<_>) =
            queue.pending.drain(..).partition(|(of, _, _)| of == path);
        queue.pending = others;
        mine.into_iter().map(|(_, serial, delta)| (serial, delta)).collect()
    };
    if batch.is_empty() {
        return Ok(());
    }
    if writer.path.as_deref() != Some(path) || writer.connection.is_none() {
        writer.connection = None;
        match open(path) {
            Ok(connection) => {
                writer.connection = Some(connection);
                writer.path = Some(path.to_path_buf());
            }
            Err(error) => {
                requeue(path, batch);
                return Err(error);
            }
        }
    }
    let last = batch.last().map(|(serial, _)| *serial).unwrap_or(serial);
    let deltas: Vec<Delta> = batch.into_iter().map(|(_, delta)| delta).collect();
    let connection = writer.connection.as_mut().expect("opened above");
    match apply(connection, &deltas) {
        Ok(()) => {
            writer.mark(path, last);
            Ok(())
        }
        Err(error) => {
            // Drop the connection: a file replaced underneath is reopened next time.
            writer.connection = None;
            let serials = (0..deltas.len()).map(|_| last);
            requeue(path, serials.zip(deltas).collect());
            Err(format!("{error}: '{}'", path.display()))
        }
    }
}

fn requeue(path: &Path, batch: Vec<(u64, Delta)>) {
    let mut queue = QUEUE
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    for (serial, delta) in batch.into_iter().rev() {
        queue.pending.push_front((path.to_path_buf(), serial, delta));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn state(value: Value) -> Map<String, Value> {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn a_changed_element_is_one_row() {
        let mut cache = Cache::default();
        let items: Vec<Value> = (0..1000)
            .map(|n| json!({"ratingKey": n.to_string()}))
            .collect();
        let first = cache.diff_all(&state(json!({"items": items.clone(), "name": "x"})));
        assert_eq!(first.len(), 1002);
        let mut changed = items.clone();
        changed[500] = json!({"ratingKey": "500", "userRating": 8});
        let second = cache.diff_all(&state(json!({"items": changed.clone(), "name": "x"})));
        // The old element out, the new one in, nothing else.
        assert_eq!(second.len(), 2, "{second:?}");
        let mut removed = changed.clone();
        removed.remove(10);
        removed.push(json!({"ratingKey": "new"}));
        assert_eq!(
            cache
                .diff_all(&state(json!({"items": removed, "name": "x"})))
                .len(),
            2
        );
        assert!(
            cache
                .diff_all(&state(json!({"items": items_after(&changed), "name": "x"})))
                .len()
                > 2
        );
    }

    impl Cache {
        fn diff_all(&mut self, state: &Map<String, Value>) -> Delta {
            self.diff(state, &[])
        }
    }

    #[test]
    fn rows_by_key_write_only_the_changed_row() {
        let mut cache = Cache::default();
        let mut rows: Vec<Value> = (0..1000).map(|n| json!({"ratingKey": n.to_string()})).collect();
        let key = |row: &Value| row.get("ratingKey").and_then(Value::as_str).map(str::to_string);
        let snapshot = rows.clone();
        let first = cache.diff_rows("items", &snapshot, None, key, |_| None, |_| None);
        assert_eq!(first.len(), 1001);
        rows[7] = json!({"ratingKey": "7", "userRating": 8});
        let changed: HashSet<String> = ["7".to_string(), "8".to_string()].into();
        let lookup = rows.clone();
        let find = |rows: &[Value], k: &str| {
            rows.iter().position(|row| row.get("ratingKey").and_then(Value::as_str) == Some(k))
        };
        let second = cache.diff_rows("items", &lookup, Some(&changed), key, |k| {
            find(&lookup, k).map(|at| &lookup[at])
        }, |k| find(&lookup, k));
        // Row 8 was touched but is unchanged: only row 7 is written.
        assert_eq!(second.len(), 1, "{second:?}");
    }

    #[test]
    fn an_appended_or_removed_row_is_one_write_and_keeps_the_order() {
        let dir = std::env::temp_dir().join(format!("rex-append-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("state.db");
        let key = |row: &Value| row.get("ratingKey").and_then(Value::as_str).map(str::to_string);
        let find = |rows: &[Value], k: &str| {
            rows.iter().position(|row| row.get("ratingKey").and_then(Value::as_str) == Some(k))
        };
        let mut rows: Vec<Value> = (0..100).map(|n| json!({"ratingKey": n.to_string()})).collect();
        let mut cache = write_fresh(&path, &state(json!({"format": 1}))).unwrap();
        let mut connection = open(&path).unwrap();
        let first = cache.diff_rows("items", &rows, None, key, |_| None, |_| None);
        apply(&mut connection, &[first]).unwrap();
        rows.remove(40);
        rows.push(json!({"ratingKey": "new-1"}));
        rows.push(json!({"ratingKey": "new-2"}));
        let changed: HashSet<String> =
            ["40".to_string(), "new-2".to_string(), "new-1".to_string()].into();
        let snapshot = rows.clone();
        let delta = cache.diff_rows("items", &snapshot, Some(&changed), key, |k| {
            find(&snapshot, k).map(|at| &snapshot[at])
        }, |k| find(&snapshot, k));
        // One delete and two appends: no other row is serialised or moved.
        assert_eq!(delta.len(), 3, "{delta:?}");
        apply(&mut connection, &[delta]).unwrap();
        drop(connection);
        match read_state(&path).unwrap() {
            Saved::Database(value, _) => assert_eq!(value["items"], Value::Array(rows)),
            _ => panic!("not read back"),
        }
        let _ = fs::remove_dir_all(&dir);
    }

    fn items_after(items: &[Value]) -> Vec<Value> {
        items.iter().rev().cloned().collect()
    }

    #[test]
    fn round_trip_through_a_database() {
        let dir = std::env::temp_dir().join(format!("rex-store-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("state.db");
        let saved = state(json!({
            "format": 1, "items": [{"a": 1}, {"a": 1}, {"b": [1, 2]}],
            "prefs": {"z": 1, "a": 2}, "empty": [], "name": "n"
        }));
        let mut cache = write_fresh(&path, &saved).unwrap();
        let mut next = saved.clone();
        next.insert(
            "items".into(),
            json!([{"a": 1}, {"c": 3}, {"a": 1}, {"b": [1, 2]}]),
        );
        next.remove("name");
        let delta = cache.diff(&next, &[]);
        let mut connection = open(&path).unwrap();
        apply(&mut connection, &[delta]).unwrap();
        match read_state(&path).unwrap() {
            Saved::Database(value, _) => assert_eq!(value, Value::Object(next)),
            _ => panic!("not read back"),
        }
        let _ = fs::remove_dir_all(&dir);
    }
}
