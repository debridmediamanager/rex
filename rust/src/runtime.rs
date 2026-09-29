//! Native Plex HTTP runtime.
//!
//! The first runtime slice deliberately keeps the transport and the mutable
//! catalog in one Rust implementation.  It is intentionally small in its
//! dependencies: the measured API is HTTP/1.1 over a bounded socket reader,
//! and the route layer works on the same ordered `serde_json` values used by
//! the phase-two serializers.  No Python process or fixture replay is involved.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::{self, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use flate2::write::GzEncoder;
use flate2::Compression;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs1KeyDer, PrivatePkcs8KeyDer};
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pemfile::{certs, pkcs8_private_keys, rsa_private_keys};
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

use crate::{catalog::Catalog, decision, xmlout};

mod edit;
mod frames;
mod artwork;
mod axes;
mod collections;
mod i18n;
mod notify;
mod showprefs;
mod butler;
mod children;
mod detail;
mod framework;
mod gates;
mod listing;
mod merge;
mod playlists;
mod resume;
mod streams;
mod search;
mod spellfix;
mod structural;
mod item_hubs;
mod transcoder;
mod wire;
use artwork::owns_artwork;
use resume::{bool_option, content_directories, exclude_continue_watching, guid_gate, hub_refusal, int_param, page_window};

const VERSION: &str = "1.42.2.10156-plexsim";
const MEDIA_TAG_PREFIX: &str = "/system/bundle/media/flags/";
const MEDIA_TAG_VERSION: i64 = 1_655_122_614;
const MAX_HEADER_BYTES: usize = 256 * 1024;
const MAX_HEADER_LINE_BYTES: usize = 64 * 1024;
const MAX_HEADER_LINES: usize = 100;
const MAX_TARGET_BYTES: usize = 32 * 1024;
const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;
const PLUGIN_REGISTRY: &[&str] = &[
    "/accounts",
    "/actions",
    "/activities",
    "/butler",
    "/channels",
    "/clients",
    "/devices",
    "/diagnostics",
    "/dlna",
    "/downloadQueue",
    "/hubs",
    "/library",
    "/library/augmentation",
    "/library/shared",
    "/livetv/dvrs",
    "/livetv/epg",
    "/livetv/sessions",
    "/log",
    "/media/grabbers",
    "/media/providers",
    "/media/subscriptions",
    "/myplex",
    "/neighborhood",
    "/playQueues",
    "/playlists",
    "/resources",
    "/search",
    "/security",
    "/server",
    "/servers",
    "/services/browse",
    "/services/clustering",
    "/services/codecs",
    "/services/defaultLibraries",
    "/services/iva",
    "/services/lyricfind",
    "/services/songkick",
    "/services/thetvdb",
    "/services/tmdb",
    "/services/ultrablur",
    "/statistics",
    "/status/sessions",
    "/system",
    "/transcode",
    "/updater",
    "/user",
];
const GDM_PORTS: [u16; 2] = [32410, 32414];
const GDM_GROUP: &str = "239.0.0.250";
const GDM_QUERY: &[u8] = b"M-SEARCH * ";
const GDM_SEARCH: &[u8] = b"M-SEARCH * HTTP/1.1\r\n";
const GDM_TTL_SECONDS: i64 = 180;

fn configured_gdm_ports() -> Vec<u16> {
    std::env::var("PLEXSIM_GDM_PORTS")
        .ok()
        .map(|value| {
            value
                .split(',')
                .filter_map(|port| port.trim().parse::<u16>().ok())
                .collect::<Vec<_>>()
        })
        .filter(|ports| !ports.is_empty())
        .unwrap_or_else(|| GDM_PORTS.to_vec())
}
const PREFIX_PROBES: &[&str] = &[
    "/s",
    "/st",
    "/sta",
    "/stat",
    "/stati",
    "/statu",
    "/l",
    "/li",
    "/lib",
    "/d",
    "/de",
    "/dev",
    "/do",
    "/down",
    "/p",
    "/pl",
    "/a",
    "/ac",
    "/m",
    "/me",
    "/med",
    "/sy",
    "/sys",
    "/up",
    "/update",
    "/re",
    "/res",
    "/resource",
    "/c",
    "/ch",
    "/cl",
    "/co",
    "/con",
    "/connection",
    "/b",
    "/but",
    "/h",
    "/hu",
    "/i",
    "/id",
    "/n",
    "/t",
    "/tr",
    "/u",
    "/us",
    "/v",
    "/w",
    "/e",
    "/f",
    "/g",
    "/j",
    "/k",
    "/q",
    "/r",
    "/x",
    "/y",
    "/z",
    "/o",
];

type SharedRuntime = Arc<Mutex<Runtime>>;

#[derive(Debug, Clone)]
pub struct RuntimeOptions {
    pub catalog: PathBuf,
    pub port: u16,
    pub host: String,
    pub part_mode: String,
    pub public_url: Option<String>,
    pub token: Option<String>,
    pub assert_claimed: bool,
    pub web_root: Option<PathBuf>,
    pub owner_email: String,
    pub owner_username: String,
    pub tls_cert: Option<PathBuf>,
    pub tls_key: Option<PathBuf>,
    pub gdm: String,
    pub state: Option<PathBuf>,
    pub announce_url: Option<String>,
    pub device_id: Option<String>,
}

impl Default for RuntimeOptions {
    fn default() -> Self {
        Self {
            catalog: PathBuf::from("catalog.json"),
            port: 32400,
            host: "0.0.0.0".to_string(),
            part_mode: "proxy".to_string(),
            public_url: None,
            token: std::env::var("PLEXSIM_TOKEN").ok(),
            assert_claimed: false,
            web_root: None,
            owner_email: "plexadmin@debridmediamanager.com".to_string(),
            owner_username: "plexa630".to_string(),
            tls_cert: None,
            tls_key: None,
            gdm: "auto".to_string(),
            state: std::env::var("PLEXSIM_STATE").ok().map(PathBuf::from),
            announce_url: None,
            device_id: None,
        }
    }
}

#[derive(Debug, Clone)]
struct Device {
    client_identifier: String,
    name: String,
    product: String,
    platform: Value,
    last_seen: i64,
}

#[derive(Debug, Clone)]
struct GdmPeer {
    machine: String,
    name: String,
    address: String,
    port: u16,
    version: String,
    host: String,
    last_seen: i64,
}

#[derive(Debug, Clone)]
struct Playlist {
    id: String,
    title: String,
    items: Vec<String>,
    item_ids: Vec<i64>,
    smart: bool,
    uri: String,
}

#[derive(Debug, Clone)]
struct Collection {
    id: String,
    title: String,
    items: Vec<String>,
    smart: bool,
    section: String,
    subtype: String,
    prefs: HashMap<String, String>,
    uri: String,
    tag: String,
    /// Everything else `library.py` keeps on a collection: summary, addedAt,
    /// updatedAt, the pinned tagID, titleSort, contentRating, Field, Label and the
    /// names it has held.
    extra: Map<String, Value>,
}

#[derive(Debug, Clone)]
struct MetadataProviderGroup {
    id: String,
    title: String,
    primary_identifier: String,
    items: Vec<Value>,
}

#[derive(Debug, Clone)]
struct QueueItem {
    id: i64,
    rating_key: String,
    added: bool,
}

#[derive(Debug, Clone)]
struct Queue {
    id: String,
    version: i64,
    selected: Option<i64>,
    items: Vec<QueueItem>,
    source: String,
    natural: Vec<String>,
    shuffled: bool,
    last_added: Option<i64>,
    client: String,
    playlist_id: Option<String>,
    playlist_title: Option<String>,
}

#[derive(Debug)]
pub struct Runtime {
    pub catalog: Catalog,
    pub machine: String,
    pub friendly: String,
    pub owner_email: String,
    pub owner_username: String,
    pub token: Option<String>,
    pub assert_claimed: bool,
    pub started_at: i64,
    pub private_address: String,
    pub private_port: u16,
    pub state_path: Option<PathBuf>,
    pub catalog_path: PathBuf,
    pub source_digest: String,
    pub rd_api: String,
    pub rd_token: String,
    decision_profiles: decision::Profiles,
    pub part_mode: String,
    pub public_url: Option<String>,
    pub prefs: Map<String, Value>,
    pub edits: HashMap<String, Map<String, Value>>,
    pub section_prefs: HashMap<(String, String), String>,
    pub added_sections: Vec<Value>,
    pub deleted: HashSet<String>,
    devices: HashMap<String, Device>,
    gdm_peers: HashMap<String, GdmPeer>,
    pub history: Vec<Value>,
    playlists: HashMap<String, Playlist>,
    collections: HashMap<String, Collection>,
    list_artwork: HashMap<String, (String, Vec<u8>)>,
    metadata_provider_groups: HashMap<String, MetadataProviderGroup>,
    next_metadata_provider_group: i64,
    queues: HashMap<String, Queue>,
    download_queues: HashMap<String, Value>,
    pub next_playlist: i64,
    next_playlist_item: i64,
    /// Keys a merge or a delete freed: a split never reuses one (measured).
    retired_keys: Vec<i64>,
    pub next_collection: i64,
    pub next_queue: i64,
    pub next_queue_item: i64,
    pub next_dynamic_key: i64,
    pub sessions: HashMap<String, Value>,
    pub started_sessions: HashSet<String>,
    timeline_offsets: HashMap<(String, String), i64>,
    completed_sessions: HashSet<(String, String)>,
    completion_pending_stop: HashSet<(String, String)>,
    /// The (session, ratingKey) pairs whose session has reported a position past
    /// the minute: only those delete the resume point with a sub-minute beat.
    progressed: HashSet<(String, String)>,
    pub hidden_continue: HashSet<String>,
    /// Leaves completed in a PLAYBACK SESSION (Python's `_viewedInSession`): history,
    /// not view state, so a later scrobble or unscrobble leaves it standing.
    viewed_in_session: HashSet<String>,
    /// Uploaded artwork by its `upload://` key: the bytes and their content type.
    uploads: HashMap<String, (Vec<u8>, String)>,
    /// Track rows the theme metadata leaf minted, by request path and by ratingKey.
    theme_rows: HashMap<String, Value>,
    theme_next: i64,
    /// The spellfix1 vocabularies search falls back on: a SNAPSHOT, built at load and
    /// rebuilt only by Optimize Database (cycles 37-38), never per edit.
    spelling: Option<spellfix::SearchIndex>,
    pub terminated_sessions: HashMap<String, String>,
    pub decided_sessions: HashSet<(String, String)>,
    pub web_root: Option<PathBuf>,
    pub bandwidth_bytes: u64,
    pub dirty: bool,
    /// Events are retained by sequence so an eventsource/websocket subscriber
    /// can take a snapshot without a mutable callback into the route thread.
    /// This is bounded in `push_event`; it is runtime state, never persisted.
    events: Vec<(String, Value)>,
    /// The tag TABLE: a tag outlives its removal from every row, so a re-add keeps
    /// its canonical spelling and id (keyed by row key and lowercased tag).
    tag_table: HashMap<(String, String), Value>,
    /// Per library, the last finished (5) or removed (9) frame of each item, in
    /// insertion order -- what `/library/sections/{id}/timeline` lists.
    section_timeline: HashMap<String, Vec<(String, Value)>>,
    /// How many events the bound has drained from the front of `events`, so a
    /// subscriber's cursor stays a sequence number once the buffer is full.
    events_dropped: usize,
    /// The history key counter: monotonic, never reused.
    history_counter: std::cell::Cell<i64>,
    /// Per-user view state written to a COLLECTION or a PLAYLIST through the
    /// `/:/` state writes (cycle 21): both take every write in the family.
    pub list_state: HashMap<String, Map<String, Value>>,
    activity_serial: u64,
}

impl Runtime {
    pub fn load(options: RuntimeOptions) -> Result<Self, String> {
        let rd_token = std::env::var("RD_TOKEN").unwrap_or_default();
        let raw_catalog =
            fs::read(&options.catalog).map_err(|error| format!("cannot read catalog: {error}"))?;
        let decision_profiles = load_json_sidecar(&options.catalog, "client_profiles.json")
            .and_then(|value| serde_json::from_value(value).ok())
            .unwrap_or_default();
        let mut catalog = Catalog::load(&options.catalog)?;
        let source_digest = bytes_digest(&raw_catalog);
        catalog.normalize();
        number_catalog_tags(&mut catalog.items);
        // Cycle 41: `Part.accessible`/`exists` are a DETAIL option (`checkFiles=1`), never
        // a stored field a listing or a plain read carries; the checked read computes them.
        for item in &mut catalog.items {
            for media in item.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                    if let Some(object) = part.as_object_mut() {
                        object.remove("accessible");
                        object.remove("exists");
                    }
                }
            }
        }
        let machine = catalog
            .document
            .get("machineIdentifier")
            .and_then(Value::as_str)
            .unwrap_or("0000000000000000000000000000000000000001")
            .to_string();
        let friendly = catalog
            .document
            .get("friendlyName")
            .and_then(Value::as_str)
            .unwrap_or("plexsim")
            .to_string();
        let started_at = now();
        let mut runtime = Self {
            catalog,
            machine,
            friendly,
            owner_email: options.owner_email,
            owner_username: options.owner_username,
            token: options.token,
            assert_claimed: options.assert_claimed,
            started_at,
            // PMS advertises the routeable interface even when its listener
            // binds all interfaces.  Returning an empty address for
            // `0.0.0.0` makes `/servers` unusable to LAN clients and differs
            // from the measured self row.
            private_address: local_interface_address().unwrap_or_else(|| options.host.clone()),
            private_port: options.port,
            state_path: options.state,
            catalog_path: options.catalog,
            source_digest,
            rd_api: std::env::var("RD_API")
                .unwrap_or_else(|_| "https://api.real-debrid.com/rest/1.0".to_string()),
            rd_token,
            decision_profiles,
            part_mode: options.part_mode,
            public_url: options.public_url,
            prefs: Map::new(),
            edits: HashMap::new(),
            section_prefs: HashMap::new(),
            added_sections: Vec::new(),
            deleted: HashSet::new(),
            devices: HashMap::new(),
            gdm_peers: HashMap::new(),
            history: Vec::new(),
            playlists: HashMap::new(),
            collections: HashMap::new(),
            list_artwork: HashMap::new(),
            uploads: HashMap::new(),
            tag_table: HashMap::new(),
            metadata_provider_groups: HashMap::new(),
            next_metadata_provider_group: 5,
            queues: HashMap::new(),
            download_queues: HashMap::new(),
            next_playlist: playlists::PLAYLIST_BASE + 1,
            next_playlist_item: 0,
            retired_keys: Vec::new(),
            // Key spaces stay disjoint (library.py): collections are 50000+.
            next_collection: 50001,
            next_queue: 2,
            next_queue_item: 1,
            next_dynamic_key: 40000,
            sessions: HashMap::new(),
            started_sessions: HashSet::new(),
            timeline_offsets: HashMap::new(),
            completed_sessions: HashSet::new(),
            completion_pending_stop: HashSet::new(),
            progressed: HashSet::new(),
            hidden_continue: HashSet::new(),
            viewed_in_session: HashSet::new(),
            theme_rows: HashMap::new(),
            theme_next: 0,
            spelling: None,
            terminated_sessions: HashMap::new(),
            decided_sessions: HashSet::new(),
            web_root: options.web_root,
            bandwidth_bytes: 0,
            dirty: false,
            events: Vec::new(),
            section_timeline: HashMap::new(),
            events_dropped: 0,
            history_counter: std::cell::Cell::new(0),
            list_state: HashMap::new(),
            activity_serial: 0,
        };
        runtime.load_state()?;
        runtime.rebuild_spelling();
        Ok(runtime)
    }

    fn load_state(&mut self) -> Result<(), String> {
        let Some(path) = self.state_path.clone() else {
            return Ok(());
        };
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(format!("cannot read state file: {error}")),
        };
        let state: Value = match serde_json::from_slice(&bytes) {
            Ok(value) => value,
            Err(_) => {
                if bytes.starts_with(&[0x80]) {
                    eprintln!("state files hold no classes");
                }
                let corrupt = sidecar(&path, "corrupt");
                let _ = fs::rename(&path, corrupt);
                return Ok(());
            }
        };
        if state.get("format").and_then(Value::as_i64) != Some(1) {
            let stale = sidecar(&path, "stale");
            let _ = fs::rename(&path, stale);
            return Ok(());
        }
        if state.get("containsClasses").and_then(Value::as_bool) == Some(true) {
            return Err("state files hold no classes".to_string());
        }
        let mut rebuilt = false;
        if let Some(saved_digest) = state.get("catalogDigest").and_then(Value::as_str) {
            if saved_digest != self.source_digest {
                eprintln!("state catalog digest changed; migrating view state");
                let stale = sidecar(&path, "stale");
                let _ = fs::rename(&path, stale);
                rebuilt = true;
            }
        }
        self.friendly = state
            .get("friendlyName")
            .and_then(Value::as_str)
            .unwrap_or(&self.friendly)
            .to_string();
        self.prefs = state
            .get("prefs")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        if let Some(rows) = state.get("viewState").and_then(Value::as_object) {
            for (key, patch) in rows {
                if let Some(row) = self.catalog.get_mut(key) {
                    if let (Some(row), Some(patch)) = (row.as_object_mut(), patch.as_object()) {
                        for field in [
                            "viewCount",
                            "viewOffset",
                            "lastViewedAt",
                            "lastRatedAt",
                            "userRating",
                            "_viewUpdatedAt",
                            "lastSkippedAt",
                        ] {
                            match patch.get(field) {
                                Some(value) => {
                                    row.insert(field.to_string(), value.clone());
                                }
                                None => {
                                    row.remove(field);
                                }
                            }
                        }
                    }
                }
            }
        }
        if let Some(history) = state.get("history").and_then(Value::as_array) {
            self.history = history.clone();
        }
        if !rebuilt {
            if let Some(sections) = state.get("addedSections").and_then(Value::as_array) {
                self.added_sections = sections.clone();
                self.catalog.sections.extend(sections.iter().cloned());
            }
            if let Some(deleted) = state.get("deleted").and_then(Value::as_array) {
                for value in deleted {
                    let key = string_value(value);
                    self.deleted.insert(key.clone());
                    let _ = self.catalog.remove(&key);
                }
            }
            if let Some(edits) = state.get("edits").and_then(Value::as_object) {
                for (key, value) in edits {
                    if let Some(fields) = value.as_object() {
                        self.edits.insert(key.clone(), fields.clone());
                        if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut)
                        {
                            for (field, field_value) in fields {
                                // A removal is recorded as null.
                                if field_value.is_null() {
                                    row.remove(field);
                                } else {
                                    row.insert(field.clone(), field_value.clone());
                                }
                            }
                        }
                    }
                }
            }
        }
        if let Some(devices) = state.get("devices").and_then(Value::as_object) {
            for (key, value) in devices {
                if let Some(row) = value.as_object() {
                    self.devices.insert(
                        key.clone(),
                        Device {
                            client_identifier: key.clone(),
                            name: row
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                            product: row
                                .get("product")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                            platform: row.get("platform").cloned().unwrap_or_else(|| json!("")),
                            last_seen: row.get("lastSeenAt").and_then(Value::as_i64).unwrap_or(0),
                        },
                    );
                }
            }
        }
        if !rebuilt {
            if let Some(playlists) = state.get("playlists").and_then(Value::as_array) {
                for value in playlists {
                    if let Some(row) = value.as_object() {
                        let id = row.get("id").map(string_value).unwrap_or_default();
                        self.next_playlist =
                            self.next_playlist.max(id.parse::<i64>().unwrap_or(0) + 1);
                        self.playlists.insert(
                            id.clone(),
                            Playlist {
                                id,
                                title: row
                                    .get("title")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string(),
                                items: row
                                    .get("items")
                                    .and_then(Value::as_array)
                                    .map(|v| v.iter().map(string_value).collect())
                                    .unwrap_or_default(),
                                item_ids: row
                                    .get("itemIds")
                                    .and_then(Value::as_array)
                                    .map(|v| v.iter().filter_map(Value::as_i64).collect())
                                    .unwrap_or_default(),
                                smart: row.get("smart").and_then(Value::as_bool).unwrap_or(false),
                                uri: row
                                    .get("uri")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string(),
                            },
                        );
                    }
                }
            }
        }
        if !rebuilt {
            if let Some(collections) = state.get("collections").and_then(Value::as_array) {
                for value in collections {
                    if let Some(row) = value.as_object() {
                        let id = row.get("id").map(string_value).unwrap_or_default();
                        self.next_collection =
                            self.next_collection.max(id.parse::<i64>().unwrap_or(0) + 1);
                        self.collections.insert(
                            id.clone(),
                            Collection {
                                id,
                                title: row
                                    .get("title")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string(),
                                items: row
                                    .get("items")
                                    .and_then(Value::as_array)
                                    .map(|v| v.iter().map(string_value).collect())
                                    .unwrap_or_default(),
                                smart: row.get("smart").and_then(Value::as_bool).unwrap_or(false),
                                section: row.get("section").map(string_value).unwrap_or_default(),
                                subtype: row
                                    .get("subtype")
                                    .and_then(Value::as_str)
                                    .unwrap_or("movie")
                                    .to_string(),
                                prefs: row
                                    .get("prefs")
                                    .and_then(Value::as_object)
                                    .map(|object| {
                                        object
                                            .iter()
                                            .map(|(key, value)| (key.clone(), string_value(value)))
                                            .collect()
                                    })
                                    .unwrap_or_default(),
                                uri: row
                                    .get("uri")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string(),
                                tag: row
                                    .get("tag")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string(),
                                extra: row
                                    .get("extra")
                                    .and_then(Value::as_object)
                                    .cloned()
                                    .unwrap_or_default(),
                            },
                        );
                    }
                }
            }
            // A collection membership is projected onto each catalog row as a
            // Collection tag.  The tag is derived mutable state, so rebuild it
            // after restoring the owning collection records; otherwise the
            // collection itself survives a restart while the item's metadata
            // silently loses its membership tag.
            for collection in self.collections.values() {
                if collection.smart {
                    continue;
                }
                for key in &collection.items {
                    let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
                        continue;
                    };
                    let tags = row
                        .entry("Collection".to_string())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    let Some(tags) = tags.as_array_mut() else {
                        continue;
                    };
                    let tag_id = tag_id("Collection", &collection.tag);
                    if !tags.iter().any(|tag| {
                        tag.get("id").and_then(Value::as_i64) == Some(tag_id)
                            || tag.get("tag").and_then(Value::as_str)
                                == Some(collection.tag.as_str())
                    }) {
                        tags.push(json!({"tag":collection.tag,"id":tag_id}));
                    }
                }
            }
        }
        Ok(())
    }

    pub fn save_state(&mut self) -> Result<(), String> {
        let Some(path) = self.state_path.clone() else {
            return Ok(());
        };
        let mut view = Map::new();
        for item in &self.catalog.items {
            let Some(key) = key_of(item) else {
                continue;
            };
            let Some(row) = item.as_object() else {
                continue;
            };
            let mut patch = Map::new();
            for field in [
                "viewCount",
                "viewOffset",
                "lastViewedAt",
                "lastRatedAt",
                "userRating",
                "_viewUpdatedAt",
                "lastSkippedAt",
            ] {
                if let Some(value) = row.get(field) {
                    patch.insert(field.to_string(), value.clone());
                }
            }
            if !patch.is_empty() {
                view.insert(key, Value::Object(patch));
            }
        }
        let state = json!({
            "format": 1,
            "machineIdentifier": self.machine,
            "catalogPath": self.catalog_path,
            "catalogDigest": self.source_digest.clone(),
            "friendlyName": self.friendly,
            "prefs": self.prefs,
            "edits": self.edits,
            "addedSections": self.added_sections,
            "deleted": self.deleted.iter().map(|value| Value::String(value.clone())).collect::<Vec<_>>(),
            "viewState": view,
            "history": self.history,
            "devices": self.devices.iter().map(|(key, value)| (key.clone(), json!({"name":value.name,"product":value.product,"platform":value.platform,"lastSeenAt":value.last_seen}))).collect::<Map<String, Value>>(),
            "playlists": self.playlists.values().map(|value| json!({"id":value.id,"title":value.title,"items":value.items,"itemIds":value.item_ids,"smart":value.smart,"uri":value.uri})).collect::<Vec<_>>(),
            "collections": self.collections.values().map(|value| json!({"id":value.id,"title":value.title,"items":value.items,"smart":value.smart,"section":value.section,"subtype":value.subtype,"prefs":value.prefs,"uri":value.uri,"tag":value.tag,"extra":value.extra})).collect::<Vec<_>>(),
        });
        let text = serde_json::to_vec_pretty(&state).map_err(|e| e.to_string())?;
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, text).map_err(|e| format!("cannot write state: {e}"))?;
        fs::rename(&tmp, &path).map_err(|e| format!("cannot replace state: {e}"))?;
        self.dirty = false;
        Ok(())
    }

    fn section(&self, key: &str) -> Option<&Value> {
        self.catalog
            .sections
            .iter()
            .find(|section| section.get("key").map(string_value).as_deref() == Some(key))
    }

    fn section_rows(&self, key: &str) -> Vec<Value> {
        self.catalog
            .items
            .iter()
            .filter(|item| {
                string_value(item.get("librarySectionID").unwrap_or(&Value::Null)) == key
                    && matches!(
                        item.get("type").and_then(Value::as_str),
                        Some("movie" | "show")
                    )
            })
            .cloned()
            .collect()
    }

    fn children(&self, key: &str) -> Vec<Value> {
        let mut rows = self.catalog.children(key);
        rows.sort_by_key(|row| row.get("index").and_then(Value::as_i64).unwrap_or(0));
        rows
    }

    fn leaves(&self, key: &str) -> Vec<Value> {
        let mut out = Vec::new();
        for child in self.children(key) {
            if child.get("type").and_then(Value::as_str) == Some("episode") {
                out.push(child);
            } else if let Some(child_key) = key_of(&child) {
                out.extend(self.leaves(&child_key));
            }
        }
        out
    }

    fn detail_row(&self, original: &Value) -> Value {
        let mut row = strip_private_value(original);
        let Some(object) = row.as_object_mut() else {
            return row;
        };
        let key = key_of(original).unwrap_or_default();
        // Both reference versions omit `Part.accessible`/`exists` from listings and
        // ordinary detail reads; only a checked detail read (`annotate_file_checks`,
        // which marks the row) carries them. The catalog's own values never leak.
        if original.get("_filesChecked").is_none() {
            if let Some(media_rows) = object.get_mut("Media").and_then(Value::as_array_mut) {
                for media in media_rows {
                    if let Some(parts) = media.get_mut("Part").and_then(Value::as_array_mut) {
                        for part in parts {
                            if let Some(part) = part.as_object_mut() {
                                part.remove("accessible");
                                part.remove("exists");
                            }
                        }
                    }
                }
            }
        }
        // Keep the discriminator independent of the mutable response map.  The
        // same map is enriched below, and retaining a borrowed `&str` here
        // would make those measured field insertions borrow-conflict with the
        // discriminator's later checks.
        let kind = object
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        // The catalog keeps generated season/episode titles as empty values.
        // PMS materializes the familiar label only in a response, while a
        // custom sort title survives that projection after an explicit title
        // was cleared.  Keeping this distinction in the row serializer also
        // makes a later index edit update `Episode N` without rewriting state.
        if matches!(kind.as_str(), "season" | "episode")
            && object
                .get("title")
                .and_then(Value::as_str)
                .is_some_and(str::is_empty)
        {
            let label = if kind == "season" {
                "Season"
            } else {
                "Episode"
            };
            let index = object.get("index").map(string_value).unwrap_or_default();
            object.insert("title".to_string(), json!(format!("{label} {index}")));
        }
        if kind == "season" {
            let generated = object
                .get("title")
                .and_then(Value::as_str)
                .is_some_and(|title| {
                    title
                        .strip_prefix("Season ")
                        .is_some_and(|suffix| suffix.chars().all(|c| c.is_ascii_digit()))
                });
            let sort_matches_generated = object
                .get("titleSort")
                .and_then(Value::as_str)
                .zip(object.get("title").and_then(Value::as_str))
                .is_some_and(|(sort, title)| sort == title);
            let sort_locked = object
                .get("Field")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .any(|field| {
                    field.get("name").and_then(Value::as_str) == Some("titleSort")
                        && field.get("locked").and_then(Value::as_bool) == Some(true)
                });
            // An unmatch writes the generated title as the sort title too
            // (library.unmatch), and that one survives the projection.
            // A title the server GENERATED on an unmatch or an emptied title
            // (`_generatedTitle`) carries the sort title it was written with.
            let sort_written = original.get("_generatedTitle").is_some();
            if generated && sort_matches_generated && !sort_locked && !sort_written {
                object.remove("titleSort");
            }
        }
        if kind == "show" {
            let seasons = self.children(&key);
            let leaves = self.leaves(&key);
            object.insert("childCount".to_string(), json!(seasons.len()));
            object.insert("leafCount".to_string(), json!(leaves.len()));
            // A show's own `duration`, `year` and `originallyAvailableAt` are the
            // STORED columns (measured: The Simpsons stays 1989 and its typical
            // episode length); the detail read only fills them when absent
            // (`show_detail`), which is done by the route, not by every projection.
            // The first air date is the exception: the catalog load gives a show
            // without one its earliest episode's, so every projection carries it.
            if object.get("originallyAvailableAt").is_none() {
                if let Some(first_air) = leaves
                    .iter()
                    .filter_map(|leaf| leaf.get("originallyAvailableAt").and_then(Value::as_str))
                    .filter(|date| !date.is_empty())
                    .min()
                {
                    object.insert("originallyAvailableAt".to_string(), json!(first_air));
                }
            }
            object.insert(
                "viewedLeafCount".to_string(),
                json!(leaves
                    .iter()
                    .filter(|leaf| leaf.get("viewCount").is_some())
                    .count()),
            );
            if let Some(first) = leaves.first() {
                if let Some(value) = first.get("duration") {
                    object
                        .entry("duration".to_string())
                        .or_insert(value.clone());
                }
            }
            // `Preferences`, `OnDeck` and `Extras` are DETAIL blocks the route adds
            // under their include options (`apply_detail_blocks`); no projection
            // carries them unasked, and a show row names its library by id only.
            // Cycle 15: the EFFECTIVE flattenSeasons -- the item's unless it inherits.
            self.apply_flatten_projection(object);
        } else if kind == "season" {
            let children = self.children(&key);
            object.insert("leafCount".to_string(), json!(children.len()));
            object.insert(
                "viewedLeafCount".to_string(),
                json!(children
                    .iter()
                    .filter(|leaf| leaf.get("viewCount").is_some())
                    .count()),
            );
            if let Some(parent_key) = object.get("parentRatingKey").map(string_value) {
                if let Some(parent) = self.catalog.get(&parent_key) {
                    for (source, destination) in [
                        ("title", "parentTitle"),
                        ("studio", "parentStudio"),
                        ("thumb", "parentThumb"),
                        ("year", "parentYear"),
                        ("index", "parentIndex"),
                    ] {
                        if destination == "parentYear" && object.get("year").is_some() {
                            object.remove(destination);
                            continue;
                        }
                        if let Some(value) = parent.get(source).filter(|value| {
                            !matches!(destination, "parentTitle" | "parentStudio")
                                || !string_value(value).is_empty()
                        }) {
                            object.insert(destination.to_string(), value.clone());
                        } else {
                            object.remove(destination);
                        }
                    }
                }
            }
        } else if kind == "episode" {
            if let Some(parent_key) = object.get("parentRatingKey").map(string_value) {
                if let Some(parent) = self.catalog.get(&parent_key) {
                    if let Some(value) = parent
                        .get("title")
                        .filter(|value| !string_value(value).is_empty())
                    {
                        object.insert("parentTitle".to_string(), value.clone());
                    } else {
                        object.remove("parentTitle");
                    }
                    object.insert(
                        "parentIndex".to_string(),
                        parent.get("index").cloned().unwrap_or(Value::Null),
                    );
                    if let Some(value) = parent
                        .get("studio")
                        .filter(|value| !string_value(value).is_empty())
                    {
                        object.insert("parentStudio".to_string(), value.clone());
                    } else {
                        object.remove("parentStudio");
                    }
                    if object.get("summary").is_none() {
                        if let Some(value) = parent.get("summary") {
                            object.insert("summary".to_string(), value.clone());
                        }
                    }
                }
            }
            if let Some(show_key) = object.get("grandparentRatingKey").map(string_value) {
                if let Some(show) = self.catalog.get(&show_key) {
                    if let Some(value) = show
                        .get("title")
                        .filter(|value| !string_value(value).is_empty())
                    {
                        object.insert("grandparentTitle".to_string(), value.clone());
                    } else {
                        object.remove("grandparentTitle");
                    }
                    if object.get("summary").is_none() {
                        if let Some(value) = self
                            .catalog
                            .get(&show_key)
                            .and_then(|show| show.get("summary"))
                        {
                            object.insert("summary".to_string(), value.clone());
                        }
                    }
                }
            }
            for field in ["Genre", "studio", "tagline", "Country"] {
                object.remove(field);
            }
            // Cycle 14/15: a flattened show's episodes skip the season level.
            self.apply_flatten_projection(object);
        }
        // Item preferences are represented twice by PMS: as a Preferences block
        // and as string attributes on the row itself.  Keep the private storage
        // out of the response while preserving those attributes for the settings
        // dialog and its save round trip.
        // `strip_private_value` removes the storage key from the public row
        // before this projection is built.  Read the private preference map
        // from the owning catalog value so item settings still echo as row
        // attributes after a PUT.
        if let Some(stored) = original.get("_prefs").and_then(Value::as_object).cloned() {
            for (name, value) in stored {
                let text = string_value(&value);
                if text.is_empty() {
                    object.remove(&name);
                } else {
                    object.insert(name, json!(text));
                }
            }
        }
        if kind == "movie" {
            let unmatched = object
                .get("guid")
                .and_then(Value::as_str)
                .is_some_and(|value| {
                    value.starts_with("local://")
                        || value.starts_with("none://")
                        || value.starts_with("tv.plex.agents.none://")
                });
            let _ = unmatched;
        }
        object.insert(
            "ratingKey".to_string(),
            original.get("ratingKey").cloned().unwrap_or(Value::Null),
        );
        // Stream titles are the catalog's own, recomputed at load
        // (`Catalog::normalize`); a response localises and suffixes them per client
        // (`stream_titles_response`).  Only the private stream content is dropped.
        // Cycle 15: a member's `Collection` tag carries the collection's own guid.
        self.stamp_collection_tags(&key, object);
        if let Some(media_rows) = object.get_mut("Media").and_then(Value::as_array_mut) {
            for media in media_rows {
                for part in media
                    .get_mut("Part")
                    .and_then(Value::as_array_mut)
                    .into_iter()
                    .flatten()
                {
                    for stream in part
                        .get_mut("Stream")
                        .and_then(Value::as_array_mut)
                        .into_iter()
                        .flatten()
                    {
                        if let Some(stream_object) = stream.as_object_mut() {
                            stream_object.remove("_content");
                        }
                    }
                }
            }
        }
        // PMS carries artwork as Image children in the public metadata row.
        // The catalog stores the same URLs as scalar attributes, but the web
        // client reads this measured child list to choose a poster or still.
        // Episodes borrow the show's poster while keeping their own thumb as
        // a snapshot; a season's and an episode's `/arts` candidate lists are
        // empty even when the inherited art attribute exists.
        let title = object
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let mut images = Vec::new();
        if kind == "episode" {
            if let Some(url) = object.get("grandparentThumb").and_then(Value::as_str) {
                images.push(json!({"alt": title, "type": "coverPoster", "url": url}));
            }
            if let Some(url) = object.get("thumb").and_then(Value::as_str) {
                images.push(json!({"alt": title, "type": "snapshot", "url": url}));
            }
        } else if let Some(url) = object.get("thumb").and_then(Value::as_str) {
            images.push(json!({"alt": title, "type": "coverPoster", "url": url}));
        }
        // An episode's background is the art it borrows (measured on both servers,
        // detail and listing alike); a clip's borrowed art is no Image row.
        if kind != "clip" {
            if let Some(url) = object.get("art").and_then(Value::as_str) {
                images.push(json!({"alt": title, "type": "background", "url": url}));
            }
        }
        if let Some(url) = original.get("_clearLogo").and_then(Value::as_str) {
            images.push(json!({"alt": title, "type": "clearLogo", "url": url}));
        }
        if !images.is_empty() {
            object.insert("Image".to_string(), Value::Array(images));
        }
        row
    }

    fn listing_row(&self, original: &Value) -> Value {
        let mut row = self.detail_row(original);
        if let Some(object) = row.as_object_mut() {
            object.remove("Media");
            object.remove("Guid");
            object.remove("Rating");
            object.remove("Field");
            object.remove("Preferences");
            object.remove("OnDeck");
            for name in ["Label", "Style", "Similar", "Mood"] {
                object.remove(name);
            }
            for name in [
                "Genre",
                "Country",
                "Director",
                "Writer",
                "Role",
                "Collection",
            ] {
                if let Some(tags) = object.get_mut(name).and_then(Value::as_array_mut) {
                    *tags = tags
                        .iter()
                        .filter_map(|tag| {
                            tag.get("tag")
                                .and_then(Value::as_str)
                                .map(|text| json!({"tag": text}))
                        })
                        .collect();
                }
            }
        }
        row
    }

    /// The light grid projection used by section listings, shelves, and search.
    /// It starts from the measured detail projection so mutable hierarchy
    /// fields remain current, then applies the same field/tag/stream trimming
    /// as the Python grid builder.
    fn grid_row(
        &self,
        original: &Value,
        keep_section: bool,
        keep_guid: bool,
        keep_location: bool,
    ) -> Value {
        let mut row = self.detail_row(original);
        let Some(object) = row.as_object_mut() else {
            return row;
        };
        for name in [
            "Rating",
            "Field",
            "Producer",
            "CommonSenseMedia",
            "Label",
            "Style",
            "Similar",
            "Mood",
            "Preferences",
            "OnDeck",
            "Extras",
        ] {
            object.remove(name);
        }
        if !keep_location {
            object.remove("Location");
        }
        if !keep_guid {
            object.remove("Guid");
        }
        if !keep_section {
            for name in [
                "librarySectionID",
                "librarySectionTitle",
                "librarySectionKey",
            ] {
                object.remove(name);
            }
        }
        let caps = [
            ("Genre", 2_usize),
            ("Writer", 2),
            ("Country", 2),
            ("Role", 3),
            ("Director", 3),
        ];
        for (name, cap) in caps {
            if let Some(tags) = object.get_mut(name).and_then(Value::as_array_mut) {
                let reduced = tags
                    .iter()
                    .filter_map(|tag| {
                        tag.get("tag")
                            .and_then(Value::as_str)
                            .map(|text| json!({"tag": text}))
                    })
                    .take(cap)
                    .collect::<Vec<_>>();
                *tags = reduced;
            }
        }
        for name in ["Country", "Director", "Writer", "Role", "Collection"] {
            if let Some(tags) = object.get_mut(name).and_then(Value::as_array_mut) {
                *tags = tags
                    .iter()
                    .filter_map(|tag| {
                        tag.get("tag")
                            .and_then(Value::as_str)
                            .map(|text| json!({"tag": text}))
                    })
                    .collect();
            }
        }
        // Cycle 62: after a section-path collection rename a member's LIGHT row keeps
        // the FIRST name the tag had, while its detail row carries the new one.
        if let Some(light) = original.get("_collectionLight").and_then(Value::as_object) {
            if let Some(tags) = object.get_mut("Collection").and_then(Value::as_array_mut) {
                for tag in tags.iter_mut() {
                    let name = tag.get("tag").map(string_value).unwrap_or_default();
                    if let Some(first) = light.get(&name.to_lowercase()) {
                        tag["tag"] = first.clone();
                    }
                }
            }
        }
        if let Some(media_rows) = object.get_mut("Media").and_then(Value::as_array_mut) {
            for media in media_rows {
                if let Some(parts) = media.get_mut("Part").and_then(Value::as_array_mut) {
                    for part in parts {
                        if let Some(part_object) = part.as_object_mut() {
                            part_object.remove("Stream");
                        }
                    }
                }
            }
        }
        row
    }

    fn section_listing_row(&self, original: &Value) -> Value {
        let mut row = self.grid_row(original, false, true, false);
        if let Some(object) = row.as_object_mut() {
            if original.get("type").and_then(Value::as_str) == Some("season") {
                object.remove("leafCount");
                object.remove("viewedLeafCount");
            }
            for name in [
                "librarySectionID",
                "librarySectionKey",
                "librarySectionTitle",
            ] {
                object.remove(name);
            }
            // A section grid exposes the file path for a row, but the
            // database location id is an internal column and is absent from
            // the measured listing projection.  `listing_row` retains the
            // catalog Location shape, so remove only its id here.
            if let Some(locations) = object.get_mut("Location").and_then(Value::as_array_mut) {
                for location in locations {
                    if let Some(location) = location.as_object_mut() {
                        location.remove("id");
                    }
                }
            }
            if let Some(media_rows) = object.get_mut("Media").and_then(Value::as_array_mut) {
                for media in media_rows {
                    if let Some(parts) = media.get_mut("Part").and_then(Value::as_array_mut) {
                        for part in parts {
                            if let Some(part_object) = part.as_object_mut() {
                                part_object.remove("Stream");
                            }
                        }
                    }
                }
            }
        }
        row
    }

    fn leaf_listing_row(&self, original: &Value) -> Value {
        // `/allLeaves` keeps Media so clients can calculate storage and
        // duration without opening every item. Ordinary grid rows omit Media;
        // using that projection here loses the measured Media/mediaCount split.
        let mut row = self.detail_row(original);
        if let Some(object) = row.as_object_mut() {
            for name in ["Guid", "Rating", "Field", "Preferences", "OnDeck"] {
                object.remove(name);
            }
            for name in ["Label", "Style", "Similar", "Mood"] {
                object.remove(name);
            }
            for name in [
                "Genre",
                "Country",
                "Director",
                "Writer",
                "Role",
                "Collection",
            ] {
                if let Some(tags) = object.get_mut(name).and_then(Value::as_array_mut) {
                    *tags = tags
                        .iter()
                        .filter_map(|tag| {
                            tag.get("tag")
                                .and_then(Value::as_str)
                                .map(|text| json!({"tag": text}))
                        })
                        .collect();
                }
            }
        }
        row
    }

    fn on_deck_for(&self, leaves: &[Value]) -> Option<Value> {
        let in_progress = leaves
            .iter()
            .enumerate()
            .filter(|(_, row)| row.get("viewOffset").and_then(Value::as_i64).unwrap_or(0) > 0)
            .max_by_key(|(index, row)| {
                (
                    row.get("lastViewedAt").and_then(Value::as_i64).unwrap_or(0),
                    row.get("_viewUpdatedAt")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                    *index,
                )
            })
            .map(|(_, row)| row.clone());
        if let Some(row) = in_progress {
            return Some(self.detail_row(&row));
        }
        if leaves.iter().any(|row| row.get("viewCount").is_some()) {
            // Next-up follows the last watched leaf in hierarchy order.  A
            // simple first-unwatched lookup points back at episode one after
            // episode two is scrobbled, which is exactly the shelf regression
            // the measured show contract catches.
            let last_watched = leaves
                .iter()
                .enumerate()
                .filter(|(_, row)| row.get("viewCount").is_some())
                .map(|(index, _)| index)
                .max();
            if let Some(index) = last_watched {
                if let Some(next) = leaves.get(index + 1) {
                    return Some(self.detail_row(next));
                }
            }
            return leaves
                .iter()
                .find(|row| row.get("viewCount").is_none())
                .map(|row| self.detail_row(row));
        }
        leaves.first().map(|row| self.detail_row(row))
    }

    /// Per-section filter/sort descriptors, retargeted from the captured
    /// template (`section_meta.json` / `section_meta_show.json`): every key in
    /// the blob names the section it came from, so the whole document is
    /// rewritten.  `advanced` is the client's `includeAdvanced=1`: a real PMS
    /// then adds six Sorts to a movie library, four to a show, three to a
    /// season and eight to an episode view, plus the `Duplicates` filter, with
    /// the sorts INTERLEAVED in the measured order.
    fn section_meta(&self, section_id: &str, section_type: &str, advanced: bool) -> Option<Value> {
        let kind = if section_type == "show" { "show" } else { "movie" };
        let template = load_json_sidecar(
            &self.catalog_path,
            if kind == "show" { "section_meta_show.json" } else { "section_meta.json" },
        )?;
        let blob = serde_json::to_string(&template).ok()?;
        let pattern = regex::Regex::new(r"/library/sections/\d+/").ok()?;
        let blob = pattern
            .replace_all(&blob, format!("/library/sections/{section_id}/").as_str())
            .to_string();
        let mut document: Value = serde_json::from_str(&blob).ok()?;
        if advanced {
            for entry in document.get_mut("Type").and_then(Value::as_array_mut).into_iter().flatten() {
                let entry_type = entry.get("type").and_then(Value::as_str).unwrap_or("").to_string();
                let Some(extra) = advanced_meta(kind, &entry_type) else {
                    continue;
                };
                for (name, rows) in extra.as_object().into_iter().flatten() {
                    for row in rows.as_array().into_iter().flatten() {
                        let mut copy = row.clone();
                        for field in ["key", "firstCharacterKey"] {
                            if let Some(text) = copy.get(field).and_then(Value::as_str) {
                                if text.contains("%s") {
                                    copy[field] = json!(text.replace("%s", section_id));
                                }
                            }
                        }
                        if let Some(object) = entry.as_object_mut() {
                            object
                                .entry(name.clone())
                                .or_insert_with(|| Value::Array(Vec::new()))
                                .as_array_mut()
                                .map(|list| list.push(copy));
                        }
                    }
                }
                if let Some(order) = advanced_sort_order(&entry_type) {
                    if let Some(sorts) = entry.get_mut("Sort").and_then(Value::as_array_mut) {
                        sorts.sort_by_key(|sort| {
                            sort.get("key")
                                .and_then(Value::as_str)
                                .and_then(|key| order.iter().position(|name| *name == key))
                                .unwrap_or(order.len())
                        });
                    }
                }
            }
        }
        Some(document)
    }

    /// An ITEM's settings: the captured list per type, with the two language
    /// enums rebuilt for the item (`item_language_enum`).
    fn item_preferences(&self, kind: &str, row: &Value) -> Vec<Value> {
        let mut rows = self.preferences_for(kind, row.get("_prefs"));
        // Cycle 16/36: an UNMATCHED show drops its agent-driven settings.
        let unmatched = row.get("guid").and_then(Value::as_str).is_some_and(|guid| {
            guid.starts_with("local://") || guid.starts_with("none://") || guid.starts_with("tv.plex.agents.none://")
        });
        if unmatched && kind == "show" {
            rows.retain(|setting| {
                !matches!(setting.get("id").and_then(Value::as_str), Some("languageOverride" | "useOriginalTitle"))
            });
        }
        for setting in &mut rows {
            let id = setting.get("id").and_then(Value::as_str).unwrap_or("");
            if matches!(id, "audioLanguage" | "subtitleLanguage") {
                let stored = setting.get("enumValues").map(string_value).unwrap_or_default();
                let rebuilt = self.item_language_enum(row, &stored);
                if let Some(object) = setting.as_object_mut() {
                    object.insert("enumValues".to_string(), json!(rebuilt));
                }
            }
        }
        rows
    }

    /// `audioLanguage`/`subtitleLanguage` on an ITEM, led by its own streams.
    ///
    /// Cycle 18, measured on zen (1.42.2) and kids (1.43.4): the enum is the
    /// inherit option, then the item's OWN stream languages as bare 639-1 tags
    /// -- AUDIO first, then subtitles, each group ordered by tag and the whole
    /// head deduped, each named in its own language -- then the generic
    /// regional list from its first REGIONAL entry on.  Cycle 21: a SEASON's
    /// inherit option reads `:Series default`.
    fn item_language_enum(&self, row: &Value, stored: &str) -> String {
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("");
        let rows = if matches!(kind, "show" | "season") {
            self.leaves(&key_of(row).unwrap_or_default())
        } else {
            vec![row.clone()]
        };
        let languages = load_json_sidecar(&self.catalog_path, "languages.json");
        let mut head = Vec::new();
        let mut seen = HashSet::new();
        for stream_kind in [2, 3] {
            let mut tags = std::collections::BTreeSet::new();
            for leaf in &rows {
                for media in leaf.get("Media").and_then(Value::as_array).into_iter().flatten() {
                    for part in media.get("Part").and_then(Value::as_array).into_iter().flatten() {
                        for stream in part.get("Stream").and_then(Value::as_array).into_iter().flatten() {
                            if stream.get("streamType").and_then(Value::as_i64) == Some(stream_kind) {
                                if let Some(tag) = stream
                                    .get("languageTag")
                                    .and_then(Value::as_str)
                                    .filter(|tag| !tag.is_empty())
                                {
                                    tags.insert(tag.to_string());
                                }
                            }
                        }
                    }
                }
            }
            for tag in tags {
                if !seen.insert(tag.clone()) {
                    continue;
                }
                let name = languages
                    .as_ref()
                    .and_then(Value::as_object)
                    .and_then(|table| {
                        table.values().find_map(|entry| {
                            (entry.get(1).and_then(Value::as_str) == Some(tag.as_str()))
                                .then(|| entry.get(0).and_then(Value::as_str).map(str::to_string))
                                .flatten()
                        })
                    });
                if let Some(name) = name {
                    head.push(format!("{tag}:{name}"));
                }
            }
        }
        let entries = stored.split('|').collect::<Vec<_>>();
        let generic = entries
            .iter()
            .position(|piece| piece.split(':').next().unwrap_or("").contains('-'))
            .unwrap_or(entries.len());
        let inherit = if kind == "season" { ":Series default" } else { ":Account default" };
        let mut pieces = vec![inherit.to_string()];
        pieces.extend(head);
        pieces.extend(entries[generic..].iter().map(|piece| piece.to_string()));
        pieces.join("|")
    }

    fn preferences_for(&self, kind: &str, own: Option<&Value>) -> Vec<Value> {
        if let Some(mut rows) = load_json_sidecar(&self.catalog_path, "item_prefs.json")
            .and_then(|value| value.get(kind).cloned())
            .and_then(|value| value.as_array().cloned())
        {
            let own = own.and_then(Value::as_object);
            for row in &mut rows {
                if let Some(object) = row.as_object_mut() {
                    let id = object.get("id").map(string_value).unwrap_or_default();
                    if let Some(value) = own.and_then(|map| map.get(&id)) {
                        // A raw, non-parsing value remains visible on the
                        // metadata row while the settings dialog keeps its
                        // measured default. Empty writes clear the setting.
                        let usable = !value.as_str().is_some_and(|text| {
                            text.is_empty()
                                || (id == "flattenSeasons" && text.parse::<i64>().is_err())
                        });
                        if usable {
                            object.insert("value".to_string(), value.clone());
                        }
                    }
                }
            }
            return rows;
        }
        let mut rows = Vec::new();
        let base = if kind == "show" {
            vec![
                ("flattenSeasons", "Flatten seasons", "-1"),
                ("episodeSort", "Episode ordering", "-1"),
                ("showOrdering", "Show ordering", "aired"),
                ("useOriginalTitle", "Use original titles", "0"),
                ("episodeOrdering", "Episode ordering", "aired"),
                ("keep", "Keep", "0"),
                ("autoDeletionItemPolicy", "Auto deletion policy", "0"),
                ("skipChildren", "Skip children", "0"),
                ("leafCount", "Leaf count", "0"),
                ("viewedLeafCount", "Viewed leaf count", "0"),
                ("displayMode", "Display mode", "0"),
            ]
        } else if kind == "season" {
            vec![
                ("audioLanguage", "Audio language", ""),
                ("subtitleLanguage", "Subtitle language", ""),
                ("subtitleMode", "Subtitle mode", ""),
            ]
        } else {
            Vec::new()
        };
        let own = own.and_then(Value::as_object);
        for (id, label, default) in base {
            let value = own
                .and_then(|map| map.get(id))
                .map(string_value)
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| default.to_string());
            rows.push(json!({"id": id, "label": label, "value": value, "type": "enum"}));
        }
        rows
    }

    fn section_pref_rows(&self, section_key: &str, kind: &str) -> Vec<Value> {
        let mut rows = load_json_sidecar(&self.catalog_path, "section_prefs.json")
            .and_then(|value| value.get(kind).cloned())
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default();
        for row in &mut rows {
            if let Some(object) = row.as_object_mut() {
                let id = object.get("id").map(string_value).unwrap_or_default();
                if let Some(value) = self.section_prefs.get(&(section_key.to_string(), id)) {
                    // Section preference values are returned as strings even
                    // when their descriptor says bool/int.  The reference's
                    // library editor writes `0`/`1`, then reads back the
                    // canonical string `false`/`true`.
                    let value = match object.get("type").and_then(Value::as_str) {
                        Some("bool") => {
                            if is_true_option(value) {
                                "true"
                            } else {
                                "false"
                            }
                        }
                        _ => value.as_str(),
                    };
                    object.insert("value".to_string(), json!(value));
                }
            }
        }
        rows
    }

    /// The `Preferences` block a library row carries under `includePreferences=1`.
    ///
    /// Measured on kids and zen: `/library/sections?includePreferences=1` and
    /// `/media/providers?includePreferences=1` answer the same block for the same
    /// library, byte for byte -- including the show library's `episodeSort`, which is
    /// the per-SHOW variant here (`Library default` in its enum and "for this show" in
    /// its summary) rather than the library leaf's own.
    fn section_preference_rows(&self, section_key: &str, kind: &str) -> Vec<Value> {
        let mut rows = self.section_pref_rows(section_key, kind);
        if kind == "show" {
            let own = load_json_sidecar(&self.catalog_path, "item_prefs.json")
                .and_then(|value| value.get("show").cloned())
                .and_then(|value| value.as_array().cloned())
                .unwrap_or_default()
                .into_iter()
                .find(|setting| setting.get("id").and_then(Value::as_str) == Some("episodeSort"));
            if let Some(own) = own {
                for row in &mut rows {
                    if row.get("id").and_then(Value::as_str) != Some("episodeSort") {
                        continue;
                    }
                    if let Some(object) = row.as_object_mut() {
                        for field in ["summary", "enumValues"] {
                            object.insert(
                                field.to_string(),
                                own.get(field).cloned().unwrap_or(Value::Null),
                            );
                        }
                    }
                }
            }
        }
        rows
    }

    fn section_directory(&self, section: &Value) -> Value {
        let key = string_value(section.get("key").unwrap_or(&Value::Null));
        let kind = section
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let rows = self.section_rows(&key);
        let newest = rows
            .iter()
            .filter_map(|row| row.get("addedAt").and_then(Value::as_i64))
            .max()
            .unwrap_or(0);
        let row = json!({
            "key": key,
            "type": kind,
            "title": section.get("title").cloned().unwrap_or(Value::Null),
            "agent": if kind == "show" { "tv.plex.agents.series" } else { "tv.plex.agents.movie" },
            "scanner": if kind == "show" { "Plex TV Series" } else { "Plex Movie" },
            "language": section.get("language").and_then(Value::as_str).unwrap_or("en-US"),
            "metadataAgentProviderGroupId": if kind == "show" { 2 } else { 1 },
            "uuid": section.get("uuid").cloned().unwrap_or(Value::Null),
            "updatedAt": section.get("updatedAt").cloned().unwrap_or(json!(newest)),
            "createdAt": newest,
            "scannedAt": newest,
            "contentChangedAt": rows.len(),
            "filters": true,
            "refreshing": false,
            "allowSync": false,
            "content": true,
            "directory": true,
            // Cycle 58: the library's Visibility setting, 0, 1 or 2.
            "hidden": self.section_visibility(&key),
            "Location": [{"id": key.parse::<i64>().unwrap_or(1), "path": format!("/plexsim/{key}")}]
        });
        row
    }

    /// The container a per-item sub-resource uses -- posters, arts, matches, tree,
    /// similar, extras: identifier plus the media-tag pair and nothing else.
    fn tag_container(&self, fields: Map<String, Value>) -> Value {
        let mut body = Map::from_iter([
            ("size".to_string(), json!(0)),
            ("identifier".to_string(), json!("com.plexapp.plugins.library")),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
            ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
        ]);
        let explicit_size = fields.contains_key("size");
        for (key, value) in fields {
            body.insert(key, value);
        }
        if !explicit_size {
            if let Some(array) = body.get("Metadata").and_then(Value::as_array) {
                body.insert("size".to_string(), json!(array.len()));
            }
        }
        json!({"MediaContainer": body})
    }

    fn container(&self, fields: Map<String, Value>, library: bool) -> Value {
        let mut body = Map::new();
        let explicit_size = fields.contains_key("size");
        if library {
            body.insert("size".to_string(), json!(0));
            body.insert(
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            );
            body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
            body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
            body.insert("allowSync".to_string(), json!(false));
        }
        for (key, value) in fields {
            body.insert(key, value);
        }
        if !explicit_size {
            for key in ["Metadata", "Directory", "Hub", "Video", "Device"] {
                if let Some(array) = body.get(key).and_then(Value::as_array) {
                    // `size` leads the container on a real PMS whatever it counts.
                    let size = array.len();
                    body.remove("size");
                    let mut sized = Map::new();
                    sized.insert("size".to_string(), json!(size));
                    sized.extend(body);
                    body = sized;
                    break;
                }
            }
        }
        Value::Object(Map::from_iter([(
            "MediaContainer".to_string(),
            Value::Object(body),
        )]))
    }

    fn pref_rows(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> Vec<Value> {
        let language = query
            .get("X-Plex-Language")
            .or_else(|| headers.get("x-plex-language"))
            .map(|value| value.replace('_', "-").to_ascii_lowercase());
        let regional = match language.as_deref() {
            Some("pt-br") | Some("pt-pt") => Some("pt-BR"),
            Some("hu-hu") => Some("hu-HU"),
            _ => None,
        };
        let mut rows = if let Some(language) = regional {
            load_regional_prefs(&self.catalog_path, language)
        } else {
            load_json_sidecar(&self.catalog_path, "server_prefs.json")
                .and_then(|value| value.as_array().cloned())
                .unwrap_or_default()
        };
        rows.retain(|row| {
            !matches!(
                row.get("id").and_then(Value::as_str),
                Some("AllowLegacyAgents" | "CLDriver")
            )
        });
        for row in &mut rows {
            if let Some(object) = row.as_object_mut() {
                if let Some(id) = object.get("id").and_then(Value::as_str) {
                    if let Some(value) = self.prefs.get(id) {
                        object.insert("value".to_string(), value.clone());
                    } else if id == "FriendlyName" {
                        object.insert("value".to_string(), json!(self.friendly));
                    }
                }
            }
        }
        if !rows.is_empty() {
            return rows;
        }
        const IDS: &[&str] = &[
            "FriendlyName",
            "OnDeckLimit",
            "ScannerLowPriority",
            "AllowLegacyAgents",
            "CLDriver",
            "LanNetworksBandwidth",
            "secureConnections",
            "PublishServerOnPlexOnline",
        ];
        IDS.iter()
            .map(|id| {
                let value = self.prefs.get(*id).cloned().unwrap_or_else(|| match *id {
                    "FriendlyName" => json!(self.friendly),
                    "OnDeckLimit" => json!(40),
                    "ScannerLowPriority" => json!(false),
                    "AllowLegacyAgents" => json!(true),
                    "CLDriver" => json!(""),
                    _ => json!(0),
                });
                json!({"id": id, "label": id, "value": value})
            })
            .collect()
    }

    fn identity(&self) -> Value {
        self.container(
            Map::from_iter([
                ("size".to_string(), json!(0)),
                ("apiVersion".to_string(), json!("1.1.1")),
                ("machineIdentifier".to_string(), json!(self.machine)),
                ("version".to_string(), json!(VERSION)),
                (
                    "claimed".to_string(),
                    json!(if self.assert_claimed { 1 } else { 0 }),
                ),
                (
                    "claimed".to_string(),
                    json!(if self.assert_claimed { 1 } else { 0 }),
                ),
            ]),
            false,
        )
    }

    fn identity_response(&self, query: &HashMap<String, String>) -> Response {
        let mut body = self.identity();
        if let Value::Object(root) = &mut body {
            if let Some(container) = root
                .get_mut("MediaContainer")
                .and_then(Value::as_object_mut)
            {
                if let (Some(start), Some(_size)) = (
                    query
                        .get("X-Plex-Container-Start")
                        .and_then(|value| value.parse::<usize>().ok()),
                    query
                        .get("X-Plex-Container-Size")
                        .and_then(|value| value.parse::<usize>().ok()),
                ) {
                    container.insert("offset".to_string(), json!(start));
                    container.insert("totalSize".to_string(), json!(0));
                }
            }
        }
        Response::json(200, body)
    }

    fn gdm_host(&self) -> Option<String> {
        self.public_url.as_deref().and_then(|url| {
            let host = url
                .split_once("://")
                .map(|(_, rest)| rest)
                .unwrap_or(url)
                .split('/')
                .next()
                .unwrap_or("")
                .split(':')
                .next()
                .unwrap_or("");
            (!host.is_empty()).then(|| host.to_string())
        })
    }

    fn gdm_response(&self) -> Vec<u8> {
        let mut lines = vec![
            "HTTP/1.0 200 OK".to_string(),
            "Content-Type: plex/media-server".to_string(),
        ];
        if let Some(host) = self.gdm_host() {
            lines.push(format!("Host: {host}"));
        }
        lines.extend([
            format!("Name: {}", self.friendly),
            format!("Port: {}", self.private_port),
            format!("Resource-Identifier: {}", self.machine),
            format!("Updated-At: {}", self.started_at),
            format!("Version: {VERSION}"),
        ]);
        format!("{}\r\n\r\n", lines.join("\r\n")).into_bytes()
    }

    fn gdm_record(&mut self, datagram: &[u8], address: &str) {
        let text = String::from_utf8_lossy(datagram);
        let mut lines = text.split("\r\n");
        if !lines
            .next()
            .is_some_and(|line| line.starts_with("HTTP/1.0 200"))
        {
            return;
        }
        let mut fields = HashMap::new();
        for line in lines {
            if let Some((name, value)) = line.split_once(':') {
                fields.insert(name.trim(), value.trim());
            }
        }
        if fields.get("Content-Type") != Some(&"plex/media-server") {
            return;
        }
        let Some(machine) = fields.get("Resource-Identifier").copied() else {
            return;
        };
        if machine == self.machine {
            return;
        }
        let Some(port) = fields.get("Port").and_then(|value| value.parse().ok()) else {
            return;
        };
        self.gdm_peers.insert(
            address.to_string(),
            GdmPeer {
                machine: machine.to_string(),
                name: fields.get("Name").copied().unwrap_or_default().to_string(),
                address: address.to_string(),
                port,
                version: fields
                    .get("Version")
                    .copied()
                    .unwrap_or_default()
                    .to_string(),
                host: fields.get("Host").copied().unwrap_or_default().to_string(),
                last_seen: now(),
            },
        );
    }

    fn gdm_server_rows(&self) -> Vec<Value> {
        let current = now();
        let mut rows = vec![json!({
            "name": self.friendly,
            "machineIdentifier": self.machine,
            "host": self.private_address,
            "address": self.private_address,
            "port": self.private_port,
            "owned": true
        })];
        rows.extend(
            self.gdm_peers
                .values()
                .filter(|peer| current.saturating_sub(peer.last_seen) <= GDM_TTL_SECONDS)
                .map(|peer| {
                    json!({
                        "name": peer.name,
                        "machineIdentifier": peer.machine,
                        "host": if peer.host.is_empty() { peer.address.clone() } else { peer.host.clone() },
                        "address": peer.address,
                        "port": peer.port,
                        "version": peer.version,
                        "owned": false
                    })
                }),
        );
        rows
    }

    fn server_root(&self) -> Value {
        let directory_keys = [
            "actions",
            "activities",
            "butler",
            "channels",
            "clients",
            "devices",
            "diagnostics",
            "downloadQueue",
            "hubs",
            "library",
            "livetv",
            "media",
            "neighborhood",
            "playQueues",
            "playlists",
            "resources",
            "search",
            "server",
            "servers",
            "service",
            "statistics",
            "system",
            "transcode",
            "updater",
            "user",
        ];
        let directories = directory_keys
            .iter()
            .map(|key| {
                json!({
                    "count": if matches!(*key, "library" | "livetv" | "media") { 3 } else { 1 },
                    "key":key,
                    "title":key
                })
            })
            .collect::<Vec<_>>();
        self.container(
            Map::from_iter([
                ("machineIdentifier".to_string(), json!(self.machine)),
                ("version".to_string(), json!(VERSION)),
                ("friendlyName".to_string(), json!(self.friendly)),
                ("platform".to_string(), json!("Linux")),
                ("platformVersion".to_string(), json!("")),
                ("product".to_string(), json!("Plex Media Server")),
                ("protocol".to_string(), json!(1)),
                (
                    "ownerFeatures".to_string(),
                    json!("adaptive_bitrate,camera_upload,collections,content_filter,download_certificates,dvr,federated-auth,hardware_transcoding,home,hwtranscode,item_clusters,kevin-bacon,livetv,loudness,lyrics,music-analysis,music_videos,pass,photosV6-edit,photosV6-tv-albums,premium_music_metadata,radio,server-manager,session_bandwidth_restrictions,session_kick,shared-radio,sync,trailers,tuner-sharing,type-first,unsupportedtuners,webhooks"),
                ),
                ("hubSearch".to_string(), json!(true)),
                ("itemClusters".to_string(), json!(true)),
                ("mediaProviders".to_string(), json!(true)),
                ("livetv".to_string(), json!(7)),
                ("multiuser".to_string(), json!(true)),
                ("transcoderPhoto".to_string(), json!(true)),
                (
                    "transcoderVideoResolutions".to_string(),
                    json!("128,128,160,240,320,480,768,720,720,1080,1080,1080,1080"),
                ),
                (
                    "transcoderVideoBitrates".to_string(),
                    json!("64,96,208,320,720,1500,2000,3000,4000,8000,10000,12000,20000"),
                ),
                (
                    "transcoderVideoQualities".to_string(),
                    json!("0,1,2,3,4,5,6,7,8,9,10,11,12"),
                ),
                ("Directory".to_string(), Value::Array(directories)),
            ]),
            false,
        )
    }

    /// Cycle 66, measured identically on kids, zen and plexscratch42: whether a Roku at
    /// `version` is one a real PMS withholds library pivots from -- an absent or
    /// unparseable version, or one below 6.4.2 on the components it carries.
    fn roku_predates_pivots(version: Option<&str>) -> bool {
        let text = version.unwrap_or("");
        let (core, _suffix) = match text.split_once('-') {
            Some((core, suffix)) => (core, Some(suffix)),
            None => (text, None),
        };
        let parts = core.split('.').collect::<Vec<_>>();
        let digits = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());
        // `(\d+)\.(\d+)(?:\.(\d+)(?:\.\d+)*(?:-.*)?)?`: a suffix only after three parts.
        if parts.len() < 2 || !parts.iter().all(|part| digits(part)) || (parts.len() == 2 && _suffix.is_some()) {
            return true;
        }
        let major = parts[0].parse::<u64>().unwrap_or(0);
        let minor = parts[1].parse::<u64>().unwrap_or(0);
        if (major, minor) != (6, 4) {
            return (major, minor) < (6, 4);
        }
        parts.get(2).is_some_and(|patch| patch.parse::<u64>().unwrap_or(0) < 2)
    }

    /// `/media/providers` -- the first call a modern client makes (the oracle's
    /// `media_providers`, `media_provider_rows` and `server_info`, in the order its
    /// JSON writer emits them).
    fn media_providers(&self, query: &HashMap<String, String>, headers: &HashMap<String, String>) -> Value {
        let claimed = self.assert_claimed;
        let mut directories = vec![json!({"hubKey": "/hubs", "title": "Home"})];
        let option_on = |name: &str| query.get(name).is_some_and(|value| bool_option(value) && value.trim_start_matches('+').trim_start_matches('0') == "1");
        let storage = option_on("includeStorage");
        let preferences = option_on("includePreferences");
        let mut duration_sum = 0i64;
        let mut storage_sum = 0i64;
        for section in &self.catalog.sections {
            let key = string_value(section.get("key").unwrap_or(&Value::Null));
            let kind = section.get("type").and_then(Value::as_str).unwrap_or("movie");
            let show = kind == "show";
            let mut pivots = vec![
                json!({"id": "recommended", "key": format!("/hubs/sections/{key}"), "type": "hub",
                       "title": "Recommended", "context": "content.discover", "symbol": "star"}),
                json!({"id": "library", "key": format!("/library/sections/{key}/all?type={}", if show { 2 } else { 1 }),
                       "type": "list", "title": "Library", "context": "content.library", "symbol": "library"}),
            ];
            // Cycle 59, measured on fun and both copies: a library that HOLDS a
            // collection gains this tab after Library; one that holds none does not.
            if self.collections.values().any(|collection| collection.section == key) {
                pivots.push(json!({"id": "collections", "key": format!("/library/sections/{key}/collections"),
                                   "type": "list", "title": "Collections", "context": "content.collections",
                                   "symbol": "stack"}));
            }
            pivots.push(json!({"id": "categories", "key": format!("/library/sections/{key}/categories"),
                               "type": "list", "title": "Categories", "context": "content.categories",
                               "symbol": "stack"}));
            let mut row = Map::new();
            row.insert("agent".to_string(), json!(if show { "tv.plex.agents.series" } else { "tv.plex.agents.movie" }));
            row.insert("language".to_string(), section.get("language").cloned().filter(|v| !v.is_null() && v != "").unwrap_or_else(|| json!("en-US")));
            row.insert("refreshing".to_string(), json!(false));
            row.insert("scanner".to_string(), json!(if show { "Plex TV Series" } else { "Plex Movie" }));
            row.insert("uuid".to_string(), section.get("uuid").cloned().unwrap_or(Value::Null));
            row.insert("id".to_string(), json!(key));
            row.insert("key".to_string(), json!(format!("/library/sections/{key}")));
            row.insert("hubKey".to_string(), json!(format!("/hubs/sections/{key}")));
            row.insert("type".to_string(), json!(kind));
            row.insert("title".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
            row.insert("updatedAt".to_string(), json!(1757500000));
            row.insert("scannedAt".to_string(), json!(1757500000));
            if preferences {
                // Cycle 25: the block `/library/sections?includePreferences=1` answers,
                // the FIRST child of the library `Directory`, before `Pivot`.
                row.insert("Preferences".to_string(), json!({"Setting": self.section_preference_rows(&key, kind)}));
            }
            if storage {
                // Cycle 46: each library `Directory` gains its own totals as JSON
                // NUMBERS; cycle 56: a SHOW library's totals are its EPISODES'.
                let mut own_duration = 0i64;
                let mut own_storage = 0i64;
                for item in self.section_rows(&key) {
                    let owners = if item.get("Media").is_some() {
                        vec![item]
                    } else {
                        self.leaves(&key_of(&item).unwrap_or_default())
                    };
                    for owner in owners.iter().filter(|owner| owner.get("Media").is_some()) {
                        own_duration += owner.get("duration").and_then(Value::as_i64).unwrap_or(0);
                        own_storage += owner
                            .get("Media")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                            .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
                            .filter_map(|part| part.get("size").and_then(Value::as_i64))
                            .sum::<i64>();
                    }
                }
                row.insert("durationTotal".to_string(), json!(own_duration));
                row.insert("storageTotal".to_string(), json!(own_storage));
                duration_sum += own_duration;
                storage_sum += own_storage;
            }
            row.insert("Pivot".to_string(), Value::Array(pivots));
            directories.push(Value::Object(row));
        }
        // Cycle 59, measured on plexscratch42 and plexscratch: a server holding a
        // VIDEO playlist lists this source LAST, with one pivot.
        if self
            .playlists
            .values()
            .any(|playlist| self.dynamic_playlist_row(playlist).get("playlistType").and_then(Value::as_str) == Some("video"))
        {
            directories.push(json!({
                "id": "playlists", "key": "/playlists", "type": "playlist", "title": "Playlists",
                "Pivot": [{"id": "playlists.video", "key": "/playlists?playlistType=video", "type": "list",
                           "title": "Video", "context": "content.playlists.video", "symbol": "playlist"}]
            }));
        }
        // Cycle 16/66: an OLD Roku (`X-Plex-Platform: Roku`, header or query, and a
        // version below 6.4.2) is handed no `id` and no `Pivot` on the library rows;
        // cycle 59: the Playlists source keeps its `id` and loses only its pivot.
        let platform = headers
            .get("x-plex-platform")
            .or_else(|| query.get("X-Plex-Platform"))
            .map(String::as_str);
        let version = headers
            .get("x-plex-version")
            .or_else(|| query.get("X-Plex-Version"))
            .map(String::as_str);
        if platform == Some("Roku") && Self::roku_predates_pivots(version) {
            for row in directories.iter_mut() {
                let Some(object) = row.as_object_mut() else { continue };
                let key = object.get("key").map(string_value).unwrap_or_default();
                if key.starts_with("/library/sections/") {
                    object.remove("id");
                    object.remove("Pivot");
                } else if key == "/playlists" {
                    object.remove("Pivot");
                }
            }
        }
        let mut content = Map::new();
        content.insert("key".to_string(), json!("/library/sections"));
        content.insert("type".to_string(), json!("content"));
        if storage {
            // ...and the `content` FEATURE gains the sum over every library.
            content.insert("durationTotal".to_string(), json!(duration_sum));
            content.insert("storageTotal".to_string(), json!(storage_sum));
        }
        content.insert("Directory".to_string(), Value::Array(directories));
        let features = vec![
            Value::Object(content),
            json!({"key": "/hubs/search", "type": "search"}),
            json!({"key": "/library/matches", "type": "match"}),
            json!({"key": "/library/metadata", "type": "metadata"}),
            json!({"key": "/:/rate", "type": "rate"}),
            json!({"key": "/photo/:/transcode", "type": "imagetranscoder"}),
            json!({"key": "/hubs/promoted", "type": "promoted"}),
            json!({"key": "/hubs/continueWatching", "type": "continuewatching"}),
            json!({"key": "/actions", "type": "actions",
                   "Action": [{"id": "removeFromContinueWatching", "key": "/actions/removeFromContinueWatching"}]}),
            json!({"flavor": "universal", "key": "/playlists", "type": "playlist"}),
            json!({"flavor": "universal", "key": "/playQueues", "type": "playqueue"}),
            json!({"key": "/library/collections", "type": "collection"}),
            json!({"scrobbleKey": "/:/scrobble", "unscrobbleKey": "/:/unscrobble", "key": "/:/timeline", "type": "timeline"}),
            json!({"type": "manage"}),
            json!({"type": "queryParser"}),
            json!({"flavor": "download", "type": "subscribe"}),
            json!({"key": "/library/search", "type": "universalsearch"}),
        ];
        let mut body = Map::new();
        for (name, value) in [
            ("size", json!(1)),
            ("allowCameraUpload", json!(false)),
            ("allowChannelAccess", json!(true)),
            ("allowMediaDeletion", json!(true)),
            ("allowSharing", json!(true)),
            ("allowSync", json!(false)),
            ("allowTuners", json!(false)),
            ("apiVersion", json!("1.1.1")),
            ("backgroundProcessing", json!(true)),
            ("certificate", json!(true)),
            ("companionProxy", json!(true)),
            ("countryCode", json!("deu")),
            ("diagnostics", json!("logs,streaminglogs")),
            ("eventStream", json!(true)),
            ("friendlyName", json!(self.friendly)),
            ("livetv", json!(7)),
            ("machineIdentifier", json!(self.machine)),
            ("musicAnalysis", json!(2)),
            ("myPlex", json!(true)),
            ("myPlexMappingState", json!("mapped")),
            ("myPlexSigninState", json!(if claimed { "ok" } else { "none" })),
            ("myPlexSubscription", json!(claimed)),
            ("myPlexUsername", if claimed { json!(self.owner_email) } else { Value::Null }),
            ("ownerFeatures", json!(OWNER_FEATURES)),
            ("platform", json!("Linux")),
            ("platformVersion", json!("6.1")),
            ("pluginHost", json!(true)),
            ("pushNotifications", json!(false)),
            ("readOnlyLibraries", json!(false)),
            ("streamingBrainABRVersion", json!(3)),
            ("streamingBrainVersion", json!(2)),
            ("sync", json!(true)),
            ("transcoderActiveVideoSessions", json!(0)),
            ("transcoderAudio", json!(false)),
            ("transcoderLyrics", json!(false)),
            ("transcoderSubtitles", json!(false)),
            ("transcoderVideo", json!(false)),
            ("transcoderVideoBitrates", json!("64,96,208,320,720,1500,2000,3000,4000,8000,10000,12000,20000")),
            ("transcoderVideoQualities", json!("0,1,2,3,4,5,6,7,8,9,10,11,12")),
            ("transcoderVideoResolutions", json!("128,128,160,240,320,480,768,720,720,1080,1080,1080,1080")),
            ("updatedAt", json!(1789127123)),
            ("updater", json!(true)),
            ("version", json!(VERSION)),
            ("voiceSearch", json!(true)),
        ] {
            if !value.is_null() {
                body.insert(name.to_string(), value);
            }
        }
        body.insert(
            "MediaProvider".to_string(),
            json!([{
                "identifier": "com.plexapp.plugins.library",
                "title": "Library",
                "types": "video,audio,photo",
                "protocols": "stream,download",
                "Feature": features,
            }]),
        );
        json!({"MediaContainer": body})
    }

    fn metadata_provider_rows(&self) -> Vec<Value> {
        let Some(document) = load_json_sidecar(&self.catalog_path, "system_agents.json") else {
            return Vec::new();
        };
        let media_types = document
            .get("mediaTypes")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        document
            .get("agents")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|agent| {
                let identifier = agent
                    .get("identifier")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let types = media_types
                    .iter()
                    .filter(|(_, providers)| {
                        providers
                            .as_array()
                            .is_some_and(|rows| rows.iter().any(|row| row.as_str() == Some(identifier)))
                    })
                    .filter_map(|(number, _)| number.parse::<i64>().ok())
                    .map(|number| json!({"type":number}))
                    .collect::<Vec<_>>();
                json!({
                    "id": agent.get("id").cloned().unwrap_or(Value::Null),
                    "identifier": identifier,
                    "title": agent.get("name").cloned().unwrap_or_else(|| json!(identifier)),
                    "uri": if identifier.ends_with(".none") { "" } else { "provider://tv.plex.provider.metadata" },
                    "agentType": "primary",
                    "online": true,
                    "MetadataType": types,
                })
            })
            .collect()
    }

    fn metadata_provider_group_row(&self, group: &MetadataProviderGroup) -> Value {
        json!({
            "id": group.id,
            "title": group.title,
            "primaryIdentifier": group.primary_identifier,
            "MetadataAgentProviderGroupItem": group.items,
        })
    }

    fn metadata_provider_groups(&self) -> Vec<Value> {
        let mut rows = self
            .metadata_provider_rows()
            .into_iter()
            .map(|provider| {
                let id = string_value(provider.get("id").unwrap_or(&Value::Null));
                let identifier = provider
                    .get("identifier")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                json!({
                    "id": id,
                    "title": provider.get("title").cloned().unwrap_or(Value::Null),
                    "primaryIdentifier": identifier,
                    "builtIn": "1",
                    "MetadataAgentProviderGroupItem": [{
                        "id": id,
                        "metadataAgentProviderGroupId": id,
                        "metadataAgentProviderId": provider.get("id").cloned().unwrap_or(Value::Null),
                        "order": 1000.0
                    }]
                })
            })
            .collect::<Vec<_>>();
        rows.extend(
            self.metadata_provider_groups
                .values()
                .map(|group| self.metadata_provider_group_row(group)),
        );
        rows
    }

    fn route_metadata_providers(
        &mut self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        // Cycle 31: creating a provider needs an external provider object; a POST to
        // either listing is a 400.
        if request.method == "POST" && (segments.len() == 3 || (segments.len() == 4 && segments[3] == "group")) {
            return Response::error(400, "plexsim: bad request");
        }
        if segments.len() == 3 {
            if request.method == "GET" || request.method == "HEAD" {
                let mut rows = self.metadata_provider_rows();
                if let Some(raw) = query.get("metadataTypes").filter(|raw| !raw.is_empty()) {
                    // Cycle 54, measured on kids and zen: a comma list of int32 media
                    // types (a 400 otherwise), keeping the providers that serve EVERY
                    // one of them -- `0` answers none, `1` the movie agent and the no-op
                    // agent.  An empty value is every provider.
                    let parts = raw.split(',').collect::<Vec<_>>();
                    if !parts.iter().all(|part| int_param(part, Some(32))) {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let wanted = parts
                        .iter()
                        .map(|part| part.trim_start_matches('+').parse::<i64>().unwrap_or(0))
                        .collect::<HashSet<_>>();
                    rows.retain(|row| {
                        let served = row
                            .get("MetadataType")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                            .map(|kind| kind.get("type").and_then(Value::as_i64).unwrap_or(0))
                            .collect::<HashSet<_>>();
                        wanted.is_subset(&served)
                    });
                }
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([
                            ("size".to_string(), json!(rows.len())),
                            ("MetadataAgentProvider".to_string(), Value::Array(rows)),
                        ]),
                        false,
                    ),
                );
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.len() == 4 && segments[3] == "group" {
            if request.method == "GET" || request.method == "HEAD" {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([(
                            "MetadataAgentProviderGroup".to_string(),
                            Value::Array(self.metadata_provider_groups()),
                        )]),
                        false,
                    ),
                );
            }
            if request.method == "POST" {
                let Some(primary) = query.get("primaryIdentifier") else {
                    return Response::error(400, "plexsim: bad request");
                };
                let provider = self
                    .metadata_provider_rows()
                    .into_iter()
                    .find(|row| row.get("identifier").and_then(Value::as_str) == Some(primary));
                let Some(provider) = provider else {
                    return Response::error(400, "plexsim: bad request");
                };
                let id = self.next_metadata_provider_group.to_string();
                self.next_metadata_provider_group += 1;
                let provider_id = provider.get("id").cloned().unwrap_or(Value::Null);
                let group = MetadataProviderGroup {
                    id: id.clone(),
                    title: query
                        .get("title")
                        .cloned()
                        .unwrap_or_else(|| "Provider Group".to_string()),
                    primary_identifier: primary.clone(),
                    items: vec![json!({
                        "id": id,
                        "metadataAgentProviderGroupId": id,
                        "metadataAgentProviderId": provider_id,
                        "order": 1000.0
                    })],
                };
                let row = self.metadata_provider_group_row(&group);
                self.metadata_provider_groups
                    .insert(group.id.clone(), group);
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([(
                            "MetadataAgentProviderGroup".to_string(),
                            Value::Array(vec![row]),
                        )]),
                        false,
                    ),
                );
            }
            return Response::error(404, "plexsim: not found");
        }
        let Some(group_id) = segments.get(4).copied() else {
            return Response::error(404, "plexsim: not found");
        };
        if segments.len() == 5 && request.method == "PUT" {
            let Some(group) = self.metadata_provider_groups.get_mut(group_id) else {
                return Response::error(404, "plexsim: not found");
            };
            if let Some(title) = query.get("title") {
                group.title = title.clone();
            }
            let row = group.clone();
            return Response::json(
                200,
                self.container(
                    Map::from_iter([(
                        "MetadataAgentProviderGroup".to_string(),
                        Value::Array(vec![self.metadata_provider_group_row(&row)]),
                    )]),
                    false,
                ),
            );
        }
        if segments.len() == 5 && request.method == "DELETE" {
            self.metadata_provider_groups.remove(group_id);
            return Response::empty(200);
        }
        if segments.len() == 7 && segments[5] == "items" {
            if request.method == "PUT" {
                return Response::error(400, "plexsim: bad request");
            }
            if request.method == "DELETE" {
                return Response::error(404, "plexsim: not found");
            }
        }
        Response::error(404, "plexsim: not found")
    }

    fn route_library_tags(&self, query: &HashMap<String, String>) -> Response {
        let Some(raw_type) = query.get("type") else {
            return Response::error(400, "plexsim: bad request");
        };
        let Ok(type_number) = raw_type.parse::<i32>() else {
            return Response::error(400, "plexsim: bad request");
        };
        if type_number == 318 {
            let mut names = self
                .catalog
                .items
                .iter()
                .filter_map(|row| row.get("studio").and_then(Value::as_str))
                .map(str::to_string)
                .collect::<HashSet<_>>()
                .into_iter()
                .collect::<Vec<_>>();
            names.sort_by_key(|name| tag_id("Studio", name));
            let rows = names
                .into_iter()
                .map(|name| {
                    let id = tag_id("Studio", &name);
                    json!({"id": id, "filter": format!("={id}"), "tag": name, "tagType": 318})
                })
                .collect::<Vec<_>>();
            return Response::json(
                200,
                self.container(
                    Map::from_iter([("Directory".to_string(), Value::Array(rows))]),
                    false,
                ),
            );
        }
        let (element, filter_name) = match type_number {
            1 => ("Genre", "genre"),
            2 => ("Collection", "collection"),
            4 => ("Director", "director"),
            5 => ("Writer", "writer"),
            6 => ("Role", "actor"),
            7 => ("Producer", "producer"),
            8 => ("Country", "country"),
            319 => ("Network", "network"),
            _ => {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Directory".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                )
            }
        };
        let mut found = HashMap::<String, Value>::new();
        for row in &self.catalog.items {
            for tag in row
                .get(element)
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let Some(name) = tag.get("tag").and_then(Value::as_str) else {
                    continue;
                };
                let id = tag
                    .get("id")
                    .map(string_value)
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| tag_id(element, name).to_string());
                found.entry(id).or_insert_with(|| tag.clone());
            }
        }
        let mut ids = found.keys().cloned().collect::<Vec<_>>();
        ids.sort_by(|left, right| {
            left.parse::<i64>()
                .unwrap_or(i64::MAX)
                .cmp(&right.parse::<i64>().unwrap_or(i64::MAX))
                .then_with(|| left.cmp(right))
        });
        let rows = ids
            .into_iter()
            .filter_map(|id| {
                let tag = found.get(&id)?;
                let name = tag.get("tag")?.as_str()?;
                let mut row = Map::from_iter([
                    ("id".to_string(), json!(id.parse::<i64>().unwrap_or(0))),
                    ("filter".to_string(), json!(format!("{filter_name}={id}"))),
                    ("tag".to_string(), json!(name)),
                    ("tagType".to_string(), json!(type_number)),
                ]);
                for extra in ["tagKey", "thumb"] {
                    if let Some(value) = tag.get(extra).filter(|value| !value.is_null()) {
                        row.insert(extra.to_string(), value.clone());
                    }
                }
                Some(Value::Object(row))
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Directory".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn route_person(
        &self,
        requested: &str,
        media: bool,
        _query: &HashMap<String, String>,
    ) -> Response {
        let wanted = requested
            .parse::<i64>()
            .map(|value| value.to_string())
            .unwrap_or_else(|_| requested.to_string());
        if requested.parse::<i64>().is_err()
            && (requested.len() != 24
                || requested
                    .chars()
                    .any(|character| !character.is_ascii_hexdigit()))
        {
            return Response::error(404, "plexsim: not found");
        }
        let mut found = None;
        let mut rows = Vec::new();
        let mut seen = HashSet::new();
        for row in &self.catalog.items {
            for (element, name, tag_type) in [
                ("Role", "actor", 6_i64),
                ("Director", "director", 4_i64),
                ("Writer", "writer", 5_i64),
                ("Producer", "producer", 7_i64),
            ] {
                for tag in row
                    .get(element)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if tag.get("id").map(string_value).as_deref() != Some(&wanted)
                        && tag.get("tagKey").and_then(Value::as_str) != Some(requested)
                    {
                        continue;
                    }
                    if found.is_none() {
                        let mut person = Map::from_iter([
                            (
                                "id".to_string(),
                                tag.get("id").cloned().unwrap_or(Value::Null),
                            ),
                            (
                                "filter".to_string(),
                                json!(format!(
                                    "{name}={}",
                                    string_value(tag.get("id").unwrap_or(&Value::Null))
                                )),
                            ),
                            (
                                "tag".to_string(),
                                tag.get("tag").cloned().unwrap_or(Value::Null),
                            ),
                            ("tagType".to_string(), json!(tag_type)),
                        ]);
                        for extra in ["tagKey", "thumb"] {
                            if let Some(value) = tag.get(extra).filter(|value| !value.is_null()) {
                                person.insert(extra.to_string(), value.clone());
                            }
                        }
                        found = Some(Value::Object(person));
                    }
                    if seen.insert(key_of(row).unwrap_or_default()) {
                        rows.push(row.clone());
                    }
                }
            }
        }
        let Some(person) = found else {
            return Response::error(404, "plexsim: not found");
        };
        if !media {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        (
                            "identifier".to_string(),
                            json!("com.plexapp.plugins.library"),
                        ),
                        ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
                        ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
                        ("Directory".to_string(), Value::Array(vec![person])),
                    ]),
                    false,
                ),
            );
        }
        let tag_type = person.get("tagType").and_then(Value::as_i64).unwrap_or(0);
        let shown = rows
            .iter()
            .map(|row| self.listing_row(row))
            .collect::<Vec<_>>();
        let mut fields = Map::from_iter([
            (
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            ),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
            ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
            ("Metadata".to_string(), Value::Array(shown)),
        ]);
        if tag_type == 6 {
            fields.insert("viewGroup".to_string(), json!("movie"));
            fields.insert("viewMode".to_string(), json!(65592));
        }
        Response::json(200, self.container(fields, false))
    }

    /// `/library/arts` (cycle 45): every row that OWNS an `art`, and a listing QUERY --
    /// the filters constrain it, a bad sort or limit is a 500, and it refuses the
    /// fifteen keys `/hubs` refuses.
    fn route_library_arts(&self, section_filter: Option<&str>, query: &HashMap<String, String>) -> Response {
        if let Some(refusal) = hub_refusal(query) {
            return refusal;
        }
        if let Some(status) = listing::listing_refusals(query, &[]) {
            return Response::error(status, "plexsim: refused");
        }
        let pairs = query
            .iter()
            .filter(|(key, _)| !matches!(key.as_str(), "type" | "limit"))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<Vec<_>>();
        let mut rows = Vec::new();
        for section in &self.catalog.sections {
            let section_key = string_value(section.get("key").unwrap_or(&Value::Null));
            if section_filter.is_some_and(|wanted| wanted != section_key) {
                continue;
            }
            let section_type = section.get("type").and_then(Value::as_str).unwrap_or("movie");
            let members = if section_filter.is_some() {
                self.catalog
                    .items
                    .iter()
                    .filter(|row| {
                        string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section_key
                    })
                    .cloned()
                    .collect::<Vec<_>>()
            } else {
                self.section_rows(&section_key)
            };
            let mut members = match self.filter_rows(members, &pairs, section_type) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
            if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
                if self.sort_rows(Vec::new(), Some(sort)).is_err() {
                    return Response::error(500, "plexsim: unsupported sort key");
                }
            }
            if section_filter.is_none() {
                members.retain(|row| {
                    let key = key_of(row).unwrap_or_default();
                    row.get("art")
                        .and_then(Value::as_str)
                        .is_some_and(|art| art.starts_with(&format!("/library/metadata/{key}/")))
                });
            }
            members.sort_by_key(|row| {
                string_value(row.get("ratingKey").unwrap_or(&Value::Null))
                    .parse::<i64>()
                    .unwrap_or(0)
            });
            for row in members {
                let art = row.get("art").and_then(Value::as_str).unwrap_or_default();
                if art.is_empty() {
                    continue;
                }
                rows.push(json!({
                    "title": row.get("title").cloned().unwrap_or(Value::Null),
                    "type": "image",
                    "key": art,
                    "thumb": "",
                    "label": "",
                    "mediaType": "",
                    "summary": "",
                    "rating": -1,
                    "addDate": -1,
                    "modDate": -1,
                    "metaModDate": -1,
                }));
            }
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("allowSync".to_string(), json!(false)),
                    ("Metadata".to_string(), Value::Array(rows.clone())),
                ]),
                false,
            ),
        )
    }

    fn route_library_matches(&self, query: &HashMap<String, String>) -> Response {
        let Some(raw_type) = query.get("type") else {
            return Response::error(400, "plexsim: bad request");
        };
        let Ok(type_number) = raw_type.parse::<i32>() else {
            return Response::error(400, "plexsim: bad request");
        };
        let wanted = match type_number {
            1 => "movie",
            2 => "show",
            3 => "season",
            4 => "episode",
            _ => "",
        };
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some(wanted))
            .filter(|row| {
                if let Some(guid) = query.get("guid") {
                    return row.get("guid").and_then(Value::as_str) == Some(guid);
                }
                if wanted == "episode" && query.contains_key("grandparentTitle") {
                    return row
                        .get("grandparentTitle")
                        .map(string_value)
                        .unwrap_or_default()
                        .eq_ignore_ascii_case(
                            query
                                .get("grandparentTitle")
                                .map(String::as_str)
                                .unwrap_or_default(),
                        )
                        && row.get("parentIndex").map(string_value)
                            == query.get("parentIndex").cloned()
                        && row.get("index").map(string_value) == query.get("index").cloned();
                }
                let Some(title) = query.get("title") else {
                    return false;
                };
                matches!(wanted, "movie" | "show")
                    && row
                        .get("title")
                        .map(string_value)
                        .unwrap_or_default()
                        .eq_ignore_ascii_case(title)
                    && query
                        .get("year")
                        .map(|year| {
                            row.get("year").map(string_value).as_deref() == Some(year.as_str())
                        })
                        .unwrap_or(true)
            })
            .map(|row| self.listing_row(row))
            .collect::<Vec<_>>();
        rows.sort_by_key(|row| {
            string_value(
                row.get("titleSort")
                    .or_else(|| row.get("title"))
                    .unwrap_or(&Value::Null),
            )
            .to_lowercase()
        });
        if query.contains_key("title") && matches!(wanted, "movie" | "show") {
            rows = rows
                .into_iter()
                .map(|row| {
                    let mut object = Map::new();
                    object.insert("score".to_string(), json!(100));
                    if let Value::Object(original) = row {
                        object.extend(original);
                    }
                    Value::Object(object)
                })
                .collect();
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn mark_device(&mut self, headers: &HashMap<String, String>) {
        let identifier = headers
            .get("x-plex-client-identifier")
            .cloned()
            .unwrap_or_default();
        let platform_text = headers.get("x-plex-platform").cloned().unwrap_or_default();
        let platform = platform_text
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number)
            .unwrap_or_else(|| json!(platform_text));
        self.devices.insert(
            identifier.clone(),
            Device {
                client_identifier: identifier,
                name: headers
                    .get("x-plex-device-name")
                    .cloned()
                    .unwrap_or_default(),
                product: headers.get("x-plex-product").cloned().unwrap_or_default(),
                platform,
                last_seen: now(),
            },
        );
        self.dirty = true;
    }

    fn item_mut(&mut self, key: &str) -> Option<&mut Value> {
        self.catalog.get_mut(key)
    }

    fn save_if_dirty(&mut self) {
        if self.dirty && self.state_path.is_some() {
            let _ = self.save_state();
        }
    }

    fn push_event(&mut self, kind: &str, value: Value) {
        self.events.push((kind.to_string(), value));
        if self.events.len() > 8192 {
            let excess = self.events.len() - 8192;
            self.events.drain(0..excess);
            self.events_dropped += excess;
        }
    }

    /// The sequence number the next event will take. A socket opened now reads from here:
    /// once the 512-event bound was reached, `events.len()` stopped moving and every
    /// subscriber after that point saw no frame at all.
    fn event_cursor(&self) -> usize {
        self.events_dropped + self.events.len()
    }

    fn events_since(&self, cursor: usize) -> Vec<(String, Value)> {
        self.events
            .iter()
            .skip(cursor.saturating_sub(self.events_dropped))
            .cloned()
            .collect()
    }

    fn push_timeline(&mut self, key: &str, state: i64, metadata_state: Option<&str>) {
        let row = self
            .catalog
            .get(key)
            .cloned()
            .unwrap_or_else(|| json!({"ratingKey": key}));
        self.push_timeline_row(&row, state, metadata_state, None);
    }

    /// A TimelineEntry for one row (`notify_timeline`): see `push_timeline_entry`.
    fn notify_timeline(
        &mut self,
        row: &Value,
        state: i64,
        metadata_state: Option<&str>,
        media_state: Option<&str>,
        type_number: Option<i64>,
    ) {
        self.push_timeline_entry(row, state, metadata_state, media_state, type_number, None);
    }

    fn timeline_key(&mut self, key: &str, state: i64, metadata_state: Option<&str>, media_state: Option<&str>) {
        if let Some(row) = self.catalog.get(key).cloned() {
            self.notify_timeline(&row, state, metadata_state, media_state, None);
        }
    }

    /// The `Updating Metadata` activity a refresh runs (shape measured on fun
    /// mid-refresh), started at -1.
    fn start_metadata_activity(&mut self, subtitle: &str) -> Map<String, Value> {
        let mut activity = butler::new_activity("library.update.item.metadata", "Updating Metadata", false);
        activity.insert("subtitle".to_string(), json!(subtitle));
        activity.insert("progress".to_string(), json!(-1));
        self.notify_activity(&activity, "started");
        activity
    }

    /// The seven-frame sequence a real PMS pushes for a metadata refresh,
    /// measured verbatim: timeline(3, queued), activity started, activity
    /// updated (49), timeline(3, processing), timeline(4, loading), activity
    /// ended, timeline(5).  Cycle 33: a row whose guid is `local://` -- never
    /// matched -- opens with `state 2 matching` and a bare `state 5` first.
    fn notify_refreshed(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        self.notify_refreshed_row(&row);
    }

    fn notify_refreshed_row(&mut self, row: &Value) {
        let row = row.clone();
        if row.get("guid").and_then(Value::as_str).unwrap_or("").starts_with("local://") {
            self.notify_timeline(&row, 2, Some("matching"), None, None);
            self.notify_timeline(&row, 5, None, None, None);
        }
        self.notify_timeline(&row, 3, Some("queued"), None, None);
        let subtitle = self.frame_title(&row);
        let mut activity = self.start_metadata_activity(&subtitle);
        activity.insert("progress".to_string(), json!(49));
        self.notify_activity(&activity, "updated");
        self.notify_timeline(&row, 3, Some("processing"), None, None);
        self.notify_timeline(&row, 4, Some("loading"), None, None);
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.notify_timeline(&row, 5, None, None, None);
    }

    /// The frames a show-wide re-read pushes (cycle 15): the show queued ->
    /// processing -> loading inside one `Updating Metadata` activity; then
    /// each SEASON (5 processing, 4 loading) with its EPISODES (5 processing,
    /// 4 loading twice, 5), the media pass per episode, the season's 5; the
    /// activity steps per season, and the SHOW's own 5 ends it.
    fn notify_show_cascade(&mut self, key: &str) {
        let Some(show) = self.catalog.get(key).cloned() else {
            return;
        };
        let subtitle = self.frame_title(&show);
        let mut activity = self.start_metadata_activity(&subtitle);
        self.notify_timeline(&show, 3, Some("queued"), None, None);
        self.notify_timeline(&show, 3, Some("processing"), None, None);
        self.notify_timeline(&show, 4, Some("loading"), None, None);
        let seasons = self.children(key);
        let show_title = show.get("title").and_then(Value::as_str).unwrap_or("").to_string();
        for (number, season) in seasons.iter().enumerate() {
            let index = season.get("index").and_then(Value::as_i64).unwrap_or(0);
            activity.insert("subtitle".to_string(), json!(format!("{show_title} S{index:02}")));
            activity.insert("progress".to_string(), json!(((number + 1) as f64 * 89.0 / seasons.len().max(1) as f64).round() as i64));
            self.notify_activity(&activity, "updated");
            self.notify_timeline(season, 5, Some("processing"), None, None);
            self.notify_timeline(season, 4, Some("loading"), None, None);
            let episodes = self.children(&key_of(season).unwrap_or_default());
            for episode in &episodes {
                self.notify_timeline(episode, 5, Some("processing"), None, None);
                self.notify_timeline(episode, 4, Some("loading"), None, None);
                self.notify_timeline(episode, 4, Some("loading"), None, None);
                self.notify_timeline(episode, 5, None, None, None);
            }
            for episode in &episodes {
                self.notify_timeline(episode, 5, None, Some("analyzing"), None);
                self.notify_timeline(episode, 5, None, Some("thumbnailing"), None);
                self.notify_timeline(episode, 5, None, None, None);
            }
            self.notify_timeline(season, 5, None, None, None);
        }
        // finish_activity: an `updated` at 49 unless it already stands there.
        if activity.get("progress").and_then(Value::as_i64) != Some(49) {
            activity.insert("progress".to_string(), json!(49));
            self.notify_activity(&activity, "updated");
        }
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.notify_timeline(&show, 5, None, None, None);
    }

    /// The eighteen frames a SEASON refresh pushes (cycle 15): season queued,
    /// the activity started with the season's frame title, season processing
    /// and loading; each episode's four; then the activity ENDED with nothing
    /// between (cycle 25) and the season's own 5.
    fn notify_season_cascade(&mut self, key: &str) {
        let Some(season) = self.catalog.get(key).cloned() else {
            return;
        };
        let subtitle = self.frame_title(&season);
        let mut activity = self.start_metadata_activity(&subtitle);
        self.notify_timeline(&season, 3, Some("queued"), None, None);
        self.notify_timeline(&season, 3, Some("processing"), None, None);
        self.notify_timeline(&season, 4, Some("loading"), None, None);
        for episode in self.children(key) {
            self.notify_timeline(&episode, 5, Some("processing"), None, None);
            self.notify_timeline(&episode, 4, Some("loading"), None, None);
            self.notify_timeline(&episode, 4, Some("loading"), None, None);
            self.notify_timeline(&episode, 5, None, None, None);
        }
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.notify_timeline(&season, 5, None, None, None);
    }

    /// The 46 frames a SHOW's unmatch pushes (cycle 25).  The opener is the
    /// movie unmatch's pair plus queued; the activity opens with the bare
    /// TITLE (not the frame title -- the one place on this surface), steps
    /// `Analyzing Media` -1/25 and `Updating Metadata` 25/50, then follows each
    /// season (subtitle first, then progress `100 - 5 * seasons left`).
    fn notify_show_unmatch_cascade(&mut self, key: &str) {
        let Some(show) = self.catalog.get(key).cloned() else {
            return;
        };
        self.notify_timeline(&show, 2, Some("matching"), None, None);
        self.notify_timeline(&show, 3, Some("matching"), None, None);
        self.notify_timeline(&show, 3, Some("queued"), None, None);
        let title = show.get("title").and_then(Value::as_str).unwrap_or("").to_string();
        let mut activity = self.start_metadata_activity(&title);
        for (name, progress) in [
            ("Analyzing Media", -1),
            ("Analyzing Media", 25),
            ("Updating Metadata", 25),
            ("Updating Metadata", 50),
        ] {
            activity.insert("title".to_string(), json!(name));
            activity.insert("progress".to_string(), json!(progress));
            self.notify_activity(&activity, "updated");
        }
        self.notify_timeline(&show, 3, Some("processing"), None, None);
        self.notify_timeline(&show, 4, Some("loading"), None, None);
        let seasons = self.children(key);
        for (number, season) in seasons.iter().enumerate() {
            activity.insert("subtitle".to_string(), json!(self.frame_title(season)));
            self.notify_activity(&activity, "updated");
            activity.insert("progress".to_string(), json!(100 - 5 * (seasons.len() - (number + 1)) as i64));
            self.notify_activity(&activity, "updated");
            self.notify_timeline(season, 5, Some("processing"), None, None);
            self.notify_timeline(season, 4, Some("loading"), None, None);
            for episode in self.children(&key_of(season).unwrap_or_default()) {
                self.notify_timeline(&episode, 5, Some("processing"), None, None);
                self.notify_timeline(&episode, 4, Some("loading"), None, None);
                self.notify_timeline(&episode, 4, Some("loading"), None, None);
                self.notify_timeline(&episode, 5, None, None, None);
            }
            self.notify_timeline(season, 5, None, None, None);
        }
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.notify_timeline(&show, 5, None, None, None);
    }

    /// A MOVIE's unmatch: TWELVE frames (cycle 23) -- `state 2` then `state 3`
    /// matching, queued, the activity through `Analyzing Media` and back to
    /// `Updating Metadata`, processing, loading, ended, and 5.
    fn notify_movie_unmatch(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        self.notify_timeline(&row, 2, Some("matching"), None, None);
        self.notify_timeline(&row, 3, Some("matching"), None, None);
        self.notify_timeline(&row, 3, Some("queued"), None, None);
        let subtitle = self.frame_title(&row);
        let mut activity = self.start_metadata_activity(&subtitle);
        for (name, progress) in [
            ("Analyzing Media", -1),
            ("Analyzing Media", 25),
            ("Updating Metadata", 25),
            ("Updating Metadata", 50),
        ] {
            activity.insert("title".to_string(), json!(name));
            activity.insert("progress".to_string(), json!(progress));
            self.notify_activity(&activity, "updated");
        }
        self.notify_timeline(&row, 3, Some("processing"), None, None);
        self.notify_timeline(&row, 4, Some("loading"), None, None);
        activity.insert("title".to_string(), json!("Updating Metadata"));
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.notify_timeline(&row, 5, None, None, None);
    }

    fn push_refresh_events_named(&mut self, key: &str, subtitle: &str) {
        if self.catalog.get(key).is_some() {
            self.notify_refreshed(key);
            return;
        }
        // A list row lives outside the catalog: frame it as the list it is.
        let row = if let Some(collection) = self.collections.get(key) {
            json!({"ratingKey": key, "type": "collection", "title": subtitle,
                   "librarySectionID": collection.section})
        } else {
            json!({"ratingKey": key, "type": "playlist", "title": subtitle, "librarySectionID": -1})
        };
        self.notify_refreshed_row(&row);
    }

    /// A metadata refresh's frames: the measured seven-frame sequence.
    fn push_refresh_events(&mut self, key: &str) {
        self.notify_refreshed(key);
    }

    fn push_edit_events(&mut self, key: &str) {
        self.push_edit_events_named(key, "loading");
    }

    fn push_edit_events_named(&mut self, key: &str, metadata_state: &str) {
        self.push_timeline(key, 3, Some(metadata_state));
        self.push_timeline(key, 5, None);
        self.push_event(
            "log",
            json!({"LogSentNotification": {"level": "info", "thread": "plexsim", "time": now(), "message": "metadata updated"}}),
        );
    }

    fn push_delete_event(&mut self, key: &str) {
        self.push_timeline(key, 9, None);
    }

    /// Strip the agent's identity, the way `PUT .../unmatch` does -- measured on
    /// the reference by diffing the row's keys across the call.
    ///
    /// A movie or show: the guid becomes `tv.plex.agents.none://<ratingKey>`,
    /// the title the FILE NAME without its extension, the summary is emptied
    /// and the agent's tag lists go; artwork, Guid, Rating and year are KEPT.
    /// An EPISODE or SEASON: the guid becomes `local://<ratingKey>` and the
    /// title the generated `Episode N` / `Season N`.  The call CASCADES: a
    /// season unmatches its episodes (each `parentGuid` follows it) and a show
    /// its seasons and every episode under them.
    fn unmatch_row(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("").to_string();
        if kind == "episode" || kind == "season" {
            let index = row.get("index").map(string_value).unwrap_or_default();
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("guid".to_string(), json!(format!("local://{key}")));
                // The generated title, marked as generated so a later index edit
                // re-titles it; a season keeps it as its sort title too.
                let label = if kind == "season" { "Season" } else { "Episode" };
                let title = format!("{label} {index}");
                object.insert("_generatedTitle".to_string(), json!(true));
                object.insert("title".to_string(), json!(title.clone()));
                if kind == "season" {
                    object.insert("titleSort".to_string(), json!(title));
                } else {
                    object.remove("titleSort");
                }
                object.insert("updatedAt".to_string(), json!(now()));
                if kind == "episode" {
                    // Re-measured against a DIRECT episode unmatch: the same field
                    // set goes as in a season's cascade, and the summary empties.
                    for field in ["Director", "Writer", "Producer", "Role", "audienceRating", "year"] {
                        object.remove(field);
                    }
                    object.insert("summary".to_string(), json!(""));
                }
            }
            if kind == "season" {
                // The season loses its own `year` (the show's then shows through
                // as `parentYear`) and its `thumb` falls back to the SHOW's.
                let show = row
                    .get("parentRatingKey")
                    .map(string_value)
                    .and_then(|parent| self.catalog.get(&parent).cloned());
                if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                    object.remove("year");
                    if let Some(thumb) = show
                        .as_ref()
                        .and_then(|show| show.get("thumb"))
                        .filter(|thumb| !string_value(thumb).is_empty())
                    {
                        object.insert("thumb".to_string(), thumb.clone());
                    }
                }
                let guid = format!("local://{key}");
                let episodes = self
                    .catalog
                    .items
                    .iter()
                    .filter(|leaf| {
                        leaf.get("type").and_then(Value::as_str) == Some("episode")
                            && leaf.get("parentRatingKey").map(string_value).as_deref() == Some(key)
                    })
                    .filter_map(key_of)
                    .collect::<Vec<_>>();
                for episode in episodes {
                    self.unmatch_row(&episode);
                    if let Some(object) = self.catalog.get_mut(&episode).and_then(Value::as_object_mut) {
                        object.insert("parentGuid".to_string(), json!(guid.clone()));
                        object.remove("parentThumb");
                    }
                }
            }
            return;
        }
        let guid = format!("tv.plex.agents.none://{key}");
        let file_title = row
            .get("Media")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
            .filter_map(|part| part.get("file").and_then(Value::as_str).map(str::to_string))
            .filter_map(|file| file.rsplit('/').next().map(str::to_string))
            .find(|name| !name.is_empty())
            .map(|name| name.rsplit_once('.').map(|(stem, _)| stem.to_string()).unwrap_or(name));
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for field in [
                "Genre", "Country", "Role", "Director", "Writer", "Producer", "studio",
                "contentRating", "contentRatingAge", "tagline", "audienceRating", "rating",
            ] {
                object.remove(field);
            }
            object.insert("summary".to_string(), json!(""));
            object.insert("guid".to_string(), json!(guid.clone()));
            if kind == "show" {
                object.remove("art");
            }
            if let Some(title) = file_title {
                object.insert("title".to_string(), json!(title));
            }
            object.insert("updatedAt".to_string(), json!(now()));
        }
        if kind == "show" {
            // A SHOW unmatch cascades the whole way down: every season and
            // episode becomes `local://`, their parent/grandparent guids follow
            // the show's new `none://` one, and the artwork and contentRating
            // the children borrowed from the show go with it.
            for season in self.children(key) {
                if season.get("type").and_then(Value::as_str) != Some("season") {
                    continue;
                }
                let season_key = key_of(&season).unwrap_or_default();
                self.unmatch_row(&season_key);
                if let Some(object) = self.catalog.get_mut(&season_key).and_then(Value::as_object_mut) {
                    object.insert("parentGuid".to_string(), json!(guid.clone()));
                    object.remove("parentThumb");
                    object.remove("parentStudio");
                }
            }
            for leaf in self.leaves(key) {
                let leaf_key = key_of(&leaf).unwrap_or_default();
                if let Some(object) = self.catalog.get_mut(&leaf_key).and_then(Value::as_object_mut) {
                    object.insert("grandparentGuid".to_string(), json!(guid.clone()));
                    object.remove("grandparentThumb");
                    object.remove("grandparentArt");
                    object.remove("contentRating");
                }
            }
        }
    }

    fn push_list_timeline(&mut self, item_id: &str, kind: i64, state: i64, metadata: &str) {
        self.push_event(
            "timeline",
            json!({"TimelineEntry": {
                "identifier": "com.plexapp.plugins.library",
                "itemID": item_id,
                "metadataState": metadata,
                "sectionID": "-1",
                "state": state,
                "title": "",
                "type": kind,
                "updatedAt": now()
            }}),
        );
    }

    fn update_view(&mut self, key: &str, field: &str, value: Option<Value>) -> bool {
        let Some(row) = self.item_mut(key) else {
            return false;
        };
        let Some(object) = row.as_object_mut() else {
            return false;
        };
        match value {
            Some(value) => {
                object.insert(field.to_string(), value);
            }
            None => {
                object.remove(field);
            }
        }
        self.dirty = true;
        true
    }

    /// One row of `/status/sessions/history/all` (library.record_view).
    ///
    /// The key is a MONOTONIC counter, never reused (the reference went 34, 35,
    /// delete 35, then 36).  Cycle 25: an EPISODE row carries `title` and
    /// `originallyAvailableAt` too, plus the parent/grandparent keys, titles,
    /// indexes and artwork; the `_history` marker gives the row its own measured
    /// attribute order in both writers and never reaches a client.
    fn history_row(&self, row: &Value) -> Value {
        let key = key_of(row).unwrap_or_default();
        let number = self.history_counter.get() + 1;
        self.history_counter.set(number);
        let mut entry = Map::new();
        entry.insert("historyKey".to_string(), json!(format!("/status/sessions/history/{number}")));
        entry.insert("key".to_string(), json!(format!("/library/metadata/{key}")));
        entry.insert("ratingKey".to_string(), json!(key));
        entry.insert(
            "librarySectionID".to_string(),
            json!(string_value(row.get("librarySectionID").unwrap_or(&Value::Null))),
        );
        entry.insert("type".to_string(), row.get("type").cloned().unwrap_or(Value::Null));
        entry.insert("viewedAt".to_string(), json!(now()));
        entry.insert("accountID".to_string(), json!(1));
        entry.insert("deviceID".to_string(), json!(1));
        entry.insert("_history".to_string(), json!(true));
        let detail = self.detail_row(row);
        if row.get("type").and_then(Value::as_str) == Some("episode") {
            for (source, target) in [("parentRatingKey", "parentKey"), ("grandparentRatingKey", "grandparentKey")] {
                if let Some(value) = row.get(source).map(string_value).filter(|value| !value.is_empty()) {
                    entry.insert(target.to_string(), json!(format!("/library/metadata/{value}")));
                }
            }
            for name in ["grandparentTitle", "index", "parentIndex", "thumb", "parentThumb", "grandparentThumb", "grandparentArt"] {
                if let Some(value) = detail.get(name).filter(|value| !value.is_null()) {
                    entry.insert(name.to_string(), value.clone());
                }
            }
        } else if let Some(value) = row.get("thumb").filter(|value| !value.is_null()) {
            entry.insert("thumb".to_string(), value.clone());
        }
        for name in ["title", "originallyAvailableAt"] {
            if let Some(value) = detail.get(name).filter(|value| !value.is_null()) {
                entry.insert(name.to_string(), value.clone());
            }
        }
        Value::Object(entry)
    }

    fn history_row_for_device(&self, row: &Value, device_id: i64) -> Value {
        let mut history = self.history_row(row);
        if let Some(object) = history.as_object_mut() {
            object.insert("deviceID".to_string(), json!(device_id));
        }
        history
    }

    fn device_id(&self, identifier: &str) -> i64 {
        let mut identifiers = self
            .devices
            .values()
            .map(|device| device.client_identifier.clone())
            .collect::<Vec<_>>();
        if let Some(index) = identifiers.iter().position(|value| value.is_empty()) {
            identifiers.swap(0, index);
        }
        identifiers
            .iter()
            .position(|value| value == identifier)
            .map(|index| index as i64 + 1)
            .unwrap_or(identifiers.len() as i64 + 1)
    }

    fn choose_rows(&self, section: Option<&str>, type_number: Option<i64>) -> Vec<Value> {
        let mut rows = if let Some(section) = section {
            self.catalog
                .items
                .iter()
                .filter(|row| {
                    string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
                })
                .cloned()
                .collect()
        } else {
            self.catalog.items.clone()
        };
        if let Some(number) = type_number {
            let kind = match number {
                1 => "movie",
                2 => "show",
                3 => "season",
                4 => "episode",
                8 => "clip",
                _ => "",
            };
            rows.retain(|row| row.get("type").and_then(Value::as_str) == Some(kind));
        }
        rows
    }

    fn filter_field_types(&self, section_type: &str) -> HashMap<String, String> {
        let mut fields = HashMap::from([
            ("title".to_string(), "string".to_string()),
            ("titleSort".to_string(), "string".to_string()),
            ("summary".to_string(), "string".to_string()),
            ("tagline".to_string(), "string".to_string()),
            ("studio".to_string(), "string".to_string()),
            ("year".to_string(), "integer".to_string()),
            ("decade".to_string(), "integer".to_string()),
            ("viewCount".to_string(), "integer".to_string()),
            ("viewOffset".to_string(), "integer".to_string()),
            ("rating".to_string(), "integer".to_string()),
            ("userRating".to_string(), "integer".to_string()),
            ("lastViewedAt".to_string(), "date".to_string()),
            ("lastRatedAt".to_string(), "date".to_string()),
            ("viewUpdatedAt".to_string(), "date".to_string()),
            ("lastSkippedAt".to_string(), "date".to_string()),
            ("beginsAt".to_string(), "date".to_string()),
            ("endsAt".to_string(), "date".to_string()),
            ("addedAt".to_string(), "date".to_string()),
            ("originallyAvailableAt".to_string(), "date".to_string()),
            ("id".to_string(), "id".to_string()),
            ("guid".to_string(), "string".to_string()),
            // Cycle 20: `type` with an operator is the row's TYPE NUMBER.
            ("type".to_string(), "integer".to_string()),
            // Cycle 15/55: `parent=` is a NUMBER column -- `+<show>` is that show's
            // seasons and `1.5` an empty 200.
            ("parent".to_string(), "integer".to_string()),
            ("parentIndex".to_string(), "integer".to_string()),
            ("originalTitle".to_string(), "string".to_string()),
            ("createdAt".to_string(), "date".to_string()),
            ("ratingCount".to_string(), "integer".to_string()),
            ("mediaCount".to_string(), "integer".to_string()),
            ("absoluteIndex".to_string(), "integer".to_string()),
            ("index".to_string(), "integer".to_string()),
            ("skipCount".to_string(), "integer".to_string()),
            ("contentRatingAge".to_string(), "integer".to_string()),
            ("extraType".to_string(), "integer".to_string()),
            ("editionTitle".to_string(), "string".to_string()),
            ("unmatched".to_string(), "boolean".to_string()),
            ("unwatched".to_string(), "boolean".to_string()),
            ("inProgress".to_string(), "boolean".to_string()),
            ("location".to_string(), "integer".to_string()),
            // These columns are read by the listing query engine even though
            // the ordinary Meta descriptor does not advertise them.  The
            // reference still parses and evaluates them; leaving them out
            // turns a real constraint into an ignored parameter.
            ("updatedAt".to_string(), "date".to_string()),
            ("remote".to_string(), "integer".to_string()),
            ("mediaAnalysisVersion".to_string(), "integer".to_string()),
            ("lastSkippedAt".to_string(), "date".to_string()),
            ("beginsAt".to_string(), "date".to_string()),
            ("endsAt".to_string(), "date".to_string()),
            ("resourcesChangedAt".to_string(), "string".to_string()),
            ("isAdult".to_string(), "string".to_string()),
            ("agent".to_string(), "string".to_string()),
            ("source".to_string(), "string".to_string()),
            ("cluster".to_string(), "integer".to_string()),
            ("clusterStartsAt".to_string(), "date".to_string()),
            ("clusterZoomLevel".to_string(), "integer".to_string()),
            ("clustering".to_string(), "integer".to_string()),
            ("trash".to_string(), "integer".to_string()),
            ("tagId".to_string(), "tagId".to_string()),
            ("duplicate".to_string(), "boolean".to_string()),
            ("unviewedLeafCount".to_string(), "integer".to_string()),
            ("viewedLeafCount".to_string(), "integer".to_string()),
            (
                "viewedOrInProgressLeafCount".to_string(),
                "integer".to_string(),
            ),
            ("mediaHeight".to_string(), "integer".to_string()),
            ("mediaWidth".to_string(), "integer".to_string()),
            ("mediaAudioChannels".to_string(), "integer".to_string()),
            ("mediaVideoCodec".to_string(), "media_string".to_string()),
            ("mediaAudioCodec".to_string(), "media_string".to_string()),
            ("mediaContainer".to_string(), "media_string".to_string()),
            ("unwatchedLeaves".to_string(), "integer".to_string()),
        ]);
        for field in [
            "genre",
            "collection",
            "director",
            "writer",
            "producer",
            "actor",
            "country",
            "label",
            "mood",
            "style",
            "role",
            // Cycle 54: eighteen more tag-table fields from the binary's parameter names.
            "similar",
            "format",
            "subformat",
            "tag",
            "make",
            "model",
            "lens",
            "aperture",
            "exposure",
            "iso",
            "device",
            "autotag",
            "place",
            "channel",
            "mediaProcessingTarget",
            "sharedWith",
        ] {
            fields.insert(field.to_string(), "tag".to_string());
        }
        fields.insert("clusterStartsAt".to_string(), "date".to_string());
        fields.insert("contentRating".to_string(), "tag".to_string());
        fields.insert("audioLanguage".to_string(), "audioLanguage".to_string());
        fields.insert(
            "subtitleLanguage".to_string(),
            "subtitleLanguage".to_string(),
        );
        fields.insert("file".to_string(), "string".to_string());
        // The Meta's own media Field types (section_meta.json): advertised on
        // both library types, so a SHOW grid filters on them through its leaves.
        for (field, kind) in [
            ("duration", "integer"),
            ("mediaSize", "integer"),
            ("mediaBitrate", "integer"),
            ("hdr", "boolean"),
        ] {
            fields.entry(field.to_string()).or_insert_with(|| kind.to_string());
        }
        fields.insert(
            "resolution".to_string(),
            if section_type == "movie" {
                "resolution".to_string()
            } else {
                "resolution".to_string()
            },
        );
        fields
    }

    fn filter_value(&self, row: &Value, field: &str) -> Value {
        if field == "title" {
            return json!(crate::hierarchy::stored_title(row.as_object().unwrap_or(&Map::new())));
        }
        // A few query columns intentionally do not appear on the row itself.
        // They are projections over hierarchy, view state, tags, or Media and
        // must be resolved before the generic scalar lookup below.
        if field == "type" {
            return json!(match row.get("type").and_then(Value::as_str) {
                Some("movie") => 1,
                Some("show") => 2,
                Some("season") => 3,
                Some("episode") => 4,
                Some("clip") => 8,
                _ => 0,
            });
        }
        if field == "tagId" {
            return Value::Array(
                [
                    "Genre",
                    "Collection",
                    "Director",
                    "Writer",
                    "Producer",
                    "Role",
                    "Country",
                    "Label",
                    "Mood",
                    "Style",
                ]
                .into_iter()
                .flat_map(|name| {
                    row.get(name)
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .filter_map(|tag| tag.get("id").cloned())
                })
                .collect(),
            );
        }
        if field == "parent" {
            return row.get("parentRatingKey").cloned().unwrap_or(Value::Null);
        }
        if field == "trash" {
            // plexsim holds no trashed row; kids' own answer is 0 everywhere.
            return json!(0);
        }
        if matches!(field, "cluster" | "clusterStartsAt" | "clusterZoomLevel" | "clustering") {
            // The photo-cluster columns match no row of a video library.
            return Value::Null;
        }
        if matches!(
            field,
            "similar" | "format" | "subformat" | "tag" | "make" | "model" | "lens" | "aperture"
                | "exposure" | "iso" | "device" | "autotag" | "place" | "channel"
                | "mediaProcessingTarget" | "sharedWith"
        ) {
            let element = if field == "iso" { "ISO".to_string() } else { capitalize_ascii(field) };
            return row.get(&element).cloned().unwrap_or_else(|| json!([]));
        }
        if field == "titleSort" {
            return json!(crate::hierarchy::stored_sort_title(
                row.as_object().unwrap_or(&Map::new()),
            ));
        }
        if field == "index" && row.get("type").and_then(Value::as_str) == Some("movie") {
            return row.get("index").cloned().unwrap_or_else(|| json!(1));
        }
        if field == "unmatched" {
            return json!(row
                .get("guid")
                .and_then(Value::as_str)
                .is_some_and(|value| value.starts_with("local://")
                    || value.starts_with("tv.plex.agents.none://")
                    || value.starts_with("none://")));
        }
        if field == "unwatched" {
            return json!(row.get("viewCount").is_none());
        }
        if field == "inProgress" {
            return json!(row.get("viewOffset").and_then(Value::as_i64).unwrap_or(0) > 0);
        }
        if field == "mediaCount" {
            return row
                .get("Media")
                .and_then(Value::as_array)
                .map(|media| json!(media.len()))
                .unwrap_or(Value::Null);
        }
        if field == "duplicate" {
            let own = row
                .get("Media")
                .and_then(Value::as_array)
                .is_some_and(|media| media.len() > 1);
            if own {
                return json!(1);
            }
            if matches!(
                row.get("type").and_then(Value::as_str),
                Some("show" | "season")
            ) {
                return Value::Array(
                    self.leaves(&string_value(row.get("ratingKey").unwrap_or(&Value::Null)))
                        .into_iter()
                        .map(|leaf| {
                            json!(leaf
                                .get("Media")
                                .and_then(Value::as_array)
                                .is_some_and(|media| media.len() > 1)
                                as i64)
                        })
                        .collect(),
                );
            }
            return json!(0);
        }
        if matches!(
            field,
            "viewedLeafCount" | "unviewedLeafCount" | "viewedOrInProgressLeafCount"
        ) {
            let leaves = self.show_leaves_for_counts(row);
            let viewed = leaves
                .iter()
                .filter(|leaf| leaf.get("viewCount").is_some())
                .count() as i64;
            let in_progress = leaves
                .iter()
                .filter(|leaf| leaf.get("viewOffset").and_then(Value::as_i64).unwrap_or(0) > 0)
                .count() as i64;
            return json!(match field {
                "viewedLeafCount" => viewed,
                "unviewedLeafCount" => leaves.len() as i64 - viewed,
                _ => viewed + in_progress,
            });
        }
        if matches!(
            field,
            "mediaHeight"
                | "mediaWidth"
                | "mediaAudioChannels"
                | "mediaVideoCodec"
                | "mediaAudioCodec"
                | "mediaContainer"
                | "mediaBitrate"
                | "mediaSize"
                | "duration"
                | "resolution"
        ) {
            let mut media = row
                .get("Media")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if media.is_empty()
                && matches!(
                    row.get("type").and_then(Value::as_str),
                    Some("show" | "season")
                )
            {
                media = self
                    .leaves(&string_value(row.get("ratingKey").unwrap_or(&Value::Null)))
                    .into_iter()
                    .flat_map(|leaf| {
                        leaf.get("Media")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default()
                    })
                    .collect();
            }
            if field == "duration" {
                return Value::Array(
                    media
                        .iter()
                        .filter_map(|m| m.get("duration").cloned())
                        .collect(),
                );
            }
            if field == "resolution" {
                return Value::Array(
                    media
                        .iter()
                        .filter_map(|m| m.get("videoResolution").cloned())
                        .collect(),
                );
            }
            let values = media
                .iter()
                .filter_map(|m| match field {
                    "mediaHeight" => m.get("height"),
                    "mediaWidth" => m.get("width"),
                    "mediaAudioChannels" => m.get("audioChannels"),
                    "mediaVideoCodec" => m.get("videoCodec"),
                    "mediaAudioCodec" => m.get("audioCodec"),
                    "mediaContainer" => m.get("container"),
                    "mediaBitrate" => m.get("bitrate"),
                    "mediaSize" => m
                        .get("Part")
                        .and_then(Value::as_array)
                        .and_then(|parts| parts.iter().find_map(|part| part.get("size"))),
                    _ => None,
                })
                .cloned()
                .collect();
            return Value::Array(values);
        }
        if field == "createdAt" {
            return row.get("addedAt").cloned().unwrap_or(Value::Null);
        }
        if field == "unwatchedLeaves" {
            // Cycle 54: 1 while the SHOW (for a season or an episode) has an unwatched leaf.
            let leaves = self.show_leaves_for_counts(row);
            let viewed = leaves.iter().filter(|leaf| leaf.get("viewCount").is_some()).count();
            return json!((leaves.len() > viewed) as i64);
        }
        if field == "viewUpdatedAt" {
            // Cycle 54: the per-user write stamp, kept private on the row.
            return row.get("_viewUpdatedAt").cloned().unwrap_or(Value::Null);
        }
        if matches!(field, "viewCount" | "skipCount") && row.get(field).is_none() {
            // Cycle 56, measured on plexscratch42 and on zen and kids: ANY per-user
            // write creates the row's settings record with both counters at 0, and
            // the counter filters read it; a row never written has no record,
            // which is `viewCount`'s null-as-zero half and a null `skipCount` that
            // matches nothing.
            return if has_settings(row) { json!(0) } else { Value::Null };
        }
        if matches!(field, "audioLanguage" | "subtitleLanguage") {
            let stream_type = if field == "audioLanguage" { 2 } else { 3 };
            return Value::Array(
                row.get("Media")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .flat_map(|media| {
                        media
                            .get("Part")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                    })
                    .flat_map(|part| {
                        part.get("Stream")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                    })
                    .filter(|stream| {
                        stream.get("streamType").and_then(Value::as_i64) == Some(stream_type)
                    })
                    .filter_map(|stream| {
                        stream
                            .get("languageCode")
                            .or_else(|| stream.get("language"))
                    })
                    .cloned()
                    .collect(),
            );
        }
        let actual = match field {
            "genre" => row.get("Genre"),
            "collection" => row.get("Collection"),
            "director" => row.get("Director"),
            "writer" => row.get("Writer"),
            "producer" => row.get("Producer"),
            "actor" | "role" => row.get("Role"),
            "country" => row.get("Country"),
            "label" => row.get("Label"),
            "mood" => row.get("Mood"),
            "style" => row.get("Style"),
            "contentRating" => row.get("contentRating"),
            "location" => row.get("librarySectionID"),
            "id" => row.get("ratingKey"),
            "decade" => row.get("year"),
            "parent" => row
                .get("parentRatingKey")
                .or_else(|| row.get("grandparentRatingKey")),
            "file" => {
                // Cycle 15: a case-insensitive substring of ANY Part path, the
                // paths joined on a newline so no match spans two; cycle 20: a
                // SEASON with no Media reads its leaves'.
                let files = |row: &Value| {
                    row.get("Media")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
                        .map(|part| part.get("file").map(string_value).unwrap_or_default())
                        .collect::<Vec<_>>()
                };
                let mut paths = files(row);
                if paths.is_empty() && row.get("type").and_then(Value::as_str) == Some("season") {
                    paths = self
                        .leaves(&key_of(row).unwrap_or_default())
                        .iter()
                        .flat_map(|leaf| files(leaf))
                        .collect();
                }
                return json!(paths.join("\n"));
            }
            "titleSort" => row.get("titleSort").or_else(|| row.get("title")),
            _ => row.get(field),
        };
        if field == "decade" {
            return actual
                .and_then(Value::as_i64)
                .map(|year| json!(year / 10 * 10))
                .unwrap_or(Value::Null);
        }
        actual.cloned().unwrap_or(Value::Null)
    }

    /// The leaves a leaf-count filter counts: a show's own, and -- cycles 14/54 -- the
    /// SHOW's for a season or an episode row.
    fn show_leaves_for_counts(&self, row: &Value) -> Vec<Value> {
        let key = string_value(row.get("ratingKey").unwrap_or(&Value::Null));
        match row.get("type").and_then(Value::as_str) {
            Some("show") => self.leaves(&key),
            Some("season") => row
                .get("parentRatingKey")
                .map(string_value)
                .map(|parent| self.leaves(&parent))
                .unwrap_or_default(),
            Some("episode") => row
                .get("grandparentRatingKey")
                .map(string_value)
                .map(|show| self.leaves(&show))
                .unwrap_or_default(),
            _ => Vec::new(),
        }
    }

    fn filter_rows(
        &self,
        rows: Vec<Value>,
        pairs: &[(String, String)],
        section_type: &str,
    ) -> Result<Vec<Value>, u16> {
        let field_types = self.filter_field_types(section_type);
        // These names are known to the query parser but the listing serializer
        // refuses them.  Treating them as unknown made the native server return
        // the unfiltered library, which is materially different from PMS's 500.
        for (name, _) in pairs {
            let (field, _) = crate::filters::split_key(name);
            let (_, bare) = crate::filters::split_scope(&field);
            if matches!(
                bare.as_str(),
                "parentIndex"
                    | "slug"
                    | "extraType"
                    | "theme"
                    | "banner"
                    | "art"
                    | "thumb"
                    | "userFields"
            ) {
                return Err(500);
            }
        }
        let mut errors = Vec::new();
        let Some(tree) = crate::filters::parse(pairs, &field_types, &mut errors) else {
            return Ok(rows);
        };
        let mut output = Vec::new();
        for row in rows {
            let value_of = |field: &str, value: &Value| self.filter_value(value, field);
            let resolve = |scope: &str, value: &Value| -> Vec<Value> {
                let kind = value
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                match (scope, kind) {
                    ("show", "show") | ("season", "season") | ("episode", "episode") => {
                        vec![value.clone()]
                    }
                    // A scoped filter on a parent row ranges over the
                    // descendants named by its prefix.  The previous
                    // implementation only resolved upward (episode -> show
                    // and season), so `episode.title=` on a show and
                    // `episode.index=` on a season silently matched nothing.
                    ("season", "show") => self.children(&string_value(
                        value.get("ratingKey").unwrap_or(&Value::Null),
                    )),
                    ("episode", "show") => self.leaves(&string_value(
                        value.get("ratingKey").unwrap_or(&Value::Null),
                    )),
                    ("episode", "season") => self.children(&string_value(
                        value.get("ratingKey").unwrap_or(&Value::Null),
                    )),
                    ("show", "season") => value
                        .get("parentRatingKey")
                        .map(string_value)
                        .and_then(|parent| self.catalog.get(&parent).cloned())
                        .into_iter()
                        .collect(),
                    ("show", "episode") => value
                        .get("grandparentRatingKey")
                        .map(string_value)
                        .and_then(|parent| self.catalog.get(&parent).cloned())
                        .into_iter()
                        .collect(),
                    ("season", "episode") => value
                        .get("parentRatingKey")
                        .map(string_value)
                        .and_then(|parent| self.catalog.get(&parent).cloned())
                        .into_iter()
                        .collect(),
                    _ => Vec::new(),
                }
            };
            match crate::filters::evaluate_with(&tree, &row, &value_of, Some(&resolve)) {
                Ok(true) => output.push(row),
                Ok(false) => {}
                Err(error) => return Err(error.status),
            }
        }
        Ok(output)
    }

    fn sort_rows(&self, mut rows: Vec<Value>, sort: Option<&str>) -> Result<Vec<Value>, u16> {
        let Some(sort) = sort else {
            return Ok(rows);
        };
        if sort.is_empty() {
            return Err(500);
        }
        let mut specs = Vec::new();
        let mut random_sort = false;
        for spec in sort.split(',') {
            let mut pieces = spec.split(':');
            let field = pieces.next().unwrap_or_default();
            let modifiers = pieces.collect::<Vec<_>>();
            if field == "random" || field.ends_with(".random") {
                random_sort = true;
                let seed = modifiers
                    .iter()
                    .find_map(|modifier| modifier.parse::<u64>().ok())
                    .unwrap_or_else(|| {
                        SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .map(|duration| duration.as_nanos() as u64)
                            .unwrap_or(0)
                    });
                // Fisher-Yates with a deterministic linear generator gives
                // distinct, repeatable orders for seeds that happen to have
                // the same modulo as the old rotate shortcut.
                let mut state = seed;
                for index in (1..rows.len()).rev() {
                    state = state
                        .wrapping_mul(6_364_136_223_846_793_005)
                        .wrapping_add(1_442_695_040_888_963_407);
                    let swap = (state as usize) % (index + 1);
                    rows.swap(index, swap);
                }
                continue;
            }
            let direction = modifiers
                .iter()
                .find(|modifier| **modifier == "asc" || **modifier == "desc")
                .copied()
                .unwrap_or("asc");
            let allowed = [
                "title",
                "titleSort",
                "year",
                "addedAt",
                "lastViewedAt",
                "viewUpdatedAt",
                "rating",
                "audienceRating",
                "userRating",
                "contentRating",
                "duration",
                "mediaCount",
                "mediaSize",
                "mediaBitrate",
                "mediaWidth",
                "mediaAudioChannels",
                "viewCount",
                "viewOffset",
                "originallyAvailableAt",
                "studio",
                "summary",
                "tagline",
                "originalTitle",
                "editionTitle",
                "contentRatingAge",
                "skipCount",
                "absoluteIndex",
                "ratingCount",
                "createdAt",
                "updatedAt",
                "lastRatedAt",
                "lastSkippedAt",
                "beginsAt",
                "endsAt",
                "remote",
                "isAdult",
                "resourcesChangedAt",
                // Cycle 53: the tenth schema column sorts too.
                "mediaAnalysisVersion",
                "mediaHeight",
                "type",
                "index",
                "id",
                "taggingCreatedAt",
                "unviewedLeafCount",
                "viewedLeafCount",
            ];
            let bare = field.rsplit('.').next().unwrap_or(field);
            if !allowed.contains(&bare) {
                return Err(500);
            }
            specs.push((field.to_string(), direction == "desc"));
        }
        if random_sort && specs.is_empty() {
            return Ok(rows);
        }
        // Python's stable sort uses titleSort as the final tie breaker for
        // every advertised key.  Seed that order before applying the explicit
        // keys in reverse priority.
        rows.sort_by(|left, right| {
            string_value(&self.sort_value(left, "titleSort"))
                .cmp(&string_value(&self.sort_value(right, "titleSort")))
        });
        for (field, descending) in specs.into_iter().rev() {
            rows.sort_by(|left, right| {
                let left_owned = self.sort_value(left, &field);
                let right_owned = self.sort_value(right, &field);
                let left_value = &left_owned;
                let right_value = &right_owned;
                let ordering = match (left_value, right_value) {
                    (Value::Number(a), Value::Number(b)) => a
                        .as_f64()
                        .partial_cmp(&b.as_f64())
                        .unwrap_or(std::cmp::Ordering::Equal),
                    _ => string_value(left_value).cmp(&string_value(right_value)),
                };
                if descending {
                    ordering.reverse()
                } else {
                    ordering
                }
            });
        }
        Ok(rows)
    }

    /// Resolve the type prefix used by Plex's query grammar (`show.titleSort`,
    /// `season.index`, and so on) against the row's owning hierarchy.  The
    /// bare field is deliberately kept separate from this lookup: the same
    /// field name means a different object when a client scopes it.
    fn sort_value(&self, row: &Value, field: &str) -> Value {
        let (scope, bare) = field
            .rsplit_once('.')
            .map_or(("", field), |(scope, bare)| (scope, bare));
        let target = if scope.is_empty() {
            Some(row)
        } else {
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
            let Some(key) = (match (kind, scope) {
                ("episode", "show") => row.get("grandparentRatingKey").map(string_value),
                ("episode", "season") => row.get("parentRatingKey").map(string_value),
                ("season", "show") => row.get("parentRatingKey").map(string_value),
                (_, wanted) if wanted == kind => key_of(row),
                _ => None,
            }) else {
                return Value::Null;
            };
            self.catalog.get(&key)
        };
        if bare == "id" {
            // The row's id is its ratingKey, ordered as a number.
            return target
                .and_then(key_of)
                .and_then(|key| key.parse::<i64>().ok())
                .map(|id| json!(id))
                .unwrap_or(Value::Null);
        }
        if bare == "viewUpdatedAt" {
            return target
                .and_then(|target| target.get("_viewUpdatedAt"))
                .cloned()
                .unwrap_or(Value::Null);
        }
        if bare == "mediaCount" {
            return target
                .and_then(|target| target.get("Media"))
                .and_then(Value::as_array)
                .map(|media| json!(media.len()))
                .unwrap_or(Value::Null);
        }
        if bare == "mediaBitrate" {
            return target
                .and_then(|target| target.get("Media"))
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|media| media.get("bitrate").and_then(Value::as_f64))
                .max_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
                .map(|value| json!(value))
                .unwrap_or(Value::Null);
        }
        if bare == "mediaSize" {
            return target
                .and_then(|target| target.get("Media"))
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .flat_map(|media| {
                    media
                        .get("Part")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                })
                .filter_map(|part| part.get("size").and_then(Value::as_f64))
                .max_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
                .map(|value| json!(value))
                .unwrap_or(Value::Null);
        }
        if bare == "type" {
            return json!(match target
                .and_then(|value| value.get("type"))
                .and_then(Value::as_str)
            {
                Some("movie") => 1,
                Some("show") => 2,
                Some("season") => 3,
                Some("episode") => 4,
                Some("clip") => 8,
                Some("playlist") => 15,
                Some("collection") => 18,
                _ => 99,
            });
        }
        if bare == "titleSort" {
            return target
                .and_then(Value::as_object)
                .map(|object| json!(crate::hierarchy::stored_sort_title(object)))
                .unwrap_or(Value::Null);
        }
        if bare == "createdAt" {
            return target
                .and_then(|value| value.get("addedAt"))
                .cloned()
                .unwrap_or(Value::Null);
        }
        if bare == "mediaHeight" {
            return target
                .and_then(|target| target.get("Media"))
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|media| media.get("height").and_then(Value::as_i64))
                .max()
                .map(|height| json!(height))
                .unwrap_or(Value::Null);
        }
        target
            .and_then(|target| {
                target
                    .get(bare)
                    .or_else(|| (bare == "titleSort").then(|| target.get("title")).flatten())
            })
            .cloned()
            .unwrap_or(Value::Null)
    }

    fn rows_for_uri(&self, uri: &str) -> Vec<Value> {
        let decoded = percent_decode(uri);
        // Cycle 16, measured on plexscratch: a uri that is not this server's own
        // `server://<machine>/...` or a stored `library://` resolves to NOTHING -- a
        // foreign machine and a scheme-less `/library/metadata/1` alike. plexsim read
        // the ratingKey out of any uri shaped like one, so another server's item
        // silently queued its own.
        if !uri.is_empty() {
            if let Some(rest) = decoded.strip_prefix("server://") {
                let machine = rest.split('/').next().unwrap_or("");
                if !machine.is_empty() && machine != self.machine {
                    return Vec::new();
                }
            } else if !decoded.starts_with("library://") {
                return Vec::new();
            }
        }
        if let Some(tail) = decoded.split("/playlists/").nth(1) {
            let playlist_id = tail.split('/').next().unwrap_or_default();
            if tail.starts_with(playlist_id) && tail.contains("/items") {
                if let Some(playlist) = self.playlists.get(playlist_id) {
                    return self
                        .playlist_keys(playlist)
                        .iter()
                        .filter_map(|key| self.catalog.get(key).cloned())
                        .collect();
                }
            }
        }
        let keys = metadata_keys_from_uri(uri);
        if !keys.is_empty() {
            return keys
                .into_iter()
                .filter_map(|key| self.catalog.get(&key).cloned())
                .collect();
        }
        if let Some(start) = decoded.find("/library/sections/") {
            let tail = &decoded[start + "/library/sections/".len()..];
            let (path_tail, query_tail) = tail.split_once('?').unwrap_or((tail, ""));
            let mut parts = path_tail.split('/');
            let section = parts.next().unwrap_or("");
            if !section.is_empty() {
                if parts.next() == Some("all") {
                    let rows = self.section_rows(section);
                    if !query_tail.is_empty() {
                        let pairs = parse_query(query_tail);
                        let section_type = self
                            .section(section)
                            .and_then(|value| value.get("type"))
                            .and_then(Value::as_str)
                            .unwrap_or("movie");
                        let sort = pairs
                            .iter()
                            .find(|(name, _)| name == "sort")
                            .map(|(_, value)| value.as_str());
                        // `limit` is part of the query a smart list stores, measured on a
                        // writable copy of the reference: a list saved with
                        // `sort=titleSort:desc&limit=2` reads back two rows.
                        let limit = pairs
                            .iter()
                            .rev()
                            .find(|(name, _)| name == "limit")
                            .and_then(|(_, value)| value.parse::<usize>().ok())
                            .filter(|limit| *limit > 0);
                        let filter_pairs = pairs
                            .iter()
                            .filter(|(name, _)| name != "sort" && name != "limit")
                            .cloned()
                            .collect::<Vec<_>>();
                        let mut rows = self
                            .filter_rows(rows, &filter_pairs, section_type)
                            .ok()
                            .and_then(|rows| self.sort_rows(rows, sort).ok())
                            .unwrap_or_default();
                        if let Some(limit) = limit {
                            rows.truncate(limit);
                        }
                        return rows;
                    }
                    return rows;
                }
            }
        }
        if let Some(start) = decoded.find("/library/metadata/") {
            let tail = &decoded[start + "/library/metadata/".len()..];
            let key = tail.split(['/', '?', ',']).next().unwrap_or("");
            if !key.is_empty() {
                return self.children(key);
            }
        }
        Vec::new()
    }

    fn dynamic_playlist_row(&self, playlist: &Playlist) -> Value {
        self.playlist_row(playlist, true, true)
    }

    fn playlist_keys(&self, playlist: &Playlist) -> Vec<String> {
        if playlist.smart {
            self.rows_for_uri(&playlist.uri)
                .iter()
                .filter_map(key_of)
                .collect()
        } else {
            playlist.items.clone()
        }
    }

    fn playlist_item_id(&self, playlist: &Playlist, index: usize) -> i64 {
        playlist
            .item_ids
            .get(index)
            .copied()
            .unwrap_or((index + 1) as i64)
    }

    fn list_artwork_response(
        &mut self,
        id: &str,
        is_collection: bool,
        field: &str,
        request: &Request,
    ) -> Response {
        if field == "clearLogos" || field == "themes" {
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("size".to_string(), json!(0)),
                        ("Metadata".to_string(), Value::Array(Vec::new())),
                    ]),
                    false,
                ),
            );
        }
        if field != "posters" && field != "arts" {
            return Response::error(404, "plexsim: not found");
        }
        if request.method == "POST" {
            if field != "posters" {
                return Response::error(404, "plexsim: not found");
            }
            if request.body.is_empty() {
                return Response::error(406, "plexsim: not acceptable");
            }
            let upload = format!("upload://posters/{}", now());
            self.list_artwork
                .insert(id.to_string(), (upload, request.body.clone()));
            self.dirty = true;
            return Response::empty(200);
        }
        if request.method != "GET" && request.method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        let mut fields = Map::from_iter([
            (
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            ),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
            ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
        ]);
        let mut entries = Vec::new();
        if field == "posters" {
            let prefix = if is_collection {
                format!("/library/collections/{id}/composite/1700000000")
            } else {
                format!("/playlists/{id}/composite/1700000000")
            };
            entries.push(json!({
                "ratingKey": "default://",
                "key": format!("{prefix}?width=400&height=600"),
                "thumb": format!("{prefix}?width=200&height=300"),
                "selected": self.list_artwork.get(id).is_none(),
            }));
            if let Some((upload, _)) = self.list_artwork.get(id) {
                entries.push(json!({
                    "ratingKey": upload,
                    "key": upload,
                    "thumb": upload,
                    "selected": true,
                }));
            }
        }
        fields.insert("size".to_string(), json!(entries.len()));
        fields.insert("Metadata".to_string(), Value::Array(entries));
        Response::json(200, self.container(fields, false))
    }

    /// The CORS preflight (`server.py` `do_OPTIONS`): with an `Origin` the echoed
    /// origin, `Vary`, the method list, the requested headers ECHOED only when asked
    /// (cycle 34) and a two-week `Max-Age`; with none, no CORS headers at all (cycle
    /// 16). Always `text/plain`, empty, naming its connection state and `no-cache`
    /// (cycle 33).
    fn preflight(request: &Request) -> Response {
        let mut headers = vec![(wire::RAW_MARK.to_string(), "preflight".to_string())];
        if let Some(origin) = request.headers.get("origin") {
            headers.push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
            headers.push(("Vary".to_string(), "Origin, X-Plex-Token".to_string()));
            headers.push((
                "Access-Control-Allow-Methods".to_string(),
                "POST, GET, OPTIONS, DELETE, PUT, HEAD".to_string(),
            ));
            if let Some(asked) = request
                .headers
                .get("access-control-request-headers")
                .filter(|asked| !asked.is_empty())
            {
                headers.push(("Access-Control-Allow-Headers".to_string(), asked.clone()));
            }
            headers.push(("Access-Control-Max-Age".to_string(), "1209600".to_string()));
        }
        headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
        Response {
            status: 200,
            content_type: "text/plain".to_string(),
            body: Vec::new(),
            headers,
            close: false,
        }
    }

    /// `/library/metadata/{id}/theme/{v}/metadata` -- the track row a real PMS MINTS
    /// (cycle 45): `guid` is `library://` + the SHA-1 of the request path, idempotent per
    /// (item, version), kept beside the catalog so no listing sees it.
    fn theme_metadata(&mut self, key: &str, version: &str) -> Response {
        let path = format!("/library/metadata/{key}/theme/{version}/metadata");
        let minted = match self.theme_rows.get(&path) {
            Some(row) => row.clone(),
            None => {
                let highest = self
                    .catalog
                    .items
                    .iter()
                    .filter_map(|row| key_of(row).and_then(|k| k.parse::<i64>().ok()))
                    .max()
                    .unwrap_or(0);
                self.theme_next = self.theme_next.max(highest + 1);
                let id = self.theme_next;
                self.theme_next += 1;
                let mut digest = Sha1::new();
                digest.update(path.as_bytes());
                let hash = digest.finalize().iter().map(|b| format!("{b:02x}")).collect::<String>();
                let row = json!({
                    "ratingKey": id.to_string(),
                    "key": format!("/library/metadata/{id}"),
                    "guid": format!("library://{hash}"),
                    "type": "track",
                    "title": "",
                    "summary": "",
                    "index": 1,
                    "addedAt": now(),
                    "genuineMediaAnalysis": "1",
                });
                self.theme_rows.insert(path, row.clone());
                self.theme_rows.insert(id.to_string(), row.clone());
                row
            }
        };
        Response::json(
            200,
            json!({"MediaContainer": {"size": 1, "identifier": "com.plexapp.plugins.library",
                "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION,
                "Metadata": [minted]}}),
        )
    }

    /// `/library/shared` and every one-segment leaf below it (cycle 45): the bare path
    /// is the tab bar, EVERY second segment answers the same library list, and a
    /// third segment is a 404.
    fn library_shared(&self, segments: &[&str], query: &HashMap<String, String>) -> Response {
        const TYPES: [(&str, &str); 7] = [
            ("/library/shared/all", "All Shared"),
            ("/library/shared/all?type=1", "Movies"),
            ("/library/shared/all?type=2,3,4", "TV"),
            ("/library/shared/all?type=8,9,10", "Music"),
            ("/library/shared/all?type=13,14,12", "Photos"),
            ("/library/shared/all?type=15", "Playlists"),
            ("/library/shared/libraries", "Libraries"),
        ];
        let sorts = || {
            vec![
                json!({"default": "asc", "defaultDirection": "asc", "descKey": "titleSort:desc",
                       "key": "titleSort", "title": "Title"}),
                json!({"defaultDirection": "asc", "descKey": "taggingCreatedAt:desc",
                       "key": "taggingCreatedAt", "title": "Date Shared"}),
            ]
        };
        if segments.len() == 2 {
            let rows = TYPES
                .iter()
                .map(|(key, title)| json!({"key": key, "type": "mixed", "title": title, "active": false, "Sort": sorts()}))
                .collect::<Vec<_>>();
            return Response::json(200, json!({"MediaContainer": {"size": rows.len(),
                "identifier": "com.plexapp.plugins.library.shared", "Type": rows}}));
        }
        if segments.len() > 3 {
            return Response::error(404, "plexsim: not found");
        }
        let mut rows = self
            .catalog
            .sections
            .iter()
            .map(|section| {
                let key = string_value(section.get("key").unwrap_or(&Value::Null));
                let is_show = section.get("type").and_then(Value::as_str) == Some("show");
                let mut out = Map::new();
                out.insert("art".to_string(), json!(""));
                out.insert("key".to_string(), json!(format!("/library/sections/{key}/all?type={}", if is_show { 2 } else { 1 })));
                out.insert("thumb".to_string(), json!(""));
                if let Some(object) = self.section_directory(section).as_object() {
                    for (name, value) in object {
                        if !matches!(name.as_str(), "key" | "allowSync" | "filters" | "refreshing") {
                            out.insert(name.clone(), value.clone());
                        }
                    }
                }
                if query.get("includePreferences").map(String::as_str) == Some("1") {
                    out.insert(
                        "Preferences".to_string(),
                        json!({"Setting": self.section_pref_rows(&key, if is_show { "show" } else { "movie" })}),
                    );
                }
                if query.get("includeSummary").map(String::as_str) == Some("1") {
                    out.insert("Summary".to_string(), self.section_summary(section));
                }
                Value::Object(out)
            })
            .collect::<Vec<_>>();
        if let Some(wanted) = query.get("type").filter(|v| !v.is_empty()) {
            // A type that PARSES selects nothing; one that does not is a 400.
            let parses = wanted.split(',').all(|part| {
                let digits = part.trim_matches('-');
                !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
            });
            if !parses {
                return Response::error(400, "plexsim: bad request");
            }
            rows.clear();
        }
        let title = |row: &Value| row.get("title").and_then(Value::as_str).unwrap_or("").to_string();
        match query.get("sort").filter(|v| !v.is_empty()) {
            Some(spec) => {
                let first = spec.split(',').next().unwrap_or("");
                let (key, modifiers) = first.split_once(':').unwrap_or((first, ""));
                if key != "titleSort" && key != "taggingCreatedAt" {
                    return Response::error(500, "plexsim: unsupported sort key");
                }
                let descending = modifiers.split(':').any(|m| m == "desc");
                if key == "titleSort" {
                    rows.sort_by_key(|row| title(row));
                } else {
                    rows.sort_by_key(|row| row.get("createdAt").and_then(Value::as_i64).unwrap_or(0));
                }
                if descending {
                    rows.reverse();
                }
            }
            None => rows.sort_by_key(|row| title(row)),
        }
        let mut body = Map::new();
        body.insert("size".to_string(), json!(rows.len()));
        body.insert("allowSync".to_string(), json!(false));
        body.insert("identifier".to_string(), json!("com.plexapp.plugins.library.shared"));
        body.insert("Metadata".to_string(), Value::Array(rows));
        if segments[2] == "all" && query.get("includeMeta").map(String::as_str) == Some("1") {
            let types = TYPES
                .iter()
                .map(|(key, title)| {
                    let mut sort_rows = sorts();
                    let active = *key == "/library/shared/all";
                    if active {
                        sort_rows[0]["active"] = json!(true);
                        sort_rows[0]["activeDirection"] = json!("asc");
                    }
                    json!({"key": key, "type": "mixed", "title": title, "active": active, "Sort": sort_rows})
                })
                .collect::<Vec<_>>();
            body.insert("Meta".to_string(), json!({"Type": types}));
        }
        Response::json(200, Value::Object(Map::from_iter([("MediaContainer".to_string(), Value::Object(body))])))
    }

    /// Rebuild both spellfix vocabularies from the current rows (`rebuild_spelling`).
    fn rebuild_spelling(&mut self) {
        let static_tags = load_json_sidecar(&self.catalog_path, "spellfix_static_tags.json")
            .and_then(|value| value.as_array().cloned())
            .map(|values| values.iter().filter_map(|v| v.as_str().map(str::to_string)).collect::<Vec<_>>())
            .unwrap_or_default();
        self.spelling = Some(spellfix::SearchIndex::build(&self.catalog.items, &static_tags));
    }

    pub fn route(&mut self, request: &Request) -> Response {
        if request.method == "PATCH" {
            // Cycle 31, on plexscratch42 (1.42.2, the build plexsim reports): PATCH is not
            // in 1.42.2's method table at all -- a 500 with the 109-byte document on every
            // path, `/identity` and `/system` included, answered before any routing, so it
            // registers no device and carries no framework cookie.
            return Response::error(500, "plexsim: unknown method");
        }
        self.mark_device(&request.headers);
        let mut response = self.request_route(request);
        exclude_fields_response(request, &mut response);
        sync_version_response(request, &mut response);
        self.stream_titles_response(request, &mut response);
        let (raw_path, _) = split_target(&request.target);
        if let Some(cookie) = Self::framework_cookie(&request.method, &raw_path) {
            // Cycle 33/52: the plug-in framework's session cookie rides every response it
            // writes itself -- but not the Tornado banner answer, which hands none back.
            let tornado = response.headers.iter().any(|(name, _)| name == "Server");
            if !tornado && !response.headers.iter().any(|(name, _)| name == "Set-Cookie") {
                response.headers.push(("Set-Cookie".to_string(), cookie));
            }
        }
        self.localize_response(request, &mut response);
        // The CORS trio is the envelope's (`wire.rs`), written for every response.
        self.save_if_dirty();
        response
    }

    fn localize_response(&self, request: &Request, response: &mut Response) {
        // The PMS localizer runs after route projection and before either JSON
        // or XML serialization.  Keep the same boundary here: malformed/custom
        // bodies (notably `/system/agents`' repeated Language members) stay
        // byte-for-byte route-owned, while ordinary JSON containers receive the
        // measured vocabulary overlay.
        if response.content_type != "application/json" || response.status != 200 {
            return;
        }
        let (path, raw_query) = split_target(&request.target);
        if path == "/system" || path == "/system/agents" {
            return;
        }
        let pairs = parse_query(&raw_query);
        // `/:/prefs` has complete version-specific regional catalogues, selected by
        // X-Plex-Language alone (cycle 50); the generic table must not run over them.
        let explicit = pairs
            .iter()
            .rev()
            .find(|(key, _)| key == "X-Plex-Language")
            .map(|(_, value)| value.clone())
            .or_else(|| request.headers.get("x-plex-language").cloned())
            .unwrap_or_default()
            .replace('_', "-")
            .to_lowercase();
        if path == "/:/prefs" && matches!(explicit.as_str(), "pt-pt" | "pt-br" | "hu-hu") {
            return;
        }
        let Some(strings) = load_json_sidecar(&self.catalog_path, "translations.json")
            .and_then(|value| value.get("strings").cloned())
        else {
            return;
        };
        let languages = i18n::Languages::from_strings_pub(&strings);
        // Cycle 16/45: X-Plex-Language > X-Plex-Accept-Language > Accept-Language.
        let Some(language) = i18n::request_language(&pairs, &request.headers, false, &languages) else {
            return;
        };
        if !languages.localised.contains(&language) {
            return;
        }
        let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
            return;
        };
        // Cycle 25: the plug-in framework's own container (its signature is
        // `noHistory`) is not localised.
        if document
            .get("MediaContainer")
            .is_some_and(|body| body.get("noHistory").is_some())
        {
            return;
        }
        let templates = load_json_sidecar(&self.catalog_path, "title_templates.json");
        let enum_hook = |text: &str| self.localized_language_enum(text, &language, &strings);
        let localiser = i18n::Localiser {
            strings: &strings,
            templates: templates.as_ref().and_then(|doc| doc.get("templates")),
            enum_leads: templates.as_ref().and_then(|doc| doc.get("enum_leads")),
            language: &language,
            language_enum: &enum_hook,
        };
        localiser.walk(&mut document, None, false);
        if let Ok(body) = serde_json::to_vec(&document) {
            response.body = body;
        }
    }

    /// Language pickers are a measured sidecar rather than ordinary enum
    /// translations.  The server keeps the source order for an item's own
    /// audio/subtitle setting, while the library language picker uses the
    /// locale-specific agent ordering.  `agent_languages.json` records both
    /// pieces, including the localized names that are absent from the sparse
    /// translation table.
    fn localized_language_enum(
        &self,
        source: &str,
        language: &str,
        strings: &Value,
    ) -> Option<String> {
        let language_list =
            source.contains("ca-ES:") || source.contains("en-US:") || source.contains("ar-SA:");
        if !language_list {
            return None;
        }
        let locale = language
            .split('-')
            .next()
            .unwrap_or(language)
            .to_ascii_lowercase();
        let rows = load_json_sidecar(&self.catalog_path, "agent_languages.json")?
            .get("languages")?
            .get(&locale)?
            .as_array()?
            .clone();
        let translated = rows
            .iter()
            .filter_map(|row| {
                let values = row.as_array()?;
                Some((
                    values.first()?.as_str()?.to_string(),
                    values.get(1)?.as_str()?.to_string(),
                ))
            })
            .collect::<HashMap<_, _>>();
        if translated.is_empty() {
            return None;
        }
        let source_rows = source
            .split('|')
            .filter_map(|entry| entry.split_once(':'))
            .filter(|(code, _)| !code.is_empty())
            .map(|(code, label)| (code.to_string(), label.to_string()))
            .collect::<Vec<_>>();
        let prefix = source.split('|').next()?.to_string();
        let has_bare_english = source_rows.iter().any(|(code, _)| code == "en");
        let mut entries = Vec::<(String, String)>::new();
        if has_bare_english {
            // Item audio/subtitle lists keep their measured source order.  The
            // bare `en` row is a separate account-language choice and is
            // followed by the regional rows in the source order.
            for (code, label) in source_rows {
                let localized = if code == "en" {
                    translated.get("en-US").cloned().unwrap_or(label)
                } else {
                    translated.get(&code).cloned().unwrap_or(label)
                };
                entries.push((code, localized));
            }
        } else {
            // Library metadata-language lists use the locale's own ordering.
            // Their source has a synthetic default entry before the first `|`.
            for (code, label) in rows.iter().filter_map(|row| {
                let values = row.as_array()?;
                Some((
                    values.first()?.as_str()?.to_string(),
                    values.get(1)?.as_str()?.to_string(),
                ))
            }) {
                entries.push((code, label));
            }
        }
        let translated_prefix = strings
            .get("enumValues")
            .and_then(|table| table.get(source))
            .and_then(|table| {
                table
                    .get(language)
                    .or_else(|| table.get(&locale))
                    .and_then(Value::as_str)
            })
            .and_then(|value| value.split('|').next())
            .unwrap_or(&prefix);
        let mut result = translated_prefix.to_string();
        for (code, label) in entries {
            result.push('|');
            result.push_str(&code);
            result.push(':');
            result.push_str(&label);
        }
        Some(result)
    }

    fn request_route(&mut self, request: &Request) -> Response {
        let (mut path, raw_query) = split_target(&request.target);
        if path.is_empty() {
            path = "/".to_string();
        }
        if raw_query
            .split('&')
            .any(|part| !part.is_empty() && !part.contains('='))
        {
            return Response::error(400, "plexsim: bad request");
        }
        // Empty path components are refused by the route table.  A small set
        // of top-level plugin prefixes is the measured exception: PMS sends
        // its normal empty plugin container for the doubled form.  Do this
        // before the ordinary trailing-slash normalization so `/identity//`
        // cannot become the valid `/identity` route.
        if path == "/:/plugins//" {
            // Cycle 50: the doubled slash answers the parent's own listing.
            path = "/:/plugins".to_string();
        }
        if path.contains("//") {
            if matches!(
                    path.as_str(),
                    "/status/sessions//"
                        | "/media/providers//"
                        | "/library//"
                        | "/hubs//"
                        | "/library/sections/1//"
                        | "/media/providers/metadata//"
                        | "/butler//"
                        | "/activities//"
                        | "/devices//"
                        | "/clients//"
                        | "/accounts//"
                        | "/security//"
                        | "/servers//"
                        | "/channels//"
                        | "/resources//"
                        | "/:/prefs//"
                )
                || (path.starts_with("/library/metadata/")
                    && path.ends_with("//")
                    && path != "/library/metadata//")
            {
                return Response::json(200, plugin_container(Vec::new()));
            }
            // Cycle 53: under `/system` an empty segment is still the FRAMEWORK's 404 --
            // empty body, no `Content-Type`.
            if path.starts_with("/system/") {
                return Response::bare(404);
            }
            return Response::error(404, "plexsim: not found");
        }
        let resource_root_with_slash = path == "/:/resources/";
        if path == "/:/plugins/" {
            return Response::error(404, "plexsim: not found");
        }
        if path.len() > 1 && path.ends_with('/') && !path.starts_with("/web/") {
            path.pop();
        }
        {
            // Cycle 45: the `+` form is a 404 in a VALIDATED VERSION position (a part's
            // changestamp before a filename, a section composite's version), checked on
            // the raw segment; cycle 36: every other padded or plus-signed id segment
            // (`\+?0*[0-9]+`) names the same row as its number.
            let pieces = path.split('/').map(str::to_string).collect::<Vec<_>>();
            let digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
            if pieces.len() > 5 && pieces[1] == "library" && pieces[2] == "parts" && !digits(&pieces[4]) {
                return Response::error(404, "plexsim: not found");
            }
            if pieces.len() > 5
                && pieces[1] == "library"
                && pieces[2] == "sections"
                && pieces[4] == "composite"
                && !digits(&pieces[5])
            {
                return Response::error(404, "plexsim: not found");
            }
            let padded = |text: &str| {
                let rest = text.strip_prefix('+').unwrap_or(text);
                digits(rest)
            };
            if pieces.iter().any(|piece| padded(piece) && piece.as_str() != piece.trim_start_matches(['+', '0']) ) {
                path = pieces
                    .iter()
                    .map(|piece| {
                        if padded(piece) {
                            let stripped = piece.trim_start_matches('+').trim_start_matches('0');
                            if stripped.is_empty() { "0".to_string() } else { stripped.to_string() }
                        } else {
                            piece.clone()
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("/");
            }
            // Cycle 36: four paths bound their id to a signed 32-bit integer -- past the
            // range a 400, a negative inside it a 404.
            let segments = path.split('/').filter(|p| !p.is_empty()).collect::<Vec<_>>();
            let bounded = match segments.as_slice() {
                ["playQueues" | "devices" | "accounts", id, ..] => Some(*id),
                ["system", "scanners", id, ..] => Some(*id),
                _ => None,
            };
            if let Some(id) = bounded {
                let body = id.strip_prefix('-').unwrap_or(id);
                if digits(body) {
                    match id.parse::<i64>() {
                        Ok(number) if (-2_147_483_648..=2_147_483_647).contains(&number) => {
                            if number < 0 {
                                return Response::error(404, "plexsim: not found");
                            }
                        }
                        _ => return Response::error(400, "plexsim: bad request"),
                    }
                }
            }
        }
        if request.method == "OPTIONS"
            && request.headers.contains_key("access-control-request-method")
        {
            // A genuine preflight is answered before routing and before the token
            // (`server.py` `do_OPTIONS`); a bare OPTIONS is routed like any verb.
            return Self::preflight(request);
        }
        let pairs = parse_query(&raw_query);
        let mut query = pairs.iter().fold(HashMap::new(), |mut map, (key, value)| {
            map.insert(key.clone(), value.clone());
            map
        });
        for name in ["X-Plex-Container-Start", "X-Plex-Container-Size"] {
            if !query.contains_key(name) {
                if let Some(value) = request.headers.get(&name.to_ascii_lowercase()) {
                    query.insert(name.to_string(), value.clone());
                }
            }
        }
        if !query.contains_key("X-Plex-Language") {
            if let Some(value) = request.headers.get("x-plex-language") {
                query.insert("X-Plex-Language".to_string(), value.clone());
            }
        }
        {
            // `server.py` `_dispatch`: the query grammar and the read-only verb gates
            // are refused before the token is even looked at.
            let refusal = gates::query_grammar_refusal(&request.method, &path, &query);
            if refusal != 0 {
                return Response::error(refusal, "plexsim: bad request");
            }
            if gates::read_only_refusal(&request.method, &path) {
                return Response::error(404, "plexsim: not found");
            }
        }
        if !self.authorized(&path, &request, &query) {
            return self.unauthorized_route(&path, request);
        }
        if path == "/player" || path.starts_with("/player/") {
            // Player commands go to a PLAYER, not to a server: a real PMS answers 400
            // for every one of them.
            if !request.headers.contains_key("x-plex-target-client-identifier") {
                return Response::error(400, "plexsim: bad request");
            }
            // Cycle 59, measured on zen, kids and both copies: WITH an
            // `X-Plex-Target-Client-Identifier` header -- python-plexapi's proxied
            // `PlexClient.sendCommand()` -- the server looks for that player to proxy
            // to and answers 404, for an unknown id, a listed device and an empty value
            // alike; only a bare command is the 400.
            return Response::error(404, "plexsim: no such player");
        }
        if matches!(request.method.as_str(), "GET" | "HEAD") {
            if let Some(status) = self.structural_refusal(&path, &query) {
                return Response::error(status, "plexsim: bad request");
            }
        }
        if resource_root_with_slash {
            return self.route_bundled_resource("", request.method == "HEAD");
        }
        // The plug-in framework's subtrees (`/system`, `/:/plugins`) are a different
        // server with their own envelope; see `framework.rs`.
        if let Some(response) = self.route_framework(request, &path, &query) {
            return response;
        }
        if (path == "/butler" && matches!(request.method.as_str(), "GET" | "HEAD" | "POST" | "DELETE"))
            || (path.starts_with("/butler/") && path.matches('/').count() == 2)
        {
            let segments = path.split('/').filter(|s| !s.is_empty()).collect::<Vec<_>>();
            return self.route_butler(&request.method, &segments);
        }
        // Single-component framework prefixes are dispatched by PMS before
        // their verb-specific handlers.  A wrong verb therefore receives the
        // plug-in container (with `/security`'s measured bare container),
        // rather than accidentally reading a GET document or falling through
        // to a 404.  Keep the actual writers such as `/playlists` out of this
        // set so their validation remains visible.
        let framework_prefix = matches!(
            path.as_str(),
            "/identity"
                | "/hubs"
                | "/library"
                | "/accounts"
                | "/devices"
                | "/servers"
                | "/butler"
                | "/clients"
                | "/activities"
                | "/connections"
                | "/dlna"
                | "/statistics"
                | "/media"
                | "/status"
                | "/services"
                | "/security"
        );
        // SINGLE_SEGMENT_WRITERS: the single-segment paths that really
        // implement a verb other than GET/HEAD; everything else falls through
        // to the dispatcher.
        let real_writer = path == "/butler" && matches!(request.method.as_str(), "POST" | "DELETE");
        if framework_prefix && !real_writer && !matches!(request.method.as_str(), "GET" | "HEAD") {
            return if path == "/security" {
                Response::json(200, json!({"MediaContainer": {"size": 0}}))
            } else if let Some(response) = self.route_plugin_prefix(&path) {
                response
            } else {
                Response::json(200, plugin_container(Vec::new()))
            };
        }
        if request.method == "OPTIONS" {
            {
                // Cycle 25: `/match` answers EVERY verb, a bare OPTIONS included,
                // with its own no-parameters 400.
                let parts = path.split('/').filter(|p| !p.is_empty()).collect::<Vec<_>>();
                if parts.len() == 4 && parts[..2] == ["library", "metadata"] && parts[3] == "match" {
                    return Response::error(400, "plexsim: bad request");
                }
                return match path.as_str() {
                    "/identity" => self.identity_response(&query),
                    "/hubs" | "/playlists" => Response::json(200, plugin_container(Vec::new())),
                    // Cycle 45: its own 400 to every verb.
                    "/:/viewChange" => Response::error(400, "plexsim: bad request"),
                    _ => Response::error(404, "plexsim: not found"),
                };
            }
            return Self::preflight(request);
        }
        // These are library read projections.  PMS rejects writes at the
        // route envelope before the projection handler runs; returning the
        // empty 404 preserves that distinction from a malformed projection.
        if !matches!(request.method.as_str(), "GET" | "HEAD")
            && (matches!(
                path.as_str(),
                "/library/recentlyAdded"
                    | "/library/onDeck"
                    | "/hubs/promoted"
                    | "/hubs/continueWatching"
                    | "/hubs/search"
                    | "/channels/all"
                    | "/services/browse"
                    | "/updater/status"
                    | "/:/plugins"
                    | "/playlists/all"
                    | "/status/sessions/history/all"
            ) || (path.starts_with("/library/metadata/")
                && matches!(
                    path.rsplit('/').next(),
                    Some("tree" | "allLeaves" | "related" | "similar" | "matches")
                )))
        {
            return Response::empty(404);
        }
        // The legacy framework exposes these one-segment names through its
        // plug-in dispatcher for every verb, including writes.
        if matches!(
            path.as_str(),
            "/prefs" | "/setting" | "/users" | "/devices.xml"
        ) {
            return Response::json(200, plugin_container(Vec::new()));
        }
        match (request.method.as_str(), path.as_str()) {
            (_, "/identity") => self.identity_response(&query),
            // Cycle 52: the three bundled static resources. No token, `Accept-Ranges`, never
            // gzipped; the two policies are `text/xml` and the favicon an octet-stream.
            ("GET" | "HEAD", "/crossdomain.xml") => static_sidecar_response(
                &self.catalog_path,
                "crossdomain.xml",
                "text/xml",
                request.method == "HEAD",
            ),
            ("GET" | "HEAD", "/clientaccesspolicy.xml") => static_sidecar_response(
                &self.catalog_path,
                "clientaccesspolicy.xml",
                "text/xml",
                request.method == "HEAD",
            ),
            ("GET" | "HEAD", "/favicon.ico") => static_sidecar_response(
                &self.catalog_path,
                "favicon.ico",
                "application/octet-stream",
                request.method == "HEAD",
            ),
            (_, "/crossdomain.xml" | "/clientaccesspolicy.xml" | "/favicon.ico") => {
                // A write verb is not the file: the dispatcher's empty listing.
                Response::json(
                    200,
                    json!({"MediaContainer": {"size": 0, "content": "plugins"}}),
                )
            }
            ("GET" | "HEAD", "/") => {
                let mut body = self.server_root();
                home_theater_version(&mut body, &query, &request.headers);
                Response::json(200, body)
            }
            ("GET" | "HEAD", "/manage") | ("GET" | "HEAD", "/web") => {
                let host = request.headers.get("host").cloned().unwrap_or_default();
                Response {
                    status: if request
                        .headers
                        .get("x-plex-neuter-redirect")
                        .is_some_and(|value| value == "1")
                    {
                        200
                    } else {
                        302
                    },
                    content_type: "text/html".to_string(),
                    body: Vec::new(),
                    headers: vec![
                        (
                            "Location".to_string(),
                            format!("http://{host}/web/index.html"),
                        ),
                        ("Cache-Control".to_string(), "public".to_string()),
                        ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                    ],
                    close: request
                        .headers
                        .get("x-plex-http-pipeline")
                        .map(|value| value == "disable")
                        .unwrap_or_else(|| {
                            query
                                .get("X-Plex-Http-Pipeline")
                                .is_some_and(|value| value == "disable")
                        }),
                }
            }
            ("POST", "/myplex/claim") => {
                // The claim token is a request parameter, independent of the
                // server's already-claimed state.  Python's do_claim rejects
                // an absent token before attempting any exchange, and the
                // native route must retain that observable error contract.
                if query.get("token").is_none_or(String::is_empty) {
                    Response::error(400, "plexsim: missing claim token")
                } else {
                    // Test instances use --assert-claimed as an injectable
                    // local account state; no native runtime path performs a
                    // plex.tv write.  A supplied token therefore marks the
                    // local state claimed and returns the same empty success
                    // shape used by the phase-3 smoke contract.
                    self.assert_claimed = true;
                    Response::empty(200)
                }
            }
            ("PATCH", "/myplex/claim") => Response::plain(500, b"500".to_vec()),
            ("PUT", "/myplex/refreshReachability") => Response::empty(200),
            ("GET" | "HEAD", "/myplex/account") => {
                // `server.py` `myplex_account`: the owner's EMAIL as `username`, the
                // address pair moving together, and (cycle 45) `subscriptionFeatures`
                // the SAME string as `/`'s `ownerFeatures`.
                let claimed = self.assert_claimed || self.token.is_some();
                let features = self
                    .server_root()
                    .pointer("/MediaContainer/ownerFeatures")
                    .cloned()
                    .unwrap_or(Value::Null);
                let private = !self.private_address.is_empty();
                Response::json(
                    200,
                    json!({"MyPlex":{
                        "username": if claimed { json!(self.owner_email) } else { Value::Null },
                        "mappingState":"mapped",
                        "mappingError":"",
                        "signInState": if claimed { "ok" } else { "none" },
                        "publicAddress": "",
                        "publicPort":0,
                        "privateAddress": self.private_address,
                        "privatePort": if private { self.private_port } else { 0 },
                        "subscriptionFeatures": features,
                        "subscriptionActive": claimed,
                        "subscriptionState": if claimed { "Active" } else { "Unknown" },
                    }}),
                )
            }
            ("GET" | "HEAD", "/security/token") => self.route_security_token(&request, &query),
            ("GET", "/log") => {
                let valid_level = query
                    .get("level")
                    .is_some_and(|value| !value.is_empty() && value.parse::<i64>().is_ok());
                if !valid_level || !query.contains_key("message") {
                    Response::error(400, "plexsim: bad request")
                } else {
                    self.push_event(
                        "log",
                        json!({"LogSentNotification": {
                            "level": query.get("level").cloned().unwrap_or_default(),
                            "thread": "plexsim",
                            "time": now(),
                            "message": query.get("message").cloned().unwrap_or_default(),
                        }}),
                    );
                    Response::empty(200)
                }
            }
            ("HEAD", "/log") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/log/networked") => Response::empty(200),
            ("GET" | "HEAD", "/diagnostics/logs") => {
                let mut response = Response::bytes(200, "application/zip");
                response.body = b"PK\x03\x04plexsim\n".to_vec();
                response
            }
            ("GET" | "HEAD", "/diagnostics") => Response::json(200, plugin_container(Vec::new())),
            ("GET" | "HEAD", "/resources") => Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            ),
            ("GET" | "HEAD", "/clients/profiles") => self.route_client_profiles(&query),
            ("POST" | "PUT", "/search") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/media/grabbers") => Response::json(
                200,
                self.container(
                    Map::from_iter([(
                        "MediaGrabber".to_string(),
                        json!([{"key":"1"},{"key":"2"},{"key":"3"},{"key":"4"}]),
                    )]),
                    false,
                ),
            ),
            ("GET" | "HEAD", "/media/providers") => {
                let mut body = self.media_providers(&query, &request.headers);
                home_theater_version(&mut body, &query, &request.headers);
                Response::json(200, body)
            }
            ("POST", "/media/providers") => Response::error(400, "plexsim: bad request"),
            ("POST", "/player/playback/play") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/player/timeline/poll") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/system/proxy") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/video/:/transcode/universal/subtitles") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/photo/:/transcode") => self.route_photo_transcode(request, &query),
            ("GET" | "HEAD", "/channels") => Response::json(
                200,
                self.container(
                    Map::from_iter([("Directory".to_string(), json!([
                        {"key":"all","title":"Channels","type":"folder"},
                        {"key":"recentlyViewed","title":"Recently Viewed","type":"folder"}
                    ]))]),
                    false,
                ),
            ),
            ("GET" | "HEAD", "/dlna") => {
                let mut rows = self
                    .catalog
                    .sections
                    .iter()
                    .map(|section| {
                        json!({
                            "key": format!("/library/sections/{}", string_value(section.get("key").unwrap_or(&Value::Null))),
                            "title": section.get("title").cloned().unwrap_or(Value::Null),
                        })
                    })
                    .collect::<Vec<_>>();
                rows.push(json!({"key":"/channels/all","title":"Channels"}));
                Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Directory".to_string(), Value::Array(rows))]),
                        false,
                    ),
                )
            }
            ("GET" | "HEAD", "/livetv") => Response::json(
                200,
                plugin_container(vec![("dvrs", false), ("epg", false), ("sessions", false)]),
            ),
            ("GET" | "HEAD", "/livetv/dvrs" | "/livetv/sessions") => {
                Response::json(200, self.container(Map::new(), false))
            }
            ("GET" | "HEAD", "/livetv/epg/channels") => {
                Response::error(400, "plexsim: bad request")
            }
            ("GET" | "HEAD", "/servers") => Response::json(
                200,
                self.container(
                    Map::from_iter([("Server".to_string(), Value::Array(self.gdm_server_rows()))]),
                    false,
                ),
            ),
            ("GET" | "HEAD", "/logs") => Response::json(
                200,
                self.container(Map::from_iter([("content".to_string(), json!("plexsim native log\n"))]), false),
            ),
            ("POST", "/log") => Response::empty(200),
            // Measured on both references: a real (empty, paged) container with
            // no `content` at all -- the one place the dispatcher does not leak.
            ("GET" | "HEAD", "/security") => Response::json(200, json!({"MediaContainer": {"size": 0}})),
            ("GET" | "HEAD", "/library/all/top") => self.route_top(&query),
            ("GET" | "HEAD", "/library/metadata") => Response::error(400, "plexsim: bad request"),
            ("GET" | "HEAD", "/library") => Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("size".to_string(), json!(3)),
                        ("title1".to_string(), json!("Plex Library")),
                        (
                            "Directory".to_string(),
                            json!([
                                {"key":"sections","title":"Library Sections"},
                                {"key":"recentlyAdded","title":"Recently Added Content"},
                                {"key":"onDeck","title":"On Deck Content"}
                            ]),
                        ),
                    ]),
                    false,
                ),
            ),
            ("GET" | "HEAD", "/library/sections") => self.route_sections(&query),
            ("GET" | "HEAD", "/library/sections/all") => self.route_sections(&query),
            ("GET" | "HEAD", "/library/all") => {
                let pairs = Self::request_pairs(request);
                self.library_all_pairs(&query, &pairs)
            }
            ("GET" | "HEAD", "/library/recentlyAdded") => self.route_recently_added(&query),
            ("GET" | "HEAD", "/library/onDeck") => self.route_on_deck(&query),
            ("GET" | "HEAD", "/devices") => self.route_devices(),
            ("GET" | "HEAD", "/accounts") => self.route_account(None),
            ("GET" | "HEAD", "/playlists") => self.route_playlists(&query),
            ("GET" | "HEAD", "/library/collections") => Response::error(404, "plexsim: not found"),
            ("GET" | "HEAD", "/statistics/bandwidth") => self
                .statistics_refusal("bandwidth", &query)
                .unwrap_or_else(|| self.route_bandwidth(&query)),
            ("GET" | "HEAD", "/statistics/media") => self
                .statistics_refusal("media", &query)
                .unwrap_or_else(|| self.route_media_statistics(&query)),
            ("GET" | "HEAD", "/statistics/resources") => self
                .statistics_refusal("resources", &query)
                .unwrap_or_else(|| self.route_resource_statistics(&query)),
            ("GET" | "HEAD", "/history") => self.route_history(&query),
            ("GET" | "HEAD", "/status/sessions") => self.route_sessions(),
            ("GET" | "HEAD", "/status/sessions/background") => Response::json(200, self.container(Map::new(), false)),
            ("GET" | "HEAD", "/transcode/sessions") => Response::json(200, self.container(Map::new(), false)),
            ("GET" | "HEAD", "/clients") => Response::json(200, self.container(Map::new(), false)),
            ("GET" | "HEAD", "/updater/status") => Response::json(200, self.container(Map::from_iter([("canInstall".to_string(), json!(false)), ("checkedAt".to_string(), json!(now()))]), false)),
            ("PUT", "/updater/check") => {
                // Cycle 19: one `update.statechange` frame; with no update channel the
                // state is `error`, as the isolated copy answers.
                self.push_event(
                    "update.statechange",
                    json!({"AutoUpdateNotification": [{"key": "", "version": "", "state": "error"}]}),
                );
                Response::empty(200)
            }
            ("GET" | "HEAD", "/status/sessions/history/all") => self.route_history(&query),
            ("GET", "/:/prefs") => self.route_prefs(&query, &request.headers, false),
            ("HEAD", "/:/prefs") => self.route_prefs(&query, &request.headers, true),
            ("GET" | "HEAD", "/:/prefs/get") => self.route_pref_get(&query),
            ("PUT", "/:/prefs") => self.route_prefs_write(&query),
            ("GET" | "HEAD", "/:/resources/movie.png") => {
                self.route_bundled_resource("movie.png", request.method == "HEAD")
            }
            ("GET" | "HEAD", "/:/resources/video.png") => {
                self.route_bundled_resource("video.png", request.method == "HEAD")
            }
            ("GET" | "HEAD", "/:/resources/actor-icon.png") => {
                self.route_bundled_resource("actor-icon.png", request.method == "HEAD")
            }
            ("GET" | "HEAD", "/:/eventsource/notifications") => Response::sse(),
            ("GET" | "HEAD", "/connections") => {
                Response::plain(200, connection_lines(&self.owner_username).into_bytes())
            }
            ("GET" | "HEAD", "/server/identity") => self.identity_response(&query),
            // The play-state writers answer on every verb the dispatcher routes, as
            // the Python oracle does: the suite's own `PUT /:/scrobble` wrote
            // nothing here and every Dashboard check after it read no history.
            ("GET" | "HEAD" | "PUT" | "POST" | "DELETE", "/:/progress") => {
                self.route_progress(&query)
            }
            ("GET" | "HEAD" | "PUT" | "POST" | "DELETE", "/:/timeline") => {
                self.route_timeline(&query, &request.headers)
            }
            ("GET" | "HEAD" | "PUT" | "POST" | "DELETE", "/:/scrobble") => {
                self.route_scrobble(&query, true)
            }
            ("GET" | "HEAD" | "PUT" | "POST" | "DELETE", "/:/unscrobble") => {
                self.route_scrobble(&query, false)
            }
            ("GET" | "HEAD" | "PUT" | "POST" | "DELETE", "/:/rate") => self.route_rate(&query),
            ("GET" | "HEAD", "/video/:/transcode/universal/decision") => {
                self.route_decision(&query, &request.headers)
            }
            ("GET" | "HEAD", "/subtitles/:/transcode/universal/start") => {
                Response::error(400, "plexsim: bad request")
            }
            ("GET" | "HEAD", "/hubs") => self.route_hubs(&query),
            ("GET" | "HEAD", "/hubs/search") => {
                // Cycle 54/55: a `sectionId` that parses as an int32 and names no
                // section is a 400; anything else is ignored.
                let wanted = query.get("sectionId").cloned().unwrap_or_default();
                if int_param(&wanted, Some(32))
                    && !wanted.starts_with('-')
                    && self
                        .section(&wanted.trim_start_matches('+').parse::<i64>().map(|n| n.to_string()).unwrap_or_default())
                        .is_none()
                {
                    Response::error(400, "plexsim: bad request")
                } else {
                    self.hubs_search(&query)
                }
            }
            ("GET" | "HEAD", "/search") => self.route_legacy_search(&query),
            ("GET" | "HEAD", "/library/search") => self.flat_search(&query),
            _ => self.route_dynamic(request, &path, &query, &pairs),
        }
    }

    fn authorized(&self, path: &str, request: &Request, query: &HashMap<String, String>) -> bool {
        let Some(expected) = self.token.as_deref() else {
            return true;
        };
        if path == "/identity"
            || path == "/web"
            || path.starts_with("/web/")
            || path == "/manage"
            || path == "/security/token"
            || (matches!(
                path,
                "/crossdomain.xml" | "/clientaccesspolicy.xml" | "/favicon.ico"
            ) && matches!(request.method.as_str(), "GET" | "HEAD"))
        {
            return true;
        }
        let header = request.headers.get("x-plex-token");
        let provided = header
            .filter(|value| !value.is_empty())
            .or_else(|| query.get("X-Plex-Token"));
        provided
            .map(|value| value.as_str() == expected)
            .unwrap_or(false)
    }

    fn unauthorized_route(&self, path: &str, request: &Request) -> Response {
        // PMS classifies the route before applying its token gate.  This is
        // observable for probes against unknown keys and for its legacy
        // single-segment framework paths: those refusals have different
        // status documents, while existing mutable routes remain 401.  Keep
        // the classification here rather than dispatching an unauthenticated
        // request through stateful handlers.
        let html = |status: u16, body: &[u8]| Response {
            status,
            content_type: "text/html".to_string(),
            body: body.to_vec(),
            headers: vec![("Cache-Control".to_string(), "no-cache".to_string())],
            close: false,
        };
        const UNAUTHORIZED: &[u8] =
            b"<html><head><title>Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>";
        const FORBIDDEN: &[u8] =
            b"<html><head><title>Forbidden</title></head><body><h1>403 Forbidden</h1></body></html>";
        const ROOT: &[u8] =
            b"<html><head><script>window.location = window.location.href.match(/(^.+\\/)[^\\/]*$/)[1] + 'web/index.html';</script><title>Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>";
        if path == "/" {
            return html(401, ROOT);
        }
        let segments = path
            .split('/')
            .filter(|segment| !segment.is_empty())
            .collect::<Vec<_>>();
        if segments.len() == 1 {
            return match segments[0] {
                "search" => html(400, b"<html><head><title>Bad Request</title></head><body><h1>400 Bad Request</h1></body></html>"),
                "downloadQueue" => html(404, b"<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>"),
                "player" => html(400, b"<html><head><title>Bad Request</title></head><body><h1>400 Bad Request</h1></body></html>"),
                "library" | "hubs" | "playlists" | "devices" | "butler" | "servers"
                | "clients" | "accounts" | "activities" | "playQueues" | "log"
                | "connections" | "dlna" => html(401, UNAUTHORIZED),
                _ => html(403, FORBIDDEN),
            };
        }
        // GET/HEAD probes of unknown library keys and sections receive a 404
        // before auth; writes and existing routes retain the ordinary 401.
        if matches!(request.method.as_str(), "GET" | "HEAD") {
            if segments.first() == Some(&"library")
                && segments.get(1) == Some(&"metadata")
                && segments.len() >= 3
            {
                let key = segments.get(2).copied().unwrap_or_default();
                if !key.is_empty()
                    && (key.parse::<i64>().is_ok() && self.catalog.get(key).is_none())
                {
                    return html(404, b"<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>");
                }
            }
            if segments.first() == Some(&"library")
                && segments.get(1) == Some(&"sections")
                && segments.len() >= 3
            {
                let key = segments.get(2).copied().unwrap_or_default();
                if !key.is_empty() && self.section(key).is_none() {
                    return html(404, b"<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>");
                }
            }
            if path == "/no/such/path" {
                return html(404, b"<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>");
            }
        }
        html(401, UNAUTHORIZED)
    }

    fn route_sections(&self, query: &HashMap<String, String>) -> Response {
        let rows = self
            .catalog
            .sections
            .iter()
            .map(|section| self.section_directory(section))
            .collect::<Vec<_>>();
        let mut fields = Map::new();
        fields.insert("title1".to_string(), json!("Plex Library"));
        fields.insert("Directory".to_string(), Value::Array(rows));
        if query
            .get("includePreferences")
            .is_some_and(|value| is_true_option(value))
        {
            let rows = fields.get_mut("Directory").and_then(Value::as_array_mut);
            if let Some(rows) = rows {
                for (section, row) in self.catalog.sections.iter().zip(rows) {
                    if let Some(object) = row.as_object_mut() {
                        object.insert(
                            "Preferences".to_string(),
                            json!({"Setting": self.section_preference_rows(
                                &string_value(section.get("key").unwrap_or(&Value::Null)),
                                section.get("type").and_then(Value::as_str).unwrap_or("movie"),
                            )}),
                        );
                    }
                }
            }
        }
        if query
            .get("includeSummary")
            .is_some_and(|value| is_true_option(value))
        {
            if let Some(rows) = fields.get_mut("Directory").and_then(Value::as_array_mut) {
                for (section, row) in self.catalog.sections.iter().zip(rows) {
                    if let Some(object) = row.as_object_mut() {
                        object.insert("Summary".to_string(), self.section_summary(section));
                    }
                }
            }
        }
        Response::json(200, self.container(fields, true))
    }

    fn section_summary(&self, section: &Value) -> Value {
        let section_key = string_value(section.get("key").unwrap_or(&Value::Null));
        let mut counts: HashMap<String, i64> = HashMap::new();
        let mut duration = 0i64;
        for row in &self.catalog.items {
            if string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) != section_key {
                continue;
            }
            if let Some(kind) = row.get("type").and_then(Value::as_str) {
                *counts.entry(kind.to_string()).or_default() += 1;
            }
            duration =
                duration.saturating_add(row.get("duration").and_then(Value::as_i64).unwrap_or(0));
        }
        let mut types = counts.into_iter().collect::<Vec<_>>();
        types.sort_by_key(|(kind, _)| match kind.as_str() {
            "movie" => 1,
            "show" => 2,
            "season" => 3,
            "episode" => 4,
            _ => 999,
        });
        json!({
            "duration": duration,
            "Type": types.into_iter().map(|(kind, count)| json!({"type":kind,"count":count})).collect::<Vec<_>>(),
        })
    }

    fn route_client_profiles(&self, query: &HashMap<String, String>) -> Response {
        // These are the measured profile resource names, in the order in the
        // checked-in sidecar.  The endpoint is a catalogue of playback
        // profiles, rather than the currently connected /clients list.
        let selected = match query.get("type") {
            None => None,
            Some(value) if value.chars().all(|ch| ch.is_ascii_digit()) => {
                let number = value.parse::<i64>().unwrap_or(-1);
                if number == 0 || number == 42 {
                    Some(number)
                } else {
                    return Response::error(400, "plexsim: bad request");
                }
            }
            Some(_) => return Response::error(400, "plexsim: bad request"),
        };
        let all_names = load_json_sidecar(&self.catalog_path, "client_profiles.json")
            .and_then(|value| value.as_object().cloned())
            .map(|object| object.into_iter().map(|(name, _)| name).collect::<Vec<_>>())
            .unwrap_or_default();
        let local_names = [
            "Android",
            "iOS",
            "Universal Mobile",
            "Universal TV",
            "Windows Phone",
            "Windows",
            "Xbox One",
        ];
        let names = if selected == Some(42) {
            local_names
                .iter()
                .map(|name| (*name).to_string())
                .collect::<Vec<_>>()
        } else {
            all_names
        };
        let rows = names
            .into_iter()
            .map(|name| {
                if selected == Some(42) {
                    json!({"title":name,"selected":1})
                } else {
                    json!({"title":name})
                }
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Profile".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn route_section_detail(&self, section: &Value, query: &HashMap<String, String>) -> Response {
        // The section root is the sidebar descriptor, rather than the section
        // directory row returned by /library/sections.  These descriptors are
        // measured sidecars and carry the exact field/operator vocabulary the
        // client uses for its pivots and Edit dialog.
        let key = string_value(section.get("key").unwrap_or(&Value::Null));
        let kind = section
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let pivot_rows = if kind == "show" {
            vec![
                json!({"key":"all","title":"All Shows"}),
                json!({"key":"unwatched","title":"Unwatched"}),
                json!({"key":"newest","title":"Recently Released"}),
                json!({"key":"recentlyAdded","title":"Recently Added"}),
                json!({"key":"recentlyViewed","title":"Recently Viewed Episodes"}),
                json!({"key":"recentlyViewedShows","title":"Recently Viewed Shows"}),
                json!({"key":"onDeck","title":"Continue Watching"}),
                json!({"secondary":true,"key":"collection","title":"By Collection"}),
                json!({"secondary":true,"key":"edition","title":"By Edition"}),
                json!({"secondary":true,"key":"firstCharacter","title":"By First Letter"}),
                json!({"secondary":true,"key":"genre","title":"By Genre"}),
                json!({"secondary":true,"key":"year","title":"By Year"}),
                json!({"secondary":true,"key":"contentRating","title":"By Content Rating"}),
                json!({"key":"folder","title":"By Folder"}),
                json!({"prompt":"Search for Shows","search":true,"key":"search?type=2","title":"Search Shows..."}),
                json!({"prompt":"Search for Episodes","search":true,"key":"search?type=4","title":"Search Episodes..."}),
            ]
        } else {
            vec![
                json!({"key":"all","title":format!("All {}", section.get("title").and_then(Value::as_str).unwrap_or("Movies"))}),
                json!({"key":"unwatched","title":"Unwatched"}),
                json!({"key":"newest","title":"Recently Released"}),
                json!({"key":"recentlyAdded","title":"Recently Added"}),
                json!({"key":"recentlyViewed","title":"Recently Viewed"}),
                json!({"key":"onDeck","title":"Continue Watching"}),
                json!({"secondary":true,"key":"collection","title":"By Collection"}),
                json!({"secondary":true,"key":"edition","title":"By Edition"}),
                json!({"secondary":true,"key":"genre","title":"By Genre"}),
                json!({"secondary":true,"key":"year","title":"By Year"}),
                json!({"secondary":true,"key":"decade","title":"By Decade"}),
                json!({"secondary":true,"key":"director","title":"By Director"}),
                json!({"secondary":true,"key":"actor","title":"By Starring Actor"}),
                json!({"secondary":true,"key":"country","title":"By Country"}),
                json!({"secondary":true,"key":"contentRating","title":"By Content Rating"}),
                json!({"secondary":true,"key":"rating","title":"By Rating"}),
                json!({"secondary":true,"key":"resolution","title":"By Resolution"}),
                json!({"secondary":true,"key":"firstCharacter","title":"By First Letter"}),
                json!({"key":"folder","title":"By Folder"}),
                json!({"prompt":"Search Movies","search":true,"key":"search?type=1","title":"Search..."}),
            ]
        };
        let mut fields = Map::new();
        fields.insert(
            "librarySectionID".to_string(),
            key.parse::<i64>().map(|id| json!(id)).unwrap_or_else(|_| json!(key)),
        );
        let mut size = pivot_rows.len();
        fields.insert("Directory".to_string(), Value::Array(pivot_rows));
        let details = query.get("includeDetails").is_some_and(|v| is_true_option(v));
        if details {
            // Cycle 19: this form honours `includeAdvanced=1` like every other --
            // 9 Sorts bare on a movie library and 15 with the flag.  Cycle 16:
            // under includeDetails a Sort carries no active/activeDirection and a
            // Filter no `advanced` -- except (cycle 49) the Duplicates row, the one
            // that keeps it.  And NO `folder` Type row, none marked active.
            let advanced = query.get("includeAdvanced").is_some_and(|value| value == "1");
            if let Some(mut meta) = self.section_meta(&key, kind, advanced) {
                for described in meta.get_mut("Type").and_then(Value::as_array_mut).into_iter().flatten() {
                    for sort in described.get_mut("Sort").and_then(Value::as_array_mut).into_iter().flatten() {
                        if let Some(object) = sort.as_object_mut() {
                            object.remove("active");
                            object.remove("activeDirection");
                        }
                    }
                    for filter in described.get_mut("Filter").and_then(Value::as_array_mut).into_iter().flatten() {
                        if filter.get("filter").and_then(Value::as_str) != Some("duplicate") {
                            if let Some(object) = filter.as_object_mut() {
                                object.remove("advanced");
                            }
                        }
                    }
                }
                if meta.get("Type").and_then(Value::as_array).is_some_and(|types| !types.is_empty()) {
                    meta_active(&mut meta, None, None);
                    let types = meta
                        .get("Type")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                        .into_iter()
                        .filter(|entry| entry.get("type").and_then(Value::as_str) != Some("folder"))
                        .collect::<Vec<_>>();
                    size += types.len();
                    fields.insert("Type".to_string(), Value::Array(types));
                }
                if let Some(field_types) = meta.get("FieldType").and_then(Value::as_array) {
                    size += field_types.len();
                    fields.insert("FieldType".to_string(), Value::Array(field_types.clone()));
                }
            }
        }
        // Measured: `size` counts the Directory rows AND the Type and FieldType
        // blocks, and (cycle 27) those rows are ONE paged sequence.
        let mut body = self.secondary_body(section, None, fields);
        body.insert("size".to_string(), json!(size));
        if details {
            body.insert("_pageflat".to_string(), json!(["Directory", "Type", "FieldType"]));
        }
        Response::json(200, json!({"MediaContainer": body}))
    }

    /// A secondary list (By Genre, By Year, sorts, filters), measured:
    /// `content=secondary`, `viewGroup=secondary`, `nocache` on a SHOW library's
    /// every secondary container, and title2 naming the axis.
    fn secondary_body(&self, section: &Value, title2: Option<&str>, fields: Map<String, Value>) -> Map<String, Value> {
        let show = section.get("type").and_then(Value::as_str) == Some("show");
        let mut body = Map::new();
        body.insert("allowSync".to_string(), json!(false));
        body.insert(
            "art".to_string(),
            json!(if show { "/:/resources/show-fanart.jpg" } else { "/:/resources/movie-fanart.jpg" }),
        );
        body.insert("content".to_string(), json!("secondary"));
        body.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
        body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        body.insert(
            "thumb".to_string(),
            json!(if show { "/:/resources/show.png" } else { "/:/resources/movie.png" }),
        );
        body.insert("title1".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
        body.insert("viewGroup".to_string(), json!("secondary"));
        body.insert("viewMode".to_string(), json!(65592));
        if show {
            body.insert("nocache".to_string(), json!(true));
        }
        if let Some(title2) = title2 {
            body.insert("title2".to_string(), json!(title2));
        }
        for (key, value) in fields {
            body.insert(key, value);
        }
        for key in ["Directory", "Metadata"] {
            if let Some(rows) = body.get(key).and_then(Value::as_array) {
                let count = rows.len();
                body.insert("size".to_string(), json!(count));
            }
        }
        if !body.contains_key("size") {
            body.insert("size".to_string(), json!(0));
        }
        body.retain(|_, value| !value.is_null());
        body
    }

    fn create_section(&mut self, query: &HashMap<String, String>) -> Response {
        let key = self
            .catalog
            .sections
            .iter()
            .filter_map(|section| section.get("key").map(string_value))
            .filter_map(|value| value.parse::<i64>().ok())
            .max()
            .unwrap_or(0)
            + 1;
        let kind = query.get("type").map(String::as_str).unwrap_or("movie");
        let section = json!({
            "key": key.to_string(),
            "type": if kind == "2" || kind == "show" { "show" } else { "movie" },
            "title": query.get("name").cloned().unwrap_or_else(|| "Library".to_string()),
            "uuid": format!("native-section-{key}"),
            "language": query.get("language").cloned().unwrap_or_else(|| "en-US".to_string()),
        });
        self.catalog.sections.push(section.clone());
        self.added_sections.push(section.clone());
        self.catalog.document.insert(
            "sections".to_string(),
            Value::Array(self.catalog.sections.clone()),
        );
        // Cycle 35, measured on both copies frame for frame: a create SCANS, and the
        // scan's skeleton survives a location holding no files at all; cycle 15: it
        // ends in ONE `backgroundProcessingQueue` `queueRegenerated` frame.
        self.notify_section_scan(&key.to_string(), &section);
        self.notify_queue_regenerated();
        for (name, value) in query {
            if let Some(pref) = name
                .strip_prefix("prefs[")
                .and_then(|value| value.strip_suffix(']'))
            {
                self.section_prefs
                    .insert((key.to_string(), pref.to_string()), value.clone());
            }
        }
        self.dirty = true;
        Response::json(
            201,
            self.container(
                Map::from_iter([(
                    "Directory".to_string(),
                    vec![self.section_directory(&section)].into_iter().collect(),
                )]),
                false,
            ),
        )
    }

    fn route_all(&self, query: &HashMap<String, String>) -> Response {
        self.library_all(query)
    }

    /// The Dashboard's view-time keys as `(key, moment)` tests over a history row's
    /// `viewedAt` (the oracle's `_history_time_filter` with `date=True`).  Cycle 54,
    /// measured on kids and zen with the client's own encoding: `viewedAt>` is `>=`,
    /// `viewedAt<` is `<=` and a bare `viewedAt` is `==`; on Top Played and
    /// `users/top` a value parses like a filter date and a bad one is a 500.
    fn history_date_tests(query: &HashMap<String, String>) -> Result<Vec<(&'static str, f64)>, u16> {
        let mut tests = Vec::new();
        for key in ["viewedAt>", "viewedAt<", "viewedAt"] {
            if let Some(value) = query.get(key) {
                let moment = crate::filters::as_epoch(value, None).map_err(|_| 500_u16)?;
                tests.push((key, moment));
            }
        }
        Ok(tests)
    }

    fn history_passes(tests: &[(&'static str, f64)], viewed: i64) -> bool {
        let viewed = viewed as f64;
        tests.iter().all(|(key, moment)| match *key {
            "viewedAt>" => viewed >= *moment,
            "viewedAt<" => viewed <= *moment,
            _ => viewed == *moment,
        })
    }

    fn route_top(&self, query: &HashMap<String, String>) -> Response {
        // Cycle 27, swept over 17 values on kids and zen: any type NUMBER is
        // answered, and only a value that does not PARSE is a 400.  A type this
        // server holds no rows of is an empty 200.
        let raw = query.get("type").map(String::as_str).unwrap_or("");
        let digits = raw.trim_start_matches(['+', '-']);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Response::error(400, "plexsim: bad request");
        }
        let wanted = match raw.trim_start_matches('+').parse::<i64>().unwrap_or(0) {
            1 => Some("movie"),
            2 => Some("show"),
            3 => Some("season"),
            4 => Some("episode"),
            _ => None,
        };
        // Cycle 54, measured on kids and zen: `accountID`, `librarySectionID` and the
        // viewedAt keys filter the HISTORY rows before the plays are counted.  A bad
        // value in any of them is a 500.
        let tests = match Self::history_date_tests(query) {
            Ok(tests) => tests,
            Err(status) => return Response::error(status, "plexsim: internal error"),
        };
        let mut ids: HashMap<&str, f64> = HashMap::new();
        for name in ["accountID", "librarySectionID"] {
            if let Some(value) = query.get(name) {
                // Cycle 55: the id is read as a NUMBER -- `+1` is section 1 and `1.5`
                // a 200 that matches nothing -- and only a non-number is the 500.
                let body = value.strip_prefix(['+', '-']).unwrap_or(value);
                let (whole, fraction) = body.split_once('.').unwrap_or((body, ""));
                let numeric = !body.is_empty()
                    && body != "."
                    && whole.bytes().all(|b| b.is_ascii_digit())
                    && fraction.bytes().all(|b| b.is_ascii_digit())
                    && body.matches('.').count() <= 1;
                if !numeric {
                    return Response::error(500, "plexsim: internal error");
                }
                ids.insert(name, value.trim_start_matches('+').parse::<f64>().unwrap_or(0.0));
            }
        }
        let mut plays: HashMap<i64, i64> = HashMap::new();
        let mut viewers: HashMap<i64, BTreeSet<i64>> = HashMap::new();
        let mut seen_at: HashMap<i64, i64> = HashMap::new();
        if let Some(wanted) = wanted {
            for entry in &self.history {
                let key = string_value(entry.get("ratingKey").unwrap_or(&Value::Null));
                if key.is_empty() || !key.bytes().all(|b| b.is_ascii_digit()) {
                    continue;
                }
                let Some(row) = self.catalog.get(&key) else { continue };
                let mut counted = key.clone();
                if row.get("type").and_then(Value::as_str) != Some(wanted) {
                    // Cycle 58, measured on zen: a play is counted on the episode's
                    // SEASON at `type=3` and its SHOW at `type=2`.
                    let parent = match wanted {
                        "show" => row.get("grandparentRatingKey").map(string_value),
                        "season" => row
                            .get("parentRatingKey")
                            .or_else(|| row.get("_parentRatingKey"))
                            .map(string_value),
                        _ => None,
                    }
                    .unwrap_or_default();
                    if row.get("type").and_then(Value::as_str) != Some("episode")
                        || parent.is_empty()
                        || !parent.bytes().all(|b| b.is_ascii_digit())
                        || self.catalog.get(&parent).is_none()
                    {
                        continue;
                    }
                    counted = parent;
                }
                let viewed = entry.get("viewedAt").and_then(Value::as_i64).unwrap_or(0);
                if !Self::history_passes(&tests, viewed) {
                    continue;
                }
                let account = entry.get("accountID").and_then(Value::as_i64).unwrap_or(1);
                if ids.get("accountID").is_some_and(|wanted| account as f64 != *wanted) {
                    continue;
                }
                let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                    .parse::<f64>()
                    .unwrap_or(0.0);
                if ids.get("librarySectionID").is_some_and(|wanted| section != *wanted) {
                    continue;
                }
                let number = counted.parse::<i64>().unwrap_or(0);
                *plays.entry(number).or_default() += 1;
                viewers.entry(number).or_default().insert(account);
                let latest = seen_at.entry(number).or_default();
                *latest = (*latest).max(viewed);
            }
        }
        // Cycle 27, measured on zen's 73 real history rows: a tie in the play count
        // breaks on the MOST RECENT view, descending, and only then on the ratingKey.
        let mut ordered = plays.keys().copied().collect::<Vec<_>>();
        ordered.sort_by_key(|key| (-plays[key], -seen_at.get(key).copied().unwrap_or(0), *key));
        let limit = query
            .get("limit")
            .filter(|value| !value.is_empty())
            .and_then(|value| value.trim_start_matches('+').parse::<i64>().ok())
            .map(|value| if value == 0 { 12 } else { value })
            .unwrap_or(12);
        let take = if limit < 0 {
            ordered.len().saturating_sub(limit.unsigned_abs() as usize)
        } else {
            limit as usize
        };
        let rows = ordered
            .into_iter()
            .take(take)
            .filter_map(|key| {
                let row = self.catalog.get(&key.to_string())?;
                // The oracle's `_listing_row` is the FULL detail projection.
                let mut shown = self.detail_row(row);
                if let Some(object) = shown.as_object_mut() {
                    object.insert("globalViewCount".to_string(), json!(plays[&key]));
                    object.insert("userCount".to_string(), json!(viewers[&key].len()));
                    object.insert(
                        "User".to_string(),
                        Value::Array(viewers[&key].iter().map(|id| json!({"id": id})).collect()),
                    );
                }
                Some(shown)
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                true,
            ),
        )
    }

    fn route_recently_added(&self, query: &HashMap<String, String>) -> Response {
        // The server-wide list is a mixed-parent projection.  A newly added
        // episode is represented once by its season, dated by the earliest
        // episode in that season; adding the season row itself must not create
        // a second entry beside it.
        let mut dated = Vec::<(i64, Value)>::new();
        for row in &self.catalog.items {
            if row.get("type").and_then(Value::as_str) == Some("movie") {
                dated.push((
                    row.get("addedAt").and_then(Value::as_i64).unwrap_or(0),
                    row.clone(),
                ));
            }
        }
        let mut seasons: HashMap<String, (i64, Value)> = HashMap::new();
        for episode in self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
        {
            let Some(parent_key) = episode.get("parentRatingKey").map(string_value) else {
                continue;
            };
            let Some(season) = self.catalog.get(&parent_key) else {
                continue;
            };
            let stamp = episode.get("addedAt").and_then(Value::as_i64).unwrap_or(0);
            seasons
                .entry(parent_key)
                .and_modify(|entry| {
                    if stamp < entry.0 {
                        entry.0 = stamp;
                    }
                })
                .or_insert((stamp, season.clone()));
        }
        dated.extend(seasons.into_values());
        // Cycle 58: a library kept off Home (Visibility 1 or 2) drops out of this list.
        dated.retain(|(_, row)| {
            self.section_visibility(&string_value(row.get("librarySectionID").unwrap_or(&Value::Null))) < 1
        });
        dated.sort_by(|left, right| {
            right.0.cmp(&left.0).then_with(|| {
                let left_title = left
                    .1
                    .get("titleSort")
                    .or_else(|| left.1.get("title"))
                    .map(string_value)
                    .unwrap_or_default();
                let right_title = right
                    .1
                    .get("titleSort")
                    .or_else(|| right.1.get("title"))
                    .map(string_value)
                    .unwrap_or_default();
                left_title.to_lowercase().cmp(&right_title.to_lowercase())
            })
        });
        // Cycle 54, measured on kids and zen: `unwatched=` is a 0/1 OPTION here (the
        // `+?0*[01]` grammar, anything else a 400), and at 1 it drops the watched LEAF
        // rows before the cap.
        if let Some(value) = query.get("unwatched") {
            if !bool_option(value) {
                return Response::error(400, "plexsim: bad request");
            }
            if value.trim_start_matches('+').trim_start_matches('0') == "1" {
                dated.retain(|(_, row)| !watched_leaf(row));
            }
        }
        if let Some(limit) = query
            .get("limit")
            .and_then(|value| value.parse::<i64>().ok())
        {
            if limit >= 0 {
                dated.truncate(limit as usize);
            }
        } else {
            dated.truncate(50);
        }
        let mut rows = Vec::with_capacity(dated.len());
        for (added_at, row) in dated {
            // Cycle 46: the grid row keeping its Guid for the `includeGuids` gate.
            let mut projected = guid_gate(self.grid_row(&row, true, true, false), query);
            if row.get("type").and_then(Value::as_str) == Some("season") {
                if let Some(parent) = row
                    .get("parentRatingKey")
                    .map(string_value)
                    .and_then(|key| self.catalog.get(&key))
                {
                    if parent
                        .get("summary")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.is_empty())
                    {
                        if let Some(object) = projected.as_object_mut() {
                            object.insert(
                                "parentSummary".to_string(),
                                parent.get("summary").cloned().unwrap_or(Value::Null),
                            );
                        }
                    }
                }
            }
            if let Some(object) = projected.as_object_mut() {
                object.remove("librarySectionKey");
                object.insert(
                    "librarySectionUUID".to_string(),
                    self.section(&string_value(
                        row.get("librarySectionID").unwrap_or(&Value::Null),
                    ))
                    .and_then(|section| section.get("uuid"))
                    .cloned()
                    .unwrap_or(Value::Null),
                );
                object.insert("allowSync".to_string(), json!(false));
                object.insert("addedAt".to_string(), json!(added_at));
            }
            rows.push(projected);
        }
        let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
        page_fields(&mut fields, query);
        fields.insert("mixedParents".to_string(), json!(true));
        Response::json(200, self.container(fields, true))
    }

    fn on_deck_rows(&self) -> Vec<Value> {
        let project = |row: &Value| {
            let mut projected = self.detail_row(row);
            if let Some(object) = projected.as_object_mut() {
                object.remove("Field");
                object.remove("librarySectionKey");
                if let Some(section) = self.section(&string_value(
                    row.get("librarySectionID").unwrap_or(&Value::Null),
                )) {
                    if let Some(uuid) = section.get("uuid") {
                        object.insert("librarySectionUUID".to_string(), uuid.clone());
                    }
                }
            }
            projected
        };
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("movie"))
            .filter(|row| {
                key_of(row)
                    .map(|key| !self.hidden_continue.contains(&key))
                    .unwrap_or(false)
                    && row
                        .get("viewOffset")
                        .and_then(Value::as_i64)
                        .is_some_and(|offset| offset > 0)
            })
            .map(project)
            .collect::<Vec<_>>();
        for show in self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("show"))
        {
            let show_key = key_of(show).unwrap_or_default();
            let mut leaves = self.leaves(&show_key);
            leaves.sort_by_key(|row| {
                (
                    row.get("parentIndex").and_then(Value::as_i64).unwrap_or(0),
                    row.get("index").and_then(Value::as_i64).unwrap_or(0),
                )
            });
            if let Some(candidate) = leaves.into_iter().find(|row| {
                key_of(row)
                    .map(|key| !self.hidden_continue.contains(&key))
                    .unwrap_or(false)
                    && row
                        .get("viewOffset")
                        .and_then(Value::as_i64)
                        .is_some_and(|offset| offset > 0)
            }) {
                rows.push(project(&candidate));
                continue;
            }
            if let Some(candidate) = self.on_deck_for(&self.leaves(&show_key)) {
                if candidate.get("viewCount").is_none() {
                    rows.push(project(&candidate));
                }
            }
        }
        /*
         * The global shelf has one resume row per show.  Choosing the lowest
         * episode above also handles the measured next-up fallback; keeping
         * the candidate pass separate would re-add every in-progress leaf.
         */
        rows
    }

    fn route_on_deck(&self, query: &HashMap<String, String>) -> Response {
        // Cycle 55: `type=abc` and an empty `type=` are 400s on zen and kids.
        if query.get("type").is_some_and(|value| !int_param(value, Some(32))) {
            return Response::error(400, "plexsim: bad request");
        }
        // Cycle 28 (zen) and cycle 30 (plexscratch42): the FULL detail row minus
        // `Field` and `librarySectionKey`, led by `allowSync` and the section UUID.
        let rows = self
            .on_home(self.continue_watching(false))
            .iter()
            .map(|row| {
                let mut entry = self.detail_row(row);
                if let Some(object) = entry.as_object_mut() {
                    object.remove("Field");
                    object.remove("librarySectionKey");
                    object.insert("allowSync".to_string(), json!(true));
                    let uuid = self
                        .section(&string_value(row.get("librarySectionID").unwrap_or(&Value::Null)))
                        .and_then(|section| section.get("uuid").cloned())
                        .unwrap_or(Value::Null);
                    object.insert("librarySectionUUID".to_string(), uuid);
                }
                entry
            })
            .collect::<Vec<_>>();
        let (page, paged, start, total) = match page_window(rows, query) {
            Ok(window) => window,
            Err(status) => return Response::error(status, "plexsim: bad container parameter"),
        };
        let mut fields = Map::from_iter([
            ("Metadata".to_string(), Value::Array(page)),
            ("mixedParents".to_string(), json!(true)),
        ]);
        if paged {
            fields.insert("offset".to_string(), json!(start));
            fields.insert("totalSize".to_string(), json!(total));
        }
        Response::json(200, self.container(fields, true))
    }

    fn route_devices(&self) -> Response {
        if self
            .devices
            .values()
            .any(|device| !device.platform.is_string())
        {
            return Response::error(500, "plexsim: internal error");
        }
        let mut rows = self
            .devices
            .values()
            .map(|device| {
                json!({
                    "clientIdentifier": device.client_identifier,
                    "name": device.name,
                    "product": device.product,
                    "platform": device.platform,
                    "lastSeenAt": device.last_seen,
                    "owned": true,
                })
            })
            .collect::<Vec<_>>();
        if let Some(index) = rows
            .iter()
            .position(|row| row.get("clientIdentifier").and_then(Value::as_str) == Some(""))
        {
            rows.swap(0, index);
        }
        for (index, row) in rows.iter_mut().enumerate() {
            if let Some(object) = row.as_object_mut() {
                object.insert("id".to_string(), json!(index + 1));
            }
        }
        rows.push(json!({"id": rows.len() + 1, "clientIdentifier": self.machine, "name": self.friendly, "product": "Plex Media Server", "platform": "Linux", "owned": true}));
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    (
                        "identifier".to_string(),
                        json!("com.plexapp.system.devices"),
                    ),
                    ("Device".to_string(), Value::Array(rows)),
                ]),
                false,
            ),
        )
    }

    fn route_device(&self, id: &str) -> Response {
        let Ok(number) = id.parse::<i64>() else {
            return Response::error(404, "plexsim: not found");
        };
        if number <= 0 || number > i32::MAX as i64 {
            return if number > i32::MAX as i64 {
                Response::error(400, "plexsim: bad request")
            } else {
                Response::error(404, "plexsim: not found")
            };
        }
        if self
            .devices
            .values()
            .any(|device| !device.platform.is_string())
        {
            // The direct read remains available while list serializers hit
            // the same measured numeric-platform cast failure.
        }
        let mut rows = self
            .devices
            .values()
            .map(|device| {
                json!({
                    "clientIdentifier": device.client_identifier,
                    "name": device.name,
                    "product": device.product,
                    "platform": device.platform,
                    "lastSeenAt": device.last_seen,
                    "owned": true,
                })
            })
            .collect::<Vec<_>>();
        if let Some(index) = rows
            .iter()
            .position(|row| row.get("clientIdentifier").and_then(Value::as_str) == Some(""))
        {
            rows.swap(0, index);
        }
        rows.push(json!({"clientIdentifier": self.machine, "name": self.friendly, "product": "Plex Media Server", "platform": "Linux", "owned": true}));
        let index = number as usize - 1;
        let Some(mut row) = rows.get(index).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        if let Some(object) = row.as_object_mut() {
            object.insert("id".to_string(), json!(number));
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([("Device".to_string(), json!([row]))]),
                false,
            ),
        )
    }

    fn route_history(&self, query: &HashMap<String, String>) -> Response {
        let mut rows = self.history.clone();
        // Cycle 14, measured case by case on kids (1.43.4) and zen (1.42.2): this page
        // VALIDATES its ids.  `accountID`, `librarySectionID` and `metadataItemID` are
        // 400 for anything that is not a whole number -- `abc`, empty and `1.5` alike --
        // while `1`, `-1`, `01` and (cycle 55) `+1` are fine.
        for name in ["accountID", "librarySectionID", "metadataItemID"] {
            if let Some(value) = query.get(name) {
                if !int_param(value, None) {
                    return Response::error(400, "plexsim: bad request");
                }
            }
        }
        // Cycle 54, measured on kids and zen: `offset` is an int32 here.
        if let Some(value) = query.get("offset") {
            if !int_param(value, Some(32)) {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let number = |value: &str| value.trim_start_matches('+').parse::<i128>().unwrap_or(0);
        if let Some(wanted) = query.get("metadataItemID") {
            // `metadataItemID` is additionally 404 for a whole number the server holds
            // no row for; cycle 57: a COLLECTION's or a PLAYLIST's id is an empty 200.
            let wanted_number = number(wanted);
            let key = wanted_number.to_string();
            if !rows.iter().any(|row| {
                string_value(row.get("ratingKey").unwrap_or(&Value::Null)) == *wanted
            }) && self.catalog.get(&key).is_none()
                && !self.playlists.contains_key(&key)
                && !self.collections.contains_key(&key)
            {
                return Response::error(404, "plexsim: not found");
            }
        }
        // Cycle 54 corrected the keys: `viewedAt%3E=` is the key `viewedAt>` and it is
        // INCLUSIVE, `viewedAt<` likewise, and a bare `viewedAt` is exact.  On this page
        // a value must be an integer and anything else is a 400; `viewedAt>=` and
        // `viewedAt<=` are unknown keys, ignored and not validated.
        let mut time_tests: Vec<(&str, i64)> = Vec::new();
        for key in ["viewedAt>", "viewedAt<", "viewedAt"] {
            if let Some(value) = query.get(key) {
                let digits = value.strip_prefix('-').unwrap_or(value);
                if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                    return Response::error(400, "plexsim: bad request");
                }
                time_tests.push((key, value.parse::<i64>().unwrap_or(0)));
            }
        }
        // Cycle 15/70: the `type` is the row's `viewType` -- a row with none (a
        // timeline completion) is type 0, and an absent `type=` means `type=0`.
        let mut wanted_types = vec![0i64];
        if let Some(kind) = query.get("type") {
            let mut parsed = Vec::new();
            for part in kind.split(',') {
                let digits = part.trim_start_matches('-');
                if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                    return Response::error(400, "plexsim: bad request");
                }
                parsed.push(part.parse::<i64>().unwrap_or(0));
            }
            wanted_types = parsed;
        }
        rows.retain(|row| {
            wanted_types.contains(&row.get("viewType").and_then(Value::as_i64).unwrap_or(0))
        });
        let row_number = |row: &Value, field: &str| -> Option<i128> {
            let text = string_value(row.get(field).unwrap_or(&Value::Null));
            let text = if text.is_empty() { "0".to_string() } else { text };
            let digits = text.trim_start_matches('-');
            (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
                .then(|| text.parse::<i128>().unwrap_or(0))
        };
        for (name, field) in [
            ("accountID", "accountID"),
            ("deviceID", "deviceID"),
            ("librarySectionID", "librarySectionID"),
            ("metadataItemID", "ratingKey"),
        ] {
            let Some(wanted) = query.get(name).filter(|value| !value.is_empty()) else {
                continue;
            };
            if name == "deviceID" {
                rows.retain(|row| string_value(row.get(field).unwrap_or(&Value::Null)) == *wanted);
            } else {
                let wanted = number(wanted);
                rows.retain(|row| row_number(row, field) == Some(wanted));
            }
        }
        rows.retain(|row| {
            let viewed = row.get("viewedAt").and_then(Value::as_i64).unwrap_or(0);
            time_tests.iter().all(|(key, moment)| match *key {
                "viewedAt>" => viewed >= *moment,
                "viewedAt<" => viewed <= *moment,
                _ => viewed == *moment,
            })
        });
        // Default order is viewedAt ASCENDING, and an unknown sort key is a 400 --
        // not the 500 a library listing answers.
        let spec = query
            .get("sort")
            .filter(|value| !value.is_empty())
            .cloned()
            .unwrap_or_else(|| "viewedAt".to_string());
        let terms = spec
            .split(',')
            .filter(|term| !term.is_empty())
            .map(|term| {
                let (field, direction) = term.split_once(':').unwrap_or((term, ""));
                (field.to_string(), direction == "desc")
            })
            .collect::<Vec<_>>();
        for (field, descending) in terms.iter().rev() {
            if !matches!(
                field.as_str(),
                "viewedAt" | "historyKey" | "ratingKey" | "accountID" | "deviceID" | "title"
            ) {
                return Response::error(400, "plexsim: bad request");
            }
            let sort_key = |row: &Value| -> (bool, Value) {
                match row.get(field.as_str()) {
                    None | Some(Value::Null) => (true, json!(0)),
                    Some(value) => (false, value.clone()),
                }
            };
            rows.sort_by(|a, b| {
                let (an, av) = sort_key(a);
                let (bn, bv) = sort_key(b);
                let order = an.cmp(&bn).then_with(|| compare_json(&av, &bv));
                if *descending { order.reverse() } else { order }
            });
        }
        // Cycle 18, measured on kids and zen: `limit=` is a THIRD grammar here --
        // `limit=-1` answers ZERO rows -- and `group=` is refused outright with a 400,
        // the empty form included.
        if query.contains_key("group") {
            return Response::error(400, "plexsim: bad request");
        }
        if let Some(raw) = query.get("limit") {
            let digits = raw.trim_start_matches('-');
            if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) {
                let limit = raw.parse::<i64>().unwrap_or(0).max(0) as usize;
                rows.truncate(limit);
            }
        }
        let (page, paged, start, total) = match page_window(rows, query) {
            Ok(window) => window,
            Err(status) => return Response::error(status, "plexsim: bad container parameter"),
        };
        let mut fields = Map::new();
        fields.insert("size".to_string(), json!(page.len()));
        fields.insert("Metadata".to_string(), Value::Array(page));
        if paged {
            fields.insert("totalSize".to_string(), json!(total));
            fields.insert("offset".to_string(), json!(start));
        }
        Response::json(200, self.container(fields, false))
    }

    fn route_bandwidth(&self, query: &HashMap<String, String>) -> Response {
        if self
            .devices
            .values()
            .any(|device| !device.platform.is_string())
        {
            return Response::error(500, "plexsim: internal error");
        }
        if let Some(value) = query.get("timespan") {
            if value.parse::<i64>().is_err() {
                return Response::error(400, "plexsim: bad request");
            }
        }

        if let Some(value) = query.get("lan") {
            if !matches!(value.as_str(), "0" | "1") {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let spans = if let Some(value) = query.get("timespan") {
            let timespan = value.parse::<i64>().unwrap_or_default();
            if !matches!(timespan, 0 | 1 | 2 | 3 | 4 | 6) {
                Vec::new()
            } else {
                vec![timespan]
            }
        } else {
            vec![0, 1, 2, 3, 4]
        };
        let account = query.get("accountID").map(String::as_str);
        let device = query.get("deviceID").map(String::as_str);
        let include_account = account.is_none_or(|value| value == "1");
        let include_device = device.is_none_or(|value| value == "1");
        let mut rows = if !include_account || !include_device {
            Vec::new()
        } else {
            spans
                .into_iter()
                .map(|timespan| {
                    json!({
                        "at": statistics_bucket(now(), timespan),
                        "bytes": self.bandwidth_bytes.max(1),
                        "lan": self.bandwidth_bytes.max(1),
                        "wan": 0,
                        "timespan": timespan,
                    })
                })
                .collect::<Vec<_>>()
        };
        if let Some(operator) = query.get("at>") {
            if let Ok(pivot) = operator.parse::<i64>() {
                rows.retain(|row| row.get("at").and_then(Value::as_i64).unwrap_or(0) >= pivot);
            }
        }
        if let Some(operator) = query.get("at<") {
            if let Ok(pivot) = operator.parse::<i64>() {
                rows.retain(|row| row.get("at").and_then(Value::as_i64).unwrap_or(0) <= pivot);
            }
        }
        if let Some(operator) = query.get("at=") {
            if let Ok(pivot) = operator.parse::<i64>() {
                rows.retain(|row| row.get("at").and_then(Value::as_i64) == Some(pivot));
            }
        }
        if let Some(lan) = query.get("lan") {
            let want = lan == "1";
            rows.retain(|row| (row.get("lan").and_then(Value::as_i64).unwrap_or(0) > 0) == want);
        }
        if let Some(limit) = query
            .get("limit")
            .and_then(|value| value.parse::<i64>().ok())
        {
            if limit <= 0 {
                rows.clear();
            } else {
                rows.truncate(limit as usize);
            }
        }
        if let Some(offset) = query
            .get("offset")
            .and_then(|value| value.parse::<i32>().ok())
        {
            if offset > 0 {
                rows = rows.into_iter().skip(offset as usize).collect();
            }
        }
        let size = rows.len();
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("size".to_string(), json!(size)),
                    ("StatisticsBandwidth".to_string(), Value::Array(rows)),
                    (
                        "Device".to_string(),
                        Value::Array(
                            self.devices
                                .values()
                                .map(|device| json!({"id":device.client_identifier,"title":device.name}))
                                .collect(),
                        ),
                    ),
                    ("Account".to_string(), json!([{"id":1,"title":self.owner_username}])),
                ]),
                false,
            ),
        )
    }

    /// The statistics endpoints' shared parameter grammar (the oracle's `statistics`),
    /// checked before any endpoint answers.
    fn statistics_refusal(&self, which: &str, query: &HashMap<String, String>) -> Option<Response> {
        let refuse = || Some(Response::error(400, "plexsim: bad request"));
        // Cycle 54/55, measured on kids and zen: `/statistics/media` parses `count`,
        // `offset` and `type` as int32 and `/statistics/bandwidth` `bytes` as int64 and
        // `offset` as int32; `offset` is an int32 on all THREE endpoints.
        let ints: &[(&str, u32)] = match which {
            "media" => &[("count", 32), ("offset", 32), ("type", 32)],
            "bandwidth" => &[("bytes", 64), ("offset", 32)],
            _ => &[("offset", 32)],
        };
        for (name, bits) in ints {
            if query.get(*name).is_some_and(|value| !int_param(value, Some(*bits))) {
                return refuse();
            }
        }
        // The `sort=` (and cycle 55: `group=`) keys each endpoint accepts.
        let keys: &[&str] = match which {
            "bandwidth" => &["at", "bytes", "deviceID", "accountID", "lan", "timespan"],
            "media" => &["at", "deviceID", "accountID", "timespan", "count", "duration"],
            _ => &[
                "at",
                "timespan",
                "hostCpuUtilization",
                "processCpuUtilization",
                "hostMemoryUtilization",
                "processMemoryUtilization",
            ],
        };
        if query.get("group").is_some_and(|group| !keys.contains(&group.as_str())) {
            return refuse();
        }
        if which == "bandwidth" && self.devices.values().any(|device| !device.platform.is_string()) {
            return Some(Response::error(500, "plexsim: internal error"));
        }
        // Cycle 55: the window is an int32; cycle 26/55: `limit=` is PARSED as one.
        for name in ["timespan", "limit"] {
            if query.get(name).is_some_and(|value| !int_param(value, Some(32))) {
                return refuse();
            }
        }
        if let Some(sort) = query.get("sort") {
            let (key, direction) = sort.split_once(':').unwrap_or((sort, ""));
            if !keys.contains(&key) || !matches!(direction, "" | "asc" | "desc") {
                return refuse();
            }
        }
        None
    }

    fn route_media_statistics(&self, query: &HashMap<String, String>) -> Response {
        let spans = if let Some(value) = query.get("timespan") {
            let timespan = value.parse::<i64>().unwrap_or_default();
            if matches!(timespan, 0 | 1 | 2 | 3 | 4) {
                vec![timespan]
            } else {
                Vec::new()
            }
        } else {
            vec![0, 1, 2, 3, 4]
        };
        let account = query.get("accountID").map(String::as_str);
        let device = query.get("deviceID").map(String::as_str);
        let include_account = account.is_none_or(|value| value == "1");
        let include_device = device.is_none_or(|value| value == "1");
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("viewCount").is_some())
            .flat_map(|row| {
                let viewed_at = row
                    .get("lastViewedAt")
                    .and_then(Value::as_i64)
                    .unwrap_or_else(now);
                spans.iter().map(move |timespan| {
                    json!({
                        "accountID": 1,
                        "deviceID": 1,
                        "ratingKey": key_of(row).unwrap_or_default(),
                        "at": statistics_bucket(viewed_at, *timespan),
                        "viewCount": row.get("viewCount").cloned().unwrap_or(json!(0)),
                        "duration": row.get("duration").cloned().unwrap_or(json!(0)),
                        "lastViewedAt": row.get("lastViewedAt").cloned().unwrap_or(json!(now())),
                        "timespan": timespan,
                    })
                })
            })
            .collect::<Vec<_>>();
        if !include_account || !include_device {
            rows.clear();
        }
        if let Some(limit) = query
            .get("limit")
            .and_then(|value| value.parse::<i32>().ok())
        {
            if limit <= 0 {
                rows.clear();
            } else {
                rows.truncate(limit as usize);
            }
        }
        if let Some(offset) = query
            .get("offset")
            .and_then(|value| value.parse::<i32>().ok())
        {
            if offset > 0 {
                rows = rows.into_iter().skip(offset as usize).collect();
            }
        }
        let size = rows.len();
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("size".to_string(), json!(size)),
                    ("StatisticsMedia".to_string(), Value::Array(rows)),
                ]),
                false,
            ),
        )
    }

    fn route_resource_statistics(&self, query: &HashMap<String, String>) -> Response {
        let Some(value) = query.get("timespan") else {
            return Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            );
        };
        let Ok(timespan) = value.parse::<i64>() else {
            return Response::error(400, "plexsim: bad request");
        };
        if timespan != 6 {
            return Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            );
        }
        for name in ["limit", "offset"] {
            if let Some(value) = query.get(name) {
                if value.parse::<i32>().is_err() {
                    return Response::error(400, "plexsim: bad request");
                }
            }
        }
        if let Some(limit) = query.get("limit") {
            if limit.parse::<i32>().ok().is_some_and(|value| value <= 0) {
                return Response::json(
                    200,
                    self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
                );
            }
        }
        if let Some(sort) = query.get("sort") {
            if !matches!(
                sort.as_str(),
                "at" | "hostCpuUtilization" | "processCpuUtilization"
            ) {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let current = now();
        Response::json(
            200,
            self.container(
                Map::from_iter([(
                    "StatisticsResources".to_string(),
                    json!([{
                        "timespan": 6,
                        "at": current,
                        "hostCpuUtilization": 0,
                        "processCpuUtilization": 0,
                        "hostMemoryUtilization": 0,
                        "processMemoryUtilization": 0,
                    }]),
                )]),
                false,
            ),
        )
    }

    fn route_sessions(&self) -> Response {
        let rows = self.sessions.values().cloned().collect::<Vec<_>>();
        if rows.is_empty() {
            return Response::json(200, self.container(Map::new(), true));
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                true,
            ),
        )
    }

    fn session_profile(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> Option<String> {
        for (canonical, lower) in [
            ("X-Plex-Client-Profile-Name", "x-plex-client-profile-name"),
            ("X-Plex-Platform", "x-plex-platform"),
            ("X-Plex-Device", "x-plex-device"),
        ] {
            let Some(name) = headers.get(lower).or_else(|| query.get(canonical)) else {
                continue;
            };
            if name == "Plex Home Theater-RaspberryPi" {
                return Some("Raspberry PI".to_string());
            }
            let Some(profile) = self.decision_profiles.get(name) else {
                continue;
            };
            return Some(
                profile
                    .get("redirect")
                    .and_then(Value::as_str)
                    .unwrap_or(name)
                    .to_string(),
            );
        }
        None
    }

    fn session_media(&self, row: &Value, decided: bool) -> Value {
        let Some(media_rows) = row.get("Media").and_then(Value::as_array) else {
            return Value::Array(Vec::new());
        };
        let media_rows = media_rows
            .iter()
            .map(|media| {
                let mut media = media.clone();
                let Some(media_object) = media.as_object_mut() else {
                    return media;
                };
                media_object.insert("_session".to_string(), json!(true));
                if let Some(id) = media_object.get("id").cloned() {
                    media_object.insert("id".to_string(), json!(string_value(&id)));
                }
                if let Some(aspect) = media_object.get("aspectRatio").cloned() {
                    media_object.insert("aspectRatio".to_string(), json!(string_value(&aspect)));
                }
                if decided {
                    media_object.insert("selected".to_string(), json!(true));
                }
                let parts = media_object
                    .get("Part")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .map(|part| {
                        let mut part = part;
                        let Some(part_object) = part.as_object_mut() else {
                            return part;
                        };
                        part_object.insert("_session".to_string(), json!(true));
                        if let Some(id) = part_object.get("id").cloned() {
                            part_object.insert("id".to_string(), json!(string_value(&id)));
                        }
                        if decided {
                            part_object.insert("selected".to_string(), json!(true));
                            part_object.insert("decision".to_string(), json!("directplay"));
                        }
                        let streams = part_object
                            .get("Stream")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default()
                            .into_iter()
                            .map(|stream| {
                                let mut stream = stream;
                                if let Some(stream_object) = stream.as_object_mut() {
                                    stream_object.insert("_session".to_string(), json!(true));
                                    if let Some(id) = stream_object.get("id").cloned() {
                                        stream_object
                                            .insert("id".to_string(), json!(string_value(&id)));
                                    }
                                    if decided {
                                        stream_object
                                            .insert("location".to_string(), json!("direct"));
                                    }
                                    if stream_object.get("streamType").and_then(Value::as_i64)
                                        == Some(1)
                                    {
                                        for field in ["displayTitle", "extendedDisplayTitle"] {
                                            if let Some(title) =
                                                stream_object.get(field).and_then(Value::as_str)
                                            {
                                                let plain = title
                                                    .split_once(" (")
                                                    .map(|(head, _)| head)
                                                    .unwrap_or(title)
                                                    .to_string();
                                                stream_object
                                                    .insert(field.to_string(), json!(plain));
                                            }
                                        }
                                    }
                                }
                                stream
                            })
                            .collect::<Vec<_>>();
                        if !streams.is_empty() {
                            part_object.insert("Stream".to_string(), Value::Array(streams));
                        }
                        part
                    })
                    .collect::<Vec<_>>();
                media_object.insert("Part".to_string(), Value::Array(parts));
                media
            })
            .collect::<Vec<_>>();
        Value::Array(media_rows)
    }

    fn session_row(
        &self,
        row: &Value,
        key: &str,
        client: &str,
        session_identifier: &str,
        state: &str,
        time: i64,
        duration: i64,
        headers: &HashMap<String, String>,
        query: &HashMap<String, String>,
        decided: bool,
    ) -> Value {
        let mut session = self.detail_row(row);
        let projected_media = self.session_media(row, decided);
        let profile = self.session_profile(query, headers);
        let Some(object) = session.as_object_mut() else {
            return session;
        };
        object.remove("Guid");
        object.remove("Image");
        object.remove("Media");
        object.remove("_light");
        object.insert("_session".to_string(), json!(true));
        object.insert("sessionKey".to_string(), json!(client));
        object.insert("ratingKey".to_string(), json!(key));
        object.insert("key".to_string(), json!(format!("/library/metadata/{key}")));
        object.insert("state".to_string(), json!(state));
        object.insert("viewOffset".to_string(), json!(time));
        object.insert("duration".to_string(), json!(duration));
        if let Some(section) = object.get("librarySectionID").cloned() {
            object.insert(
                "librarySectionID".to_string(),
                json!(string_value(&section)),
            );
        }
        object.insert("Media".to_string(), projected_media);
        for name in [
            "Genre",
            "Collection",
            "Director",
            "Writer",
            "Producer",
            "Role",
            "Country",
            "Label",
            "Similar",
            "Style",
            "Mood",
        ] {
            let Some(tags) = row.get(name).and_then(Value::as_array) else {
                continue;
            };
            let projected = tags
                .iter()
                .filter_map(Value::as_object)
                .map(|tag| {
                    let mut values = Map::new();
                    let tag_text = tag.get("tag").and_then(Value::as_str).unwrap_or_default();
                    let id = tag
                        .get("id")
                        .map(string_value)
                        .filter(|id| !id.is_empty())
                        .unwrap_or_else(|| tag_id("Genre", tag_text).to_string());
                    let mut keys = tag.keys().cloned().collect::<Vec<_>>();
                    if !keys.iter().any(|key| key == "id") {
                        keys.push("id".to_string());
                    }
                    keys.sort();
                    keys.dedup();
                    for key in keys {
                        if key == "id" {
                            values.insert(key, Value::String(id.clone()));
                        } else if let Some(value) = tag.get(&key) {
                            values.insert(key, value.clone());
                        }
                    }
                    Value::Object(values)
                })
                .collect::<Vec<_>>();
            if !projected.is_empty() {
                object.insert(name.to_string(), Value::Array(projected));
            }
        }
        if let Some(colors) = object.get("UltraBlurColors").cloned() {
            if !colors.is_array() {
                object.insert("UltraBlurColors".to_string(), json!([colors]));
            }
        }
        let mut player = Map::new();
        let header_or_query = |header: &str, parameter: &str| {
            query
                .get(parameter)
                .cloned()
                .or_else(|| headers.get(header).cloned())
                .unwrap_or_default()
        };
        for (name, value) in [
            ("address", "127.0.0.1".to_string()),
            ("device", header_or_query("x-plex-device", "X-Plex-Device")),
            (
                "machineIdentifier",
                header_or_query("x-plex-client-identifier", "X-Plex-Client-Identifier"),
            ),
            ("model", header_or_query("x-plex-model", "X-Plex-Model")),
            (
                "platform",
                header_or_query("x-plex-platform", "X-Plex-Platform"),
            ),
            (
                "platformVersion",
                header_or_query("x-plex-platform-version", "X-Plex-Platform-Version"),
            ),
            (
                "product",
                header_or_query("x-plex-product", "X-Plex-Product"),
            ),
            (
                "title",
                header_or_query("x-plex-device-name", "X-Plex-Device-Name"),
            ),
            (
                "version",
                header_or_query("x-plex-version", "X-Plex-Version"),
            ),
        ] {
            if !value.is_empty() {
                player.insert(name.to_string(), json!(value));
            }
        }
        if let Some(profile) = profile {
            player.insert("profile".to_string(), json!(profile));
        }
        player.insert("local".to_string(), json!(true));
        player.insert("relayed".to_string(), json!(false));
        player.insert("secure".to_string(), json!(false));
        player.insert("state".to_string(), json!(state));
        player.insert("userID".to_string(), json!(1));
        object.insert("Player".to_string(), Value::Object(player));
        object.insert(
            "User".to_string(),
            json!({"id":1,"title":self.owner_username}),
        );
        if decided {
            object.insert("Session".to_string(), json!({"id":session_identifier}));
        }
        session
    }

    fn route_prefs(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
        head: bool,
    ) -> Response {
        let all_rows = self.pref_rows(query, headers);
        let total = all_rows.len();
        let (start, rows) = if let (Some(start), Some(size)) = (
            query
                .get("X-Plex-Container-Start")
                .and_then(|value| value.parse::<usize>().ok()),
            query
                .get("X-Plex-Container-Size")
                .and_then(|value| value.parse::<usize>().ok()),
        ) {
            (
                Some(start),
                all_rows
                    .iter()
                    .skip(start)
                    .take(size)
                    .cloned()
                    .collect::<Vec<_>>(),
            )
        } else {
            (None, all_rows)
        };
        let mut fields = Map::from_iter([
            ("size".to_string(), json!(rows.len())),
            ("Setting".to_string(), Value::Array(rows)),
        ]);
        if let Some(start) = start {
            fields.insert("offset".to_string(), json!(start));
            fields.insert("totalSize".to_string(), json!(total));
        }
        let body = Value::Object(fields);
        let _ = head;
        Response::json(
            200,
            Value::Object(Map::from_iter([("MediaContainer".to_string(), body)])),
        )
    }

    fn route_pref_get(&self, query: &HashMap<String, String>) -> Response {
        let Some(id) = query.get("id") else {
            return Response::error(400, "plexsim: bad request");
        };
        let row = self
            .pref_rows(query, &HashMap::new())
            .into_iter()
            .find(|row| row.get("id").and_then(Value::as_str) == Some(id));
        match row {
            Some(mut row) => {
                if let Some(object) = row.as_object_mut() {
                    object.insert("label".to_string(), json!(""));
                    object.insert("summary".to_string(), json!(""));
                    object.remove("enumValues");
                }
                Response::json(
                    200,
                    self.container(
                        Map::from_iter([
                            ("size".to_string(), json!(1)),
                            ("Setting".to_string(), Value::Array(vec![row])),
                        ]),
                        false,
                    ),
                )
            }
            None => Response::error(404, "plexsim: not found"),
        }
    }

    fn route_security_token(&self, request: &Request, query: &HashMap<String, String>) -> Response {
        if query.get("type").map(String::as_str) != Some("delegation")
            || query.get("scope").map(String::as_str) != Some("all")
        {
            return Response::error(400, "plexsim: bad request");
        }
        if let Some(expected) = self.token.as_deref() {
            let provided = request
                .headers
                .get("x-plex-token")
                .or_else(|| query.get("X-Plex-Token"));
            if provided.map(String::as_str) != Some(expected) {
                return Response::error(403, "plexsim: forbidden");
            }
        }
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let token = format!("delegation-{}-{}", self.machine, nonce);
        Response::json(
            200,
            self.container(Map::from_iter([("token".to_string(), json!(token))]), false),
        )
    }

    fn route_prefs_write(&mut self, query: &HashMap<String, String>) -> Response {
        // `set_prefs` in server.py, all of it measured: an unknown id refuses the whole
        // request (400) and a write-protected one the whole request (403); an int or
        // double that does not parse is a 500 and an empty one is ignored; a bool is true
        // for exactly `1` and `true`; and each written id pushes a `preference` frame.
        let settings = self
            .pref_rows(&HashMap::new(), &HashMap::new())
            .into_iter()
            .filter_map(|row| Some((row.get("id")?.as_str()?.to_string(), row)))
            .collect::<HashMap<_, _>>();
        let mut writes = query
            .iter()
            .filter(|(key, _)| !key.starts_with("X-Plex-"))
            .collect::<Vec<_>>();
        writes.sort();
        for (key, _) in &writes {
            if !settings.contains_key(*key) {
                return Response::error(400, "plexsim: unknown preference");
            }
            // Cycle 19/30: these two answer 403 on 1.42.2 and push no frame, even when
            // the value written is the one already stored.
            if matches!(key.as_str(), "PlexOnlineUrl" | "allowMediaDeletionLanOnly") {
                return Response::error(403, "plexsim: forbidden");
            }
        }
        let mut typed = Vec::new();
        for (key, value) in &writes {
            let kind = settings[*key].get("type").and_then(Value::as_str).unwrap_or("text");
            match kind {
                "int" | "double" => {
                    if value.is_empty() {
                        continue;
                    }
                    let parsed = if kind == "int" {
                        value.trim().parse::<i64>().ok().map(|n| json!(n))
                    } else {
                        value.trim().parse::<f64>().ok().map(|n| json!(n))
                    };
                    match parsed {
                        Some(number) => typed.push(((*key).clone(), number)),
                        // Cycle 19: the two butler hours take a WORD and store nothing,
                        // alone among the 38 integer settings.
                        None if matches!(key.as_str(), "ButlerStartHour" | "ButlerEndHour") => {
                            continue
                        }
                        None => return Response::error(500, "plexsim: internal error"),
                    }
                }
                "bool" => typed.push(((*key).clone(), json!(value.as_str() == "1" || value.as_str() == "true"))),
                _ => typed.push(((*key).clone(), json!(value))),
            }
        }
        for (key, value) in &typed {
            if key == "FriendlyName" {
                let name = value.as_str().unwrap_or_default();
                self.friendly = if name.is_empty() {
                    local_host_name()
                } else {
                    name.to_string()
                };
            }
            self.prefs.insert(key.clone(), value.clone());
        }
        let written = typed.iter().map(|(key, _)| key.clone()).collect::<std::collections::BTreeSet<_>>();
        let mut moved = Vec::new();
        if written.contains("ButlerStartHour") || written.contains("ButlerEndHour") {
            // Cycle 74: a write that leaves the maintenance window EMPTY moves the end
            // hour to start + 1, wrapping 23 to 0, and frames the moved setting first.
            let current = self
                .pref_rows(&HashMap::new(), &HashMap::new())
                .into_iter()
                .filter_map(|row| Some((row.get("id")?.as_str()?.to_string(), row.get("value")?.clone())))
                .collect::<HashMap<_, _>>();
            let start = current.get("ButlerStartHour").and_then(Value::as_i64);
            let end = current.get("ButlerEndHour").and_then(Value::as_i64);
            if let (Some(start), Some(end)) = (start, end) {
                if start == end {
                    self.prefs.insert("ButlerEndHour".to_string(), json!((start + 1) % 24));
                    if !written.contains("ButlerEndHour") {
                        moved.push("ButlerEndHour".to_string());
                    }
                }
            }
        }
        let current = self
            .pref_rows(&HashMap::new(), &HashMap::new())
            .into_iter()
            .filter_map(|row| Some((row.get("id")?.as_str()?.to_string(), row)))
            .collect::<HashMap<_, _>>();
        for name in moved.iter().chain(written.iter()) {
            let Some(setting) = current.get(name).and_then(Value::as_object) else {
                continue;
            };
            let mut setting = setting.clone();
            setting.remove("enumValues");
            setting.insert("label".to_string(), json!(""));
            setting.insert("summary".to_string(), json!(""));
            self.push_event("preference", json!({"Setting": [Value::Object(setting)]}));
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// The row a `key=` parameter names, or the refusal a real PMS answers.
    ///
    /// Measured on every one of the `/:/` state writes: a MISSING key is a 400
    /// and an unknown one a 404, and the checks run in a FIXED order -- a missing
    /// key, then (cycle 21) an EMPTY key on scrobble/unscrobble/progress, then a
    /// missing identifier, then the write's own parameter (`validate`, before the
    /// identifier on `rate`), then a foreign identifier.  Cycle 21: the key is a
    /// BARE ratingKey -- an optional sign then digits -- and `rate`/`progress`
    /// int-parse it (a 500 for anything else) where scrobble just looks it up.
    fn keyed_row(
        &self,
        query: &HashMap<String, String>,
        validate: Option<&dyn Fn() -> Option<Response>>,
        numeric_key: bool,
        any_key: bool,
        validate_first: bool,
        empty_key_ok: bool,
    ) -> Result<Option<String>, Response> {
        let Some(raw) = query.get("key") else {
            return Err(Response::error(400, "plexsim: bad request"));
        };
        if raw.is_empty() && !empty_key_ok {
            return Err(Response::error(400, "plexsim: bad request"));
        }
        if query.get("identifier").is_none_or(String::is_empty) {
            return Err(Response::error(400, "plexsim: bad request"));
        }
        if validate_first {
            if let Some(refusal) = validate.and_then(|check| check()) {
                return Err(refusal);
            }
        }
        if query.get("identifier").map(String::as_str) != Some("com.plexapp.plugins.library") {
            return Err(Response::error(404, "plexsim: no such item"));
        }
        if !validate_first {
            if let Some(refusal) = validate.and_then(|check| check()) {
                return Err(refusal);
            }
        }
        let digits = raw.strip_prefix(['+', '-']).unwrap_or(raw);
        let parsed = (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
            .then(|| raw.parse::<i64>().ok())
            .flatten();
        if numeric_key && parsed.is_none() {
            return Err(Response::error(500, "plexsim: bad key"));
        }
        let found = parsed.map(|number| number.to_string()).filter(|key| {
            self.catalog.get(key).is_some()
                || self.collections.contains_key(key)
                || self.playlists.contains_key(key)
        });
        if found.is_none() && !any_key {
            return Err(Response::error(404, "plexsim: no such item"));
        }
        Ok(found)
    }

    /// Cycle 54: `viewUpdatedAt`, the time a row's per-user view state was
    /// written -- a rating, a scrobble, an unscrobble, a stored progress.
    fn stamp_view(&mut self, keys: &[String]) {
        let stamp = now();
        for key in keys {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("_viewUpdatedAt".to_string(), json!(stamp));
            }
        }
    }

    fn descendant_keys(&self, key: &str) -> Vec<String> {
        let mut out = Vec::new();
        for child in self.children(key) {
            if let Some(child_key) = key_of(&child) {
                out.push(child_key.clone());
                out.extend(self.descendant_keys(&child_key));
            }
        }
        out
    }

    fn parent_keys(&self, key: &str) -> Vec<String> {
        let Some(row) = self.catalog.get(key) else {
            return Vec::new();
        };
        ["parentRatingKey", "_parentRatingKey", "grandparentRatingKey"]
            .iter()
            .filter_map(|name| row.get(*name).map(string_value))
            .filter(|key| self.catalog.get(key).is_some())
            .collect()
    }

    fn route_progress(&mut self, query: &HashMap<String, String>) -> Response {
        // `/:/progress?key=&time=&state=`: a missing or unparseable `time` is a
        // 500 (cycle 21: a sign is fine, `1.5`/`0x10`/a space are not); an
        // integer key naming NO row is a 200 that stores nothing.
        let time = query.get("time").cloned().unwrap_or_default();
        let digits = time.strip_prefix(['+', '-']).unwrap_or(&time).to_string();
        let position = (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
            .then(|| time.parse::<i64>().ok())
            .flatten();
        let check = || position.is_none().then(|| Response::error(500, "plexsim: bad time"));
        let key = match self.keyed_row(query, Some(&check), true, true, false, false) {
            Ok(key) => key,
            Err(refusal) => return refusal,
        };
        let position = position.unwrap_or(0);
        if let Some(key) = key {
            if position >= 0 {
                self.store_progress(&key, position);
            }
        }
        self.save_if_dirty();
        Response::empty(200)
    }

    /// library.progress with `completes=True` -- a `/:/timeline` beat's
    /// view-state half, measured on half-hour media.
    ///
    /// The resume point: a position past a minute and outside the last tenth is
    /// stored (a beat with no `duration` stores nothing), and a beat at or under
    /// a minute DELETES it, but only inside a session that already passed the
    /// minute (cycle 58).  A beat at exactly the stored position writes nothing.
    /// The completion is a CROSSING (cycles 45/70): this session's previous
    /// non-zero position was under 90% and this one is at `(duration * 9) // 10`
    /// or past -- then the view counts once per session, stamps `lastViewedAt`,
    /// writes a history row, bumps the parents, and (cycle 57) records that the
    /// view came from a playback session.
    fn library_progress(
        &mut self,
        key: &str,
        offset: Option<i64>,
        total: i64,
        device_id: i64,
        session: &str,
        completes: bool,
    ) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let pair = (session.to_string(), key.to_string());
        let stored = row.get("viewOffset").and_then(Value::as_i64);
        let unmoved = offset.is_some() && stored.is_some() && offset == stored;
        if let Some(offset) = offset.filter(|_| !unmoved) {
            self.hidden_continue.remove(key);
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                let show = row.get("grandparentRatingKey").map(string_value);
                let siblings = self
                    .catalog
                    .items
                    .iter()
                    .filter(|other| other.get("grandparentRatingKey").map(string_value) == show)
                    .filter_map(key_of)
                    .collect::<Vec<_>>();
                for sibling in siblings {
                    self.hidden_continue.remove(&sibling);
                }
            }
            let near_end = completes && total > 0 && (offset as f64) >= total as f64 * 0.9;
            if (total > 0 || !completes) && offset > 60000 && !near_end {
                let stamp = now();
                self.update_view(key, "viewOffset", Some(json!(offset)));
                self.update_view(key, "lastViewedAt", Some(json!(stamp)));
                let mut stamped = vec![key.to_string()];
                stamped.extend(self.parent_keys(key));
                self.stamp_view(&stamped);
                for name in ["parentRatingKey", "grandparentRatingKey"] {
                    if let Some(parent) = row.get(name).map(string_value) {
                        self.update_view(&parent, "lastViewedAt", Some(json!(stamp)));
                    }
                }
            } else if total > 0 && offset <= 60000 && self.progressed.contains(&pair) {
                self.update_view(key, "viewOffset", None);
            }
        }
        let position = offset.or(stored).unwrap_or(0);
        let seen = self.timeline_offsets.get(&pair).copied();
        if offset.is_some() && total > 0 {
            self.timeline_offsets.insert(pair.clone(), position);
            if position > 60000 {
                self.progressed.insert(pair.clone());
            }
        }
        let crossed = seen.is_some_and(|seen| seen > 0 && (seen as f64) < total as f64 * 0.9) && total > 0;
        if completes && crossed && position >= total.saturating_mul(9) / 10 {
            let completed = self.catalog.get(key).and_then(|row| row.get("_completed")).and_then(Value::as_str).map(str::to_string);
            if completed.as_deref() != Some(session) {
                let stamp = now();
                let count = self.catalog.get(key).and_then(|row| row.get("viewCount")).and_then(Value::as_i64).unwrap_or(0);
                if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                    object.insert("_completed".to_string(), json!(session));
                    object.remove("_completionStopped");
                }
                self.update_view(key, "viewCount", Some(json!(count + 1)));
                self.viewed_in_session.insert(key.to_string());
                self.update_view(key, "viewOffset", None);
                self.update_view(key, "lastViewedAt", Some(json!(stamp)));
                let mut stamped = vec![key.to_string()];
                stamped.extend(self.parent_keys(key));
                self.stamp_view(&stamped);
                if let Some(current) = self.catalog.get(key).cloned() {
                    self.next_dynamic_key = self.next_dynamic_key.saturating_add(1);
                    let history = self.history_row_for_device(&current, device_id);
                    self.history.push(history);
                }
                for name in ["parentRatingKey", "grandparentRatingKey"] {
                    let Some(parent) = row.get(name).map(string_value) else {
                        continue;
                    };
                    if self.catalog.get(&parent).is_none() {
                        continue;
                    }
                    let previous = self.catalog.get(&parent).and_then(|p| p.get("viewCount")).and_then(Value::as_i64).unwrap_or(0);
                    self.update_view(&parent, "viewCount", Some(json!(previous + 1)));
                    self.update_view(&parent, "lastViewedAt", Some(json!(stamp)));
                }
            }
        } else if offset.is_some_and(|offset| total > 0 && (offset as f64) < total as f64 * 0.9) {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.remove("_completed");
                object.remove("_completionStopped");
            }
        }
    }

    /// library.progress with `completes=False`: `/:/progress` never completes a
    /// play, and stores any position past a minute as the resume point.  A beat
    /// at exactly the stored position writes nothing (cycle 58).
    fn store_progress(&mut self, key: &str, position: i64) {
        if self.catalog.get(key).is_none() {
            let state = self.list_state.entry(key.to_string()).or_default();
            if state.get("viewOffset").and_then(Value::as_i64) == Some(position) {
                return;
            }
            if position > 60000 {
                state.insert("viewOffset".to_string(), json!(position));
                state.insert("lastViewedAt".to_string(), json!(now()));
            }
            self.dirty = true;
            return;
        }
        self.library_progress(key, Some(position), 0, 1, "", false);
    }

    /// `/:/scrobble` and `/:/unscrobble` -- Mark as Played / Unplayed, an EMPTY
    /// 200.  A show or season marks every leaf beneath it; cycle 25: a scrobble
    /// writes one history row per LEAF it marks; an EPISODE scrobble adds one to
    /// its season's and show's own counter, and unscrobbling a season or show
    /// removes its own.  Cycle 21: a COLLECTION's scrobble is a 200 that changes
    /// nothing, its unscrobble clears; a playlist counts both halves.
    fn route_scrobble(&mut self, query: &HashMap<String, String>, watched: bool) -> Response {
        let key = match self.keyed_row(query, None, false, false, false, false) {
            Ok(Some(key)) => key,
            Ok(None) => return Response::error(404, "plexsim: no such item"),
            Err(refusal) => return refusal,
        };
        if self.catalog.get(&key).is_none() {
            let is_collection = self.collections.contains_key(&key);
            if watched && is_collection {
                return Response::empty(200);
            }
            let state = self.list_state.entry(key.clone()).or_default();
            if watched {
                let count = state.get("viewCount").and_then(Value::as_i64).unwrap_or(1);
                state.insert("viewCount".to_string(), json!(count + 1));
                state.insert("lastViewedAt".to_string(), json!(now()));
                state.remove("viewOffset");
            } else {
                state.remove("viewCount");
                state.remove("viewOffset");
                state.remove("lastViewedAt");
            }
            self.dirty = true;
            self.save_if_dirty();
            return Response::empty(200);
        }
        let kind = self
            .catalog
            .get(&key)
            .and_then(|row| row.get("type"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let targets = if matches!(kind.as_str(), "show" | "season") {
            self.leaves(&key).into_iter().filter_map(|row| key_of(&row)).collect::<Vec<_>>()
        } else {
            vec![key.clone()]
        };
        let stamp = now();
        for leaf in &targets {
            if watched {
                let count = self
                    .catalog
                    .get(leaf)
                    .and_then(|row| row.get("viewCount"))
                    .and_then(Value::as_i64)
                    .unwrap_or(0);
                self.update_view(leaf, "viewCount", Some(json!(count + 1)));
                self.update_view(leaf, "lastViewedAt", Some(json!(stamp)));
                self.update_view(leaf, "viewOffset", None);
                if let Some(row) = self.catalog.get(leaf).cloned() {
                    self.next_dynamic_key = self.next_dynamic_key.saturating_add(1);
                    let mut history = self.history_row(&row);
                    if let Some(object) = history.as_object_mut() {
                        object.insert("viewType".to_string(), json!(1));
                    }
                    self.history.push(history);
                }
            } else {
                self.update_view(leaf, "viewCount", None);
                self.update_view(leaf, "viewOffset", None);
                self.update_view(leaf, "lastViewedAt", None);
            }
        }
        if watched && kind == "episode" {
            for name in ["parentRatingKey", "grandparentRatingKey"] {
                let Some(parent) = self.catalog.get(&key).and_then(|row| row.get(name)).map(string_value) else {
                    continue;
                };
                let previous = self
                    .catalog
                    .get(&parent)
                    .and_then(|row| row.get("viewCount"))
                    .and_then(Value::as_i64)
                    .unwrap_or(0);
                self.update_view(&parent, "viewCount", Some(json!(previous + 1)));
                self.update_view(&parent, "lastViewedAt", Some(json!(stamp)));
            }
        } else if !watched && matches!(kind.as_str(), "show" | "season") {
            self.update_view(&key, "viewCount", None);
            self.update_view(&key, "lastViewedAt", None);
        }
        if !targets.is_empty() {
            let mut stamped = vec![key.clone()];
            stamped.extend(self.descendant_keys(&key));
            if watched && kind == "episode" {
                stamped.extend(self.parent_keys(&key));
            }
            self.stamp_view(&stamped);
        }
        if !watched {
            self.completed_sessions.retain(|(_, item)| item != &key);
            self.completion_pending_stop.retain(|(_, item)| item != &key);
        }
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `/:/rate?key=&identifier=&rating=` -- the star rating.  An unparseable
    /// rating is a 400 refused BEFORE a foreign identifier; -1 clears both the
    /// stars and `lastRatedAt`; any other negative clears the stars and STAMPS
    /// `lastRatedAt` (cycle 54).  Every rating stamps `viewUpdatedAt`.
    fn route_rate(&mut self, query: &HashMap<String, String>) -> Response {
        let rating = query.get("rating").and_then(|value| value.trim().parse::<f64>().ok()).filter(|value| value.is_finite());
        let check = || rating.is_none().then(|| Response::error(400, "plexsim: bad request"));
        let key = match self.keyed_row(query, Some(&check), true, false, true, true) {
            Ok(Some(key)) => key,
            Ok(None) => return Response::error(404, "plexsim: no such item"),
            Err(refusal) => return refusal,
        };
        let rating = rating.unwrap_or(-1.0);
        if self.catalog.get(&key).is_none() {
            let state = self.list_state.entry(key.clone()).or_default();
            if rating == -1.0 {
                state.remove("userRating");
                state.remove("lastRatedAt");
            } else if rating < 0.0 {
                state.remove("userRating");
                state.insert("lastRatedAt".to_string(), json!(now()));
            } else {
                state.insert("userRating".to_string(), json!(rating));
                state.insert("lastRatedAt".to_string(), json!(now()));
            }
            self.dirty = true;
            self.save_if_dirty();
            return Response::empty(200);
        }
        self.stamp_view(&[key.clone()]);
        if rating == -1.0 {
            self.update_view(&key, "userRating", None);
            self.update_view(&key, "lastRatedAt", None);
        } else if rating < 0.0 {
            self.update_view(&key, "userRating", None);
            self.update_view(&key, "lastRatedAt", Some(json!(now())));
        } else {
            self.update_view(&key, "userRating", Some(json!(rating)));
            self.update_view(&key, "lastRatedAt", Some(json!(now())));
        }
        self.save_if_dirty();
        Response::empty(200)
    }

    fn playback_session(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> String {
        query
            .get("X-Plex-Session-Identifier")
            .or_else(|| headers.get("x-plex-session-identifier"))
            .or_else(|| query.get("X-Plex-Client-Identifier"))
            .or_else(|| headers.get("x-plex-client-identifier"))
            .cloned()
            .unwrap_or_else(|| "plexsim".to_string())
    }

    fn decision_target(
        &self,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Result<String, Response> {
        let profile = [
            "X-Plex-Client-Profile-Name",
            "X-Plex-Platform",
            "X-Plex-Device",
        ]
        .iter()
        .filter_map(|name| {
            query
                .get(*name)
                .or_else(|| request.headers.get(&name.to_ascii_lowercase()))
        })
        .find(|name| self.decision_profiles.contains_key(name.as_str()));
        if profile.is_none() {
            return Err(Response::error(400, "plexsim: bad request"));
        }
        let Some(path) = query.get("path") else {
            return Err(Response::error(400, "plexsim: bad request"));
        };
        let Some(tail) = path.split("/library/metadata/").nth(1) else {
            return Err(Response::error(400, "plexsim: bad request"));
        };
        let raw_key = tail.split(['/', '?']).next().unwrap_or_default();
        if raw_key.is_empty() || !raw_key.chars().all(|ch| ch.is_ascii_digit()) {
            return Err(Response::error(400, "plexsim: bad request"));
        }
        let key = raw_key.to_string();
        let Some(row) = self.catalog.get(&key) else {
            return Err(Response::error(400, "plexsim: bad request"));
        };
        let media = row
            .get("Media")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if media.is_empty() {
            return Err(Response::error(400, "plexsim: bad request"));
        }
        let raw_index = query
            .get("mediaIndex")
            .map(String::as_str)
            .unwrap_or_default();
        if media.len() > 1 {
            if raw_index.is_empty() || !raw_index.chars().all(|ch| ch.is_ascii_digit()) {
                return Err(Response::error(400, "plexsim: bad request"));
            }
            let Ok(index) = raw_index.parse::<usize>() else {
                return Err(Response::error(400, "plexsim: bad request"));
            };
            if index >= media.len() {
                return Err(Response::error(400, "plexsim: bad request"));
            }
        }
        let selected = if media.len() > 1 {
            raw_index.parse::<usize>().unwrap_or(0)
        } else if raw_index.chars().all(|ch| ch.is_ascii_digit()) && !raw_index.is_empty() {
            let Ok(index) = raw_index.parse::<usize>() else {
                return Err(Response::error(400, "plexsim: bad request"));
            };
            if index > 0 {
                return Err(Response::error(400, "plexsim: bad request"));
            }
            0
        } else {
            0
        };
        if let Some(parts) = media[selected].get("Part").and_then(Value::as_array) {
            let raw_part = query
                .get("partIndex")
                .map(String::as_str)
                .unwrap_or_default();
            if parts.len() > 1
                && !raw_part.is_empty()
                && raw_part != "-1"
                && raw_part.chars().all(|ch| ch.is_ascii_digit())
                && raw_part
                    .parse::<usize>()
                    .ok()
                    .is_some_and(|index| index >= parts.len())
            {
                return Err(Response::error(400, "plexsim: bad request"));
            }
        }
        Ok(key)
    }

    fn route_decision(
        &mut self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> Response {
        let profile_name = [
            "X-Plex-Client-Profile-Name",
            "X-Plex-Platform",
            "X-Plex-Device",
        ]
        .iter()
        .filter_map(|name| {
            query
                .get(*name)
                .or_else(|| headers.get(&name.to_ascii_lowercase()))
        })
        .find(|name| self.decision_profiles.contains_key(name.as_str()))
        .cloned();
        let Some(profile_name) = profile_name else {
            return Response::error(400, "plexsim: bad request");
        };
        let path = query.get("path").cloned().unwrap_or_default();
        let key = path
            .split("/library/metadata/")
            .nth(1)
            .and_then(|tail| tail.split(['/', '?']).next())
            .unwrap_or("")
            .to_string();
        let Some(row) = self.catalog.get(&key).cloned() else {
            return Response::error(400, "plexsim: bad request");
        };
        let media_rows = row
            .get("Media")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if media_rows.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        let media_index_raw = query.get("mediaIndex").cloned().unwrap_or_default();
        let media_index = if media_rows.len() > 1 {
            let Ok(index) = media_index_raw.parse::<usize>() else {
                return Response::error(400, "plexsim: bad request");
            };
            if index >= media_rows.len() {
                return Response::error(400, "plexsim: bad request");
            }
            index
        } else if let Ok(index) = media_index_raw.parse::<i64>() {
            if index > 0 {
                return Response::error(400, "plexsim: bad request");
            }
            0
        } else {
            0
        };
        let selected_media = media_rows.get(media_index).cloned().unwrap_or(Value::Null);
        let parts = selected_media
            .get("Part")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let part_raw = query.get("partIndex").cloned().unwrap_or_default();
        if parts.len() > 1 && part_raw != "-1" && !part_raw.is_empty() {
            let Ok(part_index) = part_raw.parse::<usize>() else {
                return Response::error(400, "plexsim: bad request");
            };
            if part_index >= parts.len() {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let protocol = query.get("protocol").map(String::as_str).unwrap_or("http");
        let session = query
            .get("X-Plex-Session-Identifier")
            .or_else(|| headers.get("x-plex-session-identifier"))
            .or_else(|| query.get("X-Plex-Client-Identifier"))
            .or_else(|| headers.get("x-plex-client-identifier"))
            .cloned()
            .unwrap_or_default();
        let section = self
            .section(&string_value(
                row.get("librarySectionID").unwrap_or(&Value::Null),
            ))
            .cloned()
            .unwrap_or_default();
        let mut fields = Map::from_iter([
            ("allowSync".to_string(), json!("1")),
            (
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            ),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
            (
                "mediaTagVersion".to_string(),
                json!(MEDIA_TAG_VERSION.to_string()),
            ),
            (
                "librarySectionID".to_string(),
                json!(string_value(
                    row.get("librarySectionID").unwrap_or(&Value::Null)
                )),
            ),
            (
                "librarySectionTitle".to_string(),
                section.get("title").cloned().unwrap_or(Value::Null),
            ),
            (
                "librarySectionUUID".to_string(),
                section.get("uuid").cloned().unwrap_or(Value::Null),
            ),
        ]);
        let all_parts = parts.len() > 1 && part_raw == "-1";
        let direct_disabled = query.get("directPlay").is_some_and(|value| value == "0");
        let selected_for_engine = if all_parts {
            selected_media.clone()
        } else {
            let part_index = part_raw.parse::<usize>().unwrap_or(0);
            let mut media = selected_media.clone();
            if let Some(object) = media.as_object_mut() {
                if let Some(part) = parts.get(part_index).cloned() {
                    object.insert("Part".to_string(), Value::Array(vec![part]));
                }
            }
            media
        };
        let mut refusal = if all_parts {
            Some("App cannot direct play this item. Multi-part media requires a conversion when all parts are selected.".to_string())
        } else if direct_disabled {
            Some("App cannot direct play this item. Direct play is disabled.".to_string())
        } else {
            None
        };
        let mde = query.get("hasMDE") == Some(&"1".to_string());
        if refusal.is_none() && !mde {
            let answer = decision::decide(
                &self.decision_profiles,
                &profile_name,
                &selected_for_engine,
                protocol,
                true,
            );
            if answer.outcome == decision::Outcome::Refuse {
                // Xbox 360's one video target is an asf with no protocol, and both
                // references answer a 400 for every row it cannot direct play.
                return Response::error(400, "plexsim: bad request");
            }
            if answer.outcome != decision::Outcome::DirectPlay {
                refusal = answer.text;
            }
        }
        if let Some(refusal) = refusal {
            fields.insert("directPlayDecisionCode".to_string(), json!(3000));
            fields.insert("directPlayDecisionText".to_string(), json!(refusal));
            fields.insert("generalDecisionCode".to_string(), json!(2000));
            fields.insert(
                "generalDecisionText".to_string(),
                json!("Neither direct play nor conversion is available."),
            );
            let blocked = decision::dolby_vision_blocked(
                &self.decision_profiles,
                &profile_name,
                Some(&selected_for_engine),
                protocol,
            );
            fields.insert(
                "transcodeDecisionCode".to_string(),
                json!(if blocked { 2003 } else { 4005 }),
            );
            fields.insert(
                "transcodeDecisionText".to_string(),
                json!(if blocked {
                    "File is unplayable. DoVi (Profile 5) color space is not supported."
                } else {
                    "Cannot convert this item. No conversion profile found for protocol http."
                }),
            );
            fields.insert("size".to_string(), json!(0));
            return Response::json(200, self.container(fields, false));
        }
        self.decided_sessions.insert((session.clone(), key.clone()));
        for existing in self.sessions.values_mut() {
            let same_item = existing
                .get("ratingKey")
                .map(string_value)
                .is_some_and(|value| value == key);
            let client_identifier = query
                .get("X-Plex-Client-Identifier")
                .or_else(|| headers.get("x-plex-client-identifier"))
                .map(String::as_str)
                .unwrap_or_default();
            let same_session = existing
                .get("sessionKey")
                .and_then(Value::as_str)
                .is_some_and(|value| value == client_identifier || value == session);
            if same_item && same_session {
                if let Some(object) = existing.as_object_mut() {
                    object.insert("Session".to_string(), json!({"id":session}));
                }
            }
        }
        fields.insert("resourceSession".to_string(), json!(session));
        if mde {
            fields.insert("mdeDecisionCode".to_string(), json!(1000));
            fields.insert("mdeDecisionText".to_string(), json!("Direct play OK."));
        } else {
            fields.insert("directPlayDecisionCode".to_string(), json!(1000));
            fields.insert(
                "directPlayDecisionText".to_string(),
                json!("Direct play OK."),
            );
            fields.insert("generalDecisionCode".to_string(), json!(1000));
            fields.insert("generalDecisionText".to_string(), json!("Direct play OK."));
        }
        let mut output = self.detail_row(&row);
        if let Some(object) = output.as_object_mut() {
            object.remove("Guid");
            object.remove("Image");
            object.insert("_session".to_string(), json!(true));
            if object.get("summary").is_some_and(Value::is_string)
                && object.get("summary").and_then(Value::as_str) == Some("")
            {
                object.remove("summary");
            }
            let mut media = selected_media;
            if let Some(media_object) = media.as_object_mut() {
                if let Some(id) = media_object.get("id").cloned() {
                    media_object.insert("id".to_string(), json!(string_value(&id)));
                }
                if let Some(aspect_ratio) = media_object.get("aspectRatio").cloned() {
                    media_object.insert(
                        "aspectRatio".to_string(),
                        json!(string_value(&aspect_ratio)),
                    );
                }
                let has_voice_activity = media_object
                    .get("hasVoiceActivity")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                media_object.insert(
                    "hasVoiceActivity".to_string(),
                    json!(if has_voice_activity { "1" } else { "0" }),
                );
                media_object.insert("_session".to_string(), json!(true));
                media_object.insert("selected".to_string(), json!(true));
                let source_parts = media_object
                    .get("Part")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let part_index = part_raw.parse::<usize>().unwrap_or(0);
                let selected_parts = if all_parts {
                    source_parts
                } else {
                    source_parts.get(part_index).cloned().into_iter().collect()
                };
                let selected_parts = selected_parts
                    .into_iter()
                    .map(|mut part| {
                        if let Some(part_object) = part.as_object_mut() {
                            let streams = part_object
                                .remove("Stream")
                                .and_then(|value| value.as_array().cloned())
                                .unwrap_or_default()
                                .into_iter()
                                .map(|mut stream| {
                                    if let Some(stream_object) = stream.as_object_mut() {
                                        if let Some(id) = stream_object.get("id").cloned() {
                                            stream_object
                                                .insert("id".to_string(), json!(string_value(&id)));
                                        }
                                        stream_object
                                            .insert("location".to_string(), json!("direct"));
                                        if stream_object.get("streamType").and_then(Value::as_i64)
                                            == Some(1)
                                        {
                                            for title_key in
                                                ["displayTitle", "extendedDisplayTitle"]
                                            {
                                                if let Some(title) = stream_object
                                                    .get(title_key)
                                                    .and_then(Value::as_str)
                                                    .map(str::to_string)
                                                {
                                                    if let Some((base, suffix)) =
                                                        title.rsplit_once(" (")
                                                    {
                                                        if suffix.ends_with(')') {
                                                            stream_object.insert(
                                                                title_key.to_string(),
                                                                json!(base),
                                                            );
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        stream_object.insert("_session".to_string(), json!(true));
                                    }
                                    stream
                                })
                                .collect::<Vec<_>>();
                            part_object.remove("exists");
                            part_object.remove("accessible");
                            if let Some(id) = part_object.get("id").cloned() {
                                part_object.insert("id".to_string(), json!(string_value(&id)));
                            }
                            part_object.insert("decision".to_string(), json!("directplay"));
                            part_object.insert("selected".to_string(), json!(true));
                            part_object.insert("_session".to_string(), json!(true));
                            if !streams.is_empty() {
                                part_object.insert("Stream".to_string(), Value::Array(streams));
                            }
                        }
                        part
                    })
                    .collect::<Vec<_>>();
                media_object.insert("Part".to_string(), Value::Array(selected_parts));
            }
            object.insert("Media".to_string(), Value::Array(vec![media]));
            if let Some(section_id) = object.get("librarySectionID").cloned() {
                object.insert(
                    "librarySectionID".to_string(),
                    json!(string_value(&section_id)),
                );
            }
            if let Some(colors) = object.get("UltraBlurColors").cloned() {
                if !colors.is_array() {
                    let mut colors = colors;
                    if let Some(color_object) = colors.as_object_mut() {
                        color_object.insert("_session".to_string(), json!(true));
                    }
                    object.insert("UltraBlurColors".to_string(), json!([colors]));
                }
            }
            if let Some(ratings) = object.get_mut("Rating").and_then(Value::as_array_mut) {
                for rating in ratings {
                    if let Some(rating_object) = rating.as_object_mut() {
                        rating_object.insert("_session".to_string(), json!(true));
                        if let Some(id) = rating_object.get("id").cloned() {
                            rating_object.insert("id".to_string(), json!(string_value(&id)));
                        }
                        if let Some(value) = rating_object.get("value").cloned() {
                            rating_object.insert("value".to_string(), json!(string_value(&value)));
                        }
                    }
                }
            }
            for field in [
                "Genre",
                "Country",
                "Director",
                "Writer",
                "Role",
                "Producer",
                "Collection",
                "Similar",
                "Label",
                "Style",
                "Mood",
            ] {
                if let Some(tags) = object.get_mut(field).and_then(Value::as_array_mut) {
                    for tag in tags {
                        if let Some(tag_object) = tag.as_object_mut() {
                            tag_object.insert("_session".to_string(), json!(true));
                            if let Some(id) = tag_object.get("id").cloned() {
                                tag_object.insert("id".to_string(), json!(string_value(&id)));
                            }
                        }
                    }
                }
            }
        }
        output = order_scalar_fields(output);
        fields.insert("Metadata".to_string(), Value::Array(vec![output]));
        Response::json(200, self.container(fields, false))
    }

    fn route_timeline(
        &mut self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> Response {
        let key = query
            .get("ratingKey")
            .map(|v| extract_key(v))
            .or_else(|| query.get("key").map(|v| extract_key(v)));
        let Some(key) = key else {
            return Response::error(400, "plexsim: bad request");
        };
        let Some(state) = query.get("state").cloned() else {
            return Response::error(400, "plexsim: bad request");
        };
        if !["playing", "paused", "buffering", "stopped"].contains(&state.as_str()) {
            return Response::error(400, "plexsim: bad request");
        }
        // Cycle 45: `playbackTime=` is a VALIDATED signed 32-bit integer; anything else
        // is a 400 on both copies.
        if query.get("playbackTime").is_some_and(|value| !int_param(value, Some(32))) {
            return Response::error(400, "plexsim: bad request");
        }
        let time = match query.get("time") {
            Some(value) => match value.parse::<i64>() {
                Ok(parsed) => parsed,
                Err(_) => return Response::error(400, "plexsim: bad request"),
            },
            None => 0,
        };
        let duration = match query.get("duration") {
            Some(value) => match value.parse::<i64>() {
                Ok(parsed) => parsed,
                Err(_) => return Response::error(400, "plexsim: bad request"),
            },
            None => 0,
        };
        if time < 0 || duration < 0 || (duration > 0 && time > duration) {
            return Response::error(400, "plexsim: bad request");
        }
        let client = query
            .get("X-Plex-Client-Identifier")
            .cloned()
            .or_else(|| headers.get("x-plex-client-identifier").cloned())
            .unwrap_or_else(|| "default".to_string());
        if !key.is_empty() && self.catalog.get(&key).is_none() {
            return Response::error(404, "plexsim: no such item");
        }
        let session_identifier = query
            .get("X-Plex-Session-Identifier")
            .cloned()
            .or_else(|| headers.get("x-plex-session-identifier").cloned())
            .unwrap_or_else(|| client.clone());
        let session_pair = (session_identifier.clone(), key.clone());
        let decided_for_response = self.decided_sessions.contains(&session_pair);
        // The play's session tag, as `_completed` stores it (`session or True`).
        let session_tag = if session_identifier.is_empty() { "\u{1}".to_string() } else { session_identifier.clone() };
        let completed_by = |runtime: &Self| {
            runtime
                .catalog
                .get(&key)
                .and_then(|row| row.get("_completed"))
                .and_then(Value::as_str)
                .map(str::to_string)
        };
        // A completed play answers `complete` to every beat past 90% until a
        // `stopped` beat has closed it, and `ignore` to every beat after that.
        let already = !key.is_empty()
            && completed_by(self).as_deref() == Some(session_tag.as_str())
            && self.catalog.get(&key).is_some_and(|row| row.get("_completionStopped").is_some());
        let total = if query.get("duration").is_some() { duration } else { 0 };
        if !key.is_empty() {
            let device_id = self.device_id(&client);
            let offset = query.get("time").map(|_| time);
            self.library_progress(&key, offset, total, device_id, &session_tag, true);
            if state == "stopped" && completed_by(self).as_deref() == Some(session_tag.as_str()) {
                if let Some(object) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                    object.insert("_completionStopped".to_string(), json!(true));
                }
            }
        }
        // Cycle 50: `progress` is sticky within the playback SESSION once one of
        // its beats passes the minute gate.
        if time > 60000 {
            self.started_sessions.insert(session_identifier.clone());
        }
        let mut playback_state = "ignore";
        if total > 0 {
            playback_state = if self.started_sessions.contains(&session_identifier) {
                "progress"
            } else {
                "ignore"
            };
            if time >= total.saturating_mul(9) / 10 {
                // Past 90% it is never `progress`: `complete` for the beat that
                // completed the play and every one after it until a `stopped`
                // closes it, `ignore` for a beat that completed nothing.
                let finished = completed_by(self).as_deref() == Some(session_tag.as_str());
                playback_state = if finished && !already { "complete" } else { "ignore" };
            }
        }
        if state == "stopped" {
            self.started_sessions.remove(&session_identifier);
        }
        if let Some(item_id) = query
            .get("playQueueItemID")
            .and_then(|value| value.parse::<i64>().ok())
        {
            for queue in self.queues.values_mut() {
                if queue.items.iter().any(|item| item.id == item_id) {
                    queue.selected = Some(item_id);
                    queue.version += 1;
                    break;
                }
            }
        } else if !key.is_empty() {
            for queue in self.queues.values_mut() {
                if let Some(item) = queue.items.iter().find(|item| item.rating_key == key) {
                    queue.selected = Some(item.id);
                    break;
                }
            }
        }
        let already_listed = self.sessions.contains_key(&client);
        if state == "stopped" {
            self.sessions.remove(&client);
            self.decided_sessions
                .remove(&(session_identifier.clone(), key.clone()));
        } else if matches!(state.as_str(), "paused" | "buffering") && !already_listed {
            // The first paused/buffering beat is the player's probe.  PMS does
            // not publish a Now Playing row until a playing beat arrives.
        } else {
            let decided = self
                .decided_sessions
                .contains(&(session_identifier.clone(), key.clone()));
            let Some(row) = self.catalog.get(&key) else {
                return Response::error(404, "plexsim: no such item");
            };
            let session = self.session_row(
                row,
                &key,
                &client,
                &session_identifier,
                &state,
                time,
                duration,
                headers,
                query,
                decided,
            );
            self.sessions.insert(client.clone(), session);
            self.push_event(
                "playing",
                json!({"PlaySessionStateNotification": {
                    "sessionKey": client,
                    "clientIdentifier": headers.get("x-plex-client-identifier").cloned().unwrap_or_default(),
                    "ratingKey": key,
                    "key": format!("/library/metadata/{key}"),
                    "viewOffset": time,
                    "state": state,
                }}),
            );
        }
        self.save_if_dirty();
        let stored = self
            .catalog
            .get(&key)
            .and_then(|row| row.get("viewOffset"))
            .and_then(Value::as_i64)
            .unwrap_or(-1);
        let mut fields = Map::from_iter([
            ("playbackState".to_string(), json!(playback_state)),
            ("skipCount".to_string(), json!(0)),
            (
                "viewCount".to_string(),
                json!(self
                    .catalog
                    .get(&key)
                    .and_then(|row| row.get("viewCount"))
                    .and_then(Value::as_i64)
                    .unwrap_or(0)),
            ),
            ("viewOffset".to_string(), json!(stored)),
        ]);
        let product = query
            .get("X-Plex-Product")
            .or_else(|| headers.get("x-plex-product"));
        if (decided_for_response
            || self
                .decided_sessions
                .contains(&(session_identifier.clone(), key.clone())))
            && !matches!(
                product.map(String::as_str),
                Some("Plex for Windows" | "Plex for Windows Phone")
            )
        {
            let resolution = self
                .catalog
                .get(&key)
                .and_then(|row| row.get("Media"))
                .and_then(Value::as_array)
                .and_then(|media| media.first())
                .and_then(|media| media.get("videoResolution"))
                .and_then(Value::as_str)
                .map(|value| format!("{value}p"))
                .unwrap_or_else(|| "1080p".to_string());
            fields.insert(
                "Bandwidths".to_string(),
                json!([{"Bandwidth":[{"resolution":resolution,"bytes":self.bandwidth_bytes}]}]),
            );
        }
        if let Some(reason) = self.terminated_sessions.get(&session_identifier) {
            fields.insert("terminationCode".to_string(), json!(2006));
            fields.insert("terminationText".to_string(), json!(reason));
        }
        fields.insert(
            "size".to_string(),
            json!(if fields.contains_key("Bandwidths") {
                1
            } else {
                0
            }),
        );
        Response::json(200, self.container(fields, false))
    }

    fn route_hubs(&self, query: &HashMap<String, String>) -> Response {
        if let Some(refusal) = hub_refusal(query) {
            return refusal;
        }
        // Cycle 54: the TV shelf runs the request's filters, and one that does not
        // parse refuses the page.
        if let Err(status) = self.filtered_tv_leaves(query, true) {
            return Response::error(status, "plexsim: bad request");
        }
        Response::json(200, self.global_hubs(query))
    }

    /// Build the Recommended tab for one library.  These shelves are separate
    /// from the Home hubs: each one advertises the listing endpoint that owns
    /// its ordering and carries the listing's head, with `count=` applied at
    /// the shelf boundary.  Keeping the rows derived from the catalog (rather
    /// than copying a fixture response) is important because timeline and
    /// metadata edits must immediately change the shelves.
    fn route_section_hubs(&self, section_key: &str, query: &HashMap<String, String>) -> Response {
        let Some(section) = self.section(section_key) else {
            return Response::error(404, "plexsim: not found");
        };
        let include_guids = query.get("includeGuids").map(String::as_str) == Some("1");
        let is_show = section.get("type").and_then(Value::as_str) == Some("show");
        if is_show {
            if let Some(refusal) = hub_refusal(query) {
                return refusal;
            }
        }
        let title = section
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or_default();
        // Cycle 55: `count` is a SIGNED 32-bit integer with an optional sign; anything
        // else falls back to the default six.
        let count = query
            .get("count")
            .filter(|value| int_param(value, Some(32)))
            .and_then(|value| value.trim_start_matches('+').parse::<i64>().ok());
        let include_playlists = query
            .get("includeLibraryPlaylists")
            .is_some_and(|value| is_true_option(value));

        let items = self.section_rows(section_key);
        let episodes = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some("episode")
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                        == section_key
            })
            .cloned()
            .collect::<Vec<_>>();

        // Cycle 57/58: the section's Continue Watching shelf is the same list as its
        // see-all leaf (`section_continue_watching`): the library's resume rows, a
        // next-up only once its show has a playback completion.
        let in_progress = self.section_continue_watching_rows(section_key, is_show);

        let mut watched = if is_show {
            episodes.clone()
        } else {
            items.clone()
        };
        watched.retain(|row| {
            row.get("lastViewedAt")
                .and_then(Value::as_i64)
                .is_some_and(|value| value > 0)
                && row
                    .get("viewCount")
                    .and_then(Value::as_i64)
                    .is_some_and(|value| value > 0)
                && row.get("viewOffset").and_then(Value::as_i64).unwrap_or(0) == 0
        });
        watched.sort_by(|left, right| {
            right
                .get("lastViewedAt")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .cmp(
                    &left
                        .get("lastViewedAt")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                )
        });

        let mut released = if is_show {
            episodes.clone()
        } else {
            items.clone()
        };
        if !is_show {
            // The measured movie Recommended shelf excludes titles older than
            // one year; TV's Recently Released shelf has no equivalent window.
            let year_ago = unix_year(now() - 365 * 86_400);
            released.retain(|row| {
                row.get("originallyAvailableAt")
                    .and_then(Value::as_str)
                    .and_then(|value| value.get(..4))
                    .and_then(|value| value.parse::<i64>().ok())
                    .map(|year| year >= year_ago)
                    .unwrap_or(false)
            });
        }
        released.sort_by(|left, right| {
            string_value(right.get("originallyAvailableAt").unwrap_or(&Value::Null)).cmp(
                &string_value(left.get("originallyAvailableAt").unwrap_or(&Value::Null)),
            )
        });

        let mut added = self.section_recently_added_rows(section);
        if is_show {
            // Cycle 54, measured on kids and zen: the TV shelf here runs the request's
            // filters over the episodes, and `contentDirectoryID` naming another
            // library empties it -- the other shelves keep their rows.
            match self.filtered_tv_leaves(query, true) {
                Ok(Some(leaves)) => added = self.recently_added_tv(Some(&section_key.to_string()), Some(leaves)),
                Ok(None) => {}
                Err(status) => return Response::error(status, "plexsim: bad request"),
            }
            if content_directories(query).is_some_and(|wanted| !wanted.contains(section_key)) {
                added.clear();
            }
        }

        let mut top_unwatched = items.clone();
        top_unwatched.retain(|row| {
            row.get("viewCount").and_then(Value::as_i64).unwrap_or(0) == 0
                && row
                    .get("audienceRating")
                    .and_then(Value::as_f64)
                    .is_some_and(|value| value >= 7.0)
        });
        top_unwatched.sort_by(|left, right| {
            right
                .get("audienceRating")
                .and_then(Value::as_f64)
                .unwrap_or(0.0)
                .partial_cmp(
                    &left
                        .get("audienceRating")
                        .and_then(Value::as_f64)
                        .unwrap_or(0.0),
                )
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut unstarted = items.clone();
        unstarted.retain(|show| {
            let key = key_of(show).unwrap_or_default();
            self.leaves(&key).iter().all(|leaf| {
                leaf.get("viewCount").and_then(Value::as_i64).unwrap_or(0) == 0
                    && leaf.get("viewOffset").and_then(Value::as_i64).unwrap_or(0) == 0
            })
        });

        let mut top_rated = items.clone();
        top_rated.retain(|show| {
            show.get("audienceRating")
                .and_then(Value::as_f64)
                .is_some_and(|value| value >= 7.0)
                && {
                    let key = key_of(show).unwrap_or_default();
                    self.leaves(&key)
                        .iter()
                        .any(|leaf| leaf.get("viewCount").and_then(Value::as_i64).unwrap_or(0) == 0)
                }
        });

        let mut rediscover = items.clone();
        let current = now();
        rediscover.retain(|show| {
            let last = show
                .get("lastViewedAt")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            show.get("viewCount")
                .and_then(Value::as_i64)
                .is_some_and(|value| value > 0)
                && show
                    .get("viewedLeafCount")
                    .and_then(Value::as_i64)
                    .zip(show.get("leafCount").and_then(Value::as_i64))
                    .is_some_and(|(viewed, leaves)| viewed < leaves)
                && last <= current - 42 * 86_400
                && last >= current - 5 * 365 * 86_400
        });

        let (genre, genre_rows) = if !is_show {
            // Insertion-ordered, and the FIRST genre with the most rows wins, the
            // way Python's `max` over a dict picks it: a hash map's order made the
            // shelf's genre (and so its hubIdentifier) change from one read to the
            // next, and `count=1` then named a different hub than the full read.
            let mut counts: Vec<(String, (Value, Vec<Value>))> = Vec::new();
            for row in &items {
                for tag in row
                    .get("Genre")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let Some(name) = tag.get("tag").and_then(Value::as_str) else {
                        continue;
                    };
                    if name.is_empty() {
                        continue;
                    }
                    if let Some(entry) = counts.iter_mut().find(|(known, _)| known == name) {
                        entry.1 .1.push(row.clone());
                    } else {
                        counts.push((name.to_string(), (tag.clone(), vec![row.clone()])));
                    }
                }
            }
            let best = counts
                .iter()
                .map(|(_, (_, rows))| rows.len())
                .max()
                .unwrap_or(0);
            counts
                .into_iter()
                .find(|(_, (_, rows))| rows.len() == best)
                .map(|(name, (tag, rows))| {
                    let mut filtered = rows;
                    filtered.retain(|row| {
                        row.get("viewCount").and_then(Value::as_i64).unwrap_or(0) == 0
                            && row
                                .get("audienceRating")
                                .and_then(Value::as_f64)
                                .is_some_and(|value| value >= 7.0)
                    });
                    ((name, tag), filtered)
                })
                .unwrap_or_else(|| ((String::new(), Value::Null), Vec::new()))
        } else {
            ((String::new(), Value::Null), Vec::new())
        };

        let mut hubs = Vec::new();
        let mut make_shelf = |identifier: String,
                              shelf_title: String,
                              source_rows: Vec<Value>,
                              kind: &str,
                              key: String,
                              context: &str,
                              promoted: bool,
                              random: bool| {
            let total = source_rows.len();
            let mut rows = source_rows
                .into_iter()
                .map(|row| {
                    if identifier.starts_with("tv.recentlyadded.")
                        && row.get("type").and_then(Value::as_str) == Some("show")
                    {
                        self.detail_row(&row)
                    } else {
                        // Cycle 56, measured on zen and kids: `includeGuids=1` carries
                        // `Guid` on a library hub page's rows too.
                        // The oracle's `_grid_row`: a shelf row keeps its `Media` (Parts
                        // without Streams) and its librarySection fields.
                        let mut shown = self.grid_row(&row, true, false, false);
                        if include_guids {
                            let guids = row
                                .get("Guid")
                                .filter(|guids| guids.as_array().is_some_and(|list| !list.is_empty()))
                                .cloned()
                                .or_else(|| {
                                    row.get("guid")
                                        .filter(|guid| !string_value(guid).is_empty())
                                        .map(|guid| json!([{"id": guid}]))
                                });
                            if let (Some(guids), Some(object)) = (guids, shown.as_object_mut()) {
                                object.insert("Guid".to_string(), guids);
                            }
                        }
                        shown
                    }
                })
                .collect::<Vec<_>>();
            if random && rows.len() > 1 {
                // The reference holds a process-stable sample.  A stable
                // ordering is equivalent for this catalog and keeps responses
                // deterministic across clients while preserving membership.
                rows.sort_by_key(|row| key_of(row).unwrap_or_default());
            }
            // Cycle 28: a negative count is `len(rows) + count` clamped at zero, and it
            // always reports the shelf as exhausted.
            let mut exhausted = false;
            let mut limit = count.unwrap_or(6);
            if limit < 0 {
                limit = (rows.len() as i64 + limit).max(0);
                exhausted = true;
            }
            let shelf_limit = limit as usize;
            rows.truncate(shelf_limit);
            if let Some(excluded) = query.get("excludeFields") {
                let names = excluded.split(',').collect::<HashSet<_>>();
                for row in &mut rows {
                    if let Some(object) = row.as_object_mut() {
                        for name in &names {
                            object.remove(*name);
                        }
                    }
                }
            }
            let mut hub = Map::from_iter([
                ("hubIdentifier".to_string(), json!(identifier)),
                ("title".to_string(), json!(shelf_title)),
                ("type".to_string(), json!(kind)),
                ("style".to_string(), json!("shelf")),
                ("context".to_string(), json!(context)),
                ("key".to_string(), json!(key)),
                ("more".to_string(), json!(!exhausted && total > shelf_limit)),
                ("size".to_string(), json!(rows.len())),
                ("Metadata".to_string(), Value::Array(rows.clone())),
            ]);
            // The oracle carries `promoted` and `random` only on the shelves that are.
            if promoted {
                hub.insert("promoted".to_string(), json!(true));
            }
            if random {
                hub.insert("random".to_string(), json!(true));
            }
            if !rows.is_empty() {
                let keys = rows.iter().filter_map(key_of).collect::<Vec<_>>();
                if !keys.is_empty() {
                    hub.insert(
                        "hubKey".to_string(),
                        json!(format!("/library/metadata/{}", keys.join(","))),
                    );
                }
            }
            hubs.push(Value::Object(hub));
        };

        let prefix = if is_show { "tv" } else { "movie" };
        make_shelf(
            format!("{prefix}.inprogress.{section_key}"),
            "Continue Watching".to_string(),
            in_progress,
            if is_show { "episode" } else { "movie" },
            format!("/hubs/sections/{section_key}/continueWatching/items"),
            if is_show {
                "hub.tv.inprogress"
            } else {
                "hub.movie.inprogress"
            },
            false,
            false,
        );
        if is_show {
            make_shelf(
                format!("tv.recentlyaired.{section_key}"),
                "Recently Released Episodes".to_string(),
                released,
                "episode",
                format!(
                    "/library/sections/{section_key}/all?type=4&sort=originallyAvailableAt:desc"
                ),
                "hub.tv.recentlyaired",
                false,
                false,
            );
            make_shelf(
                format!("tv.recentlyadded.{section_key}"),
                format!("Recently Added in {title}"),
                added,
                "mixed",
                format!("/hubs/home/recentlyAdded?type=2&sectionID={section_key}"),
                "hub.tv.recentlyadded",
                true,
                false,
            );
            make_shelf(
                format!("tv.startwatching.{section_key}"),
                "Start Watching".to_string(),
                unstarted,
                "show",
                format!("/library/sections/{section_key}/all?type=2&viewedOrInProgressLeafCount=0"),
                "hub.tv.startwatching",
                false,
                true,
            );
            make_shelf(
                format!("tv.rediscover.{section_key}"),
                "Rediscover".to_string(),
                rediscover,
                "show",
                format!("/library/sections/{section_key}/all?type=2&lastViewedAt<=-6w&lastViewedAt>=-5y&unwatchedLeaves=1&viewCount>=1"),
                "hub.tv.rediscover",
                false,
                true,
            );
            make_shelf(
                format!("tv.toprated.{section_key}"),
                "Top Rated TV".to_string(),
                top_rated,
                "show",
                format!("/library/sections/{section_key}/all?type=2&unwatchedLeaves=1&audienceRating>=7.0&sort=audienceRating:desc"),
                "hub.tv.toprated",
                false,
                true,
            );
            make_shelf(
                format!("tv.recentlyviewed.{section_key}"),
                "Recently Watched Episodes".to_string(),
                watched,
                "episode",
                format!("/library/sections/{section_key}/all?sort=lastViewedAt:desc&unwatched=0&viewOffset=0&type=4"),
                "hub.tv.recentlyviewed",
                false,
                false,
            );
        } else {
            make_shelf(
                format!("movie.recentlyreleased.{section_key}"),
                "Recently Released Movies".to_string(),
                released,
                "movie",
                format!("/library/sections/{section_key}/all?sort=originallyAvailableAt:desc&originallyAvailableAt>=-1y"),
                "hub.movie.recentlyreleased",
                false,
                false,
            );
            make_shelf(
                format!("movie.recentlyadded.{section_key}"),
                format!("Recently Added in {title}"),
                added,
                "movie",
                format!("/library/sections/{section_key}/all?sort=addedAt:desc"),
                "hub.movie.recentlyadded",
                true,
                false,
            );
            if !genre.0.is_empty() {
                let genre_id = genre
                    .1
                    .get("id")
                    .map(string_value)
                    .filter(|id| !id.is_empty())
                    .unwrap_or_else(|| tag_id("Genre", &genre.0).to_string());
                make_shelf(
                    format!("movie.genre.{section_key}.{genre_id}"),
                    format!("Top Movies in {}", genre.0),
                    genre_rows,
                    "movie",
                    format!("/library/sections/{section_key}/all?unwatched=1&genre={genre_id}&audienceRating>=7.0"),
                    "hub.movie.genre",
                    false,
                    false,
                );
            }
            make_shelf(
                format!("movie.topunwatched.{section_key}"),
                "Top Unwatched Movies".to_string(),
                top_unwatched,
                "movie",
                format!("/library/sections/{section_key}/all?unwatched=1&audienceRating>=7.0&sort=audienceRating:desc"),
                "hub.movie.topunwatched",
                false,
                true,
            );
            make_shelf(
                format!("movie.recentlyviewed.{section_key}"),
                "Recently Watched Movies".to_string(),
                watched,
                "movie",
                format!("/library/sections/{section_key}/all?sort=lastViewedAt:desc&unwatched=0&viewOffset=0"),
                "hub.movie.recentlyviewed",
                false,
                false,
            );
        }
        if include_playlists {
            let position = hubs
                .iter()
                .position(|hub| {
                    hub.get("hubIdentifier")
                        .and_then(Value::as_str)
                        .is_some_and(|value| value.contains("recentlyadded"))
                })
                .map(|index| index + 1)
                .unwrap_or(hubs.len());
            let mut playlist = Map::from_iter([
                (
                    "hubIdentifier".to_string(),
                    json!(format!("recent.library.playlists.{section_key}")),
                ),
                ("title".to_string(), json!("Recent Playlists")),
                ("type".to_string(), json!("playlist")),
                ("style".to_string(), json!("shelf")),
                ("context".to_string(), json!("hub.recent.library.playlists")),
                (
                    "key".to_string(),
                    json!(format!("/playlists/all?type=15&playlistType=video&sort=lastViewedAt:desc&sectionID={section_key}")),
                ),
                ("promoted".to_string(), json!(false)),
                ("more".to_string(), json!(false)),
                ("size".to_string(), json!(0)),
                ("Metadata".to_string(), json!([])),
            ]);
            playlist.insert("random".to_string(), json!(false));
            hubs.insert(position, Value::Object(playlist));
        }

        let mut fields = Map::from_iter([
            ("Hub".to_string(), Value::Array(hubs)),
            ("librarySectionID".to_string(), json!(section_key)),
            ("librarySectionTitle".to_string(), json!(title)),
            ("allowSync".to_string(), json!(true)),
        ]);
        if let Some(uuid) = section.get("uuid") {
            fields.insert("librarySectionUUID".to_string(), uuid.clone());
        }
        Response::json(200, self.container(fields, true))
    }

    fn valid_content_directory(query: &HashMap<String, String>) -> bool {
        query
            .get("contentDirectoryID")
            .map(|value| {
                value.is_empty()
                    || value
                        .split(',')
                        .filter_map(|part| part.parse::<i64>().ok())
                        .any(|number| number > 0)
            })
            .unwrap_or(true)
    }

    fn route_continue_watching(&self, query: &HashMap<String, String>) -> Response {
        if !Self::valid_content_directory(query) {
            return Response::error(404, "plexsim: not found");
        }
        // Measured: this hub's identifier is `continueWatching`, its key its OWN items
        // endpoint, its style `hero`. Cycle 57: the rows are the MERGED list; cycle 53:
        // stamped `includedAt`.
        let rows = self
            .on_home(self.merged_continue_watching())
            .into_iter()
            .map(Self::with_included_at)
            .collect::<Vec<_>>();
        let hub = self.home_hub(
            "continueWatching",
            "Continue Watching",
            "/hubs/continueWatching/items",
            &rows,
            query,
            "mixed",
            "hero",
            false,
        );
        Response::json(
            200,
            self.hub_container(Map::from_iter([("Hub".to_string(), Value::Array(vec![hub]))])),
        )
    }

    fn route_continue_watching_items(&self, query: &HashMap<String, String>) -> Response {
        if !Self::valid_content_directory(query) {
            return Response::error(404, "plexsim: not found");
        }
        // Cycle 30: no resume list carries `Field`; cycle 53: `includedAt`.
        let rows = self
            .on_home(self.merged_continue_watching())
            .iter()
            .map(|row| {
                let mut entry = self.detail_row(row);
                if let Some(object) = entry.as_object_mut() {
                    object.remove("Field");
                }
                Self::with_included_at(entry)
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.hub_container(Map::from_iter([
                ("size".to_string(), json!(rows.len())),
                ("Metadata".to_string(), Value::Array(rows)),
            ])),
        )
    }

    /// A SECTION's Continue Watching see-all leaf (cycles 21, 26, 57): a show
    /// library keeps a hidden row, a movie library drops it, and a next-up whose
    /// anchor was only scrobbled is not listed.
    /// `section_continue_watching`: a show library keeps a hidden row, a movie library
    /// drops it, and a next-up whose show was never played in a session is not listed.
    fn section_continue_watching_rows(&self, section_key: &str, is_show: bool) -> Vec<Value> {
        self.continue_watching(is_show)
            .into_iter()
            .filter(|row| {
                string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section_key
                    && row.get("_nextUpFromScrobble").is_none()
            })
            .collect()
    }

    fn route_section_continue_watching_items(&self, section_key: &str) -> Response {
        let Some(section) = self.section(section_key) else {
            return Response::error(404, "plexsim: no such section");
        };
        let is_show = section.get("type").and_then(Value::as_str) == Some("show");
        let rows = self
            .section_continue_watching_rows(section_key, is_show)
            .iter()
            .map(|row| self.detail_row(row))
            .collect::<Vec<_>>();
        Response::json(
            200,
            Value::Object(Map::from_iter([(
                "MediaContainer".to_string(),
                json!({"size": rows.len(), "allowSync": true,
                       "identifier": "com.plexapp.plugins.library", "Metadata": rows}),
            )])),
        )
    }

    fn route_search(&self, query: &HashMap<String, String>) -> Response {
        let Some(raw) = query.get("query").or_else(|| query.get("term")) else {
            return Response::error(400, "plexsim: bad request");
        };
        if raw.is_empty() {
            return Response::error(
                if query.contains_key("term") { 404 } else { 400 },
                "plexsim: bad request",
            );
        }
        let needle = raw.to_lowercase();
        let matches = |kind: Option<&str>| {
            self.catalog
                .items
                .iter()
                .filter(|row| {
                    if raw.chars().count() == 1 {
                        return false;
                    }
                    kind.map(|wanted| row.get("type").and_then(Value::as_str) == Some(wanted))
                        .unwrap_or(true)
                        && {
                            let title = row
                                .get("title")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_lowercase();
                            // Global search uses word-prefix matching for the
                            // normal tier. The measured fuzzy tier accepts a
                            // short mid-word fragment (up to three characters)
                            // and leaves longer fragments unmatched.
                            let wanted = needle.split_whitespace().collect::<Vec<_>>();
                            if wanted.len() <= 1 {
                                needle.len() <= 3 && title.contains(&needle)
                                    || title
                                        .split_whitespace()
                                        .any(|word| word.starts_with(&needle))
                            } else {
                                let words = title.split_whitespace().collect::<Vec<_>>();
                                let mut cursor = 0;
                                wanted.iter().all(|term| {
                                    while cursor < words.len() && !words[cursor].starts_with(term) {
                                        cursor += 1;
                                    }
                                    let found = cursor < words.len();
                                    cursor += usize::from(found);
                                    found
                                })
                            }
                        }
                })
                .collect::<Vec<_>>()
        };
        let hubs = [
            ("show", "show"),
            ("artist", "artist"),
            ("album", "album"),
            ("episode", "episode"),
            ("movie", "movie"),
            ("track", "track"),
            ("photoalbum", "photoalbum"),
            ("autotag", "autotag"),
            ("photo", "photo"),
            ("tag", "tag"),
            ("actor", "actor"),
            ("director", "director"),
            ("genre", "genre"),
            ("collection", "collection"),
            ("playlist", "playlist"),
            ("shared", "shared"),
            ("place", "place"),
        ];
        let hub_rows = hubs
            .into_iter()
            .map(|(identifier, kind)| {
                let tag_kind = matches!(
                    kind,
                    "genre"
                        | "collection"
                        | "director"
                        | "writer"
                        | "actor"
                        | "producer"
                        | "country"
                );
                let mut directory_rows = if kind == "collection" {
                    let mut values = self
                        .search_tag_rows(raw, query.get("contentDirectoryID").map(String::as_str))
                        .into_iter()
                        .filter(|(name, _)| name == kind)
                        .map(|(_, row)| row)
                        .collect::<Vec<_>>();
                    values.extend(
                        self.collections
                            .values()
                            .filter(|collection| search_text_matches(raw, &collection.title))
                            .map(|collection| {
                                let mut row = self.dynamic_collection_row(collection);
                                if let Some(object) = row.as_object_mut() {
                                    object.insert("score".to_string(), json!("0.92000"));
                                }
                                row
                            }),
                    );
                    values
                } else if tag_kind {
                    self.search_tag_rows(raw, query.get("contentDirectoryID").map(String::as_str))
                        .into_iter()
                        .filter(|(name, _)| name == kind)
                        .map(|(_, mut row)| {
                            if let Some(object) = row.as_object_mut() {
                                let score = object
                                    .get("tag")
                                    .and_then(Value::as_str)
                                    .map(|tag| {
                                        let needle = raw.to_lowercase();
                                        let lower = tag.to_lowercase();
                                        if lower == needle {
                                            "0.92000"
                                        } else if lower
                                            .split_whitespace()
                                            .any(|word| word.starts_with(&needle))
                                        {
                                            "0.52000"
                                        } else {
                                            "0.30000"
                                        }
                                    })
                                    .unwrap_or("0.30000");
                                object.insert(
                                    "reason".to_string(),
                                    json!(format!(
                                        "{}={}",
                                        kind,
                                        object.get("id").map(string_value).unwrap_or_default()
                                    )),
                                );
                                object.insert(
                                    "reasonID".to_string(),
                                    object.get("id").cloned().unwrap_or(Value::Null),
                                );
                                object.insert(
                                    "reasonTitle".to_string(),
                                    object.get("tag").cloned().unwrap_or(Value::Null),
                                );
                                object.insert("score".to_string(), json!(score));
                            }
                            row
                        })
                        .collect::<Vec<_>>()
                } else if kind == "playlist" {
                    self.playlists
                        .values()
                        .filter(|playlist| search_text_matches(raw, &playlist.title))
                        .map(|playlist| {
                            let mut row = self.dynamic_playlist_row(playlist);
                            if let Some(object) = row.as_object_mut() {
                                object.insert("score".to_string(), json!("0.92000"));
                            }
                            row
                        })
                        .collect::<Vec<_>>()
                } else {
                    Vec::new()
                };
                let rows = matches(Some(kind))
                    .into_iter()
                    .map(|row| {
                        let mut value = self.listing_row(row);
                        let title = row.get("title").and_then(Value::as_str).unwrap_or_default();
                        let title_lower = title.to_lowercase();
                        let prefix = needle.split_whitespace().count() <= 1
                            && title_lower
                                .split_whitespace()
                                .any(|word| word.starts_with(&needle));
                        if let Some(object) = value.as_object_mut() {
                            object.remove("Guid");
                            for tag_name in
                                ["Genre", "Country", "Director", "Writer", "Producer", "Role"]
                            {
                                if let Some(tags) =
                                    object.get_mut(tag_name).and_then(Value::as_array_mut)
                                {
                                    for tag in tags {
                                        if let Some(tag_object) = tag.as_object_mut() {
                                            tag_object.remove("id");
                                        }
                                    }
                                }
                            }
                            let section_title = self
                                .section(&string_value(
                                    row.get("librarySectionID").unwrap_or(&Value::Null),
                                ))
                                .and_then(|section| section.get("title"))
                                .cloned()
                                .unwrap_or(Value::Null);
                            let old = object.clone();
                            let mut ordered = Map::new();
                            ordered.insert("librarySectionTitle".to_string(), section_title);
                            ordered.insert(
                                "score".to_string(),
                                json!(if prefix { "1.00000" } else { "0.30000" }),
                            );
                            for (key, item) in old {
                                ordered.entry(key).or_insert(item);
                            }
                            value = Value::Object(ordered);
                        }
                        value
                    })
                    .collect::<Vec<_>>();
                if !tag_kind && kind != "collection" && kind != "playlist" {
                    directory_rows.clear();
                }
                let mut values = if tag_kind || kind == "collection" || kind == "playlist" {
                    directory_rows
                } else {
                    rows
                };
                let total = values.len();
                let cap = query
                    .get("limit")
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(3);
                values.truncate(cap);
                let key_name = if tag_kind { "Directory" } else { "Metadata" };
                let hub = Map::from_iter([
                    ("hubIdentifier".to_string(), json!(identifier)),
                    ("title".to_string(), json!(identifier)),
                    ("type".to_string(), json!(kind)),
                    ("context".to_string(), json!("")),
                    ("style".to_string(), json!("shelf")),
                    ("size".to_string(), json!(values.len())),
                    ("more".to_string(), json!(total > values.len())),
                    (key_name.to_string(), Value::Array(values)),
                ]);
                Value::Object(hub)
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Hub".to_string(), Value::Array(hub_rows))]),
                false,
            ),
        )
    }

    /// Search's tag shelves are built from the live tag tables rather than the
    /// title index.  A tag is visible only when a top-level item in its
    /// library carries it, while `count` includes every matching row in that
    /// library (episodes therefore affect the count without becoming the
    /// clicked key).
    fn search_tag_rows(&self, raw: &str, section_filter: Option<&str>) -> Vec<(String, Value)> {
        let fields = [
            ("Genre", "genre", 1_i64),
            ("Collection", "collection", 2_i64),
            ("Director", "director", 4_i64),
            ("Writer", "writer", 5_i64),
            ("Role", "actor", 6_i64),
            ("Producer", "producer", 7_i64),
            ("Country", "country", 8_i64),
            ("Network", "network", 9_i64),
        ];
        let mut out = Vec::new();
        for (element, kind, tag_type) in fields {
            let mut per_section: HashMap<(String, String), (String, i64, bool, Option<String>)> =
                HashMap::new();
            for row in &self.catalog.items {
                let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
                if section_filter.is_some_and(|wanted| wanted != section) {
                    continue;
                }
                let top_level = matches!(
                    row.get("type").and_then(Value::as_str),
                    Some("movie" | "show")
                );
                for tag in row
                    .get(element)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let Some(name) = tag.get("tag").and_then(Value::as_str) else {
                        continue;
                    };
                    if !search_text_matches(raw, name) {
                        continue;
                    }
                    let id = string_value(tag.get("id").unwrap_or(&Value::Null));
                    if id.is_empty() {
                        continue;
                    }
                    let entry = per_section
                        .entry((section.clone(), id.clone()))
                        .or_insert_with(|| {
                            (
                                name.to_string(),
                                0,
                                false,
                                tag.get("tagKey")
                                    .and_then(Value::as_str)
                                    .map(str::to_string),
                            )
                        });
                    entry.1 += 1;
                    entry.2 |= top_level;
                }
            }
            for ((section, id), (name, count, visible, tag_key)) in per_section {
                if !visible {
                    continue;
                }
                let section_title = self
                    .section(&section)
                    .and_then(|value| value.get("title"))
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let section_uuid = self
                    .section(&section)
                    .and_then(|value| value.get("uuid"))
                    .cloned()
                    .unwrap_or(Value::Null);
                let mut row = Map::from_iter([
                    (
                        "librarySectionID".to_string(),
                        json!(section.parse::<i64>().unwrap_or(0)),
                    ),
                    ("librarySectionTitle".to_string(), json!(section_title)),
                    ("librarySectionUUID".to_string(), section_uuid),
                    (
                        "key".to_string(),
                        json!(format!("/library/sections/{section}/all?{kind}={id}")),
                    ),
                    ("id".to_string(), json!(id)),
                    ("filter".to_string(), json!(format!("{kind}={id}"))),
                    ("tag".to_string(), json!(name)),
                    ("tagType".to_string(), json!(tag_type)),
                ]);
                // Cycle 28, measured on kids and zen: a PERSON search tag row carries
                // its 24-hex `tagKey` directly after `tagType`, with `count` LAST, and
                // a `Genre` row carries none.
                if matches!(tag_type, 4..=7) {
                    let tag_key =
                        tag_key.unwrap_or_else(|| crate::hierarchy::person_tag_key(&name));
                    row.insert("tagKey".to_string(), json!(tag_key));
                }
                row.insert("count".to_string(), json!(count));
                out.push((kind.to_string(), Value::Object(row)));
            }
        }
        out
    }

    fn route_hub_items(&self, query: &HashMap<String, String>) -> Response {
        let Some(identifier) = query.get("identifier").filter(|value| !value.is_empty()) else {
            return Response::error(404, "plexsim: not found");
        };
        if identifier.starts_with("tv.inprogress.") || identifier == "home.playlists" {
            return Response::error(404, "plexsim: not found");
        }
        let limit = match query.get("count") {
            None => None,
            Some(value) => match value.parse::<usize>() {
                Ok(number) => Some(number),
                Err(_) => return Response::error(500, "plexsim: bad hub count"),
            },
        };
        let mut rows = Vec::new();
        if let Some((prefix, section_key)) = identifier.rsplit_once('.') {
            if section_key.parse::<i64>().is_ok()
                && (prefix.starts_with("movie.") || prefix.starts_with("tv."))
            {
                let Some(section) = self.section(section_key) else {
                    return Response::error(404, "plexsim: not found");
                };
                let is_show = section.get("type").and_then(Value::as_str) == Some("show");
                if identifier.contains("recentlyadded") {
                    let mut source = self.section_rows(section_key);
                    source.sort_by(|left, right| {
                        right
                            .get("addedAt")
                            .and_then(Value::as_i64)
                            .unwrap_or(0)
                            .cmp(&left.get("addedAt").and_then(Value::as_i64).unwrap_or(0))
                    });
                    rows = source
                        .iter()
                        .map(|row| {
                            if is_show {
                                self.detail_row(row)
                            } else {
                                self.listing_row(row)
                            }
                        })
                        .collect();
                } else if identifier.contains("inprogress") {
                    rows = self
                        .on_deck_rows()
                        .into_iter()
                        .filter(|row| {
                            string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                                == section_key
                        })
                        .collect();
                } else {
                    return Response::error(404, "plexsim: not found");
                }
            }
        }
        if rows.is_empty() && identifier.starts_with("home.") {
            let mut scoped = query.clone();
            scoped.insert("identifier".to_string(), identifier.clone());
            let source = self.route_hubs(&scoped);
            if let Ok(value) = serde_json::from_slice::<Value>(&source.body) {
                rows = value
                    .get("MediaContainer")
                    .and_then(|container| container.get("Hub"))
                    .and_then(Value::as_array)
                    .and_then(|hubs| hubs.first())
                    .and_then(|hub| hub.get("Metadata"))
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
            }
        }
        if rows.is_empty() && !identifier.contains("recentlyadded") {
            return Response::error(404, "plexsim: not found");
        }
        if let Some(limit) = limit {
            rows.truncate(limit);
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("allowSync".to_string(), json!(true)),
                    ("Metadata".to_string(), Value::Array(rows.clone())),
                ]),
                true,
            ),
        )
    }

    fn route_library_search(&self, query: &HashMap<String, String>) -> Response {
        let Some(raw) = query.get("query") else {
            return Response::error(400, "plexsim: bad request");
        };
        if raw.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        let allowed_types = query
            .get("searchTypes")
            .map(|value| value.split(',').map(str::to_string).collect::<HashSet<_>>());
        let section_filter = query
            .get("contentDirectoryID")
            .or_else(|| query.get("sectionID"))
            .map(String::as_str);
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                search_text_matches(raw, row.get("title").and_then(Value::as_str).unwrap_or(""))
                    && allowed_types.as_ref().map_or(true, |types| {
                        types.contains(row.get("type").and_then(Value::as_str).unwrap_or_default())
                    })
                    && section_filter.is_none_or(|section| {
                        string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
                    })
            })
            .map(|row| {
                let mut projected = self.listing_row(row);
                if let Some(object) = projected.as_object_mut() {
                    object.remove("librarySectionID");
                    object.remove("librarySectionKey");
                    object.remove("Guid");
                    for tag_name in ["Genre", "Country", "Director", "Writer", "Producer", "Role"] {
                        if let Some(tags) = object.get_mut(tag_name).and_then(Value::as_array_mut) {
                            for tag in tags {
                                if let Some(tag_object) = tag.as_object_mut() {
                                    tag_object.remove("id");
                                }
                            }
                        }
                    }
                    let title = self
                        .section(&string_value(
                            row.get("librarySectionID").unwrap_or(&Value::Null),
                        ))
                        .and_then(|section| section.get("title"))
                        .cloned()
                        .unwrap_or(Value::Null);
                    let old = object.clone();
                    let mut ordered = Map::new();
                    ordered.insert("librarySectionTitle".to_string(), title);
                    for (key, item) in old {
                        ordered.entry(key).or_insert(item);
                    }
                    projected = Value::Object(ordered);
                }
                json!({"score":1.0,"Metadata":projected})
            })
            .collect::<Vec<_>>();
        let tag_rows = self
            .search_tag_rows(raw, section_filter)
            .into_iter()
            .map(|(_, mut row)| {
                if let Some(object) = row.as_object_mut() {
                    object.remove("reason");
                    object.remove("reasonID");
                    object.remove("reasonTitle");
                    object.remove("score");
                }
                json!({"score":0.52,"Directory":row})
            })
            .collect::<Vec<_>>();
        rows.extend(tag_rows);
        rows.extend(
            self.collections
                .values()
                .filter(|collection| search_text_matches(raw, &collection.title))
                .map(|collection| json!({"score":0.92,"Metadata":self.dynamic_collection_row(collection)})),
        );
        rows.extend(
            self.playlists
                .values()
                .filter(|playlist| search_text_matches(raw, &playlist.title))
                .map(
                    |playlist| json!({"score":0.92,"Metadata":self.dynamic_playlist_row(playlist)}),
                ),
        );
        if let Some(limit) = query
            .get("limit")
            .and_then(|value| value.parse::<usize>().ok())
        {
            rows.truncate(limit);
        } else {
            rows.truncate(10);
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([("SearchResult".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn route_legacy_search(&self, query: &HashMap<String, String>) -> Response {
        let Some(raw) = query.get("query") else {
            return Response::error(400, "plexsim: bad request");
        };
        if raw.is_empty() {
            return Response::error(404, "plexsim: not found");
        }
        let needle = raw.to_lowercase();
        let rows = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                row.get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase()
                    .contains(&needle)
            })
            .map(|row| {
                let mut value = self.listing_row(row);
                if let Some(object) = value.as_object_mut() {
                    object.insert("personal".to_string(), json!(true));
                    object.insert("sourceTitle".to_string(), json!(self.friendly));
                }
                value
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("size".to_string(), json!(4 + rows.len())),
                    (
                        "Provider".to_string(),
                        json!([{"key":"movie"},{"key":"show"},{"key":"episode"},{"key":"person"}]),
                    ),
                    ("Metadata".to_string(), Value::Array(rows)),
                ]),
                false,
            ),
        )
    }

    fn route_playlists(&self, query: &HashMap<String, String>) -> Response {
        self.route_playlists_on(query, false)
    }

    fn route_playlists_on(&self, query: &HashMap<String, String>, listing_all: bool) -> Response {
        if let Some(value) = query.get("type") {
            let Ok(kind) = value.parse::<i64>() else {
                return Response::error(500, "plexsim: unsupported playlist type");
            };
            if kind != 15 && kind != 42 {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                );
            }
        }
        if query.get("type").is_some_and(|value| value == "42") {
            let row = json!({
                "ratingKey": "39999",
                "key": "/playlists/39999/items",
                "guid": "com.plexapp.agents.none://55d3184c-28b9-43ce-842c-b7b03ca00e0f",
                "type": "playlist",
                "title": "Background Processing List",
                "summary": "",
                "playlistType": "backgroundProcessing",
            });
            return Response::json(
                200,
                self.container(
                    Map::from_iter([("Metadata".to_string(), json!([row]))]),
                    false,
                ),
            );
        }
        if let Some(status) = listing::listing_refusals(query, &[]) {
            return Response::error(status, "plexsim: bad request");
        }
        let mut rows = self
            .playlists
            .values()
            .filter(|playlist| {
                query
                    .get("title")
                    .map(|title| playlist.title == *title)
                    .unwrap_or(true)
            })
            .filter(|playlist| {
                query
                    .get("smart")
                    .map(|smart| (smart == "1") == playlist.smart)
                    .unwrap_or(true)
            })
            .filter(|playlist| {
                let Some(section) = query.get("sectionID") else {
                    return true;
                };
                self.playlist_keys(playlist).iter().any(|key| {
                    self.catalog
                        .get(key)
                        .map(|row| {
                            string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                                == *section
                        })
                        .unwrap_or(false)
                })
            })
            .filter(|playlist| {
                let own = self
                    .playlist_columns(&playlist.id)
                    .get("playlistType")
                    .map(string_value)
                    .filter(|kind| !kind.is_empty())
                    .unwrap_or_else(|| "video".to_string());
                query
                    .get("playlistType")
                    .map(|value| value.split(',').any(|kind| kind == own))
                    .unwrap_or(true)
            })
            .map(|p| self.playlist_row(p, false, true))
            .collect::<Vec<_>>();
        // Cycle 16: the SMART lists come first, then `titleSort` within each group,
        // and every tie under an explicit sort falls back to the same.
        rows.sort_by(|left, right| {
            let smart_left = left.get("smart").and_then(Value::as_bool).unwrap_or(false);
            let smart_right = right.get("smart").and_then(Value::as_bool).unwrap_or(false);
            let sort_of = |row: &Value| {
                row.get("titleSort")
                    .or_else(|| row.get("title"))
                    .map(string_value)
                    .unwrap_or_default()
            };
            smart_right.cmp(&smart_left).then_with(|| sort_of(left).cmp(&sort_of(right)))
        });
        let spec = query.get("sort").cloned().unwrap_or_default();
        if spec.split(':').next() == Some("mediaCount") {
            // Cycle 16: `mediaCount` orders by the number of items -- the sixth sort
            // this endpoint's own Meta advertises.
            let descending = spec.ends_with(":desc");
            let count = |row: &Value| row.get("leafCount").and_then(Value::as_i64).unwrap_or(0);
            rows.sort_by(|left, right| {
                if descending {
                    count(right).cmp(&count(left))
                } else {
                    count(left).cmp(&count(right))
                }
            });
        } else if !spec.is_empty() {
            rows = match self.sort_rows(rows, Some(&spec)) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: unsupported sort key"),
            };
        }
        let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
        if query
            .get("includeMeta")
            .is_some_and(|value| is_true_option(value))
        {
            fields.insert(
                "Meta".to_string(),
                self.playlist_meta(query, listing_all),
            );
        }
        page_fields(&mut fields, query);
        Response::json(200, self.container(fields, false))
    }

    fn route_playlist_all(&self, query: &HashMap<String, String>) -> Response {
        self.route_playlists_on(query, true)
    }

    /// Cycle 56: the playlist Meta shaped by the request, measured on kids, zen and
    /// plexscratch42.  The Type `key` carries the request's `playlistType`,
    /// `sectionID` and `type` -- alphabetical, URL-encoded -- and nothing else.  On
    /// `/playlists/all` NO Type and NO Sort is active; on `/playlists` the Type is
    /// active and the active Sort is the requested one (Name ascending by default).
    fn playlist_meta(&self, query: &HashMap<String, String>, listing_all: bool) -> Value {
        let carried = ["playlistType", "sectionID", "type"]
            .iter()
            .filter_map(|name| {
                query
                    .get(*name)
                    .filter(|value| !value.is_empty())
                    .map(|value| format!("{name}={}", percent_encode(value)))
            })
            .collect::<Vec<_>>();
        let key = if carried.is_empty() {
            "/playlists".to_string()
        } else {
            format!("/playlists?{}", carried.join("&"))
        };
        let mut sorts = self.playlist_sort_rows();
        for sort in &mut sorts {
            if let Some(object) = sort.as_object_mut() {
                object.remove("active");
                object.remove("activeDirection");
            }
        }
        if !listing_all {
            let terms = crate::filters::sort_terms(
                query.get("sort").map(String::as_str).filter(|sort| !sort.is_empty()).unwrap_or("titleSort"),
            );
            let (wanted, descending) = terms
                .first()
                .map(|(key, descending, _)| (key.clone(), *descending))
                .unwrap_or(("titleSort".to_string(), false));
            for sort in &mut sorts {
                if sort.get("key").and_then(Value::as_str) == Some(wanted.as_str()) {
                    if let Some(object) = sort.as_object_mut() {
                        object.insert("active".to_string(), json!(true));
                        object.insert(
                            "activeDirection".to_string(),
                            json!(if descending { "desc" } else { "asc" }),
                        );
                    }
                }
            }
        }
        json!({"Type": [{"key": key, "type": "playlist", "title": "All",
                         "active": !listing_all, "Sort": sorts}]})
    }

    fn playlist_sort_rows(&self) -> Vec<Value> {
        vec![
            json!({"active":true,"activeDirection":"asc","default":"asc","defaultDirection":"asc","descKey":"titleSort:desc","key":"titleSort","title":"Name"}),
            json!({"defaultDirection":"desc","descKey":"viewCount:desc","key":"viewCount","title":"Plays"}),
            json!({"defaultDirection":"desc","descKey":"lastViewedAt:desc","key":"lastViewedAt","title":"Last Played"}),
            json!({"defaultDirection":"desc","descKey":"duration:desc","key":"duration","title":"Duration"}),
            json!({"defaultDirection":"desc","descKey":"addedAt:desc","key":"addedAt","title":"Date Added"}),
            json!({"defaultDirection":"desc","descKey":"mediaCount:desc","key":"mediaCount","title":"Item Count"}),
        ]
    }

    fn route_collections(&self, _query: &HashMap<String, String>) -> Response {
        let rows = self
            .collections
            .values()
            .map(|c| self.dynamic_collection_row(c))
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn route_section_collections(
        &self,
        section_key: &str,
        query: &HashMap<String, String>,
    ) -> Response {
        let type_number = match query.get("type") {
            None => Some(18),
            // Cycle 28: the leaf is the `/all` listing with a DEFAULT `type=18`, so an
            // empty `type=` is that listing's unparseable type -- a 500.
            Some(value) => match value.parse::<i64>() {
                Ok(number) => Some(number),
                Err(_) => return Response::error(500, "plexsim: bad collection type"),
            },
        };
        if let Some(limit) = query.get("limit") {
            if limit.parse::<usize>().is_err() {
                return Response::error(500, "plexsim: bad collection limit");
            }
        }
        let rows = if type_number == Some(18) {
            // Cycle 28: the same `sort=` refusals the grid makes -- an unknown key is a
            // 500 -- over the collections in titleSort order.
            match self.section_collection_rows(section_key, &[], query.get("sort").map(String::as_str)) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: unsupported sort"),
            }
        } else {
            self.choose_rows(Some(section_key), type_number)
                .into_iter()
                .map(|row| self.section_listing_row(&row))
                .collect::<Vec<_>>()
        };
        let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
        if self
            .section(section_key)
            .and_then(|section| section.get("type"))
            .and_then(Value::as_str)
            == Some("show")
        {
            fields.insert("nocache".to_string(), json!(true));
        }
        if query.get("includeMeta").map(String::as_str) == Some("1") && type_number == Some(18) {
            // Cycle 46: the FULL section descriptor -- one `Type` row for the collection
            // view with a single `titleSort` Sort scoped `?type=18`, plus the section's
            // own `FieldType` rows.
            let kind = self
                .section(section_key)
                .and_then(|s| s.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("movie")
                .to_string();
            let field_types = self
                .section_meta(section_key, &kind, false)
                .and_then(|meta| meta.get("FieldType").cloned())
                .unwrap_or_else(|| json!([]));
            fields.insert(
                "Meta".to_string(),
                json!({
                    "Type": [{
                        "key": format!("/library/sections/{section_key}/all?type=18"),
                        "type": "collection",
                        "title": "Collections",
                        "active": true,
                        "Sort": [{
                            "active": true, "activeDirection": "asc", "default": "asc",
                            "defaultDirection": "asc", "descKey": "titleSort:desc",
                            "key": "titleSort", "title": "Title",
                            "firstCharacterKey": format!("/library/sections/{section_key}/firstCharacter?type=18"),
                        }],
                    }],
                    "FieldType": field_types,
                }),
            );
        }
        page_fields(&mut fields, query);
        Response::json(200, self.container(fields, true))
    }

    /// Whether a `server://` uri names a machine that is not this one.
    fn foreign_uri(&self, uri: &str) -> bool {
        let decoded = percent_decode(uri);
        decoded
            .strip_prefix("server://")
            .map(|rest| rest.split('/').next().unwrap_or(""))
            .is_some_and(|machine| !machine.is_empty() && machine != self.machine)
    }

    fn queue_rows_for_query(&self, query: &HashMap<String, String>) -> Vec<Value> {
        if let Some(playlist_id) = query.get("playlistID") {
            return self
                .playlists
                .get(playlist_id)
                .map(|playlist| {
                    self.playlist_keys(playlist)
                        .iter()
                        .filter_map(|key| self.catalog.get(key).cloned())
                        .collect()
                })
                .unwrap_or_default();
        }
        let mut rows = self.rows_for_uri(query.get("uri").map(String::as_str).unwrap_or(""));
        let mut expanded = Vec::new();
        for row in rows.drain(..) {
            match row.get("type").and_then(Value::as_str) {
                Some("show" | "season") => {
                    if let Some(key) = key_of(&row) {
                        expanded.extend(self.leaves(&key));
                    }
                }
                _ => expanded.push(row),
            }
        }
        expanded
    }

    fn queue_container(&self, queue: &Queue, query: &HashMap<String, String>) -> Response {
        let mut metadata = queue
            .items
            .iter()
            .filter_map(|item| self.catalog.get(&item.rating_key).map(|row| (item, row)))
            .map(|(item, row)| {
                let mut value = self.detail_row(row);
                if let Some(object) = value.as_object_mut() {
                    object.insert("playQueueItemID".to_string(), json!(item.id));
                }
                value
            })
            .collect::<Vec<_>>();
        let total = metadata.len();
        let mut selected_index = queue
            .selected
            .and_then(|selected| queue.items.iter().position(|item| item.id == selected))
            .unwrap_or(0);
        if let Some(center) = query
            .get("center")
            .and_then(|value| value.parse::<i64>().ok())
        {
            if let Some(index) = queue.items.iter().position(|item| item.id == center) {
                selected_index = index;
            }
        }
        if query.contains_key("includeBefore") || query.contains_key("includeAfter") {
            let before = query.get("includeBefore").map(String::as_str) != Some("0");
            let after = query.get("includeAfter").map(String::as_str) != Some("0");
            let span = query
                .get("window")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(total);
            let low = selected_index.saturating_sub(span);
            let high = (selected_index + span + 1).min(metadata.len());
            let mut sliced = Vec::new();
            if before {
                sliced.extend(
                    metadata[low..selected_index.min(metadata.len())]
                        .iter()
                        .cloned(),
                );
            }
            if before && after && selected_index < metadata.len() {
                sliced.push(metadata[selected_index].clone());
            }
            if after && selected_index.saturating_add(1) < high {
                sliced.extend(metadata[selected_index + 1..high].iter().cloned());
            }
            metadata = sliced;
            selected_index = selected_index.saturating_sub(low);
        } else if let Some(window) = query
            .get("window")
            .and_then(|value| value.parse::<usize>().ok())
        {
            let low = selected_index.saturating_sub(window);
            let high = (selected_index + window + 1).min(metadata.len());
            metadata = metadata[low..high].to_vec();
            selected_index = selected_index.saturating_sub(low);
        }
        let mut fields = Map::from_iter([
            ("Metadata".to_string(), Value::Array(metadata.clone())),
            (
                "playQueueID".to_string(),
                json!(queue.id.parse::<i64>().unwrap_or(0)),
            ),
            ("playQueueVersion".to_string(), json!(queue.version)),
            ("playQueueShuffled".to_string(), json!(queue.shuffled)),
            (
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            ),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
        ]);
        if let Some(playlist_id) = &queue.playlist_id {
            fields.insert(
                "playQueuePlaylistID".to_string(),
                json!(playlist_id.parse::<i64>().unwrap_or(0)),
            );
            fields.insert(
                "playQueuePlaylistTitle".to_string(),
                json!(queue.playlist_title.clone().unwrap_or_default()),
            );
        } else if !queue.source.is_empty() {
            fields.insert("playQueueSourceURI".to_string(), json!(queue.source));
        }
        if !metadata.is_empty() {
            let selected = queue.selected.unwrap_or(queue.items[0].id);
            fields.insert("playQueueSelectedItemID".to_string(), json!(selected));
            fields.insert(
                "playQueueSelectedItemOffset".to_string(),
                json!(selected_index),
            );
            if let Some(item) = queue.items.iter().find(|item| item.id == selected) {
                fields.insert(
                    "playQueueSelectedMetadataItemID".to_string(),
                    json!(item.rating_key),
                );
            }
            fields.insert("playQueueTotalCount".to_string(), json!(total));
        }
        if let Some(last_added) = queue.last_added {
            fields.insert("playQueueLastAddedItemID".to_string(), json!(last_added));
        }
        Response::json(200, self.container(fields, false))
    }

    fn route_play_queue(
        &mut self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if segments.is_empty() {
            if request.method != "POST" {
                return Response::error(400, "plexsim: bad request");
            }
            if query.get("type").is_none()
                || (query.get("uri").is_none() && query.get("playlistID").is_none())
            {
                return Response::error(400, "plexsim: bad request");
            }
            for name in ["shuffle", "repeat"] {
                if let Some(value) = query.get(name) {
                    if !value.starts_with('+') && !value.chars().all(|ch| ch.is_ascii_digit()) {
                        return Response::error(400, "plexsim: bad request");
                    }
                    if value.trim_start_matches('+').parse::<i64>().ok() != Some(0)
                        && value.trim_start_matches('+').parse::<i64>().ok() != Some(1)
                    {
                        return Response::error(400, "plexsim: bad request");
                    }
                }
            }
            let mut rows = self.queue_rows_for_query(query);
            let natural = rows.iter().filter_map(key_of).collect::<Vec<_>>();
            let shuffled = query
                .get("shuffle")
                .is_some_and(|value| value.trim_start_matches('+') == "1");
            if shuffled && rows.len() > 1 {
                let seed = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|duration| duration.as_nanos())
                    .unwrap_or_default();
                rows.sort_by_key(|row| {
                    bytes_digest(format!("{seed}/{}", key_of(row).unwrap_or_default()).as_bytes())
                });
            }
            let client = request
                .headers
                .get("x-plex-client-identifier")
                .cloned()
                .unwrap_or_default();
            if !client.is_empty() {
                self.queues.retain(|_, queue| queue.client != client);
            }
            let id = self.next_queue.to_string();
            self.next_queue += 1;
            let mut items = Vec::new();
            for row in rows {
                let Some(key) = key_of(&row) else { continue };
                items.push(QueueItem {
                    id: self.next_queue_item,
                    rating_key: key,
                    added: false,
                });
                self.next_queue_item += 1;
            }
            let wanted = query.get("key").map(|value| extract_key(value));
            let selected = wanted
                .and_then(|wanted| {
                    items
                        .iter()
                        .find(|item| item.rating_key == wanted)
                        .map(|item| item.id)
                })
                .or_else(|| items.first().map(|item| item.id));
            let playlist_id = query.get("playlistID").cloned().or_else(|| {
                let uri = percent_decode(query.get("uri").map(String::as_str).unwrap_or(""));
                uri.split("/playlists/")
                    .nth(1)
                    .and_then(|tail| tail.split('/').next())
                    .filter(|id| self.playlists.contains_key(*id))
                    .map(str::to_string)
            });
            let playlist_title = playlist_id
                .as_ref()
                .and_then(|value| self.playlists.get(value))
                .map(|playlist| playlist.title.clone());
            let queue = Queue {
                id: id.clone(),
                version: 1,
                selected,
                items,
                source: if playlist_id.is_some() {
                    String::new()
                } else if self.foreign_uri(query.get("uri").map(String::as_str).unwrap_or("")) {
                    // Measured on a writable copy of the reference: a uri naming
                    // ANOTHER server builds an EMPTY queue that echoes the uri verbatim.
                    query.get("uri").cloned().unwrap_or_default()
                } else {
                    source_uri_for(query.get("uri").map(String::as_str).unwrap_or(""))
                },
                natural,
                shuffled,
                last_added: None,
                client,
                playlist_id,
                playlist_title,
            };
            let response = self.queue_container(&queue, query);
            self.queues.insert(id, queue);
            return response;
        }
        let id = segments[0];
        if id
            .parse::<i64>()
            .map_or(true, |value| value > i32::MAX as i64)
        {
            return Response::error(400, "plexsim: bad request");
        }
        let Some(mut queue) = self.queues.remove(id) else {
            return Response::error(404, "plexsim: no such play queue");
        };
        for name in ["includeBefore", "includeAfter"] {
            if let Some(value) = query.get(name) {
                if value != "0" && value != "1" {
                    self.queues.insert(id.to_string(), queue);
                    return Response::error(400, "plexsim: bad request");
                }
            }
        }
        if query.get("includeBefore") == Some(&"0".to_string())
            && query.get("includeAfter") == Some(&"0".to_string())
        {
            self.queues.insert(id.to_string(), queue);
            return Response::error(400, "plexsim: bad request");
        }
        if query.get("own") == Some(&"1".to_string()) {
            let client = request
                .headers
                .get("x-plex-client-identifier")
                .cloned()
                .unwrap_or_default();
            if !client.is_empty() && queue.client != client {
                self.queues.retain(|_, known| known.client != client);
                queue.client = client;
                queue.version += 1;
            }
        }
        let response = if segments.len() == 1 {
            match request.method.as_str() {
                "GET" | "HEAD" => self.queue_container(&queue, query),
                "PUT" => {
                    let uri = query.get("uri").cloned().unwrap_or_default();
                    let playlist_id = query.get("playlistID").cloned().unwrap_or_default();
                    if uri.is_empty() && playlist_id.is_empty() {
                        // Measured: a bare refresh PUT with no uri is a 400.
                        self.queues.insert(id.to_string(), queue);
                        return Response::error(400, "plexsim: bad request");
                    }
                    let rows = if uri.is_empty() {
                        // Measured on a writable copy of the reference: `playlistID=`
                        // adds that playlist's items as a plain add.
                        self.playlists
                            .get(&playlist_id)
                            .map(|playlist| {
                                self.playlist_keys(playlist)
                                    .iter()
                                    .filter_map(|key| self.catalog.get(key).cloned())
                                    .collect::<Vec<_>>()
                            })
                            .unwrap_or_default()
                    } else if self.foreign_uri(&uri) {
                        Vec::new()
                    } else {
                        self.rows_for_uri(&uri)
                    };
                    if rows.is_empty() {
                        // Measured on the copy: an add that adds nothing answers the
                        // queue as it stands, `playQueueVersion` unmoved -- the client
                        // discards a response whose version it does not expect.
                        let response = self.queue_container(&queue, query);
                        self.queues.insert(id.to_string(), queue);
                        return response;
                    }
                    let at = queue
                        .selected
                        .and_then(|selected| {
                            queue.items.iter().position(|item| item.id == selected)
                        })
                        .map(|index| index + 1)
                        .unwrap_or(queue.items.len());
                    let at = if query.get("next") == Some(&"1".to_string()) {
                        at
                    } else {
                        let mut position = at;
                        while position < queue.items.len() && queue.items[position].added {
                            position += 1;
                        }
                        position
                    };
                    let mut added_ids = Vec::new();
                    for row in rows {
                        let Some(key) = key_of(&row) else { continue };
                        let item = QueueItem {
                            id: self.next_queue_item,
                            rating_key: key,
                            added: true,
                        };
                        self.next_queue_item += 1;
                        added_ids.push(item.id);
                        // Keep a multi-item insertion in request order.
                        let insert_at = (at + added_ids.len() - 1).min(queue.items.len());
                        queue.items.insert(insert_at, item);
                    }
                    if !added_ids.is_empty() {
                        // Cycle 57: the marker is the tail of the Up Next BLOCK, and
                        // ANY add opens it.
                        queue.last_added = queue_block_tail(&queue);
                        queue.version += 1;
                    }
                    self.queue_container(&queue, query)
                }
                _ => Response::error(404, "plexsim: not found"),
            }
        } else if segments.len() == 2 && segments[1] == "shuffle" {
            if request.method != "PUT" || queue.last_added.is_some() {
                Response::error(404, "plexsim: not found")
            } else {
                queue.shuffled = true;
                let selected_id = queue.selected;
                let selected_key = selected_id.and_then(|id| {
                    queue
                        .items
                        .iter()
                        .find(|item| item.id == id)
                        .map(|item| item.rating_key.clone())
                });
                let mut shuffled = Vec::new();
                if let Some(selected_key) = selected_key {
                    if let Some(item) = queue
                        .items
                        .iter()
                        .find(|item| item.rating_key == selected_key)
                    {
                        shuffled.push(item.clone());
                    }
                    let mut rest = queue
                        .items
                        .iter()
                        .filter(|item| item.rating_key != selected_key)
                        .cloned()
                        .collect::<Vec<_>>();
                    rest.reverse();
                    shuffled.extend(rest);
                } else {
                    shuffled = queue.items.clone();
                    shuffled.reverse();
                }
                for item in shuffled.iter_mut().skip(1) {
                    item.id = self.next_queue_item;
                    self.next_queue_item += 1;
                }
                queue.items = shuffled;
                queue.version += 1;
                self.queue_container(&queue, query)
            }
        } else if segments.len() == 2 && segments[1] == "unshuffle" {
            if request.method != "PUT" || queue.last_added.is_some() {
                Response::error(404, "plexsim: not found")
            } else {
                queue.shuffled = false;
                let selected = queue.selected;
                let mut items = Vec::new();
                for key in &queue.natural {
                    if let Some(item) = queue.items.iter().find(|item| &item.rating_key == key) {
                        items.push(item.clone());
                    }
                }
                for item in items.iter_mut() {
                    if Some(item.id) != selected {
                        item.id = self.next_queue_item;
                        self.next_queue_item += 1;
                    }
                }
                queue.items = items;
                queue.selected = selected;
                queue.version += 1;
                self.queue_container(&queue, query)
            }
        } else if segments.len() == 2 && segments[1] == "items" && request.method == "DELETE" {
            // Measured on a writable copy of the reference: DELETE on the items clears
            // the queue down to its SELECTED item and steps the version.
            let selected = queue.selected;
            queue.items.retain(|item| Some(item.id) == selected);
            queue.version += 1;
            self.queue_container(&queue, query)
        } else if segments.len() == 3 && segments[1] == "items" && request.method == "DELETE" {
            // Cycle 16: EVERY item delete answers the queue as it stood BEFORE it -- the
            // old version and total -- with the rows that REMAIN; the SELECTED one also
            // drops the rows entirely and keeps the old selection.  The next read has
            // the stepped version and the selection on the item that followed.
            let index = segments[2]
                .parse::<i64>()
                .ok()
                .and_then(|item_id| queue.items.iter().position(|item| item.id == item_id));
            if let Some(index) = index {
                let item_id = queue.items[index].id;
                let was_selected = queue.selected == Some(item_id);
                let was_id = queue.selected;
                queue.items.remove(index);
                queue.version += 1;
                if was_selected {
                    queue.selected = queue
                        .items
                        .get(index)
                        .or_else(|| queue.items.last())
                        .map(|item| item.id);
                }
                let was_marker = queue.last_added;
                if was_marker.is_some() {
                    queue.last_added = queue_block_tail(&queue);
                }
                let remaining = queue.items.len();
                let mut response = self.queue_container(&queue, query);
                if let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) {
                    if let Some(object) = document
                        .get_mut("MediaContainer")
                        .and_then(Value::as_object_mut)
                    {
                        object.insert("playQueueVersion".to_string(), json!(queue.version - 1));
                        object.insert("playQueueTotalCount".to_string(), json!(remaining + 1));
                        if let Some(marker) = was_marker {
                            object.insert("playQueueLastAddedItemID".to_string(), json!(marker));
                        }
                        if was_selected {
                            object.remove("Metadata");
                            object.insert("size".to_string(), json!(0));
                            if let Some(was_id) = was_id {
                                object.insert("playQueueSelectedItemID".to_string(), json!(was_id));
                            }
                        }
                    }
                    response.body = serde_json::to_vec(&document).unwrap_or_default();
                }
                response
            } else {
                Response::error(404, "plexsim: no such play queue item")
            }
        } else if segments.len() == 4
            && segments[1] == "items"
            && segments[3] == "move"
            && request.method == "PUT"
        {
            let Some(item_id) = segments[2].parse::<i64>().ok() else {
                self.queues.insert(id.to_string(), queue);
                return Response::error(404, "plexsim: no such play queue item");
            };
            let Some(index) = queue.items.iter().position(|item| item.id == item_id) else {
                self.queues.insert(id.to_string(), queue);
                return Response::error(404, "plexsim: no such play queue item");
            };
            if query.get("after").is_some_and(String::is_empty) {
                self.queues.insert(id.to_string(), queue);
                return Response::error(400, "plexsim: bad request");
            }
            if query
                .get("after")
                .and_then(|value| value.parse::<i64>().ok())
                == Some(item_id)
            {
                self.queues.insert(id.to_string(), queue);
                return Response::error(404, "plexsim: no such play queue item");
            }
            let item = queue.items.remove(index);
            let after = query
                .get("after")
                .and_then(|value| value.parse::<i64>().ok());
            let position = after
                .and_then(|value| {
                    queue
                        .items
                        .iter()
                        .position(|entry| entry.id == value)
                        .map(|i| i + 1)
                })
                .unwrap_or(0);
            queue.items.insert(position, item);
            // Cycle 57: a move to just behind the selected item or a block member JOINS
            // the Up Next block; a move anywhere else leaves it.
            if Some(item_id) != queue.selected {
                let joins = position > 0 && {
                    let before = &queue.items[position - 1];
                    Some(before.id) == queue.selected || before.added
                };
                queue.items[position].added = joins;
            }
            // Cycle 57: the marker is the tail of the Up Next block.
            if queue.last_added.is_some() {
                queue.last_added = queue_block_tail(&queue);
            }
            queue.version += 1;
            self.queue_container(&queue, query)
        } else {
            Response::error(404, "plexsim: not found")
        };
        self.queues.insert(id.to_string(), queue);
        response
    }

    fn route_download_queue(&mut self, segments: &[&str], request: &Request) -> Response {
        let client = request
            .headers
            .get("x-plex-client-identifier")
            .cloned()
            .unwrap_or_default();
        if segments.is_empty() {
            if request.method == "POST" {
                let queue = self
                    .download_queues
                    .entry(client.clone())
                    .or_insert_with(|| {
                        json!({
                            "id": 1,
                            "clientIdentifier": client,
                            "status": "done"
                        })
                    })
                    .clone();
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("DownloadQueue".to_string(), json!([queue]))]),
                        false,
                    ),
                );
            }
            if request.method == "GET" || request.method == "HEAD" {
                return Response::json(200, plugin_container(Vec::new()));
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.len() == 2 && segments[1] == "items" && request.method == "GET" {
            if segments[0].parse::<i64>().is_err() {
                return Response::error(404, "plexsim: not found");
            }
            return Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            );
        }
        if segments.len() != 1 || segments[0].parse::<i64>().is_err() {
            return Response::error(404, "plexsim: not found");
        }
        let Some(queue) = self.download_queues.get(&client).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        match request.method.as_str() {
            "GET" | "HEAD" => Response::json(
                200,
                self.container(
                    Map::from_iter([("DownloadQueue".to_string(), json!([queue]))]),
                    false,
                ),
            ),
            "DELETE" => Response::error(404, "plexsim: not found"),
            _ => Response::error(404, "plexsim: not found"),
        }
    }

    fn route_dynamic(
        &mut self,
        request: &Request,
        path: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        let segments = path
            .split('/')
            .filter(|segment| !segment.is_empty())
            .collect::<Vec<_>>();
        if segments.starts_with(&["media", "providers", "metadata"]) {
            return self.route_metadata_providers(&segments, query, request);
        }
        if path == "/:/viewChange" {
            // Cycle 45: a routed state-write leaf that answers its own 400 to every verb.
            return Response::error(400, "plexsim: bad request");
        }
        if segments.len() == 4 && segments[0] == "hubs" && segments[1] == "sections" && segments[3] == "newReleases" {
            // Cycle 45: `{size: 0, allowSync: false, identifier}` and nothing more.
            return Response::json(200, json!({"MediaContainer": {"size": 0, "allowSync": false,
                "identifier": "com.plexapp.plugins.library"}}));
        }
        if segments.first() == Some(&"web") {
            return self.route_web(&segments[1..], request.method == "HEAD", path.ends_with('/'));
        }
        if path.starts_with("/:/resources/") {
            return self.route_bundled_resource(
                path.trim_start_matches("/:/resources/"),
                request.method == "HEAD",
            );
        }
        if path == "/neighborhood/devices" && matches!(request.method.as_str(), "GET" | "HEAD") {
            return Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            );
        }
        if path == "/media/subscriptions/template" && request.method == "GET" {
            return Response::error(400, "plexsim: bad request");
        }
        if path == "/services/defaultLibraries" && request.method == "GET" {
            return Response::error(400, "plexsim: bad request");
        }
        if path == "/library/all" && !matches!(request.method.as_str(), "GET" | "HEAD") {
            return Response::error(404, "plexsim: not found");
        }
        if path == "/actions/removeFromContinueWatching" {
            if request.method != "PUT" {
                return Response::error(404, "plexsim: not found");
            }
            let Some(key) = query.get("ratingKey").map(|value| extract_key(value)) else {
                return Response::error(400, "plexsim: bad request");
            };
            if self.catalog.get(&key).is_none() {
                return Response::error(404, "plexsim: not found");
            }
            self.hidden_continue.insert(key);
            self.dirty = true;
            return Response::empty(200);
        }
        if segments.first() == Some(&"downloadQueue") {
            return self.route_download_queue(&segments[1..], request);
        }
        if segments.first() == Some(&"accounts")
            && segments.len() >= 2
            && matches!(request.method.as_str(), "GET" | "HEAD")
        {
            // Cycle 52: an extra segment after the account id is a 404.
            if segments.len() > 2 {
                return Response::error(404, "plexsim: not found");
            }
            return self.route_account(Some(segments[1]));
        }
        if segments.len() >= 4 && segments[..2] == ["hubs", "sections"] && segments[3] == "popular" {
            return self.route_popular_hub(&segments, query, request);
        }
        if segments.first() == Some(&"activities") && segments.len() > 1 && request.method == "PUT" {
            // Cycle 19: a PUT to an activity is a 400, where its DELETE is a 404.
            return Response::error(400, "plexsim: bad request");
        }
        if segments.len() >= 3 && segments[..3] == ["library", "metadata", "augmentations"] {
            // Cycle 52, kids and zen: the augmentation id is PARSED before the row is
            // looked up -- not a number is a 400, a number the server does not hold a 404.
            if segments
                .get(3)
                .is_some_and(|id| id.is_empty() || !id.bytes().all(|b| b.is_ascii_digit()))
            {
                return Response::error(400, "plexsim: bad request");
            }
            return Response::error(404, "plexsim: not found");
        }
        // Universal transcode is a routed family even though this native
        // server has no encoder.  The reference validates the playback
        // target and the session decision before it reaches the encoder; a
        // bare 404 would make malformed requests indistinguishable from an
        // unsupported conversion.
        if segments.len() >= 4
            && matches!(segments[0], "audio" | "music" | "subtitles")
            && segments[1..3] == [":", "transcode"]
        {
            return Response::error(400, "plexsim: bad request");
        }
        if segments.len() >= 4
            && segments[0] == "video"
            && segments[1..3] == [":", "transcode"]
            && segments[3] == "universal"
        {
            let leaf = segments.get(4).copied().unwrap_or_default();
            if leaf == "start" || leaf.starts_with("start.") {
                let key = match self.decision_target(query, request) {
                    Ok(key) => key,
                    Err(response) => return response,
                };
                let session = self.playback_session(query, &request.headers);
                if !self.decided_sessions.contains(&(session, key)) {
                    return Response::error(400, "plexsim: no decision for this session");
                }
                return Response::error(404, "plexsim: no transcoder");
            }
            if matches!(leaf, "ping" | "stop") {
                if !query.contains_key("session") {
                    return Response::error(400, "plexsim: bad request");
                }
                return Response::error(404, "plexsim: no transcoder");
            }
            if leaf == "subtitles" {
                return Response::error(400, "plexsim: bad request");
            }
            return Response::error(404, "plexsim: no transcoder");
        }
        if segments.first() == Some(&"resources") {
            if segments.len() == 2
                && matches!(
                    request.method.as_str(),
                    "GET" | "HEAD" | "POST" | "PUT" | "DELETE" | "OPTIONS" | "PATCH"
                )
            {
                return Response::json(
                    200,
                    self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
                );
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.first() == Some(&"clients") && segments.get(1) == Some(&"profiles") {
            if segments.len() > 2 {
                return Response::error(404, "plexsim: not found");
            }
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            return self.route_client_profiles(query);
        }
        if segments.first() == Some(&"search") && segments.get(1) == Some(&"voice") {
            return if query.contains_key("query") {
                Response::error(404, "plexsim: not found")
            } else {
                Response::error(400, "plexsim: bad request")
            };
        }
        if path == "/hubs/sections/home" {
            return Response::error(404, "plexsim: not found");
        }
        if segments.first() == Some(&"devices") && segments.len() == 2 {
            return if request.method == "GET" || request.method == "HEAD" {
                self.route_device(segments[1])
            } else {
                Response::error(404, "plexsim: not found")
            };
        }
        if path.starts_with("/channels/") && request.method == "GET" {
            return Response::json(200, plugin_container(Vec::new()));
        }
        if path == "/library/tags" {
            return self.route_library_tags(query);
        }
        if path == "/library/arts" && matches!(request.method.as_str(), "GET" | "HEAD") {
            return self.route_library_arts(None, query);
        }
        if path == "/library/randomArtwork" && matches!(request.method.as_str(), "GET" | "HEAD") {
            let Some(sections) = query.get("sections") else {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                );
            };
            let wanted = sections
                .split(',')
                .filter(|value| self.section(value).is_some())
                .collect::<Vec<_>>();
            if wanted.is_empty() {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                );
            }
            // Reuse the stable Photo projection from the arts route while
            // retaining the randomArtwork container's ordinary JSON shape.
            let arts = self.route_library_arts(Some(wanted[0]), &HashMap::new());
            let value = serde_json::from_slice::<Value>(&arts.body).unwrap_or_else(|_| {
                self.container(
                    Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                    false,
                )
            });
            return Response::json(200, value);
        }
        if path == "/library/matches" && matches!(request.method.as_str(), "GET" | "HEAD") {
            return self.route_library_matches(query);
        }
        if segments.first() == Some(&"library") && segments.get(1) == Some(&"people") {
            if segments.len() == 3 && matches!(request.method.as_str(), "GET" | "HEAD") {
                return self.route_person(segments[2], false, query);
            }
            if segments.len() == 4
                && segments[3] == "media"
                && matches!(request.method.as_str(), "GET" | "HEAD")
            {
                return self.route_person(segments[2], true, query);
            }
            return Response::error(404, "plexsim: not found");
        }
        if path == "/sync" || path == "/sync/" {
            return if matches!(request.method.as_str(), "GET" | "HEAD") {
                Response::json(200, plugin_container(Vec::new()))
            } else {
                Response::error(404, "plexsim: not found")
            };
        }
        if path == "/services/browse" {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([("Path".to_string(), json!([{"path":"/"}]))]),
                    false,
                ),
            );
        }
        if segments.len() >= 3 && segments[0] == "services" && segments[1] == "browse" {
            let Some(include_files) = query.get("includeFiles") else {
                return Response::error(400, "plexsim: bad request");
            };
            if include_files != "0" && include_files != "1" {
                return Response::error(400, "plexsim: bad request");
            }
            let decoded = base64_decode(segments[2]).unwrap_or_default();
            if decoded.is_empty() {
                return Response::error(400, "plexsim: bad request");
            }
            if include_files == "0" {
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Path".to_string(), json!([{"path": decoded}]))]),
                        false,
                    ),
                );
            }
            let files = self
                .catalog
                .items
                .iter()
                .flat_map(|row| {
                    row.get("Media")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .flat_map(|media| {
                            media
                                .get("Part")
                                .and_then(Value::as_array)
                                .into_iter()
                                .flatten()
                                .filter_map(|part| part.get("file").and_then(Value::as_str))
                        })
                })
                .filter(|file| file.starts_with(&decoded))
                .map(|file| json!({"path":file}))
                .collect::<Vec<_>>();
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("Path".to_string(), json!([{"path":decoded}])),
                        ("File".to_string(), Value::Array(files)),
                    ]),
                    false,
                ),
            );
        }
        if path == "/library/shared" || path.starts_with("/library/shared/") {
            return self.library_shared(&segments, query);
        }
        if path == "/updater/check" {
            return Response::error(404, "plexsim: not found");
        }
        if path == "/updater/apply" {
            return if request.method == "PUT" {
                Response::error(400, "plexsim: no update downloaded")
            } else {
                Response::error(404, "plexsim: not found")
            };
        }
        if path == "/library/all/top" {
            return self.route_top(query);
        }
        if path == "/library/clean/bundles" && request.method == "PUT" {
            // Measured on a writable copy, frame for frame; plexsim has no bundles.
            self.notify_activity_run(
                "general.bundle.clean",
                "Cleaning bundles",
                &[
                    (0, "Analyzing metadata bundles"),
                    (25, "Deleting 0 metadata bundles"),
                    (50, "Deleting 0 metadata bundles"),
                    (50, "Analyzing media bundles"),
                    (75, "Deleting 0 media bundles"),
                    (100, "Deleting 0 media bundles"),
                ],
            );
            return Response::empty(200);
        }
        if path == "/library/optimize" {
            if request.method != "PUT" {
                return Response::error(404, "plexsim: not found");
            }
            // Measured on the copy, frame for frame: started at 0 with an EMPTY
            // subtitle, updated at 0, 10, 40 and 60 with `Optimizing Database`,
            // then ended at 100.
            self.notify_activity_run(
                "general.db.optimize",
                "Optimizing database",
                &[
                    (0, "Optimizing Database"),
                    (10, "Optimizing Database"),
                    (40, "Optimizing Database"),
                    (60, "Optimizing Database"),
                ],
            );
            // Database optimization is the measured spelling-index rebuild point.
            self.rebuild_spelling();
            return Response::empty(200);
        }
        if segments == ["playQueues"] && matches!(request.method.as_str(), "GET" | "HEAD") {
            let Some(value) = query.get("type") else {
                return Response::json(
                    200,
                    self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
                );
            };
            let digits = value.strip_prefix('+').unwrap_or(value);
            if digits.is_empty() || !digits.chars().all(|character| character.is_ascii_digit()) {
                return Response::error(400, "plexsim: bad request");
            }
            return match digits.parse::<i64>().unwrap_or(-1) {
                0 => Response::json(
                    200,
                    self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
                ),
                42 => Response::json(
                    200,
                    self.container(
                        Map::from_iter([
                            ("size".to_string(), json!(1)),
                            (
                                "PlayQueue".to_string(),
                                json!([{
                                    "id":1,
                                    "clientIdentifier":"55d3184c-28b9-43ce-842c-b7b03ca00e0f",
                                    "totalItemsCount":0,
                                    "type":"playlist",
                                    "shuffled":false
                                }]),
                            ),
                        ]),
                        false,
                    ),
                ),
                _ => Response::error(400, "plexsim: bad request"),
            };
        } else if segments == ["playQueues", "1"]
            && matches!(request.method.as_str(), "GET" | "HEAD")
        {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("size".to_string(), json!(0)),
                        (
                            "identifier".to_string(),
                            json!("com.plexapp.plugins.library"),
                        ),
                        ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
                        ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
                        ("playQueueID".to_string(), json!(1)),
                        ("playQueueShuffled".to_string(), json!(false)),
                        ("playQueueVersion".to_string(), json!(-1)),
                    ]),
                    false,
                ),
            );
        } else if segments.starts_with(&["playQueues"]) {
            return self.route_play_queue(&segments[1..], query, request);
        }
        if segments.starts_with(&["status", "sessions", "history"])
            && segments.len() == 4
            && request.method == "DELETE"
        {
            let history_key = segments[3];
            let before = self.history.len();
            self.history.retain(|row| {
                let full = string_value(row.get("historyKey").unwrap_or(&Value::Null));
                full != history_key && full.rsplit('/').next().unwrap_or_default() != history_key
            });
            if self.history.len() == before {
                return Response::error(404, "plexsim: not found");
            }
            self.dirty = true;
            return Response::json(
                200,
                self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
            );
        }
        if segments.starts_with(&["status", "sessions", "history"])
            && segments.len() == 4
            && matches!(request.method.as_str(), "GET" | "HEAD")
        {
            let requested = segments[3];
            let Some(row) = self.history.iter().find(|row| {
                let full = string_value(row.get("historyKey").unwrap_or(&Value::Null));
                full == requested || full.rsplit('/').next().unwrap_or_default() == requested
            }) else {
                return Response::error(404, "plexsim: not found");
            };
            let mut projection = Map::new();
            for name in [
                "historyKey",
                "librarySectionID",
                "title",
                "grandparentTitle",
                "type",
                "index",
                "parentIndex",
                "originallyAvailableAt",
                "viewedAt",
                "accountID",
                "deviceID",
            ] {
                if let Some(value) = row.get(name) {
                    projection.insert(name.to_string(), value.clone());
                }
            }
            projection.insert("viewType".to_string(), json!(1));
            return Response::json(
                200,
                self.container(
                    Map::from_iter([("Metadata".to_string(), json!([projection]))]),
                    true,
                ),
            );
        }
        if path == "/status/sessions/terminate" && request.method == "GET" {
            let Some(session_id) = query.get("sessionId").filter(|value| !value.is_empty()) else {
                return Response::error(400, "plexsim: bad request");
            };
            let found = self.sessions.values().any(|session| {
                session
                    .get("Session")
                    .and_then(|value| value.get("id"))
                    .and_then(Value::as_str)
                    == Some(session_id.as_str())
                    || session
                        .get("sessionKey")
                        .and_then(Value::as_str)
                        == Some(session_id.as_str())
            });
            if !found {
                return Response::error(404, "plexsim: not found");
            }
            self.terminated_sessions.insert(
                session_id.clone(),
                query
                    .get("reason")
                    .cloned()
                    .unwrap_or_else(|| "Session terminated".to_string()),
            );
            return Response::empty(200);
        }
        if path == "/hubs/continueWatching/items" {
            return self.route_continue_watching_items(query);
        }
        if path == "/hubs/continueWatching" {
            return self.route_continue_watching(query);
        }
        if path == "/hubs/items" {
            return self.route_hub_items(query);
        }
        if path == "/hubs/search/voice" {
            let Some(raw) = query.get("query") else {
                return Response::error(400, "plexsim: bad request");
            };
            let type_number = query
                .get("type")
                .and_then(|value| value.parse::<i64>().ok());
            let (identifier, wanted) = match type_number {
                Some(1) => ("results.search.1", Some("movie")),
                Some(2) => ("results.search.2", Some("show")),
                Some(4) => ("results.search.4", Some("episode")),
                Some(8) => ("results.search.8", Some("artist")),
                Some(_) => return Response::json(200, self.container(Map::new(), false)),
                None => ("results.search.1", None),
            };
            let rows = self
                .catalog
                .items
                .iter()
                .filter(|row| {
                    wanted
                        .map(|kind| row.get("type").and_then(Value::as_str) == Some(kind))
                        .unwrap_or(true)
                        && row
                            .get("title")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&raw.to_lowercase())
                })
                .map(|row| self.listing_row(row))
                .collect::<Vec<_>>();
            if type_number.is_some() && rows.is_empty() {
                return Response::json(200, self.container(Map::new(), false));
            }
            if type_number.is_none() {
                let mut hubs = Vec::new();
                for (number, kind) in [(1_i64, "movie"), (2, "show"), (4, "episode")] {
                    let typed = self
                        .catalog
                        .items
                        .iter()
                        .filter(|row| {
                            row.get("type").and_then(Value::as_str) == Some(kind)
                                && row
                                    .get("title")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_lowercase()
                                    .contains(&raw.to_lowercase())
                        })
                        .map(|row| self.listing_row(row))
                        .collect::<Vec<_>>();
                    if typed.is_empty() {
                        continue;
                    }
                    // Measured: a voice hub carries `hubKey` and NO `key`, and both
                    // the hub and every row carry a `distance`.  The reference's
                    // distance is its own fuzzy-match cost; plexsim reports the
                    // query's length, which is what an exact word-prefix hit costs.
                    let distance = raw.trim().chars().count();
                    let keys = typed.iter().filter_map(key_of).collect::<Vec<_>>();
                    let typed = typed
                        .into_iter()
                        .map(|mut row| {
                            if let Some(object) = row.as_object_mut() {
                                object.insert("distance".to_string(), json!(distance));
                            }
                            row
                        })
                        .collect::<Vec<_>>();
                    hubs.push(json!({
                        "hubKey": format!("/library/metadata/{}", keys.join(",")),
                        "title": kind,
                        "type": kind,
                        "hubIdentifier": format!("results.search.{number}"),
                        "context": "hub.results.search",
                        "size": typed.len(),
                        "more": false,
                        "style": "shelf",
                        "distance": distance,
                        "Metadata": typed,
                    }));
                }
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Hub".to_string(), Value::Array(hubs))]),
                        false,
                    ),
                );
            }
            let keys = rows.iter().filter_map(key_of).collect::<Vec<_>>();
            let mut hub = Map::from_iter([
                ("hubIdentifier".to_string(), json!(identifier)),
                ("title".to_string(), json!(wanted.unwrap_or("movie"))),
                ("context".to_string(), json!("hub.results.search")),
                ("type".to_string(), json!(wanted.unwrap_or("movie"))),
                ("style".to_string(), json!("shelf")),
                ("distance".to_string(), json!(raw.trim().chars().count())),
                ("size".to_string(), json!(rows.len())),
                ("more".to_string(), json!(false)),
                ("Metadata".to_string(), Value::Array(rows)),
            ]);
            if !keys.is_empty() {
                hub.insert(
                    "hubKey".to_string(),
                    json!(format!("/library/metadata/{}", keys.join(","))),
                );
            }
            return Response::json(
                200,
                self.container(Map::from_iter([("Hub".to_string(), json!([hub]))]), false),
            );
        }
        if path == "/hubs/promoted" {
            // Cycle 55, measured on zen and kids: the fifteen hub-refused keys are a
            // 500 here whenever a SHOW library's shelf is in scope -- unscoped, or
            // `contentDirectoryID` naming a show library -- and a 200 when only a
            // movie library is.
            let wanted = content_directories(query);
            if self.catalog.sections.iter().any(|section| {
                section.get("type").and_then(Value::as_str) == Some("show")
                    && wanted.as_ref().map_or(true, |w| {
                        w.contains(&string_value(section.get("key").unwrap_or(&Value::Null)))
                    })
            }) {
                if let Some(refusal) = hub_refusal(query) {
                    return refusal;
                }
            }
            if let Err(status) = self.filtered_tv_leaves(query, true) {
                return Response::error(status, "plexsim: bad request");
            }
            return Response::json(200, self.promoted_hubs(query));
        }
        if path == "/hubs/home/onDeck" {
            // Cycle 28: `contentDirectoryID` scopes this leaf as an exact string and
            // takes anything (no 404 for a junk value). Cycle 30: the shelf drops
            // `Field` and KEEPS `librarySectionKey`, which is what separates it from
            // `/library/onDeck`. Its rows are On Deck's episodes alone.
            let directories = content_directories(query);
            let rows = self
                .in_directories(self.continue_watching(false), &directories)
                .into_iter()
                .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                .map(|row| {
                    let mut entry = self.detail_row(&row);
                    if let Some(object) = entry.as_object_mut() {
                        object.remove("Field");
                    }
                    entry
                })
                .collect::<Vec<_>>();
            let mut answer = self.hub_container(Map::from_iter([
                ("size".to_string(), json!(rows.len())),
                ("Metadata".to_string(), Value::Array(rows)),
            ]));
            if query.get("includeMeta").map(String::as_str) == Some("1") {
                answer["MediaContainer"]["Meta"] = json!({"Type": [{"key": "/hubs/home/onDeck",
                    "type": "mixed", "title": "On Deck", "active": true}]});
            }
            return Response::json(200, answer);
        }
        if path == "/hubs/home/recentlyAdded" {
            // Measured: this endpoint keys on `type`, NOT on `sectionID`. Cycle 54: the
            // TV list refuses the per-user keys as the hub pages do.
            let type_id = query.get("type").map(String::as_str).unwrap_or("");
            if type_id == "2" {
                if let Some(refusal) = hub_refusal(query) {
                    return refusal;
                }
            }
            let mut rows = self.recently_added_rows(Some(type_id), query.get("sectionID"));
            if type_id == "2" {
                // Cycle 54: the request's listing filters run over the episodes before
                // the roll-up, and a value that does not parse is the filter's 500.
                match self.filtered_tv_leaves(query, false) {
                    Ok(Some(leaves)) => {
                        rows = Some(self.recently_added_tv(query.get("sectionID"), Some(leaves)));
                    }
                    Ok(None) => {}
                    Err(status) => return Response::error(status, "plexsim: bad request"),
                }
            }
            if let Some(personal) = query.get("personal") {
                // Cycle 54: the 0/1 option; cycle 55: only the movie list (and the
                // always-empty others) empties at 1, the TV list is unchanged.
                if !bool_option(personal) {
                    return Response::error(400, "plexsim: bad request");
                }
                if personal.trim_start_matches('+').trim_start_matches('0') == "1" && type_id != "2" {
                    rows = rows.map(|_| Vec::new());
                }
            }
            // Cycle 59: a non-empty `pinnedContentDirectoryID` REPLACES
            // `contentDirectoryID` as the scope.
            let mut scope = content_directories(query);
            if let Some(pinned) = query.get("pinnedContentDirectoryID").filter(|v| !v.is_empty()) {
                scope = Some(pinned.split(',').filter(|v| !v.is_empty()).map(str::to_string).collect());
            }
            let meta = if query.get("includeMeta").map(String::as_str) == Some("1") {
                match type_id {
                    "1" => Some(json!({"Type": [{"key": "/hubs/home/recentlyAdded?type=1",
                        "type": "movie", "title": "Recently Added", "active": true}]})),
                    "2" => Some(json!({"Type": [{"key": "/hubs/home/recentlyAdded?type=2",
                        "type": "mixed", "title": "Recently Added", "active": false}]})),
                    _ => None,
                }
            } else {
                None
            };
            let Some(rows) = rows.map(|rows| self.in_directories(rows, &scope)) else {
                let mut answer = self.hub_container(Map::new());
                if let Some(meta) = meta {
                    answer["MediaContainer"]["Meta"] = meta;
                }
                return Response::json(200, answer);
            };
            // Measured: a movie or episode row here is the light projection; a show
            // row is the full one. Cycle 41: the TV type is a listing capped at 50
            // that pages; the movie type is cut at twelve.
            let projected = rows
                .iter()
                .map(|row| {
                    if row.get("type").and_then(Value::as_str) == Some("show") {
                        self.detail_row(row)
                    } else {
                        guid_gate(self.grid_row(row, true, true, false), query)
                    }
                })
                .collect::<Vec<_>>();
            let mut answer = if type_id == "2" {
                let (page, paged, start, total) = match page_window(projected, query) {
                    Ok(window) => window,
                    Err(status) => return Response::error(status, "plexsim: bad container parameter"),
                };
                let mut fields = Map::from_iter([
                    ("size".to_string(), json!(page.len())),
                    ("Metadata".to_string(), Value::Array(page)),
                ]);
                if paged || query.contains_key("limit") || query.contains_key("count") {
                    fields.insert("totalSize".to_string(), json!(total));
                    fields.insert("offset".to_string(), json!(start));
                }
                self.hub_container(fields)
            } else {
                let page = projected.into_iter().take(12).collect::<Vec<_>>();
                self.hub_container(Map::from_iter([
                    ("size".to_string(), json!(page.len())),
                    ("Metadata".to_string(), Value::Array(page)),
                ]))
            };
            if let Some(meta) = meta {
                answer["MediaContainer"]["Meta"] = meta;
            }
            return Response::json(200, answer);
        }
        if path == "/hubs/home/continueWatching" {
            // Measured on fun: the light projection. Cycle 25: the list is
            // `resuming()` -- a next-up with no stored offset is On Deck and not
            // here. Cycle 28: `contentDirectoryID` scopes it; `sectionID` does not.
            let directories = content_directories(query);
            let rows = self
                .in_directories(self.resuming(), &directories)
                .iter()
                .map(|row| self.grid_row(row, true, false, false))
                .collect::<Vec<_>>();
            let mut answer = self.hub_container(Map::from_iter([
                ("size".to_string(), json!(rows.len())),
                ("Metadata".to_string(), Value::Array(rows)),
            ]));
            if query.get("includeMeta").map(String::as_str) == Some("1") {
                answer["MediaContainer"]["Meta"] = json!({"Type": [{"key": "/hubs/home/continueWatching",
                    "type": "mixed", "title": "Continue Watching", "active": true}]});
            }
            return Response::json(200, answer);
        }
        if segments.first() == Some(&"hubs")
            && segments.get(1) == Some(&"metadata")
            && segments.len() == 3
        {
            let key = segments[2];
            if self.catalog.get(key).is_none() {
                return Response::error(404, "plexsim: not found");
            }
            let shelves = [
                ("Most Played", "artist", "artist.mostplayedtracks"),
                ("Most Popular Tracks", "artist", "artist.mostpopulartracks"),
                ("Albums", "artist", "artist.albums"),
                ("Music Videos", "video", "music.videos.popular.new"),
            ]
            .into_iter()
            .map(|(title, kind, identifier)| {
                json!({
                    "key": format!("/hubs/metadata/{key}/{identifier}?artist.id={key}"),
                    "title": title,
                    "type": kind,
                    "hubIdentifier": identifier,
                    "context": format!("hub.{identifier}"),
                    "size": 0,
                    "more": false,
                    "style": "shelf"
                })
            })
            .collect::<Vec<_>>();
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("allowSync".to_string(), json!(true)),
                        ("Hub".to_string(), Value::Array(shelves)),
                    ]),
                    false,
                ),
            );
        }
        if segments.first() == Some(&"hubs")
            && segments.get(1) == Some(&"metadata")
            && segments.len() == 4
        {
            return self.route_item_hubs(segments[2], segments[3], query);
        }
        if path.starts_with("/hubs/sections/") && path.ends_with("/continueWatching/items") {
            let section_key = path
                .trim_start_matches("/hubs/sections/")
                .trim_end_matches("/continueWatching/items")
                .trim_end_matches('/');
            if section_key.is_empty() || self.section(section_key).is_none() {
                return Response::error(404, "plexsim: not found");
            }
            if !Self::valid_content_directory(query) {
                return Response::error(404, "plexsim: not found");
            }
            return self.route_section_continue_watching_items(section_key);
        }
        if segments.first() == Some(&"hubs")
            && segments.get(1) == Some(&"sections")
            && segments.len() == 4
            && segments[3] == "manage"
        {
            let section_key = segments[2];
            let Some(section) = self.section(section_key) else {
                return Response::error(404, "plexsim: not found");
            };
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            if let Some(raw_item) = query.get("metadataItemId") {
                // Measured on both servers: naming an item -- even with an empty id --
                // is a 400 on a read.  Cycle 55 refined it on zen and kids: the id is a
                // signed 32-bit integer and there are THREE answers.  `-1` (and `-01`)
                // is the bare hub list, a number naming NO item (`0`, `-2`, `999999`,
                // `+999999`) is an EMPTY 200, and an item the server holds -- in any
                // library -- is the 400, as are an empty value, a word, `1.5`, ` 1` and
                // `2147483648`.
                if !int_param(raw_item, Some(32)) {
                    return Response::error(400, "plexsim: bad request");
                }
                let item = raw_item.parse::<i64>().unwrap_or(0);
                if item != -1 {
                    if self.catalog.get(&item.to_string()).is_some() {
                        return Response::error(400, "plexsim: bad request");
                    }
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([("size".to_string(), json!(0))]),
                            false,
                        ),
                    );
                }
            }
            let rows = if section.get("type").and_then(Value::as_str) == Some("show") {
                vec![
                    ("tv.recentlyaired", "Recently Released Episodes", false),
                    ("tv.recentlyadded", "Recently Added TV", true),
                    ("recent.library.playlists", "Library Playlists", false),
                    ("tv.startwatching", "Start Watching", false),
                    ("tv.rediscover", "Rediscover", false),
                    ("tv.morefromnetwork", "More from (Network)", false),
                    ("tv.moreingenre", "More in (Genre)", false),
                    ("tv.toprated", "Top Rated TV", false),
                    ("tv.recentlyviewed", "Recently Watched Episodes", false),
                ]
            } else {
                vec![
                    ("movie.recentlyreleased", "Recently Released Movies", false),
                    ("movie.recentlyadded", "Recently Added Movies", true),
                    ("recent.library.playlists", "Library Playlists", false),
                    ("movie.genre", "Top Movies in (Genre)", false),
                    ("movie.by.actor.or.director", "Top Movies by (Actor or Director)", false),
                    ("movie.topunwatched", "Top Unwatched Movies", false),
                    ("movie.curated", "Seasonal Movies", true),
                    ("movie.recentlyviewed", "Recently Watched Movies", false),
                ]
            };
            let hubs = rows
                .into_iter()
                .map(|(identifier, title, home)| {
                    json!({
                        "identifier": identifier,
                        "title": title,
                        "recommendationsVisibility": "all",
                        "homeVisibility": if home { "all" } else { "none" },
                        "promotedToRecommended": true,
                        "promotedToOwnHome": home,
                        "promotedToSharedHome": home,
                    })
                })
                .collect::<Vec<_>>();
            return Response::json(
                200,
                self.container(
                    Map::from_iter([
                        ("size".to_string(), json!(hubs.len())),
                        ("Hub".to_string(), Value::Array(hubs)),
                    ]),
                    false,
                ),
            );
        }
        if path.starts_with("/hubs/sections/") {
            let section_key = path
                .trim_start_matches("/hubs/sections/")
                .split('/')
                .next()
                .unwrap_or_default();
            if !section_key.is_empty() && !section_key.contains('?') {
                return self.route_section_hubs(section_key, query);
            }
            return Response::error(404, "plexsim: not found");
        }
        if path == "/hubs" && request.method == "PUT" {
            return Response::empty(200);
        }
        if segments.len() == 1 && (request.method == "GET" || request.method == "HEAD") {
            if let Some(response) = self.route_plugin_prefix(path) {
                return response;
            }
            // PMS's top-level plug-in dispatcher returns an empty directory
            // for unknown single-component plug-ins; clients use this for
            // optional pages such as /settings and /login.
            return Response::json(200, plugin_container(Vec::new()));
        }
        if segments == ["library", "sections"] && request.method == "POST" {
            for required in ["name", "type", "agent", "scanner", "language", "location"] {
                if !query.contains_key(required) {
                    return Response::error(400, "plexsim: bad request");
                }
            }
            let kind = query.get("type").map(String::as_str).unwrap_or("");
            let expected_agent = if kind == "2" || kind == "show" {
                "tv.plex.agents.series"
            } else if kind == "1" || kind == "movie" {
                "tv.plex.agents.movie"
            } else {
                ""
            };
            let expected_scanner = if kind == "2" || kind == "show" {
                "Plex TV Series"
            } else if kind == "1" || kind == "movie" {
                "Plex Movie"
            } else {
                ""
            };
            if expected_agent.is_empty()
                || query.get("agent").map(String::as_str) != Some(expected_agent)
                || query.get("scanner").map(String::as_str) != Some(expected_scanner)
                || !matches!(
                    query.get("language").map(String::as_str),
                    Some("en-US" | "de-DE" | "fr-FR" | "es-ES")
                )
            {
                return Response::error(400, "plexsim: bad request");
            }
            return self.create_section(query);
        }
        if segments.len() == 3
            && segments[0] == "library"
            && segments[1] == "sections"
            && segments[2] == "prefs"
            && (request.method == "GET" || request.method == "HEAD")
        {
            let Some(type_number) = query
                .get("type")
                .and_then(|value| value.parse::<i64>().ok())
            else {
                return Response::error(400, "plexsim: bad request");
            };
            let agent = query.get("agent").map(String::as_str).unwrap_or("None");
            // The list depends on the type AND the agent; a legacy or unknown
            // agent gives the type's base list and an unknown type `hidden` alone.
            let sidecar = load_json_sidecar(&self.catalog_path, "section_prefs_new.json");
            let lists = sidecar.as_ref().and_then(|value| value.get("lists"));
            let ids = lists
                .and_then(|lists| lists.get(format!("{type_number}|{agent}")))
                .filter(|value| value.as_array().is_some_and(|ids| !ids.is_empty()))
                .or_else(|| lists.and_then(|lists| lists.get(type_number.to_string())))
                .filter(|value| value.as_array().is_some_and(|ids| !ids.is_empty()))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_else(|| vec![json!("hidden")]);
            let settings = sidecar
                .as_ref()
                .and_then(|value| value.get("settings"))
                .and_then(Value::as_object)
                .map(|settings| {
                    ids.into_iter()
                        .filter_map(|id| id.as_str().and_then(|id| settings.get(id)).cloned())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            return Response::json(
                200,
                json!({"MediaContainer": {"size": settings.len(), "Setting": settings}}),
            );
        }
        if segments == ["library", "sections", "all", "refresh"] {
            if request.method != "GET" && request.method != "POST" && request.method != "DELETE" {
                return Response::error(404, "plexsim: not found");
            }
            if request.method != "DELETE" {
                // Measured on a writable copy: this scans EVERY library, one after another.
                for section in self.catalog.sections.clone() {
                    let key = string_value(section.get("key").unwrap_or(&Value::Null));
                    self.refresh_section(&key, &section, query);
                }
            } else {
                // Cycle 59, measured on plexscratch42 and plexscratch: the library-wide
                // cancel pushes ONE status frame, "Library scan canceled"; a SECTION's
                // cancel pushes none.
                self.notify_status("Library scan canceled", "");
            }
            return Response::empty(200);
        }
        if segments.starts_with(&["library", "sections"]) && segments.len() >= 3 {
            let section_key = segments[2];
            if let Some(section) = self.section(section_key) {
                if segments.get(3) == Some(&"composite")
                    && matches!(request.method.as_str(), "GET" | "HEAD")
                {
                    // Cycle 16: the BARE path is a 404 on kids and zen; only
                    // `/composite/{version}` answers, the version all digits (cycle 45)
                    // and inside int32 (cycle 36's guard, a 400 past it).
                    let Some(version) = segments.get(4).filter(|_| segments.len() == 5) else {
                        return Response::error(404, "plexsim: not found");
                    };
                    if version.is_empty() || !version.bytes().all(|b| b.is_ascii_digit()) {
                        return Response::error(404, "plexsim: not found");
                    }
                    if version.len() > 10 || version.parse::<u64>().unwrap_or(u64::MAX) >= 1 << 31 {
                        return Response::error(400, "plexsim: bad request");
                    }
                    // A real PMS builds a mosaic of the members' posters; plexsim serves
                    // one image in its place (`composite_for`), here the placeholder.
                    let mut response = Response::image(jpeg_placeholder());
                    response.headers = vec![
                        ("Cache-Control".to_string(), "no-cache".to_string()),
                        ("Accept-Ranges".to_string(), "bytes".to_string()),
                    ];
                    let _ = section;
                    return response;
                }
                if segments.len() == 3 {
                    if request.method == "GET" || request.method == "HEAD" {
                        return self.route_section_detail(section, query);
                    }
                    if request.method == "DELETE" {
                        if self.catalog.sections.len() <= 1 {
                            return Response::error(400, "plexsim: bad request");
                        }
                        let uuid = format!("plexsim-library-delete-{}", now());
                        let activity = |event: &str, progress: i64| {
                            json!({"ActivityNotification": {
                                "event": event,
                                "uuid": uuid,
                                "Activity": {
                                    "type": "library.delete",
                                    "progress": progress,
                                    "subtitle": "Deleting library"
                                }
                            }})
                        };
                        self.push_event("activity", activity("started", 0));
                        for progress in [15, 30, 45, 60, 75, 90] {
                            self.push_event("activity", activity("updated", progress));
                        }
                        self.push_event("activity", activity("ended", 100));
                        self.catalog.sections.retain(|row| {
                            string_value(row.get("key").unwrap_or(&Value::Null)) != section_key
                        });
                        self.deleted.insert(section_key.to_string());
                        self.catalog.document.insert(
                            "sections".to_string(),
                            Value::Array(self.catalog.sections.clone()),
                        );
                        self.dirty = true;
                        return Response::empty(200);
                    }
                    if request.method == "PUT" {
                        return self.edit_section(section_key, section.clone(), query);
                    }
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([(
                                "Directory".to_string(),
                                vec![self.section_directory(section)].into_iter().collect(),
                            )]),
                            true,
                        ),
                    );
                }
                if segments.len() == 4 && segments[3] == "onDeck" {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    // Cycle 20/55: `/onDeck` parses `type=` the way a listing does --
                    // `abc` and an empty value are 500s -- where the added subsets
                    // ignore it.
                    if let Err(status) = listing::parsed_type(query, 500, None) {
                        return Response::error(status, "plexsim: bad type");
                    }
                    // Cycle 28: the FULL detail row; cycle 55: ordered by the
                    // view-state write time, then index and id; cycle 30: no `Field`
                    // and no `librarySectionKey`.
                    let kind = if section.get("type").and_then(Value::as_str) == Some("show") {
                        "episode"
                    } else {
                        "movie"
                    };
                    let mut rows = self
                        .continue_watching(false)
                        .into_iter()
                        .filter(|row| {
                            string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                                == section_key
                                && row.get("type").and_then(Value::as_str) == Some(kind)
                        })
                        .collect::<Vec<_>>();
                    rows.sort_by_key(|row| {
                        let stamp = row
                            .get("_viewUpdatedAt")
                            .and_then(Value::as_i64)
                            .filter(|stamp| *stamp != 0)
                            .or_else(|| row.get("lastViewedAt").and_then(Value::as_i64))
                            .unwrap_or(0);
                        (
                            -stamp,
                            row.get("index").and_then(Value::as_i64).unwrap_or(0),
                            key_of(row).and_then(|key| key.parse::<i64>().ok()).unwrap_or(0),
                        )
                    });
                    let rows = rows
                        .iter()
                        .map(|row| {
                            let mut entry = self.detail_row(row);
                            if let Some(object) = entry.as_object_mut() {
                                object.remove("Field");
                                object.remove("librarySectionKey");
                            }
                            entry
                        })
                        .collect::<Vec<_>>();
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("title2".to_string(), json!("On Deck")),
                                ("allowSync".to_string(), json!(true)),
                                ("Metadata".to_string(), Value::Array(rows)),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() >= 4 && segments[3] == "extras" {
                    return self.section_extras(section_key, &segments[4..], query);
                }
                if segments.len() == 5 && segments[3] == "stations" {
                    // Cycle 45: an EMPTY container carrying `art`, `thumb` and `title1`
                    // and nothing else, for exactly four names.
                    if !matches!(segments[4], "genre" | "mood" | "style" | "decade") {
                        return Response::error(404, "plexsim: not found");
                    }
                    let is_show = section.get("type").and_then(Value::as_str) == Some("show");
                    return Response::json(200, json!({"MediaContainer": {
                        "size": 0,
                        "art": if is_show { "/:/resources/show-fanart.jpg" } else { "/:/resources/movie-fanart.jpg" },
                        "identifier": "com.plexapp.plugins.library",
                        "mediaTagPrefix": MEDIA_TAG_PREFIX,
                        "mediaTagVersion": MEDIA_TAG_VERSION,
                        "thumb": if is_show { "/:/resources/show.png" } else { "/:/resources/movie.png" },
                        "title1": section.get("title").cloned().unwrap_or(Value::Null),
                    }}));
                }
                if segments.len() == 4 && segments[3] == "arts" {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    return self.route_library_arts(Some(section_key), &HashMap::new());
                }
                if segments.len() == 4
                    && matches!(
                        segments[3],
                        "timeline"
                            | "allLeaves"
                            | "albums"
                            | "cluster"
                            | "moment"
                            | "computePath"
                            | "nearest"
                    )
                {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    let kind = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie");
                    let leaf = segments[3];
                    if leaf == "timeline" {
                        let section = section.clone();
                        return self.section_timeline_response(section_key, &section);
                    }
                    if leaf == "nearest" {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let is_show = kind == "show";
                    let mut fields = Map::from_iter([
                        ("allowSync".to_string(), json!(false)),
                        (
                            "identifier".to_string(),
                            json!("com.plexapp.plugins.library"),
                        ),
                        ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
                        ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
                        (
                            "title1".to_string(),
                            section.get("title").cloned().unwrap_or(Value::Null),
                        ),
                        ("viewMode".to_string(), json!(65592)),
                    ]);
                    if is_show {
                        fields.insert("nocache".to_string(), json!(true));
                    }
                    if leaf == "computePath" {
                        let Some(start) = query.get("startID") else {
                            return Response::error(400, "plexsim: bad request");
                        };
                        let Some(end) = query.get("endID") else {
                            return Response::error(400, "plexsim: bad request");
                        };
                        if self.catalog.get(start).is_none() || self.catalog.get(end).is_none() {
                            return Response::error(404, "plexsim: not found");
                        }
                        fields.remove("allowSync");
                        fields.insert("size".to_string(), json!(0));
                        return Response::json(200, self.container(fields, false));
                    }
                    if leaf == "albums" {
                        fields.insert("mixedParents".to_string(), json!(true));
                        fields.insert("title2".to_string(), json!("By Album"));
                        fields.insert("viewGroup".to_string(), json!("album"));
                        fields.insert("size".to_string(), json!(0));
                        return Response::json(200, self.container(fields, false));
                    }
                    if leaf == "allLeaves" {
                        let type_number = if is_show { 4 } else { 1 };
                        let mut rows = self.choose_rows(Some(section_key), Some(type_number));
                        if let Some(asked) = query.get("type") {
                            let Ok(number) = asked.parse::<i64>() else {
                                return Response::error(500, "plexsim: bad request");
                            };
                            rows.retain(|candidate| {
                                let candidate_number =
                                    match candidate.get("type").and_then(Value::as_str) {
                                        Some("movie") => 1,
                                        Some("show") => 2,
                                        Some("season") => 3,
                                        Some("episode") => 4,
                                        _ => 0,
                                    };
                                candidate_number == number
                            });
                        }
                        let filter_pairs = query
                            .iter()
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect::<Vec<_>>();
                        rows = match self.filter_rows(rows, &filter_pairs, kind) {
                            Ok(rows) => rows,
                            Err(status) => return Response::error(status, "plexsim: bad request"),
                        };
                        if let Some(sort) = query.get("sort") {
                            rows = match self.sort_rows(rows, Some(sort)) {
                                Ok(rows) => rows,
                                Err(status) => {
                                    return Response::error(status, "plexsim: bad request")
                                }
                            };
                        }
                        let shown = rows
                            .iter()
                            .map(|candidate| self.grid_row(candidate, false, true, false))
                            .collect::<Vec<_>>();
                        fields.insert(
                            "viewGroup".to_string(),
                            json!(if is_show { "show" } else { "movie" }),
                        );
                        fields.insert("Metadata".to_string(), Value::Array(shown));
                        return Response::json(200, self.container(fields, false));
                    }
                    fields.insert("content".to_string(), json!("secondary"));
                    fields.insert("clusterZoomLevel".to_string(), json!(1));
                    fields.insert("clusteringActive".to_string(), json!(false));
                    fields.insert(
                        "key".to_string(),
                        json!(format!(
                            "/library/sections/{section_key}/all?clusterZoomLevel=1"
                        )),
                    );
                    fields.insert("title2".to_string(), json!("By Moment"));
                    fields.insert("viewGroup".to_string(), json!("secondary"));
                    fields.insert("size".to_string(), json!(0));
                    return Response::json(200, self.container(fields, false));
                }
                if segments.len() == 4
                    && matches!(
                        segments[3],
                        "recentlyAdded"
                            | "recentlyViewed"
                            | "recentlyViewedShows"
                            | "recentlyViewedGrandparents"
                            | "recentlyAddedGrandparents"
                            | "newest"
                    )
                {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    let kind = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie");
                    let leaf = segments[3];
                    // Cycle 55: the added subsets answer their ordinary list to a bad
                    // `type=`, while the recently-viewed-shows pair refuses it (500).
                    if matches!(leaf, "recentlyViewedShows" | "recentlyViewedGrandparents") {
                        if let Err(status) = listing::parsed_type(query, 500, None) {
                            return Response::error(status, "plexsim: bad type");
                        }
                    }
                    let mut fields = Map::from_iter([
                        (
                            "title2".to_string(),
                            json!(match leaf {
                                "recentlyViewedShows" | "recentlyViewedGrandparents" => {
                                    "Recently Viewed Shows"
                                }
                                "recentlyViewed" if kind == "show" => "Recently Viewed Episodes",
                                "recentlyAdded" | "recentlyAddedGrandparents" => "Recently Added",
                                "newest" => "Recently Released",
                                _ => "Recently Viewed",
                            }),
                        ),
                        ("allowSync".to_string(), json!(true)),
                    ]);
                    // A show library's subsets are EPISODE lists (its Grandparents
                    // roll-up keeps `viewGroup: "episode"`, cycle 45); a movie
                    // library's stay movies.
                    fields.insert(
                        "viewGroup".to_string(),
                        json!(if kind == "show" { "episode" } else { "movie" }),
                    );
                    let mut rows =
                        if leaf == "recentlyViewedShows" || leaf == "recentlyViewedGrandparents" {
                            // Cycle 20, measured on kids and zen: a typed QUERY --
                            // `?type=3` answers the library's seasons and `?type=4` its
                            // episodes, `type=1` on a show library none.
                            let own = if kind == "show" { 2 } else { 1 };
                            let asked = listing::parsed_type(query, 500, None).ok().flatten().unwrap_or(own);
                            let mut roots = self
                                .sort_rows(self.choose_rows(Some(section_key), Some(asked)), Some("titleSort"))
                                .unwrap_or_default();
                            roots.sort_by(|left, right| {
                                right
                                    .get("lastViewedAt")
                                    .and_then(Value::as_i64)
                                    .unwrap_or(0)
                                    .cmp(
                                        &left
                                            .get("lastViewedAt")
                                            .and_then(Value::as_i64)
                                            .unwrap_or(0),
                                    )
                                    .then_with(|| {
                                        string_value(left.get("titleSort").unwrap_or(&Value::Null))
                                            .cmp(&string_value(
                                                right.get("titleSort").unwrap_or(&Value::Null),
                                            ))
                                    })
                            });
                            roots
                        } else if kind == "show" {
                            self.choose_rows(Some(section_key), Some(4))
                        } else {
                            self.section_rows(section_key)
                        };
                    if leaf == "recentlyViewedShows" {
                        // Cycle 54: this subset runs the listing's FILTERS, where newest,
                        // recentlyViewed and recentlyAdded ignore them.
                        let filter_pairs = pairs
                            .iter()
                            .filter(|(name, _)| {
                                !name.starts_with("X-Plex-")
                                    && !name.starts_with("include")
                                    && !name.starts_with("exclude")
                            })
                            .cloned()
                            .collect::<Vec<_>>();
                        rows = match self.filter_rows(rows, &filter_pairs, kind) {
                            Ok(rows) => rows,
                            Err(status) => return Response::error(status, "plexsim: bad request"),
                        };
                    }
                    if kind == "show" && matches!(leaf, "recentlyAddedGrandparents") {
                        let mut seen = HashSet::new();
                        let mut roots = Vec::new();
                        for row in rows {
                            let mut parent = row.clone();
                            while parent.get("type").and_then(Value::as_str) != Some("show") {
                                let Some(parent_key) =
                                    parent.get("parentRatingKey").map(string_value)
                                else {
                                    break;
                                };
                                let Some(next) = self.catalog.get(&parent_key) else {
                                    break;
                                };
                                parent = next.clone();
                            }
                            if parent.get("type").and_then(Value::as_str) == Some("show") {
                                let key = key_of(&parent).unwrap_or_default();
                                if seen.insert(key) {
                                    roots.push(parent);
                                }
                            }
                        }
                        rows = roots;
                        fields.remove("mixedParents");
                    }
                    if !matches!(leaf, "recentlyViewedShows" | "recentlyViewedGrandparents") {
                        // The subsets start from the listing's default order, titleSort,
                        // and each re-sorts stably on its own key.
                        rows = self.sort_rows(rows, Some("titleSort")).unwrap_or_default();
                        let stamp = |row: &Value, name: &str| {
                            row.get(name)
                                .and_then(|value| {
                                    value.as_i64().or_else(|| string_value(value).parse().ok())
                                })
                                .unwrap_or(0)
                        };
                        let rating_key = |row: &Value| stamp(row, "ratingKey");
                        match leaf {
                            "recentlyViewed" => {
                                // Cycle 55, from `order by metadata_item_settings.updated_at
                                // desc` and measured on zen (`viewUpdatedAt` descending,
                                // ties by ratingKey) and plexscratch42: the ORDER is the
                                // view-state write time, falling back to the view time.
                                rows.retain(|row| stamp(row, "lastViewedAt") != 0);
                                rows.sort_by_key(|row| {
                                    let written = stamp(row, "_viewUpdatedAt");
                                    let viewed = stamp(row, "lastViewedAt");
                                    (-(if written != 0 { written } else { viewed }), rating_key(row))
                                });
                                rows.truncate(100);
                            }
                            "recentlyAdded" | "recentlyAddedGrandparents" => {
                                // Cycle 54: only the LITERAL `unwatched=1` drops the
                                // watched rows here, before the cap.
                                if query.get("unwatched").is_some_and(|value| value == "1") {
                                    rows.retain(|row| stamp(row, "viewCount") == 0);
                                }
                                rows.sort_by_key(|row| -stamp(row, "addedAt"));
                                rows.truncate(100);
                            }
                            _ => {
                                rows.sort_by(|left, right| {
                                    string_value(
                                        right.get("originallyAvailableAt").unwrap_or(&Value::Null),
                                    )
                                    .cmp(&string_value(
                                        left.get("originallyAvailableAt").unwrap_or(&Value::Null),
                                    ))
                                });
                            }
                        }
                    }
                    let rows = rows
                        .iter()
                        .map(|row| guid_gate(self.section_listing_row(row), query))
                        .collect::<Vec<_>>();
                    if kind == "show" {
                        // Every subset of a show library carries both, the recently
                        // viewed shows included (only the grandparents roll-up drops
                        // `mixedParents`).
                        if leaf != "recentlyAddedGrandparents" {
                            fields.insert("mixedParents".to_string(), json!(true));
                        }
                        fields.insert("nocache".to_string(), json!(true));
                    }
                    // The rest of `listing_container`'s fields.
                    let title2 = fields.get("title2").map(string_value).unwrap_or_default();
                    for (name, value) in self.listing_fields(section_key, kind, kind, &title2) {
                        fields.entry(name).or_insert(value);
                    }
                    if leaf == "recentlyViewedShows" || leaf == "recentlyViewedGrandparents" {
                        // Measured on both servers: it is the one subset with
                        // `allowSync` false, on a movie library as on a show one.
                        fields.insert("allowSync".to_string(), json!(false));
                        if kind == "show" {
                            fields.insert("viewGroup".to_string(), json!("show"));
                        } else {
                            fields.insert("viewGroup".to_string(), json!("movie"));
                        }
                        let total = rows.len();
                        let start = query
                            .get("X-Plex-Container-Start")
                            .and_then(|value| value.parse::<usize>().ok())
                            .unwrap_or(0);
                        let size = query
                            .get("X-Plex-Container-Size")
                            .and_then(|value| value.parse::<usize>().ok());
                        let rows = if let Some(size) = size {
                            rows.into_iter().skip(start).take(size).collect::<Vec<_>>()
                        } else {
                            rows
                        };
                        fields.insert("offset".to_string(), json!(start));
                        fields.insert("totalSize".to_string(), json!(total));
                        fields.insert("Metadata".to_string(), Value::Array(rows));
                    } else {
                        fields.insert("Metadata".to_string(), Value::Array(rows));
                        page_fields(&mut fields, query);
                    }
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 4 && matches!(segments[3], "recentlyAdded" | "newest") {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    let mut rows = self
                        .section_rows(section_key)
                        .into_iter()
                        .map(|row| self.section_listing_row(&row))
                        .collect::<Vec<_>>();
                    rows.sort_by(|left, right| {
                        right
                            .get("addedAt")
                            .and_then(Value::as_i64)
                            .unwrap_or(0)
                            .cmp(&left.get("addedAt").and_then(Value::as_i64).unwrap_or(0))
                    });
                    let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
                    page_fields(&mut fields, query);
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 4 && segments[3] == "allLeaves" {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    let section_kind = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie");
                    let type_number = if let Some(value) = query.get("type") {
                        match value.parse::<i64>() {
                            Ok(number) => Some(number),
                            Err(_) => return Response::error(500, "plexsim: bad request"),
                        }
                    } else if section_kind == "show" {
                        Some(4)
                    } else {
                        Some(1)
                    };
                    let mut rows = self.choose_rows(Some(section_key), type_number);
                    if section_kind == "show" && query.get("type").is_none() {
                        rows.retain(|row| {
                            row.get("type").and_then(Value::as_str) == Some("episode")
                        });
                    }
                    rows = match self.filter_rows(rows, pairs, section_kind) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    rows = match self.sort_rows(rows, query.get("sort").map(String::as_str)) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    if let Some(group) = query.get("group") {
                        // Cycle 14: the section `allLeaves` takes `group=` too -- the
                        // same as the grid's `type=4&group=index`.
                        rows = match self.grouped(rows, group) {
                            Ok(rows) => rows,
                            Err(status) => return Response::error(status, "plexsim: bad request"),
                        };
                    }
                    let mut rows = rows
                        .iter()
                        .map(|row| self.leaf_listing_row(row))
                        .collect::<Vec<_>>();
                    if let Some(limit) = query.get("limit") {
                        let limit = limit.parse::<i64>().unwrap_or(0);
                        if limit >= 0 {
                            rows.truncate(if limit == 0 { 1 } else { limit as usize });
                        }
                    }
                    if query
                        .get("excludeElements")
                        .is_some_and(|value| value.split(',').any(|name| name == "Media"))
                    {
                        for row in &mut rows {
                            if let Some(object) = row.as_object_mut() {
                                let count = object
                                    .get("Media")
                                    .and_then(Value::as_array)
                                    .map_or(0, Vec::len);
                                object.remove("Media");
                                object.insert("mediaCount".to_string(), json!(count));
                                object.insert("mediaCountOptimized".to_string(), json!(0));
                            }
                        }
                    }
                    let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
                    page_fields(&mut fields, query);
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 5 && segments[3] == "query" && segments[4] == "parse" {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    if query
                        .get("type")
                        .is_some_and(|value| value.parse::<i64>().is_err())
                    {
                        return Response::error(400, "plexsim: bad request");
                    }
                    if query
                        .get("limit")
                        .is_some_and(|value| value.parse::<i64>().is_err())
                    {
                        return Response::error(500, "plexsim: bad request");
                    }
                    let type_number = query
                        .get("type")
                        .and_then(|value| value.parse::<i64>().ok())
                        .unwrap_or(
                            if section.get("type").and_then(Value::as_str) == Some("show") {
                                2
                            } else {
                                1
                            },
                        );
                    let section_type = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie");
                    let mut field_types = self.filter_field_types(section_type);
                    for field in [
                        "parent",
                        "parentIndex",
                        "audienceRating",
                        "guid",
                        "slug",
                        "file",
                        "extraType",
                        "id",
                        "type",
                        "editionTitle",
                        // Cycle 20/53: the artwork columns and `userFields` parse as
                        // Rules with an empty fieldTitle (PARSE_ONLY_FIELD_TYPES) though a
                        // listing refuses them.
                        "theme",
                        "banner",
                        "art",
                        "thumb",
                        "userFields",
                    ] {
                        field_types
                            .entry(field.to_string())
                            .or_insert_with(|| "string".to_string());
                    }
                    let mut errors = Vec::new();
                    let tree = crate::filters::parse(pairs, &field_types, &mut errors)
                        .unwrap_or(Value::Null);
                    let order = crate::filters::orders(
                        query.get("sort").map(String::as_str).unwrap_or(""),
                        &|field| field == "random" || field_types.contains_key(field),
                    )
                    .ok();
                    if query.get("sort").is_some() && order.is_none() {
                        return Response::error(500, "plexsim: bad request");
                    }
                    // Cycle 20: a `sourceType` other than 1 brings the DEFAULT order with
                    // it (an explicit `sort=` still wins), and one that is not a whole
                    // number is a 500 here as on a listing.
                    let source = match crate::filters::source_order(query.get("sourceType").map(String::as_str)) {
                        Ok(source) => source,
                        Err(_) => return Response::error(500, "plexsim: bad request"),
                    };
                    let order = match order {
                        Some(order) if !order.is_empty() => Some(order),
                        _ => source,
                    };
                    let limit = query
                        .get("limit")
                        .and_then(|value| value.parse::<i64>().ok());
                    let descriptor = crate::filters::Descriptor {
                        title_of: &|field, scope| {
                            let base = match field {
                                "title" => "Title",
                                "studio" => "Studio",
                                "userRating" => "Rating",
                                "contentRating" => "Content Rating",
                                "year" => "Year",
                                "decade" => "Decade",
                                "originallyAvailableAt" => "Release Date",
                                "duration" => "Duration",
                                "unmatched" => "Unmatched",
                                "duplicate" => "Duplicate",
                                "genre" => "Genre",
                                "collection" => "Collection",
                                "director" => "Director",
                                "writer" => "Writer",
                                "producer" => "Producer",
                                "actor" => "Actor",
                                "country" => "Country",
                                "addedAt" => "Date Added",
                                "viewCount" => "Plays",
                                "lastViewedAt" => "Last Watched",
                                "unwatched" => "Unwatched",
                                "resolution" => "Resolution",
                                "subtitleLanguage" => "Subtitle Language",
                                "audioLanguage" => "Audio Language",
                                "inProgress" => "In Progress",
                                "editionTitle" => "Edition",
                                "contentRatingAge" => "Common Sense Media Age Rating",
                                "label" => "Label",
                                _ => "",
                            };
                            // The descriptor is type-specific.  A season
                            // query has no bare `title` rule in the measured
                            // 1.42.2 form, while an unprefixed field on an
                            // episode is titled from that requested type.
                            let type_title = match type_number {
                                1 => "Movie",
                                2 => "Show",
                                3 => "Season",
                                4 => "Episode",
                                _ => "",
                            };
                            if scope.is_empty() && type_number == 3 && field == "title" {
                                return String::new();
                            }
                            if base.is_empty() {
                                return String::new();
                            }
                            if scope.is_empty() {
                                if type_number == 4
                                    && matches!(field, "unwatched" | "inProgress" | "title")
                                {
                                    format!("{type_title} {base}")
                                } else {
                                    base.to_string()
                                }
                            } else {
                                let scope_title = match scope {
                                    "show" => "Show".to_string(),
                                    "episode" => "Episode".to_string(),
                                    "season" => "Season".to_string(),
                                    _ => canonical_tag(scope),
                                };
                                format!("{} {}", scope_title, base)
                            }
                        },
                        value_title_of: &|_field, value| {
                            self.catalog.items.iter().find_map(|row| {
                                [
                                    "Genre",
                                    "Collection",
                                    "Director",
                                    "Writer",
                                    "Producer",
                                    "Role",
                                    "Country",
                                    "Label",
                                    "Similar",
                                    "Network",
                                    "Mood",
                                    "Style",
                                ]
                                .iter()
                                .find_map(|name| {
                                    row.get(*name)
                                        .and_then(Value::as_array)
                                        .into_iter()
                                        .flatten()
                                        .find_map(|tag| {
                                            (string_value(tag.get("id").unwrap_or(&Value::Null))
                                                == value)
                                                .then(|| {
                                                    tag.get("tag")
                                                        .and_then(Value::as_str)
                                                        .map(str::to_string)
                                                })
                                                .flatten()
                                        })
                                })
                            })
                        },
                    };
                    let document = crate::filters::document_with(
                        type_number,
                        &tree,
                        order,
                        limit,
                        query.get("group").map(|value| json!([{"field":value}])),
                        &errors,
                        &descriptor,
                    );
                    return Response::json(200, document);
                }
                if segments[3] == "common" {
                    return self.route_section_common(section_key, query, request);
                }
                if segments.len() == 4
                    && matches!(
                        segments[3],
                        "emptyTrash" | "analyze" | "refresh" | "indexes"
                    )
                {
                    if segments[3] == "refresh" {
                        if let Some(force) = query.get("force") {
                            if force
                                .parse::<i64>()
                                .ok()
                                .filter(|value| *value == 0 || *value == 1)
                                .is_none()
                            {
                                return Response::error(400, "plexsim: bad request");
                            }
                        }
                    }
                    // Measured: a scan is a GET (or a POST) and its cancel a DELETE,
                    // and a PUT is a 404; emptyTrash and analyze are PUT only;
                    // indexes (Delete Preview Thumbnails) DELETE only.
                    let allowed = match segments[3] {
                        "refresh" => request.method != "PUT",
                        "indexes" => request.method == "DELETE",
                        _ => request.method == "PUT",
                    };
                    if !allowed {
                        return Response::error(404, "plexsim: not found");
                    }
                    let section = section.clone();
                    let section_key = section_key.to_string();
                    match segments[3] {
                        "analyze" => {
                            // Cycle 21: it is the LEAVES that are framed, three
                            // frames each, in title order and with no activity.
                            let rows = self
                                .sort_rows(self.section_rows(&section_key), Some("titleSort"))
                                .unwrap_or_default();
                            let mut leaves = Vec::new();
                            for row in rows {
                                if row.get("type").and_then(Value::as_str) == Some("show") {
                                    leaves.extend(self.leaves(&key_of(&row).unwrap_or_default()));
                                } else {
                                    leaves.push(row);
                                }
                            }
                            for leaf in leaves {
                                for media_state in [Some("analyzing"), Some("thumbnailing"), None] {
                                    self.notify_timeline(&leaf, 5, None, media_state, None);
                                }
                            }
                        }
                        "indexes" => {
                            // Cycle 21: the delete MOVES the section's own
                            // `updatedAt` and leaves every row's alone.
                            let stamp = now();
                            if let Some(stored) = self.catalog.sections.iter_mut().find(|candidate| {
                                candidate.get("key").map(string_value).as_deref() == Some(section_key.as_str())
                            }) {
                                stored["updatedAt"] = json!(stamp);
                            }
                            self.dirty = true;
                        }
                        "refresh" if request.method == "DELETE" => {}
                        "refresh" => self.refresh_section(&section_key, &section, query),
                        _ => {}
                    }
                    return Response::empty(200);
                }
                if segments.len() == 4 && segments[3] == "search" {
                    return self.route_section_search(section_key, section, query, pairs, request);
                }
                if segments.len() == 4 && segments[3] == "unwatched" && matches!(request.method.as_str(), "GET" | "HEAD") {
                    return self.unwatched_listing(section_key, section, query, pairs);
                }
                if segments.len() == 4 && segments[3] == "unwatched" {
                    // Cycle 20, measured on kids and zen: typed like a listing, it
                    // answers ONLY the library's own top-level type -- any other
                    // `type=` is an EMPTY container whose viewGroup flips to
                    // `episode` on a show library -- and an unparseable or empty
                    // `type=` is a 500.  Cycle 54: it runs the listing's filters;
                    // cycle 50: ordered by titleSort, and a row with children
                    // carries `?unwatched=1` in its key.
                    let kind = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie")
                        .to_string();
                    let asked = match listing::parsed_type(query, 500, None) {
                        Ok(asked) => asked,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    let own = if kind == "show" { 2 } else { 1 };
                    let asked = asked.unwrap_or(own);
                    let rows = if asked != own {
                        Vec::new()
                    } else {
                        self.choose_rows(Some(section_key), Some(own))
                            .into_iter()
                            .filter(|row| {
                                row.get("viewCount").is_none_or(|value| {
                                    value.is_null() || value.as_i64() == Some(0)
                                })
                            })
                            .collect::<Vec<_>>()
                    };
                    let filter_pairs = pairs
                        .iter()
                        .filter(|(name, _)| {
                            !matches!(
                                name.as_str(),
                                "sort" | "includeGuids" | "X-Plex-Container-Start"
                                    | "X-Plex-Container-Size" | "X-Plex-Language"
                            )
                        })
                        .cloned()
                        .collect::<Vec<_>>();
                    let rows = match self.filter_rows(rows, &filter_pairs, &kind) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    if let Some(sort) = query.get("sort") {
                        // Cycle 55: the leaf VALIDATES `sort=` and then ignores it.
                        if let Err(status) = self.sort_rows(Vec::new(), Some(sort)) {
                            return Response::error(status, "plexsim: bad request");
                        }
                    }
                    let rows = self.sort_rows(rows, Some("titleSort")).unwrap_or_default();
                    let listed = rows
                        .iter()
                        .map(|row| {
                            let mut listed = self.section_listing_row(row);
                            if let Some(object) = listed.as_object_mut() {
                                if let Some(key) = object.get("key").and_then(Value::as_str).map(str::to_string) {
                                    if key.ends_with("/children") {
                                        object.insert("key".to_string(), json!(format!("{key}?unwatched=1")));
                                    }
                                }
                            }
                            listed
                        })
                        .collect::<Vec<_>>();
                    let view_group = if kind == "show" && asked != own { "episode".to_string() } else { kind.clone() };
                    let mut fields = self.listing_fields(section_key, &kind, &view_group, "Unplayed");
                    fields.insert("Metadata".to_string(), Value::Array(listed));
                    page_fields(&mut fields, query);
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 4 && segments[3] == "categories" {
                    let mut names = HashSet::new();
                    let axis_type = query
                        .get("type")
                        .and_then(|value| value.parse::<i64>().ok());
                    for row in self.choose_rows(Some(section_key), axis_type) {
                        for tag in row
                            .get("Genre")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                        {
                            if let Some(name) = tag.get("tag").and_then(Value::as_str) {
                                names.insert(name.to_string());
                            }
                        }
                    }
                    let mut names = names.into_iter().collect::<Vec<_>>();
                    names.sort();
                    let rows = names
                        .into_iter()
                        .map(|name| {
                            json!({
                                "type":"directory",
                                "title":name,
                                "key":format!("/library/sections/{section_key}/all?genre={name}")
                            })
                        })
                        .collect::<Vec<_>>();
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("Directory".to_string(), Value::Array(rows)),
                                (
                                    "Meta".to_string(),
                                    json!({"Type":[{"type":"genre","title":"Genre"}]}),
                                ),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() == 4 && segments[3] == "folder" {
                    return self.route_section_folder(section_key, section, query, request);
                }
                if segments.len() == 4 && segments[3] == "search" {
                    return self.route_section_search(section_key, section, query, pairs, request);
                }
                if segments.len() == 4 && segments[3] == "location" {
                    // The location axis is the library's own Location id,
                    // rather than each Part's parent directory.  The path is
                    // descriptive only; clients follow fastKey and submit
                    // the numeric id back to the listing filter.
                    let rows = self
                        .section(section_key)
                        .and_then(|section| section.get("Location"))
                        .and_then(Value::as_array)
                        .map(|locations| {
                            locations
                                .iter()
                                .map(|location| {
                                    let id = location
                                        .get("id")
                                        .map(string_value)
                                        .unwrap_or_else(|| section_key.to_string());
                                    json!({
                                        "title": location.get("path").cloned().unwrap_or(Value::Null),
                                        "key": id,
                                        "fastKey": format!("/library/sections/{section_key}/all?location={id}")
                                    })
                                })
                                .collect::<Vec<_>>()
                        })
                        .filter(|rows| !rows.is_empty())
                        .unwrap_or_else(|| {
                            vec![json!({
                                "title": format!("/plexsim/{section_key}"),
                                "key": section_key,
                                "fastKey": format!("/library/sections/{section_key}/all?location={section_key}")
                            })]
                        });
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("title2".to_string(), json!("By Folder Location")),
                                ("Directory".to_string(), Value::Array(rows)),
                            ]),
                            false,
                        ),
                    );
                }
                if matches!(request.method.as_str(), "GET" | "HEAD") && segments.len() >= 5 {
                    // Cycle 16, swept on both references: a leaf with no bucket answers
                    // 404 to an extra segment, and so does a SECOND extra segment on a
                    // leaf that has one -- never the leaf itself again.
                    let bucketless = matches!(
                        segments[3],
                        "all" | "onDeck" | "recentlyAdded" | "newest" | "recentlyViewed"
                            | "allLeaves" | "collections" | "common" | "sorts" | "filters"
                            | "folder" | "search" | "unwatched" | "prefs" | "studio"
                            | "audioLanguage" | "subtitleLanguage" | "videoCodec" | "audioCodec"
                            | "audioLayout" | "location"
                    );
                    let deep_bucket = segments.len() >= 6
                        && segments[4] != "top"
                        && (segments[3] == "firstCharacter"
                            || matches!(
                                segments[3],
                                "genre" | "director" | "writer" | "producer" | "actor" | "country"
                                    | "network" | "collection" | "label" | "mood" | "style" | "year"
                                    | "decade" | "contentRating" | "resolution" | "userRating" | "rating"
                            ));
                    if (bucketless && segments[4] != "top") || deep_bucket {
                        return Response::error(404, "plexsim: not found");
                    }
                }
                if matches!(request.method.as_str(), "GET" | "HEAD") {
                    // The secondary axes and the bucket listings behind them, ported
                    // whole from server.py (see runtime/axes.rs).
                    if segments.len() == 4 {
                        if let Some(response) =
                            self.route_section_axis(section_key, section, segments[3], query, pairs)
                        {
                            return response;
                        }
                    }
                    if segments.len() == 5 && segments[3] == "firstCharacter" {
                        return self.first_character_listing(section_key, section, segments[4], query, pairs);
                    }
                    if segments.len() == 5 && segments[4] != "top" && !segments[4].is_empty() {
                        if let Some(response) =
                            self.axis_bucket(section_key, section, segments[3], segments[4], query, pairs)
                        {
                            return response;
                        }
                        // `server.py`: any other two-segment leaf is a bucket of an axis
                        // that has none -- `studio` and `platform` among them -- a 404.
                        if !matches!(
                            segments[3],
                            "query" | "indexes" | "firstCharacter" | "extras" | "composite" | "stations"
                        ) {
                            return Response::error(404, "plexsim: not found");
                        }
                    }
                }
                if segments.len() == 4
                    && matches!(
                        segments[3],
                        "genre"
                            | "year"
                            | "decade"
                            | "contentRating"
                            | "director"
                            | "country"
                            | "studio"
                            | "actor"
                            | "writer"
                            | "producer"
                            | "label"
                            | "collection"
                    )
                {
                    let axis = segments[3];
                    let mut names = HashSet::new();
                    let mut thumbs = HashMap::new();
                    let axis_type = query
                        .get("type")
                        .and_then(|value| value.parse::<i64>().ok());
                    for row in self.choose_rows(Some(section_key), axis_type) {
                        match axis {
                            "year" => {
                                if let Some(value) = row.get("year") {
                                    names.insert(string_value(value));
                                }
                            }
                            "decade" => {
                                if let Some(value) = row.get("year").and_then(Value::as_i64) {
                                    names.insert(format!("{}", value / 10 * 10));
                                }
                            }
                            "studio" => {
                                if let Some(value) = row.get("studio").and_then(Value::as_str) {
                                    names.insert(value.to_string());
                                }
                            }
                            "contentRating" => {
                                if let Some(value) =
                                    row.get("contentRating").and_then(Value::as_str)
                                {
                                    names.insert(value.to_string());
                                }
                            }
                            _ => {
                                let field = match axis {
                                    "genre" => "Genre",
                                    "director" => "Director",
                                    "country" => "Country",
                                    "actor" | "writer" | "producer" => {
                                        if axis == "actor" {
                                            "Role"
                                        } else if axis == "producer" {
                                            "Producer"
                                        } else {
                                            "Writer"
                                        }
                                    }
                                    "label" => "Label",
                                    "collection" => "Collection",
                                    _ => "",
                                };
                                for tag in row
                                    .get(field)
                                    .and_then(Value::as_array)
                                    .into_iter()
                                    .flatten()
                                {
                                    if let Some(name) = tag.get("tag").and_then(Value::as_str) {
                                        names.insert(name.to_string());
                                        if matches!(
                                            axis,
                                            "actor" | "director" | "writer" | "producer"
                                        ) {
                                            if let Some(thumb) =
                                                tag.get("thumb").and_then(Value::as_str)
                                            {
                                                thumbs.insert(name.to_string(), thumb.to_string());
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    let mut names = names.into_iter().collect::<Vec<_>>();
                    names.sort();
                    let rows = names
                        .into_iter()
                        .map(|name| {
                            let key = format!("/library/sections/{section_key}/all?{axis}={name}");
                            let key_value = if matches!(axis, "year" | "decade") {
                                name.clone()
                            } else {
                                key.clone()
                            };
                            let mut row = json!({"title":name,"key":key_value,"fastKey":key,"filter":format!("{axis}={name}"),"filterType":"string"});
                            if axis == "genre" {
                                row.as_object_mut().unwrap().insert("type".to_string(), json!("genre"));
                            }
                            if let Some(thumb) = thumbs.get(&name) {
                                row.as_object_mut().unwrap().insert("thumb".to_string(), json!(thumb));
                            }
                            row
                        })
                        .collect::<Vec<_>>();
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("viewGroup".to_string(), json!("secondary")),
                                ("content".to_string(), json!(axis)),
                                ("Directory".to_string(), Value::Array(rows)),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() == 4 && (segments[3] == "sorts" || segments[3] == "filters") {
                    let number = if let Some(value) = query.get("type") {
                        // Cycle 55: a signed 32-bit integer (`2147483648` is a 400, `+1`
                        // the movie list), measured on zen and kids.
                        if !int_param(value, Some(32)) {
                            return Response::error(400, "plexsim: bad request");
                        }
                        value.trim_start_matches('+').parse::<i64>().unwrap_or(0)
                    } else if section.get("type").and_then(Value::as_str) == Some("show") {
                        2
                    } else {
                        1
                    };
                    let name = if segments[3] == "sorts" {
                        "section_sorts.json"
                    } else {
                        "section_filters.json"
                    };
                    let mode = if query
                        .get("includeAdvanced")
                        .is_some_and(|value| is_true_option(value))
                    {
                        "advanced"
                    } else {
                        "plain"
                    };
                    let values = if segments[3] == "filters" && mode == "advanced" {
                        let mut plain = load_json_sidecar(&self.catalog_path, name)
                            .and_then(|doc| {
                                doc.get("plain")
                                    .and_then(|items| items.get(number.to_string()))
                                    .and_then(Value::as_array)
                                    .cloned()
                            })
                            .unwrap_or_default();
                        if let Some(extra) =
                            load_json_sidecar(&self.catalog_path, name).and_then(|doc| {
                                doc.get("advanced")
                                    .and_then(|items| items.get(number.to_string()))
                                    .and_then(Value::as_array)
                                    .cloned()
                            })
                        {
                            plain.extend(extra);
                        }
                        Value::Array(plain)
                    } else {
                        load_json_sidecar(&self.catalog_path, name)
                            .and_then(|doc| {
                                doc.get(mode)
                                    .and_then(|items| items.get(number.to_string()))
                                    .cloned()
                            })
                            .unwrap_or_else(|| Value::Array(Vec::new()))
                    };
                    let mut values = values;
                    replace_section_placeholder(&mut values, section_key);
                    let key = if segments[3] == "sorts" {
                        "Directory"
                    } else {
                        "Directory"
                    };
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                (key.to_string(), values),
                                (
                                    "content".to_string(),
                                    json!(if segments[3] == "sorts" {
                                        "sorts"
                                    } else {
                                        "secondary"
                                    }),
                                ),
                                ("viewGroup".to_string(), json!("secondary")),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() == 5 && segments[3] == "firstCharacter" && !segments[4].is_empty() {
                    // Cycle 16, measured on kids and zen: the bucket behind a
                    // letter is a real LISTING, not the axis.  The match is on the
                    // ROW's own STORED sort title (cycle 18: empty for a generated
                    // title, so every synthesised season answers in `#`), `#`
                    // also takes every ASCII non-letter, and the listing grammar
                    // applies on top, sorted by titleSort unless asked otherwise.
                    let wanted = percent_decode(segments[4]);
                    if query.get("type").is_some_and(|value| value.parse::<i64>().is_err()) {
                        return Response::error(500, "plexsim: bad request");
                    }
                    let section_type = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie")
                        .to_string();
                    let type_number = query.get("type").and_then(|value| value.parse::<i64>().ok());
                    let bucket = |row: &Value| {
                        let title = row
                            .as_object()
                            .map(crate::hierarchy::stored_sort_title)
                            .unwrap_or_default();
                        title
                            .chars()
                            .next()
                            .map(|first| first.to_uppercase().collect::<String>())
                            .unwrap_or_else(|| "#".to_string())
                    };
                    let rows = self
                        .choose_rows(Some(section_key), type_number)
                        .into_iter()
                        .filter(|row| {
                            type_number.is_some()
                                || row.get("type").and_then(Value::as_str) == Some(section_type.as_str())
                        })
                        .filter(|row| {
                            let first = bucket(row);
                            first == wanted
                                || (wanted == "#"
                                    && first.is_ascii()
                                    && !first.chars().all(|c| c.is_ascii_alphabetic()))
                        })
                        .collect::<Vec<_>>();
                    let filtered_pairs = pairs
                        .iter()
                        .filter(|(name, _)| {
                            !matches!(
                                name.as_str(),
                                "excludeFields" | "excludeElements" | "includeGuids"
                                    | "includeMeta" | "includeAdvanced" | "sort"
                                    | "X-Plex-Container-Start" | "X-Plex-Container-Size"
                                    | "X-Plex-Language" | "limit"
                            )
                        })
                        .cloned()
                        .collect::<Vec<_>>();
                    let rows = match self.filter_rows(rows, &filtered_pairs, &section_type) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    let sort = query.get("sort").map(String::as_str).unwrap_or("titleSort");
                    let rows = match self.sort_rows(rows, Some(sort)) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                    let projected = rows
                        .iter()
                        .map(|row| self.section_listing_row(row))
                        .collect::<Vec<_>>();
                    let mut fields = self.listing_fields(
                        section_key,
                        &section_type,
                        &section_type,
                        &format!("Start with '{wanted}'"),
                    );
                    fields.insert("Metadata".to_string(), Value::Array(projected));
                    page_fields(&mut fields, query);
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 4 && segments[3] == "firstCharacter" {
                    // Measured on zen's show library: the axis IS scoped by
                    // `?type=`, and (cycle 16) counts a row below the library's
                    // own top-level type by its SHOW's sort title.  Cycle 41:
                    // every ASCII non-letter is counted under `#`.
                    if query.get("type").is_some_and(|value| value.parse::<i64>().is_err()) {
                        return Response::error(500, "plexsim: bad request");
                    }
                    let section_type = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie")
                        .to_string();
                    let type_number = query.get("type").and_then(|value| value.parse::<i64>().ok());
                    let source = self
                        .choose_rows(Some(section_key), type_number)
                        .into_iter()
                        .filter(|row| {
                            type_number.is_some()
                                || row.get("type").and_then(Value::as_str) == Some(section_type.as_str())
                        })
                        .collect::<Vec<_>>();
                    let mut counts: HashMap<String, i64> = HashMap::new();
                    for row in &source {
                        let basis_key = match row.get("type").and_then(Value::as_str) {
                            Some("season") => row.get("parentRatingKey").map(string_value),
                            Some("episode") => row.get("grandparentRatingKey").map(string_value),
                            _ => None,
                        };
                        let basis = basis_key
                            .and_then(|key| self.catalog.get(&key))
                            .unwrap_or(row);
                        let title = basis
                            .get("titleSort")
                            .map(string_value)
                            .filter(|value| !value.is_empty())
                            .or_else(|| basis.get("title").map(string_value))
                            .unwrap_or_default();
                        let mut first = title
                            .chars()
                            .next()
                            .map(|first| first.to_uppercase().collect::<String>())
                            .unwrap_or_else(|| "#".to_string());
                        if first.is_ascii() && !first.chars().all(|c| c.is_ascii_alphabetic()) {
                            first = "#".to_string();
                        }
                        *counts.entry(first).or_default() += 1;
                    }
                    let mut letters = counts.into_iter().collect::<Vec<_>>();
                    letters.sort_by(|a, b| a.0.cmp(&b.0));
                    // The bucket KEY is percent-encoded, which `#` needs most of
                    // all: raw, it is a URL fragment.
                    let rows = letters.into_iter().map(|(letter,count)| json!({"size":count,"key":percent_encode(&letter),"title":letter})).collect::<Vec<_>>();
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([("Directory".to_string(), Value::Array(rows))]),
                            false,
                        ),
                    );
                }
                // `/axis/top` is its own measured tag-table endpoint.  It
                // must win before the generic `/axis/{value}` projection;
                // otherwise `top` is treated as a tag id and every table is
                // returned as an empty 200 (and wrong verbs are accepted).
                if segments.len() == 5 && segments[4] == "top" {
                    return self.route_section_top(section_key, segments[3], query, request);
                }
                if segments.len() == 5
                    && !segments[4].is_empty()
                    && matches!(
                        segments[3],
                        "firstCharacter"
                            | "genre"
                            | "director"
                            | "writer"
                            | "actor"
                            | "producer"
                            | "country"
                            | "studio"
                            | "rating"
                            | "tagline"
                            | "summary"
                            | "year"
                            | "decade"
                            | "contentRating"
                            | "resolution"
                    )
                {
                    let axis = segments[3];
                    let value = segments[4];
                    let axis_type = query
                        .get("type")
                        .and_then(|candidate| candidate.parse::<i64>().ok());
                    let mut rows = self.choose_rows(Some(section_key), axis_type);
                    if axis == "firstCharacter" {
                        rows.retain(|row| {
                            row.get("titleSort")
                                .or_else(|| row.get("title"))
                                .and_then(Value::as_str)
                                .and_then(|title| title.chars().next())
                                .map(|first| first.to_string() == value)
                                .unwrap_or(false)
                        });
                    } else if matches!(
                        axis,
                        "year" | "decade" | "contentRating" | "studio" | "resolution"
                    ) {
                        rows.retain(|row| {
                            let actual = match axis {
                                "year" => row.get("year").map(string_value),
                                "decade" => row
                                    .get("year")
                                    .and_then(Value::as_i64)
                                    .map(|year| (year / 10 * 10).to_string()),
                                "contentRating" | "studio" => row.get(axis).map(string_value),
                                "resolution" => row
                                    .get("Media")
                                    .and_then(Value::as_array)
                                    .and_then(|media| {
                                        media.iter().find_map(|item| item.get("videoResolution"))
                                    })
                                    .map(string_value),
                                _ => None,
                            };
                            actual.as_deref() == Some(value)
                        });
                    } else {
                        let field = match axis {
                            "genre" => "Genre",
                            "director" => "Director",
                            "writer" => "Writer",
                            "actor" | "tagline" => {
                                if axis == "actor" {
                                    "Role"
                                } else {
                                    "tagline"
                                }
                            }
                            "producer" => "Producer",
                            "country" => "Country",
                            "rating" => "Writer",
                            "summary" => "summary",
                            _ => "",
                        };
                        rows.retain(|row| {
                            row.get(field)
                                .and_then(Value::as_array)
                                .is_some_and(|tags| {
                                    tags.iter().any(|tag| {
                                        string_value(tag.get("id").unwrap_or(&Value::Null)) == value
                                            || tag.get("tag").and_then(Value::as_str) == Some(value)
                                    })
                                })
                        });
                    }
                    let mut projected = rows
                        .iter()
                        .map(|row| self.section_listing_row(row))
                        .collect::<Vec<_>>();
                    let title = if axis == "firstCharacter" {
                        format!("Start with '{value}'")
                    } else if axis == "contentRating" {
                        format!("Rated {value}")
                    } else {
                        value.to_string()
                    };
                    let mut fields = Map::from_iter([
                        ("title2".to_string(), json!(title)),
                        (
                            "Metadata".to_string(),
                            Value::Array(std::mem::take(&mut projected)),
                        ),
                    ]);
                    if axis == "firstCharacter" {
                        fields.insert(
                            "viewGroup".to_string(),
                            json!(section
                                .get("type")
                                .and_then(Value::as_str)
                                .unwrap_or("movie")),
                        );
                    }
                    return Response::json(200, self.container(fields, true));
                }
                if segments.len() == 4
                    && matches!(
                        segments[3],
                        "resolution" | "audioLanguage" | "subtitleLanguage"
                    )
                {
                    let axis = segments[3];
                    let mut seen = HashSet::new();
                    for row in self.choose_rows(
                        Some(section_key),
                        query
                            .get("type")
                            .and_then(|value| value.parse::<i64>().ok()),
                    ) {
                        for media in row
                            .get("Media")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                        {
                            if axis == "resolution" {
                                if let Some(value) =
                                    media.get("videoResolution").and_then(Value::as_str)
                                {
                                    seen.insert(value.to_string());
                                }
                            } else {
                                let stream_type = if axis == "audioLanguage" { 2 } else { 3 };
                                for part in media
                                    .get("Part")
                                    .and_then(Value::as_array)
                                    .into_iter()
                                    .flatten()
                                {
                                    for stream in part
                                        .get("Stream")
                                        .and_then(Value::as_array)
                                        .into_iter()
                                        .flatten()
                                    {
                                        if stream.get("streamType").and_then(Value::as_i64)
                                            == Some(stream_type)
                                        {
                                            if let Some(value) = stream
                                                .get("language")
                                                .or_else(|| stream.get("languageCode"))
                                                .and_then(Value::as_str)
                                            {
                                                seen.insert(value.to_string());
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    let mut values = seen.into_iter().collect::<Vec<_>>();
                    values.sort();
                    let rows = values.into_iter().map(|value| json!({"key":value,"title":if axis == "resolution" { format!("{value}p") } else { value }})).collect::<Vec<_>>();
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("viewGroup".to_string(), json!("secondary")),
                                (
                                    "title2".to_string(),
                                    json!(format!("By {}", canonical_tag(axis))),
                                ),
                                ("Directory".to_string(), Value::Array(rows)),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() == 4 && segments[3] == "albums" {
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([("Directory".to_string(), Value::Array(Vec::new()))]),
                            false,
                        ),
                    );
                }
                if segments.len() == 4 && segments[3] == "collection" && request.method == "GET" {
                    return self.route_section_collection_axis(section_key);
                }
                if segments[3] == "prefs" && request.method == "PUT" {
                    let kind = section
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("movie");
                    let definitions = self.section_pref_rows(section_key, kind);
                    let known = definitions
                        .iter()
                        .filter_map(|row| row.get("id").and_then(Value::as_str))
                        .collect::<HashSet<_>>();
                    if query
                        .keys()
                        .any(|name| !name.starts_with("X-Plex-") && !known.contains(name.as_str()))
                    {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let types = definitions
                        .iter()
                        .filter_map(|row| {
                            Some((
                                row.get("id")?.as_str()?.to_string(),
                                row.get("type")
                                    .and_then(Value::as_str)
                                    .unwrap_or("text")
                                    .to_string(),
                            ))
                        })
                        .collect::<HashMap<_, _>>();
                    for (name, value) in query {
                        if !name.starts_with("X-Plex-") {
                            let Some(kind) = types.get(name).map(String::as_str) else {
                                continue;
                            };
                            // PMS accepts a malformed integer write as a
                            // successful write of zero. It canonicalizes
                            // booleans and enum delimiters on readback.
                            let stored = match kind {
                                "int" | "integer" => value
                                    .parse::<i64>()
                                    .map(|number| number.to_string())
                                    .unwrap_or_else(|_| "0".to_string()),
                                "bool" => {
                                    if is_true_option(value) {
                                        "true".to_string()
                                    } else {
                                        "false".to_string()
                                    }
                                }
                                _ => escaped_pref(value),
                            };
                            self.section_prefs
                                .insert((section_key.to_string(), name.clone()), stored);
                        }
                    }
                    if let Some(object) = self
                        .catalog
                        .sections
                        .iter_mut()
                        .find(|known| {
                            string_value(known.get("key").unwrap_or(&Value::Null)) == section_key
                        })
                        .and_then(Value::as_object_mut)
                    {
                        object.insert("updatedAt".to_string(), json!(now()));
                    }
                    self.catalog.document.insert(
                        "sections".to_string(),
                        Value::Array(self.catalog.sections.clone()),
                    );
                    self.dirty = true;
                    return Response::empty(200);
                }
                if segments[3] == "prefs" && request.method == "GET" {
                    let settings = if query
                        .get("agent")
                        .is_some_and(|agent| agent == "tv.plex.agents.none")
                    {
                        let type_number =
                            if section.get("type").and_then(Value::as_str) == Some("show") {
                                "2"
                            } else {
                                "1"
                            };
                        load_json_sidecar(&self.catalog_path, "section_prefs_new.json")
                            .and_then(|document| {
                                let ids = document
                                    .get("lists")
                                    .and_then(|lists| lists.get(type_number))
                                    .and_then(Value::as_array)?;
                                let definitions =
                                    document.get("settings").and_then(Value::as_object)?;
                                Some(
                                    ids.iter()
                                        .filter_map(|id| {
                                            id.as_str().and_then(|id| definitions.get(id)).cloned()
                                        })
                                        .collect::<Vec<_>>(),
                                )
                            })
                            .unwrap_or_else(|| {
                                self.section_pref_rows(
                                    section_key,
                                    section
                                        .get("type")
                                        .and_then(Value::as_str)
                                        .unwrap_or("movie"),
                                )
                            })
                    } else {
                        self.section_pref_rows(
                            section_key,
                            section
                                .get("type")
                                .and_then(Value::as_str)
                                .unwrap_or("movie"),
                        )
                    };
                    let mut settings = settings;
                    // Cycle 25, measured on kids and zen: `X-Plex-Platform: Samsung` --
                    // that exact spelling, the header beating the query -- appends a
                    // HIDDEN twentieth setting to an existing library's prefs.
                    let platform = request
                        .headers
                        .get("x-plex-platform")
                        .or_else(|| query.get("X-Plex-Platform"));
                    if platform.map(String::as_str) == Some("Samsung") {
                        settings.push(json!({
                            "id": "includeInGlobal", "label": "", "summary": "", "type": "bool",
                            "default": "true", "value": "true", "hidden": "1", "advanced": "0",
                            "group": ""}));
                    }
                    return Response::json(
                        200,
                        json!({"MediaContainer": {"size": settings.len(), "Setting": settings}}),
                    );
                }
                if segments[3] == "collections" {
                    return self.route_section_collections(section_key, query);
                }
                if segments[3] == "autocomplete" {
                    if request.method != "GET" && request.method != "HEAD" {
                        return Response::error(404, "plexsim: not found");
                    }
                    let Some(type_number) = query
                        .get("type")
                        .and_then(|value| value.parse::<i64>().ok())
                    else {
                        return Response::error(400, "plexsim: bad request");
                    };
                    if query
                        .get("limit")
                        .is_some_and(|value| value.parse::<usize>().is_err())
                    {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let probes = query
                        .iter()
                        .filter(|(name, _)| name.ends_with(".query"))
                        .collect::<Vec<_>>();
                    if probes.len() != 1 {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let (probe, wanted) = probes[0];
                    if wanted.is_empty() {
                        return Response::error(400, "plexsim: bad request");
                    }
                    let field = probe.trim_end_matches(".query");
                    if field == "decade" {
                        return Response::error(500, "plexsim: bad request");
                    }
                    let mut fields = Map::new();
                    if field == "title" {
                        let mut rows = Vec::new();
                        for row in self.choose_rows(Some(section_key), Some(type_number)) {
                            let title =
                                row.get("title").and_then(Value::as_str).unwrap_or_default();
                            let lower_title = title.to_lowercase();
                            let lower_wanted = wanted.to_lowercase();
                            let range = title
                                .char_indices()
                                .filter(|(index, _)| {
                                    *index == 0
                                        || title[..*index]
                                            .chars()
                                            .last()
                                            .is_some_and(|character| !character.is_alphanumeric())
                                })
                                .find_map(|(index, _)| {
                                    lower_title[index..]
                                        .starts_with(&lower_wanted)
                                        .then_some((index, index + wanted.len() - 1))
                                });
                            if let Some((start, end)) = range {
                                let mut projected = self.listing_row(&row);
                                if let Some(object) = projected.as_object_mut() {
                                    object.insert(
                                        "title.queryRange".to_string(),
                                        json!(format!("{start},{end}")),
                                    );
                                }
                                rows.push(projected);
                            }
                        }
                        if let Some(limit) = query
                            .get("limit")
                            .and_then(|value| value.parse::<usize>().ok())
                        {
                            rows.truncate(limit);
                        }
                        fields.insert("Metadata".to_string(), Value::Array(rows));
                    } else {
                        let (tag_name, tag_type) = match field {
                            "genre" => ("Genre", 1),
                            "collection" => ("Collection", 2),
                            "director" => ("Director", 4),
                            "writer" => ("Writer", 5),
                            "actor" | "role" => ("Role", 6),
                            "producer" => ("Producer", 7),
                            "country" => ("Country", 8),
                            _ => return Response::error(400, "plexsim: bad request"),
                        };
                        let needle = wanted.to_lowercase();
                        let mut seen = HashSet::new();
                        let rows = self
                            .choose_rows(Some(section_key), Some(type_number))
                            .into_iter()
                            .flat_map(|row| {
                                row.get(tag_name)
                                    .and_then(Value::as_array)
                                    .cloned()
                                    .unwrap_or_default()
                            })
                            .filter_map(|tag| {
                                let text = tag.get("tag").and_then(Value::as_str)?.to_string();
                                if !text.to_lowercase().starts_with(&needle) || !seen.insert(text.clone()) {
                                    return None;
                                }
                                let id = string_value(tag.get("id").unwrap_or(&Value::Null));
                                Some(json!({"tag":text,"id":tag.get("id").cloned().unwrap_or(Value::Null),"tagType":tag_type,"filter":format!("{field}={id}")}))
                            })
                            .collect::<Vec<_>>();
                        fields.insert("Directory".to_string(), Value::Array(rows));
                    }
                    return Response::json(200, self.container(fields, true));
                }
                if segments[3] == "all" {
                    return self.route_section_all(
                        section_key,
                        &segments[4..],
                        query,
                        pairs,
                        request,
                    );
                }
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.starts_with(&["library", "metadata"]) && segments.len() >= 3 {
            let key = segments[2].to_string();
            if key.contains(',') && request.method == "PUT" && segments.len() == 3 {
                if query
                    .keys()
                    .all(|name| name.starts_with("X-Plex-") || name == "includeExternalMedia")
                {
                    return Response::error(400, "plexsim: bad request");
                }
                let keys = key
                    .split(',')
                    .filter(|value| !value.is_empty())
                    .map(str::to_string)
                    .collect::<Vec<_>>();
                if keys.iter().any(|child| self.catalog.get(child).is_none()) {
                    return Response::error(404, "plexsim: not found");
                }
                let pairs = Self::request_pairs(request);
                return self.apply_edit(&keys, &pairs, false, "loading");
            }
            if key.contains(',') && request.method == "GET" && segments.len() > 3 {
                let members = key
                    .split(',')
                    .filter(|member| self.catalog.get(*member).is_some())
                    .collect::<Vec<_>>();
                if members.len() == 1 {
                    if let Some(row) = self.catalog.get(members[0]).cloned() {
                        return self.route_metadata(
                            members[0],
                            &row,
                            &segments[3..],
                            query,
                            request,
                        );
                    }
                }
                return Response::error(404, "plexsim: not found");
            }
            if key.contains(',')
                && matches!(request.method.as_str(), "GET" | "HEAD")
                && segments.len() == 3
            {
                let members = key
                    .split(',')
                    .filter_map(|member| self.catalog.get(member).cloned())
                    .collect::<Vec<_>>();
                if members.is_empty() {
                    return Response::error(404, "plexsim: not found");
                }
                let first = members.first().cloned().unwrap_or(Value::Null);
                let section_key =
                    string_value(first.get("librarySectionID").unwrap_or(&Value::Null));
                let section = self.section(&section_key).cloned().unwrap_or_default();
                let mut fields = Map::from_iter([
                    ("allowSync".to_string(), json!(true)),
                    (
                        "identifier".to_string(),
                        json!("com.plexapp.plugins.library"),
                    ),
                    ("librarySectionID".to_string(), json!(section_key)),
                    (
                        "librarySectionTitle".to_string(),
                        section.get("title").cloned().unwrap_or(Value::Null),
                    ),
                    (
                        "librarySectionUUID".to_string(),
                        section.get("uuid").cloned().unwrap_or(Value::Null),
                    ),
                    ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
                    (
                        "mediaTagVersion".to_string(),
                        json!(MEDIA_TAG_VERSION.to_string()),
                    ),
                    (
                        "Metadata".to_string(),
                        Value::Array(
                            members
                                .iter()
                                .map(|member| self.detail_row(member))
                                .collect(),
                        ),
                    ),
                ]);
                fields.insert("size".to_string(), json!(members.len()));
                return Response::json(
                    200,
                    Value::Object(Map::from_iter([(
                        "MediaContainer".to_string(),
                        Value::Object(fields),
                    )])),
                );
            }
            if let Some(row) = self.catalog.get(&key).cloned() {
                return self.route_metadata(&key, &row, &segments[3..], query, request);
            }
            if let Some(minted) = self.theme_rows.get(&key).cloned() {
                // Cycle 45: a row the theme metadata leaf minted is readable at its own
                // key, and only as a bare read.
                if segments.len() > 3 || !matches!(request.method.as_str(), "GET" | "HEAD") {
                    return Response::error(404, "plexsim: not found");
                }
                return Response::json(200, self.container(Map::from_iter([
                    ("size".to_string(), json!(1)),
                    ("Metadata".to_string(), json!([minted])),
                ]), true));
            }
            if let Some(playlist) = self.playlists.get(&key).cloned() {
                if segments.len() == 3 && request.method == "DELETE" {
                    // Cycle 18, measured on plexscratch: `DELETE
                    // /library/metadata/{playlist}` is a 200 with an EMPTY body that
                    // deletes nothing; only `/playlists/{id}` really removes it.
                    return Response::empty(200);
                }
                return self.route_playlist_metadata(&playlist, &segments[3..], query, request);
            }
            if let Some(collection) = self.collections.get(&key).cloned() {
                return self.route_collection_metadata(&collection, &segments[3..], query, request);
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.starts_with(&["library", "streams"]) && segments.len() >= 3 {
            if segments.len() > 3 {
                return Response::error(404, "plexsim: not found");
            }
            let raw_id = segments[2];
            // Measured on both servers: `.srt` and `.vtt` are the sidecar extraction by
            // extension, answered as the bare stream; any other dot stays in the id.
            let bare_id = raw_id
                .strip_suffix(".srt")
                .or_else(|| raw_id.strip_suffix(".vtt"))
                .unwrap_or(raw_id);
            let stream_id = (!bare_id.is_empty() && bare_id.bytes().all(|b| b.is_ascii_digit()))
                .then(|| bare_id.parse::<i64>().ok())
                .flatten();
            let Some(stream_id) = stream_id else {
                // Measured on the copy: a PUT to an id it does not have is a 400, and so
                // is a DELETE of a NON-NUMERIC one -- `DELETE /library/streams/abc` is a
                // 400 where `DELETE /library/streams/999999` is a 404.
                if request.method == "PUT" || request.method == "DELETE" {
                    return Response::error(400, "plexsim: bad request");
                }
                return Response::error(404, "plexsim: not found");
            };
            if request.method == "GET" || request.method == "HEAD" {
                for row in &self.catalog.items {
                    for media in row
                        .get("Media")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                    {
                        for part in media
                            .get("Part")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                        {
                            for stream in part
                                .get("Stream")
                                .and_then(Value::as_array)
                                .into_iter()
                                .flatten()
                            {
                                if stream.get("id").and_then(Value::as_i64) == Some(stream_id) {
                                    let Some(content) =
                                        stream.get("_content").and_then(Value::as_str)
                                    else {
                                        return Response::error(501, "plexsim: not implemented");
                                    };
                                    return Response {
                                        status: 200,
                                        content_type: if raw_id.ends_with(".vtt") {
                                            "text/vtt".to_string()
                                        } else {
                                            "text/plain".to_string()
                                        },
                                        body: content.as_bytes().to_vec(),
                                        headers: Vec::new(),
                                        close: false,
                                    };
                                }
                            }
                        }
                    }
                }
                return Response::error(404, "plexsim: not found");
            }
            if request.method == "PUT" {
                // Measured on a writable copy: a PUT to a stream it HAS is 200 with an
                // empty body and stores nothing; one it does not have is a 400.
                let known = self.catalog.items.iter().any(|row| {
                    row.get("Media").and_then(Value::as_array).into_iter().flatten().any(|media| {
                        media.get("Part").and_then(Value::as_array).into_iter().flatten().any(
                            |part| {
                                part.get("Stream").and_then(Value::as_array).into_iter().flatten().any(
                                    |stream| stream.get("id").and_then(Value::as_i64) == Some(stream_id),
                                )
                            },
                        )
                    })
                });
                if known {
                    return Response::empty(200);
                }
                return Response::error(400, "plexsim: bad request");
            }
            if request.method == "DELETE" {
                let mut found = false;
                let mut uploaded = false;
                for row in &mut self.catalog.items {
                    for media in row
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
                            if let Some(streams) =
                                part.get_mut("Stream").and_then(Value::as_array_mut)
                            {
                                if let Some(index) = streams.iter().position(|stream| {
                                    stream.get("id").and_then(Value::as_i64) == Some(stream_id)
                                }) {
                                    found = true;
                                    uploaded = streams[index].get("_content").is_some();
                                    if uploaded {
                                        streams.remove(index);
                                    }
                                }
                            }
                        }
                    }
                }
                if !found {
                    return Response::error(404, "plexsim: not found");
                }
                if !uploaded {
                    return Response::error(403, "plexsim: forbidden");
                }
                self.dirty = true;
                self.save_if_dirty();
                return Response::empty(200);
            }
            return Response::error(404, "plexsim: not found");
        }
        if segments.starts_with(&["library", "parts"]) && segments.len() >= 3 {
            return self.route_part(&segments[2..], query, request);
        }
        if segments.first() == Some(&"playlists") {
            return self.route_playlist_path(&segments[1..], query, request);
        }
        if segments.first() == Some(&"library") && segments.get(1) == Some(&"collections") {
            return self.route_collection_path(&segments[2..], query, request);
        }
        if path == "/myplex/account" || path == "/accounts" {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([(
                        "Account".to_string(),
                        json!([{"id":1,"title":self.owner_username,"email":self.owner_email}]),
                    )]),
                    false,
                ),
            );
        }
        if path == "/library" {
            return Response::error(404, "plexsim: not found");
        }
        if path == "/system" || path.starts_with("/system/") {
            // Framework misses are deliberately bodyless.  The normal
            // library error document is a text/plain route-level refusal and
            // is observably different from a missing framework method.
            return Response {
                status: 404,
                content_type: String::new(),
                body: Vec::new(),
                headers: Vec::new(),
                close: false,
            };
        }
        let _ = pairs;
        Response::error(404, "plexsim: not found")
    }

    fn route_plugin_prefix(&self, path: &str) -> Option<Response> {
        if path == "/l" {
            return Some(Response::json(
                200,
                plugin_container(vec![
                    ("epg", false),
                    ("augmentation", false),
                    ("dvrs", false),
                    ("shared", false),
                    ("log", false),
                    ("sessions", false),
                    ("library", true),
                ]),
            ));
        }
        let names = PLUGIN_REGISTRY
            .iter()
            .filter(|registered| registered.starts_with(path) && registered.len() > path.len())
            .filter_map(|registered| registered.rsplit('/').next())
            .collect::<HashSet<_>>();
        if names.is_empty()
            && !PLUGIN_REGISTRY.iter().any(|registered| *registered == path)
            && !PREFIX_PROBES.contains(&path)
        {
            return None;
        }
        let mut names = names.into_iter().collect::<Vec<_>>();
        names.sort_unstable();
        let has_library = names.iter().any(|name| *name == "library");
        names.retain(|name| *name != "library");
        let mut rows = names
            .into_iter()
            .map(|name| (name, false))
            .collect::<Vec<_>>();
        if has_library {
            rows.push(("library", true));
        }
        Some(Response::json(200, plugin_container(rows)))
    }

    /// The fields a section LISTING's container carries (server.py's
    /// `listing_container`): the librarySection triple, `title1`, `title2`,
    /// `viewGroup`, `viewMode`, the library-type art and thumb, `allowSync` true,
    /// and `nocache` on a SHOW library whatever the paging (re-measured on both
    /// servers).
    fn listing_fields(&self, section_key: &str, section_type: &str, view_group: &str, title2: &str) -> Map<String, Value> {
        let section = self.section(section_key);
        let title = section.and_then(|section| section.get("title")).cloned().unwrap_or(Value::Null);
        let mut fields = Map::new();
        fields.insert(
            "librarySectionID".to_string(),
            section_key.parse::<i64>().map(|id| json!(id)).unwrap_or_else(|_| json!(section_key)),
        );
        fields.insert("librarySectionTitle".to_string(), title.clone());
        fields.insert(
            "librarySectionUUID".to_string(),
            section.and_then(|section| section.get("uuid")).cloned().unwrap_or(Value::Null),
        );
        fields.insert("title1".to_string(), title);
        fields.insert("title2".to_string(), json!(title2));
        fields.insert("viewGroup".to_string(), json!(view_group));
        fields.insert("viewMode".to_string(), json!(65592));
        let show = section_type == "show";
        fields.insert(
            "art".to_string(),
            json!(if show { "/:/resources/show-fanart.jpg" } else { "/:/resources/movie-fanart.jpg" }),
        );
        fields.insert(
            "thumb".to_string(),
            json!(if show { "/:/resources/show.png" } else { "/:/resources/movie.png" }),
        );
        fields.insert("allowSync".to_string(), json!(true));
        if show {
            fields.insert("nocache".to_string(), json!(true));
        }
        fields.retain(|_, value| !value.is_null());
        fields
    }

    fn route_section_all(
        &mut self,
        section_key: &str,
        tail: &[&str],
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
        request: &Request,
    ) -> Response {
        if pairs.iter().filter(|(name, _)| name == "type").count() > 1 {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                    true,
                ),
            );
        }
        if let Some(value) = query.get("type") {
            // Cycle 55, measured on zen and kids: the type is a SIGNED 32-BIT integer --
            // `type=2147483648` is refused with the same status as `abc`.
            if !int_param(value.trim(), Some(32)) {
                return Response::error(
                    if request.method == "PUT" { 400 } else { 500 },
                    "plexsim: bad request",
                );
            }
        }
        if let Some(value) = query.get("limit") {
            if value.parse::<i64>().is_err() {
                return Response::error(500, "plexsim: bad request");
            }
        }
        if matches!(request.method.as_str(), "GET" | "HEAD") && tail.is_empty() {
            // Cycle 45: `file=` is refused by the QUERY's type -- on a section listing
            // `type=2`, and a SHOW library's bare listing is that type.
            let default_type = self
                .section(section_key)
                .and_then(|section| section.get("type"))
                .and_then(Value::as_str)
                .map(|kind| if kind == "show" { "2" } else { "1" });
            if let Some(status) = listing::file_filter_refusal(query, false, default_type) {
                return Response::error(status, "plexsim: cannot filter on file");
            }
        }
        if tail.first() == Some(&"refresh") {
            return Response::empty(200);
        }
        if tail.is_empty() && matches!(request.method.as_str(), "GET" | "HEAD") {
            // Cycle 45: `file=` refuses at type=2 on a section listing -- and a SHOW
            // library's bare listing is the type=2 case.
            let default_type = if self
                .section(section_key)
                .and_then(|section| section.get("type"))
                .and_then(Value::as_str)
                == Some("show")
            {
                "2"
            } else {
                "1"
            };
            if let Some(status) = listing::file_filter_refusal(query, false, Some(default_type)) {
                return Response::error(status, "plexsim: cannot filter on file");
            }
        }
        if tail.first() == Some(&"collections") {
            if !matches!(request.method.as_str(), "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            return self.route_collections(query);
        }
        if !matches!(request.method.as_str(), "GET" | "HEAD" | "PUT") {
            return Response::error(404, "plexsim: not found");
        }
        if request.method == "PUT" {
            let Some(type_number) = query.get("type").and_then(|v| v.parse::<i64>().ok()) else {
                return Response::error(400, "plexsim: bad request");
            };
            if query.keys().any(|name| {
                name.starts_with("movie.")
                    || name.starts_with("show.")
                    || name.starts_with("season.")
                    || name.starts_with("episode.")
                    || name.contains(">>")
                    || name.contains("<<")
                    || name.contains("!=")
            }) {
                return Response::error(400, "plexsim: bad request");
            }
            let selected = query.get("id").cloned().unwrap_or_default();
            if pairs.iter().filter(|(name, _)| name == "id").count() > 1 {
                // Cycle 18, measured on plexscratch: a REPEATED `id=` edits
                // NOTHING -- a 200 in which neither row moves -- where the comma
                // list edits both.
                return Response::empty(200);
            }
            let mut candidates = self.choose_rows(Some(section_key), Some(type_number));
            for (field, value) in query {
                if let Some(base) = field.strip_suffix('=') {
                    candidates.retain(|row| {
                        row.get(base)
                            .map(string_value)
                            .is_some_and(|actual| actual == *value)
                    });
                }
            }
            let mut keys = if selected.is_empty() {
                candidates
                    .into_iter()
                    .filter_map(|row| key_of(&row))
                    .collect::<Vec<_>>()
            } else {
                selected.split(',').map(str::to_string).collect::<Vec<_>>()
            };
            keys.retain(|key| {
                if type_number == 18 {
                    return self
                        .collections
                        .get(key)
                        .is_some_and(|collection| collection.section == section_key);
                }
                self.catalog.get(key).is_some_and(|row| {
                    string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section_key
                        && type_number_for(row) == Some(type_number)
                })
            });
            let edits = query
                .iter()
                .filter(|(field, _)| {
                    field.contains(".value") || field.contains(".locked") || field.contains(".tag")
                })
                .map(|(field, value)| (field.clone(), value.clone()))
                .collect::<HashMap<_, _>>();
            let (lists, rows): (Vec<String>, Vec<String>) = keys
                .into_iter()
                .partition(|key| self.collections.contains_key(key));
            for key in lists {
                if !edits.is_empty() {
                    // Cycles 58/61: a taken name is a 409 the save must answer.
                    let answer = self.edit_collection(&key, &edits);
                    if answer.status != 200 {
                        return answer;
                    }
                }
            }
            let pairs = Self::request_pairs(request);
            return self.apply_edit(&rows, &pairs, true, "queued");
        }
        let type_number = query
            .get("type")
            .and_then(|value| value.parse::<i64>().ok());
        if type_number == Some(18) {
            // Cycle 14: a `type=18` listing is a real query -- `title=` filters it,
            // `sort=` orders it, and the default order is titleSort.
            let rows = match self.section_collection_rows(
                section_key,
                pairs,
                query.get("sort").map(String::as_str),
            ) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
            let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(rows))]);
            page_fields(&mut fields, query);
            return Response::json(200, self.container(fields, true));
        }
        let rows = self
            .choose_rows(Some(section_key), type_number)
            .into_iter()
            .filter(|row| {
                if type_number.is_none() {
                    matches!(
                        row.get("type").and_then(Value::as_str),
                        Some("movie" | "show")
                    )
                } else {
                    true
                }
            })
            .collect::<Vec<_>>();
        let section_type = self
            .section(section_key)
            .and_then(|section| section.get("type").and_then(Value::as_str))
            .unwrap_or("movie");
        let mut rows = if let Some(location) = query.get("location") {
            if location.chars().all(|character| character.is_ascii_digit()) {
                rows.into_iter()
                    .filter(|row| {
                        string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                            == *location
                    })
                    .collect::<Vec<_>>()
            } else {
                return Response::error(500, "plexsim: bad request");
            }
        } else {
            rows
        };
        // Projection, paging and client identity options are container
        // controls.  They must reach the serializer below, never the filter
        // parser: treating `excludeFields=summary` as an unknown row field
        // makes an otherwise valid listing silently empty.
        let filtered_pairs = pairs
            .iter()
            .filter(|(name, _)| {
                !matches!(
                    name.as_str(),
                    "location"
                        | "excludeFields"
                        | "excludeElements"
                        | "includeGuids"
                        | "includePreferences"
                        | "includeChildren"
                        | "includeExtras"
                        | "includeOnDeck"
                        | "includeRelated"
                        | "includeRelatedCount"
                        | "includeMeta"
                        | "includeAdvanced"
                        | "includeStorage"
                        | "includeChapters"
                        | "includeReviews"
                        | "includeAlternates"
                        | "X-Plex-Container-Start"
                        | "X-Plex-Container-Size"
                        | "X-Plex-Language"
                )
            })
            .cloned()
            .collect::<Vec<_>>();
        rows = match self.filter_rows(rows, &filtered_pairs, section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        // Cycle 18/20: the listing refusals (`parentIndex`, a `resolution` outside the
        // enum, an unparseable `sourceType`).
        if let Some(status) = listing::listing_refusals(query, &[]) {
            return Response::error(status, "plexsim: bad request");
        }
        // Cycle 55, measured on zen and kids: a `sourceType` other than 1 ORDERS the
        // grid by id; an explicit `sort=` still wins.
        let by_id = crate::filters::source_order(query.get("sourceType").map(String::as_str))
            .ok()
            .flatten()
            .is_some();
        let sort = query
            .get("sort")
            .filter(|sort| !sort.is_empty())
            .map(String::as_str)
            .or(if by_id {
                Some("id")
            } else if section_type == "show"
                && matches!(query.get("type").map(String::as_str), Some("3" | "4"))
            {
                // Cycle 16: a show library's season and episode grids keep the SHOW's
                // order, which the typed rows already come in.
                None
            } else {
                // Measured on both servers: the DEFAULT order of a listing is
                // `titleSort` ascending.
                Some("titleSort")
            });
        let rows = match self.sort_rows(rows, sort) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        let rows = if let Some(group) = query.get("group") {
            match self.grouped(rows, group) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            }
        } else {
            rows
        };
        let rows = match listing::limited(rows, query) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        let exclude_media = query
            .get("excludeElements")
            .is_some_and(|value| value.split(',').any(|name| name == "Media"));
        let excluded_elements = query
            .get("excludeElements")
            .map(|value| value.split(',').collect::<HashSet<_>>())
            .unwrap_or_default();
        let excluded_fields = query
            .get("excludeFields")
            .map(|value| value.split(',').collect::<HashSet<_>>())
            .unwrap_or_default();
        // Measured on kids and zen: `includeStorage=1` puts `durationTotal` and
        // `storageTotal` -- JSON STRINGS (cycle 27) -- on the library's OWN top-level
        // type only; a show carries the sum over its episodes.
        let own_type = if section_type == "show" { 2 } else { 1 };
        let with_storage = query.get("includeStorage").is_some_and(|value| {
            bool_option(value) && value.trim_start_matches('+').trim_start_matches('0') == "1"
        }) && query.get("type").map_or(true, |value| {
            value.is_empty() || value.trim_start_matches('+').parse::<i64>().ok() == Some(own_type)
        });
        let rows = rows
            .iter()
            .map(|row| {
                let mut projected = self.section_listing_row(row);
                if with_storage {
                    let leaves = if row.get("Media").is_some() {
                        vec![row.clone()]
                    } else {
                        self.leaves(&key_of(row).unwrap_or_default())
                    };
                    let duration = leaves
                        .iter()
                        .map(|leaf| leaf.get("duration").and_then(Value::as_i64).unwrap_or(0))
                        .sum::<i64>();
                    let storage = leaves
                        .iter()
                        .flat_map(|leaf| leaf.get("Media").and_then(Value::as_array).cloned().unwrap_or_default())
                        .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
                        .filter_map(|part| part.get("size").and_then(Value::as_i64))
                        .sum::<i64>();
                    if let Some(object) = projected.as_object_mut() {
                        object.insert("durationTotal".to_string(), json!(duration.to_string()));
                        object.insert("storageTotal".to_string(), json!(storage.to_string()));
                    }
                }
                if let Some(object) = projected.as_object_mut() {
                    if exclude_media {
                        let count = row
                            .get("Media")
                            .and_then(Value::as_array)
                            .map_or(0, Vec::len);
                        object.remove("Media");
                        object.insert("mediaCount".to_string(), json!(count));
                        object.insert("mediaCountOptimized".to_string(), json!(0));
                    }
                    for name in &excluded_elements {
                        if *name != "Media" {
                            object.remove(*name);
                        }
                    }
                    for name in &excluded_fields {
                        object.remove(*name);
                    }
                }
                projected
            })
            .collect::<Vec<_>>();
        let mut fields = Map::from_iter([
            ("Metadata".to_string(), Value::Array(rows)),
            // Section grids are synchronisable even though ordinary metadata
            // containers use the library serializer's false default.
            ("allowSync".to_string(), json!(true)),
        ]);
        let section_title = self
            .section(section_key)
            .and_then(|section| section.get("title"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        let section_uuid = self
            .section(section_key)
            .and_then(|section| section.get("uuid"))
            .cloned()
            .unwrap_or(Value::Null);
        fields.insert(
            "librarySectionID".to_string(),
            section_key
                .parse::<i64>()
                .map(|id| json!(id))
                .unwrap_or_else(|_| json!(section_key)),
        );
        fields.insert("librarySectionTitle".to_string(), json!(section_title));
        fields.insert("librarySectionUUID".to_string(), section_uuid);
        fields.insert("title1".to_string(), json!(section_title));
        // Cycle 18, measured on kids and zen: a MOVIE library COMPOSES this
        // from its own title ("All Zen Movies"), a SHOW library answers the
        // constant "All Shows" whatever it is called.
        if section_type == "show" {
            fields.insert("title2".to_string(), json!("All Shows"));
            fields.insert("viewGroup".to_string(), json!("show"));
        } else {
            fields.insert("title2".to_string(), json!(format!("All {section_title}")));
            fields.insert("viewGroup".to_string(), json!("movie"));
        }
        // Matched against a real PMS section grid: the listing keeps the
        // library's own type and mode whatever `type=` asked for.
        fields.insert("viewMode".to_string(), json!(65592));
        fields.insert(
            "art".to_string(),
            json!(if section_type == "show" { "/:/resources/show-fanart.jpg" } else { "/:/resources/movie-fanart.jpg" }),
        );
        fields.insert(
            "thumb".to_string(),
            json!(if section_type == "show" { "/:/resources/show.png" } else { "/:/resources/movie.png" }),
        );
        if section_type == "show" {
            fields.insert("nocache".to_string(), json!(true));
        }
        for name in &excluded_fields {
            fields.remove(*name);
        }
        if query.get("includeMeta").is_some_and(|value| value == "1") {
            let advanced = query.get("includeAdvanced").is_some_and(|value| value == "1");
            if let Some(mut meta) = self.section_meta(section_key, section_type, advanced) {
                let asked = query.get("type").cloned().unwrap_or_default();
                let active = if !asked.is_empty() && asked.bytes().all(|b| b.is_ascii_digit()) {
                    match asked.parse::<i64>().unwrap_or(0) {
                        1 => Some("movie"),
                        2 => Some("show"),
                        3 => Some("season"),
                        4 => Some("episode"),
                        _ => None,
                    }
                } else {
                    Some(section_type)
                };
                meta_active(&mut meta, active, query.get("sort").map(String::as_str));
                fields.insert("Meta".to_string(), meta);
            }
        }
        page_fields(&mut fields, query);
        Response::json(200, self.container(fields, true))
    }

    /// The filesystem view is a projection of the catalog's measured Part paths.
    /// Plex assigns directory ids from the sorted relative paths and uses the
    /// id as the only state carried by a subsequent parent request.
    fn route_section_folder(
        &self,
        section_key: &str,
        section: &Value,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if request.method != "GET" && request.method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        let kind = section
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let wanted_type = if kind == "show" { 4 } else { 1 };
        let root = format!("/plexsim/{section_key}/");
        let mut placed = Vec::<(Vec<String>, String, Value)>::new();
        for row in self.choose_rows(Some(section_key), Some(wanted_type)) {
            let Some(file) = row
                .get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .flat_map(|media| media.get("Part").and_then(Value::as_array))
                .flatten()
                .find_map(|part| part.get("file").and_then(Value::as_str))
            else {
                continue;
            };
            let relative = file
                .strip_prefix(&root)
                .map(str::to_string)
                .unwrap_or_else(|| file.rsplit('/').next().unwrap_or(file).to_string());
            let mut components = relative
                .split('/')
                .filter(|component| !component.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>();
            let name = components.pop().unwrap_or_default();
            placed.push((components, name, row));
        }
        let mut directories = HashSet::<Vec<String>>::new();
        for (folders, _, _) in &placed {
            for depth in 1..=folders.len() {
                directories.insert(folders[..depth].to_vec());
            }
        }
        let mut directory_list = directories.into_iter().collect::<Vec<_>>();
        directory_list.sort();
        let mut ids = HashMap::<Vec<String>, i64>::new();
        for (index, folders) in directory_list.iter().enumerate() {
            ids.insert(folders.clone(), index as i64 + 1);
        }
        let level = match query.get("parent") {
            None => Some(Vec::new()),
            Some(value) if value.is_empty() => None,
            Some(value) => value
                .parse::<i64>()
                .ok()
                .and_then(|wanted| ids.iter().find(|(_, id)| **id == wanted))
                .map(|(folders, _)| folders.clone()),
        };
        let mut rows = Vec::new();
        if let Some(level) = level {
            for folders in directory_list.iter().filter(|folders| {
                folders.len() == level.len() + 1 && folders[..level.len()] == level[..]
            }) {
                rows.push(json!({
                    "type": "directory",
                    "key": format!("/library/sections/{section_key}/folder?parent={}", ids[folders]),
                    "title": folders.last().cloned().unwrap_or_default(),
                }));
            }
            let mut files = placed
                .iter()
                .filter(|(folders, _, _)| folders == &level)
                .collect::<Vec<_>>();
            files.sort_by(|left, right| {
                left.1
                    .to_ascii_lowercase()
                    .cmp(&right.1.to_ascii_lowercase())
                    .then_with(|| {
                        key_of(&left.2)
                            .unwrap_or_default()
                            .cmp(&key_of(&right.2).unwrap_or_default())
                    })
            });
            for (_, _, row) in files {
                let mut projected = self.grid_row(row, false, false, false);
                if row.get("type").and_then(Value::as_str) == Some("episode") {
                    if let Some(object) = projected.as_object_mut() {
                        for field in ["Director", "Writer", "Role"] {
                            object.remove(field);
                        }
                    }
                }
                rows.push(projected);
            }
        }
        let total = rows.len();
        let start = query
            .get("X-Plex-Container-Start")
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0);
        let size = query
            .get("X-Plex-Container-Size")
            .and_then(|value| value.parse::<usize>().ok());
        let page = if let Some(size) = size {
            rows.into_iter().skip(start).take(size).collect::<Vec<_>>()
        } else {
            rows
        };
        let mut fields = Map::from_iter([
            ("Metadata".to_string(), Value::Array(page)),
            ("allowSync".to_string(), json!(true)),
            (
                "librarySectionID".to_string(),
                json!(section_key.parse::<i64>().unwrap_or(0)),
            ),
            (
                "librarySectionTitle".to_string(),
                section.get("title").cloned().unwrap_or(Value::Null),
            ),
            (
                "librarySectionUUID".to_string(),
                section.get("uuid").cloned().unwrap_or(Value::Null),
            ),
        ]);
        if size.is_some() {
            fields.insert("offset".to_string(), json!(start));
            fields.insert("totalSize".to_string(), json!(total));
        }
        if query.get("includeMeta").map(String::as_str) == Some("1") {
            // Cycle 46: the section's complete descriptor with only its folder Type
            // active.
            let kind = section.get("type").and_then(Value::as_str).unwrap_or("movie").to_string();
            if let Some(mut meta) = self.section_meta(section_key, &kind, false) {
                meta_active(&mut meta, Some("folder"), None);
                fields.insert("Meta".to_string(), meta);
            }
        }
        Response::json(200, self.container(fields, true))
    }

    /// In-library search is deliberately a substring matcher. Global search
    /// uses the separate word-prefix/fuzzy matcher above; sharing it here makes
    /// a mid-word fragment disappear from the section search box.
    fn route_section_search(
        &self,
        section_key: &str,
        section: &Value,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
        request: &Request,
    ) -> Response {
        if request.method != "GET" && request.method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        // Measured: only the form with NO `type` is a 400, and (cycle 20) the
        // type has to PARSE -- `abc`, `1.5` and a single space are 400s.
        let raw_type = query.get("type").map(|value| value.trim().to_string()).unwrap_or_default();
        let digits = raw_type.strip_prefix('-').unwrap_or(&raw_type);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Response::error(400, "plexsim: bad request");
        }
        let type_number = raw_type.parse::<i64>().unwrap_or(0);
        let typed = match type_number {
            1 => Some("movie"),
            2 => Some("show"),
            3 => Some("season"),
            4 => Some("episode"),
            _ => None,
        };
        let mut rows = if typed.is_some() {
            self.choose_rows(Some(section_key), Some(type_number))
        } else {
            Vec::new()
        };
        let text = query.get("query").map(|value| value.trim().to_lowercase()).unwrap_or_default();
        if !text.is_empty() {
            // Measured: a SUBSTRING match on the TITLE alone (cycle 31: not the
            // original title), and (cycle 18) a GENERATED title is not in the
            // search index -- the stored title of a synthesised season is empty.
            rows.retain(|row| {
                row.as_object()
                    .map(crate::hierarchy::stored_title)
                    .unwrap_or_default()
                    .to_lowercase()
                    .contains(&text)
            });
        }
        let section_type = section
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let filter_pairs = pairs
            .iter()
            .filter(|(name, _)| {
                !matches!(
                    name.as_str(),
                    "type" | "query" | "sort" | "sourceType" | "includeGuids"
                        | "X-Plex-Container-Start" | "X-Plex-Container-Size" | "X-Plex-Language"
                )
            })
            .cloned()
            .collect::<Vec<_>>();
        rows = match self.filter_rows(rows, &filter_pairs, section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        if query.get("sourceType").is_some_and(|value| value == "0") {
            // Cycle 55: `sourceType=0` orders the search box by id.
            rows.sort_by_key(|row| key_of(row).and_then(|key| key.parse::<i64>().ok()).unwrap_or(0));
        } else if let Some(sort) = query.get("sort") {
            rows = match self.sort_rows(rows, Some(sort)) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
        }
        let include_guids = query.get("includeGuids").is_some_and(|value| value == "1");
        let rows = rows
            .iter()
            .map(|row| {
                let mut shown = self.grid_row(row, false, include_guids, false);
                if include_guids {
                    if let Some(object) = shown.as_object_mut() {
                        if !object.contains_key("Guid") {
                            if let Some(guid) = row.get("guid").filter(|guid| !string_value(guid).is_empty()) {
                                object.insert("Guid".to_string(), json!([{"id": guid}]));
                            }
                        }
                    }
                }
                shown
            })
            .collect::<Vec<_>>();
        // Cycle 18, measured on kids and zen over two movie libraries and three
        // show ones: this container is typed by the ASKED `type=`, not by the
        // library -- `type=3` is `viewGroup="season"` with `viewMode` 65593,
        // `type=4` `viewGroup="episode"` with `mixedParents`.  It is the
        // secondary container without `content`.
        let show = section_type == "show";
        let mut fields = Map::new();
        fields.insert("allowSync".to_string(), json!(false));
        fields.insert(
            "art".to_string(),
            json!(if show { "/:/resources/show-fanart.jpg" } else { "/:/resources/movie-fanart.jpg" }),
        );
        fields.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
        fields.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        fields.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        fields.insert(
            "thumb".to_string(),
            json!(if show { "/:/resources/show.png" } else { "/:/resources/movie.png" }),
        );
        fields.insert("title1".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
        fields.insert("viewGroup".to_string(), json!(typed.unwrap_or(section_type)));
        fields.insert("viewMode".to_string(), json!(if typed == Some("season") { 65593 } else { 65592 }));
        if show {
            fields.insert("nocache".to_string(), json!(true));
        }
        fields.insert("title2".to_string(), json!(format!("Search for '{text}'")));
        if typed == Some("episode") {
            fields.insert("mixedParents".to_string(), json!(true));
        }
        fields.insert("size".to_string(), json!(rows.len()));
        if !rows.is_empty() {
            fields.insert("Metadata".to_string(), Value::Array(rows));
        }
        page_fields(&mut fields, query);
        let shown = fields.get("Metadata").and_then(Value::as_array).map(Vec::len).unwrap_or(0);
        fields.insert("size".to_string(), json!(shown));
        Response::json(200, json!({"MediaContainer": fields}))
    }

    fn route_section_common(
        &self,
        section_key: &str,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if request.method != "GET" && request.method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        let Some(type_number) = query
            .get("type")
            .and_then(|value| value.parse::<i64>().ok())
        else {
            return Response::error(400, "plexsim: bad request");
        };
        // server.py's `common_fields`: the fields the selected rows SHARE, the
        // differing ones named in `mixedFields` (a FIXED order, "" when none).
        let wanted = match type_number {
            1 => "movie",
            2 => "show",
            3 => "season",
            4 => "episode",
            _ => "",
        };
        let section = self.section(section_key).cloned().unwrap_or(Value::Null);
        let section_type = section.get("type").and_then(Value::as_str).unwrap_or("movie").to_string();
        let filter_pairs = query
            .iter()
            .filter(|(name, _)| {
                !name.starts_with("X-Plex-") && !matches!(name.as_str(), "type" | "sort" | "includeGuids")
            })
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect::<Vec<_>>();
        let mut rows = if let Some(ids) = query.get("id") {
            // An id of the wrong TYPE is simply not part of the set.
            ids.split(',')
                .filter(|key| !key.trim().is_empty() && key.trim().bytes().all(|b| b.is_ascii_digit()))
                .filter_map(|key| self.catalog.get(key.trim()).cloned())
                .filter(|row| row.get("type").and_then(Value::as_str) == Some(wanted))
                .collect::<Vec<_>>()
        } else {
            self.choose_rows(Some(section_key), Some(type_number))
        };
        rows = match self.filter_rows(rows, &filter_pairs, &section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        rows.retain(|row| row.get("type").and_then(Value::as_str) == Some(wanted));
        // An episode carries no Genre, studio, tagline or Country of its own (the
        // show's are the catalog's copy; the detail row drops them too).
        for row in rows.iter_mut() {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                if let Some(object) = row.as_object_mut() {
                    for field in ["Genre", "studio", "tagline", "Country"] {
                        object.remove(field);
                    }
                }
            }
        }
        let value = |row: &Value, field: &str| -> Value {
            match field {
                "titleSort" => row
                    .get("titleSort")
                    .filter(|value| !string_value(value).is_empty())
                    .or_else(|| row.get("title"))
                    .cloned()
                    .unwrap_or(Value::Null),
                "index" if row.get("type").and_then(Value::as_str) == Some("movie") => {
                    row.get("index").cloned().filter(|v| !v.is_null()).unwrap_or(json!(1))
                }
                _ => row.get(field).cloned().unwrap_or(Value::Null),
            }
        };
        let mut fields_list = vec![
            "title", "ratingKey", "parentRatingKey", "titleSort", "tagline", "index", "summary",
            "year", "studio", "originallyAvailableAt", "contentRating", "userRating",
        ];
        if wanted == "season" {
            fields_list.retain(|field| *field != "title");
        }
        let mut common = Map::new();
        common.insert("mixedFields".to_string(), json!(""));
        let mut mixed = Vec::new();
        for field in &fields_list {
            let distinct = rows
                .iter()
                .map(|row| serde_json::to_string(&value(row, field)).unwrap_or_default())
                .collect::<HashSet<_>>();
            if distinct.len() > 1 {
                mixed.push(*field);
            } else if let Some(first) = rows.first() {
                let v = value(first, field);
                if !v.is_null() {
                    common.insert(field.to_string(), v);
                }
            }
        }
        if rows.len() == 1 {
            common.insert(
                "key".to_string(),
                json!(format!("/library/metadata/{}/children", key_of(&rows[0]).unwrap_or_default())),
            );
        }
        common.insert("type".to_string(), json!("common"));
        common.insert("guid".to_string(), json!(""));
        if mixed.contains(&"titleSort") || rows.is_empty() {
            common.insert("titleSort".to_string(), json!(""));
        }
        if wanted == "episode" && !rows.is_empty() {
            let shows = rows
                .iter()
                .map(|row| row.get("grandparentRatingKey").map(string_value).unwrap_or_default())
                .collect::<HashSet<_>>();
            if shows.len() == 1 {
                let show = rows[0].get("grandparentRatingKey").map(string_value).unwrap_or_default();
                common.insert("grandparentRatingKey".to_string(), json!(show.clone()));
                let title = self
                    .catalog
                    .get(&show)
                    .and_then(|row| row.get("title"))
                    .cloned()
                    .or_else(|| rows[0].get("grandparentTitle").cloned())
                    .unwrap_or(Value::Null);
                common.insert("grandparentTitle".to_string(), title);
            }
            common.insert("parentTitle".to_string(), json!(""));
        }
        common.insert("mixedFields".to_string(), json!(mixed.join(",")));
        if let Some(rating) = common.get("userRating").and_then(Value::as_f64) {
            // Cycle 25, measured on plexscratch: a Common's `userRating` is a STRING
            // (`"8"`, not `8.0`); the item row keeps the float.
            let text = if rating.fract() == 0.0 { format!("{}", rating as i64) } else { rating.to_string() };
            common.insert("userRating".to_string(), json!(text));
        }
        if rows.len() == 1 {
            for key in ["Guid", "Rating", "Field"] {
                if let Some(v) = rows[0].get(key).filter(|v| v.as_array().is_some_and(|a| !a.is_empty())) {
                    common.insert(key.to_string(), v.clone());
                }
            }
        } else if !rows.is_empty() {
            let mut shared: Option<HashSet<String>> = None;
            for row in &rows {
                let images = row
                    .get("Rating")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|r| r.get("image").map(string_value).unwrap_or_default())
                    .collect::<HashSet<_>>();
                shared = Some(match shared {
                    None => images,
                    Some(prev) => prev.intersection(&images).cloned().collect(),
                });
            }
            let shared = shared.unwrap_or_default();
            let mut ratings = Vec::new();
            for first in rows[0].get("Rating").and_then(Value::as_array).into_iter().flatten() {
                let image = first.get("image").map(string_value).unwrap_or_default();
                if !shared.contains(&image) {
                    continue;
                }
                let entries = rows
                    .iter()
                    .filter_map(|row| {
                        row.get("Rating")
                            .and_then(Value::as_array)
                            .and_then(|list| list.iter().find(|r| r.get("image").map(string_value).as_deref() == Some(image.as_str())))
                            .cloned()
                    })
                    .collect::<Vec<_>>();
                ratings.push(if entries.iter().all(|e| e == first) { first.clone() } else { json!({}) });
            }
            if !ratings.is_empty() {
                common.insert("Rating".to_string(), Value::Array(ratings));
            }
        }
        for key in ["Genre", "Country", "Director", "Writer", "Producer", "Role", "Collection", "Label"] {
            let name_of = |tag: &Value| {
                tag.get("tag").or_else(|| tag.get("id")).map(string_value).unwrap_or_default()
            };
            let mut shared: Option<HashSet<String>> = None;
            for row in &rows {
                let names = row
                    .get(key)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(name_of)
                    .collect::<HashSet<_>>();
                shared = Some(match shared {
                    None => names,
                    Some(prev) => prev.intersection(&names).cloned().collect(),
                });
            }
            if let Some(shared) = shared.filter(|set| !set.is_empty()) {
                let tags = rows[0]
                    .get(key)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter(|tag| shared.contains(&name_of(tag)))
                    .cloned()
                    .collect::<Vec<_>>();
                common.insert(key.to_string(), Value::Array(tags));
            }
        }
        // The row is written as a `Common` element (the handler's `_xml` override).
        common.insert("_element".to_string(), json!("Common"));
        let mut fields = Map::new();
        fields.insert("Metadata".to_string(), json!([Value::Object(common)]));
        let body = self.secondary_body(&section, Some("Common"), fields);
        Response::json(200, json!({"MediaContainer": body}))
    }

    fn route_section_collection_axis(&self, section_key: &str) -> Response {
        let mut names = HashSet::new();
        for row in self.section_rows(section_key) {
            for tag in row
                .get("Collection")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(name) = tag.get("tag").and_then(Value::as_str) {
                    names.insert(name.to_string());
                }
            }
        }
        let mut names = names.into_iter().collect::<Vec<_>>();
        names.sort();
        let rows = names
            .into_iter()
            .map(|name| json!({"title":name,"key":format!("/library/sections/{section_key}/all?collection={name}"),"tagType":1,"filter":format!("collection={name}")}))
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Directory".to_string(), Value::Array(rows))]),
                false,
            ),
        )
    }

    fn route_section_top(
        &self,
        section_key: &str,
        leaf: &str,
        _query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if request.method != "GET" && request.method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        if matches!(
            leaf,
            "label"
                | "collection"
                | "mood"
                | "firstCharacter"
                | "year"
                | "contentRating"
                | "banner"
                | "guid"
                | "file"
                | "viewCount"
                | "resolution"
        ) {
            let section = self.section(section_key);
            let kind = section
                .and_then(|value| value.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("movie");
            let mut fields = Map::from_iter([
                (
                    "librarySectionID".to_string(),
                    json!(section_key.parse::<i64>().unwrap_or(0)),
                ),
                (
                    "librarySectionTitle".to_string(),
                    section
                        .and_then(|value| value.get("title"))
                        .cloned()
                        .unwrap_or(Value::Null),
                ),
                (
                    "librarySectionUUID".to_string(),
                    section
                        .and_then(|value| value.get("uuid"))
                        .cloned()
                        .unwrap_or(Value::Null),
                ),
                (
                    "title1".to_string(),
                    section
                        .and_then(|value| value.get("title"))
                        .cloned()
                        .unwrap_or(Value::Null),
                ),
                ("viewGroup".to_string(), json!(kind)),
                ("viewMode".to_string(), json!(65592)),
                ("allowSync".to_string(), json!(true)),
                (
                    "art".to_string(),
                    json!(if kind == "show" {
                        "/:/resources/show-fanart.jpg"
                    } else {
                        "/:/resources/movie-fanart.jpg"
                    }),
                ),
                (
                    "thumb".to_string(),
                    json!(if kind == "show" {
                        "/:/resources/show.png"
                    } else {
                        "/:/resources/movie.png"
                    }),
                ),
            ]);
            if kind == "show" {
                fields.insert("nocache".to_string(), json!(true));
            }
            fields.insert("size".to_string(), json!(0));
            return Response::json(200, self.container(fields, false));
        }
        if matches!(
            leaf,
            "platform"
                | "poster"
                | "thumb"
                | "art"
                | "theme"
                | "all"
                | "sorts"
                | "filters"
                | "search"
                | "onDeck"
                | "folder"
                | "role"
                | "album"
                | "videoCodec"
                | "hdr10plus"
                | "clearLogo"
                | "zzz"
                | "nearest"
        ) {
            return Response::error(404, "plexsim: not found");
        }
        let tag_type = match leaf {
            "genre" | "title" => Some(1),
            "director" | "studio" => Some(4),
            "writer" | "rating" => Some(5),
            "actor" | "tagline" => Some(6),
            "country" => Some(8),
            "producer" | "summary" => Some(7),
            _ => return Response::error(404, "plexsim: not found"),
        };
        let field = match tag_type.unwrap_or_default() {
            1 => "Genre",
            4 => "Director",
            5 => "Writer",
            6 => "Role",
            7 => "Producer",
            8 => "Country",
            _ => "",
        };
        let section = self.section(section_key);
        let kind = section
            .and_then(|value| value.get("type"))
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let source_rows = if kind == "show" {
            self.catalog
                .items
                .iter()
                .filter(|row| {
                    row.get("type").and_then(Value::as_str) == Some("episode")
                        && string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                            == section_key
                })
                .cloned()
                .collect::<Vec<_>>()
        } else {
            self.section_rows(section_key)
        };
        let mut found = HashMap::<String, Value>::new();
        for row in source_rows {
            for tag in row
                .get(field)
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if tag.get("tag").and_then(Value::as_str).is_none() {
                    continue;
                }
                let Some(id) = tag.get("id") else {
                    continue;
                };
                let id = string_value(id);
                found.entry(id).or_insert_with(|| tag.clone());
            }
        }
        let own_filter = match tag_type.unwrap_or_default() {
            1 => "genre",
            4 => "director",
            5 => "writer",
            6 => "actor",
            7 => "producer",
            8 => "country",
            _ => leaf,
        };
        let mut tags = found.into_iter().collect::<Vec<_>>();
        tags.sort_by(|left, right| {
            let l = left.0.parse::<i64>().unwrap_or(0);
            let r = right.0.parse::<i64>().unwrap_or(0);
            l.cmp(&r).then_with(|| left.0.cmp(&right.0))
        });
        let rows = tags
            .into_iter()
            .map(|(id, tag)| {
                let mut row = json!({
                    "key": format!("/library/sections/{section_key}/all?{leaf}={id}"),
                    "id": id,
                    "filter": format!("{own_filter}={id}"),
                    "tag": tag.get("tag").cloned().unwrap_or(Value::Null),
                    "tagType": tag_type,
                });
                if let Some(object) = row.as_object_mut() {
                    for name in ["tagKey", "thumb"] {
                        if let Some(value) = tag.get(name) {
                            object.insert(name.to_string(), value.clone());
                        }
                    }
                    if let Some(value) = tag.get("tag") {
                        object.insert("title".to_string(), value.clone());
                    }
                }
                row
            })
            .collect::<Vec<_>>();
        let mut fields = Map::from_iter([
            (
                "librarySectionID".to_string(),
                json!(section_key.parse::<i64>().unwrap_or(0)),
            ),
            (
                "librarySectionTitle".to_string(),
                section
                    .and_then(|value| value.get("title"))
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            (
                "librarySectionUUID".to_string(),
                section
                    .and_then(|value| value.get("uuid"))
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            (
                "title1".to_string(),
                section
                    .and_then(|value| value.get("title"))
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("viewGroup".to_string(), json!(kind)),
            ("viewMode".to_string(), json!(65592)),
            ("allowSync".to_string(), json!(true)),
            ("Directory".to_string(), Value::Array(rows)),
        ]);
        if kind == "show" {
            fields.insert("nocache".to_string(), json!(true));
        }
        Response::json(200, self.container(fields, false))
    }

    fn route_metadata(
        &mut self,
        key: &str,
        row: &Value,
        tail: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if tail.is_empty() {
            if request.method == "PUT" {
                let pairs = Self::request_pairs(request);
                return self.apply_edit(&[key.to_string()], &pairs, false, "loading");
            }
            if request.method == "DELETE" {
                if query.get("proxy").map(String::as_str) == Some("1") {
                    // Delete Version: plexsim has no optimized versions, and a real
                    // PMS answers 400 when there are none to delete.
                    return Response::error(400, "plexsim: no optimized versions");
                }
                let Some(row) = self.catalog.get(key).cloned() else {
                    return Response::error(404, "plexsim: not found");
                };
                // `library.delete`: the row AND its descendants go -- a season takes
                // its episodes -- and removing a season's LAST episode removes the
                // season too, measured through the reference's scanner.
                let mut doomed = vec![key.to_string()];
                let mut index = 0;
                while index < doomed.len() {
                    let parent = doomed[index].clone();
                    for item in &self.catalog.items {
                        let child = item.get("parentRatingKey").map(string_value);
                        if child.as_deref() == Some(parent.as_str()) {
                            if let Some(child_key) = key_of(item) {
                                if !doomed.contains(&child_key) {
                                    doomed.push(child_key);
                                }
                            }
                        }
                    }
                    index += 1;
                }
                if row.get("type").and_then(Value::as_str) == Some("episode") {
                    let season = row.get("parentRatingKey").map(string_value).unwrap_or_default();
                    let siblings = self
                        .catalog
                        .items
                        .iter()
                        .filter(|item| item.get("parentRatingKey").map(string_value).as_deref() == Some(season.as_str()))
                        .count();
                    if siblings <= 1 && self.catalog.get(&season).is_some() {
                        doomed.push(season);
                    }
                }
                for gone in &doomed {
                    if self.catalog.remove(gone).is_some() {
                        self.deleted.insert(gone.clone());
                    }
                }
                self.push_delete_event(key);
                self.dirty = true;
                return Response::empty(200);
            }
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            // Apply private-source transformations before projecting the row.
            // `detail_row` deliberately strips `_debrid` and the artwork source
            // URLs; doing these steps after projection makes `checkFiles` always
            // report false and `includeAlternates` silently omit the measured
            // alternate blocks even when the source data is present.
            // Cycles 19, 22, 29, 46: seven detail options carry the 0/1 grammar
            // (`+?0*[01]`, anything else a 400), and the file pair is reported only
            // when one of the three check options is ON.
            for option in [
                "checkFiles",
                "skipRefresh",
                "asyncCheckFiles",
                "asyncAugmentMetadata",
                "asyncRefreshLocalMediaAgent",
                "asyncRefreshAnalysis",
                "checkFileAvailability",
            ] {
                if query.get(option).is_some_and(|value| !bool_option(value)) {
                    return Response::error(400, "plexsim: bad request");
                }
            }
            let checks_requested = ["checkFiles", "asyncCheckFiles", "checkFileAvailability"]
                .iter()
                .any(|option| query.get(*option).is_some_and(|value| is_true_option(value)));
            let prepared = if checks_requested {
                self.annotate_file_checks(row)
            } else {
                row.clone()
            };
            let prepared = if query
                .get("includeAlternates")
                .is_some_and(|value| is_true_option(value))
            {
                self.with_alternates(prepared)
            } else {
                prepared
            };
            let mut detail = self.detail_row(&prepared);
            self.apply_detail_blocks(row, &mut detail, query);
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
            if let Some(excluded) = query.get("excludeFields") {
                let names = excluded.split(',').collect::<HashSet<_>>();
                strip_excluded_fields(&mut detail, &names);
            }
            if let Some(object) = detail.as_object_mut() {
                if query
                    .get("excludeElements")
                    .is_some_and(|value| value.split(',').any(|name| name == "Media"))
                {
                    let media_count = object
                        .get("Media")
                        .and_then(Value::as_array)
                        .map_or(0, Vec::len);
                    object.remove("Media");
                    object.insert("mediaCount".to_string(), json!(media_count));
                    object.insert("mediaCountOptimized".to_string(), json!(0));
                }
                if kind == "show"
                    && query
                        .get("includeChildren")
                        .is_some_and(|value| is_true_option(value))
                {
                    let children = self
                        .children(key)
                        .into_iter()
                        .map(|child| self.listing_row(&child))
                        .collect::<Vec<_>>();
                    object.insert(
                        "Children".to_string(),
                        json!({"size":children.len(),"Metadata":children}),
                    );
                }
                if query
                    .get("includeRelated")
                    .is_some_and(|value| is_true_option(value))
                {
                    // Measured: the item's related hubs as `{"Hub": [...]}`, and an
                    // empty OBJECT for an item with none.  Cycle 15: the INLINE block
                    // caps at FIVE rows per hub and says `more`; `includeRelatedCount=N`
                    // sets that cap, and cycle 22: `=0` EMPTIES the hub.
                    object.insert("Related".to_string(), self.related_block(row, query));
                }
            }
            // Re-measured against fun and the 1.43.4 reference: the detail container
            // says `allowSync` (not for a clip) and names the row's library by id,
            // title and UUID beside the identifier and the media-tag pair.
            let mut detail_fields = Map::new();
            detail_fields.insert("size".to_string(), json!(1));
            detail_fields.insert("allowSync".to_string(), json!(kind != "clip"));
            if kind != "clip" {
                let section_id = row.get("librarySectionID").cloned().unwrap_or(Value::Null);
                let section = self.section(&string_value(&section_id)).cloned().unwrap_or_default();
                detail_fields.insert("librarySectionID".to_string(), section_id);
                detail_fields.insert(
                    "librarySectionTitle".to_string(),
                    section.get("title").cloned().unwrap_or(Value::Null),
                );
                detail_fields.insert(
                    "librarySectionUUID".to_string(),
                    section.get("uuid").cloned().unwrap_or(Value::Null),
                );
            }
            detail_fields.insert("Metadata".to_string(), Value::Array(vec![detail]));
            let mut container = self.container(detail_fields, true);
            if query
                .get("asyncCheckFiles")
                .is_some_and(|value| is_true_option(value))
                || query
                    .get("asyncRefreshLocalMediaAgent")
                    .is_some_and(|value| is_true_option(value))
            {
                if let Some(fields) = container
                    .get_mut("MediaContainer")
                    .and_then(Value::as_object_mut)
                {
                    for name in ["identifier", "mediaTagPrefix", "mediaTagVersion"] {
                        fields.remove(name);
                    }
                }
            }
            return Response::json(200, container);
        }
        // Cycle 45: `theme` is the one artwork leaf with a `/metadata` sub-leaf, and it
        // MINTS a track row.
        if tail.len() == 3 && tail[0] == "theme" && tail[2] == "metadata" {
            if !matches!(request.method.as_str(), "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            return self.theme_metadata(key, tail[1]);
        }
        // Cycle 45: an item leaf with an EXTRA path segment is a 404, except the
        // artwork leaves (whose second segment is the version) and the two real
        // two-segment leaves, `/extras/all` and `/users/top`.
        if tail.len() > 1
            && !matches!(tail[0], "thumb" | "art" | "poster" | "banner" | "theme" | "clearLogo")
            && !(tail.len() == 2 && matches!((tail[0], tail[1]), ("extras", "all") | ("users", "top")))
        {
            return Response::error(404, "plexsim: not found");
        }
        if matches!(tail[0], "thumb" | "art" | "poster" | "banner" | "theme" | "clearLogo")
            && tail.len() > 2
        {
            return Response::error(404, "plexsim: not found");
        }
        // Cycle 45: the SINGULAR artwork leaf is routed for six names and answers its
        // own 400 to a PUT with no `url`.
        if request.method == "PUT"
            && tail.len() == 1
            && matches!(tail[0], "poster" | "art" | "thumb" | "banner" | "theme" | "clearLogo")
            && query.get("url").map_or(true, |url| url.is_empty())
        {
            return Response::error(400, "plexsim: bad request");
        }
        if request.method == "POST" && tail.len() == 1 {
            match tail[0] {
                // Cycle 15, measured on plexscratch: POSTing an EMPTY body to any
                // artwork or subtitle candidate list is a 406 -- all seven alike, and
                // for the subtitle upload the empty-body check runs before the title's.
                "posters" | "arts" | "thumbs" | "banners" | "themes" | "clearLogos"
                | "subtitles"
                    if request.body.is_empty() =>
                {
                    return Response::error(406, "plexsim: not acceptable");
                }
                // Cycle 16: `/extras` is the one candidate list that refuses a POST
                // with a 400 whatever the body.
                "extras" => return Response::error(400, "plexsim: bad request"),
                _ => {}
            }
        }
        match tail[0] {
            "prefs" => {
                let (_, raw_query) = split_target(&request.target);
                let pairs = parse_query(&raw_query);
                self.write_item_prefs(key, row, &pairs, request)
            }
            "banners" | "thumbs" if tail.len() == 1 => {
                // Cycle 14: two more candidate lists, 200 on kids AND zen: `banners`
                // is empty on both and `thumbs` lists the media bundle's video
                // thumbnails, which plexsim has none of. Only GET and HEAD read them.
                if request.method != "GET" && request.method != "HEAD" {
                    return Response::error(404, "plexsim: not found");
                }
                Response::json(200, self.tag_container(Map::new()))
            }
            "grandchildren" => {
                if request.method != "GET" && request.method != "HEAD" {
                    return Response::error(404, "plexsim: not found");
                }
                let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
                let mut rows = match kind {
                    "show" => self.leaves(key),
                    "season" => self.children(key),
                    "episode" => vec![row.clone()],
                    _ => Vec::new(),
                };
                // Cycle 54: `/grandchildren` runs the listing QUERY, as `/children` does --
                // filters constrain it and an unparseable value is a 500.
                let section_type = self
                    .section(&string_value(row.get("librarySectionID").unwrap_or(&Value::Null)))
                    .and_then(|section| section.get("type").and_then(Value::as_str))
                    .unwrap_or("show")
                    .to_string();
                let filter_pairs = query
                    .iter()
                    .filter(|(name, _)| {
                        !name.starts_with("X-Plex-")
                            && !name.starts_with("include")
                            && !name.starts_with("exclude")
                    })
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect::<Vec<_>>();
                if !filter_pairs.is_empty() {
                    rows = match self.filter_rows(rows, &filter_pairs, &section_type) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                }
                if let Some(type_value) = query.get("type") {
                    let Ok(type_number) = type_value.parse::<i64>() else {
                        return Response::error(500, "plexsim: bad request");
                    };
                    rows.retain(|candidate| {
                        let candidate_type = match candidate.get("type").and_then(Value::as_str) {
                            Some("movie") => Some(1),
                            Some("show") => Some(2),
                            Some("season") => Some(3),
                            Some("episode") => Some(4),
                            Some("clip") => Some(8),
                            _ => None,
                        };
                        candidate_type == Some(type_number)
                    });
                }
                if let Some(sort) = query.get("sort") {
                    rows = match self.sort_rows(rows, Some(sort)) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                }
                let section = self
                    .section(&string_value(
                        row.get("librarySectionID").unwrap_or(&Value::Null),
                    ))
                    .cloned()
                    .unwrap_or_default();
                let shown = rows
                    .iter()
                    .map(|candidate| self.grid_row(candidate, false, true, false))
                    .collect::<Vec<_>>();
                let mut fields = Map::from_iter([
                    (
                        "art".to_string(),
                        json!(
                            if section.get("type").and_then(Value::as_str) == Some("show") {
                                "/:/resources/show-fanart.jpg"
                            } else {
                                "/:/resources/movie-fanart.jpg"
                            }
                        ),
                    ),
                    (
                        "title1".to_string(),
                        section.get("title").cloned().unwrap_or(Value::Null),
                    ),
                    (
                        "viewGroup".to_string(),
                        json!(
                            if section.get("type").and_then(Value::as_str) == Some("show") {
                                "show"
                            } else {
                                "movie"
                            }
                        ),
                    ),
                    ("viewMode".to_string(), json!(65592)),
                    ("Metadata".to_string(), Value::Array(shown)),
                ]);
                if section.get("type").and_then(Value::as_str) == Some("show") {
                    fields.insert("nocache".to_string(), json!(true));
                }
                page_fields(&mut fields, query);
                Response::json(200, self.container(fields, false))
            }
            "users" if tail.get(1) == Some(&"top") => {
                // "Top users" of an item: one Account row per account that has
                // completed it.  Measured: rows only for an item with views, and a
                // SEASON is a 500 on the reference.
                if row.get("type").and_then(Value::as_str) == Some("season") {
                    return Response::error(500, "plexsim: top users unavailable");
                }
                // Cycle 54: the viewedAt keys filter the history counted here too (a
                // bad value is a 500); `accountID` does not.
                let tests = match Self::history_date_tests(query) {
                    Ok(tests) => tests,
                    Err(status) => return Response::error(status, "plexsim: internal error"),
                };
                let views = self
                    .history
                    .iter()
                    .filter(|entry| {
                        string_value(entry.get("ratingKey").unwrap_or(&Value::Null)) == key
                            && Self::history_passes(
                                &tests,
                                entry.get("viewedAt").and_then(Value::as_i64).unwrap_or(0),
                            )
                    })
                    .count();
                let accounts = if views > 0 {
                    json!([{"globalViewCount": views, "id": 1}])
                } else {
                    json!([])
                };
                // The oracle's `tag_container`: identifier and the media-tag pair, no
                // `allowSync`.
                let mut fields = Map::from_iter([
                    ("size".to_string(), json!(views.min(1))),
                    ("identifier".to_string(), json!("com.plexapp.plugins.library")),
                    ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
                    ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION)),
                ]);
                if views > 0 {
                    fields.insert("Account".to_string(), accounts);
                }
                Response::json(200, self.container(fields, false))
            }
            "children" => {
                if row.get("type").and_then(Value::as_str) == Some("movie") {
                    return Response::error(400, "plexsim: bad request");
                }
                let section_key = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
                let section_type = self
                    .section(&section_key)
                    .and_then(|section| section.get("type").and_then(Value::as_str))
                    .unwrap_or("movie");
                let mut children = self.children(key);
                if self.season_children_newest_first(row) {
                    // Cycle 14: the show's effective episodeSort=1, newest first.
                    children.reverse();
                }
                let filter_pairs = query
                    .iter()
                    .filter(|(name, _)| {
                        !matches!(
                            name.as_str(),
                            "X-Plex-Container-Start"
                                | "X-Plex-Container-Size"
                                | "excludeAllLeaves"
                                | "excludeElements"
                                | "excludeFields"
                                | "includeChildren"
                                | "includeRelated"
                                | "includePreferences"
                                | "includeExtras"
                                | "includeAdvanced"
                        )
                    })
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect::<Vec<_>>();
                if !filter_pairs.is_empty() {
                    children = match self.filter_rows(children, &filter_pairs, section_type) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                }
                if let Some(sort) = query.get("sort") {
                    children = match self.sort_rows(children, Some(sort)) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                }
                let group = row.get("type").and_then(Value::as_str) == Some("show");
                // Measured: a LISTING carries the `Guid` list only under
                // `includeGuids=1`, and then even a row with none of its own gets a
                // one-entry list naming its primary guid.
                let include_guids = query.get("includeGuids").is_some_and(|value| value == "1");
                let mut rows = children
                    .iter()
                    .map(|child| {
                        let mut shown = self.grid_row(child, false, include_guids, false);
                        if include_guids {
                            if let Some(object) = shown.as_object_mut() {
                                if object.get("Guid").is_none() {
                                    if let Some(guid) = object.get("guid").cloned() {
                                        object.insert("Guid".to_string(), json!([{"id": guid}]));
                                    }
                                }
                            }
                        }
                        shown
                    })
                    .collect::<Vec<_>>();
                let mut fields = self.child_container_fields(
                    row,
                    "children",
                    children.first().and_then(|child| child.get("type")).and_then(Value::as_str),
                );
                fields.insert("Metadata".to_string(), Value::Array(rows.clone()));
                if group {
                    let leaves = self.leaves(key);
                    // Re-measured on zen against a 24-season show: the "All
                    // episodes" row is present exactly when the PAGE holds more than
                    // one season row -- Start=10 Size=10 carries it, Start=0 Size=1
                    // does not -- so the gate is the page's own length.  Cycle 54: and
                    // it is the ANSWER's row count, paged or not.
                    let page_len = match (
                        query
                            .get("X-Plex-Container-Start")
                            .and_then(|value| value.parse::<i64>().ok()),
                        query
                            .get("X-Plex-Container-Size")
                            .and_then(|value| value.parse::<i64>().ok()),
                    ) {
                        (Some(start), Some(size)) if start >= 0 && size >= 0 => {
                            (children.len() as i64 - start).clamp(0, size) as usize
                        }
                        _ => children.len(),
                    };
                    let show_all = query
                        .get("excludeAllLeaves")
                        .is_none_or(|value| value != "1")
                        && page_len > 1;
                    if show_all {
                        let ios = request
                            .headers
                            .get("x-plex-platform")
                            .or_else(|| query.get("X-Plex-Platform"))
                            .is_some_and(|value| value == "iOS");
                        let mut all = Map::new();
                        all.insert("leafCount".to_string(), json!(leaves.len()));
                        all.insert(
                            "thumb".to_string(),
                            row.get("thumb").cloned().unwrap_or(Value::Null),
                        );
                        if ios {
                            // Measured on both servers, in XML and JSON: the iOS
                            // PLATFORM -- not the product, not tvOS -- types this row
                            // as a season, and no other client gets a type at all.
                            all.insert("type".to_string(), json!("season"));
                        }
                        all.insert(
                            "viewedLeafCount".to_string(),
                            json!(leaves
                                .iter()
                                .filter(|leaf| leaf
                                    .get("viewCount")
                                    .is_some_and(|count| !count.is_null()
                                        && string_value(count) != "0"))
                                .count()),
                        );
                        all.insert(
                            "key".to_string(),
                            json!(format!("/library/metadata/{key}/allLeaves")),
                        );
                        all.insert("title".to_string(), json!("All episodes"));
                        fields.insert("Directory".to_string(), json!([Value::Object(all)]));
                    }
                }
                if let Some(excluded) = query.get("excludeElements") {
                    let names = excluded.split(',').collect::<HashSet<_>>();
                    if names.contains("Media") {
                        for shown in &mut rows {
                            if let Some(object) = shown.as_object_mut() {
                                let count = object
                                    .get("Media")
                                    .and_then(Value::as_array)
                                    .map_or(0, Vec::len);
                                object.remove("Media");
                                object.insert("mediaCount".to_string(), json!(count));
                                object.insert("mediaCountOptimized".to_string(), json!(0));
                            }
                        }
                    }
                }
                if let Some(excluded) = query.get("excludeFields") {
                    let names = excluded.split(',').collect::<HashSet<_>>();
                    for shown in &mut rows {
                        strip_excluded_fields(shown, &names);
                    }
                    for name in &names {
                        fields.remove(*name);
                    }
                }
                fields.insert("Metadata".to_string(), Value::Array(rows.clone()));
                page_fields(&mut fields, query);
                if fields.contains_key("Directory") {
                    let directory = fields
                        .remove("Directory")
                        .unwrap_or(Value::Array(Vec::new()));
                    let metadata_len = fields
                        .get("Metadata")
                        .and_then(Value::as_array)
                        .map_or(0, Vec::len);
                    fields.insert(
                        "size".to_string(),
                        json!(metadata_len + directory.as_array().map_or(0, Vec::len)),
                    );
                    let mut ordered = Map::new();
                    ordered.insert("Directory".to_string(), directory);
                    ordered.extend(fields);
                    fields = ordered;
                }
                Response::json(200, self.container(fields, true))
            }
            "allLeaves" => {
                let kind = row.get("type").and_then(Value::as_str).unwrap_or("");
                let (view_group, mut rows) = if kind == "movie" {
                    ("movie", vec![self.child_list_row(row, query)])
                } else if kind == "season" {
                    // A season's own allLeaves is an intentionally empty
                    // episode container; the descendants are exposed by its
                    // /children route and by the owning show/episode views.
                    ("episode", Vec::new())
                } else if kind == "episode" {
                    let parent = row
                        .get("parentRatingKey")
                        .map(string_value)
                        .unwrap_or_default();
                    (
                        "episode",
                        self.children(&parent)
                            .into_iter()
                            .map(|leaf| self.child_list_row(&leaf, query))
                            .collect::<Vec<_>>(),
                    )
                } else {
                    // Cycle 54: `unwatched=` is a 0/1 option on `/allLeaves` too --
                    // anything else a 400 -- and at 1 it drops the watched episodes.
                    let mut leaves = self.leaves(key);
                    if let Some(value) = query.get("unwatched") {
                        if !bool_option(value) {
                            return Response::error(400, "plexsim: bad request");
                        }
                        if value.trim_start_matches('+').trim_start_matches('0') == "1" {
                            leaves.retain(|leaf| !watched_leaf(leaf));
                        }
                    }
                    (
                        "episode",
                        leaves
                            .into_iter()
                            .map(|leaf| self.child_list_row(&leaf, query))
                            .collect::<Vec<_>>(),
                    )
                };
                let section_type = self
                    .section(&string_value(
                        row.get("librarySectionID").unwrap_or(&Value::Null),
                    ))
                    .and_then(|section| section.get("type").and_then(Value::as_str))
                    .unwrap_or("movie");
                let filter_pairs = query
                    .iter()
                    .filter(|(name, _)| {
                        !matches!(
                            name.as_str(),
                            "X-Plex-Container-Start"
                                | "X-Plex-Container-Size"
                                | "excludeElements"
                                | "excludeFields"
                        )
                    })
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect::<Vec<_>>();
                // Cycle 54: the listing's filters are ignored on `/allLeaves`; only
                // `unwatched=` (above) is read.
                let _ = (&filter_pairs, section_type);
                if let Some(sort) = query.get("sort") {
                    rows = match self.sort_rows(rows, Some(sort)) {
                        Ok(rows) => rows,
                        Err(status) => return Response::error(status, "plexsim: bad request"),
                    };
                }
                if query
                    .get("excludeElements")
                    .is_some_and(|value| value.split(',').any(|name| name == "Media"))
                {
                    for row in &mut rows {
                        strip_excluded_element(row, "Media");
                    }
                }
                if let Some(excluded) = query.get("excludeFields") {
                    let names = excluded.split(',').collect::<HashSet<_>>();
                    for shown in &mut rows {
                        strip_excluded_fields(shown, &names);
                    }
                }
                let _ = view_group;
                let first_type = rows
                    .first()
                    .and_then(|row| row.get("type"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                if kind == "show" {
                    // Cycle 17, measured on kids and zen: this list -- and no other
                    // container -- stamps each episode's BORROWED artwork with the
                    // EPISODE's own `addedAt`, so a leaf's cached URL never changes.
                    for shown in &mut rows {
                        restamp_borrowed(shown);
                    }
                    // Measured on kids and zen, 20 rows across five shows: a SHOW's
                    // `/allLeaves` -- and only that list -- gives every episode row the
                    // SHOW's `studio`.
                    if let Some(studio) = row.get("studio").filter(|studio| !studio.is_null()) {
                        for shown in &mut rows {
                            if let Some(object) = shown.as_object_mut() {
                                object.insert("studio".to_string(), studio.clone());
                            }
                        }
                    }
                }
                let mut fields = self.child_container_fields(row, "allLeaves", first_type.as_deref());
                fields.insert("Metadata".to_string(), Value::Array(rows));
                Response::json(200, self.container(fields, true))
            }
            "nearest" => {
                for (name, value) in query {
                    if name == "excludeParentID" && value.parse::<i32>().is_err() {
                        return Response::error(400, "plexsim: bad request");
                    }
                    if name == "maxDistance" && value.parse::<f64>().is_err() {
                        return Response::error(400, "plexsim: bad request");
                    }
                }
                Response::json(
                    200,
                    self.container(Map::from_iter([("size".to_string(), json!(0))]), true),
                )
            }
            "related" => {
                // NOT /hubs/metadata/{id}/related: this one carries the SAME hub plus
                // the media-tag pair the hubs form drops, built by hand with
                // `allowSync: true` said explicitly.
                let mut hubs = self.related_hubs(row, query, true);
                for hub in hubs.iter_mut() {
                    if let Some(rows) = hub.get_mut("Metadata").and_then(Value::as_array_mut) {
                        self.expand_full_rows(rows, query);
                    }
                }
                let section = self
                    .section(&string_value(
                        row.get("librarySectionID").unwrap_or(&Value::Null),
                    ))
                    .cloned()
                    .unwrap_or_default();
                let mut fields = Map::new();
                fields.insert("size".to_string(), json!(hubs.len()));
                fields.insert("Hub".to_string(), Value::Array(hubs));
                fields.insert("allowSync".to_string(), json!(true));
                fields.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
                for (name, value) in [
                    ("librarySectionID", row.get("librarySectionID").cloned()),
                    ("librarySectionTitle", section.get("title").cloned()),
                    ("librarySectionUUID", section.get("uuid").cloned()),
                ] {
                    if let Some(value) = value.filter(|value| !value.is_null()) {
                        fields.insert(name.to_string(), value);
                    }
                }
                fields.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
                fields.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
                Response::json(200, self.container(fields, false))
            }
            "similar" => {
                // Measured: a real PMS 404s this for a season or an episode; for a
                // movie or a show it is a tag container of the Related rows.
                if matches!(row.get("type").and_then(Value::as_str), Some("season" | "episode")) {
                    return Response::error(404, "plexsim: not found");
                }
                let mut rows = self
                    .similar_rows(row)
                    .iter()
                    .map(|row| self.detail_row(row))
                    .collect::<Vec<_>>();
                self.expand_full_rows(&mut rows, query);
                let mut fields = Map::new();
                fields.insert("size".to_string(), json!(rows.len()));
                fields.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
                fields.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
                fields.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
                if !rows.is_empty() {
                    fields.insert("Metadata".to_string(), Value::Array(rows));
                }
                Response::json(200, self.container(fields, false))
            }
            "extras" => {
                if tail.len() > 1 && tail[1] != "all" {
                    return Response::error(404, "plexsim: not found");
                }
                // Cycle 16: a POST here is a 400 whatever the body.
                if request.method == "POST" {
                    return Response::error(400, "plexsim: bad request");
                }
                Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                )
            }
            "subtitles" => {
                if request.method != "POST" {
                    // The Search Subtitles dialog.  Measured: `language` is required
                    // (400 without it) and must be a real two-letter language, any case
                    // and with or without a region -- `en`, `EN`, `en-US` and `de` search,
                    // where `xx`, `eng` and an empty value are 500s.
                    let Some(language) = query.get("language") else {
                        return Response::error(400, "plexsim: bad request");
                    };
                    let code = language.split('-').next().unwrap_or_default().to_lowercase();
                    if !SUBTITLE_LANGUAGES.contains(&code.as_str()) {
                        return Response::error(500, "plexsim: internal error");
                    }
                    return Response::json(
                        200,
                        self.container(Map::from_iter([("size".to_string(), json!(0))]), true),
                    );
                }
                // Cycle 15, measured on the copy: the EMPTY-BODY check runs FIRST.
                // With neither a title nor bytes the answer is 406, not the 500 a
                // missing title gets on its own.
                if request.body.is_empty() {
                    return Response::error(406, "plexsim: not acceptable");
                }
                let Some(title) = query.get("title").filter(|value| !value.is_empty()) else {
                    return Response::error(500, "plexsim: title required");
                };
                let Some((stem, extension)) = title.rsplit_once('.') else {
                    return Response::error(500, "plexsim: title extension required");
                };
                if stem.is_empty() {
                    return Response::error(500, "plexsim: title extension required");
                }
                let format = query.get("format").map(String::as_str).unwrap_or(extension);
                // PMS keys this validation off the form's exact, lower-case
                // format value.  The filename extension is only metadata: a
                // text file named `.txt` is accepted when the form says srt,
                // while an upper-case SRT value is refused.
                if !matches!(format, "psb" | "srt" | "ass" | "ssa" | "smi" | "txt") {
                    return Response::error(400, "plexsim: unsupported subtitle format");
                }
                if request.body.is_empty() || !request.body_complete {
                    return Response::error(406, "plexsim: subtitle body required");
                }
                // Measured on the copy: the new stream's id is the next after the
                // highest the server holds, and it is the Stream the reference writes:
                // the format as `codec` and `format`, the stem as `title`, a
                // `/library/streams/{id}` key, `transient: "0"`, selected -- no language.
                let id = self
                    .catalog
                    .items
                    .iter()
                    .flat_map(|item| item.get("Media").and_then(Value::as_array).cloned().unwrap_or_default())
                    .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
                    .flat_map(|part| part.get("Stream").and_then(Value::as_array).cloned().unwrap_or_default())
                    .filter_map(|stream| stream.get("id").and_then(Value::as_i64))
                    .max()
                    .unwrap_or(0)
                    + 1;
                let content = String::from_utf8_lossy(&request.body).to_string();
                let mut stream_row = Map::new();
                stream_row.insert("id".to_string(), json!(id));
                stream_row.insert("streamType".to_string(), json!(3));
                stream_row.insert("codec".to_string(), json!(format));
                stream_row.insert("format".to_string(), json!(format));
                stream_row.insert("title".to_string(), json!(stem));
                stream_row.insert("key".to_string(), json!(format!("/library/streams/{id}")));
                stream_row.insert("transient".to_string(), json!("0"));
                stream_row.insert("selected".to_string(), json!(true));
                crate::mediainfo::stream_titles(&mut stream_row, None, None);
                stream_row.insert("_content".to_string(), json!(content));
                let stream = Value::Object(stream_row);
                let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
                    return Response::error(404, "plexsim: not found");
                };
                let Some(media) = object.get_mut("Media").and_then(Value::as_array_mut) else {
                    return Response::error(500, "plexsim: no media");
                };
                let Some(part) = media
                    .first_mut()
                    .and_then(|media| media.get_mut("Part"))
                    .and_then(Value::as_array_mut)
                    .and_then(|parts| parts.first_mut())
                else {
                    return Response::error(500, "plexsim: no part");
                };
                let streams = part.as_object_mut().and_then(|part| {
                    part.entry("Stream".to_string())
                        .or_insert_with(|| Value::Array(Vec::new()))
                        .as_array_mut()
                });
                let Some(streams) = streams else {
                    return Response::error(500, "plexsim: no streams");
                };
                for existing in streams.iter_mut() {
                    if existing.get("streamType").and_then(Value::as_i64) == Some(3) {
                        if let Some(existing) = existing.as_object_mut() {
                            existing.remove("selected");
                        }
                    }
                }
                streams.push(stream);
                self.dirty = true;
                self.save_if_dirty();
                Response::empty(200)
            }
            "posters" | "arts" => {
                match request.method.as_str() {
                    // Measured: the plural path is a 404 for a write.
                    "PUT" => Response::error(404, "plexsim: not found"),
                    // Cycle 15: an empty upload body is a 406.
                    "POST" if request.body.is_empty() => Response::error(406, "plexsim: not acceptable"),
                    "POST" => {
                        let body = request.body.clone();
                        self.upload_artwork(key, tail[0], &body)
                    }
                    "GET" | "HEAD" => self.artwork_list(row, tail[0]),
                    _ => Response::error(404, "plexsim: not found"),
                }
            }
            "thumbs" | "banners" | "themes" | "clearLogos" if request.method == "POST" => {
                // Cycle 15: every candidate list answers a POST as an upload does.
                if request.body.is_empty() {
                    return Response::error(406, "plexsim: not acceptable");
                }
                let body = request.body.clone();
                self.upload_artwork(key, tail[0], &body)
            }
            "clearLogos" | "themes" => {
                if request.method != "GET" && request.method != "HEAD" {
                    return Response::error(404, "plexsim: not found");
                }
                Response::json(
                    200,
                    self.container(
                        Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                        false,
                    ),
                )
            }
            "art" | "thumb" | "poster" => self.route_art(key, row, tail, query, request),
            "file" => {
                if request.method != "GET" && request.method != "HEAD" {
                    return Response::error(404, "plexsim: not found");
                }
                // Measured: without its `url` a 400; with a url naming one of this
                // row's candidates it serves that image (cycle 17); otherwise a 404.
                let Some(url) = query.get("url").filter(|url| !url.is_empty()) else {
                    return Response::error(400, "plexsim: bad request");
                };
                let wanted = percent_decode(&url.replace("%2E", "."));
                match self.uploads.get(&wanted) {
                    Some((body, content_type)) => {
                        let mut response = Response::bytes(200, content_type.clone());
                        response.body = body.clone();
                        response
                            .headers
                            .push(("Accept-Ranges".to_string(), "bytes".to_string()));
                        response
                    }
                    None => Response::error(404, "plexsim: not found"),
                }
            }
            "tree" => self.route_tree(row),
            "userState" => Response::error(404, "plexsim: not found"),
            "merge" => {
                if request.method != "PUT" && request.method != "POST" {
                    return Response::error(404, "plexsim: not found");
                }
                // Measured on a writable copy: no `ids` is a 400, an id the server
                // does not have a 404, a row of another type a 400.
                let ids = query
                    .get("ids")
                    .map(|ids| ids.split(',').filter(|id| !id.is_empty()).map(str::to_string).collect::<Vec<_>>())
                    .unwrap_or_default();
                if ids.is_empty() {
                    return Response::error(400, "plexsim: bad request");
                }
                let mut others = Vec::new();
                for other in &ids {
                    let Some(value) = self.catalog.get(other).cloned() else {
                        return Response::error(404, "plexsim: no such item");
                    };
                    if value.get("type") != row.get("type") {
                        return Response::error(400, "plexsim: bad request");
                    }
                    others.push((other.clone(), value));
                }
                if row.get("type").and_then(Value::as_str) == Some("show") {
                    return self.merge_shows(key, &others);
                }
                // Cycle 14, the one DATA-LOSS bug: a merge whose target has no Media of
                // its OWN (a season) absorbs nothing -- a 200, no frames, the tree
                // unchanged.
                if !row.get("Media").is_some_and(truthy) {
                    return Response::empty(200);
                }
                let mut media = row.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
                let mut merged = Vec::new();
                for (other, value) in others {
                    if other == key {
                        continue;
                    }
                    media.extend(value.get("Media").and_then(Value::as_array).cloned().unwrap_or_default());
                    // A merged-away key is retired: a later split never reuses it.
                    self.retire_row(&other);
                    merged.push(value);
                }
                if merged.is_empty() {
                    return Response::empty(200);
                }
                if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                    object.insert("Media".to_string(), Value::Array(media));
                    object.insert("updatedAt".to_string(), json!(now()));
                }
                // Measured: the absorbed row is deleted (state 9, untitled) and the
                // survivor refreshed under type -1, state 3 then 5.
                for gone in &merged {
                    self.push_timeline_entry(gone, 9, Some("deleted"), None, None, Some(""));
                }
                if let Some(survivor) = self.catalog.get(key).cloned() {
                    self.push_timeline_entry(&survivor, 3, None, None, Some(-1), None);
                    self.push_timeline_entry(&survivor, 5, None, None, Some(-1), None);
                }
                self.dirty = true;
                self.save_if_dirty();
                Response::empty(200)
            }
            "split" => {
                if request.method != "PUT" && request.method != "POST" {
                    return Response::error(404, "plexsim: not found");
                }
                self.route_split(key)
            }
            "matches" => Response::json(
                200,
                self.container(
                    Map::from_iter([("Metadata".to_string(), Value::Array(Vec::new()))]),
                    false,
                ),
            ),
            "match" => self.match_item(key, row, query, request),
            "refresh" | "analyze" | "unmatch" => {
                // Measured: `GET .../refresh` and `/analyze` are 404s, and cycle 17:
                // a POST on an item ACTION leaf is a 404 too -- only PUT runs it.
                if request.method != "PUT" {
                    return Response::error(404, "plexsim: not found");
                }
                let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
                match tail[0] {
                    "refresh" => {
                        // Cycle 15: a refresh CASCADES -- a season runs its
                        // episodes inside its own activity, a show nests each
                        // season's block; a leaf is the seven-frame form.
                        match kind.as_str() {
                            "show" => self.notify_show_cascade(key),
                            "season" => self.notify_season_cascade(key),
                            _ => self.notify_refreshed(key),
                        }
                    }
                    "analyze" => {
                        let row = row.clone();
                        self.analyze_item(key, &row);
                    }
                    _ => {
                        self.unmatch_row(key);
                        match kind.as_str() {
                            "episode" => {
                                // Measured: `matching` (state 2), then straight to 5.
                                self.timeline_key(key, 2, Some("matching"), None);
                                self.timeline_key(key, 5, None, None);
                            }
                            "season" => {
                                // Cycle 25: ONE matching frame, then the season cascade.
                                self.timeline_key(key, 2, Some("matching"), None);
                                self.notify_season_cascade(key);
                            }
                            "show" => self.notify_show_unmatch_cascade(key),
                            "movie" => self.notify_movie_unmatch(key),
                            _ => {}
                        }
                        self.dirty = true;
                        self.save_if_dirty();
                    }
                }
                Response::empty(200)
            }
            _ => Response::error(404, "plexsim: not found"),
        }
    }

    fn route_item_prefs(
        &mut self,
        key: &str,
        row: &Value,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if request.method != "PUT" {
            return Response::error(404, "plexsim: not found");
        }
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("");
        if kind == "episode" {
            return Response::error(400, "plexsim: bad request");
        }
        if kind != "show" && kind != "season" && kind != "movie" {
            return Response::error(404, "plexsim: not found");
        }
        let valid = if kind == "show" {
            [
                "episodeSort",
                "autoDeletionItemPolicyUnwatchedLibrary",
                "autoDeletionItemPolicyWatchedLibrary",
                "flattenSeasons",
                "showOrdering",
                "languageOverride",
                "useOriginalTitle",
                "enableCreditsMarkerGeneration",
                "audioLanguage",
                "subtitleLanguage",
                "subtitleMode",
            ]
            .as_slice()
        } else if kind == "season" {
            ["audioLanguage", "subtitleLanguage", "subtitleMode"].as_slice()
        } else {
            [
                "languageOverride",
                "useOriginalTitle",
                "enableCreditsMarkerGeneration",
            ]
            .as_slice()
        };
        if query
            .keys()
            .any(|name| name.starts_with("X-Plex-") || !valid.contains(&name.as_str()))
        {
            return Response::error(400, "plexsim: bad request");
        }
        if query.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        if kind == "movie" {
            let unmatched = row
                .get("guid")
                .and_then(Value::as_str)
                .is_some_and(|value| {
                    value.starts_with("local://")
                        || value.starts_with("none://")
                        || value.starts_with("tv.plex.agents.none://")
                });
            if unmatched {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
            return Response::error(404, "plexsim: not found");
        };
        let settings = object
            .entry("_prefs".to_string())
            .or_insert_with(|| Value::Object(Map::new()));
        let Some(settings) = settings.as_object_mut() else {
            return Response::error(500, "plexsim: internal error");
        };
        for (name, value) in query {
            settings.insert(name.clone(), json!(value));
        }
        self.dirty = true;
        Response::empty(200)
    }

    fn propagate_metadata_change(&mut self, key: &str, changed: &HashSet<String>) {
        let Some(owner) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = owner
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let snapshot = self.catalog.items.clone();
        let descendants = snapshot
            .iter()
            .filter_map(|candidate| {
                let parent = candidate
                    .get("parentRatingKey")
                    .map(string_value)
                    .unwrap_or_default();
                let grandparent = candidate
                    .get("grandparentRatingKey")
                    .map(string_value)
                    .unwrap_or_default();
                let candidate_key = key_of(candidate).unwrap_or_default();
                if parent == key || grandparent == key || candidate_key == key {
                    Some(candidate_key)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        if kind == "show" && changed.contains("contentRating") {
            let inherited = owner.get("contentRating").cloned();
            for child_key in &descendants {
                if child_key == key {
                    continue;
                }
                let Some(child) = self
                    .catalog
                    .get_mut(&child_key)
                    .and_then(Value::as_object_mut)
                else {
                    continue;
                };
                let inherited_before = child
                    .get("_inheritedContentRating")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                if child.get("contentRating").is_none() || inherited_before {
                    match &inherited {
                        Some(value) if !string_value(value).is_empty() => {
                            child.insert("contentRating".to_string(), value.clone());
                            child.insert("_inheritedContentRating".to_string(), json!(true));
                        }
                        _ => {
                            child.remove("contentRating");
                            child.remove("_inheritedContentRating");
                        }
                    }
                }
            }
        }
        if kind == "episode" && changed.contains("contentRating") {
            if let Some(child) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                child.remove("_inheritedContentRating");
            }
        }
        if kind == "season" && changed.contains("summary") {
            let summary = owner.get("summary").cloned();
            for child_key in &descendants {
                if child_key == key {
                    continue;
                }
                if let Some(child) = self
                    .catalog
                    .get_mut(&child_key)
                    .and_then(Value::as_object_mut)
                {
                    if child.get("summary").is_none() || child.get("_inheritedSummary").is_some() {
                        match &summary {
                            Some(value) if !string_value(value).is_empty() => {
                                child.insert("summary".to_string(), value.clone());
                                child.insert("_inheritedSummary".to_string(), json!(true));
                            }
                            _ => {
                                child.remove("summary");
                                child.remove("_inheritedSummary");
                            }
                        }
                    }
                }
            }
        }
        self.dirty = true;
    }

    fn cascade_artwork(
        &mut self,
        owner_key: &str,
        _field: &str,
        _old: Option<Value>,
        _new: Option<Value>,
    ) {
        self.cascade_artwork_from(owner_key);
    }

    /// Merge identical show hierarchies, combining each episode's Media (cycle 41):
    /// the absorbed episode's Media precede the survivor's, the absorbed show, seasons
    /// and episodes disappear, and a shape whose leaves cannot be paired by season and
    /// episode index is left alone. Frames: every removed row state 9 untitled, then
    /// each changed episode state 3/5 under type -1.
    /// `PUT /library/metadata/{id}/match?guid=&name=&year=`. Measured on a writable
    /// copy: the NAME is what it requires -- every verb without one is this leaf's own
    /// 400 (cycle 17) -- and an empty guid with a name renames and refreshes. Matching
    /// to a guid another row of the library already carries MERGES this row into that
    /// one. Every match runs the refresh sequence, activity and all.
    fn match_item(
        &mut self,
        key: &str,
        row: &Value,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        let Some(name) = query.get("name").filter(|_| matches!(request.method.as_str(), "PUT" | "POST")) else {
            return Response::error(400, "plexsim: match needs a name");
        };
        let guid = query.get("guid").cloned().unwrap_or_default();
        let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
        let existing = (!guid.is_empty())
            .then(|| {
                self.catalog.items.iter().find(|other| {
                    key_of(other).as_deref() != Some(key)
                        && other.get("guid").and_then(Value::as_str) == Some(guid.as_str())
                        && other.get("librarySectionID").map(string_value).as_deref() == Some(section.as_str())
                })
            })
            .flatten()
            .cloned();
        self.push_timeline_entry(row, 3, None, None, None, None);
        if let Some(existing) = existing {
            let existing_key = key_of(&existing).unwrap_or_default();
            let mut media = existing.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
            media.extend(row.get("Media").and_then(Value::as_array).cloned().unwrap_or_default());
            if let Some(object) = self.catalog.get_mut(&existing_key).and_then(Value::as_object_mut) {
                object.insert("Media".to_string(), Value::Array(media));
                object.insert("updatedAt".to_string(), json!(now()));
            }
            self.retire_row(key);
            let mut gone = row.clone();
            gone["title"] = json!("");
            if let Some(object) = gone.as_object_mut() {
                object.remove("year");
            }
            self.push_timeline_entry(&gone, 9, Some("deleted"), None, None, None);
            if let Some(survivor) = self.catalog.get(&existing_key).cloned() {
                self.push_timeline_entry(&survivor, 3, None, None, Some(-1), None);
                self.push_timeline_entry(&survivor, 5, None, None, Some(-1), None);
            }
            self.notify_refreshed(&existing_key);
        } else {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                if !guid.is_empty() {
                    object.insert("guid".to_string(), json!(guid));
                }
                if !name.is_empty() {
                    object.insert("title".to_string(), json!(name));
                }
                if let Some(year) = query
                    .get("year")
                    .filter(|year| !year.is_empty())
                    .and_then(|year| year.parse::<i64>().ok())
                {
                    object.insert("year".to_string(), json!(year));
                }
                object.insert("updatedAt".to_string(), json!(now()));
            }
            // Cycle 59: a rename here reaches the children as an edit's does.
            if let Some(updated) = self.catalog.get(key).cloned() {
                let written = ["title".to_string(), "studio".to_string()].into_iter().collect::<HashSet<_>>();
                self.follow_parent(&updated, &written);
            }
            self.notify_refreshed(key);
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    fn merge_shows(&mut self, key: &str, others: &[(String, Value)]) -> Response {
        let target_leaves = self.leaves(key);
        let pair_key = |leaf: &Value| {
            (
                leaf.get("parentIndex").map(py_str).unwrap_or_default(),
                leaf.get("index").map(py_str).unwrap_or_default(),
            )
        };
        let mut changed: Vec<String> = Vec::new();
        let mut removed_rows: Vec<Value> = Vec::new();
        for (other_key, other) in others {
            if other_key == key {
                continue;
            }
            let source_leaves = self.leaves(other_key);
            if source_leaves.is_empty() {
                continue;
            }
            let mut pairs = Vec::new();
            for source in &source_leaves {
                match target_leaves.iter().find(|target| pair_key(target) == pair_key(source)) {
                    Some(target) => pairs.push((key_of(target).unwrap_or_default(), source.clone())),
                    None => {
                        pairs.clear();
                        break;
                    }
                }
            }
            if pairs.len() != source_leaves.len() {
                continue;
            }
            for (target_key, source) in &pairs {
                if let Some(object) = self.catalog.get_mut(target_key).and_then(Value::as_object_mut) {
                    let mut media = source.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
                    media.extend(object.get("Media").and_then(Value::as_array).cloned().unwrap_or_default());
                    object.insert("Media".to_string(), Value::Array(media));
                    object.insert("updatedAt".to_string(), json!(now()));
                }
                if !changed.contains(target_key) {
                    changed.push(target_key.clone());
                }
            }
            let mut source_rows = vec![other.clone()];
            for season in self.children(other_key) {
                let season_key = key_of(&season).unwrap_or_default();
                source_rows.push(season);
                source_rows.extend(self.children(&season_key));
            }
            for gone in &source_rows {
                if let Some(gone_key) = key_of(gone) {
                    self.catalog.remove(&gone_key);
                    self.deleted.insert(gone_key);
                }
            }
            removed_rows.extend(source_rows);
        }
        for gone in &removed_rows {
            self.push_timeline_entry(gone, 9, Some("deleted"), None, None, Some(""));
        }
        for episode in &changed {
            if let Some(row) = self.catalog.get(episode).cloned() {
                self.push_timeline_entry(&row, 3, None, None, Some(-1), None);
                self.push_timeline_entry(&row, 5, None, None, Some(-1), None);
            }
        }
        if !removed_rows.is_empty() {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("updatedAt".to_string(), json!(now()));
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    fn route_art(
        &mut self,
        key: &str,
        row: &Value,
        tail: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        let mut leaf = tail[0];
        let method = request.method.as_str();
        // Cycle 52: the artwork read leaves take GET, HEAD, PUT and DELETE and answer
        // the ordinary 404 to every other verb.
        if !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE" | "PATCH") {
            return Response::error(404, "plexsim: not found");
        }
        // And the VERSIONED path is not the write.
        if method == "PUT" && tail.len() >= 2 {
            return Response::error(404, "plexsim: not found");
        }
        if matches!(method, "GET" | "HEAD") && !owns_artwork(row, leaf) {
            return Response::error(404, "plexsim: not found");
        }
        if leaf == "thumb" && method == "DELETE" {
            leaf = "poster";
        }
        if leaf == "poster" || leaf == "art" {
            if method == "PUT" {
                return self.set_artwork(key, if leaf == "poster" { "posters" } else { "arts" }, query);
            }
            // Cycle 31: `DELETE .../{poster,thumb,art}` is a 404 on 1.42.2.
            if method == "DELETE" {
                return Response::error(404, "plexsim: not found");
            }
        }
        if method != "GET" && method != "HEAD" {
            return Response::error(404, "plexsim: not found");
        }
        let field = if leaf == "art" { "art" } else { "thumb" };
        if row.get(field).map_or(true, Value::is_null) {
            return Response::error(404, "plexsim: not found");
        }
        // The image the row points at: an uploaded one, or the placeholder.  Cycle
        // 29: `X-Plex-Local-Asset: 1` marks an ITEM's own artwork.
        let private = if field == "thumb" {
            row.get("_poster_url")
        } else {
            row.get("_art_url").or_else(|| row.get("_poster_url"))
        };
        let mut response = match private.and_then(Value::as_str).and_then(|url| self.uploads.get(url)) {
            Some((body, content_type)) => {
                let mut response = Response::bytes(200, content_type.clone());
                response.body = body.clone();
                response
            }
            None if query.get("width").is_some() => Response::image(png_placeholder()),
            None => Response::image(jpeg_placeholder()),
        };
        response
            .headers
            .push(("Accept-Ranges".to_string(), "bytes".to_string()));
        response
            .headers
            .push(("X-Plex-Local-Asset".to_string(), "1".to_string()));
        response
    }

    fn annotate_file_checks(&self, row: &Value) -> Value {
        let mut out = row.clone();
        if let Some(object) = out.as_object_mut() {
            object.insert("_filesChecked".to_string(), json!(true));
        }
        let Some(media_rows) = out.get_mut("Media").and_then(Value::as_array_mut) else {
            return out;
        };
        for media in media_rows {
            let Some(parts) = media.get_mut("Part").and_then(Value::as_array_mut) else {
                continue;
            };
            for part in parts {
                // Cycle 46: a successful unrestrict is the fileless server's
                // existence check and the provider's explicit "unavailable" the
                // false/false pair; other failures are not collapsed into "missing".
                let available = part
                    .get("_debrid")
                    .and_then(Value::as_str)
                    .filter(|link| !link.is_empty())
                    .map_or(true, |link| self.resolve_link(link) != Err(true));
                if let Some(object) = part.as_object_mut() {
                    object.insert("accessible".to_string(), json!(available));
                    object.insert("exists".to_string(), json!(available));
                }
            }
        }
        out
    }

    fn with_alternates(&self, mut row: Value) -> Value {
        let key = key_of(&row).unwrap_or_default();
        let Some(object) = row.as_object() else {
            return row;
        };
        if key.is_empty() {
            return row;
        }
        let thumb = object
            .get("_poster_url")
            .and_then(Value::as_str)
            .map(|url| json!([{"key":url,"source":"tmdb","previewKey":url}]));
        let art = object
            .get("_art_url")
            .and_then(Value::as_str)
            .map(|url| json!([{"key":url,"source":"tmdb","previewKey":url}]));
        let theme = object
            .get("_theme_url")
            .and_then(Value::as_str)
            .map(|url| json!([{"key":url,"source":"tmdb","previewKey":url}]));
        if thumb.is_none() && art.is_none() && theme.is_none() {
            return row;
        }
        // serde_json's Map is insertion ordered in this crate.  The measured
        // alternate block is ordered Genre/Country/Thumb/Art/Guid/Rating/Theme
        // and then the remaining agent fields, so rebuild that small ordered
        // set as a unit rather than appending Thumb/Art after every existing tag.
        let old = object.clone();
        let mut rebuilt = Map::new();
        let ordered = [
            ("Genre", None),
            ("Country", None),
            ("Thumb", thumb),
            ("Art", art),
            ("Guid", None),
            ("Rating", None),
            ("Theme", theme),
            ("Director", None),
        ];
        let has_ordered = ordered
            .iter()
            .any(|(name, value)| value.is_some() || old.contains_key(*name));
        let mut inserted_ordered = false;
        for (name, value) in old {
            if ordered.iter().any(|(known, _)| *known == name) {
                if !inserted_ordered {
                    for (known, alternate) in &ordered {
                        if let Some(alternate) = alternate {
                            rebuilt.insert((*known).to_string(), alternate.clone());
                        } else if let Some(existing) = object.get(*known) {
                            rebuilt.insert((*known).to_string(), existing.clone());
                        }
                    }
                    inserted_ordered = true;
                }
                continue;
            }
            rebuilt.insert(name, value);
        }
        if has_ordered && !inserted_ordered {
            for (known, alternate) in &ordered {
                if let Some(alternate) = alternate {
                    rebuilt.insert((*known).to_string(), alternate.clone());
                } else if let Some(existing) = object.get(*known) {
                    rebuilt.insert((*known).to_string(), existing.clone());
                }
            }
        }
        row = Value::Object(rebuilt);
        row
    }

    /// `/library/metadata/{id}/tree` -- the raw item/media/part tree Get Info
    /// reads.  It NESTS (a show holds its seasons, each season its episodes), an
    /// EPISODE's tree is rooted at its SHOW and (cycle 21, kids and zen) carries
    /// only the ancestor CHAIN -- the show, that episode's season, the episode.
    fn route_tree(&self, row: &Value) -> Response {
        let mut top = row.clone();
        let mut keep: Option<HashSet<String>> = None;
        if row.get("type").and_then(Value::as_str) == Some("episode") {
            if let Some(show) = row
                .get("grandparentRatingKey")
                .map(string_value)
                .and_then(|key| self.catalog.get(&key))
            {
                top = show.clone();
                keep = Some(HashSet::from([
                    row.get("parentRatingKey").map(string_value).unwrap_or_default(),
                    key_of(row).unwrap_or_default(),
                ]));
            }
        }
        let item = self.tree_node(&top, &top, keep.as_ref());
        Response::json(
            200,
            json!({"MediaContainer": {
                "size": 1,
                "identifier": "com.plexapp.plugins.library",
                "mediaTagPrefix": MEDIA_TAG_PREFIX,
                "mediaTagVersion": MEDIA_TAG_VERSION,
                "MetadataItem": [item],
            }}),
        )
    }

    /// One node of the tree (server.py's `metadata_tree` item): a MediaItem
    /// carries ONLY its id; a MediaPart its id, file, size and duration (an
    /// episode's leads with `metadataType` 4) and its streams as MediaStream; a
    /// season's title is always EMPTY and every node's parentTitle is the ROOT's
    /// (a season root's show title, else empty); a show's node ends with eight
    /// of its settings, every value and default a string.
    fn tree_node(&self, row: &Value, root: &Value, keep: Option<&HashSet<String>>) -> Value {
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("");
        let mut media_items = Vec::new();
        for media in row.get("Media").and_then(Value::as_array).into_iter().flatten() {
            let mut parts = Vec::new();
            for part in media.get("Part").and_then(Value::as_array).into_iter().flatten() {
                let mut entry = Map::new();
                if kind == "episode" {
                    entry.insert("metadataType".to_string(), json!(4));
                }
                for name in ["id", "file", "size", "duration"] {
                    entry.insert(name.to_string(), part.get(name).cloned().unwrap_or(Value::Null));
                }
                let streams = part
                    .get("Stream")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(tree_stream)
                    .collect::<Vec<_>>();
                if !streams.is_empty() {
                    entry.insert("MediaStream".to_string(), Value::Array(streams));
                }
                entry.retain(|_, value| !value.is_null());
                parts.push(Value::Object(entry));
            }
            media_items.push(json!({"id": media.get("id").cloned().unwrap_or(Value::Null), "MediaPart": parts}));
        }
        let title = if kind == "season" {
            String::new()
        } else {
            self.detail_title(row)
        };
        let parent_title = if root.get("type").and_then(Value::as_str) == Some("season") {
            root.get("parentRatingKey")
                .map(string_value)
                .and_then(|key| self.catalog.get(&key))
                .and_then(|show| show.get("title"))
                .map(string_value)
                .unwrap_or_default()
        } else {
            String::new()
        };
        let number = |name: &str| row.get(name).filter(|value| !value.is_null()).cloned();
        let mut item = Map::new();
        item.insert(
            "id".to_string(),
            json!(key_of(row).and_then(|key| key.parse::<i64>().ok()).unwrap_or(0)),
        );
        item.insert("title".to_string(), json!(title));
        item.insert("guid".to_string(), json!(row.get("guid").map(string_value).unwrap_or_default()));
        item.insert("parentTitle".to_string(), json!(parent_title));
        item.insert("instanceRatingKey".to_string(), json!(""));
        item.insert(
            "index".to_string(),
            number("index").filter(|value| value.as_i64() != Some(0)).unwrap_or(json!(1)),
        );
        item.insert(
            "originallyAvailableAt".to_string(),
            number("originallyAvailableAt").unwrap_or(json!("")),
        );
        item.insert("addedAt".to_string(), number("addedAt").unwrap_or(json!(0)));
        item.insert("refreshedAt".to_string(), number("updatedAt").unwrap_or(json!(0)));
        if !media_items.is_empty() {
            item.insert("MediaItem".to_string(), Value::Array(media_items));
        }
        let children = self
            .children(&key_of(row).unwrap_or_default())
            .into_iter()
            .filter(|child| keep.is_none_or(|keep| keep.contains(&key_of(child).unwrap_or_default())))
            .map(|child| self.tree_node(&child, root, keep))
            .collect::<Vec<_>>();
        if !children.is_empty() {
            item.insert("MetadataItem".to_string(), Value::Array(children));
        }
        if kind == "show" {
            let names = [
                "episodeSort",
                "autoDeletionItemPolicyUnwatchedLibrary",
                "autoDeletionItemPolicyWatchedLibrary",
                "flattenSeasons",
                "showOrdering",
                "audioLanguage",
                "subtitleLanguage",
                "subtitleMode",
            ];
            let known = self.item_preferences("show", row);
            let mut settings = Vec::new();
            for name in names {
                let Some(mut setting) = known
                    .iter()
                    .find(|setting| setting.get("id").and_then(Value::as_str) == Some(name))
                    .cloned()
                else {
                    continue;
                };
                for field in ["default", "value"] {
                    let text = match setting.get(field) {
                        None | Some(Value::Null) => String::new(),
                        Some(Value::Bool(flag)) => if *flag { "1" } else { "0" }.to_string(),
                        Some(value) => string_value(value),
                    };
                    setting[field] = json!(text);
                }
                settings.push(setting);
            }
            if !settings.is_empty() {
                item.insert("Setting".to_string(), Value::Array(settings));
            }
        }
        Value::Object(item)
    }

    /// A row's own title as the detail row prints it (a generated season or
    /// episode label materialised from the index when stored empty).
    fn detail_title(&self, row: &Value) -> String {
        let title = row.get("title").map(string_value).unwrap_or_default();
        if title.is_empty() && row.get("type").and_then(Value::as_str) == Some("episode") {
            return format!("Episode {}", row.get("index").map(string_value).unwrap_or_default());
        }
        title
    }

    fn route_playlist_metadata(
        &mut self,
        playlist: &Playlist,
        tail: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        // Cycle 16, measured on plexscratch: EVERY item write on a smart list is a 400
        // -- the move, the single delete and the clear -- because a smart list's items
        // are its query.
        if playlist.smart
            && tail.first() == Some(&"items")
            && ((tail.len() >= 2 && matches!(request.method.as_str(), "PUT" | "DELETE"))
                || (tail.len() == 1 && request.method == "DELETE"))
        {
            return Response::error(400, "plexsim: bad request");
        }
        if tail.len() == 3 && tail[0] == "items" && tail[2] == "move" && request.method == "PUT" {
            let Some(item_id) = tail[1].parse::<i64>().ok() else {
                return Response::error(404, "plexsim: no such playlist item");
            };
            let Some(stored) = self.playlists.get_mut(&playlist.id) else {
                return Response::error(404, "plexsim: not found");
            };
            let Some(index) = stored.item_ids.iter().position(|value| *value == item_id) else {
                return Response::error(404, "plexsim: no such playlist item");
            };
            // Measured: moving an item after ITSELF leaves the order alone (it
            // would otherwise read as an unknown `after` and go to the front).
            let after_itself = query.get("after").is_some_and(|after| *after == tail[1]);
            let index = if after_itself { usize::MAX } else { index };
            if index == usize::MAX {
                self.dirty = true;
                let updated = self.playlists.get(&playlist.id).cloned().unwrap_or_else(|| playlist.clone());
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([(
                            "Metadata".to_string(),
                            Value::Array(vec![self.dynamic_playlist_row(&updated)]),
                        )]),
                        false,
                    ),
                );
            }
            let item = stored.items.remove(index);
            let id = stored.item_ids.remove(index);
            let position = query
                .get("after")
                .and_then(|value| value.parse::<i64>().ok())
                .and_then(|after| {
                    stored
                        .item_ids
                        .iter()
                        .position(|value| *value == after)
                        .map(|i| i + 1)
                })
                .unwrap_or(0);
            stored.items.insert(position, item);
            stored.item_ids.insert(position, id);
            self.dirty = true;
            let updated = self
                .playlists
                .get(&playlist.id)
                .cloned()
                .unwrap_or_else(|| playlist.clone());
            return Response::json(
                200,
                self.container(
                    Map::from_iter([(
                        "Metadata".to_string(),
                        Value::Array(vec![self.dynamic_playlist_row(&updated)]),
                    )]),
                    false,
                ),
            );
        }
        if tail.first() == Some(&"items") && request.method == "POST" {
            return Response::error(404, "plexsim: not found");
        }
        // The metadata alias still resolves a playlist's `/prefs` spelling
        // through the ordinary item writer.  It is a malformed field write,
        // so PMS returns the writer's 400 rather than treating it as a
        // missing playlist endpoint (the `/playlists/{id}/prefs` spelling is
        // a different, unimplemented route).
        if tail.first() == Some(&"prefs") {
            return Response::error(400, "plexsim: bad request");
        }
        if (tail.first() == Some(&"items") || tail.first() == Some(&"children"))
            && (request.method == "GET" || request.method == "HEAD")
        {
            let type_filter = if let Some(value) = query.get("type") {
                let Ok(value) = value.parse::<f64>() else {
                    return Response::error(400, "plexsim: bad request");
                };
                if value.fract() != 0.0 {
                    return Response::error(400, "plexsim: bad request");
                }
                Some(value as i64)
            } else {
                None
            };
            let mut typed_rows = Vec::new();
            for key in self.playlist_keys(playlist) {
                let Some(row) = self.catalog.get(&key).cloned() else {
                    continue;
                };
                let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
                match type_filter {
                    None | Some(-1) => typed_rows.push(row),
                    Some(1) if kind == "movie" => typed_rows.push(row),
                    Some(2) if kind == "show" => typed_rows.push(row),
                    Some(3) if kind == "season" => typed_rows.push(row),
                    Some(4) if kind == "episode" => typed_rows.push(row),
                    Some(3) if kind == "show" => {
                        let parent = key_of(&row).unwrap_or_default();
                        typed_rows.extend(self.children(&parent).into_iter().filter(|child| {
                            child.get("type").and_then(Value::as_str) == Some("season")
                        }));
                    }
                    Some(4) if matches!(kind, "show" | "season") => {
                        let parent = key_of(&row).unwrap_or_default();
                        typed_rows.extend(self.leaves(&parent).into_iter().filter(|child| {
                            child.get("type").and_then(Value::as_str) == Some("episode")
                        }));
                    }
                    _ => {}
                }
            }
            let mut rows = typed_rows
                .into_iter()
                .filter(|row| {
                    query
                        .get("title")
                        .map(|needle| {
                            row.get("title")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .contains(needle)
                        })
                        .unwrap_or(true)
                })
                .filter(|row| {
                    query
                        .get("unwatched")
                        .map(|value| {
                            value == "1"
                                && row.get("viewCount").and_then(Value::as_i64).unwrap_or(0) == 0
                        })
                        .unwrap_or(true)
                })
                .map(|row| {
                    // The light grid row, as measured on the copy; a playlist item
                    // keeps its librarySection fields.
                    let mut value = self.grid_row(&row, true, false, false);
                    // Cycle 16: a SMART list's items carry NO `playlistItemID` -- its
                    // members are its query -- and a parent a `type=` reaches up to is
                    // not a member either.
                    let item_id = (!playlist.smart)
                        .then(|| {
                            let key = key_of(&row)?;
                            let index = playlist.items.iter().position(|item| *item == key)?;
                            playlist.item_ids.get(index).copied()
                        })
                        .flatten();
                    if let (Some(object), Some(item_id)) = (value.as_object_mut(), item_id) {
                        object.insert("playlistItemID".to_string(), json!(item_id));
                    }
                    value
                })
                .collect::<Vec<_>>();
            if let Some(year_filter) = query.get("year>>=") {
                let Ok(year) = year_filter.parse::<i64>() else {
                    return Response::error(400, "plexsim: bad request");
                };
                rows.retain(|row| row.get("year").and_then(Value::as_i64).unwrap_or(0) >= year);
            }
            if let Some(sort) = query.get("sort") {
                if sort != "" && sort != "titleSort" && sort != "titleSort:asc" {
                    return Response::error(500, "plexsim: bad request");
                }
            }
            if let Some(limit) = query.get("limit") {
                if limit.parse::<i64>().is_err() {
                    return Response::error(500, "plexsim: bad request");
                }
            }
            // The container repeats the list's own identity.  Measured: its
            // `duration` is in SECONDS where the row's is in milliseconds (rounded),
            // and `includeMeta=1` carries the tab bar's Type rows, every one `mixed`
            // and keyed by this path with a `?type=`.
            let summary = self.playlist_row(playlist, false, true);
            let mut fields = Map::new();
            if let Some(composite) = summary.get("composite") {
                fields.insert("composite".to_string(), composite.clone());
            }
            if let Some(duration) = summary.get("duration").and_then(Value::as_i64) {
                fields.insert(
                    "duration".to_string(),
                    json!((duration as f64 / 1000.0).round() as i64),
                );
            }
            fields.insert("leafCount".to_string(), summary.get("leafCount").cloned().unwrap_or(json!(0)));
            if query.get("includeMeta").is_some_and(|value| value == "1") {
                let path = format!("/playlists/{}/items", playlist.id);
                fields.insert(
                    "Meta".to_string(),
                    json!({"Type": [
                        {"key": path, "type": "mixed", "title": "All", "active": true},
                        {"key": format!("{path}?type=1"), "type": "mixed", "title": "Movies", "active": false},
                        {"key": format!("{path}?type=4"), "type": "mixed", "title": "Episodes", "active": false},
                        {"key": format!("{path}?type=3"), "type": "mixed", "title": "Seasons", "active": false},
                        {"key": format!("{path}?type=2"), "type": "mixed", "title": "TV Shows", "active": false},
                    ]}),
                );
            }
            for name in ["playlistType", "ratingKey", "smart", "title"] {
                fields.insert(name.to_string(), summary.get(name).cloned().unwrap_or(Value::Null));
            }
            fields.insert("Metadata".to_string(), Value::Array(rows));
            page_fields(&mut fields, query);
            return Response::json(200, self.container(fields, false));
        }
        if tail.first() == Some(&"composite") {
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            return Response::image(jpeg_placeholder());
        }
        if tail.is_empty() && request.method == "PUT" {
            let pairs = Self::request_pairs(request);
            return self.edit_playlist_alias(&playlist.id, query, &pairs);
        }
        if tail.is_empty() && request.method == "GET" {
            let row = self.dynamic_playlist_row(playlist);
            let mut fields = Map::new();
            fields.insert("size".to_string(), json!(1));
            fields.insert("allowSync".to_string(), json!(false));
            fields.insert(
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            );
            fields.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
            fields.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
            fields.insert("Metadata".to_string(), Value::Array(vec![row]));
            return Response::json(
                200,
                Value::Object(Map::from_iter([(
                    "MediaContainer".to_string(),
                    Value::Object(fields),
                )])),
            );
        }
        if tail.len() == 1 && matches!(tail[0], "posters" | "arts" | "clearLogos" | "themes") {
            return self.list_artwork_response(&playlist.id, false, tail[0], request);
        }
        if tail == ["items"] && request.method == "PUT" && playlist.smart {
            return self.save_smart_playlist(&playlist.id, query);
        }
        if tail.first() == Some(&"items") && request.method == "PUT" {
            let mut keys = self.rows_for_uri(query.get("uri").map(String::as_str).unwrap_or(""));
            if keys.is_empty() {
                // Measured: a queue can be saved as a playlist by its id.
                if let Some(queue) = query.get("playQueueID").and_then(|id| self.queues.get(id)) {
                    keys = queue
                        .items
                        .iter()
                        .filter_map(|item| self.catalog.get(&item.rating_key).cloned())
                        .collect();
                }
            }
            let requested = keys.len();
            let Some(stored) = self.playlists.get_mut(&playlist.id) else {
                return Response::error(404, "plexsim: not found");
            };
            let mut added = 0_i64;
            for row in &keys {
                let Some(key) = key_of(&row) else { continue };
                if !stored.items.contains(&key) {
                    stored.items.push(key);
                    self.next_playlist_item += 1;
                    stored.item_ids.push(self.next_playlist_item);
                    added += 1;
                }
            }
            if added > 0 {
                self.edits
                    .entry(playlist.id.clone())
                    .or_default()
                    .insert("updatedAt".to_string(), json!(now()));
            }
            self.dirty = true;
            self.save_if_dirty();
            let updated = self
                .playlists
                .get(&playlist.id)
                .cloned()
                .unwrap_or_else(|| playlist.clone());
            let fields = Map::from_iter([
                ("leafCountAdded".to_string(), json!(added)),
                ("leafCountRequested".to_string(), json!(requested)),
                (
                    "Metadata".to_string(),
                    Value::Array(vec![self.dynamic_playlist_row(&updated)]),
                ),
            ]);
            return Response::json(200, self.container(fields, false));
        }
        if tail.first() == Some(&"items") && request.method == "DELETE" {
            if tail.len() == 1 {
                if let Some(stored) = self.playlists.get_mut(&playlist.id) {
                    stored.items.clear();
                    stored.item_ids.clear();
                    self.dirty = true;
                }
                let current = self
                    .playlists
                    .get(&playlist.id)
                    .cloned()
                    .unwrap_or_else(|| playlist.clone());
                return Response::json(
                    200,
                    self.container(
                        Map::from_iter([(
                            "Metadata".to_string(),
                            Value::Array(vec![self.dynamic_playlist_row(&current)]),
                        )]),
                        false,
                    ),
                );
            }
            if let Some(item_id) = tail.get(1).and_then(|value| value.parse::<i64>().ok()) {
                let mut removed = false;
                if let Some(stored) = self.playlists.get_mut(&playlist.id) {
                    if let Some(index) = stored.item_ids.iter().position(|value| *value == item_id)
                    {
                        stored.item_ids.remove(index);
                        stored.items.remove(index);
                        self.dirty = true;
                        removed = true;
                    }
                }
                if removed {
                    let current = self
                        .playlists
                        .get(&playlist.id)
                        .cloned()
                        .unwrap_or_else(|| playlist.clone());
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([(
                                "Metadata".to_string(),
                                Value::Array(vec![self.dynamic_playlist_row(&current)]),
                            )]),
                            false,
                        ),
                    );
                }
            }
            return Response::error(404, "plexsim: no such playlist item");
        }
        Response::error(404, "plexsim: not found")
    }

    fn route_playlist_path(
        &mut self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if segments.is_empty() {
            if request.method == "POST" {
                return self.create_playlist_ported(query);
            }
            return self.route_playlists(query);
        }
        if segments.len() == 1 && segments[0] == "sorts" {
            return Response::json(
                200,
                self.container(
                    Map::from_iter([(
                        "Directory".to_string(),
                        Value::Array(self.playlist_sort_rows()),
                    )]),
                    false,
                ),
            );
        }
        if segments.len() == 1 && segments[0] == "all" {
            if request.method != "GET" && request.method != "HEAD" {
                return Response::error(404, "plexsim: not found");
            }
            return self.route_playlist_all(query);
        }
        if segments.len() == 1 && segments[0] == "upload" {
            if request.method != "POST" {
                return Response::error(404, "plexsim: not found");
            }
            let Some(section) = query.get("sectionID") else {
                return Response::error(400, "plexsim: sectionID required");
            };
            let Some(path) = query.get("path") else {
                return Response::error(400, "plexsim: path required");
            };
            if self.section(section).is_none() {
                return Response::error(400, "plexsim: sectionID required");
            }
            let _ = path;
            return Response::error(500, "plexsim: playlist importer unavailable");
        }
        let id = segments[0];
        if id.parse::<i64>().is_ok_and(|value| value > i32::MAX as i64) {
            return Response::error(400, "plexsim: bad request");
        }
        if id == "39999" {
            if request.method == "GET" || request.method == "HEAD" {
                if segments.len() == 2 && segments[1] == "items" {
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([
                                ("size".to_string(), json!(0)),
                                ("totalSize".to_string(), json!(0)),
                                ("offset".to_string(), json!(0)),
                            ]),
                            false,
                        ),
                    );
                }
                if segments.len() == 1 {
                    let row = json!({
                        "ratingKey": "39999",
                        "key": "/playlists/39999/items",
                        "guid": "com.plexapp.agents.none://55d3184c-28b9-43ce-842c-b7b03ca00e0f",
                        "type": "playlist",
                        "title": "Background Processing List",
                        "summary": "",
                        "playlistType": "backgroundProcessing",
                    });
                    return Response::json(
                        200,
                        self.container(
                            Map::from_iter([("Metadata".to_string(), json!([row]))]),
                            false,
                        ),
                    );
                }
            }
            return Response::error(404, "plexsim: not found");
        }
        if request.method == "DELETE" && segments.len() == 1 {
            let existed = self.playlists.remove(id).is_some();
            if existed {
                self.push_list_timeline(id, 15, 9, "deleted");
            }
            self.dirty = true;
            self.save_if_dirty();
            return if existed {
                Response::empty(204)
            } else {
                Response::error(404, "plexsim: not found")
            };
        }
        if request.method == "PUT" && segments.len() == 1 {
            let pairs = Self::request_pairs(request);
            return self.edit_playlist_ported(id, query, &pairs);
        }
        if let Some(p) = self.playlists.get(id).cloned() {
            if segments.len() == 1 && matches!(request.method.as_str(), "GET" | "HEAD") {
                let mut fields = Map::new();
                fields.insert("size".to_string(), json!(1));
                fields.insert(
                    "Metadata".to_string(),
                    Value::Array(vec![self.playlist_row(&p, true, false)]),
                );
                return Response::json(
                    200,
                    Value::Object(Map::from_iter([(
                        "MediaContainer".to_string(),
                        Value::Object(fields),
                    )])),
                );
            }
            if segments.len() == 3 && segments[1] == "items" && request.method == "PUT" {
                // Cycle 18, measured on plexscratch: a PUT at ONE member is a 400
                // for a member id and an unknown number alike (every item write on
                // a smart list is too), and a 404 for an id that is not a number --
                // the SHAPE is checked before membership.
                let numeric = !segments[2].is_empty() && segments[2].bytes().all(|b| b.is_ascii_digit());
                return if p.smart || numeric {
                    Response::error(400, "plexsim: bad request")
                } else {
                    Response::error(404, "plexsim: no such playlist item")
                };
            }
            return self.route_playlist_metadata(&p, &segments[1..], query, request);
        }
        Response::error(404, "plexsim: not found")
    }

    fn create_playlist(&mut self, query: &HashMap<String, String>) -> Response {
        let Some(title) = query.get("title") else {
            return Response::error(400, "plexsim: title required");
        };
        let Some(kind) = query.get("type") else {
            return Response::error(400, "plexsim: type required");
        };
        if !matches!(kind.as_str(), "video" | "audio" | "photo") {
            return Response::error(500, "plexsim: unsupported playlist type");
        }
        let Some(uri) = query.get("uri") else {
            return Response::error(400, "plexsim: uri required");
        };
        let source_rows = self.rows_for_uri(uri);
        let mismatch = kind == "audio"
            && source_rows
                .iter()
                .any(|row| row.get("type").and_then(Value::as_str) == Some("movie"));
        let id = format!("{}", self.next_playlist);
        self.next_playlist += 1;
        let p = Playlist {
            id: id.clone(),
            title: title.clone(),
            // A type-mismatched create still commits the playlist shell and
            // its `created` timeline entry, but PMS leaves the item list
            // empty before returning 400.
            items: if mismatch {
                Vec::new()
            } else {
                query
                    .get("uri")
                    .map(|uri| self.rows_for_uri(uri))
                    .unwrap_or_default()
                    .iter()
                    .filter_map(key_of)
                    .collect()
            },
            item_ids: query
                .get("uri")
                .filter(|_| !mismatch)
                .map(|uri| {
                    let count = self.rows_for_uri(uri).len();
                    (0..count)
                        .map(|_| {
                            let value = self.next_dynamic_key;
                            self.next_dynamic_key += 1;
                            value
                        })
                        .collect()
                })
                .unwrap_or_default(),
            smart: query.get("smart").is_some_and(|v| v == "1"),
            uri: query.get("uri").cloned().unwrap_or_default(),
        };
        let row = self.dynamic_playlist_row(&p);
        self.playlists.insert(id, p);
        self.edits
            .entry(row.get("ratingKey").map(string_value).unwrap_or_default())
            .or_default()
            .insert("playlistType".to_string(), json!(kind));
        let playlist_id = row.get("ratingKey").map(string_value).unwrap_or_default();
        self.push_list_timeline(&playlist_id, 15, 0, "created");
        self.dirty = true;
        self.save_if_dirty();
        if mismatch {
            return Response::error(400, "plexsim: playlist type mismatch");
        }
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), vec![row].into_iter().collect())]),
                false,
            ),
        )
    }

    fn edit_playlist(&mut self, id: &str, query: &HashMap<String, String>) -> Response {
        let allowed = [
            "title",
            "title.value",
            "titleSort",
            "titleSort.value",
            "summary",
            "summary.value",
            "summary",
            "contentRating",
            "contentRating.value",
            "year",
            "year.value",
            "studio",
            "studio.value",
            "originallyAvailableAt",
            "originallyAvailableAt.value",
            "thumb",
            "thumb.value",
            "art",
            "art.value",
            "userRating",
            "userRating.value",
            "guid",
            "duration",
            "addedAt",
            "updatedAt",
            "viewCount",
            "lastViewedAt",
            "viewUpdatedAt",
            "type",
        ];
        let names = query
            .keys()
            .filter(|name| !name.starts_with("X-Plex-"))
            .collect::<Vec<_>>();
        if names.is_empty()
            || names
                .iter()
                .any(|name| !allowed.contains(&name.as_str()) && !name.starts_with("label["))
        {
            return Response::error(400, "plexsim: bad request");
        }
        let Some(playlist) = self.playlists.get_mut(id) else {
            return Response::error(404, "plexsim: not found");
        };
        if let Some(title) = query.get("title.value").or_else(|| query.get("title")) {
            playlist.title = title.clone();
        }
        let edits = self.edits.entry(id.to_string()).or_default();
        for name in names {
            if let Some(value) = query.get(name.as_str()) {
                let base = name
                    .strip_suffix(".value")
                    .unwrap_or(name.as_str())
                    .to_string();
                if matches!(base.as_str(), "title" | "summary") {
                    edits.insert(base, json!(value));
                } else if !matches!(
                    base.as_str(),
                    "userRating"
                        | "guid"
                        | "duration"
                        | "addedAt"
                        | "updatedAt"
                        | "viewCount"
                        | "lastViewedAt"
                        | "type"
                ) {
                    edits.insert(base, json!(value));
                }
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `PUT /library/parts/{partID}?audioStreamID=&subtitleStreamID=` -- track
    /// selection.  Cycle 44, measured on both writable copies: the two
    /// parameters have DIFFERENT grammars.  `subtitleStreamID` parses as a
    /// signed integer, a value that does not parse reads as 0 (clears), `-1`
    /// reverts to the default, any other negative is a 400; `audioStreamID`
    /// has no 0 -- 0 and negatives but -1 are 400s, and an unparseable value is
    /// an accepted no-op.  Cycle 18: the PART is resolved before anything else
    /// (unknown or malformed is a 404), and a request naming no stream is a 200.
    fn select_stream(&mut self, part_id: &str, query: &HashMap<String, String>) -> Response {
        let mut wanted: Vec<(i64, String)> = Vec::new();
        for (name, stream_type) in [("audioStreamID", 2_i64), ("subtitleStreamID", 3_i64)] {
            let Some(raw) = query.get(name) else {
                continue;
            };
            let number = raw.parse::<i64>().ok().filter(|_| {
                // Python's int(): an optional sign and digits, nothing else.
                let digits = raw.strip_prefix(['-', '+']).unwrap_or(raw);
                !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
            });
            if stream_type == 3 {
                let number = number.unwrap_or(0);
                if number < -1 {
                    return Response::error(400, "plexsim: bad request");
                }
                wanted.push((3, (if number == -1 { 0 } else { number }).to_string()));
            } else {
                let Some(number) = number else {
                    continue;
                };
                if number == 0 || number < -1 {
                    return Response::error(400, "plexsim: bad request");
                }
                wanted.push((2, number.to_string()));
            }
        }
        let owner_index = self.catalog.items.iter().position(|row| {
            row.get("Media").and_then(Value::as_array).into_iter().flatten().any(|media| {
                media.get("Part").and_then(Value::as_array).into_iter().flatten().any(|part| {
                    part.get("id").map(string_value).as_deref() == Some(part_id)
                })
            })
        });
        let Some(owner_index) = owner_index else {
            return Response::error(404, "plexsim: no such part");
        };
        if wanted.is_empty() {
            return Response::empty(200);
        }
        let owner = self.catalog.items[owner_index].clone();
        let streams_of = |stream_type: i64| {
            owner
                .get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
                .flat_map(|part| part.get("Stream").and_then(Value::as_array).cloned().unwrap_or_default())
                .filter(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(stream_type))
                .filter_map(|stream| stream.get("id").map(string_value))
                .collect::<Vec<_>>()
        };
        for entry in wanted.iter_mut() {
            if entry.0 == 2 && entry.1 == "-1" {
                // Revert to the default: the first audio stream.
                match streams_of(2).first() {
                    Some(first) => entry.1 = first.clone(),
                    None => entry.1 = String::new(),
                }
            }
        }
        wanted.retain(|entry| !entry.1.is_empty());
        for (stream_type, chosen) in &wanted {
            if *stream_type == 3 && chosen == "0" {
                continue;
            }
            if !streams_of(*stream_type).contains(chosen) {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let all_parts = query.get("allParts").is_some_and(|value| value == "1");
        if let Some(media_rows) = self.catalog.items[owner_index]
            .get_mut("Media")
            .and_then(Value::as_array_mut)
        {
            for media in media_rows {
                for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                    if part.get("id").map(string_value).as_deref() != Some(part_id) && !all_parts {
                        continue;
                    }
                    for stream in part.get_mut("Stream").and_then(Value::as_array_mut).into_iter().flatten() {
                        let stream_type = stream.get("streamType").and_then(Value::as_i64).unwrap_or(0);
                        let Some((_, chosen)) = wanted.iter().find(|entry| entry.0 == stream_type) else {
                            continue;
                        };
                        let id = stream.get("id").map(string_value).unwrap_or_default();
                        if let Some(object) = stream.as_object_mut() {
                            if &id == chosen {
                                object.insert("selected".to_string(), json!(true));
                            } else {
                                object.remove("selected");
                            }
                        }
                    }
                }
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    fn route_part(
        &mut self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        // The media URL has exactly three path components after `parts`: the
        // part id, the catalog version stamp, and the file name.  The shorter
        // `/library/parts/{id}` form is reserved for stream-selection writes;
        // a write against the actual byte URL is a measured 404.
        if request.method != "GET" && request.method != "HEAD" {
            if matches!(request.method.as_str(), "PUT" | "POST") && segments.len() == 1 {
                return self.select_stream(segments[0], query);
            }
            return Response::error(404, "plexsim: not found");
        }
        // A terminated playback session is refused before the byte URL is
        // resolved.  PMS applies this gate to the complete `/parts/{id}/...`
        // path, so a missing provider link still answers 503 for the next read.
        if let Some(session_id) = query
            .get("X-Plex-Session-Identifier")
            .or_else(|| request.headers.get("x-plex-session-identifier"))
        {
            if self.terminated_sessions.remove(session_id).is_some() {
                return Response::error(503, "plexsim: session terminated");
            }
        }
        // Cycle 52: PMS's route table spells this leaf
        // `/parts/:partID(\d+)/:changestamp(\d+)?/:filename?`, so ONE extra segment is a
        // filename whatever it says, and the digit rule decides the changestamp only when
        // a filename follows it.
        if segments.len() > 3
            || (segments.len() == 3
                && (segments[1].is_empty() || !segments[1].bytes().all(|b| b.is_ascii_digit())))
        {
            return Response::error(404, "plexsim: not found");
        }
        let part_id = segments
            .first()
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(0);
        let mut part = None;
        for row in &self.catalog.items {
            for media in row
                .get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                for candidate in media
                    .get("Part")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if candidate.get("id").and_then(Value::as_i64) == Some(part_id) {
                        let mut selected = candidate.clone();
                        // Catalog normalization keeps the provider link on the
                        // owning item, while measured part fixtures may keep it
                        // directly on the Part object.  The stream route needs
                        // one owned copy with either representation resolved;
                        // never discard the row-level link when projecting it.
                        if selected.get("_debrid").is_none() {
                            if let Some(link) = row.get("_debrid") {
                                if let Some(object) = selected.as_object_mut() {
                                    object.insert("_debrid".to_string(), link.clone());
                                }
                            }
                        }
                        part = Some(selected);
                    }
                }
            }
        }
        let Some(part) = part else {
            return Response::error(404, "plexsim: not found");
        };
        // Cycle 29: `download=` is one of the 0/1 options, anything else a 400.
        if query.get("download").is_some_and(|value| !bool_option(value)) {
            return Response::error(400, "plexsim: bad request");
        }
        let link = part.get("_debrid").and_then(Value::as_str).unwrap_or("").to_string();
        if link.is_empty() {
            return Response::error(404, "plexsim: no such part");
        }
        // Cycle 44: the provider saying the file is gone is a plain 404; any other
        // resolve failure is the 502.
        if let Err(unavailable) = self.resolve_link(&link) {
            return if unavailable {
                Response::error(404, "plexsim: not found")
            } else {
                Response::error(502, "plexsim: debrid resolve failed")
            };
        }
        self.stream_part(&part, request)
    }

    fn stream_part(&mut self, part: &Value, request: &Request) -> Response {
        let size = part.get("size").and_then(Value::as_u64).unwrap_or(0);
        let range = request.headers.get("range").cloned();
        let selected = parse_range(range.as_deref(), size);
        let (status, start, end, content_range) = match selected {
            RangeResult::Unsatisfiable => (416, 0, 0, None),
            RangeResult::Partial(window) => (
                206,
                window.start,
                window.end,
                Some(format!(
                    "bytes {}-{}/{}",
                    window.advertised_start, window.advertised_end, size
                )),
            ),
            RangeResult::Whole => (200, 0, size.saturating_sub(1), None),
        };
        if status == 416 {
            return Response {
                status,
                content_type: "text/html".to_string(),
                body: error_document(416),
                headers: vec![
                    ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                    ("Cache-Control".to_string(), "no-cache".to_string()),
                    ("Date".to_string(), http_date()),
                ],
                close: false,
            };
        }
        let Some(bytes) = self.fetch_part(part, start, end) else {
            return Response {
                status: 404,
                content_type: "text/html".to_string(),
                body: b"<!DOCTYPE html><html><body>404 Not Found</body></html>".to_vec(),
                headers: Vec::new(),
                close: true,
            };
        };
        if request.method != "HEAD" {
            self.bandwidth_bytes = self.bandwidth_bytes.saturating_add(bytes.len() as u64);
        }
        let mut response = Response::bytes(status, part_content_type(part));
        response.body = bytes;
        response
            .headers
            .push(("X-Plex-Protocol".to_string(), "1.0".to_string()));
        let origin = request.headers.get("origin").cloned();
        if origin.is_some() {
            response.headers.push((
                "Access-Control-Expose-Headers".to_string(),
                "X-Plex-Protocol, ETag, Last-Modified, Accept-Ranges, Content-Range, Content-Length, Content-Type".to_string(),
            ));
            response
                .headers
                .push(("Vary".to_string(), "Origin".to_string()));
        }
        let part_id = string_value(part.get("id").unwrap_or(&Value::Null));
        let stamp = string_value(
            part.get("addedAt")
                .or_else(|| part.get("updatedAt"))
                .unwrap_or(&Value::Null),
        );
        let etag = bytes_digest(format!("{part_id}/{stamp}/{size}").as_bytes());
        response
            .headers
            .push(("ETag".to_string(), format!("\"{etag}\"")));
        response
            .headers
            .push(("Last-Modified".to_string(), http_date()));
        response
            .headers
            .push(("Accept-Ranges".to_string(), "bytes".to_string()));
        if let Some(content_range) = content_range {
            response
                .headers
                .push(("Content-Range".to_string(), content_range));
        }
        if let Some(origin) = origin {
            response
                .headers
                .push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
        }
        response.headers.push(("Date".to_string(), http_date()));
        response
    }

    fn fetch_part(&self, part: &Value, start: u64, end: u64) -> Option<Vec<u8>> {
        let link = part.get("_debrid").and_then(Value::as_str).unwrap_or("");
        if link.is_empty() {
            return None;
        }
        let download = self.unrestrict(link)?;
        if let Some((host, path, port)) = parse_http_url(&download) {
            if let Ok(mut stream) = TcpStream::connect((host.as_str(), port)) {
                let request = format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nRange: bytes={start}-{end}\r\nConnection: close\r\n\r\n");
                if stream.write_all(request.as_bytes()).is_err() {
                    return None;
                }
                let mut bytes = Vec::new();
                if stream.read_to_end(&mut bytes).is_err() {
                    return None;
                }
                if let Some(pos) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
                    let status = String::from_utf8_lossy(&bytes[..pos])
                        .lines()
                        .next()
                        .and_then(|line| line.split_whitespace().nth(1))
                        .and_then(|value| value.parse::<u16>().ok())
                        .unwrap_or(0);
                    if (200..300).contains(&status) {
                        return Some(bytes[pos + 4..].to_vec());
                    }
                }
            }
        }
        None
    }

    fn unrestrict(&self, link: &str) -> Option<String> {
        self.resolve_link(link).ok()
    }

    /// Restricted link -> CDN url.  `Err(true)` is Real-Debrid saying the FILE is not
    /// there (a 404 or `unavailable_file`, error code 24) -- which a real PMS answers
    /// with a plain 404 -- and `Err(false)` every other failure, the 502 (cycle 44).
    fn resolve_link(&self, link: &str) -> Result<String, bool> {
        let (host, base_path, port) = parse_http_url(&self.rd_api).ok_or(false)?;
        let result = self.unrestrict_http(link, &host, &base_path, port);
        result
    }

    fn unrestrict_http(&self, link: &str, host: &str, base_path: &str, port: u16) -> Result<String, bool> {
        let host = host.to_string();
        let base = base_path.trim_end_matches('/');
        let path = format!("{base}/unrestrict/link");
        let body = format!("link={}", form_encode(link));
        let mut stream = TcpStream::connect((host.as_str(), port)).map_err(|_| false)?;
        let authorization = if self.rd_token.is_empty() {
            String::new()
        } else {
            format!("Authorization: Bearer {}\r\n", self.rd_token)
        };
        let request = format!(
            "POST {path} HTTP/1.1\r\nHost: {host}\r\n{authorization}Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(request.as_bytes()).map_err(|_| false)?;
        let mut bytes = Vec::new();
        stream.read_to_end(&mut bytes).map_err(|_| false)?;
        let header_end = bytes
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .ok_or(false)?;
        let status = String::from_utf8_lossy(&bytes[..header_end])
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|code| code.parse::<u16>().ok())
            .ok_or(false)?;
        let payload = serde_json::from_slice::<Value>(&bytes[header_end + 4..]).unwrap_or(Value::Null);
        if !(200..300).contains(&status) {
            let unavailable = status == 404
                || payload.get("error_code").and_then(Value::as_i64) == Some(24)
                || payload.get("error").and_then(Value::as_str) == Some("unavailable_file");
            return Err(unavailable);
        }
        payload
            .get("download")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or(false)
    }

    fn route_bundled_resource(&self, relative: &str, head: bool) -> Response {
        let relative = relative.trim_end_matches('/');
        let assets = load_json_sidecar(&self.catalog_path, "resource_assets.json");
        let Some(assets) = assets.as_ref().and_then(Value::as_object) else {
            return Response::error(404, "plexsim: not found");
        };
        if relative.is_empty()
            || assets
                .keys()
                .any(|name| name.starts_with(&format!("{relative}/")))
        {
            return Response {
                status: 200,
                content_type: "application/octet-stream".to_string(),
                body: Vec::new(),
                headers: Vec::new(),
                close: false,
            };
        }
        let Some(asset) = assets.get(relative).and_then(Value::as_object) else {
            return Response::error(404, "plexsim: not found");
        };
        let content_type = asset
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("application/octet-stream")
            .to_string();
        let body = if let Some(encoded) = asset.get("body").and_then(Value::as_str) {
            base64_decode_bytes(encoded).unwrap_or_default()
        } else {
            let width = asset.get("width").and_then(Value::as_u64).unwrap_or(1) as u32;
            let height = asset.get("height").and_then(Value::as_u64).unwrap_or(1) as u32;
            match content_type.as_str() {
                "image/png" => generated_png(width, height, [32, 34, 38]),
                "image/jpeg" => generated_jpeg(width, height),
                _ => Vec::new(),
            }
        };
        Response {
            status: 200,
            content_type,
            body: { let _ = head; body },
            headers: vec![
                ("Cache-Control".to_string(), "no-cache".to_string()),
                ("Accept-Ranges".to_string(), "bytes".to_string()),
            ],
            close: false,
        }
    }

    fn route_web(&self, segments: &[&str], head: bool, trailing_slash: bool) -> Response {
        let Some(root) = &self.web_root else {
            return Response::error(404, "plexsim: not found");
        };
        let relative = if segments.is_empty() {
            PathBuf::from("index.html")
        } else {
            segments.iter().collect()
        };
        let path = root.join(relative);
        let Ok(canonical_root) = fs::canonicalize(root) else {
            return Response::error(404, "plexsim: not found");
        };
        let Ok(canonical) = fs::canonicalize(&path) else {
            return Response::error(404, "plexsim: not found");
        };
        if !canonical.starts_with(&canonical_root) {
            return Response::error(404, "plexsim: not found");
        }
        let mut response = if canonical.is_dir() {
            // Cycle 29: a DIRECTORY is an empty `application/octet-stream` 200 on both
            // references, never the index page.
            Response::bytes(200, "application/octet-stream")
        } else if trailing_slash && !segments.is_empty() {
            // Cycle 29: a trailing slash after a FILE is a 404.
            return Response::error(404, "plexsim: not found");
        } else {
            let Ok(body) = fs::read(&canonical) else {
                return Response::error(404, "plexsim: not found");
            };
            let mut response = Response::bytes(200, content_type(path.to_string_lossy().as_ref()));
            // The envelope drops a HEAD's body and keeps its length.
            let _ = head;
            response.body = body;
            response
        };
        response
            .headers
            .push(("Cache-Control".to_string(), "no-cache".to_string()));
        response
            .headers
            .push(("Accept-Ranges".to_string(), "bytes".to_string()));
        response
    }
}

#[derive(Debug, Clone)]
pub struct Request {
    pub method: String,
    pub target: String,
    pub version: String,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
    /// A GET may be dispatched as soon as its header arrives, even when its
    /// declared entity bytes have not.  The connection must close after that
    /// response instead of waiting forever for a body the caller never sends.
    body_complete: bool,
}

#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
    pub headers: Vec<(String, String)>,
    pub close: bool,
}

impl Response {
    fn json(status: u16, value: Value) -> Self {
        let body = upper_json_escapes(serde_json::to_vec(&drop_empty_value(&value)).unwrap_or_default());
        Self {
            status,
            content_type: "application/json".to_string(),
            body,
            headers: vec![("Cache-Control".to_string(), "no-cache".to_string())],
            close: false,
        }
    }
    fn bytes(status: u16, content_type: impl Into<String>) -> Self {
        Self {
            status,
            content_type: content_type.into(),
            body: Vec::new(),
            headers: Vec::new(),
            close: false,
        }
    }
    fn plain(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            content_type: "text/plain".to_string(),
            body,
            headers: Vec::new(),
            close: false,
        }
    }
    fn image(body: Vec<u8>) -> Self {
        Self {
            status: 200,
            content_type: "image/jpeg".to_string(),
            body,
            headers: Vec::new(),
            close: false,
        }
    }
    fn photo_error(status: u16, message: &str) -> Self {
        let _ = message;
        Self {
            status,
            content_type: "text/html".to_string(),
            body: error_document(status),
            headers: vec![
                ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                ("Cache-Control".to_string(), "no-cache".to_string()),
                ("Date".to_string(), http_date()),
            ],
            close: false,
        }
    }
    fn empty(status: u16) -> Self {
        Self {
            status,
            // Plex labels successful mutation responses as JSON even when
            // the entity is empty.  The wire writer places this type after
            // Content-Length for that measured empty-body shape.
            content_type: "application/json".to_string(),
            body: Vec::new(),
            headers: Vec::new(),
            close: false,
        }
    }
    fn error(status: u16, message: &str) -> Self {
        // Cycle 36, measured on kids and zen: every refusal on the surface answers the
        // reference's own `text/html` document (85 bytes for a 404, 89 for a 400), never
        // a plexsim sentence. The sentence is kept for the log only.
        let _ = message;
        Self {
            status,
            content_type: "text/html".to_string(),
            body: error_document(status),
            headers: Vec::new(),
            close: false,
        }
    }
    fn unauthorized() -> Self {
        let mut response = Self {
            status: 401,
            content_type: "text/html".to_string(),
            body: b"<!DOCTYPE html><html><body>401 Unauthorized</body></html>".to_vec(),
            headers: Vec::new(),
            close: false,
        };
        response
            .headers
            .push(("Cache-Control".to_string(), "no-cache".to_string()));
        response
    }
    fn sse() -> Self {
        let mut response = Self {
            status: 200,
            content_type: "text/event-stream".to_string(),
            body: b"\n".to_vec(),
            headers: Vec::new(),
            close: false,
        };
        response
            .headers
            .push(("Cache-Control".to_string(), "no-cache".to_string()));
        response
    }
}

/// Cycle 32: a real PMS writes a C0 control as `\u00XX` in UPPER-case hex, where
/// serde_json writes lower-case; every other byte of the compact form already matches.
fn upper_json_escapes(mut body: Vec<u8>) -> Vec<u8> {
    let mut index = 0;
    while index + 1 < body.len() {
        if body[index] == b'\\' {
            if body[index + 1] == b'u' && index + 5 < body.len() {
                for byte in &mut body[index + 2..index + 6] {
                    byte.make_ascii_uppercase();
                }
                index += 6;
            } else {
                index += 2;
            }
        } else {
            index += 1;
        }
    }
    body
}

/// The reference's refusal document, Python's `http.HTTPStatus(status).phrase` spelling.
fn error_document(status: u16) -> Vec<u8> {
    let reason = match status {
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        406 => "Not Acceptable",
        409 => "Conflict",
        412 => "Precondition Failed",
        413 => "Request Entity Too Large",
        416 => "Requested Range Not Satisfiable",
        422 => "Unprocessable Entity",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        _ => "",
    };
    format!("<html><head><title>{reason}</title></head><body><h1>{status} {reason}</h1></body></html>")
        .into_bytes()
}

/// The host's own name, which a real PMS shows when `FriendlyName` is written empty.
fn local_host_name() -> String {
    let mut buffer = [0_u8; 256];
    // SAFETY: the buffer is valid for its whole length and gethostname NUL-terminates
    // within it on success.
    let status = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if status != 0 {
        return "plexsim".to_string();
    }
    let end = buffer.iter().position(|byte| *byte == 0).unwrap_or(buffer.len());
    String::from_utf8_lossy(&buffer[..end]).into_owned()
}

fn drop_empty_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .filter(|(_, item)| !item.is_null() && !item.as_array().is_some_and(Vec::is_empty))
                .map(|(key, item)| (key.clone(), drop_empty_value(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(drop_empty_value).collect()),
        _ => value.clone(),
    }
}

fn load_tls_config(cert_path: &Path, key_path: Option<&Path>) -> Result<ServerConfig, String> {
    let cert_file = fs::File::open(cert_path).map_err(|error| {
        format!(
            "cannot open TLS certificate {}: {error}",
            cert_path.display()
        )
    })?;
    let mut cert_reader = BufReader::new(cert_file);
    let certificates = certs(&mut cert_reader)
        .collect::<Result<Vec<CertificateDer<'static>>, _>>()
        .map_err(|error| format!("cannot parse TLS certificate: {error}"))?;
    if certificates.is_empty() {
        return Err(format!(
            "TLS certificate {} contains no certificates",
            cert_path.display()
        ));
    }

    let key_bytes = if let Some(key_path) = key_path {
        fs::read(key_path)
            .map_err(|error| format!("cannot read TLS key {}: {error}", key_path.display()))?
    } else {
        fs::read(cert_path).map_err(|error| {
            format!(
                "cannot read TLS certificate {}: {error}",
                cert_path.display()
            )
        })?
    };
    let mut key_reader = BufReader::new(key_bytes.as_slice());
    let key = pkcs8_private_keys(&mut key_reader)
        .next()
        .transpose()
        .map_err(|error| format!("cannot parse PKCS#8 TLS key: {error}"))?
        .map(PrivatePkcs8KeyDer::from)
        .map(PrivateKeyDer::Pkcs8)
        .or_else(|| {
            let mut key_reader = BufReader::new(key_bytes.as_slice());
            let parsed = rsa_private_keys(&mut key_reader)
                .next()
                .transpose()
                .ok()
                .flatten()
                .map(PrivatePkcs1KeyDer::from)
                .map(PrivateKeyDer::Pkcs1);
            parsed
        })
        .ok_or_else(|| "TLS key file contains no PKCS#8 or RSA private key".to_string())?;

    let mut config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certificates, key)
        .map_err(|error| format!("cannot build TLS server configuration: {error}"))?;
    // PMS deliberately does not negotiate HTTP/2, even when a client offers
    // ALPN.  An empty list retains that measured no-ALPN result.
    config.alpn_protocols.clear();
    Ok(config)
}

fn start_gdm(runtime: SharedRuntime, mode: &str) {
    if matches!(
        mode.to_ascii_lowercase().as_str(),
        "off" | "false" | "0" | "no"
    ) {
        return;
    }
    let ports = configured_gdm_ports();
    for port in &ports {
        let Ok(socket) = UdpSocket::bind(("0.0.0.0", *port)) else {
            continue;
        };
        let _ = socket.set_read_timeout(Some(std::time::Duration::from_millis(500)));
        let _ = socket.join_multicast_v4(
            &GDM_GROUP
                .parse()
                .unwrap_or(std::net::Ipv4Addr::new(239, 0, 0, 250)),
            &std::net::Ipv4Addr::UNSPECIFIED,
        );
        let shared = Arc::clone(&runtime);
        thread::spawn(move || {
            let mut packet = [0_u8; 4096];
            loop {
                match socket.recv_from(&mut packet) {
                    Ok((length, address)) => {
                        if packet[..length]
                            .iter()
                            .zip(GDM_QUERY.iter())
                            .take(length.min(GDM_QUERY.len()))
                            .all(|(got, wanted)| got == wanted)
                        {
                            let response = shared.lock().ok().map(|runtime| runtime.gdm_response());
                            if let Some(response) = response {
                                let _ = socket.send_to(&response, address);
                            }
                        } else if packet[..length].starts_with(b"HTTP/1.0 200") {
                            if let Ok(mut runtime) = shared.lock() {
                                runtime.gdm_record(&packet[..length], &address.ip().to_string());
                            }
                        }
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                        ) => {}
                    Err(_) => break,
                }
            }
        });
    }

    let search_runtime = Arc::clone(&runtime);
    thread::spawn(move || {
        let Ok(socket) = UdpSocket::bind("0.0.0.0:0") else {
            return;
        };
        let _ = socket.set_multicast_ttl_v4(2);
        let _ = socket.set_read_timeout(Some(std::time::Duration::from_millis(500)));
        let group = match GDM_GROUP.parse::<std::net::Ipv4Addr>() {
            Ok(group) => group,
            Err(_) => return,
        };
        loop {
            let _ = socket.send_to(GDM_SEARCH, (group, ports.last().copied().unwrap_or(32414)));
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            let mut packet = [0_u8; 4096];
            while std::time::Instant::now() < deadline {
                match socket.recv_from(&mut packet) {
                    Ok((length, address)) => {
                        if let Ok(mut runtime) = search_runtime.lock() {
                            runtime.gdm_record(&packet[..length], &address.ip().to_string());
                        }
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                        ) => {}
                    Err(_) => return,
                }
            }
        }
    });
}

pub fn serve(options: RuntimeOptions) -> Result<(), String> {
    let host = options.host.clone();
    let port = options.port;
    let gdm_mode = options.gdm.clone();
    let tls_config = match options.tls_cert.as_deref() {
        Some(cert_path) => Some(Arc::new(load_tls_config(
            cert_path,
            options.tls_key.as_deref(),
        )?)),
        None => None,
    };
    let runtime = Arc::new(Mutex::new(Runtime::load(options)?));
    start_gdm(Arc::clone(&runtime), &gdm_mode);
    let listener = TcpListener::bind((host.as_str(), port))
        .map_err(|e| format!("cannot bind {host}:{port}: {e}"))?;
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let shared = Arc::clone(&runtime);
                let tls_config = tls_config.clone();
                // PMS drops an incomplete request twenty seconds after its
                // first byte.  The timeout is per socket read, so the
                // already received prefix starts that measured window.
                let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(20)));
                let peer = stream
                    .peer_addr()
                    .map(|address| address.to_string())
                    .unwrap_or_else(|_| "???".to_string());
                thread::spawn(move || {
                    let _live = LiveConnectionGuard::register(peer);
                    if let Some(config) = tls_config {
                        // A TLS ClientHello is identified by the content-type
                        // byte.  Any other first byte remains the plain HTTP
                        // path on this same listening port.
                        let mut byte = [0_u8; 1];
                        let first = stream
                            .peek(&mut byte)
                            .ok()
                            .filter(|count| *count == 1)
                            .map(|_| byte[0]);
                        if first == Some(0x16) {
                            match ServerConnection::new(config) {
                                Ok(connection) => {
                                    let mut tls = StreamOwned::new(connection, stream);
                                    let _ = handle_connection(&mut tls, shared);
                                }
                                Err(_) => {}
                            }
                        } else {
                            serve_plain(stream, shared);
                        }
                    } else {
                        serve_plain(stream, shared);
                    }
                });
            }
            Err(error) => eprintln!("plexsim accept: {error}"),
        }
    }
    Ok(())
}

/// Cycle 52: `/connections`' own bookkeeping -- one row per live socket, in slot order.
struct LiveConnection {
    slot: u64,
    peer: String,
    request: String,
    number: u64,
    agent: String,
}

static LIVE_CONNECTIONS: Mutex<Vec<LiveConnection>> = Mutex::new(Vec::new());
static NEXT_CONNECTION_SLOT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
static REQUEST_SERIAL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

thread_local! {
    static CONNECTION_SLOT: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

struct LiveConnectionGuard(u64);

impl LiveConnectionGuard {
    fn register(peer: String) -> Self {
        let slot = NEXT_CONNECTION_SLOT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        CONNECTION_SLOT.with(|cell| cell.set(slot));
        if let Ok(mut rows) = LIVE_CONNECTIONS.lock() {
            rows.push(LiveConnection {
                slot,
                peer,
                request: String::new(),
                number: 0,
                agent: String::new(),
            });
        }
        Self(slot)
    }
}

impl Drop for LiveConnectionGuard {
    fn drop(&mut self) {
        if let Ok(mut rows) = LIVE_CONNECTIONS.lock() {
            rows.retain(|row| row.slot != self.0);
        }
    }
}

/// Record the request a live connection is routing, as `_dispatch` does before it answers.
fn note_live_request(request: &Request) {
    let slot = CONNECTION_SLOT.with(|cell| cell.get());
    let number = REQUEST_SERIAL.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
    // The client names itself with `X-Plex-Device-Name, X-Plex-Product` when it sends
    // them and with its `User-Agent` when it does not -- measured on both references.
    let named = ["x-plex-device-name", "x-plex-product"]
        .iter()
        .filter_map(|name| request.headers.get(*name).filter(|value| !value.is_empty()))
        .cloned()
        .collect::<Vec<_>>();
    let agent = if named.is_empty() {
        request.headers.get("user-agent").cloned().unwrap_or_default()
    } else {
        named.join(", ")
    };
    if let Ok(mut rows) = LIVE_CONNECTIONS.lock() {
        if let Some(row) = rows.iter_mut().find(|row| row.slot == slot) {
            row.request = format!("{} {}", request.method, request.target);
            row.number = number;
            row.agent = agent;
        }
    }
}

/// `/connections` -- plexsim's own live connections, in PMS's line grammar.
fn connection_lines(owner: &str) -> String {
    let Ok(rows) = LIVE_CONNECTIONS.lock() else {
        return String::new();
    };
    let mut lines = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        if row.request.is_empty() {
            lines.push(format!("{} {} - Uninitialized", index + 1, row.peer));
            continue;
        }
        let label = if owner.is_empty() {
            String::new()
        } else {
            format!(" - Token ({owner})")
        };
        let agent = if row.agent.is_empty() {
            String::new()
        } else {
            format!(" ({})", row.agent)
        };
        lines.push(format!(
            "{} {} - Routing - {} #{:x}{label}{agent}",
            index + 1,
            row.peer,
            row.request,
            row.number
        ));
    }
    if lines.is_empty() {
        String::new()
    } else {
        lines.join("\n") + "\n"
    }
}

fn handle_connection<S: Read + Write>(
    mut stream: &mut S,
    runtime: SharedRuntime,
) -> Result<(), String> {
    let mut pending = Vec::new();
    let mut served_any = false;
    loop {
        let request = match read_request(&mut stream, &mut pending) {
            Ok(Some(request)) => request,
            Ok(None) => return Ok(()),
            Err(error) if matches!(error.as_str(), "silence" | "request deadline") => return Ok(()),
            Err(error) => {
                // The parser has already received the complete envelope for
                // the bounded-header cases.  Waiting for the peer to close
                // before answering would deadlock a client that is waiting
                // for the measured 400 while keeping its write side open.
                let status = if error == "unknown method" {
                    500
                } else if error == "negative content length" {
                    500
                } else {
                    400
                };
                let mut response = Response::error(
                    status,
                    if status == 500 {
                        "plexsim: internal error"
                    } else {
                        "plexsim: bad request"
                    },
                );
                response.close = true;
                write_response(&mut stream, &response, false)?;
                return Ok(());
            }
        };
        if !known_method(&request.method) {
            if request
                .method
                .as_bytes()
                .first()
                .is_some_and(|byte| byte.is_ascii_lowercase())
            {
                // PMS leaves an unknown lower-case method unanswered while
                // keeping a fresh connection available.  Once a valid request
                // has already completed, the same token is a framework 500
                // and the keep-alive connection remains usable.
                if !served_any {
                    continue;
                }
                let response = Response::error(500, "plexsim: internal error");
                write_response(&mut stream, &response, true)?;
                continue;
            }
            let mut response = Response::error(500, "plexsim: internal error");
            response.close = false;
            write_response(&mut stream, &response, true)?;
            continue;
        }
        note_live_request(&request);
        let close_after_body = !request.body_complete;
        let invalid_absolute_target = if request.target.starts_with("http:") {
            if !request.target.starts_with("http://") {
                true
            } else {
                request.target[7..].find('/').is_none()
            }
        } else {
            // A literal URI such as `server://...` is valid inside a query
            // value sent by the Plex client.  Only an absolute-form target
            // whose scheme starts the request line belongs to this envelope
            // check; searching the whole target rejects those playlist and
            // queue requests before their route sees the query.
            !request.target.starts_with('/')
                && request
                    .target
                    .find("://")
                    .is_some_and(|scheme_end| &request.target[..scheme_end] != "http")
        };
        let (request_path, request_query) = split_target(&request.target);
        let path_segments = request_path.split('/').collect::<Vec<_>>();
        let invalid_dot_path = path_segments.iter().enumerate().any(|(index, segment)| {
            segment.len() >= 2
                && segment.bytes().all(|byte| byte == b'.')
                && index + 1 < path_segments.len()
        });
        let query_pairs = parse_query(&request_query);
        let malformed_query = !request_query.is_empty()
            && request_query
                .split('&')
                .any(|part| part.is_empty() || !part.contains('='));
        let mut query = query_pairs
            .iter()
            .fold(HashMap::new(), |mut map, (key, value)| {
                map.insert(key.clone(), value.clone());
                map
            });
        // Plex accepts the container window in either the query string or the
        // two X-Plex headers.  Keep the canonical query names here so every
        // route shares the same measured paging implementation.
        for (header, query_name) in [
            ("x-plex-container-start", "X-Plex-Container-Start"),
            ("x-plex-container-size", "X-Plex-Container-Size"),
        ] {
            if !query.contains_key(query_name) {
                if let Some(value) = request.headers.get(header) {
                    query.insert(query_name.to_string(), value.clone());
                }
            }
        }
        if request_path == "/:/websockets/notifications"
            && request
                .headers
                .get("upgrade")
                .is_some_and(|value| value.eq_ignore_ascii_case("websocket"))
        {
            let event_start = {
                let mut guard = runtime
                    .lock()
                    .map_err(|_| "runtime lock poisoned".to_string())?;
                if !guard.authorized(&request_path, &request, &query) {
                    let response = Response::unauthorized();
                    write_response(&mut stream, &response, false)?;
                    return Ok(());
                }
                guard.mark_device(&request.headers);
                guard.event_cursor()
            };
            return handle_websocket(&mut stream, &runtime, &request, event_start);
        }
        if request_path == "/:/eventsource/notifications" && request.method == "GET" {
            let event_start = {
                let mut guard = runtime
                    .lock()
                    .map_err(|_| "runtime lock poisoned".to_string())?;
                if !guard.authorized(&request_path, &request, &query) {
                    let response = Response::unauthorized();
                    write_response(&mut stream, &response, false)?;
                    return Ok(());
                }
                guard.mark_device(&request.headers);
                guard.event_cursor()
            };
            return handle_eventsource(&mut stream, &runtime, &request, event_start);
        }
        let pipeline_disabled = request
            .headers
            .get("x-plex-http-pipeline")
            .map(|value| value == "disable")
            .unwrap_or_else(|| {
                query
                    .get("X-Plex-Http-Pipeline")
                    .is_some_and(|value| value == "disable")
            });
        let keep = request.version == "HTTP/1.1"
            && request
                .headers
                .get("connection")
                .map(|v| !v.eq_ignore_ascii_case("close"))
                .unwrap_or(true)
            && !pipeline_disabled;
        let empty_container_size = query
            .get("X-Plex-Container-Size")
            .is_some_and(String::is_empty);
        let mut response = if invalid_absolute_target || invalid_dot_path {
            Response::error(400, "plexsim: bad request")
        } else if malformed_query {
            Response::error(400, "plexsim: bad request")
        } else if empty_container_size {
            Response::error(500, "plexsim: bad request")
        } else {
            runtime
                .lock()
                .map_err(|_| "runtime lock poisoned".to_string())?
                .route(&request)
        };
        // Cycle 30/49: a refusal of the request's ENVELOPE closes the socket -- the
        // malformed query pair among them -- while a refusal of its MEANING keeps it.
        if invalid_absolute_target
            || invalid_dot_path
            || (malformed_query && response.status == 400)
            || (response.status == 404
                && (request_path.starts_with("/:/resources/") || request_path.starts_with("/web/")))
        {
            response.close = true;
        }
        if close_after_body {
            response.close = true;
        }
        // Cycle 45: `X-Plex-Container-Focus-Key` (header or query) supplies the START as
        // `max(0, index - (size - 1) // 2)` when it names `/library/metadata/...`, a Size
        // is given and no explicit Start is; a key naming no row starts at 0.
        let focus = request
            .headers
            .get("x-plex-container-focus-key")
            .or_else(|| query.get("X-Plex-Container-Focus-Key"))
            .cloned();
        if let Some(focus) = focus.filter(|focus| focus.starts_with("/library/metadata/")) {
            if query.contains_key("X-Plex-Container-Size") && !query.contains_key("X-Plex-Container-Start") {
                let wanted = focus.trim_end_matches('/').rsplit('/').next().unwrap_or("").to_string();
                let index = serde_json::from_slice::<Value>(&response.body)
                    .ok()
                    .and_then(|document| {
                        let body = document.get("MediaContainer")?.as_object()?.clone();
                        let longest = body
                            .values()
                            .filter_map(Value::as_array)
                            .max_by_key(|rows| rows.len())
                            .cloned()
                            .unwrap_or_default();
                        longest.iter().position(|row| key_of(row).as_deref() == Some(wanted.as_str()))
                    })
                    .unwrap_or(0) as i64;
                let size = query
                    .get("X-Plex-Container-Size")
                    .and_then(|value| value.trim().parse::<i64>().ok())
                    .unwrap_or(0);
                query.insert(
                    "X-Plex-Container-Start".to_string(),
                    (index - (size - 1).div_euclid(2)).max(0).to_string(),
                );
            }
        }
        wire::exclude_container(&mut response, &query, &request_path);
        page_container(&mut response, &query, &request_path);
        add_paging_headers(&mut response, &query);
        response = xml_or_json(response, request.headers.get("accept").map(String::as_str));
        order_json_body(&mut response, &request_path);
        if response.content_type.starts_with("application/json") {
            // The post-passes re-serialise; keep the upper-case control escapes
            // (cycle 32) on the bytes that go out.
            response.body = upper_json_escapes(std::mem::take(&mut response.body));
        }
        let envelope = wire::Wire::of(&request, &request_path, &query);
        let still_open = wire::write_enveloped(&mut stream, &response, &envelope, keep)?;
        served_any = true;
        if response.close || !keep || !still_open {
            break;
        }
    }
    Ok(())
}

/// Serve one plain connection, then close it the way Python's socketserver does:
/// half-close the write side and drain what the peer still sends. Closing outright
/// with a request in flight answers it with a RST, which destroys the refusal the
/// client has not yet read (cycle 30's closing malformed-pair 400 among them).
fn serve_plain(mut stream: TcpStream, shared: SharedRuntime) {
    let _ = handle_connection(&mut stream, shared);
    let _ = stream.shutdown(std::net::Shutdown::Write);
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(2)));
    let mut sink = [0_u8; 4096];
    let started = std::time::Instant::now();
    while started.elapsed() < std::time::Duration::from_secs(2) {
        match stream.read(&mut sink) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
    }
}

fn handle_websocket<S: Write>(
    stream: &mut S,
    runtime: &SharedRuntime,
    request: &Request,
    event_start: usize,
) -> Result<(), String> {
    let Some(key) = request.headers.get("sec-websocket-key") else {
        let response = Response::error(400, "plexsim: bad request");
        write_response(stream, &response, false)?;
        return Ok(());
    };
    let mut digest = Sha1::new();
    digest.update(key.as_bytes());
    digest.update(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    let accept = base64_encode(&digest.finalize());
    // Cycle 30: the 101 carries `Date` last like every other response; cycle 34: the
    // reference's ORDER, read off a client-session capture -- the CORS pair leads, the
    // handshake follows, then `Cache-Control`, the allowed origin and `Date`.
    let origin = request.headers.get("origin");
    let mut header = String::from("HTTP/1.1 101 Switching Protocols\r\nX-Plex-Protocol: 1.0\r\n");
    if origin.is_some() {
        header.push_str("Access-Control-Expose-Headers: Location, Date\r\nVary: Origin, X-Plex-Token\r\n");
    }
    header.push_str(&format!(
        "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\nCache-Control: no-cache\r\n"
    ));
    if let Some(origin) = origin {
        header.push_str(&format!("Access-Control-Allow-Origin: {origin}\r\n"));
    }
    header.push_str(&format!("Date: {}\r\n\r\n", http_date()));
    stream
        .write_all(header.as_bytes())
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;
    // The client starts its trigger only after it has received the upgrade
    // response.  Give that trigger a full scheduling window before taking the
    // retained event snapshot; a fixed sub-second sleep races on a busy host.
    //
    // The channel then STREAMS, as the reference's does: a snapshot taken at a
    // fixed moment lost every frame of a trigger that took longer than the
    // window on a loaded host, and a real PMS keeps the socket open.  The
    // loop ends when the peer has gone (a write fails) or after a bound.
    let (_, raw_query) = split_target(&request.target);
    let pairs = parse_query(&raw_query);
    // Measured: `filters=` is an exact comma-separated whitelist of type
    // names, and `log` is OPT-IN -- no `filters` delivers everything except
    // log frames, while an EMPTY `filters=` delivers everything including them.
    let filter_value = pairs
        .iter()
        .rev()
        .find(|(key, _)| key == "filters")
        .map(|(_, value)| value.clone());
    let filters = filter_value
        .as_deref()
        .unwrap_or("")
        .split(',')
        .filter(|name| !name.is_empty())
        .map(str::to_string)
        .collect::<HashSet<_>>();
    let everything = filters.is_empty() && filter_value.is_some();
    let started = std::time::Instant::now();
    let mut cursor = event_start;
    let mut last_write = std::time::Instant::now();
    while started.elapsed() < std::time::Duration::from_secs(120) {
        let (events, next) = {
            let guard = runtime
                .lock()
                .map_err(|_| "runtime lock poisoned".to_string())?;
            (guard.events_since(cursor), guard.event_cursor())
        };
        cursor = next;
        for (kind, value) in events {
            let wanted = if filters.is_empty() {
                everything || kind != "log"
            } else {
                filters.contains(&kind)
            };
            if !wanted {
                continue;
            }
            let mut container = Map::new();
            container.insert("type".to_string(), json!(kind));
            if let Value::Object(object) = value {
                for (key, value) in object {
                    if key != "NotificationContainer" {
                        let value = if matches!(
                            key.as_str(),
                            "ActivityNotification"
                                | "TimelineEntry"
                                | "StatusNotification"
                                | "ProgressNotification"
                                | "PlaySessionStateNotification"
                        ) && !value.is_array()
                        {
                            Value::Array(vec![value])
                        } else {
                            value
                        };
                        let size = value.as_array().map(Vec::len).unwrap_or(1);
                        container.insert("size".to_string(), json!(size));
                        container.insert(key, value);
                    }
                }
            }
            let payload = serde_json::to_vec(&json!({"NotificationContainer": container}))
                .map_err(|error| error.to_string())?;
            write_websocket_frame(stream, &payload)?;
            last_write = std::time::Instant::now();
        }
        if last_write.elapsed() > std::time::Duration::from_secs(5) {
            // A ping on an idle channel is how a gone peer is noticed: the
            // write fails and the loop ends.  Clients ignore control frames.
            stream
                .write_all(&[0x89, 0x00])
                .and_then(|_| stream.flush())
                .map_err(|error| error.to_string())?;
            last_write = std::time::Instant::now();
        }
        thread::sleep(std::time::Duration::from_millis(100));
    }
    stream
        .write_all(&[0x88, 0x00])
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())
}

fn handle_eventsource<S: Write>(
    stream: &mut S,
    runtime: &SharedRuntime,
    request: &Request,
    event_start: usize,
) -> Result<(), String> {
    let origin = request.headers.get("origin");
    let mut header = String::from("HTTP/1.1 200 OK\r\nX-Plex-Protocol: 1.0\r\n");
    if origin.is_some() {
        header.push_str("Access-Control-Expose-Headers: X-Plex-Protocol, ETag, Last-Modified, Accept-Ranges, Content-Range, Content-Length, Content-Type\r\nVary: Origin\r\n");
    }
    header.push_str(
        "Content-Type: text/event-stream\r\nConnection: close\r\nCache-Control: no-cache\r\n",
    );
    if let Some(origin) = origin {
        header.push_str(&format!("Access-Control-Allow-Origin: {origin}\r\n"));
    }
    header.push_str("Date: Thu, 01 Jan 1970 00:00:00 GMT\r\n\r\n");
    stream
        .write_all(header.as_bytes())
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;
    // Give the triggering request a short window to append its events.  The
    // reference sends real notifications as soon as they exist and reserves
    // the ten-second delay for the initial keepalive ping; sleeping before
    // reading the event log loses every short-lived client request.
    // The client starts its trigger only after the SSE headers arrive.  The
    // extra window keeps the event snapshot deterministic without changing the
    // measured ten-second quiet-channel ping below.
    thread::sleep(std::time::Duration::from_millis(1500));
    let (_, raw_query) = split_target(&request.target);
    let filters = parse_query(&raw_query)
        .into_iter()
        .find(|(key, _)| key == "filters")
        .map(|(_, value)| value);
    let events = runtime
        .lock()
        .map_err(|_| "runtime lock poisoned".to_string())?
        .events_since(event_start);
    for (kind, value) in events {
        if filters.as_deref() == Some("all")
            || (filters.as_deref() == Some("log") && kind != "log")
            || (filters
                .as_deref()
                .map(|value| !value.is_empty() && value != "log")
                .unwrap_or(true)
                && kind == "log")
        {
            continue;
        }
        // The SSE shape is ONE row per `data:` line under its element name, with no
        // container and no list around it (measured); an event holding several rows
        // is several frames.
        let mut frames = Vec::new();
        match &value {
            Value::Object(object) if object.len() == 1 && object.values().all(Value::is_array) => {
                for (element, rows) in object {
                    for row in rows.as_array().into_iter().flatten() {
                        frames.push(json!({ element.clone(): row.clone() }));
                    }
                }
            }
            _ => frames.push(value.clone()),
        }
        for frame_value in frames {
            let payload = serde_json::to_string(&frame_value).map_err(|error| error.to_string())?;
            let frame = format!("event: {kind}\ndata: {payload}\n\n");
            stream
                .write_all(frame.as_bytes())
                .map_err(|error| error.to_string())?;
        }
    }
    stream.flush().map_err(|error| error.to_string())?;
    // Keep the channel open until the measured ten-second keepalive.  A quiet
    // short read therefore sees headers but no ping, while a long-lived
    // client receives the same initial ping as PMS.
    thread::sleep(
        std::time::Duration::from_secs(10).saturating_sub(std::time::Duration::from_millis(1500)),
    );
    stream
        .write_all(b"event: ping\ndata: {}\n\n")
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())
}

fn write_websocket_frame<S: Write>(stream: &mut S, payload: &[u8]) -> Result<(), String> {
    let mut frame = Vec::with_capacity(payload.len() + 10);
    frame.push(0x81);
    match payload.len() {
        0..=125 => frame.push(payload.len() as u8),
        126..=65535 => {
            frame.push(126);
            frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
        }
        length => {
            frame.push(127);
            frame.extend_from_slice(&(length as u64).to_be_bytes());
        }
    }
    frame.extend_from_slice(payload);
    stream
        .write_all(&frame)
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())
}

fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    let mut index = 0;
    while index < bytes.len() {
        let first = bytes[index];
        let second = bytes.get(index + 1).copied();
        let third = bytes.get(index + 2).copied();
        output.push(TABLE[(first >> 2) as usize] as char);
        output.push(TABLE[(((first & 0x03) << 4) | second.unwrap_or(0) >> 4) as usize] as char);
        output.push(match second {
            Some(second) => {
                TABLE[(((second & 0x0f) << 2) | third.unwrap_or(0) >> 6) as usize] as char
            }
            None => '=',
        });
        output.push(match third {
            Some(third) => TABLE[(third & 0x3f) as usize] as char,
            None => '=',
        });
        index += 3;
    }
    output
}

fn maybe_compress(response: &mut Response, accept_encoding: Option<&String>) {
    // PMS uses the literal lower-case token as a feature switch.  It does not
    // negotiate case-insensitively and it still compresses a body when the
    // resulting gzip is a few bytes larger (both details are observable on
    // small plug-in containers).  `q=0` is likewise left untouched because
    // the measured implementation only searches for the token.
    let compressible = response.content_type.starts_with("application/")
        || response.content_type.starts_with("text/xml")
        || (response.content_type == "text/html" && response.status != 200);
    let offered = accept_encoding.is_some_and(|value| {
        value.split(',').any(|token| {
            token
                .trim()
                .split(';')
                .next()
                .is_some_and(|encoding| encoding == "gzip")
        })
    });
    // A body path -- a bundled asset, artwork, a `/web` file -- names a range unit and
    // is never compressed, measured on kids and zen.
    let ranged = response.headers.iter().any(|(name, _)| name == "Accept-Ranges");
    if response.body.is_empty() || !compressible || !offered || ranged {
        return;
    }
    let original = response.body.len();
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    if encoder.write_all(&response.body).is_err() {
        return;
    }
    let Ok(compressed) = encoder.finish() else {
        return;
    };
    response.body = compressed;
    response
        .headers
        .push(("Content-Encoding".to_string(), "gzip".to_string()));
    response.headers.push((
        "X-Plex-Content-Original-Length".to_string(),
        original.to_string(),
    ));
    response.headers.push((
        "X-Plex-Content-Compressed-Length".to_string(),
        response.body.len().to_string(),
    ));
}

fn read_request<S: Read>(stream: &mut S, pending: &mut Vec<u8>) -> Result<Option<Request>, String> {
    let mut first_byte_at = (!pending.is_empty()).then(std::time::Instant::now);
    let header_end = loop {
        if let Some(pos) = pending.windows(4).position(|w| w == b"\r\n\r\n") {
            break Some(pos + 4);
        }
        // The method token is bounded before the parser needs a delimiter.
        // This is observable on a raw socket: the eleventh byte earns a 400,
        // while a shorter unterminated token remains an idle connection.
        if let Some(delimiter) = pending
            .iter()
            .position(|byte| matches!(*byte, b' ' | b'\t' | b'\r' | b'\n'))
        {
            if delimiter == 0 {
                return Err("silence".to_string());
            }
            if delimiter > 10 {
                return Err("method length".to_string());
            }
        } else if pending.len() > 10 {
            return Err("method length".to_string());
        }
        if pending.len() >= MAX_HEADER_BYTES {
            break None;
        }
        let mut chunk = [0u8; 4096];
        let n = stream.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            if pending.is_empty() {
                return Ok(None);
            }
            // A peer that half-closes after a complete request line and one or
            // more headers, without the terminating blank line, receives no
            // response.  The framework leaves that truncated envelope alone;
            // only a syntactically complete header block is dispatched.
            if pending.windows(2).any(|window| window == b"\r\n") {
                return Ok(None);
            }
            break None;
        }
        if first_byte_at.is_none() {
            first_byte_at = Some(std::time::Instant::now());
        }
        pending.extend_from_slice(&chunk[..n]);
        if first_byte_at
            .is_some_and(|started| started.elapsed() >= std::time::Duration::from_secs(20))
        {
            return Err("request deadline".to_string());
        }
    };
    let Some(header_end) = header_end else {
        return Err("request headers exceed limit".to_string());
    };
    let head = String::from_utf8_lossy(&pending[..header_end]).into_owned();
    let mut lines = head.split("\r\n");
    let Some(first) = lines.next() else {
        return Err("bad request line".to_string());
    };
    // Cycle 49: an EMPTY method token (a leading space) draws no answer at all; a
    // tab is not a separator in the grammar, so a tab-separated line is a 400
    // (cycle 27).
    if first.is_empty() || first.starts_with(' ') {
        return Err("silence".to_string());
    }
    if first.contains('\t') {
        return Err("bad request line".to_string());
    }
    let parts = first.split(' ').collect::<Vec<_>>();
    if parts.first().is_some_and(|method| method.len() > 10) {
        return Err("method length".to_string());
    }
    let legacy_http09 =
        parts.len() == 2 && parts[0] == "GET" && !parts.iter().any(|part| part.is_empty());
    if (!legacy_http09 && parts.len() != 3) || parts.iter().any(|part| part.is_empty()) {
        return Err("bad request line".to_string());
    }
    if parts[1].len() > MAX_TARGET_BYTES {
        return Err("target too long".to_string());
    }
    // Cycle 36, swept byte by byte on kids and zen: the request TARGET must be printable
    // ASCII (0x21-0x7E), and every `%` in its PATH must begin an escape a real PMS reads
    // -- two characters present, parsed like `strtol` (an optional sign, then a hex
    // digit), `%%` a literal pair. The query is not validated this way.
    if !parts[1].bytes().all(|byte| (0x21..=0x7e).contains(&byte)) {
        return Err("bad request target".to_string());
    }
    {
        let raw_path = parts[1].split('?').next().unwrap_or("").as_bytes();
        let mut index = 0;
        while let Some(offset) = raw_path[index..].iter().position(|b| *b == b'%') {
            let at = index + offset;
            let following = &raw_path[at + 1..(at + 3).min(raw_path.len())];
            if following.first() == Some(&b'%') {
                index = at + 2;
                continue;
            }
            if following.len() < 2 {
                return Err("bad request target".to_string());
            }
            let signs = following.iter().take_while(|b| matches!(**b, b'+' | b'-')).count();
            let digit = following.get(signs);
            if signs > 1 || !digit.is_some_and(|b| b.is_ascii_hexdigit()) {
                return Err("bad request target".to_string());
            }
            index = at + 3;
            if index >= raw_path.len() {
                break;
            }
        }
    }
    if !legacy_http09 && !parts[2].starts_with("HTTP/") {
        return Err("bad request line".to_string());
    }
    let mut headers = HashMap::new();
    let mut header_lines = 0usize;
    for line in lines.filter(|line| !line.is_empty()) {
        header_lines += 1;
        if header_lines > MAX_HEADER_LINES || line.len() > MAX_HEADER_LINE_BYTES {
            return Err("header limit".to_string());
        }
        if line.starts_with([' ', '\t']) {
            return Err("header fold".to_string());
        }
        let Some((name, _value)) = line.split_once(':') else {
            return Err("bad header".to_string());
        };
        if name.is_empty() || name.bytes().any(|byte| byte <= 0x20 || byte >= 0x7f) {
            return Err("bad header".to_string());
        }
        let Some((_, value)) = line.split_once(':') else {
            return Err("bad header".to_string());
        };
        // Cycle 41: the value's length is measured after parsing, so the one optional
        // separator space does not count against the 4096-byte bound.
        if name.len() > 1024
            || value.strip_prefix(' ').unwrap_or(value).len() > 4096
            || value.bytes().any(|byte| byte == b'\t' || byte < 0x20)
        {
            return Err("header limit".to_string());
        }
        headers
            .entry(name.to_ascii_lowercase())
            .or_insert_with(|| value.strip_prefix(' ').unwrap_or(value).to_string());
    }
    let method = parts[0].to_string();
    let target = parts[1].to_string();
    let version = if legacy_http09 {
        "HTTP/0.9".to_string()
    } else {
        parts[2].to_string()
    };
    // Cycle 37/38: GET and HEAD answer without consuming a declared body, and so do the
    // two query-only UI writes (an item edit, a playlist create); an upload waits for
    // its bytes.
    let target_path = target.split('?').next().unwrap_or("");
    let query_only_write = (method == "PUT"
        && target_path
            .strip_prefix("/library/metadata/")
            .is_some_and(|id| !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit())))
        || (method == "POST" && target_path == "/playlists");
    let is_read_only = matches!(method.as_str(), "GET" | "HEAD") || query_only_write;
    let transfer_chunked = headers
        .get("transfer-encoding")
        .is_some_and(|value| value.eq_ignore_ascii_case("chunked"));
    let length = match headers.get("content-length") {
        Some(value) if value.starts_with('-') => return Err("negative content length".to_string()),
        Some(value) => value.parse::<usize>().unwrap_or(0),
        None => 0,
    };
    if length > MAX_BODY_BYTES {
        return Err("body exceeds limit".to_string());
    }

    if transfer_chunked {
        let mut cursor = header_end;
        let mut decoded = Vec::new();
        let complete = true;
        loop {
            let Some(line_end) = pending[cursor..]
                .windows(2)
                .position(|window| window == b"\r\n")
                .map(|offset| cursor + offset)
            else {
                if is_read_only {
                    pending.clear();
                    return Ok(Some(Request {
                        method,
                        target: target.clone(),
                        version: version.clone(),
                        headers,
                        body: decoded,
                        body_complete: false,
                    }));
                }
                if !read_more(stream, pending)? {
                    return Err("short chunk".to_string());
                }
                if first_byte_at
                    .is_some_and(|started| started.elapsed() >= std::time::Duration::from_secs(20))
                {
                    return Err("request deadline".to_string());
                }
                continue;
            };
            let line = &pending[cursor..line_end];
            let line_text = String::from_utf8_lossy(line);
            let Some(size_text) = parse_chunk_size(&line_text) else {
                // A negative chunk is treated as a short body by the
                // reference, whereas extensions, trailing spaces and
                // overflow are envelope errors.
                if line_text.starts_with('-') && is_read_only {
                    pending.clear();
                    return Ok(Some(Request {
                        method,
                        target: target.clone(),
                        version: version.clone(),
                        headers,
                        body: decoded,
                        body_complete: false,
                    }));
                }
                return Err("bad chunk".to_string());
            };
            let size = match i128::from_str_radix(size_text.trim_start_matches("0x"), 16) {
                Ok(value) if value <= i64::MAX as i128 => value as usize,
                _ => return Err("bad chunk".to_string()),
            };
            cursor = line_end + 2;
            if size == 0 {
                if pending.len() >= cursor + 2 && pending[cursor..cursor + 2] == *b"\r\n" {
                    cursor += 2;
                }
                break;
            }
            if pending.len() < cursor.saturating_add(size).saturating_add(2) {
                if is_read_only {
                    pending.clear();
                    return Ok(Some(Request {
                        method,
                        target: target.clone(),
                        version: version.clone(),
                        headers,
                        body: decoded,
                        body_complete: false,
                    }));
                }
                if !read_more(stream, pending)? {
                    return Err("short chunk".to_string());
                }
                if first_byte_at
                    .is_some_and(|started| started.elapsed() >= std::time::Duration::from_secs(20))
                {
                    return Err("request deadline".to_string());
                }
                continue;
            }
            decoded.extend_from_slice(&pending[cursor..cursor + size]);
            if pending[cursor + size..cursor + size + 2] != *b"\r\n" {
                return Err("bad chunk".to_string());
            }
            cursor += size + 2;
        }
        if complete {
            pending.drain(..cursor);
        } else {
            pending.clear();
        }
        return Ok(Some(Request {
            method,
            target,
            version,
            headers,
            body: decoded,
            body_complete: complete,
        }));
    }

    let available = pending.len().saturating_sub(header_end);
    let body_complete;
    if available < length && !is_read_only {
        while pending.len().saturating_sub(header_end) < length {
            if first_byte_at
                .is_some_and(|started| started.elapsed() >= std::time::Duration::from_secs(20))
            {
                return Err("request deadline".to_string());
            }
            if !read_more(stream, pending)? {
                break;
            }
            if first_byte_at
                .is_some_and(|started| started.elapsed() >= std::time::Duration::from_secs(20))
            {
                return Err("request deadline".to_string());
            }
        }
    }
    body_complete = pending.len().saturating_sub(header_end) >= length;
    let available = pending.len().saturating_sub(header_end);
    let take = available.min(length);
    let body = pending[header_end..header_end + take].to_vec();
    if body_complete {
        pending.drain(..header_end + length);
    } else {
        pending.clear();
    }
    Ok(Some(Request {
        method,
        target,
        version,
        headers,
        body,
        body_complete,
    }))
}

fn read_more<S: Read>(stream: &mut S, pending: &mut Vec<u8>) -> Result<bool, String> {
    let mut chunk = [0u8; 8192];
    let n = stream.read(&mut chunk).map_err(|error| error.to_string())?;
    if n == 0 {
        return Ok(false);
    }
    pending.extend_from_slice(&chunk[..n]);
    Ok(true)
}

fn parse_chunk_size(line: &str) -> Option<&str> {
    let token = line
        .strip_prefix("0x")
        .or_else(|| line.strip_prefix("0X"))
        .unwrap_or(line);
    let token = token.strip_prefix('+').unwrap_or(token);
    if token.is_empty() || token.contains([' ', '\t', ';']) {
        return None;
    }
    if token.starts_with('-') {
        return None;
    }
    if !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    Some(token)
}

/// Cycle 31, on plexscratch42 (1.42.2, the build plexsim reports): PATCH is not in
/// the method table at all -- a 500 on every path, `/identity` included, exactly as
/// `FROB` is. Cycle 15's routed PATCH was 1.43.4's.
fn known_method(method: &str) -> bool {
    matches!(method, "GET" | "HEAD" | "POST" | "PUT" | "DELETE" | "OPTIONS")
}

fn write_response<S: Write>(stream: &mut S, response: &Response, keep: bool) -> Result<(), String> {
    wire::write_enveloped(stream, response, &wire::Wire::bare(), keep).map(|_| ())
}

fn split_target(target: &str) -> (String, String) {
    if let Some(scheme_end) = target.find("://") {
        if &target[..scheme_end] == "http" {
            let authority_start = scheme_end + 3;
            let path_start = target[authority_start..]
                .find('/')
                .map(|offset| authority_start + offset)
                .unwrap_or(target.len());
            let origin_form = if path_start == target.len() {
                "/"
            } else {
                &target[path_start..]
            };
            return split_target(origin_form);
        }
    }
    // A `+` in the PATH is a literal plus (cycle 36's `+1` id); only a query decodes
    // it as a space.
    if let Some((path, query)) = target.split_once('?') {
        (percent_decode(&path.replace('+', "%2B")), query.to_string())
    } else {
        (percent_decode(&target.replace('+', "%2B")), String::new())
    }
}
fn parse_query(query: &str) -> Vec<(String, String)> {
    query
        .split('&')
        .filter(|part| !part.is_empty())
        .map(|part| {
            let (mut key, mut val) = (part, " ");
            if let Some((k, v)) = part.split_once('=') {
                key = k;
                val = v;
            }
            (percent_decode(key), percent_decode(val))
        })
        .collect()
}
fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Ok(a), Ok(b)) = (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                out.push(a * 16 + b);
                i += 3;
                continue;
            }
        }
        if bytes[i] == b'+' {
            out.push(b' ')
        } else {
            out.push(bytes[i])
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
fn hex(value: u8) -> Result<u8, ()> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        b'A'..=b'F' => Ok(value - b'A' + 10),
        _ => Err(()),
    }
}
fn key_of(value: &Value) -> Option<String> {
    value.get("ratingKey").map(string_value)
}

fn strip_excluded_fields(value: &mut Value, names: &HashSet<&str>) {
    match value {
        Value::Object(object) => {
            for name in names {
                object.remove(*name);
            }
            for child in object.values_mut() {
                strip_excluded_fields(child, names);
            }
        }
        Value::Array(values) => {
            for child in values {
                strip_excluded_fields(child, names);
            }
        }
        _ => {}
    }
}

fn strip_excluded_element(value: &mut Value, name: &str) {
    if let Value::Object(object) = value {
        if name == "Media" {
            let count = object
                .get("Media")
                .and_then(Value::as_array)
                .map_or(0, Vec::len);
            object.remove("Media");
            object.insert("mediaCount".to_string(), json!(count));
            object.insert("mediaCountOptimized".to_string(), json!(0));
        } else {
            object.remove(name);
        }
        for child in object.values_mut() {
            strip_excluded_element(child, name);
        }
    } else if let Value::Array(values) = value {
        for child in values {
            strip_excluded_element(child, name);
        }
    }
}

fn order_scalar_fields(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(order_scalar_fields).collect()),
        Value::Object(object) => {
            let mut scalar = Vec::new();
            let mut compound = Vec::new();
            for (key, value) in object {
                let value = order_scalar_fields(value);
                if value.is_array() || value.is_object() {
                    compound.push((key, value));
                } else {
                    scalar.push((key, value));
                }
            }
            scalar.sort_by(|left, right| left.0.cmp(&right.0));
            let mut ordered = Map::new();
            for (key, value) in scalar.into_iter().chain(compound) {
                ordered.insert(key, value);
            }
            Value::Object(ordered)
        }
        other => other,
    }
}

fn string_value(value: &Value) -> String {
    match value {
        Value::String(v) => v.clone(),
        Value::Number(v) => v.to_string(),
        Value::Bool(v) => v.to_string(),
        _ => String::new(),
    }
}

fn canonical_tag(value: &str) -> String {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return String::new();
    };
    first.to_uppercase().collect::<String>() + chars.as_str()
}

fn search_text_matches(wanted: &str, text: &str) -> bool {
    let wanted = wanted.trim().to_lowercase();
    if wanted.is_empty() || wanted.chars().count() == 1 {
        return false;
    }
    let terms = wanted.split_whitespace().collect::<Vec<_>>();
    let words = text
        .to_lowercase()
        .split_whitespace()
        .map(str::to_string)
        .collect::<Vec<_>>();
    if terms.len() == 1 && wanted.len() <= 3 && text.to_lowercase().contains(&wanted) {
        return true;
    }
    let mut cursor = 0usize;
    terms.iter().all(|term| {
        while cursor < words.len() && !words[cursor].starts_with(term) {
            cursor += 1;
        }
        let found = cursor < words.len();
        cursor += usize::from(found);
        found
    })
}

fn tag_id(field: &str, value: &str) -> i64 {
    (crc32(format!("{field}:{value}").as_bytes()) % 900_000 + 1_000) as i64
}

fn number_catalog_tags(items: &mut [Value]) {
    const TAG_FIELDS: &[(&str, &str)] = &[
        ("Genre", "genre"),
        ("Collection", "collection"),
        ("Director", "director"),
        ("Writer", "writer"),
        ("Producer", "producer"),
        ("Role", "actor"),
        ("Country", "country"),
        ("Label", "label"),
        ("Similar", "similar"),
        ("Mood", "mood"),
        ("Style", "style"),
    ];
    for item in items {
        for (field, filter_name) in TAG_FIELDS {
            let Some(tags) = item.get_mut(*field).and_then(Value::as_array_mut) else {
                continue;
            };
            for tag in tags {
                let Some(object) = tag.as_object_mut() else {
                    continue;
                };
                let Some(name) = object
                    .get("tag")
                    .and_then(Value::as_str)
                    .map(str::to_string)
                else {
                    continue;
                };
                if object.get("id").is_none() {
                    object.insert("id".to_string(), json!(tag_id(field, &name)));
                }
                if object.get("filter").is_none() {
                    let id = object.get("id").map(string_value).unwrap_or_default();
                    object.insert("filter".to_string(), json!(format!("{filter_name}={id}")));
                }
            }
        }
    }
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut value = !0_u32;
    for byte in bytes {
        value ^= u32::from(*byte);
        for _ in 0..8 {
            value = if value & 1 != 0 {
                (value >> 1) ^ 0xedb8_8320
            } else {
                value >> 1
            };
        }
    }
    !value
}
fn extract_key(value: &str) -> String {
    value.rsplit('/').next().unwrap_or(value).to_string()
}

fn metadata_keys_from_uri(uri: &str) -> Vec<String> {
    let decoded = percent_decode(uri);
    let marker = "/library/metadata/";
    let mut keys = Vec::new();
    let mut cursor = 0;
    while let Some(relative) = decoded[cursor..].find(marker) {
        let start = cursor + relative + marker.len();
        let tail = &decoded[start..];
        let end = tail.find(['?', '/']).unwrap_or(tail.len());
        let segment = &tail[..end];
        for key in segment.split(',').filter(|key| !key.is_empty()) {
            keys.push(key.to_string());
        }
        cursor = start + end;
        if cursor >= decoded.len() {
            break;
        }
    }
    keys
}

fn source_uri_for(uri: &str) -> String {
    let decoded = percent_decode(uri);
    let marker = "/com.plexapp.plugins.library";
    let Some(index) = decoded.find(marker) else {
        return uri.to_string();
    };
    let path = &decoded[index + marker.len()..];
    let single = path.starts_with("/library/metadata/")
        && !path.contains(',')
        && path
            .trim_start_matches("/library/metadata/")
            .split(['/', '?'])
            .next()
            .is_some_and(|value| !value.is_empty() && value.chars().all(|ch| ch.is_ascii_digit()));
    let kind = if single { "item" } else { "directory" };
    format!("library://x/{kind}/{}", percent_encode(path))
}

fn percent_encode(value: &str) -> String {
    value
        .as_bytes()
        .iter()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-_.~".contains(byte) {
                (*byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

fn unix_year(timestamp: i64) -> i64 {
    // Gregorian civil date conversion from days since 1970-01-01.  Keeping the
    // conversion local avoids a date dependency in the server binary while
    // still applying the same calendar-year window as the measured shelf.
    let days = timestamp.div_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096).div_euclid(365);
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let month = (5 * doy + 2).div_euclid(153);
    year + if month < 10 { 0 } else { 1 }
}

/// The account feature names plex.tv reports for the owner (the oracle's
/// `OWNER_FEATURES` with no claim of its own).
const OWNER_FEATURES: &str = "adaptive_bitrate,camera_upload,collections,content_filter,download_certificates,dvr,federated-auth,hardware_transcoding,home,hwtranscode,item_clusters,kevin-bacon,livetv,loudness,lyrics,music-analysis,music_videos,pass,photosV6-edit,photosV6-tv-albums,premium_music_metadata,radio,server-manager,session_bandwidth_restrictions,session_kick,shared-radio,sync,trailers,tuner-sharing,type-first,unsupportedtuners,webhooks";

/// Cycle 54: the rows `unwatched=1` drops from a list that is not a query -- a movie
/// or an episode with a play recorded.  A parent row is never dropped for it.
fn watched_leaf(row: &Value) -> bool {
    matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
        && row.get("viewCount").is_some_and(|count| {
            !count.is_null() && count.as_i64() != Some(0) && count.as_str().is_none_or(|text| !text.is_empty() && text != "0")
        })
}

/// Cycle 64, measured on zen, kids and plexscratch42: `X-Plex-Product: Plex Home
/// Theater` -- that spelling only, the header beating the query -- gets a FIVE-component
/// `version` on `/` and `/media/providers` (`1.42.2.10156.0-<build>`).
fn home_theater_version(body: &mut Value, query: &HashMap<String, String>, headers: &HashMap<String, String>) {
    let product = headers
        .get("x-plex-product")
        .filter(|value| !value.is_empty())
        .or_else(|| query.get("X-Plex-Product"));
    if product.map(String::as_str) != Some("Plex Home Theater") {
        return;
    }
    if let Some(version) = body["MediaContainer"].get("version").and_then(Value::as_str) {
        if let Some((head, tail)) = version.split_once('-') {
            body["MediaContainer"]["version"] = json!(format!("{head}.0-{tail}"));
        }
    }
}

/// Python's ordering of two JSON scalars for a sort: numbers by value, strings
/// by code point, anything else as equal.
/// The two-letter codes Python's `locale.locale_alias` names, which is the set the
/// oracle's subtitle search accepts (`SUBTITLE_LANGUAGES`).
const SUBTITLE_LANGUAGES: &[&str] = &[
    "aa", "af", "ak", "am", "an", "ar", "as", "az", "be", "bg", "bi", "bn", "bo", "br", "bs",
    "ca", "ce", "cs", "cv", "cy", "cz", "da", "de", "dv", "dz", "ee", "el", "en", "eo", "es",
    "et", "eu", "fa", "ff", "fi", "fo", "fr", "fy", "ga", "gd", "gl", "gu", "gv", "ha", "he",
    "hi", "hr", "ht", "hu", "hy", "ia", "id", "ig", "ik", "in", "is", "it", "iu", "iw", "ja",
    "jp", "ka", "kk", "kl", "km", "kn", "ko", "ks", "ku", "kw", "ky", "lb", "lg", "li", "ln",
    "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "nb", "ne", "nl",
    "nn", "no", "nr", "ny", "oc", "om", "or", "os", "pa", "pd", "ph", "pl", "pp", "ps", "pt",
    "ro", "ru", "rw", "sa", "sc", "sd", "se", "sh", "si", "sk", "sl", "sm", "so", "sp", "sq",
    "sr", "ss", "st", "sv", "sw", "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr",
    "ts", "tt", "ug", "uk", "ur", "uz", "ve", "vi", "wa", "wo", "xh", "yi", "yo", "zh", "zu",
];

fn compare_json(left: &Value, right: &Value) -> std::cmp::Ordering {
    match (left, right) {
        (Value::Number(a), Value::Number(b)) => a
            .as_f64()
            .unwrap_or(0.0)
            .partial_cmp(&b.as_f64().unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal),
        (Value::String(a), Value::String(b)) => a.cmp(b),
        (Value::Bool(a), Value::Bool(b)) => a.cmp(b),
        _ => std::cmp::Ordering::Equal,
    }
}

fn local_interface_address() -> Option<String> {
    for destination in ["192.0.2.1:9", "8.8.8.8:9", "1.1.1.1:9"] {
        let Ok(socket) = UdpSocket::bind("0.0.0.0:0") else {
            continue;
        };
        if socket.connect(destination).is_ok() {
            if let Ok(address) = socket.local_addr() {
                if !address.ip().is_unspecified() {
                    return Some(address.ip().to_string());
                }
            }
        }
    }
    None
}

/// Return the start of the requested dashboard calendar bucket in the host's
/// local timezone. PMS labels day/week/month/year samples by local midnight,
/// Monday, the first of the month, or January 1 respectively; fixed epoch
/// widths drift across both timezone and daylight-saving boundaries.
fn statistics_bucket(timestamp: i64, timespan: i64) -> i64 {
    if timespan == 6 {
        return timestamp;
    }
    #[cfg(unix)]
    unsafe {
        let mut local: libc::tm = std::mem::zeroed();
        let source = timestamp as libc::time_t;
        if libc::localtime_r(&source, &mut local).is_null() {
            return timestamp;
        }
        local.tm_hour = 0;
        local.tm_min = 0;
        local.tm_sec = 0;
        match timespan {
            3 => {}
            2 => {
                // tm_wday is Sunday=0; move to the preceding Monday.
                let days_since_monday = (local.tm_wday + 6) % 7;
                local.tm_mday -= days_since_monday;
            }
            1 => local.tm_mday = 1,
            0 => {
                local.tm_mon = 0;
                local.tm_mday = 1;
            }
            _ => return timestamp,
        }
        return libc::mktime(&mut local) as i64;
    }
    #[cfg(not(unix))]
    {
        let width = match timespan {
            3 => 86_400,
            2 => 7 * 86_400,
            1 => 30 * 86_400,
            0 => 365 * 86_400,
            _ => 1,
        };
        timestamp - timestamp.rem_euclid(width)
    }
}

/// Python's truthiness for a JSON value: null, false, 0, "" and empty containers
/// are false. The Python oracle reads most optional fields this way.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// An integer out of a JSON number or a numeric string, the way `int(x)` reads the
/// Python rows (whose indexes are sometimes stored as strings).
fn value_as_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64)),
        Value::String(text) => text.trim().parse::<i64>().ok(),
        Value::Bool(flag) => Some(*flag as i64),
        _ => None,
    }
}

/// Python's `"%s" % value` for a JSON scalar: None, True/False, the number, the text.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Python 3's `round()`: halves go to the even neighbour.
fn py_round(value: f64) -> i64 {
    let floor = value.floor();
    let diff = value - floor;
    if (diff - 0.5).abs() < 1e-9 {
        if (floor as i64) % 2 == 0 {
            floor as i64
        } else {
            floor as i64 + 1
        }
    } else {
        value.round() as i64
    }
}

fn bytes_digest(bytes: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    PathBuf::from(format!("{}.{}", path.display(), suffix))
}

fn sidecar_roots(catalog: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(parent) = catalog.parent() {
        roots.push(parent.to_path_buf());
        if let Some(root) = parent.parent() {
            roots.push(root.to_path_buf());
        }
    }
    roots.push(PathBuf::from("."));
    roots
}

fn load_json_sidecar(catalog: &Path, name: &str) -> Option<Value> {
    sidecar_roots(catalog)
        .into_iter()
        .map(|root| root.join(name))
        .find_map(|path| fs::read(path).ok())
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
}

fn static_sidecar_response(catalog: &Path, name: &str, content_type: &str, head: bool) -> Response {
    let Some(body) = sidecar_roots(catalog)
        .into_iter()
        .map(|root| root.join(name))
        .find_map(|path| fs::read(path).ok())
    else {
        return Response::error(404, "plexsim: not found");
    };
    Response {
        status: 200,
        content_type: content_type.to_string(),
        body: { let _ = head; body },
        headers: vec![
            ("Cache-Control".to_string(), "no-cache".to_string()),
            ("Accept-Ranges".to_string(), "bytes".to_string()),
        ],
        close: false,
    }
}

fn replace_section_placeholder(value: &mut Value, section: &str) {
    match value {
        Value::String(text) => {
            *text = text.replace("{section}", section);
        }
        Value::Array(values) => {
            for value in values {
                replace_section_placeholder(value, section);
            }
        }
        Value::Object(values) => {
            for value in values.values_mut() {
                replace_section_placeholder(value, section);
            }
        }
        _ => {}
    }
}

fn load_regional_prefs(catalog: &Path, language: &str) -> Vec<Value> {
    let Some(document) = load_json_sidecar(catalog, "regional_server_prefs.json") else {
        return Vec::new();
    };
    let key = if language == "pt-BR" {
        "pt-BR"
    } else {
        language
    };
    let localized = document
        .get("catalogues")
        .and_then(|catalogues| catalogues.get(key))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(mut base) =
        load_json_sidecar(catalog, "server_prefs.json").and_then(|value| value.as_array().cloned())
    else {
        return localized;
    };
    let localized = localized
        .into_iter()
        .filter_map(|row| {
            Some((
                row.get("id")?.as_str()?.to_string(),
                row.as_object()?.clone(),
            ))
        })
        .collect::<HashMap<_, _>>();
    for row in &mut base {
        let Some(id) = row.get("id").and_then(Value::as_str) else {
            continue;
        };
        let Some(local) = localized.get(id) else {
            continue;
        };
        if let Some(object) = row.as_object_mut() {
            for field in ["label", "summary", "enumValues"] {
                if let Some(value) = local.get(field) {
                    object.insert(field.to_string(), value.clone());
                }
            }
        }
    }
    base
}
/// Which `Meta/Type` row carries `active`, and which Sort inside it.
///
/// Cycle 19, measured on kids and zen: the flag follows the REQUEST --
/// `/all?includeMeta=1` marks the library's type, `&type=3` marks `season`,
/// `&type=4` `episode`, and `?includeDetails=1` marks NONE.  Cycle 23: the
/// active SORT is marked only inside the Type that matches, and which one
/// follows `sort=` (the `default` one when none is asked).
fn meta_active(meta: &mut Value, active: Option<&str>, spec: Option<&str>) {
    for entry in meta.get_mut("Type").and_then(Value::as_array_mut).into_iter().flatten() {
        let is_active = active.is_some() && entry.get("type").and_then(Value::as_str) == active;
        entry["active"] = json!(is_active);
        for sort in entry.get_mut("Sort").and_then(Value::as_array_mut).into_iter().flatten() {
            if let Some(object) = sort.as_object_mut() {
                object.remove("active");
                object.remove("activeDirection");
            }
        }
        if !is_active {
            continue;
        }
        let spec = spec.unwrap_or("");
        let (wanted, direction) = spec.split_once(':').unwrap_or((spec, ""));
        for sort in entry.get_mut("Sort").and_then(Value::as_array_mut).into_iter().flatten() {
            if spec.is_empty() {
                if sort.get("default").is_none_or(|value| {
                    value.is_null() || value.as_str() == Some("") || value.as_bool() == Some(false)
                }) {
                    continue;
                }
            } else if sort.get("key").and_then(Value::as_str) != Some(wanted) {
                continue;
            }
            let chosen = if !direction.is_empty() {
                direction.split(':').next().unwrap_or("").to_string()
            } else {
                sort.get("defaultDirection")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .or_else(|| sort.get("default").and_then(Value::as_str).filter(|value| !value.is_empty()))
                    .unwrap_or("asc")
                    .to_string()
            };
            if let Some(object) = sort.as_object_mut() {
                object.insert("active".to_string(), json!(true));
                object.insert("activeDirection".to_string(), json!(chosen));
            }
            break;
        }
    }
}

/// What `includeAdvanced=1` ADDS to a section's Meta, captured type by type
/// off the reference by diffing the plain and advanced documents.
fn advanced_meta(kind: &str, entry_type: &str) -> Option<Value> {
    Some(match (kind, entry_type) {
        ("movie", "movie") => json!({
            "Sort": [
                {"defaultDirection": "desc", "descKey": "year:desc", "key": "year", "title": "Year"},
                {"defaultDirection": "desc", "descKey": "userRating:desc", "key": "userRating", "title": "Rating"},
                {"defaultDirection": "desc", "descKey": "contentRating:desc", "key": "contentRating", "title": "Content Rating"},
                {"defaultDirection": "desc", "descKey": "viewOffset:desc", "key": "viewOffset", "title": "Progress"},
                {"defaultDirection": "desc", "descKey": "viewCount:desc", "key": "viewCount", "title": "Plays"},
                {"defaultDirection": "desc", "descKey": "mediaBitrate:desc", "key": "mediaBitrate", "title": "Bitrate"}
            ],
            "Filter": [{"filter": "duplicate", "filterType": "boolean",
                        "key": "/library/sections/%s/duplicate", "title": "Duplicates",
                        "type": "filter", "advanced": true}]
        }),
        ("show", "show") => json!({
            "Sort": [
                {"defaultDirection": "desc", "descKey": "year:desc", "key": "year", "title": "Year"},
                {"defaultDirection": "desc", "descKey": "userRating:desc", "key": "userRating", "title": "Rating"},
                {"defaultDirection": "desc", "descKey": "contentRating:desc", "key": "contentRating", "title": "Content Rating"},
                {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt", "title": "Date Added"}
            ]
        }),
        ("show", "season") => json!({
            "Sort": [
                {"defaultDirection": "asc", "descKey": "season.index:desc,season.titleSort",
                 "key": "season.index,season.titleSort", "title": "Season"},
                {"defaultDirection": "desc", "descKey": "userRating:desc", "key": "userRating", "title": "Rating"},
                {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt", "title": "Date Added"}
            ]
        }),
        ("show", "episode") => json!({
            "Sort": [
                {"defaultDirection": "desc", "descKey": "titleSort:desc",
                 "firstCharacterKey": "/library/sections/%s/firstCharacter?type=4",
                 "key": "titleSort", "title": "Title"},
                {"defaultDirection": "desc", "descKey": "year:desc", "key": "year", "title": "Year"},
                {"defaultDirection": "desc", "descKey": "userRating:desc", "key": "userRating", "title": "Rating"},
                {"defaultDirection": "desc", "descKey": "duration:desc", "key": "duration", "title": "Duration"},
                {"defaultDirection": "desc", "descKey": "viewOffset:desc", "key": "viewOffset", "title": "Progress"},
                {"defaultDirection": "desc", "descKey": "viewCount:desc", "key": "viewCount", "title": "Plays"},
                {"defaultDirection": "desc", "descKey": "mediaHeight:desc", "key": "mediaHeight", "title": "Resolution"},
                {"defaultDirection": "desc", "descKey": "mediaBitrate:desc", "key": "mediaBitrate", "title": "Bitrate"}
            ],
            "Filter": [{"filter": "duplicate", "filterType": "boolean",
                        "key": "/library/sections/%s/duplicate?type=4",
                        "title": "Duplicates", "type": "filter", "advanced": true}]
        }),
        _ => return None,
    })
}

/// The ORDER of a type's sorts once the advanced ones are in, captured from
/// the reference's Meta with includeAdvanced=1 -- interleaved, not appended.
fn advanced_sort_order(entry_type: &str) -> Option<&'static [&'static str]> {
    Some(match entry_type {
        "movie" => &[
            "titleSort", "year", "originallyAvailableAt", "rating", "audienceRating", "userRating",
            "contentRating", "duration", "viewOffset", "viewCount", "addedAt", "lastViewedAt",
            "mediaHeight", "mediaBitrate", "random",
        ],
        "show" => &[
            "titleSort", "year", "originallyAvailableAt", "rating", "audienceRating", "userRating",
            "contentRating", "unviewedLeafCount", "episode.addedAt", "addedAt", "lastViewedAt", "random",
        ],
        "season" => &[
            "season.index,season.titleSort", "show.titleSort,index", "userRating", "addedAt", "random",
        ],
        "episode" => &[
            "titleSort",
            "show.titleSort,season.index:nullsLast,episode.index:nullsLast,episode.originallyAvailableAt:nullsLast,episode.titleSort,episode.id",
            "year", "originallyAvailableAt", "rating", "audienceRating", "userRating", "duration",
            "viewOffset", "viewCount", "addedAt", "lastViewedAt", "mediaHeight", "mediaBitrate", "random",
        ],
        _ => return None,
    })
}

/// Cycle 56: whether a row carries a per-user settings record -- any view-state
/// write stamps `_viewUpdatedAt`, and a catalog row may arrive with state already.
fn has_settings(row: &Value) -> bool {
    [
        "_viewUpdatedAt", "viewCount", "viewOffset", "lastViewedAt", "userRating",
        "lastRatedAt", "skipCount",
    ]
    .iter()
    .any(|name| row.get(*name).is_some_and(|value| !value.is_null() && value.as_str() != Some("")))
}

/// The tree's MediaStream (server.py's `_tree_stream`): a fixed field set per
/// stream type, `type` rather than `streamType`, the language as its short
/// TAG, and every number a STRING except `id`, `index`, `type` and `channels`.
fn tree_stream(stream: &Value) -> Value {
    let kind = stream.get("streamType").and_then(Value::as_i64).unwrap_or(0);
    let names: &[&str] = match kind {
        1 => &[
            "id", "index", "type", "codec", "language", "bitDepth", "chromaLocation",
            "chromaSubsampling", "codedHeight", "codedWidth", "frameRate", "hasScalingMatrix",
            "height", "level", "profile", "refFrames", "scanType", "width",
        ],
        2 => &["id", "index", "type", "channels", "codec", "language", "samplingRate"],
        _ => &["id", "index", "type", "codec", "language"],
    };
    let mut out = Map::new();
    for name in names {
        let value = match *name {
            "type" => stream.get("streamType").cloned(),
            "language" => stream
                .get("languageTag")
                .filter(|value| !value.is_null())
                .or_else(|| stream.get("language"))
                .cloned(),
            other => stream.get(other).cloned(),
        };
        let Some(value) = value.filter(|value| !value.is_null()) else {
            continue;
        };
        let value = if matches!(*name, "id" | "index" | "type" | "channels") {
            value
        } else if *name == "frameRate" && value.is_number() {
            json!(format!("{:.3}", value.as_f64().unwrap_or(0.0)))
        } else if let Some(flag) = value.as_bool() {
            json!(if flag { "1" } else { "0" })
        } else if value.is_number() {
            json!(string_value(&value))
        } else {
            value
        };
        out.insert(name.to_string(), value);
    }
    Value::Object(out)
}

/// A text preference as a real PMS stores it (server.py's `_escaped_pref`).
/// Cycle 21: the value is percent-decoded a SECOND time -- a decode that FAILS
/// (a `%` not followed by two hex digits, or bytes that are not UTF-8) stores
/// the empty string -- and then `%`, `:` and `|` are escaped in LOWERCASE hex.
fn escaped_pref(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let hex = |b: u8| (b as char).to_digit(16);
            match (bytes.get(index + 1).and_then(|b| hex(*b)), bytes.get(index + 2).and_then(|b| hex(*b))) {
                (Some(high), Some(low)) => {
                    out.push((high * 16 + low) as u8);
                    index += 3;
                    continue;
                }
                _ => return String::new(),
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    let Ok(text) = String::from_utf8(out) else {
        return String::new();
    };
    text.replace('%', "%25").replace(':', "%3a").replace('|', "%7c")
}

fn page_fields(fields: &mut Map<String, Value>, query: &HashMap<String, String>) {
    if let (Some(start), Some(size)) = (
        query
            .get("X-Plex-Container-Start")
            .and_then(|v| v.parse::<usize>().ok()),
        query
            .get("X-Plex-Container-Size")
            .and_then(|v| v.parse::<usize>().ok()),
    ) {
        if let Some(rows) = fields.get_mut("Metadata").and_then(Value::as_array_mut) {
            let total = rows.len();
            *rows = rows.iter().skip(start).take(size).cloned().collect();
            fields.insert("offset".to_string(), json!(start));
            fields.insert("totalSize".to_string(), json!(total));
        }
    }
}

fn plugin_container(rows: Vec<(&str, bool)>) -> Value {
    let directories = rows
        .into_iter()
        .map(|(name, library)| {
            if library {
                Value::Object(Map::from_iter([
                    ("art".to_string(), json!("/:/resources/library-art.png")),
                    ("thumb".to_string(), json!("/:/resources/library-icon.png")),
                    ("key".to_string(), json!(name)),
                    ("title".to_string(), json!("Library")),
                    ("share".to_string(), json!("0")),
                    (
                        "identifier".to_string(),
                        json!("com.plexapp.plugins.library"),
                    ),
                    ("_pluginLibrary".to_string(), json!(true)),
                ]))
            } else {
                Value::Object(Map::from_iter([
                    ("key".to_string(), json!(name)),
                    ("title".to_string(), json!(name)),
                ]))
            }
        })
        .collect::<Vec<_>>();
    Value::Object(Map::from_iter([(
        "MediaContainer".to_string(),
        Value::Object(Map::from_iter([
            ("size".to_string(), json!(directories.len())),
            ("content".to_string(), json!("plugins")),
            ("Directory".to_string(), Value::Array(directories)),
        ])),
    )]))
}

fn strip_private_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .filter(|(key, _)| !key.starts_with('_'))
                .map(|(key, value)| (key.clone(), strip_private_value(value)))
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(strip_private_value).collect()),
        _ => value.clone(),
    }
}

#[derive(Debug, Clone, Copy)]
struct RangeWindow {
    start: u64,
    end: u64,
    advertised_start: u64,
    advertised_end: u64,
}

#[derive(Debug, Clone, Copy)]
enum RangeResult {
    Whole,
    Partial(RangeWindow),
    Unsatisfiable,
}

fn parse_range(header: Option<&str>, size: u64) -> RangeResult {
    let Some(header) = header else {
        return RangeResult::Whole;
    };
    let Some((_unit, spec)) = header.split_once('=') else {
        return RangeResult::Whole;
    };
    if spec.is_empty() || spec == "-" {
        return RangeResult::Whole;
    }
    let whole = || {
        RangeResult::Partial(RangeWindow {
            start: 0,
            end: size.saturating_sub(1),
            advertised_start: 0,
            advertised_end: size.saturating_sub(1),
        })
    };
    if let Some(rest) = spec.strip_prefix('-') {
        let digits = leading_digits(rest);
        let suffix = digits.unwrap_or(0);
        if suffix == 0 {
            return RangeResult::Partial(RangeWindow {
                start: 0,
                end: size.saturating_sub(1),
                advertised_start: 0,
                advertised_end: 0,
            });
        }
        if suffix >= size {
            return whole();
        }
        return RangeResult::Partial(RangeWindow {
            start: size.saturating_sub(suffix),
            end: size.saturating_sub(1),
            advertised_start: size.saturating_sub(suffix),
            advertised_end: size.saturating_sub(1),
        });
    }
    let Some((first, last)) = spec.split_once('-') else {
        return whole();
    };
    if last.contains('-') {
        return whole();
    }
    let Some(start) = leading_digits(first) else {
        return whole();
    };
    let end = if last.is_empty() {
        size.saturating_sub(1)
    } else {
        leading_digits(last)
            .filter(|_| last.bytes().all(|byte| byte.is_ascii_digit()))
            .unwrap_or(0)
    };
    if start >= size || end < start {
        return RangeResult::Unsatisfiable;
    }
    let end = end.min(size.saturating_sub(1));
    RangeResult::Partial(RangeWindow {
        start,
        end,
        advertised_start: start,
        advertised_end: end,
    })
}

fn leading_digits(value: &str) -> Option<u64> {
    let digits = value
        .as_bytes()
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if digits == 0 {
        return None;
    }
    let mut result = 0_u64;
    for byte in value.as_bytes().iter().take(digits) {
        result = result
            .saturating_mul(10)
            .saturating_add((byte - b'0') as u64);
    }
    Some(result)
}
fn part_content_type(part: &Value) -> String {
    let file = part.get("file").and_then(Value::as_str).unwrap_or("");
    match file
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "mkv" => "video/x-matroska",
        "mp4" | "m4v" => "video/mp4",
        "webm" => "video/webm",
        "avi" | "divx" => "video/x-msvideo",
        "mov" => "video/quicktime",
        "ts" => "video/MP2T",
        "mpg" | "mpeg" => "video/mpeg",
        "flv" => "video/x-flv",
        "wmv" => "video/x-ms-wmv",
        "asf" => "video/x-ms-asf",
        _ => "application/octet-stream",
    }
    .to_string()
}
fn content_type(path: &str) -> String {
    if path.ends_with(".html") {
        "text/html".to_string()
    } else if path.ends_with(".js") {
        "text/javascript".to_string()
    } else if path.ends_with(".css") {
        "text/css".to_string()
    } else {
        "application/octet-stream".to_string()
    }
}
fn png_placeholder() -> Vec<u8> {
    b"\x89PNG\r\n\x1a\n".to_vec()
}
fn jpeg_placeholder() -> Vec<u8> {
    b"\xff\xd8\xff\xd9".to_vec()
}

fn generated_png(width: u32, height: u32, rgb: [u8; 3]) -> Vec<u8> {
    let width = width.clamp(1, 2000);
    let height = height.clamp(1, 2000);
    let mut raw = Vec::with_capacity((width as usize * 3 + 1) * height as usize);
    let row = vec![rgb[0], rgb[1], rgb[2]]
        .into_iter()
        .cycle()
        .take(width as usize * 3)
        .collect::<Vec<_>>();
    for _ in 0..height {
        raw.push(0);
        raw.extend_from_slice(&row);
    }
    let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(&raw).ok();
    let compressed = encoder.finish().unwrap_or_default();
    let chunk = |tag: &[u8; 4], payload: &[u8]| {
        let mut body = Vec::with_capacity(8 + payload.len());
        body.extend_from_slice(tag);
        body.extend_from_slice(payload);
        let mut out = Vec::with_capacity(12 + payload.len());
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc32(&body).to_be_bytes());
        out
    };
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    let mut output = b"\x89PNG\r\n\x1a\n".to_vec();
    output.extend_from_slice(&chunk(b"IHDR", &ihdr));
    output.extend_from_slice(&chunk(b"IDAT", &compressed));
    output.extend_from_slice(&chunk(b"IEND", &[]));
    output
}

fn generated_jpeg(width: u32, height: u32) -> Vec<u8> {
    let width = width.clamp(1, u16::MAX as u32) as u16;
    let height = height.clamp(1, u16::MAX as u32) as u16;
    // The UI only needs a stable bundled-art placeholder.  Keep a valid SOF
    // geometry and terminator so image sniffers do not mistake JPEG resources
    // for the PNG placeholders used by older routes.
    let mut output = vec![
        0xff, 0xd8, // SOI
        0xff, 0xc0, 0x00, 0x11, 0x08,
    ];
    output.extend_from_slice(&height.to_be_bytes());
    output.extend_from_slice(&width.to_be_bytes());
    output.extend_from_slice(&[3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1]);
    output.extend_from_slice(&[0xff, 0xd9]);
    output
}

fn parse_http_url(url: &str) -> Option<(String, String, u16)> {
    let rest = url.strip_prefix("http://")?;
    let (host_port, path) = rest.split_once('/').unwrap_or((rest, ""));
    let (host, port) = host_port
        .split_once(':')
        .map(|(h, p)| (h, p.parse().ok().unwrap_or(80)))
        .unwrap_or((host_port, 80));
    Some((host.to_string(), format!("/{path}"), port))
}

fn form_encode(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push('%');
            out.push_str(&format!("{byte:02X}"));
        }
    }
    out
}

fn base64_decode(text: &str) -> Option<String> {
    String::from_utf8(base64_decode_bytes(text)?).ok()
}

fn base64_decode_bytes(text: &str) -> Option<Vec<u8>> {
    let mut value = 0_u32;
    let mut bits = 0_u8;
    let mut bytes = Vec::new();
    for character in text.bytes() {
        if character == b'=' {
            break;
        }
        let digit = match character {
            b'A'..=b'Z' => character - b'A',
            b'a'..=b'z' => character - b'a' + 26,
            b'0'..=b'9' => character - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32;
        value = (value << 6) | digit;
        bits = bits.saturating_add(6);
        while bits >= 8 {
            bits -= 8;
            bytes.push((value >> bits) as u8);
            if bits > 0 {
                value &= (1_u32 << bits) - 1;
            } else {
                value = 0;
            }
        }
    }
    Some(bytes)
}

fn capitalize_ascii(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        Some(first) => first.to_ascii_uppercase().to_string() + chars.as_str(),
        None => String::new(),
    }
}

fn type_number_for(value: &Value) -> Option<i64> {
    match value.get("type").and_then(Value::as_str)? {
        "movie" => Some(1),
        "show" => Some(2),
        "season" => Some(3),
        "episode" => Some(4),
        "clip" => Some(8),
        "playlist" => Some(15),
        "collection" => Some(18),
        _ => None,
    }
}

fn is_true_option(value: &str) -> bool {
    let normalized = value.trim_start_matches('+');
    !normalized.is_empty()
        && normalized.chars().all(|ch| ch.is_ascii_digit())
        && normalized.parse::<u64>().ok() == Some(1)
}

/// The current time in the IMF-fixdate form every response's `Date` carries
/// (`BaseHTTPRequestHandler.date_time_string`).
fn http_date() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    // Howard Hinnant's civil-from-days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let weekday = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"][days.rem_euclid(7) as usize];
    let month_name = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        [(month - 1) as usize];
    format!(
        "{weekday}, {day:02} {month_name} {year} {:02}:{:02}:{:02} GMT",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

pub fn xml_or_json(response: Response, accept: Option<&str>) -> Response {
    let want_xml = accept_wants_xml(accept.unwrap_or(""));
    if response.status != 200 || !want_xml || response.content_type != "application/json" {
        return response;
    }
    let Ok(value) = serde_json::from_slice::<Value>(&response.body) else {
        return response;
    };
    let body = xmlout::to_xml(&value, None);
    Response {
        status: response.status,
        content_type: "text/xml;charset=utf-8".to_string(),
        body,
        headers: response.headers,
        close: response.close,
    }
}

/// A JSON body in the order the XML writer emits, with every private marker
/// dropped (server.py's `json_body`).
///
/// Cycle 19, measured on kids and zen: a real PMS's JSON object keys come out
/// in exactly its XML order -- the container's attributes, then its child keys;
/// a row's attributes in the row's attribute order, then its child element
/// keys.  Cycle 22: a LIGHT row's JSON child order is the light order, read off
/// the row's own `_light` marker -- so the markers are dropped AFTER ordering.
/// The plug-in framework's subtrees write their own bodies and are left alone.
fn order_json_body(response: &mut Response, path: &str) {
    if !response.content_type.starts_with("application/json")
        || path == "/system"
        || path.starts_with("/system/")
        || path.starts_with("/:/plugins")
    {
        return;
    }
    let Ok(document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(object) = document.as_object() else {
        return;
    };
    if object.len() != 1 || !object.values().next().is_some_and(Value::is_object) {
        return;
    }
    let ordered = strip_private_value(&xmlout::ordered(&document, None));
    if let Ok(body) = serde_json::to_vec(&ordered) {
        response.body = body;
    }
}

/// Apply the client's container paging to any MediaContainer not yet paged.
///
/// Measured on both servers: EVERY MediaContainer pages when both
/// `X-Plex-Container-Start` and `X-Plex-Container-Size` are present -- `/`,
/// `/library`, `/:/prefs`, a detail row, a tree, every hub list -- and even an
/// empty one gains `totalSize: 0` and `offset: 0`.  A container holding several
/// lists slices each, counts `size` over all of them and takes `totalSize` from
/// the longest.  The exceptions are measured too: a container that already
/// paged itself (`totalSize`), a plug-in framework container (`noHistory`), and
/// `/system/search`.  Cycle 27: a `_pageflat` marker pages the named lists as ONE
/// sequence (`/library/sections/{id}?includeDetails=1`).
fn page_container(response: &mut Response, query: &HashMap<String, String>, path: &str) {
    if response.status != 200 || !response.content_type.starts_with("application/json") {
        return;
    }
    let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(object) = document.as_object() else {
        return;
    };
    if object.len() != 1 || !object.contains_key("MediaContainer") {
        return;
    }
    let Some(body) = document.get_mut("MediaContainer").and_then(Value::as_object_mut) else {
        return;
    };
    let flat = body.remove("_pageflat");
    let rewrite = |response: &mut Response, document: &Value| {
        response.body = serde_json::to_vec(document).unwrap_or_default();
    };
    if body.contains_key("totalSize") || body.contains_key("noHistory") || path.trim_end_matches('/') == "/system/search" {
        if flat.is_some() {
            rewrite(response, &document);
        }
        return;
    }
    let start_raw = query.get("X-Plex-Container-Start");
    let size_raw = query.get("X-Plex-Container-Size");
    let window = match (start_raw, size_raw) {
        (Some(start), Some(size)) => match (start.trim().parse::<i64>(), size.trim().parse::<i64>()) {
            (Ok(start), Ok(size)) if start >= 0 && size >= 0 => Some((start as usize, size as usize)),
            (Ok(_), Ok(_)) => None,
            _ => {
                *response = Response::error(500, "plexsim: bad container parameter");
                return;
            }
        },
        _ => None,
    };
    if let Some(flat) = flat {
        let names = flat
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect::<Vec<_>>();
        let order = names
            .iter()
            .flat_map(|name| {
                body.get(name)
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .map(move |row| (name.clone(), row))
            })
            .collect::<Vec<_>>();
        let Some((start, size)) = window else {
            body.insert("size".to_string(), json!(order.len()));
            rewrite(response, &document);
            return;
        };
        let shown = order.iter().skip(start).take(size).cloned().collect::<Vec<_>>();
        let mut out = Map::new();
        out.insert("size".to_string(), json!(shown.len()));
        out.insert("totalSize".to_string(), json!(order.len()));
        out.insert("offset".to_string(), json!(start));
        for (key, value) in body.iter() {
            if key != "size" && !value.is_array() {
                out.insert(key.clone(), value.clone());
            }
        }
        for name in &names {
            let rows = shown
                .iter()
                .filter(|(owner, _)| owner == name)
                .map(|(_, row)| row.clone())
                .collect::<Vec<_>>();
            if !rows.is_empty() {
                out.insert(name.clone(), Value::Array(rows));
            }
        }
        rewrite(response, &json!({"MediaContainer": out}));
        return;
    }
    let Some((start, size)) = window else {
        return;
    };
    let mut lists = body
        .iter()
        .filter(|(_, value)| value.is_array())
        .map(|(key, _)| key.clone())
        .collect::<Vec<_>>();
    // A statistics container does NOT slice its Device and Account lookups
    // with the page: the page carries exactly the ones its own rows name.
    let series = lists
        .iter()
        .filter(|key| key.starts_with("Statistics"))
        .cloned()
        .collect::<Vec<_>>();
    let lookups = [("Device", "deviceID"), ("Account", "accountID")];
    let use_series = !series.is_empty() && lookups.iter().any(|(key, _)| lists.iter().any(|name| name == key));
    if use_series {
        lists = series.clone();
    }
    let total = lists
        .iter()
        .filter_map(|key| body.get(key).and_then(Value::as_array).map(Vec::len))
        .max()
        .unwrap_or_else(|| body.get("size").and_then(Value::as_u64).unwrap_or(0) as usize);
    let mut sliced: Map<String, Value> = Map::new();
    for key in &lists {
        let rows = body
            .get(key)
            .and_then(Value::as_array)
            .map(|rows| rows.iter().skip(start).take(size).cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        sliced.insert(key.clone(), Value::Array(rows));
    }
    if use_series {
        for (key, field) in lookups {
            let named = series
                .iter()
                .flat_map(|name| sliced.get(name).and_then(Value::as_array).cloned().unwrap_or_default())
                .filter_map(|row| row.get(field).map(string_value))
                .collect::<HashSet<_>>();
            if let Some(rows) = body.get(key).and_then(Value::as_array) {
                sliced.insert(
                    key.to_string(),
                    Value::Array(
                        rows.iter()
                            .filter(|row| row.get("id").map(string_value).is_some_and(|id| named.contains(&id)))
                            .cloned()
                            .collect(),
                    ),
                );
            }
        }
    }
    let count = if lists.is_empty() {
        body.get("size").and_then(Value::as_u64).unwrap_or(0) as usize
    } else {
        lists
            .iter()
            .filter_map(|key| sliced.get(key).and_then(Value::as_array).map(Vec::len))
            .sum()
    };
    let mut out = Map::new();
    out.insert("size".to_string(), json!(count));
    out.insert("totalSize".to_string(), json!(total));
    out.insert("offset".to_string(), json!(start));
    for (key, value) in body.iter() {
        if key == "size" {
            continue;
        }
        let value = sliced.get(key).cloned().unwrap_or_else(|| value.clone());
        if value.as_array().is_some_and(Vec::is_empty) {
            continue;
        }
        out.insert(key.clone(), value);
    }
    rewrite(response, &json!({"MediaContainer": out}));
}

fn add_paging_headers(response: &mut Response, query: &HashMap<String, String>) {
    if response.status != 200 {
        return;
    }
    let Ok(document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(container) = document.get("MediaContainer") else {
        return;
    };
    let Some(total) = container.get("totalSize").and_then(Value::as_i64) else {
        // Framework containers such as `/system` still expose the paging
        // headers when the requested window is present, but their body omits
        // offset. A zero-size identity response follows that same envelope.
        if query.contains_key("X-Plex-Container-Start")
            && query.contains_key("X-Plex-Container-Size")
            && container.get("size").and_then(Value::as_i64) == Some(0)
        {
            response.headers.push((
                "X-Plex-Container-Start".to_string(),
                query
                    .get("X-Plex-Container-Start")
                    .cloned()
                    .unwrap_or_default(),
            ));
            response
                .headers
                .push(("X-Plex-Container-Total-Size".to_string(), "0".to_string()));
        }
        return;
    };
    let offset = container.get("offset").and_then(Value::as_i64).or_else(|| {
        query
            .get("X-Plex-Container-Start")
            .and_then(|value| value.parse().ok())
    });
    let Some(offset) = offset else {
        return;
    };
    response
        .headers
        .push(("X-Plex-Container-Start".to_string(), offset.to_string()));
    response
        .headers
        .push(("X-Plex-Container-Total-Size".to_string(), total.to_string()));
}

/// Plex's content negotiation recognizes only exact, case-sensitive JSON and
/// XML ranges. Parameterized or otherwise unknown ranges fall back to XML;
/// exact ranges with q weights use the highest weight, then first position.
fn accept_wants_xml(header: &str) -> bool {
    let mut best: Option<(f64, usize, bool)> = None;
    for (position, token) in header.split(',').enumerate() {
        let parts = token.split(';').collect::<Vec<_>>();
        let media = parts.first().map(|part| part.trim()).unwrap_or_default();
        if media != "application/json" && media != "application/xml" {
            continue;
        }
        if parts.len() > 1 && !parts[1].starts_with("q=") {
            continue;
        }
        let mut weight = 1.0;
        if parts.len() > 1 {
            weight = parts[1][2..].parse::<f64>().unwrap_or(0.0);
            if weight < 0.0 || parts.len() > 2 {
                weight = 0.0;
            }
        }
        let is_xml = media == "application/xml";
        let replace = best
            .map(|(old_weight, old_position, _)| {
                weight > old_weight || (weight == old_weight && position < old_position)
            })
            .unwrap_or(true);
        if replace {
            best = Some((weight, position, is_xml));
        }
    }
    best.map(|(_, _, is_xml)| is_xml).unwrap_or(true)
}


/// `excludeFields=` applied to a whole response, as a real PMS applies it.
///
/// Measured: the named fields go from EVERY row -- section listings, `/children`,
/// `/allLeaves`, hubs, search -- and from the container itself (a show's
/// `/children?excludeFields=summary` loses the container's summary too).  Cycle 19:
/// the named field goes from every CHILD ELEMENT of a row too (`id` off `Media`,
/// `Part`, `Stream`, `Genre`), but only the container's own attributes, not its
/// Directory rows.  Applied once, after the route, where Python applies it.
fn exclude_fields_response(request: &Request, response: &mut Response) {
    if !matches!(request.method.as_str(), "GET" | "HEAD")
        || response.content_type != "application/json"
    {
        return;
    }
    let (_, raw_query) = split_target(&request.target);
    let Some(spec) = parse_query(&raw_query)
        .into_iter()
        .rev()
        .find(|(key, _)| key == "excludeFields")
        .map(|(_, value)| value)
    else {
        return;
    };
    let names = spec
        .split(',')
        .filter(|name| !name.is_empty())
        .collect::<HashSet<_>>();
    if names.is_empty() {
        return;
    }
    let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(body) = document
        .get_mut("MediaContainer")
        .and_then(Value::as_object_mut)
    else {
        return;
    };
    for name in &names {
        body.remove(*name);
    }
    if let Some(rows) = body.get_mut("Metadata").and_then(Value::as_array_mut) {
        for row in rows {
            strip_excluded_fields(row, &names);
        }
    }
    if let Some(hubs) = body.get_mut("Hub").and_then(Value::as_array_mut) {
        for hub in hubs {
            if let Some(rows) = hub.get_mut("Metadata").and_then(Value::as_array_mut) {
                for row in rows {
                    strip_excluded_fields(row, &names);
                }
            }
        }
    }
    if let Ok(bytes) = serde_json::to_vec(&document) {
        response.body = bytes;
    }
}

/// `_restamped`: a leaf's borrowed artwork versions replaced by its own `addedAt`.
fn restamp_borrowed(row: &mut Value) {
    let Some(object) = row.as_object_mut() else {
        return;
    };
    let Some(stamp) = object.get("addedAt").filter(|stamp| !stamp.is_null()).map(string_value) else {
        return;
    };
    let mut borrowed = Vec::new();
    for field in ["grandparentThumb", "grandparentArt", "grandparentTheme", "art"] {
        let Some(value) = object.get(field).map(string_value).filter(|value| !value.is_empty()) else {
            continue;
        };
        if let Some((head, _)) = value.rsplit_once('/') {
            let restamped = format!("{head}/{stamp}");
            borrowed.push((value.clone(), restamped.clone()));
            object.insert(field.to_string(), json!(restamped));
        }
    }
    if let Some(images) = object.get_mut("Image").and_then(Value::as_array_mut) {
        for image in images {
            let url = image.get("url").map(string_value).unwrap_or_default();
            if let Some((_, new)) = borrowed.iter().find(|(old, _)| *old == url) {
                if let Some(image) = image.as_object_mut() {
                    image.insert("url".to_string(), json!(new));
                }
            }
        }
    }
}

/// `playQueueLastAddedItemID`: the last item of the Up Next block -- the selected item
/// followed by the contiguous run of ADDED items (cycle 57, both writable copies).
fn queue_block_tail(queue: &Queue) -> Option<i64> {
    if queue.items.is_empty() {
        return None;
    }
    let mut at = queue
        .selected
        .and_then(|selected| queue.items.iter().position(|item| item.id == selected))
        .unwrap_or(0);
    while at + 1 < queue.items.len() && queue.items[at + 1].added {
        at += 1;
    }
    Some(queue.items[at].id)
}

/// Cycle 57, measured on plexscratch42 and plexscratch: under `X-Plex-Sync-Version`
/// (python-plexapi sends it on every request) a playlist row on `/playlists` and
/// `/playlists/{id}`, and the `/playlists/{id}/items` container, carry `allowSync` 1 --
/// though the server's own root says 0.  Nothing else on the surface moves with it.
fn sync_version_response(request: &Request, response: &mut Response) {
    if !matches!(request.method.as_str(), "GET" | "HEAD")
        || response.status != 200
        || response.content_type != "application/json"
    {
        return;
    }
    let (path, raw_query) = split_target(&request.target);
    let segments = path.trim_matches('/').split('/').collect::<Vec<_>>();
    if segments.first() != Some(&"playlists") || segments.len() > 3 {
        return;
    }
    let synced = request.headers.get("x-plex-sync-version").is_some_and(|v| !v.is_empty())
        || parse_query(&raw_query)
            .iter()
            .any(|(name, value)| name == "X-Plex-Sync-Version" && !value.is_empty());
    if !synced {
        return;
    }
    let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(body) = document.get_mut("MediaContainer").and_then(Value::as_object_mut) else {
        return;
    };
    if segments.len() == 3 && segments[2] == "items" {
        body.insert("allowSync".to_string(), json!(true));
    } else if segments.len() <= 2 {
        if let Some(rows) = body.get_mut("Metadata").and_then(Value::as_array_mut) {
            for row in rows {
                if row.get("type").and_then(Value::as_str) != Some("playlist") {
                    continue;
                }
                if let Some(object) = row.as_object_mut() {
                    let mut marked = Map::new();
                    marked.insert("allowSync".to_string(), json!(true));
                    for (key, value) in std::mem::take(object) {
                        if key != "allowSync" {
                            marked.insert(key, value);
                        }
                    }
                    *object = marked;
                }
            }
        }
    } else {
        return;
    }
    if let Ok(bytes) = serde_json::to_vec(&document) {
        response.body = bytes;
    }
}
