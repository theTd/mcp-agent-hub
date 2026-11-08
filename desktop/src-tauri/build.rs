fn main() {
    // App commands get an ACL manifest, so only windows/origins granted in capabilities/ may call
    // them: the bundled splash page, never the hub console loaded from http://127.0.0.1.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["splash_state", "retry_hub"])),
    )
    .expect("failed to run tauri-build");
}
