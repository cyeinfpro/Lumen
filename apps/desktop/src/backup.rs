use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{params, Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use zip::{write::FileOptions, CompressionMethod, ZipArchive, ZipWriter};

const BACKUP_FORMAT: &str = "lumen-desktop-backup";
const BACKUP_FORMAT_VERSION: u32 = 1;
const PENDING_RESTORE_ZIP: &str = "pending-restore.lumen-backup.zip";
const PENDING_RESTORE_JSON: &str = "pending-restore.json";
const FAILED_RESTORE_JSON: &str = "pending-restore.failed.json";
const STORAGE_RESTORE_SENTINEL: &str = "storage-restore-in-progress.json";
const FIXED_BACKUP_ENTRIES: &[&str] = &[
    "data/db/lumen.sqlite",
    "data/settings.json",
    "data/providers.json",
    "data/.bootstrap-done",
];
const BACKUP_ENTRY_PREFIXES: &[&str] = &["data/storage/"];

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BackupEntry {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BackupDatabase {
    pub present: bool,
    pub path: Option<String>,
    pub bytes: u64,
    pub sha256: Option<String>,
    pub quick_check: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BackupManifest {
    pub format: String,
    pub format_version: u32,
    pub created_at_ms: u128,
    pub app_version: String,
    pub platform: String,
    pub arch: String,
    pub database: BackupDatabase,
    pub entries: Vec<BackupEntry>,
    pub excluded: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopBackupOut {
    pub path: PathBuf,
    pub bytes: u64,
    pub manifest: BackupManifest,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PendingRestore {
    pub source_path: PathBuf,
    pub pending_path: PathBuf,
    pub scheduled_at_ms: u128,
    pub manifest: BackupManifest,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopRestorePlanOut {
    pub source_path: PathBuf,
    pub pending_path: PathBuf,
    pub requires_restart: bool,
    pub manifest: BackupManifest,
}

#[derive(Debug, Clone, Serialize)]
pub struct PendingRestoreStatus {
    pub pending: bool,
    pub failed: bool,
    pub pending_path: Option<PathBuf>,
    pub source_path: Option<PathBuf>,
    pub scheduled_at_ms: Option<u128>,
    pub manifest: Option<BackupManifest>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppliedRestoreOut {
    pub restored_from: PathBuf,
    pub safety_backup_path: Option<PathBuf>,
    pub manifest: BackupManifest,
}

pub fn create_desktop_backup(data_root: &Path, app_version: &str) -> Result<DesktopBackupOut> {
    ensure_backup_dirs(data_root)?;
    let now = unix_epoch_ms();
    let work_root = data_root
        .join("data/tmp")
        .join(format!("backup-work-{now}"));
    fs::create_dir_all(&work_root).context("create backup workspace")?;
    let result = create_desktop_backup_inner(data_root, app_version, now, &work_root);
    let _ = fs::remove_dir_all(&work_root);
    result
}

pub fn schedule_restore_backup(
    data_root: &Path,
    source_path: &Path,
) -> Result<DesktopRestorePlanOut> {
    ensure_backup_dirs(data_root)?;
    let manifest = validate_backup_file(source_path)?;
    let pending_path = pending_restore_zip_path(data_root);
    fs::copy(source_path, &pending_path).with_context(|| {
        format!(
            "copy restore backup {} to {}",
            source_path.display(),
            pending_path.display()
        )
    })?;
    let pending = PendingRestore {
        source_path: source_path.to_path_buf(),
        pending_path: pending_path.clone(),
        scheduled_at_ms: unix_epoch_ms(),
        manifest: manifest.clone(),
    };
    write_json_private(&pending_restore_json_path(data_root), &pending)?;
    let _ = fs::remove_file(failed_restore_json_path(data_root));
    Ok(DesktopRestorePlanOut {
        source_path: source_path.to_path_buf(),
        pending_path,
        requires_restart: true,
        manifest,
    })
}

pub fn pending_restore_status(data_root: &Path) -> PendingRestoreStatus {
    let pending_json = pending_restore_json_path(data_root);
    if let Ok(raw) = fs::read_to_string(&pending_json) {
        if let Ok(pending) = serde_json::from_str::<PendingRestore>(&raw) {
            return PendingRestoreStatus {
                pending: true,
                failed: false,
                pending_path: Some(pending.pending_path),
                source_path: Some(pending.source_path),
                scheduled_at_ms: Some(pending.scheduled_at_ms),
                manifest: Some(pending.manifest),
                last_error: None,
            };
        }
    }

    let failed_json = failed_restore_json_path(data_root);
    if let Ok(raw) = fs::read_to_string(&failed_json) {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) {
            let last_error = value
                .get("error")
                .and_then(|v| v.as_str())
                .map(|v| v.to_string());
            let manifest = value
                .get("pending")
                .and_then(|v| v.get("manifest"))
                .and_then(|v| serde_json::from_value(v.clone()).ok());
            return PendingRestoreStatus {
                pending: false,
                failed: true,
                pending_path: value
                    .get("pending")
                    .and_then(|v| v.get("pending_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                source_path: value
                    .get("pending")
                    .and_then(|v| v.get("source_path"))
                    .and_then(|v| v.as_str())
                    .map(PathBuf::from),
                scheduled_at_ms: value
                    .get("pending")
                    .and_then(|v| v.get("scheduled_at_ms"))
                    .and_then(|v| v.as_u64())
                    .map(u128::from),
                manifest,
                last_error,
            };
        }
    }

    let sentinel = storage_restore_sentinel_path(data_root);
    if sentinel.is_file() {
        return PendingRestoreStatus {
            pending: false,
            failed: true,
            pending_path: None,
            source_path: None,
            scheduled_at_ms: None,
            manifest: None,
            last_error: Some(format!(
                "storage restore was interrupted; inspect {} before starting",
                sentinel.display()
            )),
        };
    }

    PendingRestoreStatus {
        pending: false,
        failed: false,
        pending_path: None,
        source_path: None,
        scheduled_at_ms: None,
        manifest: None,
        last_error: None,
    }
}

pub fn apply_pending_restore(
    data_root: &Path,
    app_version: &str,
) -> Result<Option<AppliedRestoreOut>> {
    let pending_json = pending_restore_json_path(data_root);
    if !pending_json.is_file() {
        return Ok(None);
    }

    let raw = fs::read_to_string(&pending_json).context("read pending restore marker")?;
    let pending: PendingRestore =
        serde_json::from_str(&raw).context("parse pending restore marker")?;
    match apply_pending_restore_inner(data_root, app_version, &pending) {
        Ok(out) => {
            let _ = fs::remove_file(&pending_json);
            let _ = fs::remove_file(&pending.pending_path);
            let _ = fs::remove_file(failed_restore_json_path(data_root));
            Ok(Some(out))
        }
        Err(err) => {
            let failed = serde_json::json!({
                "failed_at_ms": unix_epoch_ms(),
                "error": format!("{err:#}"),
                "pending": pending,
            });
            let _ = write_json_private(&failed_restore_json_path(data_root), &failed);
            let _ = fs::remove_file(&pending_json);
            Err(err)
        }
    }
}

pub fn clear_failed_restore_marker(data_root: &Path) -> Result<()> {
    match fs::remove_file(failed_restore_json_path(data_root)) {
        Ok(()) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Err(err).context("clear failed restore marker"),
    }
    match fs::remove_file(storage_restore_sentinel_path(data_root)) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err).context("clear failed restore marker"),
    }
}

pub fn clear_pending_restore(data_root: &Path) -> Result<()> {
    for path in [
        pending_restore_json_path(data_root),
        pending_restore_zip_path(data_root),
        storage_restore_sentinel_path(data_root),
    ] {
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err).with_context(|| format!("clear {}", path.display())),
        }
    }
    Ok(())
}

pub fn mark_pending_restore_failed(data_root: &Path, error: &str) -> Result<()> {
    let pending_json = pending_restore_json_path(data_root);
    let pending = fs::read_to_string(&pending_json)
        .ok()
        .and_then(|raw| serde_json::from_str::<PendingRestore>(&raw).ok());
    let failed = serde_json::json!({
        "failed_at_ms": unix_epoch_ms(),
        "error": error,
        "pending": pending,
    });
    write_json_private(&failed_restore_json_path(data_root), &failed)?;
    let _ = fs::remove_file(pending_json);
    Ok(())
}

fn create_desktop_backup_inner(
    data_root: &Path,
    app_version: &str,
    now: u128,
    work_root: &Path,
) -> Result<DesktopBackupOut> {
    let backup_root = data_root.join("data/backup");
    let path = backup_root.join(format!("lumen-desktop-{now}.lumen-backup.zip"));
    let mut entries = Vec::new();
    let db_snapshot = work_root.join("lumen.sqlite");
    let database = snapshot_database(data_root, &db_snapshot)?;

    let file = fs::File::create(&path).context("create desktop backup zip")?;
    let mut zip = ZipWriter::new(file);
    let options = FileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .unix_permissions(0o600);

    if database.present {
        add_file_entry(
            &mut zip,
            options,
            &db_snapshot,
            "data/db/lumen.sqlite",
            &mut entries,
        )?;
    }
    add_optional_file(
        &mut zip,
        options,
        &data_root.join("data/settings.json"),
        "data/settings.json",
        &mut entries,
    )?;
    add_optional_file(
        &mut zip,
        options,
        &data_root.join("data/providers.json"),
        "data/providers.json",
        &mut entries,
    )?;
    add_optional_file(
        &mut zip,
        options,
        &data_root.join("data/.bootstrap-done"),
        "data/.bootstrap-done",
        &mut entries,
    )?;
    add_dir_entries(
        &mut zip,
        options,
        &data_root.join("data/storage"),
        "data/storage",
        &mut entries,
    )?;

    let manifest = BackupManifest {
        format: BACKUP_FORMAT.to_string(),
        format_version: BACKUP_FORMAT_VERSION,
        created_at_ms: now,
        app_version: app_version.to_string(),
        platform: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
        database,
        entries,
        excluded: vec![
            "provider API keys in desktop secret storage".to_string(),
            "redis runtime state".to_string(),
            "cache".to_string(),
            "logs".to_string(),
            "diagnostics bundles".to_string(),
        ],
    };
    zip.start_file("manifest.json", options)?;
    zip.write_all(serde_json::to_vec_pretty(&manifest)?.as_slice())?;
    zip.finish().context("finalize desktop backup zip")?;
    let bytes = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    Ok(DesktopBackupOut {
        path,
        bytes,
        manifest,
    })
}

fn snapshot_database(data_root: &Path, snapshot_path: &Path) -> Result<BackupDatabase> {
    let db_path = data_root.join("data/db/lumen.sqlite");
    if !db_path.is_file() {
        return Ok(BackupDatabase {
            present: false,
            path: None,
            bytes: 0,
            sha256: None,
            quick_check: None,
        });
    }
    if let Some(parent) = snapshot_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let conn = Connection::open_with_flags(&db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("open sqlite database {}", db_path.display()))?;
    conn.busy_timeout(Duration::from_secs(10))?;
    let quick_check = sqlite_quick_check(&conn)?;
    if quick_check != "ok" {
        bail!("sqlite quick_check failed before backup: {quick_check}");
    }
    let _ = fs::remove_file(snapshot_path);
    conn.execute(
        "VACUUM main INTO ?1",
        params![snapshot_path.to_string_lossy().as_ref()],
    )
    .context("snapshot sqlite database with VACUUM INTO")?;
    drop(conn);
    let snapshot_conn =
        Connection::open_with_flags(snapshot_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .context("open sqlite backup snapshot")?;
    let snapshot_check = sqlite_quick_check(&snapshot_conn)?;
    if snapshot_check != "ok" {
        bail!("sqlite quick_check failed after backup: {snapshot_check}");
    }
    let (sha256, bytes) = sha256_file(snapshot_path)?;
    Ok(BackupDatabase {
        present: true,
        path: Some("data/db/lumen.sqlite".to_string()),
        bytes,
        sha256: Some(sha256),
        quick_check: Some(snapshot_check),
    })
}

fn sqlite_quick_check(conn: &Connection) -> Result<String> {
    conn.query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0))
        .context("run sqlite quick_check")
}

fn add_optional_file(
    zip: &mut ZipWriter<fs::File>,
    options: FileOptions,
    source: &Path,
    entry_path: &str,
    entries: &mut Vec<BackupEntry>,
) -> Result<()> {
    if source.is_file() {
        add_file_entry(zip, options, source, entry_path, entries)?;
    }
    Ok(())
}

fn add_dir_entries(
    zip: &mut ZipWriter<fs::File>,
    options: FileOptions,
    source_root: &Path,
    entry_root: &str,
    entries: &mut Vec<BackupEntry>,
) -> Result<()> {
    if !source_root.is_dir() {
        return Ok(());
    }
    let mut children = fs::read_dir(source_root)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .collect::<Vec<_>>();
    children.sort();
    for path in children {
        let file_type = match fs::symlink_metadata(&path) {
            Ok(meta) => meta.file_type(),
            Err(_) => continue,
        };
        let name = match path.file_name().and_then(|name| name.to_str()) {
            Some(name) => name,
            None => continue,
        };
        let child_entry = format!("{entry_root}/{name}");
        if file_type.is_symlink() {
            continue;
        }
        if file_type.is_dir() {
            add_dir_entries(zip, options, &path, &child_entry, entries)?;
        } else if file_type.is_file() {
            add_file_entry(zip, options, &path, &child_entry, entries)?;
        }
    }
    Ok(())
}

fn add_file_entry(
    zip: &mut ZipWriter<fs::File>,
    options: FileOptions,
    source: &Path,
    entry_path: &str,
    entries: &mut Vec<BackupEntry>,
) -> Result<()> {
    if !is_allowed_backup_entry(entry_path) {
        bail!("refusing to add unsupported backup entry {entry_path}");
    }
    let (sha256, bytes) = sha256_file(source)?;
    zip.start_file(entry_path, options)?;
    let mut input = fs::File::open(source)?;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        zip.write_all(&buffer[..n])?;
    }
    entries.push(BackupEntry {
        path: entry_path.to_string(),
        bytes,
        sha256,
    });
    Ok(())
}

fn validate_backup_file(path: &Path) -> Result<BackupManifest> {
    let file = fs::File::open(path).with_context(|| format!("open backup {}", path.display()))?;
    let mut archive = ZipArchive::new(file).context("open backup zip")?;
    let mut raw = String::new();
    archive
        .by_name("manifest.json")
        .context("read backup manifest")?
        .read_to_string(&mut raw)?;
    let manifest: BackupManifest = serde_json::from_str(&raw).context("parse backup manifest")?;
    if manifest.format != BACKUP_FORMAT || manifest.format_version != BACKUP_FORMAT_VERSION {
        bail!(
            "unsupported backup format {} version {}",
            manifest.format,
            manifest.format_version
        );
    }
    for entry in &manifest.entries {
        if !is_allowed_backup_entry(&entry.path) {
            bail!("backup manifest contains unsupported entry {}", entry.path);
        }
        let mut file = archive
            .by_name(&entry.path)
            .with_context(|| format!("backup entry missing {}", entry.path))?;
        let (sha256, bytes) = sha256_reader(&mut file)?;
        if bytes != entry.bytes || sha256 != entry.sha256 {
            bail!("backup checksum mismatch for {}", entry.path);
        }
    }
    if manifest.database.present {
        let db_entry = manifest
            .database
            .path
            .as_deref()
            .ok_or_else(|| anyhow!("backup database path missing"))?;
        if db_entry != "data/db/lumen.sqlite" {
            bail!("unsupported database entry path {db_entry}");
        }
    }
    Ok(manifest)
}

fn apply_pending_restore_inner(
    data_root: &Path,
    app_version: &str,
    pending: &PendingRestore,
) -> Result<AppliedRestoreOut> {
    let manifest = validate_backup_file(&pending.pending_path)?;
    let now = unix_epoch_ms();
    let work_root = data_root
        .join("data/tmp")
        .join(format!("restore-work-{now}"));
    let extract_root = work_root.join("extract");
    fs::create_dir_all(&extract_root).context("create restore workspace")?;
    let result = (|| {
        extract_backup(&pending.pending_path, &manifest, &extract_root)?;
        if manifest.database.present {
            let db_path = extract_root.join("data/db/lumen.sqlite");
            let conn = Connection::open_with_flags(&db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                .context("open restored sqlite snapshot")?;
            let check = sqlite_quick_check(&conn)?;
            if check != "ok" {
                bail!("restored sqlite quick_check failed: {check}");
            }
        }

        let safety_backup = if data_root.join("data/db/lumen.sqlite").exists()
            || data_root.join("data/storage").exists()
        {
            match create_desktop_backup(data_root, app_version) {
                Ok(backup) => Some(backup.path),
                Err(err) => {
                    write_restore_log(
                        data_root,
                        &format!("safety backup failed before restore; restore aborted: {err:#}\n"),
                    );
                    return Err(err).context("safety backup failed before restore; restore aborted");
                }
            }
        } else {
            None
        };

        // Restore storage before DB so a storage swap failure cannot leave the
        // restored database pointing at missing old media files.
        restore_storage_dir(data_root, &extract_root, now)?;
        restore_optional_top_file(data_root, &extract_root, "data/settings.json")?;
        restore_optional_top_file(data_root, &extract_root, "data/providers.json")?;
        if should_restore_bootstrap_marker(&manifest.app_version, app_version) {
            restore_optional_top_file(data_root, &extract_root, "data/.bootstrap-done")?;
        } else {
            let _ = fs::remove_file(data_root.join("data/.bootstrap-done"));
            write_restore_log(
                data_root,
                &format!(
                    "restore skipped data/.bootstrap-done backup_version={} current_version={}\n",
                    manifest.app_version, app_version
                ),
            );
        }
        restore_database(data_root, &extract_root, manifest.database.present)?;
        write_restore_log(
            data_root,
            &format!(
                "restore applied from {} safety_backup={}\n",
                pending.source_path.display(),
                safety_backup
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "none".to_string())
            ),
        );
        Ok(AppliedRestoreOut {
            restored_from: pending.source_path.clone(),
            safety_backup_path: safety_backup,
            manifest,
        })
    })();
    let _ = fs::remove_dir_all(&work_root);
    result
}

fn extract_backup(zip_path: &Path, manifest: &BackupManifest, target_root: &Path) -> Result<()> {
    let file = fs::File::open(zip_path).context("open pending restore backup")?;
    let mut archive = ZipArchive::new(file).context("open pending restore zip")?;
    for entry in &manifest.entries {
        let safe_path = safe_relative_path(&entry.path)?;
        let target = target_root.join(&safe_path);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut zip_file = archive
            .by_name(&entry.path)
            .with_context(|| format!("read backup entry {}", entry.path))?;
        let mut output = fs::File::create(&target)?;
        let mut hasher = Sha256::new();
        let mut bytes = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let n = zip_file.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            bytes += n as u64;
            hasher.update(&buffer[..n]);
            output.write_all(&buffer[..n])?;
        }
        let sha256 = hex_lower(&hasher.finalize());
        if bytes != entry.bytes || sha256 != entry.sha256 {
            bail!("backup checksum mismatch while extracting {}", entry.path);
        }
    }
    Ok(())
}

fn restore_database(data_root: &Path, extract_root: &Path, present: bool) -> Result<()> {
    let db_dir = data_root.join("data/db");
    fs::create_dir_all(&db_dir)?;
    let current_db = db_dir.join("lumen.sqlite");
    if current_db.is_file() {
        if let Ok(conn) =
            Connection::open_with_flags(&current_db, OpenFlags::SQLITE_OPEN_READ_WRITE)
        {
            let _ = conn.busy_timeout(Duration::from_secs(5));
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
        }
    }
    for suffix in ["", "-wal", "-shm"] {
        remove_file_retry(&db_dir.join(format!("lumen.sqlite{suffix}")))?;
    }
    if present {
        fs::copy(
            extract_root.join("data/db/lumen.sqlite"),
            db_dir.join("lumen.sqlite"),
        )
        .context("restore sqlite database")?;
    }
    Ok(())
}

fn restore_optional_top_file(data_root: &Path, extract_root: &Path, rel: &str) -> Result<()> {
    let rel_path = safe_relative_path(rel)?;
    let source = extract_root.join(&rel_path);
    let target = data_root.join(&rel_path);
    if source.is_file() {
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(source, target).with_context(|| format!("restore {rel}"))?;
    } else {
        let _ = fs::remove_file(target);
    }
    Ok(())
}

fn restore_storage_dir(data_root: &Path, extract_root: &Path, now: u128) -> Result<()> {
    let source = extract_root.join("data/storage");
    let target = data_root.join("data/storage");
    let old = data_root
        .join("data")
        .join(format!("storage.before-restore-{now}"));
    let tmp = data_root
        .join("data")
        .join(format!("storage.restore-tmp-{now}"));
    let sentinel = storage_restore_sentinel_path(data_root);
    write_json_private(
        &sentinel,
        &serde_json::json!({
            "started_at_ms": now,
            "target": target,
            "backup": old,
        }),
    )
    .context("write storage restore sentinel")?;
    let _ = fs::remove_dir_all(&old);
    let _ = fs::remove_dir_all(&tmp);
    let stage_result = if source.is_dir() {
        copy_dir_all(&source, &tmp).context("stage restored storage directory")
    } else {
        fs::create_dir_all(&tmp).context("stage empty storage directory")
    };
    if let Err(err) = stage_result {
        let _ = fs::remove_dir_all(&tmp);
        return Err(err);
    }
    if target.exists() {
        if let Err(err) = rename_retry(&target, &old).context("move current storage out of the way")
        {
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::remove_file(&sentinel);
            return Err(err);
        }
    }
    if let Err(err) = rename_retry(&tmp, &target).context("install restored storage directory") {
        if old.exists() && !target.exists() {
            if let Err(rollback_err) = rename_retry(&old, &target) {
                write_restore_log(
                    data_root,
                    &format!(
                        "storage restore rollback failed backup={} target={} error={rollback_err:#}\n",
                        old.display(),
                        target.display()
                    ),
                );
                let _ = fs::remove_dir_all(&tmp);
                return Err(err).with_context(|| {
                    format!(
                        "storage restore failed and rollback failed; original storage remains at {}",
                        old.display()
                    )
                });
            }
        }
        let _ = fs::remove_dir_all(&tmp);
        let _ = fs::remove_file(&sentinel);
        return Err(err);
    }
    let _ = fs::remove_dir_all(old);
    let _ = fs::remove_file(sentinel);
    Ok(())
}

fn remove_file_retry(path: &Path) -> Result<()> {
    retry_fs_op(|| match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err),
    })
    .with_context(|| format!("remove {}", path.display()))
}

fn rename_retry(from: &Path, to: &Path) -> Result<()> {
    retry_fs_op(|| fs::rename(from, to))
        .with_context(|| format!("rename {} to {}", from.display(), to.display()))
}

fn retry_fs_op<F>(mut op: F) -> Result<()>
where
    F: FnMut() -> std::io::Result<()>,
{
    let delays = [
        Duration::from_millis(0),
        Duration::from_millis(100),
        Duration::from_millis(500),
        Duration::from_secs(1),
    ];
    let mut last_err = None;
    for delay in delays {
        if delay > Duration::from_millis(0) {
            std::thread::sleep(delay);
        }
        match op() {
            Ok(()) => return Ok(()),
            Err(err) => last_err = Some(err),
        }
    }
    Err(last_err
        .map(anyhow::Error::from)
        .unwrap_or_else(|| anyhow!("filesystem operation failed")))
}

fn copy_dir_all(source: &Path, target: &Path) -> Result<()> {
    fs::create_dir_all(target)?;
    let mut children = fs::read_dir(source)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .collect::<Vec<_>>();
    children.sort();
    for path in children {
        let target_path = target.join(
            path.file_name()
                .ok_or_else(|| anyhow!("source path has no file name"))?,
        );
        let meta = fs::symlink_metadata(&path)?;
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            copy_dir_all(&path, &target_path)?;
        } else if meta.is_file() {
            fs::copy(&path, &target_path)?;
        }
    }
    Ok(())
}

fn safe_relative_path(raw: &str) -> Result<PathBuf> {
    if !is_allowed_backup_entry(raw) {
        bail!("unsupported backup entry {raw}");
    }
    let path = Path::new(raw);
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => out.push(part),
            _ => bail!("unsafe backup entry path {raw}"),
        }
    }
    Ok(out)
}

fn is_allowed_backup_entry(raw: &str) -> bool {
    if raw.is_empty() || raw.starts_with('/') || raw.starts_with('\\') || raw.contains('\\') {
        return false;
    }
    if raw
        .split('/')
        .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return false;
    }
    FIXED_BACKUP_ENTRIES.contains(&raw)
        || BACKUP_ENTRY_PREFIXES
            .iter()
            .any(|prefix| raw.starts_with(prefix))
}

fn should_restore_bootstrap_marker(backup_version: &str, current_version: &str) -> bool {
    let Some((backup_major, backup_minor)) = version_major_minor(backup_version) else {
        return true;
    };
    let Some((current_major, current_minor)) = version_major_minor(current_version) else {
        return true;
    };
    backup_major == current_major && backup_minor == current_minor
}

fn version_major_minor(version: &str) -> Option<(u64, u64)> {
    let normalized = version.trim().trim_start_matches('v');
    let mut parts = normalized.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    let minor = parts.next()?.parse::<u64>().ok()?;
    Some((major, minor))
}

fn sha256_file(path: &Path) -> Result<(String, u64)> {
    let mut file = fs::File::open(path)?;
    sha256_reader(&mut file)
}

fn sha256_reader<R: Read>(reader: &mut R) -> Result<(String, u64)> {
    let mut hasher = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        bytes += n as u64;
        hasher.update(&buffer[..n]);
    }
    Ok((hex_lower(&hasher.finalize()), bytes))
}

fn hex_lower(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn ensure_backup_dirs(data_root: &Path) -> Result<()> {
    for rel in ["data/backup", "data/tmp", "data/db", "data/storage"] {
        fs::create_dir_all(data_root.join(rel))?;
    }
    Ok(())
}

fn pending_restore_zip_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(PENDING_RESTORE_ZIP)
}

fn pending_restore_json_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(PENDING_RESTORE_JSON)
}

fn failed_restore_json_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(FAILED_RESTORE_JSON)
}

fn storage_restore_sentinel_path(data_root: &Path) -> PathBuf {
    data_root.join("data/tmp").join(STORAGE_RESTORE_SENTINEL)
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

fn write_restore_log(data_root: &Path, line: &str) {
    let path = data_root.join("data/logs/restore.log");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{} {}", unix_epoch_ms(), line.trim_end());
    }
}

fn unix_epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backup_and_pending_restore_round_trip() -> Result<()> {
        let root = std::env::temp_dir().join(format!("lumen-backup-test-{}", unix_epoch_ms()));
        fs::create_dir_all(root.join("data/db"))?;
        fs::create_dir_all(root.join("data/storage/nested"))?;
        fs::write(root.join("data/storage/nested/image.txt"), "before")?;
        fs::write(root.join("data/settings.json"), r#"{"theme":"dark"}"#)?;
        fs::write(
            root.join("data/providers.json"),
            r#"{"providers":[{"name":"OpenAI 官方","base_url":"https://api.openai.com/v1","enabled":false}],"proxies":[]}"#,
        )?;
        fs::write(root.join("data/.bootstrap-done"), "done")?;
        let db_path = root.join("data/db/lumen.sqlite");
        let conn = Connection::open(&db_path)?;
        conn.execute("CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT)", [])?;
        conn.execute(
            "INSERT INTO example (id, value) VALUES ('one', 'before')",
            [],
        )?;
        drop(conn);

        let backup = create_desktop_backup(&root, "test")?;
        assert!(backup.path.is_file());
        assert!(backup.manifest.database.present);
        assert!(backup
            .manifest
            .entries
            .iter()
            .any(|entry| entry.path == "data/storage/nested/image.txt"));

        fs::write(root.join("data/storage/nested/image.txt"), "after")?;
        let conn = Connection::open(&db_path)?;
        conn.execute("UPDATE example SET value='after' WHERE id='one'", [])?;
        drop(conn);

        let plan = schedule_restore_backup(&root, &backup.path)?;
        assert!(plan.requires_restart);
        let applied = apply_pending_restore(&root, "test")?.expect("restore applied");
        assert_eq!(applied.restored_from, backup.path);

        let conn = Connection::open(&db_path)?;
        let value: String =
            conn.query_row("SELECT value FROM example WHERE id='one'", [], |row| {
                row.get(0)
            })?;
        assert_eq!(value, "before");
        assert_eq!(
            fs::read_to_string(root.join("data/storage/nested/image.txt"))?,
            "before"
        );
        assert!(!pending_restore_status(&root).pending);

        let _ = fs::remove_dir_all(root);
        Ok(())
    }

    #[test]
    fn restore_aborts_when_safety_backup_fails() -> Result<()> {
        let source_root = std::env::temp_dir()
            .join(format!("lumen-backup-source-test-{}", unix_epoch_ms()));
        fs::create_dir_all(source_root.join("data/db"))?;
        fs::create_dir_all(source_root.join("data/storage"))?;
        fs::write(source_root.join("data/storage/restored.txt"), "restored")?;
        let source_db = source_root.join("data/db/lumen.sqlite");
        let conn = Connection::open(&source_db)?;
        conn.execute("CREATE TABLE example (id TEXT PRIMARY KEY)", [])?;
        drop(conn);
        let backup = create_desktop_backup(&source_root, "test")?;

        let root = std::env::temp_dir()
            .join(format!("lumen-backup-safety-test-{}", unix_epoch_ms()));
        fs::create_dir_all(root.join("data/db"))?;
        fs::create_dir_all(root.join("data/storage"))?;
        fs::write(root.join("data/db/lumen.sqlite"), "not a sqlite database")?;
        fs::write(root.join("data/storage/current.txt"), "current")?;

        let _plan = schedule_restore_backup(&root, &backup.path)?;
        let err = apply_pending_restore(&root, "test").expect_err("restore must abort");

        assert!(format!("{err:#}").contains("safety backup failed before restore"));
        assert_eq!(
            fs::read_to_string(root.join("data/storage/current.txt"))?,
            "current"
        );
        assert!(!root.join("data/storage/restored.txt").exists());
        assert!(failed_restore_json_path(&root).is_file());

        let _ = fs::remove_dir_all(source_root);
        let _ = fs::remove_dir_all(root);
        Ok(())
    }

    #[test]
    fn bootstrap_marker_only_survives_same_major_minor_restore() {
        assert!(should_restore_bootstrap_marker("1.1.50", "1.1.64"));
        assert!(!should_restore_bootstrap_marker("1.1.50", "1.2.0"));
        assert!(!should_restore_bootstrap_marker("1.1.50", "2.0.0"));
        assert!(should_restore_bootstrap_marker("test", "1.2.0"));
    }
}
