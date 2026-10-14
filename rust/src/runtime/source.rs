//! Sources: where a Part's bytes come from, and who may push items (`feed.rs`).
//!
//! rex reads no media itself. A source is any HTTP server that answers a Range GET
//! with a file's bytes -- zurg's WebDAV is one -- plus the feed token it pushes its items
//! with. A row the feed made names its source and the file's path under the source's url
//! (`_source`, `_path`); a catalog row with neither reads from the source whose `mount`
//! holds its Part's `file`, which is how parts were read before there was a feed.
//!
//! `--sources FILE` (or `$REX_SOURCES`) names them, as `docs/feed-api.md` describes.
//! Without one, `ZURG_URL` (+`ZURG_USER`/`ZURG_PASSWORD`) is the source `zurg`, read at
//! `$ZURG_URL/dav` under `--zurg-mount`, pushing with `$REX_FEED_TOKEN`.

use super::upstream;
use super::*;
use std::time::Duration;

#[derive(Debug, Clone)]
pub(crate) struct Source {
    pub name: String,
    base: String,
    /// `Basic <base64>` or `Bearer <token>` for the bytes; never logged.
    auth: Option<String>,
    /// What the source sends as `Authorization: Bearer` to the feed; never logged.
    pub token: Option<String>,
    /// The prefix the source's files carry as a Part's `file`, without a trailing `/`.
    pub mount: Option<String>,
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
    pub(crate) fn new(name: &str, base: &str, auth: Option<String>, token: Option<String>, mount: Option<&str>) -> Self {
        Self {
            name: name.to_string(),
            base: base.trim_end_matches('/').to_string(),
            auth,
            token: token.filter(|token| !token.is_empty()),
            mount: mount.filter(|mount| !mount.is_empty()).map(trimmed_mount),
        }
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

    /// Whether `presented` is this source's feed token (compared without an early exit).
    pub(super) fn admits(&self, presented: &str) -> bool {
        let Some(token) = &self.token else {
            return false;
        };
        let (a, b) = (token.as_bytes(), presented.as_bytes());
        a.len() == b.len() && a.iter().zip(b).fold(0u8, |diff, (x, y)| diff | (x ^ y)) == 0
    }
}

/// The sources: the file's (`--sources` / `$REX_SOURCES`), then `ZURG_URL`'s as
/// `zurg` unless the file names one. A file that cannot be read or parsed stops the start.
pub(crate) fn load(file: Option<&Path>, zurg_mount: &str) -> Result<Vec<Arc<Source>>, String> {
    let mut sources = Vec::new();
    let from_env = std::env::var("REX_SOURCES").ok().filter(|path| !path.is_empty()).map(PathBuf::from);
    if let Some(path) = file.map(Path::to_path_buf).or(from_env) {
        let text = fs::read_to_string(&path).map_err(|error| format!("cannot read sources {}: {error}", path.display()))?;
        let document: Value =
            serde_json::from_str(&text).map_err(|error| format!("cannot parse sources {}: {error}", path.display()))?;
        let Some(entries) = document.as_object() else {
            return Err(format!("sources {} must be a JSON object of name -> source", path.display()));
        };
        for (name, entry) in entries {
            if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
                return Err(format!("source name {name:?}: letters, digits, '-' and '_' only"));
            }
            let field = |key: &str| entry.get(key).and_then(Value::as_str).unwrap_or_default().to_string();
            let url = field("url");
            if !url.starts_with("http://") && !url.starts_with("https://") {
                return Err(format!("source {name}: url must be http(s)"));
            }
            let auth = Source::basic(&field("user"), &field("password"))
                .or_else(|| Some(field("bearer")).filter(|bearer| !bearer.is_empty()).map(|bearer| format!("Bearer {bearer}")));
            sources.push(Arc::new(Source::new(name, &url, auth, Some(field("token")), Some(&field("mount")))));
        }
    }
    let zurg_url = std::env::var("ZURG_URL").ok().filter(|url| !url.is_empty());
    if let Some(url) = zurg_url.filter(|_| !sources.iter().any(|source| source.name == "zurg")) {
        let auth = Source::basic(
            &std::env::var("ZURG_USER").unwrap_or_default(),
            &std::env::var("ZURG_PASSWORD").unwrap_or_default(),
        );
        sources.push(Arc::new(Source::new(
            "zurg",
            &format!("{}/dav", url.trim_end_matches('/')),
            auth,
            std::env::var("REX_FEED_TOKEN").ok(),
            Some(zurg_mount),
        )));
    }
    Ok(sources)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_mount_path_and_a_feed_path_name_the_same_url() {
        let source = Source::new("zurg", "http://127.0.0.1:9999/dav/", None, None, Some("/mnt/zurg/"));
        let file = "/mnt/zurg/movies/A+B (2020)/A+B.mkv";
        let path = source.path_under_mount(file).unwrap();
        assert_eq!(path, "movies/A+B (2020)/A+B.mkv");
        assert_eq!(source.default_file(&path), file);
        assert_eq!(source.url(&path), "http://127.0.0.1:9999/dav/movies/A%2BB%20%282020%29/A%2BB.mkv");
        assert_eq!(source.path_under_mount("/rex/1/movie.mkv"), None);
        assert_eq!(source.path_under_mount("/mnt/zurgish/x.mkv"), None);
        let bare = Source::new("nas", "https://nas/media", None, Some("t".into()), None);
        assert_eq!(bare.default_file("a/b.mkv"), "/a/b.mkv");
        assert_eq!(bare.path_under_mount("/a/b.mkv"), None);
        assert!(bare.admits("t") && !bare.admits("tt") && !bare.admits(""));
        assert!(!source.admits(""), "a source without a token admits nobody");
    }
}
