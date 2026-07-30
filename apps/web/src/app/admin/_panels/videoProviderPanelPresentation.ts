import { ApiError } from "@/lib/apiClient";
import type { Issue } from "./videoProviderPanelTypes";

export function saveError(err: Error): string {
  if (err instanceof ApiError) {
    return err.message || `保存失败 (HTTP ${err.status})`;
  }
  return err.message || "保存失败";
}

export function sourceLabel(source: string | undefined): string {
  if (source === "db") return "数据库";
  if (source === "env") return "环境变量";
  return "未配置";
}

export function issueTone(
  issues: Issue[],
): "danger" | "warning" | "success" {
  if (issues.some((item) => item.severity === "error")) return "danger";
  if (issues.length > 0) return "warning";
  return "success";
}
