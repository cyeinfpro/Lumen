"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from "react";

import { ApiError } from "@/lib/apiClient";
import {
  applyCanvasMutations,
  createCanvas,
} from "@/lib/api/canvases";
import { SerialAutosave, type AutosaveBatch } from "@/lib/canvas/autosave";
import {
  canvasGraphReadyToSave,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import {
  deleteCanvasDraft,
  getCanvasDraft,
  putCanvasDraft,
} from "@/lib/canvas/persistence";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import type {
  CanvasDocument,
  CanvasNodeType,
  CanvasOperation,
} from "@/lib/canvas/types";
import {
  useCanvasQuery,
  useExecuteCanvasNodeMutation,
  usePatchCanvasMutation,
} from "@/lib/queries/canvases";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { Button, ErrorState, Spinner, toast } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { CanvasInspector } from "./CanvasInspector";
import { CanvasNodePalette } from "./CanvasNodePalette";
import { CanvasStoreProvider, useCanvasStore, useCanvasStoreApi } from "./CanvasStoreProvider";
import { CanvasTopBar } from "./CanvasTopBar";
import type { CanvasViewportApi } from "./CanvasViewport";
import { CanvasMobileToolbar } from "./mobile/CanvasMobileToolbar";

const CanvasViewport = dynamic(
  () => import("./CanvasViewport").then((module) => module.CanvasViewport),
  {
    ssr: false,
    loading: () => (
      <div className="grid h-full place-items-center bg-[var(--surface-canvas)]">
        <Spinner size={24} />
      </div>
    ),
  },
);

interface SavePayload {
  baseRevision: number;
  operations: CanvasOperation[];
}

const ACTIVE_EXECUTION_STATUSES = new Set([
  "pending",
  "ready",
  "queued",
  "running",
  "reconciling",
  "canceling",
]);

export function CanvasWorkspace({ canvasId }: { canvasId: string }) {
  const query = useCanvasQuery(canvasId);
  if (query.isLoading) {
    return (
      <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)]">
        <Spinner size={24} />
      </div>
    );
  }
  if (query.isError || !query.data) {
    return (
      <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)] px-6">
        <ErrorState
          title="画布加载失败"
          description={query.error instanceof Error ? query.error.message : "网络异常"}
          onRetry={() => query.refetch()}
        />
      </div>
    );
  }
  return (
    <CanvasStoreProvider
      key={query.data.id}
      graph={query.data.graph}
      revision={query.data.revision}
    >
      <CanvasWorkspaceInner
        canvasId={canvasId}
        document={query.data}
        onRefetch={() => query.refetch()}
      />
    </CanvasStoreProvider>
  );
}

function CanvasWorkspaceInner({
  canvasId,
  document,
  onRefetch,
}: {
  canvasId: string;
  document: CanvasDocument;
  onRefetch: () => Promise<unknown>;
}) {
  const router = useRouter();
  const isMobile = useIsMobile() === true;
  const store = useCanvasStoreApi();
  const graph = useCanvasStore((state) => state.graph);
  const revision = useCanvasStore((state) => state.revision);
  const saveState = useCanvasStore((state) => state.saveState);
  const saveMessage = useCanvasStore((state) => state.saveMessage);
  const pendingCount = useCanvasStore((state) => state.pendingOperations.length);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const addNode = useCanvasStore((state) => state.addNode);
  const [title, setTitle] = useState(document.title);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [viewportApi, setViewportApi] = useState<CanvasViewportApi | null>(null);
  const [clientId] = useState(() => browserClientId(canvasId));
  const submittingNodeIdsRef = useRef(new Set<string>());
  const patchCanvas = usePatchCanvasMutation(canvasId);
  const executeNode = useExecuteCanvasNodeMutation(canvasId);

  const mergedDocument = useMemo(
    () => ({ ...document, title, revision, graph }),
    [document, graph, revision, title],
  );
  const activeNodeIds = useMemo(
    () =>
      new Set(
        document.recent_executions
          .filter((execution) =>
            ACTIVE_EXECUTION_STATUSES.has(execution.status),
          )
          .map((execution) => execution.node_id),
    ),
    [document.recent_executions],
  );
  const runningNodeId = resolveRunningNodeId(
    executeNode.isPending,
    executeNode.variables?.nodeId,
    selectedNodeId,
    activeNodeIds,
  );

  useRemoteDocumentSync(document, store);
  useCanvasDraftPersistence({
    canvasId,
    clientId,
    document,
    store,
  });
  const notifySaved = useCanvasTabCoordination({
    canvasId,
    clientId,
    store,
    onRefetch,
  });
  const autosaveRef = useCanvasAutosave({
    canvasId,
    clientId,
    pendingCount,
    saveState,
    store,
    notifySaved,
  });
  useCanvasKeyboardShortcuts(store, viewportApi);

  const runNode = useCallback(
    async (nodeId: string) => {
      if (
        activeNodeIds.has(nodeId) ||
        submittingNodeIdsRef.current.has(nodeId)
      ) {
        toast.error("节点正在运行，请等待当前任务完成");
        return;
      }
      submittingNodeIdsRef.current.add(nodeId);
      try {
        const validation = validateCanvasNodeExecution(
          store.getState().graph,
          nodeId,
        );
        if (!validation.valid) {
          toast.error(validation.reason);
          return;
        }
        await autosaveRef.current?.flush();
        const state = store.getState();
        if (
          state.saveState === "conflict" ||
          state.pendingOperations.length > 0
        ) {
          toast.error("画布尚未保存，暂不能运行");
          return;
        }
        await executeNode.mutateAsync({ nodeId, revision: state.revision });
        toast.success("任务已提交");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "运行失败");
      } finally {
        submittingNodeIdsRef.current.delete(nodeId);
      }
    },
    [activeNodeIds, autosaveRef, executeNode, store],
  );

  const runSelected = useCallback(() => {
    const nodeId = store.getState().selectedNodeId;
    if (nodeId) void runNode(nodeId);
  }, [runNode, store]);

  const addAtCenter = useCallback(
    (type: CanvasNodeType) => {
      const offset = graph.nodes.length * 18;
      addNode(type, { x: 240 + offset, y: 180 + offset });
      setPaletteOpen(false);
    },
    [addNode, graph.nodes.length],
  );

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[var(--bg-0)] text-[var(--fg-0)]">
      <CanvasTopBar
        title={title}
        saveState={saveState}
        saveMessage={saveMessage}
        onRename={(nextTitle) => {
          setTitle(nextTitle);
          patchCanvas.mutate(
            { title: nextTitle },
            {
              onError: (error) => {
                setTitle(document.title);
                toast.error(error.message);
              },
            },
          );
        }}
        onFitView={() => viewportApi?.fitView()}
        onRunSelected={runSelected}
        onOpenInspector={() => setInspectorOpen(true)}
        running={Boolean(runningNodeId)}
      />

      {saveState === "conflict" ? (
        <ConflictBanner
          onAdoptRemote={async () => {
            await onRefetch();
            const fresh = await import("@/lib/api/canvases").then((module) =>
              module.getCanvas(canvasId),
            );
            store.getState().replaceFromRemote(fresh.graph, fresh.revision);
            setTitle(fresh.title);
          }}
          onKeepCopy={async () => {
            try {
              const copy = await createCanvas({
                title: `${title} 冲突副本`,
                description: document.description ?? "",
                graph,
              });
              router.push(`/projects/canvas/${copy.id}`);
            } catch (error) {
              toast.error(error instanceof Error ? error.message : "副本创建失败");
            }
          }}
        />
      ) : null}

      <div className="grid min-h-0 flex-1 md:grid-cols-[224px_minmax(0,1fr)_320px]">
        <aside className="hidden min-h-0 border-r border-[var(--border)] bg-[var(--bg-1)] md:flex md:flex-col">
          <header className="border-b border-[var(--border)] px-3 py-3">
            <p className="type-page-kicker">节点工具</p>
            <h2 className="type-card-title mt-1">添加节点</h2>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <CanvasNodePalette onAdd={addAtCenter} />
          </div>
        </aside>

        <main className="relative min-h-0 min-w-0">
          <CanvasViewport
            document={mergedDocument}
            onRunNode={runNode}
            onReady={setViewportApi}
          />
          <CanvasMobileToolbar
            onAdd={() => setPaletteOpen(true)}
            onFitView={() => viewportApi?.fitView()}
          />
        </main>

        <aside className="hidden min-h-0 border-l border-[var(--border)] bg-[var(--bg-1)] md:block">
          <CanvasInspector
            document={mergedDocument}
            onRunNode={runNode}
            runningNodeId={runningNodeId}
          />
        </aside>
      </div>

      <BottomSheet
        open={isMobile && (inspectorOpen || Boolean(selectedNodeId))}
        onClose={() => {
          setInspectorOpen(false);
          store.getState().selectNode(null);
        }}
        ariaLabel="节点检查器"
        snapPoints={["88%"]}
        className="mobile-dialog-sheet"
      >
        <div className="h-full min-h-0">
          <CanvasInspector
            document={mergedDocument}
            onRunNode={runNode}
            runningNodeId={runningNodeId}
          />
        </div>
      </BottomSheet>

      <BottomSheet
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        ariaLabel="添加节点"
        snapPoints={["72%"]}
        className="mobile-dialog-sheet"
      >
        <div className="mobile-dialog-scroll h-full overflow-y-auto p-4">
          <p className="type-page-kicker">节点工具</p>
          <h2 className="type-section-title mt-1 mb-4">添加节点</h2>
          <CanvasNodePalette onAdd={addAtCenter} compact />
        </div>
      </BottomSheet>
    </div>
  );
}

function useRemoteDocumentSync(
  document: CanvasDocument,
  store: CanvasEditorStore,
) {
  useEffect(() => {
    const state = store.getState();
    if (document.revision <= state.revision) return;
    if (state.pendingOperations.length === 0) {
      state.replaceFromRemote(document.graph, document.revision);
      return;
    }
    state.markConflict("版本冲突：远端画布已更新，本地修改已暂停保存。");
  }, [document.graph, document.revision, store]);
}

function useCanvasDraftPersistence({
  canvasId,
  clientId,
  document,
  store,
}: {
  canvasId: string;
  clientId: string;
  document: CanvasDocument;
  store: CanvasEditorStore;
}) {
  const initialDocumentRef = useRef(document);
  useEffect(() => {
    let canceled = false;
    let unsubscribe: (() => void) | undefined;
    let timer: number | undefined;

    const persist = () => {
      const state = store.getState();
      const action =
        state.pendingOperations.length > 0
          ? putCanvasDraft({
              canvas_id: canvasId,
              client_id: clientId,
              base_revision: state.revision,
              graph: state.graph,
              operations: state.pendingOperations,
              updated_at: Date.now(),
            })
          : deleteCanvasDraft(canvasId, clientId);
      void action.catch(() => undefined);
    };

    const setup = async () => {
      const draft = await getCanvasDraft(canvasId, clientId).catch(() => null);
      if (canceled) return;
      if (draft?.operations.length) {
        const initialDocument = initialDocumentRef.current;
        const matchesServer = sameGraph(draft.graph, initialDocument.graph);
        if (matchesServer && draft.base_revision <= initialDocument.revision) {
          await deleteCanvasDraft(canvasId, clientId).catch(() => undefined);
        } else {
          const conflict = draft.base_revision !== initialDocument.revision;
          store.setState({
            graph: draft.graph,
            revision: draft.base_revision,
            pendingOperations: draft.operations,
            history: [],
            future: [],
            saveState: conflict ? "conflict" : "dirty",
            saveMessage: conflict
              ? "检测到基于旧版本的本地草稿，请采用远端或另存副本。"
              : "已恢复未保存的本地草稿。",
          });
        }
      }
      unsubscribe = store.subscribe(() => {
        if (timer !== undefined) window.clearTimeout(timer);
        timer = window.setTimeout(persist, 180);
      });
    };

    void setup();
    return () => {
      canceled = true;
      unsubscribe?.();
      if (timer !== undefined) window.clearTimeout(timer);
      persist();
    };
  }, [canvasId, clientId, store]);
}

function useCanvasTabCoordination({
  canvasId,
  clientId,
  store,
  onRefetch,
}: {
  canvasId: string;
  clientId: string;
  store: CanvasEditorStore;
  onRefetch: () => Promise<unknown>;
}) {
  const channelRef = useRef<BroadcastChannel | null>(null);
  const onRefetchRef = useRef(onRefetch);
  useEffect(() => {
    onRefetchRef.current = onRefetch;
  }, [onRefetch]);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(`lumen:canvas:${canvasId}`);
    channelRef.current = channel;
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const payload = event.data;
      if (!isCanvasSavedBroadcast(payload) || payload.clientId === clientId) {
        return;
      }
      const state = store.getState();
      if (payload.revision <= state.revision) return;
      if (state.pendingOperations.length === 0) {
        void onRefetchRef.current();
        return;
      }
      state.markConflict("另一个标签页已保存更新，本地修改已暂停保存。");
    };
    return () => {
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, [canvasId, clientId, store]);
  return useCallback(
    (revision: number) => {
      channelRef.current?.postMessage({
        type: "canvas.saved",
        clientId,
        revision,
      });
    },
    [clientId],
  );
}

function useCanvasAutosave({
  canvasId,
  clientId,
  pendingCount,
  saveState,
  store,
  notifySaved,
}: {
  canvasId: string;
  clientId: string;
  pendingCount: number;
  saveState: string;
  store: CanvasEditorStore;
  notifySaved: (revision: number) => void;
}): MutableRefObject<SerialAutosave<SavePayload> | null> {
  const autosaveRef = useRef<SerialAutosave<SavePayload> | null>(null);
  useEffect(() => {
    const autosave = new SerialAutosave<SavePayload>({
      delayMs: 750,
      readBatch: () => readCanvasSaveBatch(store),
      sendBatch: async (batch) => {
        store.getState().markSaving();
        const mutationId = randomId();
        try {
          const result = await applyCanvasMutations(canvasId, {
            base_revision: batch.payload.baseRevision,
            client_id: clientId,
            mutation_id: mutationId,
            operations: batch.payload.operations,
          });
          store.getState().acknowledgeOperations(batch.count, result.revision);
          notifySaved(result.revision);
        } catch (error) {
          handleCanvasSaveError(store, error);
          throw error;
        }
      },
      onError: () => undefined,
    });
    autosaveRef.current = autosave;
    const flushOnPageHide = () => void autosave.flush();
    window.addEventListener("pagehide", flushOnPageHide);
    return () => {
      window.removeEventListener("pagehide", flushOnPageHide);
      autosave.stop();
      autosaveRef.current = null;
    };
  }, [canvasId, clientId, notifySaved, store]);
  useEffect(() => {
    if (pendingCount > 0 && saveState === "dirty") {
      autosaveRef.current?.schedule();
    }
  }, [pendingCount, saveState]);
  return autosaveRef;
}

function readCanvasSaveBatch(
  store: CanvasEditorStore,
): AutosaveBatch<SavePayload> | null {
  const state = store.getState();
  if (
    state.pendingOperations.length === 0 ||
    state.saveState === "conflict"
  ) {
    return null;
  }
  if (!canvasGraphReadyToSave(state.graph)) {
    state.markSaveError("画布规模超过当前保存上限，请拆分后重试。");
    return null;
  }
  return {
    count: state.pendingOperations.length,
    payload: {
      baseRevision: state.revision,
      operations: state.pendingOperations.slice(),
    },
  };
}

function handleCanvasSaveError(store: CanvasEditorStore, error: unknown) {
  if (
    error instanceof ApiError &&
    (error.status === 409 || error.code === "canvas_revision_conflict")
  ) {
    store
      .getState()
      .markConflict("版本冲突：远端画布已更新。本地修改仍保留，但自动保存已暂停。");
    return;
  }
  store
    .getState()
    .markSaveError(error instanceof Error ? error.message : "保存失败");
}

function useCanvasKeyboardShortcuts(
  store: CanvasEditorStore,
  viewportApi: CanvasViewportApi | null,
) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) {
        return;
      }
      const modifier = event.metaKey || event.ctrlKey;
      if (modifier && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) store.getState().redo();
        else store.getState().undo();
      }
      if (modifier && event.key === "0") {
        event.preventDefault();
        viewportApi?.fitView();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [store, viewportApi]);
}

function isCanvasSavedBroadcast(
  value: unknown,
): value is { type: "canvas.saved"; clientId: string; revision: number } {
  if (!value || typeof value !== "object") return false;
  const payload = value as Record<string, unknown>;
  return (
    payload.type === "canvas.saved" &&
    typeof payload.clientId === "string" &&
    typeof payload.revision === "number"
  );
}

function sameGraph(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function resolveRunningNodeId(
  isSubmitting: boolean,
  submittingNodeId: string | undefined,
  selectedNodeId: string | null,
  activeNodeIds: Set<string>,
): string | null {
  if (isSubmitting) return submittingNodeId ?? null;
  return selectedNodeId && activeNodeIds.has(selectedNodeId)
    ? selectedNodeId
    : null;
}

function ConflictBanner({
  onAdoptRemote,
  onKeepCopy,
}: {
  onAdoptRemote: () => Promise<void>;
  onKeepCopy: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<"remote" | "copy" | null>(null);
  return (
    <div
      role="alert"
      className="flex shrink-0 flex-wrap items-center gap-2 border-b border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]"
    >
      <span className="min-w-[220px] flex-1">
        版本冲突：远端画布已更新。本地修改仍保留，自动保存已暂停。
      </span>
      <Button
        size="sm"
        variant="secondary"
        loading={busy === "remote"}
        onClick={async () => {
          setBusy("remote");
          try {
            await onAdoptRemote();
          } finally {
            setBusy(null);
          }
        }}
      >
        采用远端
      </Button>
      <Button
        size="sm"
        variant="outline"
        loading={busy === "copy"}
        onClick={async () => {
          setBusy("copy");
          try {
            await onKeepCopy();
          } finally {
            setBusy(null);
          }
        }}
      >
        另存副本
      </Button>
    </div>
  );
}

function browserClientId(canvasId: string): string {
  if (typeof window === "undefined") return `ssr-${canvasId}`;
  const key = `lumen:canvas-client:${canvasId}`;
  const existing = window.sessionStorage.getItem(key);
  if (existing) return existing;
  const value = randomId();
  window.sessionStorage.setItem(key, value);
  return value;
}

function randomId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `canvas-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
