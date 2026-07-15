import { listVideoAssets } from "@/lib/apiClient";
import type {
  VideoAssetCapabilitiesOut,
  VideoAssetOut,
  VideoAssetStatus,
  VideoAssetType,
} from "@/lib/types";

import {
  VOLCANO_CREATE_ASSET_QPM,
  volcanoAssetStatusKind,
} from "./volcano-asset-domain";
import { mergeUniqueAssetPage } from "./volcano-asset-manager-state";
import {
  DELETE_SCAN_PAGE_SIZE,
  type UploadPhase,
} from "./volcano-asset-manager-types";

export const CREATE_ASSET_MIN_INTERVAL_MS =
  60_000 / VOLCANO_CREATE_ASSET_QPM;
export const VOLCANO_ASSET_SCAN_LIMIT = 3_000;

export function clientId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `volcano-asset-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function abortableDelay(
  delayMs: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) {
    return Promise.reject(new DOMException("Aborted", "AbortError"));
  }
  if (delayMs <= 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function formatTime(value?: string | null): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}

export function capabilityCopy(capability: VideoAssetCapabilitiesOut): {
  title: string;
  description: string;
  action: string;
} {
  if (capability.reason === "not_volcano_official") {
    return {
      title: "当前模型不是火山官方 Seedance",
      description:
        "火山虚拟素材库仅适用于 kind=volcano 的官方 Seedance。普通非人像素材继续使用视频页的“上传参考”。",
      action: "管理员操作：在 AI 视频供应商中为该模型选择火山官方配置。",
    };
  }
  if (capability.reason === "missing_credentials") {
    return {
      title: "缺少火山 AK / SK",
      description:
        "当前 Seedance 可以生成视频，但火山虚拟素材库还没有资产管理凭据。",
      action:
        "管理员操作：补全火山官方供应商的 AK、SK、ProjectName 和 Region。",
    };
  }
  if (capability.reason === "missing_project_name") {
    return {
      title: "ProjectName 未配置",
      description: "火山 AIGC Asset Group 必须绑定明确的 ProjectName。",
      action: "管理员操作：在火山官方供应商中保存 ProjectName 后重试。",
    };
  }
  if (capability.reason === "public_base_url_unavailable") {
    return {
      title: "公开地址不可用",
      description:
        "后台优化后的图片或视频需要通过公开 HTTPS 地址交给火山拉取。",
      action:
        "管理员操作：配置 PUBLIC_BASE_URL 或站点公开地址，并确认外网可以访问。",
    };
  }
  return {
    title: "火山虚拟素材库暂不可用",
    description:
      capability.detail ||
      "当前视频供应商不可用或没有为该模型配置参考生成能力。",
    action: "管理员操作：检查视频供应商、模型映射和服务连接状态。",
  };
}

export function statusPresentation(status: string): {
  label: string;
  className: string;
} {
  const kind = volcanoAssetStatusKind(status);
  if (kind === "active") {
    return {
      label: "可用",
      className: "border-success-border bg-success-soft text-success",
    };
  }
  if (kind === "processing") {
    return {
      label: "处理中",
      className: "border-info-border bg-info-soft text-info",
    };
  }
  if (kind === "failed") {
    return {
      label: "失败",
      className: "border-danger-border bg-danger-soft text-danger",
    };
  }
  return {
    label: status || "未知",
    className: "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
  };
}

export function uploadPresentation(phase: UploadPhase): {
  label: string;
  className: string;
} {
  if (phase === "queued") {
    return {
      label: "排队",
      className: "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
    };
  }
  if (phase === "uploading") {
    return {
      label: "上传中",
      className: "border-info-border bg-info-soft text-info",
    };
  }
  if (phase === "optimizing") {
    return {
      label: "后台优化",
      className: "border-warning-border bg-warning-soft text-warning",
    };
  }
  if (phase === "waiting_quota") {
    return {
      label: "等待火山提交配额",
      className: "border-warning-border bg-warning-soft text-warning",
    };
  }
  if (phase === "processing") {
    return {
      label: "火山处理中",
      className: "border-info-border bg-info-soft text-info",
    };
  }
  if (phase === "needs_refresh") {
    return {
      label: "等待确认",
      className: "border-warning-border bg-warning-soft text-warning",
    };
  }
  if (phase === "ready") {
    return {
      label: "已可用",
      className: "border-success-border bg-success-soft text-success",
    };
  }
  return {
    label: "失败",
    className: "border-danger-border bg-danger-soft text-danger",
  };
}

export async function allGroupAssetIds(
  model: string,
  groupId: string,
  signal: AbortSignal,
): Promise<string[]> {
  const result = await scanVideoAssets({
    model,
    groupIds: [groupId],
    signal,
  });
  return result.items.map((item) => item.id);
}

export async function scanVideoAssets({
  model,
  groupIds,
  name,
  statuses,
  assetTypes,
  signal,
}: {
  model: string;
  groupIds?: string[];
  name?: string;
  statuses?: VideoAssetStatus[];
  assetTypes?: VideoAssetType[];
  signal: AbortSignal;
}): Promise<{ items: VideoAssetOut[] }> {
  let items: VideoAssetOut[] = [];
  const maxPages = Math.ceil(
    VOLCANO_ASSET_SCAN_LIMIT / DELETE_SCAN_PAGE_SIZE,
  );
  for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
    if (signal.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    const request = {
      model,
      group_ids: groupIds,
      name,
      statuses,
      asset_types: assetTypes,
      page_number: pageNumber,
      page_size: DELETE_SCAN_PAGE_SIZE,
      signal,
    };
    const page = await listVideoAssets(request);
    if (page.items.length === 0) break;
    const merged = mergeUniqueAssetPage(
      items,
      page.items,
      VOLCANO_ASSET_SCAN_LIMIT,
    );
    items = merged.items;
    if (merged.added === 0 || items.length >= VOLCANO_ASSET_SCAN_LIMIT) {
      break;
    }
  }
  const allowedTypes = new Set(assetTypes ?? []);
  return {
    items:
      allowedTypes.size === 0
        ? items
        : items.filter((item) => allowedTypes.has(item.asset_type)),
  };
}

export function fullAssetSet(
  items: VideoAssetOut[],
  groupId: string | null,
  loadedGroupId: string | null,
): VideoAssetOut[] {
  return groupId && loadedGroupId === groupId ? items : [];
}
