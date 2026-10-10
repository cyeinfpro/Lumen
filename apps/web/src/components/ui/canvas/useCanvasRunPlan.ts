"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { canvasPlanIntents, getCanvasPlanRun, listCanvasPlanRuns, previewCanvasPlan } from "@/lib/api/canvasPlans";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { blurActiveCanvasEditor } from "@/lib/canvas/interaction";
import { CanvasPlanPendingError } from "@/lib/canvas/runPlanIntentClient";
import { canvasPlanCanStart, canvasPlanReducer, initialCanvasPlanState } from "@/lib/canvas/runPlanMachine";
import { canvasRepairCandidates, parseCanvasPlanBudget } from "@/lib/canvas/runPlanValidation";
import type { CanvasPlanRunDetail } from "@/lib/canvas/runPlanTypes";
import { useCanvasPlanRunRefresh } from "./useCanvasPlanRunRefresh";
import type { CanvasDocument } from "@/lib/canvas/types";
import { canvasQueryKeys } from "@/lib/queries/canvases";
import { canvasActiveNodeIds, canvasUncertainNodeIds } from "./canvasRunReadiness";
import { canvasPlanGraphKey, canvasPlanCandidateNodes, canvasPlanFormInput, initialCanvasPlanForm, type CanvasPlanForm } from "./canvasPlanForm";
import { useCanvasStore, useCanvasStoreApi } from "./CanvasStoreProvider";

export function useCanvasRunPlan(canvasId: string, document: CanvasDocument, saveDraft: () => Promise<void>, open: boolean) {
  const client = useQueryClient(), store = useCanvasStoreApi();
  const graph = useCanvasStore(state => state.graph);
  const revision = useCanvasStore(state => state.revision);
  const selected = useCanvasStore(state => state.selectedNodeIds);
  const [form, setForm] = useState(initialCanvasPlanForm);
  const [state, dispatch] = useReducer(canvasPlanReducer, initialCanvasPlanState);
  const [busy, setBusy] = useState(false);
  const [runs, setRuns] = useState<Array<{ id: string; kind: string; status: string }>>([]);
  const busyRef = useRef(false), requestRef = useRef(0), alive = useRef(true);
  const formRef = useRef(form), documentRef = useRef(document);
  useLayoutEffect(() => { formRef.current = form; documentRef.current = document; }, [form, document]);
  const context = useCallback(() => {
    const current = store.getState();
    return JSON.stringify([current.revision, canvasPlanGraphKey(current.graph), current.selectedNodeIds,
      formRef.current, documentRef.current.selections]);
  }, [store]);
  const contextKey = useMemo(() => JSON.stringify([revision, canvasPlanGraphKey(graph), selected, form, document.selections]),
    [revision, graph, selected, form, document.selections]);
  useEffect(() => {
    if (state.previewContext && state.previewContext !== contextKey) dispatch({ type: "invalidate" });
  }, [contextKey, state.previewContext]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; requestRef.current += 1; }; }, []);
  const perform = useCallback(async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true);
    const identity = getPrivateIdentitySnapshot();
    try {
      if (!identity.userId) throw new Error("登录身份尚未确认");
      await action();
    } catch (error) {
      if (!alive.current || !isPrivateIdentitySnapshotCurrent(identity)) return;
      if (error instanceof CanvasPlanPendingError) dispatch({ type: "unknown", pending: error.pending });
      else dispatch({ type: "error", error: planErrorMessage(error) });
    } finally { busyRef.current = false; if (alive.current) setBusy(false); }
  }, []);
  const accept = useCallback((run: CanvasPlanRunDetail) => {
    if (!alive.current) return;
    dispatch({ type: "admitted", run });
    void client.invalidateQueries({ queryKey: canvasQueryKeys.detail(canvasId) });
  }, [canvasId, client]);
  const refreshProjection = useCallback((run: CanvasPlanRunDetail) => dispatch({ type: "admitted", run, refresh: true }), []);
  useCanvasPlanRunRefresh(canvasId, state.run?.id, document, open && !busy && !state.pending, refreshProjection);
  const recover = useCallback(() => perform(async () => {
    const identity = getPrivateIdentitySnapshot();
    const pending = await canvasPlanIntents.pending(canvasId);
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current) return;
    if (pending) {
      dispatch({ type: "unknown", pending });
      const run = await canvasPlanIntents.resolve(pending);
      if (run && isPrivateIdentitySnapshotCurrent(identity)) accept(run);
    }
    const items = await listCanvasPlanRuns(canvasId);
    if (isPrivateIdentitySnapshotCurrent(identity) && alive.current) setRuns(items);
  }), [accept, canvasId, perform]);
  const preview = () => perform(async () => {
    const identity = getPrivateIdentitySnapshot(), generation = ++requestRef.current;
    const pending = await canvasPlanIntents.pending(canvasId);
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current) return;
    if (pending) throw new CanvasPlanPendingError(pending);
    blurActiveCanvasEditor();
    const graphKey = canvasPlanGraphKey(store.getState().graph);
    const formKey = JSON.stringify(formRef.current);
    await saveDraft();
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current) return;
    const current = store.getState();
    assertPlanNodesIdle(formRef.current, current.graph, current.selectedNodeIds, documentRef.current);
    if (current.saveState === "conflict" || current.pendingOperations.length || current.inFlightOperationCount ||
      graphKey !== canvasPlanGraphKey(current.graph) || formKey !== JSON.stringify(formRef.current)) throw new Error("草稿已变化或尚未保存，请重新预览");
    const input = canvasPlanFormInput(formRef.current, current.graph, current.selectedNodeIds, current.revision);
    const captured = context();
    dispatch({ type: "previewing", generation });
    const result = await previewCanvasPlan(canvasId, input);
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current || generation !== requestRef.current) return;
    if (context() !== captured) throw new Error("预览期间画布或选项已变化，请重新预览");
    dispatch({ type: "preview", generation, preview: result, input, context: captured });
  });
  const start = () => perform(async () => {
    if (!canvasPlanCanStart(state, context()) || !state.input || !state.preview) throw new Error("计划已失效，请重新预览");
    const identity = getPrivateIdentitySnapshot(), captured = context();
    // Requote before admission: changing capability or prices invalidates approval.
    const fresh = await previewCanvasPlan(canvasId, state.input);
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current) return;
    if (context() !== captured || fresh.plan.plan_hash !== state.preview.plan.plan_hash) throw new Error("画布、能力或价格已变化，请重新预览确认");
    assertPlanNodesIdle(formRef.current, store.getState().graph, store.getState().selectedNodeIds, documentRef.current);
    dispatch({ type: "submitting" });
    accept(await canvasPlanIntents.submit(canvasId, { kind: "start", body: { ...state.input, plan_hash: state.preview.plan.plan_hash } }));
  });
  const refreshRun = (runId: string) => perform(async () => {
    const identity = getPrivateIdentitySnapshot();
    const run = await getCanvasPlanRun(canvasId, runId);
    if (isPrivateIdentitySnapshotCurrent(identity)) accept(run);
  });
  const repair = (executionIds: string[], budget: string) => perform(async () => {
    if (!state.run || state.pending) throw new Error("请先查询原运行状态");
    const identity = getPrivateIdentitySnapshot();
    const run = await getCanvasPlanRun(canvasId, state.run.id);
    if (!isPrivateIdentitySnapshotCurrent(identity) || !alive.current) return;
    const allowed = new Set(canvasRepairCandidates(run).map(execution => execution.id));
    if (!executionIds.length || executionIds.some(id => !allowed.has(id))) throw new Error("仅可修复最新且已确认失败的步骤");
    dispatch({ type: "submitting" });
    accept(await canvasPlanIntents.submit(canvasId, { kind: "repair", run_id: run.id,
      body: { execution_ids: [...executionIds].sort(), additional_budget_micro: parseCanvasPlanBudget(budget) } }));
  });
  const queryPending = (replay = false) => perform(async () => {
    if (!state.pending) return;
    const run = replay ? await canvasPlanIntents.replay(state.pending) : await canvasPlanIntents.resolve(state.pending);
    if (run) accept(run);
  });
  const invalidatePreview = useCallback(() => { requestRef.current += 1; dispatch({ type: "invalidate" }); }, []);
  const updateForm = (next: CanvasPlanForm) => {
    formRef.current = next; setForm(next); dispatch({ type: "invalidate" });
  };
  return { form, updateForm, state, busy, runs, graph, selected, recover, preview, start, refreshRun, repair,
    queryPending, invalidatePreview, canStart: canvasPlanCanStart(state, contextKey) && !busy };
}
function planErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : "计划操作失败";
  type PlanError = { details?: { missing_node_ids?: string[] } };
  const payload = (error as { payload?: { detail?: { error?: PlanError }; error?: PlanError } })?.payload;
  const missing = payload?.detail?.error?.details?.missing_node_ids ?? payload?.error?.details?.missing_node_ids;
  return missing?.length ? `${message}；还须显式选择失败节点：${missing.join("、")}` : message;
}

function assertPlanNodesIdle(form: CanvasPlanForm, graph: CanvasDocument["graph"], selected: string[], document: CanvasDocument) {
  const active = canvasActiveNodeIds(document), unknown = canvasUncertainNodeIds(document);
  for (const node of canvasPlanCandidateNodes(graph, selected, form.kind, form.reuseOutputs)) {
    if (form.reuseOutputs[node.id]) continue;
    if (unknown.has(node.id)) throw new Error(`节点 ${node.id} 的提交结果未知，请先查询原任务`);
    if (active.has(node.id)) throw new Error(`节点 ${node.id} 已有运行中的任务`);
  }
}
