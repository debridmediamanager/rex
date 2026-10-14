//! `rex catalog`: writes rex's catalog, the server's identity and its libraries (the port of
//! build_catalog.py, whose output it matches byte for byte).
//!
//! The library itself is what rex's sources push (docs/feed-api.md). A pushed item lands in
//! the library holding the directory its path starts with (`movies/<release>/<file>`), so a
//! new server's catalog is a seed: a machineIdentifier, a name, and a movie and a show
//! library naming those directories:
//!
//! ```text
//! rex catalog --seed --name den --out rex.db
//! rex catalog --seed --name den --shows shows --shows anime --out rex.db
//! ```
//!
//! `--mount` (repeatable) is the prefix the folders are shown with. zurg's own Plex matcher
//! compares full paths, so it is zurg's `mount_path` when zurg's `plex_server_url` is rex.
//! An `--out` ending in `.db` (or already a SQLite database) is written by the row
//! (`store::write_catalog`), which the server takes as `--catalog` and keeps its state in
//! too; anything else is JSON. An existing JSON catalog is moved into a database with
//! `rex catalog --convert catalog.json --out rex.db`.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sha1::{Digest, Sha1};

use crate::{hierarchy, platform, store};

const USAGE: &str = "usage: rex catalog [-h] [--out OUT] [--name NAME] [--seed] [--mount MOUNTS]\n                   [--movies MOVIES] [--shows SHOWS] [--convert JSON] [--relink]";

const HELP: &str = "
options:
  -h, --help            show this help message and exit
  --out OUT             the catalog to write: .db (or an existing database) is
                        SQLite, anything else JSON. Default catalog.json at the
                        checkout's root
  --name NAME           friendlyName the clients show
  --seed                write an empty library for a zurg-backed server
  --mount MOUNTS, --zurg-mount MOUNTS
                        with --seed: the prefix the library folders are shown
                        with (zurg's mount_path, for zurg's matcher); repeat for
                        several. Default /mnt/zurg
  --movies MOVIES       with --seed: a directory the movie library holds under
                        each mount; repeat for several. Default movies
  --shows SHOWS         with --seed: a directory the show library holds under
                        each mount; repeat for several. Default shows
  --convert JSON        write an existing JSON catalog into the --out database
                        and stop
  --relink              only relink the guids of the existing --out catalog to
                        Plex's (needs PLEX_TOKEN)
";

fn usage_error(message: &str) -> String {
    format!("{USAGE}\nrex: error: {message}")
}

pub fn main(args: &[String]) -> Result<(), String> {
    let (mut out, mut name, mut convert) = (None, "rex".to_string(), None);
    let (mut seed, mut relink) = (false, false);
    let (mut mounts, mut movies, mut shows) = (Vec::new(), Vec::new(), Vec::new());
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        index += 1;
        if arg == "-h" || arg == "--help" {
            print!("{USAGE}\n{HELP}");
            return Ok(());
        }
        let (flag, inline) = match arg.split_once('=') {
            Some((flag, value)) if arg.starts_with("--") => (flag, Some(value.to_string())),
            _ => (arg.as_str(), None),
        };
        if matches!(flag, "--seed" | "--relink") {
            if inline.is_some() {
                return Err(usage_error(&format!("argument {flag}: ignored explicit argument")));
            }
            *(if flag == "--seed" { &mut seed } else { &mut relink }) = true;
            continue;
        }
        let target = match flag {
            "--out" | "--name" | "--convert" | "--mount" | "--zurg-mount" | "--movies" | "--shows" => flag,
            _ => return Err(usage_error(&format!("unrecognized arguments: {arg}"))),
        };
        let value = match inline {
            Some(value) => value,
            None => match args.get(index) {
                Some(next) if !next.starts_with("--") => {
                    index += 1;
                    next.clone()
                }
                _ => return Err(usage_error(&format!("argument {target}: expected one argument"))),
            },
        };
        match target {
            "--out" => out = Some(value),
            "--name" => name = value,
            "--convert" => convert = Some(value),
            "--movies" => movies.push(value),
            "--shows" => shows.push(value),
            _ => mounts.push(value),
        }
    }
    let out = out.unwrap_or_else(|| checkout_root().join("catalog.json").to_string_lossy().into_owned());

    if let Some(source) = convert {
        let raw = fs::read(&source).map_err(|e| format!("cannot read {source}: {e}"))?;
        let document: Value = serde_json::from_slice(&raw).map_err(|e| format!("cannot parse {source}: {e}"))?;
        let document = document.as_object().ok_or_else(|| format!("{source} is not a JSON object"))?;
        store::write_catalog(Path::new(&out), document, &sha1_hex(&raw))?;
        let count = match document.get("items") {
            Some(Value::Array(items)) => items.len(),
            Some(Value::Object(items)) => items.len(),
            _ => 0,
        };
        println!("wrote {out}: {count} item(s) from {source}");
        return Ok(());
    }

    if relink {
        let mut document = load_catalog(&out)?;
        let machine = document
            .get("machineIdentifier")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("{out} names no machineIdentifier"))?
            .to_string();
        let items = document
            .get_mut("items")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| format!("{out} holds no items"))?;
        let count = relink_guids(items, &out, machine);
        save_catalog(&out, &document)?;
        println!("relinked {count} row(s) in {out}");
        return Ok(());
    }

    if !seed {
        return Err(usage_error("nothing to do: --seed, --convert or --relink"));
    }
    let or = |given: Vec<String>, default: &str| if given.is_empty() { vec![default.to_string()] } else { given };
    let machine = write_seed(&out, &name, &or(mounts, "/mnt/zurg"), &or(movies, "movies"), &or(shows, "shows"))?;
    println!("wrote {out}: an empty library, machine={machine}");
    Ok(())
}

/// `--seed`: an empty library at `out`, keeping the machineIdentifier already there.
///
/// machineIdentifier must be stable across restarts: plex.tv keys the registered device on
/// it, and a client that saw the old one treats a new value as a different server -- so a
/// re-seed keeps the one already there. A new one is random, as a real PMS's is: it was
/// sha1("plexsim/" + name), which plex.tv could predict and which two servers given the
/// same name shared. A placeholder (the example catalog's) is replaced.
fn write_seed(out: &str, name: &str, mounts: &[String], movies: &[String], shows: &[String]) -> Result<String, String> {
    let machine = match existing_machine(out).filter(|machine| !placeholder_machine(machine)) {
        Some(machine) => machine,
        None => random_machine()?,
    };
    let mut sections = hierarchy::sections(name);
    seed_locations(&mut sections, mounts, movies, shows);
    let document = json!({"machineIdentifier": machine, "friendlyName": name, "sections": sections, "items": []});
    save_catalog(out, &document)?;
    Ok(machine)
}

/// `secrets.token_hex(20)`.
fn random_machine() -> Result<String, String> {
    let mut bytes = [0_u8; 20];
    platform::random_bytes(&mut bytes).map_err(|e| format!("no random source: {e}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Each library holds every one of its folders under every mount. zurg's own Plex
/// integration finds a library by its location, and the feed places an item by its
/// `<mount>/<directory>/<release>/<file>`. Location ids are numbered across libraries, so
/// one mount and one folder each keeps the ids the seed always wrote (1 and 2).
fn seed_locations(sections: &mut [Value], mounts: &[String], movies: &[String], shows: &[String]) {
    let mut next_id = 1;
    for (section, folders) in sections.iter_mut().zip([movies, shows]) {
        let mut locations = Vec::new();
        for mount in mounts {
            for folder in folders {
                let path = format!("{}/{}", mount.trim_end_matches('/'), folder.trim_matches('/'));
                locations.push(json!({"id": next_id, "path": path}));
                next_id += 1;
            }
        }
        section["Location"] = Value::Array(locations);
    }
}

/// The example catalog's all-zero id, or the native server's fallback for a catalog without
/// one: every copy shares it, so no server may claim or announce on it.
fn placeholder_machine(machine: &str) -> bool {
    matches!(machine.trim_matches('0'), "" | "1")
}

/// The machineIdentifier of the catalog already at `out`, if any. A re-seed (the manager's
/// `build`) must not change it: clients and plex.tv know the server by it, and a new one is
/// a different server to them. An unreadable file is no catalog.
fn existing_machine(out: &str) -> Option<String> {
    let document = load_catalog(out).ok()?;
    document.get("machineIdentifier")?.as_str().filter(|machine| !machine.is_empty()).map(str::to_string)
}

fn is_database(path: &str) -> bool {
    path.ends_with(".db") || store::is_sqlite(Path::new(path))
}

fn load_catalog(path: &str) -> Result<Value, String> {
    if store::is_sqlite(Path::new(path)) {
        return store::read_catalog(Path::new(path)).map(|(document, _)| document);
    }
    let text = fs::read_to_string(path).map_err(|e| format!("cannot read {path}: {e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("cannot parse {path}: {e}"))
}

/// `catalog.json`, or the catalog half of a database (its state is left alone). The
/// digest of a database written here is that of the JSON this would have written.
fn save_catalog(path: &str, document: &Value) -> Result<(), String> {
    let text = py_dumps(document, Some(2), false, true);
    if is_database(path) {
        let document = document.as_object().ok_or("a catalog is a JSON object")?;
        return store::write_catalog(Path::new(path), document, &sha1_hex(text.as_bytes()));
    }
    fs::write(path, text).map_err(|e| format!("cannot write {path}: {e}"))
}

/// Give the rows Plex's own guids (metadata.PlexProvider); a no-op without PLEX_TOKEN.
fn relink_guids(items: &mut [Value], out: &str, machine: String) -> usize {
    let Some(token) = std::env::var("PLEX_TOKEN").ok().filter(|token| !token.is_empty()) else {
        println!("no PLEX_TOKEN: guids stay minted, and the Android app's detail pages stay empty");
        return 0;
    };
    let directory = std::path::absolute(out)
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));
    crate::runtime::plexmeta::relink(items, token, directory.join("plex_cache.json"), machine)
}

pub(crate) fn sha1_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha1::digest(bytes))
}

/// The checkout (or unpacked release) the binary runs from: `rust/target/release/rex` sits
/// three directories under it, beside `data/`. A binary anywhere else gets the working
/// directory, which is what `oracle/`'s tools resolved to from their own place.
pub(crate) fn checkout_root() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.ancestors().nth(4).map(Path::to_path_buf))
        .filter(|root| root.join("data").is_dir())
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

/// Python's `json.dumps(value, indent=indent, ensure_ascii=ascii)` byte for byte, floats
/// (`repr`) included: separators `,`/`: ` when indented and `, `/`: ` when not, or `,`/`:`
/// with `compact`. What build_catalog.py wrote and hashed is what this writes and hashes.
pub(crate) fn py_dumps(value: &Value, indent: Option<usize>, compact: bool, ascii: bool) -> String {
    fn write(out: &mut String, value: &Value, indent: Option<usize>, separators: (&str, &str), ascii: bool, depth: usize) {
        let newline = |out: &mut String, depth: usize| {
            if let Some(width) = indent {
                out.push('\n');
                out.push_str(&" ".repeat(width * depth));
            }
        };
        match value {
            Value::Array(rows) if !rows.is_empty() => {
                out.push('[');
                for (index, row) in rows.iter().enumerate() {
                    if index > 0 {
                        out.push_str(separators.0);
                    }
                    newline(out, depth + 1);
                    write(out, row, indent, separators, ascii, depth + 1);
                }
                newline(out, depth);
                out.push(']');
            }
            Value::Object(members) if !members.is_empty() => {
                out.push('{');
                for (index, (key, member)) in members.iter().enumerate() {
                    if index > 0 {
                        out.push_str(separators.0);
                    }
                    newline(out, depth + 1);
                    write(out, &Value::String(key.clone()), indent, separators, ascii, depth + 1);
                    out.push_str(separators.1);
                    write(out, member, indent, separators, ascii, depth + 1);
                }
                newline(out, depth);
                out.push('}');
            }
            Value::Number(number) if number.is_f64() => out.push_str(&py_float(number.as_f64().unwrap_or(0.0))),
            Value::String(_) if ascii => {
                // serde_json escapes what Python does below 0x20; ensure_ascii also escapes
                // DEL and everything past it, as UTF-16 units.
                for c in value.to_string().chars() {
                    if c > '~' {
                        let mut units = [0_u16; 2];
                        for unit in c.encode_utf16(&mut units) {
                            out.push_str(&format!("\\u{unit:04x}"));
                        }
                    } else {
                        out.push(c);
                    }
                }
            }
            other => out.push_str(&other.to_string()),
        }
    }
    let separators = match (compact, indent) {
        (true, _) => (",", ":"),
        (false, Some(_)) => (",", ": "),
        (false, None) => (", ", ": "),
    };
    let mut out = String::new();
    write(&mut out, value, indent, separators, ascii, 0);
    out
}

/// `repr(float)`: the shortest digits that round-trip (serde_json's, the same digits), laid
/// out as Python does -- positional from 1e-4 up to 1e16, else `d.ddde+XX`.
fn py_float(value: f64) -> String {
    let text = Value::from(value).to_string();
    let (sign, text) = text.strip_prefix('-').map_or(("", text.as_str()), |rest| ("-", rest));
    let (mantissa, exponent) = text
        .split_once('e')
        .map_or((text, 0), |(mantissa, exponent)| (mantissa, exponent.parse::<i32>().unwrap_or(0)));
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all = format!("{whole}{fraction}");
    let leading = all.len() - all.trim_start_matches('0').len();
    let digits = all.trim_matches('0');
    if digits.is_empty() {
        return format!("{sign}0.0");
    }
    // value = 0.<digits> x 10^point
    let point = whole.len() as i32 + exponent - leading as i32;
    let length = digits.len() as i32;
    if (-3..=16).contains(&point) {
        if point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat((-point) as usize))
        } else if point >= length {
            format!("{sign}{digits}{}.0", "0".repeat((point - length) as usize))
        } else {
            format!("{sign}{}.{}", &digits[..point as usize], &digits[point as usize..])
        }
    } else {
        let rest = &digits[1..];
        let exponent = point - 1;
        format!(
            "{sign}{}{}{rest}e{}{:02}",
            &digits[..1],
            if rest.is_empty() { "" } else { "." },
            if exponent < 0 { '-' } else { '+' },
            exponent.abs()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("rex-catalog-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    fn locations(mounts: &[&str], movies: &[&str], shows: &[&str]) -> Vec<Vec<(i64, String)>> {
        let mut sections = hierarchy::sections("den");
        seed_locations(&mut sections, &strings(mounts), &strings(movies), &strings(shows));
        sections
            .iter()
            .map(|section| {
                section["Location"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|row| (row["id"].as_i64().unwrap(), row["path"].as_str().unwrap().to_string()))
                    .collect()
            })
            .collect()
    }

    #[test]
    fn one_mount_keeps_the_ids_the_seed_always_wrote() {
        assert_eq!(
            locations(&["/mnt/zurg"], &["movies"], &["shows"]),
            vec![vec![(1, "/mnt/zurg/movies".to_string())], vec![(2, "/mnt/zurg/shows".to_string())]]
        );
    }

    #[test]
    fn location_ids_are_numbered_across_libraries() {
        let got = locations(&["/mnt/zurg", "/mnt/zurg_ad/", "/mnt/zurg_tb"], &["movies"], &["/shows/", "anime"]);
        let paths = |rows: &Vec<(i64, String)>| rows.iter().map(|(_, path)| path.clone()).collect::<Vec<_>>();
        assert_eq!(paths(&got[0]), ["/mnt/zurg/movies", "/mnt/zurg_ad/movies", "/mnt/zurg_tb/movies"]);
        assert_eq!(
            paths(&got[1]),
            ["/mnt/zurg/shows", "/mnt/zurg/anime", "/mnt/zurg_ad/shows", "/mnt/zurg_ad/anime", "/mnt/zurg_tb/shows", "/mnt/zurg_tb/anime"]
        );
        let ids = got.iter().flatten().map(|(id, _)| *id).collect::<Vec<_>>();
        assert_eq!(ids, (1..=9).collect::<Vec<_>>());
    }

    #[test]
    fn placeholders_are_the_zero_ids() {
        assert!(placeholder_machine(&"0".repeat(40)));
        assert!(placeholder_machine(&format!("{}1", "0".repeat(39))));
        assert!(placeholder_machine(""));
        assert!(!placeholder_machine(&"f".repeat(40)));
        assert!(!placeholder_machine("f1x7u4e0000000000000000000000000000000f1"));
        assert!(!placeholder_machine(&format!("{}2", "0".repeat(39))));
    }

    #[test]
    fn a_reseed_keeps_the_machine_and_replaces_a_placeholder() {
        let dir = temp("reseed");
        let machine = "a".repeat(40);
        let catalog = json!({"machineIdentifier": machine, "friendlyName": "Café", "sections": [], "items": [{"ratingKey": 1}]});
        let as_json = dir.join("catalog.json").to_string_lossy().into_owned();
        fs::write(&as_json, serde_json::to_string(&catalog).unwrap()).unwrap();
        let as_db = dir.join("rex.db").to_string_lossy().into_owned();
        store::write_catalog(Path::new(&as_db), catalog.as_object().unwrap(), "digest").unwrap();
        let seed = |out: &str| write_seed(out, "den", &strings(&["/mnt/zurg"]), &strings(&["movies"]), &strings(&["shows"])).unwrap();
        assert_eq!(seed(&as_json), machine);
        assert_eq!(seed(&as_db), machine);
        assert_eq!(load_catalog(&as_db).unwrap()["items"], json!([]), "a re-seed empties the library");

        let fresh = seed(&dir.join("new.db").to_string_lossy());
        assert!(fresh.len() == 40 && fresh.bytes().all(|b| b.is_ascii_hexdigit()) && !placeholder_machine(&fresh));
        assert_ne!(seed(&dir.join("other.json").to_string_lossy()), fresh, "every seed mints its own");

        let empty = dir.join("empty.db").to_string_lossy().into_owned();
        fs::write(&empty, b"").unwrap();
        assert_eq!(existing_machine(&empty), None);
        assert_eq!(seed(&empty).len(), 40);
        assert!(store::is_sqlite(Path::new(&empty)));

        let placeholder = dir.join("example.json").to_string_lossy().into_owned();
        fs::write(&placeholder, json!({"machineIdentifier": "0".repeat(40)}).to_string()).unwrap();
        assert!(!placeholder_machine(&seed(&placeholder)));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_database_keeps_the_digest_it_was_given() {
        let dir = temp("digest");
        let path = dir.join("rex.db");
        let document = json!({"machineIdentifier": "m", "items": [{"a": 1}, {"b": "é"}], "prefs": {"x": 1.5}});
        store::write_catalog(&path, document.as_object().unwrap(), "abc").unwrap();
        let (read, digest) = store::read_catalog(&path).unwrap();
        assert_eq!((read, digest.as_str()), (document, "abc"));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn dumps_is_pythons_json_dumps() {
        let value: Value = serde_json::from_str(
            r#"{"a": [1, 2.5, {"b": []}, {}], "é": "Ümlaut \u007f 😀", "f": [1e16, 1.5e-7, 0.0001, 0.00001, 100.0, -0.0, 123456.789, 1e22]}"#,
        )
        .unwrap();
        assert_eq!(
            py_dumps(&value, Some(2), false, true),
            "{\n  \"a\": [\n    1,\n    2.5,\n    {\n      \"b\": []\n    },\n    {}\n  ],\n  \"\\u00e9\": \"\\u00dcmlaut \\u007f \\ud83d\\ude00\",\n  \"f\": [\n    1e+16,\n    1.5e-07,\n    0.0001,\n    1e-05,\n    100.0,\n    -0.0,\n    123456.789,\n    1e+22\n  ]\n}"
        );
        assert_eq!(py_dumps(&json!({"a": [1, "é"]}), None, true, false), "{\"a\":[1,\"é\"]}");
        assert_eq!(py_dumps(&json!({"a": [1, null]}), None, false, true), "{\"a\": [1, null]}");
        assert_eq!(py_dumps(&json!({"a": 1}), Some(1), false, true), "{\n \"a\": 1\n}");
    }
}
