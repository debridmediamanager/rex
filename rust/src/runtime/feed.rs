//! The feed: sources push their items here, and rex lists or queries no source.
//!
//! `docs/feed-api.md` is the reference. A request is authenticated by its source's feed
//! token (never a Plex token), parsed, classified under the runtime lock, identified off
//! it (TMDB and Plex's matcher are network lookups), and applied under it again, then
//! saved before it is answered. Feed requests apply one at a time (`FEED`), which is also
//! what keeps a snapshot's chunks in order.
//!
//! A row the feed made carries `_source`, `_sid` (the source's id), `_path` (its bytes
//! under the source's url) and `_ids` (the ids it was identified by). An item without
//! `media_info` is not a row at all: it waits in `feed_held` until a push brings one.
//!
//! A real PMS makes every file of one title one item, each file a Media of it, so a
//! second release of a movie or episode already here joins its row (`fold_versions`).
//! Each Media of such a row carries its own `_source`, `_sid` and `_path`, and the row's
//! own are always its first Media's.

use super::scan::{is_sample, name_release, row_files, under, Arrival, Found, Library};
use super::source::{is_video, Source};
use super::*;
use std::collections::BTreeMap;
use std::sync::LazyLock;

/// The snapshot each source is in the middle of, and the lock every feed request holds.
static FEED: LazyLock<Mutex<HashMap<String, Session>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

struct Session {
    id: String,
    next_part: u64,
    seen: HashSet<String>,
}

/// One pushed item, its defaults filled in.
#[derive(Debug, Clone)]
struct Item {
    id: String,
    path: String,
    file: String,
    library: Option<String>,
    name: String,
    release: String,
    size: u64,
    hash: Option<String>,
    imdb: Option<String>,
    tmdb: Option<String>,
    ids: Map<String, Value>,
    media_info: Option<Value>,
}

impl Item {
    fn parse(source: &Source, value: &Value) -> Result<Self, (String, String)> {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::trim).filter(|v| !v.is_empty()).map(str::to_string);
        let id = text("id").ok_or_else(|| (String::new(), "no id".to_string()))?;
        let refuse = |reason: &str| (id.clone(), reason.to_string());
        let path = text("path").map(|path| path.trim_matches('/').to_string()).filter(|path| !path.is_empty()).ok_or_else(|| refuse("no path"))?;
        if path.split('/').any(|segment| segment.is_empty() || segment == "." || segment == "..") {
            return Err(refuse("path has an empty, . or .. segment"));
        }
        let file = text("file").unwrap_or_else(|| source.default_file(&path));
        if !is_video(&file) {
            return Err(refuse("not a video file"));
        }
        if is_sample(&file) {
            return Err(refuse("a sample"));
        }
        let segments: Vec<&str> = path.split('/').collect();
        let folder = (segments.len() > 1).then(|| segments[..segments.len() - 1].join("/"));
        let stem = |name: &str| name.rsplit_once('.').map_or(name, |(stem, _)| stem).to_string();
        let name = text("name").unwrap_or_else(|| match &folder {
            Some(folder) => folder.rsplit('/').next().unwrap_or(folder).to_string(),
            None => stem(segments[segments.len() - 1]),
        });
        let release = text("release").unwrap_or_else(|| folder.clone().unwrap_or_else(|| path.clone()));
        let ids = value.get("ids").and_then(Value::as_object).cloned().unwrap_or_default();
        let id_text = |key: &str| ids.get(key).map(string_value).filter(|v| !v.is_empty() && v != "null" && v != "0");
        let imdb = id_text("imdb").filter(|imdb| imdb.starts_with("tt"));
        Ok(Self {
            library: value.get("library").map(string_value).filter(|v| !v.is_empty() && v != "null"),
            size: value.get("size").and_then(Value::as_u64).unwrap_or(0),
            hash: text("hash").map(|hash| hash.to_ascii_lowercase()),
            tmdb: id_text("tmdb"),
            // A probe with no streams is no probe: Plex Web will not play such an item
            // (findings, "An item without a probe cannot play"), so it is held like a missing one.
            media_info: value
                .get("media_info")
                .filter(|probe| probe.get("streams").and_then(Value::as_array).is_some_and(|streams| !streams.is_empty()))
                .cloned(),
            ids: Map::from_iter(
                [("imdb", imdb.clone()), ("tmdb", id_text("tmdb")), ("tvdb", id_text("tvdb"))]
                    .into_iter()
                    .filter_map(|(key, value)| value.map(|value| (key.to_string(), json!(value)))),
            ),
            imdb,
            id,
            path,
            file,
            name,
            release,
        })
    }

    /// What `name_release` and `leaf_for` read, in `/api/library/entry`'s words.
    fn arrival(&self, source: &str) -> Arrival {
        let folder = self.file.rsplit_once('/').map_or(String::new(), |(folder, _)| folder.to_string());
        Arrival {
            found: Found { file: self.file.clone(), size: self.size },
            release: self.name.clone(),
            release_path: folder,
            entry: json!({"hash": self.hash, "imdb_id": self.imdb, "tmdb_id": self.tmdb,
                          "size": self.size, "media_info": self.media_info}),
            extra: Map::from_iter([
                ("_source".to_string(), json!(source)),
                ("_sid".to_string(), json!(self.id)),
                ("_path".to_string(), json!(self.path)),
                ("_ids".to_string(), Value::Object(self.ids.clone())),
            ]),
            meta: None,
        }
    }

    /// The held record: what the push said, minus nothing (it has no probe).
    fn held(&self, since: i64) -> Value {
        json!({"id": self.id, "path": self.path, "file": self.file, "library": self.library,
               "name": self.name, "release": self.release, "size": self.size, "hash": self.hash,
               "ids": self.ids, "since": since})
    }
}

/// What a push did, as the response reports it.
#[derive(Debug, Default)]
struct Counts {
    created: usize,
    updated: usize,
    held: usize,
    unchanged: usize,
    rejected: Vec<Value>,
}

impl Counts {
    fn to_json(&self) -> Map<String, Value> {
        Map::from_iter([
            ("created".to_string(), json!(self.created)),
            ("updated".to_string(), json!(self.updated)),
            ("held".to_string(), json!(self.held)),
            ("unchanged".to_string(), json!(self.unchanged)),
            ("rejected".to_string(), json!(self.rejected)),
        ])
    }
}

/// How each item is applied, decided under the lock before identification runs.
enum Plan {
    /// A row the source already has (or the old scanner made for this file: `adopt`).
    Row { key: String, adopt: bool, reidentify: bool },
    Hold,
    Show,
}

fn held_key(source: &str, id: &str) -> String {
    format!("{source}:{id}")
}

fn lock(shared: &SharedRuntime) -> std::sync::MutexGuard<'_, Runtime> {
    shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn answer(status: u16, body: Value) -> Response {
    Response::json(status, body)
}

/// `/rex/feed/{source}/{items|snapshot|held}`, outside the Plex gate.
pub(super) fn handle(shared: &SharedRuntime, request: &Request, path: &str) -> Response {
    let rest = path.trim_start_matches("/rex/feed/").trim_end_matches('/');
    let (name, leaf) = rest.split_once('/').unwrap_or((rest, ""));
    let Some(source) = lock(shared).sources.iter().find(|source| source.name == name).cloned() else {
        return answer(404, json!({"error": "no such source"}));
    };
    if source.token.is_none() {
        return answer(404, json!({"error": "this source has no feed token"}));
    }
    let presented = request
        .headers
        .get("authorization")
        .and_then(|value| value.strip_prefix("Bearer ").or_else(|| value.strip_prefix("bearer ")))
        .unwrap_or_default();
    if !source.admits(presented.trim()) {
        warning!("FEED {name}: refused a {} without its feed token", request.method);
        return answer(401, json!({"error": "a feed request needs the source's feed token"}));
    }
    let mut sessions = FEED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let body = || -> Result<Value, Response> {
        serde_json::from_slice::<Value>(&request.body)
            .ok()
            .filter(Value::is_object)
            .ok_or_else(|| answer(400, json!({"error": "the body must be a JSON object"})))
    };
    let response = match (request.method.as_str(), leaf) {
        ("GET" | "HEAD", "items") => answer(200, lock(shared).feed_listing(&source)),
        ("GET" | "HEAD", "held") => answer(200, lock(shared).feed_held_listing(&source)),
        ("POST", "items") => match body() {
            Ok(body) => match body.get("items").and_then(Value::as_array) {
                Some(items) => answer(200, Value::Object(upsert(shared, &source, items, None, false).to_json())),
                None => answer(400, json!({"error": "expected {\"items\": [...]}"})),
            },
            Err(response) => response,
        },
        ("DELETE", "items") => match body() {
            Ok(body) => match body.get("ids").and_then(Value::as_array) {
                Some(ids) => {
                    let ids: Vec<String> = ids.iter().map(string_value).filter(|id| !id.is_empty()).collect();
                    let removed = remove(shared, &source, &ids, &[]);
                    answer(200, Value::Object(removed))
                }
                None => answer(400, json!({"error": "expected {\"ids\": [...]}"})),
            },
            Err(response) => response,
        },
        ("PUT", "snapshot") => match body() {
            Ok(body) => snapshot(shared, &source, &body, &mut sessions),
            Err(response) => response,
        },
        (_, "items" | "snapshot" | "held") => answer(405, json!({"error": "method not allowed"})),
        _ => answer(404, json!({"error": "no such feed endpoint"})),
    };
    drop(sessions);
    response
}

/// `PUT .../snapshot`: upsert the chunk, and on the last one remove what no chunk named.
fn snapshot(shared: &SharedRuntime, source: &Source, body: &Value, sessions: &mut HashMap<String, Session>) -> Response {
    let Some(items) = body.get("items").and_then(Value::as_array) else {
        return answer(400, json!({"error": "expected {\"items\": [...]}"}));
    };
    let session = body.get("session").map(string_value).filter(|id| !id.is_empty() && id != "null");
    let part = body.get("part").and_then(Value::as_u64).unwrap_or(0);
    let complete = session.is_none() || body.get("complete").is_some_and(truthy);
    let force = body.get("force").is_some_and(truthy);
    if let Some(id) = &session {
        let continues = sessions.get(&source.name).is_some_and(|open| open.id == *id && open.next_part == part);
        if part == 0 {
            sessions.insert(source.name.clone(), Session { id: id.clone(), next_part: 0, seen: HashSet::new() });
        } else if !continues {
            return answer(409, json!({"error": "no such snapshot session at this part: start again from part 0",
                                      "session": id, "part": part}));
        }
    }
    let mut seen = HashSet::new();
    let counts = upsert(shared, source, items, Some(&mut seen), false);
    let mut out = counts.to_json();
    let mut session_seen = seen;
    if let Some(id) = &session {
        let open = sessions.get_mut(&source.name).expect("the session was opened above");
        open.seen.extend(session_seen.drain());
        open.next_part = part + 1;
        session_seen = open.seen.clone();
        out.insert("session".to_string(), json!(id));
    }
    out.insert("seen".to_string(), json!(session_seen.len()));
    if !complete {
        return answer(200, Value::Object(out));
    }
    sessions.remove(&source.name);
    let (known, orphans) = {
        let guard = lock(shared);
        (guard.feed_ids(&source.name), guard.unadopted_rows(source))
    };
    let missing: Vec<String> = known.iter().filter(|id| !session_seen.contains(*id)).cloned().collect();
    let (total, gone) = (known.len() + orphans.len(), missing.len() + orphans.len());
    if session_seen.is_empty() {
        // Measured (6b3): an empty library folder trashes nothing; neither does this.
        info!("FEED {} snapshot named nothing: {} item(s) kept", source.name, total);
        out.insert("removed".to_string(), json!({"trashed": 0, "deleted": 0, "released": 0}));
        return answer(200, Value::Object(out));
    }
    if gone * 2 > total && !force {
        warning!(
            "FEED {} snapshot refused: it would remove {gone} of {total} item(s); resend with \"force\": true to accept",
            source.name
        );
        out.insert("error".to_string(), json!("the snapshot would remove more than half of this source's items"));
        out.insert("would_remove".to_string(), json!(gone));
        out.insert("of".to_string(), json!(total));
        return answer(409, Value::Object(out));
    }
    // A row the old scanner made from this source that no item claimed is a file the
    // source no longer has, as a whole-library scan found it.
    let mut removed = remove(shared, source, &missing, &orphans);
    removed.shift_remove("unknown");
    out.insert("removed".to_string(), Value::Object(removed));
    answer(200, Value::Object(out))
}

/// Remove ids: held ones are forgotten, rows go to the trash (or are deleted under
/// `autoEmptyTrash`), as a real PMS treats a file that is gone.
fn remove(shared: &SharedRuntime, source: &Source, ids: &[String], keys: &[String]) -> Map<String, Value> {
    let mut guard = lock(shared);
    let rows = guard.feed_rows(&source.name);
    let (mut trashed, mut deleted, mut released, mut unknown) = (0, 0, 0, 0);
    let mut sections = BTreeMap::new();
    let mut doomed = Vec::new();
    for id in ids {
        if guard.release(&held_key(&source.name, id)).is_some() {
            released += 1;
            guard.dirty = true;
            continue;
        }
        match rows.get(id) {
            // One release of a row holding several: that Media goes, the row stays.
            Some(key) if guard.drop_version(key, &source.name, id) => {
                deleted += 1;
                guard.dirty = true;
            }
            Some(key) => doomed.push(key.clone()),
            None => unknown += 1,
        }
    }
    doomed.extend(keys.iter().cloned());
    for key in &doomed {
        if guard.catalog.get(key).is_none() {
            continue;
        }
        let section = guard.catalog.get(key).and_then(|row| row.get("librarySectionID")).map(string_value).unwrap_or_default();
        let activity = sections.entry(section.clone()).or_insert_with(|| None);
        if activity.is_none() {
            let title = guard.section_title(&section);
            *activity = Some(guard.scan_started(&section, &title));
        }
        match guard.remove_missing(key) {
            Some(true) => deleted += 1,
            Some(false) => trashed += 1,
            None => {}
        }
    }
    for (section, activity) in sections {
        if let Some(mut activity) = activity {
            guard.scan_finished(&mut activity, &section);
        }
    }
    if trashed + deleted > 0 {
        guard.dirty = true;
    }
    info!("FEED {} removed {} trashed, {} deleted, {} released, {} unknown", source.name, trashed, deleted, released, unknown);
    let snapshot = guard.flush_state();
    drop(guard);
    write_or_log(snapshot);
    Map::from_iter([
        ("trashed".to_string(), json!(trashed)),
        ("deleted".to_string(), json!(deleted)),
        ("released".to_string(), json!(released)),
        ("unknown".to_string(), json!(unknown)),
    ])
}

/// Upsert a batch: classify under the lock, identify off it, apply under it. `retry`
/// identifies every existing row again (`retry_blank`), not only one whose ids changed.
fn upsert(shared: &SharedRuntime, source: &Source, values: &[Value], mut seen: Option<&mut HashSet<String>>, retry: bool) -> Counts {
    let mut counts = Counts::default();
    let mut items = Vec::new();
    for value in values {
        match Item::parse(source, value) {
            Ok(item) => items.push(item),
            Err((id, reason)) => counts.rejected.push(json!({"id": id, "reason": reason})),
        }
    }
    // The last push of an id wins within a batch.
    let mut unique: BTreeMap<String, usize> = BTreeMap::new();
    for (index, item) in items.iter().enumerate() {
        unique.insert(item.id.clone(), index);
    }
    let items: Vec<Item> = unique.into_values().map(|index| items[index].clone()).collect();

    // -- under the lock: which library, which row, what has to be identified ---------------
    let mut plans: Vec<(Item, Library, Plan)> = Vec::new();
    {
        let guard = lock(shared);
        let rows = guard.feed_rows(&source.name);
        let legacy = guard.legacy_rows_by_file();
        let named = guard.tmdb.is_some() || guard.plex.is_some();
        for item in items {
            let Some(library) = guard.feed_library(&item) else {
                counts.rejected.push(json!({"id": item.id, "reason": format!("no movie or show library holds {}", item.file)}));
                continue;
            };
            if let Some(seen) = seen.as_deref_mut() {
                seen.insert(item.id.clone());
            }
            let existing = rows.get(&item.id).map(|key| (key.clone(), false)).or_else(|| {
                legacy.get(&item.file).map(|key| (key.clone(), true))
            });
            let plan = match existing {
                Some((key, adopt)) => {
                    let row = guard.catalog.get(&key);
                    let had = row.map(row_ids).unwrap_or_default();
                    // ponytail: a row holding several releases is not re-identified by one of
                    // them; splitting the release back out is Split's job.
                    // An id that named nothing when it came is tried again on each push:
                    // 285 of the production server's shows sat on `local://` with zurg's
                    // exact id (2026-10-11), an episode's IMDb id that `tmdb_find_imdb` did
                    // not read yet.
                    let unnamed = row.and_then(|row| row.get("_identify")).is_some_and(|report| {
                        ["tmdb", "plex"].iter().all(|field| report.get(*field).is_none_or(Value::is_null))
                    });
                    // A file whose library changed is placed again in the one it belongs to now.
                    let moved = row.is_some_and(|row| {
                        row.get("librarySectionID").map(string_value).as_deref() != Some(library.key.as_str())
                    });
                    // What a person matched is theirs: no push and no retry identifies it again.
                    let reidentify = !row.is_some_and(|row| guard.matched_by_hand(row))
                        && row.is_some_and(|row| versions(row).len() < 2)
                        && (moved
                            || named
                                && (retry
                                    || match (&item.imdb, &item.tmdb) {
                                        (Some(imdb), _) => unnamed || had.get("imdb").map(string_value).as_deref() != Some(imdb.as_str()),
                                        (None, Some(tmdb)) => unnamed || had.get("tmdb").map(string_value).as_deref() != Some(tmdb.as_str()),
                                        _ => false,
                                    }));
                    Plan::Row { key, adopt, reidentify }
                }
                // A retry names rows that are here; it never makes or holds one.
                None if retry => continue,
                None if item.media_info.is_some() => Plan::Show,
                None => Plan::Hold,
            };
            plans.push((item, library, plan));
        }
    }

    // -- off the lock: identification, once per release ---------------------------------
    let mut groups: BTreeMap<(String, String), Vec<usize>> = BTreeMap::new();
    for (index, (item, library, plan)) in plans.iter().enumerate() {
        if matches!(plan, Plan::Show | Plan::Row { reidentify: true, .. }) {
            groups.entry((library.key.clone(), item.release.clone())).or_default().push(index);
        }
    }
    let mut arrivals: HashMap<usize, Arrival> = HashMap::new();
    for indices in groups.values() {
        let library = &plans[indices[0]].1;
        let mut release: Vec<Arrival> = indices.iter().map(|index| plans[*index].0.arrival(&source.name)).collect();
        let files = release.len();
        name_release(library, &mut release, files);
        for (index, arrival) in indices.iter().zip(release) {
            arrivals.insert(*index, arrival);
        }
    }

    // -- under the lock: apply -----------------------------------------------------------
    let mut guard = lock(shared);
    let stamp = now();
    let mut new_rows: BTreeMap<String, Vec<Arrival>> = BTreeMap::new();
    let mut libraries: HashMap<String, Library> = HashMap::new();
    let mut probed = Vec::new();
    let mut touched = HashSet::new();
    // Each re-identified row's new identity: a show all of whose episodes move to the
    // same one is re-identified in place, keeping its key and its seasons'.
    let moving: HashMap<String, String> = plans
        .iter()
        .enumerate()
        .filter_map(|(index, (_, _, plan))| match plan {
            Plan::Row { key, reidentify: true, .. } => {
                let meta = arrivals.get(&index)?.meta.as_ref()?;
                Some((key.clone(), meta.get("_identity").map(string_value)?))
            }
            _ => None,
        })
        .collect();
    let mut placed = 0;
    for (index, (item, library, plan)) in plans.into_iter().enumerate() {
        let held = held_key(&source.name, &item.id);
        match plan {
            Plan::Hold => {
                let since = guard.feed_held.get(&held).and_then(|old| old.get("since")).and_then(Value::as_i64).unwrap_or(stamp);
                let record = item.held(since);
                if guard.feed_held.get(&held) != Some(&record) {
                    if !guard.feed_held.contains_key(&held) {
                        info!("FEED {} held {}: no media_info yet", source.name, item.id);
                    }
                    guard.hold(held, record);
                    guard.dirty = true;
                }
                counts.held += 1;
            }
            Plan::Show => {
                guard.release(&held);
                let Some(arrival) = arrivals.remove(&index) else {
                    continue;
                };
                new_rows.entry(library.key.clone()).or_default().push(arrival);
                libraries.entry(library.key.clone()).or_insert(library);
            }
            Plan::Row { key, adopt, reidentify } => {
                guard.release(&held);
                if guard.catalog.get(&key).is_none() {
                    counts.unchanged += 1;
                    continue;
                }
                let mut changed = guard.feed_update_row(&key, &source.name, &item, adopt, &mut probed);
                if reidentify {
                    if let Some(arrival) = arrivals.remove(&index) {
                        changed |= guard.reidentify(&library, &key, arrival, &moving);
                        // Placing a row again costs tens of milliseconds, and every request
                        // waits on this lock: the retry's first pass over the production rex's rows held
                        // it 2.5 minutes for 2,154 rows (the test host, 2026-10-13). Requests get
                        // through every few rows.
                        placed += 1;
                        if placed % 8 == 0 {
                            drop(guard);
                            thread::sleep(std::time::Duration::from_millis(5));
                            guard = lock(shared);
                        }
                    }
                    if guard.tmdb.is_some() || guard.plex.is_some() {
                        if let Some(row) = guard.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                            row.insert("_ids".to_string(), Value::Object(item.ids.clone()));
                        }
                    }
                }
                if changed {
                    counts.updated += 1;
                    guard.dirty = true;
                    touched.insert(key.clone());
                } else {
                    counts.unchanged += 1;
                }
            }
        }
    }
    let mut created = Vec::new();
    for (section, arrivals) in new_rows {
        let Some(library) = libraries.remove(&section) else {
            continue;
        };
        let mut activity = guard.scan_started(&section, &library.title);
        let last = arrivals.last().map(|arrival| arrival.release.clone()).unwrap_or_default();
        guard.scan_progress(&mut activity, &section, &last, 99);
        // Measured (4a): a folder scan announces its new rows after the step at 99.
        let keys = guard.place_arrivals(&library, arrivals);
        activity.insert("subtitle".to_string(), json!(last));
        guard.scan_finished(&mut activity, &section);
        created.extend(keys);
    }
    let filled = guard.fill_probes(probed);
    touched.extend(created.iter().cloned());
    let folded = guard.fold_versions(Some(&touched));
    created.retain(|key| !folded.contains(key));
    counts.created += created
        .iter()
        .filter(|key| {
            guard.catalog.get(key).and_then(|row| row.get("type")).and_then(Value::as_str).is_some_and(|kind| kind == "movie" || kind == "episode")
        })
        .count();
    counts.updated += folded.len();
    for key in &created {
        guard.created_metadata_frames(key);
    }
    if !created.is_empty() || filled > 0 || !folded.is_empty() {
        guard.touch_gdm();
        guard.dirty = true;
    }
    info!(
        "FEED {} upsert: {} created, {} updated, {} held, {} unchanged, {} rejected",
        source.name,
        counts.created,
        counts.updated,
        counts.held,
        counts.unchanged,
        counts.rejected.len()
    );
    let snapshot = guard.flush_state();
    drop(guard);
    write_or_log(snapshot);
    counts
}

/// Seasons left as they were made from an unidentified first episode -- the local agent's
/// guid, or no poster -- under a show TMDB names (the production server, 2026-10-12: Black
/// Mirror's Season 6 on `local://` with the blank tile, its show re-identified in place the
/// day before). Run once at start: each is built again as a leaf of its show's identity
/// builds it, from TMDB's season and Plex's guid for it (both cached when its episodes were
/// identified), and refreshed with that (`refresh_season`). Answers how many changed.
pub(super) fn repair_seasons(shared: &SharedRuntime) -> usize {
    let present = |value: &&Value| !value.is_null() && value.as_str() != Some("");
    // -- under the lock: which seasons, and what names them ---------------------------------
    let (wanted, tmdb, plex) = {
        let guard = lock(shared);
        let Some(tmdb) = guard.tmdb.clone() else {
            return 0;
        };
        let wanted: Vec<(String, i64, i64, Option<String>, Map<String, Value>)> = guard
            .catalog
            .items()
            .iter()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some("season")
                    && row.get("_scanned").is_some_and(truthy)
                    && (row.get("guid").and_then(Value::as_str).is_none_or(|guid| guid.is_empty() || guid.starts_with("local://"))
                        || row.get("_poster_url").filter(present).is_none())
            })
            .filter_map(|season| {
                let key = key_of(season)?;
                let index = season.get("index").and_then(Value::as_i64)?;
                let show = guard.catalog.get(&season.get("parentRatingKey").map(string_value)?)?;
                let id = show.get("_identity").and_then(Value::as_str)?.strip_prefix("tmdb:")?.parse::<i64>().ok()?;
                // Plex's guid for the show, as the episodes' identification named it.
                let named = guard.catalog.children_refs(&key).into_iter().find_map(|episode| {
                    episode.pointer("/_identify/plex").and_then(Value::as_str).map(str::to_string)
                });
                let artwork = [("_show_poster", "_poster_url"), ("_show_art", "_art_url")]
                    .into_iter()
                    .filter_map(|(hint, field)| Some((hint.to_string(), show.get(field).filter(present)?.clone())))
                    .collect();
                Some((key, id, index, named, artwork))
            })
            .collect();
        (wanted, tmdb, guard.plex.clone())
    };
    if wanted.is_empty() {
        return 0;
    }
    // -- off the lock: the season each one's identity builds --------------------------------
    let mut children: HashMap<String, HashMap<String, String>> = HashMap::new();
    let built: Vec<(String, Value)> = wanted
        .into_iter()
        .filter_map(|(key, id, index, named, artwork)| {
            let mut leaf = artwork;
            for (field, value) in [("_kind", json!("tv")), ("_tmdb_id", json!(format!("tmdb:{id}"))), ("_season", json!(index)), ("_episode", json!(0))] {
                leaf.insert(field.to_string(), value);
            }
            if let Some(season) = tmdb.season(&json!(id), &json!(index)).filter(truthy) {
                if let Some(season) = season.as_object() {
                    crate::metadata::season_hints(&mut leaf, season);
                }
            }
            if let (Some(plex), Some(show)) = (plex.as_deref(), named) {
                let seasons = children.entry(show.clone()).or_insert_with(|| crate::identify::plex_children(plex, &show));
                if let Some(guid) = seasons.get(&index.to_string()) {
                    leaf.insert("_season_guid".to_string(), json!(guid));
                }
            }
            let season = crate::hierarchy::build(vec![Value::Object(leaf)])
                .into_iter()
                .find(|row| row.get("type").and_then(Value::as_str) == Some("season"))?;
            Some((key, season))
        })
        .collect();
    // -- under the lock: apply -------------------------------------------------------------
    let mut guard = lock(shared);
    let refreshed = built.iter().filter(|(key, season)| guard.refresh_season(key, season)).count();
    if refreshed > 0 {
        info!("FEED refreshed {refreshed} season(s) their show's identity names");
        let snapshot = guard.flush_state();
        drop(guard);
        write_or_log(snapshot);
    }
    refreshed
}

/// How long a row the retry left on the blank tile waits for its next try: an hour after
/// the first, doubling to about five days.
fn retry_after(attempts: i64) -> i64 {
    3600 << (attempts.clamp(1, 8) - 1)
}

/// The item a row's source pushed, rebuilt from the row: what `upsert` needs to identify it
/// again. Its probe is the row's already, so none is sent, and it stays in its library
/// unless it is a film whose release names an episode, which `feed_library` moves.
/// ponytail: the `library` a source named is not kept on the row, so such a film is filed
/// by location, as zurg's items are; a source naming its films' library itself would move
/// it back on its next push.
fn pushed_item(row: &Value) -> Option<(String, Value)> {
    let text = |field: &str| row.get(field).and_then(Value::as_str).filter(|value| !value.is_empty());
    let name = text("_release")?;
    let episode = text("type") == Some("movie") && crate::identify::parse_name(name).episode.is_some();
    let library = row.get("librarySectionID").map(string_value).filter(|_| !episode);
    Some((
        text("_source")?.to_string(),
        json!({"id": text("_sid")?, "path": text("_path")?, "file": row.pointer("/Media/0/Part/0/file")?,
               "name": name, "release": name, "size": row.pointer("/Media/0/Part/0/size").cloned().unwrap_or(json!(0)),
               "hash": row.get("_hash"), "ids": row.get("_ids").cloned().unwrap_or_else(|| json!({})),
               "library": library}),
    ))
}

/// Rows identification left on the blank tile -- a film with no poster, or an episode whose
/// show has none -- and films whose release names an episode, identified again as their
/// source pushed them, `limit` films or shows a pass. The feed names a row again only when its
/// source's ids change, so a lookup that failed once (TMDB or Plex down, a key missing) and a
/// title TMDB has learned since stayed blank for good: the production rex held 56 films and 45 shows
/// so on 2026-10-12. Each row still blank afterwards waits longer before its next try
/// (`retry_after`, kept on the row as `_retry`); what a person matched is never touched.
/// Answers how many rows it tried.
pub(super) fn retry_blank(shared: &SharedRuntime, limit: usize) -> usize {
    let stamp = now();
    // -- under the lock: which rows, a show's episodes together so it is named whole --------
    let (by_source, keys, sources) = {
        let guard = lock(shared);
        if guard.tmdb.is_none() && guard.plex.is_none() {
            return 0;
        }
        let mut groups: BTreeMap<String, Vec<(String, (String, Value))>> = BTreeMap::new();
        for row in guard.catalog.items() {
            let due = row.pointer("/_retry/at").and_then(Value::as_i64).is_none_or(|at| at <= stamp);
            if !due || !guard.blank(row) {
                continue;
            }
            let (Some(key), Some(item)) = (key_of(row), pushed_item(row)) else {
                continue;
            };
            let group = row.get("grandparentRatingKey").map(string_value).unwrap_or_else(|| key.clone());
            groups.entry(group).or_default().push((key, item));
        }
        let mut by_source: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        let mut keys = Vec::new();
        for (key, (source, item)) in groups.into_values().take(limit).flatten() {
            by_source.entry(source).or_default().push(item);
            keys.push(key);
        }
        (by_source, keys, guard.sources.clone())
    };
    if keys.is_empty() {
        return 0;
    }
    info!("FEED retrying {} row(s) left on the blank tile", keys.len());
    for (name, items) in &by_source {
        if let Some(source) = sources.iter().find(|source| source.name == *name) {
            // Feed requests apply one at a time; so does this.
            let _feed = FEED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            upsert(shared, source, items, None, true);
        }
    }
    // -- under the lock: the next try for what is still blank -------------------------------
    let mut guard = lock(shared);
    for key in &keys {
        let Some(row) = guard.catalog.get(key).cloned() else {
            continue;
        };
        let attempts = row.pointer("/_retry/n").and_then(Value::as_i64).unwrap_or(0) + 1;
        let blank = guard.blank(&row);
        if let Some(object) = guard.catalog.get_mut(key).and_then(Value::as_object_mut) {
            if blank {
                object.insert("_retry".to_string(), json!({"n": attempts, "at": stamp + retry_after(attempts)}));
            } else {
                object.shift_remove("_retry");
            }
        }
    }
    guard.dirty = true;
    let snapshot = guard.flush_state();
    drop(guard);
    write_or_log(snapshot);
    keys.len()
}

/// The ids a row was identified by: its `_ids`, or for a row the old scanner made, the
/// IMDb id its identification settled on.
fn row_ids(row: &Value) -> Map<String, Value> {
    if let Some(ids) = row.get("_ids").and_then(Value::as_object) {
        return ids.clone();
    }
    row.get("_identify")
        .and_then(|report| report.get("imdb"))
        .filter(|imdb| imdb.is_string())
        .map(|imdb| Map::from_iter([("imdb".to_string(), imdb.clone())]))
        .unwrap_or_default()
}

/// A row's releases as (source, source id, Media index): each Media's own tags on a row
/// that holds several, else the row's.
pub(super) fn versions(row: &Value) -> Vec<(String, String, usize)> {
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

/// The (source, source id) a tagged Media came from.
fn media_release(media: &Value) -> Option<(String, String)> {
    Some((media.get("_source")?.as_str()?.to_string(), media.get("_sid")?.as_str()?.to_string()))
}

/// The file a row's Media at `index` reads.
fn media_file(row: &Value, index: usize) -> Option<String> {
    row.pointer(&format!("/Media/{index}/Part/0/file")).and_then(Value::as_str).map(str::to_string)
}

/// A row's Media, the first tagged with the row's own source fields when none is.
pub(super) fn tagged_media(row: &Value) -> Vec<Value> {
    let mut media = row.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
    if media.iter().all(|media| media.get("_sid").is_none()) {
        if let Some(first) = media.first_mut().and_then(Value::as_object_mut) {
            for field in ["_source", "_sid", "_path"] {
                if let Some(value) = row.get(field) {
                    first.insert(field.to_string(), value.clone());
                }
            }
        }
    }
    media
}

/// Which title a row a source pushed is, for folding: its type, section, season, agent
/// guid and edition. None for an unmatched row (a `local://` guid is its own key's), one
/// in the trash or waiting for a probe, and a row no source pushed.
fn version_slot(row: &Value) -> Option<String> {
    let kind = row.get("type").and_then(Value::as_str).filter(|kind| matches!(*kind, "movie" | "episode"))?;
    if row.get("_sid").is_none() || row.get("deletedAt").is_some() || row.get("_probePending").is_some_and(truthy) {
        return None;
    }
    let guid = row.get("guid").and_then(Value::as_str).filter(|guid| !guid.is_empty() && !guid.starts_with("local://"))?;
    let field = |name: &str| row.get(name).map(string_value).unwrap_or_default();
    Some([kind, &field("librarySectionID"), &field("parentRatingKey"), guid, &field("editionTitle")].join("\u{1}"))
}

/// What identification wrote on a row, and the hierarchy fields placing it rewrites. A
/// re-identified row keeps everything else (its watch state, ratings, labels and
/// collections) and loses these, so nothing of the old identity lingers.
const IDENTITY_FIELDS: &[&str] = &[
    "title", "titleSort", "originalTitle", "summary", "year", "originallyAvailableAt", "tagline",
    "rating", "audienceRating", "ratingImage", "audienceRatingImage", "contentRating", "studio",
    "Genre", "Country", "Role", "Director", "Writer", "Producer", "Similar", "guid", "Guid", "slug",
    "index", "thumb", "art", "duration", "Media", "_show_external_guids", "_poster_url", "_art_url",
    "_identity", "_identify", "_tmdb_id", "_kind", "_season", "_episode", "_show_title",
    "_show_summary", "_show_year", "_show_guid", "_season_guid", "_generatedTitle", "_release",
    "_release_path", "_probePending", "_hash",
];

/// Whether a library folder names a directory the item's path sits in: `/mnt/zurg/movies`
/// and `/movies` both hold `movies/<release>/<file>`, whatever prefix the source's files
/// carry. zurg's default `mount_path` is empty, so its items come with no prefix at all and a
/// full-path match alone rejected every one against a seeded `/mnt/zurg/movies`.
fn names_directory(location: &str, path: &str) -> bool {
    let location = location.trim_end_matches('/');
    let segments: Vec<&str> = path.split('/').collect();
    (1..segments.len()).any(|count| {
        let directory = segments[..count].join("/");
        location == directory || location.ends_with(&format!("/{directory}"))
    })
}

impl Runtime {
    /// A row a person matched (Fix Match writes `_matched`), or an episode or season under a
    /// show they matched: what they chose wins over anything a source pushes later.
    pub(super) fn matched_by_hand(&self, row: &Value) -> bool {
        let by_hand = |row: &Value| row.get("_matched").is_some_and(truthy);
        by_hand(row)
            || ["parentRatingKey", "grandparentRatingKey"].iter().any(|field| {
                row.get(*field).map(string_value).and_then(|key| self.catalog.get(&key)).is_some_and(by_hand)
            })
    }

    /// A row its source pushed that draws the blank tile -- a film with no poster, or an
    /// episode whose show has none -- or a film whose release names an episode (zurg filed
    /// it under movies), unless a person matched or unmatched it.
    fn blank(&self, row: &Value) -> bool {
        let has = |row: Option<&Value>, field: &str| {
            row.and_then(|row| row.get(field)).is_some_and(|value| !value.is_null() && value.as_str() != Some(""))
        };
        let kind = row.get("type").and_then(Value::as_str);
        if !matches!(kind, Some("movie" | "episode"))
            || row.get("_sid").is_none()
            || row.get("deletedAt").is_some()
            || row.get("_probePending").is_some_and(truthy)
            || versions(row).len() > 1
            || self.matched_by_hand(row)
            || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("tv.plex.agents.none://"))
        {
            return false;
        }
        if kind == Some("episode") {
            let show = row.get("grandparentRatingKey").map(string_value).and_then(|show| self.catalog.get(&show));
            return !has(show, "_poster_url");
        }
        !has(Some(row), "_poster_url")
            || row.get("_release").and_then(Value::as_str).is_some_and(|name| crate::identify::parse_name(name).episode.is_some())
    }

    fn section_title(&self, key: &str) -> String {
        self.section(key).map(|section| string_value(section.get("title").unwrap_or(&Value::Null))).unwrap_or_default()
    }

    /// The library an item belongs in: the section it names, else the movie or show
    /// library (of its type, when it names one) whose `Location` holds its file, else the
    /// first library of that type.
    fn feed_library(&self, item: &Item) -> Option<Library> {
        let library = |section: &Value| Library {
            key: string_value(section.get("key").unwrap_or(&Value::Null)),
            kind: section.get("type").and_then(Value::as_str).unwrap_or_default().to_string(),
            title: string_value(section.get("title").unwrap_or(&Value::Null)),
            tmdb: self.tmdb.clone(),
            plex: self.plex.clone(),
        };
        let playable = |section: &&Value| matches!(section.get("type").and_then(Value::as_str), Some("movie" | "show"));
        let sections = self.catalog.sections.iter().filter(playable);
        let wanted = item.library.as_deref();
        if let Some(key) = wanted.filter(|wanted| !matches!(*wanted, "movie" | "show")) {
            return self.catalog.sections.iter().filter(playable).find(|section| section.get("key").map(string_value).as_deref() == Some(key)).map(library);
        }
        let of_kind: Vec<&Value> =
            sections.filter(|section| wanted.is_none() || section.get("type").and_then(Value::as_str) == wanted).collect();
        let holds = |section: &Value| {
            self.locations(section).into_iter().find(|location| under(&item.file, location)).or_else(|| {
                self.locations(section).into_iter().find(|location| names_directory(location, &item.path))
            })
        };
        let found = of_kind
            .iter()
            .find(|section| self.locations(section).iter().any(|location| under(&item.file, location)))
            .or_else(|| of_kind.iter().find(|section| self.locations(section).iter().any(|location| names_directory(location, &item.path))))
            .or_else(|| wanted.and(of_kind.first()))?;
        // zurg files a release by its own name rules, and the production server's Usenet zurg filed 40 anime
        // episodes under movies on 2026-10-12 (`Detektiv.Conan.E0221…`, `[Moozzi2]
        // Eighty-Six-15 […]`): each became a film nothing matched, on the blank tile. A
        // release whose own name carries an episode number goes to a show library, the one
        // beside that movie library's folder when there is one. Only the release's name: a
        // film's extras (`… - 2011 documentary.mkv` in A Trip to the Moon) are not held to it.
        let episode = || crate::identify::parse_name(&item.name).episode.is_some();
        if wanted.is_none() && found.get("type").and_then(Value::as_str) == Some("movie") && episode() {
            let parent = |location: &str| location.trim_end_matches('/').rsplit_once('/').map(|(parent, _)| parent.to_string());
            let beside = holds(found).and_then(|location| parent(&location));
            let shows: Vec<&Value> =
                self.catalog.sections.iter().filter(|section| section.get("type").and_then(Value::as_str) == Some("show")).collect();
            if let Some(show) = shows
                .iter()
                .find(|section| beside.is_some() && self.locations(section).iter().any(|location| parent(location) == beside))
                .or(shows.first())
            {
                return Some(library(show));
            }
        }
        Some(library(found))
    }

    /// This source's rows, by the source's id.
    fn feed_rows(&self, source: &str) -> HashMap<String, String> {
        self.catalog
            .items()
            .iter()
            .flat_map(|row| {
                let key = key_of(row);
                versions(row).into_iter().filter(|(of, _, _)| of == source).filter_map(move |(_, sid, _)| Some((sid, key.clone()?)))
            })
            .collect()
    }

    /// Rows no feed has claimed (what the WebDAV scanner made), by their Part's file.
    fn legacy_rows_by_file(&self) -> HashMap<String, String> {
        self.catalog
            .items()
            .iter()
            .filter(|row| row.get("_sid").is_none() && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")))
            .filter_map(|row| Some((row_files(row).into_iter().next()?, key_of(row)?)))
            .collect()
    }

    /// Rows the old WebDAV scanner made from this source's files that no item has adopted
    /// yet, not in the trash: what a first snapshot must also account for.
    fn unadopted_rows(&self, source: &Source) -> Vec<String> {
        self.catalog
            .items()
            .iter()
            .filter(|row| {
                row.get("_sid").is_none()
                    && row.get("_scanned").is_some_and(truthy)
                    && row.get("deletedAt").is_none()
                    && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
                    && row_files(row).first().is_some_and(|file| source.path_under_mount(file).is_some())
            })
            .filter_map(key_of)
            .collect()
    }

    /// Every id the source has here that a snapshot must name: shown rows not in the
    /// trash, and held items.
    fn feed_ids(&self, source: &str) -> Vec<String> {
        let prefix = held_key(source, "");
        self.catalog
            .items()
            .iter()
            .filter(|row| row.get("deletedAt").is_none())
            .flat_map(|row| versions(row).into_iter().filter(|(of, _, _)| of == source).map(|(_, sid, _)| sid))
            .chain(self.feed_held.keys().filter_map(|key| key.strip_prefix(&prefix).map(str::to_string)))
            .collect()
    }

    fn feed_listing(&self, source: &Source) -> Value {
        let items: Vec<Value> = self
            .catalog
            .items()
            .iter()
            .flat_map(|row| {
                versions(row).into_iter().filter(|(of, _, _)| *of == source.name).map(move |(_, sid, index)| {
                    json!({"id": sid, "ratingKey": key_of(row), "file": media_file(row, index),
                           "deleted": row.get("deletedAt").is_some()})
                })
            })
            .collect();
        json!({"size": items.len(), "items": items})
    }

    /// Hold an item, recording its key for the next snapshot's keyed write.
    pub(super) fn hold(&mut self, key: String, record: Value) {
        self.feed_held_changed.insert(key.clone());
        self.feed_held.insert(key, record);
    }

    pub(super) fn release(&mut self, key: &str) -> Option<Value> {
        let released = self.feed_held.remove(key);
        if released.is_some() {
            self.feed_held_changed.insert(key.to_string());
        }
        released
    }

    fn feed_held_listing(&self, source: &Source) -> Value {
        let prefix = held_key(&source.name, "");
        let held: Vec<Value> = self
            .feed_held
            .iter()
            .filter(|(key, _)| key.starts_with(&prefix))
            .map(|(_, record)| {
                json!({"id": record.get("id"), "path": record.get("path"), "name": record.get("name"),
                       "size": record.get("size"), "since": record.get("since")})
            })
            .collect();
        json!({"size": held.len(), "held": held})
    }

    /// An existing row and a push of its id: adopt it for the source, restore it from the
    /// trash, follow a moved file and a new size, and queue a probe it was waiting for.
    /// Answers whether anything changed.
    fn feed_update_row(&mut self, key: &str, source: &str, item: &Item, adopt: bool, probed: &mut Vec<(String, Value)>) -> bool {
        let mut changed = false;
        if self.catalog.get(key).is_some_and(|row| row.get("deletedAt").is_some()) {
            // Measured (6a2): the file back before the trash was emptied is the same row.
            self.untrash_row(key);
            changed = true;
        }
        let Some(row) = self.catalog.get(key).cloned() else {
            return changed;
        };
        if adopt {
            let ids = row_ids(&row);
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("_source".to_string(), json!(source));
                object.insert("_sid".to_string(), json!(item.id));
                object.insert("_ids".to_string(), Value::Object(ids));
            }
            info!("FEED {source} adopted {key} for {}", item.id);
            changed = true;
        }
        let index = versions(&row).into_iter().find(|(of, sid, _)| of == source && *sid == item.id).map_or(0, |(_, _, index)| index);
        let tagged = row.pointer(&format!("/Media/{index}/_sid")).is_some();
        let path = if tagged { row.pointer(&format!("/Media/{index}/_path")) } else { row.get("_path") };
        if path.and_then(Value::as_str) != Some(item.path.as_str()) {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                if tagged {
                    if let Some(media) = object.get_mut("Media").and_then(|media| media.get_mut(index)).and_then(Value::as_object_mut) {
                        media.insert("_path".to_string(), json!(item.path));
                    }
                }
                if index == 0 {
                    object.insert("_path".to_string(), json!(item.path));
                }
            }
            changed = true;
        }
        let part = |row: &Value| -> Option<Value> {
            row.get("Media")?.as_array()?.get(index)?.get("Part")?.as_array()?.first().cloned()
        };
        let current = part(&row).unwrap_or_default();
        let moved = current.get("file").and_then(Value::as_str) != Some(item.file.as_str());
        let resized = item.size > 0 && current.get("size").and_then(Value::as_u64) != Some(item.size);
        if moved || resized {
            if let Some(part) = self
                .catalog
                .get_mut(key)
                .and_then(|row| row.get_mut("Media"))
                .and_then(Value::as_array_mut)
                .and_then(|media| media.get_mut(index))
                .and_then(|media| media.get_mut("Part"))
                .and_then(Value::as_array_mut)
                .and_then(|parts| parts.first_mut())
                .and_then(Value::as_object_mut)
            {
                if moved {
                    part.insert("file".to_string(), json!(item.file));
                }
                if resized {
                    part.insert("size".to_string(), json!(item.size));
                }
            }
            changed = true;
        }
        if row.get("_probePending").is_some_and(truthy) {
            if let Some(probe) = &item.media_info {
                probed.push((key.to_string(), probe.clone()));
                changed = true;
            }
        }
        changed
    }

    /// A row whose source now names it by an id it was not identified by: identified
    /// again on the same ratingKey. A movie is rewritten in place; an episode is placed
    /// under the show its new identity names, and the show and season it leaves go when
    /// they are left empty. What a person did to the row stays (`IDENTITY_FIELDS`).
    fn reidentify(&mut self, library: &Library, key: &str, arrival: Arrival, moving: &HashMap<String, String>) -> bool {
        let Some(old) = self.catalog.get(key).cloned() else {
            return false;
        };
        let meta = arrival.meta.clone().unwrap_or_default();
        // A row whose id still names nothing gets no guid from identification and keeps
        // its own: nothing changed, so it is not placed again on every push -- unless its
        // library changed, or the same identity now brings artwork its tile lacks (TMDB had
        // no poster yet for Camp CrunchLabs when the production rex first asked, 2026-10-12).
        let guid = meta.get("guid").or(old.get("guid").filter(|guid| guid.as_str().is_some_and(|guid| guid.starts_with("local://"))));
        let moved = old.get("librarySectionID").map(string_value).as_deref() != Some(library.key.as_str());
        let has = |row: Option<&Value>, field: &str| {
            row.and_then(|row| row.get(field)).is_some_and(|value| !value.is_null() && value.as_str() != Some(""))
        };
        let parent = |field: &str| old.get(field).map(string_value).and_then(|key| self.catalog.get(&key));
        let brought = Value::Object(meta.clone());
        // A show's poster draws its seasons' too, but only the episodes that brought it would
        // place their season again: Wallace & Gromit's Specials stayed blank in a rehearsal
        // on the production rex's rows once Season 1's episodes had given the show Plex's poster.
        let gains_art = if old.get("type").and_then(Value::as_str) == Some("episode") {
            (!has(parent("grandparentRatingKey"), "_poster_url") || !has(parent("parentRatingKey"), "_poster_url"))
                && (has(Some(&brought), "_show_poster") || has(Some(&brought), "_season_poster"))
        } else {
            !has(Some(&old), "_poster_url") && has(Some(&brought), "_poster_url")
        };
        if meta.get("_identity") == old.get("_identity") && guid == old.get("guid") && !moved && !gains_art {
            return false;
        }
        let Some(mut leaf) = self.leaf_for(library, &arrival, Some(key.to_string())) else {
            return false;
        };
        let pending = old.get("_probePending").is_some_and(truthy);
        if !pending {
            // The file is the same file: its Media, ids and part URL stay.
            for field in ["Media", "duration"] {
                if let Some(value) = old.get(field) {
                    leaf[field] = value.clone();
                }
            }
            if let Some(object) = leaf.as_object_mut() {
                object.shift_remove("_probePending");
            }
        }
        if let Some(added) = old.get("addedAt") {
            leaf["addedAt"] = added.clone();
        }
        let (season, show) = (
            old.get("parentRatingKey").map(string_value),
            old.get("grandparentRatingKey").map(string_value),
        );
        if let Some(show) = show.as_deref() {
            self.reidentify_show(library, show, &leaf, moving);
        }
        self.catalog.remove(key);
        self.place_leaves(library, vec![leaf], &HashSet::from([key.to_string()]));
        if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in old.as_object().into_iter().flatten() {
                let placed = field.starts_with("parent") || field.starts_with("grandparent") || field.starts_with("librarySection");
                if !placed && !IDENTITY_FIELDS.contains(&field.as_str()) && !row.contains_key(field) {
                    row.insert(field.clone(), value.clone());
                }
            }
        }
        for parent in [season, show].into_iter().flatten() {
            self.drop_if_empty(&parent);
        }
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 5, None, None, None, None);
        }
        info!(
            "FEED re-identified {key}: {} -> {}",
            old.get("_identity").map(string_value).unwrap_or_default(),
            meta.get("_identity").map(string_value).unwrap_or_default()
        );
        true
    }

    /// zurg's matcher keeps the SHOW's key for an episode's release, so a show whose
    /// episodes all move to one new identity, which no other show here has, is that show
    /// re-identified: it takes the new identity's fields on its own key (its seasons keep
    /// theirs, found again by index), as Fix Match re-identifies a show in place.
    fn reidentify_show(&mut self, library: &Library, show: &str, leaf: &Value, moving: &HashMap<String, String>) {
        let Some(identity) = leaf.get("_identity").map(string_value) else {
            return;
        };
        let taken = self.catalog.items().iter().any(|row| {
            row.get("type").and_then(Value::as_str) == Some("show")
                && string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == library.key
                && row.get("_identity").map(string_value).as_deref() == Some(identity.as_str())
        });
        let leaves = self.leaves(show);
        let all = !leaves.is_empty()
            && leaves.iter().all(|row| key_of(row).and_then(|key| moving.get(&key)) == Some(&identity));
        if taken || !all {
            return;
        }
        let built = crate::hierarchy::build(vec![leaf.clone()]);
        let Some(fresh) = built.iter().find(|row| row.get("type").and_then(Value::as_str) == Some("show")) else {
            return;
        };
        if let Some(row) = self.catalog.get_mut(show).and_then(Value::as_object_mut) {
            for field in [
                "title", "summary", "year", "guid", "Guid", "contentRating", "studio", "Genre", "Country",
                "Role", "rating", "audienceRating", "tagline", "_poster_url", "_art_url",
            ] {
                match fresh.get(field).filter(|value| !value.is_null()) {
                    Some(value) => {
                        row.insert(field.to_string(), value.clone());
                    }
                    None => {
                        row.shift_remove(field);
                    }
                }
            }
            row.insert("_identity".to_string(), json!(identity));
            // A show nobody identified has the local agent's guid on its own key; `build`
            // copied the episode's up.
            if row.get("guid").and_then(Value::as_str).is_none_or(|guid| guid.starts_with("local://")) {
                row.insert("guid".to_string(), json!(format!("local://{show}")));
            }
        }
        info!("FEED re-identified show {show} in place -> {identity}");
    }

    /// Rows a source pushed that are one title -- the same agent guid in the same section
    /// (an episode: the same season) -- made one row holding each release as a Media, as a
    /// real PMS's scanner and agent make several files of one movie one item. The row
    /// someone watched survives, else the oldest. `only` limits it to slots holding one of
    /// those keys. Answers the keys folded away.
    pub(super) fn fold_versions(&mut self, only: Option<&HashSet<String>>) -> HashSet<String> {
        let mut slots: BTreeMap<String, Vec<(bool, i64, String)>> = BTreeMap::new();
        for row in self.catalog.items() {
            if let (Some(slot), Some(key)) = (version_slot(row), key_of(row)) {
                let watched = ["viewCount", "viewOffset", "lastViewedAt", "userRating"].iter().any(|field| row.get(*field).is_some_and(truthy));
                slots.entry(slot).or_default().push((!watched, key.parse().unwrap_or(i64::MAX), key));
            }
        }
        let mut folded = HashSet::new();
        let mut gone = Vec::new();
        for (_, mut rows) in slots {
            if rows.len() < 2 || only.is_some_and(|only| !rows.iter().any(|(_, _, key)| only.contains(key))) {
                continue;
            }
            rows.sort();
            let survivor = rows[0].2.clone();
            let mut media = self.catalog.get(&survivor).map(tagged_media).unwrap_or_default();
            for (_, _, other) in &rows[1..] {
                let Some(row) = self.catalog.get(other).cloned() else {
                    continue;
                };
                // A save stopped between the survivor's write and the others' deletes
                // brings an absorbed row back: its release is already a Media here.
                let held: HashSet<(String, String)> = media.iter().filter_map(media_release).collect();
                media.extend(tagged_media(&row).into_iter().filter(|version| media_release(version).is_none_or(|release| !held.contains(&release))));
                info!("FEED folded {other} into {survivor} as another version");
                folded.insert(other.clone());
                gone.push(row);
            }
            if let Some(object) = self.catalog.get_mut(&survivor).and_then(Value::as_object_mut) {
                object.insert("Media".to_string(), Value::Array(media));
            }
            if let Some(row) = self.catalog.get(&survivor).cloned() {
                self.push_timeline_entry(&row, 5, None, None, None, None);
            }
        }
        if folded.is_empty() {
            return folded;
        }
        // Movies and episodes have no rows beneath them: retired the way `retire_row`
        // retires one, with one reindex for them all rather than one each.
        self.catalog.remove_many(&folded);
        for row in &gone {
            if let Some(key) = key_of(row) {
                self.forget_row(&key);
            }
            self.push_timeline_entry(row, 9, Some("deleted"), None, None, None);
        }
        self.drop_from_queues(&folded.iter().cloned().collect::<Vec<_>>());
        self.dirty = true;
        folded
    }

    /// One release of a row holding several gone: its Media dropped, the row's own
    /// source fields its new first Media's. Answers false for a row of one release.
    pub(super) fn drop_version(&mut self, key: &str, source: &str, sid: &str) -> bool {
        let Some(row) = self.catalog.get(key) else {
            return false;
        };
        let all = versions(row);
        let Some(index) = all.iter().find(|(of, id, _)| of == source && id == sid).map(|(_, _, index)| *index).filter(|_| all.len() > 1) else {
            return false;
        };
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            if let Some(media) = object.get_mut("Media").and_then(Value::as_array_mut) {
                media.remove(index);
            }
            let first = object.get("Media").and_then(|media| media.get(0)).cloned().unwrap_or_default();
            for field in ["_source", "_sid", "_path"] {
                if let Some(value) = first.get(field) {
                    object.insert(field.to_string(), value.clone());
                }
            }
            if let Some(duration) = first.get("duration") {
                object.insert("duration".to_string(), duration.clone());
            }
        }
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 5, None, None, None, None);
        }
        info!("FEED {source} dropped {sid} from {key}");
        true
    }

    /// A show or season a re-identified episode left with no children: deleted, its key
    /// retired, announced in `state 9` as a deleted row is.
    fn drop_if_empty(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        if !self.catalog.children(key).is_empty() {
            return;
        }
        self.catalog.remove(key);
        self.deleted.insert(key.to_string());
        if let Ok(number) = key.parse::<i64>() {
            self.retired_keys.push(number);
        }
        self.push_timeline_entry(&row, 9, Some("deleted"), None, None, None);
    }
}
