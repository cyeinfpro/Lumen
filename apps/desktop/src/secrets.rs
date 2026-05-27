use anyhow::{anyhow, Context, Result};
use keyring::Entry;
use serde_json::{Map, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const SERVICE: &str = "com.lumen.desktop";
const FALLBACK_RELATIVE_PATH: &str = "data/tmp/secrets.local.json";

enum FallbackSecret {
    Missing,
    Deleted,
    Value(String),
}

fn set_keychain_secret(kind: &str, name: &str, value: &str) -> Result<()> {
    let entry = Entry::new(SERVICE, &format!("{kind}:{name}")).context("create keychain entry")?;
    if value.trim().is_empty() {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(err).context("delete keychain entry"),
        }
    } else {
        entry.set_password(value).context("write keychain entry")
    }
}

fn get_keychain_secret(kind: &str, name: &str) -> Result<Option<String>> {
    let entry = Entry::new(SERVICE, &format!("{kind}:{name}")).context("create keychain entry")?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(err).context("read keychain entry"),
    }
}

fn fallback_path(data_root: &Path) -> PathBuf {
    data_root.join(FALLBACK_RELATIVE_PATH)
}

fn read_fallback_file(path: &Path) -> Result<Map<String, Value>> {
    if !path.is_file() {
        return Ok(Map::new());
    }
    let raw = fs::read_to_string(path)
        .with_context(|| format!("read local desktop secret fallback {}", path.display()))?;
    match serde_json::from_str::<Value>(&raw)
        .with_context(|| format!("parse local desktop secret fallback {}", path.display()))?
    {
        Value::Object(map) => Ok(map),
        _ => Err(anyhow!(
            "local desktop secret fallback is not a JSON object"
        )),
    }
}

fn read_fallback_secret_marker(data_root: &Path, kind: &str, name: &str) -> Result<FallbackSecret> {
    let path = fallback_path(data_root);
    let map = read_fallback_file(&path)?;
    let Some(value) = map
        .get(kind)
        .and_then(Value::as_object)
        .and_then(|items| items.get(name))
    else {
        return Ok(FallbackSecret::Missing);
    };
    if value.is_null() {
        return Ok(FallbackSecret::Deleted);
    }
    Ok(value
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|value| FallbackSecret::Value(value.to_string()))
        .unwrap_or(FallbackSecret::Missing))
}

#[cfg(test)]
fn read_fallback_secret(data_root: &Path, kind: &str, name: &str) -> Result<Option<String>> {
    match read_fallback_secret_marker(data_root, kind, name)? {
        FallbackSecret::Value(value) => Ok(Some(value)),
        FallbackSecret::Missing | FallbackSecret::Deleted => Ok(None),
    }
}

fn write_fallback_secret(data_root: &Path, kind: &str, name: &str, value: &str) -> Result<()> {
    let path = fallback_path(data_root);
    let mut map = read_fallback_file(&path).unwrap_or_default();
    let entry = map
        .entry(kind.to_string())
        .or_insert_with(|| Value::Object(Map::new()));
    if !entry.is_object() {
        *entry = Value::Object(Map::new());
    }
    let items = entry
        .as_object_mut()
        .context("local desktop secret fallback category is not an object")?;
    if value.trim().is_empty() {
        items.insert(name.to_string(), Value::Null);
    } else {
        items.insert(name.to_string(), Value::String(value.trim().to_string()));
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_path = path.with_file_name(format!(
        "{}.tmp",
        path.file_name()
            .map(|name| name.to_string_lossy())
            .unwrap_or_else(|| "secrets.local.json".into())
    ));
    let payload = serde_json::to_vec_pretty(&Value::Object(map))?;
    fs::write(&tmp_path, payload)
        .with_context(|| format!("write local desktop secret fallback {}", tmp_path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp_path, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(&tmp_path, &path)
        .with_context(|| format!("replace local desktop secret fallback {}", path.display()))?;
    Ok(())
}

fn set_secret(data_root: &Path, kind: &str, name: &str, value: &str) -> Result<()> {
    let name = name.trim();
    let value = value.trim();
    if name.is_empty() {
        return Ok(());
    }

    let fallback_result = retry_secret_op(|| write_fallback_secret(data_root, kind, name, value));
    let keychain_result = retry_secret_op(|| set_keychain_secret(kind, name, value));
    match (fallback_result, keychain_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Ok(()), Err(err)) => {
            eprintln!(
                "desktop keychain write failed for {kind}:{name}; using local fallback: {err:#}"
            );
            write_out_of_sync_log(data_root, kind, name, "keychain", &err);
            Ok(())
        }
        (Err(err), Ok(())) => {
            eprintln!(
                "desktop local fallback write failed for {kind}:{name}; using keychain only: {err:#}"
            );
            write_out_of_sync_log(data_root, kind, name, "fallback", &err);
            Ok(())
        }
        (Err(fallback_err), Err(keychain_err)) => Err(anyhow!(
            "write desktop secret failed; local fallback error: {fallback_err:#}; keychain error: {keychain_err:#}"
        )),
    }
}

fn retry_secret_op<F>(mut op: F) -> Result<()>
where
    F: FnMut() -> Result<()>,
{
    let mut last_err = None;
    for idx in 0..3 {
        match op() {
            Ok(()) => return Ok(()),
            Err(err) => {
                last_err = Some(err);
                if idx < 2 {
                    std::thread::sleep(std::time::Duration::from_millis(100 * (idx + 1) as u64));
                }
            }
        }
    }
    Err(last_err.unwrap_or_else(|| anyhow!("secret operation failed")))
}

fn write_out_of_sync_log(
    data_root: &Path,
    kind: &str,
    name: &str,
    side: &str,
    err: &anyhow::Error,
) {
    let path = data_root.join("data/logs/secrets-out-of-sync.log");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(
            file,
            "{} kind={} name={} failed_side={} error={:#}",
            unix_epoch_ms(),
            kind,
            name,
            side,
            err
        );
    }
}

fn unix_epoch_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

fn get_secret(data_root: &Path, kind: &str, name: &str) -> Result<Option<String>> {
    let name = name.trim();
    if name.is_empty() {
        return Ok(None);
    }
    match read_fallback_secret_marker(data_root, kind, name) {
        Ok(FallbackSecret::Value(value)) => return Ok(Some(value)),
        Ok(FallbackSecret::Deleted) => return Ok(None),
        Ok(FallbackSecret::Missing) => {}
        Err(err) => {
            eprintln!("desktop local fallback read failed for {kind}:{name}: {err:#}");
        }
    }
    match get_keychain_secret(kind, name) {
        Ok(value) => Ok(value),
        Err(err) => {
            eprintln!("desktop keychain read failed for {kind}:{name}: {err:#}");
            Ok(None)
        }
    }
}

pub fn set_provider_key(data_root: &Path, provider: &str, value: &str) -> Result<()> {
    set_secret(data_root, "provider", provider, value)
}

pub fn get_provider_key(data_root: &Path, provider: &str) -> Result<Option<String>> {
    get_secret(data_root, "provider", provider)
}

pub fn set_proxy_password(data_root: &Path, proxy: &str, value: &str) -> Result<()> {
    set_secret(data_root, "proxy", proxy, value)
}

pub fn get_proxy_password(data_root: &Path, proxy: &str) -> Result<Option<String>> {
    get_secret(data_root, "proxy", proxy)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root() -> PathBuf {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before epoch")
            .as_nanos();
        let root = std::env::temp_dir().join(format!("lumen-secret-test-{now}"));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("create temp root");
        root
    }

    #[test]
    fn fallback_secret_round_trips_and_clears() {
        let root = temp_root();
        write_fallback_secret(&root, "provider", "openai", "sk-test")
            .expect("write fallback secret");
        assert_eq!(
            read_fallback_secret(&root, "provider", "openai")
                .expect("read fallback secret")
                .as_deref(),
            Some("sk-test")
        );
        write_fallback_secret(&root, "provider", "openai", "").expect("clear fallback secret");
        assert_eq!(
            read_fallback_secret(&root, "provider", "openai")
                .expect("read cleared fallback secret")
                .as_deref(),
            None
        );
        let _ = fs::remove_dir_all(root);
    }
}
