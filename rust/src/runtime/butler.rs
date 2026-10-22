//! The butler, the section scan and the activity frames they push, ported from
//! `server.py` (`butler`, `run_butler`, `notify_section_scan`, `notify_empty_section_scan`).
//!
//! Every sequence here was measured on a writable copy of the reference (plexscratch,
//! plexscratch42) with a notification socket open; the comments in `server.py` carry the
//! per-cycle measurements.

use super::*;

/// `/butler`'s task table, as a real PMS lists it: the root carries no `size`.
const BUTLER_TASKS: &str = r#"[{"name": "AutomaticUpdates", "interval": 1, "scheduleRandomized": false, "enabled": false}, {"name": "BackupDatabase", "interval": 3, "scheduleRandomized": false, "enabled": true, "title": "Backup Database", "description": "Create a backup copy of the server's database in the configured backup directory"}, {"name": "ButlerTaskCleanSupplementalLogFiles", "interval": 1, "scheduleRandomized": false, "enabled": false}, {"name": "ButlerTaskGenerateAdMarkers", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "ButlerTaskGenerateCreditsMarkers", "interval": 1, "scheduleRandomized": true, "enabled": true}, {"name": "ButlerTaskGenerateIntroMarkers", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "ButlerTaskGenerateVoiceActivity", "interval": 1, "scheduleRandomized": true, "enabled": false}, {"name": "CleanOldBundles", "interval": 7, "scheduleRandomized": false, "enabled": true}, {"name": "CleanOldCacheFiles", "interval": 7, "scheduleRandomized": false, "enabled": true}, {"name": "DeepMediaAnalysis", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "GarbageCollectBlobs", "interval": 7, "scheduleRandomized": false, "enabled": true}, {"name": "GarbageCollectLibraryMedia", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "GenerateChapterThumbs", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "GenerateMediaIndexFiles", "interval": 1, "scheduleRandomized": false, "enabled": false}, {"name": "LoudnessAnalysis", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "MusicAnalysis", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "OptimizeDatabase", "interval": 7, "scheduleRandomized": false, "enabled": true}, {"name": "ProcessAssets", "interval": 1, "scheduleRandomized": false, "enabled": true}, {"name": "RefreshEpgGuides", "interval": 1, "scheduleRandomized": true, "enabled": true}, {"name": "RefreshLibraries", "interval": 1, "scheduleRandomized": false, "enabled": false}, {"name": "RefreshLocalMedia", "interval": 3, "scheduleRandomized": false, "enabled": true}, {"name": "RefreshPeriodicMetadata", "interval": 1, "scheduleRandomized": true, "enabled": true}, {"name": "UpgradeMediaAnalysis", "interval": 1, "scheduleRandomized": false, "enabled": true}]"#;

/// Each task's progress messages and, for the three with an activity of their own, its
/// `(type, title, subtitle)`; a `None` subtitle steps through `BUNDLE_CLEAN_PHASES`.
fn butler_messages(
    name: &str,
) -> (
    &'static [&'static str],
    Option<(&'static str, &'static str, Option<&'static str>)>,
) {
    match name {
        "BackupDatabase" => (
            &["The butler backed up your database."],
            Some(("general.db.backup", "Database", Some("Backing up database"))),
        ),
        "OptimizeDatabase" => (
            &["The butler optimized your database."],
            Some((
                "general.db.optimize",
                "Optimizing database",
                Some("Optimizing Database"),
            )),
        ),
        "CleanOldBundles" => (
            &[
                "The butler cleaned %d media bundles.",
                "The butler cleaned %d metadata bundles.",
            ],
            Some(("general.bundle.clean", "Cleaning Bundles", None)),
        ),
        "CleanOldCacheFiles" => (&["The butler deleted 0 old cache files."], None),
        "GarbageCollectBlobs" => (&["The butler collected blob garbage."], None),
        "RefreshLocalMedia" => (&["The butler refreshed %d items."], None),
        "RefreshPeriodicMetadata" => (
            &[
                "The butler refreshed 0 artists.",
                "The butler refreshed 0 seasons.",
                "The butler refreshed %d movies.",
                "The butler refreshed %d shows.",
            ],
            None,
        ),
        "UpgradeMediaAnalysis" => (&["The butler analyzed %d items."], None),
        "LoudnessAnalysis" => (
            &["The butler performed loudness analysis on 0 albums."],
            None,
        ),
        "GenerateChapterThumbs" => (
            &["The butler generated chapter thumbnails for %d files."],
            None,
        ),
        "DeepMediaAnalysis" => (&["The butler performed deep analysis on %d files."], None),
        "GenerateMediaIndexFiles" => (&["The butler generated %d video preview thumbnails."], None),
        "RefreshLibraries" => (&["The butler refreshed all libraries."], None),
        "ButlerTaskCleanSupplementalLogFiles" => (&["The butler cleaned 0 directories."], None),
        "ButlerTaskGenerateAdMarkers" => {
            (&["The butler generated ad markers for %d show(s)."], None)
        }
        "MusicAnalysis" => (
            &["The butler performed sonic analysis on 0 batches of albums."],
            None,
        ),
        _ => (&[], None),
    }
}

/// `CleanOldBundles`' phases: subtitle, progress, and the message index pushed after it.
const BUNDLE_CLEAN_PHASES: [(&str, i64, Option<usize>); 6] = [
    ("Analyzing media bundles", -1, None),
    ("Deleting %d media bundles", 75, None),
    ("Deleting %d media bundles", 100, Some(0)),
    ("Analyzing metadata bundles", 100, None),
    ("Deleting %d metadata bundles", 25, None),
    ("Deleting %d metadata bundles", 50, Some(1)),
];

/// A version-4 style uuid from the clock and a per-process counter; an activity only
/// needs one that no other live activity carries.
pub(super) fn activity_uuid() -> String {
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let count = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let mut state = nanos ^ count.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    let (a, b) = (next(), next());
    format!(
        "{:08x}-{:04x}-4{:03x}-{:04x}-{:012x}",
        (a >> 32) as u32,
        (a >> 16) as u16,
        (a as u16) & 0x0fff,
        ((b >> 48) as u16 & 0x3fff) | 0x8000,
        b & 0xffff_ffff_ffff
    )
}

pub(super) fn new_activity(kind: &str, title: &str, cancellable: bool) -> Map<String, Value> {
    Map::from_iter([
        ("uuid".to_string(), json!(activity_uuid())),
        ("type".to_string(), json!(kind)),
        ("cancellable".to_string(), json!(cancellable)),
        ("userID".to_string(), json!(1)),
        ("title".to_string(), json!(title)),
        ("subtitle".to_string(), json!("")),
        ("progress".to_string(), json!(0)),
    ])
}

impl Runtime {
    /// `notify_activity`: one ActivityNotification row for the activity as it stands.
    pub(super) fn notify_activity(&mut self, activity: &Map<String, Value>, event: &str) {
        self.push_event(
            "activity",
            // One OBJECT per event: the eventsource sends `data: {"El": {row}}` and the
            // websocket writer wraps it in its own list (the oracle's `notify`).
            json!({"ActivityNotification": {
                "event": event,
                "uuid": activity.get("uuid").cloned().unwrap_or(Value::Null),
                "Activity": Value::Object(activity.clone()),
            }}),
        );
    }

    pub(super) fn notify_status(&mut self, title: &str, description: &str) {
        self.push_event(
            "status",
            json!({"StatusNotification": {
                "title": title,
                "description": description,
                "notificationName": "LIBRARY_UPDATE",
            }}),
        );
    }

    pub(super) fn notify_progress(&mut self, message: &str) {
        self.push_event(
            "progress",
            json!({"ProgressNotification": {"message": message}}),
        );
    }

    /// The frames a library scan pushes (`notify_section_scan`).
    pub(super) fn notify_section_scan(&mut self, section_id: &str, section: &Value) {
        let title = string_value(section.get("title").unwrap_or(&Value::Null));
        let context = json!({"librarySectionID": section_id});
        let mut activity = new_activity("library.update.section", "Scanning", false);
        self.notify_activity(&activity, "started");
        self.notify_status(&format!("Scanning the \"{title}\" section"), "");
        activity.insert("title".to_string(), json!(format!("Scanning {title}")));
        self.notify_activity(&activity, "updated");
        // A MOVIE library steps per movie and a SHOW library names the show, then steps
        // per season, deepest first.
        let mut steps: Vec<(String, Option<String>)> = Vec::new();
        // Borrowed: every field read is a hot one, and copying the library twice per refresh
        // (old zurg builds send one per release) read every row from disk under the lock.
        for row in self.section_row_refs(section_id) {
            let row_title = string_value(row.get("title").unwrap_or(&Value::Null));
            let label = match row
                .get("year")
                .filter(|year| !year.is_null() && *year != &json!(0))
            {
                Some(year) => format!("{row_title} ({})", string_value(year)),
                None => row_title,
            };
            match row.get("type").and_then(Value::as_str) {
                Some("movie") => steps.push((label.clone(), Some(label))),
                Some("show") => {
                    steps.push((label.clone(), None));
                    let key = key_of(&row).unwrap_or_default();
                    let mut seasons = self.children_refs(&key);
                    seasons.sort_by_key(|season| {
                        -season.get("index").and_then(Value::as_i64).unwrap_or(0)
                    });
                    for season in &seasons {
                        let name = format!(
                            "Season {:02}",
                            season.get("index").and_then(Value::as_i64).unwrap_or(0)
                        );
                        steps.push((format!("{label}/{name}"), Some(format!("{label} - {name}"))));
                    }
                    if seasons.is_empty() {
                        steps.push((label.clone(), Some(label)));
                    }
                }
                _ => {}
            }
        }
        let counted = steps
            .iter()
            .filter(|(_, subtitle)| subtitle.is_some())
            .count();
        let mut number = 0;
        for (message, subtitle) in &steps {
            self.notify_progress(&format!("Scanning {message}"));
            let Some(subtitle) = subtitle else {
                continue;
            };
            number += 1;
            activity.insert("subtitle".to_string(), json!(subtitle));
            activity.insert(
                "progress".to_string(),
                json!(((number as f64) * 99.0 / counted as f64).round_ties_even() as i64),
            );
            activity.insert("Context".to_string(), context.clone());
            self.notify_activity(&activity, "updated");
        }
        if counted == 0 {
            activity.insert("progress".to_string(), json!(99));
            activity.insert("Context".to_string(), context.clone());
            self.notify_activity(&activity, "updated");
        }
        activity.insert("progress".to_string(), json!(100));
        activity.insert("Context".to_string(), context);
        self.notify_activity(&activity, "updated");
        let mut outer = new_activity(
            "provider.subscriptions.process",
            "Processing subscriptions",
            true,
        );
        self.notify_activity(&outer, "started");
        self.notify_activity(&activity, "ended");
        self.notify_status(
            "Library scan complete",
            "Extra information may still be downloading from the Internet",
        );
        let mut inner = outer.clone();
        inner.insert("uuid".to_string(), json!(activity_uuid()));
        self.notify_activity(&inner, "started");
        inner.insert("progress".to_string(), json!(100));
        self.notify_activity(&inner, "ended");
        outer.insert("progress".to_string(), json!(100));
        self.notify_activity(&outer, "ended");
    }

    /// A scan of a path the library does not hold: the same skeleton, no progress rows.
    pub(super) fn notify_empty_section_scan(&mut self, section_id: &str, section: &Value) {
        let title = string_value(section.get("title").unwrap_or(&Value::Null));
        let mut activity = new_activity("library.update.section", "Scanning", false);
        self.notify_activity(&activity, "started");
        self.notify_status(&format!("Scanning the \"{title}\" section"), "");
        activity.insert("title".to_string(), json!(format!("Scanning {title}")));
        self.notify_activity(&activity, "updated");
        activity.insert("progress".to_string(), json!(100));
        activity.insert(
            "Context".to_string(),
            json!({"librarySectionID": section_id}),
        );
        self.notify_activity(&activity, "updated");
        let mut outer = new_activity(
            "provider.subscriptions.process",
            "Processing subscriptions",
            true,
        );
        self.notify_activity(&outer, "started");
        self.notify_activity(&activity, "ended");
        self.notify_status(
            "Library scan complete",
            "Extra information may still be downloading from the Internet",
        );
        outer.insert("progress".to_string(), json!(100));
        self.notify_activity(&outer, "ended");
    }

    /// `/butler` and `/butler/{task}` -- the Scheduled Tasks page.
    pub(super) fn route_butler(&mut self, method: &str, segments: &[&str]) -> Response {
        let tasks: Vec<Value> = serde_json::from_str(BUTLER_TASKS).unwrap_or_default();
        if segments.len() == 1 {
            if matches!(method, "POST" | "DELETE") {
                if method == "POST" {
                    let names = tasks
                        .iter()
                        .filter(|task| {
                            task.get("enabled")
                                .and_then(Value::as_bool)
                                .unwrap_or(false)
                        })
                        .map(|task| string_value(task.get("name").unwrap_or(&Value::Null)))
                        .collect::<Vec<_>>();
                    self.run_butler(&names);
                }
                let mut response = Response::empty(200);
                response.content_type = "text/html".to_string();
                return response;
            }
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "rex: not found");
            }
            return Response::json(200, json!({"ButlerTasks": {"ButlerTask": tasks}}));
        }
        let name = segments[1];
        if !tasks
            .iter()
            .any(|task| task.get("name").and_then(Value::as_str) == Some(name))
        {
            return Response::error(404, "rex: no such task");
        }
        if method != "POST" {
            return Response::error(404, "rex: not found");
        }
        self.run_butler(&[name.to_string()]);
        let mut response = Response::empty(200);
        response.content_type = "text/html".to_string();
        response
    }

    fn run_butler(&mut self, names: &[String]) {
        let mut wrapper = new_activity("butler", "Butler tasks", false);
        wrapper.insert("progress".to_string(), json!(-1));
        self.notify_activity(&wrapper, "started");
        let mut deferred = Vec::new();
        let count = |kind: &[&str]| {
            self.catalog
                .hot()
                .lazy()
                .filter(|row| {
                    row.get("type")
                        .and_then(Value::as_str)
                        .is_some_and(|value| kind.contains(&value))
                })
                .count()
        };
        let leaves = count(&["movie", "episode"]);
        let movies = count(&["movie"]);
        let shows = count(&["show"]);
        for (index, name) in names.iter().enumerate() {
            let index = index + 1;
            let processing = match name.as_str() {
                "GenerateChapterThumbs" => Some(analysis::Options { chapters: true, ..Default::default() }),
                "GenerateMediaIndexFiles" => Some(analysis::Options { previews: true, ..Default::default() }),
                "DeepMediaAnalysis" => Some(analysis::Options { markers: true, ..Default::default() }),
                _ => None,
            };
            if let Some(options) = processing {
                let keys = self.catalog.hot().lazy()
                    .filter(|row| matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")))
                    .filter_map(|row| key_of(&row)).collect::<Vec<_>>();
                let queued = self.request_analysis(&keys, options);
                self.notify_progress(&format!("Queued {queued} media analysis job(s)."));
                info!("BUTLER queued {name}: {queued} job(s)");
                continue;
            }
            let inner = butler_messages(name).1;
            let runs = if name == "BackupDatabase" { 2 } else { 1 };
            for _run in 0..runs {
                let mut messages = butler_messages(name).0.to_vec();
                if let Some((kind, title, subtitle)) = inner {
                    let opens_empty = kind == "general.db.optimize";
                    let mut activity = new_activity(kind, title, false);
                    activity.insert(
                        "subtitle".to_string(),
                        json!(if opens_empty {
                            ""
                        } else {
                            subtitle.unwrap_or("")
                        }),
                    );
                    activity.insert(
                        "progress".to_string(),
                        json!(if opens_empty { 0 } else { -1 }),
                    );
                    self.notify_activity(&activity, "started");
                    activity.insert("subtitle".to_string(), json!(subtitle.unwrap_or("")));
                    if subtitle.is_none() {
                        for (phase, step, says) in BUNDLE_CLEAN_PHASES {
                            activity.insert(
                                "subtitle".to_string(),
                                json!(phase.replace("%d", &leaves.to_string())),
                            );
                            activity.insert("progress".to_string(), json!(step));
                            self.notify_activity(&activity, "updated");
                            if let Some(says) = says.filter(|says| *says < messages.len()) {
                                let message = messages[says].replace("%d", &leaves.to_string());
                                self.notify_progress(&message);
                            }
                        }
                        messages = Vec::new();
                    } else {
                        for step in [0, 10, 40, 60] {
                            activity.insert("progress".to_string(), json!(step));
                            self.notify_activity(&activity, "updated");
                        }
                    }
                    // Cycle 38 recovered the spellfix rebuild's callers from both PMS
                    // binaries: the Butler's OptimizeDatabase is one of the three (with
                    // `PUT /library/optimize` and a library delete); an edit is not.
                    if name == "OptimizeDatabase" {
                        self.rebuild_spelling();
                    }
                    activity.insert("progress".to_string(), json!(100));
                    self.notify_activity(&activity, "ended");
                }
                if name == "RefreshLibraries" {
                    deferred.extend(self.catalog.sections.clone());
                }
                for message in &messages {
                    let text = if message.contains("%d") {
                        let number = if message.contains("movies") {
                            movies
                        } else if message.contains("shows") || message.contains("show(s)") {
                            shows
                        } else {
                            leaves
                        };
                        message.replace("%d", &number.to_string())
                    } else {
                        message.to_string()
                    };
                    self.notify_progress(&text);
                }
            }
            wrapper.insert("progress".to_string(), json!(100));
            self.notify_activity(&wrapper, "updated");
            if index < names.len() {
                wrapper.insert(
                    "progress".to_string(),
                    json!((100.0 * index as f64 / (index + 1) as f64).round_ties_even() as i64),
                );
                self.notify_activity(&wrapper, "updated");
            }
            info!("BUTLER ran {name}");
        }
        wrapper.insert("progress".to_string(), json!(100));
        self.notify_activity(&wrapper, "ended");
        for section in deferred {
            let key = string_value(section.get("key").unwrap_or(&Value::Null));
            self.notify_section_scan(&key, &section);
        }
    }
}

/// `KNOWN_AGENTS` / `KNOWN_SCANNERS` in server.py: what a library edit may name.
pub(super) const KNOWN_AGENTS: [&str; 6] = [
    "tv.plex.agents.movie",
    "tv.plex.agents.series",
    "tv.plex.agents.none",
    "tv.plex.agents.nfo.movie",
    "tv.plex.agents.nfo.series",
    "com.plexapp.agents.none",
];
pub(super) const KNOWN_SCANNERS: [&str; 6] = [
    "Plex Movie",
    "Plex Movie Scanner",
    "Plex Video Files Scanner",
    "Plex Video Files",
    "Plex TV Series",
    "Plex Series Scanner",
];

impl Runtime {
    /// `KNOWN_LANGUAGES`: the regional codes of the first `languageOverride` enum in the
    /// item preference descriptors, lower-cased.
    pub(super) fn known_languages(&self) -> HashSet<String> {
        let Some(document) = load_json_sidecar(&self.catalog_path, "item_prefs.json") else {
            return HashSet::new();
        };
        for (key, settings) in document.as_object().into_iter().flatten() {
            if key.starts_with('_') {
                continue;
            }
            for setting in settings.as_array().into_iter().flatten() {
                if setting.get("id").and_then(Value::as_str) != Some("languageOverride") {
                    continue;
                }
                return setting
                    .get("enumValues")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .split('|')
                    .filter_map(|entry| {
                        let code = entry.split(':').next().unwrap_or_default();
                        code.contains('-').then(|| code.to_lowercase())
                    })
                    .collect();
            }
        }
        HashSet::new()
    }

    /// `notify_activity_run`: one activity from `started` to `ended` with its steps.
    pub(super) fn notify_activity_run(&mut self, kind: &str, title: &str, steps: &[(i64, &str)]) {
        let mut activity = new_activity(kind, title, false);
        self.notify_activity(&activity, "started");
        for (progress, subtitle) in steps {
            activity.insert("progress".to_string(), json!(progress));
            activity.insert("subtitle".to_string(), json!(subtitle));
            self.notify_activity(&activity, "updated");
        }
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
    }

    /// `frame_title`: a movie's and a show's frames carry the year; a season's
    /// `<show> S01` and an episode's `<show> S01 E07`.
    pub(super) fn frame_title(&self, row: &Value) -> String {
        let title = string_value(row.get("title").unwrap_or(&Value::Null));
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
        let number = |value: Option<&Value>| {
            value
                .map(|value| string_value(value).parse::<i64>().unwrap_or(0))
                .unwrap_or(0)
        };
        match kind {
            "movie" | "show" => match row
                .get("year")
                .filter(|year| !year.is_null() && **year != json!(0) && **year != json!(""))
            {
                Some(year) => format!("{title} ({})", string_value(year)),
                None => title,
            },
            "season" | "episode" => {
                let show_key = string_value(
                    row.get(if kind == "season" {
                        "parentRatingKey"
                    } else {
                        "grandparentRatingKey"
                    })
                    .unwrap_or(&Value::Null),
                );
                let show_title = self
                    .catalog
                    .get(&show_key)
                    .and_then(|show| show.get("title"))
                    .map(string_value)
                    .unwrap_or_default();
                // An episode's `parentIndex` is read off its season at read time,
                // so a stored row may not carry it: fall back to the season's index.
                let own_parent_index = row.get(if kind == "season" {
                    "index"
                } else {
                    "parentIndex"
                });
                let index = if own_parent_index.is_none() && kind == "episode" {
                    number(
                        row.get("parentRatingKey")
                            .map(string_value)
                            .and_then(|parent| self.catalog.get(&parent))
                            .and_then(|season| season.get("index")),
                    )
                } else {
                    number(own_parent_index)
                };
                let mut text = format!("{show_title} S{index:02}");
                if kind == "episode" {
                    text.push_str(&format!(" E{:02}", number(row.get("index"))));
                }
                text
            }
            _ => title,
        }
    }

    /// `PUT /library/metadata/{id}/analyze`: per LEAF three `state 5` frames carrying
    /// `mediaState` analyzing, thumbnailing and none, then ONE chapter-thumbs activity,
    /// started then ended, whose subtitle is the target's frame title -- and nothing
    /// else (cycle 19: no `backgroundProcessingQueue` frame).
    pub(super) fn analyze_item(&mut self, key: &str, row: &Value) -> usize {
        let leaves = if matches!(
            row.get("type").and_then(Value::as_str),
            Some("show" | "season")
        ) {
            self.leaves(key)
        } else {
            vec![row.clone()]
        };
        for leaf in &leaves {
            for media_state in [Some("analyzing"), Some("thumbnailing"), None] {
                self.push_timeline_row(leaf, 5, None, media_state);
            }
        }
        let mut activity = new_activity(
            "media.generate.chapter.thumbs",
            "Generating video chapter thumbnails",
            false,
        );
        activity.insert("subtitle".to_string(), json!(self.frame_title(row)));
        activity.insert("progress".to_string(), json!(-1));
        self.notify_activity(&activity, "started");
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        leaves.len()
    }

    /// `GET /library/sections/{id}/refresh`: `?path=` scopes the scan (cycles 19/36) --
    /// the library's own folder or one inside it scans everything, any other path runs
    /// the empty skeleton -- and `force=1` refreshes each item after the scan.
    pub(super) fn refresh_section(
        &mut self,
        section_id: &str,
        section: &Value,
        query: &HashMap<String, String>,
    ) {
        self.pending_auto_collections = true;
        let mut paths = section
            .get("Location")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .map(|row| string_value(row.get("path").unwrap_or(&Value::Null)))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if paths.is_empty() {
            paths.push(format!("/rex/{section_id}"));
        }
        if let Some(wanted) = query.get("path").filter(|value| !value.is_empty()) {
            let inside = paths.iter().any(|path| {
                wanted == path || wanted.starts_with(&format!("{}/", path.trim_end_matches('/')))
            });
            if !inside {
                self.notify_empty_section_scan(section_id, section);
                info!(
                    "SECTION refresh {section_id} path={} -> no folder",
                    crate::filters::py_repr(wanted)
                );
                return;
            }
        }
        let force = query.get("force").map(String::as_str) == Some("1");
        // A library has no folders rex reads: its sources push what it holds
        // (`feed.rs`), so a refresh -- old zurg builds still send one per release -- is
        // the catalog's measured skeleton and changes nothing.
        self.notify_section_scan(section_id, section);
        let keys = self.section_row_refs(section_id).iter().filter_map(key_of).collect::<Vec<_>>();
        if force {
            for key in &keys {
                self.push_refresh_events(key);
            }
        }
        info!(
            "SECTION refresh {section_id} force={} -> {} row(s)",
            if force { "True" } else { "False" },
            keys.len()
        );
    }

    /// `PUT /library/sections/{id}` -- the Edit Library dialog, validated as a real PMS
    /// validates it: an `agent` is required and must be known, a `type` must be the
    /// library's own (as a word or its number), a scanner and a language must be known;
    /// the form is applied, and it scans only when it carries `language` or `scanner`.
    pub(super) fn edit_section(
        &mut self,
        section_key: &str,
        section: Value,
        query: &HashMap<String, String>,
    ) -> Response {
        let Some(agent) = query.get("agent").filter(|value| !value.is_empty()) else {
            return Response::error(400, "rex: bad request");
        };
        if !KNOWN_AGENTS.contains(&agent.as_str()) {
            return Response::error(400, "rex: bad request");
        }
        if let Some(asked) = query.get("type").filter(|value| !value.is_empty()) {
            let named = if asked.bytes().all(|b| b.is_ascii_digit()) {
                match asked.parse::<i64>().unwrap_or(0) {
                    1 => Some("movie"),
                    2 => Some("show"),
                    3 => Some("season"),
                    4 => Some("episode"),
                    _ => None,
                }
                .map(str::to_string)
            } else {
                Some(asked.clone())
            };
            if named.as_deref() != section.get("type").and_then(Value::as_str) {
                return Response::error(400, "rex: bad request");
            }
        }
        if let Some(scanner) = query.get("scanner").filter(|value| !value.is_empty()) {
            if !KNOWN_SCANNERS.contains(&scanner.as_str()) {
                return Response::error(400, "rex: bad request");
            }
        }
        if let Some(language) = query.get("language").filter(|value| !value.is_empty()) {
            if !self.known_languages().contains(&language.to_lowercase()) {
                return Response::error(400, "rex: bad request");
            }
        }
        if let Some(object) = self
            .catalog
            .sections
            .iter_mut()
            .find(|row| string_value(row.get("key").unwrap_or(&Value::Null)) == section_key)
            .and_then(Value::as_object_mut)
        {
            if let Some(name) = query.get("name").filter(|value| !value.is_empty()) {
                object.insert("title".to_string(), json!(name));
            }
            if let Some(language) = query.get("language").filter(|value| !value.is_empty()) {
                object.insert("language".to_string(), json!(language));
            }
        }
        for (field, value) in query {
            if let Some(pref) = field
                .strip_prefix("prefs[")
                .and_then(|value| value.strip_suffix(']'))
            {
                if pref == "autoCollectionThreshold" && value != "0" {
                    self.pending_auto_collections = true;
                }
                self.section_prefs
                    .insert((section_key.to_string(), pref.to_string()), value.clone());
            }
        }
        self.catalog.document.insert(
            "sections".to_string(),
            Value::Array(self.catalog.sections.clone()),
        );
        if ["language", "scanner"]
            .iter()
            .any(|field| query.get(*field).is_some_and(|value| !value.is_empty()))
        {
            if let Some(updated) = self.section(section_key).cloned() {
                self.notify_section_scan(section_key, &updated);
            }
        }
        self.touch_gdm();
        info!(
            "SECTION edit {section_key} -> {}",
            self.section(section_key)
                .and_then(|row| row.get("title"))
                .map_or_else(|| "None".to_string(), string_value)
        );
        self.dirty = true;
        Response::empty(200)
    }
}

impl Runtime {
    /// `/hubs/sections/{id}/popular` (cycles 52/55): the library's SEASONS ranked by their
    /// episodes' HISTORY rows, most-played first; a movie library answers the empty
    /// container; `monthsAgo` must be a whole number, checked before the section.
    pub(super) fn route_popular_hub(
        &self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        if let Some(months) = query.get("monthsAgo") {
            let digits = months.strip_prefix(['+', '-']).unwrap_or(months);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return Response::error(400, "rex: bad request");
            }
        }
        let Some(section) = self.section(segments[2]).cloned() else {
            return Response::error(404, "rex: not found");
        };
        if segments.len() != 4 || !matches!(request.method.as_str(), "GET" | "HEAD") {
            return Response::error(404, "rex: not found");
        }
        let section_key = string_value(section.get("key").unwrap_or(&Value::Null));
        let mut plays: Vec<(String, usize)> = Vec::new();
        for entry in &self.history {
            let key = string_value(entry.get("ratingKey").unwrap_or(&Value::Null));
            let Some(leaf) = self.catalog.get(&key) else {
                continue;
            };
            if leaf.get("type").and_then(Value::as_str) != Some("episode") {
                continue;
            }
            let parent = leaf
                .get("parentRatingKey")
                .or_else(|| leaf.get("_parentRatingKey"))
                .map(string_value)
                .unwrap_or_default();
            if parent.is_empty() || !parent.bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            match plays.iter_mut().find(|(key, _)| *key == parent) {
                Some((_, count)) => *count += 1,
                None => plays.push((parent, 1)),
            }
        }
        let mut seasons = plays
            .into_iter()
            .filter_map(|(key, count)| {
                let row = self.catalog.get(&key)?;
                (string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section_key)
                    .then(|| (count, key.parse::<i64>().unwrap_or(0), row.clone()))
            })
            .collect::<Vec<_>>();
        seasons.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let rows = seasons
            .into_iter()
            .map(|(_, _, row)| self.detail_row(&row))
            .collect::<Vec<_>>();
        let mut body = Map::new();
        body.insert("size".to_string(), json!(rows.len()));
        body.insert("allowSync".to_string(), json!(true));
        body.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        body.insert(
            "librarySectionID".to_string(),
            json!(section_key.parse::<i64>().unwrap_or(0)),
        );
        body.insert(
            "librarySectionTitle".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        body.insert(
            "librarySectionUUID".to_string(),
            section.get("uuid").cloned().unwrap_or(Value::Null),
        );
        body.insert("Metadata".to_string(), Value::Array(rows));
        Response::json(200, json!({"MediaContainer": body}))
    }
}

impl Runtime {
    /// `/accounts` and `/accounts/{id}` (`accounts` in server.py): the server's own user
    /// row, then every share plex.tv lists, by plex.tv id (plexlab's `/accounts` once shared
    /// with a Home user), in a bare container carrying only its `identifier`; any other id
    /// is a 404.
    pub(super) fn route_account(&self, account_id: Option<&str>) -> Response {
        let mut rows = vec![json!({
            "id": 1,
            "key": "/accounts/1",
            "name": self.owner_name(),
            "defaultAudioLanguage": "en",
            "autoSelectAudio": true,
            "defaultSubtitleLanguage": "en",
            "subtitleMode": 2,
            "thumb": "",
        })];
        rows.extend(self.shared_account_rows());
        if let Some(id) = account_id {
            let digits = id.strip_prefix('+').unwrap_or(id);
            let wanted = (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
                .then(|| digits.trim_start_matches('0').parse::<i64>().ok())
                .flatten();
            rows.retain(|row| wanted.is_some() && row.get("id").and_then(Value::as_i64) == wanted);
            if rows.is_empty() {
                return Response::error(404, "rex: no such account");
            }
        }
        Response::json(
            200,
            json!({"MediaContainer": {
                "size": rows.len(),
                "identifier": "com.plexapp.system.accounts",
                "Account": rows,
            }}),
        )
    }
}

/// Cycle 52: thirteen genre names carry a FIXED Categories colour (the 1.42.2 reference
/// and plexlab agree).
fn category_colour(name: &str) -> Option<&'static str> {
    Some(match name {
        "action" => "2B717E",
        "animation" => "347431",
        "comedy" => "51284C",
        "crime" => "33486E",
        "documentary" => "884624",
        "drama" => "846E49",
        "history" => "37397A",
        "horror" => "7A281A",
        "musical" => "3B8265",
        "romance" => "892959",
        "science fiction" => "673E8B",
        "thriller" => "57222B",
        "western" => "704A23",
        _ => return None,
    })
}
/// ...and every other genre takes the next of this cycle, walked in TAG-ID order.
const CATEGORY_COLOUR_CYCLE: [&str; 7] = [
    "494616", "91293D", "4C561B", "883E7D", "8B6122", "8B4D43", "23533C",
];

impl Runtime {
    /// `/library/sections/{id}/categories` (cycle 52): one tile per genre of the genre
    /// axis, its thumb FIRST -- an art transcode tinted with the genre's `blendColor`,
    /// the art the highest-rated not-yet-taken item carrying the genre and `art` --
    /// then `type`, `key` (the axis's fastKey) and `title`.
    pub(super) fn route_categories(
        &mut self,
        section_key: &str,
        section: Value,
        request: &Request,
    ) -> Response {
        if !matches!(request.method.as_str(), "GET" | "HEAD") {
            return Response::error(404, "rex: not found");
        }
        let mut headers = super::inner_headers(request);
        headers.insert("accept".to_string(), "application/json".to_string());
        let axis = self.request_route(&Request {
            method: "GET".to_string(),
            target: format!("/library/sections/{section_key}/genre"),
            version: request.version.clone(),
            headers,
            body: Vec::new(),
            body_complete: true,
        });
        let entries = serde_json::from_slice::<Value>(&axis.body)
            .ok()
            .and_then(|doc| {
                doc.get("MediaContainer")?
                    .get("Directory")?
                    .as_array()
                    .cloned()
            })
            .unwrap_or_default();
        let id_of = |entry: &Value| {
            string_value(entry.get("key").unwrap_or(&Value::Null))
                .parse::<i64>()
                .unwrap_or(0)
        };
        let mut by_id = entries.clone();
        by_id.sort_by_key(|entry| id_of(entry));
        let mut colours = HashMap::new();
        let mut unknown = 0;
        for entry in &by_id {
            let name = string_value(entry.get("title").unwrap_or(&Value::Null)).to_lowercase();
            let colour = match category_colour(&name) {
                Some(colour) => colour,
                None => {
                    let colour = CATEGORY_COLOUR_CYCLE[unknown % CATEGORY_COLOUR_CYCLE.len()];
                    unknown += 1;
                    colour
                }
            };
            colours.insert(id_of(entry), colour);
        }
        let items = self.section_rows(section_key);
        let mut taken = HashSet::new();
        let mut arts = HashMap::new();
        for entry in &by_id {
            let id = id_of(entry);
            let mut candidates = items
                .iter()
                .filter(|row| {
                    row.get("art")
                        .is_some_and(|art| !art.is_null() && string_value(art) != "")
                        && row
                            .get("Genre")
                            .and_then(Value::as_array)
                            .is_some_and(|tags| {
                                tags.iter().any(|tag| {
                                    string_value(tag.get("id").unwrap_or(&Value::Null))
                                        == id.to_string()
                                })
                            })
                })
                .collect::<Vec<_>>();
            candidates.sort_by(|a, b| {
                let rating = |row: &Value| {
                    row.get("audienceRating")
                        .and_then(Value::as_f64)
                        .unwrap_or(0.0)
                };
                let key = |row: &Value| key_of(row).unwrap_or_default().parse::<i64>().unwrap_or(0);
                rating(b)
                    .partial_cmp(&rating(a))
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(key(a).cmp(&key(b)))
            });
            let fresh = candidates
                .iter()
                .find(|row| !taken.contains(&key_of(row).unwrap_or_default()))
                .or_else(|| candidates.first());
            if let Some(pick) = fresh {
                taken.insert(key_of(pick).unwrap_or_default());
                arts.insert(id, string_value(pick.get("art").unwrap_or(&Value::Null)));
            }
        }
        let tiles = entries
            .iter()
            .map(|entry| {
                let id = id_of(entry);
                let mut tile = Map::new();
                if let Some(art) = arts.get(&id) {
                    tile.insert(
                        "thumb".to_string(),
                        json!(format!(
                            "/photo/:/transcode?blendColor={}&width=2560&height=1440&url={}",
                            colours
                                .get(&id)
                                .copied()
                                .unwrap_or(CATEGORY_COLOUR_CYCLE[0]),
                            quote_component(art)
                        )),
                    );
                }
                tile.insert("type".to_string(), json!("directory"));
                tile.insert(
                    "key".to_string(),
                    entry.get("fastKey").cloned().unwrap_or(Value::Null),
                );
                tile.insert(
                    "title".to_string(),
                    entry.get("title").cloned().unwrap_or(Value::Null),
                );
                Value::Object(tile)
            })
            .collect::<Vec<_>>();
        let mut fields = Map::new();
        fields.insert("Directory".to_string(), Value::Array(tiles));
        fields.insert(
            "Meta".to_string(),
            json!({"Type": [{"key": format!("/library/sections/{section_key}/categories"),
                             "type": "directory", "title": "Categories", "active": true}]}),
        );
        let body = self.secondary_body(&section, Some(""), fields);
        Response::json(200, json!({"MediaContainer": body}))
    }
}

/// `urllib.parse.quote(value, safe="")`.
fn quote_component(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

impl Runtime {
    /// Cycle 46: `/hubs` applies the detail expansions to its FULL rows -- Children,
    /// OnDeck and Preferences on a full SHOW row, Extras and Related on a full row or any
    /// row of the On Deck shelf -- where a light grid row stays light.
    pub(super) fn expand_global_hub_rows(
        &self,
        hubs: &mut [Value],
        query: &HashMap<String, String>,
    ) {
        // The oracle's `_include_int`: an optional `+`, digits, and the value ONE --
        // plexlab draws no block for `-1` or `2` (cycle 83), only for `1`, `01`, `+1`.
        let include_int = |name: &str| {
            query.get(name).is_some_and(|value| {
                let digits = value.strip_prefix('+').unwrap_or(value);
                !digits.is_empty()
                    && digits.bytes().all(|byte| byte.is_ascii_digit())
                    && digits.trim_start_matches('0') == "1"
            })
        };
        let children = include_int("includeChildren");
        let extras = include_int("includeExtras");
        let on_deck = include_int("includeOnDeck");
        let related = include_int("includeRelated");
        let prefs = query.get("includePreferences").map(String::as_str) == Some("1");
        if !(children || extras || on_deck || related || prefs) {
            return;
        }
        let mut related_query = HashMap::new();
        if let Some(count) = query.get("includeRelatedCount") {
            related_query.insert("includeRelatedCount".to_string(), count.clone());
        }
        for hub in hubs.iter_mut() {
            let ondeck_shelf =
                hub.get("hubIdentifier").and_then(Value::as_str) == Some("home.ondeck");
            let Some(rows) = hub.get_mut("Metadata").and_then(Value::as_array_mut) else {
                continue;
            };
            for projected in rows.iter_mut() {
                let light = projected.get("_light").is_some();
                let Some(key) = key_of(projected) else {
                    continue;
                };
                let Some(stored) = self.catalog.get(&key).cloned() else {
                    continue;
                };
                let show = projected.get("type").and_then(Value::as_str) == Some("show");
                if children && show && !light {
                    let inlined = self
                        .children(&key)
                        .iter()
                        .map(|child| {
                            let mut shown = self.detail_row(child);
                            if let Some(object) = shown.as_object_mut() {
                                object.retain(|name, _| !name.starts_with("librarySection"));
                            }
                            shown
                        })
                        .collect::<Vec<_>>();
                    projected["Children"] = json!({"size": inlined.len(), "Metadata": inlined});
                }
                let mut blocks = HashMap::new();
                if extras && (!light || ondeck_shelf) {
                    blocks.insert("includeExtras".to_string(), "1".to_string());
                }
                if on_deck && show && !light {
                    blocks.insert("includeOnDeck".to_string(), "1".to_string());
                }
                if prefs && show && !light {
                    blocks.insert("includePreferences".to_string(), "1".to_string());
                }
                if !blocks.is_empty() {
                    self.apply_detail_blocks(&stored, projected, &blocks);
                }
                if related && (!light || ondeck_shelf) {
                    projected["Related"] = self.related_block(&stored, &related_query);
                }
            }
        }
    }
}
