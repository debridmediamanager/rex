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
    pub section: String,
    pub title_trimmed: Box<str>,
    /// The title's words joined by one space, and the original title's.
    pub title_words: Box<str>,
    pub original_words: Box<str>,
    /// By `CORPUS_TAG_FIELDS`: (index in the row's list, name id).
    pub tags: [Vec<(u32, u32)>; 4],
}

/// Every tag name a row has held, each once, folded into words; ids are never reused.
#[derive(Debug, Default)]
pub struct Names {
    ids: HashMap<String, u32>,
    pub words: Vec<Vec<String>>,
}

fn text_of<'a>(row: &'a Value, field: &str) -> &'a str {
    row.get(field).and_then(Value::as_str).unwrap_or("")
}

fn words(text: &str) -> Vec<String> {
    text.split_whitespace().map(str::to_string).collect()
}

pub fn corpus_row(row: &Value, names: &mut Names) -> CorpusRow {
    let title = fold_query(text_of(row, "title"));
    let mut tags: [Vec<(u32, u32)>; 4] = Default::default();
    for (slot, field) in CORPUS_TAG_FIELDS.iter().enumerate() {
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
            tags[slot].push((index as u32, id));
        }
    }
    CorpusRow {
        searchable: matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show" | "season" | "episode")),
        section: match row.get("librarySectionID") {
            Some(Value::String(value)) => value.clone(),
            Some(Value::Number(value)) => value.to_string(),
            Some(Value::Bool(value)) => value.to_string(),
            _ => String::new(),
        },
        title_trimmed: title.trim().into(),
        title_words: words(&title).join(" ").into(),
        original_words: words(&fold_query(text_of(row, "originalTitle"))).join(" ").into(),
        tags,
    }
}
