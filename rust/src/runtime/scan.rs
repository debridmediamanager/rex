//! The live library: rows a source's items become, on keys that never change.
//!
//! The catalog file used to be the whole library, loaded once at start. A library a source
//! feeds (`feed.rs`) changes while the server runs, and this module is what a real PMS's
//! scanner does with what it finds: each new file becomes a row (identified, its Media
//! from the source's ffprobe), an episode is placed under its show and season, a row
//! added before its file was probed gets its streams later, and a file that is gone is
//! trashed as measured on plexscratch (`tests/fixtures/missing_file_scan_s43.json`). zurg
//! keeps the ratingKey it matched each release to and drops the release's IMDb id when
//! that key disappears, so a row's key is minted once and kept across pushes and restarts.

use super::*;
use crate::identify::{self, Identifier, PlexSource, TmdbSource};
use crate::{hierarchy, mediainfo};

/// Where minted keys start: above the catalog's own ranges (movies and episodes 1000+,
/// shows 20000+, seasons 30000+), playlists (40000+) and collections (50000+).
const SCAN_KEY_BASE: i64 = 100_000;

/// A media file to become a row: its Part `file` and size.
#[derive(Debug, Clone)]
pub(super) struct Found {
    pub file: String,
    pub size: u64,
}

/// The library rows are placed in, and the providers that identify them.
pub(super) struct Library {
    pub key: String,
    pub kind: String,
    pub title: String,
    pub tmdb: Option<Arc<tmdb::Tmdb>>,
    pub plex: Option<Arc<plexmeta::PlexMeta>>,
}

/// A new file and what its source says about its release, ready to become a row.
pub(super) struct Arrival {
    pub found: Found,
    /// The release's name, and the folder its files sit in (a show's `Location`).
    pub release: String,
    pub release_path: String,
    /// `hash`, `imdb_id`, `size` and `media_info`, as zurg's `/api/library/entry` named them.
    pub entry: Value,
    /// Fields the row carries for its source (`_source`, `_sid`, `_path`, `_ids`).
    pub extra: Map<String, Value>,
    /// What identification gave this file's row: TMDB's fields (`metadata::enrich`),
    /// Plex's guids, and how sure (`identify.rs`).
    pub meta: Option<Map<String, Value>>,
}

fn extension(file: &str) -> String {
    file.rsplit_once('.')
        .map(|(_, extension)| extension.to_ascii_lowercase())
        .filter(|extension| (2..=5).contains(&extension.len()))
        .unwrap_or_else(|| "mkv".to_string())
}

fn stem(file: &str) -> &str {
    let name = file.rsplit('/').next().unwrap_or(file);
    name.rsplit_once('.').map_or(name, |(stem, _)| stem)
}

/// Plex's scanners skip a release's sample clip; a separate row titled as the film
/// would be the result here.
pub(super) fn is_sample(file: &str) -> bool {
    stem(file)
        .to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .any(|word| word == "sample")
}

/// (season, episode) of a show's file: the file's own numbers, else its release's; an
/// episode with no season (absolute numbering) is season 1.
fn episode_of(release: &str, file_name: &str) -> Option<(i64, i64)> {
    let release = identify::parse_name(release);
    let file = identify::parse_name(file_name);
    let episode = file.episode.or(release.episode)?;
    Some((file.season.or(release.season).unwrap_or(1), episode))
}

/// Identification, off the lock, once per release: who it is (`identify.rs` -- an IMDb
/// id the source pushed is exact; otherwise its name to Plex's matcher and TMDB, their agreement
/// the confidence), then for each new file the fields TMDB gives its row and Plex's own
/// guids for it. A release named as the other kind of title than its library's, or not
/// at all, stays unmatched for Fix Match.
pub(super) fn name_release(plan: &Library, arrivals: &mut [Arrival], files: usize) {
    let (tmdb, plex) = (plan.tmdb.as_deref(), plan.plex.as_deref());
    let Some(first) = arrivals.first() else {
        return;
    };
    if tmdb.is_none() && plex.is_none() {
        return;
    }
    let hash = first
        .entry
        .get("hash")
        .and_then(Value::as_str)
        .filter(|hash| !hash.is_empty())
        .unwrap_or("zurg")
        .to_ascii_lowercase();
    let mut known = HashMap::new();
    if let Some(imdb) = first.entry.get("imdb_id").and_then(Value::as_str).filter(|id| !id.is_empty()) {
        known.insert(hash.clone(), imdb.to_string());
    }
    let release = first.release.clone();
    // A TMDB id with no IMDb id is read the way a `{tmdb-…}` folder tag is: exact.
    let named = match first.entry.get("tmdb_id").and_then(Value::as_str) {
        Some(id) if known.is_empty() && !id.is_empty() => format!("{release} {{tmdb-{id}}}"),
        _ => release.clone(),
    };
    let identifier = Identifier {
        tmdb: tmdb.map(|t| t as &dyn TmdbSource),
        plex: plex.map(|p| p as &dyn PlexSource),
        known,
    };
    let identity = identifier.identify_as(&named, Some(&hash), files, Some(&plan.kind));
    let report = json!({
        "confidence": identity.confidence, "source": identity.source, "imdb": identity.imdb,
        "tmdb": identity.tmdb, "plex": identity.plex,
        "disputed": identity.disputed.as_ref().map(|d| json!({"tmdb": d.tmdb, "imdb": d.imdb, "kind": d.kind})),
    });
    let matched = identity.kind == plan.kind
        && (identity.tmdb.is_some() || identity.plex.is_some() || identity.imdb.is_some());
    info!(
        "FEED identified {release:?} -> {} {} ({})",
        identity.confidence,
        identity.imdb.as_deref().unwrap_or("?"),
        identity.source.as_deref().unwrap_or("-")
    );
    if !matched {
        for arrival in arrivals.iter_mut() {
            arrival.meta = Some(Map::from_iter([("_identify".to_string(), report.clone())]));
        }
        return;
    }
    let show = plan.kind == "show";
    let tmdb_kind = if show { "tv" } else { "movie" };
    let details = tmdb.zip(identity.tmdb).and_then(|(t, id)| identify::tmdb_details(t, tmdb_kind, id));
    let externals: Vec<Value> = identity
        .imdb
        .iter()
        .map(|imdb| json!({"id": format!("imdb://{imdb}")}))
        .chain(identity.tmdb.iter().map(|id| json!({"id": format!("tmdb://{id}")})))
        .collect();
    let seasons = match (show, plex, identity.plex.as_deref()) {
        (true, Some(p), Some(guid)) => identify::plex_children(p, guid),
        _ => HashMap::new(),
    };
    let mut episodes: HashMap<String, HashMap<String, String>> = HashMap::new();
    for arrival in arrivals.iter_mut() {
        let file_name = arrival.found.file.rsplit('/').next().unwrap_or_default().to_string();
        let numbers = if show { episode_of(&release, &file_name) } else { None };
        let mut meta = Map::new();
        meta.insert("title".to_string(), json!(identity.parsed.title));
        meta.insert("summary".to_string(), json!(""));
        if let (Some(details), Some(id), Some(t)) = (&details, identity.tmdb, plan.tmdb.as_deref()) {
            let parsed = Map::from_iter([
                ("title".to_string(), json!(identity.parsed.title)),
                ("year".to_string(), json!(identity.parsed.year)),
                ("season".to_string(), json!(numbers.map(|n| n.0))),
                ("episode".to_string(), json!(numbers.map(|n| n.1))),
            ]);
            let (episode, season) = match numbers {
                Some((season, episode)) => (
                    t.episode(&json!(id), &json!(season), &json!(episode)),
                    t.season(&json!(id), &json!(season)),
                ),
                None => (None, None),
            };
            metadata::enrich(
                &mut meta,
                details,
                &parsed,
                episode.as_ref(),
                Some(season.as_ref().unwrap_or(&json!({}))),
                tmdb_kind,
                id,
            );
        } else if !externals.is_empty() {
            // Named, but no TMDB to describe it: the ids alone.
            let field = if show { "_show_external_guids" } else { "Guid" };
            meta.insert(field.to_string(), Value::Array(externals.clone()));
        }
        if let Some(year) = identity.parsed.year.filter(|_| !meta.contains_key("year") && !show) {
            meta.insert("year".to_string(), json!(year));
        }
        let identity_key = identity
            .tmdb
            .map(|id| format!("tmdb:{id}"))
            .or_else(|| identity.imdb.as_ref().map(|imdb| format!("imdb:{imdb}")))
            .or_else(|| identity.plex.clone())
            .unwrap_or_default();
        meta.insert("_identity".to_string(), json!(identity_key));
        if show {
            meta.insert("_tmdb_id".to_string(), json!(identity_key));
        }
        // Plex's own guids, which a real PMS's rows carry and plex.tv knows (cycle 70).
        if let Some(guid) = &identity.plex {
            if !show {
                meta.insert("guid".to_string(), json!(guid));
            } else {
                meta.insert("_show_guid".to_string(), json!(guid));
                if let Some((season, episode)) = numbers {
                    if let Some(season_guid) = seasons.get(&season.to_string()) {
                        meta.insert("_season_guid".to_string(), json!(season_guid));
                        let children = episodes.entry(season_guid.clone()).or_insert_with(|| {
                            plex.map(|p| identify::plex_children(p, season_guid)).unwrap_or_default()
                        });
                        if let Some(episode_guid) = children.get(&episode.to_string()) {
                            meta.insert("guid".to_string(), json!(episode_guid));
                        }
                    }
                }
            }
        }
        meta.insert("_identify".to_string(), report.clone());
        arrival.meta = Some(meta);
    }
}

pub(super) fn under(file: &str, folder: &str) -> bool {
    let folder = folder.trim_end_matches('/');
    file == folder || file.starts_with(&format!("{folder}/"))
}

impl Runtime {
    /// After the state is restored: the next minted key sits above every key in use or
    /// retired, whatever the saved counter says.
    pub(super) fn settle_scan_key(&mut self) {
        let highest = self
            .catalog
            .items()
            .iter()
            .filter_map(|row| key_of(row).and_then(|key| key.parse::<i64>().ok()))
            .chain(self.retired_keys.iter().copied())
            .max()
            .unwrap_or(0);
        self.next_scan_key = self.next_scan_key.max(highest + 1).max(SCAN_KEY_BASE);
    }

    /// A ratingKey no row has ever had.
    pub(super) fn mint_key(&mut self) -> String {
        let key = self.next_scan_key;
        self.next_scan_key += 1;
        self.dirty = true;
        key.to_string()
    }

    /// A library's folders, as its `Location` rows name them.
    pub(super) fn locations(&self, section: &Value) -> Vec<String> {
        section
            .get("Location")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .map(|row| string_value(row.get("path").unwrap_or(&Value::Null)))
                    .filter(|path| !path.is_empty())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// `autoEmptyTrash`, as `/:/prefs` reports it: a write, else the captured value.
    fn auto_empty_trash(&self) -> bool {
        let value = self.prefs.get("autoEmptyTrash").cloned().or_else(|| {
            load_json_sidecar(&self.catalog_path, "server_prefs.json")
                .and_then(|rows| rows.as_array().cloned())
                .and_then(|rows| {
                    rows.into_iter()
                        .find(|row| row.get("id").and_then(Value::as_str) == Some("autoEmptyTrash"))
                })
                .and_then(|row| row.get("value").cloned())
        });
        match value {
            Some(Value::Bool(on)) => on,
            Some(Value::String(text)) => matches!(text.as_str(), "1" | "true"),
            Some(Value::Number(number)) => number.as_i64() == Some(1),
            _ => false,
        }
    }

    /// The scan's opening frames, as `notify_section_scan` writes them.
    pub(super) fn scan_started(&mut self, section: &str, title: &str) -> Map<String, Value> {
        let mut activity = butler::new_activity("library.update.section", "Scanning", false);
        self.notify_activity(&activity, "started");
        self.notify_status(&format!("Scanning the \"{title}\" section"), "");
        activity.insert("title".to_string(), json!(format!("Scanning {title}")));
        self.notify_activity(&activity, "updated");
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        activity
    }

    /// Measured (6a2): a folder scan's steps name the release folder, at 0 then 99.
    pub(super) fn scan_progress(&mut self, activity: &mut Map<String, Value>, section: &str, subtitle: &str, progress: i64) {
        activity.insert("subtitle".to_string(), json!(subtitle));
        activity.insert("progress".to_string(), json!(progress));
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        self.notify_activity(activity, "updated");
    }

    pub(super) fn scan_finished(&mut self, activity: &mut Map<String, Value>, section: &str) {
        activity.insert("progress".to_string(), json!(100));
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        self.notify_activity(activity, "updated");
        let mut outer = butler::new_activity(
            "provider.subscriptions.process",
            "Processing subscriptions",
            true,
        );
        self.notify_activity(&outer, "started");
        self.notify_activity(activity, "ended");
        self.notify_status(
            "Library scan complete",
            "Extra information may still be downloading from the Internet",
        );
        let mut inner = outer.clone();
        inner.insert("uuid".to_string(), json!(butler::activity_uuid()));
        self.notify_activity(&inner, "started");
        inner.insert("progress".to_string(), json!(100));
        self.notify_activity(&inner, "ended");
        outer.insert("progress".to_string(), json!(100));
        self.notify_activity(&outer, "ended");
    }

    /// New files become rows: keys minted, Media built from the source's probe, episodes
    /// placed under the show and season they belong to (existing ones kept, by identity).
    /// Answers the keys of every row created, parents first.
    pub(super) fn place_arrivals(&mut self, plan: &Library, arrivals: Vec<Arrival>) -> Vec<String> {
        let taken: HashSet<String> = self.catalog.items().iter().flat_map(row_files).collect();
        let mut leaves = Vec::new();
        for arrival in arrivals {
            // A file another row already reads.
            if taken.contains(&arrival.found.file) {
                continue;
            }
            match self.leaf_for(plan, &arrival, None) {
                Some(leaf) => leaves.push(leaf),
                None => warning!("FEED {} has no episode number; not placed", arrival.found.file),
            }
        }
        self.place_leaves(plan, leaves, &HashSet::new())
    }

    /// Leaf rows into the library: a show's episode under the show of its identity and the
    /// season of its index, each found or made. `quiet` names leaves that are not new
    /// (a re-identified row keeps its key): they get no creation frames.
    pub(super) fn place_leaves(&mut self, plan: &Library, leaves: Vec<Value>, quiet: &HashSet<String>) -> Vec<String> {
        if leaves.is_empty() {
            return Vec::new();
        }
        let mut rows = hierarchy::build(leaves);
        // `build` numbers new shows and seasons from its own bases; each becomes the
        // library's existing show (same identity) or season (same show and index), or
        // takes a fresh key.
        let mut rekeyed: HashMap<String, String> = HashMap::new();
        let mut existing: HashSet<String> = HashSet::new();
        let identity_of = |rows: &[Value], show: &str| {
            rows.iter()
                .find(|row| row.get("grandparentRatingKey").map(string_value).as_deref() == Some(show))
                .and_then(|row| row.get("_identity").cloned())
        };
        for index in 0..rows.len() {
            if rows[index].get("type").and_then(Value::as_str) != Some("show") {
                continue;
            }
            let temporary = key_of(&rows[index]).unwrap_or_default();
            let identity = identity_of(&rows, &temporary).unwrap_or(Value::Null);
            let found = self.catalog.items().iter().find(|row| {
                row.get("type").and_then(Value::as_str) == Some("show")
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == plan.key
                    && row.get("_identity") == Some(&identity)
            });
            let key = match found.and_then(key_of) {
                Some(key) => {
                    existing.insert(key.clone());
                    key
                }
                None => self.mint_key(),
            };
            rows[index]["_identity"] = identity;
            rekeyed.insert(temporary, key);
        }
        for index in 0..rows.len() {
            if rows[index].get("type").and_then(Value::as_str) != Some("season") {
                continue;
            }
            let temporary = key_of(&rows[index]).unwrap_or_default();
            let show = rows[index].get("parentRatingKey").map(string_value).unwrap_or_default();
            let show = rekeyed.get(&show).cloned().unwrap_or(show);
            let season_index = rows[index].get("index").and_then(Value::as_i64);
            let found = self.catalog.items().iter().find(|row| {
                row.get("type").and_then(Value::as_str) == Some("season")
                    && row.get("parentRatingKey").map(string_value).as_deref() == Some(show.as_str())
                    && row.get("index").and_then(Value::as_i64) == season_index
            });
            let key = match found.and_then(key_of) {
                Some(key) => {
                    existing.insert(key.clone());
                    key
                }
                None => self.mint_key(),
            };
            rekeyed.insert(temporary, key);
        }
        rekey(&mut rows, &rekeyed);
        let section_id = plan.key.parse::<i64>().map(Value::from).unwrap_or_else(|_| json!(plan.key));
        let mut created = Vec::new();
        let mut episodes = Vec::new();
        for mut row in rows {
            let key = key_of(&row).unwrap_or_default();
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
            if let Some(object) = row.as_object_mut() {
                object.insert("librarySectionID".to_string(), section_id.clone());
                object.insert("librarySectionTitle".to_string(), json!(plan.title));
                object.insert(
                    "librarySectionKey".to_string(),
                    json!(format!("/library/sections/{}", plan.key)),
                );
                object.insert("_scanned".to_string(), json!(true));
                // A show and season nobody identified: the local agent's guid, on their
                // own keys (the episode's was copied up by `build`).
                if matches!(kind.as_str(), "show" | "season")
                    && object.get("guid").and_then(Value::as_str).map_or(true, |guid| guid.starts_with("local://"))
                {
                    object.insert("guid".to_string(), json!(format!("local://{key}")));
                }
                if kind == "show" {
                    // A real PMS's show Location is the show's folder; here, the
                    // release folder each episode came from.
                    object.insert("Location".to_string(), json!([]));
                }
            }
            if existing.contains(&key) {
                continue;
            }
            if kind == "episode" {
                episodes.push(key.clone());
            }
            prepare_rows(std::slice::from_mut(&mut row));
            self.catalog.add(row);
            created.push(key);
        }
        for key in &episodes {
            self.inherit_parents(key);
        }
        for key in created.iter().filter(|key| !quiet.contains(*key)) {
            self.push_created_frames(key);
        }
        created
    }

    /// The leaf row a new file becomes, on `key` or a minted one; `None` for a show's file
    /// with no episode.
    pub(super) fn leaf_for(&mut self, plan: &Library, arrival: &Arrival, key: Option<String>) -> Option<Value> {
        let file_name = arrival.found.file.rsplit('/').next().unwrap_or_default().to_string();
        let release = identify::parse_name(&arrival.release);
        let (season, episode) = if plan.kind == "show" {
            episode_of(&arrival.release, &file_name)?
        } else {
            (0, 0)
        };
        let key = key.unwrap_or_else(|| self.mint_key());
        let rating = key.parse::<i64>().unwrap_or(0);
        let stamp = now();
        let extension = extension(&file_name);
        let part_id = rating * 10;
        let probe = arrival
            .entry
            .get("media_info")
            .filter(|probe| probe.is_object())
            .cloned();
        let size = arrival
            .entry
            .get("size")
            .and_then(Value::as_u64)
            .filter(|size| *size > 0)
            .unwrap_or(arrival.found.size);
        let media = mediainfo::build_media(
            probe.as_ref().unwrap_or(&json!({"format": {}, "streams": []})),
            rating * 100,
            part_id,
            &format!("/library/parts/{part_id}/{stamp}/file.{extension}"),
            &arrival.found.file,
            size as i64,
            Some(&extension),
        );
        let title = if release.title.is_empty() { stem(&file_name).to_string() } else { release.title.clone() };
        let mut leaf = json!({
            "ratingKey": key,
            "key": format!("/library/metadata/{key}"),
            "guid": format!("local://{key}"),
            "type": plan.kind.clone(),
            "title": title,
            "summary": "",
            "duration": media.get("duration").cloned().unwrap_or(json!(0)),
            "addedAt": stamp,
            "updatedAt": stamp,
            "thumb": format!("/library/metadata/{key}/thumb/{stamp}"),
            "art": format!("/library/metadata/{key}/art/{stamp}"),
            "Media": [media],
            "_identity": format!("title:{}", hierarchy::fold_title(&release.title).to_lowercase()),
            "_release": arrival.release.clone(),
        });
        if let Some(year) = release.year {
            leaf["year"] = json!(year);
        }
        if probe.is_none() {
            leaf["_probePending"] = json!(true);
        }
        if let Some(hash) = arrival.entry.get("hash").and_then(Value::as_str) {
            leaf["_hash"] = json!(hash);
        }
        for (field, value) in &arrival.extra {
            leaf[field.as_str()] = value.clone();
        }
        if plan.kind == "show" {
            leaf["_kind"] = json!("tv");
            leaf["_tmdb_id"] = leaf["_identity"].clone();
            leaf["_season"] = json!(season);
            leaf["_episode"] = json!(episode);
            leaf["_show_title"] = json!(leaf["title"].clone());
            leaf["_show_summary"] = json!("");
            if let Some(year) = release.year {
                leaf["_show_year"] = json!(year);
            }
            // The local agent's episode: its generated title, marked as generated so an
            // index edit re-titles it (`unmatch_row`).
            leaf["title"] = json!(format!("Episode {episode}"));
            leaf["_generatedTitle"] = json!(true);
            leaf.as_object_mut()?.shift_remove("year");
            leaf["_release_path"] = json!(arrival.release_path.clone());
        }
        if let Some(meta) = &arrival.meta {
            for (field, value) in meta {
                leaf[field.as_str()] = value.clone();
            }
            // TMDB named the show but not this episode: `enrich` left the SHOW's title
            // on it, so it keeps its generated one.
            if plan.kind == "show" && !meta.contains_key("index") {
                leaf["title"] = json!(format!("Episode {episode}"));
            } else if let Some(object) = leaf.as_object_mut() {
                object.shift_remove("_generatedTitle");
            }
        }
        Some(leaf)
    }

    /// An episode's parent fields, read off the rows it now sits under.
    fn inherit_parents(&mut self, key: &str) {
        let Some(episode) = self.catalog.get(key).cloned() else {
            return;
        };
        let season_key = episode.get("parentRatingKey").map(string_value).unwrap_or_default();
        let Some(season) = self.catalog.get(&season_key).cloned() else {
            return;
        };
        let show_key = season.get("parentRatingKey").map(string_value).unwrap_or_default();
        let Some(show) = self.catalog.get(&show_key).cloned() else {
            return;
        };
        let release_path = episode.get("_release_path").cloned();
        if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in [
                ("parentRatingKey", json!(season_key)),
                ("parentKey", json!(format!("/library/metadata/{season_key}"))),
                ("parentTitle", season.get("title").cloned().unwrap_or(Value::Null)),
                ("parentIndex", season.get("index").cloned().unwrap_or(Value::Null)),
                ("parentGuid", season.get("guid").cloned().unwrap_or(Value::Null)),
                ("parentThumb", season.get("thumb").cloned().unwrap_or(Value::Null)),
                ("grandparentRatingKey", json!(show_key)),
                ("grandparentKey", json!(format!("/library/metadata/{show_key}"))),
                ("grandparentTitle", show.get("title").cloned().unwrap_or(Value::Null)),
                ("grandparentGuid", show.get("guid").cloned().unwrap_or(Value::Null)),
                ("grandparentThumb", show.get("thumb").cloned().unwrap_or(Value::Null)),
                ("grandparentArt", show.get("art").cloned().unwrap_or(Value::Null)),
                ("grandparentSlug", show.get("slug").cloned().unwrap_or(Value::Null)),
            ] {
                if value.is_null() {
                    row.shift_remove(field);
                } else {
                    row.insert(field.to_string(), value);
                }
            }
            row.shift_remove("_release_path");
        }
        if let (Some(path), Some(show)) = (release_path, self.catalog.get_mut(&show_key)) {
            let locations = show
                .as_object_mut()
                .map(|object| object.entry("Location").or_insert_with(|| json!([])));
            if let Some(Value::Array(rows)) = locations {
                if !rows.iter().any(|row| row.get("path") == Some(&path)) {
                    rows.push(json!({"path": path}));
                }
            }
        }
    }

    /// Measured (4a/4b): a row a scan creates is announced `state 0` with an empty title,
    /// then `state 1` created, analysing and thumbnailing, then `state 1` again; an
    /// episode's first frame also names its season and show.
    fn push_created_frames(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let parents = (row.get("type").and_then(Value::as_str) == Some("episode")).then(|| {
            (
                row.get("parentRatingKey").map(string_value).unwrap_or_default(),
                row.get("grandparentRatingKey").map(string_value).unwrap_or_default(),
            )
        });
        let episode = parents.is_some();
        self.push_timeline_entry_with(&row, 0, Some("created"), None, None, Some(""), parents);
        let media_states: &[Option<&str>] = if episode {
            &[None, Some("analyzing"), None]
        } else {
            &[None, Some("analyzing"), Some("thumbnailing"), None]
        };
        for media_state in media_states {
            self.push_timeline_entry(&row, 1, Some("created"), *media_state, None, None);
        }
    }

    /// Measured (4a): after the scan, a new row's metadata pass -- the `Updating
    /// Metadata` activity around processing, matching, queued and loading frames.
    pub(super) fn created_metadata_frames(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        if !matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")) {
            return;
        }
        let mut activity = self.start_metadata_activity(&self.frame_title(&row));
        activity.insert("progress".to_string(), json!(49));
        self.notify_activity(&activity, "updated");
        self.push_timeline_entry(&row, 1, Some("processing"), None, None, None);
        self.push_timeline_entry(&row, 2, Some("matching"), None, None, None);
        self.push_timeline_entry(&row, 5, None, None, None, None);
        self.push_timeline_entry(&row, 3, Some("queued"), None, None, None);
        self.push_timeline_entry(&row, 4, Some("loading"), None, None, None);
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.push_timeline_entry(&row, 5, None, None, None, None);
    }

    /// Rows added before their file was probed get their Media from the probe, keeping
    /// their ids.
    pub(super) fn fill_probes(&mut self, probed: Vec<(String, Value)>) -> usize {
        let mut filled = 0;
        for (key, probe) in probed {
            let Some(row) = self.catalog.get(&key).cloned() else {
                continue;
            };
            let Some(old) = row.get("Media").and_then(Value::as_array).and_then(|media| media.first()).cloned() else {
                continue;
            };
            let part = old.get("Part").and_then(Value::as_array).and_then(|parts| parts.first()).cloned().unwrap_or_default();
            let file = part.get("file").and_then(Value::as_str).unwrap_or_default().to_string();
            let media = mediainfo::build_media(
                &probe,
                old.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("key").and_then(Value::as_str).unwrap_or_default(),
                &file,
                part.get("size").and_then(Value::as_i64).unwrap_or(0),
                Some(&extension(&file)),
            );
            if let Some(object) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                object.insert("duration".to_string(), media.get("duration").cloned().unwrap_or(json!(0)));
                object.insert("Media".to_string(), json!([media]));
                object.shift_remove("_probePending");
            }
            if let Some(row) = self.catalog.get(&key).cloned() {
                self.push_timeline_entry(&row, 5, None, None, None, None);
            }
            filled += 1;
        }
        filled
    }

    /// A file the source no longer has: to the trash, or deleted at once under
    /// `autoEmptyTrash` (measured, 5a: one `state 9` frame and nothing else). Answers
    /// whether it was deleted rather than trashed; a row already in the trash is left.
    pub(super) fn remove_missing(&mut self, key: &str) -> Option<bool> {
        if self.catalog.get(key)?.get("deletedAt").is_some() {
            return None;
        }
        if self.auto_empty_trash() {
            self.purge_row(key);
            Some(true)
        } else {
            self.trash_row(key);
            Some(false)
        }
    }

    /// Measured (1, 2): a missing file soft-deletes its row -- `deletedAt` on the row and
    /// its Media (never the Part), and on an episode's season and show too, counts
    /// unchanged -- each announced as a `type -1` state 3 then state 5 pair.
    pub(super) fn trash_row(&mut self, key: &str) {
        let stamp = now();
        let mut chain = vec![key.to_string()];
        if let Some(row) = self.catalog.get(key) {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                chain.extend(row.get("parentRatingKey").map(string_value));
                chain.extend(row.get("grandparentRatingKey").map(string_value));
            }
        }
        for (index, member) in chain.iter().enumerate() {
            let Some(object) = self.catalog.get_mut(member).and_then(Value::as_object_mut) else {
                continue;
            };
            object.insert("deletedAt".to_string(), json!(stamp));
            if index == 0 {
                for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                    if let Some(media) = media.as_object_mut() {
                        media.insert("deletedAt".to_string(), json!(stamp));
                    }
                }
            }
            self.deletion_pair(member);
        }
        info!("FEED trashed {key}");
    }

    /// Measured (6a2): the file back before the trash was emptied restores the SAME row
    /// -- key, Media and guid -- with the same `type -1` pair. Its season and show leave
    /// the trash once none of their episodes is in it (inferred: not measured).
    pub(super) fn untrash_row(&mut self, key: &str) {
        let mut chain = vec![key.to_string()];
        if let Some(row) = self.catalog.get(key) {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                chain.extend(row.get("parentRatingKey").map(string_value));
                chain.extend(row.get("grandparentRatingKey").map(string_value));
            }
        }
        for (index, member) in chain.iter().enumerate() {
            if index > 0 && self.leaves(member).iter().any(|leaf| leaf.get("deletedAt").is_some() && key_of(leaf).as_deref() != Some(key)) {
                continue;
            }
            let Some(object) = self.catalog.get_mut(member).and_then(Value::as_object_mut) else {
                continue;
            };
            if object.shift_remove("deletedAt").is_none() {
                continue;
            }
            for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                if let Some(media) = media.as_object_mut() {
                    media.shift_remove("deletedAt");
                }
            }
            self.deletion_pair(member);
        }
        info!("FEED restored {key}");
    }

    fn deletion_pair(&mut self, key: &str) {
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 3, None, None, Some(-1), None);
            self.push_timeline_entry(&row, 5, None, None, Some(-1), None);
        }
    }

    /// A trashed leaf deleted for good: the row goes (and an episode's season with its
    /// last episode, as a DELETE takes it; a show with its last season, inferred), its key
    /// retired, announced as the row's own type in `state 9`.
    pub(super) fn purge_row(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let parents = (row.get("type").and_then(Value::as_str) == Some("episode")).then(|| {
            (
                row.get("parentRatingKey").map(string_value).unwrap_or_default(),
                row.get("grandparentRatingKey").map(string_value).unwrap_or_default(),
            )
        });
        let mut doomed = vec![key.to_string()];
        if let Some((season, show)) = &parents {
            if self.children(season).len() <= 1 {
                doomed.push(season.clone());
                if self.children(show).len() <= 1 {
                    doomed.push(show.clone());
                }
            }
        }
        for gone in &doomed {
            if self.catalog.remove(gone).is_some() {
                self.deleted.insert(gone.clone());
                if let Ok(number) = gone.parse::<i64>() {
                    self.retired_keys.push(number);
                }
            }
        }
        self.drop_from_queues(&doomed);
        self.push_timeline_entry_with(&row, 9, Some("deleted"), None, None, None, parents);
        info!("FEED deleted {key}");
    }

    /// `PUT /library/sections/{id}/emptyTrash`: every trashed leaf deleted. Measured
    /// (3a/3b): a show's season and show leave the trash first (their `type -1` pairs),
    /// then each episode's `state 9` names its season and show.
    pub(super) fn empty_section_trash(&mut self, section: &str) -> usize {
        let trashed: Vec<Value> = self
            .catalog
            .items()
            .iter()
            .filter(|row| {
                string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
                    && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
                    && row.get("deletedAt").is_some()
            })
            .cloned()
            .collect();
        for row in &trashed {
            for parent in ["parentRatingKey", "grandparentRatingKey"] {
                let Some(key) = row.get(parent).map(string_value) else {
                    continue;
                };
                let cleared = self
                    .catalog
                    .get_mut(&key)
                    .and_then(Value::as_object_mut)
                    .and_then(|object| object.shift_remove("deletedAt"))
                    .is_some();
                if cleared {
                    self.deletion_pair(&key);
                }
            }
        }
        for row in &trashed {
            if let Some(key) = key_of(row) {
                self.purge_row(&key);
            }
        }
        if !trashed.is_empty() {
            self.dirty = true;
        }
        trashed.len()
    }
}

/// Every file a row's Parts read.
pub(super) fn row_files(row: &Value) -> Vec<String> {
    row.get("Media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
        .filter_map(|part| part.get("file").and_then(Value::as_str).map(str::to_string))
        .collect()
}

/// `build`'s temporary show and season keys replaced, in the key fields and in every
/// `/library/metadata/<key>` path built from them.
fn rekey(rows: &mut [Value], keys: &HashMap<String, String>) {
    for row in rows {
        let Some(object) = row.as_object_mut() else {
            continue;
        };
        for field in ["ratingKey", "parentRatingKey", "grandparentRatingKey"] {
            if let Some(new) = object.get(field).map(string_value).and_then(|old| keys.get(&old)) {
                object.insert(field.to_string(), json!(new));
            }
        }
        for value in object.values_mut() {
            let Value::String(text) = value else {
                continue;
            };
            let Some(rest) = text.strip_prefix("/library/metadata/") else {
                continue;
            };
            let (head, tail) = rest.split_once('/').unwrap_or((rest, ""));
            if let Some(new) = keys.get(head) {
                *text = if tail.is_empty() {
                    format!("/library/metadata/{new}")
                } else {
                    format!("/library/metadata/{new}/{tail}")
                };
            }
        }
    }
}

/// The load-time corrections every catalog row gets (`Runtime::load`), for rows a scan
/// adds: tag ids, no show-level fields on an episode, no stored file-check pair.
fn prepare_rows(rows: &mut [Value]) {
    number_catalog_tags(rows);
    for row in rows.iter_mut() {
        let episode = row.get("type").and_then(Value::as_str) == Some("episode");
        let Some(object) = row.as_object_mut() else {
            continue;
        };
        if episode {
            for field in ["Genre", "Country", "studio", "tagline", "originalTitle", "rating", "Label"] {
                object.shift_remove(field);
            }
        }
        for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
            for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                if let Some(part) = part.as_object_mut() {
                    part.shift_remove("accessible");
                    part.shift_remove("exists");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn options(dir: &Path, catalog: &Path) -> RuntimeOptions {
        RuntimeOptions {
            catalog: catalog.to_path_buf(),
            state: Some(dir.join("rex.db")),
            ..RuntimeOptions::default()
        }
    }

    fn seed(name: &str) -> Value {
        json!({"machineIdentifier": "0000000000000000000000000000000000000077",
               "friendlyName": name,
               "sections": [{"key": "1", "type": "movie", "title": "Movies",
                             "uuid": "00000000000000000000000000000000000000aa"}],
               "items": [{"ratingKey": "1001", "key": "/library/metadata/1001",
                          "type": "movie", "title": "Seeded", "librarySectionID": 1,
                          "guid": "plex://movie/000000000000000000000001",
                          "addedAt": 1700000000, "updatedAt": 1700000000}]})
    }

    fn scanned_row(key: &str) -> Value {
        json!({"ratingKey": key, "key": format!("/library/metadata/{key}"), "type": "movie",
               "title": "Scanned", "librarySectionID": 1, "_scanned": true,
               "guid": "plex://movie/000000000000000000000002",
               "addedAt": 1700000100, "updatedAt": 1700000100})
    }

    /// One test, because the store's write queue is process-wide.
    #[test]
    fn a_snapshot_after_one_held_change_does_not_reserialise_every_held_item() {
        // fun, 2026-10-10: 28,859 held items, every one serialised, hashed and compared
        // on each save -- two seconds apart while any client polled -- under the runtime
        // lock every request waits on. One real held record stands in for all of them.
        let fixture: Value =
            serde_json::from_str(include_str!("../../tests/fixtures/feed_held_record.json")).unwrap();
        let record = fixture["record"].clone();
        let dir = std::env::temp_dir().join(format!("rex-held-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        fs::write(&catalog, seed("held").to_string()).unwrap();

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        for n in 0..28_859 {
            runtime.hold(format!("rd:held/{n}"), record.clone());
        }
        runtime.save_state().unwrap();
        let mut slowest = std::time::Duration::ZERO;
        for n in 0..10 {
            runtime.hold(format!("rd:new/{n}"), record.clone());
            runtime.release(&format!("rd:held/{n}"));
            let started = std::time::Instant::now();
            let snapshot = runtime.snapshot_state().unwrap();
            slowest = slowest.max(started.elapsed());
            write_snapshot(&snapshot.expect("a held change is a change")).unwrap();
        }
        // The unfixed pass took 1.2 s here (a debug build); the keyed one about 11 ms.
        assert!(slowest < std::time::Duration::from_millis(100), "a one-item snapshot took {slowest:?}");
        drop(runtime);

        let runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        assert_eq!(runtime.feed_held.len(), 28_859);
        assert_eq!(runtime.feed_held.get("rd:new/9"), Some(&record));
        assert!(!runtime.feed_held.contains_key("rd:held/0"));
        drop(runtime);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_runtime_loaded_after_another_has_saved_keeps_its_own_writes() {
        // The save serials were per runtime and the written mark per process: a second
        // runtime's first saves counted as written and were dropped. The test suite runs
        // several runtimes in one process; this one failed whenever another saved first.
        let base = std::env::temp_dir().join(format!("rex-two-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let (first_dir, second_dir) = (base.join("first"), base.join("second"));
        for dir in [&first_dir, &second_dir] {
            fs::create_dir_all(dir).unwrap();
            fs::write(dir.join("catalog.json"), seed("two").to_string()).unwrap();
        }
        let mut first = Runtime::load(options(&first_dir, &first_dir.join("catalog.json"))).unwrap();
        for _ in 0..3 {
            let key = first.mint_key();
            first.catalog.add(scanned_row(&key));
            first.save_state().unwrap();
        }
        drop(first);

        let mut second = Runtime::load(options(&second_dir, &second_dir.join("catalog.json"))).unwrap();
        let key = second.mint_key();
        second.catalog.add(scanned_row(&key));
        second.save_state().unwrap();
        drop(second);
        let second = Runtime::load(options(&second_dir, &second_dir.join("catalog.json"))).unwrap();
        assert!(second.catalog.get(&key).is_some(), "the second runtime's save was dropped");
        drop(second);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn two_runtimes_saving_at_once_each_write_their_own_file() {
        // The save queue is the process's: one runtime's flush drained another's queued
        // delta into its own file and marked it done, so the other's flush wrote nothing.
        // The unit tests run runtimes on parallel threads; Windows CI lost a row this way.
        let base = std::env::temp_dir().join(format!("rex-both-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let dirs = [base.join("a"), base.join("b")];
        for dir in &dirs {
            fs::create_dir_all(dir).unwrap();
            fs::write(dir.join("catalog.json"), seed("both").to_string()).unwrap();
        }
        let mut a = Runtime::load(options(&dirs[0], &dirs[0].join("catalog.json"))).unwrap();
        let mut b = Runtime::load(options(&dirs[1], &dirs[1].join("catalog.json"))).unwrap();
        let (key_a, key_b) = (a.mint_key(), b.mint_key());
        a.catalog.add(scanned_row(&key_a));
        b.catalog.add(scanned_row(&key_b));
        let saved_a = a.snapshot_state().unwrap().expect("a change");
        let saved_b = b.snapshot_state().unwrap().expect("a change");
        write_snapshot(&saved_a).unwrap();
        write_snapshot(&saved_b).unwrap();
        drop((a, b));
        let a = Runtime::load(options(&dirs[0], &dirs[0].join("catalog.json"))).unwrap();
        let b = Runtime::load(options(&dirs[1], &dirs[1].join("catalog.json"))).unwrap();
        assert!(a.catalog.get(&key_a).is_some(), "a's save is missing from a's file");
        assert!(b.catalog.get(&key_b).is_some(), "b's save is missing from b's file");
        drop((a, b));
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn scanned_rows_keep_their_keys_and_state_across_restarts_and_catalog_edits() {
        let dir = std::env::temp_dir().join(format!("rex-scan-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        fs::write(&catalog, seed("one").to_string()).unwrap();

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        let first = runtime.mint_key();
        assert!(first.parse::<i64>().unwrap() >= SCAN_KEY_BASE);
        runtime.catalog.add(scanned_row(&first));
        let second = runtime.mint_key();
        runtime.catalog.add(scanned_row(&second));
        if let Some(row) = runtime.catalog.get_mut(&first).and_then(Value::as_object_mut) {
            row.insert("viewCount".to_string(), json!(2));
        }
        // A row removed by a scan: its key is never minted again.
        runtime.catalog.remove(&second);
        runtime.save_state().unwrap();
        drop(runtime);

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        let row = runtime.catalog.get(&first).expect("the scanned row survives a restart");
        assert_eq!(row["viewCount"], 2);
        assert!(runtime.catalog.get(&second).is_none());
        let third = runtime.mint_key();
        assert!(third.parse::<i64>().unwrap() > second.parse::<i64>().unwrap());
        runtime.save_state().unwrap();
        drop(runtime);

        // The catalog FILE changes (a new seeded row, a rename): a scanned library is
        // kept by row, not set aside as a rebuilt catalog's state is.
        let mut edited = seed("two");
        edited["items"]
            .as_array_mut()
            .unwrap()
            .push(json!({"ratingKey": "1002", "type": "movie", "title": "Also seeded",
                         "librarySectionID": 1, "addedAt": 1700000200}));
        fs::write(&catalog, edited.to_string()).unwrap();
        let runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        assert_eq!(runtime.catalog.get(&first).unwrap()["viewCount"], 2);
        assert!(runtime.catalog.get("1001").is_some());
        assert!(runtime.catalog.get("1002").is_some(), "the file's new row joins");
        assert!(!dir.join("rex.db.stale").exists());
        drop(runtime);
        let _ = fs::remove_dir_all(&dir);
    }

    /// The identification pass on zurg's two real entries (`fixtures/zurg/entries.json`),
    /// offline: TMDB's and Plex's answers sit in their on-disk caches, which is where a
    /// live lookup would have put them. The answers are written for the test.
    #[test]
    fn an_imdb_id_from_zurg_names_the_release_and_plex_names_its_guids() {
        let dir = std::env::temp_dir().join(format!("rex-name-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let api = "https://api.themoviedb.org/3";
        let details = "append_to_response=credits%2Cexternal_ids%2Ccontent_ratings%2Crelease_dates";
        let tmdb_cache = json!({
            format!("{api}/find/tt0103827?external_source=imdb_id&api_key=KEY"):
                {"movie_results": [{"id": 31639}], "tv_results": []},
            format!("{api}/movie/31639?{details}&api_key=KEY"):
                {"title": "Blame It on the Bellboy", "release_date": "1992-03-06",
                 "overview": "A hotel mix-up.", "external_ids": {"imdb_id": "tt0103827"}},
            format!("{api}/find/tt0397306?external_source=imdb_id&api_key=KEY"):
                {"movie_results": [], "tv_results": [{"id": 1433}]},
            format!("{api}/tv/1433?{details}&api_key=KEY"):
                {"name": "American Dad!", "first_air_date": "2005-02-06",
                 "external_ids": {"imdb_id": "tt0397306"}},
            format!("{api}/tv/1433/season/5?api_key=KEY"): {"id": 3626, "air_date": "2009-09-27"},
            format!("{api}/tv/1433/season/5/episode/7?append_to_response=external_ids&api_key=KEY"):
                {"id": 512000, "name": "Phantom of the Telethon", "air_date": "2009-11-15",
                 "external_ids": {"imdb_id": "tt1508003"}},
        });
        fs::write(dir.join("tmdb_cache.json"), tmdb_cache.to_string()).unwrap();
        let plex_cache = json!({
            r#"POST /library/metadata/matches {"guid": "tmdb://31639", "type": 1}"#:
                {"MediaContainer": {"Metadata": [{"guid": "plex://movie/5d7768279ab5440020ad6e0e"}]}},
            r#"POST /library/metadata/matches {"guid": "tmdb://1433", "type": 2}"#:
                {"MediaContainer": {"Metadata": [{"guid": "plex://show/5d9c086c46115600200aa2fe"}]}},
            "GET /library/metadata/5d9c086c46115600200aa2fe/children ":
                {"MediaContainer": {"Metadata": [{"index": 5, "guid": "plex://season/602e67d31d3358002c411c37"}]}},
            "GET /library/metadata/602e67d31d3358002c411c37/children ":
                {"MediaContainer": {"Metadata": [{"index": 7, "guid": "plex://episode/5d9c0ab4705e7a001e6d4a2b"}]}},
        });
        fs::write(dir.join("plex_cache.json"), plex_cache.to_string()).unwrap();
        let entries: Value = serde_json::from_str(
            // The build's own path, else the tree the test runs in: a test binary built in a
            // container and run on another machine (Windows from a cross build) has only the latter.
            &fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/zurg/entries.json"))
                .or_else(|_| fs::read_to_string("../tests/fixtures/zurg/entries.json"))
                .unwrap(),
        )
        .unwrap();
        let arrival = |kind: &str| {
            let fixture = &entries["entries"][kind];
            let release = fixture["release"].as_str().unwrap().to_string();
            let file = format!("/zurg/x/{release}/{}", fixture["file"].as_str().unwrap());
            Arrival {
                found: Found { file, size: 1 },
                release: release.clone(),
                release_path: format!("/zurg/x/{release}"),
                entry: fixture["entry"].clone(),
                extra: Map::new(),
                meta: None,
            }
        };
        let plan = |kind: &str| Library {
            key: "1".to_string(),
            kind: kind.to_string(),
            title: "x".to_string(),
            tmdb: Some(Arc::new(tmdb::Tmdb::new("testkey".to_string(), dir.join("tmdb_cache.json")))),
            plex: Some(Arc::new(plexmeta::PlexMeta::new("token".to_string(), dir.join("plex_cache.json")))),
        };
        let mut movie = vec![arrival("movie")];
        name_release(&plan("movie"), &mut movie, 1);
        let meta = movie[0].meta.clone().unwrap();
        assert_eq!(meta["title"], "Blame It on the Bellboy");
        assert_eq!(meta["year"], 1992);
        assert_eq!(meta["guid"], "plex://movie/5d7768279ab5440020ad6e0e");
        assert_eq!(meta["Guid"], json!([{"id": "imdb://tt0103827"}, {"id": "tmdb://31639"}]));
        assert_eq!(meta["_identify"]["confidence"], "exact");
        let mut episode = vec![arrival("episode")];
        name_release(&plan("show"), &mut episode, 1);
        let meta = episode[0].meta.clone().unwrap();
        assert_eq!(meta["title"], "Phantom of the Telethon");
        assert_eq!(meta["_show_title"], "American Dad!");
        assert_eq!(meta["_show_guid"], "plex://show/5d9c086c46115600200aa2fe");
        assert_eq!(meta["_season_guid"], "plex://season/602e67d31d3358002c411c37");
        assert_eq!(meta["guid"], "plex://episode/5d9c0ab4705e7a001e6d4a2b");
        assert_eq!((meta["_season"].clone(), meta["_episode"].clone()), (json!(5), json!(7)));
        // A movie library's release that is a show is not forced into a movie.
        let mut wrong = vec![arrival("episode")];
        name_release(&plan("movie"), &mut wrong, 1);
        assert!(!wrong[0].meta.clone().unwrap().contains_key("guid"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn samples_and_folders() {
        assert!(is_sample("/x/Film.2020.Sample.mkv") && !is_sample("/x/Samples.of.Life.mkv"));
        assert!(under("/mnt/zurg/movies/A/b.mkv", "/mnt/zurg/movies/") && !under("/mnt/zurg/moviesX/b.mkv", "/mnt/zurg/movies"));
    }
}
