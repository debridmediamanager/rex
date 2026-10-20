//! The fields of a row that passes over the whole library read, kept apart from the row.
//!
//! A row as the server holds it is a JSON map of a hundred-odd keys, its cast, its Media and
//! their streams: 1.4 GB for the production server's 42,806 rows (2026-10-18), where Plex keeps
//! 21 MB and reads rows from its database. A pass over every row -- a section's hubs, Continue
//! Watching, a feed push placing its arrivals -- reads a few dozen of those keys. `HotRow` is
//! those keys alone, numbered instead of named, which is what lets the rows themselves live on
//! disk (`catalog.rs`).
//!
//! A hot row answers `get` for its fields exactly as the row does, so a pass reads it as it
//! read the row. Asking it for any other field panics: a pass that needs one reads the row.

use serde_json::Value;

/// Every field a hot row keeps, sorted (`get` searches it).
pub const FIELDS: &[&str] = &[
    "_librarySectionID",
    "_parentRatingKey",
    "_poster_url",
    "_prefs",
    "_probePending",
    "_release",
    "_retry",
    "_scanned",
    "_sid",
    "_viewUpdatedAt",
    "addedAt",
    "audienceRating",
    "childCount",
    "contentRating",
    "deletedAt",
    "duration",
    "editionTitle",
    "extraType",
    "grandparentRatingKey",
    "grandparentTitle",
    "guid",
    "index",
    "lastRatedAt",
    "lastSkippedAt",
    "lastViewedAt",
    "leafCount",
    "librarySectionID",
    "originalTitle",
    "originallyAvailableAt",
    "parentIndex",
    "parentRatingKey",
    "parentTitle",
    "rating",
    "ratingKey",
    "skipCount",
    "studio",
    "title",
    "titleSort",
    "type",
    "updatedAt",
    "userRating",
    "viewCount",
    "viewOffset",
    "viewedLeafCount",
    "year",
];

fn field(name: &str) -> Option<u8> {
    FIELDS.binary_search(&name).ok().map(|at| at as u8)
}

/// A row's hot fields, and where the row sits.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HotRow {
    /// The row's position among the catalog's rows.
    pub position: usize,
    /// Which fields the row holds, ascending, and their values: apart, as a pair padded each
    /// value by seven bytes.
    ids: Box<[u8]>,
    values: Box<[Value]>,
    /// The row's releases (`catalog::versions`): a source's listing and a snapshot's end walk
    /// every row for them, and on the production server reading each row for its Media took
    /// 26 s for one source's 40,335 rows, holding every Plex request (2026-10-18).
    pub releases: Box<[Release]>,
}

/// A release on a row: its source, its id there, its Media and the file that Media reads.
#[derive(Debug, Clone, PartialEq)]
pub struct Release {
    pub source: Box<str>,
    pub sid: Box<str>,
    pub index: usize,
    /// The file, or what comes before `sid` in it when it ends with `sid`: 56% of the production
    /// server's files are their mount and their id, so half the bytes are kept once.
    file: Option<(Box<str>, bool)>,
}

impl Release {
    pub fn file(&self) -> Option<String> {
        self.file.as_ref().map(|(file, before_sid)| if *before_sid { format!("{file}{}", self.sid) } else { file.to_string() })
    }
}

impl HotRow {
    pub fn of(position: usize, row: &Value) -> Self {
        let mut ids = Vec::new();
        let mut values = Vec::new();
        if let Some(object) = row.as_object() {
            for (at, name) in FIELDS.iter().enumerate() {
                if let Some(value) = object.get(*name) {
                    ids.push(at as u8);
                    values.push(fitted(value));
                }
            }
        }
        let releases = crate::catalog::versions(row)
            .into_iter()
            .map(|(source, sid, index)| {
                let file = row.pointer(&format!("/Media/{index}/Part/0/file")).and_then(Value::as_str).map(|file| {
                    match file.strip_suffix(sid.as_str()) {
                        Some(before) => (before.into(), true),
                        None => (file.into(), false),
                    }
                });
                Release { source: source.into(), sid: sid.into(), index, file }
            })
            .collect();
        Self { position, ids: ids.into_boxed_slice(), values: values.into_boxed_slice(), releases }
    }

    /// The field as the row holds it. Panics for a field a hot row does not keep.
    pub fn get(&self, name: &str) -> Option<&Value> {
        match self.lookup(name) {
            Some(value) => value,
            None => panic!("a hot row does not keep {name}; read the row"),
        }
    }

    /// `get`, or `None` for a field a hot row does not keep.
    pub fn lookup(&self, name: &str) -> Option<Option<&Value>> {
        let id = field(name)?;
        Some(self.ids.binary_search(&id).ok().map(|at| &self.values[at]))
    }
}

/// A copy of `value` with no room to spare.
fn fitted(value: &Value) -> Value {
    let mut value = value.clone();
    crate::catalog::compact(&mut value);
    value
}

/// What a pass over the library reads a row through: the row, or its hot fields.
pub trait Row {
    fn get(&self, name: &str) -> Option<&Value>;
    /// The whole row, for what `get` cannot answer: the row as one value (a copy of it, its
    /// keys in order). A hot row alone has none and panics.
    fn whole(&self) -> &Value;
}

impl Row for Value {
    fn get(&self, name: &str) -> Option<&Value> {
        Value::get(self, name)
    }
    fn whole(&self) -> &Value {
        self
    }
}

/// A row's object, as `hierarchy` reads it.
impl Row for serde_json::Map<String, Value> {
    fn get(&self, name: &str) -> Option<&Value> {
        serde_json::Map::get(self, name)
    }
    fn whole(&self) -> &Value {
        panic!("a map is not a whole row")
    }
}

impl Row for HotRow {
    fn get(&self, name: &str) -> Option<&Value> {
        HotRow::get(self, name)
    }
    fn whole(&self) -> &Value {
        panic!("a hot row is not the whole row; read it through `Lazy`")
    }
}

impl Row for std::borrow::Cow<'_, Value> {
    fn get(&self, name: &str) -> Option<&Value> {
        Value::get(self, name)
    }
    fn whole(&self) -> &Value {
        self
    }
}

impl Row for std::sync::Arc<Value> {
    fn get(&self, name: &str) -> Option<&Value> {
        Value::get(self, name)
    }
    fn whole(&self) -> &Value {
        self
    }
}

impl<T: Row + ?Sized> Row for &T {
    fn get(&self, name: &str) -> Option<&Value> {
        T::get(self, name)
    }
    fn whole(&self) -> &Value {
        T::whole(self)
    }
}

/// A row read through its hot fields, and through the row itself for any other field: a pass
/// written against it answers as it did over the rows, and reads a row only for what the hot
/// fields do not hold.
#[derive(Clone, Copy)]
pub struct Lazy<'h, 'a> {
    pub hot: &'h HotRow,
    rows: &'a crate::rows::Rows,
}

/// What a row that cannot be read is read as.
static UNREAD: Value = Value::Null;

impl<'h, 'a: 'h> Lazy<'h, 'a> {
    pub fn new(hot: &'h HotRow, rows: &'a crate::rows::Rows) -> Self {
        Self { hot, rows }
    }

    /// The whole row, which outlives the hot rows it was read beside: read from the state
    /// file when it is not in memory (`rows.rs`).
    pub fn body(&self) -> &'a Value {
        self.rows.get(self.hot.position).unwrap_or(&UNREAD)
    }

    pub fn get(&self, name: &str) -> Option<&'h Value> {
        if let Some(value) = self.rows.view_field(self.hot.position, name) {
            return value;
        }
        match self.hot.lookup(name) {
            Some(value) => value,
            None => self.body().get(name),
        }
    }
}

/// A row a list holds: the catalog's, read through its hot fields, or one built for the answer.
#[derive(Clone)]
pub enum AnyRow<'a> {
    Ref(Lazy<'a, 'a>),
    Owned(Value),
    Borrowed(&'a Value),
}

impl AnyRow<'_> {
    /// The row as a value of its own.
    pub fn into_value(self) -> Value {
        match self {
            AnyRow::Ref(row) => row.body().clone(),
            AnyRow::Owned(row) => row,
            AnyRow::Borrowed(row) => row.clone(),
        }
    }
}

impl Row for AnyRow<'_> {
    fn get(&self, name: &str) -> Option<&Value> {
        match self {
            AnyRow::Ref(row) => Row::get(row, name),
            AnyRow::Owned(row) => row.get(name),
            AnyRow::Borrowed(row) => row.get(name),
        }
    }
    fn whole(&self) -> &Value {
        match self {
            AnyRow::Ref(row) => row.body(),
            AnyRow::Owned(row) => row,
            AnyRow::Borrowed(row) => row,
        }
    }
}

impl<'h, 'a: 'h> Row for Lazy<'h, 'a> {
    fn get(&self, name: &str) -> Option<&Value> {
        Lazy::get(self, name)
    }
    fn whole(&self) -> &Value {
        self.body()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fields_are_sorted_and_answer_as_the_row_does() {
        assert!(FIELDS.windows(2).all(|pair| pair[0] < pair[1]), "FIELDS must stay sorted");
        let row = serde_json::json!({"ratingKey": "7", "type": "episode", "viewCount": 2,
                                     "summary": "long", "librarySectionID": true, "_retry": {"at": 5}});
        let hot = HotRow::of(3, &row);
        for name in ["ratingKey", "type", "viewCount", "librarySectionID", "_retry", "viewOffset"] {
            assert_eq!(hot.get(name), row.get(name), "{name}");
        }
        assert_eq!(hot.position, 3);
    }

    #[test]
    fn releases_keep_each_media_s_source_id_and_file() {
        let row = serde_json::json!({"Media": [
            {"_source": "rd", "_sid": "movies/A/a.mkv", "Part": [{"file": "/mnt/zurg/movies/A/a.mkv"}]},
            {"_source": "ad", "_sid": "x1", "Part": [{"file": "/mnt/zurg/movies/B/b.mkv"}]},
            {"_source": "tb", "_sid": "x2"}]});
        let releases = HotRow::of(0, &row).releases;
        let got: Vec<_> = releases.iter().map(|r| (&*r.source, &*r.sid, r.index, r.file())).collect();
        assert_eq!(got, vec![("rd", "movies/A/a.mkv", 0, Some("/mnt/zurg/movies/A/a.mkv".into())),
                             ("ad", "x1", 1, Some("/mnt/zurg/movies/B/b.mkv".into())),
                             ("tb", "x2", 2, None)]);
        // A row tagged on itself, not on its Media.
        let row = serde_json::json!({"_source": "zurg", "_sid": "s", "Media": [{"Part": [{"file": "/f/s"}]}]});
        let got: Vec<_> = HotRow::of(0, &row).releases.iter().map(|r| (r.sid.to_string(), r.file())).collect();
        assert_eq!(got, vec![("s".to_string(), Some("/f/s".to_string()))]);
    }

    #[test]
    #[should_panic(expected = "a hot row does not keep summary")]
    fn a_field_it_does_not_keep_is_refused() {
        HotRow::of(0, &serde_json::json!({"summary": "x"})).get("summary");
    }
}
