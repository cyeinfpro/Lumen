#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backup;
mod diagnostics;
mod docker_import;
mod power;
mod secrets;
mod sidecar;

use anyhow::Context;
use backup::{DesktopBackupOut, DesktopRestorePlanOut, PendingRestoreStatus};
use diagnostics::{DiagnosticBundleOut, DiagnosticSnapshot};
use docker_import::{DesktopDockerImportPlanOut, PendingDockerImportStatus};
use power::SleepGuard;
use serde::Serialize;
use serde_json::json;
use sidecar::{RuntimeInfo, SidecarRecovery, SidecarStatus, Supervisor};
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};
use std::{env, fs};
use tauri::menu::MenuBuilder;
use tauri::path::BaseDirectory;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_updater::UpdaterExt;

const TRAY_SHOW_ID: &str = "show";
const TRAY_QUIT_ID: &str = "quit";

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

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum UpdateInstallStatus {
    NoUpdate,
    Installing { version: String },
}

#[derive(Debug, Clone, Serialize)]
struct UpdateProgressOut {
    downloaded: u64,
    total: Option<u64>,
    percent: Option<f64>,
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
    phase: String,
    error: Option<DesktopStartupError>,
}

#[derive(Debug, Serialize)]
struct DesktopStartupStatus {
    starting: bool,
    ready: bool,
    phase: String,
    error: Option<DesktopStartupError>,
}

fn default_data_root(app: &tauri::AppHandle) -> anyhow::Result<PathBuf> {
    if let Some(raw) = env::var_os("LUMEN_DATA_ROOT") {
        let path = PathBuf::from(raw);
        fs::create_dir_all(&path)
            .with_context(|| format!("create LUMEN_DATA_ROOT {}", path.display()))?;
        let probe = path.join(".lumen-write-probe");
        fs::write(&probe, b"ok")
            .with_context(|| format!("write LUMEN_DATA_ROOT probe {}", probe.display()))?;
        let _ = fs::remove_file(probe);
        return Ok(path);
    }
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
        phase: guard.phase.clone(),
        error: guard.error.clone(),
    })
}

#[tauri::command]
async fn desktop_status(state: State<'_, DesktopState>) -> Result<DesktopStatus, String> {
    let supervisor = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = supervisor
            .lock()
            .map_err(|_| "state poisoned".to_string())?;
        let sidecars = guard.sidecar_statuses();
        Ok(DesktopStatus {
            runtime: guard.runtime.clone(),
            sidecar_count: sidecars.len(),
            sidecars,
        })
    })
    .await
    .map_err(|err| err.to_string())?
}

#[tauri::command]
fn set_provider_key(
    provider: String,
    api_key: String,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    let provider = provider.trim().to_string();
    if provider.is_empty() {
        return Err("provider name is required".to_string());
    }
    let data_root = {
        let guard = state
            .supervisor
            .lock()
            .map_err(|_| "state poisoned".to_string())?;
        guard.runtime.data_root.clone()
    };
    secrets::set_provider_key(&data_root, &provider, api_key.trim()).map_err(|err| err.to_string())
}

#[tauri::command]
fn set_proxy_secret(
    proxy: String,
    password: String,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    let proxy = proxy.trim().to_string();
    if proxy.is_empty() {
        return Err("proxy name is required".to_string());
    }
    let data_root = {
        let guard = state
            .supervisor
            .lock()
            .map_err(|_| "state poisoned".to_string())?;
        guard.runtime.data_root.clone()
    };
    secrets::set_proxy_password(&data_root, &proxy, password.trim()).map_err(|err| err.to_string())
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
fn clear_failed_restore_marker(state: State<'_, DesktopState>) -> Result<(), String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    backup::clear_failed_restore_marker(&guard.runtime.data_root).map_err(|err| err.to_string())
}

#[tauri::command]
fn clear_pending_restore(state: State<'_, DesktopState>) -> Result<(), String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    backup::clear_pending_restore(&guard.runtime.data_root).map_err(|err| err.to_string())
}

#[tauri::command]
fn clear_failed_docker_import_marker(state: State<'_, DesktopState>) -> Result<(), String> {
    let guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    docker_import::clear_failed_docker_import_marker(&guard.runtime.data_root)
        .map_err(|err| err.to_string())
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
    guard.shutdown_with_timeout(Duration::from_secs(5));
    drop(guard);
    app.request_restart();
    Ok(())
}

#[tauri::command]
fn quit_desktop_app(app: tauri::AppHandle, state: State<'_, DesktopState>) -> Result<(), String> {
    request_desktop_exit(&app, &state)
}

#[tauri::command]
fn retry_desktop_startup(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    {
        let mut guard = state
            .startup
            .lock()
            .map_err(|_| "startup state poisoned".to_string())?;
        if guard.starting || guard.ready {
            guard.error = None;
            return Ok(());
        }
    }
    if let Ok(mut guard) = state.supervisor.lock() {
        guard.reset_recovery_state();
    }
    set_startup_starting(&state);
    spawn_desktop_preflight_and_runtime(app, state.inner().clone());
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
async fn install_desktop_update(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<UpdateInstallStatus, String> {
    let updater = app.updater().map_err(|err| err.to_string())?;
    let Some(update) = updater.check().await.map_err(|err| err.to_string())? else {
        return Ok(UpdateInstallStatus::NoUpdate);
    };
    let version = update.version.clone();
    let finished_version = version.clone();
    let progress_app = app.clone();
    let result = update
        .download_and_install(
            move |downloaded, total| {
                let downloaded = downloaded as u64;
                let percent = total.map(|total| downloaded as f64 / total as f64 * 100.0);
                let _ = progress_app.emit(
                    "update://progress",
                    UpdateProgressOut {
                        downloaded,
                        total,
                        percent,
                    },
                );
            },
            {
                let app = app.clone();
                move || {
                    let _ = app.emit("update://finished", json!({ "version": finished_version }));
                }
            },
        )
        .await;
    if let Err(err) = result {
        let message = err.to_string();
        if let Ok(guard) = state.supervisor.lock() {
            guard.note_update_failed(&message);
        }
        if let Ok(cache_dir) = app.path().app_cache_dir() {
            let _ = fs::remove_dir_all(cache_dir.join("updater"));
        }
        return Err(message);
    }
    let status = UpdateInstallStatus::Installing { version };
    let restart_app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(150));
        restart_app.restart();
    });
    Ok(status)
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

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn request_desktop_exit(app: &tauri::AppHandle, state: &DesktopState) -> Result<(), String> {
    let mut guard = state
        .supervisor
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    guard.shutdown_with_timeout(Duration::from_secs(5));
    drop(guard);
    app.exit(0);
    Ok(())
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
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if let Some(arg) = argv.iter().skip(1).find(|arg| !arg.trim().is_empty()) {
                handle_file_open(app, arg);
            }
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
            install_desktop_tray(app)?;
            let data_root = default_data_root(app.handle())?;
            let supervisor = Supervisor::new(data_root)?;
            let state = DesktopState {
                supervisor: Arc::new(Mutex::new(supervisor)),
                startup: Arc::new(Mutex::new(StartupState::default())),
            };
            app.manage(state.clone());
            spawn_desktop_preflight_and_runtime(app.handle().clone(), state);
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
            clear_failed_restore_marker,
            clear_pending_restore,
            select_desktop_restore_backup,
            desktop_docker_import_status,
            clear_failed_docker_import_marker,
            select_docker_import_backup,
            restart_desktop_app,
            retry_desktop_startup,
            check_desktop_update,
            install_desktop_update,
            quit_desktop_app
        ])
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run Lumen desktop");
}

fn handle_file_open(app: &tauri::AppHandle, arg: &str) {
    let _ = app.emit("desktop://file-open", json!({ "path": arg }));
}

fn install_desktop_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let menu = MenuBuilder::new(app)
        .text(TRAY_SHOW_ID, "显示 Lumen")
        .text(TRAY_QUIT_ID, "退出 Lumen")
        .build()?;
    let mut builder = TrayIconBuilder::with_id("main")
        .menu(&menu)
        .tooltip("Lumen")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            TRAY_SHOW_ID => show_main_window(app),
            TRAY_QUIT_ID => {
                if let Some(state) = app.try_state::<DesktopState>() {
                    let _ = request_desktop_exit(app, &state);
                } else {
                    app.exit(0);
                }
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            }
            | TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } => show_main_window(tray.app_handle()),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    }
    let _tray = builder.build(app)?;
    Ok(())
}

fn run_headless_smoke() -> anyhow::Result<()> {
    let data_root = env::var_os("LUMEN_DATA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            env::temp_dir().join(format!("lumen-desktop-smoke-{}", unix_epoch_ms()))
        });
    let mut supervisor = Supervisor::new(data_root)?;
    spawn_all_with_timeout(&mut supervisor, Duration::from_secs(90))?;
    let activity = supervisor
        .desktop_activity()?
        .ok_or_else(|| anyhow::anyhow!("desktop activity endpoint did not return 200"))?;
    if activity.should_keep_awake() {
        return Err(anyhow::anyhow!(
            "desktop activity endpoint reported active tasks before smoke workload"
        ));
    }
    run_headless_command_smoke(&mut supervisor)?;
    let supervisor = Arc::new(Mutex::new(supervisor));
    let monitor = supervisor.clone();
    let fatal = Arc::new(AtomicBool::new(false));
    let monitor_fatal = fatal.clone();
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
                    if let Err(err) = spawn_all_with_timeout(&mut guard, Duration::from_secs(90)) {
                        eprintln!("desktop sidecar full restart failed: {err:#}");
                        monitor_fatal.store(true, Ordering::SeqCst);
                        return;
                    }
                }
                Err(err) => {
                    eprintln!("desktop sidecar monitor failed: {err:#}");
                    monitor_fatal.store(true, Ordering::SeqCst);
                    return;
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
        std::thread::sleep(Duration::from_secs(2));
        if fatal.load(Ordering::SeqCst) {
            anyhow::bail!("desktop headless smoke monitor reported a fatal sidecar state");
        }
        drop(
            supervisor
                .lock()
                .map_err(|_| anyhow::anyhow!("state poisoned"))?,
        );
    }
}

fn spawn_all_with_timeout(supervisor: &mut Supervisor, duration: Duration) -> anyhow::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("create Tokio runtime for desktop headless smoke")?;
    match runtime.block_on(async { tokio::time::timeout(duration, supervisor.spawn_all()).await }) {
        Ok(result) => result,
        Err(_) => anyhow::bail!(
            "desktop sidecar spawn_all timed out after {}s",
            duration.as_secs()
        ),
    }
}

fn run_headless_command_smoke(supervisor: &mut Supervisor) -> anyhow::Result<()> {
    let runtime = supervisor.runtime.clone();
    let statuses = supervisor.sidecar_statuses();
    if statuses.len() < 4 {
        anyhow::bail!(
            "desktop command smoke expected at least 4 sidecars, got {}",
            statuses.len()
        );
    }
    let unready = statuses
        .iter()
        .filter(|status| status.critical && !status.ready)
        .map(|status| status.name.clone())
        .collect::<Vec<_>>();
    if !unready.is_empty() {
        anyhow::bail!(
            "desktop command smoke critical sidecars were not ready: {}",
            unready.join(", ")
        );
    }

    let provider_name = "Headless Smoke Provider";
    let provider_secret = "sk-headless-command-smoke";
    let proxy_name = "Headless Smoke Proxy";
    let proxy_secret = "proxy-headless-command-smoke";
    let provider_metadata_path = runtime.data_root.join("data/providers.json");
    if let Some(parent) = provider_metadata_path.parent() {
        fs::create_dir_all(parent).context("create provider metadata parent")?;
    }
    let provider_metadata = json!({
        "providers": [
            {
                "name": provider_name,
                "base_url": "https://example.test/v1",
                "api_key": "",
                "priority": 100,
                "weight": 1,
                "enabled": true,
                "purposes": ["chat", "image"]
            }
        ],
        "proxies": [
            {
                "name": proxy_name,
                "type": "http",
                "url": "http://127.0.0.1:9",
                "password": ""
            }
        ]
    });
    fs::write(
        &provider_metadata_path,
        serde_json::to_vec_pretty(&provider_metadata)?,
    )
    .with_context(|| {
        format!(
            "write provider metadata for desktop command smoke {}",
            provider_metadata_path.display()
        )
    })?;

    secrets::set_provider_key(&runtime.data_root, provider_name, provider_secret)
        .context("set provider secret during desktop command smoke")?;
    if secrets::get_provider_key(&runtime.data_root, provider_name)?.as_deref()
        != Some(provider_secret)
    {
        anyhow::bail!("desktop command smoke provider secret did not round-trip");
    }
    secrets::set_proxy_password(&runtime.data_root, proxy_name, proxy_secret)
        .context("set proxy secret during desktop command smoke")?;
    if secrets::get_provider_key(&runtime.data_root, provider_name)?.as_deref()
        != Some(provider_secret)
    {
        anyhow::bail!("desktop command smoke provider secret was lost after proxy secret write");
    }
    if secrets::get_proxy_password(&runtime.data_root, proxy_name)?.as_deref() != Some(proxy_secret)
    {
        anyhow::bail!("desktop command smoke proxy secret did not round-trip");
    }

    supervisor
        .refresh_provider_runtime()
        .context("refresh provider runtime during desktop command smoke")?;
    let runtime_config_raw = fs::read(&runtime.provider_runtime_file).with_context(|| {
        format!(
            "read provider runtime file {}",
            runtime.provider_runtime_file.display()
        )
    })?;
    let runtime_config: serde_json::Value =
        serde_json::from_slice(&runtime_config_raw).context("parse provider runtime file")?;
    let provider_runtime_key = runtime_config
        .get("providers")
        .and_then(|value| value.as_array())
        .and_then(|items| {
            items.iter().find_map(|item| {
                (item.get("name").and_then(|value| value.as_str()) == Some(provider_name))
                    .then(|| item.get("api_key").and_then(|value| value.as_str()))
                    .flatten()
            })
        });
    if provider_runtime_key != Some(provider_secret) {
        anyhow::bail!("desktop command smoke provider runtime did not include saved key");
    }
    let proxy_runtime_password = runtime_config
        .get("proxies")
        .and_then(|value| value.as_array())
        .and_then(|items| {
            items.iter().find_map(|item| {
                (item.get("name").and_then(|value| value.as_str()) == Some(proxy_name))
                    .then(|| item.get("password").and_then(|value| value.as_str()))
                    .flatten()
            })
        });
    if proxy_runtime_password != Some(proxy_secret) {
        anyhow::bail!("desktop command smoke provider runtime did not include proxy password");
    }

    let redis_info = supervisor.redis_info();
    let diagnostics = diagnostics::create_diagnostic_bundle(
        &runtime.data_root,
        &json!({
            "command_smoke": true,
            "runtime": runtime,
            "sidecars": statuses,
        }),
        redis_info.as_deref(),
    )
    .context("create diagnostics bundle during desktop command smoke")?;
    if diagnostics.bytes == 0 || !diagnostics.path.is_file() {
        anyhow::bail!("desktop command smoke diagnostics bundle was empty or missing");
    }

    let backup =
        backup::create_desktop_backup(&supervisor.runtime.data_root, env!("CARGO_PKG_VERSION"))
            .context("create desktop backup during desktop command smoke")?;
    if backup.bytes == 0 || !backup.path.is_file() {
        anyhow::bail!("desktop command smoke backup was empty or missing");
    }
    if backup.manifest.format != "lumen-desktop-backup" || !backup.manifest.database.present {
        anyhow::bail!("desktop command smoke backup manifest did not include the SQLite database");
    }

    let restore_status = backup::pending_restore_status(&supervisor.runtime.data_root);
    if restore_status.pending || restore_status.failed {
        anyhow::bail!("desktop command smoke unexpectedly found a pending restore marker");
    }
    let docker_status = docker_import::pending_docker_import_status(&supervisor.runtime.data_root);
    if docker_status.pending || docker_status.failed {
        anyhow::bail!("desktop command smoke unexpectedly found a pending Docker import marker");
    }

    secrets::set_provider_key(&supervisor.runtime.data_root, provider_name, "")
        .context("clear provider secret during desktop command smoke")?;
    secrets::set_proxy_password(&supervisor.runtime.data_root, proxy_name, "")
        .context("clear proxy secret during desktop command smoke")?;

    let marker = supervisor
        .runtime
        .data_root
        .join("data/tmp/headless-command-smoke-ok.json");
    if let Some(parent) = marker.parent() {
        fs::create_dir_all(parent).context("create desktop command smoke marker parent")?;
    }
    fs::write(
        &marker,
        serde_json::to_vec_pretty(&json!({
            "ok": true,
            "sidecar_count": statuses.len(),
            "diagnostics_bundle_path": diagnostics.path,
            "diagnostics_bundle_bytes": diagnostics.bytes,
            "backup_path": backup.path,
            "backup_bytes": backup.bytes,
        }))?,
    )
    .with_context(|| format!("write desktop command smoke marker {}", marker.display()))?;
    println!(
        "desktop_headless_command_smoke_ok marker={}",
        marker.display()
    );
    Ok(())
}

fn spawn_desktop_preflight_and_runtime(app_handle: tauri::AppHandle, state: DesktopState) {
    set_startup_starting(&state);
    std::thread::spawn(move || {
        let data_root = match state.supervisor.lock() {
            Ok(guard) => guard.runtime.data_root.clone(),
            Err(_) => return,
        };
        let restore_pending = backup::pending_restore_status(&data_root).pending;
        let docker_import_pending = docker_import::pending_docker_import_status(&data_root).pending;
        if restore_pending && docker_import_pending {
            let message = "同时存在待恢复备份和待导入 Docker 数据，已标记为失败记录。请重试启动后在存储设置中保留其中一个任务。".to_string();
            let _ = backup::mark_pending_restore_failed(&data_root, &message);
            let _ = docker_import::mark_pending_docker_import_failed(&data_root, &message);
            set_startup_error(&state, message, data_root, Some(&app_handle));
            return;
        }
        if restore_pending {
            set_startup_phase(&state, "正在恢复本机备份");
            if let Err(err) = backup::apply_pending_restore(&data_root, env!("CARGO_PKG_VERSION")) {
                let message = format!("{err:#}");
                eprintln!("desktop pending restore failed: {message}");
                set_startup_error(&state, message, data_root, Some(&app_handle));
                return;
            }
        }
        if docker_import_pending {
            set_startup_phase(&state, "正在导入 Docker 数据");
            if let Err(err) =
                docker_import::apply_pending_docker_import(&data_root, env!("CARGO_PKG_VERSION"))
            {
                let message = format!("{err:#}");
                eprintln!("desktop pending Docker import failed: {message}");
                set_startup_error(&state, message, data_root, Some(&app_handle));
                return;
            }
        }
        spawn_desktop_runtime(app_handle, state);
    });
}

fn spawn_desktop_runtime(app_handle: tauri::AppHandle, state: DesktopState) {
    let reassign_ports = startup_has_error(&state);
    set_startup_starting(&state);
    std::thread::spawn(move || {
        let web_port = {
            let mut supervisor = match state.supervisor.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            supervisor.shutdown();
            if reassign_ports {
                set_startup_phase(&state, "重新选择本机端口");
                if let Err(err) = supervisor.reassign_ports() {
                    let message = format!("{err:#}");
                    eprintln!("desktop port reassignment failed: {message}");
                    supervisor.note_startup_failure(&message);
                    let data_root = supervisor.runtime.data_root.clone();
                    set_startup_error(&state, message, data_root, Some(&app_handle));
                    return;
                }
            }
            match tauri::async_runtime::block_on(
                supervisor.spawn_all_with_progress(|phase| set_startup_phase(&state, phase)),
            ) {
                Ok(()) => {
                    supervisor.reset_recovery_state();
                    supervisor.runtime.web_port
                }
                Err(err) => {
                    supervisor.shutdown();
                    if !is_probably_port_conflict(&err) {
                        let message = format!("{err:#}");
                        eprintln!("desktop sidecar startup failed: {message}");
                        supervisor.note_startup_failure(&message);
                        let data_root = supervisor.runtime.data_root.clone();
                        set_startup_error(&state, message, data_root, Some(&app_handle));
                        return;
                    }
                    set_startup_phase(&state, "重新选择本机端口");
                    if let Err(reassign_err) = supervisor.reassign_ports() {
                        let message =
                            format!("{err:#}; port reassignment failed: {reassign_err:#}");
                        eprintln!("desktop sidecar startup failed: {message}");
                        supervisor.note_startup_failure(&message);
                        let data_root = supervisor.runtime.data_root.clone();
                        set_startup_error(&state, message, data_root, Some(&app_handle));
                        return;
                    }
                    match tauri::async_runtime::block_on(
                        supervisor
                            .spawn_all_with_progress(|phase| set_startup_phase(&state, phase)),
                    ) {
                        Ok(()) => {
                            supervisor.reset_recovery_state();
                            supervisor.runtime.web_port
                        }
                        Err(retry_err) => {
                            let message = format!("{err:#}; retry failed: {retry_err:#}");
                            eprintln!("desktop sidecar startup failed: {message}");
                            supervisor.note_startup_failure(&message);
                            let data_root = supervisor.runtime.data_root.clone();
                            supervisor.shutdown();
                            set_startup_error(&state, message, data_root, Some(&app_handle));
                            return;
                        }
                    }
                }
            }
        };
        set_startup_ready(&state);
        if let Some(window) = app_handle.get_webview_window("main") {
            if let Ok(url) = format!("http://127.0.0.1:{web_port}").parse::<tauri::Url>() {
                let _ = window.navigate(url);
            }
        }
        start_sidecar_monitor(state, app_handle);
    });
}

fn set_startup_starting(state: &DesktopState) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = true;
        guard.ready = false;
        guard.phase = "准备本机运行时".to_string();
        guard.error = None;
    }
}

fn set_startup_ready(state: &DesktopState) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = false;
        guard.ready = true;
        guard.phase = "完成".to_string();
        guard.error = None;
    }
}

fn set_startup_error(
    state: &DesktopState,
    message: String,
    data_root: PathBuf,
    app_handle: Option<&tauri::AppHandle>,
) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.starting = false;
        guard.ready = false;
        guard.phase = "启动失败".to_string();
        guard.error = Some(DesktopStartupError {
            message,
            logs_root: data_root.join("data/logs"),
            data_root,
            at_ms: unix_epoch_ms(),
        });
    }
    if let Some(app_handle) = app_handle {
        navigate_startup_page(app_handle);
    }
}

fn set_startup_phase(state: &DesktopState, phase: impl Into<String>) {
    if let Ok(mut guard) = state.startup.lock() {
        guard.phase = phase.into();
    }
}

fn startup_has_error(state: &DesktopState) -> bool {
    state
        .startup
        .lock()
        .map(|guard| guard.error.is_some())
        .unwrap_or(false)
}

fn is_probably_port_conflict(err: &anyhow::Error) -> bool {
    let text = format!("{err:#}").to_ascii_lowercase();
    text.contains("address already in use")
        || text.contains("addrinuse")
        || text.contains("eaddrinuse")
        || text.contains("os error 48")
        || text.contains("os error 98")
        || text.contains("os error 10048")
}

fn start_sidecar_monitor(state: DesktopState, app_handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut last_heartbeat = Instant::now();
        let mut last_activity_probe = Instant::now() - Duration::from_secs(5);
        let mut activity_probe_failed = false;
        let mut sleep_protection_failed = false;
        let mut sleep_guard = SleepGuard::new();
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let (recovery, activity_probe) = {
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
                let activity_probe = if last_activity_probe.elapsed() >= Duration::from_secs(5) {
                    last_activity_probe = Instant::now();
                    Some(supervisor.desktop_activity())
                } else {
                    None
                };
                (recovery, activity_probe)
            };
            if let Some(activity_probe) = activity_probe {
                match activity_probe {
                    Ok(Some(activity)) => {
                        activity_probe_failed = false;
                        match sleep_guard.set_active(activity.should_keep_awake()) {
                            Ok(()) => sleep_protection_failed = false,
                            Err(err) if !sleep_protection_failed => {
                                eprintln!("desktop sleep protection update failed: {err:#}");
                                sleep_protection_failed = true;
                            }
                            Err(_) => {}
                        }
                    }
                    Ok(None) if !activity_probe_failed => {
                        eprintln!("desktop activity probe returned no data");
                        activity_probe_failed = true;
                    }
                    Ok(None) => {}
                    Err(err) if !activity_probe_failed => {
                        eprintln!("desktop activity probe failed: {err:#}");
                        activity_probe_failed = true;
                    }
                    Err(_) => {}
                }
            }
            match recovery {
                Ok(SidecarRecovery::None) => {}
                Ok(SidecarRecovery::Restarted(names)) => {
                    eprintln!("desktop sidecar restarted: {}", names.join(", "));
                }
                Ok(SidecarRecovery::FullRestart { reason }) => {
                    eprintln!("desktop sidecar full restart: {reason}");
                    let delay = {
                        let mut supervisor = match state.supervisor.lock() {
                            Ok(guard) => guard,
                            Err(_) => return,
                        };
                        supervisor.note_full_restart(&reason);
                        match supervisor.full_restart_backoff(&reason) {
                            Ok(delay) => delay,
                            Err(err) => {
                                let message = format!("{err:#}");
                                eprintln!("desktop sidecar full restart suppressed: {message}");
                                supervisor.note_startup_failure(&message);
                                let data_root = supervisor.runtime.data_root.clone();
                                supervisor.shutdown();
                                drop(supervisor);
                                set_startup_error(&state, message, data_root, Some(&app_handle));
                                return;
                            }
                        }
                    };
                    std::thread::sleep(delay);
                    let mut supervisor = match state.supervisor.lock() {
                        Ok(guard) => guard,
                        Err(_) => return,
                    };
                    if let Err(err) =
                        spawn_all_with_timeout(&mut supervisor, Duration::from_secs(90))
                    {
                        let message = format!("{err:#}");
                        eprintln!("desktop sidecar full restart failed: {message}");
                        supervisor.note_startup_failure(&message);
                        let data_root = supervisor.runtime.data_root.clone();
                        supervisor.shutdown();
                        drop(supervisor);
                        set_startup_error(&state, message, data_root, Some(&app_handle));
                        return;
                    }
                }
                Err(err) => {
                    eprintln!("desktop sidecar monitor failed: {err:#}");
                }
            }
        }
    });
}

fn navigate_startup_page(app_handle: &tauri::AppHandle) {
    let Some(window) = app_handle.get_webview_window("main") else {
        return;
    };
    let Ok(path) = app_handle
        .path()
        .resolve("dist/web/index.html", BaseDirectory::Resource)
    else {
        return;
    };
    if let Ok(url) = tauri::Url::from_file_path(path) {
        let _ = window.navigate(url);
    }
}

fn unix_epoch_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}
