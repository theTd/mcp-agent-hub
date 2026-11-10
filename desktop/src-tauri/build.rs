fn main() {
    if std::env::var_os("CARGO_FEATURE_PORTABLE").is_some() {
        embed_portable_resources();
    }
    // App commands get an ACL manifest, so only windows/origins granted in capabilities/ may call
    // them: the bundled splash page, never the hub console loaded from http://127.0.0.1.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["splash_state", "retry_hub"])),
    )
    .expect("failed to run tauri-build");
}

fn embed_portable_resources() {
    use sha2::{Digest, Sha256};
    use std::{collections::BTreeMap, fs, path::Path};
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let manifest = repo.join("desktop/portable/payload.txt");
    println!("cargo:rerun-if-changed={}", manifest.display());
    let mut files = BTreeMap::new();
    for name in fs::read_to_string(manifest).unwrap().lines().filter(|s| !s.is_empty()) {
        assert!(!name.contains("..") && !name.contains(':') && !name.contains('\\'), "invalid portable payload path");
        let path = repo.join(name);
        assert!(!path.is_symlink() && path.is_file(), "payload must be a regular file");
        let target = name.strip_prefix("desktop/portable/").unwrap_or(name);
        assert!(files.insert(target.to_string(), path).is_none(), "duplicate portable payload");
    }
    let mut digest = Sha256::new();
    let mut source = String::from("pub const FILES: &[(&str, &[u8])] = &[\n");
    for (name, path) in files {
        let path = path.canonicalize().expect("missing portable resource");
        println!("cargo:rerun-if-changed={}", path.display());
        let bytes = fs::read(&path).unwrap();
        digest.update(name.as_bytes());
        digest.update((bytes.len() as u64).to_le_bytes());
        digest.update(&bytes);
        source.push_str(&format!("({name:?}, include_bytes!({path:?})),\n"));
    }
    source.push_str(&format!(
        "];\npub const PAYLOAD_ID: &str = \"{:x}\";\n",
        digest.finalize()
    ));
    fs::write(
        Path::new(&std::env::var_os("OUT_DIR").unwrap()).join("portable_resources.rs"),
        source,
    )
    .unwrap();
}
