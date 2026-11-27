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

/// Text with every token masked: `X-Plex-Token=`, `token=` (a claim code), `access_token=` and
/// `auth_token=` (rex's own plex.tv calls),
/// plain or percent-encoded once or more (the `url=` of `/photo/:/transcode` carries one, and a url
/// naming another transcode carries it twice); the `X-Plex-Token` and `Authorization` headers;
/// a token-named value quoted as JSON or an XML attribute (plex.tv's `authToken="..."`); and a
/// url's userinfo (`http://user:password@host`, card 859).
/// The spellings `harness/scripts/redact.py` scrubs from real client traffic and rex-android's
/// `redact()` hides. `log` applies it to every line, so a message that formats a url cannot
/// leak one (card 850: artwork lines carried the token at INFO after card 295 masked only
/// the request line); a value cut short is masked too, since its name comes first. A real
/// PMS's own log masks every token as a run of `x`.
pub(crate) fn redact_tokens(text: &str) -> String {
    redact(text).into_owned()
}

fn redact(text: &str) -> std::borrow::Cow<'_, str> {
    static TOKEN: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    TOKEN
        .get_or_init(|| {
            // A name starts the text, follows a non-word character or a percent escape: in a
            // url's encoded query `?token=` is `%3Ftoken%3D`, which no `\b` precedes. ASCII
            // classes and boundaries keep the lazy DFA on every line.
            regex::Regex::new(concat!(
                r#"(?i-u)((?:%(?:25)*[0-9a-f]{2}|\b)(?:(?:x-plex-token|token|access_token|auth_token)(?:=|%(?:25)*3d)"#,
                r#"|x-plex-token(?::|%(?:25)*3a)(?:\s|%(?:25)*20|\+)*)"#,
                r#"|"[a-z_-]*token"\s*:\s*"|\b[a-z_-]*token=")[A-Za-z0-9_.~-]+"#,
                // Base64 for these two: `+`, `/` and `=` are part of the value, never of a path.
                r#"|((?:%(?:25)*[0-9a-f]{2}|\b)authorization(?::|%(?:25)*3a)(?:\s|%(?:25)*20|\+)*(?:bearer|basic)(?:\s|%(?:25)*20|\+)+)[A-Za-z0-9_.~+/=-]+"#,
                r#"|((?:%(?:25)*[0-9a-f]{2}|\b)bearer(?:\s|%(?:25)*20|\+)+)[A-Za-z0-9_.~+/=-]{8,}"#,
                // A url's `user:password@`, plain or percent-encoded (inside `url=`): its whole
                // userinfo, as zurg's log masks it. Never past the authority's `/`, `?`, `#`.
                r#"|(\b[a-z][a-z0-9+.-]*(?::|%(?:25)*3a)(?:/|%(?:25)*2f){2})"#,
                r#"(?:[A-Za-z0-9._~!$&()*+,;=:-]|%(?:25)*(?:[015-9a-f][0-9a-f]|2[0-24-9a-e]|3[0-9a-e]|4[1-9a-f]))+(@|%(?:25)*40)"#,
            ))
            .unwrap()
        })
        .replace_all(text, "${1}${2}${3}${4}xxxxxxxxxxxxxxxxxxxx${5}")
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
    let redacted = redact(message);
    let message = redacted.as_ref();
    // One message is one line: request text (a percent-decoded path, a client's `/log`
    // message) carried line breaks that wrote lines of its choosing into Download Logs.
    let escaped;
    let message = if message.contains(['\r', '\n']) {
        escaped = message.replace('\r', "\\r").replace('\n', "\\n");
        escaped.as_str()
    } else {
        message
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let seconds = now.as_secs() as i64;
    let millis = now.subsec_millis();
    let tm = local(seconds);
    // Not `eprintln!`, which panics when stderr is gone: Android's `Process.destroy()` closes
    // the log pipe as it sends SIGTERM, and the panic killed the thread saving and stopping rex,
    // which then never exited (zurg+rex on a Pixel 6, 2026-11-09).
    let _ = std::io::Write::write_fmt(
        &mut std::io::stderr(),
        format_args!("{:02}:{:02}:{:02} {:<7} {}\n", tm.tm_hour, tm.tm_min, tm.tm_sec, level.name(), message),
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

/// PMS's Download Logs filename (recorded from 1.43.5 on 2026-10-22).
pub(super) fn archive_disposition() -> String {
    let tm = local(super::now());
    format!(
        "attachment; filename=\"Plex Media Server Logs_{:04}-{:02}-{:02}_{:02}-{:02}-{:02}.zip\"",
        tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec
    )
}

impl super::Runtime {
    /// `log_archive`: Download Logs' zip of this process's own log, the requests it
    /// answered and the ones it could not route.
    pub(super) fn log_archive(&self) -> Vec<u8> {
        let joined = |lines: &[String]| format!("{}\n", lines.join("\n"));
        let requests = &self.requests[self.requests.len().saturating_sub(2000)..];
        // `log` masked the first already; the other two are kept beside it, not logged.
        zip(&[
            ("Plex Media Server.log", joined(&lines())),
            ("requests.log", redact_tokens(&joined(requests))),
            ("unrouted.log", redact_tokens(&joined(&self.unknown))),
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

    #[test]
    fn redacts_plex_tokens_and_claim_codes() {
        assert_eq!(redact_tokens("/library?X-Plex-Token=abc123"), "/library?X-Plex-Token=xxxxxxxxxxxxxxxxxxxx");
        assert_eq!(redact_tokens("/myplex/claim?token=claim-SECRETabc123"), "/myplex/claim?token=xxxxxxxxxxxxxxxxxxxx");
        assert_eq!(redact_tokens("/rex/sources?accessToken=keep"), "/rex/sources?accessToken=keep");
    }

    /// Card 859: every case of the table rex-android's `redact()` checks too (the same file is
    /// its `app/src/test/resources/redaction/log_redaction_cases.json`): its secrets gone, the
    /// rest of the line kept.
    #[test]
    fn the_shared_redaction_cases_are_masked() {
        let table: Value = serde_json::from_str(include_str!("../../../tests/fixtures/log_redaction_cases.json")).unwrap();
        let mut failed = Vec::new();
        for case in table["cases"].as_array().unwrap() {
            let line = case["line"].as_str().unwrap();
            let masked = redact_tokens(line);
            let strings = |name: &str| case[name].as_array().unwrap().iter().map(|value| value.as_str().unwrap().to_string()).collect::<Vec<_>>();
            if strings("secrets").iter().any(|secret| masked.contains(secret.as_str()))
                || !strings("kept").iter().all(|kept| masked.contains(kept.as_str()))
            {
                failed.push(format!("{line}\n  -> {masked}"));
            }
        }
        assert!(failed.is_empty(), "{}", failed.join("\n"));
    }

    /// Card 850: what `info!` formats reaches stderr, Download Logs and `filters=log` masked,
    /// whatever line it is in and however the token is spelt, cut short by the line included.
    #[test]
    fn every_logged_line_has_its_tokens_masked() {
        start_capture();
        let url = "http://127.0.0.1:19920/library/metadata/1007/thumb/1?X-Plex-Token=SINK850abcdefghijklmnop";
        let logged = [
            format!("ARTWORK fetch {}", url.chars().take(110).collect::<String>()),
            format!("ARTWORK 1007 posters -> {}", url.chars().take(80).collect::<String>()),
            "artwork fetch failed for http://h/photo/:/transcode?url=http%3A%2F%2Fh%2Fx%3FX-Plex-Token%3DSINK850a%26w%3D1".into(),
            "PLAYQUEUE 1 -> 1 item(s) from uri=server://m/x?X-Plex-Token%253DSINK850b".into(),
            "unsupported url \"ftp://h/x?token=SINK850c\"".into(),
            "CLIENT LOG [3] web: X-Plex-Token: SINK850d Authorization: Bearer SINK850e".into(),
            "plex.tv answered {\"authToken\":\"SINK850f\"} <user authToken=\"SINK850g\"/>".into(),
            "\"GET /photo/:/transcode?url=http%3A%2F%2Fh%2Fposter.jpg%3Ftoken%3DSINK850h%26w%3D1 HTTP/1.1\" 404".into(),
            "\"POST /log?message=headers%20X-Plex-Token%3A%20SINK850i%20Authorization%3A%20Bearer%20SINK850j\" 200".into(),
            "url=http%253A%252F%252Fh%252Fx%253Ftoken%253DSINK850k%2526access_token%253DSINK850l".into(),
            "plex.tv servers.xml?auth_token=SINK850m failed".into(),
        ];
        for line in &logged {
            log(Level::Info, line);
        }
        let kept: Vec<String> = lines().into_iter().filter(|line| line.contains("SINK850") || line.contains("ARTWORK 1007")).collect();
        assert!(kept.iter().all(|line| !line.contains("SINK850")), "{kept:#?}");
        assert!(
            kept.iter().any(|line| line.ends_with("ARTWORK 1007 posters -> http://127.0.0.1:19920/library/metadata/1007/thumb/1?X-Plex-Token=xxxxxxxxxxxxxxxxxxxx")),
            "{kept:#?}"
        );
    }
}
