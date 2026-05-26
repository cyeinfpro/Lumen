#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backup;
mod diagnostics;
mod docker_import;
mod secrets;
mod sidecar;

use anyhow::Context;
use backup::{DesktopBackupOut, DesktopRestorePlanOut, PendingRestoreStatus};
use diagnostics::{DiagnosticBundleOut, DiagnosticSnapshot};
use docker_import::{DesktopDockerImportPlanOut, PendingDockerImportStatus};
use serde::Serialize;
use serde_json::json;
use sidecar::{RuntimeInfo, SidecarRecovery, SidecarStatus, Supervisor};
use std::env;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_updater::UpdaterExt;

#[derive(Clone)]
struct DesktopState {
    supervisor: Arc<Mutex<Supervisor>>,
    startup: Arc<Mutex<StartupState>>,
}

#[derive(Debug, Serialize)]
struct DesktopStatus {
    runtime: RuntimeInfo,
    sidecar_count: usize,
    sidecars: Vec<SidecarStatus>,
}

#[derive(Debug, Serialize)]
struct UpdateCheckOut {
    available: bool,
    current_version: String,
    version: Option<String>,
    date: Option<String>,
    body: Option<String>,
    target: Option<String>,
    download_url: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct DesktopStartupError {
    message: String,
    data_root: PathBuf,
    logs_root: PathBuf,
    at_ms: u128,
}

#[derive(Debug, Clone, Default)]
struct StartupState {
    starting: bool,
    ready: bool,
    error: Option<DesktopStartupError>,
}

#[derive(Debug, Serialize)]
struct DesktopStartupStatus {
    starting: bool,
    ready: bool,
    error: Option<DesktopStartupError>,
}

fn default_data_root(app: &tauri::AppHandle) -> anyhow::Result<PathBuf> {
    Ok(app.path().app_data_dir().context("resolve app data dir")?)
}

#[tauri::command]
fn desktop_startup_status(state: State<'_, DesktopState>) -> Result<DesktopStartupStatus, String> {
    let guard = state
        .startup
        .lock()
        .map_err(|_| "startup state poisoned".to_string())?;
    Ok(DesktopStartupStatus {
        starting: guard.starting,
        ready: guard.ready,
        error: guard.error.clone(),
    })
}

#[tauri::command]
fn desktop_status(state: State<'_, DesktopState>) -> Result<DesktopStatus, String> {
    let mut guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    let sidecars = guard.sidecar_statuses();
    Ok(DesktopStatus {
        runtime: guard.runtime.clone(),
        sidecar_count: sidecars.len(),
        sidecars,
    })
}

#[tauri::command]
fn set_provider_key(provider: String, api_key: String) -> Result<(), String> {
    secrets::set_provider_key(provider.trim(), api_key.trim()).map_err(|err| err.to_string())
}

#[tauri::command]
fn set_proxy_secret(proxy: String, password: String) -> Result<(), String> {
    secrets::set_proxy_password(proxy.trim(), password.trim()).map_err(|err| err.to_string())
}

#[tauri::command]
fn refresh_provider_runtime(state: State<'_, DesktopState>) -> Result<(), String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    guard
        .refresh_provider_runtime()
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn open_data_dir(state: State<'_, DesktopState>) -> Result<(), String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    open_path(&guard.runtime.data_root)
}

#[tauri::command]
fn diagnostics_snapshot(state: State<'_, DesktopState>) -> Result<DiagnosticSnapshot, String> {
    let mut guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    let sidecar_count = guard.sidecar_statuses().len();
    Ok(DiagnosticSnapshot {
        data_root: guard.runtime.data_root.clone(),
        logs_root: guard.runtime.data_root.join("data/logs"),
        provider_runtime_file: guard.runtime.provider_runtime_file.clone(),
        sidecar_count,
    })
}

#[tauri::command]
fn export_diagnostics_bundle(
    state: State<'_, DesktopState>,
) -> Result<DiagnosticBundleOut, String> {
    let mut guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    let sidecars = guard.sidecar_statuses();
    let redis_info = guard.redis_info();
    let metadata = json!({
        "created_at_ms": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_millis())
            .unwrap_or(0),
        "app_version": env!("CARGO_PKG_VERSION"),
        "target_os": std::env::consts::OS,
        "target_arch": std::env::consts::ARCH,
        "runtime": guard.runtime.clone(),
        "sidecars": sidecars,
    });
    diagnostics::create_diagnostic_bundle(
        &guard.runtime.data_root,
        &metadata,
        redis_info.as_deref(),
    )
    .map_err(|err| err.to_string())
}

#[tauri::command]
fn export_desktop_backup(state: State<'_, DesktopState>) -> Result<DesktopBackupOut, String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    backup::create_desktop_backup(&guard.runtime.data_root, env!("CARGO_PKG_VERSION"))
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn desktop_restore_status(state: State<'_, DesktopState>) -> Result<PendingRestoreStatus, String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    Ok(backup::pending_restore_status(&guard.runtime.data_root))
}

#[tauri::command]
fn select_desktop_restore_backup(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<Option<DesktopRestorePlanOut>, String> {
    let Some(path) = app
        .dialog()
        .file()
        .add_filter("Lumen Backup", &["zip"])
        .blocking_pick_file()
        .and_then(|path| path.into_path().ok())
    else {
        return Ok(None);
    };
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    backup::schedule_restore_backup(&guard.runtime.data_root, &path)
        .map(Some)
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn desktop_docker_import_status(
    state: State<'_, DesktopState>,
) -> Result<PendingDockerImportStatus, String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    Ok(docker_import::pending_docker_import_status(
        &guard.runtime.data_root,
    ))
}

#[tauri::command]
fn select_docker_import_backup(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
    user_id: Option<String>,
) -> Result<Option<DesktopDockerImportPlanOut>, String> {
    let Some(dump_path) = app
        .dialog()
        .file()
        .add_filter("Lumen Docker Export", &["dump", "sql"])
        .blocking_pick_file()
        .and_then(|path| path.into_path().ok())
    else {
        return Ok(None);
    };
    let storage_path = app
        .dialog()
        .file()
        .add_filter("Lumen Storage Archive", &["gz", "tgz", "tar"])
        .blocking_pick_file()
        .and_then(|path| path.into_path().ok());
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    docker_import::schedule_docker_import(
        &guard.runtime.data_root,
        &dump_path,
        storage_path.as_deref(),
        user_id,
    )
    .map(Some)
    .map_err(|err| err.to_string())
}

#[tauri::command]
fn restart_desktop_app(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    let mut guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    guard.shutdown();
    drop(guard);
    app.request_restart();
    Ok(())
}

#[tauri::command]
fn retry_desktop_startup(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    {
        let guard = state
            .startup
            .lock()
            .map_err(|_| "startup state poisoned".to_string())?;
        if guard.starting || guard.ready {
            return Ok(());
        }
    }
    spawn_desktop_runtime(app, state.inner().clone());
    Ok(())
}

#[tauri::command]
async fn check_desktop_update(app: tauri::AppHandle) -> Result<UpdateCheckOut, String> {
    let current_version = env!("CARGO_PKG_VERSION").to_string();
    let updater = app.updater().map_err(|err| err.to_string())?;
    match updater.check().await.map_err(|err| err.to_string())? {
        Some(update) => Ok(UpdateCheckOut {
            available: true,
            current_version: update.current_version,
            version: Some(update.version),
            date: update.date.map(|date| date.to_string()),
            body: update.body,
            target: Some(update.target),
            download_url: Some(update.download_url.to_string()),
        }),
        None => Ok(UpdateCheckOut {
            available: false,
            current_version,
            version: None,
            date: None,
            body: None,
            target: None,
            download_url: None,
        }),
    }
}

#[tauri::command]
async fn install_desktop_update(app: tauri::AppHandle) -> Result<bool, String> {
    let updater = app.updater().map_err(|err| err.to_string())?;
    let Some(update) = updater.check().await.map_err(|err| err.to_string())? else {
        return Ok(false);
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|err| err.to_string())?;
    app.restart();
}

fn open_path(path: &PathBuf) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let result = std::process::Command::new("open").arg(path).spawn();
    #[cfg(target_os = "windows")]
    let result = std::process::Command::new("explorer").arg(path).spawn();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let result = std::process::Command::new("xdg-open").arg(path).spawn();
    result.map(|_| ()).map_err(|err| err.to_string())
}

fn main() {
    if env::var_os("LUMEN_DESKTOP_HEADLESS_SMOKE").is_some() {
        if let Err(err) = run_headless_smoke() {
            eprintln!("desktop headless smoke failed: {err:#}");
            std::process::exit(1);
        }
        return;
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let data_root = default_data_root(app.handle())?;
            let restore_pending = backup::pending_restore_status(&data_root).pending;
            let docker_import_pending =
                docker_import::pending_docker_import_status(&data_root).pending;
            if restore_pending && docker_import_pending {
                eprintln!(
                    "desktop has both pending restore and pending Docker import; leaving both untouched"
                );
            } else {
                if let Err(err) =
                    backup::apply_pending_restore(&data_root, env!("CARGO_PKG_VERSION"))
                {
                    eprintln!("desktop pending restore failed: {err:#}");
                }
                if let Err(err) =
                    docker_import::apply_pending_docker_import(&data_root, env!("CARGO_PKG_VERSION"))
                {
                    eprintln!("desktop pending Docker import failed: {err:#}");
                }
            }
            let supervisor = Supervisor::new(data_root)?;
            let state = DesktopState {
                supervisor: Arc::new(Mutex::new(supervisor)),
                startup: Arc::new(Mutex::new(StartupState::default())),
            };
            spawn_desktop_runtime(app.handle().clone(), state.clone());
            app.manage(state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_startup_status,
            desktop_status,
            set_provider_key,
            set_proxy_secret,
            refresh_provider_runtime,
            open_data_dir,
            diagnostics_snapshot,
            export_diagnostics_bundle,
            export_desktop_backup,
            desktop_restore_status,
            select_desktop_restore_backup,
            desktop_docker_import_status,
            select_docker_import_backup,
            restart_desktop_app,
            retry_desktop_startup,
            check_desktop_update,
            install_desktop_update
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Lumen desktop");
}

fn run_headless_smoke() -> anyhow::Result<()> {
    let data_root = env::var_os("LUMEN_DATA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            env::temp_dir().join(format!("lumen-desktop-smoke-{}", unix_epoch_ms()))
        });
    let mut supervisor = Supervisor::new(data_root)?;
    tauri::async_runtime::block_on(supervisor.spawn_all())?;
    let supervisor = Arc::new(Mutex::new(supervisor));
    let monitor = supervisor.clone();
    std::thread::spawn(move || {
        let mut last_heartbeat = Instant::now();
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let mut guard = match monitor.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            match guard.recover_exited() {
                Ok(SidecarRecovery::None) => {}
                Ok(SidecarRecovery::Restarted(names)) => {
                    eprintln!("desktop sidecar restarted: {}", names.join(", "));
                }
                Ok(SidecarRecovery::FullRestart { reason }) => {
                    eprintln!("desktop sidecar full restart: {reason}");
                    guard.note_full_restart(&reason);
                    if let Err(err) = tauri::async_runtime::block_on(guard.spawn_all()) {
                        eprintln!("desktop sidecar full restart failed: {err:#}");
                    }
                }
                Err(err) => {
                    eprintln!("desktop sidecar monitor failed: {err:#}");
                }
            }
            if last_heartbeat.elapsed() >= Duration::from_secs(5) {
                if let Err(err) = guard.write_heartbeat() {
                    eprintln!("desktop sidecar heartbeat failed: {err:#}");
                }
                last_heartbeat = Instant::now();
            }
        }
    });

    loop {
        std::thread::sleep(Duration::from_secs(60));
        drop(
            supervisor
                .lock()
                .map_err(|_| anyhow::anyhow!("state poisoned"))?,
        );
    }
}

fn spawn_desktop_runtime(app_handle: tauri::AppHandle, state: DesktopState) {
    set_startup_starting(&state);
    std::thread::spawn(move || {
        let web_port = {
            let mut supervisor = match state.supervisor.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            supervisor.shutdown();
            match tauri::async_runtime::block_on(supervisor.spawn_all()) {
                Ok(()) => supervisor.runtime.web_port,
                Err(err) => {
                    let message = format!("{err:#}");
                    eprintln!("desktop sidecar startup failed: {message}");
                    supervisor.note_startup_failure(&message);
                    let data_root = supervisor.runtime.data_root.clone();
                    supervisor.shutdown();
                    set_startup_error(&state, message, data_root);
                    return;
                }
            }
        };
        set_startup_ready(&state);
        if let Some(window) = app_handle.get_webview_window("main") {
            if let Ok(url) = format!("http://127.0.0.1:{web_port}").parse::<tauri::Url>() {
                let _ = window.navigate(url);
            }
        }
        start_sidecar_monitor(state);
    });
}

fn set_startup_starting(state: &DesktopState) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = true;
        guard.ready = false;
        guard.error = None;
    }
}

fn set_startup_ready(state: &DesktopState) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = false;
        guard.ready = true;
        guard.error = None;
    }
}

fn set_startup_error(state: &DesktopState, message: String, data_root: PathBuf) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = false;
        guard.ready = false;
        guard.error = Some(DesktopStartupError {
            message,
            logs_root: data_root.join("data/logs"),
            data_root,
            at_ms: unix_epoch_ms(),
        });
    }
}

fn start_sidecar_monitor(state: DesktopState) {
    std::thread::spawn(move || {
        let mut last_heartbeat = Instant::now();
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let recovery = {
                let mut supervisor = match state.supervisor.lock() {
                    Ok(guard) => guard,
                    Err(_) => return,
                };
                let recovery = supervisor.recover_exited();
                if last_heartbeat.elapsed() >= Duration::from_secs(5) {
                    if let Err(err) = supervisor.write_heartbeat() {
                        eprintln!("desktop sidecar heartbeat failed: {err:#}");
                    }
                    last_heartbeat = Instant::now();
                }
                recovery
            };
            match recovery {
                Ok(SidecarRecovery::None) => {}
                Ok(SidecarRecovery::Restarted(names)) => {
                    eprintln!("desktop sidecar restarted: {}", names.join(", "));
                }
                Ok(SidecarRecovery::FullRestart { reason }) => {
                    eprintln!("desktop sidecar full restart: {reason}");
                    let mut supervisor = match state.supervisor.lock() {
                        Ok(guard) => guard,
                        Err(_) => return,
                    };
                    supervisor.note_full_restart(&reason);
                    if let Err(err) = tauri::async_runtime::block_on(supervisor.spawn_all()) {
                        eprintln!("desktop sidecar full restart failed: {err:#}");
                    }
                }
                Err(err) => {
                    eprintln!("desktop sidecar monitor failed: {err:#}");
                }
            }
        }
    });
}

fn unix_epoch_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}
