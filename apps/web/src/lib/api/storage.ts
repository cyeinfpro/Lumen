// Lumen 管理面板：存储后端（local / SMB）API client.
//
// 后端契约见 apps/api/app/routes/admin_storage.py：
//   GET  /admin/storage          → StorageConfigOut
//   POST /admin/storage/test     → StorageTestResultOut（不重启 lumen-api）
//   PUT  /admin/storage          → StorageApplyResponseOut（PUT 后 host 会 docker
//                                  stop lumen-api，前端需要 polling GET 直到
//                                  last_apply.call_id 命中且 status != "pending"）
//
// 这里只做薄封装；polling / loading 状态由组件层负责（StoragePanel）。
//
// 复用 @/lib/api/http 的 apiFetch（自动带 CSRF + credentials），不要再走一套。

import { apiFetch } from "./http";

// —— 后端返回类型 ——

export type StorageBackend = "" | "local" | "smb";

export interface StorageLocalConfig {
  /** 本机绝对路径，例如 "/var/lib/lumen-data" */
  root: string;
}

export interface StorageSmbConfig {
  host: string;
  share: string;
  subpath: string;
  username: string;
  /** 后端只暴露是否已存在密码；明文密码不会回传 */
  has_password: boolean;
}

export interface StorageStatus {
  mode: string;
  mounted: boolean;
  source: string;
  fstype: string;
  target: string;
  /** disabled flag 文件存在 → 强制回退到本地默认路径（恢复用） */
  disabled: boolean;
  updated_at: number | null;
}

export interface StorageApplyRecord {
  call_id: string;
  status: "ok" | "fail" | "pending";
  message: string;
  started_at: number;
  finished_at: number;
}

export interface StorageTestRecord {
  call_id: string;
  status: "ok" | "fail";
  message: string;
  tested_at: number;
}

export interface StorageConfigOut {
  backend: StorageBackend;
  local: StorageLocalConfig;
  smb: StorageSmbConfig;
  /** host 还没写过 status.json 时为 null */
  status: StorageStatus | null;
  last_apply: StorageApplyRecord | null;
  last_test: StorageTestRecord | null;
}

// —— 请求类型 ——

export interface StorageTestIn {
  host: string;
  share: string;
  subpath: string;
  username: string;
  /** "" 表示沿用已存的密码（要求 has_password === true） */
  password: string;
}

export interface StorageTestResultOut {
  status: "ok" | "fail" | "pending";
  message: string;
  tested_at: number | null;
  call_id: string | null;
}

export interface StorageLocalUpdateIn {
  root: string;
}

export interface StorageSmbUpdateIn {
  host: string;
  share: string;
  subpath: string;
  username: string;
  /** "" 表示保留旧密码 */
  password: string;
}

export interface StorageConfigUpdateIn {
  backend: "local" | "smb";
  local: StorageLocalUpdateIn | null;
  smb: StorageSmbUpdateIn | null;
}

export interface StorageApplyResponseOut {
  config: StorageConfigOut;
  call_id: string;
  status: "pending" | "ok" | "fail";
  message: string;
}

// —— API 函数 ——

export function getAdminStorage(): Promise<StorageConfigOut> {
  return apiFetch<StorageConfigOut>("/admin/storage");
}

export function testAdminStorage(
  body: StorageTestIn,
): Promise<StorageTestResultOut> {
  return apiFetch<StorageTestResultOut>("/admin/storage/test", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function putAdminStorage(
  body: StorageConfigUpdateIn,
): Promise<StorageApplyResponseOut> {
  return apiFetch<StorageApplyResponseOut>("/admin/storage", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
