// 共享工具函数：步骤定位、图片解析、JSON 渲染、时间格式。
// 没有任何 React 依赖，可被 server / worker 单测直接 import。

import {
  imageVariantUrl,
  type BackendImageMeta,
  type WorkflowRun,
  type WorkflowRunListItem,
  type WorkflowStep,
} from "@/lib/apiClient";
import {
  JSON_KEY_LABEL,
  QUALITY_VALUE_LABEL,
  RECOMMENDATION_LABEL,
  SHOT_VALUE_LABEL,
  STATUS_LABEL,
  STEP_INDEX,
  STEPS,
  TEMPLATE_VALUE_LABEL,
} from "./types";

export function imageSrc(image?: BackendImageMeta | null): string {
  if (!image) return "";
  return image.display_url || image.preview_url || image.thumb_url || image.url;
}

export function productThumbSrc(item: WorkflowRunListItem): string {
  const first = item.product_image_ids[0];
  return first ? imageVariantUrl(first, "display2048") : "";
}

export function stepOf(workflow: WorkflowRun, key: string): WorkflowStep | undefined {
  return workflow.steps.find((step) => step.step_key === key);
}

export function imageById(
  workflow: WorkflowRun,
  id?: string | null,
): BackendImageMeta | undefined {
  if (!id) return undefined;
  return [...workflow.product_images, ...workflow.generated_images].find(
    (image) => image.id === id,
  );
}

export function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.length > 0)
    : [];
}

export function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function accessorySuggestionText(workflow: WorkflowRun): string {
  const output = stepOf(workflow, "product_analysis")?.output_json ?? {};
  const recommendations = output.styling_recommendations;
  if (Array.isArray(recommendations)) {
    return recommendations
      .map((item) => String(item).trim())
      .filter(Boolean)
      .slice(0, 3)
      .join("、");
  }
  return typeof recommendations === "string" ? recommendations : "";
}

export function showcaseImages(workflow: WorkflowRun): BackendImageMeta[] {
  const showcaseStep = stepOf(workflow, "showcase_generation");
  const ids = showcaseStep?.image_ids ?? [];
  return ids
    .map((imageId) => imageById(workflow, imageId))
    .filter((image): image is BackendImageMeta => Boolean(image));
}

export function jsonValue(value: unknown): string {
  if (value == null || value === "") return "未知";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) {
    if (!value.length) return "无";
    return value.map((item) => jsonValue(item)).join("、");
  }
  if (typeof value === "object") {
    return (
      Object.entries(value as Record<string, unknown>)
        .filter(([, item]) => item !== undefined && item !== null && item !== "")
        .map(([key, item]) => `${JSON_KEY_LABEL[key] ?? key}：${jsonValue(item)}`)
        .join("\n") || "无"
    );
  }
  const raw = String(value);
  return (
    STATUS_LABEL[raw] ??
    RECOMMENDATION_LABEL[raw] ??
    TEMPLATE_VALUE_LABEL[raw] ??
    SHOT_VALUE_LABEL[raw] ??
    QUALITY_VALUE_LABEL[raw] ??
    raw
  );
}

export function formatShortDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatRelativeTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const now = Date.now();
  const diff = (date.getTime() - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return "刚刚";
  if (abs < 3600) return `${Math.floor(abs / 60)} 分钟前`;
  if (abs < 86400) return `${Math.floor(abs / 3600)} 小时前`;
  if (abs < 86400 * 7) return `${Math.floor(abs / 86400)} 天前`;
  return formatShortDate(value);
}

export function canDownload(image: BackendImageMeta): string | null {
  return image.url || image.display_url || null;
}

export function workflowProgress(workflow: WorkflowRun): number {
  const total = STEPS.length;
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  if (workflow.status === "completed") return 1;
  return Math.min(0.99, currentIndex / Math.max(1, total - 1));
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
