fn main() {
    if std::env::var("PROFILE").as_deref() == Ok("debug") {
        create_debug_sidecar_placeholders();
    }
    tauri_build::build();
}

fn create_debug_sidecar_placeholders() {
    let Ok(target) = std::env::var("TARGET") else {
        return;
    };
    let _ = std::fs::create_dir_all("binaries");
    for name in ["lumen-web"] {
        create_placeholder(&format!("binaries/{name}-{target}"));
        create_placeholder(&format!("binaries/{name}-{target}.exe"));
    }
    for path in [
        "resources/alembic/desktop/.placeholder",
        "resources/runtime/.placeholder",
        "resources/web/.placeholder",
        "resources/licenses/.placeholder",
    ] {
        create_placeholder(path);
    }
}

fn create_placeholder(path: &str) {
    let path = std::path::Path::new(path);
    if path.exists() {
        return;
    }
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(path, b"debug sidecar placeholder\n");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755));
    }
}
