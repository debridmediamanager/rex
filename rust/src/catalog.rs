//! Native catalog model shared by the future library and HTTP layers.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;

use serde_json::{Map, Value};

use crate::corpus::{corpus_row, CorpusRow, Names};
use crate::hotrow::{HotRow, Lazy};
use crate::rows::Rows;
use crate::{hierarchy, mediainfo};

#[derive(Debug, Clone)]
pub struct Catalog {
    /// Untyped top-level document, retained so future routes can preserve
    /// arbitrary server properties rather than narrowing the catalog schema.
    pub document: Map<String, Value>,
    pub sections: Vec<Value>,
    /// Private so that every mutable path is seen: `get_mut` records its row and
    /// `items_mut` marks them all, which is how a save writes only what changed
    /// (`store.rs`) instead of serialising every row to find out. In memory, or in the state
    /// database once `attach`ed (`rows.rs`).
    rows: Rows,
    pub by_id: HashMap<String, usize>,
    changes: Changes,
    /// The rows written since the last `take_touched`, kept apart from `changes` because a
    /// save takes those: the runtime re-stamps the artwork paths of these (`restamp_artwork`).
    touched: Changes,
    /// The rows written since the last `take_writes`, kept apart again: the account overlay
    /// (`accounts.rs`) follows which rows hold the owner's watch state by them.
    writes: Changes,
    /// `children`'s rows by parent key and each section's movies and shows, built on first
    /// use. Without it a listing scanned all rows once per show and per season: 9 s for a
    /// page of the production server's 1,500 movies and over 30 s for its 13,500 episodes
    /// (2026-10-10). A row written in place re-files only itself: dropping the index on
    /// every `get_mut` made the first listing after each progress beat rescan the library,
    /// which on the production server (2026-10-16) paged all of it back in from swap.
    index: RowIndex,
    /// Rows written since `pack_pending` last packed their streams (`PACKED_STREAMS`), or all.
    unpacked: Changes,
    /// Changes with every mutable access and is never reused (`generation`), so an answer
    /// computed from the rows can be kept until they change (the runtime's `LibraryMemo`).
    generation: u64,
}

static GENERATIONS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

fn next_generation() -> u64 {
    GENERATIONS.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
}

/// `Catalog::index`: the rows under each key of every `By` lookup, and the positions written since.
#[derive(Default)]
struct RowIndex(std::cell::RefCell<IndexState>);

/// A clone builds its own index when it is first read.
impl Clone for RowIndex {
    fn clone(&self) -> Self {
        Self::default()
    }
}

impl std::fmt::Debug for RowIndex {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RowIndex")
    }
}

#[derive(Default)]
struct IndexState {
    built: Option<Positions>,
    stale: Vec<usize>,
}

/// What a row is looked up by without reading every row. Each lookup keeps the text its
/// callers compared before it existed, so an answer read through it is the same.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum By {
    /// `parentRatingKey`, a string or a number (`children`).
    Parent,
    /// A movie's or show's `librarySectionID` (`section_refs`).
    Section,
    /// Each `Media[].Part[].id`, as `string_value` writes it: the byte reads find their Part by it.
    Part,
    /// Each `Media[].Part[].file`.
    File,
    /// A clip's owner, `_parentRatingKey` as `string_value` writes it (`extras_of`).
    ClipParent,
    /// The directory of each `Media[].Part[].file`: a pushed extra finds the title of its
    /// release by it (`scan::add_clip`).
    Directory,
    /// Each (source, source id) the row's Media came from (`feed::versions`), as `release_key`
    /// joins them: a feed push finds the rows its items are by it.
    Release,
    /// Each uploaded subtitle's stream `id` (a Stream carrying `_content`): the only streams
    /// `/library/streams/{id}` reads back or deletes, found without reading every row.
    Upload,
}

const LOOKUPS: usize = 8;

/// The `By::Release` key of a source's item.
pub fn release_key(source: &str, sid: &str) -> String {
    format!("{source}\0{sid}")
}

/// What a lookup files a key under: the lookup and the key's text, hashed. Two keys sharing
/// a hash -- about one chance in 10^10 for a library of a million keys -- would list each
/// other's rows; the text is not kept, which was half the index (2026-10-18).
fn hash_key(by: By, key: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    by.slot().hash(&mut hasher);
    key.hash(&mut hasher);
    hasher.finish()
}

impl By {
    fn slot(self) -> usize {
        self as usize
    }

    fn keys(self, item: &Value, into: &mut Vec<u64>) {
        let parts = || {
            item.get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
        };
        let hash = |key: &str| hash_key(self, key);
        match self {
            By::Parent => into.extend(key_text(item.get("parentRatingKey")).as_deref().map(hash)),
            By::Section => into.extend(section_of(item).as_deref().map(hash)),
            By::Part => into.extend(parts().filter_map(|part| part.get("id")).map(|id| hash(&text_of(id)))),
            By::File => into.extend(parts().filter_map(|part| part.get("file").and_then(Value::as_str)).map(hash)),
            By::ClipParent => into.extend(item.get("_parentRatingKey").map(|key| hash(&text_of(key)))),
            By::Directory => into.extend(
                parts()
                    .filter_map(|part| part.get("file").and_then(Value::as_str))
                    .filter_map(|file| file.rsplit_once('/').map(|(directory, _)| directory))
                    .map(hash),
            ),
            By::Release => into.extend(versions(item).iter().map(|(source, sid, _)| hash(&release_key(source, sid)))),
            By::Upload => into.extend(
                parts()
                    // A packed Part is decoded only when its text holds an upload.
                    .filter(|part| part.get(PACKED_STREAMS).and_then(Value::as_str).is_none_or(|text| text.contains("\"_content\"")))
                    .flat_map(|part| {
                        part_streams(part)
                            .iter()
                            .filter(|stream| stream.get("_content").is_some())
                            .filter_map(|stream| stream.get("id").map(|id| hash(&text_of(id))))
                            .collect::<Vec<_>>()
                    }),
            ),
        }
    }
}

/// `runtime::string_value`: the text a key field is compared by.
fn text_of(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Bool(value) => value.to_string(),
        _ => String::new(),
    }
}

/// The positions filed under one key, in row order; most keys name one row.
enum Filed {
    One(u32),
    Many(Vec<u32>),
}

impl Filed {
    fn positions(&self) -> &[u32] {
        match self {
            Filed::One(position) => std::slice::from_ref(position),
            Filed::Many(positions) => positions,
        }
    }
}

#[derive(Default)]
struct Positions {
    /// Each row's keys in every lookup, so a row written again leaves the keys it had.
    of: Vec<Box<[u64]>>,
    at: HashMap<u64, Filed>,
    hot: Vec<HotRow>,
    /// Each row's search entry (`corpus.rs`), and the tag names they share.
    corpus: Vec<CorpusRow>,
    names: Names,
}

impl Positions {
    fn file(&mut self, position: usize, item: &Value) {
        let mut keys = Vec::new();
        for by in BY {
            by.keys(item, &mut keys);
        }
        keys.sort_unstable();
        keys.dedup();
        if self.of.len() <= position {
            self.of.resize_with(position + 1, Default::default);
        }
        if *self.of[position] != *keys {
            for key in std::mem::take(&mut self.of[position]).iter() {
                self.unfile(*key, position as u32);
            }
            for key in &keys {
                self.refile(*key, position as u32);
            }
            self.of[position] = keys.into_boxed_slice();
        }
        let entry = corpus_row(item, &mut self.names);
        match self.corpus.get_mut(position) {
            Some(slot) => {
                self.names.release(slot);
                *slot = entry;
                self.names.compact(&mut self.corpus);
            }
            None => {
                self.corpus.resize_with(position, CorpusRow::default);
                self.corpus.push(entry);
            }
        }
        let hot = HotRow::of(position, item);
        match self.hot.get_mut(position) {
            Some(slot) => *slot = hot,
            None => {
                while self.hot.len() < position {
                    self.hot.push(HotRow::of(self.hot.len(), &Value::Null));
                }
                self.hot.push(hot);
            }
        }
    }

    fn refile(&mut self, key: u64, position: u32) {
        match self.at.get_mut(&key) {
            None => {
                self.at.insert(key, Filed::One(position));
            }
            Some(Filed::One(one)) if *one == position => {}
            Some(Filed::One(one)) => {
                let mut positions = vec![*one, position];
                positions.sort_unstable();
                self.at.insert(key, Filed::Many(positions));
            }
            Some(Filed::Many(positions)) => {
                if let Err(at) = positions.binary_search(&position) {
                    positions.insert(at, position);
                }
            }
        }
    }

    fn unfile(&mut self, key: u64, position: u32) {
        match self.at.get_mut(&key) {
            Some(Filed::One(one)) if *one == position => {
                self.at.remove(&key);
            }
            Some(Filed::Many(positions)) => {
                if let Ok(at) = positions.binary_search(&position) {
                    positions.remove(at);
                }
                if let [only] = positions[..] {
                    self.at.insert(key, Filed::One(only));
                }
            }
            _ => {}
        }
    }

    fn under(&self, by: By, key: &str) -> &[u32] {
        self.at.get(&hash_key(by, key)).map_or(&[], Filed::positions)
    }
}

fn key_text(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(value)) => Some(value.clone()),
        Some(Value::Number(value)) => Some(value.to_string()),
        _ => None,
    }
}

/// The section a movie or show row is listed in; other rows are listed under their parents.
fn section_of(item: &Value) -> Option<String> {
    matches!(item.get("type").and_then(Value::as_str), Some("movie" | "show"))
        .then(|| match item.get("librarySectionID") {
            Some(Value::Bool(value)) => value.to_string(),
            other => key_text(other).unwrap_or_default(),
        })
}

impl RowIndex {
    fn drop_all(&self) {
        let mut state = self.0.borrow_mut();
        state.built = None;
        state.stale.clear();
    }

    /// Rows were removed at `gone` (ascending positions before the removal): the rows after
    /// them moved down. Shifted rather than rebuilt: a rebuild derives every row's hot fields
    /// again, which on a 40,000-row library made each row delete cost 0.15 s (`test_perf_c87`).
    fn removed(&self, gone: &[usize]) {
        let mut state = self.0.borrow_mut();
        let Some(positions) = state.built.as_mut() else {
            return;
        };
        let moved = |position: usize| position - gone.partition_point(|&at| at < position);
        for &at in gone.iter().rev() {
            if at < positions.of.len() {
                for key in std::mem::take(&mut positions.of[at]).iter() {
                    positions.unfile(*key, at as u32);
                }
            }
        }
        let is_gone = |position: usize| gone.binary_search(&position).is_ok();
        let mut index = 0;
        positions.of.retain(|_| {
            index += 1;
            !is_gone(index - 1)
        });
        positions.hot.retain(|hot| !is_gone(hot.position));
        let mut index = 0;
        let names = &mut positions.names;
        positions.corpus.retain(|entry| {
            index += 1;
            let keep = !is_gone(index - 1);
            if !keep {
                names.release(entry);
            }
            keep
        });
        for hot in positions.hot.iter_mut() {
            hot.position = moved(hot.position);
        }
        for filed in positions.at.values_mut() {
            match filed {
                Filed::One(one) => *one = moved(*one as usize) as u32,
                Filed::Many(many) => many.iter_mut().for_each(|one| *one = moved(*one as usize) as u32),
            }
        }
        state.stale = std::mem::take(&mut state.stale)
            .into_iter()
            .filter(|position| !is_gone(*position))
            .map(moved)
            .collect();
    }

    fn mark(&self, position: usize) {
        let mut state = self.0.borrow_mut();
        if state.built.is_some() {
            state.stale.push(position);
        }
    }

    /// Bring the index up to date with `items`. Only this borrows it mutably, and only when a
    /// row was written since, which needs `&mut Catalog`: so a read inside another read (a
    /// pass over `hot` asking for a row's children) never finds it borrowed.
    fn fresh(&self, rows: &Rows) {
        let needed = {
            let state = self.0.borrow();
            state.built.is_none() || !state.stale.is_empty()
        };
        if !needed {
            return;
        }
        let mut state = self.0.borrow_mut();
        let stale = std::mem::take(&mut state.stale);
        let positions = state.built.get_or_insert_with(|| {
            let mut positions = Positions::default();
            rows.each(|position, item| positions.file(position, item));
            positions
        });
        // A row written is held (`Rows::get_mut`), so this reads nothing from the file.
        for position in stale {
            if let Some(item) = rows.get(position) {
                positions.file(position, item);
            }
        }
    }

    /// The positions under `key` in the `by` lookup.
    fn read(&self, rows: &Rows, by: By, key: &str) -> Vec<usize> {
        self.fresh(rows);
        let state = self.0.borrow();
        let positions = state.built.as_ref().expect("fresh builds it");
        positions.under(by, key).iter().map(|position| *position as usize).collect()
    }

    /// The index, borrowed for as long as the catalog is: like a row from `get`.
    fn positions(&self, rows: &Rows) -> &Positions {
        self.fresh(rows);
        // SAFETY: the index changes only in `fresh` -- when a row was written since, which takes
        // `&mut Catalog` and so ends every borrow handed out before it, and before this one is
        // handed out -- and through `&mut Catalog` (`mark`, `removed`, `drop_all`). While the
        // catalog is borrowed shared, `fresh` finds nothing to do and the index stands still.
        let state = unsafe { &*self.0.as_ptr() };
        state.built.as_ref().expect("fresh builds it")
    }
}

const BY: [By; LOOKUPS] = [By::Parent, By::Section, By::Part, By::File, By::ClipParent, By::Directory, By::Release, By::Upload];

/// A row as a pass over the library holds it: its hot fields, and the row read only for
/// what they do not hold (`hotrow::Lazy`).
pub type RowRef<'a> = Lazy<'a, 'a>;

/// The catalog's hot rows, read in row order (`Catalog::hot`).
#[derive(Clone, Copy)]
pub struct Hot<'a> {
    positions: &'a Positions,
    rows: &'a Rows,
}

/// What a row that cannot be read is read as.
static UNREAD: Value = Value::Null;

impl<'a> Hot<'a> {
    pub fn rows(&self) -> impl Iterator<Item = &'a HotRow> {
        self.positions.hot.iter().take(self.rows.len())
    }

    /// The whole row a hot row stands for.
    pub fn row(&self, hot: &HotRow) -> &'a Value {
        self.rows.get(hot.position).unwrap_or(&UNREAD)
    }

    /// Every row, read through its hot fields first (`hotrow::Lazy`).
    pub fn lazy(&self) -> impl Iterator<Item = Lazy<'a, 'a>> {
        let rows = self.rows;
        self.rows().map(move |hot| Lazy::new(hot, rows))
    }

    /// The row at a position, read through its hot fields first.
    pub fn at(&self, position: usize) -> Option<Lazy<'a, 'a>> {
        (position < self.rows.len()).then(|| Lazy::new(&self.positions.hot[position], self.rows))
    }

    /// Every row's search entry, in row order (its index is the row's position), and the tag
    /// names their ids name.
    pub fn corpus(&self) -> (&'a [CorpusRow], &'a Names) {
        let rows = &self.positions.corpus;
        (&rows[..rows.len().min(self.rows.len())], &self.positions.names)
    }

    /// The id of a tag name in the rows' search entries (`CorpusRow::tags`).
    pub fn raw_tag(&self, name: &str) -> Option<u32> {
        self.positions.names.raw(name)
    }

    /// The id of a lowercased tag name in the rows' search entries (`CorpusRow::related`).
    pub fn lower_tag(&self, name: &str) -> Option<u32> {
        self.positions.names.lower(name)
    }

    /// The hot rows filed under `key` in the `by` lookup, in row order.
    pub fn by(&self, by: By, key: &str) -> impl Iterator<Item = &'a HotRow> {
        let positions = self.positions;
        positions.under(by, key).iter().map(move |position| &positions.hot[*position as usize])
    }

    /// `by`, each read through its hot fields first.
    pub fn lazy_by(&self, by: By, key: &str) -> impl Iterator<Item = Lazy<'a, 'a>> {
        let rows = self.rows;
        self.by(by, key).map(move |hot| Lazy::new(hot, rows))
    }
}

/// The rows changed since the last `take_changes`: by key, or all of them.
#[derive(Debug, Clone, Default)]
pub struct Changes {
    pub all: bool,
    pub keys: HashSet<String>,
}

impl Catalog {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let Value::Object(mut document) = value else {
            return Err("catalog must be a JSON object".to_string());
        };
        // Taken out of the document, which keeps the keys in place (`to_value` writes them
        // back): copying them held every row twice for the life of the server.
        let mut take_array = |name: &str| match document.get_mut(name).map(Value::take) {
            Some(Value::Array(rows)) => rows,
            _ => Vec::new(),
        };
        let sections = take_array("sections");
        let items = take_array("items");
        let mut catalog = Self {
            document,
            sections,
            rows: Rows::new(items),
            by_id: HashMap::new(),
            changes: Changes { all: true, keys: HashSet::new() },
            touched: Changes { all: true, keys: HashSet::new() },
            writes: Changes { all: true, keys: HashSet::new() },
            index: RowIndex::default(),
            unpacked: Changes { all: true, keys: HashSet::new() },
            generation: next_generation(),
        };
        catalog.rebuild_index();
        Ok(catalog)
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self, String> {
        let text =
            fs::read_to_string(path.as_ref()).map_err(|e| format!("cannot read catalog: {e}"))?;
        let value =
            serde_json::from_str(&text).map_err(|e| format!("cannot parse catalog: {e}"))?;
        Self::from_value(value)
    }

    pub fn to_value(&self) -> Value {
        let mut document = self.document.clone();
        document.insert("sections".to_string(), Value::Array(self.sections.clone()));
        let mut all = Vec::with_capacity(self.rows.len());
        self.rows.each(|_, row| all.push(row.clone()));
        let mut items = Value::Array(all);
        unpack_streams(&mut items);
        document.insert("items".to_string(), items);
        Value::Object(document)
    }

    pub fn rebuild_index(&mut self) {
        self.index.drop_all();
        self.rekey();
    }

    /// `by_id` again, from every row's hot `ratingKey`.
    fn rekey(&mut self) {
        self.generation = next_generation();
        self.by_id.clear();
        let hot = Hot { positions: self.index.positions(&self.rows), rows: &self.rows };
        for row in hot.rows() {
            if let Some(key) = key_value_of(row.get("ratingKey")) {
                self.by_id.insert(key, row.position);
            }
        }
    }

    /// `by_id` after the rows at `gone` (ascending) were removed: the rows after them moved down.
    fn rekey_removed(&mut self, gone: &[usize]) {
        self.generation = next_generation();
        self.by_id.retain(|_, position| gone.binary_search(position).is_err());
        for position in self.by_id.values_mut() {
            *position -= gone.partition_point(|&at| at < *position);
        }
    }

    pub fn get(&self, rating_key: &str) -> Option<&Value> {
        self.by_id
            .get(rating_key)
            .and_then(|index| self.rows.get(*index))
    }
    pub fn get_mut(&mut self, rating_key: &str) -> Option<&mut Value> {
        self.generation = next_generation();
        let index = *self.by_id.get(rating_key)?;
        if !self.changes.all {
            self.changes.keys.insert(rating_key.to_string());
        }
        if !self.touched.all {
            self.touched.keys.insert(rating_key.to_string());
        }
        if !self.writes.all {
            self.writes.keys.insert(rating_key.to_string());
        }
        if !self.unpacked.all {
            self.unpacked.keys.insert(rating_key.to_string());
        }
        self.index.mark(index);
        self.rows.get_mut(index)
    }

    /// A value that changes whenever a row may have, never repeated across catalogs.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Every row, while all are in memory: before `attach`, or with no state file.
    pub fn items(&self) -> &Vec<Value> {
        self.rows.all()
    }

    /// Keep the rows in the state database at `path` (`rows.rs`): `clean` when it holds every
    /// row as it is now (a start that restored them unchanged).
    pub fn attach(&mut self, path: &Path, key_of: impl Fn(&Value) -> Option<String>, clean: bool) {
        self.rows.attach(path, key_of, clean);
    }

    /// The state database the rows are kept in.
    pub fn attached(&self) -> Option<&Path> {
        self.rows.attached()
    }

    /// A save took the changes and queued them as `serial` (`None`: there was nothing to
    /// write): the rows it took are let go of once the file has it (`pack_pending`).
    pub fn saved(&mut self, serial: Option<u64>, key_of: impl Fn(&Value) -> Option<String>) {
        self.rows.saved(serial, key_of);
    }

    /// Call `visit` with every row in order, reading those not in memory one at a time.
    pub fn each_row(&self, visit: impl FnMut(usize, &Value)) {
        self.rows.each(visit);
    }

    /// Every row, mutably: a save then looks at them all.
    pub fn items_mut(&mut self) -> &mut Vec<Value> {
        self.generation = next_generation();
        self.changes.all = true;
        self.touched.all = true;
        self.writes.all = true;
        self.unpacked.all = true;
        self.index.drop_all();
        self.rows.all_mut()
    }

    /// One row, mutably, marking nothing: the same contract as `items_unmarked_mut`.
    pub fn get_unmarked_mut(&mut self, rating_key: &str) -> Option<&mut Value> {
        self.generation = next_generation();
        let index = *self.by_id.get(rating_key)?;
        self.rows.get_unmarked_mut(index)
    }

    /// Pack the streams of the rows written since the last call. Packing changes no row's
    /// content as anything outside this file sees it, so it marks nothing to save.
    pub fn pack_pending(&mut self) -> usize {
        let pending = std::mem::take(&mut self.unpacked);
        let mut packed = 0;
        // A row read from the file is packed and fitted as it is read (`rows.rs`).
        let positions: Vec<usize> = if pending.all {
            (0..self.rows.len()).filter(|position| self.rows.held(*position)).collect()
        } else {
            pending.keys.iter().filter_map(|key| self.by_id.get(key).copied()).collect()
        };
        for position in positions {
            if let Some(row) = self.rows.get_unmarked_mut(position) {
                packed += usize::from(pack_streams(row));
                compact(row);
            }
        }
        self.rows.trim();
        // Runs after every request; only a row it packed is a change.
        if packed > 0 {
            self.generation = next_generation();
        }
        packed
    }

    /// Whether no row changed since the last `take_changes`.
    pub fn unchanged(&self) -> bool {
        !self.changes.all && self.changes.keys.is_empty()
    }

    pub fn take_changes(&mut self) -> Changes {
        std::mem::take(&mut self.changes)
    }

    pub fn take_touched(&mut self) -> Changes {
        std::mem::take(&mut self.touched)
    }

    pub fn take_writes(&mut self) -> Changes {
        std::mem::take(&mut self.writes)
    }

    pub fn children(&self, parent_key: &str) -> Vec<Value> {
        self.children_refs(parent_key).into_iter().cloned().collect()
    }

    /// `children` without the copies, for callers that only read or count.
    pub fn children_refs(&self, parent_key: &str) -> Vec<&Value> {
        self.rows_by(By::Parent, parent_key)
    }

    /// A section's movies and shows, in row order.
    pub fn section_refs(&self, section_key: &str) -> Vec<&Value> {
        self.rows_by(By::Section, section_key)
    }

    /// Every row's hot fields (`hotrow.rs`), for a pass over the library that needs no more.
    pub fn hot(&self) -> Hot<'_> {
        Hot { positions: self.index.positions(&self.rows), rows: &self.rows }
    }

    /// The rows `keep` keeps, in row order, each tested through its hot fields first: a pass
    /// over the library that reads the rows it answers and nothing of the others.
    pub fn rows_where(&self, mut keep: impl FnMut(&Lazy<'_, '_>) -> bool) -> Vec<&Value> {
        let positions: Vec<usize> = self.hot().lazy().filter(|row| keep(row)).map(|row| row.hot.position).collect();
        positions.into_iter().filter_map(|position| self.rows.get(position)).collect()
    }

    /// The first row `keep` keeps (`rows_where`).
    pub fn find_where(&self, mut keep: impl FnMut(&Lazy<'_, '_>) -> bool) -> Option<&Value> {
        let position = self.hot().lazy().find(|row| keep(row)).map(|row| row.hot.position)?;
        self.rows.get(position)
    }

    /// The rows filed under `key` in the `by` lookup, in row order, read through their hot
    /// fields first.
    pub fn lazy_by(&self, by: By, key: &str) -> Vec<RowRef<'_>> {
        self.hot().lazy_by(by, key).collect()
    }

    /// The rows `keep` keeps, in row order, as `RowRef`s (`rows_where`).
    pub fn lazy_where(&self, mut keep: impl FnMut(&RowRef<'_>) -> bool) -> Vec<RowRef<'_>> {
        self.hot().lazy().filter(|row| keep(row)).collect()
    }

    /// The row with `rating_key` as a `RowRef`.
    pub fn lazy_get(&self, rating_key: &str) -> Option<RowRef<'_>> {
        self.lazy_at(*self.by_id.get(rating_key)?)
    }

    /// The row at a position as a `RowRef`.
    pub fn lazy_at(&self, position: usize) -> Option<RowRef<'_>> {
        self.hot().at(position)
    }

    /// The rows filed under `key` in the `by` lookup, in row order.
    pub fn rows_by(&self, by: By, key: &str) -> Vec<&Value> {
        self.index.read(&self.rows, by, key).into_iter().filter_map(|position| self.rows.get(position)).collect()
    }

    /// Append a row. A row with a key is recorded by its key, so a save writes that one
    /// row (`store.rs` appends it) rather than looking at every row: a scan adds rows to a
    /// live library one release at a time.
    pub fn add(&mut self, item: Value) {
        self.generation = next_generation();
        let key = key_value(&item);
        self.rows.push(item);
        self.index.mark(self.rows.len() - 1);
        match key {
            Some(key) => {
                self.by_id.insert(key.clone(), self.rows.len() - 1);
                if !self.changes.all {
                    self.changes.keys.insert(key.clone());
                }
                if !self.unpacked.all {
                    self.unpacked.keys.insert(key.clone());
                }
                if !self.writes.all {
                    self.writes.keys.insert(key.clone());
                }
                if !self.touched.all {
                    self.touched.keys.insert(key);
                }
            }
            None => {
                self.changes.all = true;
                self.touched.all = true;
                self.unpacked.all = true;
            }
        }
    }
    /// Remove a row; the others keep their order, so a save deletes the one row.
    pub fn remove(&mut self, rating_key: &str) -> Option<Value> {
        self.generation = next_generation();
        let index = *self.by_id.get(rating_key)?;
        if !self.changes.all {
            self.changes.keys.insert(rating_key.to_string());
        }
        let removed = self.rows.remove(index)?;
        self.index.removed(&[index]);
        self.rekey_removed(&[index]);
        Some(removed)
    }

    /// Remove several rows with one reindex: `remove` reindexes every row each time.
    pub fn remove_many(&mut self, rating_keys: &HashSet<String>) {
        self.generation = next_generation();
        if rating_keys.is_empty() {
            return;
        }
        if !self.changes.all {
            self.changes.keys.extend(rating_keys.iter().cloned());
        }
        let gone: Vec<usize> = self
            .hot()
            .rows()
            .filter(|row| key_value_of(row.get("ratingKey")).is_some_and(|key| rating_keys.contains(&key)))
            .map(|row| row.position)
            .collect();
        self.rows.remove_all(&gone);
        self.index.removed(&gone);
        self.rekey_removed(&gone);
    }

    /// The rows with `keys` for a listing to project, in order: read in parallel and kept
    /// nowhere, without their Parts' `Stream` lists (`Rows::read_listed`); `None` for a key
    /// with no row.
    pub fn read_listed(&self, keys: &[String]) -> Vec<Option<std::borrow::Cow<'_, Value>>> {
        let threads = crate::rows::listing_threads();
        let positions: Vec<Option<usize>> = keys.iter().map(|key| self.by_id.get(key).copied()).collect();
        let found: Vec<usize> = positions.iter().flatten().copied().collect();
        let mut read = self.rows.read_listed(&found, threads).into_iter();
        positions.into_iter().map(|position| position.and_then(|_| read.next().flatten())).collect()
    }

    /// Show another account's view fields in place of the owner's until `end_view_overlay`
    /// (`rows::Overlay`): on the rows `hide` names (those holding the owner's), and `show`'s
    /// fields by row. A row is changed only once it is written to.
    pub fn overlay_views(&mut self, fields: &'static [&'static str], hide: &HashSet<String>, show: &HashMap<String, Value>) {
        self.generation = next_generation();
        let hide = hide.iter().filter_map(|key| self.by_id.get(key).copied()).collect();
        let show = show
            .iter()
            .filter_map(|(key, fields)| Some((*self.by_id.get(key)?, fields.as_object()?.clone())))
            .collect();
        self.rows.overlay(fields, hide, show);
    }

    /// End the overlay: the rows changed in place, each with the owner's fields it took off,
    /// by key; `refile` each once it is put back.
    pub fn end_view_overlay(&mut self) -> HashMap<String, Vec<(usize, String, Value)>> {
        self.generation = next_generation();
        let swapped = self.rows.end_overlay();
        let hot = self.hot();
        swapped
            .into_iter()
            .filter_map(|(position, taken)| Some((key_value_of(hot.at(position)?.hot.get("ratingKey"))?, taken)))
            .collect()
    }

    /// Read a row's hot fields and keys again (`end_view_overlay`'s rows, once put back).
    pub fn refile(&mut self, rating_key: &str) {
        if let Some(position) = self.by_id.get(rating_key) {
            self.index.mark(*position);
        }
    }

    /// Changes whenever a row may differ from what a read of the file returned before:
    /// with every write (`generation`) and whenever rows held in memory are let go of.
    pub fn listing_epoch(&self) -> (u64, u64) {
        (self.generation, self.rows.evictions())
    }

    /// For a listing that read the row `rating_key` from the file itself (`rows::prefetch_listed`):
    /// the row as it is in memory when it is held, `Some(None)` when the file's copy is the row,
    /// `None` when there is no such row any more.
    pub fn held_row(&self, rating_key: &str) -> Option<Option<&Value>> {
        let position = *self.by_id.get(rating_key)?;
        Some(if self.rows.held(position) { self.rows.get(position) } else { None })
    }

    /// The row at a position (`position`, the hot rows' `position`).
    pub fn row_at(&self, position: usize) -> Option<&Value> {
        self.rows.get(position)
    }

    /// How many rows there are.
    pub fn len(&self) -> usize {
        self.rows.len()
    }

    /// Where a row sits in `items`, which is the order a save stores rows in.
    pub fn position(&self, rating_key: &str) -> Option<usize> {
        self.by_id.get(rating_key).copied()
    }

    /// Apply load-time corrections to row hierarchy, media fields, and stream
    /// projections. This is deliberately explicit: callers can load raw rows
    /// for diagnostics without silently changing the evidence document.
    pub fn normalize(&mut self) {
        self.generation = next_generation();
        self.changes.all = true;
        for item in self.rows.all_mut().iter_mut() {
            if let Some(media) = item.get_mut("Media").and_then(Value::as_array_mut) {
                for media in media.iter_mut() {
                    mediainfo::normalize_media(media);
                }
            }
        }
        hierarchy::normalize_rows(self.rows.all_mut());
        self.rebuild_index();
    }

    /// `server.py` `_retitle_streams`: every Stream's display titles recomputed from
    /// the row's own fields, so a catalog built before a title rule was measured is
    /// corrected rather than needing a rebuild. A server-load step, not part of
    /// `normalize`: Python runs it in `Catalog.__init__` after `normalize_media` and
    /// `normalize_rows`, and the `catalog-normalize` parity command mirrors only those
    /// two, which leave the stored titles alone.
    pub fn retitle_streams(&mut self) {
        self.generation = next_generation();
        self.changes.all = true;
        for item in self.rows.all_mut().iter_mut() {
            for media in item
                .get_mut("Media")
                .and_then(Value::as_array_mut)
                .into_iter()
                .flatten()
            {
                for part in media
                    .get_mut("Part")
                    .and_then(Value::as_array_mut)
                    .into_iter()
                    .flatten()
                {
                    edit_part_streams(part, |streams| {
                        for stream in streams.iter_mut().filter_map(Value::as_object_mut) {
                            mediainfo::stream_titles(stream, None, None);
                        }
                    });
                }
            }
        }
    }
}

/// A Part's `Stream` rows as the server holds them: the rows' JSON text under this key, in
/// the place `Stream` had among the Part's keys. Each stream as a map of thirty-odd keys cost
/// ~3.5 KB: the production server's 40,197 rows held 1.25 GB of streams in a 2.6 GB process
/// (2026-10-16, heaptrack), interleaved with the rows every listing walks, and fun swapped it.
/// Responses (`Response::json`, `strip_private_value`) and saves (`unpacked`) see `Stream`.
pub const PACKED_STREAMS: &str = "_streams";

/// The Part's `Stream` rows, whichever way the Part holds them.
pub fn part_streams(part: &Value) -> std::borrow::Cow<'_, [Value]> {
    match part.get("Stream") {
        Some(Value::Array(rows)) => std::borrow::Cow::Borrowed(rows.as_slice()),
        _ => match part.get(PACKED_STREAMS).and_then(Value::as_str) {
            Some(text) => std::borrow::Cow::Owned(serde_json::from_str(text).unwrap_or_default()),
            None => std::borrow::Cow::Borrowed(&[]),
        },
    }
}

/// Whether the Part may hold the stream `id`: a packed Part whose text does not name it
/// cannot, so a scan of the library for one stream decodes almost nothing.
pub fn part_may_hold_stream(part: &Value, id: i64) -> bool {
    match part.get(PACKED_STREAMS).and_then(Value::as_str) {
        Some(text) if !matches!(part.get("Stream"), Some(Value::Array(_))) => text.contains(&format!("\"id\":{id}")),
        _ => true,
    }
}

/// The highest stream `id` on the Part, read off packed text without decoding it.
pub fn max_stream_id(part: &Value) -> Option<i64> {
    match (part.get("Stream"), part.get(PACKED_STREAMS).and_then(Value::as_str)) {
        (Some(Value::Array(rows)), _) => rows.iter().filter_map(|stream| stream.get("id").and_then(Value::as_i64)).max(),
        (_, Some(text)) => text
            .split("\"id\":")
            .skip(1)
            .filter_map(|tail| {
                let digits: String = tail.chars().take_while(|ch| ch.is_ascii_digit() || *ch == '-').collect();
                digits.parse::<i64>().ok()
            })
            .max(),
        _ => None,
    }
}

/// Whether the Part has `Stream` rows at all, in either form.
pub fn has_part_streams(part: &Value) -> bool {
    matches!(part.get("Stream"), Some(Value::Array(_))) || part.get(PACKED_STREAMS).is_some_and(Value::is_string)
}

/// Edit a Part's `Stream` rows in place: `edit` gets them as an array and the Part keeps
/// whichever form it had. Answers `None` when the Part has none.
pub fn edit_part_streams<R>(part: &mut Value, edit: impl FnOnce(&mut Vec<Value>) -> R) -> Option<R> {
    let object = part.as_object_mut()?;
    if let Some(Value::Array(rows)) = object.get_mut("Stream") {
        return Some(edit(rows));
    }
    let text = object.get(PACKED_STREAMS)?.as_str()?;
    let mut rows: Vec<Value> = serde_json::from_str(text).unwrap_or_default();
    let answer = edit(&mut rows);
    object.insert(PACKED_STREAMS.to_string(), Value::String(Value::Array(rows).to_string()));
    Some(answer)
}

/// Unpack one Part's packed `Stream` rows in place, for code that edits them as an array;
/// answers whether it was packed, so `repack_part` puts it back as it was.
pub fn unpack_part(part: &mut Map<String, Value>) -> bool {
    let Some(Value::String(text)) = part.get(PACKED_STREAMS) else {
        return false;
    };
    let rows = serde_json::from_str(text).unwrap_or_else(|_| Value::Array(Vec::new()));
    rename(part, PACKED_STREAMS, "Stream", rows);
    true
}

/// Pack one Part's `Stream` rows again after `unpack_part`.
pub fn repack_part(part: &mut Map<String, Value>) {
    if let Some(Value::Array(rows)) = part.get("Stream") {
        let text = Value::String(serde_json::to_string(rows).unwrap_or_default());
        rename(part, "Stream", PACKED_STREAMS, text);
    }
}

/// Replace a key with another in its own place among the map's keys.
fn rename(object: &mut Map<String, Value>, from: &str, to: &str, value: Value) {
    match object.iter().position(|(key, _)| key == from) {
        Some(at) => {
            object.shift_remove(from);
            object.shift_insert(at, to.to_string(), value);
        }
        None => {
            object.insert(to.to_string(), value);
        }
    }
}

/// Pack every Part's `Stream` rows of a row. Answers whether any were packed.
/// Every map, list and string in `value` at exactly its size. A parsed row keeps the room
/// each grew into while it was read -- a fifth of the library's memory, 302 MB of the
/// production server's 1.61 GB (2026-10-18) -- and nothing reads capacity, so this changes
/// no answer. Rows written since the last pack are compacted with it (`pack_pending`).
pub fn compact(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.values_mut().for_each(compact);
            // `Map` has no `shrink_to_fit`; collecting allocates exactly its entries.
            let entries = std::mem::take(object);
            *object = entries.into_iter().collect();
        }
        Value::Array(values) => {
            values.iter_mut().for_each(compact);
            values.shrink_to_fit();
        }
        Value::String(text) => text.shrink_to_fit(),
        _ => {}
    }
}

pub fn pack_streams(row: &mut Value) -> bool {
    let mut packed = false;
    for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
        for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
            let Some(object) = part.as_object_mut() else {
                continue;
            };
            if let Some(Value::Array(rows)) = object.get("Stream") {
                let text = Value::String(serde_json::to_string(rows).unwrap_or_default());
                rename(object, "Stream", PACKED_STREAMS, text);
                packed = true;
            }
        }
    }
    packed
}

/// `value` with every packed `Stream` written out again, wherever in it a Part sits.
pub fn unpack_streams(value: &mut Value) {
    match value {
        Value::Object(object) => {
            if let Some(Value::String(text)) = object.get(PACKED_STREAMS) {
                let rows = serde_json::from_str(text).unwrap_or_else(|_| Value::Array(Vec::new()));
                rename(object, PACKED_STREAMS, "Stream", rows);
            }
            for (_, child) in object.iter_mut() {
                if matches!(child, Value::Object(_) | Value::Array(_)) {
                    unpack_streams(child);
                }
            }
        }
        Value::Array(values) => values.iter_mut().for_each(unpack_streams),
        _ => {}
    }
}

/// Whether anything in `value` is packed: a response without Parts is not copied to unpack.
pub fn holds_packed_streams(value: &Value) -> bool {
    match value {
        Value::Object(object) => {
            object.contains_key(PACKED_STREAMS) || object.values().any(|child| matches!(child, Value::Object(_) | Value::Array(_)) && holds_packed_streams(child))
        }
        Value::Array(values) => values.iter().any(holds_packed_streams),
        _ => false,
    }
}

/// `value` as it is written out: packed `Stream` rows unpacked, nothing copied when none are.
pub fn unpacked(value: &Value) -> std::borrow::Cow<'_, Value> {
    if holds_packed_streams(value) {
        let mut copy = value.clone();
        unpack_streams(&mut copy);
        std::borrow::Cow::Owned(copy)
    } else {
        std::borrow::Cow::Borrowed(value)
    }
}

/// A row's releases as (source, source id, Media index): each Media's own tags on a row
/// that holds several, else the row's.
pub fn versions(row: &Value) -> Vec<(String, String, usize)> {
    let tag = |value: &Value, field: &str| value.get(field).and_then(Value::as_str).map(str::to_string);
    let tagged: Vec<_> = row
        .get("Media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .enumerate()
        .filter_map(|(index, media)| Some((tag(media, "_source")?, tag(media, "_sid")?, index)))
        .collect();
    if !tagged.is_empty() {
        return tagged;
    }
    match (tag(row, "_source"), tag(row, "_sid")) {
        (Some(source), Some(sid)) => vec![(source, sid, 0)],
        _ => Vec::new(),
    }
}

fn key_value(item: &Value) -> Option<String> {
    key_value_of(item.get("ratingKey"))
}

fn key_value_of(key: Option<&Value>) -> Option<String> {
    key.and_then(|v| match v {
        Value::String(v) => Some(v.clone()),
        Value::Number(v) => Some(v.to_string()),
        _ => None,
    })
}

impl TryFrom<Value> for Catalog {
    type Error = String;
    fn try_from(value: Value) -> Result<Self, Self::Error> {
        Self::from_value(value)
    }
}

#[cfg(test)]
mod tests {
    /// What a show listing asks for every show and every season (`leafCount`,
    /// `childCount`), on the production server's library as it stood on 2026-10-10: 361
    /// shows, 698 seasons, 13,509 episodes and 1,565 movies.
    #[test]
    fn children_of_every_show_and_season_does_not_rescan_the_library_each_time() {
        let shape: Vec<Vec<usize>> =
            serde_json::from_str(include_str!("../tests/fixtures/fun_show_shape.json")).unwrap();
        let mut items = Vec::new();
        let mut next = 100_000u64;
        let mut key = || {
            next += 1;
            next.to_string()
        };
        for _ in 0..1565 {
            items.push(serde_json::json!({"ratingKey": key(), "type": "movie"}));
        }
        let mut parents = Vec::new();
        for seasons in &shape {
            let show = key();
            items.push(serde_json::json!({"ratingKey": show, "type": "show"}));
            parents.push((show.clone(), seasons.len()));
            for (index, episodes) in seasons.iter().enumerate() {
                let season = key();
                items.push(serde_json::json!({"ratingKey": season, "type": "season",
                                              "parentRatingKey": show, "index": index + 1}));
                parents.push((season.clone(), *episodes));
                for episode in 0..*episodes {
                    // Numeric parents, as rows a feed placed sometimes carry.
                    items.push(serde_json::json!({"ratingKey": key(), "type": "episode",
                                                  "parentRatingKey": season.parse::<u64>().unwrap(),
                                                  "index": episode + 1}));
                }
            }
        }
        let catalog = Catalog::from_value(serde_json::json!({"items": items})).unwrap();
        let started = std::time::Instant::now();
        for (parent, count) in &parents {
            assert_eq!(catalog.children(parent).len(), *count, "children of {parent}");
        }
        let elapsed = started.elapsed();
        assert!(elapsed < std::time::Duration::from_millis(500), "1,059 lookups took {elapsed:?}");
    }

    #[test]
    fn children_follow_a_row_added_or_reparented_after_a_lookup() {
        let mut catalog = Catalog::from_value(serde_json::json!({"items": [
            {"ratingKey": "1", "type": "season"},
            {"ratingKey": "2", "type": "episode", "parentRatingKey": "1"}]})).unwrap();
        assert_eq!(catalog.children("1").len(), 1);
        catalog.add(serde_json::json!({"ratingKey": "3", "type": "episode", "parentRatingKey": "1"}));
        assert_eq!(catalog.children("1").len(), 2);
        catalog.get_mut("2").unwrap()["parentRatingKey"] = serde_json::json!("9");
        assert_eq!(catalog.children("1").len(), 1);
        assert_eq!(catalog.children("9").len(), 1);
        catalog.remove("3");
        assert!(catalog.children("1").is_empty());
    }

    #[test]
    fn a_section_lists_its_movies_and_shows_through_adds_moves_and_removes() {
        let mut catalog = Catalog::from_value(serde_json::json!({"items": [
            {"ratingKey": "1", "type": "movie", "librarySectionID": 1},
            {"ratingKey": "2", "type": "show", "librarySectionID": "2"},
            {"ratingKey": "3", "type": "season", "librarySectionID": 2, "parentRatingKey": "2"},
            {"ratingKey": "4", "type": "movie", "librarySectionID": "1"}]})).unwrap();
        let keys = |catalog: &Catalog, section: &str| -> Vec<String> {
            catalog.section_refs(section).iter().filter_map(|row| key_value(row)).collect()
        };
        assert_eq!(keys(&catalog, "1"), ["1", "4"]);
        assert_eq!(keys(&catalog, "2"), ["2"]);
        catalog.add(serde_json::json!({"ratingKey": "5", "type": "movie", "librarySectionID": 1}));
        assert_eq!(keys(&catalog, "1"), ["1", "4", "5"]);
        // A film moved among the shows keeps its place in row order there.
        catalog.get_mut("1").unwrap()["librarySectionID"] = serde_json::json!(2);
        assert_eq!(keys(&catalog, "1"), ["4", "5"]);
        assert_eq!(keys(&catalog, "2"), ["1", "2"]);
        catalog.get_mut("4").unwrap()["type"] = serde_json::json!("episode");
        assert_eq!(keys(&catalog, "1"), ["5"]);
        catalog.remove("2");
        assert_eq!(keys(&catalog, "2"), ["1"]);
    }

    #[test]
    fn packed_streams_read_edit_and_write_out_as_they_were() {
        let original = serde_json::json!({"ratingKey": "1", "Media": [{"id": 1, "Part": [
            {"id": 7, "key": "/library/parts/7", "Stream": [
                {"id": 11, "streamType": 1, "codec": "hevc", "bitrate": 2.5},
                {"id": 12, "streamType": 2, "language": "English", "selected": true},
                {"id": 113, "streamType": 3, "displayTitle": "\"Forced\" \u{e9}"}], "size": 9},
            {"id": 8, "file": "b.mkv"}]}]});
        let mut row = original.clone();
        assert!(pack_streams(&mut row));
        let part = &row["Media"][0]["Part"][0];
        assert!(part.get("Stream").is_none() && part[PACKED_STREAMS].is_string());
        // In the place `Stream` had, so writing it out restores the same text.
        let keys: Vec<&String> = part.as_object().unwrap().keys().collect();
        assert_eq!(keys, ["id", "key", PACKED_STREAMS, "size"]);
        assert_eq!(part_streams(part).len(), 3);
        assert!(has_part_streams(part) && !has_part_streams(&row["Media"][0]["Part"][1]));
        assert_eq!(max_stream_id(part), Some(113));
        assert!(part_may_hold_stream(part, 12) && !part_may_hold_stream(part, 13));
        assert_eq!(unpacked(&row).to_string(), original.to_string());
        let part = &mut row["Media"][0]["Part"][0];
        edit_part_streams(part, |streams| streams[1]["selected"] = serde_json::json!(false));
        assert!(part[PACKED_STREAMS].is_string());
        assert_eq!(part_streams(part)[1]["selected"], false);
        assert!(unpack_part(part.as_object_mut().unwrap()));
        assert_eq!(part["Stream"][1]["selected"], false);
        repack_part(part.as_object_mut().unwrap());
        assert!(part.get("Stream").is_none());
        assert!(!pack_streams(&mut row), "already packed");
    }

    /// A progress beat writes one row in place; the listings after it read the index. On
    /// the production server (2026-10-16) every write dropped the index, so each listing after
    /// a beat scanned all 40,197 rows again: 2,000 alternations took 4 s here, unfixed.
    #[test]
    #[ignore = "wall-clock throughput check; run explicitly without competing build/transcode work"]
    fn a_row_written_in_place_does_not_rescan_the_library() {
        let mut items = Vec::new();
        for show in 0..400u64 {
            let show_key = (100_000 + show * 100).to_string();
            items.push(serde_json::json!({"ratingKey": show_key, "type": "show", "librarySectionID": 2}));
            for episode in 1..100u64 {
                items.push(serde_json::json!({"ratingKey": (100_000 + show * 100 + episode).to_string(),
                                              "type": "episode", "parentRatingKey": show_key}));
            }
        }
        let mut catalog = Catalog::from_value(serde_json::json!({"items": items})).unwrap();
        assert_eq!(catalog.children("100000").len(), 99);
        let started = std::time::Instant::now();
        for beat in 0..2000u64 {
            let key = (100_000 + (beat % 400) * 100 + 1 + beat % 99).to_string();
            catalog.get_mut(&key).unwrap()["viewOffset"] = serde_json::json!(beat * 1000);
            assert_eq!(catalog.children(&(100_000 + (beat % 400) * 100).to_string()).len(), 99);
            assert_eq!(catalog.section_refs("2").len(), 400);
        }
        let elapsed = started.elapsed();
        assert!(elapsed < std::time::Duration::from_millis(500), "2,000 beats took {elapsed:?}");
    }

    /// A removal shifts the index instead of rebuilding it; whatever it answers afterwards is
    /// what an index built from the remaining rows answers.
    #[test]
    fn an_index_shifted_by_removals_answers_as_one_built_afresh() {
        let mut items = Vec::new();
        for show in 0..30u64 {
            let show_key = (1000 + show * 100).to_string();
            items.push(serde_json::json!({"ratingKey": show_key, "type": "show", "librarySectionID": 2}));
            for episode in 1..8u64 {
                let key = 1000 + show * 100 + episode;
                items.push(serde_json::json!({"ratingKey": key.to_string(), "type": "episode",
                    "parentRatingKey": show_key, "viewCount": episode % 2,
                    "Media": [{"_source": "zurg", "_sid": format!("s{key}"), "Part": [{"id": key, "file": format!("/f/{key}.mkv")}]}]}));
            }
        }
        let mut catalog = Catalog::from_value(serde_json::json!({"items": items})).unwrap();
        let answers = |catalog: &Catalog| {
            let mut out = Vec::new();
            for key in 1000..4000u64 {
                let key = key.to_string();
                for by in BY {
                    let lookup = match by {
                        By::File => format!("/f/{key}.mkv"),
                        By::Release => release_key("zurg", &format!("s{key}")),
                        _ => key.clone(),
                    };
                    let rows: Vec<Option<String>> = catalog.rows_by(by, &lookup).into_iter().map(key_value).collect();
                    if !rows.is_empty() {
                        out.push((format!("{by:?}"), lookup, rows));
                    }
                }
            }
            let hot = catalog.hot();
            let rows: Vec<(usize, Option<Value>)> = hot.rows().map(|row| (row.position, row.get("ratingKey").cloned())).collect();
            (out, rows)
        };
        answers(&catalog);
        catalog.remove("1203");
        catalog.remove_many(&HashSet::from(["1000".to_string(), "1001".to_string(), "2907".to_string(), "1500".to_string()]));
        catalog.get_mut("1105").unwrap()["parentRatingKey"] = serde_json::json!("1200");
        catalog.add(serde_json::json!({"ratingKey": "3999", "type": "episode", "parentRatingKey": "1200"}));
        let fresh = Catalog::from_value(serde_json::json!({"items": catalog.items().clone()})).unwrap();
        assert_eq!(answers(&catalog), answers(&fresh));
    }

    use super::*;
    #[test]
    fn preserves_unknown_document_properties() {
        let catalog = Catalog::from_value(
            serde_json::json!({"custom":{"x":1},"sections":[],"items":[{"ratingKey":"1"}]}),
        )
        .unwrap();
        assert_eq!(catalog.to_value()["custom"]["x"], 1);
        assert_eq!(catalog.get("1").unwrap()["ratingKey"], "1");
    }

    #[test]
    fn add_and_remove_record_only_their_own_key() {
        let mut catalog = Catalog::from_value(serde_json::json!({"sections":[],"items":[
            {"ratingKey":"1"},{"ratingKey":"2"},{"ratingKey":"3"}]}))
        .unwrap();
        catalog.take_changes();
        catalog.add(serde_json::json!({"ratingKey":"9"}));
        catalog.remove("2");
        let changes = catalog.take_changes();
        assert!(!changes.all);
        assert_eq!(changes.keys, HashSet::from(["9".to_string(), "2".to_string()]));
        assert_eq!(catalog.position("9"), Some(2));
        assert_eq!(catalog.get("3").unwrap()["ratingKey"], "3");
        catalog.add(serde_json::json!({"title":"keyless"}));
        assert!(catalog.take_changes().all, "a row without a key is found by a full pass");
    }
}
