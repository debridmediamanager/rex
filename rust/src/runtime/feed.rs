//! The feed: sources push their items here, and rex lists or queries no source.
//!
//! `docs/feed-api.md` is the reference. A request is admitted from any peer on a private
//! network (no token, never the Plex gate; `source_peer`), parsed, classified under the
//! runtime lock, identified off it (Plex's matcher is a network lookup), and
//! applied under it again, then saved before it is answered. Feed requests apply one at a
//! time (`FEED`), which is also what keeps a snapshot's chunks in order. A source pushes
//! under the name it registered with `PUT /rex/sources/{name}` (`sources`).
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

/// The code each `DELETE /rex/sources/{name}` answered, until it is confirmed or expires.
/// In memory only: a restart forgets every pending delete.
static DELETES: LazyLock<Mutex<HashMap<String, (String, std::time::Instant)>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

/// How long a source delete's code confirms it, in seconds.
const DELETE_WINDOW: u64 = 300;

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
    /// Where the provider lists the file inside its release; `""` at its top level, `None`
    /// when the source does not say.
    folder: Option<String>,
    /// The `extraType` of a file its source pushed as an extra (`kind: "extra"`,
    /// `scan::pushed_extra_type`): a clip of its release's title, never an episode or a film.
    extra: Option<i64>,
    /// Where the source placed the file, kept on the row as `_placed`: its `kind` ("movie" or
    /// "episode") and an episode's TMDB `season`, `episode` and `episode_end`. `None` when it
    /// sends no `kind` rex knows: nothing places the file, and its name is not read.
    placed: Option<Value>,
    /// The season and episode a show's file its source did not number is on: its row's, when it
    /// has one, else the next in its release's file order (`number_unplaced`). Set before it is
    /// identified, which reads them.
    kept: Option<(i64, i64)>,
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
        let number = |key: &str, least: i64| {
            value.get(key).and_then(|n| n.as_i64().or_else(|| n.as_str()?.trim().parse().ok())).filter(|n| *n >= least)
        };
        let placed = text("kind").filter(|kind| matches!(kind.as_str(), "movie" | "episode")).map(|kind| {
            let mut placed = Map::from_iter([("kind".to_string(), json!(kind))]);
            if let (true, Some(season), Some(episode)) = (kind == "episode", number("season", 0), number("episode", 1)) {
                placed.insert("season".to_string(), json!(season));
                placed.insert("episode".to_string(), json!(episode));
                if let Some(end) = number("episode_end", episode + 1) {
                    placed.insert("episode_end".to_string(), json!(end));
                }
            }
            Value::Object(placed)
        });
        Ok(Self {
            placed,
            extra: (text("kind").as_deref() == Some("extra")).then(|| super::scan::pushed_extra_type(text("extra_type").as_deref())),
            kept: None,
            library: value.get("library").map(string_value).filter(|v| !v.is_empty() && v != "null"),
            size: value.get("size").and_then(Value::as_u64).unwrap_or(0),
            hash: text("hash").map(|hash| hash.to_ascii_lowercase()),
            tmdb: id_text("tmdb"),
            folder: value.get("folder").and_then(Value::as_str).map(|folder| folder.trim_matches('/').to_string()),
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
            ]
            .into_iter()
            .chain(self.folder.as_ref().map(|folder| ("_folder".to_string(), json!(folder))))
            .chain(self.placed.as_ref().map(|placed| ("_placed".to_string(), placed.clone())))),
            meta: None,
            numbers: super::scan::placed_numbers(self.placed.as_ref()).or(self.kept),
        }
    }

    /// The `kind` its source placed it as, if any.
    fn kind(&self) -> Option<&str> {
        self.placed.as_ref()?.get("kind")?.as_str()
    }

    /// Whether anything names it in `library` (`scan::identified_by`): a placement with ids, or a
    /// TMDB id set by hand.
    fn identifiable(&self, library: &str) -> bool {
        super::scan::identified_by(Some(&self.ids), self.placed.as_ref(), library).is_some()
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
    /// A file its source pushed as an extra: a clip of the title its release's other files are.
    Extra(i64),
}

fn held_key(source: &str, id: &str) -> String {
    format!("{source}:{id}")
}

/// A peer the source API answers, by its socket address alone (never `X-Forwarded-For` or
/// `Host`): this host, the unclaimed gate's private networks, Tailscale's 100.64/10 and
/// link-local, IPv4-mapped ones included.
pub(super) fn source_peer(peer: std::net::IpAddr) -> bool {
    let peer = peer.to_canonical();
    peer.is_loopback()
        || private_address(peer)
        || ["100.64.0.0/10", "169.254.0.0/16", "fe80::/10"]
            .into_iter()
            .filter_map(network)
            .any(|(net, bits)| in_network(peer, net, bits))
}

fn lock(shared: &SharedRuntime) -> std::sync::MutexGuard<'_, Runtime> {
    shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn listing(listed: Vec<(String, Option<String>, Option<String>, bool)>) -> Value {
    let items: Vec<Value> = listed
        .into_iter()
        .map(|(id, key, file, deleted)| json!({"id": id, "ratingKey": key, "file": file, "deleted": deleted}))
        .collect();
    json!({"size": items.len(), "items": items})
}

fn answer(status: u16, body: Value) -> Response {
    Response::json(status, body)
}

/// `/rex/feed/{source}/{items|snapshot|held}` and `/rex/sources[/{name}]`, outside the Plex
/// gate and open to any peer on a private network: a feed token per source, set by hand on
/// both sides, guarded it before, and the unclaimed gate's check refused sources that name rex
/// by host name (a Docker service) or reach it over Tailscale. This one reads the peer alone.
pub(super) fn handle(shared: &SharedRuntime, request: &Request, path: &str) -> Response {
    // rex-direct has rex's port forwarded from the internet and the feed takes no token.
    if CONNECTION.with(std::cell::Cell::get).is_some_and(|(peer, _)| !source_peer(peer)) {
        return answer(403, json!({"error": "the source API only answers private networks"}));
    }
    if let Some(rest) = path.strip_prefix("/rex/sources") {
        return sources(shared, request, rest.trim_matches('/'));
    }
    let rest = path.trim_start_matches("/rex/feed/").trim_end_matches('/');
    let (name, leaf) = rest.split_once('/').unwrap_or((rest, ""));
    // The source is looked up once this request holds the feed lock: a confirmed delete
    // removes it holding that lock, and a push waiting behind it must get the 404 that makes
    // zurg register again, not apply to a source that is gone.
    let mut sessions = FEED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(source) = lock(shared).sources.iter().find(|source| source.name == name).cloned() else {
        return answer(404, json!({"error": format!("no such source: PUT /rex/sources/{name} first")}));
    };
    let body = || -> Result<Value, Response> {
        serde_json::from_slice::<Value>(&request.body)
            .ok()
            .filter(Value::is_object)
            .ok_or_else(|| answer(400, json!({"error": "the body must be a JSON object"})))
    };
    let response = match (request.method.as_str(), leaf) {
        ("GET" | "HEAD", "items") => {
            // Its own statement, so the runtime lock is let go before 40,000 entries are built
            // and written: built under it, rex-fun's RD listing held a Plex request 0.6-2 s.
            let listed = lock(shared).feed_listed(&source);
            answer(200, listing(listed))
        }
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

/// A feed or sources path as the log shows it: a delete's confirmation code is a credential
/// for 300 s, and the log is read by more people than may delete a source.
pub(super) fn logged_path(path: &str) -> String {
    match path.split_once("/confirm/") {
        Some((head, _)) => format!("{head}/confirm/***"),
        None => path.to_string(),
    }
}

/// `GET /rex/sources`, `GET /rex/sources/{name}`, `PUT /rex/sources/{name}`: what a part
/// read streams from. A PUT creates or replaces the source -- zurg sends it again at the
/// start of every push session -- and the next part read uses it; credentials are kept in
/// the state file and never answered. `DELETE /rex/sources/{name}` and its confirmation
/// remove a source and everything it pushed (`ask_delete`, `confirm_delete`).
fn sources(shared: &SharedRuntime, request: &Request, name: &str) -> Response {
    match (request.method.as_str(), name) {
        ("DELETE", _) => match name.split_once("/confirm/") {
            Some((name, code)) => confirm_delete(shared, name, code),
            None => ask_delete(shared, name),
        },
        ("GET" | "HEAD", "") => {
            let listed: Vec<Value> = lock(shared).sources.iter().map(|source| source.public()).collect();
            answer(200, json!({"size": listed.len(), "sources": listed}))
        }
        ("GET" | "HEAD", _) => match lock(shared).sources.iter().find(|source| source.name == name) {
            Some(source) => answer(200, source.public()),
            None => answer(404, json!({"error": "no such source"})),
        },
        ("PUT", _) if !name.is_empty() => {
            let body = serde_json::from_slice::<Value>(&request.body).unwrap_or(Value::Null);
            let source = match Source::register(name, &body) {
                Ok(source) => source,
                Err(error) => return answer(400, json!({"error": error})),
            };
            let public = source.public();
            let mut guard = lock(shared);
            let changed = guard.sources.iter().find(|known| known.name == name).map(|known| &known.record) != Some(&source.record);
            let declared = source.clone();
            if changed {
                info!("SOURCES {name} registered at {}", public["url"].as_str().unwrap_or_default());
                guard.put_source(source);
                guard.dirty = true;
            }
            guard.declare_libraries(&declared);
            let snapshot = guard.flush_state();
            drop(guard);
            write_or_log(snapshot);
            answer(200, public)
        }
        _ => answer(405, json!({"error": "method not allowed"})),
    }
}

/// `DELETE /rex/sources/{name}` deletes nothing: it answers how many items the source has
/// here and the one path that removes them, valid for `DELETE_WINDOW` seconds. A source's
/// whole library goes with one request, and a stray or replayed DELETE must not be that
/// request. A new ask replaces the source's earlier code.
fn ask_delete(shared: &SharedRuntime, name: &str) -> Response {
    let items = {
        let guard = lock(shared);
        if !guard.sources.iter().any(|source| source.name == name) {
            return answer(404, json!({"error": "no such source"}));
        }
        guard.source_items(name).iter().map(|(_, sids, _)| sids.len()).sum::<usize>()
            + guard.feed_held.keys().filter(|key| key.starts_with(&held_key(name, ""))).count()
    };
    let mut bytes = [0_u8; 16];
    if let Err(error) = crate::platform::random_bytes(&mut bytes) {
        return answer(500, json!({"error": format!("no random source: {error}")}));
    }
    let code: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    let until = std::time::Instant::now() + std::time::Duration::from_secs(DELETE_WINDOW);
    DELETES.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).insert(name.to_string(), (code.clone(), until));
    info!("SOURCES {name}: delete asked for, {items} item(s); confirm within {DELETE_WINDOW} s");
    answer(200, json!({"confirm": format!("/rex/sources/{name}/confirm/{code}"), "items": items, "expires_in": DELETE_WINDOW}))
}

/// `DELETE /rex/sources/{name}/confirm/{code}`: the code `ask_delete` answered for this
/// source, unexpired and not used before, removes the source and every item it pushed from
/// rex -- never the files at the source. Any other code is a 404 that removes nothing.
fn confirm_delete(shared: &SharedRuntime, name: &str, code: &str) -> Response {
    let confirmed = {
        let mut deletes = DELETES.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let (confirmed, spent) = confirms(deletes.get(name), code, std::time::Instant::now());
        if spent {
            deletes.remove(name);
        }
        confirmed
    };
    if !confirmed {
        warning!("SOURCES {name}: a delete confirmation with no matching code removed nothing");
        return answer(404, json!({"error": format!("no delete to confirm with this code: ask again with DELETE /rex/sources/{name}")}));
    }
    // Feed requests apply one at a time; the source's open snapshot ends with it.
    let mut sessions = FEED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    sessions.remove(name);
    let mut guard = lock(shared);
    let removed = guard.delete_source(name);
    let snapshot = guard.flush_state();
    drop(guard);
    drop(sessions);
    write_or_log(snapshot);
    answer(200, json!({"deleted": true, "removed": removed}))
}

/// Whether `code` confirms a source's pending delete at `now`, and whether that pending
/// delete is spent: by its use (single use) or its expiry. A wrong code spends nothing.
fn confirms(pending: Option<&(String, std::time::Instant)>, code: &str, now: std::time::Instant) -> (bool, bool) {
    match pending {
        None => (false, false),
        Some((_, until)) if now >= *until => (false, true),
        // Compared without an early exit.
        Some((known, _)) => {
            let same = known.len() == code.len() && known.bytes().zip(code.bytes()).fold(0, |diff, (a, b)| diff | (a ^ b)) == 0;
            (same, same)
        }
    }
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
    let rows = guard.feed_rows(&source.name, ids.iter().map(String::as_str));
    let (mut trashed, mut deleted, mut released, mut unknown) = (0, 0, 0, 0);
    let mut sections = BTreeMap::new();
    let mut doomed = Vec::new();
    let mut seen = HashSet::new();
    for id in ids {
        if !seen.insert(id) {
            continue;
        }
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
        let rows = guard.feed_rows(&source.name, items.iter().map(|item| item.id.as_str()));
        let legacy = guard.legacy_rows_by_file(items.iter().map(|item| item.file.as_str()));
        let named = guard.plex.is_some();
        for mut item in items {
            // A `kind` the source sent says which it is (`feed_library`); no name guesses.
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
                    // An id that named nothing when it came (Plex unreachable, no token) is tried
                    // again on each push.
                    let unnamed = row.and_then(|row| row.get("_identify")).is_some_and(|report| {
                        ["tmdb", "plex"].iter().all(|field| report.get(*field).is_none_or(Value::is_null))
                    });
                    // A file whose library changed is placed again in the one it belongs to now.
                    let moved = row.is_some_and(|row| {
                        row.get("librarySectionID").map(string_value).as_deref() != Some(library.key.as_str())
                    });
                    // Placed somewhere else than before, it is placed again, providers or none. A
                    // push with no placement leaves it where the last one put it (absent is
                    // unknown), unless a TMDB id set by hand names it again.
                    let replaced = item.placed.is_some() && row.is_some_and(|row| version_placed(row, 0) != item.placed.as_ref());
                    // What a person matched is theirs: no push and no retry identifies it again.
                    // A clip is its parent's: no library, so never "moved", and no title of its own.
                    let reidentify = !row.is_some_and(|row| guard.matched_by_hand(row) || row.get("type").and_then(Value::as_str) == Some("clip"))
                        && row.is_some_and(|row| versions(row).len() < 2)
                        && (moved
                            || replaced
                            || named
                                && item.identifiable(&library.kind)
                                && (retry
                                    || match (&item.imdb, &item.tmdb) {
                                        (Some(imdb), _) => unnamed || had.get("imdb").map(string_value).as_deref() != Some(imdb.as_str()),
                                        (None, Some(tmdb)) => unnamed || had.get("tmdb").map(string_value).as_deref() != Some(tmdb.as_str()),
                                        _ => false,
                                    }));
                    // Nothing numbered it: it keeps the numbers it has, and is identified on them
                    // (a row is renumbered by its source alone).
                    if super::scan::placed_numbers(item.placed.as_ref()).is_none() {
                        item.kept = row.filter(|row| row.get("type").and_then(Value::as_str) == Some("episode")).and_then(|row| {
                            Some((row.get("parentIndex")?.as_i64()?, row.get("index")?.as_i64()?))
                        });
                    }
                    Plan::Row { key, adopt, reidentify }
                }
                // A retry names rows that are here; it never makes or holds one.
                None if retry => continue,
                None if item.media_info.is_some() => match item.extra {
                    Some(extra_type) => Plan::Extra(extra_type),
                    None => Plan::Show,
                },
                None => Plan::Hold,
            };
            plans.push((item, library, plan));
        }
        number_unplaced(&guard, &mut plans);
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
        name_release(library, &mut release);
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
    let mut extras = Vec::new();
    for (index, (item, library, plan)) in plans.into_iter().enumerate() {
        let held = held_key(&source.name, &item.id);
        match plan {
            Plan::Extra(extra_type) => {
                guard.release(&held);
                extras.push((item, library, extra_type));
            }
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
                    if guard.plex.is_some() {
                        if let Some(row) = guard.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                            row.insert("_ids".to_string(), Value::Object(item.ids.clone()));
                        }
                    }
                    // The placement it now sits on, also where it already sat there and nothing
                    // moved: without it every push placed it again and no pass knew it placed.
                    if let (Some(placed), Some(row)) = (&item.placed, guard.catalog.get_mut(&key).and_then(Value::as_object_mut)) {
                        if row.get("_placed") != Some(placed) {
                            row.insert("_placed".to_string(), placed.clone());
                            changed = true;
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
    let mut folded_new = HashSet::new();
    for (section, arrivals) in new_rows {
        let Some(library) = libraries.remove(&section) else {
            continue;
        };
        let mut activity = guard.scan_started(&section, &library.title);
        let last = arrivals.last().map(|arrival| arrival.release.clone()).unwrap_or_default();
        guard.scan_progress(&mut activity, &section, &last, 99);
        // Measured (4a): a folder scan announces its new rows after the step at 99.
        let keys = guard.place_arrivals(&library, arrivals);
        // A new release of a title already here becomes another version of it before any
        // client hears of it. Announced and then folded, it reached Plex for Mac as a row
        // created and deleted, which it kept drawing in Recently Added with the trash icon
        // (the production server, 2026-10-13: three releases of The Darjeeling Limited). A real PMS
        // adds the file to the item it already has.
        let new: HashSet<String> = keys.iter().cloned().collect();
        let absorbed = guard.fold_versions(Some(&new), &new);
        let keys: Vec<String> = keys.into_iter().filter(|key| !absorbed.contains(key)).collect();
        for key in &keys {
            guard.push_created_frames(key);
        }
        folded_new.extend(absorbed);
        activity.insert("subtitle".to_string(), json!(last));
        guard.scan_finished(&mut activity, &section);
        created.extend(keys);
    }
    // After the new rows: a release's extras arrive with its feature, in one push or a snapshot.
    for (item, library, extra_type) in extras {
        match guard.add_clip(&library, &item.arrival(&source.name), extra_type) {
            Some(key) => {
                counts.created += 1;
                touched.insert(key);
            }
            None => counts.unchanged += 1,
        }
    }
    let filled = guard.fill_probes(probed);
    touched.extend(created.iter().cloned());
    let mut folded = guard.fold_versions(Some(&touched), &HashSet::new());
    folded.extend(folded_new);
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
    // The production server's Usenet snapshot rejected 579 items on 2026-10-17 and said why for none.
    for rejected in &counts.rejected {
        info!("FEED {} rejected {}: {}", source.name, string_value(&rejected["id"]), string_value(&rejected["reason"]));
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
/// guid, or no poster -- under a show Plex names (the production server, 2026-10-12: Black
/// Mirror's Season 6 on `local://` with the blank tile, its show re-identified in place the
/// day before). Run once at start: each is built again as a leaf of its show's identity
/// builds it, from Plex's row for the season (cached when its episodes were identified), and
/// refreshed with that (`refresh_season`). Answers how many changed.
pub(super) fn repair_seasons(shared: &SharedRuntime) -> usize {
    let present = |value: &&Value| !value.is_null() && value.as_str() != Some("");
    // -- under the lock: which seasons, and what names them ---------------------------------
    let (wanted, plex) = {
        let guard = lock(shared);
        let Some(plex) = guard.plex.clone() else {
            return 0;
        };
        let wanted: Vec<(String, Value, i64, String, Map<String, Value>)> = guard
            .catalog
            .hot()
            .lazy()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some("season")
                    && row.get("_scanned").is_some_and(truthy)
                    && (row.get("guid").and_then(Value::as_str).is_none_or(|guid| guid.is_empty() || guid.starts_with("local://"))
                        || row.get("_poster_url").filter(present).is_none())
            })
            .filter_map(|season| {
                let key = key_of(&season)?;
                let index = season.get("index").and_then(Value::as_i64)?;
                let show = guard.catalog.get(&season.get("parentRatingKey").map(string_value)?)?;
                // A show an id names: `title:` ones are the retired name search's, left as they are.
                let identity = show.get("_identity").filter(present).filter(|id| !string_value(id).starts_with("title:"))?.clone();
                // Plex's guid for the show, as the episodes' identification named it.
                let named = guard.catalog.children_refs(&key).into_iter().find_map(|episode| {
                    episode.pointer("/_identify/plex").and_then(Value::as_str).filter(|guid| guid.starts_with("plex://show/")).map(str::to_string)
                })?;
                let artwork = [("_show_poster", "_poster_url"), ("_show_art", "_art_url")]
                    .into_iter()
                    .filter_map(|(hint, field)| Some((hint.to_string(), show.get(field).filter(present)?.clone())))
                    .collect();
                Some((key, identity, index, named, artwork))
            })
            .collect();
        (wanted, plex)
    };
    if wanted.is_empty() {
        return 0;
    }
    // -- off the lock: the season each one's identity builds --------------------------------
    let mut children: HashMap<String, HashMap<String, Value>> = HashMap::new();
    let built: Vec<(String, Value)> = wanted
        .into_iter()
        .filter_map(|(key, identity, index, show, artwork)| {
            let mut leaf = artwork;
            for (field, value) in [("_kind", json!("tv")), ("_tmdb_id", identity), ("_season", json!(index)), ("_episode", json!(0))] {
                leaf.insert(field.to_string(), value);
            }
            let seasons = children.entry(show.clone()).or_insert_with(|| crate::identify::plex_children_rows(plex.as_ref(), &show));
            if let Some(season) = seasons.get(&index.to_string()) {
                crate::metadata::season_hints(&mut leaf, season);
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

/// Numbers for the new files of a show's release its source did not place: season 1, episodes in
/// the release's file order (its paths sorted), after any of the release's own rows already here.
/// rex reads no number off a name: DMM places all but 0.9% of the production server's episodes
/// (2026-10-17), and these are for a person to see and Fix Match.
/// ponytail: numbers stay file order until the source places the files, and a file that arrives
/// after its release's others is numbered after them, not between; a placement moves it.
fn number_unplaced(runtime: &Runtime, plans: &mut [(Item, Library, Plan)]) {
    let mut releases: BTreeMap<(String, String), Vec<usize>> = BTreeMap::new();
    for (index, (item, library, plan)) in plans.iter().enumerate() {
        if matches!(plan, Plan::Show) && library.kind == "show" && super::scan::placed_numbers(item.placed.as_ref()).is_none() {
            releases.entry((library.key.clone(), item.name.clone())).or_default().push(index);
        }
    }
    if releases.is_empty() {
        return;
    }
    let mut numbered: HashMap<(String, String), i64> = HashMap::new();
    for row in runtime.catalog.hot().lazy() {
        let (Some(release), Some(index)) = (row.get("_release").and_then(Value::as_str), row.get("index").and_then(Value::as_i64)) else {
            continue;
        };
        if row.get("type").and_then(Value::as_str) == Some("episode") && row.get("parentIndex").and_then(Value::as_i64) == Some(1) {
            let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
            let last = numbered.entry((section, release.to_string())).or_insert(0);
            *last = (*last).max(index);
        }
    }
    for (release, mut indices) in releases {
        let last = numbered.get(&release).copied().unwrap_or(0);
        indices.sort_by(|a, b| plans[*a].0.path.cmp(&plans[*b].0.path));
        for (position, index) in indices.into_iter().enumerate() {
            plans[index].0.kept = Some((1, last + 1 + position as i64));
        }
    }
}

/// Whether `row` may be one `retry_blank` tries now, read off hot fields alone: due (`_retry`),
/// a source's film or episode out of the trash and probed, and no poster (an episode's show's).
/// `Runtime::blank` decides on the whole row; this keeps the pass every ten minutes from reading
/// every row of the library from the state file under the lock, as the listing once did (26 s
/// on the production server, every Plex request waiting behind it).
fn may_be_blank(catalog: &crate::catalog::Catalog, row: &(impl Row + ?Sized), stamp: i64) -> bool {
    let has = |row: Option<&Value>| row.is_some_and(|value| !value.is_null() && value.as_str() != Some(""));
    let due = row.get("_retry").and_then(|retry| retry.get("at")).and_then(Value::as_i64).is_none_or(|at| at <= stamp);
    let kind = row.get("type").and_then(Value::as_str);
    if !due
        || !matches!(kind, Some("movie" | "episode"))
        || row.get("_sid").is_none()
        || row.get("deletedAt").is_some()
        || row.get("_probePending").is_some_and(truthy)
        || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("tv.plex.agents.none://"))
    {
        return false;
    }
    if kind == Some("episode") {
        let show = row.get("grandparentRatingKey").map(string_value).and_then(|show| catalog.lazy_get(&show));
        return !has(show.as_ref().and_then(|show| show.get("_poster_url")))
            || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("plex://movie/"));
    }
    !has(row.get("_poster_url"))
}

/// The item a row's source pushed, rebuilt from the row: what `upsert` needs to identify it
/// again, in the library it is in. Its probe is the row's already, so none is sent.
fn pushed_item(row: &Value) -> Option<(String, Value)> {
    let text = |field: &str| row.get(field).and_then(Value::as_str).filter(|value| !value.is_empty());
    // The release's name, not an archive's: the rows the production server split off as
    // `aaf-wilty.301.rar` were retried by that name and stayed on the blank tile (2026-10-16).
    let name = version_release(row, 0)?;
    // Where its source placed it, which put it in this library: the placement goes with it.
    let placed = version_placed(row, 0).and_then(Value::as_object);
    let mut item = json!({"id": text("_sid")?, "path": text("_path")?, "file": row.pointer("/Media/0/Part/0/file")?,
                          "name": name, "release": name, "size": row.pointer("/Media/0/Part/0/size").cloned().unwrap_or(json!(0)),
                          "hash": row.get("_hash"), "ids": row.get("_ids").cloned().unwrap_or_else(|| json!({})),
                          "library": row.get("librarySectionID").map(string_value), "folder": row.get("_folder")});
    if let (Some(placed), Some(object)) = (placed, item.as_object_mut()) {
        object.extend(placed.clone());
    }
    Some((text("_source")?.to_string(), item))
}

/// Where its source placed a row's release at `index` (`_placed`): the version's own, else the
/// row's for its first.
fn version_placed(row: &Value, index: usize) -> Option<&Value> {
    row.pointer(&format!("/Media/{index}/_placed")).or_else(|| row.get("_placed").filter(|_| index == 0))
}

/// Rows identification left on the blank tile -- a film with no poster, or an episode whose
/// show has none -- identified again as their source pushed them, `limit` films or shows a
/// pass. The feed names a row again only when its source's ids change, so a lookup that failed
/// once (Plex down, no token) and a title Plex has learned since stayed blank for
/// good: the production rex held 56 films and 45 shows so on 2026-10-12. Only a row that came
/// with something to name it by (`scan::identified_by`) is tried; one with no ids has nothing
/// to retry with and stays as it is, for Fix Match. Each row still blank afterwards waits
/// longer before its next try (`retry_after`, kept on the row as `_retry`); what a person
/// matched is never touched. Answers how many rows it tried.
pub(super) fn retry_blank(shared: &SharedRuntime, limit: usize) -> usize {
    let stamp = now();
    // -- under the lock: which rows, a show's episodes together so it is named whole --------
    let (by_source, keys, sources) = {
        let guard = lock(shared);
        if guard.plex.is_none() {
            return 0;
        }
        let mut groups: BTreeMap<String, Vec<(String, (String, Value))>> = BTreeMap::new();
        for row in guard.catalog.hot().lazy() {
            // Hot fields first: the rows that may be blank, without reading any row from disk.
            if !may_be_blank(&guard.catalog, &row, stamp) {
                continue;
            }
            let library = if row.get("type").and_then(Value::as_str) == Some("movie") { "movie" } else { "show" };
            // Only what its source placed: rows pushed before DMM placed anything still carry ids
            // zurg's own Plex gave, an episode's among them (Pawn Stars' 1559521), and trusted
            // as they are those would name junk titles; they stay as they are, for Fix Match.
            if !guard.blank(row.body())
                || version_placed(row.body(), 0).is_none()
                || super::scan::identified_by(row.get("_ids").and_then(Value::as_object), version_placed(row.body(), 0), library).is_none()
            {
                continue;
            }
            let (Some(key), Some(item)) = (key_of(&row), pushed_item(row.body())) else {
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
        let source = named(&sources, name);
        // Feed requests apply one at a time; so does this.
        let _feed = FEED.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        upsert(shared, &source, items, None, true);
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

/// The source a row names, to identify its items again as it pushed them: that needs its
/// name alone, never its bytes. The retry at start runs before a source registers on a
/// catalog from before registration was kept, and skipping an unregistered source's rows
/// left them on the blank tile until the next pass ten minutes on.
fn named(sources: &[Arc<Source>], name: &str) -> Arc<Source> {
    sources
        .iter()
        .find(|source| source.name == name)
        .cloned()
        .unwrap_or_else(|| Arc::new(Source::new(name, "", None, None)))
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

pub(super) use crate::catalog::versions;

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
            for field in ["_source", "_sid", "_path", "_folder", "_release", "_ids", "_placed"] {
                if let Some(value) = row.get(field) {
                    first.insert(field.to_string(), value.clone());
                }
            }
        }
    }
    media
}

/// Which title a row a source pushed is, for folding: a film's section, agent guid and edition;
/// an episode's season and index, whatever guid identification gave it. None for an unmatched
/// film (a `local://` guid is its own key's), one waiting for a probe, and a row no source pushed.
fn version_slot(row: &(impl Row + ?Sized)) -> Option<String> {
    let kind = row.get("type").and_then(Value::as_str).filter(|kind| matches!(*kind, "movie" | "episode"))?;
    if row.get("_sid").is_none() || row.get("_probePending").is_some_and(truthy) {
        return None;
    }
    let field = |name: &str| row.get(name).map(string_value).unwrap_or_default();
    // The episode's index too: a pack identified as one title gives every episode its guid,
    // and the production server folded The Boys S03E01-E08 into one episode (2026-10-14).
    // And the index alone: SpongeBob's S10E64, which TMDB and Plex do not list, was five
    // `local://` rows of four releases on one season (the production server, 2026-10-17); a real
    // PMS makes every file of one episode one item.
    let guid = match kind {
        "episode" => row.get("index").and_then(Value::as_i64).map(|_| "")?,
        _ => row.get("guid").and_then(Value::as_str).filter(|guid| !guid.is_empty() && !guid.starts_with("local://"))?,
    };
    Some([kind, &field("librarySectionID"), &field("parentRatingKey"), &field("index"), guid, &field("editionTitle")].join("\u{1}"))
}

/// Each version's (release, file name): a release's other file on the same episode is no other
/// version of it. One collection of Mike Judge's shows put Daria's, Tales From the Tour Bus's
/// and Beavis and Butt-Head's S01E04 on Beavis and Butt-Head's, and one Erai-raws batch seven
/// specials on One Piece's 892 (the production server, 2026-10-17). The same file listed twice
/// (`…part1.rar/x.mkv`, `…part6.rar/x.mkv`) is one.
fn release_files(row: &Value) -> Vec<(String, String)> {
    (0..row.get("Media").and_then(Value::as_array).map_or(0, Vec::len))
        .filter_map(|index| {
            let file = media_file(row, index)?;
            Some((version_release(row, index)?.to_string(), file.rsplit('/').next()?.to_string()))
        })
        .collect()
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
    "_release_path", "_probePending", "_hash", "_placed",
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

// An archive a source lists as a folder of its members.
static ARCHIVE: LazyLock<regex::Regex> = LazyLock::new(|| regex::Regex::new(r"(?i)\.(?:rar|zip|7z)$").unwrap());

/// The release a source path's file is in: its folder, or the folder above the archive it
/// is a member of. `shows/Would.I.Lie.To.You.S03E01.PROPER…/aaf-wilty.301.rar/aaf-wilty.301.avi`
/// is the PROPER release's; read as a release called `aaf-wilty.301.rar`, the production server
/// split it off its episode into a show of that name (2026-10-16).
fn release_dir(path: &str) -> Option<&str> {
    let mut parents = path.rsplit('/').skip(1);
    let mut parent = parents.next()?;
    while ARCHIVE.is_match(parent) {
        match parents.next() {
            Some(up) => parent = up,
            None => break,
        }
    }
    Some(parent)
}

/// The release name a row's Media at `index` came from: what its source pushed as the
/// name, kept on the version since the fold carries it, else the row's for its first
/// version, else its path's (`release_dir`).
fn version_release(row: &Value, index: usize) -> Option<&str> {
    fn text(value: Option<&Value>) -> Option<&str> {
        value.and_then(Value::as_str).filter(|name| !name.is_empty())
    }
    text(row.pointer(&format!("/Media/{index}/_release")))
        .or_else(|| text(row.get("_release")).filter(|name| index == 0 && !ARCHIVE.is_match(name)))
        .or_else(|| row.pointer(&format!("/Media/{index}/_path")).and_then(Value::as_str).and_then(release_dir))
        .or_else(|| text(row.get("_release")))
}

/// A row made of one release: that release its only Media, its source fields and name the row's.
fn take_release(row: &mut Map<String, Value>, media: &Value) {
    name_by_release(row, media);
    row.insert("Media".to_string(), json!([media]));
}

/// The row's own source fields, name and ids, as its first release has them. A row dropping a
/// release kept the dropped one's name and ids, and was named again by them.
fn name_by_release(row: &mut Map<String, Value>, media: &Value) {
    for field in ["_source", "_sid", "_path"] {
        if let Some(value) = media.get(field) {
            row.insert(field.to_string(), value.clone());
        }
    }
    // The row's folder and placement are its release's, or none: another release's would number it.
    for field in ["_folder", "_placed"] {
        match media.get(field) {
            Some(value) => row.insert(field.to_string(), value.clone()),
            None => row.shift_remove(field),
        };
    }
    let release = media
        .get("_release")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .or_else(|| media.get("_path").and_then(Value::as_str).and_then(release_dir))
        .map(str::to_string);
    if let Some(release) = release {
        row.insert("_release".to_string(), json!(release));
    }
    // The ids its source pushed with it, not the row's: those name the other release.
    if let Some(ids) = media.get("_ids") {
        row.insert("_ids".to_string(), ids.clone());
    }
}

impl Runtime {
    /// A source registered (or restored): it replaces the one of its name.
    pub(super) fn put_source(&mut self, source: Source) {
        let source = Arc::new(source);
        match self.sources.iter_mut().find(|known| known.name == source.name) {
            Some(known) => *known = source,
            None => self.sources.push(source),
        }
    }

    /// The rows holding a release of this source: (key, its ids there, whether every
    /// release of the row is this source's).
    fn source_items(&self, source: &str) -> Vec<(String, Vec<String>, bool)> {
        self.catalog
            .hot()
            .lazy()
            .filter_map(|row| {
                let all = versions(row.body());
                let mine: Vec<String> = all.iter().filter(|(of, _, _)| of == source).map(|(_, sid, _)| sid.clone()).collect();
                let whole = mine.len() == all.len();
                Some((key_of(&row)?, mine, whole)).filter(|(_, mine, _)| !mine.is_empty())
            })
            .collect()
    }

    /// A confirmed source delete: its registration, its held items, and its releases --
    /// a row of its releases alone deleted for good as `emptyTrash` deletes one, a row
    /// holding another source's too keeping those. Nothing is asked of the source. Answers
    /// how many items went.
    fn delete_source(&mut self, source: &str) -> usize {
        self.sources.retain(|known| known.name != source);
        let prefix = held_key(source, "");
        let held: Vec<String> = self.feed_held.keys().filter(|key| key.starts_with(&prefix)).cloned().collect();
        for key in &held {
            self.release(key);
        }
        let mut removed = held.len();
        for (key, sids, whole) in self.source_items(source) {
            removed += sids.len();
            if whole {
                self.purge_row(&key);
            } else {
                for sid in &sids {
                    self.drop_version(&key, source, sid);
                }
            }
        }
        self.dirty = true;
        info!("SOURCES {source} deleted: {removed} item(s) removed from rex with its registration");
        removed
    }

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
    /// episode whose show has none -- or an episode named as a film, unless a person matched or
    /// unmatched it.
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
            // An episode with a film's guid is one `metadata::enrich` named after its show: every
            // season-0 episode before it read Specials (zen's Firefly S0E1-E3, `Episode N` under
            // one `plex://movie/` guid, 2026-10-16). Same ids, same placement: no push names it again.
            return !has(show, "_poster_url") || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("plex://movie/"));
        }
        !has(Some(row), "_poster_url")
    }

    fn section_title(&self, key: &str) -> String {
        self.section(key).map(|section| string_value(section.get("title").unwrap_or(&Value::Null))).unwrap_or_default()
    }

    /// A registration's `directories`: each library it titles, made unless one of that type
    /// already has a folder naming the directory (seeded, added by hand, made before and
    /// perhaps renamed since) or a person deleted one there (`deleted_locations`). A movie
    /// library comes first, so an unplaced file, which goes to the first library holding its
    /// folder, goes to a movie library there as it does anywhere else. rex never deletes or
    /// retitles a library this made; a directory dropped from the list keeps its library.
    pub(super) fn declare_libraries(&mut self, source: &Source) {
        let playable = |section: &&Value| matches!(section.get("type").and_then(Value::as_str), Some("movie" | "show"));
        // The seed's libraries sit under the mount (`/mnt/zurg/movies`): a source naming none
        // gets its directories there too.
        let prefix = source.mount.clone().unwrap_or_else(|| {
            let seeded = self.catalog.sections.iter().filter(playable).flat_map(|section| self.locations(section)).next();
            seeded.and_then(|folder| folder.trim_end_matches('/').rsplit_once('/').map(|(parent, _)| parent.to_string())).unwrap_or_else(|| "/mnt/zurg".to_string())
        });
        let mut made = false;
        for entry in &source.directories {
            let names = |folder: &str| {
                let folder = folder.trim_end_matches('/');
                folder == entry.name || folder.ends_with(&format!("/{}", entry.name))
            };
            for (kind, title) in [("movie", &entry.movies), ("show", &entry.shows)] {
                let Some(title) = title else {
                    continue;
                };
                let of_kind = |section: &&Value| section.get("type").and_then(Value::as_str) == Some(kind);
                if self.catalog.sections.iter().filter(of_kind).any(|section| self.locations(section).iter().any(|folder| names(folder))) {
                    continue;
                }
                let deleted = |gone: &&mut Value| of_kind(&&**gone) && gone.get("path").and_then(Value::as_str).is_some_and(names);
                if let Some(gone) = self.deleted_locations.iter_mut().find(deleted) {
                    // Said once per deletion: zurg registers again at every push session.
                    if gone.get("told").is_none() {
                        info!("SECTION {title} ({kind}) for {}'s {} stays deleted: a library there was deleted", source.name, entry.name);
                        gone["told"] = json!(true);
                        self.dirty = true;
                    }
                    continue;
                }
                let folder = format!("{}/{}", prefix.trim_end_matches('/'), entry.name);
                let key = self.next_section_key();
                let mut section = json!({
                    "key": key.to_string(),
                    "type": kind,
                    "title": title,
                    // Its folder too: two sources may title two directories alike.
                    "uuid": format!("{:032x}", crc32(format!("{title}-{kind}-{folder}").as_bytes())),
                    "createdAt": now(),
                    "contentChangedAt": -1,
                    "Location": [{"id": key, "path": folder}],
                });
                // What Add Library would have sent for it, so Plex's dialogs read it as theirs.
                if let Some(model) = self.catalog.sections.iter().find(of_kind) {
                    for field in ["agent", "scanner", "language", "metadataAgentProviderGroupId"] {
                        if let Some(value) = model.get(field) {
                            section[field] = value.clone();
                        }
                    }
                }
                info!("SECTION create {title} ({kind}) for {}'s {}", source.name, entry.name);
                self.push_section(section.clone());
                // As Add Library's create: a scan of the empty library and the queue frame.
                self.notify_section_scan(&key.to_string(), &section);
                self.notify_queue_regenerated();
                self.dirty = true;
                made = true;
            }
        }
        if made {
            self.republish_sections();
        }
    }

    /// The library an item belongs in: the section it names, else the movie or show
    /// library (of its type, when it names one) whose `Location` holds its file, else the
    /// first library of that type.
    fn feed_library(&self, item: &Item) -> Option<Library> {
        let library = |section: &Value| Library {
            key: string_value(section.get("key").unwrap_or(&Value::Null)),
            kind: section.get("type").and_then(Value::as_str).unwrap_or_default().to_string(),
            title: string_value(section.get("title").unwrap_or(&Value::Null)),
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
        // zurg files a release by its own name rules, and the production server's Usenet zurg filed 40 anime
        // episodes under movies on 2026-10-12 (`Detektiv.Conan.E0221…`, `[Moozzi2]
        // Eighty-Six-15 […]`). One its source placed as an episode goes to a show library, the
        // one beside that movie library's folder when there is one, and a film filed with the
        // shows to Movies; the name is never read (it was until 2026-10-17).
        let episode = || item.kind() == Some("episode");
        // A folder no library holds (zurg's `anime`) goes to the first library of its kind:
        // refusing it made every setup with such a folder seed its libraries by hand first.
        let of_its_kind = || {
            let kind = if episode() { "show" } else { "movie" };
            of_kind.iter().find(|section| section.get("type").and_then(Value::as_str) == Some(kind))
        };
        let found = of_kind
            .iter()
            .find(|section| self.locations(section).iter().any(|location| under(&item.file, location)))
            .or_else(|| of_kind.iter().find(|section| self.locations(section).iter().any(|location| names_directory(location, &item.path))))
            .or_else(|| wanted.and(of_kind.first()))
            .or_else(of_its_kind)?;
        if wanted.is_none() && found.get("type").and_then(Value::as_str) == Some("show") && item.kind() == Some("movie") {
            if let Some(movies) = self.feed_library(&Item { library: Some("movie".to_string()), ..item.clone() }) {
                return Some(movies);
            }
        }
        if wanted.is_none() && found.get("type").and_then(Value::as_str) == Some("movie") && episode() {
            let parent = |location: &str| location.trim_end_matches('/').rsplit_once('/').map(|(parent, _)| parent.to_string());
            let beside = holds(found).and_then(|location| parent(&location));
            let shows: Vec<&Value> =
                self.catalog.sections.iter().filter(|section| section.get("type").and_then(Value::as_str) == Some("show")).collect();
            // A show library holding the folder too (a source's `directories` gives a folder a
            // movie and a show library on it) comes before one merely beside it.
            if let Some(show) = shows
                .iter()
                .find(|section| holds(section).is_some())
                .or_else(|| shows.iter().find(|section| beside.is_some() && self.locations(section).iter().any(|location| parent(location) == beside)))
                .or(shows.first())
            {
                return Some(library(show));
            }
        }
        Some(library(found))
    }

    /// The rows of this source's `ids`, by id; of two rows of one id, the later. Found by
    /// release (`catalog::By::Release`): a map of the source's every row read the library on
    /// each push.
    fn feed_rows<'a>(&self, source: &str, ids: impl Iterator<Item = &'a str>) -> HashMap<String, String> {
        ids.filter_map(|sid| {
            let key = crate::catalog::release_key(source, sid);
            let row = self
                .catalog
                .rows_by(crate::catalog::By::Release, &key)
                .into_iter()
                .filter(|row| versions(row).iter().any(|(of, id, _)| of == source && id == sid))
                .next_back()?;
            Some((sid.to_string(), key_of(row)?))
        })
        .collect()
    }

    /// Rows no feed has claimed (what the WebDAV scanner made) by their first Part's file, for
    /// `files`; of two rows of one file, the later (`catalog::By::File`).
    fn legacy_rows_by_file<'a>(&self, files: impl Iterator<Item = &'a str>) -> HashMap<String, String> {
        files
            .filter_map(|file| {
                let row = self
                    .catalog
                    .rows_by(crate::catalog::By::File, file)
                    .into_iter()
                    .filter(|row| {
                        row.get("_sid").is_none()
                            && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
                            && row_files(row).first().map(String::as_str) == Some(file)
                    })
                    .next_back()?;
                Some((file.to_string(), key_of(row)?))
            })
            .collect()
    }

    /// Rows the old WebDAV scanner made from this source's files that no item has adopted
    /// yet, not in the trash: what a first snapshot must also account for.
    fn unadopted_rows(&self, source: &Source) -> Vec<String> {
        self.catalog
            .hot()
            .lazy()
            .filter(|row| {
                row.get("_sid").is_none()
                    && row.get("_scanned").is_some_and(truthy)
                    && row.get("deletedAt").is_none()
                    && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
                    && row_files(row.body()).first().is_some_and(|file| source.path_under_mount(file).is_some())
            })
            .filter_map(|row| key_of(&row))
            .collect()
    }

    /// Every id the source has here that a snapshot must name: shown rows not in the
    /// trash, and held items.
    fn feed_ids(&self, source: &str) -> Vec<String> {
        let prefix = held_key(source, "");
        self.catalog
            .hot()
            .lazy()
            .filter(|row| row.get("deletedAt").is_none())
            .flat_map(|row| row.hot.releases.iter().filter(|release| &*release.source == source).map(|release| release.sid.to_string()))
            .chain(self.feed_held.keys().filter_map(|key| key.strip_prefix(&prefix).map(str::to_string)))
            .collect()
    }

    /// The source's releases as its listing names them: id, key, file and whether trashed.
    fn feed_listed(&self, source: &Source) -> Vec<(String, Option<String>, Option<String>, bool)> {
        self.catalog
            .hot()
            .lazy()
            .flat_map(|row| {
                row.hot.releases.iter().filter(|release| *release.source == *source.name).map(move |release| {
                    (release.sid.to_string(), key_of(&row), release.file(), row.get("deletedAt").is_some())
                })
            })
            .collect()
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
        // The folder the source names now, on the version and, for the first, the row: rows
        // made or folded before versions carried their folder get it from the next push.
        let folder_at = if tagged { row.pointer(&format!("/Media/{index}/_folder")) } else { row.get("_folder") };
        if let (Some(folder), true) = (&item.folder, folder_at.and_then(Value::as_str) != item.folder.as_deref()) {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                if tagged {
                    if let Some(media) = object.get_mut("Media").and_then(|media| media.get_mut(index)).and_then(Value::as_object_mut) {
                        media.insert("_folder".to_string(), json!(folder));
                    }
                }
                if index == 0 {
                    object.insert("_folder".to_string(), json!(folder));
                }
            }
            changed = true;
        }
        // The name and ids the source pushes now, on the version (`version_release`): one folded
        // before versions carried them takes them from the next push.
        if tagged {
            let ids = Value::Object(item.ids.clone());
            // A placement too, kept when a push sends none (`replaced`).
            let placed = item.placed.as_ref().filter(|placed| row.pointer(&format!("/Media/{index}/_placed")) != Some(*placed));
            let stale = row.pointer(&format!("/Media/{index}/_release")).and_then(Value::as_str) != Some(item.name.as_str())
                || row.pointer(&format!("/Media/{index}/_ids")) != Some(&ids)
                || placed.is_some();
            if stale {
                if let Some(media) = self
                    .catalog
                    .get_mut(key)
                    .and_then(|row| row.get_mut("Media"))
                    .and_then(|media| media.get_mut(index))
                    .and_then(Value::as_object_mut)
                {
                    media.insert("_release".to_string(), json!(item.name));
                    media.insert("_ids".to_string(), ids);
                    if let Some(placed) = placed {
                        media.insert("_placed".to_string(), placed.clone());
                    }
                }
                changed = true;
            }
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
        // Repeated snapshots must enrich existing releases too, not just new or
        // probe-pending rows. Update only this version; preserve its streams,
        // selected tracks, playback state and other versions' chapter timings.
        if let Some(probe) = &item.media_info {
            if probe.get("chapters").is_some_and(Value::is_array) {
                let chapters = mediainfo::build_chapters(probe,
                    current.get("id").and_then(Value::as_i64).unwrap_or(0));
                if row.pointer(&format!("/Media/{index}/_chapters")) != Some(&chapters) {
                    if let Some(media) = self.catalog.get_mut(key)
                        .and_then(|row| row.get_mut("Media"))
                        .and_then(|media| media.get_mut(index)).and_then(Value::as_object_mut) {
                        media.insert("_chapters".to_string(), chapters);
                        changed = true;
                    }
                }
            }
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
        // An identification that names nothing takes no title away: since a name alone is no
        // longer searched (2026-10-17), the start's retry found nothing for the rows the name
        // search had named, and placed Wallace & Gromit's episodes off their show.
        let named = |row: Option<&Value>| {
            row.and_then(|row| row.get("guid")).and_then(Value::as_str).is_some_and(|guid| !guid.starts_with("local://"))
        };
        if !meta.contains_key("_identity") && !moved && (named(Some(&old)) || named(parent("grandparentRatingKey"))) {
            return false;
        }
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
        // An episode its source placed on other numbers than it sits on moves there: re-identification
        // only ever compared ids, so a placement arriving with the same ids moved nothing.
        let here = (old.get("parentIndex").and_then(Value::as_i64), old.get("index").and_then(Value::as_i64));
        let renumbered = old.get("type").and_then(Value::as_str) == Some("episode")
            && library.kind == "show"
            && arrival.numbers.is_some_and(|(season, episode)| here != (Some(season), Some(episode)));
        if meta.get("_identity") == old.get("_identity") && guid == old.get("guid") && !moved && !gains_art && !renumbered {
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
        self.place_leaves(library, vec![leaf], &HashSet::from([key.to_string()]), true);
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
        let taken = self.catalog.hot().lazy().any(|row| {
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

    /// Rows a source pushed that are one title (`version_slot`) made one row holding each
    /// release as a Media, as a real PMS's scanner and agent make several files of one movie
    /// one item. The row someone watched survives, else one not in the trash, else the oldest;
    /// a row in the trash brings no Media, its files being gone. `only` limits it to slots
    /// holding one of those keys. Answers the keys folded away.
    /// `unannounced`: rows no client has heard of, folded without a `deleted` frame.
    pub(super) fn fold_versions(&mut self, only: Option<&HashSet<String>>, unannounced: &HashSet<String>) -> HashSet<String> {
        // (unwatched, trashed, key): the row someone watched survives, else one not in the trash.
        let mut slots: BTreeMap<String, Vec<(bool, bool, i64, String)>> = BTreeMap::new();
        for row in self.catalog.hot().lazy() {
            if let (Some(slot), Some(key)) = (version_slot(&row), key_of(&row)) {
                let watched = ["viewCount", "viewOffset", "lastViewedAt", "userRating"].iter().any(|field| row.get(*field).is_some_and(truthy));
                slots.entry(slot).or_default().push((!watched, row.get("deletedAt").is_some(), key.parse().unwrap_or(i64::MAX), key));
            }
        }
        let mut folded = HashSet::new();
        let mut gone = Vec::new();
        let mut untrash = Vec::new();
        for (_, mut rows) in slots {
            // Rows in the trash are folded only into what is there: their files are gone.
            if rows.len() < 2 || rows.iter().all(|row| row.1) || only.is_some_and(|only| !rows.iter().any(|row| only.contains(&row.3))) {
                continue;
            }
            rows.sort();
            let (survivor, revived) = (rows[0].3.clone(), rows[0].1);
            let Some(first) = self.catalog.get(&survivor).cloned() else {
                continue;
            };
            let mut releases = release_files(&first);
            let mut media = if revived { Vec::new() } else { tagged_media(&first) };
            let mut emptied = false;
            for (_, trashed, _, other) in &rows[1..] {
                let Some(row) = self.catalog.get(other).cloned() else {
                    continue;
                };
                let theirs = release_files(&row);
                if theirs.iter().any(|(release, file)| releases.iter().any(|(held, name)| held == release && name != file)) {
                    continue;
                }
                releases.extend(theirs);
                // A save stopped between the survivor's write and the others' deletes
                // brings an absorbed row back: its release is already a Media here.
                let held: HashSet<(String, String)> = media.iter().filter_map(media_release).collect();
                if !trashed {
                    media.extend(tagged_media(&row).into_iter().filter(|version| media_release(version).is_none_or(|release| !held.contains(&release))));
                }
                emptied |= *trashed;
                info!("FEED folded {other} into {survivor} as another version");
                folded.insert(other.clone());
                gone.push(row);
            }
            // A row in the trash that another release of its episode joins leaves it with that
            // release's files, as a real PMS's item takes a new file of its episode: Freakazoid's
            // S1E1 and S1E2 were trashed when zurg stopped naming their archives' members, and a
            // release of them coming later would have been a row beside them.
            if !(revived && media.is_empty()) {
                if let Some(object) = self.catalog.get_mut(&survivor).and_then(Value::as_object_mut) {
                    if revived {
                        take_release(object, &media[0]);
                        if let Some(duration) = media[0].get("duration") {
                            object.insert("duration".to_string(), duration.clone());
                        }
                    }
                    object.insert("Media".to_string(), Value::Array(media));
                }
                if revived || emptied {
                    untrash.push(survivor.clone());
                }
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
        // Once the trashed rows are gone, their season and show leave the trash with them.
        for key in &untrash {
            self.untrash_row(key);
        }
        for row in &gone {
            if let Some(key) = key_of(row) {
                self.forget_row(&key);
            }
            if key_of(row).is_none_or(|key| !unannounced.contains(&key)) {
                self.push_timeline_entry(row, 9, Some("deleted"), None, None, None);
            }
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
            name_by_release(object, &first);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_blank_tile_pass_reads_no_row_from_disk_to_find_its_rows() {
        let directory = std::env::temp_dir().join(format!("rex-blank-pass-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("rex.db");
        let mut items = vec![json!({"ratingKey": "500", "type": "show", "title": "Named", "_poster_url": "http://p/s"}),
                             json!({"ratingKey": "501", "type": "show", "title": "Blank"})];
        for n in 0..40 {
            let poster = if n % 4 == 0 { json!(null) } else { json!(format!("http://p/{n}")) };
            items.push(json!({"ratingKey": (1000 + n).to_string(), "type": "movie", "title": format!("M{n}"), "_sid": format!("m{n}"),
                              "_release": format!("R{n}"), "_poster_url": poster, "summary": "x".repeat(400)}));
            items.push(json!({"ratingKey": (2000 + n).to_string(), "type": "episode", "title": format!("E{n}"), "_sid": format!("e{n}"),
                              "grandparentRatingKey": if n % 2 == 0 { "500" } else { "501" }, "summary": "x".repeat(400)}));
        }
        items.push(json!({"ratingKey": "3000", "type": "movie", "title": "Trashed", "_sid": "t", "deletedAt": 1}));
        let mut catalog = crate::catalog::Catalog::from_value(json!({"items": items})).unwrap();
        let mut cache = crate::store::Cache::default();
        catalog.attach(&path, crate::rows::test_support::key_of, false);
        crate::rows::test_support::save(&mut catalog, &mut cache, &path, true);
        catalog.pack_pending();
        let before = crate::rows::test_support::DISK_READS.with(std::cell::Cell::get);
        let mut tried = catalog.hot().lazy().filter(|row| may_be_blank(&catalog, row, 0)).filter_map(|row| key_of(&row)).collect::<Vec<_>>();
        let releases = catalog.hot().lazy().filter_map(|row| row.get("_release").and_then(Value::as_str).map(str::to_string)).count();
        let read = crate::rows::test_support::DISK_READS.with(std::cell::Cell::get) - before;
        let _ = std::fs::remove_dir_all(&directory);
        tried.sort();
        let mut wanted = (0..40).filter(|n| n % 4 == 0).map(|n| (1000 + n).to_string())
            .chain((0..40).filter(|n| n % 2 == 1).map(|n| (2000 + n).to_string()))
            .collect::<Vec<_>>();
        wanted.sort();
        assert_eq!(tried, wanted);
        assert_eq!(releases, 40);
        assert_eq!(read, 0, "rows read from disk to find the blank ones");
    }

    fn work(name: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!("rex-feed-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    fn runtime_with_episode(dir: &std::path::Path) -> (SharedRuntime, String) {
        let catalog_path = dir.join("catalog.json");
        let show = json!({
            "ratingKey": "10",
            "key": "/library/metadata/10",
            "type": "show",
            "title": "Original",
            "librarySectionID": 1,
            "_identity": "tmdb:100"
        });
        let episode = json!({
            "ratingKey": "11",
            "key": "/library/metadata/11",
            "type": "episode",
            "title": "Episode",
            "librarySectionID": 1,
            "grandparentRatingKey": "10",
            "parentRatingKey": "12",
            "parentIndex": 1,
            "index": 2,
            "_release": "Original.S01E02.1080p",
            "_source": "test",
            "_sid": "one",
            "Media": [{"Part": [{"file": "/shows/Original/episode.mkv"}]}]
        });
        let catalog = json!({
            "format": 1,
            "machineIdentifier": "feed-test-machine",
            "friendlyName": "feed-test",
            "sections": [{"key": "1", "type": 2, "title": "Shows", "Location": [{"path": "/shows"}]}],
            "items": [show, episode]
        });
        std::fs::write(&catalog_path, serde_json::to_vec(&catalog).unwrap()).unwrap();
        let runtime = Runtime::load(RuntimeOptions {
            catalog: catalog_path,
            state: Some(dir.join("no-state")),
            // The suites' stand-in for a claim: an unclaimed server serves no client.
            assert_claimed: true,
            ..RuntimeOptions::default()
        })
        .unwrap();
        (Arc::new(Mutex::new(runtime)), "11".to_string())
    }

    #[test]
    fn a_dropped_release_leaves_the_row_named_and_identified_by_the_one_left() {
        let dir = work("drop-version");
        let (shared, key) = runtime_with_episode(&dir);
        let mut guard = shared.lock().unwrap();
        guard.catalog.get_mut(&key).unwrap()["Media"] = json!([
            {"_source": "test", "_sid": "one", "_path": "shows/Original/one.mkv", "_release": "Original.S01E02.1080p",
             "_ids": {"tmdb": 100}, "Part": [{"file": "/shows/Original/one.mkv"}]},
            {"_source": "test", "_sid": "two", "_path": "shows/Other/two.mkv", "_release": "Other.S01E02.720p",
             "_ids": {"tmdb": 200}, "Part": [{"file": "/shows/Other/two.mkv"}]}]);
        guard.catalog.get_mut(&key).unwrap()["_ids"] = json!({"tmdb": 100});
        assert!(guard.drop_version(&key, "test", "one"));
        let row = guard.catalog.get(&key).unwrap();
        assert_eq!((&row["_sid"], &row["_release"], &row["_ids"]), (&json!("two"), &json!("Other.S01E02.720p"), &json!({"tmdb": 200})));
        drop(guard);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn chapters_feed_updates_existing_versions_and_clears_explicit_empty() {
        let dir = work("chapters");
        let (shared, key) = runtime_with_episode(&dir);
        let mut guard = shared.lock().unwrap();
        guard.catalog.get_mut(&key).unwrap()["Media"] = json!([
            {"_source": "test", "_sid": "one", "_path": "one.mkv", "_folder": "",
             "Part": [{"id": 11, "file": "/shows/one.mkv", "Stream": [{"id": 123, "selected": true}]}]},
            {"_source": "test", "_sid": "two", "_path": "two.mkv", "_folder": "",
             "Part": [{"id": 22, "file": "/shows/two.mkv", "Stream": [{"id": 223, "selected": true}]}]}
        ]);
        let row = guard.catalog.get_mut(&key).unwrap();
        crate::catalog::pack_streams(row);
        let streams = row.pointer("/Media/1/Part/0").unwrap().clone();
        let source = Source::register("test", &json!({"url": "http://127.0.0.1:9"})).unwrap();
        let mut pushed = json!({"id": "two", "path": "two.mkv", "file": "/shows/two.mkv", "folder": "",
            "media_info": {"streams": [{"codec_type": "video"}],
                "chapters": [{"start_time": "0", "end_time": "1", "tags": {"title": "Opening"}}]}});
        let mut probed = Vec::new();
        let item = Item::parse(&source, &pushed).unwrap();
        assert!(guard.feed_update_row(&key, "test", &item, false, &mut probed));
        assert!(!guard.feed_update_row(&key, "test", &item, false, &mut probed));
        let row = guard.catalog.get(&key).unwrap();
        assert_eq!(row.pointer("/Media/1/_chapters/0/id"), Some(&json!(220001)));
        assert!(row.pointer("/Media/0/_chapters").is_none());
        assert_eq!(row.pointer("/Media/1/Part/0"), Some(&streams), "chapter updates preserve packed streams and track selections");
        pushed["media_info"].as_object_mut().unwrap().shift_remove("chapters");
        assert!(!guard.feed_update_row(&key, "test", &Item::parse(&source, &pushed).unwrap(), false, &mut probed));
        assert_eq!(guard.catalog.get(&key).unwrap().pointer("/Media/1/_chapters/0/index"), Some(&json!(1)));
        pushed["media_info"]["chapters"] = json!([]);
        assert!(guard.feed_update_row(&key, "test", &Item::parse(&source, &pushed).unwrap(), false, &mut probed));
        assert_eq!(guard.catalog.get(&key).unwrap().pointer("/Media/1/_chapters"), Some(&json!([])));
        assert!(probed.is_empty());
        drop(guard);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn chapters_reach_metadata_and_play_queues_only_when_requested() {
        let dir = work("chapter-routes");
        let (shared, key) = runtime_with_episode(&dir);
        let mut runtime = shared.lock().unwrap();
        let fixture: Value = serde_json::from_str(include_str!("../../../tests/fixtures/chapters/embedded.json")).unwrap();
        let chapters = mediainfo::build_chapters(&fixture["probe"], 11);
        runtime.catalog.get_mut(&key).unwrap()["Media"] = json!([
            {"Part": [{"id": 11}], "_chapters": chapters},
            {"Part": [{"id": 22}], "_chapters": []}
        ]);
        let request = |method: &str, target: &str| Request {
            method: method.to_string(), target: target.to_string(), version: "HTTP/1.1".to_string(),
            headers: HashMap::new(), body: Vec::new(), body_complete: true,
        };
        let read = |runtime: &mut Runtime, method: &str, target: &str| -> Value {
            let response = runtime.request_route(&request(method, target));
            assert_eq!(response.status, 200, "{method} {target}: {}", String::from_utf8_lossy(&response.body));
            let body: Value = serde_json::from_slice(&response.body).unwrap();
            assert!(!String::from_utf8_lossy(&response.body).contains("_chapters"));
            body
        };
        for suffix in ["", "?includeChapters=0", "?includeChapters=1"] {
            let body = read(&mut runtime, "GET", &format!("/library/metadata/11{suffix}"));
            let row = &body["MediaContainer"]["Metadata"][0];
            assert_eq!(row["chapterSource"], "media");
            if suffix.ends_with('1') { assert_eq!(row["Chapter"], chapters); }
            else { assert!(row.get("Chapter").is_none()); }
        }
        let body = read(&mut runtime, "POST", "/playQueues?type=video&includeChapters=1&uri=server://feed-test-machine/com.plexapp.plugins.library/library/metadata/11");
        assert_eq!(body["MediaContainer"]["Metadata"][0]["Chapter"], chapters);
        let id = body["MediaContainer"]["playQueueID"].as_i64().unwrap();
        for suffix in ["", "?includeChapters=0", "?includeChapters=1"] {
            let body = read(&mut runtime, "GET", &format!("/playQueues/{id}{suffix}"));
            let row = &body["MediaContainer"]["Metadata"][0];
            assert_eq!(row["chapterSource"], "media");
            if suffix.ends_with('1') { assert_eq!(row["Chapter"], chapters); }
            else { assert!(row.get("Chapter").is_none()); }
        }
        drop(runtime);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A confirmed delete removes its source holding the feed lock. A push for that source
    /// already waiting on the lock must find it gone, so zurg hears the 404 that makes it
    /// register again: one that looked its source up before it waited put the deleted
    /// source's rows back under a name nothing registers.
    #[test]
    fn a_push_waiting_behind_a_source_delete_finds_the_source_gone() {
        let dir = work("delete-race");
        let (shared, _) = runtime_with_episode(&dir);
        shared.lock().unwrap().put_source(Source::register("b", &json!({"url": "http://127.0.0.1:9"})).unwrap());
        let feed = FEED.lock().unwrap();
        let push = {
            let shared = shared.clone();
            std::thread::spawn(move || {
                let request = Request {
                    method: "POST".to_string(),
                    target: "/rex/feed/b/items".to_string(),
                    version: "HTTP/1.1".to_string(),
                    headers: HashMap::new(),
                    body: br#"{"items": []}"#.to_vec(),
                    body_complete: true,
                };
                handle(&shared, &request, "/rex/feed/b/items").status
            })
        };
        // The push reaches the feed lock and waits there, as behind a confirmed delete.
        std::thread::sleep(std::time::Duration::from_millis(300));
        shared.lock().unwrap().delete_source("b");
        drop(feed);
        assert_eq!(push.join().unwrap(), 404, "a push behind the delete was applied to a source that is gone");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn source_peers_are_this_host_private_networks_tailscale_and_link_local() {
        for (peer, answered) in [
            ("127.0.0.1", true), ("127.8.9.10", true), ("::1", true),
            ("10.1.2.3", true), ("172.16.0.1", true), ("172.31.255.255", true), ("192.168.1.10", true),
            ("100.64.0.1", true), ("100.101.20.2", true), ("100.127.255.254", true),
            ("169.254.1.1", true), ("fd7a:115c:a1e0::1", true), ("fc00::1", true), ("fe80::1", true),
            ("febf::1", true), ("::ffff:192.168.1.10", true), ("::ffff:100.64.0.1", true),
            ("203.0.113.7", false), ("8.8.8.8", false), ("172.32.0.1", false), ("172.15.255.255", false),
            ("100.63.255.255", false), ("100.128.0.1", false), ("169.255.0.1", false),
            ("192.169.0.1", false), ("::ffff:203.0.113.7", false), ("2001:db8::1", false),
            ("2a00:1450:4001::1", false), ("fec0::1", false), ("0.0.0.0", false), ("::", false),
        ] {
            assert_eq!(source_peer(peer.parse().unwrap()), answered, "{peer}");
        }
    }

    /// rex-direct forwards rex's port from the internet, and the source API takes no token:
    /// a peer that is not on a private network is refused on every route and method, and
    /// headers naming a local client do not change who the socket's peer is.
    #[test]
    fn the_source_api_refuses_a_public_peer_whatever_its_headers_say() {
        let dir = work("source-gate");
        let (shared, _) = runtime_with_episode(&dir);
        shared.lock().unwrap().put_source(Source::register("b", &json!({"url": "http://127.0.0.1:9"})).unwrap());
        let local: std::net::IpAddr = "192.168.1.2".parse().unwrap();
        let request = |method: &str, target: &str, headers: &[(&str, &str)]| Request {
            method: method.to_string(),
            target: target.to_string(),
            version: "HTTP/1.1".to_string(),
            headers: headers.iter().map(|(name, value)| (name.to_string(), value.to_string())).collect(),
            body: br#"{"items": [], "url": "http://127.0.0.1:9"}"#.to_vec(),
            body_complete: true,
        };
        let forged = [("x-forwarded-for", "127.0.0.1"), ("host", "localhost")];
        for peer in ["203.0.113.7", "::ffff:203.0.113.7", "2001:db8::7"] {
            CONNECTION.with(|cell| cell.set(Some((peer.parse().unwrap(), local))));
            for (method, target) in [
                ("GET", "/rex/sources"),
                ("PUT", "/rex/sources/c"),
                ("DELETE", "/rex/sources/b"),
                ("DELETE", "/rex/sources/b/confirm/0123456789abcdef0123456789abcdef"),
                ("GET", "/rex/feed/b/items"),
                ("POST", "/rex/feed/b/items"),
                ("PUT", "/rex/feed/b/snapshot"),
            ] {
                for headers in [&[][..], &forged[..]] {
                    let response = handle(&shared, &request(method, target, headers), target);
                    assert_eq!(response.status, 403, "{peer} {method} {target} {headers:?}");
                }
            }
        }
        assert_eq!(shared.lock().unwrap().sources.len(), 1, "a refused PUT registered a source");
        for peer in ["127.0.0.1", "192.168.1.10", "100.101.20.2", "fd7a:115c:a1e0::1", "::ffff:10.0.0.5"] {
            CONNECTION.with(|cell| cell.set(Some((peer.parse().unwrap(), local))));
            assert_eq!(handle(&shared, &request("GET", "/rex/sources", &[]), "/rex/sources").status, 200, "{peer}");
        }
        CONNECTION.with(|cell| cell.set(None));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_delete_code_confirms_once_and_only_in_time() {
        let now = std::time::Instant::now();
        let code = "0123456789abcdef0123456789abcdef".to_string();
        let pending = (code.clone(), now + std::time::Duration::from_secs(DELETE_WINDOW));
        assert_eq!(confirms(Some(&pending), &code, now), (true, true), "the right code in time, spent by its use");
        assert_eq!(confirms(Some(&pending), "0123456789abcdef0123456789abcdee", now), (false, false), "a wrong code spends nothing");
        assert_eq!(confirms(Some(&pending), "0123", now), (false, false));
        assert_eq!(confirms(Some(&pending), "", now), (false, false));
        assert_eq!(confirms(None, &code, now), (false, false), "nothing asked for, or already used");
        let late = now + std::time::Duration::from_secs(DELETE_WINDOW);
        assert_eq!(confirms(Some(&pending), &code, late), (false, true), "expired: refused and forgotten");
    }

    /// The production server's Would I Lie to You? S03E01 (2026-10-16) as it held it: a WEB-DL
    /// release and the `S03E01.PROPER` NZB's archive member folded onto it at 21:45, the
    /// version with only its source fields. The start at 22:08 read the archive as its release,
    /// `aaf-wilty.301.rar`, and split it off into a show of that name.
    #[test]
    fn a_version_inside_an_archive_is_its_releases() {
        let web = "shows/Would.I.Lie.To.You.S03E09.Unseen.bits.UKTV.WEB-DL.AAC2.0.H.264-BTW/f28908120d6f4baa84e4ff62cb06b8f0.part01.rar/Would.I.Lie.To.You.S03E01.Jo.Brand.Carol.Vorderman.Russell.Howard.Larry.Lamb.UKTV.WEB-DL.AAC2.0.H.264-BTW.mkv";
        let proper = "shows/Would.I.Lie.To.You.S03E01.PROPER.WS.PDTV.XviD-aAF/aaf-wilty.301.rar/aaf-wilty.301.avi";
        let second = "shows/Would.I.Lie.To.You.S03E01.PROPER.WS.PDTV.XviD-aAF/aaf-wilty.302.rar/aaf-wilty.302.avi";
        let row = json!({"ratingKey": "160370", "type": "episode", "parentIndex": 3, "index": 1,
                         "_release": "Would.I.Lie.To.You.S03E09.Unseen.bits.UKTV.WEB-DL.AAC2.0.H.264-BTW", "_ids": {},
                         "Media": [{"_source": "usenet", "_sid": web, "_path": web, "_folder": ""},
                                   {"_source": "usenet", "_sid": proper, "_path": proper, "_folder": ""},
                                   {"_source": "usenet", "_sid": second, "_path": second, "_folder": ""}]});
        assert_eq!(version_release(&row, 0), Some("Would.I.Lie.To.You.S03E09.Unseen.bits.UKTV.WEB-DL.AAC2.0.H.264-BTW"));
        assert_eq!(version_release(&row, 1), Some("Would.I.Lie.To.You.S03E01.PROPER.WS.PDTV.XviD-aAF"));
        assert_eq!(version_release(&row, 2), Some("Would.I.Lie.To.You.S03E01.PROPER.WS.PDTV.XviD-aAF"));
        // A row split off a version takes the version's release and ids, never the archive's name.
        let mut split = row.as_object().unwrap().clone();
        let version = json!({"_source": "usenet", "_sid": proper, "_path": proper, "_ids": {"imdb": "tt1055238"}});
        take_release(&mut split, &version);
        assert_eq!(split["_release"], json!("Would.I.Lie.To.You.S03E01.PROPER.WS.PDTV.XviD-aAF"));
        assert_eq!(split["_ids"], json!({"imdb": "tt1055238"}));
        // A version folded since keeps what its source pushed.
        let pushed = json!({"_source": "usenet", "_sid": "one", "_path": "shows/one/f.mkv", "_release": "Other.Name.S03E01", "_ids": {"imdb": "tt1"}});
        let folded = tagged_media(&json!({"Media": [{"Part": []}], "_source": "usenet", "_sid": "one", "_path": "shows/one/f.mkv",
                                          "_release": "Other.Name.S03E01", "_ids": {"imdb": "tt1"}}));
        assert_eq!(folded[0]["_release"], pushed["_release"]);
        assert_eq!(folded[0]["_ids"], pushed["_ids"]);
    }

    /// Detective Conan S33E01, which TMDB numbers S1E1103 (the placement contract's own example,
    /// 2026-10-16): the placement is parsed, kept on the row, rebuilt from it for the blank-tile
    /// retry, and goes with its release when a version is split off.
    #[test]
    fn a_placement_is_kept_with_its_release() {
        let source = Source::new("zurg", "", None, None);
        let path = "shows/Detective.Conan.S33.1080p.WEB.H264-GRP/Detective.Conan.S33E01.mkv";
        let pushed = json!({"id": path, "path": path, "name": "Detective.Conan.S33.1080p.WEB.H264-GRP",
                            "ids": {"imdb": "tt0131179", "tmdb": 30983}, "kind": "episode", "season": 1, "episode": 1103});
        let item = Item::parse(&source, &pushed).unwrap();
        let placed = json!({"kind": "episode", "season": 1, "episode": 1103});
        assert_eq!(item.placed.as_ref(), Some(&placed));
        let arrival = item.arrival("zurg");
        assert_eq!((arrival.numbers, arrival.extra.get("_placed")), (Some((1, 1103)), Some(&placed)));
        let ids = arrival.extra.get("_ids").and_then(Value::as_object);
        assert_eq!(
            super::super::scan::identified_by(ids, arrival.extra.get("_placed"), "show"),
            Some(("show", Some(30983), Some("tt0131179".to_string()))),
            "placed with its ids: the title is theirs"
        );
        // A TMDB id set by hand names it in its library's kind; an IMDb or TVDB id alone names nothing.
        for (ids, want) in [
            (json!({"tmdb": "1428"}), Some(("show", Some(1428), None))),
            (json!({"imdb": "tt0383126"}), None),
            (json!({"tvdb": "73388"}), None),
        ] {
            assert_eq!(super::super::scan::identified_by(ids.as_object(), None, "show"), want, "{ids}");
        }
        assert_eq!(super::super::scan::identified_by(None, Some(&placed), "show"), None, "a placement with no ids names nothing");
        let row = json!({"type": "episode", "librarySectionID": 2, "_source": "zurg", "_sid": path, "_path": path,
                         "_release": "Detective.Conan.S33.1080p.WEB.H264-GRP", "_ids": item.ids, "_placed": placed,
                         "Media": [{"Part": [{"file": format!("/{path}"), "size": 1}]}]});
        let (_, again) = pushed_item(&row).unwrap();
        assert_eq!(Item::parse(&source, &again).unwrap().placed.as_ref(), Some(&placed), "rebuilt for retry_blank and the start");
        let range = Item::parse(&source, &json!({"id": path, "path": path, "kind": "episode", "season": 1, "episode": 1103, "episode_end": 1104})).unwrap();
        assert_eq!(range.placed, Some(json!({"kind": "episode", "season": 1, "episode": 1103, "episode_end": 1104})));
        // What rex does not know places nothing, and the name is not read instead.
        for odd in [json!({"kind": "show", "season": 1, "episode": 2}), json!({"season": 1, "episode": 2})] {
            let mut odd = odd;
            odd["id"] = json!(path);
            odd["path"] = json!(path);
            assert_eq!(Item::parse(&source, &odd).unwrap().placed, None, "{odd}");
        }
        let movie = Item::parse(&source, &json!({"id": path, "path": path, "kind": "movie", "season": 1, "episode": 2})).unwrap();
        assert_eq!(movie.placed, Some(json!({"kind": "movie"})));
        let mut split = row.as_object().unwrap().clone();
        take_release(&mut split, &json!({"_source": "zurg", "_sid": "other", "_path": "shows/Other/Other.S01E01.mkv"}));
        assert!(split.get("_placed").is_none(), "another release's placement is not the split row's");
    }

    /// The production server's The Boys S03E01 (2026-10-14): its pack was identified through
    /// the pack's IMDb id as one title, so E02-E08 carried the same guid on the same season and
    /// were folded onto it as versions.
    #[test]
    fn episodes_sharing_a_guid_are_not_versions_of_one_another() {
        let episode = |index: i64, file: &str| {
            json!({"ratingKey": format!("12523{index}"), "type": "episode", "librarySectionID": 2,
                   "parentRatingKey": "125247", "index": index, "guid": "plex://movie/fa2808235df1f6cc0b74a3df",
                   "_sid": format!("shows/The.Boys.S03.2022.BDRemux.1080p.Ukr.Eng/{file}")})
        };
        let first = episode(1, "The.Boys.S03E01.Payback.1080p.BDRemux.UkrEng.mkv");
        let second = episode(2, "The.Boys.S03E02.The.Only.Man.in.the.Sky.1080p.BDRemux.UkrEng.mkv");
        assert!(version_slot(&first).is_some());
        assert_ne!(version_slot(&first), version_slot(&second));
        let mut again = second.clone();
        again["_sid"] = json!("shows/The.Boys.S03.2160p/The.Boys.S03E02.2160p.mkv");
        assert_eq!(version_slot(&second), version_slot(&again), "another release of E02 is still a version of it");
    }

    /// rex-fun's two libraries, each with a folder per zurg mount, and its feed rows from the
    /// folders no Location held (`fun_unheld_folders_1019.json`), which went to the first
    /// library of their kind. Its zurgs declaring libraries for some of those folders: each
    /// titled library is made once, on the declaring source's mount, except where a library of
    /// that type already names the directory (`anime`, in TV Shows for every mount); the rows
    /// then belong to them, a type a folder does not title still to the first of its kind.
    #[test]
    fn declared_directories_get_libraries_and_their_files_follow() {
        let dir = work("declared");
        let fixture: Value = serde_json::from_str(include_str!("../../../tests/fixtures/feed/fun_unheld_folders_1019.json")).unwrap();
        let catalog_path = dir.join("catalog.json");
        let catalog = json!({"machineIdentifier": "feed-test-machine", "friendlyName": "feed-test",
                             "sections": fixture["sections"], "items": fixture["items"]});
        std::fs::write(&catalog_path, serde_json::to_vec(&catalog).unwrap()).unwrap();
        let mut runtime = Runtime::load(RuntimeOptions { catalog: catalog_path, state: Some(dir.join("no-state")), ..RuntimeOptions::default() }).unwrap();
        let rd = Source::register("rd", &json!({"url": "http://127.0.0.1:9", "mount": "/mnt/zurg", "directories": [
            {"name": "anime", "shows": "Anime"},
            {"name": "OnePieceLatest", "shows": "One Piece"},
            {"name": "music", "shows": "Music Shows", "movies": "Music Films"}]})).unwrap();
        let usenet = Source::register("usenet", &json!({"url": "http://127.0.0.1:9", "mount": "/mnt/zurg_usenet",
                                                         "directories": [{"name": "recent", "movies": "Recent"}]})).unwrap();
        let plain = Source::register("other", &json!({"url": "http://127.0.0.1:9"})).unwrap();
        runtime.declare_libraries(&rd);
        runtime.declare_libraries(&usenet);
        runtime.declare_libraries(&plain);
        let made = |runtime: &Runtime| -> Vec<(String, String, String, Vec<String>)> {
            runtime.catalog.sections.iter().map(|section| (string_value(&section["key"]), string_value(&section["type"]),
                string_value(&section["title"]), runtime.locations(section))).collect()
        };
        let sections = made(&runtime);
        let want = |key: &str, kind: &str, title: &str, folder: &str| (key.to_string(), kind.to_string(), title.to_string(), vec![folder.to_string()]);
        assert_eq!(sections[2..], [want("3", "show", "One Piece", "/mnt/zurg/OnePieceLatest"), want("4", "movie", "Music Films", "/mnt/zurg/music"),
                                   want("5", "show", "Music Shows", "/mnt/zurg/music"), want("6", "movie", "Recent", "/mnt/zurg_usenet/recent")]);
        assert_eq!(runtime.added_sections.len(), 4, "kept in the state as Add Library's are");
        runtime.declare_libraries(&rd);
        runtime.declare_libraries(&usenet);
        assert_eq!(made(&runtime), sections, "registering again makes nothing");
        // A source with no mount: beside the first library's folder, as the seed puts its own.
        let bare = Source::register("bare", &json!({"url": "http://127.0.0.1:9", "directories": [{"name": "kids", "movies": "Kids"}]})).unwrap();
        runtime.declare_libraries(&bare);
        assert_eq!(made(&runtime).last(), Some(&want("7", "movie", "Kids", "/mnt/zurg_tb/kids")));
        runtime.catalog.sections.pop();

        fn pick(runtime: &Runtime, source: &Source, file: &str, kind: Option<&str>) -> String {
            let path = source.path_under_mount(file).unwrap();
            let mut pushed = json!({"id": path, "path": path, "file": file, "media_info": {"streams": [{"codec_type": "video"}]}});
            if let Some(kind) = kind {
                pushed["kind"] = json!(kind);
                pushed["ids"] = json!({"tmdb": 1});
            }
            runtime.feed_library(&Item::parse(source, &pushed).unwrap()).map(|library| library.title).unwrap_or_default()
        }
        let mut seen = Vec::new();
        for row in fixture["items"].as_array().unwrap() {
            let file = row.pointer("/Media/0/Part/0/file").and_then(Value::as_str).unwrap();
            let source = if file.starts_with("/mnt/zurg_usenet/") { &usenet } else { &rd };
            let kind = if row["type"] == "movie" { "movie" } else { "episode" };
            seen.push((file.split('/').nth(3).unwrap().to_string(), kind, pick(&runtime, source, file, Some(kind))));
        }
        let expect = |folder: &str, kind: &'static str, title: &str| (folder.to_string(), kind, title.to_string());
        assert_eq!(seen, [
            expect("recent", "episode", "TV Shows"),        // `recent` titles films only
            expect("music", "episode", "Music Shows"),      // not TV Shows, which is merely beside Music Films
            expect("audiobooks", "movie", "Movies"),        // declared by nobody
            expect("OnePieceLatest", "episode", "One Piece"),
            expect("recent", "movie", "Recent"),
            expect("audiobooks", "episode", "TV Shows"),
            expect("music", "movie", "Music Films"),
        ]);
        let unplaced = "/mnt/zurg/music/[KH] Sword Art Online - Extra Edition (BD 1920x1080 x264-10bit FLAC) [Dual-Audio]/Extra.mkv";
        assert_eq!(pick(&runtime, &rd, unplaced, None), "Music Films", "an unplaced file goes to the folder's movie library");
        let anime = "/mnt/zurg/anime/[SubsPlease] Frieren - 01 (1080p) [ABCD1234]/[SubsPlease] Frieren - 01 (1080p) [ABCD1234].mkv";
        assert_eq!(pick(&runtime, &rd, anime, Some("episode")), "TV Shows", "a folder TV Shows names stays there");
        // Another source pushing a declared directory fills the same library.
        let usenet_music = "/mnt/zurg_usenet/music/Release/Release.S01E01.mkv";
        assert_eq!(pick(&runtime, &usenet, usenet_music, Some("episode")), "Music Shows");

        // A library a person deleted is not made again; its files go to the first of their kind.
        let one_piece = runtime.catalog.sections.iter().position(|section| section["title"] == "One Piece").unwrap();
        let gone = runtime.catalog.sections.remove(one_piece);
        runtime.deleted_locations.extend(runtime.locations(&gone).into_iter().map(|path| json!({"path": path, "type": "show"})));
        runtime.declare_libraries(&rd);
        assert!(runtime.catalog.sections.iter().all(|section| section["title"] != "One Piece"), "made again");
        assert_eq!(runtime.deleted_locations, [json!({"path": "/mnt/zurg/OnePieceLatest", "type": "show", "told": true})], "said once");
        let latest = row_file(&fixture, "OnePieceLatest");
        assert_eq!(pick(&runtime, &rd, &latest, Some("episode")), "TV Shows");
        let _ = std::fs::remove_dir_all(&dir);

        fn row_file(fixture: &Value, folder: &str) -> String {
            fixture["items"].as_array().unwrap().iter().filter_map(|row| row.pointer("/Media/0/Part/0/file").and_then(Value::as_str))
                .find(|file| file.split('/').nth(3) == Some(folder)).unwrap().to_string()
        }
    }
}
