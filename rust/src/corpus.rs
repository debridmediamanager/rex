//! What every search reads off every row (`runtime/search.rs`), kept with the catalog's index
//! so a written row re-derives only its own entry. Built whole from every row on the first
//! search after any write, it would read every row from disk (`hotrow.rs`).

use std::collections::HashMap;

use serde_json::Value;

use crate::hierarchy::fold_query;

/// The tag lists a search reads: the three `SEARCH_TAG_HUBS` fields, then `Collection`.
pub const CORPUS_TAG_FIELDS: [&str; 4] = ["Role", "Director", "Genre", "Collection"];

#[derive(Debug, Default, Clone)]
pub struct CorpusRow {
    pub searchable: bool,
    /// The row's `librarySectionID` (`Names::section`).
    pub section: u32,
    pub title_trimmed: Box<str>,
    /// The title's words joined by one space, and the original title's.
    pub title_words: Box<str>,
    pub original_words: Box<str>,
    /// By `CORPUS_TAG_FIELDS`: where its (index in the row's list, name id) pairs sit in
    /// `Names::pool` (`Names::tags`).
    tags: [(u32, u32); 4],
    /// A movie's or show's `Genre` and `Role` names, lowercased (`Names::lower`), in list
    /// order: what Related counts against every title of a library (`item_hubs.rs`).
    pub related: Option<Box<[Box<[u32]>; 2]>>,
}

/// Every tag name a row has held, each once, folded into words; ids are never reused.
#[derive(Debug, Default)]
pub struct Names {
    ids: HashMap<String, u32>,
    pub words: Vec<Vec<String>>,
    /// Lowercased tag names, each once.
    lower: HashMap<String, u32>,
    /// Every row's tags in one list, in row order: a search reads every row's, and rows' own
    /// small lists, apart in memory, made it twice as slow as it was (2026-10-18). A row
    /// written again leaves its old span behind (`garbage`), until `compact`.
    pool: Vec<(u32, u32)>,
    garbage: usize,
    /// Each section id's text, and the other way.
    sections: Vec<String>,
    section_ids: HashMap<String, u32>,
}

impl Names {
    /// A row's tags of one `CORPUS_TAG_FIELDS` slot.
    pub fn tags(&self, row: &CorpusRow, slot: usize) -> &[(u32, u32)] {
        let (start, len) = row.tags[slot];
        &self.pool[start as usize..(start + len) as usize]
    }

    /// The `librarySectionID` text of a section id.
    pub fn section(&self, id: u32) -> &str {
        self.sections.get(id as usize).map_or("", String::as_str)
    }

    /// The id of a section's text, if a row ever held it.
    pub fn section_id(&self, text: &str) -> Option<u32> {
        self.section_ids.get(text).copied()
    }

    /// `row`'s entry is no longer held (written again, or its row removed).
    pub fn release(&mut self, row: &CorpusRow) {
        self.garbage += row.tags.iter().map(|(_, len)| *len as usize).sum::<usize>();
    }

    /// The pool again, rows in order, once over half of it is left behind.
    pub fn compact(&mut self, rows: &mut [CorpusRow]) {
        if self.garbage < 4096 || self.garbage * 2 < self.pool.len() {
            return;
        }
        let mut pool = Vec::with_capacity(self.pool.len() - self.garbage);
        for row in rows.iter_mut() {
            for slot in row.tags.iter_mut() {
                let start = pool.len() as u32;
                pool.extend_from_slice(&self.pool[slot.0 as usize..(slot.0 + slot.1) as usize]);
                slot.0 = start;
            }
        }
        self.pool = pool;
        self.garbage = 0;
    }

    /// The id of a tag name as rows hold it (`CorpusRow::tags`), if a row ever held it.
    pub fn raw(&self, name: &str) -> Option<u32> {
        self.ids.get(name).copied()
    }

    /// Every tag name and its id, each once.
    pub fn each(&self) -> impl Iterator<Item = (&str, u32)> {
        self.ids.iter().map(|(name, id)| (name.as_str(), *id))
    }

    /// The id of a lowercased tag name (`CorpusRow::related`), if a row ever held it.
    pub fn lower(&self, name: &str) -> Option<u32> {
        self.lower.get(name).copied()
    }

    fn lower_id(&mut self, name: String) -> u32 {
        let next = self.lower.len() as u32;
        *self.lower.entry(name).or_insert(next)
    }
}

fn text_of<'a>(row: &'a Value, field: &str) -> &'a str {
    row.get(field).and_then(Value::as_str).unwrap_or("")
}

/// `runtime::string_value`.
fn text(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Bool(value) => value.to_string(),
        _ => String::new(),
    }
}

fn words(text: &str) -> Vec<String> {
    text.split_whitespace().map(str::to_string).collect()
}

pub fn corpus_row(row: &Value, names: &mut Names) -> CorpusRow {
    let title = fold_query(text_of(row, "title"));
    let mut tags = [(0u32, 0u32); 4];
    for (slot, field) in CORPUS_TAG_FIELDS.iter().enumerate() {
        tags[slot].0 = names.pool.len() as u32;
        for (index, tag) in row.get(*field).and_then(Value::as_array).into_iter().flatten().enumerate() {
            let name = text_of(tag, "tag");
            let id = match names.ids.get(name) {
                Some(id) => *id,
                None => {
                    names.words.push(words(&fold_query(name)));
                    let id = (names.words.len() - 1) as u32;
                    names.ids.insert(name.to_string(), id);
                    id
                }
            };
            names.pool.push((index as u32, id));
            tags[slot].1 += 1;
        }
    }
    let related = matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show")).then(|| {
        let mut lists = ["Genre", "Role"].map(|field| {
            row.get(field)
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|tag| tag.get("tag"))
                .map(|name| names.lower_id(text(name).to_lowercase()))
                .collect::<Box<[u32]>>()
        });
        Box::new([std::mem::take(&mut lists[0]), std::mem::take(&mut lists[1])])
    });
    CorpusRow {
        related,
        searchable: matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show" | "season" | "episode")),
        section: {
            let section = row.get("librarySectionID").map(text).unwrap_or_default();
            let next = names.sections.len() as u32;
            *names.section_ids.entry(section.clone()).or_insert_with(|| {
                names.sections.push(section);
                next
            })
        },
        title_trimmed: title.trim().into(),
        title_words: words(&title).join(" ").into(),
        original_words: words(&fold_query(text_of(row, "originalTitle"))).join(" ").into(),
        tags,
    }
}
