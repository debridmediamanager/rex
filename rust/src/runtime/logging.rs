//! The oracle's `logging` set-up in `server.py` `main()`, and the surfaces that read it.
//!
//! Three sinks, as there:
//!
//! * stderr, `logging.basicConfig`'s `%(asctime)s %(levelname)-7s %(message)s` with
//!   `%H:%M:%S`, at DEBUG under `--verbose` and INFO otherwise;
//! * the in-memory lines Download Logs zips (`sim.log_lines`), in the Console page's
//!   `MMM dd, yyyy HH:mm:ss.SSS [thread] LEVEL - message` format, kept from the moment
//!   the capture handler is attached -- after the catalog, the claim and the state file
//!   are loaded, so those first lines reach stderr only, as they do there;
//! * one `LogSentNotification` per line for channels opened with `filters=log`
//!   (`push_log`), which the runtime drains into its event buffer.
//!
//! Global rather than on the runtime because the plex.tv threads log without the
//! runtime lock, and a request logs while holding it.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::{json, Value};

static VERBOSE: AtomicBool = AtomicBool::new(false);
static CAPTURING: AtomicBool = AtomicBool::new(false);
static THREADS: AtomicU64 = AtomicU64::new(0);

struct Sink {
    lines: Vec<String>,
    rows: Vec<Value>,
}

/// A request target with every `X-Plex-Token` value masked, plain or URL-encoded (the
/// `url=` of `/photo/:/transcode` carries one): the request line and `requests.log` are what
/// people hand over for support. A real PMS's own log masks every token as a run of `x`.
pub(crate) fn redact_tokens(target: &str) -> String {
    static TOKEN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    TOKEN
        .get_or_init(|| regex::Regex::new(r"(?i)(x-plex-token(?:=|%3d))[A-Za-z0-9_.~-]+").unwrap())
        .replace_all(target, "${1}xxxxxxxxxxxxxxxxxxxx")
        .into_owned()
}

static SINK: Mutex<Sink> = Mutex::new(Sink {
    lines: Vec::new(),
    rows: Vec::new(),
});

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Level {
    Debug,
    Info,
    Warning,
    Error,
}

impl Level {
    fn name(self) -> &'static str {
        match self {
            Level::Debug => "DEBUG",
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
    }

    /// `push_log`'s level numbers, measured on a `filters=log` channel.
    fn number(self) -> i64 {
        match self {
            Level::Debug => 4,
            Level::Info => 3,
            Level::Warning => 2,
            Level::Error => 1,
        }
    }
}

pub(crate) fn set_verbose(verbose: bool) {
    VERBOSE.store(verbose, Ordering::SeqCst);
}

pub(crate) fn verbose() -> bool {
    VERBOSE.load(Ordering::SeqCst)
}

/// `logger.addHandler(_Capture())`: from here on lines are kept and pushed.
pub(crate) fn start_capture() {
    CAPTURING.store(true, Ordering::SeqCst);
}

/// A name for a connection thread the way `threading.Thread` numbers its own:
/// `socketserver.ThreadingMixIn` starts one per request, `Thread-N (process_request_thread)`.
pub(crate) fn request_thread_name() -> String {
    let number = THREADS.fetch_add(1, Ordering::SeqCst) + 1;
    format!("Thread-{number} (process_request_thread)")
}

fn local(timestamp: i64) -> libc::tm {
    // SAFETY: an all-zero tm is a valid value (the epoch's fields) for the rare failure.
    crate::platform::local_tm(timestamp).unwrap_or_else(|| unsafe { std::mem::zeroed() })
}

const MONTHS: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// `time.strftime("%b %d, %Y %H:%M:%S", localtime) + ".%03d" % msecs`.
fn console_time(seconds: i64, millis: u32) -> String {
    let tm = local(seconds);
    format!(
        "{} {:02}, {} {:02}:{:02}:{:02}.{:03}",
        MONTHS[(tm.tm_mon.clamp(0, 11)) as usize],
        tm.tm_mday,
        tm.tm_year + 1900,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        millis
    )
}

thread_local! {
    static THREAD_NAME: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

/// Names the calling thread for the log, for threads `std::thread::spawn` started.
pub(crate) fn name_thread(name: String) {
    THREAD_NAME.with(|slot| *slot.borrow_mut() = Some(name));
}

fn thread_name() -> String {
    if let Some(name) = THREAD_NAME.with(|slot| slot.borrow().clone()) {
        return name;
    }
    match std::thread::current().name() {
        Some("main") => "MainThread".to_string(),
        Some(name) => name.to_string(),
        None => "Thread".to_string(),
    }
}

pub(crate) fn log(level: Level, message: &str) {
    if level == Level::Debug && !verbose() {
        return;
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let seconds = now.as_secs() as i64;
    let millis = now.subsec_millis();
    let tm = local(seconds);
    eprintln!(
        "{:02}:{:02}:{:02} {:<7} {}",
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        level.name(),
        message
    );
    if !CAPTURING.load(Ordering::SeqCst) {
        return;
    }
    let time = console_time(seconds, millis);
    let line = format!("{time} [{}] {} - {message}", thread_name(), level.name());
    // `record.thread` is `threading.get_ident()`, which is `pthread_self()`.
    let ident = crate::platform::thread_ident();
    let row = json!({
        "level": level.number(),
        "thread": ident.to_string(),
        "time": time,
        "message": message,
    });
    let mut sink = SINK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    sink.lines.push(line);
    if sink.lines.len() > 5000 {
        sink.lines.drain(..1000);
    }
    sink.rows.push(row);
    if sink.rows.len() > 5000 {
        sink.rows.drain(..1000);
    }
}

/// `%s` of an optional query value, the way the oracle formats `query.get(name)`.
pub(crate) fn py_opt(value: Option<&String>) -> String {
    value.cloned().unwrap_or_else(|| "None".to_string())
}

/// `%s` of a stored value the way Python's `str()` writes it: `True`, `4`, `1.0`.
pub(crate) fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        Value::Number(number) if number.is_f64() => {
            let float = number.as_f64().unwrap_or_default();
            if float.fract() == 0.0 && float.abs() < 1e16 {
                format!("{float:.1}")
            } else {
                format!("{float}")
            }
        }
        other => other.to_string(),
    }
}

pub(crate) fn lines() -> Vec<String> {
    SINK.lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .lines
        .clone()
}

/// The `LogSentNotification` rows logged since the last drain.
pub(crate) fn drain_rows() -> Vec<Value> {
    std::mem::take(
        &mut SINK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .rows,
    )
}

macro_rules! info {
    ($($arg:tt)*) => { $crate::runtime::logging::log($crate::runtime::logging::Level::Info, &format!($($arg)*)) };
}
macro_rules! warning {
    ($($arg:tt)*) => { $crate::runtime::logging::log($crate::runtime::logging::Level::Warning, &format!($($arg)*)) };
}
macro_rules! error {
    ($($arg:tt)*) => { $crate::runtime::logging::log($crate::runtime::logging::Level::Error, &format!($($arg)*)) };
}
macro_rules! debug {
    ($($arg:tt)*) => { $crate::runtime::logging::log($crate::runtime::logging::Level::Debug, &format!($($arg)*)) };
}
pub(crate) use {debug, error, info, warning};

/// A stored zip of named text files (`zipfile.ZipFile(..., ZIP_DEFLATED)`): deflated
/// entries with CRC-32, sizes and a central directory, readable by any unzip.
pub(crate) fn zip(files: &[(&str, String)]) -> Vec<u8> {
    use flate2::write::DeflateEncoder;
    use flate2::Compression;
    use std::io::Write;

    let (date, time) = dos_now();
    let mut out = Vec::new();
    let mut central = Vec::new();
    for (name, text) in files {
        let data = text.as_bytes();
        let crc = crc32(data);
        let mut encoder = DeflateEncoder::new(Vec::new(), Compression::default());
        let _ = encoder.write_all(data);
        let compressed = encoder.finish().unwrap_or_default();
        let offset = out.len() as u32;
        let name = name.as_bytes();
        let mut local = Vec::new();
        local.extend_from_slice(&0x0403_4b50_u32.to_le_bytes());
        local.extend_from_slice(&20_u16.to_le_bytes());
        local.extend_from_slice(&0_u16.to_le_bytes());
        local.extend_from_slice(&8_u16.to_le_bytes());
        local.extend_from_slice(&time.to_le_bytes());
        local.extend_from_slice(&date.to_le_bytes());
        local.extend_from_slice(&crc.to_le_bytes());
        local.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
        local.extend_from_slice(&(data.len() as u32).to_le_bytes());
        local.extend_from_slice(&(name.len() as u16).to_le_bytes());
        local.extend_from_slice(&0_u16.to_le_bytes());
        local.extend_from_slice(name);
        out.extend_from_slice(&local);
        out.extend_from_slice(&compressed);
        central.extend_from_slice(&0x0201_4b50_u32.to_le_bytes());
        central.extend_from_slice(&20_u16.to_le_bytes());
        central.extend_from_slice(&20_u16.to_le_bytes());
        central.extend_from_slice(&0_u16.to_le_bytes());
        central.extend_from_slice(&8_u16.to_le_bytes());
        central.extend_from_slice(&time.to_le_bytes());
        central.extend_from_slice(&date.to_le_bytes());
        central.extend_from_slice(&crc.to_le_bytes());
        central.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
        central.extend_from_slice(&(data.len() as u32).to_le_bytes());
        central.extend_from_slice(&(name.len() as u16).to_le_bytes());
        central.extend_from_slice(&[0; 8]);
        central.extend_from_slice(&0o600_u32.wrapping_shl(16).to_le_bytes());
        central.extend_from_slice(&offset.to_le_bytes());
        central.extend_from_slice(name);
    }
    let start = out.len() as u32;
    out.extend_from_slice(&central);
    out.extend_from_slice(&0x0605_4b50_u32.to_le_bytes());
    out.extend_from_slice(&[0; 4]);
    out.extend_from_slice(&(files.len() as u16).to_le_bytes());
    out.extend_from_slice(&(files.len() as u16).to_le_bytes());
    out.extend_from_slice(&(central.len() as u32).to_le_bytes());
    out.extend_from_slice(&start.to_le_bytes());
    out.extend_from_slice(&0_u16.to_le_bytes());
    out
}

fn dos_now() -> (u16, u16) {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let tm = local(seconds);
    let date = (((tm.tm_year + 1900 - 1980).max(0) as u16) << 9)
        | (((tm.tm_mon + 1) as u16) << 5)
        | tm.tm_mday as u16;
    let time = ((tm.tm_hour as u16) << 11) | ((tm.tm_min as u16) << 5) | (tm.tm_sec as u16 / 2);
    (date, time)
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFF_u32;
    for byte in data {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

impl super::Runtime {
    /// `log_archive`: Download Logs' zip of this process's own log, the requests it
    /// answered and the ones it could not route.
    pub(super) fn log_archive(&self) -> Vec<u8> {
        let joined = |lines: &[String]| format!("{}\n", lines.join("\n"));
        let requests = &self.requests[self.requests.len().saturating_sub(2000)..];
        zip(&[
            ("rex.log", joined(&lines())),
            ("requests.log", joined(requests)),
            ("unrouted.log", joined(&self.unknown)),
        ])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_matches_the_check_value() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }
}
