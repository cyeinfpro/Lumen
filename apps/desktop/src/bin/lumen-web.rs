use std::env;
use std::path::{Path, PathBuf};
use std::process::{exit, Command};

fn main() {
    let web_root = match resolve_web_root() {
        Some(path) => path,
        None => {
            eprintln!("unable to locate Lumen web standalone resources");
            exit(2);
        }
    };
    let server = web_root.join("server.js");
    let node = resolve_node_bin().unwrap_or_else(|| PathBuf::from("node"));
    let status = Command::new(node).arg(server).status();
    match status {
        Ok(status) => exit(status.code().unwrap_or(1)),
        Err(err) => {
            eprintln!("failed to start Lumen web runtime: {err}");
            exit(1);
        }
    }
}

fn resolve_web_root() -> Option<PathBuf> {
    if let Ok(raw) = env::var("LUMEN_WEB_ROOT") {
        let path = PathBuf::from(raw);
        if has_server(&path) {
            return Some(path);
        }
    }
    let exe = env::current_exe().ok()?;
    let dir = exe.parent()?;
    for path in [
        dir.join("resources/web"),
        dir.join("../resources/web"),
        dir.join("../Resources/resources/web"),
        dir.join("../../resources/web"),
    ] {
        if has_server(&path) {
            return Some(path);
        }
    }
    None
}

fn has_server(path: &Path) -> bool {
    path.join("server.js").is_file()
}

fn resolve_node_bin() -> Option<PathBuf> {
    if let Ok(raw) = env::var("LUMEN_NODE_BIN") {
        let path = PathBuf::from(raw);
        if path.is_file() {
            return Some(path);
        }
    }
    let exe = env::current_exe().ok()?;
    let dir = exe.parent()?;
    let node_name = if cfg!(target_os = "windows") {
        "node.exe"
    } else {
        "node"
    };
    for path in [
        dir.join("runtime/node").join(node_name),
        dir.join("resources/runtime/node").join(node_name),
        dir.join("../resources/runtime/node").join(node_name),
        dir.join("../Resources/resources/runtime/node")
            .join(node_name),
    ] {
        if path.is_file() {
            return Some(path);
        }
    }
    None
}
