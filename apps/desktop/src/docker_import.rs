use crate::sidecar::resolve_sidecar;
use crate::{backup, secrets};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const PENDING_IMPORT_JSON: &str = "pending-docker-import.json";
const FAILED_IMPORT_JSON: &str = "pending-docker-import.failed.json";

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PendingDockerImport {
    pub source_dump_path: PathBuf,
    pub pending_dump_path: PathBuf,
    pub source_storage_tar_path: Option<PathBuf>,
    pub pending_storage_tar_path: Option<PathBuf>,
    pub user_id: Option<String>,
    pub scheduled_at_ms: u128,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopDockerImportPlanOut {
    pub source_dump_path: PathBuf,
    pub pending_dump_path: PathBuf,
    pub source_storage_tar_path: Option<PathBuf>,
    pub pending_storage_tar_path: Option<PathBuf>,
    pub user_id: Option<String>,
    pub requires_restart: bool,
    pub scheduled_at_ms: u128,
}

#[derive(Debug, Clone, Serialize)]
pub struct PendingDockerImportStatus {
    pub pending: bool,
    pub failed: bool,
    pub source_dump_path: Option<PathBuf>,
    pub pending_dump_path: Option<PathBuf>,
    pub source_storage_tar_path: Option<PathBuf>,
    pub pending_storage_tar_path: Option<PathBuf>,
    pub user_id: Option<String>,
    pub scheduled_at_ms: Option<u128>,
    pub last_error: Option<String>,
    pub report: Option<Value>,
    pub safety_backup_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppliedDockerImportOut {
    pub imported_from: PathBuf,
    pub storage_from: Option<PathBuf>,
    pub safety_backup_path: Option<PathBuf>,
    pub report_path: PathBuf,
    pub report: Value,
}

#[derive(Debug, Deserialize)]
struct ProviderKeyFile {
    #[serde(default)]
    provider_keys: Vec<ProviderKeyEntry>,
}

#[derive(Debug, Deserialize)]
struct ProviderKeyEntry {
    name: String,
    api_key: String,
}

pub fn schedule_docker_import(
    data_root: &Path,
    source_dump_path: &Path,
    source_storage_tar_path: Option<&Path>,
    user_id: Option<String>,
) -> Result<DesktopDockerImportPlanOut> {
    ensure_import_dirs(data_root)?;
    if !source_dump_path.is_file() {
        bail!(
            "Docker database export does not exist: {}",
            source_dump_path.display()
        );
    }
    if backup::pending_restore_status(data_root).pending {
        bail!("a desktop restore is already pending; restart or cancel it before importing Docker data");
    }

    let pending_dump_path = pending_dump_path(data_root, source_dump_path);
    fs::copy(source_dump_path, &pending_dump_path).with_context(|| {
        format!(
            "copy Docker export {} to {}",
            source_dump_path.display(),
            pending_dump_path.display()
        )
    })?;

    let (source_storage_tar_path, pending_storage_tar_path) =
        if let Some(storage_path) = source_storage_tar_path {
            if !storage_path.is_file() {
                bail!(
                    "Docker storage archive does not exist: {}",
                    storage_path.display()
                );
            }
            let pending = pending_storage_tar_path(data_root);
            fs::copy(storage_path, &pending).with_context(|| {
                format!(
                    "copy Docker storage archive {} to {}",
                    storage_path.display(),
                    pending.display()
                )
            })?;
            (Some(storage_path.to_path_buf()), Some(pending))
        } else {
            (None, None)
        };

    let pending = PendingDockerImport {
        source_dump_path: source_dump_path.to_path_buf(),
        pending_dump_path: pending_dump_path.clone(),
        source_storage_tar_path: source_storage_tar_path.clone(),
        pending_storage_tar_path: pending_storage_tar_path.clone(),
        user_id: user_id.and_then(|value| {
            let trimmed = value.trim().to_string();
            (!trimmed.is_empty()).then_some(trimmed)
        }),
        scheduled_at_ms: unix_epoch_ms(),
    };
    write_json_private(&pending_import_json_path(data_root), &pending)?;
    let _ = fs::remove_file(failed_import_json_path(data_root));
    Ok(DesktopDockerImportPlanOut {
        source_dump_path: source_dump_path.to_path_buf(),
        pending_dump_path,
        source_storage_tar_path,
        pending_storage_tar_path,
        user_id: pending.user_id,
        requires_restart: true,
        scheduled_at_ms: pending.scheduled_at_ms,
    })
}

pub fn pending_docker_import_status(data_root: &Path) -> PendingDockerImportStatus {
    let pending_json = pending_import_json_path(data_root);
    if let Ok(raw) = fs::read_to_string(&pending_json) {
        if let Ok(pending) = serde_json::from_str::<PendingDockerImport>(&raw) {
            return PendingDockerImportStatus {
                pending: true,
                failed: false,
                source_dump_path: Some(pending.source_dump_path),
                pending_dump_path: Some(pending.pending_dump_path),
                source_storage_tar_path: pending.source_storage_tar_path,
                pending_storage_tar_path: pending.pending_storage_tar_path,
                user_id: pending.user_id,
                scheduled_at_ms: Some(pending.scheduled_at_ms),
                last_error: None,
                report: None,
                safety_backup_path: None,
            };
        }
    }

    let failed_json = failed_import_json_path(data_root);
    if let Ok(raw) = fs::read_to_string(&failed_json) {
        if let Ok(value) = serde_json::from_str::<Value>(&raw) {
            let pending = value.get("pending");
            return PendingDockerImportStatus {
                pending: false,
                failed: true,
                source_dump_path: pending
                    .and_then(|v| v.get("source_dump_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                pending_dump_path: pending
                    .and_then(|v| v.get("pending_dump_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                source_storage_tar_path: pending
                    .and_then(|v| v.get("source_storage_tar_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                pending_storage_tar_path: pending
                    .and_then(|v| v.get("pending_storage_tar_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                user_id: pending
                    .and_then(|v| v.get("user_id"))
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string),
                scheduled_at_ms: pending
                    .and_then(|v| v.get("scheduled_at_ms"))
                    .and_then(|v| v.as_u64())
                    .map(u128::from),
                last_error: value
                    .get("error")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string),
                report: value.get("report").cloned(),
                safety_backup_path: value
                    .get("safety_backup_path")
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
            };
        }
    }

    PendingDockerImportStatus {
        pending: false,
        failed: false,
        source_dump_path: None,
        pending_dump_path: None,
        source_storage_tar_path: None,
        pending_storage_tar_path: None,
        user_id: None,
        scheduled_at_ms: None,
        last_error: None,
        report: None,
        safety_backup_path: None,
    }
}

pub fn apply_pending_docker_import(
    data_root: &Path,
    app_version: &str,
) -> Result<Option<AppliedDockerImportOut>> {
    let pending_json = pending_import_json_path(data_root);
    if !pending_json.is_file() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&pending_json).context("read pending Docker import marker")?;
    let pending: PendingDockerImport =
        serde_json::from_str(&raw).context("parse pending Docker import marker")?;
    match apply_pending_docker_import_inner(data_root, app_version, &pending) {
        Ok(out) => {
            let _ = fs::remove_file(&pending_json);
            let _ = fs::remove_file(&pending.pending_dump_path);
            if let Some(path) = &pending.pending_storage_tar_path {
                let _ = fs::remove_file(path);
            }
            let _ = fs::remove_file(failed_import_json_path(data_root));
            Ok(Some(out))
        }
        Err(err) => {
            let failed = serde_json::json!({
                "failed_at_ms": unix_epoch_ms(),
                "error": format!("{err:#}"),
                "pending": pending,
            });
            let _ = write_json_private(&failed_import_json_path(data_root), &failed);
            let _ = fs::remove_file(&pending_json);
            Err(err)
        }
    }
}

pub fn clear_failed_docker_import_marker(data_root: &Path) -> Result<()> {
    match fs::remove_file(failed_import_json_path(data_root)) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err).context("clear failed Docker import marker"),
    }
}

pub fn mark_pending_docker_import_failed(data_root: &Path, error: &str) -> Result<()> {
    let pending_json = pending_import_json_path(data_root);
    let pending = fs::read_to_string(&pending_json)
        .ok()
        .and_then(|raw| serde_json::from_str::<PendingDockerImport>(&raw).ok());
    let failed = serde_json::json!({
        "failed_at_ms": unix_epoch_ms(),
        "error": error,
        "pending": pending,
    });
    write_json_private(&failed_import_json_path(data_root), &failed)?;
    let _ = fs::remove_file(pending_json);
    Ok(())
}

fn apply_pending_docker_import_inner(
    data_root: &Path,
    app_version: &str,
    pending: &PendingDockerImport,
) -> Result<AppliedDockerImportOut> {
    if !pending.pending_dump_path.is_file() {
        bail!(
            "pending Docker export is missing: {}",
            pending.pending_dump_path.display()
        );
    }
    let now = unix_epoch_ms();
    let report_path = data_root
        .join("data/tmp")
        .join(format!("docker-import-report-{now}.json"));
    let provider_key_path = data_root
        .join("data/tmp")
        .join(format!("docker-import-provider-keys-{now}.json"));
    let safety_backup = if data_root.join("data/db/lumen.sqlite").exists()
        || data_root.join("data/storage").exists()
    {
        Some(backup::create_desktop_backup(data_root, app_version)?.path)
    } else {
        None
    };

    let mut command = Command::new(resolve_sidecar("lumen-api")?);
    hide_windows_console(&mut command);
    command
        .arg("desktop-import")
        .arg("--dump")
        .arg(&pending.pending_dump_path)
        .arg("--data-root")
        .arg(data_root)
        .arg("--replace")
        .arg("--replace-storage")
        .arg("--report")
        .arg(&report_path)
        .arg("--provider-key-output")
        .arg(&provider_key_path)
        .stdin(Stdio::null());
    if let Some(storage_path) = &pending.pending_storage_tar_path {
        command.arg("--storage-tar").arg(storage_path);
    }
    if let Some(user_id) = &pending.user_id {
        command.arg("--user-id").arg(user_id);
    }

    let output = run_importer_with_timeout(command).context("run Docker desktop importer")?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!(
            "Docker desktop importer failed with {}: stdout={} stderr={}",
            output.status,
            stdout.trim(),
            stderr.trim()
        );
    }

    let mut report = read_json(&report_path).unwrap_or(Value::Null);
    match import_provider_keys(data_root, &provider_key_path) {
        Ok(imported) => annotate_report(&mut report, "provider_keys_imported", json!(imported)),
        Err(err) => annotate_report(
            &mut report,
            "provider_key_import_error",
            json!(format!("{err:#}")),
        ),
    }
    let _ = fs::remove_file(&provider_key_path);
    if let Ok(payload) = serde_json::to_vec_pretty(&report) {
        let _ = fs::write(&report_path, payload);
    }
    write_import_log(
        data_root,
        &format!(
            "docker import applied from {} storage={} safety_backup={} report={}\n",
            pending.source_dump_path.display(),
            pending
                .source_storage_tar_path
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "none".to_string()),
            safety_backup
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "none".to_string()),
            report_path.display()
        ),
    );
    Ok(AppliedDockerImportOut {
        imported_from: pending.source_dump_path.clone(),
        storage_from: pending.source_storage_tar_path.clone(),
        safety_backup_path: safety_backup,
        report_path,
        report,
    })
}

fn run_importer_with_timeout(mut command: Command) -> Result<std::process::Output> {
    let timeout_secs = std::env::var("LUMEN_DESKTOP_IMPORT_TIMEOUT_SECS")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(30 * 60);
    let capture_root =
        std::env::temp_dir().join(format!("lumen-desktop-import-{}", unix_epoch_ms()));
    fs::create_dir_all(&capture_root).context("create Docker import capture directory")?;
    let stdout_path = capture_root.join("stdout.log");
    let stderr_path = capture_root.join("stderr.log");
    let stdout_file = fs::File::create(&stdout_path).context("create Docker import stdout log")?;
    let stderr_file = fs::File::create(&stderr_path).context("create Docker import stderr log")?;
    let mut child = command
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .context("spawn Docker desktop importer")?;
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    let status = loop {
        if let Some(status) = child.try_wait().context("poll Docker desktop importer")? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = fs::remove_dir_all(&capture_root);
            bail!("Docker desktop importer timed out after {timeout_secs}s");
        }
        thread::sleep(Duration::from_millis(250));
    };
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let _ = fs::File::open(&stdout_path).and_then(|mut file| file.read_to_end(&mut stdout));
    let _ = fs::File::open(&stderr_path).and_then(|mut file| file.read_to_end(&mut stderr));
    let _ = fs::remove_dir_all(&capture_root);
    Ok(std::process::Output {
        status,
        stdout,
        stderr,
    })
}

#[cfg(windows)]
fn hide_windows_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_windows_console(_command: &mut Command) {}

fn read_json(path: &Path) -> Result<Value> {
    let raw = fs::read_to_string(path)?;
    serde_json::from_str(&raw).map_err(Into::into)
}

fn import_provider_keys(data_root: &Path, path: &Path) -> Result<usize> {
    if !path.is_file() {
        return Ok(0);
    }
    let raw = fs::read_to_string(path).context("read Docker provider key output")?;
    let parsed: ProviderKeyFile =
        serde_json::from_str(&raw).context("parse Docker provider key output")?;
    let mut imported = 0;
    for item in parsed.provider_keys {
        let name = item.name.trim();
        let api_key = item.api_key.trim();
        if name.is_empty() || api_key.is_empty() {
            continue;
        }
        secrets::set_provider_key(data_root, name, api_key)
            .with_context(|| format!("import provider key into desktop secret store for {name}"))?;
        imported += 1;
    }
    Ok(imported)
}

fn annotate_report(report: &mut Value, key: &str, value: Value) {
    if !report.is_object() {
        *report = json!({});
    }
    if let Some(object) = report.as_object_mut() {
        object.insert(key.to_string(), value);
    }
}

fn pending_dump_path(data_root: &Path, source: &Path) -> PathBuf {
    let name = source
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let suffix = if name.ends_with(".sql") {
        "copy.sql"
    } else {
        "dump"
    };
    data_root
        .join("data/tmp")
        .join(format!("pending-docker-import.{suffix}"))
}

fn pending_storage_tar_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp/pending-docker-storage.tar.gz")
}

fn pending_import_json_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(PENDING_IMPORT_JSON)
}

fn failed_import_json_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(FAILED_IMPORT_JSON)
}

fn ensure_import_dirs(data_root: &Path) -> Result<()> {
    for rel in ["data/tmp", "data/backup", "data/db", "data/storage"] {
        fs::create_dir_all(data_root.join(rel))?;
    }
    Ok(())
}

fn write_json_private<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, serde_json::to_vec_pretty(value)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn write_import_log(data_root: &Path, line: &str) {
    let path = data_root.join("data/logs/docker-import.log");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        use std::io::Write;
        let _ = writeln!(file, "{} {}", unix_epoch_ms(), line.trim_end());
    }
}

fn unix_epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}
