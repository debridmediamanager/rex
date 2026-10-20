//! `rex manager`: start and stop rex servers from stored configurations (the port of
//! manager.py, whose `managed/` directory it keeps working on).
//!
//! One long-lived process that, by default, runs no rex server at all. A server is created
//! from a configuration (port, keys, catalog, options), kept stopped until it is started,
//! and supervised once it is: an unexpected exit is restarted with a backoff, and a server
//! that keeps dying is marked failed instead of looping. What was running when the manager
//! stopped is running again when it starts, and nothing else.
//!
//! ```text
//! rex manager serve [--root managed] [--listen 127.0.0.1:32300] [--binary PATH]
//! rex manager create living-room --port 32400 --catalog catalog.json   # keys from env
//! rex manager start living-room
//! rex manager list | status NAME | logs NAME | stop NAME | restart NAME | delete NAME
//! rex manager create den --port 32401           # no catalog: seeded, then filled by its sources
//! rex manager build den --start                 # rex catalog --seed into its database, then start
//! ```
//!
//! Everything lives under the root (default `managed/` at the checkout's root, mode 700):
//!
//! ```text
//! manager.db              the servers and their configurations (mode 600: it holds keys)
//! admin.token             the API's bearer token, generated on first start (mode 600)
//! servers/<name>/         rex.db (the catalog and the server's state in the same file),
//!                         server.log, build.log, plex_cache.db ...
//! ```
//!
//! The API (JSON, `Authorization: Bearer <admin token>` on every route but the page at /):
//!
//! ```text
//! GET    /servers                     every server and its status
//! POST   /servers                     create (stopped)
//! GET    /servers/<name>              one server, keys redacted
//! PATCH  /servers/<name>              change its configuration (stopped servers only)
//! POST   /servers/<name>/start|stop|restart
//! DELETE /servers/<name>[?purge=1]    stop and forget it; purge also removes its files
//! GET    /servers/<name>/logs?lines=N
//! POST   /servers/<name>/build        run rex catalog --seed for it
//! GET    /servers/<name>/build        that job's status and log tail
//! ```
//!
//! Secrets reach a server through its environment (REX_TOKEN, REX_DIRECT_KEY), never
//! its command line, which `ps` shows. A server's sources register with it themselves
//! (docs/feed-api.md): nothing about them is configured here. Linux and
//! macOS only: servers are process groups of their own, stopped with signals.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::types::Value as Sql;
use rusqlite::{params, params_from_iter, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::catalog_tool::{checkout_root, py_dumps, sha1_hex};
use crate::{platform, store};

const DEFAULT_LISTEN: &str = "127.0.0.1:32300";
// `tmdb_key` reaches no server since rex reads no TMDB (2026-10-17), nor `dmm_api_key` since
// rex-direct needs no key, nor `plex_token` since rex asks plex.tv with its claim only
// (2026-10-19); a configuration saved before then still carries one, which stays accepted
// and redacted.
const SECRETS: &[&str] = &["tmdb_key", "plex_token", "token", "dmm_api_key", "rex_direct_key"];
/// What each environment field is called in the server's environment.
const SECRET_ENV: &[(&str, &str)] = &[
    ("token", "REX_TOKEN"),
    // rex-direct's provider and its key (`runtime/rexdirect.rs`); the URL is no secret, but
    // reaches the server the same way.
    ("rex_direct_key", "REX_DIRECT_KEY"),
    ("rex_direct_url", "REX_DIRECT_URL"),
];
/// Seconds between SIGTERM and SIGKILL; the server saves on SIGTERM.
const STOP_GRACE: Duration = Duration::from_secs(15);
/// A server that fails this often within ten minutes ...
const FAILURE_WINDOW: f64 = 600.0;
/// ... is marked failed rather than restarted again.
const FAILURE_LIMIT: usize = 5;
const LOG_LIMIT: u64 = 10 * 1024 * 1024;
// Keep the manager's small HTTP envelope bounded before it can allocate a body. These
// match the native server's request limits, while the manager deliberately keeps its own
// parser because its API is a separate contract.
const MAX_HEADER_BYTES: usize = 256 * 1024;
const MAX_HEADER_LINE_BYTES: usize = 64 * 1024;
const MAX_HEADER_LINES: usize = 100;
const MAX_TARGET_BYTES: usize = 32 * 1024;
const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

/// manager.py's schema: a database it wrote is read as it is.
const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS servers(
    name TEXT PRIMARY KEY, config TEXT NOT NULL, desired TEXT NOT NULL,
    pid INTEGER, status TEXT NOT NULL, last_exit TEXT, last_error TEXT,
    started_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS builds(
    name TEXT PRIMARY KEY, status TEXT NOT NULL, exit INTEGER,
    started_at REAL, ended_at REAL);
";

/// A request the manager answers with a 4xx (or a 500): (status, message).
#[derive(Debug)]
struct Refused(u16, String);

impl From<rusqlite::Error> for Refused {
    fn from(error: rusqlite::Error) -> Self {
        Refused(500, format!("sqlite: {error}"))
    }
}

impl From<std::io::Error> for Refused {
    fn from(error: std::io::Error) -> Self {
        Refused(500, format!("OSError: {error}"))
    }
}

type Answer<T> = Result<T, Refused>;

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// Python's truthiness of a JSON value.
fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(crate::metadata::truthy)
}

fn text<'a>(config: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    config.get(field).and_then(Value::as_str).filter(|value| !value.is_empty())
}

// -- configuration ------------------------------------------------------------------

/// The configuration as stored, or a 400. `partial` checks a PATCH's fields.
fn validate(config: Value, partial: bool) -> Answer<Map<String, Value>> {
    let refuse = |message: String| Err(Refused(400, message));
    let Value::Object(mut out) = config else {
        return refuse("the configuration must be a JSON object".to_string());
    };
    const KNOWN: &[&str] = &[
        "name", "port", "catalog", "zurg_mount", "tmdb_key",
        "plex_token", "token", "dmm_api_key", "rex_direct_key", "rex_direct_url", "friendly_name", "part_mode", "host", "gdm", "announce_url", "public_url",
        "web_root", "assert_claimed", "verbose", "extra_args", "restart",
    ];
    let mut unknown = out.keys().filter(|key| !KNOWN.contains(&key.as_str())).cloned().collect::<Vec<_>>();
    if !unknown.is_empty() {
        unknown.sort();
        return refuse(format!("unknown field(s): {}", unknown.join(", ")));
    }
    // No catalog is needed to create a server: one created from its keys alone gets its
    // catalog from `build` (an empty library at zurg's mount) before it can start.
    if !partial {
        for field in ["name", "port"] {
            if matches!(out.get(field), None | Some(Value::Null)) || out.get(field) == Some(&json!("")) {
                return refuse(format!("{field} is required"));
            }
        }
    }
    if let Some(name) = out.get("name") {
        let valid = name.as_str().is_some_and(|name| {
            (1..=40).contains(&name.len()) && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        });
        if !valid {
            return refuse("name must be 1-40 characters of a-z, 0-9 and -".to_string());
        }
    }
    if let Some(port) = out.get("port") {
        if !port.as_i64().is_some_and(|port| (1024..=65535).contains(&port)) {
            return refuse("port must be an integer from 1024 to 65535".to_string());
        }
    }
    if let Some(catalog) = out.get("catalog").filter(|catalog| !catalog.is_null()) {
        let catalog = if catalog.is_object() { catalog.get("path").unwrap_or(&Value::Null) } else { catalog };
        let Some(path) = catalog.as_str().filter(|path| Path::new(path).is_absolute()) else {
            return refuse("catalog must be an absolute path to a .json or .db catalog".to_string());
        };
        if !Path::new(path).is_file() {
            return refuse(format!("catalog {path} does not exist"));
        }
        let path = path.to_string();
        out.insert("catalog".to_string(), Value::String(path));
    }
    for field in [
        "zurg_mount", "tmdb_key", "plex_token", "token", "dmm_api_key", "rex_direct_key", "rex_direct_url",
        "friendly_name", "announce_url", "public_url", "web_root", "host",
    ] {
        if out.get(field).is_some_and(|value| !value.is_null() && !value.is_string()) {
            return refuse(format!("{field} must be a string"));
        }
    }
    for (field, allowed) in [
        ("part_mode", &["proxy"][..]),
        ("gdm", &["auto", "on", "off"][..]),
        ("restart", &["no", "on-failure"][..]),
    ] {
        if out.get(field).is_some_and(|value| !value.as_str().is_some_and(|value| allowed.contains(&value))) {
            return refuse(format!("{field} must be one of {}", allowed.join(", ")));
        }
    }
    for field in ["assert_claimed", "verbose"] {
        if out.get(field).is_some_and(|value| !value.is_boolean()) {
            return refuse(format!("{field} must be true or false"));
        }
    }
    if out.get("extra_args").is_some_and(|value| !value.as_array().is_some_and(|rows| rows.iter().all(Value::is_string))) {
        return refuse("extra_args must be a list of strings".to_string());
    }
    if !partial {
        for (field, default) in [
            ("host", json!("0.0.0.0")),
            ("part_mode", json!("proxy")),
            ("gdm", json!("auto")),
            ("restart", json!("on-failure")),
            ("assert_claimed", json!(false)),
            ("verbose", json!(false)),
            ("extra_args", json!([])),
        ] {
            out.entry(field).or_insert(default);
        }
    }
    Ok(out)
}

fn redact(config: &Map<String, Value>) -> Value {
    Value::Object(
        config
            .iter()
            .map(|(key, value)| {
                let hidden = SECRETS.contains(&key.as_str()) && truthy(Some(value));
                (key.clone(), if hidden { json!("***") } else { value.clone() })
            })
            .collect(),
    )
}

/// The server's own copy of its catalog, as a database at `target`.
///
/// Written the way `rex catalog --convert` writes one: a JSON catalog keeps its own
/// digest, so a state saved over that file is still the same catalog's, and a database
/// catalog keeps the digest it recorded. The server's `--friendly-name` names only a
/// catalog it seeds -- an existing one's name is its `friendlyName` -- so a name given here
/// is written into the copy, which then is a different catalog (a new digest). Only the
/// catalog half of an existing `target` is replaced: the state the server wrote there
/// stays, and the server sets it aside by digest if the catalog changed.
fn import_catalog(source: &str, target: &Path, friendly_name: Option<&str>) -> Answer<()> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    let (document, digest) = if store::is_sqlite(Path::new(source)) {
        store::read_catalog(Path::new(source))
            .map_err(|error| Refused(400, format!("catalog {source} is not a catalog database: {error}")))?
    } else {
        let raw = fs::read(source)?;
        let document = serde_json::from_slice(&raw)
            .map_err(|error| Refused(400, format!("catalog {source} is not JSON: {error}")))?;
        (document, sha1_hex(&raw))
    };
    let Value::Object(mut document) = document else {
        return Err(Refused(400, format!("catalog {source} holds no items")));
    };
    if !document.get("items").is_some_and(Value::is_array) {
        return Err(Refused(400, format!("catalog {source} holds no items")));
    }
    let digest = match friendly_name.filter(|name| !name.is_empty()) {
        Some(name) => {
            document.insert("friendlyName".to_string(), json!(name));
            sha1_hex(py_dumps(&Value::Object(document.clone()), Some(2), false, true).as_bytes())
        }
        None => digest,
    };
    store::write_catalog(target, &document, &digest).map_err(|error| Refused(500, error))?;
    fs::set_permissions(target, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

fn port_free(host: &str, port: i64) -> bool {
    let host = if host.is_empty() { "0.0.0.0" } else { host };
    // std sets SO_REUSEADDR on Unix, as the probe in manager.py did.
    u16::try_from(port).is_ok_and(|port| TcpListener::bind((host, port)).is_ok())
}

fn port_answers(port: i64) -> bool {
    u16::try_from(port).is_ok_and(|port| {
        TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)).is_ok()
    })
}

/// The last `lines` lines of the last 512 KiB of a log.
fn tail(path: &Path, lines: usize) -> Vec<String> {
    let Ok(bytes) = fs::read(path) else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&bytes[bytes.len().saturating_sub(512 * 1024)..]).into_owned();
    let all = text.lines().map(str::to_string).collect::<Vec<_>>();
    all[all.len().saturating_sub(lines)..].to_vec()
}

/// `Popen.returncode`: the exit status, or minus the signal that killed the process.
fn exit_code(status: ExitStatus) -> i64 {
    status.code().map_or_else(|| -i64::from(status.signal().unwrap_or(0)), i64::from)
}

fn describe_exit(code: Option<i64>) -> Option<String> {
    let code = code?;
    Some(if code < 0 {
        match platform::signal_name(-code as i32) {
            Some(name) => format!("killed by {name}"),
            None => format!("killed by signal {}", -code),
        }
    } else {
        format!("exit {code}")
    })
}

/// The failures still inside the window plus this one, and the backoff before the next
/// start -- none once the limit is reached.
fn backoff(previous: &[f64], now: f64) -> (Vec<f64>, Option<u64>) {
    let history = previous.iter().copied().filter(|at| now - at < FAILURE_WINDOW).chain([now]).collect::<Vec<_>>();
    let delay = (history.len() < FAILURE_LIMIT).then(|| 60.min(1_u64 << (history.len() - 1)));
    (history, delay)
}

/// `time.strftime("%Y-%m-%d %H:%M:%S")`.
fn local_stamp() -> String {
    platform::local_tm(now() as i64).map_or_else(String::new, |tm| {
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            tm.tm_year + 1900,
            tm.tm_mon + 1,
            tm.tm_mday,
            tm.tm_hour,
            tm.tm_min,
            tm.tm_sec
        )
    })
}

/// Copy-truncate: the server holds the log open in append mode, so truncating in place is
/// safe and the previous megabytes stay in `<log>.1`.
fn rotate(path: &Path) {
    if fs::metadata(path).is_ok_and(|meta| meta.len() > LOG_LIMIT) {
        let mut old = path.as_os_str().to_owned();
        old.push(".1");
        if fs::copy(path, &old).is_ok() {
            let _ = fs::OpenOptions::new().write(true).open(path).and_then(|file| file.set_len(0));
        }
    }
}

// -- the supervisor ---------------------------------------------------------------

/// A supervised process; the monitor and a stop both wait on it.
#[derive(Clone)]
struct Proc {
    pid: u32,
    child: Arc<Mutex<Child>>,
}

impl Proc {
    fn poll(&self) -> Option<i64> {
        let mut child = self.child.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        child.try_wait().ok().flatten().map(exit_code)
    }

    /// SIGTERM the process group, SIGKILL it after the grace period.
    fn terminate(&self) -> Option<i64> {
        platform::signal_group(self.pid, false);
        let deadline = Instant::now() + STOP_GRACE;
        let mut killed = false;
        loop {
            if let Some(code) = self.poll() {
                return Some(code);
            }
            if !killed && Instant::now() >= deadline {
                platform::signal_group(self.pid, true);
                killed = true;
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
}

struct Row {
    config: Map<String, Value>,
    desired: String,
    pid: Option<i64>,
    status: String,
    last_exit: Option<String>,
    last_error: Option<String>,
    started_at: Option<f64>,
}

struct Inner {
    db: Connection,
    procs: HashMap<String, Proc>,
    failures: HashMap<String, Vec<f64>>,
    /// name -> when a backoff restart is due
    due: HashMap<String, f64>,
    builds: HashMap<String, Child>,
    /// builds asked to (re)start their server on success
    start_after: HashSet<String>,
}

impl Inner {
    fn row(&self, name: &str) -> Answer<Row> {
        self.db
            .query_row(
                "SELECT config, desired, pid, status, last_exit, last_error, started_at FROM servers WHERE name=?1",
                params![name],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        Row {
                            config: Map::new(),
                            desired: row.get(1)?,
                            pid: row.get(2)?,
                            status: row.get(3)?,
                            last_exit: row.get(4)?,
                            last_error: row.get(5)?,
                            started_at: row.get(6)?,
                        },
                    ))
                },
            )
            .optional()?
            .map(|(config, mut row)| {
                row.config = serde_json::from_str(&config).unwrap_or_default();
                row
            })
            .ok_or_else(|| Refused(404, format!("no server named {name}")))
    }

    fn names(&self) -> Answer<Vec<String>> {
        let mut statement = self.db.prepare("SELECT name FROM servers ORDER BY name")?;
        let names = statement.query_map([], |row| row.get(0))?.collect::<Result<Vec<String>, _>>()?;
        Ok(names)
    }

    fn update(&self, name: &str, fields: &[(&str, Sql)]) -> Answer<()> {
        let mut values = fields.iter().map(|(_, value)| value.clone()).collect::<Vec<_>>();
        values.push(Sql::Real(now()));
        values.push(Sql::Text(name.to_string()));
        let sets = fields.iter().map(|(key, _)| format!("{key}=?")).chain(["updated_at=?".to_string()]);
        let sql = format!("UPDATE servers SET {} WHERE name=?", sets.collect::<Vec<_>>().join(", "));
        self.db.execute(&sql, params_from_iter(values))?;
        Ok(())
    }
}

fn s(value: &str) -> Sql {
    Sql::Text(value.to_string())
}

fn opt(value: Option<String>) -> Sql {
    value.map_or(Sql::Null, Sql::Text)
}

struct Manager {
    root: PathBuf,
    binary: String,
    /// The servers' working directory, where they find `data/`: the checkout's root.
    checkout: PathBuf,
    stopping: AtomicBool,
    inner: Mutex<Inner>,
}

impl Manager {
    fn open(root: &Path, binary: String) -> Result<Self, String> {
        let root = std::path::absolute(root).map_err(|e| e.to_string())?;
        fs::create_dir_all(root.join("servers")).map_err(|e| format!("cannot create {}: {e}", root.display()))?;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
        let path = root.join("manager.db");
        let fresh = !path.exists();
        let db = Connection::open(&path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
        db.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
        if fresh {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
        }
        let inner = Inner {
            db,
            procs: HashMap::new(),
            failures: HashMap::new(),
            due: HashMap::new(),
            builds: HashMap::new(),
            start_after: HashSet::new(),
        };
        Ok(Manager { root, binary, checkout: checkout_root(), stopping: AtomicBool::new(false), inner: Mutex::new(inner) })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn directory(&self, name: &str) -> PathBuf {
        self.root.join("servers").join(name)
    }

    fn catalog_path(&self, name: &str) -> PathBuf {
        self.directory(name).join("rex.db")
    }

    // views
    fn view_of(&self, inner: &Inner, name: &str) -> Answer<Value> {
        let row = inner.row(name)?;
        let running = inner.procs.contains_key(name);
        let uptime = row.started_at.filter(|at| running && *at != 0.0).map(|at| ((now() - at) * 10.0).round() / 10.0);
        Ok(json!({
            "name": name, "port": row.config.get("port"), "desired": row.desired,
            "status": row.status, "pid": if running { row.pid } else { None },
            "uptime": uptime, "last_exit": row.last_exit, "last_error": row.last_error,
            "recent_failures": inner.failures.get(name).map_or(0, Vec::len),
            "directory": self.directory(name), "config": redact(&row.config),
        }))
    }

    fn view(&self, name: &str) -> Answer<Value> {
        self.view_of(&self.lock(), name)
    }

    fn list(&self) -> Answer<Value> {
        let inner = self.lock();
        let views = inner.names()?.iter().map(|name| self.view_of(&inner, name)).collect::<Answer<Vec<_>>>()?;
        Ok(Value::Array(views))
    }

    // configuration
    fn check_port(inner: &Inner, name: &str, port: Option<&Value>) -> Answer<()> {
        for other in inner.names()? {
            if other != name && inner.row(&other)?.config.get("port") == port {
                let port = port.and_then(Value::as_i64).unwrap_or(0);
                return Err(Refused(409, format!("port {port} is already {other}'s")));
            }
        }
        Ok(())
    }

    fn create(&self, config: Value) -> Answer<Value> {
        let config = validate(config, false)?;
        let name = text(&config, "name").unwrap_or_default().to_string();
        let inner = self.lock();
        if inner.names()?.contains(&name) {
            return Err(Refused(409, format!("a server named {name} exists")));
        }
        Self::check_port(&inner, &name, config.get("port"))?;
        if let Some(catalog) = text(&config, "catalog") {
            import_catalog(catalog, &self.catalog_path(&name), text(&config, "friendly_name"))?;
        } else {
            fs::create_dir_all(self.directory(&name))?;
        }
        let now = now();
        inner.db.execute(
            "INSERT INTO servers(name, config, desired, status, created_at, updated_at) VALUES (?1, ?2, 'stopped', 'stopped', ?3, ?4)",
            params![name, py_dumps(&Value::Object(config), None, false, true), now, now],
        )?;
        self.view_of(&inner, &name)
    }

    fn patch(&self, name: &str, changes: Value) -> Answer<Value> {
        let changes = validate(changes, true)?;
        let inner = self.lock();
        let row = inner.row(name)?;
        if inner.procs.contains_key(name) || row.desired == "running" {
            return Err(Refused(409, format!("stop {name} before changing its configuration")));
        }
        if changes.get("name").is_some_and(|given| given.as_str() != Some(name)) {
            return Err(Refused(400, "a server cannot be renamed".to_string()));
        }
        let mut config = row.config;
        config.extend(changes.clone());
        let config = validate(Value::Object(config), false)?;
        if changes.contains_key("port") {
            Self::check_port(&inner, name, config.get("port"))?;
        }
        if let Some(catalog) = text(&config, "catalog") {
            if changes.contains_key("catalog") || changes.contains_key("friendly_name") {
                import_catalog(catalog, &self.catalog_path(name), text(&config, "friendly_name"))?;
            }
        }
        inner.update(name, &[("config", s(&py_dumps(&Value::Object(config), None, false, true)))])?;
        self.view_of(&inner, name)
    }

    // processes
    fn command(&self, config: &Map<String, Value>, name: &str) -> Vec<String> {
        let field = |key: &str, default: &str| text(config, key).unwrap_or(default).to_string();
        let port = config.get("port").map(Value::to_string).unwrap_or_default();
        let mut argv = vec![
            "--port".to_string(), port,
            "--host".to_string(), field("host", "0.0.0.0"),
            "--catalog".to_string(), self.catalog_path(name).to_string_lossy().into_owned(),
            "--part-mode".to_string(), field("part_mode", "proxy"),
            "--gdm".to_string(), field("gdm", "auto"),
        ];
        for (key, flag) in [
            ("announce_url", "--announce-url"),
            ("public_url", "--public-url"),
            ("web_root", "--web-root"),
            ("zurg_mount", "--zurg-mount"),
        ] {
            if let Some(value) = text(config, key) {
                argv.extend([flag.to_string(), value.to_string()]);
            }
        }
        if truthy(config.get("assert_claimed")) {
            argv.push("--assert-claimed".to_string());
        }
        if truthy(config.get("verbose")) {
            argv.push("--verbose".to_string());
        }
        argv.extend(config.get("extra_args").and_then(Value::as_array).into_iter().flatten().filter_map(|arg| arg.as_str().map(str::to_string)));
        argv
    }

    /// The manager's environment without anyone's keys or `REX_STATE`, plus this server's.
    fn environment(command: &mut Command, config: &Map<String, Value>) {
        command.env_remove("REX_STATE");
        for (field, variable) in SECRET_ENV {
            command.env_remove(variable);
            if let Some(value) = text(config, field) {
                command.env(variable, value);
            }
        }
    }

    /// A child of ours, in a session of its own, writing to `log`.
    fn child(&self, program: &str, args: &[String], config: &Map<String, Value>, log: fs::File) -> std::io::Result<Child> {
        let mut command = Command::new(program);
        command.args(args).current_dir(&self.checkout).stdin(Stdio::null()).stdout(log.try_clone()?).stderr(log);
        Self::environment(&mut command, config);
        platform::new_session(&mut command);
        command.spawn()
    }

    /// Start the process; the caller holds the lock and has set desired=running.
    fn spawn(&self, inner: &mut Inner, name: &str) -> Answer<()> {
        let config = inner.row(name)?.config;
        if !store::is_sqlite(&self.catalog_path(name)) {
            return Err(Refused(409, format!("{name} has no catalog yet: build it first (POST /servers/{name}/build)")));
        }
        let port = config.get("port").and_then(Value::as_i64).unwrap_or(0);
        if !port_free(text(&config, "host").unwrap_or("0.0.0.0"), port) {
            return Err(Refused(409, format!("port {port} is in use")));
        }
        let log_path = self.directory(name).join("server.log");
        rotate(&log_path);
        let mut log = fs::OpenOptions::new().create(true).append(true).open(&log_path)?;
        writeln!(log, "--- manager: starting {name} at {}", local_stamp())?;
        match self.child(&self.binary, &self.command(&config, name), &config, log) {
            Err(error) => {
                inner.update(name, &[("status", s("failed")), ("last_error", s(&format!("cannot start: {error}")))])?;
                Err(Refused(500, format!("cannot start {name}: {error}")))
            }
            Ok(child) => {
                let pid = child.id();
                inner.procs.insert(name.to_string(), Proc { pid, child: Arc::new(Mutex::new(child)) });
                inner.due.remove(name);
                inner.update(
                    name,
                    &[("pid", Sql::Integer(pid.into())), ("status", s("starting")), ("started_at", Sql::Real(now())), ("last_error", Sql::Null)],
                )
            }
        }
    }

    fn start(&self, name: &str) -> Answer<Value> {
        let mut inner = self.lock();
        let row = inner.row(name)?;
        if !inner.procs.contains_key(name) {
            inner.failures.remove(name);
            inner.update(name, &[("desired", s("running"))])?;
            if let Err(refused) = self.spawn(&mut inner, name) {
                let status = if row.status == "failed" { "failed" } else { "stopped" };
                inner.update(name, &[("desired", s("stopped")), ("status", s(status))])?;
                return Err(refused);
            }
        }
        self.view_of(&inner, name)
    }

    fn stop(&self, name: &str, keep_desired: bool) -> Answer<Value> {
        let proc = {
            let mut inner = self.lock();
            inner.row(name)?;
            if !keep_desired {
                inner.update(name, &[("desired", s("stopped"))])?;
            }
            inner.due.remove(name);
            inner.procs.get(name).cloned()
        };
        // Waited outside the lock: a server saving on SIGTERM must not freeze the API.
        let code = proc.as_ref().and_then(Proc::terminate);
        let mut inner = self.lock();
        match &proc {
            Some(proc) if inner.procs.get(name).is_some_and(|held| Arc::ptr_eq(&held.child, &proc.child)) => {
                inner.procs.remove(name);
                inner.update(name, &[("pid", Sql::Null), ("status", s("stopped")), ("last_exit", opt(describe_exit(code)))])?;
            }
            None => {
                if inner.row(name)?.status != "failed" {
                    inner.update(name, &[("status", s("stopped"))])?;
                }
            }
            _ => {}
        }
        self.view_of(&inner, name)
    }

    fn restart(&self, name: &str) -> Answer<Value> {
        self.stop(name, false)?;
        self.start(name)
    }

    fn delete(&self, name: &str, purge: bool) -> Answer<Value> {
        self.stop(name, false)?;
        {
            let mut inner = self.lock();
            inner.db.execute("DELETE FROM servers WHERE name=?1", params![name])?;
            inner.db.execute("DELETE FROM builds WHERE name=?1", params![name])?;
            inner.failures.remove(name);
        }
        if purge {
            let _ = fs::remove_dir_all(self.directory(name));
        }
        Ok(json!({"deleted": name, "purged": purge}))
    }

    fn logs_of(&self, name: &str, lines: usize) -> Answer<Value> {
        self.lock().row(name)?;
        Ok(json!({"name": name, "lines": tail(&self.directory(name).join("server.log"), lines)}))
    }

    // the monitor
    /// One pass: exits, restarts that are due, readiness, log size, finished builds.
    fn reap(self: &Arc<Self>) -> Answer<()> {
        let mut inner = self.lock();
        if self.stopping.load(Ordering::SeqCst) {
            return Ok(());
        }
        let now = now();
        let procs = inner.procs.iter().map(|(name, proc)| (name.clone(), proc.clone())).collect::<Vec<_>>();
        for (name, proc) in procs {
            match proc.poll() {
                None => {
                    let row = inner.row(&name)?;
                    if row.status == "starting" && port_answers(row.config.get("port").and_then(Value::as_i64).unwrap_or(0)) {
                        inner.update(&name, &[("status", s("running"))])?;
                    }
                    rotate(&self.directory(&name).join("server.log"));
                }
                Some(code) => {
                    inner.procs.remove(&name);
                    self.exited(&mut inner, &name, Some(code), now, None)?;
                }
            }
        }
        let due = inner.due.iter().filter(|(_, when)| **when <= now).map(|(name, _)| name.clone()).collect::<Vec<_>>();
        for name in due {
            inner.due.remove(&name);
            if let Err(Refused(_, reason)) = self.spawn(&mut inner, &name) {
                self.exited(&mut inner, &name, None, now, Some(reason))?;
            }
        }
        let mut finished = Vec::new();
        for (name, build) in inner.builds.iter_mut() {
            if let Ok(Some(status)) = build.try_wait() {
                finished.push((name.clone(), exit_code(status)));
            }
        }
        for (name, code) in finished {
            inner.builds.remove(&name);
            inner.db.execute(
                "UPDATE builds SET status=?1, exit=?2, ended_at=?3 WHERE name=?4",
                params![if code == 0 { "succeeded" } else { "failed" }, code, now, name],
            )?;
            if inner.start_after.remove(&name) && code == 0 {
                // A running server reads its catalog at start: restart it. On its own
                // thread, since stopping waits for the server.
                let restart = inner.procs.contains_key(&name);
                let manager = Arc::clone(self);
                thread::spawn(move || {
                    let done = if restart { manager.restart(&name) } else { manager.start(&name) };
                    if let Err(Refused(_, error)) = done {
                        eprintln!("manager: {name} after its build: {error}");
                    }
                });
            }
        }
        Ok(())
    }

    /// A supervised server stopped without being asked to.
    fn exited(&self, inner: &mut Inner, name: &str, code: Option<i64>, now: f64, reason: Option<String>) -> Answer<()> {
        let Ok(row) = inner.row(name) else {
            return Ok(());
        };
        let last_exit = opt(if code.is_some() { describe_exit(code) } else { reason });
        let policy = row.config.get("restart").and_then(Value::as_str).unwrap_or("on-failure");
        if row.desired != "running" {
            return inner.update(name, &[("pid", Sql::Null), ("status", s("stopped")), ("last_exit", last_exit)]);
        }
        if code == Some(0) || policy == "no" {
            // A clean exit was the server's own decision; it is not restarted.
            let status = if code == Some(0) { "exited" } else { "failed" };
            return inner.update(name, &[("pid", Sql::Null), ("desired", s("stopped")), ("status", s(status)), ("last_exit", last_exit)]);
        }
        let (history, delay) = backoff(inner.failures.get(name).map_or(&[][..], Vec::as_slice), now);
        let failures = history.len();
        inner.failures.insert(name.to_string(), history);
        match delay {
            None => inner.update(
                name,
                &[
                    ("pid", Sql::Null),
                    ("desired", s("stopped")),
                    ("status", s("failed")),
                    ("last_exit", last_exit),
                    ("last_error", s(&format!("{failures} failures within {} s; not restarted again", FAILURE_WINDOW as u64))),
                ],
            ),
            Some(delay) => {
                inner.due.insert(name.to_string(), now + delay as f64);
                inner.update(
                    name,
                    &[("pid", Sql::Null), ("status", s("restarting")), ("last_exit", last_exit), ("last_error", s(&format!("restarting in {delay} s")))],
                )
            }
        }
    }

    fn monitor(self: &Arc<Self>) {
        while !self.stopping.load(Ordering::SeqCst) {
            // The monitor must outlive one bad pass.
            if let Err(Refused(_, error)) = self.reap() {
                eprintln!("manager: monitor pass failed: {error}");
            }
            thread::sleep(Duration::from_millis(500));
        }
    }

    // start and stop of the manager itself
    /// A manager killed hard leaves its servers running in their own sessions. They cannot
    /// be supervised without being our children, so they are stopped -- and started again
    /// by `restore` if they should be running.
    fn adopt_orphans(&self) -> Answer<()> {
        let names = self.lock().names()?;
        for name in names {
            let Some(pid) = self.lock().row(&name)?.pid.filter(|pid| *pid > 0) else {
                continue;
            };
            let pid = pid as u32;
            if platform::command_line(pid).contains(self.catalog_path(&name).to_string_lossy().as_ref()) {
                platform::signal_group(pid, false);
                let deadline = Instant::now() + STOP_GRACE;
                while Instant::now() < deadline && platform::alive(pid) {
                    thread::sleep(Duration::from_millis(200));
                }
                if platform::alive(pid) {
                    platform::signal_group(pid, true);
                }
            }
            self.lock().update(&name, &[("pid", Sql::Null)])?;
        }
        Ok(())
    }

    fn restore(&self) -> Answer<Vec<String>> {
        self.adopt_orphans()?;
        let mut restored = Vec::new();
        let mut inner = self.lock();
        for name in inner.names()? {
            let row = inner.row(&name)?;
            if row.desired == "running" {
                match self.spawn(&mut inner, &name) {
                    Ok(()) => restored.push(name),
                    Err(Refused(_, reason)) => self.exited(&mut inner, &name, None, now(), Some(reason))?,
                }
            } else if row.status != "failed" && row.status != "exited" {
                inner.update(&name, &[("status", s("stopped")), ("pid", Sql::Null)])?;
            }
        }
        Ok(restored)
    }

    /// Stop every server but keep what should run, for the next start.
    fn shutdown(self: &Arc<Self>) {
        self.stopping.store(true, Ordering::SeqCst);
        let running = self.lock().procs.keys().cloned().collect::<Vec<_>>();
        let threads = running
            .into_iter()
            .map(|name| {
                let manager = Arc::clone(self);
                thread::spawn(move || {
                    let _ = manager.stop(&name, true);
                })
            })
            .collect::<Vec<_>>();
        for thread in threads {
            let _ = thread.join();
        }
        for build in self.lock().builds.values() {
            platform::signal_group(build.id(), false);
        }
    }

    // catalog builds
    /// `rex catalog --seed` into the server's database, as a job: an empty library whose
    /// locations are zurg's folders, which its sources fill. `start`: once it succeeds,
    /// start the server (or restart it).
    fn build(&self, name: &str, start: bool) -> Answer<Value> {
        {
            let mut inner = self.lock();
            let config = inner.row(name)?.config;
            if inner.builds.contains_key(name) {
                return Err(Refused(409, format!("a build for {name} is already running")));
            }
            fs::create_dir_all(self.directory(name))?;
            let log = fs::File::create(self.directory(name).join("build.log"))?;
            // REX_MANAGER_BUILDER replaces the builder (tests/test_manager.py).
            let (program, mut argv) = match std::env::var("REX_MANAGER_BUILDER").ok().filter(|path| !path.is_empty()) {
                Some(builder) => (builder, Vec::new()),
                None => (std::env::current_exe()?.to_string_lossy().into_owned(), vec!["catalog".to_string()]),
            };
            argv.extend(["--seed".to_string(), "--out".to_string(), self.catalog_path(name).to_string_lossy().into_owned()]);
            for (field, flag) in [("friendly_name", "--name"), ("zurg_mount", "--zurg-mount")] {
                if let Some(value) = text(&config, field) {
                    argv.extend([flag.to_string(), value.to_string()]);
                }
            }
            let child = self
                .child(&program, &argv, &config, log)
                .map_err(|error| Refused(500, format!("cannot build {name}: {error}")))?;
            inner.builds.insert(name.to_string(), child);
            if start {
                inner.start_after.insert(name.to_string());
            }
            inner.db.execute("INSERT OR REPLACE INTO builds VALUES (?1, 'running', NULL, ?2, NULL)", params![name, now()])?;
        }
        self.build_status(name)
    }

    fn build_status(&self, name: &str) -> Answer<Value> {
        let found = {
            let inner = self.lock();
            inner.row(name)?;
            inner
                .db
                .query_row("SELECT status, exit, started_at, ended_at FROM builds WHERE name=?1", params![name], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?, row.get::<_, Option<f64>>(2)?, row.get::<_, Option<f64>>(3)?))
                })
                .optional()?
        };
        Ok(match found {
            None => json!({"name": name, "status": "never"}),
            Some((status, code, started, ended)) => json!({
                "name": name, "status": status, "exit": code, "started_at": started, "ended_at": ended,
                "log": tail(&self.directory(name).join("build.log"), 40),
            }),
        })
    }
}

// -- the API ----------------------------------------------------------------------

const PAGE: &str = r##"<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>rex manager</title>
<style>body{font:14px system-ui,sans-serif;margin:16px;max-width:960px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ccc;padding:4px 6px;text-align:left}input,select{margin:2px}
pre{background:#f4f4f4;padding:8px;max-height:300px;overflow:auto}</style></head><body>
<h1>rex manager</h1>
<p>Admin token: <input id=tok type=password size=40> <button onclick="save()">Use</button></p>
<table><thead><tr><th>name<th>port<th>status<th>desired<th>last exit<th></tr></thead><tbody id=rows></tbody></table>
<h2>New server</h2>
<form id=f onsubmit="create(event)">
<input name=name placeholder="name" required> <input name=port type=number placeholder="port" required>
<input name=catalog placeholder="catalog .json/.db (optional: else seed an empty library)" size=48><br>
<input name=zurg_mount placeholder="zurg mount_path (default /mnt/zurg)"><br>
<input name=token type=password placeholder="server token (optional)"><br>
<input name=friendly_name placeholder="friendly name (optional)">
<select name=part_mode><option>proxy</select>
<select name=gdm><option>auto<option>on<option>off</select>
<label><input name=assert_claimed type=checkbox> assert claimed</label>
<button>Create</button></form>
<pre id=out></pre>
<script>
const out=document.getElementById('out');
function tok(){try{return sessionStorage.getItem('t')||''}catch(e){return ''}}
function save(){try{sessionStorage.setItem('t',document.getElementById('tok').value)}catch(e){};load()}
async function api(method,path,body){const r=await fetch(path,{method,headers:{'Authorization':'Bearer '+tok(),
'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();
if(!r.ok)throw new Error(j.error||r.status);return j}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){try{const list=await api('GET','/servers');document.getElementById('rows').innerHTML=list.map(s=>
`<tr><td>${esc(s.name)}<td>${s.port}<td>${esc(s.status)}<td>${esc(s.desired)}<td>${esc(s.last_exit)}<td>
${['start','stop','restart'].map(a=>`<button onclick="act('${esc(s.name)}','${a}')">${a}</button>`).join(' ')}
<button onclick="build('${esc(s.name)}')">build &amp; start</button> <button onclick="logs('${esc(s.name)}')">logs</button> <button onclick="del('${esc(s.name)}')">delete</button>`).join('')}
catch(e){out.textContent=e.message}}
async function act(n,a){try{await api('POST','/servers/'+n+'/'+a)}catch(e){out.textContent=e.message};load()}
async function build(n){try{await api('POST','/servers/'+n+'/build',{start:true});out.textContent='seeding '+n+'...'}catch(e){out.textContent=e.message}}
async function logs(n){try{out.textContent=(await api('GET','/servers/'+n+'/logs?lines=200')).lines.join('\n')}catch(e){out.textContent=e.message}}
async function del(n){if(!confirm('Delete '+n+'? Its files are kept.'))return;try{await api('DELETE','/servers/'+n)}catch(e){out.textContent=e.message};load()}
async function create(ev){ev.preventDefault();const d=Object.fromEntries(new FormData(ev.target));
const c={name:d.name,port:+d.port,part_mode:d.part_mode,gdm:d.gdm,assert_claimed:!!d.assert_claimed};
for(const k of ['catalog','zurg_mount','token','friendly_name'])if(d[k])c[k]=d[k];
try{await api('POST','/servers',c);ev.target.reset()}catch(e){out.textContent=e.message};load()}
load();setInterval(load,3000);
</script></body></html>"##;

/// `urllib.parse.parse_qs(query)[key][0]`: the first non-blank value, decoded.
fn query_value(query: &str, key: &str) -> Option<String> {
    let decode = |text: &str| {
        let bytes = text.replace('+', " ").into_bytes();
        let mut out = Vec::new();
        let mut index = 0;
        while index < bytes.len() {
            let hex = bytes.get(index + 1..index + 3).and_then(|hex| u8::from_str_radix(std::str::from_utf8(hex).ok()?, 16).ok());
            match (bytes[index], hex) {
                (b'%', Some(byte)) => {
                    out.push(byte);
                    index += 3;
                }
                (byte, _) => {
                    out.push(byte);
                    index += 1;
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    };
    query.split('&').filter_map(|pair| pair.split_once('=')).find_map(|(name, value)| {
        (decode(name) == key && !value.is_empty()).then(|| decode(value))
    })
}

/// `hmac.compare_digest`.
fn same_secret(given: &str, expected: &str) -> bool {
    given.len() == expected.len() && given.bytes().zip(expected.bytes()).fold(0, |diff, (a, b)| diff | (a ^ b)) == 0
}

fn public_get(method: &str, target: &str) -> bool {
    method == "GET"
        && target
            .split_once('?')
            .map_or(target, |(path, _)| path)
            .split('/')
            .all(str::is_empty)
}

fn authorized(method: &str, target: &str, authorization: &str, token: &str) -> bool {
    public_get(method, target)
        || authorization
            .strip_prefix("Bearer ")
            .is_some_and(|given| !given.is_empty() && same_secret(given, token))
}

fn route(manager: &Manager, token: &str, method: &str, target: &str, authorization: &str, body: &[u8]) -> (u16, &'static str, Vec<u8>) {
    let json_reply = |status: u16, payload: &Value| (status, "application/json", py_dumps(payload, Some(1), false, true).into_bytes());
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let parts = path.split('/').filter(|part| !part.is_empty()).collect::<Vec<_>>();
    if public_get(method, target) {
        return (200, "text/html; charset=utf-8", PAGE.as_bytes().to_vec());
    }
    if !authorized(method, target, authorization, token) {
        return json_reply(401, &json!({"error": "an Authorization: Bearer <admin token> is required"}));
    }
    let body = || -> Answer<Value> {
        if body.is_empty() {
            return Ok(json!({}));
        }
        serde_json::from_slice(body).map_err(|_| Refused(400, "the body is not JSON".to_string()))
    };
    let answer = (|| -> Answer<(u16, Value)> {
        if parts.first() != Some(&"servers") {
            return Err(Refused(404, "no such route".to_string()));
        }
        if parts.len() == 1 {
            return match method {
                "GET" => Ok((200, manager.list()?)),
                "POST" => Ok((201, manager.create(body()?)?)),
                _ => Err(Refused(405, "GET or POST".to_string())),
            };
        }
        let name = parts[1];
        if parts.len() == 2 {
            return match method {
                "GET" => Ok((200, manager.view(name)?)),
                "PATCH" => Ok((200, manager.patch(name, body()?)?)),
                "DELETE" => {
                    let purge = matches!(query_value(query, "purge").as_deref(), Some("1" | "true"));
                    Ok((200, manager.delete(name, purge)?))
                }
                _ => Err(Refused(405, "GET, PATCH or DELETE".to_string())),
            };
        }
        let action = parts[2];
        if parts.len() == 3 {
            match (method, action) {
                ("POST", "start") => return Ok((200, manager.start(name)?)),
                ("POST", "stop") => return Ok((200, manager.stop(name, false)?)),
                ("POST", "restart") => return Ok((200, manager.restart(name)?)),
                ("GET", "logs") => {
                    let lines = query_value(query, "lines").unwrap_or_else(|| "200".to_string());
                    let lines = lines
                        .trim()
                        .parse::<i64>()
                        .map_err(|_| Refused(500, format!("ValueError: invalid literal for int() with base 10: '{lines}'")))?;
                    return Ok((200, manager.logs_of(name, lines.clamp(1, 10000) as usize)?));
                }
                ("POST", "build") => return Ok((202, manager.build(name, truthy(body()?.get("start")))?)),
                ("GET", "build") => return Ok((200, manager.build_status(name)?)),
                _ => {}
            }
        }
        Err(Refused(404, "no such route".to_string()))
    })();
    match answer {
        Ok((status, payload)) => json_reply(status, &payload),
        Err(Refused(status, message)) => json_reply(status, &json!({"error": message})),
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        400 => "Bad Request",
        401 => "Unauthorized",
        413 => "Payload Too Large",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        501 => "Not Implemented",
        _ => "Internal Server Error",
    }
}

fn write_answer(stream: &mut TcpStream, request_line: &str, status: u16, content_type: &str, payload: &[u8]) -> std::io::Result<()> {
    // The request log goes to stderr, tokenless.
    eprintln!("{} \"{request_line}\" {status} -", local_stamp().get(11..).unwrap_or(""));
    let head = format!(
        "HTTP/1.0 {status} {}\r\nServer: rex-manager\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\n\r\n",
        reason(status),
        payload.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

fn error_answer(status: u16, message: &str) -> (u16, &'static str, Vec<u8>) {
    (status, "application/json", py_dumps(&json!({"error": message}), Some(1), false, true).into_bytes())
}

fn read_line_limited(reader: &mut BufReader<TcpStream>, limit: usize) -> std::io::Result<Option<Vec<u8>>> {
    let mut line = Vec::with_capacity(limit.min(256));
    // `read_until` grows without a bound while waiting for a newline. `take` caps the
    // read at one byte beyond the accepted line so an unterminated peer cannot grow it.
    let mut limited = reader.take(limit.saturating_add(1) as u64);
    std::io::BufRead::read_until(&mut limited, b'\n', &mut line)?;
    if line.len() > limit {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "line too long"));
    }
    Ok((!line.is_empty()).then_some(line))
}

/// One request and its answer, then the connection closes (http.server's HTTP/1.0).
fn handle(stream: TcpStream, manager: &Manager, token: &str) -> std::io::Result<()> {
    stream.set_nonblocking(false)?;
    stream.set_read_timeout(Some(Duration::from_secs(60)))?;
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut stream = stream;
    let request_line_bytes = match read_line_limited(&mut reader, MAX_HEADER_LINE_BYTES) {
        Ok(Some(line)) => line,
        Ok(None) => {
            // A port probe that connects and closes: http.server answers nothing.
            return Ok(());
        }
        Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
            let (status, content_type, payload) = error_answer(400, "request line too long");
            return write_answer(&mut stream, "", status, content_type, &payload);
        }
        Err(error) => return Err(error),
    };
    if !request_line_bytes.ends_with(b"\n") {
        let (status, content_type, payload) = error_answer(400, "malformed request line");
        return write_answer(&mut stream, "", status, content_type, &payload);
    }
    let request_line = String::from_utf8_lossy(&request_line_bytes).trim_end_matches(['\r', '\n']).to_string();
    if request_line.is_empty() {
        // A port probe that connects and closes: http.server answers nothing.
        return Ok(());
    }
    let mut words = request_line.split_whitespace();
    let (Some(method), Some(target), Some(_version)) = (words.next(), words.next(), words.next()) else {
        let (status, content_type, payload) = error_answer(400, "malformed request line");
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    };
    if words.next().is_some() || target.len() > MAX_TARGET_BYTES {
        let (status, content_type, payload) = error_answer(400, "malformed request target");
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    }
    let (method, target) = (method.to_string(), target.to_string());
    let (mut length, mut authorization) = (None, String::new());
    let mut transfer_encoding = false;
    let mut header_bytes = 0_usize;
    let mut header_lines = 0_usize;
    loop {
        let line = match read_line_limited(&mut reader, MAX_HEADER_LINE_BYTES) {
            Ok(Some(line)) => line,
            Ok(None) => {
                let (status, content_type, payload) = error_answer(400, "malformed headers");
                return write_answer(&mut stream, &request_line, status, content_type, &payload);
            }
            Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
                let (status, content_type, payload) = error_answer(400, "header line too long");
                return write_answer(&mut stream, &request_line, status, content_type, &payload);
            }
            Err(error) => return Err(error),
        };
        if !line.ends_with(b"\n") {
            let (status, content_type, payload) = error_answer(400, "malformed headers");
            return write_answer(&mut stream, &request_line, status, content_type, &payload);
        }
        let line = String::from_utf8_lossy(&line).trim_end_matches(['\r', '\n']).to_string();
        if line.is_empty() {
            break;
        }
        header_lines += 1;
        header_bytes = header_bytes.saturating_add(line.len() + 2);
        if header_lines > MAX_HEADER_LINES || header_bytes > MAX_HEADER_BYTES {
            let (status, content_type, payload) = error_answer(400, "request headers exceed limit");
            return write_answer(&mut stream, &request_line, status, content_type, &payload);
        }
        let Some((name, value)) = line.split_once(':') else {
            let (status, content_type, payload) = error_answer(400, "malformed header");
            return write_answer(&mut stream, &request_line, status, content_type, &payload);
        };
        match name.trim().to_ascii_lowercase().as_str() {
            "content-length" => {
                let value = value.trim();
                let parsed = value.parse::<usize>();
                if let Ok(parsed) = parsed {
                    if length.is_some_and(|previous| previous != parsed) {
                        let (status, content_type, payload) = error_answer(400, "conflicting Content-Length");
                        return write_answer(&mut stream, &request_line, status, content_type, &payload);
                    }
                    length = Some(parsed);
                } else {
                    let (status, content_type, payload) = error_answer(400, "malformed Content-Length");
                    return write_answer(&mut stream, &request_line, status, content_type, &payload);
                }
            }
            "transfer-encoding" => transfer_encoding = true,
            "authorization" => authorization = value.trim().to_string(),
            _ => {}
        }
    }
    // Authorization is checked before any body allocation or read. A public page can
    // answer without consuming a body, just as it did before the bounds were added.
    if !authorized(&method, &target, &authorization, token) {
        let (status, content_type, payload) = route(manager, token, &method, &target, &authorization, &[]);
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    }
    if transfer_encoding {
        let (status, content_type, payload) = error_answer(400, "Transfer-Encoding is unsupported");
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    }
    let length = length.unwrap_or(0);
    if length > MAX_BODY_BYTES {
        let (status, content_type, payload) = error_answer(413, "request body exceeds limit");
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    }
    if public_get(&method, &target) {
        let (status, content_type, payload) = route(manager, token, &method, &target, &authorization, &[]);
        return write_answer(&mut stream, &request_line, status, content_type, &payload);
    }
    let mut body = vec![0_u8; length];
    if let Err(error) = reader.read_exact(&mut body) {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            let (status, content_type, payload) = error_answer(400, "request body is incomplete");
            return write_answer(&mut stream, &request_line, status, content_type, &payload);
        }
        return Err(error);
    }
    let (status, content_type, payload) = match method.as_str() {
        "GET" | "POST" | "PATCH" | "DELETE" => route(manager, token, &method, &target, &authorization, &body),
        _ => (501, "application/json", py_dumps(&json!({"error": format!("Unsupported method ('{method}')")}), Some(1), false, true).into_bytes()),
    };
    write_answer(&mut stream, &request_line, status, content_type, &payload)
}

/// The API's token: an explicit file, else `<root>/admin.token`, created on first start
/// (mode 600) and printed once so the operator can copy it.
fn admin_token(root: &Path, path: Option<&str>) -> Result<(String, bool, PathBuf), String> {
    let path = path.map_or_else(|| root.join("admin.token"), PathBuf::from);
    if path.exists() {
        let token = fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        return Ok((token.trim().to_string(), false, path));
    }
    let mut bytes = [0_u8; 32];
    platform::random_bytes(&mut bytes).map_err(|e| format!("no random source: {e}"))?;
    let token = bytes.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(|e| format!("cannot create {}: {e}", path.display()))?;
    writeln!(file, "{token}").map_err(|e| e.to_string())?;
    Ok((token, true, path))
}

fn serve(root: &Path, listen: &str, binary: String, token_file: Option<&str>) -> Result<(), String> {
    let manager = Arc::new(Manager::open(root, binary)?);
    let (token, created, token_path) = admin_token(&manager.root, token_file)?;
    if created {
        println!("manager: admin token (stored in {}): {token}", token_path.display());
    }
    let (host, port) = listen.rsplit_once(':').ok_or_else(|| format!("--listen {listen}: host:port"))?;
    let port = port.parse::<u16>().map_err(|_| format!("--listen {listen}: host:port"))?;
    let listener = TcpListener::bind((if host.is_empty() { "127.0.0.1" } else { host }, port))
        .map_err(|e| format!("cannot listen on {listen}: {e}"))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let terminated = platform::termination_flag();
    let restored = manager.restore().map_err(|Refused(_, error)| error)?;
    let defined = manager.lock().names().map_err(|Refused(_, error)| error)?.len();
    let restored = if restored.is_empty() {
        "none".to_string()
    } else {
        format!("[{}]", restored.iter().map(|name| format!("'{name}'")).collect::<Vec<_>>().join(", "))
    };
    println!("manager: listening on http://{listen}, root {}, {defined} server(s) defined, restored {restored}", manager.root.display());
    let monitor = Arc::clone(&manager);
    thread::Builder::new().name("monitor".to_string()).spawn(move || monitor.monitor()).map_err(|e| e.to_string())?;
    while !terminated.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _)) => {
                let (manager, token) = (Arc::clone(&manager), token.clone());
                thread::spawn(move || {
                    let _ = handle(stream, &manager, &token);
                });
            }
            Err(_) => thread::sleep(Duration::from_millis(100)),
        }
    }
    manager.shutdown();
    println!("manager: stopped");
    Ok(())
}

// -- the CLI client ---------------------------------------------------------------

const USAGE: &str = "usage: rex manager [--root ROOT] {serve,create,list,start,stop,restart,status,logs,delete,build} ...

  serve   [--listen HOST:PORT] [--binary PATH] [--admin-token-file FILE]
  create  NAME --port PORT [--catalog PATH] [--friendly-name NAME] [--zurg-mount PATH]
          [--part-mode {proxy}] [--gdm {auto,on,off}] [--host HOST] [--announce-url URL]
          [--public-url URL] [--web-root DIR] [--assert-claimed] [--verbose]
          [--restart {no,on-failure}] [--extra-arg ARG]... [--<field>-env VAR]...
  list
  start|stop|restart|status NAME
  logs    NAME [--lines N]
  delete  NAME [--purge]
  build   NAME [--status] [--start]

Every command but serve also takes --url URL (default http://127.0.0.1:32300) and
--token-file FILE (default <root>/admin.token; or $REX_MANAGER_TOKEN). Keys come from the
environment (REX_TOKEN, REX_DIRECT_KEY, REX_DIRECT_URL, or the variable --token-env,
--rex-direct-key-env and --rex-direct-url-env name), never
the command line. Sources register with the server themselves (docs/feed-api.md).";

fn usage_error(message: &str) -> String {
    format!("{}\nrex: error: {message}", USAGE.lines().next().unwrap_or_default())
}

/// What the command line asked for, argparse's way: `--flag value` or `--flag=value`.
#[derive(Default)]
struct Cli {
    root: Option<String>,
    command: String,
    name: Option<String>,
    values: HashMap<String, String>,
    extra_args: Vec<String>,
    flags: HashSet<String>,
}

impl Cli {
    fn value(&self, flag: &str) -> Option<&str> {
        self.values.get(flag).map(String::as_str)
    }
}

fn parse(args: &[String]) -> Result<Option<Cli>, String> {
    const COMMANDS: &[&str] = &["serve", "create", "list", "start", "stop", "restart", "status", "logs", "delete", "build"];
    const CLIENT: &[&str] = &["--url", "--token-file"];
    let mut cli = Cli::default();
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        index += 1;
        if arg == "-h" || arg == "--help" {
            return Ok(None);
        }
        if !arg.starts_with("--") {
            if cli.command.is_empty() {
                if !COMMANDS.contains(&arg.as_str()) {
                    return Err(usage_error(&format!("argument command: invalid choice: '{arg}'")));
                }
                cli.command = arg.clone();
            } else if cli.name.is_none() && !matches!(cli.command.as_str(), "serve" | "list") {
                cli.name = Some(arg.clone());
            } else {
                return Err(usage_error(&format!("unrecognized arguments: {arg}")));
            }
            continue;
        }
        let (flag, inline) = match arg.split_once('=') {
            Some((flag, value)) => (flag, Some(value.to_string())),
            None => (arg.as_str(), None),
        };
        let (values, flags): (Vec<&str>, &[&str]) = match cli.command.as_str() {
            "" => (vec!["--root"], &[]),
            "serve" => (vec!["--listen", "--binary", "--admin-token-file"], &[]),
            "create" => {
                let mut values = vec![
                    "--port", "--catalog", "--friendly-name", "--zurg-mount", "--part-mode", "--gdm", "--host",
                    "--announce-url", "--public-url", "--web-root", "--restart", "--extra-arg",
                    "--token-env", "--rex-direct-key-env", "--rex-direct-url-env",
                ];
                values.extend(CLIENT);
                (values, &["--assert-claimed", "--verbose"][..])
            }
            "logs" => (["--lines"].into_iter().chain(CLIENT.iter().copied()).collect(), &[]),
            "delete" => (CLIENT.to_vec(), &["--purge"][..]),
            "build" => (CLIENT.to_vec(), &["--status", "--start"][..]),
            _ => (CLIENT.to_vec(), &[]),
        };
        if flags.contains(&flag) {
            if inline.is_some() {
                return Err(usage_error(&format!("argument {flag}: ignored explicit argument")));
            }
            cli.flags.insert(flag.to_string());
            continue;
        }
        if !values.contains(&flag) && flag != "--root" {
            return Err(usage_error(&format!("unrecognized arguments: {arg}")));
        }
        let value = match inline {
            Some(value) => value,
            None => match args.get(index) {
                Some(next) if !next.starts_with("--") => {
                    index += 1;
                    next.clone()
                }
                _ => return Err(usage_error(&format!("argument {flag}: expected one argument"))),
            },
        };
        match flag {
            "--root" => cli.root = Some(value),
            "--extra-arg" => cli.extra_args.push(value),
            _ => {
                cli.values.insert(flag.to_string(), value);
            }
        }
    }
    if cli.command.is_empty() {
        return Err(usage_error("the following arguments are required: command"));
    }
    if cli.name.is_none() && !matches!(cli.command.as_str(), "serve" | "list") {
        return Err(usage_error("the following arguments are required: name"));
    }
    for (flag, choices) in [("--part-mode", &["proxy"][..]), ("--gdm", &["auto", "on", "off"][..]), ("--restart", &["no", "on-failure"][..])] {
        if let Some(value) = cli.value(flag).filter(|value| !choices.contains(value)) {
            let listed = choices.iter().map(|choice| format!("'{choice}'")).collect::<Vec<_>>().join(", ");
            return Err(usage_error(&format!("argument {flag}: invalid choice: '{value}' (choose from {listed})")));
        }
    }
    for flag in ["--port", "--lines"] {
        if let Some(value) = cli.value(flag).filter(|value| value.trim().parse::<i64>().is_err()) {
            return Err(usage_error(&format!("argument {flag}: invalid int value: '{value}'")));
        }
    }
    if cli.command == "create" && cli.value("--port").is_none() {
        return Err(usage_error("the following arguments are required: --port"));
    }
    Ok(Some(cli))
}

pub fn main(args: &[String]) -> Result<(), String> {
    let Some(cli) = parse(args)? else {
        println!("{USAGE}");
        return Ok(());
    };
    let root = cli.root.clone().map_or_else(|| checkout_root().join("managed"), PathBuf::from);
    let root = std::path::absolute(&root).map_err(|e| e.to_string())?;
    if cli.command == "serve" {
        let binary = match cli.value("--binary") {
            Some(binary) => binary.to_string(),
            None => std::env::current_exe().map_err(|e| e.to_string())?.to_string_lossy().into_owned(),
        };
        return serve(&root, cli.value("--listen").unwrap_or(DEFAULT_LISTEN), binary, cli.value("--admin-token-file"));
    }
    client(&cli, &root)
}

struct Client {
    url: String,
    token: String,
}

impl Client {
    fn call(&self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        let url = format!("{}{path}", self.url.trim_end_matches('/'));
        let payload = body.map(|body| body.to_string()).unwrap_or_default();
        let headers = [("Authorization", format!("Bearer {}", self.token)), ("Content-Type", "application/json".to_string())];
        let (status, _, raw) = crate::runtime::upstream::fetch(method, &url, &headers, payload.as_bytes(), Duration::from_secs(60))
            .map_err(|error| format!("manager: cannot reach {}: {error}", self.url))?;
        let answer = serde_json::from_slice::<Value>(&raw);
        if status >= 400 {
            let message = answer
                .ok()
                .and_then(|answer| answer.get("error").map(|error| error.as_str().map_or_else(|| error.to_string(), str::to_string)))
                .unwrap_or_else(|| reason(status).to_string());
            return Err(format!("manager: {method} {path} -> {status}: {message}"));
        }
        answer.map_err(|error| format!("manager: {method} {path}: {error}"))
    }
}

fn client(cli: &Cli, root: &Path) -> Result<(), String> {
    let token = match std::env::var("REX_MANAGER_TOKEN").ok().filter(|token| !token.is_empty()) {
        Some(token) => token,
        None => {
            let path = cli.value("--token-file").map_or_else(|| root.join("admin.token"), PathBuf::from);
            fs::read_to_string(&path)
                .map_err(|_| format!("no admin token: set REX_MANAGER_TOKEN or --token-file (looked in {})", path.display()))?
                .trim()
                .to_string()
        }
    };
    let client = Client { url: cli.value("--url").map_or_else(|| format!("http://{DEFAULT_LISTEN}"), str::to_string), token };
    let indented = |value: &Value| println!("{}", py_dumps(value, Some(1), false, true));
    if cli.command == "list" {
        for row in client.call("GET", "/servers", None)?.as_array().into_iter().flatten() {
            let field = |key: &str| row.get(key).and_then(Value::as_str).unwrap_or("").to_string();
            println!(
                "{:<20} port {:<5} {:<10} desired {:<8} {}",
                field("name"),
                row.get("port").and_then(Value::as_i64).unwrap_or(0),
                field("status"),
                field("desired"),
                field("last_exit")
            );
        }
        return Ok(());
    }
    let name = cli.name.clone().unwrap_or_default();
    if cli.command == "create" {
        let port = cli.value("--port").and_then(|port| port.trim().parse::<i64>().ok()).unwrap_or_default();
        let catalog = match cli.value("--catalog") {
            Some(catalog) => json!(std::path::absolute(catalog).map_err(|e| e.to_string())?),
            None => Value::Null,
        };
        let mut config = json!({
            "name": name, "port": port, "catalog": catalog,
            "part_mode": cli.value("--part-mode").unwrap_or("proxy"), "gdm": cli.value("--gdm").unwrap_or("auto"),
            "host": cli.value("--host").unwrap_or("0.0.0.0"), "restart": cli.value("--restart").unwrap_or("on-failure"),
            "assert_claimed": cli.flags.contains("--assert-claimed"), "verbose": cli.flags.contains("--verbose"),
            "extra_args": cli.extra_args,
        });
        // A key from the variable --<field>-env names (default the field's own), never argv.
        for (field, default_env) in SECRET_ENV {
            let variable = cli.value(&format!("--{}-env", field.replace('_', "-"))).unwrap_or(*default_env).to_string();
            if let Some(value) = std::env::var(variable).ok().filter(|value| !value.is_empty()) {
                config[*field] = json!(value);
            }
        }
        for (field, flag) in [
            ("friendly_name", "--friendly-name"),
            ("announce_url", "--announce-url"),
            ("public_url", "--public-url"),
            ("web_root", "--web-root"),
            ("zurg_mount", "--zurg-mount"),
        ] {
            if let Some(value) = cli.value(flag).filter(|value| !value.is_empty()) {
                config[field] = json!(value);
            }
        }
        indented(&client.call("POST", "/servers", Some(config))?);
        return Ok(());
    }
    // Names are a-z, 0-9 and -: quoting changes nothing a valid name holds.
    let quoted = name.bytes().map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") }).collect::<String>();
    match cli.command.as_str() {
        "start" | "stop" | "restart" => {
            let row = client.call("POST", &format!("/servers/{quoted}/{}", cli.command), None)?;
            let field = |key: &str| row.get(key).and_then(Value::as_str).unwrap_or("").to_string();
            println!("{}: {} (desired {})", field("name"), field("status"), field("desired"));
        }
        "status" => indented(&client.call("GET", &format!("/servers/{quoted}"), None)?),
        "logs" => {
            let lines = cli.value("--lines").and_then(|lines| lines.trim().parse::<i64>().ok()).unwrap_or(200);
            let answer = client.call("GET", &format!("/servers/{quoted}/logs?lines={lines}"), None)?;
            let lines = answer.get("lines").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str).collect::<Vec<_>>();
            println!("{}", lines.join("\n"));
        }
        "delete" => {
            let purge = if cli.flags.contains("--purge") { "?purge=1" } else { "" };
            println!("{}", py_dumps(&client.call("DELETE", &format!("/servers/{quoted}{purge}"), None)?, None, false, true));
        }
        "build" if cli.flags.contains("--status") => indented(&client.call("GET", &format!("/servers/{quoted}/build"), None)?),
        "build" => {
            let body = json!({"start": cli.flags.contains("--start")});
            indented(&client.call("POST", &format!("/servers/{quoted}/build"), Some(body))?);
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failures_back_off_then_give_up_within_the_window() {
        let mut history = Vec::new();
        let mut delays = Vec::new();
        for at in [0.0, 10.0, 20.0, 30.0] {
            let (next, delay) = backoff(&history, at);
            history = next;
            delays.push(delay);
        }
        assert_eq!(delays, [Some(1), Some(2), Some(4), Some(8)]);
        assert_eq!(backoff(&history, 40.0), (vec![0.0, 10.0, 20.0, 30.0, 40.0], None), "the fifth in ten minutes gives up");
        // Failures older than the window are forgotten: the count starts over.
        let (kept, delay) = backoff(&history, 615.0);
        assert_eq!((kept, delay), (vec![20.0, 30.0, 615.0], Some(4)));
        assert_eq!(backoff(&[], 0.0).1, Some(1));
    }

    #[test]
    fn exits_are_described_as_popen_reports_them() {
        assert_eq!(describe_exit(None), None);
        assert_eq!(describe_exit(Some(2)).as_deref(), Some("exit 2"));
        assert_eq!(describe_exit(Some(-9)).as_deref(), Some("killed by SIGKILL"));
        assert_eq!(describe_exit(Some(-15)).as_deref(), Some("killed by SIGTERM"));
        assert_eq!(describe_exit(Some(-64)).as_deref(), Some("killed by signal 64"));
    }

    #[test]
    fn a_configuration_is_validated_and_defaulted() {
        let made = validate(json!({"name": "den", "port": 32401}), false).unwrap();
        assert_eq!(
            Value::Object(made),
            json!({"name": "den", "port": 32401, "host": "0.0.0.0", "part_mode": "proxy", "gdm": "auto",
                   "restart": "on-failure", "assert_claimed": false, "verbose": false, "extra_args": []})
        );
        let refused = |config: Value, partial: bool| validate(config, partial).err().map(|Refused(status, message)| (status, message));
        assert_eq!(refused(json!({"port": 32401}), false), Some((400, "name is required".to_string())));
        assert_eq!(refused(json!({"name": "den", "port": ""}), false), Some((400, "port is required".to_string())));
        assert_eq!(refused(json!({"name": "Bad Name", "port": 32401}), false).unwrap().1, "name must be 1-40 characters of a-z, 0-9 and -");
        assert_eq!(refused(json!({"name": "den", "port": 80}), false).unwrap().1, "port must be an integer from 1024 to 65535");
        assert_eq!(refused(json!({"name": "den", "port": true}), false).unwrap().1, "port must be an integer from 1024 to 65535");
        assert_eq!(refused(json!({"name": "den", "port": 32401, "zeta": 1, "colour": 2}), false).unwrap().1, "unknown field(s): colour, zeta");
        assert_eq!(refused(json!({"name": "den", "port": 32401, "catalog": "rel.json"}), false).unwrap().1, "catalog must be an absolute path to a .json or .db catalog");
        assert_eq!(refused(json!({"name": "den", "port": 32401, "gdm": "maybe"}), false).unwrap().1, "gdm must be one of auto, on, off");
        assert_eq!(refused(json!({"name": "den", "port": 32401, "verbose": 1}), false).unwrap().1, "verbose must be true or false");
        assert_eq!(refused(json!({"name": "den", "port": 32401, "extra_args": [1]}), false).unwrap().1, "extra_args must be a list of strings");
        // A claimed server announces with its own token; an unclaimed one waits for its claim.
        assert!(validate(json!({"name": "den", "port": 32401, "announce_url": "https://x"}), false).is_ok());
        // Saved before rex-direct needed no key: still accepted, and reaches no server.
        assert!(validate(json!({"name": "den", "port": 32401, "dmm_api_key": "old"}), false).is_ok());
        assert_eq!(refused(json!({"name": "den", "port": 32401, "rex_direct_url": 1}), false).unwrap().1, "rex_direct_url must be a string");
        assert_eq!(refused(json!([]), true).unwrap().1, "the configuration must be a JSON object");
    }

    #[test]
    fn keys_are_redacted() {
        let config = json!({"name": "den", "token": "secret", "tmdb_key": "", "plex_token": "p", "dmm_api_key": "d",
                            "rex_direct_key": "k", "rex_direct_url": "https://provider.example/r", "zurg_mount": "/mnt/zurg"});
        assert_eq!(
            redact(config.as_object().unwrap()),
            json!({"name": "den", "token": "***", "tmdb_key": "", "plex_token": "***", "dmm_api_key": "***",
                   "rex_direct_key": "***", "rex_direct_url": "https://provider.example/r", "zurg_mount": "/mnt/zurg"})
        );
    }

    #[test]
    fn query_values_are_parse_qs() {
        assert_eq!(query_value("purge=1", "purge").as_deref(), Some("1"));
        assert_eq!(query_value("purge=&purge=true", "purge").as_deref(), Some("true"));
        assert_eq!(query_value("lines=2%30", "lines").as_deref(), Some("20"));
        assert_eq!(query_value("other=1", "purge"), None);
    }
}
