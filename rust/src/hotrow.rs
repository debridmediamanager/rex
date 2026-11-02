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
//! What `get` answers is a `Field`, borrowed from wherever the field is held, read with the
//! names `Value` reads with.

use std::borrow::Cow;

use serde_json::Value;

/// Every field a hot row keeps, sorted (`get` searches it).
pub const FIELDS: &[&str] = &[
    "_collections",
    "_librarySectionID",
    "_matched",
    "_parentRatingKey",
    "_placed",
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
    /// Each field the row holds, by ascending id: the id, its kind and its value (`put`).
    bytes: Box<[u8]>,
    /// The row's releases (`catalog::versions`): a source's listing and a snapshot's end walk
    /// every row for them, and on the production server reading each row for its Media took
    /// 26 s for one source's 40,335 rows, holding every Plex request (2026-10-18).
    pub releases: Box<[Release]>,
    /// Whether this row has generated analysis records to restore or retire.
    pub has_analysis: bool,
}

/// A release on a row: its source, its id there, its Media and the file that Media reads.
#[derive(Debug, Clone, PartialEq)]
pub struct Release {
    pub source: Box<str>,
    pub sid: Box<str>,
    pub index: usize,
    /// Whether a person moved this Media onto its row by a merge (`_matched_at` on it): a
    /// source's matches listing finds it without reading the row.
    pub matched: bool,
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
        let mut bytes = Vec::new();
        if let Some(object) = row.as_object() {
            for (id, name) in FIELDS.iter().enumerate() {
                if let Some(value) = object.get(*name) {
                    put(&mut bytes, id as u8, value);
                }
            }
        }
        let releases = crate::catalog::releases(row)
            .into_iter()
            .map(|(source, sid, index, part)| {
                let file = row.pointer(&format!("/Media/{index}/Part/{part}/file")).and_then(Value::as_str).map(|file| {
                    match file.strip_suffix(sid.as_str()) {
                        Some(before) => (before.into(), true),
                        None => (file.into(), false),
                    }
                });
                let matched = row.pointer(&format!("/Media/{index}/_matched_at")).is_some();
                Release { source: source.into(), sid: sid.into(), index, matched, file }
            })
            .collect();
        let has_analysis = row.get("Media").and_then(Value::as_array).into_iter().flatten()
            .any(|media| media.get("_analysis").and_then(Value::as_object).is_some_and(|records| !records.is_empty()));
        Self { position, bytes: bytes.into_boxed_slice(), releases, has_analysis }
    }

    /// The field as the row holds it. Panics for a field a hot row does not keep.
    pub fn get(&self, name: &str) -> Option<Field<'_>> {
        match self.lookup(name) {
            Some(value) => value,
            None => panic!("a hot row does not keep {name}; read the row"),
        }
    }

    /// `get`, or `None` for a field a hot row does not keep.
    pub fn lookup(&self, name: &str) -> Option<Option<Field<'_>>> {
        let id = field(name)?;
        let mut at = 0;
        while at < self.bytes.len() {
            let (held, kind, start, end) = entry(&self.bytes, at);
            if held >= id {
                return Some((held == id).then(|| decode(kind, &self.bytes[start..end])));
            }
            at = end;
        }
        Some(None)
    }
}

const NULL: u8 = 0;
const FALSE: u8 = 1;
const TRUE: u8 = 2;
const INT: u8 = 3;
const FLOAT: u8 = 4;
const STR: u8 = 5;
const JSON: u8 = 6;

/// Field `id`'s entry: an int as a zigzag varint, a float as its eight bytes, a string, and the
/// JSON text of anything else, after its varint length.
fn put(bytes: &mut Vec<u8>, id: u8, value: &Value) {
    let text = |bytes: &mut Vec<u8>, kind: u8, text: &str| {
        bytes.extend([id, kind]);
        varint(bytes, text.len() as u64);
        bytes.extend_from_slice(text.as_bytes());
    };
    match Field::from(value) {
        Field::Null => bytes.extend([id, NULL]),
        Field::Bool(flag) => bytes.extend([id, if flag { TRUE } else { FALSE }]),
        Field::Int(int) => {
            bytes.extend([id, INT]);
            varint(bytes, ((int << 1) ^ (int >> 63)) as u64);
        }
        Field::Float(float) => {
            bytes.extend([id, FLOAT]);
            bytes.extend(float.to_le_bytes());
        }
        Field::Str(value) => text(bytes, STR, value),
        Field::Json(_) | Field::Value(_) => text(bytes, JSON, &value.to_string()),
    }
}

fn varint(bytes: &mut Vec<u8>, mut value: u64) {
    while value >= 0x80 {
        bytes.push(value as u8 | 0x80);
        value >>= 7;
    }
    bytes.push(value as u8);
}

/// The varint at `at`, and where it ends.
fn read_varint(bytes: &[u8], mut at: usize) -> (u64, usize) {
    let (mut value, mut shift) = (0, 0);
    loop {
        let byte = bytes[at];
        at += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte < 0x80 {
            return (value, at);
        }
        shift += 7;
    }
}

/// The entry at `at`: its id, its kind, and where its value starts and ends. A lookup steps
/// over the entries before the one it wants without reading their values.
fn entry(bytes: &[u8], at: usize) -> (u8, u8, usize, usize) {
    let (id, kind, at) = (bytes[at], bytes[at + 1], at + 2);
    let (start, end) = match kind {
        NULL | FALSE | TRUE => (at, at),
        INT => (at, read_varint(bytes, at).1),
        FLOAT => (at, at + 8),
        _ => {
            let (len, at) = read_varint(bytes, at);
            (at, at + len as usize)
        }
    };
    (id, kind, start, end)
}

fn decode(kind: u8, value: &[u8]) -> Field<'_> {
    let text = || std::str::from_utf8(value).expect("`put` writes text");
    match kind {
        NULL => Field::Null,
        FALSE => Field::Bool(false),
        TRUE => Field::Bool(true),
        INT => {
            let zigzag = read_varint(value, 0).0;
            Field::Int((zigzag >> 1) as i64 ^ -((zigzag & 1) as i64))
        }
        FLOAT => Field::Float(f64::from_le_bytes(value.try_into().expect("eight bytes"))),
        STR => Field::Str(text()),
        _ => Field::Json(text()),
    }
}

/// A row's field as a pass reads it: borrowed from a hot row, or from a row held whole. Read
/// with `Value`'s names (`as_str`, `as_i64`, ...); an object or array a hot row keeps is its
/// JSON text, parsed by `value`.
#[derive(Debug, Clone, Copy)]
pub enum Field<'a> {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(&'a str),
    /// A hot row's object or array, or a number neither an i64 nor an f64 holds as it was.
    Json(&'a str),
    /// An object, an array or such a number of a row held whole.
    Value(&'a Value),
}

impl<'a> From<&'a Value> for Field<'a> {
    fn from(value: &'a Value) -> Self {
        match value {
            Value::Null => Field::Null,
            Value::Bool(flag) => Field::Bool(*flag),
            Value::Number(number) => match (number.as_i64(), number.as_f64()) {
                (Some(int), _) => Field::Int(int),
                (None, Some(float)) if number.is_f64() => Field::Float(float),
                _ => Field::Value(value),
            },
            Value::String(text) => Field::Str(text),
            _ => Field::Value(value),
        }
    }
}

/// A field passed on by reference, as `.filter(|field| truthy(field))` hands it.
impl<'a> From<&Field<'a>> for Field<'a> {
    fn from(field: &Field<'a>) -> Self {
        *field
    }
}

impl<'a> From<&&'a Value> for Field<'a> {
    fn from(value: &&'a Value) -> Self {
        Field::from(*value)
    }
}

impl<'a> Field<'a> {
    pub fn as_str(self) -> Option<&'a str> {
        match self {
            Field::Str(text) => Some(text),
            _ => None,
        }
    }
    pub fn as_i64(self) -> Option<i64> {
        match self {
            Field::Int(int) => Some(int),
            Field::Value(value) => value.as_i64(),
            _ => None,
        }
    }
    pub fn as_u64(self) -> Option<u64> {
        match self {
            Field::Int(int) => u64::try_from(int).ok(),
            Field::Value(value) => value.as_u64(),
            Field::Json(text) => text.parse().ok(),
            _ => None,
        }
    }
    pub fn as_f64(self) -> Option<f64> {
        match self {
            Field::Int(int) => Some(int as f64),
            Field::Float(float) => Some(float),
            Field::Value(value) => value.as_f64(),
            Field::Json(text) => text.parse::<u64>().ok().map(|int| int as f64),
            _ => None,
        }
    }
    pub fn as_bool(self) -> Option<bool> {
        match self {
            Field::Bool(flag) => Some(flag),
            _ => None,
        }
    }
    pub fn is_null(self) -> bool {
        matches!(self, Field::Null)
    }
    /// A list of a row held whole. Panics for a hot row's: its text has no list to borrow.
    pub fn as_array(self) -> Option<&'a Vec<Value>> {
        match self {
            Field::Value(value) => value.as_array(),
            Field::Json(text) if text.starts_with('[') => panic!("a hot row's list is its JSON text; read it with `value`"),
            _ => None,
        }
    }
    /// An object of a row held whole. Panics for a hot row's: its text has no map to borrow.
    pub fn as_object(self) -> Option<&'a serde_json::Map<String, Value>> {
        match self {
            Field::Value(value) => value.as_object(),
            Field::Json(text) if text.starts_with('{') => panic!("a hot row's object is its JSON text; read it with `value`"),
            _ => None,
        }
    }
    /// The field as a value: the row's own when it holds one, else made from the field.
    pub fn value(self) -> Cow<'a, Value> {
        match self {
            Field::Value(value) => Cow::Borrowed(value),
            field => Cow::Owned(field.to_value()),
        }
    }
    pub fn to_value(self) -> Value {
        match self {
            Field::Null => Value::Null,
            Field::Bool(flag) => Value::Bool(flag),
            Field::Int(int) => Value::from(int),
            Field::Float(float) => Value::from(float),
            Field::Str(text) => Value::from(text),
            Field::Json(text) => serde_json::from_str(text).unwrap_or(Value::Null),
            Field::Value(value) => value.clone(),
        }
    }
}

/// As the values they stand for compare.
impl PartialEq for Field<'_> {
    fn eq(&self, other: &Self) -> bool {
        match (*self, *other) {
            (Field::Null, Field::Null) => true,
            (Field::Bool(one), Field::Bool(other)) => one == other,
            (Field::Int(one), Field::Int(other)) => one == other,
            (Field::Float(one), Field::Float(other)) => one == other,
            (Field::Str(one), Field::Str(other)) => one == other,
            (Field::Value(one), Field::Value(other)) => one == other,
            (one @ (Field::Json(_) | Field::Value(_)), other @ (Field::Json(_) | Field::Value(_))) => one.value() == other.value(),
            _ => false,
        }
    }
}

/// As the value it stands for: `json!` and the XML writer take a field as they took the value.
impl serde::Serialize for Field<'_> {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match *self {
            Field::Null => serializer.serialize_unit(),
            Field::Bool(flag) => serializer.serialize_bool(flag),
            Field::Int(int) => serializer.serialize_i64(int),
            Field::Float(float) => serializer.serialize_f64(float),
            Field::Str(text) => serializer.serialize_str(text),
            Field::Json(_) | Field::Value(_) => self.value().serialize(serializer),
        }
    }
}

/// What a pass over the library reads a row through: the row, or its hot fields.
pub trait Row {
    fn get(&self, name: &str) -> Option<Field<'_>>;
    /// The whole row, for what `get` cannot answer: the row as one value (a copy of it, its
    /// keys in order). A hot row alone has none and panics.
    fn whole(&self) -> &Value;
}

impl Row for Value {
    fn get(&self, name: &str) -> Option<Field<'_>> {
        Value::get(self, name).map(Field::from)
    }
    fn whole(&self) -> &Value {
        self
    }
}

/// A row's object, as `hierarchy` reads it.
impl Row for serde_json::Map<String, Value> {
    fn get(&self, name: &str) -> Option<Field<'_>> {
        serde_json::Map::get(self, name).map(Field::from)
    }
    fn whole(&self) -> &Value {
        panic!("a map is not a whole row")
    }
}

impl Row for HotRow {
    fn get(&self, name: &str) -> Option<Field<'_>> {
        HotRow::get(self, name)
    }
    fn whole(&self) -> &Value {
        panic!("a hot row is not the whole row; read it through `Lazy`")
    }
}

impl Row for Cow<'_, Value> {
    fn get(&self, name: &str) -> Option<Field<'_>> {
        Value::get(self, name).map(Field::from)
    }
    fn whole(&self) -> &Value {
        self
    }
}

impl Row for std::sync::Arc<Value> {
    fn get(&self, name: &str) -> Option<Field<'_>> {
        Value::get(self, name).map(Field::from)
    }
    fn whole(&self) -> &Value {
        self
    }
}

impl<T: Row + ?Sized> Row for &T {
    fn get(&self, name: &str) -> Option<Field<'_>> {
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

    pub fn get(&self, name: &str) -> Option<Field<'h>> {
        if let Some(value) = self.rows.view_field(self.hot.position, name) {
            return value.map(Field::from);
        }
        match self.hot.lookup(name) {
            Some(value) => value,
            None => self.body().get(name).map(Field::from),
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
    fn get(&self, name: &str) -> Option<Field<'_>> {
        match self {
            AnyRow::Ref(row) => Row::get(row, name),
            AnyRow::Owned(row) => Row::get(row, name),
            AnyRow::Borrowed(row) => Row::get(*row, name),
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
    fn get(&self, name: &str) -> Option<Field<'_>> {
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
                                     "summary": "long", "librarySectionID": true, "_retry": {"at": 5},
                                     "addedAt": -1_791_651_463i64, "updatedAt": i64::MIN, "duration": u64::MAX, "rating": 6.9,
                                     "audienceRating": -0.5, "year": 0, "deletedAt": null, "title": "", "_matched": false,
                                     "grandparentTitle": "Ångström \"Ü\" 東京", "_collections": [{"tag": "Saga"}, 1.5, null],
                                     "_placed": {"kind": "episode", "season": 1, "episode": 1, "tmdb_episode": 4789477}});
        let hot = HotRow::of(3, &row);
        for name in FIELDS {
            assert_eq!(hot.get(name), row.get(*name).map(Field::from), "{name}");
            assert_eq!(hot.get(name).map(Field::to_value).as_ref(), row.get(*name), "{name}");
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
        // An episode its source sent in parts: each Part its own release, on its own file.
        let row = serde_json::json!({"Media": [{"_source": "zurg", "_sid": "e13", "Part": [
            {"_source": "zurg", "_sid": "e13", "file": "/f/e13"}, {"_source": "zurg", "_sid": "e14", "file": "/f/e14"}]}]});
        let got: Vec<_> = HotRow::of(0, &row).releases.iter().map(|r| (r.sid.to_string(), r.index, r.file())).collect();
        assert_eq!(got, vec![("e13".to_string(), 0, Some("/f/e13".to_string())), ("e14".to_string(), 0, Some("/f/e14".to_string()))]);
    }

    #[test]
    #[should_panic(expected = "a hot row does not keep summary")]
    fn a_field_it_does_not_keep_is_refused() {
        HotRow::of(0, &serde_json::json!({"summary": "x"})).get("summary");
    }
}
