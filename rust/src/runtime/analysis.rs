//! Bounded source-identified media analysis. All processing and artifact reads run off Runtime.
use super::*;
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Weak};
use std::time::{Duration, SystemTime};

#[derive(Clone, Debug)]
pub(super) struct Input {
    pub key: String,
    pub part_id: String,
    pub url: String,
    pub auth: Option<String>,
    pub duration_ms: i64,
    pub audio_index: Option<i64>,
    pub video_index: Option<i64>,
    pub chapters: Vec<Value>,
    pub group: Option<String>,
}

#[derive(Clone, Copy, Debug, Default)]
pub(super) struct Options { pub markers: bool, pub chapters: bool, pub previews: bool }
impl Options {
    pub fn all() -> Self { Self { markers: true, chapters: true, previews: true } }
    fn merge(&mut self, other: Self) { self.markers |= other.markers; self.chapters |= other.chapters; self.previews |= other.previews; }
}
#[derive(Debug, Clone)]
struct Work { key: String, part: String, fingerprint: String, options: Options, marker_types: Option<[bool; 2]>, preview_epoch: u64 }
#[derive(Debug, Default)]
struct Lane { queue: VecDeque<Work>, running: Option<Work>, status: HashMap<String, Value>, started: bool, stopped: bool, cleanup: bool, cancelled: HashSet<(String,String)>, preview_epochs: HashMap<String,u64> }
#[derive(Debug)]
pub(super) struct Jobs {
    pub root: PathBuf,
    ephemeral: bool,
    lane: Mutex<Lane>,
    wake: Condvar,
    verified: Mutex<HashMap<PathBuf, (u64, SystemTime, String)>>,
    references: Mutex<HashMap<PathBuf,usize>>,
    garbage: Mutex<HashSet<PathBuf>>,
    file_references: Mutex<HashMap<PathBuf,usize>>,
    garbage_files: Mutex<HashSet<PathBuf>>,
}
impl Jobs {
    pub fn new(state: Option<&Path>) -> Arc<Self> {
        static SERIAL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let root = state.map(|p| { let mut p = p.as_os_str().to_os_string(); p.push(".analysis"); PathBuf::from(p) })
            .unwrap_or_else(|| std::env::temp_dir().join(format!("rex-analysis-{}-{}", std::process::id(), SERIAL.fetch_add(1, std::sync::atomic::Ordering::Relaxed))));
        let root = std::fs::canonicalize(&root).unwrap_or_else(|_| {
            let parent = root.parent().filter(|p|!p.as_os_str().is_empty()).unwrap_or(Path::new("."));
            std::fs::canonicalize(parent).unwrap_or_else(|_|std::env::current_dir().unwrap_or_default().join(parent)).join(root.file_name().unwrap())
        });
        Arc::new(Self { root, ephemeral: state.is_none(), lane: Mutex::new(Lane::default()), wake: Condvar::new(), verified: Mutex::new(HashMap::new()), references: Mutex::new(HashMap::new()), garbage: Mutex::new(HashSet::new()), file_references: Mutex::new(HashMap::new()), garbage_files: Mutex::new(HashSet::new()) })
    }
    pub fn status(&self) -> Vec<Value> { self.lane.lock().unwrap().status.values().cloned().collect() }
    fn replace_artifacts(&self, old: &Value, new: &Value) {
        let mut references = self.references.lock().unwrap();
        let mut files = self.file_references.lock().unwrap();
        let mut garbage_files = self.garbage_files.lock().unwrap();
        let mut garbage = self.garbage.lock().unwrap();
        for artifact in artifacts(old) { if let Some(parent) = artifact["path"].as_str().and_then(|p|Path::new(p).parent()) {
            if let Some(count) = references.get_mut(parent) { *count = count.saturating_sub(1); if *count == 0 { references.remove(parent); garbage.insert(parent.to_path_buf()); } }
            if let Some(path) = artifact["path"].as_str().map(PathBuf::from) { if let Some(count) = files.get_mut(&path) { *count = count.saturating_sub(1); if *count == 0 { files.remove(&path); garbage_files.insert(path); } } }
        } }
        for artifact in artifacts(new) { if let Some(path) = artifact["path"].as_str().map(PathBuf::from) { if let Some(parent) = path.parent() { *references.entry(parent.to_path_buf()).or_default() += 1; *files.entry(path).or_default() += 1; } } }
    }
    fn known(&self, artifact: &Value) -> bool {
        let Some(path) = artifact.get("path").and_then(Value::as_str).map(PathBuf::from) else { return false };
        let Some(hash) = artifact.get("sha256").and_then(Value::as_str) else { return false };
        let verified = self.verified.lock().unwrap();
        let Some((len,modified,digest)) = verified.get(&path) else { return false };
        let Ok(meta) = std::fs::metadata(&path) else { return false };
        *len == meta.len() && meta.modified().ok() == Some(*modified) && digest == hash
    }
    /// Called ONLY outside Runtime lock. Canonical containment precedes any file read.
    pub fn resolve(&self, artifact: &Value) -> Option<PathBuf> {
        let supplied = Path::new(artifact.get("path")?.as_str()?);
        if supplied.parent()?.parent()? != self.root || !supplied.parent()?.file_name()?.to_str()?.starts_with("v1-") { return None }
        let root = std::fs::canonicalize(&self.root).ok()?;
        let path = std::fs::canonicalize(supplied).ok()?;
        if path.parent()?.parent()? != root || !path.parent()?.file_name()?.to_str()?.starts_with("v1-") { return None }
        if self.known(artifact) { return Some(path) }
        let hash = hash_file(&path).ok()?;
        if artifact.get("sha256")?.as_str()? != hash { return None }
        if path.extension()?.to_str()? == "bif" {
            super::thumbnails::validate_bif(&path).ok()?;
        } else if !super::thumbnails::valid_jpeg(&path) { return None }
        let meta = std::fs::metadata(&path).ok()?;
        self.verified.lock().unwrap().insert(path.clone(), (meta.len(), meta.modified().ok()?, hash));
        Some(path)
    }
}
impl Drop for Jobs { fn drop(&mut self) { if self.ephemeral { let _ = std::fs::remove_dir_all(&self.root); } } }
fn digest(bytes: &[u8]) -> String { format!("{:x}", Sha256::digest(bytes)) }
fn hash_file(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    let mut file = std::fs::File::open(path)?;
    let mut buffer = [0u8;65536]; let mut hash = Sha256::new();
    loop { let count = file.read(&mut buffer)?; if count == 0 { break } hash.update(&buffer[..count]); }
    Ok(format!("{:x}",hash.finalize()))
}
fn fingerprint(input: &Input, media: &Value) -> String {
    let part = media.get("Part").and_then(Value::as_array).and_then(|parts| parts.iter().find(|p| p.get("id").map(string_value).as_deref() == Some(input.part_id.as_str())));
    digest(&serde_json::to_vec(&json!([1,input.key,input.part_id,input.url,input.auth,input.group,input.duration_ms,input.audio_index,input.video_index,input.chapters,media.get("id"),media.get("_sid"),media.get("_source"),media.get("_path"),part.and_then(|p|p.get("file")),part.and_then(|p|p.get("size"))])).unwrap())
}
fn dependency_group(dependencies: &[(String,String,String)], marker_types: Option<[bool;2]>) -> String {
    let mut sorted = dependencies.iter().collect::<Vec<_>>(); sorted.sort_unstable();
    digest(&serde_json::to_vec(&json!([1,marker_types,sorted])).unwrap())
}
fn reusable_markers(record: &Value, group: &str) -> bool {
    record["version"] == 1 && record["marker_group"] == group && record["markers"].is_array()
}

impl Runtime {
    fn analysis_record(&self, key: &str, part: &str) -> Option<&Value> {
        self.catalog.get(key)?.get("Media")?.as_array()?.iter().find_map(|m|m.get("_analysis").and_then(|a|a.get(part)))
    }
    fn marker_dependencies_valid(&self, record: &Value) -> bool {
        record.get("marker_dependencies").and_then(Value::as_array).is_some_and(|dependencies| dependencies.iter().all(|d| {
            let (Some(key),Some(part),Some(hash)) = (d[0].as_str(),d[1].as_str(),d[2].as_str()) else { return false };
            self.analysis_inputs(key).iter().any(|(i,h)|i.part_id == part && h == hash)
        }))
    }
    fn store_analysis_record(&mut self, key: &str, part: &str, fingerprint: &str, fields: &Value) {
        let Some(row) = self.catalog.get_mut(key) else { return };
        for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
            if !media["Part"].as_array().is_some_and(|parts|parts.iter().any(|p|p.get("id").map(string_value).as_deref() == Some(part))) { continue }
            if media.get("_analysis").is_none() { media["_analysis"] = json!({}); }
            let previous = media["_analysis"].get(part).unwrap_or(&Value::Null);
            let mut merged = if previous["version"] == 1 && previous["fingerprint"] == fingerprint { previous.clone() } else { json!({}) };
            merged["version"] = json!(1); merged["fingerprint"] = json!(fingerprint);
            for (key,value) in fields.as_object().into_iter().flatten() { merged[key] = value.clone(); }
            self.analysis_jobs.replace_artifacts(previous,&merged);
            media["_analysis"][part] = merged;
        }
    }
    fn analysis_inputs(&self, key: &str) -> Vec<(Input, String)> {
        let Some(row) = self.catalog.get(key) else { return Vec::new() };
        if row.get("deletedAt").is_some() || self.deleted.contains(key) { return Vec::new() }
        let mut result = Vec::new();
        for (media_index,media) in row.get("Media").and_then(Value::as_array).into_iter().flatten().enumerate() {
            for part in media.get("Part").and_then(Value::as_array).into_iter().flatten() {
                let Some(file) = part.get("file").and_then(Value::as_str) else { continue };
                let Some((source, path)) = self.part_source(row, file) else { continue };
                let (url, auth) = source.ffmpeg_input(&path);
                let streams = crate::catalog::part_streams(part);
                let stream = |kind| streams.iter().find(|s| s["streamType"].as_i64() == Some(kind)).and_then(|s| s["index"].as_i64());
                let input = Input { key: key.into(), part_id: part.get("id").map(string_value).unwrap_or_default(), url, auth,
                    duration_ms: part.get("duration").or_else(|| media.get("duration")).or_else(|| row.get("duration")).and_then(Value::as_i64).unwrap_or(0),
                    audio_index: stream(2), video_index: stream(1), chapters: media.get("_chapters").or_else(|| (media_index == 0).then(||row.get("Chapter")).flatten()).and_then(Value::as_array).cloned().unwrap_or_default(),
                    group: (row["type"] == "episode").then(|| row.get("parentRatingKey").map(string_value)).flatten() };
                if !input.part_id.is_empty() {
                    let source_hash = media.get("_hash").or_else(||(media_index == 0 && media.get("_sid").is_none()).then(||row.get("_hash")).flatten());
                    let hash = digest(&serde_json::to_vec(&json!([fingerprint(&input,media),source_hash,media.get("container"),media.get("videoCodec"),media.get("audioCodec"),part.get("hash"),part.get("updatedAt"),part.get("mtime")])).unwrap());
                    result.push((input,hash));
                }
            }
        }
        result
    }
    /// Explicit calls retry failed jobs; source hooks only enqueue changed/unprocessed Parts.
    pub(super) fn request_analysis(&mut self, keys: &[String], options: Options) -> usize {
        self.enqueue_analysis(keys, options, None)
    }
    fn enqueue_analysis(&mut self, keys: &[String], options: Options, marker_types: Option<[bool;2]>) -> usize {
        let mut pending = Vec::new();
        for key in keys {
            let rows = self.catalog.get(key).filter(|r| matches!(r["type"].as_str(), Some("show"|"season"))).map(|_| self.leaves(key));
            let keys = rows.map(|r| r.iter().filter_map(key_of).collect()).unwrap_or_else(|| vec![key.clone()]);
            for key in keys { for (input, hash) in self.analysis_inputs(&key) { pending.push(Work { key: key.clone(), part: input.part_id, fingerprint: hash, options, marker_types, preview_epoch: 0 }); } }
        }
        let mut lane = self.analysis_jobs.lane.lock().unwrap();
        let mut count = 0;
        if lane.stopped { return 0 }
        for mut work in pending {
            work.preview_epoch = lane.preview_epochs.get(&work.key).copied().unwrap_or(0);
            if let Some(existing) = lane.queue.iter_mut().find(|w| w.key == work.key && w.part == work.part && w.fingerprint == work.fingerprint) {
                let had_markers = existing.options.markers;
                existing.options.merge(options);
                if options.previews { existing.preview_epoch = work.preview_epoch; }
                if options.markers {
                    existing.marker_types = if had_markers { match (existing.marker_types,marker_types) { (Some(a),Some(b)) => Some([a[0]||b[0],a[1]||b[1]]), _ => None } } else { marker_types };
                }
                continue
            }
            if !lane.cancelled.contains(&(work.key.clone(),work.fingerprint.clone())) && lane.running.as_ref().is_some_and(|w| w.key == work.key && w.part == work.part && w.fingerprint == work.fingerprint && (!options.markers || (w.options.markers && match (w.marker_types,marker_types) { (None,_)=>true,(Some(_),None)=>false,(Some(a),Some(b))=>(!b[0]||a[0])&&(!b[1]||a[1]) })) && (!options.chapters || w.options.chapters) && (!options.previews || w.options.previews)) { continue }
            lane.status.insert(format!("{}:{}",work.key,work.part),json!({"ratingKey":work.key,"part":work.part,"state":"queued","fingerprint":work.fingerprint}));
            lane.queue.push_back(work); count += 1;
        }
        self.analysis_jobs.wake.notify_one(); count
    }
    pub(super) fn analysis_artifact(&self, key: &str, part: Option<&str>, chapter: Option<usize>) -> Option<Value> {
        let row = self.catalog.get(key)?;
        let primary = row.pointer("/Media/0/Part/0/id").map(string_value);
        for (input, hash) in self.analysis_inputs(key) {
            if part.is_some_and(|id| input.part_id != id) { continue }
            if chapter.is_some() && part.is_none() && primary.as_deref() != Some(input.part_id.as_str()) { continue }
            for media in row["Media"].as_array()? {
                let Some(record) = media.get("_analysis").and_then(|a| a.get(&input.part_id)) else { continue };
                if record["version"] != 1 || record["fingerprint"] != hash { continue }
                let artifact = match chapter { Some(index) => record["chapters"].get(index.to_string()).cloned(), None => record.get("bif").cloned() };
                if artifact.is_some() { return artifact }
            }
        }
        None
    }
    /// Removal/invalidation is immediate; filesystem cleanup runs on the analysis lane.
    pub(super) fn remove_analysis(&mut self, matches: impl Fn(&str) -> bool) {
        let keys = self.catalog.hot().rows().filter(|row| row.has_analysis).filter_map(|row| {
            let value = row.get("ratingKey")?;
            let key = match value {
                Value::String(key) => std::borrow::Cow::Borrowed(key.as_str()),
                _ => std::borrow::Cow::Owned(string_value(value)),
            };
            matches(&key).then(|| key.into_owned())
        }).collect::<Vec<_>>();
        for key in &keys { if let Some(row) = self.catalog.get_mut(key) { for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() { if let Some(o) = media.as_object_mut() { if let Some(records) = o.remove("_analysis") { for record in records.as_object().into_iter().flat_map(|r|r.values()) { self.analysis_jobs.replace_artifacts(record,&Value::Null); } } } } } }
        let mut lane = self.analysis_jobs.lane.lock().unwrap();
        lane.queue.retain(|work| !matches(&work.key));
        lane.status.retain(|_,status|!status["ratingKey"].as_str().is_some_and(&matches));
        if let Some(work) = lane.running.clone().filter(|w|matches(&w.key)) { lane.cancelled.insert((work.key,work.fingerprint)); }
        lane.cleanup = true;
        self.analysis_jobs.wake.notify_one();
        self.dirty = true;
    }
    pub(super) fn remove_media_analysis(&mut self, key: &str, index: usize) {
        let parts = self.catalog.get(key).and_then(|row| row.get("Media")?.as_array()?.get(index))
            .and_then(|media| media.get("_analysis")?.as_object())
            .map(|records| records.iter().map(|(part,record)| (
                key.to_string(), part.clone(), record["fingerprint"].as_str().unwrap_or_default().to_string()
            )).collect::<Vec<_>>()).unwrap_or_default();
        self.discard_analysis_parts(&parts);
    }
    /// One Part's records, as `remove_media_analysis` removes a whole Media's.
    pub(super) fn remove_part_analysis(&mut self, key: &str, part: &str) {
        let parts: Vec<_> = self.analysis_record(key, part)
            .map(|record| (key.to_string(), part.to_string(), record["fingerprint"].as_str().unwrap_or_default().to_string()))
            .into_iter().collect();
        self.discard_analysis_parts(&parts);
    }
    fn discard_analysis_parts(&mut self, parts: &[(String,String,String)]) {
        for (key,part,_) in parts {
            if let Some(row) = self.catalog.get_mut(key) {
                for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                    if let Some(record) = media.get_mut("_analysis").and_then(Value::as_object_mut).and_then(|records|records.remove(part)) {
                        self.analysis_jobs.replace_artifacts(&record,&Value::Null);
                    }
                }
            }
        }
        if parts.is_empty() { return }
        let mut lane = self.analysis_jobs.lane.lock().unwrap();
        lane.queue.retain(|w|!parts.iter().any(|(key,part,hash)|key == &w.key && part == &w.part && hash == &w.fingerprint));
        for (key,part,_) in parts { lane.status.remove(&format!("{key}:{part}")); }
        if let Some(work) = lane.running.clone().filter(|w|parts.iter().any(|(key,part,hash)|key == &w.key && part == &w.part && hash == &w.fingerprint)) {
            lane.cancelled.insert((work.key,work.fingerprint));
        }
        lane.cleanup = true; self.analysis_jobs.wake.notify_one(); self.dirty = true;
    }
    /// Call before replacing/removing this source; legacy mount ownership uses
    /// the old registration, and other providers' versions remain untouched.
    pub(super) fn invalidate_source_analysis(&mut self, name: &str) {
        let mut parts = Vec::new();
        self.catalog.each_row(|_,row| {
            let Some(key) = key_of(row) else { return };
            for media in row["Media"].as_array().into_iter().flatten() {
                let Some(records) = media.get("_analysis").and_then(Value::as_object) else { continue };
                for part in media["Part"].as_array().into_iter().flatten() {
                    let Some(id) = part.get("id").map(string_value) else { continue };
                    let Some(record) = records.get(&id) else { continue };
                    if part.get("file").and_then(Value::as_str).and_then(|file|self.part_source(row,file)).is_some_and(|(source,_)|source.name == name) {
                        parts.push((key.clone(),id,record["fingerprint"].as_str().unwrap_or_default().to_string()));
                    }
                }
            }
        });
        self.discard_analysis_parts(&parts);
    }
    /// Prune only feed-touched rows; no full-library work on a feed response.
    pub(super) fn prune_analysis(&mut self, keys: &[String]) {
        let mut stale = Vec::new();
        for key in keys {
            let Some(row) = self.catalog.get(key) else { continue };
            if !row["Media"].as_array().into_iter().flatten().any(|m|m.get("_analysis").is_some()) { continue }
            let current = self.analysis_inputs(key);
            for media in row["Media"].as_array().into_iter().flatten() {
                for (part,record) in media["_analysis"].as_object().into_iter().flat_map(|records|records.iter()) {
                    if record["version"] != 1 || !current.iter().any(|(input,hash)|&input.part_id == part && record["fingerprint"] == *hash) {
                        stale.push((key.clone(),part.clone(),record["fingerprint"].as_str().unwrap_or_default().to_string()));
                    }
                }
            }
        }
        self.discard_analysis_parts(&stale);
    }
    /// Delete only seek previews, not authored/generated markers or chapter JPEGs.
    /// Epochs suppress late preview publication without cancelling other work.
    pub(super) fn remove_preview_indexes(&mut self, keys: &[String]) {
        for key in keys { if let Some(row) = self.catalog.get_mut(key) {
            for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                for record in media.get_mut("_analysis").and_then(Value::as_object_mut).into_iter().flat_map(|r|r.values_mut()) {
                    let previous = record.clone();
                    if let Some(o) = record.as_object_mut() { o.remove("bif"); }
                    self.analysis_jobs.replace_artifacts(&previous,record);
                }
            }
        } }
        let mut lane = self.analysis_jobs.lane.lock().unwrap();
        for key in keys { *lane.preview_epochs.entry(key.clone()).or_default() += 1; }
        for work in &mut lane.queue { if keys.contains(&work.key) { work.options.previews = false; } }
        let cancelled = lane.queue.iter().filter(|w|!w.options.markers && !w.options.chapters && !w.options.previews).cloned().collect::<Vec<_>>();
        lane.queue.retain(|w|w.options.markers || w.options.chapters || w.options.previews);
        for work in cancelled { lane.status.insert(format!("{}:{}",work.key,work.part),json!({"ratingKey":work.key,"part":work.part,"state":"cancelled","fingerprint":work.fingerprint})); }
        lane.cleanup = true; self.analysis_jobs.wake.notify_one(); self.dirty = true;
    }
    pub(super) fn project_analysis(&self, row: &Value, projected: &mut Value, markers: bool) {
        let Some(key) = key_of(row) else { return };
        let inputs = self.analysis_inputs(&key);
        let primary = row.pointer("/Media/0/Part/0/id").map(string_value);
        if let Some(o) = projected.as_object_mut() { o.remove("Marker"); }
        for chapter in projected.get_mut("Chapter").and_then(Value::as_array_mut).into_iter().flatten() { if let Some(o) = chapter.as_object_mut() { o.remove("thumb"); } }
        for part in projected.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten().flat_map(|m|m.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten()) { if let Some(o) = part.as_object_mut() { o.remove("indexes"); } }
        if markers { if let Some(source) = row.get("Marker") { projected["Marker"] = source.clone(); } }
        for (input, hash) in inputs {
            let record = row["Media"].as_array().into_iter().flatten().find_map(|m| m.get("_analysis").and_then(|a| a.get(&input.part_id))).filter(|r| r["version"] == 1 && r["fingerprint"] == hash);
            for part in projected.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten().flat_map(|m| m.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten()) {
                if part.get("id").map(string_value).as_deref() == Some(input.part_id.as_str()) { if let Some(o) = part.as_object_mut() { if record.and_then(|r| r.get("bif")).is_some_and(|a| self.analysis_jobs.known(a)) { o.insert("indexes".into(),json!("sd")); } } }
            }
            if primary.as_deref() != Some(input.part_id.as_str()) { continue }
            if markers { if let Some(generated) = record.filter(|r|self.marker_dependencies_valid(r)).and_then(|r| r.get("markers")).and_then(Value::as_array) {
                if projected.get("Marker").is_none() { projected["Marker"] = json!([]); }
                if let Some(found) = projected.get_mut("Marker").and_then(Value::as_array_mut) { for marker in generated { if !found.contains(marker) { found.push(marker.clone()); } } }
            } }
            if let Some(chapters) = projected.get_mut("Chapter").and_then(Value::as_array_mut) {
                for chapter in chapters { if let Some(o) = chapter.as_object_mut() { o.remove("thumb"); } let Some(index) = chapter["index"].as_u64() else { continue }; if record.and_then(|r| r["chapters"].get(index.to_string())).is_some_and(|a| self.analysis_jobs.known(a)) { chapter["thumb"] = json!(format!("/library/metadata/{key}/chapters/{index}/thumb")); } }
            }
        }
    }
}

/// Start once after state/source restoration, before serving. Restored artifacts are
/// authenticated off-lock; no automatic library analysis is scheduled at startup.
pub(super) fn start(shared: &SharedRuntime) {
    let jobs = shared.lock().unwrap().analysis_jobs.clone();
    { let mut lane = jobs.lane.lock().unwrap(); if lane.started { return } lane.started = true; }
    let weak = Arc::downgrade(shared);
    std::thread::spawn(move || worker(weak, jobs));
}
/// Graceful shutdown stops admission and removes only ephemeral analysis files.
/// A running engine's bounded call may finish later; it cannot publish afterwards.
pub(super) fn shutdown(shared: &SharedRuntime) {
    let jobs = shared.lock().unwrap().analysis_jobs.clone();
    { let mut lane = jobs.lane.lock().unwrap(); lane.stopped = true; lane.queue.clear(); jobs.wake.notify_one(); }
    if jobs.ephemeral { let _ = std::fs::remove_dir_all(&jobs.root); }
}
fn worker(shared: Weak<Mutex<Runtime>>, jobs: Arc<Jobs>) {
    let restored = if let Some(runtime) = shared.upgrade() {
        let guard = runtime.lock().unwrap(); let mut result = Vec::new();
        for row in guard.catalog.hot().lazy().filter(|row| row.hot.has_analysis) {
            for media in row.get("Media").and_then(Value::as_array).into_iter().flatten() {
                for (part,record) in media["_analysis"].as_object().into_iter().flat_map(|m|m.iter()) {
                    jobs.replace_artifacts(&Value::Null,record); result.extend(artifacts(record));
                    if let (Some(key),Some(error)) = (key_of(&row),record["failure"].as_str()) {
                        jobs.lane.lock().unwrap().status.insert(format!("{key}:{part}"),json!({"ratingKey":key,"part":part,"state":"failed","error":error,"fingerprint":record["fingerprint"]}));
                    }
                }
            }
        } result
    } else { return };
    for artifact in restored { jobs.resolve(&artifact); }
    if let Ok(entries) = std::fs::read_dir(&jobs.root) { for entry in entries.flatten() { let path = entry.path(); if entry.file_type().is_ok_and(|t|t.is_dir()) && entry.file_name().to_str().is_some_and(|n|n.starts_with("v1-") || n.starts_with(".pending-v1-")) { jobs.garbage.lock().unwrap().insert(path); } } }
    collect_unused(&jobs);
    loop {
        let work = {
            let mut lane = jobs.lane.lock().unwrap();
            if lane.stopped { return }
            while lane.queue.is_empty() && !lane.cleanup {
                if lane.stopped { return }
                if shared.strong_count() == 0 { return }
                lane = jobs.wake.wait_timeout(lane,Duration::from_secs(1)).unwrap().0;
            }
            if lane.cleanup { lane.cleanup = false; None } else { let work = lane.queue.pop_front().unwrap(); lane.running = Some(work.clone()); Some(work) }
        };
        let Some(work) = work else { collect_unused(&jobs); continue };
        let Some(runtime) = shared.upgrade() else { return };
        let prepared = { let mut guard = runtime.lock().unwrap(); let target = guard.analysis_inputs(&work.key).into_iter().find(|(i,h)| i.part_id == work.part && h == &work.fingerprint);
            target.map(|(input,hash)| {
                let mut inputs = vec![input.clone()];
                let mut dependencies = vec![(input.key.clone(),input.part_id.clone(),hash)];
                if work.options.markers { if let Some(group) = &input.group {
                    for (other,hash) in guard.analysis_inputs(&work.key) { if other.part_id != input.part_id && other.group == input.group { dependencies.push((other.key.clone(),other.part_id.clone(),hash)); inputs.push(other); } }
                    for sibling in guard.leaves(group) { if let Some(key) = key_of(&sibling).filter(|k| k != &work.key) {
                        for (other,hash) in guard.analysis_inputs(&key) {
                            if other.group != input.group { continue }
                            dependencies.push((other.key.clone(),other.part_id.clone(),hash)); inputs.push(other);
                        }
                    } }
                } }
                let group = dependency_group(&dependencies,work.marker_types);
                let reuse = guard.analysis_record(&work.key,&work.part).is_some_and(|record| reusable_markers(record,&group));
                let ffmpeg = guard.transcoder.as_ref().map(|t| t.ffmpeg().to_path_buf());
                let voice_job = if work.options.markers { guard.prepare_voice_analysis(&work.key,&work.part) } else { None };
                let mut activity = super::butler::new_activity("media.analysis", "Analyzing media", false);
                activity.insert("Context".into(),json!({"ratingKey":work.key})); activity.insert("progress".into(),json!(-1));
                guard.notify_activity(&activity,"started");
                (input,inputs,dependencies,group,reuse,ffmpeg,voice_job,activity)
            }) };
        let Some((input,inputs,dependencies,group,reuse,ffmpeg,voice_job,mut activity)) = prepared else { finish(&jobs,&work,"stale",None); continue };
        finish(&jobs,&work,"running",None);
        let result = (|| -> Result<(Value,Vec<super::markers::ResultRow>,Option<super::subtitles::VoiceResult>),String> { let ffmpeg = ffmpeg.ok_or("media analysis requires REX_TRANSCODER")?;
            let mut markers = if work.options.markers && !reuse { super::markers::analyze(&ffmpeg,&inputs)? } else { Vec::new() };
            for row in &mut markers { row.markers.retain(|m|work.marker_types.is_none_or(|allowed|match m["type"].as_str() { Some("intro") => allowed[0], Some("credits") => allowed[1], _ => false })); }
            if jobs.lane.lock().unwrap().stopped { return Err("analysis stopped".into()) }
            let generated = super::thumbnails::generate(&ffmpeg,&input,&jobs.root,work.options.previews,work.options.chapters)?;
            for path in generated.bif.iter().chain(generated.chapter_images.iter().map(|(_,p)|p)) { if let Some(parent) = path.parent() { jobs.garbage.lock().unwrap().insert(parent.to_path_buf()); } }
            // The thumbnail engine validated these trusted generated paths before
            // returning; hash once, then cache their immutable publication stamps.
            let artifact = |path: &Path| -> Result<Value,String> {
                let hash = hash_file(path).map_err(|e|e.to_string())?;
                let metadata = std::fs::metadata(path).map_err(|e|e.to_string())?;
                let modified = metadata.modified().map_err(|e|e.to_string())?;
                jobs.verified.lock().unwrap().insert(path.to_path_buf(),(metadata.len(),modified,hash.clone()));
                Ok(json!({"path":path,"sha256":hash}))
            };
            let mut record = json!({});
            if work.options.chapters { record["chapters"] = json!({}); }
            if let Some(path) = generated.bif { record["bif"] = artifact(&path)?; }
            for (index,path) in generated.chapter_images { record["chapters"][index.to_string()] = artifact(&path)?; }
            if jobs.lane.lock().unwrap().stopped { return Err("analysis stopped".into()) }
            let voice = voice_job.map(super::subtitles::analyze_voice).transpose()?;
            Ok((record,markers,voice))
        })();
        let snapshot = { let mut guard = runtime.lock().unwrap(); let current = dependencies.iter().all(|(key,part,hash)| guard.analysis_inputs(key).iter().any(|(i,h)| &i.part_id == part && h == hash)) && { let lane = jobs.lane.lock().unwrap(); !lane.stopped && !lane.cancelled.contains(&(work.key.clone(),work.fingerprint.clone())) };
            match result { Ok((mut record,marker_rows,voice)) if current => {
                let voice_current = voice.is_none_or(|result|guard.finish_voice_analysis(result));
                if !voice_current {
                    finish(&jobs,&work,"stale",None);
                } else {
                if jobs.lane.lock().unwrap().preview_epochs.get(&work.key).copied().unwrap_or(0) != work.preview_epoch {
                    if let Some(artifact) = record.as_object_mut().and_then(|o|o.remove("bif")) { if let Some(path) = artifact["path"].as_str() { jobs.garbage_files.lock().unwrap().insert(PathBuf::from(path)); } }
                }
                record["failure"] = Value::Null;
                guard.store_analysis_record(&work.key,&work.part,&work.fingerprint,&record);
                for marker in marker_rows {
                    if let Some((_,_,hash)) = dependencies.iter().find(|(key,part,_)|key == &marker.key && part == &marker.part_id) {
                        guard.store_analysis_record(&marker.key,&marker.part_id,hash,&json!({"markers":marker.markers,"marker_group":group,"marker_dependencies":dependencies}));
                    }
                }
                guard.dirty = true; finish(&jobs,&work,"completed",None);
                }
            },
                Ok(_) => { finish(&jobs,&work,"stale",None); },
                Err(error) => { if current { guard.store_analysis_record(&work.key,&work.part,&work.fingerprint,&json!({"failure":error})); guard.dirty = true; } error!("media analysis: {error}"); finish(&jobs,&work,"failed",Some(&error)); }
            }
            // PMS finishes the activity at 100 even when its scanner cannot
            // parse the media; engine diagnostics stay in logs/private state.
            activity.insert("progress".into(),json!(100));
            guard.notify_activity(&activity,"ended"); guard.catalog.pack_pending(); guard.flush_state() };
        if let Some(snapshot) = snapshot { if let Err(error) = super::write_snapshot(&snapshot) { error!("analysis state save: {error}"); } }
        collect_unused(&jobs);
        if jobs.ephemeral && jobs.lane.lock().unwrap().stopped { let _ = std::fs::remove_dir_all(&jobs.root); }
    }
}
fn finish(jobs: &Jobs, work: &Work, state: &str, error: Option<&str>) { let mut lane = jobs.lane.lock().unwrap(); lane.status.insert(format!("{}:{}",work.key,work.part),json!({"ratingKey":work.key,"part":work.part,"state":state,"error":error,"fingerprint":work.fingerprint})); if state != "running" { lane.running = None; lane.cancelled.remove(&(work.key.clone(),work.fingerprint.clone())); } }
fn artifacts(record: &Value) -> Vec<Value> { record.get("bif").into_iter().chain(record.get("chapters").and_then(Value::as_object).into_iter().flat_map(|m| m.values())).cloned().collect() }
/// Safe orphan GC: only engine-named direct child directories, never arbitrary row paths.
fn collect_unused(jobs: &Jobs) {
    let files = std::mem::take(&mut *jobs.garbage_files.lock().unwrap());
    for path in files {
        if jobs.file_references.lock().unwrap().contains_key(&path) { continue }
        if path.parent().and_then(Path::parent) != Some(jobs.root.as_path()) { continue }
        if !path.parent().and_then(Path::file_name).and_then(|n|n.to_str()).is_some_and(|n|n.starts_with("v1-")) { continue }
        let Some(name) = path.file_name().and_then(|n|n.to_str()) else { continue };
        if name != "index-sd.bif" && !name.strip_prefix("chapter-").and_then(|n|n.strip_suffix(".jpg")).is_some_and(|n|!n.is_empty() && n.bytes().all(|b|b.is_ascii_digit())) { continue }
        if !path.parent().and_then(|p|std::fs::canonicalize(p).ok()).is_some_and(|p|p.parent() == Some(jobs.root.as_path())) { continue }
        if std::fs::symlink_metadata(&path).is_ok_and(|m|m.file_type().is_file()) { let _ = std::fs::remove_file(&path); }
        jobs.verified.lock().unwrap().remove(&path);
    }
    let candidates = std::mem::take(&mut *jobs.garbage.lock().unwrap());
    for path in candidates {
        if jobs.references.lock().unwrap().contains_key(&path) { continue }
        if path.parent() != Some(jobs.root.as_path()) || !path.file_name().and_then(|n|n.to_str()).is_some_and(|n|n.starts_with("v1-") || n.starts_with(".pending-v1-")) { continue }
        if std::fs::symlink_metadata(&path).is_ok_and(|m|m.file_type().is_dir()) { let _ = std::fs::remove_dir_all(&path); }
        jobs.verified.lock().unwrap().retain(|p,_|p.parent() != Some(path.as_path()));
    }
}

/// Feed hooks opt in explicitly. No retry of unchanged failed source jobs.
pub(super) fn schedule_for_sources(shared: &SharedRuntime, keys: &[String]) {
    let mut guard = shared.lock().unwrap();
    guard.prune_analysis(keys);
    let names = ["GenerateBIFBehavior","GenerateChapterThumbBehavior","GenerateIntroMarkerBehavior","GenerateCreditsMarkerBehavior"];
    if !names.iter().any(|name|guard.prefs.get(*name).and_then(Value::as_str) == Some("asap"))
        && !guard.section_prefs.iter().any(|((_,name),value)|names.contains(&name.as_str()) && value == "asap") { return }
    for key in keys {
        let Some(row) = guard.catalog.get(key) else { continue };
        let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
        let enabled = |name: &str| guard.section_prefs.get(&(section.clone(),name.into())).is_none_or(|v| !matches!(v.as_str(),"0"|"false"));
        let asap = |name: &str| guard.section_prefs.get(&(section.clone(),name.into())).map(String::as_str).or_else(||guard.prefs.get(name).and_then(Value::as_str)) == Some("asap");
        let marker_types = [enabled("enableIntroMarkerGeneration") && asap("GenerateIntroMarkerBehavior"), enabled("enableCreditsMarkerGeneration") && asap("GenerateCreditsMarkerBehavior")];
        let options = Options { previews: enabled("enableBIFGeneration") && asap("GenerateBIFBehavior"), chapters: asap("GenerateChapterThumbBehavior"), markers: marker_types.iter().any(|v|*v) };
        if !options.markers && !options.chapters && !options.previews { continue }
        let inputs = guard.analysis_inputs(key);
        let unchanged = { let lane = guard.analysis_jobs.lane.lock().unwrap(); inputs.iter().all(|(i,h)| lane.status.get(&format!("{}:{}",key,i.part_id)).is_some_and(|s| s["fingerprint"] == *h) || row["Media"].as_array().into_iter().flatten().any(|m| m["_analysis"][&i.part_id]["fingerprint"] == *h)) };
        if !unchanged { guard.enqueue_analysis(&[key.clone()],options,Some(marker_types)); }
    }
}

#[cfg(test)]
#[path = "analysis_tests.rs"]
mod tests;
