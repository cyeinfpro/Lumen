import { useCallback, useEffect, useRef, type MutableRefObject } from "react";

import { ApiError } from "@/lib/apiClient";
import { onOnlineRestore, startConnectivity } from "@/lib/connectivity";
import { applyCanvasMutations } from "@/lib/api/canvases";
import {
  RetryableAutosaveBatchReader,
  SerialAutosave,
  takeAtomicAutosaveOperations,
  type AutosaveBatch,
} from "@/lib/canvas/autosave";
import { canvasGraphReadyToSave } from "@/lib/canvas/graph";
import { decideCanvasRemoteSync } from "@/lib/canvas/documentMerge";
import { blurActiveCanvasEditor } from "@/lib/canvas/interaction";
import {
  canvasDraftKey,
  canvasSaveBatchMatchesPending,
  deleteCanvasDraft,
  deleteCanvasEmergencyDraft,
  deleteCanvasSaveBatch,
  getCanvasDraft,
  getCanvasEmergencyDraft,
  getCanvasSaveBatch,
  isSuspiciousEmptyCanvasDraft,
  listCanvasDrafts,
  putCanvasEmergencyDraft,
  putCanvasSaveBatch,
  putCanvasDraft,
  SerialCanvasDraftWriter,
  type CanvasDraft,
  type PersistedCanvasSaveBatch,
} from "@/lib/canvas/persistence";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import {
  cloneCanonicalCanvasGraph,
  normalizePendingOperationGroupSizes,
} from "@/lib/canvas/storeHelpers";
import type {
  CanvasDocument,
  CanvasGraph,
  CanvasOperation,
} from "@/lib/canvas/types";
import { createBroadcastChannel } from "@/shared/realtime/browser";
import { toast } from "@/components/ui/primitives";

import {
  canvasClientLeaseIsActive,
  isCanvasCoordinationBroadcast,
  randomId,
  sameGraph,
} from "./canvasPersistenceDomain";
export { browserClientId, randomId, useCanvasClientLease } from "./canvasPersistenceDomain";

interface SavePayload {
  baseRevision: number;
  clientId: string;
  mutationId: string;
  operations: CanvasOperation[];
}

export function useRemoteDocumentSync(
  document: CanvasDocument,
  store: CanvasEditorStore,
  inFlightOperationCount: number,
) {
  useEffect(() => {
    const state = store.getState();
    const decision = decideCanvasRemoteSync(document.revision, {
      revision: state.revision,
      pendingOperationCount: state.pendingOperations.length,
      inFlightOperationCount: state.inFlightOperationCount,
      activeInteractionCount: state.activeInteractionCount,
      editingNodeId: state.editingNodeId,
    });
    if (decision === "ignore" || decision === "defer") return;
    if (decision === "replace") {
      state.replaceFromRemote(document.graph, document.revision);
      return;
    }
    state.markConflict("版本冲突：远端画布已更新，本地修改已暂停保存。");
  }, [document.graph, document.revision, inFlightOperationCount, store]);
}

export function useCanvasDraftPersistence({
  canvasId,
  clientId,
  document,
  onDurabilityWarning,
  recoveredSaveBatchRef,
  store,
}: {
  canvasId: string;
  clientId: string;
  document: CanvasDocument;
  onDurabilityWarning: (message: string | null) => void;
  recoveredSaveBatchRef: MutableRefObject<PersistedCanvasSaveBatch | null>;
  store: CanvasEditorStore;
}) {
  const initialDocumentRef = useRef(document);
  useEffect(() => {
    let canceled = false;
    let ready = false;
    let timer: number | undefined;
    let migratedDraftClientId: string | null = null;

    const writer = new SerialCanvasDraftWriter(
      async () => {
        const state = store.getState();
        const action =
          state.pendingOperations.length > 0
            ? putCanvasDraft({
                canvas_id: canvasId,
                client_id: clientId,
                base_revision: state.revision,
                graph: state.graph,
                operations: state.pendingOperations,
                operation_group_sizes: state.pendingOperationGroupSizes,
                updated_at: Date.now(),
              })
            : deleteCanvasDraft(canvasId, clientId);
        await action;
        deleteCanvasEmergencyDraft(canvasId, clientId);
        onDurabilityWarning(null);
        if (migratedDraftClientId && migratedDraftClientId !== clientId) {
          await deleteCanvasDraft(canvasId, migratedDraftClientId).catch(
            () => undefined,
          );
          deleteCanvasEmergencyDraft(canvasId, migratedDraftClientId);
          migratedDraftClientId = null;
        }
      },
      () => {
        onDurabilityWarning(
          "浏览器本地恢复存储不可用；请保持页面打开，系统仍会尝试云端保存。",
        );
      },
    );
    const persist = () => {
      void writer.request();
    };
    let lastGraph = store.getState().graph;
    let lastPendingOperations = store.getState().pendingOperations;
    let lastPendingOperationGroupSizes =
      store.getState().pendingOperationGroupSizes;
    let lastRevision = store.getState().revision;
    const unsubscribe = store.subscribe((state) => {
      if (!ready) return;
      if (
        state.graph === lastGraph &&
        state.pendingOperations === lastPendingOperations &&
        state.pendingOperationGroupSizes ===
          lastPendingOperationGroupSizes &&
        state.revision === lastRevision
      ) {
        return;
      }
      lastGraph = state.graph;
      lastPendingOperations = state.pendingOperations;
      lastPendingOperationGroupSizes = state.pendingOperationGroupSizes;
      lastRevision = state.revision;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(persist, 180);
    });

    const setup = async () => {
      try {
        const recovery = await loadCanvasDraftRecovery(canvasId, clientId);
        if (canceled) return;
        migratedDraftClientId = await applyCanvasDraftRecovery({
          canvasId,
          clientId,
          initialDocument: initialDocumentRef.current,
          recoveredSaveBatchRef,
          recovery,
          store,
        });
      } finally {
        if (!canceled) {
          ready = true;
          persist();
        }
      }
    };

    const persistOnPageHide = () => {
      blurActiveCanvasEditor();
      if (!ready) return;
      if (!persistCanvasEmergencySnapshot(canvasId, clientId, store)) {
        onDurabilityWarning(
          "页面关闭前无法写入紧急恢复副本；请等待云端保存完成。",
        );
      }
      persist();
    };
    const persistOnVisibilityChange = () => {
      if (window.document.visibilityState !== "hidden") return;
      blurActiveCanvasEditor();
      if (!ready) return;
      if (!persistCanvasEmergencySnapshot(canvasId, clientId, store)) {
        onDurabilityWarning(
          "页面进入后台前无法写入紧急恢复副本；请等待云端保存完成。",
        );
      }
      persist();
    };
    window.addEventListener("pagehide", persistOnPageHide);
    window.document.addEventListener(
      "visibilitychange",
      persistOnVisibilityChange,
    );
    void setup();
    return () => {
      canceled = true;
      unsubscribe?.();
      if (timer !== undefined) window.clearTimeout(timer);
      window.removeEventListener("pagehide", persistOnPageHide);
      window.document.removeEventListener(
        "visibilitychange",
        persistOnVisibilityChange,
      );
      if (ready) {
        persistCanvasEmergencySnapshot(canvasId, clientId, store);
        persist();
      }
    };
  }, [canvasId, clientId, onDurabilityWarning, recoveredSaveBatchRef, store]);
}

interface LoadedCanvasDraftRecovery {
  draft: CanvasDraft | null;
  draftClientId: string;
  persistedSaveBatch: PersistedCanvasSaveBatch | null;
}

async function loadCanvasDraftRecovery(
  canvasId: string,
  clientId: string,
): Promise<LoadedCanvasDraftRecovery> {
  let draft = await getCanvasDraft(canvasId, clientId).catch(() => null);
  if (!draft) {
    const drafts = await listCanvasDrafts(canvasId).catch(() => []);
    for (const candidate of drafts) {
      if (
        candidate.client_id !== clientId &&
        !(await canvasClientLeaseIsActive(canvasId, candidate.client_id))
      ) {
        draft = candidate;
        break;
      }
    }
  }
  const emergency = getCanvasEmergencyDraft(canvasId, clientId);
  if (
    emergency &&
    (emergency.client_id === clientId ||
      !(await canvasClientLeaseIsActive(canvasId, emergency.client_id))) &&
    (!draft || emergency.updated_at > draft.updated_at)
  ) {
    draft = {
      ...emergency,
      key: canvasDraftKey(canvasId, emergency.client_id),
    };
  }
  const draftClientId = draft?.client_id ?? clientId;
  const persistedSaveBatch = await getCanvasSaveBatch(
    canvasId,
    draftClientId,
  ).catch(() => null);
  return { draft, draftClientId, persistedSaveBatch };
}

async function applyCanvasDraftRecovery({
  canvasId,
  clientId,
  initialDocument,
  recoveredSaveBatchRef,
  recovery,
  store,
}: {
  canvasId: string;
  clientId: string;
  initialDocument: CanvasDocument;
  recoveredSaveBatchRef: MutableRefObject<PersistedCanvasSaveBatch | null>;
  recovery: LoadedCanvasDraftRecovery;
  store: CanvasEditorStore;
}): Promise<string | null> {
  const { draft, draftClientId, persistedSaveBatch } = recovery;
  if (!draft || draft.operations.length === 0) {
    deleteCanvasEmergencyDraft(canvasId, draftClientId);
    if (persistedSaveBatch) {
      await deleteCanvasSaveBatch(canvasId, draftClientId).catch(
        () => undefined,
      );
    }
    recoveredSaveBatchRef.current = null;
    return null;
  }
  if (
    isSuspiciousEmptyCanvasDraft(
      draft.graph,
      initialDocument.graph,
      draft.operations,
    )
  ) {
    await discardCanvasDraftRecovery(canvasId, draftClientId);
    recoveredSaveBatchRef.current = null;
    toast.error("已忽略异常空白草稿，并恢复服务器画布。");
    return null;
  }
  if (canvasDraftMatchesServer(draft, initialDocument)) {
    await discardCanvasDraftRecovery(canvasId, draftClientId);
    recoveredSaveBatchRef.current = null;
    return null;
  }
  const current = store.getState();
  if (canvasEditorChangedSinceMount(current, initialDocument)) {
    current.markConflict(
      "本地草稿恢复期间画布已发生新修改，请采用远端或另存副本。",
    );
    return null;
  }
  const status = resolveCanvasDraftRecoveryStatus(
    draft,
    initialDocument,
    persistedSaveBatch,
  );
  if (persistedSaveBatch && !status.recoverableSaveBatch) {
    await deleteCanvasSaveBatch(canvasId, draftClientId).catch(() => undefined);
  }
  recoveredSaveBatchRef.current = status.recoverableSaveBatch;
  store.setState({
    graph: cloneCanonicalCanvasGraph(draft.graph),
    revision: draft.base_revision,
    pendingOperations: draft.operations,
    pendingOperationGroupSizes: normalizePendingOperationGroupSizes(
      draft.operations.length,
      draft.operation_group_sizes,
    ),
    history: [],
    future: [],
    saveState: status.saveState,
    saveMessage: status.saveMessage,
  });
  return draftClientId === clientId ? null : draftClientId;
}

function persistCanvasEmergencySnapshot(
  canvasId: string,
  clientId: string,
  store: CanvasEditorStore,
): boolean {
  const state = store.getState();
  if (state.pendingOperations.length === 0) {
    deleteCanvasEmergencyDraft(canvasId, clientId);
    return true;
  }
  return putCanvasEmergencyDraft({
    canvas_id: canvasId,
    client_id: clientId,
    base_revision: state.revision,
    graph: state.graph,
    operations: state.pendingOperations,
    operation_group_sizes: state.pendingOperationGroupSizes,
    updated_at: Date.now(),
  });
}

function resolveCanvasDraftRecoveryStatus(
  draft: CanvasDraft,
  initialDocument: CanvasDocument,
  persistedSaveBatch: PersistedCanvasSaveBatch | null,
): {
  recoverableSaveBatch: PersistedCanvasSaveBatch | null;
  saveState: "conflict" | "dirty";
  saveMessage: string;
} {
  const recoverableSaveBatch =
    persistedSaveBatch &&
    canvasSaveBatchMatchesPending(
      persistedSaveBatch,
      draft.base_revision,
      draft.operations,
    )
      ? persistedSaveBatch
      : null;
  const conflict =
    draft.base_revision !== initialDocument.revision && !recoverableSaveBatch;
  return {
    recoverableSaveBatch,
    saveState: conflict ? "conflict" : "dirty",
    saveMessage: conflict
      ? "检测到基于旧版本的本地草稿，请采用远端或另存副本。"
      : recoverableSaveBatch
        ? "已恢复上次未确认的保存请求，正在安全重试。"
        : "已恢复未保存的本地草稿。",
  };
}

function canvasDraftMatchesServer(
  draft: CanvasDraft,
  initialDocument: CanvasDocument,
): boolean {
  return (
    sameGraph(draft.graph, initialDocument.graph) &&
    draft.base_revision <= initialDocument.revision
  );
}

function canvasEditorChangedSinceMount(
  current: ReturnType<CanvasEditorStore["getState"]>,
  initialDocument: CanvasDocument,
): boolean {
  return (
    current.revision !== initialDocument.revision ||
    current.pendingOperations.length > 0 ||
    current.history.length > 0 ||
    current.activeInteractionCount > 0 ||
    current.editingNodeId !== null ||
    !sameGraph(current.graph, initialDocument.graph)
  );
}

async function discardCanvasDraftRecovery(
  canvasId: string,
  clientId: string,
): Promise<void> {
  deleteCanvasEmergencyDraft(canvasId, clientId);
  await Promise.all([
    deleteCanvasDraft(canvasId, clientId).catch(() => undefined),
    deleteCanvasSaveBatch(canvasId, clientId).catch(() => undefined),
  ]);
}

export function useCanvasTabCoordination({
  canvasId,
  clientId,
  tabId,
  store,
  onRefetch,
}: {
  canvasId: string;
  clientId: string;
  tabId: string;
  store: CanvasEditorStore;
  onRefetch: () => Promise<unknown>;
}) {
  const channelRef = useRef<BroadcastChannel | null>(null);
  const deferredRevisionRef = useRef(0);
  const onRefetchRef = useRef(onRefetch);
  useEffect(() => {
    onRefetchRef.current = onRefetch;
  }, [onRefetch]);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    let channel: BroadcastChannel;
    try {
      channel = createBroadcastChannel(`lumen:canvas:${canvasId}`);
    } catch {
      return;
    }
    channelRef.current = channel;
    const handleSavedRevision = (revision: number) => {
      const state = store.getState();
      const decision = decideCanvasRemoteSync(revision, {
        revision: state.revision,
        pendingOperationCount: state.pendingOperations.length,
        inFlightOperationCount: state.inFlightOperationCount,
        activeInteractionCount: state.activeInteractionCount,
        editingNodeId: state.editingNodeId,
      });
      if (decision === "ignore") {
        deferredRevisionRef.current = 0;
        return;
      }
      if (decision === "defer") {
        deferredRevisionRef.current = Math.max(
          deferredRevisionRef.current,
          revision,
        );
        return;
      }
      deferredRevisionRef.current = 0;
      if (decision === "replace") {
        void onRefetchRef.current();
        return;
      }
      state.markConflict("另一个标签页已保存更新，本地修改已暂停保存。");
    };
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const payload = event.data;
      if (!isCanvasCoordinationBroadcast(payload)) {
        return;
      }
      if (payload.type === "canvas.presence.ping") {
        if (payload.targetClientId === clientId) {
          try {
            channel.postMessage({
              type: "canvas.presence.pong",
              requestId: payload.requestId,
              clientId,
            });
          } catch {
            // Lease timestamps remain available when presence replies are blocked.
          }
        }
        return;
      }
      if (payload.type === "canvas.presence.pong") return;
      if (payload.type === "canvas.selection.changed") {
        void onRefetchRef.current();
        return;
      }
      if (payload.clientId === tabId) return;
      handleSavedRevision(payload.revision);
    };
    const unsubscribe = store.subscribe((state, previous) => {
      if (
        previous.inFlightOperationCount > 0 &&
        state.inFlightOperationCount === 0 &&
        deferredRevisionRef.current > 0
      ) {
        handleSavedRevision(deferredRevisionRef.current);
      }
    });
    return () => {
      unsubscribe();
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, [canvasId, clientId, store, tabId]);
  return useCallback(
    (revision: number) => {
      try {
        channelRef.current?.postMessage({
          type: "canvas.saved",
          clientId: tabId,
          revision,
        });
      } catch {
        // Cross-tab notification is optional; query refetch remains authoritative.
      }
    },
    [tabId],
  );
}

export function useCanvasAutosave({
  canvasId,
  clientId,
  pendingCount,
  recoveredSaveBatchRef,
  saveState,
  store,
  notifySaved,
  onDurabilityWarning,
  onSaved,
}: {
  canvasId: string;
  clientId: string;
  pendingCount: number;
  recoveredSaveBatchRef: MutableRefObject<PersistedCanvasSaveBatch | null>;
  saveState: string;
  store: CanvasEditorStore;
  notifySaved: (revision: number) => void;
  onDurabilityWarning: (message: string | null) => void;
  onSaved: (graph: CanvasGraph, revision: number) => void;
}): MutableRefObject<SerialAutosave<SavePayload> | null> {
  const autosaveRef = useRef<SerialAutosave<SavePayload> | null>(null);
  useEffect(() => {
    let retryTimer: number | undefined;
    let retryAttempt = 0;
    const clearRetryTimer = () => {
      if (retryTimer === undefined) return;
      window.clearTimeout(retryTimer);
      retryTimer = undefined;
    };
    const scheduleRetry = () => {
      clearRetryTimer();
      const delay = Math.min(30_000, 1_000 * 2 ** retryAttempt);
      retryAttempt = Math.min(retryAttempt + 1, 5);
      retryTimer = window.setTimeout(() => {
        retryTimer = undefined;
        void autosave.flush();
      }, delay);
    };
    const retryableBatches = new RetryableAutosaveBatchReader(() =>
      readCanvasSaveBatch(store, clientId, randomId(), recoveredSaveBatchRef),
    );
    const unsubscribeStore = store.subscribe((state) => {
      if (
        state.saveState === "conflict" ||
        (state.saveState === "saved" &&
          state.pendingOperations.length === 0 &&
          state.inFlightOperationCount === 0)
      ) {
        const recoveredClientId =
          recoveredSaveBatchRef.current?.client_id ?? null;
        recoveredSaveBatchRef.current = null;
        retryableBatches.discard();
        clearRetryTimer();
        retryAttempt = 0;
        void deleteCanvasSaveBatch(canvasId, clientId).catch(() => undefined);
        if (recoveredClientId && recoveredClientId !== clientId) {
          void deleteCanvasSaveBatch(canvasId, recoveredClientId).catch(
            () => undefined,
          );
        }
      }
    });
    const autosave = new SerialAutosave<SavePayload>({
      delayMs: 750,
      readBatch: () => {
        if (store.getState().saveState === "conflict") {
          retryableBatches.discard();
          return null;
        }
        return retryableBatches.read();
      },
      sendBatch: async (batch) => {
        store.getState().markSaving(batch.count);
        try {
          try {
            await putCanvasSaveBatch({
              canvas_id: canvasId,
              client_id: batch.payload.clientId,
              base_revision: batch.payload.baseRevision,
              mutation_id: batch.payload.mutationId,
              operations: batch.payload.operations,
              updated_at: Date.now(),
            });
          } catch {
            onDurabilityWarning(
              "保存请求无法写入本地恢复存储；系统仍会继续尝试云端保存。",
            );
          }
          const result = await applyCanvasMutations(canvasId, {
            base_revision: batch.payload.baseRevision,
            client_id: batch.payload.clientId,
            mutation_id: batch.payload.mutationId,
            operations: batch.payload.operations,
          });
          const acknowledged = store
            .getState()
            .acknowledgeOperations(batch.count, result.revision);
          retryableBatches.acknowledge(batch);
          clearRetryTimer();
          retryAttempt = 0;
          if (!acknowledged) return;
          if (
            recoveredSaveBatchRef.current?.mutation_id ===
            batch.payload.mutationId
          ) {
            recoveredSaveBatchRef.current = null;
          }
          void deleteCanvasSaveBatch(canvasId, batch.payload.clientId).catch(
            () => undefined,
          );
          const savedState = store.getState();
          if (savedState.pendingOperations.length === 0) {
            deleteCanvasEmergencyDraft(canvasId, batch.payload.clientId);
            onDurabilityWarning(null);
            onSaved(savedState.graph, result.revision);
          }
          notifySaved(result.revision);
        } catch (error) {
          const disposition = handleCanvasSaveError(store, error);
          if (disposition !== "retryable") {
            retryableBatches.discard();
            clearRetryTimer();
            if (disposition === "blocked") {
              recoveredSaveBatchRef.current = null;
              void deleteCanvasSaveBatch(
                canvasId,
                batch.payload.clientId,
              ).catch(() => undefined);
            }
          } else {
            scheduleRetry();
          }
          throw error;
        }
      },
      onError: () => undefined,
    });
    autosaveRef.current = autosave;
    const flush = () => {
      blurActiveCanvasEditor();
      void autosave.flush();
    };
    const flushOnVisibilityChange = () => {
      if (document.visibilityState === "hidden") flush();
    };
    const unsubscribeOnlineRestore = onOnlineRestore(() => {
      clearRetryTimer();
      retryAttempt = 0;
      flush();
    });
    const stopConnectivity = startConnectivity();
    const flushOnPageHide = () => flush();
    window.addEventListener("pagehide", flushOnPageHide);
    document.addEventListener("visibilitychange", flushOnVisibilityChange);
    return () => {
      window.removeEventListener("pagehide", flushOnPageHide);
      document.removeEventListener("visibilitychange", flushOnVisibilityChange);
      unsubscribeOnlineRestore();
      stopConnectivity();
      unsubscribeStore();
      clearRetryTimer();
      autosave.stop();
      autosaveRef.current = null;
    };
  }, [
    canvasId,
    clientId,
    notifySaved,
    onDurabilityWarning,
    onSaved,
    recoveredSaveBatchRef,
    store,
  ]);
  useEffect(() => {
    if (pendingCount > 0 && saveState === "dirty") {
      autosaveRef.current?.schedule();
    }
  }, [pendingCount, saveState]);
  return autosaveRef;
}

function readCanvasSaveBatch(
  store: CanvasEditorStore,
  clientId: string,
  mutationId: string,
  recoveredSaveBatchRef: MutableRefObject<PersistedCanvasSaveBatch | null>,
): AutosaveBatch<SavePayload> | null {
  const state = store.getState();
  const recoveredSaveBatch = recoveredSaveBatchRef.current;
  if (
    recoveredSaveBatch &&
    canvasSaveBatchMatchesPending(
      recoveredSaveBatch,
      state.revision,
      state.pendingOperations,
    )
  ) {
    return {
      count: recoveredSaveBatch.operations.length,
      payload: {
        baseRevision: recoveredSaveBatch.base_revision,
        clientId: recoveredSaveBatch.client_id,
        mutationId: recoveredSaveBatch.mutation_id,
        operations: recoveredSaveBatch.operations.slice(),
      },
    };
  }
  if (recoveredSaveBatch && state.pendingOperations.length > 0) {
    recoveredSaveBatchRef.current = null;
  }
  if (
    state.pendingOperations.length === 0 ||
    state.saveState === "conflict" ||
    (state.saveState === "error" && state.retryPrefixOperationCount === 0)
  ) {
    return null;
  }
  if (!canvasGraphReadyToSave(state.graph)) {
    state.markSaveError("画布规模超过当前保存上限，请拆分后重试。", false);
    return null;
  }
  const operations = takeAtomicAutosaveOperations(
    state.pendingOperations,
    state.pendingOperationGroupSizes,
  );
  if (operations.length === 0) {
    state.markSaveError(
      "单次画布操作超过保存上限，请撤销后缩小操作范围。",
      false,
    );
    return null;
  }
  return {
    count: operations.length,
    payload: {
      baseRevision: state.revision,
      clientId,
      mutationId,
      operations,
    },
  };
}

type CanvasSaveErrorDisposition = "retryable" | "blocked" | "conflict";

function handleCanvasSaveError(
  store: CanvasEditorStore,
  error: unknown,
): CanvasSaveErrorDisposition {
  if (error instanceof ApiError && error.code === "canvas_revision_conflict") {
    store
      .getState()
      .markConflict(
        "版本冲突：远端画布已更新。本地修改仍保留，但自动保存已暂停。",
      );
    return "conflict";
  }
  if (error instanceof ApiError && canvasSaveErrorIsBlocked(error.status)) {
    store
      .getState()
      .markSaveError(
        `${canvasBlockedSaveMessage(error)}。本地修改仍已保留，自动保存已暂停。`,
        false,
      );
    return "blocked";
  }
  store
    .getState()
    .markSaveError(error instanceof Error ? error.message : "保存失败");
  return "retryable";
}

function canvasSaveErrorIsBlocked(status: number): boolean {
  return (
    status >= 400 &&
    status < 500 &&
    status !== 408 &&
    status !== 425 &&
    status !== 429
  );
}

function canvasBlockedSaveMessage(error: ApiError): string {
  if (error.status === 401 || error.status === 403) {
    return "当前会话无权保存此画布，请重新登录或检查访问权限";
  }
  if (error.status === 404) {
    return "远端画布已不存在";
  }
  if (error.status === 413) {
    return "画布保存批次超过服务器限制，请拆分画布后重试";
  }
  return `${error.message}。请修正画布内容或另存副本`;
}
