//! `identify.rs` against the user's own Real-Debrid library, with DMM's labels as the
//! key: `tests/fixtures/identify/corpus.json` replayed exactly as `tests/test_identify.py`
//! replays it. Every request the port makes must be one the Python identifier made when
//! the corpus was recorded; a miss answers `{}` and fails the test, which is what proves
//! the two send the same requests.
//!
//! The keys are Python's `json.dumps` text, so they are parsed and indexed by value,
//! never reproduced: TMDB's by `(path, sorted params)` with each value as `str()` puts
//! it in a URL (the years are recorded as JSON numbers), Plex's by
//! `[method, path, body]` with object keys sorted.

use std::cell::RefCell;
use std::collections::HashMap;

use simplex::identify::{Identifier, Identity, PlexSource, TmdbSource, AGREED};
use serde_json::{json, Value};

type TmdbKey = (String, Vec<(String, String)>);

fn text(value: &Value) -> String {
    value
        .as_str()
        .map_or_else(|| value.to_string(), str::to_string)
}

fn sorted_keys(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys = map.keys().collect::<Vec<_>>();
            keys.sort();
            Value::Object(
                keys.into_iter()
                    .map(|key| (key.clone(), sorted_keys(&map[key])))
                    .collect(),
            )
        }
        Value::Array(rows) => Value::Array(rows.iter().map(sorted_keys).collect()),
        other => other.clone(),
    }
}

struct ReplayTmdb {
    record: HashMap<TmdbKey, Value>,
    missed: RefCell<Vec<String>>,
}

impl TmdbSource for ReplayTmdb {
    fn get(&self, path: &str, params: &[(&str, String)]) -> Option<Value> {
        let mut pairs = params
            .iter()
            .map(|(name, value)| (name.to_string(), value.clone()))
            .collect::<Vec<_>>();
        pairs.sort();
        let key = (path.to_string(), pairs);
        let answer = self.record.get(&key).cloned();
        if answer.is_none() {
            self.missed.borrow_mut().push(format!("{key:?}"));
        }
        Some(answer.unwrap_or_else(|| json!({})))
    }
}

struct ReplayPlex {
    record: HashMap<String, Value>,
    missed: RefCell<Vec<String>>,
}

impl PlexSource for ReplayPlex {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        let key = sorted_keys(&json!([method, path, body])).to_string();
        let answer = self.record.get(&key).cloned();
        if answer.is_none() {
            self.missed.borrow_mut().push(key);
        }
        Some(answer.unwrap_or_else(|| json!({})))
    }
}

#[test]
fn identify_corpus() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/identify/corpus.json"
    );
    let corpus: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let tmdb = ReplayTmdb {
        record: corpus["tmdb"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, answer)| {
                let key: Value = serde_json::from_str(key).unwrap();
                let params = key[1]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|pair| (text(&pair[0]), text(&pair[1])));
                ((text(&key[0]), params.collect()), answer.clone())
            })
            .collect(),
        missed: RefCell::default(),
    };
    let plex = ReplayPlex {
        record: corpus["plex"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, answer)| {
                let key: Value = serde_json::from_str(key).unwrap();
                (sorted_keys(&key).to_string(), answer.clone())
            })
            .collect(),
        missed: RefCell::default(),
    };
    let identifier = Identifier {
        tmdb: Some(&tmdb),
        plex: Some(&plex),
        known: HashMap::new(),
    };
    let torrents = corpus["torrents"].as_array().unwrap();
    let identities = torrents
        .iter()
        .map(|torrent| {
            let files = torrent["files"].as_u64().unwrap() as usize;
            (
                torrent["truth"].as_str(),
                identifier.identify(torrent["name"].as_str().unwrap(), None, files),
            )
        })
        .collect::<Vec<(Option<&str>, Identity)>>();

    let (tmdb_missed, plex_missed) = (tmdb.missed.borrow(), plex.missed.borrow());
    assert!(
        tmdb_missed.is_empty() && plex_missed.is_empty(),
        "requests the recording does not hold: tmdb {tmdb_missed:?} plex {plex_missed:?}"
    );

    for (truth, identity) in &identities {
        if identity.imdb.as_deref() != *truth {
            println!(
                "differs from the label {}: {:?} ({}, {:?}) for {:?}",
                truth.unwrap_or("-"),
                identity.imdb,
                identity.confidence,
                identity.disputed,
                identity.name
            );
        }
    }
    let right = identities
        .iter()
        .filter(|(truth, identity)| identity.imdb.as_deref() == *truth)
        .count();
    let agreed = identities
        .iter()
        .filter(|(_, identity)| identity.confidence == AGREED)
        .collect::<Vec<_>>();
    let agreed_right = agreed
        .iter()
        .filter(|(truth, identity)| identity.imdb.as_deref() == *truth)
        .count();
    let undisputed_wrong = identities
        .iter()
        .filter(|(truth, identity)| {
            identity
                .imdb
                .as_deref()
                .is_some_and(|imdb| !imdb.is_empty() && Some(imdb) != *truth)
                && identity.confidence != AGREED
                && identity.disputed.is_none()
        })
        .map(|(_, identity)| identity.name.as_str())
        .collect::<Vec<_>>();
    println!(
        "right {right} of {}; agreed right {agreed_right} of {}; undisputed wrong {}",
        identities.len(),
        agreed.len(),
        undisputed_wrong.len()
    );

    // The Python oracle scores 193 of 200 and 169 of 173 agreed.
    assert!(right >= 193, "right {right}");
    assert!(
        agreed.len() >= 150 && agreed_right as f64 / agreed.len() as f64 >= 0.97,
        "agreed right {agreed_right} of {}",
        agreed.len()
    );
    assert!(
        undisputed_wrong.len() <= 2,
        "undisputed wrong: {undisputed_wrong:?}"
    );
    for (_, identity) in identities.iter().filter(|(_, identity)| {
        identity
            .imdb
            .as_deref()
            .is_some_and(|imdb| !imdb.is_empty())
    }) {
        assert!(
            identity
                .plex
                .as_deref()
                .is_some_and(|plex| !plex.is_empty())
                && ["movie", "show"].contains(&identity.kind.as_str())
                && identity
                    .source
                    .as_deref()
                    .is_some_and(|source| !source.is_empty()),
            "an answer without a Plex guid, kind or source: {identity:?}"
        );
    }
}
