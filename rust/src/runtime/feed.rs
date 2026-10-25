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

use super::scan::{extension, is_sample, name_release, row_files, under, Arrival, Found, Library};
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

/// What was logged once since the start: a `directories` entry left out (`source`, entry), and
/// a row kept where it is because its releases sit in folders of different libraries (its key).
/// A source registers again at every push session and pushes every row in each snapshot.
static SAID: LazyLock<Mutex<HashSet<String>>> = LazyLock::new(|| Mutex::new(HashSet::new()));

/// Whether `what` is said for the first time since the start.
fn first_time(what: String) -> bool {
    SAID.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).insert(what)
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
    /// What the source calls this file's `media_info` (`probe`): kept on its version, so a later
    /// push may leave the probe out while it is the same (`wants`).
    probe: Option<String>,
    /// The source's explicit movie edition; absent preserves it, empty clears it.
    edition: Option<String>,
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
    /// What the source says the title is called and looks like (`display`): shown only while
    /// nothing identifies the row (`scan::dress`), and never over a field a person locked.
    display: Option<Map<String, Value>>,
    /// The sidecar subtitles the source serves beside the file (`subtitles`); `None` when it
    /// sent none, which keeps what the row has, as an absent `chapters` does.
    subtitles: Option<Vec<SourceSubtitle>>,
    /// The collections its title (a film, or an episode's show) is in, by name, in its library
    /// (`collections`); `None` when it sent none, which keeps what the title has.
    collections: Option<Vec<String>>,
}

/// One entry of an item's `subtitles`: a subtitle file under the source's url, which becomes
/// an external Stream of the item's own Part. Its language is the source's to say; rex reads
/// nothing off its name.
#[derive(Debug, Clone)]
struct SourceSubtitle {
    path: String,
    /// The extension, lower-cased: the Stream's `codec` and `format`, as an upload's form is.
    format: String,
    language: Option<String>,
    forced: bool,
    hearing_impaired: bool,
    size: Option<u64>,
}

/// The fields an entry sets only sometimes: a stream rewritten without them drops them.
const SUBTITLE_FIELDS: [&str; 6] = ["forced", "hearingImpaired", "language", "languageTag", "languageCode", "_sourceSize"];

/// The most text a sidecar subtitle may hold: a feature's SRT is 50-150 KB, an ASS with
/// typesetting a few MB, and the text is kept on its row.
const SUBTITLE_TEXT_CAP: usize = 4 << 20;

/// An item's `subtitles`. Absent, `null` (a Go nil slice) or no list is none sent, so the row
/// keeps what it has; `[]` removes them. An entry rex cannot use is left out and said once,
/// never the item: one odd file beside a film would otherwise keep the film off rex.
fn subtitle_entries(source: &str, id: &str, value: Option<&Value>) -> Option<Vec<SourceSubtitle>> {
    let entries = match value {
        None | Some(Value::Null) => return None,
        Some(Value::Array(entries)) => entries,
        Some(_) => {
            say(source, id, "subtitles must be a list: the row keeps the subtitles it has");
            return None;
        }
    };
    let check = |entry: &Value| -> Result<SourceSubtitle, String> {
        let path = entry.get("path").and_then(Value::as_str).map(|path| path.trim_matches('/')).filter(|path| !path.is_empty());
        let path = path.ok_or_else(|| format!("subtitle entry {entry}: no path"))?;
        if path.split('/').any(|segment| segment.is_empty() || segment == "." || segment == "..") {
            return Err(format!("subtitle {path}: path has an empty, . or .. segment"));
        }
        let name = path.rsplit('/').next().unwrap_or(path);
        let format = name.rsplit_once('.').map(|(_, extension)| extension.to_ascii_lowercase()).unwrap_or_default();
        if !SUBTITLE_FORMATS.contains(&format.as_str()) {
            return Err(format!("subtitle {path}: not a subtitle format an upload takes ({})", SUBTITLE_FORMATS.join(", ")));
        }
        let flag = |field: &str| match entry.get(field) {
            None | Some(Value::Null) => Ok(false),
            Some(Value::Bool(flag)) => Ok(*flag),
            Some(_) => Err(format!("subtitle {path}: {field} must be true or false")),
        };
        let language = match entry.get("language") {
            None | Some(Value::Null) => None,
            Some(Value::String(code)) => Some(code.trim().to_string()).filter(|code| !code.is_empty()),
            Some(_) => return Err(format!("subtitle {path}: language must be a code")),
        };
        let size = match entry.get("size") {
            None | Some(Value::Null) => None,
            Some(size) => Some(size.as_u64().ok_or_else(|| format!("subtitle {path}: size must be a whole number of bytes"))?),
        };
        if size.is_some_and(|size| size > SUBTITLE_TEXT_CAP as u64) {
            return Err(format!("subtitle {path}: over {} bytes", SUBTITLE_TEXT_CAP));
        }
        Ok(SourceSubtitle { path: path.to_string(), format, language, forced: flag("forced")?, hearing_impaired: flag("hearing_impaired")?, size })
    };
    let mut kept: Vec<SourceSubtitle> = Vec::new();
    for entry in entries {
        match check(entry) {
            Ok(subtitle) if kept.iter().any(|other| other.path == subtitle.path) => {
                say(source, id, &format!("subtitle {}: listed twice; left out", subtitle.path))
            }
            Ok(subtitle) => kept.push(subtitle),
            Err(reason) => say(source, id, &format!("{reason}; left out")),
        }
    }
    Some(kept)
}

/// What became of a subtitle rex could not use, said once since the start: a source pushes
/// every item again in each snapshot.
fn say(source: &str, id: &str, what: &str) {
    if first_time(format!("{source} {id} {what}")) {
        warning!("FEED {source} {id}: {what}");
    }
}

/// A pushed `display`, checked: the title's own `title`, `summary`, `date` (YYYY-MM-DD, or YYYY
/// for a film known only by its year), `thumb`
/// and `art`, its show's `title`, `summary`, `poster` and `art`, its season's `summary` and
/// `poster`. Artwork is an http(s) URL rex fetches as it fetches Plex's agent artwork, from
/// public hosts only; one under the source's own url is kept as `source://<name>/<path>` and
/// fetched from the source with its login, as its bytes are (a folder's thumbnails live on the
/// machine that serves them). Fields it does not know are left out; an empty one says nothing.
fn display_fields(display: &Map<String, Value>, source: &Source) -> Result<Map<String, Value>, String> {
    fn pick(from: &Map<String, Value>, fields: &[&str], within: &str, source: &Source) -> Result<Map<String, Value>, String> {
        let mut kept = Map::new();
        for field in fields {
            let Some(value) = from.get(*field).filter(|value| !value.is_null()) else { continue };
            let named = format!("display.{within}{field}");
            let raw = value.as_str().ok_or_else(|| format!("{named} must be a string"))?;
            let text = raw.trim();
            if text.is_empty() {
                continue;
            }
            let artwork = matches!(*field, "thumb" | "art" | "poster");
            if artwork && !(text.starts_with("https://") || text.starts_with("http://")) {
                return Err(format!("{named} must be an http(s) URL"));
            }
            if let Some(path) = source.own_path(text).filter(|_| artwork) {
                kept.insert(field.to_string(), json!(format!("source://{}/{path}", source.name)));
                continue;
            }
            let date = |text: &str| {
                let bytes = text.as_bytes();
                (bytes.len() == 4 || bytes.len() == 10 && bytes[4] == b'-' && bytes[7] == b'-')
                    && bytes.iter().enumerate().all(|(at, byte)| at == 4 || at == 7 || byte.is_ascii_digit())
            };
            if *field == "date" && !date(text) {
                return Err(format!("{named} must be YYYY-MM-DD or YYYY"));
            }
            // A title or summary is shown as its source wrote it; a URL or date is read trimmed.
            let artwork_or_date = artwork || *field == "date";
            kept.insert(field.to_string(), json!(if artwork_or_date { text } else { raw }));
        }
        Ok(kept)
    }
    let mut kept = pick(display, &["title", "summary", "date", "thumb", "art"], "", source)?;
    for (part, fields) in [("show", &["title", "summary", "poster", "art"][..]), ("season", &["summary", "poster"][..])] {
        match display.get(part) {
            None | Some(Value::Null) => {}
            Some(Value::Object(inner)) => {
                let inner = pick(inner, fields, &format!("{part}."), source)?;
                if !inner.is_empty() {
                    kept.insert(part.to_string(), Value::Object(inner));
                }
            }
            Some(_) => return Err(format!("display.{part} must be an object")),
        }
    }
    Ok(kept)
}

/// The size ffprobe read off a file (`format.size`, a string in its JSON), for an item sent
/// without `size`: the probe is required anyway. None for a playlist, whose size is its text's.
fn probed_size(value: &Value) -> Option<u64> {
    let format = value.pointer("/media_info/format")?;
    if format.get("format_name").and_then(Value::as_str) == Some("hls") {
        return None;
    }
    match format.get("size")? {
        Value::String(text) => text.trim().parse().ok(),
        other => other.as_u64(),
    }
    .filter(|size| *size > 0)
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
        // A source that serves an HLS playlist (a YouTube relay) names it `.m3u8`, a video only
        // by its probe: rex plays one through a conversion, never as a file.
        let playlist = file.to_ascii_lowercase().ends_with(".m3u8")
            && value.pointer("/media_info/format/format_name").and_then(Value::as_str) == Some("hls");
        if !is_video(&file) && !playlist {
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
            // TMDB's id for the episode its source placed the file on: the content id rex
            // exchanges for Plex's episode (`identify::plex_episode`).
            if let (true, Some(id)) = (kind == "episode", number("tmdb_episode", 1)) {
                placed.insert("tmdb_episode".to_string(), json!(id));
            }
            Value::Object(placed)
        });
        let edition = match value.get("edition_title") {
            None => None,
            Some(Value::String(title)) if text("kind").as_deref() == Some("movie") => Some(title.trim().to_string()),
            Some(Value::String(_)) => return Err(refuse("edition_title requires kind movie")),
            Some(_) => return Err(refuse("edition_title must be a string")),
        };
        let display = match value.get("display") {
            None | Some(Value::Null) => None,
            Some(Value::Object(display)) => Some(display_fields(display, source).map_err(|reason| refuse(&reason))?),
            Some(_) => return Err(refuse("display must be an object")),
        };
        let collections = match value.get("collections") {
            None | Some(Value::Null) => None,
            Some(Value::Array(names)) => {
                let mut kept: Vec<String> = Vec::new();
                for name in names {
                    let Some(name) = name.as_str().map(str::trim).filter(|name| !name.is_empty()) else {
                        return Err(refuse("collections must be a list of names"));
                    };
                    if !kept.iter().any(|seen| seen.to_lowercase() == name.to_lowercase()) {
                        kept.push(name.to_string());
                    }
                }
                Some(kept)
            }
            Some(_) => return Err(refuse("collections must be a list of names")),
        };
        Ok(Self {
            collections,
            subtitles: subtitle_entries(&source.name, &id, value.get("subtitles")),
            placed,
            edition,
            display,
            extra: (text("kind").as_deref() == Some("extra")).then(|| super::scan::pushed_extra_type(text("extra_type").as_deref())),
            kept: None,
            library: value.get("library").map(string_value).filter(|v| !v.is_empty() && v != "null"),
            size: value.get("size").and_then(Value::as_u64).filter(|size| *size > 0).or_else(|| probed_size(value)).unwrap_or(0),
            hash: text("hash").map(|hash| hash.to_ascii_lowercase()),
            probe: text("probe").filter(|probe| probe.len() <= 128),
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
                          "size": self.size, "media_info": self.media_info, "probe": self.probe}),
            extra: Map::from_iter([
                ("_source".to_string(), json!(source)),
                ("_sid".to_string(), json!(self.id)),
                ("_path".to_string(), json!(self.path)),
                ("_ids".to_string(), Value::Object(self.ids.clone())),
            ]
            .into_iter()
            .chain(self.folder.as_ref().map(|folder| ("_folder".to_string(), json!(folder))))
            .chain(self.placed.as_ref().map(|placed| ("_placed".to_string(), placed.clone())))
            .chain(self.display.as_ref().map(|display| ("_display".to_string(), Value::Object(display.clone()))))
            .chain(self.edition.as_ref().into_iter().flat_map(|edition| [
                ("_editionTitle".to_string(), json!(edition)),
                ("editionTitle".to_string(), json!(edition)),
            ]))),
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
    /// Items sent with a `probe` and no `media_info` whose probe rex needs: applied by none of
    /// their fields until the source sends them again with it.
    wants: Vec<String>,
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
        .into_iter()
        .chain((!self.wants.is_empty()).then(|| ("wants".to_string(), json!(self.wants))))
        .collect()
    }
}

/// How each item is applied, decided under the lock before identification runs.
enum Plan {
    /// A row the source already has (or the old scanner made for this file: `adopt`); moved
    /// as it is to the library its folder now belongs to (`relocate`), or identified again.
    Row { key: String, adopt: bool, reidentify: bool, relocate: bool, refresh_title: bool },
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
    // Keep direct callers behind the same boundary as the socket dispatcher.
    if CONNECTION.with(std::cell::Cell::get).is_some_and(|(peer, _)| !source_peer(peer)) {
        return lock(shared).route(request);
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
            let mut public = source.public();
            for ignored in &source.ignored {
                let reason = string_value(&ignored["reason"]);
                if first_time(format!("{name} {reason}")) {
                    warning!("SOURCES {name}: {reason}; that entry is ignored");
                }
            }
            if !source.ignored.is_empty() {
                public["ignored"] = json!(source.ignored);
            }
            let mut guard = lock(shared);
            let changed = guard.sources.iter().find(|known| known.name == name).map(|known| &known.record) != Some(&source.record);
            let declared = source.clone();
            if changed {
                info!("SOURCES {name} registered at {}", public["url"].as_str().unwrap_or_default());
                guard.invalidate_source_analysis(name);
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
    guard.invalidate_source_analysis(name);
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
        let mut guard = lock(shared);
        let rows = guard.feed_rows(&source.name, items.iter().map(|item| item.id.as_str()));
        let legacy = guard.legacy_rows_by_file(items.iter().map(|item| item.file.as_str()));
        let named = guard.plex.is_some();
        for directory in items.iter().filter_map(|item| item.path.split_once('/').map(|(directory, _)| directory)) {
            if guard.feed_directories.entry(source.name.clone()).or_default().insert(directory.to_string()) {
                guard.dirty = true;
            }
        }
        let mut batch_files: HashMap<String, String> = HashMap::new();
        for mut item in items {
            // A `kind` the source sent says which it is (`feed_library`); no name guesses.
            let Some(library) = guard.feed_library(&item) else {
                counts.rejected.push(json!({"id": item.id, "reason": format!("no movie or show library holds {}", item.file)}));
                continue;
            };
            if let Some(seen) = seen.as_deref_mut() {
                seen.insert(item.id.clone());
            }
            let mut existing = rows.get(&item.id).map(|key| (key.clone(), false)).or_else(|| {
                legacy.get(&item.file).map(|key| (key.clone(), true))
            });
            // Sent by its digest alone: applied only where rex needs no probe, else asked for
            // whole. A snapshot sent every file's probe every six hours for rows that already had
            // it: 41,549 items, about 400 MB, all but 2% unchanged (the production server, 2026-10-24).
            if let (None, Some(probe)) = (&item.media_info, &item.probe) {
                // A person's match keeps its row whole: no extra or version leaves it.
                let kept = existing.as_ref().is_some_and(|(key, legacy)| {
                    !legacy && guard.catalog.get(key).is_some_and(|row| {
                        let (same, splits) = probe_kept(row, &source.name, &item, probe);
                        same && (!splits || guard.matched_by_hand(row))
                    })
                });
                if !kept && !retry {
                    counts.wants.push(item.id.clone());
                    continue;
                }
            }
            // A new id whose file another id of this source already reads made no row and was
            // counted nowhere: re-pushing files under new ids looked like a push that did nothing
            // (rex-fun, 2026-10-23). A file is one row's, so the new id is refused, naming the holder.
            if existing.is_none() && !retry {
                let holder = guard.file_holder(&source.name, &item.file).or_else(|| batch_files.get(&item.file).cloned());
                if let Some(holder) = holder.filter(|holder| *holder != item.id) {
                    let holder = if holder.is_empty() { "a row no source pushed".to_string() } else { holder };
                    counts.rejected.push(json!({"id": item.id, "reason": format!("{} is already {holder}'s file", item.file)}));
                    continue;
                }
                batch_files.insert(item.file.clone(), item.id.clone());
            }
            // The source now says a file it pushed as an episode or a film is an extra (zurg
            // 27e589f3 on the production server: TBBT's interviews, InuYasha's clean endings): its row is no
            // title any more and the file becomes a clip, as a first push of it would. A version
            // of a row holding several leaves that row alone; a person's match stays theirs.
            if let (Some(_), Some((key, _)), false) = (item.extra, &existing, retry) {
                let titled = guard.catalog.get(key).is_some_and(|row| {
                    matches!(row.get("type").and_then(Value::as_str), Some("episode" | "movie")) && !guard.matched_by_hand(row)
                });
                if titled {
                    if !guard.drop_version(key, &source.name, &item.id) {
                        guard.purge_row(key);
                    }
                    guard.dirty = true;
                    existing = None;
                }
            }
            // A version of a row of several that its source now places on other numbers than the
            // row's leaves the row and is placed on its own, as an extra's does above: the row was
            // never re-identified by one of its releases, so SpongeBob's Frys S10 pack, which zurg
            // placed S10E24 as TMDB's S9E15 (production server, 2026-10-23), sat on S10E24 beside
            // three other releases' files for good while its own version stored the new placement.
            if let (Some(numbers), Some((key, _)), false, true) =
                (super::scan::placed_numbers(item.placed.as_ref()), &existing, retry, item.media_info.is_some())
            {
                let leaves = guard.catalog.get(key).is_some_and(|row| {
                    let here = row.get("parentIndex").and_then(Value::as_i64).zip(row.get("index").and_then(Value::as_i64));
                    row.get("type").and_then(Value::as_str) == Some("episode")
                        && versions(row).len() > 1
                        && here != Some(numbers)
                        && !guard.matched_by_hand(row)
                });
                if leaves && guard.drop_version(key, &source.name, &item.id) {
                    guard.dirty = true;
                    existing = None;
                }
            }
            let plan = match existing {
                Some((key, adopt)) => {
                    let row = guard.catalog.get(&key);
                    let had = row.map(row_ids).unwrap_or_default();
                    // ponytail: a row holding several releases is not re-identified by one of
                    // them; splitting the release back out is Split's job.
                    // An id that named nothing when it came (Plex unreachable, no token) is tried
                    // again on each push. The report keeps the TMDB id its source placed it by
                    // whatever Plex answered, so only a Plex guid says it was named: asking for
                    // both left a sponsor's 7,035 releases pushed before the claim unnamed
                    // through every push (2026-10-24).
                    let unnamed = row
                        .and_then(|row| row.get("_identify"))
                        .is_some_and(|report| report.get("plex").is_none_or(Value::is_null));
                    // A file whose library changed is placed again in the one it belongs to now.
                    let moved = row.is_some_and(|row| {
                        row.get("librarySectionID").map(string_value).as_deref() != Some(library.key.as_str())
                    });
                    // Placed somewhere else than before, it is placed again, providers or none. A
                    // push with no placement leaves it where the last one put it (absent is
                    // unknown), unless a TMDB id set by hand names it again.
                    let replaced = item.placed.is_some() && row.is_some_and(|row| version_placed(row, 0) != item.placed.as_ref());
                    let by_hand = row.is_some_and(|row| guard.matched_by_hand(row));
                    let several = row.is_some_and(|row| versions(row).len() > 1);
                    let refresh_title = !by_hand && row.is_some_and(|row| {
                        let title = row.get("grandparentRatingKey").map(string_value)
                            .and_then(|key| guard.catalog.get(&key)).unwrap_or(row);
                        !several && (version_release(row, 0) != Some(item.name.as_str())
                            || row.get("_release").and_then(Value::as_str) != Some(item.name.as_str()))
                            || item.identifiable(&library.kind)
                                && title.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("plex://"))
                                && title.get("_titleGuid") != title.get("guid")
                    });
                    // A row of the type its new library takes moves there as it is, on its key,
                    // with what a person did to it and to its show (`relocate`): one a person
                    // matched by hand, one holding several releases, or one whose source placed
                    // it where it was. Re-identifying it lost a hand match, kept a row of two
                    // releases where it was, and dropped its show's edits with the show.
                    let fits = row.is_some_and(|row| {
                        matches!((row.get("type").and_then(Value::as_str), library.kind.as_str()), (Some("movie"), "movie") | (Some("episode"), "show"))
                    });
                    let relocate = moved && fits && (by_hand || several || !replaced) && row.is_some_and(|row| guard.releases_agree(row, &item, &library));
                    // What a person matched is theirs: no push and no retry identifies it again.
                    // A clip is its parent's: no library, so never "moved", and no title of its own.
                    let reidentify = !relocate
                        && !row.is_some_and(|row| by_hand || row.get("type").and_then(Value::as_str) == Some("clip"))
                        && (!several || row.is_some_and(|row| unmatched_alike(row) && version_placed(row, 0) == item.placed.as_ref()))
                        && (moved
                            || replaced
                            // Placed, but Plex's agent had no row for its numbers when it came
                            // (its season was read to 50 rows, Animaniacs' S1E109): asked again on
                            // each push, which changes nothing while the answer is the same.
                            || super::scan::placed_numbers(item.placed.as_ref()).is_some()
                                && item.identifiable(&library.kind)
                                && row.and_then(|row| row.get("guid")).and_then(Value::as_str).is_some_and(|guid| guid.starts_with("local://"))
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
                    Plan::Row { key, adopt, reidentify, relocate, refresh_title }
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
        if matches!(plan, Plan::Show | Plan::Row { reidentify: true, .. } | Plan::Row { refresh_title: true, .. }) {
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
    let sidecars: Vec<(String, Vec<SourceSubtitle>)> =
        plans.iter().filter_map(|(item, _, _)| Some((item.id.clone(), item.subtitles.clone()?))).collect();
    let collected: Vec<(String, Vec<String>)> =
        plans.iter().filter_map(|(item, _, _)| Some((item.id.clone(), item.collections.clone()?))).collect();
    let mut unchanged = HashSet::new();
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
            Plan::Row { key, adopt, reidentify, relocate, refresh_title } => {
                guard.release(&held);
                if guard.catalog.get(&key).is_none() {
                    counts.unchanged += 1;
                    continue;
                }
                let (key, edition_changed) = guard.feed_edition_key(&key, &source.name, &item);
                let mut changed = edition_changed;
                if refresh_title {
                    if let Some(arrival) = arrivals.get(&index) {
                        changed |= guard.refresh_source_title(&key, &item, arrival);
                    }
                }
                changed |= guard.feed_update_row(&key, &source.name, &item, adopt, &mut probed);
                if relocate {
                    changed |= guard.relocate(&library, &key);
                }
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
                }
                // The placement it now sits on, also where it already sat there and nothing moved:
                // without it every push placed it again and no pass knew it placed. A row kept
                // where a person matched it records it too: a Fix Match of its show reads the TMDB
                // episode id its source sent (`identify_show_children`), which Ben 10: Omniverse's
                // rows under a matched show never kept.
                if let (Some(placed), Some(row)) = (&item.placed, guard.catalog.get_mut(&key).and_then(Value::as_object_mut)) {
                    if row.get("_placed") != Some(placed) {
                        row.insert("_placed".to_string(), placed.clone());
                        changed = true;
                    }
                }
                if changed {
                    counts.updated += 1;
                    guard.dirty = true;
                    touched.insert(key.clone());
                } else {
                    counts.unchanged += 1;
                    unchanged.insert(item.id.clone());
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
    // Last, on the rows as this push left them: a new release folded into a title is a Part of
    // that row by now, and a probe filled in has its streams.
    let (mut guard, resubtitled) = put_subtitles(shared, guard, source, &sidecars);
    let rows = guard.feed_rows(&source.name, collected.iter().map(|(id, _)| id.as_str()));
    let mut regrouped = HashSet::new();
    for (id, names) in &collected {
        if rows.get(id).is_some_and(|key| guard.put_collections(key, names)) {
            regrouped.insert(id.clone());
        }
    }
    if !regrouped.is_empty() {
        guard.dirty = true;
    }
    let moved = resubtitled.union(&regrouped).filter(|id| unchanged.contains(*id)).count();
    counts.unchanged -= moved;
    counts.updated += moved;
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
    if !touched.is_empty() {
        super::analysis::schedule_for_sources(shared, &touched.into_iter().collect::<Vec<_>>());
    }
    counts
}

/// Each item's sidecar subtitles on its own Part. Which texts a push needs is read under the
/// lock, they are fetched off it, from the source as a Part's bytes are, and the streams are
/// written under it again: a feed listing built under the lock held every Plex client 26 s
/// (2026-10-19). A text is fetched for a path new to the Part or a `size` that changed; a
/// failed fetch keeps the stream the Part had, and a new one is absent until a later push.
/// Answers the ids whose subtitles changed.
fn put_subtitles<'a>(
    shared: &'a SharedRuntime,
    mut guard: std::sync::MutexGuard<'a, Runtime>,
    source: &Source,
    wanted: &[(String, Vec<SourceSubtitle>)],
) -> (std::sync::MutexGuard<'a, Runtime>, HashSet<String>) {
    // A snapshot pushes every item again: one whose Part already shows its list is passed over
    // here, so an unchanged push costs one read of each Part.
    let (mut fetch, mut pending) = (Vec::new(), Vec::new());
    let rows = guard.feed_rows(&source.name, wanted.iter().map(|(id, _)| id.as_str()));
    for (id, subtitles) in wanted {
        let Some(streams) = rows.get(id).and_then(|key| guard.item_streams(key, &source.name, id)) else { continue };
        let ours: Vec<&Value> = streams.iter().filter(|stream| stream.get("_sourceSubtitle").is_some()).collect();
        if ours.len() == subtitles.len() && subtitles.iter().all(|subtitle| ours.iter().any(|stream| subtitle.shown_by(stream))) {
            continue;
        }
        fetch.extend(subtitles.iter().filter(|subtitle| !ours.iter().any(|stream| subtitle.kept_by(stream))).map(|subtitle| (id, subtitle)));
        pending.push((id, subtitles));
    }
    let mut texts: HashMap<(&str, &str), String> = HashMap::new();
    if !fetch.is_empty() {
        drop(guard);
        for (id, subtitle) in fetch {
            match subtitle_text(source, &subtitle.path) {
                Ok(text) => {
                    texts.insert((id.as_str(), subtitle.path.as_str()), text);
                }
                Err(reason) => say(&source.name, id, &format!("subtitle {} not read: {reason}", subtitle.path)),
            }
        }
        guard = lock(shared);
    }
    let rows = guard.feed_rows(&source.name, pending.iter().map(|(id, _)| id.as_str()));
    let mut changed = HashSet::new();
    for (id, subtitles) in pending {
        if rows.get(id).is_some_and(|key| guard.write_subtitles(key, &source.name, id, subtitles, &texts)) {
            changed.insert(id.clone());
        }
    }
    if !changed.is_empty() {
        guard.dirty = true;
    }
    (guard, changed)
}

/// A sidecar's text, read as a Part's bytes are (`Source::open`: its login, its redirects).
fn subtitle_text(source: &Source, path: &str) -> Result<String, String> {
    let mut response = source.open(path, &[])?;
    if !(200..300).contains(&response.status) {
        return Err(format!("the source answered {}", response.status));
    }
    let raw = response.read_body(SUBTITLE_TEXT_CAP)?;
    Ok(super::subtitles::decode_text(&raw).0)
}

impl SourceSubtitle {
    /// Whether `stream` already holds this file's text: the same path, and the same size when
    /// the source sends one.
    fn kept_by(&self, stream: &Value) -> bool {
        stream.get("_sourceSubtitle").and_then(Value::as_str) == Some(self.path.as_str())
            && (self.size.is_none() || stream.get("_sourceSize").and_then(Value::as_u64) == self.size)
    }

    /// Whether `stream` is this entry as it stands: its text kept and every field it sets the
    /// same, none it no longer sets left over.
    fn shown_by(&self, stream: &Value) -> bool {
        let fresh = self.stream(0, None);
        self.kept_by(stream)
            && fresh.iter().all(|(field, value)| matches!(field.as_str(), "id" | "key") || stream.get(field) == Some(value))
            && SUBTITLE_FIELDS.iter().all(|field| fresh.contains_key(*field) || stream.get(*field).is_none())
    }

    /// Its Stream, in the order a real PMS answers an on-disk sidecar (the 1.42.2 reference's
    /// `Subclip (2024).fr.srt`, `fixtures/transcode/subtitles_s1422.json`, and a real 1.43
    /// library's `.en.srt` in `fixtures/decisions/rows.json`): a `/library/streams/{id}` key and
    /// the codec as its `format`, which titles it `English (SRT External)`, and no title. The
    /// forced and hearing-impaired flags sit where a real PMS puts them on an embedded track; no
    /// sidecar carrying one was measured. `_content` serves it as an upload's is served; `None`
    /// leaves the text a kept stream has.
    fn stream(&self, id: i64, text: Option<String>) -> Map<String, Value> {
        let mut fields = Map::from_iter([
            ("id".to_string(), json!(id)),
            ("key".to_string(), json!(format!("/library/streams/{id}"))),
            ("streamType".to_string(), json!(3)),
            ("canAutoSync".to_string(), json!(false)),
        ]);
        if self.forced {
            fields.insert("forced".to_string(), json!(true));
        }
        fields.insert("codec".to_string(), json!(self.format));
        fields.extend(self.language.as_deref().map(mediainfo::code_language).unwrap_or_default());
        if self.hearing_impaired {
            fields.insert("hearingImpaired".to_string(), json!(true));
        }
        fields.insert("format".to_string(), json!(self.format));
        mediainfo::stream_titles(&mut fields, None, None);
        if let Some(text) = text {
            fields.insert("_content".to_string(), json!(text));
        }
        fields.insert("_sourceSubtitle".to_string(), json!(self.path));
        if let Some(size) = self.size {
            fields.insert("_sourceSize".to_string(), json!(size));
        }
        fields
    }
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
            || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("plex://movie/"))
            || placed_unmatched(row.get("guid"), row.get("_placed"), show.as_ref().and_then(|show| show.get("guid")));
    }
    !has(row.get("_poster_url"))
}

/// An episode its source placed (numbers or a TMDB episode id) still on `local://` under a show
/// Plex's agent named: Plex had no row for it when it came, and only a push named it again.
/// Animaniacs' rows on the production server stayed unmatched after the agent's answers were read
/// whole (43c6fd5c, 2026-10-23) until zurg's next snapshot, six hours on.
fn placed_unmatched(guid: Option<&Value>, placed: Option<&Value>, show_guid: Option<&Value>) -> bool {
    let placed = placed.filter(|placed| super::scan::placed_numbers(Some(placed)).is_some() || super::scan::placed_tmdb_episode(Some(placed)).is_some());
    guid.and_then(Value::as_str).is_some_and(|guid| guid.starts_with("local://"))
        && placed.is_some()
        && show_guid.and_then(Value::as_str).is_some_and(|guid| guid.starts_with("plex://show/"))
}

/// A row of several releases still on `local://`, each placed as its first: named by one, all are named
/// alike. A row of several was never retried nor re-identified by a push, so Re:ZERO's S1E79, three
/// releases of `S04E13` zurg placed on TMDB's episode 7130159, stayed unmatched on the production
/// server while Plex's agent listed that episode (2026-10-24).
fn unmatched_alike(row: &Value) -> bool {
    let first = version_placed(row, 0);
    row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("local://"))
        && first.is_some()
        && (1..versions(row).len()).all(|index| version_placed(row, index) == first)
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
    if let Some(edition) = row.get("_editionTitle").filter(|edition| edition.is_string()) {
        item["edition_title"] = edition.clone();
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
            for field in ["_source", "_sid", "_path", "_folder", "_release", "_ids", "_placed", "_editionTitle", "_hash"] {
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
    "_release_path", "_probePending", "_hash", "_placed", "_titleGuid", "_show_titleGuid",
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
    for field in ["_folder", "_placed", "_editionTitle", "_hash"] {
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
        self.feed_directories.remove(source);
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
    /// episode whose show has none -- or an episode named as a film, or one placed that Plex's
    /// agent has not named under a show it named (`placed_unmatched`), unless a person matched or
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
            || versions(row).len() > 1 && !unmatched_alike(row)
            || self.matched_by_hand(row)
            || row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("tv.plex.agents.none://"))
        {
            return false;
        }
        if kind == Some("episode") {
            let show = row.get("grandparentRatingKey").map(string_value).and_then(|show| self.catalog.get(&show));
            if placed_unmatched(row.get("guid"), version_placed(row, 0), show.and_then(|show| show.get("guid"))) {
                return true;
            }
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

    /// The folder a source's directories are libraries under: its `mount`, else where the seed
    /// put its own (`/mnt/zurg` of `/mnt/zurg/movies`), without a trailing `/`.
    pub(super) fn library_prefix(&self, source: &Source) -> String {
        let playable = |section: &&Value| matches!(section.get("type").and_then(Value::as_str), Some("movie" | "show"));
        let prefix = source.mount.clone().unwrap_or_else(|| {
            let seeded = self.catalog.sections.iter().filter(playable).flat_map(|section| self.locations(section)).next();
            seeded.and_then(|folder| folder.trim_end_matches('/').rsplit_once('/').map(|(parent, _)| parent.to_string())).unwrap_or_else(|| crate::catalog_tool::DEFAULT_MOUNT.to_string())
        });
        prefix.trim_end_matches('/').to_string()
    }

    /// The folders Add Library's picker offers for the sources' directories: each directory a
    /// source pushed from or declared, under its prefix (`/mnt/zurg/anime`).
    pub(super) fn feed_folders(&self) -> BTreeSet<String> {
        let mut folders = BTreeSet::new();
        for source in &self.sources {
            let prefix = self.library_prefix(source);
            let pushed = self.feed_directories.get(&source.name).into_iter().flatten().cloned();
            for directory in pushed.chain(source.directories.iter().map(|entry| entry.name.clone())) {
                folders.insert(format!("{prefix}/{directory}"));
            }
        }
        folders
    }

    /// A registration's `directories`: each library it titles, made unless one of that type
    /// already has a folder naming the directory (seeded, added by hand, made before and
    /// perhaps renamed since) or a person deleted one there (`deleted_locations`). A movie
    /// library comes first, so an unplaced file, which goes to the first library holding its
    /// folder, goes to a movie library there as it does anywhere else. rex never deletes or
    /// retitles a library this made; a directory dropped from the list keeps its library.
    pub(super) fn declare_libraries(&mut self, source: &Source) {
        let prefix = self.library_prefix(source);
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

    /// A changed source edition leaves a folded row rather than relabeling its other
    /// versions. A person's locked edition remains authoritative.
    fn feed_edition_key(&mut self, key: &str, source: &str, item: &Item) -> (String, bool) {
        let Some(edition) = item.edition.as_deref() else { return (key.to_string(), false) };
        let Some(original) = self.catalog.get(key).cloned() else { return (key.to_string(), false) };
        let Some(object) = original.as_object() else { return (key.to_string(), false) };
        if original.get("type").and_then(Value::as_str) != Some("movie") {
            return (key.to_string(), false);
        }
        let mut media = tagged_media(&original);
        let index = versions(&original).into_iter().find(|(of, sid, _)| of == source && sid == &item.id).map_or(0, |(_, _, index)| index);
        let raw_changed = media.get(index).and_then(|media| media.get("_editionTitle")).and_then(Value::as_str) != Some(edition);
        let title_changed = !super::edit::field_locked(object, "editionTitle")
            && original.get("editionTitle").and_then(Value::as_str).unwrap_or("") != edition;
        if !raw_changed && !title_changed {
            return (key.to_string(), false);
        }
        if let Some(selected) = media.get_mut(index).and_then(Value::as_object_mut) {
            selected.insert("_editionTitle".to_string(), json!(edition));
        }
        if title_changed && media.len() > 1 {
            self.freeze_merged_collections(key);
            let selected = media.remove(index);
            if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                row.insert("Media".to_string(), json!(media));
                name_by_release(row, &media[0]);
                if let Some(duration) = media[0].get("duration") { row.insert("duration".to_string(), duration.clone()); }
            }
            let new_key = self.mint_key();
            let mut split = original;
            if let Some(row) = split.as_object_mut() {
                take_release(row, &selected);
                if let Some(duration) = selected.get("duration") { row.insert("duration".to_string(), duration.clone()); }
                row.insert("ratingKey".to_string(), json!(new_key));
                row.insert("key".to_string(), json!(format!("/library/metadata/{new_key}")));
                row.insert("editionTitle".to_string(), json!(edition));
                row.insert("_editionTitle".to_string(), json!(edition));
            }
            self.catalog.add(split.clone());
            self.deleted.remove(&new_key);
            for collection in self.collections.values_mut().filter(|collection| !collection.smart) {
                if let Some(position) = collection.items.iter().position(|item| item == key) {
                    collection.items.insert(position + 1, new_key.clone());
                }
            }
            self.push_created_frames(&new_key);
            self.push_timeline_entry(&split, 5, None, None, None, None);
            if let Some(remaining) = self.catalog.get(key).cloned() {
                self.push_timeline_entry(&remaining, 5, None, None, None, None);
            }
            self.dirty = true;
            return (new_key, true);
        }
        if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            row.insert("Media".to_string(), json!(media));
            if index == 0 {
                row.insert("_editionTitle".to_string(), json!(edition));
            }
            if title_changed {
                row.insert("editionTitle".to_string(), json!(edition));
            }
        }
        (key.to_string(), true)
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
        if let Some(hash) = &item.hash {
            let previous = row.pointer(&format!("/Media/{index}/_hash")).or_else(|| row.get("_hash").filter(|_| index == 0));
            if previous.and_then(Value::as_str) != Some(hash.as_str()) {
                if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                    if let Some(media) = object.get_mut("Media").and_then(|media| media.get_mut(index)).and_then(Value::as_object_mut) {
                        media.insert("_hash".to_string(), json!(hash));
                    }
                    if index == 0 { object.insert("_hash".to_string(), json!(hash)); }
                }
                changed = true;
            }
        }
        if let (Some(probe), Some(_)) = (&item.probe, &item.media_info) {
            if row.pointer(&format!("/Media/{index}/_probe")).and_then(Value::as_str) != Some(probe.as_str()) {
                if let Some(media) = self.catalog.get_mut(key).and_then(|row| row.get_mut("Media")).and_then(|media| media.get_mut(index)).and_then(Value::as_object_mut) {
                    media.insert("_probe".to_string(), json!(probe));
                    changed = true;
                }
            }
        }
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
        if index == 0 && row.get("_release").and_then(Value::as_str) != Some(item.name.as_str()) {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("_release".to_string(), json!(item.name));
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
        } else if let (Some(probe), Some([old]), true) =
            (&item.media_info, row.get("Media").and_then(Value::as_array).map(Vec::as_slice), item.path.ends_with(".m3u8"))
        {
            // A playlist probed with streams at other indexes or of other codecs is another stream
            // under the same id: a Stremio addon's live channel back from another mirror
            // (2026-10-24). The conversion maps tracks by these indexes, so the row takes the new
            // streams. Only a playlist: a file does not change under its id, and building every
            // pushed file's Media to compare would cost each push of a large library.
            let part = old.pointer("/Part/0").cloned().unwrap_or_default();
            let file = part.get("file").and_then(Value::as_str).unwrap_or_default();
            let fresh = mediainfo::build_media(
                probe,
                old.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("key").and_then(Value::as_str).unwrap_or_default(),
                file,
                part.get("size").and_then(Value::as_i64).unwrap_or(0),
                Some(&extension(file)),
            );
            if stream_layout(&fresh) != stream_layout(old) {
                probed.push((key.to_string(), probe.clone()));
                changed = true;
            }
        }
        if let Some(display) = &item.display {
            changed |= self.redress(key, display);
        }
        changed
    }

    /// The Part a source's item `id` is on row `key` (its version's), as a JSON pointer.
    fn item_part(&self, key: &str, source: &str, id: &str) -> Option<String> {
        let row = self.catalog.get(key)?;
        let index = versions(row).into_iter().find(|(of, sid, _)| of == source && sid == id)?.2;
        row.pointer(&format!("/Media/{index}/Part/0")).map(|_| format!("/Media/{index}/Part/0"))
    }

    /// The streams of the Part a source's item `id` is on row `key`.
    fn item_streams(&self, key: &str, source: &str, id: &str) -> Option<std::borrow::Cow<'_, [Value]>> {
        let at = self.item_part(key, source, id)?;
        Some(crate::catalog::part_streams(self.catalog.get(key)?.pointer(&at)?))
    }

    /// The item's sidecar subtitles on its Part, as `wanted` lists them: one whose text was
    /// fetched (`texts`) is written, on the id its path had; one the Part holds stays, its
    /// fields brought up to date; one the Part holds that the list no longer names goes. Other
    /// subtitles -- uploads, search results, embedded tracks -- are never touched. Answers
    /// whether the Part changed.
    fn write_subtitles(&mut self, key: &str, source: &str, id: &str, wanted: &[SourceSubtitle], texts: &HashMap<(&str, &str), String>) -> bool {
        let (Some(at), Some(old)) = (self.item_part(key, source, id), self.item_streams(key, source, id).map(std::borrow::Cow::into_owned)) else {
            return false;
        };
        let path = |stream: &Value| stream.get("_sourceSubtitle").and_then(Value::as_str).map(str::to_string);
        // The stream each entry has now: fresh fields over the Part's own (its selection kept).
        let mut fresh: Vec<(&str, Value)> = Vec::new();
        for subtitle in wanted {
            let had = old.iter().find(|stream| path(stream).as_deref() == Some(subtitle.path.as_str()));
            let text = texts.get(&(id, subtitle.path.as_str())).cloned();
            if text.is_none() && !had.is_some_and(|had| subtitle.kept_by(had)) {
                // Not fetched: a new one waits for a later push, a changed one keeps its old text.
                fresh.extend(had.map(|had| (subtitle.path.as_str(), had.clone())));
                continue;
            }
            let stream_id = had.and_then(|had| had.get("id")?.as_i64()).unwrap_or_else(|| self.mint_subtitle_ids(1));
            let mut merged = had.and_then(Value::as_object).cloned().unwrap_or_default();
            for field in SUBTITLE_FIELDS {
                merged.shift_remove(field);
            }
            merged.extend(subtitle.stream(stream_id, text));
            fresh.push((subtitle.path.as_str(), Value::Object(merged)));
        }
        // In place, so a push that changes nothing leaves the Part as it was; new ones last.
        let mut streams: Vec<Value> = old
            .iter()
            .filter_map(|stream| match path(stream) {
                None => Some(stream.clone()),
                Some(path) => fresh.iter().find(|(of, _)| *of == path).map(|(_, stream)| stream.clone()),
            })
            .collect();
        streams.extend(fresh.iter().filter(|(of, _)| !old.iter().any(|stream| path(stream).as_deref() == Some(*of))).map(|(_, stream)| stream.clone()));
        if streams == old {
            return false;
        }
        let prefs = self.track_prefs.clone();
        let Some(part) = self.catalog.get_mut(key).and_then(|row| row.pointer_mut(&at)).filter(|part| part.is_object()) else {
            return false;
        };
        let packed = part.as_object_mut().is_some_and(crate::catalog::unpack_part);
        part["Stream"] = Value::Array(streams);
        // Selected only as the account's track settings select an embedded track.
        if let Some(prefs) = &prefs {
            mediainfo::select_tracks(part, prefs);
        }
        if packed {
            part.as_object_mut().map(crate::catalog::repack_part);
        }
        info!("FEED {source} {id}: {} sidecar subtitle(s) on {key}", wanted.len());
        true
    }

    /// An item's `collections` on its title: the film's row, or the episode's show. Only what
    /// changed since the source's last list is applied, as a person's `Collection` tag write is
    /// (`join_tag_collection`), so a member a person took out stays out until the source drops
    /// and names that collection again. Answers whether anything changed.
    fn put_collections(&mut self, key: &str, wanted: &[String]) -> bool {
        let Some(row) = self.catalog.get(key).cloned() else { return false };
        let title = match row.get("type").and_then(Value::as_str) {
            Some("movie") => key.to_string(),
            Some("episode") => row.get("grandparentRatingKey").map(string_value).unwrap_or_default(),
            _ => return false,
        };
        let Some(owner) = self.catalog.get(&title).cloned() else { return false };
        let before: Vec<String> = owner
            .get("_feedCollections")
            .and_then(Value::as_array)
            .map(|names| names.iter().map(string_value).collect())
            .unwrap_or_default();
        let has = |names: &[String], name: &str| names.iter().any(|other| other.to_lowercase() == name.to_lowercase());
        if before.len() == wanted.len() && wanted.iter().all(|name| has(&before, name)) {
            return false;
        }
        let section = owner.get("librarySectionID").map(string_value).unwrap_or_default();
        for name in before.iter().filter(|name| !has(wanted, name)) {
            self.untag_collection_member(&title, name);
            self.leave_tag_collection(&title, &section, name);
        }
        for name in wanted.iter().filter(|name| !has(&before, name)) {
            self.tag_collection_member(&title, name);
            self.join_tag_collection(&title, &owner, &section, name);
        }
        if let Some(object) = self.catalog.get_mut(&title).and_then(Value::as_object_mut) {
            if wanted.is_empty() {
                object.shift_remove("_feedCollections");
            } else {
                object.insert("_feedCollections".to_string(), json!(wanted));
            }
        }
        true
    }

    /// A later push's `display` over a row nothing identified, its show and its season: each
    /// field it names and a person did not lock (`Field`). Artwork changes are `_poster_url` /
    /// `_art_url`, which `restamp_artwork` mints new paths for; a person's upload stays theirs.
    fn redress(&mut self, key: &str, display: &Map<String, Value>) -> bool {
        let Some(row) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
            return false;
        };
        if !row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("local://")) {
            return false;
        }
        let episode = row.get("type").and_then(Value::as_str) == Some("episode");
        let part = |name: &str| display.get(name).and_then(Value::as_object).cloned().unwrap_or_default();
        let mut leaf = Map::new();
        super::scan::dress(&mut leaf, display, episode);
        // `dress` writes a new leaf's fields; on a placed episode its still is its own poster.
        if episode {
            if let Some(still) = leaf.remove("_art_url") {
                leaf.insert("_poster_url".to_string(), still);
            }
        }
        let mut changed = self.write_display(key, &leaf, Some(Value::Object(display.clone())));
        if episode {
            for (parent, from, fields) in [
                ("grandparentRatingKey", "show", &[("summary", "summary"), ("poster", "_poster_url"), ("art", "_art_url")][..]),
                ("parentRatingKey", "season", &[("summary", "summary"), ("poster", "_poster_url")][..]),
            ] {
                let Some(parent) = row.get(parent).map(string_value) else { continue };
                let given = part(from);
                let values: Map<String, Value> = fields
                    .iter()
                    .filter_map(|(field, target)| Some((target.to_string(), given.get(*field)?.clone())))
                    .collect();
                changed |= self.write_display(&parent, &values, None);
            }
        }
        changed
    }

    /// `values` onto one row, leaving any field a person locked: `title` and `summary` by name,
    /// a date as `originallyAvailableAt`, the poster and background as `thumb` and `art`.
    fn write_display(&mut self, key: &str, values: &Map<String, Value>, kept: Option<Value>) -> bool {
        let Some(row) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
            return false;
        };
        fn lock(field: &str) -> &str {
            match field {
                "year" => "originallyAvailableAt",
                "_poster_url" => "thumb",
                "_art_url" => "art",
                other => other,
            }
        }
        let writes: Vec<(String, Value)> = values
            .iter()
            .filter(|(field, value)| {
                // An upload is a person's too, and is no lock: it only names `upload://` (`upload_artwork`).
                let uploaded = row.get(field.as_str()).and_then(Value::as_str).is_some_and(|url| url.starts_with("upload://"));
                !field.starts_with("_show_") && !field.starts_with("_season_") && !uploaded
                    && !super::edit::field_locked(&row, lock(field)) && row.get(field.as_str()) != Some(value)
            })
            .map(|(field, value)| (field.clone(), value.clone()))
            .collect();
        let keep = kept.filter(|kept| row.get("_display") != Some(kept));
        if writes.is_empty() && keep.is_none() {
            return false;
        }
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in writes {
                if field == "title" {
                    object.shift_remove("_generatedTitle");
                }
                object.insert(field, value);
            }
            if let Some(kept) = keep {
                object.insert("_display".to_string(), kept);
            }
        }
        true
    }

    /// Whether every release of `row` belongs in `library`: a row of several releases moves only
    /// when all of them do, or it would move with whichever was pushed last. One that does not
    /// stays where it is, said once since the start.
    fn releases_agree(&self, row: &Value, item: &Item, library: &Library) -> bool {
        let all = versions(row);
        if all.len() < 2 {
            return true;
        }
        let agree = all.iter().all(|(_, _, index)| {
            match (row.pointer(&format!("/Media/{index}/_path")).and_then(Value::as_str), media_file(row, *index)) {
                (Some(path), Some(file)) => self
                    .feed_library(&Item { path: path.to_string(), file, ..item.clone() })
                    .is_some_and(|found| found.key == library.key),
                _ => false,
            }
        });
        if let Some(key) = key_of(row).filter(|key| !agree && first_time(format!("held {key}"))) {
            let here = row.get("librarySectionID").map(string_value).unwrap_or_default();
            info!(
                "FEED {key} stays in {}: its releases sit in folders of different libraries, {} among them",
                self.section_title(&here),
                library.title
            );
        }
        agree
    }

    /// A row moved to `library` as it is, on its key: its identification, a hand match among
    /// it, its watch state (every account's, kept by key) and its edits stay. An episode goes
    /// under its show's identity there, else under a copy of the show and season it leaves,
    /// which keeps everything a person did to them; those it leaves go once empty.
    fn relocate(&mut self, library: &Library, key: &str) -> bool {
        let Some(mut row) = self.catalog.get(key).cloned() else {
            return false;
        };
        let section_id = library.key.parse::<i64>().map(Value::from).unwrap_or_else(|_| json!(library.key));
        let into = |row: &mut Value| {
            if let Some(object) = row.as_object_mut() {
                object.insert("librarySectionID".to_string(), section_id.clone());
                object.insert("librarySectionTitle".to_string(), json!(library.title));
                object.insert("librarySectionKey".to_string(), json!(format!("/library/sections/{}", library.key)));
            }
        };
        into(&mut row);
        let mut left = Vec::new();
        let mut made = Vec::new();
        if row.get("type").and_then(Value::as_str) == Some("episode") {
            let parent = |field: &str| row.get(field).map(string_value).filter(|key| self.catalog.get(key).is_some());
            let (Some(season), Some(show)) = (parent("parentRatingKey"), parent("grandparentRatingKey")) else {
                return false;
            };
            let identity = self.catalog.get(&show).and_then(|row| row.get("_identity")).filter(|value| !value.is_null()).cloned();
            let there = identity.and_then(|identity| {
                self.catalog
                    .find_where(|row| {
                        row.get("type").and_then(Value::as_str) == Some("show")
                            && string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == library.key
                            && row.get("_identity") == Some(&identity)
                    })
                    .and_then(key_of)
            });
            let index = self.catalog.get(&season).and_then(|row| row.get("index")).cloned();
            let mut keys = HashMap::new();
            let new_show = match there {
                Some(found) => found,
                None => self.mint_key(),
            };
            keys.insert(show.clone(), new_show.clone());
            let new_season = self
                .catalog
                .find_where(|row| {
                    row.get("type").and_then(Value::as_str) == Some("season")
                        && row.get("parentRatingKey").map(string_value).as_deref() == Some(new_show.as_str())
                        && row.get("index") == index.as_ref()
                })
                .and_then(key_of)
                .unwrap_or_else(|| self.mint_key());
            keys.insert(season.clone(), new_season.clone());
            // A show or season not there yet is the one left, copied onto a new key.
            for (old, new) in [(&show, &new_show), (&season, &new_season)] {
                if self.catalog.get(new).is_some() {
                    self.carry_edits(old, new);
                    continue;
                }
                let mut copy = vec![self.catalog.get(old).cloned().unwrap_or_default()];
                super::scan::rekey(&mut copy, &keys);
                let mut copy = copy.remove(0);
                into(&mut copy);
                if copy.get("guid").and_then(Value::as_str) == Some(format!("local://{old}").as_str()) {
                    copy["guid"] = json!(format!("local://{new}"));
                }
                self.catalog.add(copy);
                if let Some(edits) = self.edits.get(old.as_str()).cloned() {
                    self.edits.insert(new.clone(), edits);
                }
                self.carry_collections(new);
                made.push(new.clone());
            }
            let mut moved = vec![row];
            super::scan::rekey(&mut moved, &keys);
            row = moved.remove(0);
            left = vec![season, show];
        }
        if let Some(slot) = self.catalog.get_mut(key) {
            *slot = row;
        }
        self.inherit_parents(key);
        for parent in &left {
            self.drop_if_empty(parent);
        }
        for created in &made {
            self.push_created_frames(created);
        }
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 5, None, None, None, None);
        }
        info!("FEED moved {key} to {} as it was", library.title);
        true
    }

    /// What a person changed on a show or season, carried to the one its episodes moved to:
    /// each field of its edit record the target has no edit of its own for (titles, sort
    /// title, summary, labels, collections, locks), and the artwork they picked or uploaded
    /// unless the target's own is locked.
    fn carry_edits(&mut self, from: &str, to: &str) {
        let (Some(old), Some(mut row)) = (self.catalog.get(from).cloned(), self.catalog.get(to).cloned()) else {
            return;
        };
        let theirs = self.edits.get(to).cloned().unwrap_or_default();
        let carried: Map<String, Value> =
            self.edits.get(from).into_iter().flatten().filter(|(field, _)| !theirs.contains_key(*field)).map(|(f, v)| (f.clone(), v.clone())).collect();
        let Some(object) = row.as_object_mut() else {
            return;
        };
        let locked = |row: &Map<String, Value>, field: &str| super::edit::field_locked(row, field);
        let own = object.clone();
        for (field, value) in &carried {
            if field == "Field" {
                // Locks join the target's own, which a picked poster's lock is among.
                let mut locks = object.get("Field").and_then(Value::as_array).cloned().unwrap_or_default();
                for lock in value.as_array().into_iter().flatten() {
                    if !locks.iter().any(|known| known.get("name") == lock.get("name")) {
                        locks.push(lock.clone());
                    }
                }
                object.insert(field.clone(), Value::Array(locks));
            } else if value.is_null() {
                object.shift_remove(field);
            } else {
                object.insert(field.clone(), value.clone());
            }
        }
        let mut artwork = false;
        for (field, private) in [("thumb", "_poster_url"), ("art", "_art_url")] {
            if !old.as_object().is_some_and(|old| locked(old, field)) || locked(&own, field) {
                continue;
            }
            match old.get(private) {
                Some(value) => object.insert(private.to_string(), value.clone()),
                None => object.shift_remove(private),
            };
            object.insert(field.to_string(), json!(format!("/library/metadata/{to}/{field}/{}", now())));
            let mut locks = object.get("Field").and_then(Value::as_array).cloned().unwrap_or_default();
            locks.retain(|lock| lock.get("name").and_then(Value::as_str) != Some(field));
            locks.push(json!({"locked": true, "name": field}));
            object.insert("Field".to_string(), Value::Array(locks));
            if let Some(uploads) = old.get("_uploads") {
                object.insert("_uploads".to_string(), uploads.clone());
            }
            artwork = true;
        }
        if carried.is_empty() && !artwork {
            return;
        }
        let written: HashSet<String> = carried.keys().cloned().collect();
        if let Some(slot) = self.catalog.get_mut(to) {
            *slot = row.clone();
        }
        self.edits.entry(to.to_string()).or_default().extend(carried);
        self.follow_parent(&row, &written);
        if written.contains("Collection") {
            self.carry_collections(to);
        }
        if artwork {
            self.cascade_artwork_from(to);
        }
    }

    /// The collections of its new library a moved show or season was a member of by name: a
    /// collection is one library's, so the one it was in stays behind.
    fn carry_collections(&mut self, to: &str) {
        let Some(row) = self.catalog.get(to) else {
            return;
        };
        let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
        let titles: Vec<String> = row
            .get("Collection")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|tag| tag.get("tag").and_then(Value::as_str).map(str::to_lowercase))
            .collect();
        for collection in self.collections.values_mut() {
            let tag = if collection.tag.is_empty() { &collection.title } else { &collection.tag };
            if collection.section == section && titles.contains(&tag.to_lowercase()) && !collection.items.contains(to) {
                collection.items.push(to.to_string());
                collection.extra.shift_remove("_frozenCounts");
            }
        }
    }

    /// A corrected source label is not identification. Known release fallbacks may follow
    /// it; an agent title needs a title-bearing answer from that very same guid.
    fn refresh_source_title(&mut self, key: &str, item: &Item, arrival: &Arrival) -> bool {
        let Some(leaf) = self.catalog.get(key).cloned() else { return false };
        if self.matched_by_hand(&leaf) { return false; }
        let show = leaf.get("grandparentRatingKey").map(string_value);
        let target = show.as_deref().unwrap_or(key);
        let Some(old) = self.catalog.get(target).cloned() else { return false };
        let Some(object) = old.as_object() else { return false };
        if super::edit::field_locked(object, "title") || self.edits.get(target).is_some_and(|fields| fields.contains_key("title")) {
            return false;
        }
        let meta = arrival.meta.as_ref();
        let local_identity = old.get("_identity").and_then(Value::as_str).is_some_and(|identity| identity.starts_with("title:"));
        let source_title = old.get("_titleGuid").is_some_and(Value::is_null)
            || old.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with("local://"))
                && (local_identity || leaf.get("_identify").is_some_and(|report| report.get("plex").is_some_and(Value::is_null)));
        let guid_field = if show.is_some() { "_show_titleGuid" } else { "_titleGuid" };
        let title_field = if show.is_some() { "_show_title" } else { "title" };
        let authoritative = meta
            .filter(|meta| meta.get(guid_field).and_then(Value::as_str).is_some() && meta.get(guid_field) == old.get("guid"))
            .and_then(|meta| meta.get(title_field)).cloned();
        let (title, metadata_title) = if let Some(title) = authoritative {
            (title, true)
        } else {
            if !source_title || versions(&leaf).len() > 1
                || old.get("title").and_then(Value::as_str) != leaf.get("_release").and_then(Value::as_str)
            {
                return false;
            }
            (json!(item.name), false)
        };
        let owner = if metadata_title { old.get("guid").cloned().unwrap_or(Value::Null) } else { Value::Null };
        let mut changed = old.get("title") != Some(&title) || old.get("_titleGuid") != Some(&owner);
        if let Some(row) = self.catalog.get_mut(target).and_then(Value::as_object_mut) {
            row.insert("title".to_string(), title);
            row.insert("_titleGuid".to_string(), owner);
            super::edit::derive_title_sort(row);
        }
        if !metadata_title {
            let identity = if local_identity {
                json!(format!("title:{}", crate::hierarchy::fold_title(&item.name).to_lowercase()))
            } else {
                old.get("_identity").cloned().unwrap_or(Value::Null)
            };
            let mut keys: Vec<String> = show.as_deref().map(|show| self.leaves(show).iter().filter_map(key_of).collect()).unwrap_or_default();
            keys.push(target.to_string());
            for key in keys {
                if let Some(row) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                    if row.get("_identity") == old.get("_identity") {
                        changed |= row.get("_identity") != Some(&identity);
                        row.insert("_identity".to_string(), identity.clone());
                        if show.is_some() && key != target {
                            row.insert("_tmdb_id".to_string(), identity.clone());
                            row.insert("_show_title".to_string(), json!(item.name));
                        }
                    }
                    if row.get("_source").and_then(Value::as_str) == leaf.get("_source").and_then(Value::as_str)
                        && row.get("_release") == leaf.get("_release")
                    {
                        row.insert("_release".to_string(), json!(item.name));
                    }
                }
            }
        }
        if changed {
            if let Some(row) = self.catalog.get(target).cloned() {
                self.follow_parent(&row, &HashSet::from(["title".to_string()]));
                self.push_timeline_entry(&row, 5, None, None, None, None);
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
        // Every field a person locked keeps their value, as a push's own update keeps it (a
        // locked title stayed only until a push re-identified the row, which the retry of a placed
        // row Plex had no episode for did on every push).
        if let Some(row) = old.as_object() {
            let locked: Vec<String> = row.get("Field").and_then(Value::as_array).into_iter().flatten()
                .filter_map(|entry| entry.get("name").and_then(Value::as_str).map(str::to_string))
                .filter(|field| super::edit::field_locked(row, field))
                .collect();
            for field in locked {
                if let Some(value) = row.get(&field) {
                    leaf[field.as_str()] = value.clone();
                }
            }
        }
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
        // Another show, not this one: a show placed before Plex's agent could name it already holds
        // the identity its episodes are named by again, and found itself here, so the 7,035 releases
        // a sponsor's rex took before its claim left every show on its release name (2026-10-24).
        let taken = self.catalog.hot().lazy().any(|row| {
            key_of(&row).as_deref() != Some(show)
                && row.get("type").and_then(Value::as_str) == Some("show")
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
                "Role", "rating", "audienceRating", "tagline", "_poster_url", "_art_url", "_titleGuid",
            ] {
                match fresh.get(field).filter(|value| field == "_titleGuid" || !value.is_null()) {
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
        self.remove_media_analysis(key, index);
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
    pub(super) fn drop_if_empty(&mut self, key: &str) {
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
    fn an_item_sent_without_its_size_takes_the_one_ffprobe_read() {
        let source = Source::register("test", &json!({"url": "http://127.0.0.1:9"})).unwrap();
        let probe = |format: Value| json!({"streams": [{"codec_type": "video"}], "format": format});
        let item = |pushed: Value| Item::parse(&source, &pushed).unwrap().size;
        // ffprobe's JSON writes format.size as a string.
        assert_eq!(item(json!({"id": "a", "path": "movies/a.mkv", "media_info": probe(json!({"format_name": "matroska,webm", "size": "1468006400"}))})), 1468006400);
        assert_eq!(item(json!({"id": "a", "path": "movies/a.mkv", "size": 7, "media_info": probe(json!({"size": "1468006400"}))})), 7);
        assert_eq!(item(json!({"id": "a", "path": "movies/a.mkv", "size": 0, "media_info": probe(json!({"size": "9"}))})), 9);
        // A playlist's size is its text's, never the stream's.
        assert_eq!(item(json!({"id": "s", "path": "kids/s.m3u8", "media_info": probe(json!({"format_name": "hls", "size": "2048"}))})), 0);
        assert_eq!(item(json!({"id": "a", "path": "movies/a.mkv"})), 0);
    }

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
    /// a public peer gets the same authentication/unknown-route answer as any PMS path.
    /// Headers naming a local client cannot expose the source API.
    #[test]
    fn the_source_api_does_not_identify_itself_to_a_public_peer() {
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
                    let ordinary = lock(&shared).route(&request(method, "/does-not-exist/leaf", headers));
                    assert_eq!((response.status, &response.content_type, &response.body),
                               (ordinary.status, &ordinary.content_type, &ordinary.body),
                               "{peer} {method} {target} {headers:?}");
                }
            }
        }
        let capture: Value = serde_json::from_str(include_str!("../../../tests/fixtures/pms_identity_2026_10_22.json")).unwrap();
        lock(&shared).token = Some("source-gate-token".to_string());
        for row in capture["public_rows"].as_array().unwrap().iter().filter(|row| {
            row["token"] == "valid" && row["path"].as_str().unwrap().starts_with("/rex/")
        }) {
            let method = row["method"].as_str().unwrap();
            let path = row["path"].as_str().unwrap();
            let response = handle(&shared, &request(method, path, &[("x-plex-token", "source-gate-token")]), path);
            assert_eq!(response.status as u64, row["status"].as_u64().unwrap(), "{method} {path}");
            assert_eq!(response.body, row["body"].as_str().unwrap().as_bytes(), "{method} {path}");
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

    /// A show's edits reach the show its episodes moved to field by field: what the target has
    /// no edit of its own for is carried, what it has stays; a picked poster likewise, unless
    /// the target's own is locked.
    #[test]
    fn a_moved_shows_edits_fill_only_what_the_target_left_alone() {
        let dir = work("carry");
        let (shared, _) = runtime_with_episode(&dir);
        let mut guard = shared.lock().unwrap();
        let picked = "https://image.tmdb.org/t/p/original/ours.jpg";
        guard.catalog.get_mut("10").unwrap().as_object_mut().unwrap().extend([
            ("title".to_string(), json!("Ours")), ("titleSort".to_string(), json!("Ours, The")),
            ("summary".to_string(), json!("As we tell it.")), ("Label".to_string(), json!([{"tag": "Kyoto"}])),
            ("_poster_url".to_string(), json!(picked)), ("thumb".to_string(), json!("/library/metadata/10/thumb/5")),
            ("Field".to_string(), json!([{"locked": true, "name": "title"}, {"locked": true, "name": "thumb"}])),
        ]);
        guard.edits.insert("10".to_string(), json!({"title": "Ours", "titleSort": "Ours, The", "summary": "As we tell it.",
                                                    "Label": [{"tag": "Kyoto"}], "Field": [{"locked": true, "name": "title"}]})
            .as_object().unwrap().clone());
        for (key, own) in [("20", json!({"title": "Theirs"})), ("21", json!({}))] {
            guard.catalog.add(json!({"ratingKey": key, "key": format!("/library/metadata/{key}/children"), "type": "show",
                                     "title": "Theirs", "librarySectionID": 1, "thumb": format!("/library/metadata/{key}/thumb/1")}));
            if !own.as_object().unwrap().is_empty() {
                guard.edits.insert(key.to_string(), own.as_object().unwrap().clone());
            }
        }
        guard.catalog.get_mut("21").unwrap()["Field"] = json!([{"locked": true, "name": "thumb"}]);
        guard.catalog.get_mut("21").unwrap()["_poster_url"] = json!("https://image.tmdb.org/t/p/original/theirs.jpg");
        guard.carry_edits("10", "20");
        guard.carry_edits("10", "21");
        let (twenty, twenty_one) = (guard.catalog.get("20").unwrap().clone(), guard.catalog.get("21").unwrap().clone());
        assert_eq!((&twenty["title"], &twenty["titleSort"], &twenty["summary"], &twenty["Label"]),
                   (&json!("Theirs"), &json!("Ours, The"), &json!("As we tell it."), &json!([{"tag": "Kyoto"}])), "its own title stays");
        assert_eq!(twenty["_poster_url"], json!(picked));
        assert!(twenty["thumb"].as_str().is_some_and(|thumb| thumb.starts_with("/library/metadata/20/thumb/")));
        assert_eq!(guard.edits["20"]["title"], json!("Theirs"));
        assert_eq!(guard.edits["20"]["summary"], json!("As we tell it."), "kept as its own edit across a restart");
        assert_eq!(twenty_one["title"], json!("Ours"), "no edit of its own: the moved show's title");
        assert_eq!(twenty_one["_poster_url"], json!("https://image.tmdb.org/t/p/original/theirs.jpg"), "its own locked poster stays");
        assert_eq!(twenty_one["Field"], json!([{"locked": true, "name": "thumb"}, {"locked": true, "name": "title"}]), "locks joined");
        drop(guard);
        let _ = std::fs::remove_dir_all(dir);
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

/// A Media's embedded streams as the conversion addresses them: index and codec. A stored
/// Part holds them packed (`catalog::part_streams`).
fn stream_layout(media: &Value) -> Vec<(i64, String)> {
    let part = media.pointer("/Part/0").cloned().unwrap_or_default();
    crate::catalog::part_streams(&part).iter()
        .filter_map(|stream| Some((stream.get("index")?.as_i64()?, stream.get("codec").and_then(Value::as_str).unwrap_or_default().to_string())))
        .collect()
}

/// Whether `row` can take `item` without its probe: whether the version `item` names keeps the
/// same `probe` with nothing on the row waiting for one, and whether the item would turn into a
/// row of its own -- an extra out of a title, or a version leaving a row of several for other
/// numbers (`upsert`), which a person's match prevents -- which needs it.
fn probe_kept(row: &Value, source: &str, item: &Item, probe: &str) -> (bool, bool) {
    let index = versions(row).into_iter().find(|(of, sid, _)| of == source && *sid == item.id).map(|(_, _, index)| index);
    let same = index.is_some_and(|index| {
        !row.get("_probePending").is_some_and(truthy)
            && row.pointer(&format!("/Media/{index}/_probe")).and_then(Value::as_str) == Some(probe)
    });
    let titled = matches!(row.get("type").and_then(Value::as_str), Some("episode" | "movie"));
    let here = row.get("parentIndex").and_then(Value::as_i64).zip(row.get("index").and_then(Value::as_i64));
    let leaves = super::scan::placed_numbers(item.placed.as_ref()).is_some_and(|numbers| {
        row.get("type").and_then(Value::as_str) == Some("episode") && versions(row).len() > 1 && here != Some(numbers)
    });
    (same, item.extra.is_some() && titled || leaves)
}
