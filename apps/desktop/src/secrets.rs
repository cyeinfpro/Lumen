use anyhow::{anyhow, Context, Result};
use keyring::Entry;
use serde_json::{Map, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const SERVICE: &str = "com.lumen.desktop";
const FALLBACK_RELATIVE_PATH: &str = "data/tmp/secrets.local.json";
static PRIVATE_TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, PartialEq, Eq)]
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

pub(crate) fn harden_private_file(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .with_context(|| format!("restrict private file permissions {}", path.display()))?;
        return Ok(());
    }

    #[cfg(windows)]
    {
        return harden_private_file_windows(path);
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = path;
        Ok(())
    }
}

pub(crate) fn write_private_file(path: &Path, payload: &[u8]) -> Result<()> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }

    let mut file = options
        .open(path)
        .with_context(|| format!("create private file {}", path.display()))?;
    let write_result = (|| -> Result<()> {
        #[cfg(any(windows, not(unix)))]
        harden_private_file(path)?;

        file.write_all(payload)
            .with_context(|| format!("write private file {}", path.display()))?;
        file.flush()
            .with_context(|| format!("flush private file {}", path.display()))?;
        Ok(())
    })();
    if let Err(err) = write_result {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(err);
    }
    Ok(())
}

pub(crate) fn private_tmp_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "private".into());
    let seq = PRIVATE_TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    path.with_file_name(format!(
        "{file_name}.{}.{}.{}.tmp",
        std::process::id(),
        unix_epoch_ms(),
        seq
    ))
}

#[cfg(windows)]
fn harden_private_file_windows(path: &Path) -> Result<()> {
    use std::process::{Command, Stdio};

    let principal = current_windows_user_principal()?;
    let status = Command::new("icacls")
        .arg(path)
        .arg("/inheritance:r")
        .arg("/grant:r")
        .arg(format!("{principal}:F"))
        .arg("/remove:g")
        .arg("*S-1-1-0")
        .arg("*S-1-5-11")
        .arg("*S-1-5-32-545")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .with_context(|| format!("run icacls for private file {}", path.display()))?;
    if status.success() {
        Ok(())
    } else {
        Err(anyhow!(
            "icacls failed with status {status} for private file {}",
            path.display()
        ))
    }
}

#[cfg(windows)]
fn current_windows_user_principal() -> Result<String> {
    use std::process::{Command, Stdio};

    if let Ok(output) = Command::new("whoami")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
    {
        if output.status.success() {
            let principal = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !principal.is_empty() {
                return Ok(principal);
            }
        }
    }

    let username = std::env::var("USERNAME")
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow!("resolve current Windows user for private file ACL"))?;
    let domain = std::env::var("USERDOMAIN")
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty());
    Ok(domain
        .map(|domain| format!("{domain}\\{username}"))
        .unwrap_or(username))
}

fn write_fallback_map(path: &Path, map: Map<String, Value>) -> Result<()> {
    if map.is_empty() {
        match fs::remove_file(path) {
            Ok(()) => return Ok(()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(err) => {
                return Err(err).with_context(|| {
                    format!("remove local desktop secret fallback {}", path.display())
                });
            }
        }
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_path = private_tmp_path(path);
    let payload = serde_json::to_vec_pretty(&Value::Object(map))?;
    let _ = fs::remove_file(&tmp_path);
    write_private_file(&tmp_path, &payload)
        .with_context(|| format!("write local desktop secret fallback {}", tmp_path.display()))?;
    #[cfg(windows)]
    {
        let _ = fs::remove_file(path);
    }
    fs::rename(&tmp_path, path)
        .with_context(|| format!("replace local desktop secret fallback {}", path.display()))?;
    harden_private_file(path)?;
    Ok(())
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
    write_fallback_map(&path, map)
}

fn remove_fallback_secret(data_root: &Path, kind: &str, name: &str) -> Result<()> {
    let path = fallback_path(data_root);
    if !path.is_file() {
        return Ok(());
    }
    let mut map = read_fallback_file(&path)?;
    let mut remove_kind = false;
    let changed = if let Some(items) = map.get_mut(kind).and_then(Value::as_object_mut) {
        let changed = items.remove(name).is_some();
        remove_kind = items.is_empty();
        changed
    } else {
        false
    };
    if remove_kind {
        map.remove(kind);
    }
    if changed {
        write_fallback_map(&path, map)?;
    }
    Ok(())
}

fn set_secret(data_root: &Path, kind: &str, name: &str, value: &str) -> Result<()> {
    let name = name.trim();
    let value = value.trim();
    if name.is_empty() {
        return Ok(());
    }

    if value.is_empty() {
        return clear_secret(data_root, kind, name);
    }

    match retry_secret_op(|| set_keychain_secret(kind, name, value)) {
        Ok(()) => match get_keychain_secret(kind, name) {
            Ok(Some(stored)) if stored == value => {
                match retry_secret_op(|| remove_fallback_secret(data_root, kind, name)) {
                    Ok(()) => Ok(()),
                    Err(err) => {
                        eprintln!(
                            "desktop keychain write succeeded for {kind}:{name}, but local fallback cleanup failed: {err:#}"
                        );
                        write_out_of_sync_log(data_root, kind, name, "fallback_cleanup", &err);
                        Ok(())
                    }
                }
            }
            verify_result => {
                let verify_err = match verify_result {
                    Ok(Some(_)) => anyhow!("keychain read returned a different value after write"),
                    Ok(None) => anyhow!("keychain read returned no value after write"),
                    Err(err) => err.context("verify keychain write"),
                };
                let _ = retry_secret_op(|| set_keychain_secret(kind, name, ""));
                match retry_secret_op(|| write_fallback_secret(data_root, kind, name, value)) {
                    Ok(()) => {
                        eprintln!(
                            "desktop keychain write for {kind}:{name} did not round-trip; using protected local fallback: {verify_err:#}"
                        );
                        write_out_of_sync_log(data_root, kind, name, "keychain_verify", &verify_err);
                        Ok(())
                    }
                    Err(fallback_err) => Err(anyhow!(
                        "write desktop secret fallback failed after keychain verification error: {verify_err:#}; local fallback error: {fallback_err:#}"
                    )),
                }
            }
        },
        Err(keychain_err) => {
            match retry_secret_op(|| write_fallback_secret(data_root, kind, name, value)) {
                Ok(()) => {
                    eprintln!(
                        "desktop keychain write failed for {kind}:{name}; using protected local fallback: {keychain_err:#}"
                    );
                    write_out_of_sync_log(data_root, kind, name, "keychain", &keychain_err);
                    Ok(())
                }
                Err(fallback_err) => Err(anyhow!(
                    "write desktop secret failed; keychain error: {keychain_err:#}; local fallback error: {fallback_err:#}"
                )),
            }
        }
    }
}

fn clear_secret(data_root: &Path, kind: &str, name: &str) -> Result<()> {
    let keychain_result = retry_secret_op(|| set_keychain_secret(kind, name, ""));
    let fallback_result = retry_secret_op(|| write_fallback_secret(data_root, kind, name, ""));
    match (keychain_result, fallback_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Ok(()), Err(err)) => {
            eprintln!(
                "desktop keychain delete succeeded for {kind}:{name}, but local delete marker write failed: {err:#}"
            );
            write_out_of_sync_log(data_root, kind, name, "fallback_delete_marker", &err);
            Err(anyhow!(
                "delete desktop secret keychain succeeded, but local delete marker failed: {err:#}"
            ))
        }
        (Err(err), Ok(())) => {
            eprintln!(
                "desktop keychain delete failed for {kind}:{name}; local delete marker is not sufficient: {err:#}"
            );
            write_out_of_sync_log(data_root, kind, name, "keychain", &err);
            Err(anyhow!(
                "delete desktop secret keychain failed; local delete marker cannot mask a readable keychain value: {err:#}"
            ))
        }
        (Err(keychain_err), Err(fallback_err)) => Err(anyhow!(
            "delete desktop secret failed; keychain error: {keychain_err:#}; local delete marker error: {fallback_err:#}"
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
    let keychain_value = match get_keychain_secret(kind, name) {
        Ok(value) => value,
        Err(err) => {
            eprintln!("desktop keychain read failed for {kind}:{name}: {err:#}");
            None
        }
    };
    let fallback_marker = match read_fallback_secret_marker(data_root, kind, name) {
        Ok(marker) => marker,
        Err(err) => {
            eprintln!("desktop local fallback read failed for {kind}:{name}: {err:#}");
            FallbackSecret::Missing
        }
    };
    Ok(select_secret_value(keychain_value, fallback_marker))
}

fn select_secret_value(
    keychain_value: Option<String>,
    fallback_marker: FallbackSecret,
) -> Option<String> {
    if keychain_value.is_some() {
        return keychain_value;
    }
    match fallback_marker {
        FallbackSecret::Deleted => None,
        FallbackSecret::Value(value) => Some(value),
        FallbackSecret::Missing => None,
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
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static TEST_ROOT_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temp_root() -> PathBuf {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before epoch")
            .as_nanos();
        let seq = TEST_ROOT_COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "lumen-secret-test-{}-{now}-{seq}",
            std::process::id()
        ));
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

    #[test]
    fn fallback_secret_remove_drops_plaintext_without_touching_markers() {
        let root = temp_root();
        write_fallback_secret(&root, "provider", "openai", "sk-test")
            .expect("write provider fallback secret");
        write_fallback_secret(&root, "provider", "deleted-provider", "")
            .expect("write provider delete marker");
        write_fallback_secret(&root, "proxy", "corp", "proxy-secret")
            .expect("write proxy fallback secret");

        remove_fallback_secret(&root, "provider", "openai").expect("remove provider fallback");

        let raw = fs::read_to_string(fallback_path(&root)).expect("read fallback file");
        assert!(!raw.contains("sk-test"));
        assert!(raw.contains("deleted-provider"));
        assert!(raw.contains("proxy-secret"));
        assert_eq!(
            read_fallback_secret_marker(&root, "provider", "openai")
                .expect("read removed provider marker"),
            FallbackSecret::Missing
        );
        assert_eq!(
            read_fallback_secret_marker(&root, "provider", "deleted-provider")
                .expect("read preserved delete marker"),
            FallbackSecret::Deleted
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn secret_selection_prefers_keychain_before_plaintext_fallback() {
        assert_eq!(
            select_secret_value(
                Some("from-keychain".to_string()),
                FallbackSecret::Value("from-fallback".to_string())
            )
            .as_deref(),
            Some("from-keychain")
        );
        assert_eq!(
            select_secret_value(None, FallbackSecret::Value("from-fallback".to_string()))
                .as_deref(),
            Some("from-fallback")
        );
    }

    #[test]
    fn secret_selection_prefers_readable_keychain_over_stale_delete_marker() {
        assert_eq!(
            select_secret_value(Some("from-keychain".to_string()), FallbackSecret::Deleted)
                .as_deref(),
            Some("from-keychain")
        );
        assert_eq!(select_secret_value(None, FallbackSecret::Deleted), None);
    }

    #[cfg(unix)]
    #[test]
    fn fallback_secret_file_is_owner_only_on_unix() {
        use std::os::unix::fs::PermissionsExt;

        let root = temp_root();
        write_fallback_secret(&root, "provider", "openai", "sk-test")
            .expect("write fallback secret");
        let mode = fs::metadata(fallback_path(&root))
            .expect("read fallback metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
        let _ = fs::remove_dir_all(root);
    }
}
