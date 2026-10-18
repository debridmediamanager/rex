//! rex-direct: DMM's own plex.direct. With `DMM_API_KEY` (a DMM sponsor's key) rex POSTs
//! its machineIdentifier to DMM (`REGISTER_URL`, or `REX_DIRECT_URL`), which points
//! `<hash>.rex-direct.debridmediamanager.com` at the IP the request came from and answers
//! that name with the wildcard certificate for it (dmm `src/pages/api/rex-direct/register.ts`).
//! rex serves that certificate to a client asking for that name and announces
//! `https://<name>:<port>` beside `--announce-url`'s list, so the Plex apps reach it over
//! HTTPS from anywhere without the operator owning a domain.
//!
//! Registering again every 6 h is the dynamic DNS (DMM writes the record only when the IP
//! changed) and picks up a renewed certificate without a restart. The answer is kept in
//! `rex-direct.json` beside the catalog, owner-only since it holds the private key, and serves
//! a start DMM does not answer. Nothing here is ever fatal: without an answer rex runs as it
//! would without the key.

use std::io::Write;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rustls::server::{ClientHello, ResolvesServerCert};
use rustls::sign::CertifiedKey;
use serde_json::{json, Value};

use super::logging::{info, warning};
use super::plextv::{Beat, PlexTv};
use super::upstream;

const REGISTER_URL: &str = "https://debridmediamanager.com/api/rex-direct/register";
const REFRESH: Duration = Duration::from_secs(6 * 3600);
const RETRY: Duration = Duration::from_secs(600);
/// Short: the start waits on it before the port opens.
const TIMEOUT: Duration = Duration::from_secs(10);

/// What the TLS port serves, by SNI: the rex-direct certificate for its name and any other
/// under its wildcard, `--tls-cert`'s for every other name (no SNI included), and the
/// rex-direct one for every name when there is no `--tls-cert`.
#[derive(Debug)]
pub(crate) struct Certificates {
    pub base: Option<Arc<CertifiedKey>>,
    /// The rex-direct name and its certificate, replaced in place by a renewal.
    direct: Mutex<Option<(String, Arc<CertifiedKey>)>>,
}

impl Certificates {
    pub(crate) fn new(base: Option<Arc<CertifiedKey>>) -> Self {
        Self { base, direct: Mutex::new(None) }
    }
}

impl ResolvesServerCert for Certificates {
    fn resolve(&self, hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        let direct = self
            .direct
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        pick(self.base.as_ref(), direct.as_ref(), hello.server_name())
    }
}

fn pick<T: Clone>(base: Option<&T>, direct: Option<&(String, T)>, sni: Option<&str>) -> Option<T> {
    match direct {
        Some((name, key)) if base.is_none() || covers(name, sni) => Some(key.clone()),
        _ => base.cloned(),
    }
}

/// `sni` is `name` or another name under its wildcard (`*.<name's parent>`). The parent is
/// read off the name DMM answered, so a test stub's domain works as DMM's does.
fn covers(name: &str, sni: Option<&str>) -> bool {
    let Some(sni) = sni.map(str::to_ascii_lowercase) else {
        return false;
    };
    let name = name.to_ascii_lowercase();
    let parent = name.split_once('.').map_or("", |(_, parent)| parent);
    sni == name
        || (parent.contains('.')
            && sni
                .strip_suffix(parent)
                .and_then(|label| label.strip_suffix('.'))
                .is_some_and(|label| !label.is_empty() && !label.contains('.')))
}

/// DMM's answer: the name, the IP it points at, and the wildcard certificate.
pub(crate) struct Registration {
    hostname: String,
    ip: String,
    certificate: String,
    private_key: String,
    /// The answer as DMM sent it: what `rex-direct.json` holds.
    raw: Vec<u8>,
}

/// An answer, from DMM or from `rex-direct.json`. Errors never quote it: it holds the key.
fn parse(body: &[u8]) -> Result<Registration, String> {
    let answer: Value = serde_json::from_slice(body).map_err(|error| format!("not JSON ({error})"))?;
    let field = |name: &str| {
        answer
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or_else(|| format!("no {name} in the answer"))
    };
    let hostname = field("hostname")?;
    // It becomes an announced URL and the SNI rule: a DNS name and nothing else.
    if !hostname.contains('.') || !hostname.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-') {
        return Err("the answer's hostname is not a DNS name".to_string());
    }
    Ok(Registration {
        hostname,
        ip: field("ip")?,
        certificate: field("certificate")?,
        private_key: field("privateKey")?,
        raw: body.to_vec(),
    })
}

/// `--announce-url`'s list with the rex-direct URL appended.
fn announce_list(announce: Option<&str>, direct: Option<&str>) -> Option<String> {
    let list = announce.into_iter().chain(direct).filter(|url| !url.is_empty()).collect::<Vec<_>>();
    (!list.is_empty()).then(|| list.join(","))
}

/// The server's rex-direct registration: what it registers as and announces with.
pub(crate) struct RexDirect {
    key: String,
    url: String,
    machine: String,
    name: String,
    announce: Option<String>,
    port: u16,
    file: PathBuf,
}

impl RexDirect {
    /// On with `DMM_API_KEY` in the environment (never argv). `machine` is the identity rex
    /// announces on; a placeholder one is every copy's, so it registers nothing.
    pub(crate) fn configured(machine: &str, name: &str, announce: Option<String>, port: u16, catalog: &std::path::Path) -> Option<Self> {
        let key = std::env::var("DMM_API_KEY").ok().filter(|key| !key.is_empty())?;
        if super::placeholder_machine(machine) {
            warning!("rex-direct: off -- the catalog's machineIdentifier is a placeholder (seed one with rex catalog --seed)");
            return None;
        }
        Some(Self {
            key,
            url: std::env::var("REX_DIRECT_URL").ok().filter(|url| !url.is_empty()).unwrap_or_else(|| REGISTER_URL.to_string()),
            machine: machine.to_string(),
            name: name.to_string(),
            announce,
            // The port routers forward to rex, when it is not rex's own.
            port: std::env::var("REX_DIRECT_PORT").ok().and_then(|port| port.parse().ok()).unwrap_or(port),
            file: super::plextv::claim_path(catalog).with_file_name("rex-direct.json"),
        })
    }

    fn register(&self) -> Result<Registration, String> {
        let body = json!({"machineIdentifier": self.machine}).to_string();
        let headers = [
            ("x-api-key", self.key.clone()),
            ("Content-Type", "application/json".to_string()),
            ("User-Agent", upstream::UPSTREAM_UA.to_string()),
        ];
        let (status, _, answer) = upstream::fetch("POST", &self.url, &headers, body.as_bytes(), TIMEOUT)?;
        if status != 200 {
            // DMM says why in `error`: a refused or lapsed key, no certificate yet.
            let reason = serde_json::from_slice::<Value>(&answer)
                .ok()
                .and_then(|answer| answer.get("error").and_then(Value::as_str).map(str::to_string));
            return Err(format!("HTTP {status}{}", reason.map(|reason| format!(" ({reason})")).unwrap_or_default()));
        }
        parse(&answer)
    }

    /// Serve, keep and announce an answer that differs from `previous`.
    fn apply(&self, fresh: &Registration, previous: Option<&Registration>, certificates: &Certificates, plextv: &PlexTv) -> Result<(), String> {
        if previous.is_some_and(|previous| previous.raw == fresh.raw) {
            return Ok(());
        }
        let key = super::certified_key(fresh.certificate.as_bytes(), fresh.private_key.as_bytes(), "from DMM")?;
        *certificates.direct.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some((fresh.hostname.clone(), key));
        if let Err(error) = self.keep(&fresh.raw) {
            warning!("rex-direct: could not keep {}: {error}", self.file.display());
        }
        if previous.is_none_or(|previous| previous.hostname != fresh.hostname) {
            let url = format!("https://{}:{}", fresh.hostname, self.port);
            *plextv.beat.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) =
                announce_list(self.announce.as_deref(), Some(&url)).map(|url| Beat {
                    machine: self.machine.clone(),
                    url,
                    name: self.name.clone(),
                });
        }
        if previous.is_none_or(|previous| (&previous.hostname, &previous.ip) != (&fresh.hostname, &fresh.ip)) {
            info!("rex-direct: https://{}:{} -> {}", fresh.hostname, self.port, fresh.ip);
        }
        Ok(())
    }

    /// `rex-direct.json`, owner-only from its first byte: it holds the private key.
    fn keep(&self, raw: &[u8]) -> std::io::Result<()> {
        if std::fs::read(&self.file).is_ok_and(|kept| kept == raw) {
            return Ok(());
        }
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
            options.mode(0o600);
            // An existing file keeps its mode through `open`.
            if self.file.exists() {
                std::fs::set_permissions(&self.file, std::fs::Permissions::from_mode(0o600))?;
            }
        }
        options.open(&self.file)?.write_all(raw)
    }

    /// At start, before the port opens: DMM's answer, else the one kept, else none (a WARN).
    /// Then a thread re-registers every 6 h, 10 min after a failure.
    pub(crate) fn start(self, certificates: Arc<Certificates>, plextv: Arc<PlexTv>) {
        let mut current = match self.register() {
            Ok(fresh) => self.apply(&fresh, None, &certificates, &plextv).map(|_| fresh),
            Err(error) => Err(error),
        };
        let mut failed = current.is_err();
        if let Err(error) = &current {
            warning!("rex-direct: registration failed: {error}");
            current = std::fs::read(&self.file)
                .map_err(|error| error.to_string())
                .and_then(|kept| parse(&kept))
                .and_then(|kept| self.apply(&kept, None, &certificates, &plextv).map(|_| kept));
            match &current {
                Ok(_) => info!("rex-direct: serving the answer kept in {} until DMM answers", self.file.display()),
                Err(_) => warning!("rex-direct: running without it until DMM answers"),
            }
        }
        let mut current = current.ok();
        let _ = thread::Builder::new().name("rex-direct".to_string()).spawn(move || loop {
            thread::sleep(if failed { RETRY } else { REFRESH });
            match self.register().and_then(|fresh| self.apply(&fresh, current.as_ref(), &certificates, &plextv).map(|_| fresh)) {
                Ok(fresh) => {
                    current = Some(fresh);
                    failed = false;
                }
                Err(error) => {
                    warning!("rex-direct: registration failed: {error}; retrying in {} min", RETRY.as_secs() / 60);
                    failed = true;
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn its_name_and_its_wildcard_get_the_rex_direct_certificate() {
        let direct = ("a1b2.rex-direct.debridmediamanager.com".to_string(), "direct");
        let cases = [
            (Some("a1b2.rex-direct.debridmediamanager.com"), "direct"),
            (Some("A1B2.Rex-Direct.DebridMediaManager.com"), "direct"),
            // Another server's name under the same wildcard: the certificate covers it.
            (Some("ffff.rex-direct.debridmediamanager.com"), "direct"),
            (Some("rex.example.com"), "base"),
            (None, "base"),
            // Not under the wildcard: a deeper name, a lookalike, the parent itself.
            (Some("x.a1b2.rex-direct.debridmediamanager.com"), "base"),
            (Some("evilrex-direct.debridmediamanager.com"), "base"),
            (Some("rex-direct.debridmediamanager.com"), "base"),
        ];
        for (sni, want) in cases {
            assert_eq!(pick(Some(&"base"), Some(&direct), sni), Some(want), "SNI {sni:?}");
        }
    }

    #[test]
    fn without_a_base_certificate_every_name_gets_rex_direct_and_without_either_none() {
        let direct = ("a1b2.rex-direct.test".to_string(), "direct");
        for sni in [Some("a1b2.rex-direct.test"), Some("rex.example.com"), None] {
            assert_eq!(pick(None, Some(&direct), sni), Some("direct"), "SNI {sni:?}");
            assert_eq!(pick(Some(&"base"), None, sni), Some("base"), "SNI {sni:?}");
            assert_eq!(pick::<&str>(None, None, sni), None, "SNI {sni:?}");
        }
    }

    #[test]
    fn dmm_answer_is_read_and_a_broken_one_refused_without_quoting_it() {
        let answer = json!({
            "hostname": "a1b2.rex-direct.debridmediamanager.com", "hash": "a1b2", "ip": "203.0.113.7",
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
            "privateKey": "-----BEGIN EC PRIVATE KEY-----\nSECRET\n-----END EC PRIVATE KEY-----\n",
        })
        .to_string();
        let parsed = parse(answer.as_bytes()).expect("DMM's answer parses");
        assert_eq!(parsed.hostname, "a1b2.rex-direct.debridmediamanager.com");
        assert_eq!(parsed.ip, "203.0.113.7");
        assert!(parsed.certificate.contains("BEGIN CERTIFICATE"));
        assert!(parsed.private_key.contains("SECRET"));
        assert_eq!(parsed.raw, answer.as_bytes());

        let mut missing: Value = serde_json::from_str(&answer).unwrap();
        missing.as_object_mut().unwrap().remove("privateKey");
        assert_eq!(parse(missing.to_string().as_bytes()).err().as_deref(), Some("no privateKey in the answer"));
        let mut bad = missing.clone();
        bad["privateKey"] = json!("SECRET");
        bad["hostname"] = json!("evil.example/path");
        let error = parse(bad.to_string().as_bytes()).err().expect("a non-DNS hostname is refused");
        assert!(!error.contains("SECRET") && !error.contains("evil"), "{error}");
        let error = parse(b"{\"privateKey\": \"SECRET\"").err().expect("truncated JSON is refused");
        assert!(!error.contains("SECRET"), "{error}");
    }

    #[test]
    fn rex_direct_url_is_appended_to_the_announce_list() {
        assert_eq!(announce_list(Some("http://10.0.0.1:32400"), Some("https://a.b.c:32400")).as_deref(),
                   Some("http://10.0.0.1:32400,https://a.b.c:32400"));
        assert_eq!(announce_list(None, Some("https://a.b.c:32400")).as_deref(), Some("https://a.b.c:32400"));
        assert_eq!(announce_list(None, None), None);
    }
}
