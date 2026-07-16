import type {
  VideoAssetDeleteResultOut,
  VideoAssetGroupOut,
  VideoAssetOperationOut,
  VideoAssetOperationResult,
  VideoAssetOut,
  VideoAssetStatus,
  VideoAssetType,
} from "@/lib/types";

export const LUMEN_ASSET_IMAGE_MAX_BYTES = 50 * 1024 * 1024;
export const LUMEN_ASSET_VIDEO_MAX_BYTES = 64 * 1024 * 1024;
export const VOLCANO_PROJECT_ASSET_LIMIT = 50;
export const VOLCANO_PROJECT_GROUP_LIMIT = 50;
export const VOLCANO_CREATE_ASSET_QPM = 3;
export const VOLCANO_ASSET_NAME_MAX_LENGTH = 64;
export const VOLCANO_OPERATION_POLL_TIMEOUT_MS = 20 * 60 * 1000;

export type VolcanoQuotaUsage = {
  used: number;
  remaining: number;
  limit: number;
  reached: boolean;
};

export type VolcanoAssetSelectionLike = {
  id: string;
  name: string;
  asset_type: VideoAssetType;
  url?: string | null;
  preview_url?: string | null;
  status: string;
  group_id: string;
};

export type VolcanoAssetStatusKind =
  "active" | "processing" | "failed" | "unknown";

export type VolcanoOperationStatusKind =
  "pending" | "succeeded" | "failed" | "unknown";

export type VolcanoUploadRetryMode =
  "create" | "operation" | "refresh" | "none";

export type VolcanoCreateFailureRecovery = "retry_create" | "verify" | "none";

export type VolcanoOperationResultKind =
  "group" | "asset" | "delete" | "unknown";

export type VolcanoManagedOperationPhase =
  "pending" | "paused" | "uncertain" | "succeeded" | "failed";

export type VolcanoOperationRecoveryMode =
  "resume" | "retry" | "refresh" | "none";

export type VolcanoOperationCheckpoint = {
  phase: VolcanoManagedOperationPhase;
  remoteOperationId?: string;
  submissionStartedAt?: number;
  recovery: VolcanoOperationRecoveryMode;
  error?: string;
};

export type VolcanoAssetSelectionIssue =
  "duplicate" | "unavailable" | "image_limit" | "video_limit";

export type VolcanoAssetSelectionResult<T extends VolcanoAssetSelectionLike> = {
  items: T[];
  issue?: VolcanoAssetSelectionIssue;
};

export type VolcanoAssetFileLike = Pick<File, "name" | "size" | "type">;

export type VolcanoAssetFileValidation =
  { ok: true; assetType: VideoAssetType } | { ok: false; error: string };

export type VolcanoQuotaReservationLike = {
  quotaReserved?: boolean;
  quotaReservationTarget?: number;
};

type VolcanoErrorLike = {
  code?: unknown;
  message?: unknown;
  status?: unknown;
  payload?: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

export function volcanoOperationResultKind(
  result: VideoAssetOperationResult | null | undefined,
): VolcanoOperationResultKind {
  if (!isRecord(result)) return "unknown";
  if (typeof result.deleted === "boolean") return "delete";
  if (typeof result.asset_type === "string") return "asset";
  if (typeof result.group_type === "string") return "group";
  return "unknown";
}

export function volcanoOperationGroupResult(
  operation: VideoAssetOperationOut,
): VideoAssetGroupOut | null {
  return volcanoOperationResultKind(operation.result) === "group"
    ? (operation.result as VideoAssetGroupOut)
    : null;
}

export function volcanoOperationAssetResult(
  operation: VideoAssetOperationOut,
): VideoAssetOut | null {
  return volcanoOperationResultKind(operation.result) === "asset"
    ? (operation.result as VideoAssetOut)
    : null;
}

export function volcanoOperationDeleteResult(
  operation: VideoAssetOperationOut,
): VideoAssetDeleteResultOut | null {
  return volcanoOperationResultKind(operation.result) === "delete"
    ? (operation.result as VideoAssetDeleteResultOut)
    : null;
}

export function volcanoOperationIsRetryable(
  operation: VideoAssetOperationOut,
): boolean {
  return operation.retryable || Boolean(operation.error?.retryable);
}

export function volcanoDeletedAssetIds(
  result: VideoAssetDeleteResultOut | null,
  fallbackIds: Iterable<string> = [],
): string[] {
  const ids = new Set(fallbackIds);
  for (const assetId of result?.deleted_asset_ids ?? []) {
    if (assetId) ids.add(assetId);
  }
  if (result?.asset_id) ids.add(result.asset_id);
  if (result?.resource_type === "asset" && result.id) ids.add(result.id);
  return Array.from(ids);
}

export function volcanoUniqueNewGroupMatch(
  groups: VideoAssetGroupOut[],
  baselineGroupIds: Iterable<string>,
  expected: { name: string; description: string },
): VideoAssetGroupOut | null {
  const baseline = new Set(baselineGroupIds);
  const expectedName = expected.name.trim();
  const expectedDescription = expected.description.trim();
  const matches = groups.filter(
    (group) =>
      !baseline.has(group.id) &&
      group.name.trim() === expectedName &&
      group.description.trim() === expectedDescription,
  );
  return matches.length === 1 ? matches[0] : null;
}

export function pauseVolcanoOperationCheckpoints<
  T extends VolcanoOperationCheckpoint,
>(items: T[]): T[] {
  let changed = false;
  const paused = items.map((item) => {
    if (item.phase !== "pending") return item;
    changed = true;
    if (item.remoteOperationId) {
      return {
        ...item,
        phase: "paused" as const,
        recovery: "resume" as const,
        error: "状态轮询已暂停，重新打开后会继续确认后台结果。",
      };
    }
    if (item.submissionStartedAt) {
      return {
        ...item,
        phase: "uncertain" as const,
        recovery: "refresh" as const,
        error:
          "提交请求已发出但结果未知。系统不会自动重发，请检查素材库后再继续。",
      };
    }
    return {
      ...item,
      phase: "paused" as const,
      recovery: "resume" as const,
      error: undefined,
    };
  });
  return changed ? paused : items;
}

export function volcanoOperationBlocksMutation(
  operation: Pick<VolcanoOperationCheckpoint, "phase">,
): boolean {
  return (
    operation.phase === "pending" ||
    operation.phase === "paused" ||
    operation.phase === "uncertain"
  );
}

type VolcanoLock =
  | { kind: "group-create" }
  | { kind: "group"; groupId: string }
  | { kind: "asset"; groupId: string; assetId: string }
  | { kind: "unknown"; value: string };

export function volcanoQuotaUsage(
  used: number,
  limit: number,
): VolcanoQuotaUsage {
  const normalizedUsed = Number.isFinite(used)
    ? Math.max(0, Math.floor(used))
    : 0;
  const normalizedLimit = Number.isFinite(limit)
    ? Math.max(0, Math.floor(limit))
    : 0;
  return {
    used: normalizedUsed,
    remaining: Math.max(0, normalizedLimit - normalizedUsed),
    limit: normalizedLimit,
    reached: normalizedUsed >= normalizedLimit,
  };
}

export function volcanoAssetMediaUrl(
  asset: Pick<VolcanoAssetSelectionLike, "preview_url" | "url">,
): string | null {
  for (const candidate of [asset.preview_url, asset.url]) {
    const value = candidate?.trim();
    if (value && (/^https?:\/\//i.test(value) || value.startsWith("/api/"))) {
      return value;
    }
  }
  return null;
}

export function volcanoReservedQuotaCount(
  items: VolcanoQuotaReservationLike[],
): number {
  return items.filter((item) => item.quotaReserved).length;
}

export function settleVolcanoQuotaReservations<
  T extends VolcanoQuotaReservationLike,
>(items: T[], remoteTotal: number): T[] {
  const normalizedRemoteTotal = Number.isFinite(remoteTotal)
    ? Math.max(0, Math.floor(remoteTotal))
    : 0;
  let changed = false;
  const settled = items.map((item) => {
    if (
      !item.quotaReserved ||
      item.quotaReservationTarget == null ||
      normalizedRemoteTotal < item.quotaReservationTarget
    ) {
      return item;
    }
    changed = true;
    return { ...item, quotaReserved: false };
  });
  return changed ? settled : items;
}

export function volcanoAssetNameFromFile(fileName: string): string {
  const trimmed = fileName.trim();
  const withoutExtension = trimmed.replace(/\.[^./\\]+$/, "").trim();
  return (withoutExtension || "虚拟素材").slice(
    0,
    VOLCANO_ASSET_NAME_MAX_LENGTH,
  );
}

export function truncateVolcanoAssetName(value: string): string {
  return value.slice(0, VOLCANO_ASSET_NAME_MAX_LENGTH);
}

function normalizedFileExtension(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

export function volcanoAssetStatusKind(
  status: VideoAssetStatus | string,
): VolcanoAssetStatusKind {
  const normalized = status.trim().toLowerCase();
  if (normalized === "active" || normalized === "available") return "active";
  if (
    normalized === "processing" ||
    normalized === "pending" ||
    normalized === "creating"
  ) {
    return "processing";
  }
  if (
    normalized === "failed" ||
    normalized === "error" ||
    normalized === "inactive"
  ) {
    return "failed";
  }
  return "unknown";
}

export function volcanoOperationStatusKind(
  status: string,
): VolcanoOperationStatusKind {
  const normalized = status.trim().toLowerCase();
  if (
    normalized === "queued" ||
    normalized === "pending" ||
    normalized === "running" ||
    normalized === "processing"
  ) {
    return "pending";
  }
  if (
    normalized === "succeeded" ||
    normalized === "success" ||
    normalized === "completed"
  ) {
    return "succeeded";
  }
  if (
    normalized === "failed" ||
    normalized === "error" ||
    normalized === "cancelled" ||
    normalized === "canceled"
  ) {
    return "failed";
  }
  return "unknown";
}

export function volcanoOperationStageMessage(stage?: string | null): string {
  const normalized = String(stage ?? "")
    .trim()
    .toLowerCase();
  const messages: Record<string, string> = {
    queued: "后台任务已排队",
    validating_scope: "正在校验素材组和项目范围",
    checking_quota: "正在确认素材配额",
    normalizing_image: "正在后台优化图片尺寸与格式",
    normalizing_video: "正在后台转码视频尺寸、帧率与编码",
    waiting_submit_slot: "正在等待火山提交配额",
    waiting_rate_limit: "触发火山限流，后台稍后自动继续",
    submitting: "正在提交到火山素材库",
    completed: "已提交到火山素材库",
    failed: "后台任务失败",
  };
  return (
    messages[normalized] ?? (normalized ? `后台阶段：${stage}` : "后台处理中")
  );
}

export function volcanoOperationTimedOut(
  startedAtMs: number | undefined,
  nowMs = Date.now(),
  timeoutMs = VOLCANO_OPERATION_POLL_TIMEOUT_MS,
): boolean {
  if (!Number.isFinite(startedAtMs) || !startedAtMs) return false;
  return nowMs - startedAtMs >= Math.max(0, timeoutMs);
}

export function volcanoAssetIsSelectable(
  status: VideoAssetStatus | string,
): boolean {
  return volcanoAssetStatusKind(status) === "active";
}

export function volcanoAssetFileType(
  file: Pick<VolcanoAssetFileLike, "name" | "type">,
): VideoAssetType | null {
  const mime = file.type.trim().toLowerCase();
  const extension = normalizedFileExtension(file.name);
  if (mime.startsWith("image/")) {
    return ["image/jpeg", "image/png", "image/webp"].includes(mime)
      ? "Image"
      : null;
  }
  if ([".jpg", ".jpeg", ".png", ".webp"].includes(extension) && !mime) {
    return "Image";
  }
  if (
    mime === "video/mp4" ||
    mime === "video/quicktime" ||
    ((!mime || mime === "application/octet-stream") &&
      (extension === ".mp4" || extension === ".mov"))
  ) {
    return "Video";
  }
  return null;
}

export function validateVolcanoAssetFile(
  file: VolcanoAssetFileLike,
  durationSeconds?: number | null,
): VolcanoAssetFileValidation {
  void durationSeconds;
  const assetType = volcanoAssetFileType(file);
  if (!assetType) {
    return { ok: false, error: "仅支持 PNG、JPEG、WebP、MP4 或 MOV" };
  }
  if (assetType === "Image" && file.size > LUMEN_ASSET_IMAGE_MAX_BYTES) {
    return { ok: false, error: "图片不能超过 50 MiB" };
  }
  if (assetType === "Video" && file.size > LUMEN_ASSET_VIDEO_MAX_BYTES) {
    return { ok: false, error: "视频不能超过 64 MiB" };
  }
  return { ok: true, assetType };
}

export function toggleVolcanoAssetSelection<
  T extends VolcanoAssetSelectionLike,
>({
  current,
  candidate,
  existingAssetIds,
  remainingLimits,
}: {
  current: T[];
  candidate: T;
  existingAssetIds: Iterable<string>;
  remainingLimits: { image: number; video: number };
}): VolcanoAssetSelectionResult<T> {
  if (current.some((item) => item.id === candidate.id)) {
    return {
      items: current.filter((item) => item.id !== candidate.id),
    };
  }

  const existing = new Set(existingAssetIds);
  if (existing.has(candidate.id)) {
    return { items: current, issue: "duplicate" };
  }
  if (!volcanoAssetIsSelectable(candidate.status)) {
    return { items: current, issue: "unavailable" };
  }

  const sameTypeCount = current.filter(
    (item) => item.asset_type === candidate.asset_type,
  ).length;
  if (
    candidate.asset_type === "Image" &&
    sameTypeCount >= Math.max(0, remainingLimits.image)
  ) {
    return { items: current, issue: "image_limit" };
  }
  if (
    candidate.asset_type === "Video" &&
    sameTypeCount >= Math.max(0, remainingLimits.video)
  ) {
    return { items: current, issue: "video_limit" };
  }

  return { items: [...current, candidate] };
}

export function volcanoGroupCreateLockKey(): string {
  return "group-create";
}

export function volcanoGroupLockKey(groupId: string): string {
  return `group|${groupId}`;
}

export function volcanoAssetLockKey(groupId: string, assetId: string): string {
  return `asset|${groupId}|${assetId}`;
}

function parseVolcanoLock(value: string): VolcanoLock {
  if (value === "group-create") return { kind: "group-create" };
  if (value.startsWith("group|")) {
    return { kind: "group", groupId: value.slice("group|".length) };
  }
  if (value.startsWith("asset|")) {
    const body = value.slice("asset|".length);
    const separator = body.indexOf("|");
    if (separator > 0 && separator < body.length - 1) {
      return {
        kind: "asset",
        groupId: body.slice(0, separator),
        assetId: body.slice(separator + 1),
      };
    }
  }
  return { kind: "unknown", value };
}

export function volcanoOperationLocksConflict(
  currentLockKey: string,
  nextLockKey: string,
): boolean {
  if (currentLockKey === nextLockKey) return true;
  const current = parseVolcanoLock(currentLockKey);
  const next = parseVolcanoLock(nextLockKey);
  if (current.kind === "unknown" || next.kind === "unknown") return false;
  if (current.kind === "group-create" || next.kind === "group-create") {
    return false;
  }
  if (current.kind === "group" && next.kind === "group") {
    return current.groupId === next.groupId;
  }
  if (current.kind === "asset" && next.kind === "asset") {
    return current.groupId === next.groupId && current.assetId === next.assetId;
  }
  const group = current.kind === "group" ? current : next;
  const asset = current.kind === "asset" ? current : next;
  return group.groupId === asset.groupId;
}

function volcanoErrorRecord(error: unknown): VolcanoErrorLike {
  return error && typeof error === "object" ? (error as VolcanoErrorLike) : {};
}

function volcanoErrorCode(error: unknown): string {
  const code = volcanoErrorRecord(error).code;
  return typeof code === "string" ? code.trim().toLowerCase() : "";
}

function volcanoErrorStatus(error: unknown): number {
  const status = volcanoErrorRecord(error).status;
  return typeof status === "number" && Number.isFinite(status) ? status : 0;
}

export function volcanoAssetErrorMessage(
  error: unknown,
  fallback: string,
): string {
  const record = volcanoErrorRecord(error);
  const code = volcanoErrorCode(error);
  const messages: Record<string, string> = {
    network_error: "网络连接中断，无法确认请求结果",
    unauthorized: "登录状态已失效，请重新登录后再操作",
    csrf_failed: "安全令牌已失效，请刷新页面后重试",
    video_asset_queue_unavailable: "素材后台队列暂不可用，请稍后重试",
    volcano_asset_create_rate_limited: "火山提交频率已达上限，后台稍后可继续",
    volcano_asset_quota_exceeded: "当前 Project 的素材配额已满",
    volcano_asset_group_quota_exceeded: "当前 Project 的素材组配额已满",
    video_asset_operation_not_found: "后台任务记录已过期，请刷新素材库确认结果",
    video_asset_operation_not_retryable:
      "该后台任务不能重试，请刷新素材库确认结果",
    video_asset_provider_missing: "当前模型没有可用的火山素材供应商",
    video_asset_provider_unsupported: "当前模型不是火山官方素材供应商",
    volcano_asset_credentials_missing: "火山素材库 AK/SK 配置不完整",
    video_asset_public_url_missing: "素材公开访问地址未配置，火山无法拉取文件",
    video_asset_public_url_invalid: "素材公开访问地址不可用，火山无法拉取文件",
    video_asset_image_not_found: "本地图片已不存在，请重新选择文件",
    video_asset_video_not_found: "本地视频已不存在，请重新选择文件",
    volcano_asset_scope_mismatch: "素材不属于当前 Project 或 AIGC 素材组",
    volcano_asset_not_found: "云端素材已不存在，请刷新素材库",
  };
  if (messages[code]) return messages[code];
  const message =
    typeof record.message === "string" ? record.message.trim() : "";
  return message || fallback;
}

export function volcanoCreateFailureRecovery(
  error: unknown,
): VolcanoCreateFailureRecovery {
  const code = volcanoErrorCode(error);
  const status = volcanoErrorStatus(error);
  if (code === "unauthorized" || code === "csrf_failed") return "none";
  if (
    code === "volcano_asset_create_rate_limited" ||
    code === "video_asset_queue_unavailable" ||
    (status >= 400 && status < 500)
  ) {
    return "retry_create";
  }
  return "verify";
}

export function volcanoAssetSelectionIssueMessage(
  issue: VolcanoAssetSelectionIssue,
): string {
  if (issue === "duplicate") return "该素材已在当前草稿中";
  if (issue === "unavailable") return "仅可选择状态为可用的素材";
  if (issue === "image_limit") return "图片素材已达到本次上限";
  return "视频素材已达到本次上限";
}
