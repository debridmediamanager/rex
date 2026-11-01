//! rex-direct: a plex.direct of rex's own, from a provider (docs/rex-direct-api.md). rex POSTs
//! its machineIdentifier with the claim's server token to the provider (`REGISTER_URL`, DMM's,
//! or `REX_DIRECT_URL`), which checks with plex.tv that the token's account owns that server,
//! points a name of its choosing at the IP the request came from and answers that name with a
//! certificate for it (dmm `src/pages/api/rex-direct/register.ts`). rex serves that certificate
//! to a client asking for that name and announces `https://<name>:<port>` beside
//! `--announce-url`'s list, so the Plex apps reach it over HTTPS from anywhere without the
//! operator owning a domain. On by default; `REX_DIRECT_URL=off` turns it off. It may also
//! answer a name on rex's LAN (`lanHostname`, at the `lanIp` rex sent), served and announced
//! after it: plex.tv turns rex's plain LAN connection into a plex.direct name rex has no
//! certificate for, so a client at home otherwise reaches rex only through the router.
//!
//! The provider knows rex by its claim, so rex registers only once claimed: before the port
//! opens on a claimed start, or the moment a claim lands (`plextv::finish_claim`). Registering
//! again every 6 h is the dynamic DNS (DMM writes the record only when the IP changed) and
//! picks up a renewed certificate without a restart. The answer is kept in `rex-direct.json`
//! beside the catalog, owner-only since it holds the private key, and serves a start the
//! provider does not answer. Nothing here is ever fatal: without an answer rex runs as it would
//! without rex-direct.

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

/// What the TLS port serves, by SNI: the rex-direct certificate for its name, its LAN name and
/// any other under its wildcard, `--tls-cert`'s for every other name (no SNI included), and the
/// rex-direct one for every name when there is no `--tls-cert`.
#[derive(Debug)]
pub(crate) struct Certificates {
    pub base: Option<Arc<CertifiedKey>>,
    /// The rex-direct name, whether a LAN name sits under it, and its certificate, replaced in
    /// place by a renewal.
    direct: Mutex<Option<(String, bool, Arc<CertifiedKey>)>>,
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

fn pick<T: Clone>(base: Option<&T>, direct: Option<&(String, bool, T)>, sni: Option<&str>) -> Option<T> {
    match direct {
        Some((name, lan, key)) if base.is_none() || covers(name, *lan, sni) => Some(key.clone()),
        _ => base.cloned(),
    }
}

/// `sni` is `name`, another name under the shared wildcard (`*.<name's parent>`), or, when
/// the provider gave a LAN name, one label under `name` itself, where that name lives (a
/// per-server `*.<name>`). The parent is read off the name the provider answered, so a test
/// stub's domain works as DMM's does. A per-host certificate is for `name` itself.
fn covers(name: &str, lan: bool, sni: Option<&str>) -> bool {
    let Some(sni) = sni.map(str::to_ascii_lowercase) else {
        return false;
    };
    let name = name.to_ascii_lowercase();
    let parent = name.split_once('.').map_or("", |(_, parent)| parent);
    let under = |domain: &str| {
        domain.contains('.')
            && sni
                .strip_suffix(domain)
                .and_then(|label| label.strip_suffix('.'))
                .is_some_and(|label| !label.is_empty() && !label.contains('.'))
    };
    sni == name || under(parent) || (lan && under(&name))
}

/// The provider's answer: the name, the IP it points at, and the certificate for that name.
pub(crate) struct Registration {
    hostname: String,
    ip: String,
    /// A name on rex's LAN under `hostname`, and the address the provider says it points at.
    lan_hostname: Option<String>,
    lan_ip: Option<String>,
    certificate: String,
    private_key: String,
    /// The answer as the provider sent it: what `rex-direct.json` holds.
    raw: Vec<u8>,
}

/// An answer, from the provider or from `rex-direct.json`. Errors never quote it: it holds the key.
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
    // Each name becomes an announced URL and an SNI rule: a DNS name and nothing else.
    let dns_name = |name: &str| name.contains('.') && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-');
    let hostname = field("hostname")?;
    if !dns_name(&hostname) {
        return Err("the answer's hostname is not a DNS name".to_string());
    }
    // Optional: null is its absence, anything else must be a name.
    let lan_hostname = match answer.get("lanHostname") {
        None | Some(Value::Null) => None,
        Some(name) => Some(name.as_str().filter(|name| dns_name(name)).ok_or("the answer's lanHostname is not a DNS name")?.to_string()),
    };
    Ok(Registration {
        hostname,
        ip: field("ip")?,
        lan_hostname,
        lan_ip: answer.get("lanIp").and_then(Value::as_str).filter(|ip| !ip.is_empty()).map(str::to_string),
        certificate: field("certificate")?,
        private_key: field("privateKey")?,
        raw: body.to_vec(),
    })
}

/// No answer to serve: a provider still issuing the certificate (202, not a failure), or a
/// failure. Either way rex keeps serving what it has.
#[derive(Debug, PartialEq)]
enum Later {
    Pending(Duration, Option<String>),
    Failed(String),
}

impl Later {
    /// A 202's body: `retryAfter` seconds, kept within 15 s .. 10 min (60 s when it is not a
    /// whole number), and why, if it says.
    fn pending(body: &[u8]) -> Self {
        let answer = serde_json::from_slice::<Value>(body).unwrap_or(Value::Null);
        let after = answer.get("retryAfter").and_then(Value::as_u64).map_or(60, |seconds| seconds.clamp(15, 600));
        Self::Pending(Duration::from_secs(after), reason(&answer))
    }

    fn after(&self) -> Duration {
        match self {
            Self::Pending(after, _) => *after,
            Self::Failed(_) => RETRY,
        }
    }

    fn log(&self) {
        match self {
            Self::Pending(after, why) => info!(
                "rex-direct: the provider is still issuing the certificate{}; asking again in {} s",
                why.as_ref().map(|why| format!(" ({why})")).unwrap_or_default(),
                after.as_secs()
            ),
            Self::Failed(why) => warning!("rex-direct: registration failed: {why}; retrying in {} min", RETRY.as_secs() / 60),
        }
    }
}

/// The provider says why in `error`.
fn reason(answer: &Value) -> Option<String> {
    answer.get("error").and_then(Value::as_str).map(str::to_string)
}

/// The answer carries a private key and the request the claim's token: https only, or plain
/// http to this machine (a test's stub).
fn trusted(url: &str) -> bool {
    upstream::scheme_host(url).is_some_and(|(tls, host)| {
        tls || host == "localhost" || host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.to_canonical().is_loopback())
    })
}

/// `--announce-url`'s list with the rex-direct URL appended.
fn announce_list(announce: Option<&str>, direct: Option<&str>) -> Option<String> {
    let list = announce.into_iter().chain(direct).filter(|url| !url.is_empty()).collect::<Vec<_>>();
    (!list.is_empty()).then(|| list.join(","))
}

/// The server's rex-direct registration: what it registers as and announces with.
pub(crate) struct RexDirect {
    /// `REX_DIRECT_KEY`, for a provider that wants one; DMM's ignores it.
    key: Option<String>,
    url: String,
    machine: String,
    name: String,
    announce: Option<String>,
    port: u16,
    /// `--port`, which the LAN name is announced on: a client at home reaches rex directly,
    /// not through the router's forward that `REX_DIRECT_PORT` names.
    lan_port: u16,
    file: PathBuf,
    certificates: Arc<Certificates>,
}

/// `PlexTv` is `Debug`; this never prints the key.
impl std::fmt::Debug for RexDirect {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RexDirect").field("url", &self.url).finish_non_exhaustive()
    }
}

impl RexDirect {
    /// On unless `REX_DIRECT_URL` is `off` or not https. `machine` is the identity rex
    /// announces on; a placeholder one is every copy's and can never be claimed.
    pub(crate) fn configured(
        machine: &str,
        name: &str,
        announce: Option<String>,
        port: u16,
        catalog: &std::path::Path,
        certificates: Arc<Certificates>,
    ) -> Option<Self> {
        let url = std::env::var("REX_DIRECT_URL").ok().filter(|url| !url.is_empty()).unwrap_or_else(|| REGISTER_URL.to_string());
        if url.eq_ignore_ascii_case("off") || super::placeholder_machine(machine) {
            return None;
        }
        if !trusted(&url) {
            warning!("rex-direct: off -- REX_DIRECT_URL must be https:// (plain http only to this machine): the answer carries a private key and the request the claim's token");
            return None;
        }
        Some(Self {
            key: std::env::var("REX_DIRECT_KEY").ok().filter(|key| !key.is_empty()),
            url,
            machine: machine.to_string(),
            name: name.to_string(),
            announce,
            // The port routers forward to rex, when it is not rex's own.
            port: std::env::var("REX_DIRECT_PORT").ok().and_then(|port| port.parse().ok()).unwrap_or(port),
            lan_port: port,
            file: super::plextv::claim_path(catalog).with_file_name("rex-direct.json"),
            certificates,
        })
    }

    /// Unclaimed, it waits for the claim; said once, here.
    pub(crate) fn wait(self, plextv: &PlexTv) {
        info!("rex-direct: registers with {} once claimed (POST /myplex/claim)", self.url);
        *plextv.direct.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(self);
    }

    fn register(&self, token: &str) -> Result<Registration, Later> {
        let mut body = json!({"machineIdentifier": self.machine});
        // For a LAN name: plex.tv turns rex's http LAN connection into a plex.direct name rex
        // has no certificate for. Only RFC 1918, since the provider's record takes it on trust.
        if let Some(ip) = super::plextv::lan_ipv4() {
            body["lanIp"] = json!(ip.to_string());
        }
        let body = body.to_string();
        let mut headers = vec![
            ("Content-Type", "application/json".to_string()),
            ("User-Agent", upstream::UPSTREAM_UA.to_string()),
            // How the provider knows the caller owns this server: it asks plex.tv with them.
            ("X-Plex-Token", token.to_string()),
            ("X-Plex-Client-Identifier", self.machine.clone()),
        ];
        if let Some(key) = &self.key {
            headers.push(("Authorization", format!("Bearer {key}")));
        }
        let (status, _, answer) = upstream::fetch("POST", &self.url, &headers, body.as_bytes(), TIMEOUT).map_err(Later::Failed)?;
        match status {
            200 => parse(&answer).map_err(Later::Failed),
            202 => Err(Later::pending(&answer)),
            _ => {
                let reason = serde_json::from_slice::<Value>(&answer).ok().as_ref().and_then(reason);
                Err(Later::Failed(format!("HTTP {status}{}", reason.map(|reason| format!(" ({reason})")).unwrap_or_default())))
            }
        }
    }

    /// Serve, keep and announce an answer that differs from `previous`.
    fn apply(&self, fresh: &Registration, previous: Option<&Registration>, plextv: &PlexTv) -> Result<(), String> {
        if previous.is_some_and(|previous| previous.raw == fresh.raw) {
            return Ok(());
        }
        let key = super::certified_key(fresh.certificate.as_bytes(), fresh.private_key.as_bytes(), "from the rex-direct provider")?;
        *self.certificates.direct.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) =
            Some((fresh.hostname.clone(), fresh.lan_hostname.is_some(), key));
        if let Err(error) = self.keep(&fresh.raw) {
            warning!("rex-direct: could not keep {}: {error}", self.file.display());
        }
        if previous.is_none_or(|previous| (&previous.hostname, &previous.lan_hostname) != (&fresh.hostname, &fresh.lan_hostname)) {
            let mut urls = vec![format!("https://{}:{}", fresh.hostname, self.port)];
            urls.extend(fresh.lan_hostname.iter().map(|lan| format!("https://{lan}:{}", self.lan_port)));
            *plextv.beat.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) =
                announce_list(self.announce.as_deref(), Some(&urls.join(","))).map(|url| Beat {
                    machine: self.machine.clone(),
                    url,
                    name: self.name.clone(),
                });
        }
        if previous.is_none_or(|previous| (&previous.hostname, &previous.ip) != (&fresh.hostname, &fresh.ip)) {
            info!("rex-direct: https://{}:{} -> {}", fresh.hostname, self.port, fresh.ip);
        }
        if let Some(lan) = fresh.lan_hostname.as_ref().filter(|_| {
            previous.is_none_or(|previous| (&previous.lan_hostname, &previous.lan_ip) != (&fresh.lan_hostname, &fresh.lan_ip))
        }) {
            let ip = fresh.lan_ip.as_ref().map(|ip| format!(" -> {ip}")).unwrap_or_default();
            info!("rex-direct: https://{lan}:{}{ip} (LAN)", self.lan_port);
        }
        Ok(())
    }

    /// Register with the claim's server token and serve what comes back.
    fn attempt(&self, previous: Option<&Registration>, plextv: &PlexTv) -> Result<Registration, Later> {
        let token = super::plextv::claim_token(&plextv.claim).ok_or_else(|| Later::Failed("not claimed".to_string()))?;
        let fresh = self.register(&token)?;
        self.apply(&fresh, previous, plextv).map_err(Later::Failed)?;
        Ok(fresh)
    }

    /// `rex-direct.json`, owner-only from its first byte: it holds the private key.
    fn keep(&self, raw: &[u8]) -> std::io::Result<()> {
        if std::fs::read(&self.file).is_ok_and(|kept| kept == raw) {
            return Ok(());
        }
        crate::platform::write_atomic(&self.file, raw, true)
    }

    /// Once claimed, on the caller's thread (a claimed start's, before the port opens): the
    /// provider's answer, else the one kept, else none. Then a thread registers again every
    /// 6 h, 10 min after a failure, and when a 202 says.
    pub(crate) fn start(self, plextv: Arc<PlexTv>) {
        let (mut current, mut next) = match self.attempt(None, &plextv) {
            Ok(fresh) => (Some(fresh), REFRESH),
            Err(later) => {
                later.log();
                let kept = std::fs::read(&self.file)
                    .map_err(|error| error.to_string())
                    .and_then(|kept| parse(&kept))
                    .and_then(|kept| self.apply(&kept, None, &plextv).map(|_| kept))
                    .ok();
                match (&kept, &later) {
                    (Some(_), _) => info!("rex-direct: serving the answer kept in {} until the provider answers", self.file.display()),
                    (None, Later::Failed(_)) => warning!("rex-direct: running without it until the provider answers"),
                    (None, Later::Pending(..)) => {}
                }
                (kept, later.after())
            }
        };
        let _ = thread::Builder::new().name("rex-direct".to_string()).spawn(move || loop {
            thread::sleep(next);
            next = match self.attempt(current.as_ref(), &plextv) {
                Ok(fresh) => {
                    current = Some(fresh);
                    REFRESH
                }
                Err(later) => {
                    later.log();
                    later.after()
                }
            };
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn its_name_and_its_wildcard_get_the_rex_direct_certificate() {
        let direct = ("a1b2.rex-direct.debridmediamanager.com".to_string(), false, "direct");
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
        let direct = ("a1b2.rex-direct.test".to_string(), false, "direct");
        for sni in [Some("a1b2.rex-direct.test"), Some("rex.example.com"), None] {
            assert_eq!(pick(None, Some(&direct), sni), Some("direct"), "SNI {sni:?}");
            assert_eq!(pick(Some(&"base"), None, sni), Some("base"), "SNI {sni:?}");
            assert_eq!(pick::<&str>(None, None, sni), None, "SNI {sni:?}");
        }
    }

    #[test]
    fn the_answer_is_read_and_a_broken_one_refused_without_quoting_it() {
        let answer = json!({
            "hostname": "a1b2.rex-direct.debridmediamanager.com", "hash": "a1b2", "ip": "203.0.113.7",
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
            "privateKey": "-----BEGIN EC PRIVATE KEY-----\nSECRET\n-----END EC PRIVATE KEY-----\n",
        })
        .to_string();
        let parsed = parse(answer.as_bytes()).expect("the answer parses");
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
    fn with_a_lan_name_one_label_under_the_name_gets_the_rex_direct_certificate_too() {
        let name = "a1b2.rex-direct.debridmediamanager.com";
        // (SNI, with a LAN name, without one)
        let cases = [
            (Some("lan.a1b2.rex-direct.debridmediamanager.com"), "direct", "base"),
            (Some("LAN.A1B2.Rex-Direct.DebridMediaManager.com"), "direct", "base"),
            (Some("a1b2.rex-direct.debridmediamanager.com"), "direct", "direct"),
            // The shared wildcard's rule stays.
            (Some("ffff.rex-direct.debridmediamanager.com"), "direct", "direct"),
            // A deeper name, lookalikes, an empty label, the parent itself.
            (Some("x.lan.a1b2.rex-direct.debridmediamanager.com"), "base", "base"),
            (Some("lan.xa1b2.rex-direct.debridmediamanager.com"), "base", "base"),
            (Some("lan.a1b2.rex-direct.debridmediamanager.com.evil.example"), "base", "base"),
            (Some(".a1b2.rex-direct.debridmediamanager.com"), "base", "base"),
            (Some("rex-direct.debridmediamanager.com"), "base", "base"),
            (None, "base", "base"),
        ];
        for (sni, with_lan, without) in cases {
            assert_eq!(pick(Some(&"base"), Some(&(name.to_string(), true, "direct")), sni), Some(with_lan), "SNI {sni:?}, LAN name");
            assert_eq!(pick(Some(&"base"), Some(&(name.to_string(), false, "direct")), sni), Some(without), "SNI {sni:?}, none");
        }
    }

    #[test]
    fn a_lan_name_is_optional_and_a_broken_one_refuses_the_whole_answer() {
        let mut answer = json!({
            "hostname": "a1b2.rex-direct.debridmediamanager.com", "ip": "203.0.113.7",
            "certificate": "CERT", "privateKey": "SECRET",
            "lanHostname": "lan.a1b2.rex-direct.debridmediamanager.com", "lanIp": "192.168.1.20",
        });
        let parsed = parse(answer.to_string().as_bytes()).expect("the answer parses");
        assert_eq!(parsed.lan_hostname.as_deref(), Some("lan.a1b2.rex-direct.debridmediamanager.com"));
        assert_eq!(parsed.lan_ip.as_deref(), Some("192.168.1.20"));
        answer["lanHostname"] = Value::Null;
        assert_eq!(parse(answer.to_string().as_bytes()).map(|parsed| parsed.lan_hostname).ok(), Some(None));
        answer.as_object_mut().unwrap().remove("lanHostname");
        answer.as_object_mut().unwrap().remove("lanIp");
        let parsed = parse(answer.to_string().as_bytes()).expect("an answer without a LAN name parses");
        assert_eq!((parsed.lan_hostname, parsed.lan_ip), (None, None));
        for bad in [json!("lan.evil.example/x"), json!("evil"), json!(""), json!(7)] {
            answer["lanHostname"] = bad.clone();
            let error = parse(answer.to_string().as_bytes()).err().unwrap_or_else(|| panic!("{bad} is refused"));
            assert_eq!(error, "the answer's lanHostname is not a DNS name");
        }
    }

    #[test]
    fn a_202_waits_its_retry_after_within_15_s_and_10_min() {
        let wait = |body: &str| Later::pending(body.as_bytes());
        assert_eq!(wait(r#"{"retryAfter": 30, "error": "issuing"}"#), Later::Pending(Duration::from_secs(30), Some("issuing".to_string())));
        assert_eq!(wait(r#"{"retryAfter": 1}"#), Later::Pending(Duration::from_secs(15), None));
        assert_eq!(wait(r#"{"retryAfter": 86400}"#), Later::Pending(Duration::from_secs(600), None));
        for body in ["{}", r#"{"retryAfter": -5}"#, r#"{"retryAfter": "30"}"#, r#"{"retryAfter": 2.5}"#, "not json", ""] {
            assert_eq!(wait(body).after(), Duration::from_secs(60), "{body:?}");
        }
        assert_eq!(Later::Failed("HTTP 500".to_string()).after(), RETRY);
    }

    #[test]
    fn the_provider_is_https_or_on_this_machine() {
        for url in ["https://debridmediamanager.com/api/rex-direct/register", "HTTPS://provider.example/x",
                    "http://127.0.0.1:8080/r", "http://127.9.9.9/r", "http://localhost:9/r", "http://[::1]:9/r",
                    "http://[::ffff:127.0.0.1]:9/r"] {
            assert!(trusted(url), "{url}");
        }
        for url in ["http://provider.example/r", "http://10.0.0.1/r", "http://localhost.example/r",
                    "http://127.0.0.1.example/r", "ftp://127.0.0.1/r", "provider.example/r", ""] {
            assert!(!trusted(url), "{url}");
        }
    }

    #[test]
    fn rex_direct_url_is_appended_to_the_announce_list() {
        assert_eq!(announce_list(Some("http://10.0.0.1:32400"), Some("https://a.b.c:32400")).as_deref(),
                   Some("http://10.0.0.1:32400,https://a.b.c:32400"));
        assert_eq!(announce_list(None, Some("https://a.b.c:32400")).as_deref(), Some("https://a.b.c:32400"));
        assert_eq!(announce_list(None, None), None);
    }
}
