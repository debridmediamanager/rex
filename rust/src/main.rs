use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;

use rex::{catalog, decision, filters, gate, hierarchy, mediainfo, metadata, runtime, xmlout};
use serde_json::json;

fn usage() -> &'static str {
    "Usage: rex [server options] | rex <decision|mediainfo|normalize-media|hierarchy-build|hierarchy-normalize|hierarchy-fold|hierarchy-case|catalog-normalize|xml|system-xml|ordered|guid|provider-guids|gate|metadata-parse|metadata-enrich|filter-compare|filter-parse|filter-evaluate|filter-describe|filter-document|filter-orders|filter-source-order> ..."
}

/// `main()`'s `argparse` options, in its declaration order: (flag, metavar, takes a value).
const SERVER_OPTIONS: &[(&str, &str, bool)] = &[
    ("--catalog", "CATALOG", true),
    ("--port", "PORT", true),
    ("--host", "HOST", true),
    ("--part-mode", "{proxy}", true),
    ("--public-url", "PUBLIC_URL", true),
    ("--token", "TOKEN", true),
    ("--verbose", "", false),
    ("--assert-claimed", "", false),
    ("--web-root", "WEB_ROOT", true),
    ("--owner-email", "OWNER_EMAIL", true),
    ("--owner-username", "OWNER_USERNAME", true),
    ("--announce-url", "ANNOUNCE_URL", true),
    ("--device-id", "DEVICE_ID", true),
    ("--tls-cert", "TLS_CERT", true),
    ("--tls-key", "TLS_KEY", true),
    ("--gdm", "{auto,on,off}", true),
    ("--plex-direct-host", "PLEX_DIRECT_HOST", true),
    ("--heartbeat-interval", "HEARTBEAT_INTERVAL", true),
    ("--account-state-interval", "ACCOUNT_STATE_INTERVAL", true),
    ("--state", "STATE", true),
    ("--no-state", "", false),
    ("--zurg-mount", "ZURG_MOUNT", true),
    ("--sources", "SOURCES", true),
];

/// `parser.print_usage()`: argparse's wrapped usage block, with this binary's name.
fn server_usage() -> String {
    let indent = " ".repeat("usage: rex ".len());
    let mut lines = vec![String::from("usage: rex")];
    let mut parts = vec!["[-h]".to_string()];
    parts.extend(SERVER_OPTIONS.iter().map(|(flag, metavar, takes)| {
        if *takes {
            format!("[{flag} {metavar}]")
        } else {
            format!("[{flag}]")
        }
    }));
    for part in parts {
        let last = lines.last_mut().expect("usage has a first line");
        if last.len() + 1 + part.len() > 78 && last.trim() != "usage: rex" {
            lines.push(format!("{indent}{part}"));
        } else {
            last.push(' ');
            last.push_str(&part);
        }
    }
    lines.join("\n")
}

/// `parser.print_help()`: the usage, the description, and one line per option with
/// the oracle's own help text.
fn server_help() -> String {
    let helps: HashMap<&str, &str> = [
        ("--part-mode", "proxy streams a part's bytes from its source through rex (the only mode: redirect read zurg's signed links, which a source that pushes its items does not hand over)"),
        ("--token", "require this X-Plex-Token on every request except /identity and /web/* -- which is what a real PMS does. Default OFF, because the test suites and probes talk to rex without one; a real server always has a token, so leaving it off is a deliberate deviation."),
        ("--assert-claimed", "report claimed=1 without performing a real plex.tv claim exchange"),
        ("--web-root", "directory of the extracted Plex Web client, served at /web"),
        ("--owner-email", "owner email, which is what a real PMS reports as myPlexUsername"),
        ("--owner-username", "the Plex account that OWNS this server; shared users are not owners"),
        ("--announce-url", "publicly reachable https URL(s), comma-separated, to register with plex.tv, and keep registered"),
        ("--device-id", "machineIdentifier to announce on; defaults to the catalog's own"),
        ("--tls-cert", "PEM certificate chain to serve TLS with, on the same port as plain HTTP"),
        ("--tls-key", "PEM private key for --tls-cert (defaults to the certificate file itself)"),
        ("--gdm", "answer Plex's LAN discovery protocol on udp/32410 and udp/32414"),
        ("--plex-direct-host", "the <hash>.plex.direct hostname to advertise in GDM's Host: line; omitted entirely when unknown, which is what a PMS with no certificate does"),
        ("--account-state-interval", "seconds between claimed-account watch/rating imports"),
        ("--state", "file to keep watch state, edits, lists, history, preferences, uploads and statistics in across restarts. Default: rex.db beside the catalog, or the catalog itself when it is a database (state.pickle for the Python server), or $REX_STATE"),
        ("--no-state", "keep nothing across a restart: a restart reloads the catalog as it is on disk, which the test suites rely on. Also an empty $REX_STATE"),
        ("--zurg-mount", "zurg's mount_path, the mount of the source $ZURG_URL defines (bytes at $ZURG_URL/dav, with $ZURG_USER/$ZURG_PASSWORD; feed token $REX_FEED_TOKEN). Default /mnt/zurg"),
        ("--sources", "JSON file of the sources that push items and serve their bytes (docs/feed-api.md). Default $REX_SOURCES"),
    ]
    .into_iter()
    .collect();
    let wrap = |text: &str| -> Vec<String> {
        let mut lines = vec![String::new()];
        for word in text.split(' ') {
            let last = lines.last_mut().expect("wrap has a line");
            if !last.is_empty() && last.len() + 1 + word.len() > 54 {
                lines.push(word.to_string());
            } else {
                if !last.is_empty() {
                    last.push(' ');
                }
                last.push_str(word);
            }
        }
        lines
    };
    let mut out = format!(
        "{}\n\nrex -- a PMS that owns no files\n\noptions:\n  -h, --help            show this help message and exit\n",
        server_usage()
    );
    for (flag, metavar, takes) in SERVER_OPTIONS {
        let head = if *takes { format!("  {flag} {metavar}") } else { format!("  {flag}") };
        match helps.get(flag) {
            None => out.push_str(&format!("{head}\n")),
            Some(help) => {
                let body = wrap(help);
                let mut rest = body.iter();
                if head.len() <= 22 {
                    out.push_str(&format!("{head:<24}{}\n", rest.next().expect("help has a line")));
                } else {
                    out.push_str(&format!("{head}\n"));
                }
                for line in rest {
                    out.push_str(&format!("{}{line}\n", " ".repeat(24)));
                }
            }
        }
    }
    out.push_str(&format!("\n{}\n", usage()));
    out
}

/// What the server command line asked for: to serve, or argparse's `-h`/`--help`.
#[derive(Debug)]
enum ServerArgs {
    Serve(Box<runtime::RuntimeOptions>),
    Help,
}

/// `argparse.ArgumentParser.parse_args()` for `main()`'s options: `--flag value` and
/// `--flag=value`, unique prefixes of a long flag (`allow_abbrev`), `-h`/`--help`
/// anywhere acting when it is reached, a value refused when it looks like a flag,
/// and the options it does not know reported together once everything else parsed.
fn parse_server_args(args: &[String]) -> Result<ServerArgs, String> {
    let mut options = runtime::RuntimeOptions::default();
    let mut unknown: Vec<String> = Vec::new();
    let negative_number = |text: &str| {
        text.strip_prefix('-')
            .is_some_and(|rest| !rest.is_empty() && rest.parse::<f64>().is_ok())
    };
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        index += 1;
        if arg == "-h" || arg == "--help" {
            return Ok(ServerArgs::Help);
        }
        if !arg.starts_with('-') || arg == "-" || negative_number(arg) {
            unknown.push(arg.to_string());
            continue;
        }
        let (flag_text, inline) = match arg.split_once('=') {
            Some((flag, value)) if arg.starts_with("--") => (flag, Some(value.to_string())),
            _ => (arg, None),
        };
        let mut matches: Vec<&(&str, &str, bool)> = SERVER_OPTIONS
            .iter()
            .filter(|(flag, _, _)| *flag == flag_text)
            .collect();
        if matches.is_empty() && flag_text.starts_with("--") && flag_text.len() > 2 {
            matches = SERVER_OPTIONS
                .iter()
                .filter(|(flag, _, _)| flag.starts_with(flag_text))
                .collect();
            if "--help".starts_with(flag_text) {
                if matches.is_empty() {
                    return Ok(ServerArgs::Help);
                }
                return Err(format!(
                    "ambiguous option: {flag_text} could match --help, {}",
                    matches.iter().map(|(flag, _, _)| *flag).collect::<Vec<_>>().join(", ")
                ));
            }
            if matches.len() > 1 {
                return Err(format!(
                    "ambiguous option: {flag_text} could match {}",
                    matches.iter().map(|(flag, _, _)| *flag).collect::<Vec<_>>().join(", ")
                ));
            }
        }
        let Some(&&(flag, _, takes)) = matches.first() else {
            unknown.push(arg.to_string());
            continue;
        };
        let value = if !takes {
            if inline.is_some() {
                return Err(format!("argument {flag}: ignored explicit argument '{}'", inline.unwrap_or_default()));
            }
            String::new()
        } else if let Some(value) = inline {
            value
        } else {
            match args.get(index) {
                Some(next)
                    if !(next.starts_with('-') && next.len() > 1 && !negative_number(next)) =>
                {
                    index += 1;
                    next.clone()
                }
                _ => return Err(format!("argument {flag}: expected one argument")),
            }
        };
        let int = |value: &str| -> Result<i64, String> {
            value
                .trim()
                .replace('_', "")
                .parse::<i64>()
                .map_err(|_| format!("argument {flag}: invalid int value: '{value}'"))
        };
        match flag {
            "--catalog" => options.catalog = PathBuf::from(value),
            "--port" => {
                options.port = u16::try_from(int(&value)?)
                    .map_err(|_| format!("argument {flag}: port must be 0-65535"))?
            }
            "--host" => options.host = value,
            "--part-mode" => options.part_mode = value,
            "--public-url" => options.public_url = Some(value),
            "--token" => options.token = Some(value),
            "--verbose" => options.verbose = true,
            "--assert-claimed" => options.assert_claimed = true,
            "--web-root" => options.web_root = Some(PathBuf::from(value)),
            "--owner-email" => options.owner_email = value,
            "--owner-username" => options.owner_username = value,
            "--announce-url" => options.announce_url = Some(value),
            "--device-id" => options.device_id = Some(value),
            "--tls-cert" => options.tls_cert = Some(PathBuf::from(value)),
            "--tls-key" => options.tls_key = Some(PathBuf::from(value)),
            "--gdm" => options.gdm = value,
            "--plex-direct-host" => options.plex_direct_host = Some(value),
            "--heartbeat-interval" => options.heartbeat_interval = int(&value)?.try_into().map_err(|_| format!("argument {flag}: invalid int value: '{value}'"))?,
            "--account-state-interval" => options.account_state_interval = int(&value)?.try_into().map_err(|_| format!("argument {flag}: invalid int value: '{value}'"))?,
            "--state" => options.state = Some(PathBuf::from(value)),
            "--no-state" => options.state = Some(PathBuf::new()),
            "--zurg-mount" => options.zurg_mount = value,
            "--sources" => options.sources = Some(PathBuf::from(value)),
            _ => unreachable!("every SERVER_OPTIONS flag is matched"),
        }
        // argparse checks `choices` as it consumes the option, before any later `-h`.
        for (name, current, choices) in [
            ("--part-mode", &options.part_mode, &["proxy"][..]),
            ("--gdm", &options.gdm, &["auto", "on", "off"][..]),
        ] {
            if name == flag && !choices.contains(&current.as_str()) {
                return Err(format!(
                    "argument {name}: invalid choice: '{current}' (choose from {})",
                    choices
                        .iter()
                        .map(|choice| format!("'{choice}'"))
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
        }
    }
    if !unknown.is_empty() {
        return Err(format!("unrecognized arguments: {}", unknown.join(" ")));
    }
    if options.tls_key.is_some() && options.tls_cert.is_none() {
        return Err("--tls-key needs --tls-cert".to_string());
    }
    // `--web-root` defaults to the `webclient` folder beside the program, and a root
    // that is not a directory serves no client at all: `web_root=... if isdir else None`.
    let root = options.web_root.take().or_else(default_web_root);
    options.web_root = root.filter(|path| path.is_dir());
    Ok(ServerArgs::Serve(Box::new(options)))
}

/// server.py's default is `webclient` beside itself. The native binary lives three
/// levels under the checkout (`rust/target/release/rex`), so the same folder is
/// `./webclient` when started from the checkout, or that one found from the binary.
fn default_web_root() -> Option<PathBuf> {
    let local = PathBuf::from("webclient");
    if local.is_dir() {
        return Some(local);
    }
    let exe = env::current_exe().ok()?;
    let checkout = exe.parent()?.parent()?.parent()?.parent()?;
    Some(checkout.join("webclient")).filter(|path| path.is_dir())
}

fn run_server(args: &[String]) -> Result<(), String> {
    // argparse's `parser.error()`: the usage block, then `prog: error: message`, exit 2.
    let failed = |error: String| format!("{}\nrex: error: {error}", server_usage());
    match parse_server_args(args).map_err(failed)? {
        ServerArgs::Help => {
            print!("{}", server_help());
            Ok(())
        }
        ServerArgs::Serve(options) => runtime::serve(*options),
    }
}

#[derive(Debug, PartialEq, Eq)]
struct DecisionOptions {
    profile: String,
    media_path: String,
    profiles_path: String,
    protocol: String,
    direct: bool,
}

fn option_value(args: &[String], index: &mut usize, name: &str) -> Result<String, String> {
    let value = args
        .get(*index + 1)
        .ok_or_else(|| format!("{name} needs a value"))?;
    if value.starts_with('-') {
        return Err(format!("{name} needs a value"));
    }
    *index += 2;
    Ok(value.clone())
}

fn parse_decision_args(args: &[String]) -> Result<DecisionOptions, String> {
    let mut profile = None;
    let mut media_path = None;
    let mut profiles_path = None;
    let mut protocol = None;
    let mut direct = true;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--profile" => {
                if profile.is_some() {
                    return Err("option --profile was supplied more than once".to_string());
                }
                profile = Some(option_value(args, &mut index, "--profile")?);
            }
            "--media" => {
                if media_path.is_some() {
                    return Err("option --media was supplied more than once".to_string());
                }
                media_path = Some(option_value(args, &mut index, "--media")?);
            }
            "--profiles" => {
                if profiles_path.is_some() {
                    return Err("option --profiles was supplied more than once".to_string());
                }
                profiles_path = Some(option_value(args, &mut index, "--profiles")?);
            }
            "--protocol" => {
                if protocol.is_some() {
                    return Err("option --protocol was supplied more than once".to_string());
                }
                protocol = Some(option_value(args, &mut index, "--protocol")?);
            }
            "--no-direct" => {
                if !direct {
                    return Err("option --no-direct was supplied more than once".to_string());
                }
                direct = false;
                index += 1;
            }
            option if option.starts_with('-') => {
                return Err(format!("unknown option {option}"));
            }
            value => return Err(format!("unexpected argument {value}")),
        }
    }

    Ok(DecisionOptions {
        profile: profile.ok_or_else(|| "--profile is required".to_string())?,
        media_path: media_path.ok_or_else(|| "--media is required".to_string())?,
        profiles_path: profiles_path.unwrap_or_else(|| "client_profiles.json".to_string()),
        protocol: protocol.unwrap_or_else(|| "http".to_string()),
        direct,
    })
}

fn run_decision(args: &[String]) -> Result<(), String> {
    let options = parse_decision_args(args)?;

    let profiles_text = fs::read_to_string(&options.profiles_path)
        .map_err(|error| format!("cannot read profiles {}: {error}", options.profiles_path))?;
    let profiles = decision::parse_profiles(&profiles_text)
        .map_err(|error| format!("cannot parse profiles {}: {error}", options.profiles_path))?;
    let media_text = fs::read_to_string(&options.media_path)
        .map_err(|error| format!("cannot read media {}: {error}", options.media_path))?;
    let media: serde_json::Value = serde_json::from_str(&media_text)
        .map_err(|error| format!("cannot parse media {}: {error}", options.media_path))?;
    let answer = decision::decide(
        &profiles,
        &options.profile,
        &media,
        &options.protocol,
        options.direct,
    );
    let output = json!({
        "outcome": answer.outcome.as_str(),
        "text": answer.text,
    });
    println!(
        "{}",
        serde_json::to_string(&output).expect("decision JSON is serializable")
    );
    Ok(())
}

fn parse_core_args(args: &[String], allowed: &[&str]) -> Result<HashMap<String, String>, String> {
    let mut values = HashMap::new();
    let mut index = 0;
    while index < args.len() {
        let option = &args[index];
        if !allowed.contains(&option.as_str()) {
            if option.starts_with('-') {
                return Err(format!("unknown option {option}"));
            }
            return Err(format!("unexpected argument {option}"));
        }
        if values.contains_key(option) {
            return Err(format!("option {option} was supplied more than once"));
        }
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("{option} needs a value"))?;
        // Values such as negative filter sentinels (`rating=-1`) are valid;
        // only another long option can begin the next option pair.
        if value.starts_with("--") {
            return Err(format!("{option} needs a value"));
        }
        values.insert(option.clone(), value.clone());
        index += 2;
    }
    Ok(values)
}

fn required<'a>(values: &'a HashMap<String, String>, name: &str) -> Result<&'a str, String> {
    values
        .get(name)
        .map(String::as_str)
        .ok_or_else(|| format!("{name} is required"))
}

fn optional<'a>(values: &'a HashMap<String, String>, name: &str) -> Option<&'a str> {
    values.get(name).map(String::as_str)
}

fn read_json(path: &str) -> Result<serde_json::Value, String> {
    let text = if path == "-" {
        std::io::read_to_string(std::io::stdin()).map_err(|e| e.to_string())?
    } else {
        fs::read_to_string(path).map_err(|e| format!("cannot read {path}: {e}"))?
    };
    serde_json::from_str(&text).map_err(|e| format!("cannot parse {path}: {e}"))
}

fn print_json(value: &serde_json::Value) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string(value).map_err(|e| e.to_string())?
    );
    Ok(())
}

fn run_core(command: &str, args: &[String]) -> Result<(), String> {
    match command {
        "mediainfo" => {
            let options = parse_core_args(
                args,
                &[
                    "--input",
                    "--media-id",
                    "--part-id",
                    "--part-key",
                    "--file",
                    "--size",
                    "--container-hint",
                ],
            )?;
            let input = required(&options, "--input")?;
            let probe = read_json(input)?;
            let media_id = optional(&options, "--media-id")
                .and_then(|v| v.parse().ok())
                .unwrap_or(1);
            let part_id = optional(&options, "--part-id")
                .and_then(|v| v.parse().ok())
                .unwrap_or(1);
            let key = optional(&options, "--part-key").unwrap_or("/library/parts/1/1/file.mkv");
            let file = optional(&options, "--file").unwrap_or("file.mkv");
            let size = optional(&options, "--size")
                .and_then(|v| v.parse().ok())
                .unwrap_or_else(|| {
                    probe
                        .get("format")
                        .and_then(|x| x.get("size"))
                        .and_then(|x| x.as_str())
                        .and_then(|x| x.parse().ok())
                        .unwrap_or(0)
                });
            let hint = optional(&options, "--container-hint");
            print_json(&mediainfo::build_media(
                &probe, media_id, part_id, key, file, size, hint,
            ))
        }
        "normalize-media" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let mut value = read_json(input)?;
            mediainfo::normalize_media(&mut value);
            print_json(&value)
        }
        "hierarchy-build" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let value = read_json(input)?;
            let leaves = value
                .as_array()
                .ok_or_else(|| "hierarchy-build input must be an array".to_string())?
                .clone();
            print_json(&serde_json::Value::Array(hierarchy::build(leaves)))
        }
        "hierarchy-normalize" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let value = read_json(input)?;
            let mut rows = value
                .as_array()
                .ok_or_else(|| "hierarchy-normalize input must be an array".to_string())?
                .clone();
            hierarchy::normalize_rows(&mut rows);
            print_json(&serde_json::Value::Array(rows))
        }
        "hierarchy-fold" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let texts = input
                .as_array()
                .ok_or_else(|| "hierarchy-fold input must be an array".to_string())?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .map(hierarchy::fold_title)
                        .ok_or_else(|| "hierarchy-fold values must be strings".to_string())
                })
                .collect::<Result<Vec<_>, String>>()?;
            print_json(&serde_json::Value::Array(
                texts.into_iter().map(serde_json::Value::String).collect(),
            ))
        }
        "hierarchy-case" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let texts = input
                .as_array()
                .ok_or_else(|| "hierarchy-case values must be an array".to_string())?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .map(hierarchy::fold_case)
                        .ok_or_else(|| "hierarchy-case values must be strings".to_string())
                })
                .collect::<Result<Vec<_>, String>>()?;
            print_json(&serde_json::Value::Array(
                texts.into_iter().map(serde_json::Value::String).collect(),
            ))
        }
        "catalog-normalize" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let value = read_json(input)?;
            let mut catalog = catalog::Catalog::from_value(value)?;
            catalog.normalize();
            print_json(&catalog.to_value())
        }
        "xml" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let value = read_json(input)?;
            let xml = xmlout::to_xml(&value, None);
            print!("{}", String::from_utf8(xml).map_err(|e| e.to_string())?);
            Ok(())
        }
        "system-xml" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            let value = read_json(input)?;
            let xml = xmlout::to_system_xml(&value, None);
            print!("{}", String::from_utf8(xml).map_err(|e| e.to_string())?);
            Ok(())
        }
        "ordered" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = required(&options, "--input")?;
            print_json(&xmlout::ordered(&read_json(input)?, None))
        }
        "guid" => {
            let options = parse_core_args(args, &["--kind", "--seed"])?;
            let kind = required(&options, "--kind")?;
            let seed = required(&options, "--seed")?;
            print_json(&serde_json::json!({"guid":metadata::plex_guid(kind,seed)}))
        }
        "provider-guids" => {
            let options = parse_core_args(args, &["--items", "--recorded"])?;
            let items_path = required(&options, "--items")?;
            let recorded_path = required(&options, "--recorded")?;
            let mut items = read_json(items_path)?
                .as_array()
                .cloned()
                .ok_or_else(|| "items input must be an array".to_string())?;
            let recorded = read_json(recorded_path)?;
            let count = metadata::apply_plex_guids_recorded(&mut items, &recorded);
            print_json(&serde_json::json!({"count":count,"items":items}))
        }
        "gate" => {
            let options = parse_core_args(
                args,
                &[
                    "--input",
                    "--max-height",
                    "--max-bitrate",
                    "--allow-hevc",
                    "--supports-1080p60",
                ],
            )?;
            let input = required(&options, "--input")?;
            let media = read_json(input)?;
            let max_height = required(&options, "--max-height")?
                .parse()
                .map_err(|_| "bad --max-height".to_string())?;
            let max_bitrate = required(&options, "--max-bitrate")?
                .parse()
                .map_err(|_| "bad --max-bitrate".to_string())?;
            let allow_hevc = optional(&options, "--allow-hevc")
                .map(|v| matches!(v, "1" | "true"))
                .unwrap_or(false);
            let p60 = optional(&options, "--supports-1080p60")
                .map(|v| matches!(v, "1" | "true"))
                .unwrap_or(false);
            print_json(&gate::verdict_value(
                &media,
                max_height,
                max_bitrate,
                allow_hevc,
                p60,
            ))
        }
        "metadata-parse" => {
            let options = parse_core_args(args, &["--filename"])?;
            let filename = required(&options, "--filename")?;
            print_json(&metadata::parse_release(filename))
        }
        "metadata-enrich" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "metadata-enrich input must be an object".to_string())?;
            let mut row = object
                .get("row")
                .and_then(serde_json::Value::as_object)
                .cloned()
                .ok_or_else(|| "metadata-enrich row must be an object".to_string())?;
            let details = object
                .get("details")
                .ok_or_else(|| "metadata-enrich details is required".to_string())?;
            let parsed = object
                .get("parsed")
                .and_then(serde_json::Value::as_object)
                .ok_or_else(|| "metadata-enrich parsed must be an object".to_string())?;
            let episode = object.get("episode").filter(|value| !value.is_null());
            let season = object.get("season").filter(|value| !value.is_null());
            let kind = object
                .get("kind")
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| "metadata-enrich kind is required".to_string())?;
            let tmdb_id = object
                .get("tmdbId")
                .and_then(serde_json::Value::as_i64)
                .ok_or_else(|| "metadata-enrich tmdbId is required".to_string())?;
            let ok = metadata::enrich(&mut row, details, parsed, episode, season, kind, tmdb_id);
            print_json(&json!({"ok": ok, "row": row}))
        }
        "filter-compare" => {
            let options = parse_core_args(
                args,
                &["--kind", "--operator", "--field", "--actual", "--wanted"],
            )?;
            let kind = required(&options, "--kind")?;
            let operator = required(&options, "--operator")?;
            let field = required(&options, "--field")?;
            let actual = read_json(required(&options, "--actual")?)?;
            let wanted = required(&options, "--wanted")?;
            let matched = filters::compare(kind, operator, &actual, wanted, field)
                .map_err(|error| format!("filter {}: {}", error.status, error.detail))?;
            print_json(&json!({"match": matched}))
        }
        "filter-parse" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "filter-parse input must be an object".to_string())?;
            let pairs = object
                .get("pairs")
                .and_then(serde_json::Value::as_array)
                .ok_or_else(|| "filter-parse pairs must be an array".to_string())?
                .iter()
                .map(|pair| {
                    let pair = pair
                        .as_array()
                        .ok_or_else(|| "filter-parse pair must be an array".to_string())?;
                    if pair.len() != 2 {
                        return Err("filter-parse pair must have two values".to_string());
                    }
                    Ok((
                        pair[0]
                            .as_str()
                            .ok_or_else(|| "filter-parse key must be a string".to_string())?
                            .to_string(),
                        pair[1]
                            .as_str()
                            .ok_or_else(|| "filter-parse value must be a string".to_string())?
                            .to_string(),
                    ))
                })
                .collect::<Result<Vec<_>, String>>()?;
            let field_types = object
                .get("fieldTypes")
                .and_then(serde_json::Value::as_object)
                .ok_or_else(|| "filter-parse fieldTypes must be an object".to_string())?
                .iter()
                .map(|(key, value)| {
                    value
                        .as_str()
                        .map(|value| (key.clone(), value.to_string()))
                        .ok_or_else(|| "filter-parse field type must be a string".to_string())
                })
                .collect::<Result<HashMap<_, _>, String>>()?;
            let mut errors = Vec::new();
            let tree = filters::parse(&pairs, &field_types, &mut errors);
            print_json(&json!({"tree": tree, "errors": errors}))
        }
        "filter-evaluate" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "filter-evaluate input must be an object".to_string())?;
            let tree = object
                .get("tree")
                .ok_or_else(|| "filter-evaluate tree is required".to_string())?;
            let row = object
                .get("row")
                .ok_or_else(|| "filter-evaluate row is required".to_string())?;
            let value_of = |field: &str, row: &serde_json::Value| {
                row.as_object()
                    .and_then(|row| row.get(field))
                    .cloned()
                    .unwrap_or(serde_json::Value::Null)
            };
            let resolve = |scope: &str, row: &serde_json::Value| {
                row.as_object()
                    .and_then(|row| row.get(scope))
                    .and_then(serde_json::Value::as_array)
                    .cloned()
                    .unwrap_or_default()
            };
            let use_resolver = object.get("resolve").is_some();
            let matched = if use_resolver {
                filters::evaluate_with(tree, row, &value_of, Some(&resolve))
            } else {
                filters::evaluate(tree, row, &value_of)
            }
            .map_err(|error| format!("filter {}: {}", error.status, error.detail))?;
            print_json(&json!({"match": matched}))
        }
        "filter-describe" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "filter-describe input must be an object".to_string())?;
            let tree = object
                .get("tree")
                .ok_or_else(|| "filter-describe tree is required".to_string())?;
            let titles = object.get("titles").and_then(serde_json::Value::as_object);
            let value_titles = object
                .get("valueTitles")
                .and_then(serde_json::Value::as_object);
            let title_of = |field: &str, scope: &str| {
                titles
                    .and_then(|titles| {
                        titles
                            .get(&format!("{}.{}", scope, field))
                            .or_else(|| titles.get(field))
                    })
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("")
                    .to_string()
            };
            let value_title_of = |field: &str, value: &str| {
                value_titles
                    .and_then(|fields| fields.get(field))
                    .and_then(serde_json::Value::as_object)
                    .and_then(|values| values.get(value))
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
            };
            print_json(
                &filters::describe_with(tree, &title_of, &value_title_of)
                    .unwrap_or(serde_json::Value::Null),
            )
        }
        "filter-document" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "filter-document input must be an object".to_string())?;
            let tree = object.get("tree").unwrap_or(&serde_json::Value::Null);
            let type_number = object
                .get("type")
                .and_then(serde_json::Value::as_i64)
                .unwrap_or(1);
            let order = object
                .get("order")
                .and_then(serde_json::Value::as_array)
                .cloned();
            let limit = object.get("limit").and_then(serde_json::Value::as_i64);
            let group = object.get("group").cloned();
            let errors = object
                .get("errors")
                .and_then(serde_json::Value::as_array)
                .cloned()
                .unwrap_or_default();
            let titles = object.get("titles").and_then(serde_json::Value::as_object);
            let value_titles = object
                .get("valueTitles")
                .and_then(serde_json::Value::as_object);
            let title_of = |field: &str, scope: &str| {
                titles
                    .and_then(|titles| {
                        titles
                            .get(&format!("{}.{}", scope, field))
                            .or_else(|| titles.get(field))
                    })
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("")
                    .to_string()
            };
            let value_title_of = |field: &str, value: &str| {
                value_titles
                    .and_then(|fields| fields.get(field))
                    .and_then(serde_json::Value::as_object)
                    .and_then(|values| values.get(value))
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
            };
            let descriptor = filters::Descriptor {
                title_of: &title_of,
                value_title_of: &value_title_of,
            };
            print_json(&filters::document_with(
                type_number,
                tree,
                order,
                limit,
                group,
                &errors,
                &descriptor,
            ))
        }
        "filter-orders" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let object = input
                .as_object()
                .ok_or_else(|| "filter-orders input must be an object".to_string())?;
            let spec = object
                .get("spec")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            let supported = object
                .get("supported")
                .and_then(serde_json::Value::as_array)
                .cloned()
                .unwrap_or_default();
            let order = filters::orders(spec, &|field| {
                supported.iter().any(|value| value.as_str() == Some(field))
            })
            .map_err(|error| format!("filter {}: {}", error.status, error.detail))?;
            print_json(&serde_json::Value::Array(order))
        }
        "filter-source-order" => {
            let options = parse_core_args(args, &["--input"])?;
            let input = read_json(required(&options, "--input")?)?;
            let value = input
                .as_object()
                .and_then(|object| object.get("value"))
                .and_then(serde_json::Value::as_str);
            let order = filters::source_order(value)
                .map_err(|error| format!("filter {}: {}", error.status, error.detail))?;
            print_json(&order.map_or(serde_json::Value::Null, serde_json::Value::Array))
        }
        _ => Err(format!("unknown command {command}\n{}", usage())),
    }
}

fn main() -> ExitCode {
    let mut args = env::args().skip(1).collect::<Vec<_>>();
    // `python3 server.py` with no arguments serves on the defaults, and `--help`
    // anywhere among the server options prints argparse's help.
    if args.is_empty() {
        args.push("serve".to_string());
    }
    let command = args.remove(0);
    let result = if command == "decision" {
        run_decision(&args)
    } else if command == "serve" {
        run_server(&args)
    } else if command.starts_with('-') {
        let mut server_args = vec![command];
        server_args.extend(args);
        run_server(&server_args)
    } else {
        run_core(&command, &args)
    };
    if let Err(error) = result {
        if error.contains("\nrex: error: ") {
            eprintln!("{error}");
        } else {
            eprintln!("rex: {error}");
        }
        ExitCode::from(2)
    } else {
        ExitCode::SUCCESS
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn parses_options_sequentially() {
        let parsed = parse_decision_args(&args(&[
            "--profile",
            "Web",
            "--media",
            "media file.json",
            "--profiles",
            "profiles.json",
            "--protocol",
            "https",
            "--no-direct",
        ]))
        .expect("valid options parse");
        assert_eq!(
            parsed,
            DecisionOptions {
                profile: "Web".to_string(),
                media_path: "media file.json".to_string(),
                profiles_path: "profiles.json".to_string(),
                protocol: "https".to_string(),
                direct: false,
            }
        );
    }

    #[test]
    fn rejects_unconsumed_and_malformed_options() {
        let cases = [
            (
                &["--profile", "Web", "--media", "media.json", "stray"][..],
                "unexpected argument stray",
            ),
            (
                &["--profile", "Web", "--media"][..],
                "--media needs a value",
            ),
            (
                &["--profile", "Web", "--media", "media.json", "--bogus"][..],
                "unknown option --bogus",
            ),
            (
                &[
                    "--profile",
                    "Web",
                    "--media",
                    "media.json",
                    "--profile",
                    "Other",
                ][..],
                "option --profile was supplied more than once",
            ),
            (
                &[
                    "--profile",
                    "Web",
                    "--media",
                    "media.json",
                    "--no-direct",
                    "--no-direct",
                ][..],
                "option --no-direct was supplied more than once",
            ),
        ];
        for (values, expected) in cases {
            assert_eq!(
                parse_decision_args(&args(values)),
                Err(expected.to_string())
            );
        }
    }
}
