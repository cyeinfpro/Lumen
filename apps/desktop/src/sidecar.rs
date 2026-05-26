use crate::diagnostics::ensure_runtime_dirs;
use crate::secrets::{get_provider_key, get_proxy_password, set_provider_key, set_proxy_password};
use anyhow::{anyhow, Context, Result};
use portpicker::pick_unused_port;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream as StdTcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{sleep, timeout};

#[derive(Debug, Clone, Serialize)]
pub struct RuntimeInfo {
    pub data_root: PathBuf,
    pub api_port: u16,
    pub web_port: u16,
    pub redis_port: u16,
    pub worker_metrics_port: u16,
    pub provider_runtime_file: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct ProviderMetadata {
    name: String,
    base_url: String,
    #[serde(default = "default_priority")]
    priority: i64,
    #[serde(default = "default_weight")]
    weight: i64,
    #[serde(default)]
    enabled: bool,
    #[serde(default = "default_purposes")]
    purposes: Vec<String>,
    #[serde(flatten)]
    extra: serde_json::Map<String, Value>,
}

fn default_priority() -> i64 {
    100
}

fn default_weight() -> i64 {
    1
}

fn default_purposes() -> Vec<String> {
    vec!["chat".into(), "image".into()]
}

pub struct Supervisor {
    pub runtime: RuntimeInfo,
    local_token: String,
    redis_password: String,
    children: Vec<(String, Child)>,
    restart_counts: BTreeMap<String, u64>,
    last_exit_statuses: BTreeMap<String, String>,
    last_restart_reasons: BTreeMap<String, String>,
    started_at_ms: BTreeMap<String, u128>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SidecarStatus {
    pub name: String,
    pub pid: u32,
    pub running: bool,
    pub exit_status: Option<String>,
    pub port: Option<u16>,
    pub critical: bool,
    pub ready: bool,
    pub restart_count: u64,
    pub started_at_ms: Option<u128>,
    pub last_exit_status: Option<String>,
    pub last_restart_reason: Option<String>,
    pub rss_bytes: Option<u64>,
    pub log_path: PathBuf,
    pub stderr_log_path: PathBuf,
}

#[derive(Debug, Clone)]
pub enum SidecarRecovery {
    None,
    Restarted(Vec<String>),
    FullRestart { reason: String },
}

impl Supervisor {
    pub fn new(data_root: PathBuf) -> Result<Self> {
        ensure_runtime_dirs(&data_root).context("create desktop runtime directories")?;
        let api_port = pick_unused_port().ok_or_else(|| anyhow!("no free api port"))?;
        let web_port = pick_unused_port().ok_or_else(|| anyhow!("no free web port"))?;
        let redis_port = pick_unused_port().ok_or_else(|| anyhow!("no free redis port"))?;
        let worker_metrics_port =
            pick_unused_port().ok_or_else(|| anyhow!("no free worker metrics port"))?;
        let provider_runtime_file = data_root.join("data/tmp/providers.runtime.json");
        Ok(Self {
            runtime: RuntimeInfo {
                data_root,
                api_port,
                web_port,
                redis_port,
                worker_metrics_port,
                provider_runtime_file,
            },
            local_token: random_token(),
            redis_password: random_token(),
            children: Vec::new(),
            restart_counts: BTreeMap::new(),
            last_exit_statuses: BTreeMap::new(),
            last_restart_reasons: BTreeMap::new(),
            started_at_ms: BTreeMap::new(),
        })
    }

    pub fn refresh_provider_runtime(&self) -> Result<()> {
        let metadata_path = self.runtime.data_root.join("data/providers.json");
        let (mut metadata, mut proxy_metadata) = read_provider_config_metadata(&metadata_path)?;
        let (runtime_metadata, runtime_proxy_metadata) =
            read_provider_config_metadata(&self.runtime.provider_runtime_file)?;
        if metadata.is_empty() && !runtime_metadata.is_empty() {
            metadata = runtime_metadata.clone();
        }
        if proxy_metadata.is_empty() && !runtime_proxy_metadata.is_empty() {
            proxy_metadata = runtime_proxy_metadata.clone();
        }
        let runtime_provider_keys = provider_secret_map(&runtime_metadata);
        let runtime_proxy_passwords = proxy_secret_map(&runtime_proxy_metadata);
        let mut providers = Vec::with_capacity(metadata.len());
        for item in metadata {
            let keychain_api_key = get_provider_key(&item.name)?;
            let api_key = match keychain_api_key.as_deref() {
                Some(value) if !value.is_empty() => value.to_string(),
                _ => runtime_provider_keys
                    .get(&item.name)
                    .cloned()
                    .unwrap_or_default(),
            };
            if !api_key.is_empty() && keychain_api_key.as_deref().unwrap_or("").is_empty() {
                set_provider_key(&item.name, &api_key)?;
            }
            let enabled = item.enabled && !api_key.is_empty();
            let mut row = item.extra;
            row.insert("name".into(), json!(item.name));
            row.insert("base_url".into(), json!(item.base_url));
            row.insert("api_key".into(), json!(api_key));
            row.insert("priority".into(), json!(item.priority));
            row.insert("weight".into(), json!(item.weight.max(1)));
            row.insert("enabled".into(), json!(enabled));
            row.insert("purposes".into(), json!(item.purposes));
            providers.push(Value::Object(row));
        }
        let mut proxies = Vec::with_capacity(proxy_metadata.len());
        for item in proxy_metadata {
            let Value::Object(mut row) = item else {
                continue;
            };
            let name = row
                .get("name")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_string();
            if !name.is_empty() {
                let keychain_password = get_proxy_password(&name)?;
                let metadata_password = row
                    .get("password")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .map(str::to_string);
                let password = match keychain_password.as_deref() {
                    Some(value) if !value.is_empty() => value.to_string(),
                    _ => metadata_password
                        .or_else(|| runtime_proxy_passwords.get(&name).cloned())
                        .unwrap_or_default(),
                };
                if !password.is_empty() {
                    if keychain_password.as_deref().unwrap_or("").is_empty() {
                        set_proxy_password(&name, &password)?;
                    }
                    row.insert("password".into(), json!(password));
                }
            }
            proxies.push(Value::Object(row));
        }
        if providers.is_empty() {
            providers.push(json!({
                "name": "OpenAI 官方",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "priority": 100,
                "weight": 1,
                "enabled": false,
                "purposes": ["chat", "image"]
            }));
        }
        if let Some(parent) = self.runtime.provider_runtime_file.parent() {
            fs::create_dir_all(parent)?;
        }
        let payload =
            serde_json::to_vec_pretty(&json!({ "providers": providers, "proxies": proxies }))?;
        fs::write(&self.runtime.provider_runtime_file, payload)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                &self.runtime.provider_runtime_file,
                fs::Permissions::from_mode(0o600),
            )?;
        }
        Ok(())
    }

    pub async fn spawn_all(&mut self) -> Result<()> {
        self.refresh_provider_runtime()?;
        self.spawn_redis()?;
        wait_for_redis(
            self.runtime.redis_port,
            &self.redis_password,
            Duration::from_secs(10),
        )
        .await?;
        self.spawn_api()?;
        wait_for_http_ok(
            self.runtime.api_port,
            "/system/desktop-ready",
            Duration::from_secs(30),
            &[],
        )
        .await?;
        self.spawn_worker()?;
        wait_for_http_ok(
            self.runtime.worker_metrics_port,
            "/metrics",
            Duration::from_secs(20),
            &[],
        )
        .await?;
        self.spawn_web()?;
        wait_for_http_ok(self.runtime.web_port, "/", Duration::from_secs(25), &[]).await?;
        Ok(())
    }

    pub fn shutdown(&mut self) {
        let _ = self.log_supervisor_event("shutdown", json!({}));
        for (_name, child) in self.children.iter_mut().rev() {
            terminate_child(child);
            let _ = child.wait();
        }
        self.children.clear();
    }

    pub fn sidecar_statuses(&mut self) -> Vec<SidecarStatus> {
        let runtime = self.runtime.clone();
        let restart_counts = self.restart_counts.clone();
        let last_exit_statuses = self.last_exit_statuses.clone();
        let last_restart_reasons = self.last_restart_reasons.clone();
        let started_at_ms = self.started_at_ms.clone();
        self.children
            .iter_mut()
            .map(|(name, child)| {
                let pid = child.id();
                let (running, exit_status) = match child.try_wait() {
                    Ok(Some(status)) => (false, Some(status.to_string())),
                    Ok(None) => (true, None),
                    Err(err) => (false, Some(format!("poll failed: {err}"))),
                };
                let port = sidecar_port(&runtime, name);
                SidecarStatus {
                    name: name.clone(),
                    pid,
                    running,
                    exit_status: exit_status
                        .clone()
                        .or_else(|| last_exit_statuses.get(name).cloned()),
                    port,
                    critical: is_critical_sidecar(name),
                    ready: running && sidecar_ready(&runtime, name),
                    restart_count: *restart_counts.get(name).unwrap_or(&0),
                    started_at_ms: started_at_ms.get(name).copied(),
                    last_exit_status: last_exit_statuses.get(name).cloned(),
                    last_restart_reason: last_restart_reasons.get(name).cloned(),
                    rss_bytes: running.then(|| process_rss_bytes(pid)).flatten(),
                    log_path: sidecar_log_path(&runtime.data_root, name, false),
                    stderr_log_path: sidecar_log_path(&runtime.data_root, name, true),
                }
            })
            .collect()
    }

    pub fn write_heartbeat(&mut self) -> Result<()> {
        let statuses = self.sidecar_statuses();
        self.log_supervisor_event("heartbeat", json!({ "sidecars": statuses }))
    }

    pub fn note_full_restart(&mut self, reason: &str) {
        for name in ["redis", "api", "worker", "web"] {
            self.record_restart(name, reason);
        }
        let _ = self.log_supervisor_event("full_restart", json!({ "reason": reason }));
    }

    pub fn note_startup_failure(&self, error: &str) {
        let _ = self.log_supervisor_event(
            "startup_failure",
            json!({
                "error": error,
                "logs_root": self.runtime.data_root.join("data/logs"),
            }),
        );
    }

    pub fn redis_info(&self) -> Option<String> {
        redis_info_sync(self.runtime.redis_port, &self.redis_password).ok()
    }

    pub fn recover_exited(&mut self) -> Result<SidecarRecovery> {
        let mut exited = Vec::new();
        let mut idx = 0;
        while idx < self.children.len() {
            let status = {
                let (_name, child) = &mut self.children[idx];
                child.try_wait()
            };
            match status {
                Ok(Some(status)) => {
                    let (name, mut child) = self.children.remove(idx);
                    let status_text = status.to_string();
                    cleanup_exited_child(&mut child);
                    self.record_exit(&name, &status_text);
                    exited.push((name, status_text));
                }
                Ok(None) => idx += 1,
                Err(err) => {
                    let (name, _child) = self.children.remove(idx);
                    return Err(err).with_context(|| format!("poll sidecar {name}"));
                }
            }
        }

        let critical_exit = exited
            .iter()
            .find(|(name, _status)| is_critical_sidecar(name));
        if let Some((name, status)) = critical_exit {
            let reason = format!("critical sidecar {name} exited with {status}");
            let _ = self.log_supervisor_event("critical_exit", json!({ "reason": reason }));
            self.shutdown();
            return Ok(SidecarRecovery::FullRestart { reason });
        }

        for critical in ["redis", "api"] {
            if !self.children.iter().any(|(name, _child)| name == critical) {
                let reason = format!("critical sidecar {critical} is missing");
                let _ = self.log_supervisor_event("critical_missing", json!({ "reason": reason }));
                self.shutdown();
                return Ok(SidecarRecovery::FullRestart { reason });
            }
        }

        let mut restarted = Vec::new();
        for name in ["worker", "web"] {
            if !self
                .children
                .iter()
                .any(|(child_name, _child)| child_name == name)
            {
                let reason = exited
                    .iter()
                    .find(|(exited_name, _status)| exited_name == name)
                    .map(|(_exited_name, status)| format!("sidecar exited with {status}"))
                    .unwrap_or_else(|| "sidecar missing from supervisor state".to_string());
                self.spawn_by_name(name)?;
                self.record_restart(name, &reason);
                let _ = self.log_supervisor_event(
                    "sidecar_restart",
                    json!({ "name": name, "reason": reason }),
                );
                restarted.push(name.to_string());
            }
        }

        if restarted.is_empty() {
            Ok(SidecarRecovery::None)
        } else {
            Ok(SidecarRecovery::Restarted(restarted))
        }
    }

    fn spawn_redis(&mut self) -> Result<()> {
        let bin = resolve_sidecar("lumen-redis")?;
        let mut command = sidecar_command(bin);
        if let Some(dotnet_root) = resolve_runtime_dir("dotnet") {
            command
                .env("DOTNET_ROOT", dotnet_root)
                .env("DOTNET_MULTILEVEL_LOOKUP", "0");
        }
        let child = command
            .arg("--bind")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(self.runtime.redis_port.to_string())
            .arg("--auth")
            .arg("Password")
            .arg("--password")
            .arg(&self.redis_password)
            .arg("--checkpointdir")
            .arg(self.runtime.data_root.join("data/redis"))
            .arg("--aof")
            .arg("--recover")
            .stdout(log_file(&self.runtime.data_root, "redis.log")?)
            .stderr(log_file(&self.runtime.data_root, "redis.err.log")?)
            .spawn()
            .context("spawn lumen-redis")?;
        self.record_started("redis");
        let _ = self.log_supervisor_event(
            "spawn",
            json!({ "name": "redis", "pid": child.id(), "port": self.runtime.redis_port }),
        );
        self.children.push(("redis".into(), child));
        Ok(())
    }

    fn spawn_api(&mut self) -> Result<()> {
        let mut command = sidecar_command(resolve_sidecar("lumen-api")?);
        self.inject_common_env(&mut command);
        let child = command
            .env("APP_PORT", self.runtime.api_port.to_string())
            .env("LUMEN_SKIP_MIGRATION_CHECK", "1")
            .stdout(log_file(&self.runtime.data_root, "api.log")?)
            .stderr(log_file(&self.runtime.data_root, "api.err.log")?)
            .spawn()
            .context("spawn lumen-api")?;
        self.record_started("api");
        let _ = self.log_supervisor_event(
            "spawn",
            json!({ "name": "api", "pid": child.id(), "port": self.runtime.api_port }),
        );
        self.children.push(("api".into(), child));
        Ok(())
    }

    fn spawn_worker(&mut self) -> Result<()> {
        let mut command = sidecar_command(resolve_sidecar("lumen-worker")?);
        self.inject_common_env(&mut command);
        let child = command
            .env(
                "WORKER_METRICS_PORT",
                self.runtime.worker_metrics_port.to_string(),
            )
            .env("WORKER_METRICS_HOST", "127.0.0.1")
            .stdout(log_file(&self.runtime.data_root, "worker.log")?)
            .stderr(log_file(&self.runtime.data_root, "worker.err.log")?)
            .spawn()
            .context("spawn lumen-worker")?;
        self.record_started("worker");
        let _ = self.log_supervisor_event(
            "spawn",
            json!({ "name": "worker", "pid": child.id(), "port": self.runtime.worker_metrics_port }),
        );
        self.children.push(("worker".into(), child));
        Ok(())
    }

    fn spawn_web(&mut self) -> Result<()> {
        let server = resolve_web_root()?.join("server.js");
        let node = resolve_node_bin()?;
        let child = sidecar_command(node)
            .arg(server)
            .env("NODE_ENV", "production")
            .env("PORT", self.runtime.web_port.to_string())
            .env(
                "LUMEN_BACKEND_URL",
                format!("http://127.0.0.1:{}", self.runtime.api_port),
            )
            .env("LUMEN_LOCAL_TOKEN", &self.local_token)
            .env("NEXT_PUBLIC_LUMEN_RUNTIME", "desktop")
            .stdout(log_file(&self.runtime.data_root, "web.log")?)
            .stderr(log_file(&self.runtime.data_root, "web.err.log")?)
            .spawn()
            .context("spawn lumen-web")?;
        self.record_started("web");
        let _ = self.log_supervisor_event(
            "spawn",
            json!({ "name": "web", "pid": child.id(), "port": self.runtime.web_port }),
        );
        self.children.push(("web".into(), child));
        Ok(())
    }

    fn spawn_by_name(&mut self, name: &str) -> Result<()> {
        match name {
            "redis" => self.spawn_redis(),
            "api" => self.spawn_api(),
            "worker" => self.spawn_worker(),
            "web" => self.spawn_web(),
            _ => Err(anyhow!("unknown sidecar: {name}")),
        }
    }

    fn inject_common_env(&self, command: &mut Command) {
        command
            .env("LUMEN_RUNTIME", "desktop")
            .env("LUMEN_DATA_ROOT", &self.runtime.data_root)
            .env("LUMEN_LOCAL_TOKEN", &self.local_token)
            .env("APP_ENV", "desktop")
            .env("DATABASE_URL", sqlite_url(&self.runtime.data_root))
            .env("STORAGE_ROOT", self.runtime.data_root.join("data/storage"))
            .env(
                "REDIS_URL",
                format!(
                    "redis://:{}@127.0.0.1:{}/0",
                    self.redis_password, self.runtime.redis_port
                ),
            )
            .env(
                "LUMEN_DESKTOP_PROVIDER_FILE",
                &self.runtime.provider_runtime_file,
            )
            .stdin(Stdio::null());
    }

    fn record_started(&mut self, name: &str) {
        self.started_at_ms.insert(name.to_string(), unix_epoch_ms());
    }

    fn record_exit(&mut self, name: &str, status: &str) {
        self.last_exit_statuses
            .insert(name.to_string(), status.to_string());
    }

    fn record_restart(&mut self, name: &str, reason: &str) {
        *self.restart_counts.entry(name.to_string()).or_insert(0) += 1;
        self.last_restart_reasons
            .insert(name.to_string(), reason.to_string());
    }

    fn log_supervisor_event(&self, event: &str, payload: Value) -> Result<()> {
        let path = self.runtime.data_root.join("data/logs/supervisor.log");
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        let line = serde_json::to_string(&json!({
            "at_ms": unix_epoch_ms(),
            "event": event,
            "payload": payload,
        }))?;
        writeln!(file, "{line}")?;
        Ok(())
    }
}

fn is_critical_sidecar(name: &str) -> bool {
    matches!(name, "redis" | "api")
}

fn sidecar_port(runtime: &RuntimeInfo, name: &str) -> Option<u16> {
    match name {
        "redis" => Some(runtime.redis_port),
        "api" => Some(runtime.api_port),
        "worker" => Some(runtime.worker_metrics_port),
        "web" => Some(runtime.web_port),
        _ => None,
    }
}

fn sidecar_ready(runtime: &RuntimeInfo, name: &str) -> bool {
    match name {
        "redis" => tcp_port_open(runtime.redis_port),
        "api" => http_ok_sync(runtime.api_port, "/system/desktop-ready"),
        "worker" => http_ok_sync(runtime.worker_metrics_port, "/metrics"),
        "web" => http_ok_sync(runtime.web_port, "/"),
        _ => false,
    }
}

fn sidecar_log_path(data_root: &Path, name: &str, stderr: bool) -> PathBuf {
    let suffix = if stderr { ".err.log" } else { ".log" };
    data_root.join("data/logs").join(format!("{name}{suffix}"))
}

fn unix_epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

fn sidecar_command(bin: PathBuf) -> Command {
    let mut command = Command::new(bin);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
}

fn terminate_child(child: &mut Child) {
    #[cfg(unix)]
    {
        signal_unix_process_group(child.id(), "TERM");
        if wait_for_child_exit(child, Duration::from_secs(2)) {
            return;
        }
        signal_unix_process_group(child.id(), "KILL");
    }
    #[cfg(windows)]
    {
        terminate_windows_process_tree(child.id());
    }
    let _ = child.kill();
    let _ = wait_for_child_exit(child, Duration::from_secs(1));
}

fn cleanup_exited_child(child: &mut Child) {
    #[cfg(unix)]
    {
        signal_unix_process_group(child.id(), "TERM");
    }
    let _ = child.wait();
}

fn wait_for_child_exit(child: &mut Child, duration: Duration) -> bool {
    let deadline = std::time::Instant::now() + duration;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => {}
            Err(_) => return true,
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(unix)]
fn signal_unix_process_group(pid: u32, signal: &str) {
    let _ = Command::new("kill")
        .arg(format!("-{signal}"))
        .arg(format!("-{pid}"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(windows)]
fn terminate_windows_process_tree(pid: u32) {
    let pid = pid.to_string();
    let _ = Command::new("taskkill")
        .arg("/PID")
        .arg(&pid)
        .arg("/T")
        .arg("/F")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn random_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn sqlite_url(data_root: &Path) -> String {
    format!(
        "sqlite+aiosqlite:///{}",
        data_root.join("data/db/lumen.sqlite").display()
    )
}

fn read_provider_config_metadata(path: &Path) -> Result<(Vec<ProviderMetadata>, Vec<Value>)> {
    let raw = match fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Ok((Vec::new(), Vec::new()))
        }
        Err(err) => return Err(err).context("read provider metadata"),
    };
    if raw.trim().is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }
    let value: Value = serde_json::from_str(&raw).context("parse provider metadata")?;
    let (providers_value, proxies_value) = if let Some(items) = value.get("providers") {
        (
            items.clone(),
            value.get("proxies").cloned().unwrap_or_else(|| json!([])),
        )
    } else {
        (value, json!([]))
    };
    let providers = serde_json::from_value(providers_value).context("decode provider metadata")?;
    let proxies = match proxies_value {
        Value::Array(items) => items
            .into_iter()
            .filter(|value| value.is_object())
            .collect(),
        _ => Vec::new(),
    };
    Ok((providers, proxies))
}

fn provider_secret_map(items: &[ProviderMetadata]) -> BTreeMap<String, String> {
    items
        .iter()
        .filter_map(|item| {
            let value = item.extra.get("api_key")?.as_str()?.trim();
            (!value.is_empty()).then(|| (item.name.clone(), value.to_string()))
        })
        .collect()
}

fn proxy_secret_map(items: &[Value]) -> BTreeMap<String, String> {
    items
        .iter()
        .filter_map(|item| {
            let Value::Object(row) = item else {
                return None;
            };
            let name = row.get("name")?.as_str()?.trim();
            let password = row.get("password")?.as_str()?.trim();
            (!name.is_empty() && !password.is_empty())
                .then(|| (name.to_string(), password.to_string()))
        })
        .collect()
}

pub fn resolve_sidecar(name: &str) -> Result<PathBuf> {
    let exe = std::env::current_exe().context("locate current executable")?;
    let dir = exe
        .parent()
        .ok_or_else(|| anyhow!("executable has no parent directory"))?;
    let executable_name = if cfg!(target_os = "windows") {
        format!("{name}.exe")
    } else {
        name.to_string()
    };
    let mut candidates = vec![
        dir.join(name),
        dir.join(format!("{name}.exe")),
        dir.join(format!("{name}.cmd")),
        dir.join("binaries").join(name),
        dir.join("binaries").join(format!("{name}.exe")),
        dir.join("binaries").join(format!("{name}.cmd")),
        dir.join("resources")
            .join("runtime")
            .join(name)
            .join(&executable_name),
        dir.join("../resources")
            .join("runtime")
            .join(name)
            .join(&executable_name),
        dir.join("../../resources")
            .join("runtime")
            .join(name)
            .join(&executable_name),
    ];
    if cfg!(target_os = "macos") {
        candidates.push(dir.join("../Resources").join(name));
        candidates.push(
            dir.join("../Resources/resources")
                .join("runtime")
                .join(name)
                .join(&executable_name),
        );
    }
    for path in candidates {
        if path.is_file() {
            return Ok(path);
        }
    }
    Err(anyhow!("missing sidecar binary: {name}"))
}

fn resolve_runtime_dir(name: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let mut candidates = vec![
        dir.join("resources").join("runtime").join(name),
        dir.join("../resources").join("runtime").join(name),
        dir.join("../../resources").join("runtime").join(name),
    ];
    if cfg!(target_os = "macos") {
        candidates.push(
            dir.join("../Resources/resources")
                .join("runtime")
                .join(name),
        );
    }
    candidates.into_iter().find(|path| path.is_dir())
}

fn resolve_web_root() -> Result<PathBuf> {
    if let Ok(raw) = env::var("LUMEN_WEB_ROOT") {
        let path = PathBuf::from(raw);
        if has_web_server(&path) {
            return Ok(path);
        }
    }
    let exe = std::env::current_exe().context("locate current executable")?;
    let dir = exe
        .parent()
        .ok_or_else(|| anyhow!("executable has no parent directory"))?;
    for path in [
        dir.join("resources/web"),
        dir.join("../resources/web"),
        dir.join("../Resources/resources/web"),
        dir.join("../../resources/web"),
    ] {
        if has_web_server(&path) {
            return Ok(path);
        }
    }
    Err(anyhow!("missing bundled web server resources"))
}

fn has_web_server(path: &Path) -> bool {
    path.join("server.js").is_file()
}

fn resolve_node_bin() -> Result<PathBuf> {
    if let Ok(raw) = env::var("LUMEN_NODE_BIN") {
        let path = PathBuf::from(raw);
        if path.is_file() {
            return Ok(path);
        }
    }
    let node_name = if cfg!(target_os = "windows") {
        "node.exe"
    } else {
        "node"
    };
    if let Some(node_dir) = resolve_runtime_dir("node") {
        let node = node_dir.join(node_name);
        if node.is_file() {
            return Ok(node);
        }
    }
    Err(anyhow!("missing bundled Node runtime"))
}

fn log_file(data_root: &Path, name: &str) -> Result<Stdio> {
    let path = data_root.join("data/logs").join(name);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    Ok(Stdio::from(file))
}

async fn wait_for_redis(port: u16, password: &str, duration: Duration) -> Result<()> {
    let password = password.to_string();
    let deadline = timeout(duration, async move {
        loop {
            if try_redis_ping(port, &password).await.unwrap_or(false) {
                return Ok(());
            }
            sleep(Duration::from_millis(200)).await;
        }
    });
    deadline
        .await
        .map_err(|_| anyhow!("redis sidecar {port} did not become ready"))?
}

async fn try_redis_ping(port: u16, password: &str) -> Result<bool> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await?;
    let auth = format!(
        "*2\r\n$4\r\nAUTH\r\n${}\r\n{}\r\n*1\r\n$4\r\nPING\r\n",
        password.len(),
        password
    );
    stream.write_all(auth.as_bytes()).await?;
    let mut response = Vec::with_capacity(512);
    let mut buf = [0_u8; 256];
    for _ in 0..4 {
        let Ok(read_result) = timeout(Duration::from_millis(250), stream.read(&mut buf)).await
        else {
            break;
        };
        let n = read_result?;
        if n == 0 {
            break;
        }
        response.extend_from_slice(&buf[..n]);
        if response.windows(5).any(|chunk| chunk == b"+PONG") {
            return Ok(true);
        }
    }
    Ok(false)
}

async fn wait_for_http_ok(
    port: u16,
    path: &str,
    duration: Duration,
    headers: &[(&str, &str)],
) -> Result<()> {
    let path = path.to_string();
    let display_path = path.clone();
    let headers = headers
        .iter()
        .map(|(name, value)| ((*name).to_string(), (*value).to_string()))
        .collect::<Vec<_>>();
    let deadline = timeout(duration, async move {
        loop {
            if try_http_ok(port, &path, &headers).await.unwrap_or(false) {
                return Ok(());
            }
            sleep(Duration::from_millis(250)).await;
        }
    });
    deadline
        .await
        .map_err(|_| anyhow!("http sidecar {port}{display_path} did not become ready"))?
}

async fn try_http_ok(port: u16, path: &str, headers: &[(String, String)]) -> Result<bool> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await?;
    let mut request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    for (name, value) in headers {
        request.push_str(name);
        request.push_str(": ");
        request.push_str(value);
        request.push_str("\r\n");
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).await?;

    let mut buf = [0_u8; 256];
    let n = stream.read(&mut buf).await?;
    let head = String::from_utf8_lossy(&buf[..n]);
    Ok(head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200"))
}

fn tcp_port_open(port: u16) -> bool {
    let Ok(addr) = format!("127.0.0.1:{port}").parse() else {
        return false;
    };
    StdTcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

fn http_ok_sync(port: u16, path: &str) -> bool {
    let Ok(mut stream) = StdTcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(400)));
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0_u8; 256];
    let Ok(n) = stream.read(&mut buf) else {
        return false;
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

fn redis_info_sync(port: u16, password: &str) -> Result<String> {
    let mut stream = StdTcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    let auth = format!(
        "*2\r\n$4\r\nAUTH\r\n${}\r\n{}\r\n*1\r\n$4\r\nINFO\r\n",
        password.len(),
        password
    );
    stream.write_all(auth.as_bytes())?;
    let mut response = Vec::new();
    let mut buf = [0_u8; 4096];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                response.extend_from_slice(&buf[..n]);
                if response.windows(5).any(|chunk| chunk == b"\r\n# ") {
                    break;
                }
            }
            Err(err)
                if matches!(
                    err.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                break;
            }
            Err(err) => return Err(err.into()),
        }
    }
    let response = String::from_utf8_lossy(&response).into_owned();
    if let Some((_, body)) = response.split_once("\r\n#") {
        Ok(format!("#{body}").trim_end_matches("\r\n").to_string())
    } else {
        Ok(response)
    }
}

#[cfg(unix)]
fn process_rss_bytes(pid: u32) -> Option<u64> {
    let output = Command::new("ps")
        .args(["-o", "rss=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let raw = String::from_utf8_lossy(&output.stdout);
    let kb = raw.trim().parse::<u64>().ok()?;
    Some(kb.saturating_mul(1024))
}

#[cfg(windows)]
fn process_rss_bytes(pid: u32) -> Option<u64> {
    let output = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!("(Get-Process -Id {pid}).WorkingSet64"),
        ])
        .output()
        .ok()?;
    let raw = String::from_utf8_lossy(&output.stdout);
    raw.trim().parse::<u64>().ok()
}

#[cfg(not(any(unix, windows)))]
fn process_rss_bytes(_pid: u32) -> Option<u64> {
    None
}
