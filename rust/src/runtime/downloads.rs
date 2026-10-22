//! Offline downloads: the mobile apps' download queue (`/downloadQueue/{id}/add`, `/items`,
//! `/item/{id}/decision`, `/item/{id}/media`). Measured on a claimed 1.42.2 PMS through Plex
//! for Android's exact flow (`tests/fixtures/download_queue_s1422.json`): an owner with no
//! Plex Pass downloads; each item is decided at once, then converted to one MP4 file (the
//! profile's `static` target), one item at a time, and served as an attachment.

use super::*;
use std::io::Seek;
use std::sync::atomic::{AtomicBool, Ordering};

/// Asks `route_decision` for a download's decision: the profile's `static` MP4 target, and the
/// plan it decides kept in `download_plan`.
pub(super) const DOWNLOAD_MARK: &str = "rex-download";

pub(crate) struct Item {
    id: i64,
    queue: String,
    key: String,
    owner: i64,
    generation: String,
    identity: Value,
    /// The add's own decision params, replayed for `/decision`.
    query: HashMap<String, String>,
    status: &'static str,
    result: Value,
    plan: Option<universal::Plan>,
    file: Option<PathBuf>,
    /// FFmpeg's output seconds, bytes written and speed.
    progress: (f64, u64, f64),
    session: String,
    cancel: Arc<AtomicBool>,
}

#[derive(Default)]
pub(crate) struct Downloads {
    items: Vec<Item>,
    next: i64,
    working: bool,
    revision: u64,
    checkpoint: u64,
}

impl std::fmt::Debug for Downloads {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Downloads").field("items", &self.items.len()).finish()
    }
}

/// A converted file's bytes, served outside the runtime lock.
#[derive(Debug)]
pub(crate) struct FileRead {
    path: PathBuf,
    first: u64,
    length: u64,
    head: bool,
    opened: Option<std::fs::File>,
}

/// Authenticated generated artifacts use the same Range streamer as Downloads.
/// Open outside Runtime so replacement/cleanup cannot change the bytes mid-response.
pub(super) fn file_response(path: PathBuf, request: &Request, content_type: &str) -> Result<(Response, FileRead), Response> {
    let file = std::fs::File::open(&path).map_err(|_| Response::error(404, "rex: artifact not found"))?;
    let size = file.metadata().map_err(|_| Response::error(404, "rex: artifact not found"))?.len();
    let (status, first, length, content_range) = match parse_range(request.headers.get("range").map(String::as_str), size) {
        RangeResult::Whole => (200, 0, size, None),
        RangeResult::Partial(window) => (206, window.start, window.end - window.start + 1,
            Some(format!("bytes {}-{}/{size}", window.advertised_start, window.advertised_end))),
        RangeResult::Unsatisfiable => {
            let mut response = Response::error(416, "rex: range not satisfiable");
            response.headers.push(("Content-Range".into(), format!("bytes */{size}")));
            return Err(response);
        }
    };
    let mut response = Response::bytes(status, content_type);
    response.headers.push(("Accept-Ranges".into(), "bytes".into()));
    if let Some(range) = content_range { response.headers.push(("Content-Range".into(), range)); }
    response.close = true;
    Ok((response, FileRead { path, first, length, head: request.method == "HEAD", opened: Some(file) }))
}

impl Runtime {
    fn download_lock(&self) -> std::sync::MutexGuard<'_, Downloads> {
        self.downloads.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn download_identity(&self, key: &str) -> Value {
        let key = key.rsplit('/').next().unwrap_or_default();
        let Some(row) = self.catalog.get(key) else { return Value::Null };
        let part = row.get("Media").and_then(|media| media.get(0)).and_then(|media| media.get("Part")).and_then(|parts| parts.get(0)).unwrap_or(&Value::Null);
        json!({"source": row.get("_source"), "path": row.get("_path"), "id": part.get("id"), "file": part.get("file")})
    }

    fn download_visible(&self, key: &str) -> bool {
        self.catalog.get(key.rsplit('/').next().unwrap_or_default()).is_some_and(|row| self.visible(row))
    }

    fn download_directory(&self) -> PathBuf {
        self.state_path.as_ref().map(|path| PathBuf::from(format!("{}.downloads", path.display())))
            .unwrap_or_else(|| std::env::temp_dir().join(format!("rex-downloads-{}-{}", self.machine, std::process::id())))
    }

    /// The sole persisted download representation: never serialize executable plans/auth.
    pub(crate) fn download_state(&self) -> Value {
        let downloads = self.download_lock();
        json!({"next": downloads.next, "queues": self.download_queues, "items": downloads.items.iter().map(|item| {
            json!({"id":item.id,"queue":item.queue,"key":item.key,"owner":item.owner,
                "generation":item.generation,"identity":item.identity,"query":item.query,
                "status":item.status,"result":item.result,"complete":item.file.is_some()})
        }).collect::<Vec<_>>()})
    }

    pub(crate) fn restore_download_state(&mut self, state: &Value) {
        if let Some(queues) = state.get("queues").and_then(Value::as_object) {
            self.download_queues = queues.iter().map(|(key,value)| (key.clone(),value.clone())).collect();
        }
        let mut downloads = self.download_lock();
        for item in &downloads.items { item.cancel.store(true, Ordering::SeqCst); }
        downloads.items.clear();
        downloads.next = state["next"].as_i64().unwrap_or(0).max(0);
        for saved in state["items"].as_array().into_iter().flatten() {
            let (Some(id),Some(owner),Some(queue),Some(key),Some(generation)) = (
                saved["id"].as_i64(),saved["owner"].as_i64(),saved["queue"].as_str(),
                saved["key"].as_str(),saved["generation"].as_str()) else { continue };
            if id <= 0 || owner <= 0 || generation.is_empty() || !generation.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') { continue; }
            let query = saved["query"].as_object().map(|query| query.iter().filter_map(|(key,value)| {
                (!download_secret(key)).then(|| value.as_str().map(|value| (key.clone(),value.to_string()))).flatten()
            }).collect()).unwrap_or_default();
            let file = self.download_directory().join(format!("{id}-{generation}.mp4"));
            let same = saved["identity"] == self.download_identity(key) && !saved["identity"].is_null();
            let complete = same && saved["complete"] == true && file.metadata().is_ok_and(|meta| meta.is_file() && meta.len() > 0);
            downloads.next = downloads.next.max(id);
            downloads.items.push(Item { id, owner, queue:queue.into(), key:key.into(), generation:generation.into(),
                identity:saved["identity"].clone(), query, status:if complete {"available"} else {"queued"},
                result:saved["result"].clone(), plan:None, file:complete.then_some(file),
                progress:(0.0,0,0.0),session:String::new(),cancel:Arc::new(AtomicBool::new(false)) });
        }
        downloads.revision += 1;
    }

    /// Called on startup and periodically (after refreshing cloud grants).
    pub(crate) fn resume_downloads(&mut self) {
        let jobs = self.download_lock().items.iter().filter(|item| item.status == "queued")
            .map(|item| (item.id,item.owner,item.key.clone(),item.query.clone())).collect::<Vec<_>>();
        for (id, owner, key, query) in jobs {
            let grant = if owner == accounts::OWNER { Some(accounts::Grant::owner()) }
                else { self.known_accounts().into_iter().find(|grant| grant.id == owner) };
            let Some(grant) = grant else { continue };
            let previous = std::mem::replace(&mut self.viewer, grant);
            let allowed = self.download_allowed() && self.download_visible(&key);
            self.download_plan = None;
            let answer = allowed.then(|| self.route_decision(&query, &HashMap::new()));
            let plan = self.download_plan.take();
            self.viewer = previous;
            let identity = self.download_identity(&key);
            let mut downloads = self.download_lock();
            if let Some(item) = downloads.items.iter_mut().find(|item| item.id == id) {
                item.identity = identity;
                item.generation = plextv::uuid4();
                item.plan = plan;
                let answer = answer.as_ref().and_then(|answer| serde_json::from_slice::<Value>(&answer.body).ok());
                let direct = answer.as_ref().is_some_and(|answer| answer["MediaContainer"]["generalDecisionCode"] == 1000);
                item.result = answer.and_then(|answer| answer.get("MediaContainer").cloned()).map(|mut result| {
                    if let Some(result) = result.as_object_mut() {
                        result.retain(|key,_| matches!(key.as_str(),"generalDecisionCode"|"generalDecisionText"|"directPlayDecisionCode"|"directPlayDecisionText"|"transcodeDecisionCode"|"transcodeDecisionText"));
                    }
                    result
                }).unwrap_or_else(|| json!({"generalDecisionText":"Download permission or source is unavailable"}));
                item.status = if item.plan.is_some() { "waiting" } else if direct { "available" } else { "error" };
            }
            downloads.revision += 1;
        }
        self.start_download_worker();
        self.checkpoint_downloads();
    }

    /// Stop owned conversions before the parent's final shutdown snapshot.
    pub(crate) fn stop_downloads(&mut self) {
        {
            let mut downloads = self.download_lock();
            for item in &mut downloads.items {
                if matches!(item.status, "waiting" | "processing") {
                    item.cancel.store(true, Ordering::SeqCst);
                    item.status = "queued";
                    item.plan = None;
                }
            }
            downloads.revision += 1;
        }
        self.checkpoint_downloads();
    }

    /// Poll from the parent's saver before testing dirty; worker mutations are lock-local.
    pub(crate) fn checkpoint_downloads(&mut self) {
        let changed = {
            let mut downloads = self.download_lock();
            let changed = downloads.revision != downloads.checkpoint;
            downloads.checkpoint = downloads.revision;
            changed
        };
        if changed { self.dirty = true; }
    }

    pub(super) fn download_queue_route(&mut self, segments: &[&str], request: &Request, query: &HashMap<String,String>) -> Response {
        if !self.download_allowed() { return Response::error(403,"rex: downloads are not shared"); }
        if segments.is_empty() {
            if request.method != "POST" { return Response::json(200,plugin_container(Vec::new())); }
            let client = query.get("X-Plex-Client-Identifier").or_else(|| request.headers.get("x-plex-client-identifier")).cloned().unwrap_or_default();
            let owner = self.viewer.id;
            let next = self.download_queues.values().filter_map(|queue| queue["id"].as_i64()).max().unwrap_or(0) + 1;
            let queue = self.download_queues.entry(format!("{owner}:{client}")).or_insert_with(|| json!({
                "id":next,"owner":owner,"clientIdentifier":client,"itemCount":0,"status":"done"
            })).clone();
            self.dirty = true;
            return Response::json(200,json!({"MediaContainer":{"size":1,"DownloadQueue":[queue]}}));
        }
        let queue = self.download_queues.values().find(|queue| queue["id"].as_i64().map(|id| id.to_string()).as_deref() == Some(segments[0])
            && queue["owner"].as_i64() == Some(self.viewer.id)).cloned();
        let Some(queue) = queue else { return Response::error(404,"rex: not found") };
        match (request.method.as_str(), &segments[1..]) {
            ("POST",["add"]) => self.download_add(segments[0],request,query),
            ("GET",["items"]) => self.download_items(segments[0]),
            ("DELETE",["items",ids]) => self.download_delete(segments[0],ids),
            ("GET",["item",item,"decision"]) => self.download_decision(segments[0],item,request),
            ("GET"|"HEAD",["item",item,"media"]) => self.download_media(segments[0],item,request),
            ("GET"|"HEAD",[]) => Response::json(200,json!({"MediaContainer":{"size":1,"DownloadQueue":[queue]}})),
            _ => Response::error(404,"rex: not found")
        }
    }

    /// `POST /downloadQueue/{id}/add?keys=...`: each key decided at once; a key the queue holds
    /// already answers its own item (measured).
    pub(super) fn download_add(&mut self, queue: &str, request: &Request, query: &HashMap<String, String>) -> Response {
        let mut added = Vec::new();
        if !self.download_allowed() { return Response::error(403,"rex: downloads are not shared"); }
        for key in query.get("keys").map(String::as_str).unwrap_or("").split(',').filter(|key| !key.is_empty()) {
            if !self.download_visible(key) { return Response::error(404,"rex: not found"); }
            if let Some(id) = self.download_lock().items.iter().find(|item| item.owner == self.viewer.id && item.queue == queue && item.key == key).map(|item| item.id) {
                added.push(json!({"key": key, "id": id}));
                continue;
            }
            let mut decide = query.clone();
            decide.retain(|key,_| !download_secret(key));
            decide.remove("keys");
            decide.insert("path".to_string(), key.to_string());
            decide.insert("protocol".to_string(), "http".to_string());
            decide.insert(DOWNLOAD_MARK.to_string(), "1".to_string());
            self.download_plan = None;
            let answer = self.route_decision(&decide, &request.headers);
            // Not measured: what the reference does with a key it cannot decide; rex adds nothing.
            let Ok(answer) = serde_json::from_slice::<Value>(&answer.body).map(|body| body["MediaContainer"].clone()) else {
                continue;
            };
            if answer.get("generalDecisionCode").is_none() {
                continue;
            }
            let mut result = Map::new();
            for field in [
                "generalDecisionCode", "generalDecisionText", "directPlayDecisionCode", "directPlayDecisionText",
                "transcodeDecisionCode", "transcodeDecisionText",
            ] {
                if let Some(value) = answer.get(field) {
                    result.insert(field.to_string(), value.clone());
                }
            }
            let plan = self.download_plan.take();
            let direct = answer.get("generalDecisionCode").and_then(Value::as_i64) == Some(1000);
            // Not measured: the reference converted every item recorded; a direct-play decision is
            // served as the source file itself, a refusal (no encoder) is an `error` item.
            let status = if plan.is_some() { "waiting" } else if direct { "available" } else { "error" };
            let identity = self.download_identity(key);
            let mut downloads = self.download_lock();
            downloads.next += 1;
            let id = downloads.next;
            downloads.items.push(Item {
                id,
                queue: queue.to_string(),
                key: key.to_string(),
                owner: self.viewer.id,
                generation: plextv::uuid4(),
                identity,
                query: decide,
                status,
                result: Value::Object(result),
                plan,
                file: None,
                progress: (0.0, 0, 0.0),
                session: String::new(),
                cancel: Arc::new(AtomicBool::new(false)),
            });
            added.push(json!({"key": key, "id": id}));
            downloads.revision += 1;
        }
        self.start_download_worker();
        self.checkpoint_downloads();
        Response::json(200, json!({"MediaContainer": {"size": added.len(), "AddedQueueItems": added}}))
    }

    /// One worker converts the waiting items in order, as the reference does (the rest wait).
    fn start_download_worker(&self) {
        let Some(transcoder) = self.transcoder.clone() else { return };
        {
            let mut downloads = self.download_lock();
            if downloads.working || !downloads.items.iter().any(|item| item.status == "waiting") {
                return;
            }
            downloads.working = true;
        }
        let shared = Arc::clone(&self.downloads);
        let directory = self.download_directory();
        thread::spawn(move || loop {
            let job = {
                let mut downloads = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                match downloads.items.iter_mut().find(|item| item.status == "waiting") {
                    Some(item) => {
                        item.status = "processing";
                        item.session = plextv::uuid4();
                        let output = directory.join(format!("{}-{}.mp4",item.id,item.generation));
                        item.plan.clone().map(|plan| (item.id, item.generation.clone(), output, plan, Arc::clone(&item.cancel)))
                    }
                    None => {
                        downloads.working = false;
                        None
                    }
                }
            };
            let Some((id, generation, output, plan, cancel)) = job else { return };
            info!("DOWNLOAD {id} converting {}", plan.rating_key);
            let progress = |seconds: f64, bytes: u64, speed: f64| {
                if let Some(item) = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).items.iter_mut().find(|item| item.id == id && item.generation == generation) {
                    item.progress = (seconds, bytes, speed);
                }
            };
            let outcome = std::fs::create_dir_all(&directory).map_err(|error| error.to_string())
                .and_then(|_| transcoder.convert_file(&output, &plan, &cancel, progress))
                .and_then(|file| {
                    let handle = std::fs::File::open(&file).map_err(|error| error.to_string())?;
                    if handle.metadata().map_err(|error| error.to_string())?.len() == 0 { return Err("empty download output".into()); }
                    handle.sync_all().map_err(|error| error.to_string())?;
                    #[cfg(unix)]
                    std::fs::File::open(&directory).and_then(|directory| directory.sync_all()).map_err(|error| error.to_string())?;
                    Ok(file)
                });
            let mut downloads = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            downloads.revision += 1;
            if let Some(item) = downloads.items.iter_mut().find(|item| item.id == id && item.generation == generation && !item.cancel.load(Ordering::SeqCst)) {
                match outcome {
                    Ok(file) => {
                        item.status = "available";
                        item.file = Some(file);
                    }
                    Err(error) => {
                        error!("DOWNLOAD {id} failed: {error}");
                        let _ = std::fs::remove_file(&output);
                        item.status = "error";
                    }
                }
            } else {
                let _ = std::fs::remove_file(&output);
            }
        });
    }

    /// `GET /downloadQueue/{id}/items`: each item's state, with its conversion while it runs.
    pub(super) fn download_items(&self, queue: &str) -> Response {
        let downloads = self.download_lock();
        let rows = downloads
            .items
            .iter()
            .filter(|item| item.owner == self.viewer.id && item.queue == queue && self.download_visible(&item.key))
            .map(|item| {
                let mut row = json!({
                    "id": item.id,
                    "queueId": queue.parse::<i64>().map_or(json!(queue), |id| json!(id)),
                    "key": item.key,
                    "status": if item.status == "available" && (item.identity != self.download_identity(&item.key)
                        || item.file.as_ref().is_some_and(|file| !file.metadata().is_ok_and(|meta| meta.is_file() && meta.len() > 0))) { "error" } else { item.status },
                    "DecisionResult": item.result,
                });
                if let (Some(plan), "processing") = (&item.plan, item.status) {
                    row["TranscodeSession"] = download_session(item, plan);
                }
                row
            })
            .collect::<Vec<_>>();
        Response::json(200, json!({"MediaContainer": {"size": rows.len(), "DownloadQueueItem": rows}}))
    }

    /// `GET /downloadQueue/{id}/item/{item}/decision`: the add's decision again.
    pub(super) fn download_decision(&mut self, queue: &str, item: &str, request: &Request) -> Response {
        let query = self.download_lock().items.iter().find(|row| row.owner == self.viewer.id && row.queue == queue && row.id.to_string() == item && self.download_visible(&row.key)).map(|row| row.query.clone());
        match query {
            Some(query) => {
                let answer = self.route_decision(&query, &request.headers);
                self.download_plan = None;
                answer
            }
            None => Response::error(404, "rex: not found"),
        }
    }

    /// `GET|HEAD /downloadQueue/{id}/item/{item}/media`: the converted file as an attachment named
    /// `<item>.mp4`, with Range (measured: 200, or 206 with Content-Range).
    pub(super) fn download_media(&mut self, queue: &str, item: &str, request: &Request) -> Response {
        let found = self
            .download_lock()
            .items
            .iter()
            .find(|row| row.owner == self.viewer.id && row.queue == queue && row.id.to_string() == item && row.status == "available" && self.download_visible(&row.key) && row.identity == self.download_identity(&row.key))
            .map(|row| (row.file.clone(), row.key.clone()));
        let Some((file, key)) = found else {
            // Not measured: the reference was only read once an item was available.
            return Response::error(404, "rex: not found");
        };
        let Some(file) = file else {
            return self.download_source(&key, item, request);
        };
        let Ok(meta) = std::fs::metadata(&file) else {
            return Response::error(404, "rex: not found");
        };
        let size = meta.len();
        let (status, content_range, first, length) = match parse_range(request.headers.get("range").map(String::as_str), size) {
            RangeResult::Unsatisfiable => return Response::error(416, "rex: range not satisfiable"),
            RangeResult::Partial(window) => (
                206,
                Some(format!("bytes {}-{}/{size}", window.advertised_start, window.advertised_end)),
                window.start,
                window.end - window.start + 1,
            ),
            RangeResult::Whole => (200, None, 0, size),
        };
        let mut response = Response::bytes(status, "application/octet-stream");
        let modified = meta.modified().ok().and_then(|time| time.duration_since(UNIX_EPOCH).ok()).map_or(0, |since| since.as_secs() as i64);
        response.headers.extend([
            ("X-Plex-Protocol".to_string(), "1.0".to_string()),
            ("Content-Disposition".to_string(), download_disposition(&format!("{item}.mp4"))),
            ("Last-Modified".to_string(), http_date_at(modified)),
            ("Cache-Control".to_string(), "no-cache".to_string()),
            ("Accept-Ranges".to_string(), "bytes".to_string()),
        ]);
        if let Some(range) = content_range {
            response.headers.push(("Content-Range".to_string(), range));
        }
        response.close = true;
        self.pending_download = Some(FileRead { path: file, first, length, head: request.method == "HEAD", opened: None });
        response
    }

    /// A direct-play download: the source's own bytes, as a part read.
    fn download_source(&mut self, key: &str, item: &str, request: &Request) -> Response {
        let rating_key = key.rsplit('/').next().unwrap_or_default().to_string();
        let Some(row) = self.catalog.get(&rating_key).cloned() else {
            return Response::error(404, "rex: not found");
        };
        let part = row.get("Media").and_then(Value::as_array).and_then(|media| media.first()).and_then(|media| media.get("Part")).and_then(Value::as_array).and_then(|parts| parts.first()).cloned();
        let Some(part) = part else {
            return Response::error(404, "rex: not found");
        };
        let file = part.get("file").and_then(Value::as_str).unwrap_or_default().to_string();
        let Some((source, path)) = self.part_source(&row, &file) else {
            return Response::error(502, "rex: upstream failed");
        };
        let part_id = part.get("id").map(string_value).unwrap_or_default();
        let extension = file.rsplit('.').next().filter(|ext| !ext.contains('/')).unwrap_or("mkv");
        self.stream_part(&part, &part_id, source, path, Some(format!("{item}.{extension}")), request)
    }

    /// `DELETE /downloadQueue/{id}/items/{a,b}`: 204; a running conversion is stopped.
    pub(super) fn download_delete(&mut self, queue: &str, ids: &str) -> Response {
        let ids = ids.split(',').map(str::to_string).collect::<HashSet<_>>();
        let mut downloads = self.download_lock();
        for item in downloads.items.iter().filter(|item| item.owner == self.viewer.id && item.queue == queue && ids.contains(&item.id.to_string())) {
            item.cancel.store(true, Ordering::SeqCst);
            if let Some(file) = &item.file { let _ = std::fs::remove_file(file); }
        }
        downloads.items.retain(|item| !(item.owner == self.viewer.id && item.queue == queue && ids.contains(&item.id.to_string())));
        downloads.revision += 1;
        drop(downloads);
        self.checkpoint_downloads();
        Response::empty(204)
    }
}

fn download_secret(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    key.contains("token") || key.contains("authorization") || key.contains("password") || key.contains("secret")
}

/// A running download's `TranscodeSession`, as the reference listed it under its item.
fn download_session(item: &Item, plan: &universal::Plan) -> Value {
    let (seconds, bytes, speed) = item.progress;
    let duration = plan.duration_ms.max(1) as f64 / 1000.0;
    let video = plan.video.as_ref();
    let audio = plan.audio.as_ref();
    json!({
        "key": format!("/transcode/sessions/{}", item.session),
        "throttled": false,
        "complete": false,
        "progress": (seconds / duration * 100.0).min(100.0),
        "size": bytes,
        "speed": speed,
        "error": false,
        "duration": plan.duration_ms,
        "remaining": if speed > 0.0 { ((duration - seconds).max(0.0) / speed).round() as i64 } else { 0 },
        "context": "static",
        "sourceVideoCodec": video.map(|video| video.source_codec.clone()),
        "sourceAudioCodec": audio.map(|audio| audio.source_codec.clone()),
        "videoDecision": video.map(|video| if video.copy { "copy" } else { "transcode" }),
        "audioDecision": audio.map(|audio| if audio.copy { "copy" } else { "transcode" }),
        "protocol": "http",
        "container": "mp4",
        "videoCodec": video.map(|video| if video.copy { video.source_codec.clone() } else { "h264".to_string() }),
        "audioCodec": audio.map(|audio| audio.codec.clone()),
        "audioChannels": audio.map(|audio| audio.channels),
        "transcodeHwRequested": false,
        "offlineTranscode": false,
    })
}

/// Writes a `FileRead` after the head, outside the runtime lock.
pub(crate) fn serve_file<S: Write>(stream: &mut S, runtime: &SharedRuntime, response: &Response, mut read: FileRead, envelope: &wire::Wire, keep: bool) -> Result<bool, String> {
    let mut file = match read.opened.take() {
        Some(file) => file,
        None => std::fs::File::open(&read.path).map_err(|error| error.to_string())?,
    };
    file.seek(std::io::SeekFrom::Start(read.first)).map_err(|error| error.to_string())?;
    let open = wire::write_enveloped_head(stream, response, envelope, keep, Some(read.length))?;
    if read.head { return Ok(open); }
    let mut remaining = read.length;
    let mut written = 0_u64;
    let mut buffer = [0_u8; 65_536];
    'body: while remaining > 0 {
        let limit = (remaining.min(buffer.len() as u64)) as usize;
        let Ok(count) = file.read(&mut buffer[..limit]) else { break };
        if count == 0 { break; }
        let mut bytes = &buffer[..count];
        while !bytes.is_empty() {
            match stream.write(bytes) {
                Ok(0) | Err(_) => break 'body,
                Ok(count) => { written += count as u64; bytes = &bytes[count..]; }
            }
        }
        remaining -= count as u64;
    }
    let _ = stream.flush();
    let owner = sessions::BYTE_OWNER.with(|owner| owner.borrow().clone());
    runtime.lock().unwrap_or_else(PoisonError::into_inner).record_bytes(&owner, written);
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runtime() -> (Runtime, PathBuf) {
        let root = std::env::temp_dir().join(format!("rex-download-test-{}",plextv::uuid4()));
        std::fs::create_dir_all(&root).unwrap();
        let catalog = root.join("catalog.json");
        std::fs::write(&catalog,json!({"machineIdentifier":"download-tests","friendlyName":"downloads",
            "sections":[{"key":"1","type":"movie","title":"Movies"}],
            "items":[{"ratingKey":"10","key":"/library/metadata/10","type":"movie","title":"Fixture",
                "librarySectionID":"1","contentRating":"PG","Media":[{"Part":[{"id":"100","file":"/fixture.mp4"}]}]}]}).to_string()).unwrap();
        let mut runtime = Runtime::load(RuntimeOptions { catalog, state:Some(root.join("state.db")), ..RuntimeOptions::default() }).unwrap();
        runtime.transcoder = None;
        (runtime,root)
    }

    fn request(method: &str) -> Request {
        Request { method:method.into(),target:"/downloadQueue".into(),version:"HTTP/1.1".into(),
            headers:HashMap::new(),body:Vec::new(),body_complete:true }
    }

    fn body(response: Response) -> Value { serde_json::from_slice(&response.body).unwrap() }

    fn completed(runtime: &mut Runtime, owner: i64) -> Vec<u8> {
        let media = include_bytes!("../../../tests/fixtures/transcode/mov_text_faststart.mp4").to_vec();
        let generation = plextv::uuid4();
        std::fs::create_dir_all(runtime.download_directory()).unwrap();
        std::fs::write(runtime.download_directory().join(format!("1-{generation}.mp4")),&media).unwrap();
        let identity = runtime.download_identity("/library/metadata/10");
        runtime.restore_download_state(&json!({"next":1,"queues":{"fixture":{"id":1,"owner":owner}},
            "items":[{"id":1,"owner":owner,"queue":"1","key":"/library/metadata/10","generation":generation,
                "identity":identity,"query":{"path":"/library/metadata/10","protocol":"http","rex-download":"1"},
                "status":"available","complete":true,"result":{}}]}));
        media
    }

    #[test]
    fn completed_fixture_survives_restart_and_serves_range_only_to_its_account() {
        let (mut runtime,root) = runtime();
        let media = completed(&mut runtime,accounts::OWNER);
        let saved = runtime.download_state();
        let mut get = request("GET");
        get.headers.insert("range".into(),"bytes=4-11".into());
        assert_eq!(runtime.download_media("1","1",&get).status,206);
        let read = runtime.pending_download.take().unwrap();
        let mut file = std::fs::File::open(read.path).unwrap();
        file.seek(std::io::SeekFrom::Start(read.first)).unwrap();
        let mut bytes = Vec::new();
        file.take(read.length).read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes,&media[4..12]);
        drop(runtime);
        let mut runtime = Runtime::load(RuntimeOptions { catalog:root.join("catalog.json"),
            state:Some(root.join("state.db")), ..RuntimeOptions::default() }).unwrap();
        runtime.transcoder = None;
        runtime.restore_download_state(&saved);
        assert_eq!(runtime.download_media("1","1",&get).status,206);
        assert_eq!(body(runtime.download_items("1"))["MediaContainer"]["DownloadQueueItem"][0]["status"],"available");
        runtime.viewer.id = 2;
        runtime.viewer.allow_sync = true;
        assert_eq!(runtime.download_queue_route(&["1","items"],&get,&HashMap::new()).status,404);
        assert_eq!(runtime.download_media("1","1",&get).status,404);
        assert_eq!(body(runtime.download_items("1"))["MediaContainer"]["size"],0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn cloud_sync_permission_and_local_restrictions_never_broaden_access() {
        let (mut runtime,root) = runtime();
        runtime.viewer.id = 2;
        runtime.viewer.allow_sync = false;
        assert_eq!(runtime.download_queue_route(&[],&request("POST"),&HashMap::new()).status,403);
        runtime.set_share_restrictions(2,&json!({"allowSync":true})).unwrap();
        assert!(!runtime.download_allowed());
        runtime.viewer.allow_sync = true;
        let queue = body(runtime.download_queue_route(&[],&request("POST"),&HashMap::new()));
        assert_eq!(queue["MediaContainer"]["DownloadQueue"][0]["owner"],2);
        completed(&mut runtime,2);
        assert_eq!(runtime.download_media("1","1",&request("GET")).status,200);
        runtime.set_share_restrictions(2,&json!({"filterMovies":"contentRating=G"})).unwrap();
        assert_eq!(runtime.download_media("1","1",&request("GET")).status,404);
        runtime.remove_share_restrictions(2).unwrap();
        assert_eq!(runtime.download_media("1","1",&request("GET")).status,200);
        runtime.viewer.sections = Some(HashSet::from(["other".into()]));
        assert_eq!(runtime.download_media("1","1",&request("GET")).status,404);
        assert!(runtime.set_share_restrictions(2,&json!({"filterMovies":"unknown=x"})).is_err());
        assert!(runtime.set_share_restrictions(2,&json!({"filterMovies":"label="})).is_err());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn missing_output_and_processing_are_redecided_without_saved_credentials() {
        let (mut runtime,root) = runtime();
        completed(&mut runtime,accounts::OWNER);
        let mut saved = runtime.download_state();
        let generation = saved["items"][0]["generation"].as_str().unwrap();
        std::fs::remove_file(runtime.download_directory().join(format!("1-{generation}.mp4"))).unwrap();
        saved["items"][0]["query"]["X-Plex-Token"] = json!("never-save");
        saved["items"][0]["status"] = json!("processing");
        runtime.restore_download_state(&saved);
        assert_eq!(body(runtime.download_items("1"))["MediaContainer"]["DownloadQueueItem"][0]["status"],"queued");
        assert!(!runtime.download_state().to_string().contains("never-save"));
        assert_eq!(runtime.download_media("1","1",&request("GET")).status,404);
        runtime.resume_downloads();
        assert_ne!(body(runtime.download_items("1"))["MediaContainer"]["DownloadQueueItem"][0]["status"],"processing");
        let cancel = Arc::clone(&runtime.download_lock().items[0].cancel);
        runtime.download_delete("1","1");
        assert_eq!(body(runtime.download_items("1"))["MediaContainer"]["size"],0);
        assert!(cancel.load(Ordering::SeqCst));
        assert_eq!(runtime.download_state()["next"],1,"deletion must not reuse an in-flight job ID");
        let _ = std::fs::remove_dir_all(root);
    }
}
