"use client";

export const DESKTOP_RUNTIME =
  process.env.NEXT_PUBLIC_LUMEN_RUNTIME === "desktop";

export type SidecarName = "redis" | "api" | "worker" | "web";

export type DesktopRuntimeInfo = {
  data_root: string;
  api_port: number;
  web_port: number;
  redis_port: number;
  worker_metrics_port: number;
  provider_runtime_file: string;
};

export type DesktopSidecarStatus = {
  name: SidecarName;
  pid: number;
  running: boolean;
  exit_status: string | null;
  port: number | null;
  critical: boolean;
  ready: boolean;
  restart_count: number;
  started_at_ms: number | null;
  last_exit_status: string | null;
  last_restart_reason: string | null;
  rss_bytes: number | null;
  log_path: string;
  stderr_log_path: string;
};

export type DesktopStatus = {
  runtime: DesktopRuntimeInfo;
  sidecar_count: number;
  sidecars: DesktopSidecarStatus[];
};

export type DiagnosticBundle = {
  path: string;
  bytes: number;
};

export type DesktopBackupManifest = {
  format: string;
  format_version: number;
  created_at_ms: number;
  app_version: string;
  platform: string;
  arch: string;
  database: {
    present: boolean;
    path: string | null;
    bytes: number;
    sha256: string | null;
    quick_check: string | null;
  };
  entries: { path: string; bytes: number; sha256: string }[];
  excluded: string[];
};

export type DesktopBackup = {
  path: string;
  bytes: number;
  manifest: DesktopBackupManifest;
};

export type DesktopRestorePlan = {
  source_path: string;
  pending_path: string;
  requires_restart: boolean;
  manifest: DesktopBackupManifest;
};

export type DesktopRestoreStatus = {
  pending: boolean;
  failed: boolean;
  pending_path: string | null;
  source_path: string | null;
  scheduled_at_ms: number | null;
  manifest: DesktopBackupManifest | null;
  last_error: string | null;
};

export type DesktopDockerImportPlan = {
  source_dump_path: string;
  pending_dump_path: string;
  source_storage_tar_path: string | null;
  pending_storage_tar_path: string | null;
  user_id: string | null;
  requires_restart: boolean;
  scheduled_at_ms: number;
};

export type DesktopDockerImportStatus = {
  pending: boolean;
  failed: boolean;
  source_dump_path: string | null;
  pending_dump_path: string | null;
  source_storage_tar_path: string | null;
  pending_storage_tar_path: string | null;
  user_id: string | null;
  scheduled_at_ms: number | null;
  last_error: string | null;
  report: Record<string, unknown> | null;
  safety_backup_path: string | null;
};

export type DesktopUpdateCheck = {
  available: boolean;
  current_version: string;
  version: string | null;
  date: string | null;
  body: string | null;
  target: string | null;
  download_url: string | null;
};

export type DesktopUpdateInstallStatus =
  | { status: "no_update" }
  | { status: "installing"; version: string };

export type DesktopUpdateProgress = {
  downloaded: number;
  total: number | null;
  percent: number | null;
};

declare global {
  interface Window {
    __TAURI__?: {
      core?: {
        invoke?: <T = unknown>(
          command: string,
          args?: Record<string, unknown>,
        ) => Promise<T>;
      };
      event?: {
        listen?: <T = unknown>(
          event: string,
          handler: (event: { payload: T }) => void,
        ) => Promise<() => void>;
      };
    };
  }
}

export function isDesktopRuntime(): boolean {
  return DESKTOP_RUNTIME;
}

export function isDesktopBridgeAvailable(): boolean {
  if (typeof window === "undefined") return false;
  return typeof window.__TAURI__?.core?.invoke === "function";
}

export async function desktopInvoke<T = unknown>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (typeof window === "undefined") {
    throw new Error("desktop bridge is only available in the browser");
  }
  // Tauri injects this because tauri.conf.json keeps app.withGlobalTauri=true.
  const invoke = window.__TAURI__?.core?.invoke;
  if (!invoke) {
    throw new Error("desktop bridge is unavailable");
  }
  return invoke<T>(command, args);
}

export async function listenDesktopEvent<T = unknown>(
  event: string,
  handler: (payload: T) => void,
): Promise<() => void> {
  if (typeof window === "undefined") return () => {};
  const listen = window.__TAURI__?.event?.listen;
  if (!listen) return () => {};
  return listen<T>(event, (evt) => handler(evt.payload));
}

export async function revealDesktopDataDir(): Promise<void> {
  await desktopInvoke("open_data_dir");
}

export async function getDesktopStatus(): Promise<DesktopStatus> {
  return desktopInvoke<DesktopStatus>("desktop_status");
}

export async function saveProviderSecret(
  provider: string,
  apiKey: string,
): Promise<void> {
  await desktopInvoke("set_provider_key", { provider, apiKey });
}

export async function saveProxySecret(
  proxy: string,
  password: string,
): Promise<void> {
  await desktopInvoke("set_proxy_secret", { proxy, password });
}

export async function refreshProviderRuntime(): Promise<void> {
  await desktopInvoke("refresh_provider_runtime");
}

export async function exportDiagnosticsBundle(): Promise<DiagnosticBundle> {
  return desktopInvoke<DiagnosticBundle>("export_diagnostics_bundle");
}

export async function exportDesktopBackup(): Promise<DesktopBackup> {
  return desktopInvoke<DesktopBackup>("export_desktop_backup");
}

export async function getDesktopRestoreStatus(): Promise<DesktopRestoreStatus> {
  return desktopInvoke<DesktopRestoreStatus>("desktop_restore_status");
}

export async function clearFailedRestoreMarker(): Promise<void> {
  await desktopInvoke("clear_failed_restore_marker");
}

export async function clearPendingRestore(): Promise<void> {
  await desktopInvoke("clear_pending_restore");
}

export async function selectDesktopRestoreBackup(): Promise<DesktopRestorePlan | null> {
  return desktopInvoke<DesktopRestorePlan | null>("select_desktop_restore_backup");
}

export async function getDesktopDockerImportStatus(): Promise<DesktopDockerImportStatus> {
  return desktopInvoke<DesktopDockerImportStatus>("desktop_docker_import_status");
}

export async function clearFailedDockerImportMarker(): Promise<void> {
  await desktopInvoke("clear_failed_docker_import_marker");
}

export async function selectDockerImportBackup(
  userId?: string,
): Promise<DesktopDockerImportPlan | null> {
  return desktopInvoke<DesktopDockerImportPlan | null>("select_docker_import_backup", {
    userId: userId?.trim() || null,
  });
}

export async function restartDesktopApp(): Promise<void> {
  await desktopInvoke("restart_desktop_app");
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateCheck> {
  return desktopInvoke<DesktopUpdateCheck>("check_desktop_update");
}

export async function installDesktopUpdate(): Promise<DesktopUpdateInstallStatus> {
  return desktopInvoke<DesktopUpdateInstallStatus>("install_desktop_update");
}
