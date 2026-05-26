use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use std::fs;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use zip::{write::FileOptions, CompressionMethod, ZipWriter};

#[derive(Debug, Clone, Serialize)]
pub struct DiagnosticSnapshot {
    pub data_root: PathBuf,
    pub logs_root: PathBuf,
    pub provider_runtime_file: PathBuf,
    pub sidecar_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiagnosticBundleOut {
    pub path: PathBuf,
    pub bytes: u64,
}

pub fn ensure_runtime_dirs(data_root: &Path) -> std::io::Result<()> {
    for rel in [
        "data/db",
        "data/storage",
        "data/cache",
        "data/redis",
        "data/logs",
        "data/backup",
        "data/tmp",
        "data/diagnostics",
    ] {
        fs::create_dir_all(data_root.join(rel))?;
    }
    Ok(())
}

pub fn create_diagnostic_bundle<T: Serialize>(
    data_root: &Path,
    metadata: &T,
    redis_info: Option<&str>,
) -> Result<DiagnosticBundleOut> {
    let diagnostics_root = data_root.join("data/diagnostics");
    fs::create_dir_all(&diagnostics_root).context("create diagnostics directory")?;
    let path = diagnostics_root.join(format!("lumen-diagnostics-{}.zip", unix_epoch_ms()));
    let file = fs::File::create(&path).context("create diagnostics zip")?;
    let mut zip = ZipWriter::new(file);
    let options = FileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .unix_permissions(0o600);

    let metadata_json =
        serde_json::to_vec_pretty(metadata).context("serialize diagnostics metadata")?;
    zip.start_file("metadata.json", options)?;
    zip.write_all(&metadata_json)?;

    add_logs(&mut zip, options, &data_root.join("data/logs"))?;
    add_sqlite_schema(&mut zip, options, &data_root.join("data/db/lumen.sqlite"))?;
    if let Some(info) = redis_info {
        zip.start_file("redis/info.txt", options)?;
        zip.write_all(redact_text(info).as_bytes())?;
    }

    zip.finish().context("finalize diagnostics zip")?;
    let bytes = fs::metadata(&path).map(|meta| meta.len()).unwrap_or(0);
    Ok(DiagnosticBundleOut { path, bytes })
}

fn add_logs(zip: &mut ZipWriter<fs::File>, options: FileOptions, logs_root: &Path) -> Result<()> {
    let Ok(entries) = fs::read_dir(logs_root) else {
        zip.start_file("logs/.missing", options)?;
        zip.write_all(b"logs directory is not present")?;
        return Ok(());
    };
    let mut paths = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.is_file())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .map(|name| name.ends_with(".log"))
                .unwrap_or(false)
        })
        .collect::<Vec<_>>();
    paths.sort();
    for path in paths {
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        zip.start_file(format!("logs/{name}"), options)?;
        let data = read_tail(&path, 2 * 1024 * 1024)
            .with_context(|| format!("read log tail {}", path.display()))?;
        zip.write_all(redact_text(&data).as_bytes())?;
    }
    Ok(())
}

fn add_sqlite_schema(
    zip: &mut ZipWriter<fs::File>,
    options: FileOptions,
    db_path: &Path,
) -> Result<()> {
    zip.start_file("sqlite/schema.sql", options)?;
    if !db_path.is_file() {
        zip.write_all(b"-- lumen.sqlite is not present\n")?;
        return Ok(());
    }
    let schema = sqlite_schema_dump(db_path)
        .unwrap_or_else(|err| format!("-- failed to dump sqlite schema: {err:#}\n"));
    zip.write_all(schema.as_bytes())?;
    Ok(())
}

fn sqlite_schema_dump(db_path: &Path) -> Result<String> {
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("open sqlite database {}", db_path.display()))?;
    let quick_check = conn
        .query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0))
        .unwrap_or_else(|err| format!("unavailable: {err}"));
    let mut out = format!("-- PRAGMA quick_check: {quick_check}\n\n");
    let mut stmt = conn.prepare(
        "SELECT type, name, tbl_name, sql \
         FROM sqlite_master \
         WHERE sql IS NOT NULL \
         ORDER BY type, name",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?;
    for row in rows {
        let (kind, name, table, sql) = row?;
        out.push_str(&format!("-- {kind}: {name} table={table}\n{sql};\n\n"));
    }
    Ok(out)
}

fn read_tail(path: &Path, max_bytes: u64) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let len = file.metadata()?.len();
    let mut truncated = false;
    if len > max_bytes {
        file.seek(SeekFrom::End(-(max_bytes as i64)))?;
        truncated = true;
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let mut text = String::from_utf8_lossy(&bytes).into_owned();
    if truncated {
        text.insert_str(0, "[truncated to last 2 MiB]\n");
    }
    Ok(text)
}

fn redact_text(raw: &str) -> String {
    raw.lines()
        .map(|line| {
            if line.contains('\u{0}')
                || line.contains("-----BEGIN ")
                || line.contains("PRIVATE KEY-----")
                || line.to_ascii_lowercase().contains("api_key")
                || line.contains("Authorization")
                || line.contains("sk-")
                || line.contains("sess-")
            {
                return "[REDACTED sensitive line]".to_string();
            }
            line.split(' ')
                .map(|token| {
                    if token.starts_with("sk-") || token.starts_with("sess-") {
                        "[REDACTED]"
                    } else {
                        token
                    }
                })
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n")
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
    use serde_json::json;
    use zip::ZipArchive;

    #[test]
    fn creates_diagnostic_bundle_with_expected_entries() -> Result<()> {
        let root = std::env::temp_dir().join(format!("lumen-diag-test-{}", unix_epoch_ms()));
        fs::create_dir_all(root.join("data/logs"))?;
        fs::create_dir_all(root.join("data/db"))?;
        fs::write(root.join("data/logs/supervisor.log"), "heartbeat ok\n")?;
        let db_path = root.join("data/db/lumen.sqlite");
        let conn = Connection::open(&db_path)?;
        conn.execute("CREATE TABLE example (id TEXT PRIMARY KEY)", [])?;
        drop(conn);

        let out = create_diagnostic_bundle(
            &root,
            &json!({ "runtime": "test" }),
            Some("# Server\r\nredis_version:test\r\n"),
        )?;
        assert!(out.path.is_file());
        let file = fs::File::open(&out.path)?;
        let mut archive = ZipArchive::new(file)?;
        assert!(archive.by_name("metadata.json").is_ok());
        assert!(archive.by_name("logs/supervisor.log").is_ok());
        assert!(archive.by_name("sqlite/schema.sql").is_ok());
        assert!(archive.by_name("redis/info.txt").is_ok());

        let _ = fs::remove_dir_all(root);
        Ok(())
    }
}
