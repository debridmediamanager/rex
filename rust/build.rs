// Compiles `data/` into the binary, each file gzipped, so a rex started anywhere answers what a
// rex beside its checkout does. A sponsor's binary run from `rust/target/release` found no
// `data/` and refused every play (2026-10-20, `tests/test_sidecars_embedded.py`).
use std::io::Write;
use std::path::Path;
use std::{env, fs};

fn main() {
    let data = Path::new(env!("CARGO_MANIFEST_DIR")).join("../data");
    println!("cargo:rerun-if-changed={}", data.display());
    let out = Path::new(&env::var("OUT_DIR").unwrap()).to_path_buf();
    let mut names: Vec<String> = fs::read_dir(&data)
        .expect("data/ beside rust/")
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_file()))
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    let mut table = String::from("pub(crate) static SIDECARS: &[(&str, &[u8])] = &[\n");
    for name in &names {
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::best());
        encoder.write_all(&fs::read(data.join(name)).unwrap()).unwrap();
        fs::write(out.join(format!("{name}.gz")), encoder.finish().unwrap()).unwrap();
        table.push_str(&format!(
            "    ({name:?}, include_bytes!(concat!(env!(\"OUT_DIR\"), \"/{name}.gz\"))),\n"
        ));
    }
    table.push_str("];\n");
    fs::write(out.join("sidecars.rs"), table).unwrap();
}
