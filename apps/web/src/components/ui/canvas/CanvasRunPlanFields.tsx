"use client";

import { useState } from "react";
import { Select } from "@/components/ui/primitives";
import type { CanvasDocument, CanvasGraph } from "@/lib/canvas/types";
import { canvasPlanCandidateNodes, type CanvasPlanForm } from "./canvasPlanForm";

export const planInputClass = "w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-[var(--fg-0)]";
export function CanvasRunPlanFields({ form, onChange, graph, selected, document, disabled }: {
  form: CanvasPlanForm; onChange: (form: CanvasPlanForm) => void; graph: CanvasGraph;
  selected: string[]; document: CanvasDocument; disabled: boolean;
}) {
  const [page, setPage] = useState(0);
  const candidates = canvasPlanCandidateNodes(graph, selected, form.kind, form.reuseOutputs);
  const pages = Math.max(1, Math.ceil(candidates.length / 30)), currentPage = Math.min(page, pages - 1);
  const patch = (update: Partial<CanvasPlanForm>) => onChange({ ...form, ...update });
  return <fieldset disabled={disabled} className="space-y-3">
    <legend className="type-card-title mb-2">运行范围与准入预算</legend>
    <label className="block space-y-1 type-body-sm">范围
      <Select aria-label="计划范围" className={planInputClass} value={form.kind} onChange={event => patch({ kind: event.target.value as CanvasPlanForm["kind"], outputIndices: {}, reuseOutputs: {} })}>
        <option value="selection">仅运行所选节点</option><option value="upstream">所选目标及上游依赖</option><option value="all">所有可运行节点</option>
      </Select>
    </label>
    <p className="type-caption text-[var(--fg-2)]">当前选择 {selected.length} 个节点。下方是配置候选，实际执行目标以服务端预览为准。</p>
    <label className="block space-y-1 type-body-sm">准入预算（CNY 微单位；1 元 = 1,000,000）
      <input aria-label="计划准入预算" inputMode="numeric" autoComplete="off" className={planInputClass} value={form.budget} onChange={event => patch({ budget: event.target.value })} placeholder="请显式填写整数" />
    </label>
    <p className="type-caption text-[var(--fg-2)]">预算用于提交时的准入预估，不保证最终结算上限；未知价格不会按零计算。</p>
    <label className="block space-y-1 type-body-sm">失败策略
      <Select aria-label="计划失败策略" className={planInputClass} value={form.failurePolicy} onChange={event => patch({ failurePolicy: event.target.value as CanvasPlanForm["failurePolicy"] })}>
        <option value="continue_independent">继续独立分支</option><option value="fail_fast">失败后阻止未开始步骤</option>
      </Select>
    </label>
    <label className="flex items-center gap-2 type-body-sm"><input type="checkbox" checked={form.autoSelect} onChange={event => patch({ autoSelect: event.target.checked })} />成功后选中成品（保留并发选择保护）</label>
    <details className="rounded-[var(--radius-control)] border border-[var(--border)] p-3">
      <summary className="cursor-pointer type-body-sm">输出选择与精确复用（{candidates.length} 个候选节点）</summary>
      <div className="mt-3 space-y-4">
        {candidates.slice(currentPage * 30, (currentPage + 1) * 30).map(node => {
          const count = Math.min(10, Math.max(1, Number(node.config.count) || 1));
          const choices = document.recent_executions.filter(execution => execution.node_id === node.id &&
            ["succeeded", "reused"].includes(execution.status)).flatMap(execution =>
              execution.outputs.map((output, index) => ({ execution, output, index })));
          const reuse = form.reuseOutputs[node.id];
          const reuseValue = reuse ? JSON.stringify([reuse.execution_id, reuse.output_index]) : "";
          return <div key={node.id} className="space-y-1">
            <p className="type-body-sm font-medium">{node.title || node.id}</p>
            {count > 1 && !reuse ? <label className="block type-caption">供下游使用的候选
              <Select aria-label={`节点 ${node.id} 输出候选`} className={planInputClass} value={form.outputIndices[node.id] ?? ""} onChange={event => {
                const outputIndices = { ...form.outputIndices };
                if (event.target.value === "") delete outputIndices[node.id]; else outputIndices[node.id] = Number(event.target.value);
                patch({ outputIndices });
              }}><option value="">请显式选择</option>{Array.from({ length: count }, (_, index) => <option key={index} value={index}>候选 {index + 1}（原始序号 {index}）</option>)}</Select>
            </label> : null}
            {choices.length ? <label className="block type-caption">已有成品
              <Select aria-label={`节点 ${node.id} 精确复用`} className={planInputClass} value={reuseValue} onChange={event => {
                const reuseOutputs = { ...form.reuseOutputs };
                if (!event.target.value) delete reuseOutputs[node.id];
                else { const [execution_id, output_index] = JSON.parse(event.target.value) as [string, number]; reuseOutputs[node.id] = { execution_id, output_index }; }
                patch({ reuseOutputs });
              }}><option value="">生成新成品，不自动复用</option>{choices.map(({ execution, output, index }) =>
                <option key={`${execution.id}:${index}`} value={JSON.stringify([execution.id, index])}>复用 {execution.id} / 输出 {index} / {output.image_id || output.video_id}</option>)}</Select>
            </label> : null}
          </div>;
        })}
        {pages > 1 ? <div className="flex items-center justify-between type-caption">
          <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页配置</button>
          <span>{currentPage + 1} / {pages}</span>
          <button type="button" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>下一页配置</button>
        </div> : null}
      </div>
    </details>
  </fieldset>;
}
