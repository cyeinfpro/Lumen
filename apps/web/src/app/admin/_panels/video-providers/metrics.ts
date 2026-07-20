import {
  summaryIsUsable,
  type Issue,
  type ProviderSummary,
  type VideoAction,
} from "./domain";

export type ProviderPanelMetrics = {
  enabledCount: number;
  usableCount: number;
  totalConcurrency: number;
  coveredActions: Set<VideoAction>;
  issues: Issue[];
};

export type DraftPanelMetrics = {
  errorCount: number;
  warningCount: number;
  globalIssue: string | null;
  statusText: string;
};

function draftStatusLabel(
  globalIssue: string | null,
  errorCount: number,
  warningCount: number,
): string {
  if (globalIssue) return globalIssue;
  if (errorCount > 0) return `还有 ${errorCount} 个错误需要处理`;
  if (warningCount > 0) return `${warningCount} 个提示不会阻止保存`;
  return "配置可以保存";
}

export function summarizeProviders(
  summaries: ProviderSummary[],
): ProviderPanelMetrics {
  const coveredActions = new Set<VideoAction>();
  const issues: Issue[] = [];
  let enabledCount = 0;
  let usableCount = 0;
  let totalConcurrency = 0;

  for (const summary of summaries) {
    if (summary.enabled) {
      enabledCount += 1;
      totalConcurrency += summary.concurrency;
    }
    if (summary.enabled && summary.hasKey && summary.modelNames.length > 0) {
      usableCount += 1;
    }
    if (summary.enabled && summary.hasKey) {
      summary.capabilities.forEach((action) => coveredActions.add(action));
    }
    issues.push(
      ...summary.issues.map((issue) => ({
        ...issue,
        message: `${summary.name}：${issue.message}`,
      })),
    );
  }

  return {
    enabledCount,
    usableCount,
    totalConcurrency,
    coveredActions,
    issues,
  };
}

export function summarizeDrafts(
  summaries: ProviderSummary[],
  enabled: boolean,
): DraftPanelMetrics {
  let errorCount = 0;
  let warningCount = 0;

  for (const summary of summaries) {
    errorCount += summary.issues.filter(
      (issue) => issue.severity === "error",
    ).length;
    warningCount += summary.issues.filter(
      (issue) => issue.severity === "warning",
    ).length;
  }

  const globalIssue =
    enabled && !summaries.some(summaryIsUsable)
      ? "启用视频生成前至少需要一个启用且可用的供应商"
      : null;

  return {
    errorCount,
    warningCount,
    globalIssue,
    statusText: draftStatusLabel(globalIssue, errorCount, warningCount),
  };
}
