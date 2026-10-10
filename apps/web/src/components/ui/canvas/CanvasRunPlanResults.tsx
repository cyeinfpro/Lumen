"use client";

import { useState } from "react";
import { Button } from "@/components/ui/primitives";
import { formatCanvasMicroAmount } from "@/lib/canvas/generationDetails";
import type { CanvasPlanPreview, CanvasPlanRunDetail } from "@/lib/canvas/runPlanTypes";
import { canvasRepairCandidates } from "@/lib/canvas/runPlanValidation";
import { planInputClass } from "./CanvasRunPlanFields";

export function CanvasPlanPreviewSummary({ preview }: { preview: CanvasPlanPreview }) {
  const [page, setPage] = useState(0);
  const plan = preview.plan, pages = Math.max(1, Math.ceil(plan.steps.length / 30));
  return <section aria-label="执行计划预览" className="space-y-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] p-3">
    <h3 className="type-card-title">预览：{plan.steps.length} 步 / {formatCanvasMicroAmount(preview.estimated_cost_micro)}</h3>
    <p className="break-all type-caption">保存版本 {plan.document_revision}；目标：{plan.target_node_ids.join("、")}</p>
    <p className="type-caption text-[var(--fg-2)]">每一步的模型、能力版本、价格及精确输入已绑定；运行前再次校验。</p>
    <ul className="space-y-2 type-caption">{plan.steps.slice(page * 30, (page + 1) * 30).map(step => <li key={step.node_id} className="break-all">
      {step.node_id}：{step.reuse ? `复用 ${step.reuse.execution_id} / 输出 ${step.reuse.output_index} / ${step.reuse.asset_id}`
        : `${step.effective_model || "模型待确认"} · ${formatCanvasMicroAmount(step.estimated_cost_micro)} · 下游输出序号 ${step.output_index}`}
      <span className="block text-[var(--fg-2)]">上游：{step.dependencies.join("、") || "无"}；能力版本：{step.capability_version || "不适用"}</span>
    </li>)}</ul>
    {pages > 1 ? <div className="flex justify-between type-caption"><button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>上页步骤</button><span>{page + 1} / {pages}</span><button type="button" disabled={page + 1 === pages} onClick={() => setPage(page + 1)}>下页步骤</button></div> : null}
  </section>;
}
export function CanvasPlanRunRepair({ run, busy, onRefresh, onRepair }: {
  run: CanvasPlanRunDetail; busy: boolean; onRefresh: () => void; onRepair: (ids: string[], budget: string) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [budget, setBudget] = useState("");
  const candidates = canvasRepairCandidates(run);
  const allowed = new Set(candidates.map(execution => execution.id));
  const selectedIds = selected.filter(id => allowed.has(id));
  return <section aria-label="计划运行与修复" className="space-y-3 rounded-[var(--radius-control)] border border-[var(--border)] p-3">
    <h3 className="type-card-title">运行 {run.status}</h3>
    <p className="break-all type-caption">{run.id}</p>
    <Button variant="secondary" disabled={busy} onClick={onRefresh}>查询原运行状态</Button>
    <p className="type-caption text-[var(--fg-2)]">仅修复已确认失败且没有成品的最新步骤。成功、部分成品和结果未知的步骤不可重试。</p>
    <div className="max-h-48 space-y-2 overflow-y-auto">
      {candidates.map(execution => <label key={execution.id} className="flex items-start gap-2 break-all type-caption">
        <input type="checkbox" disabled={busy} checked={selectedIds.includes(execution.id)} onChange={event => setSelected(event.target.checked ? [...selectedIds, execution.id] : selectedIds.filter(id => id !== execution.id))} />
        {execution.node_id} / {execution.id} / {execution.error_message || execution.error_code || "已失败"}
      </label>)}
    </div>
    {candidates.length ? <>
      <label className="block space-y-1 type-caption">额外准入预算（CNY 微单位）
        <input aria-label="修复额外预算" inputMode="numeric" className={planInputClass} value={budget} disabled={busy} onChange={event => setBudget(event.target.value)} />
      </label>
      <Button variant="secondary" disabled={busy || !selectedIds.length || !budget} onClick={() => onRepair(selectedIds, budget)}>修复所选失败步骤</Button>
    </> : <p className="type-caption text-[var(--fg-2)]">当前无可安全修复的步骤。</p>}
  </section>;
}
