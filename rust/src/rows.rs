//! Where the catalog's rows are: all in RAM, or in the state database with only some here.
//!
//! The production server held its 42,806 rows as parsed JSON, 1.1 GB of a 1.4 GB process, where
//! Plex holds 21 MB of its own library and reads the rest from its database (2026-10-18). A
//! catalog `attach`ed to its state file keeps a row here only while it must: written since the
//! last save that reached the file, or read during the current request. A pass over the library
//! reads the rows' hot fields (`hotrow.rs`), which the index keeps for every row.
//!
//! A row read from the file is kept until `trim`, which needs `&mut`: so a `&Value` handed out
//! by `get` stays valid for as long as the catalog is borrowed, as it did when every row was in
//! a `Vec`.

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

fn log_error(message: &str) {
    crate::runtime::logging::log(crate::runtime::logging::Level::Error, message);
}

/// What the database stores the catalog's rows under (`store.rs`).
const COLLECTION: &str = "state:items";

/// The byte budget for rows read from the file and kept between requests (`REX_ROW_CACHE_MB`).
static BUDGET: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(usize::MAX);

fn cache_budget() -> usize {
    use std::sync::atomic::Ordering;
    let budget = BUDGET.load(Ordering::Relaxed);
    if budget != usize::MAX {
        return budget;
    }
    let budget = std::env::var("REX_ROW_CACHE_MB").ok().and_then(|mb| mb.parse::<usize>().ok()).unwrap_or(64) << 20;
    BUDGET.store(budget, Ordering::Relaxed);
    budget
}

#[derive(Debug, Clone)]
struct Meta {
    /// `items` holds the row; else only the file does.
    held: bool,
    /// The row's key in the file, when it is stored under one of its own (`attach`); a row
    /// with none -- keyless, or a repeated key, kept by content -- is always held.
    stored: Option<Box<str>>,
    /// Written since the last save took it (`saved`).
    dirty: bool,
    /// The last save that wrote it: held until the file has it (`store::written`).
    serial: u64,
}

impl Meta {
    fn new() -> Self {
        Meta { held: true, stored: None, dirty: true, serial: 0 }
    }
}

#[derive(Debug, Default)]
struct Cache {
    rows: HashMap<usize, (Box<Value>, u64, usize)>,
    bytes: usize,
    tick: u64,
    /// Rows read from the file since the last `trim`, and the time it took.
    reads: usize,
    reading: std::time::Duration,
}

#[derive(Debug)]
struct Disk {
    path: PathBuf,
    reader: RefCell<Option<rusqlite::Connection>>,
    /// Connections for `read_listed`'s threads, kept between listings.
    pool: std::sync::Mutex<Vec<rusqlite::Connection>>,
}

/// Another account's view of the rows for one request (`accounts.rs`): reading a row shows
/// the account's view fields in place of the owner's, and a row is changed only once it is
/// written to. Swapping them onto every row holding the owner's -- 2,100 of the production
/// server's rows (2026-10-18) -- read all of those from the file for each request.
#[derive(Debug, Default)]
struct Overlay {
    fields: &'static [&'static str],
    /// The rows holding the owner's view fields, and the account's own by row.
    hide: std::collections::HashSet<usize>,
    show: HashMap<usize, serde_json::Map<String, Value>>,
    /// Copies read during the request, the account's fields in place (dropped at `end_overlay`).
    copies: RefCell<HashMap<usize, Box<Value>>>,
    /// The rows changed in place, each with the owner's fields taken off it and their places.
    swapped: HashMap<usize, Vec<(usize, String, Value)>>,
}

impl Overlay {
    fn covers(&self, position: usize) -> bool {
        !self.swapped.contains_key(&position) && (self.hide.contains(&position) || self.show.contains_key(&position))
    }

    /// `row` as the account sees it (`Runtime::with_account_views`).
    fn view(&self, position: usize, mut row: Value) -> Value {
        if let Some(object) = row.as_object_mut() {
            for name in self.fields {
                object.shift_remove(*name);
            }
            for (name, value) in self.show.get(&position).into_iter().flatten() {
                object.insert(name.clone(), value.clone());
            }
        }
        row
    }
}

#[derive(Debug, Default)]
pub struct Rows {
    overlay: Option<Overlay>,
    /// Each row by position: its body when held, `Null` when only the file has it.
    items: Vec<Value>,
    /// How many times `trim` let go of rows it held: a listing that read rows ahead of time
    /// reads them again once it has (`runtime::project_planned`).
    evictions: u64,
    meta: Vec<Meta>,
    disk: Option<Disk>,
    cache: RefCell<Cache>,
}

/// A clone holds every row, read from the file where it must be.
impl Clone for Rows {
    fn clone(&self) -> Self {
        let mut items = Vec::with_capacity(self.len());
        self.each(|_, row| items.push(row.clone()));
        Rows::new(items)
    }
}

impl Rows {
    pub fn new(items: Vec<Value>) -> Self {
        let meta = vec![Meta::new(); items.len()];
        Rows { overlay: None, items, evictions: 0, meta, disk: None, cache: RefCell::default() }
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    /// Whether the row at `position` is in memory.
    pub fn held(&self, position: usize) -> bool {
        position < self.items.len() && self.held_at(position)
    }

    /// Whether every row is held, as before `attach`: only then may a pass take them all.
    pub fn all_held(&self) -> bool {
        self.meta.iter().all(|meta| meta.held)
    }

    /// `meta` for every row: `all_mut`'s caller may have replaced the rows, which are then
    /// all held and unsaved.
    fn sync(&mut self) {
        if self.meta.len() != self.items.len() {
            self.meta.resize(self.items.len(), Meta::new());
        }
    }

    fn held_at(&self, position: usize) -> bool {
        self.meta.get(position).is_none_or(|meta| meta.held)
    }

    /// Every row, when all are held (`all_held`).
    pub fn all(&self) -> &Vec<Value> {
        assert!(self.all_held(), "rows not all in memory");
        &self.items
    }

    /// Every row mutably, when all are held; each is then unsaved.
    pub fn all_mut(&mut self) -> &mut Vec<Value> {
        self.sync();
        assert!(self.all_held(), "rows not all in memory");
        self.meta.iter_mut().for_each(|meta| meta.dirty = true);
        &mut self.items
    }

    pub fn get(&self, position: usize) -> Option<&Value> {
        if let Some(overlay) = self.overlay.as_ref().filter(|overlay| overlay.covers(position)) {
            let mut copies = overlay.copies.borrow_mut();
            if !copies.contains_key(&position) {
                let row = overlay.view(position, self.underlying(position)?.clone());
                copies.insert(position, Box::new(row));
            }
            let row: *const Value = &**copies.get(&position)?;
            // SAFETY: a copy is dropped only by `end_overlay`, `hold` and `remove`, through `&mut self`.
            return Some(unsafe { &*row });
        }
        self.underlying(position)
    }

    /// A view field of the row as the account in `overlay` sees it, without reading the row:
    /// `None` when no overlay covers the row and field (`hotrow::Lazy`).
    pub fn view_field(&self, position: usize, name: &str) -> Option<Option<&Value>> {
        let overlay = self.overlay.as_ref()?;
        if !overlay.fields.contains(&name) {
            return None;
        }
        if overlay.swapped.contains_key(&position) {
            return Some(self.items.get(position).and_then(|row| row.get(name)));
        }
        overlay.covers(position).then(|| overlay.show.get(&position).and_then(|fields| fields.get(name)))
    }

    /// Show `show`'s view fields (by position) in place of the owner's on the rows at `hide`
    /// until `end_overlay`.
    pub fn overlay(&mut self, fields: &'static [&'static str], hide: std::collections::HashSet<usize>, show: HashMap<usize, serde_json::Map<String, Value>>) {
        self.overlay = Some(Overlay { fields, hide, show, ..Overlay::default() });
    }

    /// End the overlay: the rows it changed in place, each with the owner's fields it took off.
    pub fn end_overlay(&mut self) -> HashMap<usize, Vec<(usize, String, Value)>> {
        self.overlay.take().map(|overlay| overlay.swapped).unwrap_or_default()
    }

    /// The row itself, whatever an overlay shows.
    fn underlying(&self, position: usize) -> Option<&Value> {
        if self.held_at(position) {
            return self.items.get(position);
        }
        let key = self.meta[position].stored.as_deref()?;
        let mut cache = self.cache.borrow_mut();
        cache.tick += 1;
        let tick = cache.tick;
        if let Some((row, used, _)) = cache.rows.get_mut(&position) {
            *used = tick;
            let row: *const Value = &**row;
            // SAFETY: a cached row's box is dropped or moved only through `&mut self`
            // (`trim`, `hold`, `remove`), which cannot happen while this borrow of `self` lives.
            return Some(unsafe { &*row });
        }
        let started = std::time::Instant::now();
        let (row, bytes) = self.read(key)?;
        cache.reads += 1;
        cache.reading += started.elapsed();
        let row = Box::new(row);
        let pointer: *const Value = &*row;
        cache.bytes += bytes;
        cache.rows.insert(position, (row, tick, bytes));
        // SAFETY: as above; the box's heap value does not move when the map grows.
        Some(unsafe { &*pointer })
    }

    /// The row, mutably: held from now on, and unsaved.
    pub fn get_mut(&mut self, position: usize) -> Option<&mut Value> {
        self.sync();
        self.hold(position)?;
        self.swap(position);
        let meta = &mut self.meta[position];
        meta.dirty = true;
        self.items.get_mut(position)
    }

    /// The row, mutably, for a change undone before the request ends (`accounts.rs`): held
    /// until then, not marked unsaved.
    pub fn get_unmarked_mut(&mut self, position: usize) -> Option<&mut Value> {
        self.sync();
        self.hold(position)?;
        self.swap(position);
        self.items.get_mut(position)
    }

    pub fn push(&mut self, row: Value) {
        self.sync();
        self.items.push(row);
        self.meta.push(Meta::new());
    }

    pub fn remove(&mut self, position: usize) -> Option<Value> {
        self.sync();
        self.hold(position)?;
        self.meta.remove(position);
        let row = self.items.remove(position);
        self.shift_cache(&[position]);
        Some(row)
    }

    /// Remove the rows at `gone`, ascending.
    pub fn remove_all(&mut self, gone: &[usize]) {
        self.sync();
        let is_gone = |position: usize| gone.binary_search(&position).is_ok();
        let mut index = 0;
        self.items.retain(|_| {
            index += 1;
            !is_gone(index - 1)
        });
        let mut index = 0;
        self.meta.retain(|_| {
            index += 1;
            !is_gone(index - 1)
        });
        self.shift_cache(gone);
    }

    fn shift_cache(&mut self, gone: &[usize]) {
        if let Some(overlay) = self.overlay.as_mut() {
            let moved = |position: usize| (gone.binary_search(&position).is_err()).then(|| position - gone.partition_point(|&at| at < position));
            overlay.hide = overlay.hide.iter().filter_map(|position| moved(*position)).collect();
            overlay.show = std::mem::take(&mut overlay.show).into_iter().filter_map(|(position, fields)| Some((moved(position)?, fields))).collect();
            overlay.swapped = std::mem::take(&mut overlay.swapped).into_iter().filter_map(|(position, taken)| Some((moved(position)?, taken))).collect();
            overlay.copies.get_mut().clear();
        }
        let cache = self.cache.get_mut();
        let rows = std::mem::take(&mut cache.rows);
        cache.bytes = 0;
        for (position, entry) in rows {
            if gone.binary_search(&position).is_err() {
                cache.bytes += entry.2;
                cache.rows.insert(position - gone.partition_point(|&at| at < position), entry);
            }
        }
    }

    /// Call `visit` with every row in order, reading each the file holds without keeping it.
    pub fn each(&self, mut visit: impl FnMut(usize, &Value)) {
        for position in 0..self.len() {
            if self.held_at(position) {
                visit(position, &self.items[position]);
                continue;
            }
            let meta = &self.meta[position];
            let cached = self.cache.borrow().rows.get(&position).map(|(row, _, _)| (**row).clone());
            if let Some(row) = cached {
                visit(position, &row);
            } else if let Some((row, _)) = meta.stored.as_deref().and_then(|key| self.read(key)) {
                visit(position, &row);
            }
        }
    }

    /// Keep rows in the state database at `path` from now on: each held row is then the
    /// file's under `keys`' key for it (`runtime::key_of`; the first of a repeated key), and
    /// held until a save writes it -- `clean` when the file already holds every row as it is.
    pub fn attach(&mut self, path: &Path, key_of: impl Fn(&Value) -> Option<String>, clean: bool) {
        self.sync();
        let mut taken = std::collections::HashSet::new();
        for (row, meta) in self.items.iter().zip(self.meta.iter_mut()) {
            if !meta.held {
                continue;
            }
            meta.stored = key_of(row).filter(|key| !key.starts_with('#') && taken.insert(key.clone())).map(Into::into);
            meta.dirty = !clean || meta.stored.is_none();
            meta.serial = 0;
        }
        self.disk = Some(Disk { path: path.to_path_buf(), reader: RefCell::new(None), pool: Default::default() });
    }

    pub fn attached(&self) -> Option<&Path> {
        self.disk.as_ref().map(|disk| disk.path.as_path())
    }

    /// A save took every unsaved row (`Catalog::take_changes`) and queued it as `serial`, or
    /// found nothing to write (`None`): each is held until the file has that save.
    pub fn saved(&mut self, serial: Option<u64>, key_of: impl Fn(&Value) -> Option<String>) {
        self.sync();
        let attached = self.disk.is_some();
        let mut taken: std::collections::HashSet<String> =
            self.meta.iter().filter_map(|meta| meta.stored.as_deref().map(str::to_string)).collect();
        for (row, meta) in self.items.iter().zip(self.meta.iter_mut()) {
            if !meta.dirty {
                continue;
            }
            meta.dirty = false;
            if let Some(serial) = serial {
                meta.serial = meta.serial.max(serial);
            }
            // A row added since `attach` is stored under its own key once a save wrote it.
            if attached && meta.stored.is_none() {
                meta.stored = key_of(row).filter(|key| !key.starts_with('#') && taken.insert(key.clone())).map(Into::into);
                if meta.stored.is_none() {
                    meta.dirty = true;
                }
            }
        }
    }

    /// Let go of what the file holds: every saved row whose save it has (`durable`, the last
    /// serial written), and the rows read from it beyond the budget, least recently used first.
    pub fn trim(&mut self) {
        let Some(disk) = &self.disk else {
            return;
        };
        let durable = crate::store::written(&disk.path);
        self.meta.resize(self.items.len(), Meta::new());
        let mut evicted = false;
        for (row, meta) in self.items.iter_mut().zip(self.meta.iter_mut()) {
            if meta.held && !meta.dirty && meta.stored.is_some() && meta.serial <= durable {
                *row = Value::Null;
                meta.held = false;
                evicted = true;
            }
        }
        self.evictions += u64::from(evicted);
        let budget = cache_budget();
        let cache = self.cache.get_mut();
        if cache.reads > 0 {
            crate::runtime::logging::log(
                crate::runtime::logging::Level::Debug,
                &format!("rows: {} read from the file in {:?}", cache.reads, cache.reading),
            );
        }
        cache.reads = 0;
        cache.reading = std::time::Duration::ZERO;
        if cache.bytes > budget {
            let mut by_use: Vec<(u64, usize)> = cache.rows.iter().map(|(position, (_, used, _))| (*used, *position)).collect();
            by_use.sort_unstable();
            for (_, position) in by_use {
                if cache.bytes <= budget / 2 {
                    break;
                }
                if let Some((_, _, bytes)) = cache.rows.remove(&position) {
                    cache.bytes -= bytes;
                }
            }
        }
    }

    /// The rows at `positions` for a listing, in order: those in memory as they are, the others
    /// read on `threads` threads without their Parts' `Stream` lists -- which no listing row
    /// carries -- and kept nowhere. A listing of a show library's 34k episodes read them one at
    /// a time, and kept them all, until the request ended (2026-10-18).
    pub fn read_listed(&self, positions: &[usize], threads: usize) -> Vec<Option<std::borrow::Cow<'_, Value>>> {
        let mut out: Vec<Option<std::borrow::Cow<'_, Value>>> = Vec::with_capacity(positions.len());
        let mut wanted: Vec<(usize, &str)> = Vec::new();
        {
            let cache = self.cache.borrow();
            for (at, &position) in positions.iter().enumerate() {
                if self.held_at(position) {
                    out.push(self.items.get(position).map(std::borrow::Cow::Borrowed));
                } else if let Some((row, _, _)) = cache.rows.get(&position) {
                    let row: *const Value = &**row;
                    // SAFETY: as in `get`.
                    out.push(Some(std::borrow::Cow::Borrowed(unsafe { &*row })));
                } else {
                    out.push(None);
                    if let Some(key) = self.meta[position].stored.as_deref() {
                        wanted.push((at, key));
                    }
                }
            }
        }
        let Some(disk) = self.disk.as_ref().filter(|_| !wanted.is_empty()) else {
            return out;
        };
        let per = wanted.len().div_ceil(threads.max(1));
        let (path, pool) = (&disk.path, &disk.pool);
        let read: Vec<(usize, Value)> = std::thread::scope(|scope| {
            let workers: Vec<_> = wanted
                .chunks(per.max(1))
                .map(|chunk| {
                    scope.spawn(move || {
                        let connection = pool.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).pop();
                        let connection = match connection {
                            Some(connection) => connection,
                            None => match crate::store::open_rows(path) {
                                Ok(connection) => connection,
                                Err(error) => {
                                    log_error(&format!("rows: cannot read {}: {error}", path.display()));
                                    return Vec::new();
                                }
                            },
                        };
                        let mut found = Vec::with_capacity(chunk.len());
                        if let Ok(mut statement) = connection.prepare_cached("SELECT body FROM row WHERE coll = ?1 AND key = ?2") {
                            for &(at, key) in chunk {
                                let row = statement.query_row(rusqlite::params![COLLECTION, key], |found| {
                                    let text = found.get_ref(0)?.as_str()?;
                                    Ok(listed(text))
                                });
                                match row {
                                    Ok(Some(row)) => found.push((at, row)),
                                    _ => log_error(&format!("rows: row {key} not read")),
                                }
                            }
                        }
                        pool.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).push(connection);
                        found
                    })
                })
                .collect();
            workers.into_iter().flat_map(|worker| worker.join().unwrap_or_default()).collect()
        });
        for (at, row) in read {
            out[at] = Some(std::borrow::Cow::Owned(row));
        }
        out
    }

    pub fn evictions(&self) -> u64 {
        self.evictions
    }

    /// Whether the row at `position` is held, and its key in the file when it has one.
    pub fn stored(&self, position: usize) -> Option<&str> {
        self.meta.get(position).and_then(|meta| meta.stored.as_deref())
    }

    /// A row about to be written while an overlay covers it: the account's fields put on it in
    /// place, the owner's taken off with their places (`end_overlay` hands them back).
    fn swap(&mut self, position: usize) {
        // Every row written while the overlay stands: one the owner holds no view fields on
        // takes the account's fields too, and gives back none.
        let Some(overlay) = self.overlay.as_mut().filter(|overlay| !overlay.swapped.contains_key(&position)) else {
            return;
        };
        overlay.copies.get_mut().remove(&position);
        let Some(object) = self.items.get_mut(position).and_then(Value::as_object_mut) else {
            return;
        };
        let mut taken = Vec::new();
        for (index, (name, _)) in object.iter().enumerate() {
            if overlay.fields.contains(&name.as_str()) {
                taken.push((index, name.clone(), Value::Null));
            }
        }
        for entry in taken.iter_mut() {
            entry.2 = object.shift_remove(&entry.1).unwrap_or(Value::Null);
        }
        for (name, value) in overlay.show.get(&position).into_iter().flatten() {
            object.insert(name.clone(), value.clone());
        }
        overlay.swapped.insert(position, taken);
    }

    /// Bring the row at `position` into `items`.
    fn hold(&mut self, position: usize) -> Option<()> {
        let meta = self.meta.get(position)?;
        if meta.held {
            return Some(());
        }
        let cached = self.cache.get_mut().rows.remove(&position);
        let row = match cached {
            Some((row, _, bytes)) => {
                self.cache.get_mut().bytes -= bytes;
                *row
            }
            None => self.read(meta.stored.as_deref()?)?.0,
        };
        self.items[position] = row;
        self.meta[position].held = true;
        Some(())
    }

    /// A row from the file, as the catalog holds rows: its streams packed, fitted to size
    /// (`catalog::compact`), and the bytes of its text.
    fn read(&self, key: &str) -> Option<(Value, usize)> {
        let disk = self.disk.as_ref()?;
        #[cfg(test)]
        test_support::DISK_READS.with(|reads| reads.set(reads.get() + 1));
        let mut reader = disk.reader.borrow_mut();
        if reader.is_none() {
            match crate::store::open_rows(&disk.path) {
                Ok(connection) => *reader = Some(connection),
                Err(error) => {
                    log_error(&format!("rows: cannot read {}: {error}", disk.path.display()));
                    return None;
                }
            }
        }
        let connection = reader.as_ref()?;
        let read = connection
            .prepare_cached("SELECT body FROM row WHERE coll = ?1 AND key = ?2")
            .and_then(|mut statement| {
                statement.query_row(rusqlite::params![COLLECTION, key], |found| {
                    let text = found.get_ref(0)?.as_str()?;
                    Ok(serde_json::from_str::<Value>(text).map(|row| (row, text.len())))
                })
            });
        match read {
            Ok(Ok((mut row, bytes))) => {
                crate::catalog::pack_streams(&mut row);
                crate::catalog::compact(&mut row);
                Some((row, bytes * 3))
            }
            Ok(Err(error)) => {
                log_error(&format!("rows: row {key} unreadable: {error}"));
                None
            }
            Err(error) => {
                log_error(&format!("rows: row {key} not read: {error}"));
                None
            }
        }
    }
}

/// A row's text parsed for a listing: every Part without its `Stream` list, which a listing
/// row drops and which is most of a row's text, and without the fields every listing row
/// drops (`runtime::GRID_DROPPED`): parsed here only to be freed by the projecting thread.
pub(crate) fn listed(text: &str) -> Option<Value> {
    use serde::de::{DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
    struct Pruned {
        top: bool,
        in_part: bool,
        parts: bool,
    }
    impl<'de> DeserializeSeed<'de> for Pruned {
        type Value = Value;
        fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
            deserializer.deserialize_any(self)
        }
    }
    impl<'de> Visitor<'de> for Pruned {
        type Value = Value;
        fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
            formatter.write_str("a row")
        }
        fn visit_bool<E>(self, value: bool) -> Result<Value, E> {
            Ok(Value::Bool(value))
        }
        fn visit_i64<E>(self, value: i64) -> Result<Value, E> {
            Ok(Value::from(value))
        }
        fn visit_u64<E>(self, value: u64) -> Result<Value, E> {
            Ok(Value::from(value))
        }
        fn visit_f64<E>(self, value: f64) -> Result<Value, E> {
            Ok(serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number))
        }
        fn visit_str<E>(self, value: &str) -> Result<Value, E> {
            Ok(Value::String(value.to_string()))
        }
        fn visit_string<E>(self, value: String) -> Result<Value, E> {
            Ok(Value::String(value))
        }
        fn visit_unit<E>(self) -> Result<Value, E> {
            Ok(Value::Null)
        }
        fn visit_none<E>(self) -> Result<Value, E> {
            Ok(Value::Null)
        }
        fn visit_seq<S: SeqAccess<'de>>(self, mut seq: S) -> Result<Value, S::Error> {
            let mut out = Vec::new();
            // The elements of a `Part` list are Parts.
            while let Some(value) = seq.next_element_seed(Pruned { top: false, in_part: self.parts, parts: false })? {
                out.push(value);
            }
            Ok(Value::Array(out))
        }
        fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Value, M::Error> {
            let mut out = serde_json::Map::new();
            while let Some(key) = map.next_key::<String>()? {
                if (self.in_part && key == "Stream") || (self.top && crate::runtime::GRID_DROPPED.contains(&key.as_str())) {
                    map.next_value::<IgnoredAny>()?;
                    continue;
                }
                let parts = key == "Part";
                let value = map.next_value_seed(Pruned { top: false, in_part: false, parts })?;
                out.insert(key, value);
            }
            Ok(Value::Object(out))
        }
    }
    let mut deserializer = serde_json::Deserializer::from_str(text);
    Pruned { top: true, in_part: false, parts: false }.deserialize(&mut deserializer).ok()
}

/// How many threads read a listing's rows (`prefetch_listed`, `read_listed`): two kept ahead of
/// the projecting thread on the production server's library, and four left the heap memory
/// they parsed into held by their allocator arenas after the listing (`test_perf_memory`).
pub fn listing_threads() -> usize {
    std::thread::available_parallelism().map_or(1, |count| count.get()).min(2)
}

/// Read and parse the rows `keys` names from the file at `path` as `listed`, on `threads`
/// threads of its own, sending each chunk of `chunk` keys as it is ready: a listing projects
/// one chunk while the next are read (`runtime::project_planned`). At most `ahead` chunks wait.
pub fn prefetch_listed(
    path: PathBuf,
    keys: Vec<String>,
    chunk: usize,
    threads: usize,
    ahead: usize,
) -> std::sync::mpsc::Receiver<(usize, Vec<Option<Value>>)> {
    let (sender, receiver) = std::sync::mpsc::sync_channel(ahead.max(1));
    let keys = std::sync::Arc::new(keys);
    let chunks = keys.len().div_ceil(chunk.max(1));
    for worker in 0..threads.max(1) {
        let (sender, keys, path) = (sender.clone(), std::sync::Arc::clone(&keys), path.clone());
        let _ = std::thread::Builder::new().name("rows-listed".to_string()).spawn(move || {
            let connection = match crate::store::open_rows(&path) {
                Ok(connection) => connection,
                Err(error) => {
                    log_error(&format!("rows: cannot read {}: {error}", path.display()));
                    return;
                }
            };
            let Ok(mut statement) = connection.prepare_cached("SELECT body FROM row WHERE coll = ?1 AND key = ?2") else {
                return;
            };
            for index in (worker..chunks).step_by(threads.max(1)) {
                let rows = keys[index * chunk..((index + 1) * chunk).min(keys.len())]
                    .iter()
                    .map(|key| {
                        statement
                            .query_row(rusqlite::params![COLLECTION, key], |found| Ok(listed(found.get_ref(0)?.as_str()?)))
                            .ok()
                            .flatten()
                    })
                    .collect();
                // The listing went away (a closed connection): stop reading for it.
                if sender.send((index, rows)).is_err() {
                    return;
                }
            }
        });
    }
    receiver
}

#[cfg(test)]
mod overlay_tests {
    use serde_json::json;

    use crate::catalog::Catalog;

    const FIELDS: [&str; 2] = ["viewCount", "viewOffset"];

    /// The account's view while it stands, the owner's afterwards; what the account wrote is
    /// handed back as the account's (`accounts.rs`).
    #[test]
    fn an_overlay_shows_the_account_and_gives_the_owner_back() {
        let mut catalog = Catalog::from_value(json!({"items": [
            {"ratingKey": "1", "title": "a", "viewCount": 1, "year": 2000},
            {"ratingKey": "2", "title": "b", "viewOffset": 30},
            {"ratingKey": "3", "title": "c"}]})).unwrap();
        let hide = ["1".to_string(), "2".to_string()].into_iter().collect();
        let show = [("2".to_string(), json!({"viewCount": 4}))].into_iter().collect();
        catalog.overlay_views(&FIELDS, &hide, &show);
        assert_eq!(catalog.get("1").unwrap(), &json!({"ratingKey": "1", "title": "a", "year": 2000}));
        assert_eq!(catalog.get("2").unwrap(), &json!({"ratingKey": "2", "title": "b", "viewCount": 4}));
        assert_eq!(catalog.hot().at(1).unwrap().get("viewCount"), Some(&json!(4)));
        assert_eq!(catalog.hot().at(0).unwrap().get("viewCount"), None);
        catalog.get_mut("1").unwrap()["viewCount"] = json!(9);
        catalog.get_mut("3").unwrap()["viewOffset"] = json!(5);
        assert_eq!(catalog.get("1").unwrap()["viewCount"], 9);
        let swapped = catalog.end_view_overlay();
        assert_eq!(swapped.len(), 2);
        assert_eq!(swapped["1"], vec![(2, "viewCount".to_string(), json!(1))]);
        assert!(swapped["3"].is_empty());
        assert_eq!(catalog.get("1").unwrap()["viewCount"], 9, "the account's, until put back");
    }
}

/// What a test needs to put a catalog's rows on disk and count the rows read back from it.
#[cfg(test)]
pub(crate) mod test_support {
    use serde_json::Value;

    use crate::catalog::Catalog;

    thread_local! {
        /// Rows this thread read from the state file.
        pub(crate) static DISK_READS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
    }

    pub(crate) fn key_of(row: &Value) -> Option<String> {
        row.get("ratingKey").map(|key| match key {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        })
    }

    /// What the runtime does to save (`snapshot_state`), and to flush when asked.
    pub(crate) fn save(catalog: &mut Catalog, cache: &mut crate::store::Cache, path: &std::path::Path, flush: bool) {
        let changes = catalog.take_changes();
        let delta = {
            let catalog = &*catalog;
            cache.diff_rows(
                "items",
                &|visit| catalog.each_row(|_, row| visit(row)),
                (!changes.all).then_some(&changes.keys),
                key_of,
                |key| catalog.get(key),
                |key| catalog.position(key),
            )
        };
        if delta.is_empty() {
            catalog.saved(None, key_of);
        } else {
            let serial = crate::store::next_serial();
            crate::store::enqueue(path, serial, delta);
            catalog.saved(Some(serial), key_of);
        }
        if flush {
            crate::store::flush(path, u64::MAX).unwrap();
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::{json, Value};
    use std::collections::HashSet;

    use super::test_support::{key_of, save};
    use crate::catalog::{By, Catalog};

    fn same(disk: &Catalog, memory: &Catalog, step: usize) {
        assert_eq!(disk.len(), memory.len(), "step {step}: row count");
        for position in 0..memory.len() {
            let row = memory.row_at(position).unwrap();
            let key = key_of(row).unwrap();
            assert_eq!(disk.get(&key), Some(row), "step {step}: row {key}");
            let (hot, held) = (disk.hot(), memory.hot());
            let (a, b) = (hot.at(position).unwrap(), held.at(position).unwrap());
            for field in ["ratingKey", "viewOffset", "parentRatingKey", "title"] {
                assert_eq!(a.get(field), b.get(field), "step {step}: {key} {field}");
            }
            if let Some(parent) = row.get("parentRatingKey").and_then(Value::as_str) {
                let keys = |catalog: &Catalog| catalog.rows_by(By::Parent, parent).into_iter().filter_map(key_of).collect::<Vec<_>>();
                assert_eq!(keys(disk), keys(memory), "step {step}: children of {parent}");
            }
        }
    }

    /// A catalog kept in its state file answers, after any mix of writes, adds, removes, saves
    /// that did or did not reach the file, and rows let go of, what one in memory answers.
    #[test]
    fn rows_in_the_file_answer_as_rows_in_memory() {
        super::BUDGET.store(4096, std::sync::atomic::Ordering::Relaxed);
        let directory = std::env::temp_dir().join(format!("rex-rows-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("rex.db");
        let mut items = Vec::new();
        for show in 0..6 {
            items.push(json!({"ratingKey": format!("{}", 100 + show), "type": "show", "title": format!("Show {show}"), "librarySectionID": 2}));
            for episode in 0..8 {
                items.push(json!({"ratingKey": format!("{}", 1000 + show * 10 + episode), "type": "episode", "title": format!("E{episode}"),
                    "parentRatingKey": format!("{}", 100 + show), "summary": "x".repeat(500),
                    "Media": [{"id": 1, "Part": [{"id": show * 10 + episode, "file": format!("/f/{show}/{episode}.mkv"), "Stream": [{"id": 1, "codec": "h264"}]}]}]}));
            }
        }
        let mut memory = Catalog::from_value(json!({"items": items.clone()})).unwrap();
        let mut disk = Catalog::from_value(json!({"items": items})).unwrap();
        let mut cache = crate::store::Cache::default();
        disk.attach(&path, key_of, false);
        save(&mut disk, &mut cache, &path, true);
        disk.pack_pending();
        memory.pack_pending();
        let held = (0..disk.len()).filter(|position| disk.held_row(&key_of(memory.row_at(*position).unwrap()).unwrap()) != Some(None)).count();
        assert_eq!(held, 0, "a saved row is let go of once the file has it");
        same(&disk, &memory, 0);
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let mut next = |limit: usize| {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            (seed % limit as u64) as usize
        };
        let mut minted = 5000;
        for step in 1..400 {
            let keys: Vec<String> = (0..memory.len()).filter_map(|position| key_of(memory.row_at(position).unwrap())).collect();
            match next(10) {
                0..=3 => {
                    let key = &keys[next(keys.len())];
                    let offset = json!(step);
                    memory.get_mut(key).unwrap()["viewOffset"] = offset.clone();
                    disk.get_mut(key).unwrap()["viewOffset"] = offset;
                }
                4 => {
                    minted += 1;
                    let row = json!({"ratingKey": minted.to_string(), "type": "episode", "title": "new", "parentRatingKey": "101"});
                    memory.add(row.clone());
                    disk.add(row);
                }
                5 if keys.len() > 10 => {
                    let gone = HashSet::from([keys[next(keys.len())].clone()]);
                    memory.remove_many(&gone);
                    disk.remove_many(&gone);
                }
                6 => {
                    let flush = next(2) == 0;
                    save(&mut disk, &mut cache, &path, flush);
                    let _ = memory.take_changes();
                }
                7 => {
                    crate::store::flush(&path, u64::MAX).unwrap();
                }
                _ => {
                    disk.pack_pending();
                    memory.pack_pending();
                }
            }
            same(&disk, &memory, step);
        }
        let _ = std::fs::remove_dir_all(&directory);
    }
}

#[cfg(test)]
mod listed_tests {
    #[test]
    fn a_listed_row_is_the_row_less_what_every_listing_row_drops() {
        let text = r#"{"ratingKey":"7","Rating":[{"image":"x"}],"Extras":{"size":1},"Genre":[{"tag":"Drama"}],
            "Media":[{"id":1,"Part":[{"id":2,"Stream":[{"id":3}],"Rating":"kept below the top"}]}]}"#;
        let expected = serde_json::json!({"ratingKey":"7","Genre":[{"tag":"Drama"}],
            "Media":[{"id":1,"Part":[{"id":2,"Rating":"kept below the top"}]}]});
        assert_eq!(super::listed(text), Some(expected));
    }
}
