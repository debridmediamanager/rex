//! Sources: where a Part's bytes come from, registered over rex's own API (`feed.rs`).
//!
//! rex reads no media itself. A source is any HTTP server that answers a Range GET with a
//! file's bytes -- zurg's WebDAV is one -- registered by name with `PUT /rex/sources/{name}`
//! and kept in the state file, so it outlives a restart before the source registers again.
//! Registering is the only way a source exists: rex is configured with none.
//! A row the feed made names its source and the file's path under the source's url
//! (`_source`, `_path`), and plays again once that name is registered; a catalog row with
//! neither reads from the source whose `mount` holds its Part's `file`, which is how parts
//! were read before there was a feed.

use super::upstream;
use super::*;
use std::time::Duration;

#[derive(Debug, Clone)]
pub(crate) struct Source {
    pub name: String,
    base: String,
    /// `Basic <base64>` or `Bearer <token>` for the bytes; never logged or answered.
    auth: Option<String>,
    /// The prefix the source's files carry as a Part's `file`, without a trailing `/`.
    pub mount: Option<String>,
    /// What registered it, credentials included, as the state file keeps it.
    pub record: Value,
    /// The directories it asks a library of their own for (`directories`, `declare_libraries`).
    pub directories: Vec<Declared>,
    /// The `directories` entries it sent that were left out, `{"entry", "reason"}`: answered
    /// to the registration and logged, never kept.
    pub ignored: Vec<Value>,
    /// False when it registered `"trash": false`: an item it removes is deleted, never kept in
    /// the trash for its file to come back. A live channel that ends never does: the production
    /// server's Live Sports library held 2,115 trashed channels beside 853 live ones (2026-11-01).
    pub trash: bool,
}

/// One entry of a registration's `directories`: the first directory of the source's item
/// paths, and the titles of the movie and show libraries it is to have.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Declared {
    pub name: String,
    pub movies: Option<String>,
    pub shows: Option<String>,
}

/// A registration's `directories` and the entries left out, with why. Absent, `null` (a Go nil
/// slice) or empty is none. A malformed entry, or a value that is no list, is left out and the
/// source registers all the same: refusing it stopped the whole feed over one typo in a config.
fn declared(value: Option<&Value>) -> (Vec<Declared>, Vec<Value>) {
    let entries = match value {
        None | Some(Value::Null) => return (Vec::new(), Vec::new()),
        Some(Value::Array(entries)) => entries,
        Some(other) => return (Vec::new(), vec![json!({"entry": other, "reason": format!("directories {other}: not a list")})]),
    };
    // Each reason names the directory it is about: zurg logs each distinct one once.
    let check = |entry: &Value| -> Result<Declared, String> {
        let Some(name) = entry.get("name").and_then(Value::as_str).filter(|name| !name.is_empty()) else {
            return Err(format!("directory entry {entry}: no name"));
        };
        if name.contains('/') {
            return Err(format!("directory {name:?}: name must be one directory, with no '/'"));
        }
        let title = |kind: &str| match entry.get(kind) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(title)) if !title.trim().is_empty() => Ok(Some(title.clone())),
            Some(Value::String(_)) => Err(format!("directory {name:?}: {kind} title is empty")),
            Some(_) => Err(format!("directory {name:?}: {kind} title must be text")),
        };
        let (movies, shows) = (title("movies")?, title("shows")?);
        if movies.is_none() && shows.is_none() {
            return Err(format!("directory {name:?}: names no shows or movies library"));
        }
        Ok(Declared { name: name.to_string(), movies, shows })
    };
    let (mut kept, mut ignored) = (Vec::new(), Vec::new());
    for entry in entries {
        match check(entry) {
            Ok(declared) => kept.push(declared),
            Err(reason) => ignored.push(json!({"entry": entry, "reason": reason})),
        }
    }
    (kept, ignored)
}

/// One path segment as a URL carries it: unreserved bytes pass, every other is `%XX`
/// (a `+` in a release name is a literal plus, never a space).
fn quote_segment(segment: &str) -> String {
    let mut out = String::new();
    for byte in segment.bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// The extensions taken as media; anything else in a release (subtitles, nfo, samples'
/// images) is not a Part.
pub(super) fn is_video(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    [
        ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mts", ".webm", ".wmv",
        ".mpg", ".mpeg", ".flv", ".divx", ".iso",
    ]
    .iter()
    .any(|extension| lower.ends_with(extension))
}

fn trimmed_mount(mount: &str) -> String {
    let mount = mount.trim_end_matches('/');
    if mount.is_empty() { "/".to_string() } else { mount.to_string() }
}

impl Source {
    pub(crate) fn new(name: &str, base: &str, auth: Option<String>, mount: Option<&str>) -> Self {
        Self {
            name: name.to_string(),
            base: base.trim_end_matches('/').to_string(),
            auth,
            mount: mount.filter(|mount| !mount.is_empty()).map(trimmed_mount),
            record: Value::Null,
            directories: Vec::new(),
            ignored: Vec::new(),
            trash: true,
        }
    }

    /// A `PUT /rex/sources/{name}` body: `url` (http or https), `user`/`password` or
    /// `bearer` for it, `mount`, `directories` and `trash`. Answers why it is refused.
    pub(crate) fn register(name: &str, body: &Value) -> Result<Self, String> {
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
            return Err(format!("source name {name:?}: letters, digits, '-' and '_' only"));
        }
        if !body.is_object() {
            return Err("the body must be a JSON object".to_string());
        }
        let field = |key: &str| body.get(key).and_then(Value::as_str).unwrap_or_default().to_string();
        let url = field("url");
        let Some(rest) = url.strip_prefix("http://").or_else(|| url.strip_prefix("https://")) else {
            return Err("url must be http(s)".to_string());
        };
        // GET /rex/sources answers the url: credentials in it would be answered with it.
        if rest.split(['/', '?', '#']).next().unwrap_or_default().contains('@') {
            return Err("url carries credentials: send them as user and password".to_string());
        }
        let auth = Source::basic(&field("user"), &field("password"))
            .or_else(|| Some(field("bearer")).filter(|bearer| !bearer.is_empty()).map(|bearer| format!("Bearer {bearer}")));
        let (directories, ignored) = declared(body.get("directories"));
        let mut record = Map::from_iter(
            ["url", "user", "password", "bearer", "mount"]
                .into_iter()
                .filter_map(|key| Some((key.to_string(), json!(Some(field(key)).filter(|value| !value.is_empty())?)))),
        );
        // Kept only when it names one, so a registration without it is stored as before.
        if !directories.is_empty() {
            let entries = directories.iter().map(|entry| {
                let mut out = json!({"name": entry.name});
                for (kind, title) in [("shows", &entry.shows), ("movies", &entry.movies)] {
                    if let Some(title) = title {
                        out[kind] = json!(title);
                    }
                }
                out
            });
            record.insert("directories".to_string(), Value::Array(entries.collect()));
        }
        let trash = body.get("trash") != Some(&Value::Bool(false));
        if !trash {
            record.insert("trash".to_string(), json!(false));
        }
        let mut source = Source::new(name, &url, auth, Some(&field("mount")));
        source.trash = trash;
        source.record = Value::Object(record);
        source.directories = directories;
        source.ignored = ignored;
        Ok(source)
    }

    /// What the sources API answers for it: never a credential, only which kind it has.
    pub(crate) fn public(&self) -> Value {
        let auth = self.auth.as_deref().and_then(|auth| auth.split(' ').next()).map(str::to_ascii_lowercase);
        // `features`: what a source may lean on beyond the base contract -- `probe_digest`, an
        // item's `probe` standing for a `media_info` rex keeps (`feed::upsert`, `wants`).
        let mut public = json!({"name": self.name, "url": self.base, "auth": auth, "mount": self.mount,
                                "features": ["probe_digest"]});
        if let Some(directories) = self.record.get("directories") {
            public["directories"] = directories.clone();
        }
        if !self.trash {
            public["trash"] = json!(false);
        }
        public
    }

    fn basic(user: &str, password: &str) -> Option<String> {
        (!user.is_empty() || !password.is_empty())
            .then(|| format!("Basic {}", base64_encode(format!("{user}:{password}").as_bytes())))
    }

    /// A Part `file` under this source's mount -> its path under the url; `None` outside.
    pub(super) fn path_under_mount(&self, file: &str) -> Option<String> {
        let mount = self.mount.as_deref()?;
        let rest = if mount == "/" {
            file.strip_prefix('/')?
        } else {
            file.strip_prefix(mount)?.strip_prefix('/')?
        };
        let rest = rest.trim_matches('/');
        (!rest.is_empty()).then(|| rest.to_string())
    }

    /// The Part `file` clients see for a path an item did not give one for.
    pub(super) fn default_file(&self, path: &str) -> String {
        match self.mount.as_deref() {
            Some("/") | None => format!("/{path}"),
            Some(mount) => format!("{mount}/{path}"),
        }
    }

    /// The URL of a path under the source, each segment escaped.
    pub(super) fn url(&self, path: &str) -> String {
        let escaped = path
            .trim_matches('/')
            .split('/')
            .map(quote_segment)
            .collect::<Vec<_>>()
            .join("/");
        format!("{}/{escaped}", self.base)
    }

    /// What the transcoder's FFmpeg reads a path with: its URL and the source's auth.
    pub(super) fn ffmpeg_input(&self, path: &str) -> (String, Option<String>) {
        (self.url(path), self.auth.clone())
    }

    /// An artwork url under this source's url -> its path there, as written; `None` elsewhere.
    pub(super) fn own_path(&self, url: &str) -> Option<String> {
        let rest = url.strip_prefix(&self.base)?.strip_prefix('/')?;
        (!rest.is_empty()).then(|| rest.to_string())
    }

    /// A `source://` artwork's url and the headers that fetch it: the path as the source wrote
    /// it (`own_path`), not escaped again.
    pub(super) fn artwork(&self, path: &str) -> (String, Vec<(&'static str, String)>) {
        (format!("{}/{path}", self.base), self.headers())
    }

    fn headers(&self) -> Vec<(&'static str, String)> {
        let mut headers = vec![("User-Agent", upstream::UPSTREAM_UA.to_string())];
        if let Some(auth) = &self.auth {
            headers.push(("Authorization", auth.clone()));
        }
        headers
    }

    /// Open a file's bytes: a GET with the caller's headers (a `Range`) and the source's auth.
    pub(super) fn open(
        &self,
        path: &str,
        extra: &[(&'static str, String)],
    ) -> Result<upstream::Upstream, String> {
        let mut headers = self.headers();
        headers.extend(extra.iter().cloned());
        upstream::open("GET", &self.url(path), &headers, b"", Duration::from_secs(30))
    }

    /// `open` that answers a redirect to another origin instead of following it.
    pub(super) fn open_here(
        &self,
        path: &str,
        extra: &[(&'static str, String)],
        method: &str,
    ) -> Result<upstream::Opened, String> {
        let mut headers = self.headers();
        headers.extend(extra.iter().cloned());
        upstream::open_here(method, &self.url(path), &headers, Duration::from_secs(30))
    }

    /// `checkFiles`: whether the source still serves the file. Only its own 404 is "gone";
    /// a transport failure or any other answer is not collapsed into a missing file.
    pub(super) fn exists(&self, path: &str) -> bool {
        match upstream::open("HEAD", &self.url(path), &self.headers(), b"", Duration::from_secs(15)) {
            Ok(response) => response.status != 404,
            Err(_) => true,
        }
    }

    /// Delete a file at the source the way a media server's delete reaches zurg through its
    /// mount: a WebDAV `DELETE` of its path. Answers the source's status.
    pub(super) fn delete(&self, path: &str) -> Result<u16, String> {
        upstream::open("DELETE", &self.url(path), &self.headers(), b"", Duration::from_secs(60)).map(|response| response.status)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_mount_path_and_a_feed_path_name_the_same_url() {
        let source = Source::new("zurg", "http://127.0.0.1:9999/dav/", None, Some("/mnt/zurg/"));
        let file = "/mnt/zurg/movies/A+B (2020)/A+B.mkv";
        let path = source.path_under_mount(file).unwrap();
        assert_eq!(path, "movies/A+B (2020)/A+B.mkv");
        assert_eq!(source.default_file(&path), file);
        assert_eq!(source.url(&path), "http://127.0.0.1:9999/dav/movies/A%2BB%20%282020%29/A%2BB.mkv");
        assert_eq!(source.path_under_mount("/rex/1/movie.mkv"), None);
        assert_eq!(source.path_under_mount("/mnt/zurgish/x.mkv"), None);
        let bare = Source::new("nas", "https://nas/media", None, None);
        assert_eq!(bare.default_file("a/b.mkv"), "/a/b.mkv");
        assert_eq!(bare.path_under_mount("/a/b.mkv"), None);
    }

    #[test]
    fn a_registration_is_checked_and_answered_without_its_credentials() {
        let body = json!({"url": "http://127.0.0.1:9999/dav", "user": "zurg-user", "password": "s3cret", "mount": "/mnt/zurg"});
        let source = Source::register("zurg", &body).unwrap();
        assert_eq!(source.url("movies/a.mkv"), "http://127.0.0.1:9999/dav/movies/a.mkv");
        assert_eq!(source.headers()[1], ("Authorization", format!("Basic {}", base64_encode(b"zurg-user:s3cret"))));
        assert_eq!(source.record, body, "the state file keeps what registered it");
        let public = source.public();
        assert_eq!(public["auth"], "basic");
        assert!(!public.to_string().contains("s3cret") && !public.to_string().contains("zurg-user"));
        let bearer = Source::register("nas", &json!({"url": "https://nas/media", "bearer": "tok"})).unwrap();
        assert_eq!(bearer.public()["auth"], "bearer");
        assert!(!bearer.public().to_string().contains("tok"));
        assert_eq!(Source::register("plain", &json!({"url": "http://h/x"})).unwrap().public()["auth"], Value::Null);
        for (name, body) in [
            ("bad.name", json!({"url": "http://h/x"})),
            ("", json!({"url": "http://h/x"})),
            ("ok", json!({"url": "ftp://h/x"})),
            ("ok", json!({})),
            ("ok", json!({"url": "http://user:pass@h/x"})),
            ("ok", json!(["not", "an", "object"])),
        ] {
            assert!(Source::register(name, &body).is_err(), "{name:?} {body}");
        }
        assert!(Source::register("ok", &json!({"url": "http://h/x?next=a@b"})).is_ok(), "an @ past the host is no userinfo");
    }

    #[test]
    fn declared_directories_are_checked_kept_and_answered() {
        let body = json!({"url": "http://h/dav", "directories": [
            {"name": "anime", "shows": "Anime", "movies": "Anime Movies"},
            {"name": "wrestling", "shows": "Wrestling", "movies": null},
            {"name": "movies-1080p", "movies": "1080p Movies"}]});
        let source = Source::register("zurg", &body).unwrap();
        assert_eq!(source.directories[0], Declared { name: "anime".into(), movies: Some("Anime Movies".into()), shows: Some("Anime".into()) });
        assert_eq!(source.directories[1].movies, None);
        let kept = json!([{"name": "anime", "shows": "Anime", "movies": "Anime Movies"},
                          {"name": "wrestling", "shows": "Wrestling"}, {"name": "movies-1080p", "movies": "1080p Movies"}]);
        assert_eq!(source.record["directories"], kept);
        assert_eq!(source.public()["directories"], kept);
        assert_eq!(Source::register("zurg", &source.record).unwrap().directories, source.directories, "restored from the state file");
        let live = Source::register("live", &json!({"url": "http://h/x", "trash": false})).unwrap();
        assert!(!live.trash && live.public()["trash"] == json!(false), "{}", live.public());
        assert!(!Source::register("live", &live.record).unwrap().trash, "trash false restored from the state file");
        assert!(Source::register("plain", &json!({"url": "http://h/x"})).unwrap().trash, "trash kept by default");
        for none in [json!({"url": "http://h/dav"}), json!({"url": "http://h/dav", "directories": []}), json!({"url": "http://h/dav", "directories": null})] {
            let plain = Source::register("zurg", &none).unwrap();
            assert!(plain.directories.is_empty() && plain.record == json!({"url": "http://h/dav"}) && plain.public().get("directories").is_none());
        }
        // A malformed entry is left out, with why; the source and its other entries register.
        for (bad, reason) in [(json!("anime"), r#"directories "anime": not a list"#), (json!({"name": "anime"}), r#"directories {"name":"anime"}: not a list"#)] {
            let source = Source::register("zurg", &json!({"url": "http://h/dav", "directories": bad})).unwrap();
            assert_eq!((source.directories.len(), source.ignored.clone()), (0, vec![json!({"entry": bad, "reason": reason})]));
            assert_eq!(source.record, json!({"url": "http://h/dav"}), "nothing ignored is kept");
        }
        let entries = [json!({"name": "anime"}), json!({"shows": "Anime"}), json!({"name": "", "shows": "Anime"}),
                       json!({"name": "a/b", "shows": "Anime"}), json!("anime"), json!({"name": "anime", "shows": ""}),
                       json!({"name": "anime", "shows": 1}), json!({"name": 7, "shows": "Anime"})];
        let mut sent: Vec<Value> = entries.to_vec();
        sent.insert(3, json!({"name": "wrestling", "shows": "Wrestling"}));
        let source = Source::register("zurg", &json!({"url": "http://h/dav", "directories": sent})).unwrap();
        assert_eq!(source.directories, [Declared { name: "wrestling".into(), movies: None, shows: Some("Wrestling".into()) }]);
        assert_eq!(source.ignored.iter().map(|ignored| ignored["entry"].clone()).collect::<Vec<_>>(), entries);
        // Each reason names its directory: zurg logs each distinct reason once.
        let reasons: Vec<&str> = source.ignored.iter().filter_map(|ignored| ignored["reason"].as_str()).collect();
        assert_eq!(reasons, [
            r#"directory "anime": names no shows or movies library"#,
            r#"directory entry {"shows":"Anime"}: no name"#,
            r#"directory entry {"name":"","shows":"Anime"}: no name"#,
            r#"directory "a/b": name must be one directory, with no '/'"#,
            r#"directory entry "anime": no name"#,
            r#"directory "anime": shows title is empty"#,
            r#"directory "anime": shows title must be text"#,
            r#"directory entry {"name":7,"shows":"Anime"}: no name"#,
        ]);
        assert_eq!(source.record["directories"], json!([{"name": "wrestling", "shows": "Wrestling"}]));
    }
}
