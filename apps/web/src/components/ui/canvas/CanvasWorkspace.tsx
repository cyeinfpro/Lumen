"use client";

import dynamic from "next/dynamic";
import { X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from "react";

import { ApiError } from "@/lib/apiClient";
import { onOnlineRestore, startConnectivity } from "@/lib/connectivity";
import {
  applyCanvasMutations,
  createCanvas,
} from "@/lib/api/canvases";
import {
  RetryableAutosaveBatchReader,
  SerialAutosave,
  takeAutosaveOperations,
  type AutosaveBatch,
} from "@/lib/canvas/autosave";
import {
  canvasGraphReadyToSave,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
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
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import type {
  CanvasDocument,
  CanvasGraph,
  CanvasOperation,
} from "@/lib/canvas/types";
import {
  canvasQueryKeys,
  useCanvasQuery,
  useExecuteCanvasNodeMutation,
  usePatchCanvasMutation,
} from "@/lib/queries/canvases";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { cn } from "@/lib/utils";
import {
  Button,
  ErrorState,
  IconButton,
  Spinner,
  toast,
} from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { CanvasCommandMenu } from "./CanvasCommandMenu";
import { CanvasInspector } from "./CanvasInspector";
import { CanvasNodePalette } from "./CanvasNodePalette";
import { CanvasSelectionToolbar } from "./CanvasSelectionToolbar";
import { CanvasShortcutsDialog } from "./CanvasShortcutsDialog";
import { CanvasStoreProvider, useCanvasStore, useCanvasStoreApi } from "./CanvasStoreProvider";
import { CanvasTopBar } from "./CanvasTopBar";
import type { CanvasViewportApi } from "./CanvasViewport";
import { CanvasMobileToolbar } from "./mobile/CanvasMobileToolbar";
import { useCanvasWorkspaceTools } from "./useCanvasWorkspaceTools";

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
  clientId: string;
  mutationId: string;
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
const AUTO_FIT_NODE_LIMIT = 200;

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
  const queryClient = useQueryClient();
  const isCompact = useMediaQuery("(max-width: 1199px)") !== false;
  const store = useCanvasStoreApi();
  const graph = useCanvasStore((state) => state.graph);
  const revision = useCanvasStore((state) => state.revision);
  const saveState = useCanvasStore((state) => state.saveState);
  const saveMessage = useCanvasStore((state) => state.saveMessage);
  const pendingCount = useCanvasStore((state) => state.pendingOperations.length);
  const inFlightOperationCount = useCanvasStore(
    (state) => state.inFlightOperationCount,
  );
  const retryPrefixOperationCount = useCanvasStore(
    (state) => state.retryPrefixOperationCount,
  );
  const activeInteractionCount = useCanvasStore(
    (state) => state.activeInteractionCount,
  );
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectedNodeIds = useCanvasStore((state) => state.selectedNodeIds);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const [title, setTitle] = useState(document.title);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [viewportApi, setViewportApi] = useState<CanvasViewportApi | null>(null);
  const [durabilityWarning, setDurabilityWarning] = useState<string | null>(
    null,
  );
  const { fullscreen, toggleFullscreen, exitFullscreen } =
    useCanvasFullscreen();
  const [tabId] = useState(randomId);
  const [clientId] = useState(() => browserClientId(canvasId, tabId));
  const recoveredSaveBatchRef =
    useRef<PersistedCanvasSaveBatch | null>(null);
  const submittingNodeIdsRef = useRef(new Set<string>());
  const confirmedTitleRef = useRef(document.title);
  const titleMutationQueueRef = useRef<Promise<void>>(Promise.resolve());
  const titleMutationIdRef = useRef(0);
  const patchCanvas = usePatchCanvasMutation(canvasId);
  const executeNode = useExecuteCanvasNodeMutation(canvasId);
  useCanvasClientLease(canvasId, clientId, tabId);

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

  useRemoteDocumentSync(document, store, inFlightOperationCount);
  useCanvasDraftPersistence({
    canvasId,
    clientId,
    document,
    onDurabilityWarning: setDurabilityWarning,
    recoveredSaveBatchRef,
    store,
  });
  const notifySaved = useCanvasTabCoordination({
    canvasId,
    clientId,
    tabId,
    store,
    onRefetch,
  });
  const handleSaved = useCallback(
    (savedGraph: CanvasGraph, savedRevision: number) => {
      queryClient.setQueryData<CanvasDocument>(
        canvasQueryKeys.detail(canvasId),
        (current) =>
          current && current.revision <= savedRevision
            ? { ...current, graph: savedGraph, revision: savedRevision }
            : current,
      );
      void queryClient.invalidateQueries({ queryKey: canvasQueryKeys.all });
    },
    [canvasId, queryClient],
  );
  const autosaveRef = useCanvasAutosave({
    canvasId,
    clientId,
    pendingCount,
    recoveredSaveBatchRef,
    saveState,
    store,
    notifySaved,
    onDurabilityWarning: setDurabilityWarning,
    onSaved: handleSaved,
  });
  useEffect(() => {
    if (
      !viewportApi ||
      activeInteractionCount > 0 ||
      graph.nodes.length > AUTO_FIT_NODE_LIMIT
    ) {
      return;
    }
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => viewportApi.fitView());
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [
    activeInteractionCount,
    fullscreen,
    graph.nodes.length,
    isCompact,
    viewportApi,
  ]);

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

  const tools = useCanvasWorkspaceTools({
    graph,
    selectedNodeIds,
    selectedEdgeId,
    store,
    viewportApi,
    onRunSelected: runSelected,
  });
  const handleEscape = useCallback(() => {
    setInspectorOpen(false);
    setPaletteOpen(false);
    tools.setCommandMenuOpen(false);
    tools.setShortcutsOpen(false);
    store.getState().setConnectionDraft(null);
    void exitFullscreen();
  }, [exitFullscreen, store, tools]);
  useCanvasKeyboardShortcuts(store, viewportApi, {
    onEscape: handleEscape,
    onOpenCommandMenu: () => tools.openCommandMenu(null),
    onOpenShortcuts: tools.openShortcuts,
    onCopy: tools.copySelection,
    onPaste: tools.pasteSelection,
    onDuplicate: tools.duplicateSelection,
    onAutoLayout: tools.autoLayoutSelection,
    onFitSelection: tools.fitSelection,
    onRunSelected: runSelected,
    onToggleGrid: tools.toggleGrid,
    onToggleMiniMap: () => viewportApi?.toggleMiniMap(),
  });

  const addAtCenter = useCallback(
    (type: Parameters<typeof tools.addNode>[0]) => {
      tools.addNode(type);
      setPaletteOpen(false);
    },
    [tools],
  );
  const renameCanvas = useCallback(
    (nextTitle: string) => {
      const mutationId = titleMutationIdRef.current + 1;
      titleMutationIdRef.current = mutationId;
      setTitle(nextTitle);

      const saveTitle = async () => {
        try {
          const updated = await patchCanvas.mutateAsync({ title: nextTitle });
          confirmedTitleRef.current = updated.title;
          if (titleMutationIdRef.current === mutationId) {
            setTitle(updated.title);
          }
        } catch (error) {
          if (titleMutationIdRef.current === mutationId) {
            setTitle(confirmedTitleRef.current);
          }
          toast.error(error instanceof Error ? error.message : "标题保存失败");
        }
      };
      titleMutationQueueRef.current =
        titleMutationQueueRef.current.then(saveTitle, saveTitle);
    },
    [patchCanvas],
  );

  return (
    <div
      data-app-viewport
      data-canvas-fullscreen={fullscreen || undefined}
      className={cn(
        "relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[var(--bg-0)] text-[var(--fg-0)]",
        fullscreen &&
          "fixed inset-0 z-[calc(var(--z-banner)-1)] h-[100dvh] w-screen",
      )}
    >
      <CanvasTopBar
        title={title}
        saveState={saveState}
        saveMessage={saveMessage}
        onRename={renameCanvas}
        onFitView={() => viewportApi?.fitView()}
        onRunSelected={runSelected}
        onOpenInspector={() => setInspectorOpen(true)}
        onOpenCommandMenu={() => tools.openCommandMenu(null)}
        onOpenShortcuts={tools.openShortcuts}
        onToggleFullscreen={() => void toggleFullscreen()}
        onRetrySave={
          saveState === "error" && retryPrefixOperationCount === 0
            ? undefined
            : () => void autosaveRef.current?.flush()
        }
        fullscreen={fullscreen}
        running={Boolean(runningNodeId)}
      />

      {saveState === "conflict" ? (
        <ConflictBanner
          onAdoptRemote={async () => {
            blurActiveCanvasEditor();
            await onRefetch();
            const fresh = await import("@/lib/api/canvases").then((module) =>
              module.getCanvas(canvasId),
            );
            store.getState().replaceFromRemote(fresh.graph, fresh.revision);
            confirmedTitleRef.current = fresh.title;
            titleMutationIdRef.current += 1;
            setTitle(fresh.title);
          }}
          onKeepCopy={async () => {
            try {
              blurActiveCanvasEditor();
              const copy = await createCanvas({
                title: `${title} 冲突副本`,
                description: document.description ?? "",
                graph: store.getState().graph,
              });
              router.push(`/projects/canvas/${copy.id}`);
            } catch (error) {
              toast.error(error instanceof Error ? error.message : "副本创建失败");
            }
          }}
        />
      ) : null}
      {durabilityWarning && pendingCount > 0 ? (
        <DurabilityBanner message={durabilityWarning} />
      ) : null}

      <div className="grid min-h-0 flex-1 min-[1200px]:grid-cols-[224px_minmax(0,1fr)_320px]">
        <aside className="hidden min-h-0 border-r border-[var(--border)] bg-[var(--bg-1)] min-[1200px]:flex min-[1200px]:flex-col">
          <header className="border-b border-[var(--border)] px-3 py-3">
            <p className="type-page-kicker">节点工具</p>
            <h2 className="type-card-title mt-1">添加节点</h2>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <CanvasNodePalette onAdd={addAtCenter} />
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-col">
          <div className="relative min-h-0 flex-1">
            <CanvasViewport
              document={mergedDocument}
              onRunNode={runNode}
              onReady={setViewportApi}
              onOpenInspector={() => setInspectorOpen(true)}
              onOpenQuickAdd={tools.openQuickAdd}
              onOpenContextMenu={tools.openContextMenu}
            />
            <CanvasSelectionToolbar
              selectedCount={tools.selectedCount}
              onCopy={() => void tools.copySelection()}
              onAlign={tools.alignSelection}
              onDistribute={tools.distributeSelection}
              onAutoLayout={tools.autoLayoutSelection}
              onFitSelection={tools.fitSelection}
              onDelete={tools.deleteSelection}
              className="absolute bottom-3 left-1/2 z-[var(--z-tabbar)] -translate-x-1/2 max-[1199px]:hidden"
            />
          </div>
          <CanvasMobileToolbar
            onAdd={() => setPaletteOpen(true)}
            onFitView={() => viewportApi?.fitView()}
            onOpenCommandMenu={() => tools.openCommandMenu(null)}
          />
        </main>

        <aside className="hidden min-h-0 border-l border-[var(--border)] bg-[var(--bg-1)] min-[1200px]:block">
          <CanvasInspector
            document={mergedDocument}
            onRunNode={runNode}
            runningNodeId={runningNodeId}
            onDuplicateSelection={tools.duplicateSelection}
            onAlignSelection={tools.alignSelection}
            onDistributeSelection={tools.distributeSelection}
            onAutoLayoutSelection={tools.autoLayoutSelection}
            onFitSelection={tools.fitSelection}
          />
        </aside>
      </div>

      <BottomSheet
        open={isCompact && inspectorOpen}
        onClose={() => {
          setInspectorOpen(false);
          store.getState().selectNode(null);
          store.getState().selectEdge(null);
        }}
        ariaLabel="节点检查器"
        snapPoints={["88%"]}
        className="mobile-dialog-sheet"
      >
        <div className="relative h-full min-h-0">
          <IconButton
            aria-label="关闭检查器"
            size="lg"
            onClick={() => {
              setInspectorOpen(false);
              store.getState().selectNode(null);
              store.getState().selectEdge(null);
            }}
            className="absolute right-3 top-3 z-[var(--z-tabbar)]"
          >
            <X className="h-4 w-4" />
          </IconButton>
          <CanvasInspector
            document={mergedDocument}
            onRunNode={runNode}
            runningNodeId={runningNodeId}
            onDuplicateSelection={tools.duplicateSelection}
            onAlignSelection={tools.alignSelection}
            onDistributeSelection={tools.distributeSelection}
            onAutoLayoutSelection={tools.autoLayoutSelection}
            onFitSelection={tools.fitSelection}
          />
        </div>
      </BottomSheet>

      <BottomSheet
        open={isCompact && paletteOpen}
        onClose={() => setPaletteOpen(false)}
        ariaLabel="添加节点"
        snapPoints={["72%"]}
        className="mobile-dialog-sheet"
      >
        <div className="mobile-dialog-scroll h-full overflow-y-auto p-4">
          <div className="mb-4 flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="type-page-kicker">节点工具</p>
              <h2 className="type-section-title mt-1">添加节点</h2>
            </div>
            <IconButton
              aria-label="关闭节点工具"
              size="lg"
              onClick={() => setPaletteOpen(false)}
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
          <CanvasNodePalette onAdd={addAtCenter} compact />
        </div>
      </BottomSheet>

      <CanvasCommandMenu
        open={tools.commandMenuOpen}
        items={tools.commandItems}
        title={tools.commandMenuTitle}
        onOpenChange={tools.setCommandMenuOpen}
        onSelect={tools.handleCommandSelect}
      />
      <CanvasShortcutsDialog
        open={tools.shortcutsOpen}
        onOpenChange={tools.setShortcutsOpen}
      />
    </div>
  );
}

function useRemoteDocumentSync(
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
  }, [
    document.graph,
    document.revision,
    inFlightOperationCount,
    store,
  ]);
}

function useCanvasDraftPersistence({
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
    let lastRevision = store.getState().revision;
    const unsubscribe = store.subscribe((state) => {
      if (!ready) return;
      if (
        state.graph === lastGraph &&
        state.pendingOperations === lastPendingOperations &&
        state.revision === lastRevision
      ) {
        return;
      }
      lastGraph = state.graph;
      lastPendingOperations = state.pendingOperations;
      lastRevision = state.revision;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(persist, 180);
    });

    const setup = async () => {
      try {
        const recovery = await loadCanvasDraftRecovery(
          canvasId,
          clientId,
        );
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
  }, [
    canvasId,
    clientId,
    onDurabilityWarning,
    recoveredSaveBatchRef,
    store,
  ]);
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
    await deleteCanvasSaveBatch(canvasId, draftClientId).catch(
      () => undefined,
    );
  }
  recoveredSaveBatchRef.current = status.recoverableSaveBatch;
  store.setState({
    graph: draft.graph,
    revision: draft.base_revision,
    pendingOperations: draft.operations,
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
    draft.base_revision !== initialDocument.revision &&
    !recoverableSaveBatch;
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

function useCanvasTabCoordination({
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
      channel = new BroadcastChannel(`lumen:canvas:${canvasId}`);
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

function useCanvasAutosave({
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
      readCanvasSaveBatch(
        store,
        clientId,
        randomId(),
        recoveredSaveBatchRef,
      ),
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
          void deleteCanvasSaveBatch(
            canvasId,
            batch.payload.clientId,
          ).catch(
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
    document.addEventListener(
      "visibilitychange",
      flushOnVisibilityChange,
    );
    return () => {
      window.removeEventListener("pagehide", flushOnPageHide);
      document.removeEventListener(
        "visibilitychange",
        flushOnVisibilityChange,
      );
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
    (state.saveState === "error" &&
      state.retryPrefixOperationCount === 0)
  ) {
    return null;
  }
  if (!canvasGraphReadyToSave(state.graph)) {
    state.markSaveError("画布规模超过当前保存上限，请拆分后重试。", false);
    return null;
  }
  const operations = takeAutosaveOperations(state.pendingOperations);
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
  if (
    error instanceof ApiError &&
    error.code === "canvas_revision_conflict"
  ) {
    store
      .getState()
      .markConflict("版本冲突：远端画布已更新。本地修改仍保留，但自动保存已暂停。");
    return "conflict";
  }
  if (error instanceof ApiError && canvasSaveErrorIsBlocked(error.status)) {
    store.getState().markSaveError(
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

function useCanvasKeyboardShortcuts(
  store: CanvasEditorStore,
  viewportApi: CanvasViewportApi | null,
  actions: CanvasShortcutActions,
) {
  const actionsRef = useRef(actions);
  useEffect(() => {
    actionsRef.current = actions;
  }, [actions]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select, [contenteditable='true']") ||
        target?.closest("[role='dialog']")
      ) {
        return;
      }
      const bindings = createCanvasShortcutBindings(
        store,
        viewportApi,
        actionsRef.current,
      );
      const binding = bindings.find((item) => item.matches(event));
      if (!binding) return;
      event.preventDefault();
      binding.run(event);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [store, viewportApi]);
}

interface CanvasShortcutActions {
  onEscape: () => void;
  onOpenCommandMenu: () => void;
  onOpenShortcuts: () => void;
  onCopy: () => void | Promise<unknown>;
  onPaste: () => void | Promise<unknown>;
  onDuplicate: () => void;
  onAutoLayout: () => void;
  onFitSelection: () => void;
  onRunSelected: () => void;
  onToggleGrid: () => void;
  onToggleMiniMap: () => void;
}

interface CanvasShortcutBinding {
  matches: (event: KeyboardEvent) => boolean;
  run: (event: KeyboardEvent) => void;
}

function createCanvasShortcutBindings(
  store: CanvasEditorStore,
  viewportApi: CanvasViewportApi | null,
  actions: CanvasShortcutActions,
): CanvasShortcutBinding[] {
  return [
    binding(modifiedShiftedKey("k"), actions.onOpenCommandMenu),
    binding(modifiedKey("z"), (event) => {
      if (event.shiftKey) store.getState().redo();
      else store.getState().undo();
    }),
    binding(modifiedKey("y"), () => store.getState().redo()),
    binding(modifiedKey("0"), () => viewportApi?.fitView()),
    binding(modifiedKey("a"), () => {
      const state = store.getState();
      state.selectNodes(state.graph.nodes.map((node) => node.id));
    }),
    binding(modifiedKey("c"), () => void actions.onCopy()),
    binding(modifiedKey("v"), () => void actions.onPaste()),
    binding(modifiedKey("d"), actions.onDuplicate),
    binding(
      (event) => hasModifier(event) && event.key === "Enter",
      actions.onRunSelected,
    ),
    binding(shiftedKey("2"), actions.onFitSelection),
    binding(shiftedKey("a"), actions.onAutoLayout),
    binding(
      (event) => !hasModifier(event) && !event.shiftKey && event.key === "/",
      actions.onOpenCommandMenu,
    ),
    binding((event) => event.key === "?", actions.onOpenShortcuts),
    binding(plainKey("g"), actions.onToggleGrid),
    binding(plainKey("m"), actions.onToggleMiniMap),
    binding(
      (event) => event.key === "+" || event.key === "=",
      () => viewportApi?.zoomIn(),
    ),
    binding(plainKey("-"), () => viewportApi?.zoomOut()),
    binding(plainKey("0"), () => viewportApi?.resetZoom()),
    binding(plainKey("escape"), actions.onEscape),
  ];
}

function binding(
  matches: CanvasShortcutBinding["matches"],
  run: CanvasShortcutBinding["run"],
): CanvasShortcutBinding {
  return { matches, run };
}

function modifiedKey(key: string) {
  return (event: KeyboardEvent) =>
    hasModifier(event) && event.key.toLowerCase() === key;
}

function shiftedKey(key: string) {
  return (event: KeyboardEvent) =>
    !hasModifier(event) && event.shiftKey && event.key.toLowerCase() === key;
}

function modifiedShiftedKey(key: string) {
  return (event: KeyboardEvent) =>
    hasModifier(event) && event.shiftKey && event.key.toLowerCase() === key;
}

function plainKey(key: string) {
  return (event: KeyboardEvent) =>
    !hasModifier(event) && !event.shiftKey && event.key.toLowerCase() === key;
}

function hasModifier(event: KeyboardEvent): boolean {
  return event.metaKey || event.ctrlKey;
}

function isCanvasCoordinationBroadcast(
  value: unknown,
): value is
  | { type: "canvas.saved"; clientId: string; revision: number }
  | { type: "canvas.selection.changed"; revision?: number }
  | {
      type: "canvas.presence.ping";
      requestId: string;
      targetClientId: string;
    }
  | {
      type: "canvas.presence.pong";
      requestId: string;
      clientId: string;
    } {
  if (!value || typeof value !== "object") return false;
  const payload = value as Record<string, unknown>;
  if (payload.type === "canvas.selection.changed") return true;
  if (payload.type === "canvas.presence.ping") {
    return (
      typeof payload.requestId === "string" &&
      typeof payload.targetClientId === "string"
    );
  }
  if (payload.type === "canvas.presence.pong") {
    return (
      typeof payload.requestId === "string" &&
      typeof payload.clientId === "string"
    );
  }
  return (
    payload.type === "canvas.saved" &&
    typeof payload.clientId === "string" &&
    typeof payload.revision === "number" &&
    Number.isSafeInteger(payload.revision) &&
    payload.revision >= 0
  );
}

function useCanvasFullscreen() {
  const [fullscreen, setFullscreen] = useState(false);
  const ownsNativeFullscreenRef = useRef(false);

  const exitFullscreen = useCallback(async () => {
    setFullscreen(false);
    if (
      ownsNativeFullscreenRef.current &&
      document.fullscreenElement &&
      typeof document.exitFullscreen === "function"
    ) {
      await document.exitFullscreen().catch(() => undefined);
    }
    ownsNativeFullscreenRef.current = false;
  }, []);

  const toggleFullscreen = useCallback(async () => {
    if (fullscreen) {
      await exitFullscreen();
      return;
    }
    setFullscreen(true);
    const target = document.documentElement;
    if (typeof target.requestFullscreen !== "function") return;
    ownsNativeFullscreenRef.current = true;
    try {
      await target.requestFullscreen();
    } catch {
      ownsNativeFullscreenRef.current = false;
    }
  }, [exitFullscreen, fullscreen]);

  useEffect(() => {
    const onFullscreenChange = () => {
      if (
        fullscreen &&
        ownsNativeFullscreenRef.current &&
        !document.fullscreenElement
      ) {
        ownsNativeFullscreenRef.current = false;
        setFullscreen(false);
      }
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () =>
      document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, [fullscreen]);

  useEffect(() => {
    if (!fullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [fullscreen]);

  return { fullscreen, toggleFullscreen, exitFullscreen };
}

function sameGraph(left: CanvasGraph, right: CanvasGraph): boolean {
  return (
    stableSerialize(comparableGraph(left)) ===
    stableSerialize(comparableGraph(right))
  );
}

function comparableGraph(graph: CanvasGraph): CanvasGraph {
  return {
    ...graph,
    nodes: graph.nodes.map((node) => ({
      ...node,
      parent_group_id: node.parent_group_id ?? null,
      size: node.size ?? undefined,
      config: {
        ...CANVAS_NODE_SPECS[node.type].defaultConfig,
        ...node.config,
      },
      ui: {
        collapsed: node.ui?.collapsed === true,
        color_tag: node.ui?.color_tag ?? null,
      },
    })),
    edges: graph.edges.map((edge) => ({
      ...edge,
      pinned_execution_id: edge.pinned_execution_id ?? null,
      pinned_output_index: edge.pinned_output_index ?? null,
      role: edge.role ?? null,
      order: edge.order ?? null,
    })),
  };
}

function stableSerialize(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map(
        (key) =>
          `${JSON.stringify(key)}:${stableSerialize(record[key])}`,
      )
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
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
        disabled={busy !== null}
        onClick={async () => {
          setBusy("remote");
          try {
            await onAdoptRemote();
          } catch (error) {
            toast.error(
              error instanceof Error ? error.message : "采用远端版本失败",
            );
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
        disabled={busy !== null}
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

function DurabilityBanner({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="shrink-0 border-b border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 type-caption text-[var(--fg-1)]"
    >
      {message}
    </div>
  );
}

const CANVAS_CLIENT_LEASE_TTL_MS = 120_000;
const CANVAS_SUSPENDED_CLIENT_LEASE_TTL_MS = 30 * 60_000;
const CANVAS_PRESENCE_PROBE_TIMEOUT_MS = 600;

interface CanvasClientLease {
  tabId: string;
  updatedAt: number;
  state: "active" | "suspended";
}

function useCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
) {
  useEffect(() => {
    const refresh = () =>
      writeCanvasClientLease(
        canvasId,
        clientId,
        tabId,
        document.visibilityState === "hidden" ? "suspended" : "active",
      );
    const clear = () => clearCanvasClientLease(canvasId, clientId, tabId);
    const handlePageHide = (event: PageTransitionEvent) => {
      if (event.persisted) {
        writeCanvasClientLease(
          canvasId,
          clientId,
          tabId,
          "suspended",
        );
        return;
      }
      clear();
    };
    refresh();
    const heartbeat = window.setInterval(refresh, 15_000);
    window.addEventListener("pagehide", handlePageHide);
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(heartbeat);
      window.removeEventListener("pagehide", handlePageHide);
      window.removeEventListener("pageshow", refresh);
      document.removeEventListener("visibilitychange", refresh);
      clear();
    };
  }, [canvasId, clientId, tabId]);
}

function browserClientId(canvasId: string, tabId: string): string {
  if (typeof window === "undefined") return `ssr-${canvasId}`;
  const key = `lumen:canvas-client:${canvasId}`;
  let existing: string | null = null;
  try {
    existing = window.sessionStorage.getItem(key);
  } catch {
    return randomId();
  }
  const lease = existing ? readCanvasClientLease(canvasId, existing) : null;
  const value =
    existing &&
    !(
      lease &&
      lease.tabId !== tabId &&
      canvasClientLeaseIsFresh(lease)
    )
      ? existing
      : randomId();
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    return value;
  }
  writeCanvasClientLease(canvasId, value, tabId);
  return value;
}

async function canvasClientLeaseIsActive(
  canvasId: string,
  clientId: string,
): Promise<boolean> {
  const lease = readCanvasClientLease(canvasId, clientId);
  if (lease && canvasClientLeaseIsFresh(lease)) {
    return true;
  }
  const presence = await probeCanvasClientPresence(canvasId, clientId);
  if (presence !== null) return presence;
  return lease === undefined;
}

function probeCanvasClientPresence(
  canvasId: string,
  targetClientId: string,
): Promise<boolean | null> {
  if (typeof BroadcastChannel === "undefined") return Promise.resolve(null);
  let channel: BroadcastChannel;
  try {
    channel = new BroadcastChannel(`lumen:canvas:${canvasId}`);
  } catch {
    return Promise.resolve(null);
  }
  const requestId = randomId();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (active: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      channel.close();
      resolve(active);
    };
    const timer = window.setTimeout(
      () => finish(false),
      CANVAS_PRESENCE_PROBE_TIMEOUT_MS,
    );
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const payload = event.data;
      if (
        isCanvasCoordinationBroadcast(payload) &&
        payload.type === "canvas.presence.pong" &&
        payload.requestId === requestId &&
        payload.clientId === targetClientId
      ) {
        finish(true);
      }
    };
    try {
      channel.postMessage({
        type: "canvas.presence.ping",
        requestId,
        targetClientId,
      });
    } catch {
      finish(false);
    }
  });
}

function readCanvasClientLease(
  canvasId: string,
  clientId: string,
): CanvasClientLease | null | undefined {
  try {
    const raw = window.localStorage.getItem(
      canvasClientLeaseKey(canvasId, clientId),
    );
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<CanvasClientLease>;
    return typeof value.tabId === "string" &&
      typeof value.updatedAt === "number"
      ? {
          tabId: value.tabId,
          updatedAt: value.updatedAt,
          state: value.state === "suspended" ? "suspended" : "active",
        }
      : null;
  } catch {
    return undefined;
  }
}

function writeCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
  state: CanvasClientLease["state"] = "active",
) {
  try {
    window.localStorage.setItem(
      canvasClientLeaseKey(canvasId, clientId),
      JSON.stringify({ tabId, updatedAt: Date.now(), state }),
    );
  } catch {
    // Draft persistence still works without cross-tab lease discovery.
  }
}

function canvasClientLeaseIsFresh(
  lease: CanvasClientLease,
  now = Date.now(),
): boolean {
  const ttl =
    lease.state === "suspended"
      ? CANVAS_SUSPENDED_CLIENT_LEASE_TTL_MS
      : CANVAS_CLIENT_LEASE_TTL_MS;
  return now - lease.updatedAt < ttl;
}

function clearCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
) {
  try {
    const key = canvasClientLeaseKey(canvasId, clientId);
    const lease = readCanvasClientLease(canvasId, clientId);
    if (lease?.tabId === tabId) window.localStorage.removeItem(key);
  } catch {
    // Ignore storage restrictions during teardown.
  }
}

function canvasClientLeaseKey(canvasId: string, clientId: string): string {
  return `lumen:canvas-lease:${canvasId}:${clientId}`;
}

function randomId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `canvas-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
