//! `identify.rs` against the user's own Real-Debrid library, with DMM's labels as the
//! key: `tests/fixtures/identify/corpus.json` replayed offline. Every request the port
//! makes must be one the recording holds (the Python identifier's, when the corpus was
//! recorded); a miss answers `{}` and fails the test, which is what proves the two send
//! the same requests.
//!
//! The keys are Python's `json.dumps` text, so they are parsed and indexed by value,
//! never reproduced: TMDB's by `(path, sorted params)` with each value as `str()` puts
//! it in a URL (the years are recorded as JSON numbers), Plex's by
//! `[method, path, body]` with object keys sorted.

use std::cell::RefCell;
use std::collections::HashMap;

use rex::identify::{Identifier, Identity, PlexSource, TmdbSource, AGREED};
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

/// A recorded corpus and its provider answers, ready to replay.
fn replay(path: &str) -> (Value, ReplayTmdb, ReplayPlex) {
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
    (corpus, tmdb, plex)
}

#[test]
fn identify_corpus() {
    let (corpus, tmdb, plex) = replay(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/identify/corpus.json"
    ));
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

/// One release of each of the 285 shows the production rex left unmatched on 2026-10-11
/// although zurg pushed the IMDb id DMM knows its hash by: 271 of those ids are an
/// episode's (Arrow's tt2340185 is its pilot), which TMDB's /find answers under
/// `tv_episode_results`, and each show stayed on `local://`.
#[test]
fn episode_ids_name_their_show() {
    let (corpus, tmdb, plex) = replay(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/identify/episode_ids.json"
    ));
    let torrents = corpus["torrents"].as_array().unwrap();
    let identifier = Identifier {
        tmdb: Some(&tmdb),
        plex: Some(&plex),
        known: torrents
            .iter()
            .map(|torrent| {
                (
                    torrent["hash"].as_str().unwrap().to_string(),
                    torrent["known"].as_str().unwrap().to_string(),
                )
            })
            .collect(),
    };
    let mut shows = 0;
    let mut missed = Vec::new();
    for torrent in torrents {
        let identity = identifier.identify(
            torrent["name"].as_str().unwrap(),
            torrent["hash"].as_str(),
            torrent["files"].as_u64().unwrap() as usize,
        );
        if torrent["known"] == "tt2340185" {
            assert_eq!(identity.imdb.as_deref(), Some("tt2193021"), "{identity:?}");
        }
        let Some(truth) = torrent["truth"].as_i64() else {
            continue;
        };
        shows += 1;
        let right = identity.kind == "show"
            && identity.tmdb == Some(truth)
            && identity.plex.as_deref().is_some_and(|guid| guid.starts_with("plex://show/"));
        if !right {
            missed.push(identity.name.clone());
        }
    }
    let (tmdb_missed, plex_missed) = (tmdb.missed.borrow(), plex.missed.borrow());
    assert!(
        tmdb_missed.is_empty() && plex_missed.is_empty(),
        "requests the recording does not hold: tmdb {tmdb_missed:?} plex {plex_missed:?}"
    );
    assert!(shows >= 270, "{shows} shows");
    assert!(missed.is_empty(), "{} of {shows} missed: {:?}", missed.len(), &missed[..missed.len().min(3)]);
}

/// Recorded on the test host 2026-10-11. Plex's matcher answers
/// `The.Tom.and.Jerry.Show.S02E05` with the 1975 series, one season long; TMDB's first hit
/// is the 2014 series, five seasons. Plex won the dispute and every episode sat under a
/// show with no season 2 or 3.
#[test]
fn a_season_only_tmdb_has_decides_the_dispute() {
    let (corpus, tmdb, plex) = replay(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/identify/tom_and_jerry.json"
    ));
    let identifier = Identifier {
        tmdb: Some(&tmdb),
        plex: Some(&plex),
        known: HashMap::new(),
    };
    for torrent in corpus["torrents"].as_array().unwrap() {
        let identity = identifier.identify(
            torrent["name"].as_str().unwrap(),
            None,
            torrent["files"].as_u64().unwrap() as usize,
        );
        assert_eq!(identity.tmdb, torrent["truth"].as_i64(), "{identity:?}");
        assert!(
            identity.kind == "show" && identity.plex.as_deref().is_some_and(|guid| guid.starts_with("plex://show/")),
            "{identity:?}"
        );
        assert_eq!(identity.disputed.as_ref().and_then(|disputed| disputed.tmdb), Some(7842), "{identity:?}");
    }
    let (tmdb_missed, plex_missed) = (tmdb.missed.borrow(), plex.missed.borrow());
    assert!(
        tmdb_missed.is_empty() && plex_missed.is_empty(),
        "requests the recording does not hold: tmdb {tmdb_missed:?} plex {plex_missed:?}"
    );
}

/// One release of each movie and show the production rex drew the blank tile for on 2026-10-12, as
/// zurg pushed it (the IMDb id DMM gave its hash, or the TMDB id zurg's own Plex gave it),
/// identified as the feed identifies it (`name_release`): in its library's kind, a TMDB id
/// with no IMDb id read as a `{tmdb-…}` tag. The TMDB id each is was labelled by hand.
/// The oracle names 82 of the 112 labelled; before this the port named 10.
#[test]
fn releases_on_the_blank_tile_are_named_in_their_library_kind() {
    let (corpus, tmdb, plex) = replay(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/identify/blank_tile_releases.json"
    ));
    let torrents = corpus["torrents"].as_array().unwrap();
    let identifier = Identifier {
        tmdb: Some(&tmdb),
        plex: Some(&plex),
        known: torrents
            .iter()
            .filter_map(|torrent| Some((torrent["hash"].as_str()?.to_string(), torrent["known"].as_str()?.to_string())))
            .collect(),
    };
    let (mut labelled, mut right, mut missed) = (0, 0, Vec::new());
    for torrent in torrents {
        let mut name = torrent["name"].as_str().unwrap().to_string();
        if let (Some(id), None) = (torrent["tmdb"].as_str(), torrent["known"].as_str()) {
            name = format!("{name} {{tmdb-{id}}}");
        }
        let kind = torrent["kind"].as_str().unwrap();
        let identity = identifier.identify_as(
            &name,
            Some(torrent["hash"].as_str().unwrap_or("zurg")),
            torrent["files"].as_u64().unwrap() as usize,
            Some(kind),
        );
        if identity.tmdb.is_some() && identity.confidence != rex::identify::EXACT {
            assert_eq!(identity.kind, kind, "{identity:?}");
        }
        let Some(truth) = torrent["truth"].as_i64() else {
            continue;
        };
        labelled += 1;
        if identity.tmdb == Some(truth) && identity.kind == kind {
            right += 1;
        } else {
            missed.push(name);
        }
    }
    let (tmdb_missed, plex_missed) = (tmdb.missed.borrow(), plex.missed.borrow());
    assert!(
        tmdb_missed.is_empty() && plex_missed.is_empty(),
        "requests the recording does not hold: tmdb {tmdb_missed:?} plex {plex_missed:?}"
    );
    assert_eq!(labelled, 112);
    assert!(right >= 82, "{right} of {labelled} named; missed {:?}", &missed[..missed.len().min(5)]);
}
