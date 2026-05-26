fn main() {
    if std::env::var("PROFILE").as_deref() == Ok("debug") {
        create_debug_resource_placeholders();
    }
    tauri_build::build();
}

fn create_debug_resource_placeholders() {
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
