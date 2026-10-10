"use client";

import { useEffect, useMemo, useState, useSyncExternalStore, type ComponentType } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, toast } from "@/components/ui/primitives";
import { getCanvasExecutionHistory } from "@/lib/api/canvasHistory";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import {
  CanvasHistoryPager, CANVAS_HISTORY_MAX_PAGES, canvasHistoryQueryKey,
  compareHistoricalExecutions, completeExecutionProvenance,
} from "@/lib/canvas/executionHistory";
import type { CanvasDocument, CanvasHistoricalExecution, CanvasNodeExecution } from "@/lib/canvas/types";
import { useUserQueryScope } from "@/lib/queries/userScope";
import { CanvasInspectorExecutionHistory, type CanvasHistoryOutputProps } from "./CanvasInspectorExecutionHistory";
import { useCanvasStoreApi } from "./CanvasStoreProvider";

interface Props {
  document: CanvasDocument;
  selectedNodeId: string;
  executions: CanvasNodeExecution[];
  OutputComponent: ComponentType<CanvasHistoryOutputProps>;
}

export function CanvasHistoryPanel(props: Props) {
  const { userId, enabled } = useUserQueryScope();
  const identity = getPrivateIdentitySnapshot();
  if (!enabled || !userId || identity.userId !== userId) return null;
  // Keyed shell clears both immutable comparisons and cursors on every identity,
  // canvas or node transition, before an old page can become visible.
  return <ScopedHistoryPanel key={JSON.stringify([userId, identity.epoch, props.document.id, props.selectedNodeId])}
    {...props} userId={userId} epoch={identity.epoch} />;
}

function ScopedHistoryPanel({ document, selectedNodeId, executions, OutputComponent, userId, epoch }: Props & { userId: string; epoch: number }) {
  const client = useQueryClient();
  const store = useCanvasStoreApi();
  const [open, setOpen] = useState(false);
  const pager = useMemo(() => {
    const identity = { userId, epoch };
    const scope = { ...identity, canvasId: document.id, nodeId: selectedNodeId };
    return new CanvasHistoryPager(scope, async (cursor, signal) => {
      return client.fetchQuery({
        queryKey: canvasHistoryQueryKey(scope, cursor), gcTime: 0, staleTime: 0, retry: false,
        queryFn: async ({ signal: querySignal }) => {
          const page = await getCanvasExecutionHistory(scope.canvasId, scope.nodeId, cursor, AbortSignal.any([signal, querySignal]));
          if (!isPrivateIdentitySnapshotCurrent(identity)) throw new DOMException("历史身份已改变", "AbortError");
          return page;
        },
      });
    }, () => isPrivateIdentitySnapshotCurrent(identity) && store.getState().selectedNodeId === selectedNodeId);
  }, [client, document.id, selectedNodeId, userId, epoch, store]);
  const history = useSyncExternalStore(pager.subscribe, pager.getSnapshot, pager.getSnapshot);
  useEffect(() => () => pager.dispose(), [pager]);
  const visible = open ? history.items : executions.slice(0, 30);
  const branch = (execution: CanvasHistoricalExecution) => {
    if (!isPrivateIdentitySnapshotCurrent({ userId, epoch }) || store.getState().selectedNodeId !== selectedNodeId) return;
    const result = store.getState().branchHistoricalExecution(execution);
    if (result.ok) toast.success("已创建固定历史分支，可撤销；尚未运行");
    else toast.error(result.reason);
  };
  return <div className="grid gap-2">
    <div className="px-4 pt-4">
      <Button variant="outline" size="sm" fullWidth onClick={() => {
        if (!open) { setOpen(true); void pager.load("initial"); } else { pager.cancel(); setOpen(false); }
      }}>{open ? "收起完整历史" : "查看完整执行历史"}</Button>
      <p className="mt-1 type-caption text-[var(--fg-2)]">每页最多 30 条；比较保留所选执行的原始快照。</p>
    </div>
    {open ? <>
      <ComparisonPanel a={history.a} b={history.b} onClear={() => pager.clear()} />
      <div className="flex items-center justify-between gap-2 px-4" aria-label="历史分页">
        <Button size="sm" variant="outline" disabled={history.loading || !history.previous.length} onClick={() => { void pager.load("previous"); }}>上一页</Button>
        <span className="type-caption text-[var(--fg-2)]">第 {history.previous.length + 1} 页</span>
        <Button size="sm" variant="outline" disabled={history.loading || !history.nextCursor || history.previous.length >= CANVAS_HISTORY_MAX_PAGES - 1} onClick={() => { void pager.load("next"); }}>下一页</Button>
      </div>
      {history.previous.length >= CANVAS_HISTORY_MAX_PAGES - 1 ? <p className="px-4 type-caption text-[var(--fg-2)]">已到达本次浏览上限。</p> : null}
      {history.loading ? <p role="status" className="px-4 type-caption text-[var(--fg-2)]">正在加载历史…</p> : null}
      {history.error ? <div className="px-4">
        <p role="alert" className="type-caption text-[var(--danger-fg)]">{history.error}</p>
        <Button size="sm" variant="outline" onClick={() => { void pager.load("retry"); }}>重试本页</Button>
      </div> : null}
      {!history.loading && !history.error && history.items.length === 0 ? <p className="px-4 type-caption text-[var(--fg-2)]">此页没有执行记录。</p> : null}
    </> : null}
    <CanvasInspectorExecutionHistory document={document} selectedNodeId={selectedNodeId} executions={visible} OutputComponent={OutputComponent}
      renderExecutionTools={open ? (execution) => {
        const saved = execution as CanvasHistoricalExecution;
        return <div className="mt-2 grid gap-2">
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant={history.a?.id === execution.id ? "primary" : "outline"} onClick={() => pager.select("a", saved)}>比较 A</Button>
            <Button size="sm" variant={history.b?.id === execution.id ? "primary" : "outline"} onClick={() => pager.select("b", saved)}>比较 B</Button>
            <Button size="sm" variant="outline" disabled={!completeExecutionProvenance(saved)} onClick={() => branch(saved)}>从此记录分支</Button>
          </div>
          {!completeExecutionProvenance(saved) ? <p className="type-caption text-[var(--fg-2)]">旧记录的来源快照不完整，无法创建固定分支。</p> : null}
          <details className="type-caption text-[var(--fg-2)]">
            <summary className="min-h-11 cursor-pointer">执行与来源标识</summary>
            <dl className="grid gap-1 break-all">
              <dt>执行</dt><dd>{saved.id}</dd>
              <dt>定义哈希</dt><dd>{saved.definition_hash ?? "未知"}</dd>
              <dt>输入哈希</dt><dd>{saved.input_hash ?? "未知"}</dd>
              <dt>处理器版本</dt><dd>{saved.processor_version ?? "未知"}</dd>
            </dl>
          </details>
        </div>;
      } : undefined} />
  </div>;
}

function ComparisonPanel({ a, b, onClear }: { a: CanvasHistoricalExecution | null; b: CanvasHistoricalExecution | null; onClear: () => void }) {
  const comparison = useMemo(() => a && b ? compareHistoricalExecutions(a, b) : null, [a, b]);
  if (!a && !b) return null;
  return <section aria-label="历史 A/B 比较" className="mx-4 grid gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3">
    <div className="flex justify-between gap-2">
      <h3 className="type-caption font-medium">历史 A/B 比较</h3>
      <Button size="sm" variant="outline" onClick={onClear}>清除</Button>
    </div>
    <p className="break-all type-caption text-[var(--fg-2)]">A：{a?.id ?? "未选择"}<br />B：{b?.id ?? "未选择"}</p>
    {comparison ? <>
      {comparison.incomplete ? <p className="type-caption text-[var(--warning-fg)]">存在缺失快照，比较不完整，不能据此认定相同。</p> : null}
      {comparison.truncated ? <p className="type-caption text-[var(--warning-fg)]">比较内容已截断；仅显示有界差异。</p> : null}
      {!comparison.rows.length ? <p className="type-caption text-[var(--fg-2)]">{comparison.incomplete || comparison.truncated ? "可见范围内未发现差异。" : "保存的参数、输入、输出与处理器信息一致。"}</p> : null}
      <div className="max-h-96 overflow-y-auto">
        {comparison.rows.map((row) => <div key={row.path} className="border-t border-[var(--border-subtle)] py-2 type-caption">
          <p className="break-all font-medium">{row.path}</p>
          <p className="whitespace-pre-wrap break-all text-[var(--fg-2)]">A：{row.a}</p>
          <p className="whitespace-pre-wrap break-all text-[var(--fg-2)]">B：{row.b}</p>
        </div>)}
      </div>
    </> : <p className="type-caption text-[var(--fg-2)]">再选择另一条执行记录，可跨页比较。</p>}
  </section>;
}
