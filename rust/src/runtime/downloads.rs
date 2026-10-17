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
}

// ponytail: the queue's items live in memory and their files in REX_TRANSCODE_DIR, which a start
// clears, so a restart forgets every download; persist both if apps are seen re-adding after one.
impl Runtime {
    fn download_lock(&self) -> std::sync::MutexGuard<'_, Downloads> {
        self.downloads.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// `POST /downloadQueue/{id}/add?keys=...`: each key decided at once; a key the queue holds
    /// already answers its own item (measured).
    pub(super) fn download_add(&mut self, queue: &str, request: &Request, query: &HashMap<String, String>) -> Response {
        let mut added = Vec::new();
        for key in query.get("keys").map(String::as_str).unwrap_or("").split(',').filter(|key| !key.is_empty()) {
            if let Some(id) = self.download_lock().items.iter().find(|item| item.queue == queue && item.key == key).map(|item| item.id) {
                added.push(json!({"key": key, "id": id}));
                continue;
            }
            let mut decide = query.clone();
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
            let mut downloads = self.download_lock();
            downloads.next += 1;
            let id = downloads.next;
            downloads.items.push(Item {
                id,
                queue: queue.to_string(),
                key: key.to_string(),
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
        }
        self.start_download_worker();
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
        thread::spawn(move || loop {
            let job = {
                let mut downloads = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                match downloads.items.iter_mut().find(|item| item.status == "waiting") {
                    Some(item) => {
                        item.status = "processing";
                        item.session = plextv::uuid4();
                        item.plan.clone().map(|plan| (item.id, plan, Arc::clone(&item.cancel)))
                    }
                    None => {
                        downloads.working = false;
                        None
                    }
                }
            };
            let Some((id, plan, cancel)) = job else { return };
            info!("DOWNLOAD {id} converting {}", plan.rating_key);
            let progress = |seconds: f64, bytes: u64, speed: f64| {
                if let Some(item) = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).items.iter_mut().find(|item| item.id == id) {
                    item.progress = (seconds, bytes, speed);
                }
            };
            let outcome = transcoder.convert_file(id, &plan, &cancel, progress);
            let mut downloads = shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            if let Some(item) = downloads.items.iter_mut().find(|item| item.id == id) {
                match outcome {
                    Ok(file) => {
                        item.status = "available";
                        item.file = Some(file);
                    }
                    Err(error) => {
                        error!("DOWNLOAD {id} failed: {error}");
                        item.status = "error";
                    }
                }
            }
        });
    }

    /// `GET /downloadQueue/{id}/items`: each item's state, with its conversion while it runs.
    pub(super) fn download_items(&self, queue: &str) -> Response {
        let downloads = self.download_lock();
        let rows = downloads
            .items
            .iter()
            .filter(|item| item.queue == queue)
            .map(|item| {
                let mut row = json!({
                    "id": item.id,
                    "queueId": queue.parse::<i64>().map_or(json!(queue), |id| json!(id)),
                    "key": item.key,
                    "status": item.status,
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
        let query = self.download_lock().items.iter().find(|row| row.queue == queue && row.id.to_string() == item).map(|row| row.query.clone());
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
            .find(|row| row.queue == queue && row.id.to_string() == item && row.status == "available")
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
        self.pending_download = Some(FileRead { path: file, first, length, head: request.method == "HEAD" });
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
        for item in downloads.items.iter().filter(|item| item.queue == queue && ids.contains(&item.id.to_string())) {
            item.cancel.store(true, Ordering::SeqCst);
            if let Some(dir) = item.file.as_ref().and_then(|file| file.parent()) {
                let _ = std::fs::remove_dir_all(dir);
            }
        }
        downloads.items.retain(|item| !(item.queue == queue && ids.contains(&item.id.to_string())));
        Response::empty(204)
    }
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
pub(crate) fn serve_file<S: Write>(stream: &mut S, response: &Response, read: FileRead, envelope: &wire::Wire, keep: bool) -> Result<bool, String> {
    let open = wire::write_enveloped_head(stream, response, envelope, keep, Some(read.length))?;
    if read.head {
        return Ok(open);
    }
    let mut file = std::fs::File::open(&read.path).map_err(|error| error.to_string())?;
    file.seek(std::io::SeekFrom::Start(read.first)).map_err(|error| error.to_string())?;
    // A client closing mid-body is a cancelled download. Normal.
    let _ = std::io::copy(&mut file.take(read.length), stream);
    let _ = stream.flush();
    Ok(false)
}
