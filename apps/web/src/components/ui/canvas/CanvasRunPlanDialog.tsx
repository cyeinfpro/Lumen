"use client";

import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useModalLayer, usePortalReady } from "@/components/ui/primitives/mobile/useModalLayer";
import { Button, IconButton, Select } from "@/components/ui/primitives";
import { useUserQueryScope } from "@/lib/queries/userScope";
import type { CanvasDocument } from "@/lib/canvas/types";
import { CanvasRunPlanFields, planInputClass } from "./CanvasRunPlanFields";
import { CanvasPlanPreviewSummary, CanvasPlanRunRepair } from "./CanvasRunPlanResults";
import { useCanvasRunPlan } from "./useCanvasRunPlan";

interface Props {
  open: boolean; canvasId: string; document: CanvasDocument;
  saveDraft: () => Promise<void>; onClose: () => void;
}
export function CanvasRunPlanDialog(props: Props) {
  const identity = useUserQueryScope();
  return identity.enabled ? <CanvasRunPlanDialogInner key={`${identity.userId}:${props.canvasId}`} {...props} /> : null;
}
function CanvasRunPlanDialogInner({ open, canvasId, document, saveDraft, onClose }: Props) {
  const model = useCanvasRunPlan(canvasId, document, saveDraft, open);
  const portalReady = usePortalReady(), heading = useId();
  const root = useRef<HTMLElement>(null), close = useRef<HTMLButtonElement>(null);
  useBodyScrollLock(open);
  const onKeyDown = useModalLayer({ open, rootRef: root, initialFocusRef: close, onClose });
  const { recover, invalidatePreview } = model;
  useEffect(() => { if (open) void recover(); else invalidatePreview(); }, [open, recover, invalidatePreview]);
  if (!open || !portalReady) return null;
  const { state, busy } = model;
  return createPortal(<div className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center sm:items-center sm:p-5">
    <button type="button" tabIndex={-1} aria-label="关闭运行计划" className="absolute inset-0 cursor-default bg-[var(--surface-scrim)]" onClick={onClose} />
    <section ref={root} role="dialog" aria-modal="true" aria-labelledby={heading} tabIndex={-1} onKeyDown={onKeyDown}
      className="mobile-dialog-panel surface-dialog relative flex max-h-[92dvh] w-full max-w-2xl flex-col overflow-hidden max-sm:rounded-t-[var(--radius-sheet)]">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
        <h2 id={heading} className="type-section-title">画布运行计划</h2><IconButton ref={close} aria-label="关闭计划面板" onClick={onClose}><X className="h-4 w-4" /></IconButton>
      </header>
      <div className="mobile-dialog-scroll space-y-4 overflow-y-auto p-4">
        {state.error ? <p role="alert" className="break-words type-body-sm text-[var(--danger)]">{state.error}</p> : null}
        {state.pending ? <section className="space-y-2 rounded-[var(--radius-control)] border border-[var(--border)] p-3">
          <p className="type-body-sm">保留原预算、步骤与幂等标识。关闭或刷新不会自动重新生成。</p>
          <Button variant="secondary" disabled={busy} onClick={() => void model.queryPending()}>查询原提交</Button>
          <details><summary className="cursor-pointer type-caption">原请求仍未被确认？</summary>
            <p className="my-2 type-caption text-[var(--fg-2)]">只重发已保存的同一请求与同一标识，不创建新任务意图。服务端若已接收，会返回原运行。</p>
            <Button variant="secondary" disabled={busy} onClick={() => void model.queryPending(true)}>重发原幂等请求</Button>
          </details>
        </section> : null}
        <CanvasRunPlanFields form={model.form} onChange={model.updateForm} graph={model.graph} selected={model.selected} document={document} disabled={busy || !!state.pending} />
        {state.preview ? <CanvasPlanPreviewSummary key={state.preview.plan.plan_hash} preview={state.preview} /> : null}
        {model.runs.length ? <label className="block space-y-1 type-body-sm">近期批量运行
          <Select aria-label="查看批量运行" className={planInputClass} disabled={busy || !!state.pending} value={state.run?.id ?? ""} onChange={event => { if (event.target.value) void model.refreshRun(event.target.value); }}>
            <option value="">选择运行查看状态与修复</option>{model.runs.map(run => <option value={run.id} key={run.id}>{run.id} · {run.status}</option>)}
          </Select>
        </label> : null}
        {state.run ? <CanvasPlanRunRepair key={state.run.id} run={state.run} busy={busy || !!state.pending} onRefresh={() => void model.refreshRun(state.run!.id)} onRepair={(ids, budget) => void model.repair(ids, budget)} /> : null}
      </div>
      <footer className="mobile-dialog-footer flex flex-wrap gap-2 border-t border-[var(--border)] bg-[var(--bg-1)] p-4">
        <Button variant="secondary" disabled={busy || !!state.pending} onClick={() => void model.preview()}>{busy ? "处理中…" : "保存并预览计划"}</Button>
        <Button disabled={!model.canStart} onClick={() => void model.start()}>确认运行此计划</Button>
      </footer>
    </section>
  </div>, window.document.body);
}
