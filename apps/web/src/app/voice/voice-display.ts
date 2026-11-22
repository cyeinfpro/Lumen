export function privateAudioPath(raw?: string | null): string | undefined {
  if (!raw || !/^\/(api\/)?(?:speech\/jobs\/[A-Za-z0-9_-]+\/audio|voices\/[A-Za-z0-9_-]+\/preview)$/.test(raw)) return undefined;
  return raw.startsWith("/api/") ? raw : `/api${raw}`;
}
/** Clear the accepted draft, never the one typed while its request was pending. */
export function draftAfterAcceptedSubmission(current: string, submitted: string): string {
  return current === submitted ? "" : current;
}

export const isInProgress = (state: string) =>
  ["queued", "validating", "pending", "processing", "creating", "enrolling", "deploying", "running", "retrying", "revoking", "rendering", "assembling"].includes(state);

export const hasUnknownOutcome = (state: string) =>
  ["ambiguous", "uncertain", "outcome_unknown", "unknown", "needs_reconciliation"].includes(state);

const LABELS: Record<string, string> = {
  pending: "排队中", queued: "排队中", validating: "正在检查录音", creating: "正在创建音色",
  enrolling: "正在创建音色", deploying: "音色准备中", processing: "处理中", running: "生成中",
  rendering: "生成中", retrying: "正在重做", assembling: "合并音频中", ready: "可用", draft: "草稿",
  succeeded: "已完成", completed: "已完成", failed: "未完成", revoked: "已撤销", revoking: "撤销确认中",
  rejected: "未通过", canceled: "已取消", cancelled: "已取消", deleted: "已删除",
};
export const stateLabel = (state: string) =>
  hasUnknownOutcome(state) ? "结果待确认" : LABELS[state] ?? "状态待刷新";
export const yuan = (micro: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(micro / 1_000_000);
